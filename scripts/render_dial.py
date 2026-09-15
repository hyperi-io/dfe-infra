#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/render_dial.py
#  Purpose:      Render the deployment dial (k8s slice) into the DFE_* env file
#                dfe-ops consumes, and print the derived `dfe-ops cycle` line.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Render the deployment dial into the DFE_* env file, for the k8s substrate.

Two renders, one dial. ``--tofu`` writes the tfvars file the cloud roots read
(``terraform/environments/<cloud>/dial.auto.tfvars.json``) from the dial's
``target.provision`` block and the cloud-provisioning section beside it; the
default render writes the DFE_* env file bootstrap and dfe-ops read. The tofu
roots receive values and compute none, so everything they know arrives here.

The deployment dial (``deployment.yaml``) is the single SSoT a deployment turns:
one file the whole automation reads, authored by hand today and populated by the
QA GUI wizard later. This is the k8s SIBLING of dfe-docker's ``render_dial.py``.
It renders the ``k8s:`` slice of the CANONICAL SUPERSET dial (whose schema +
example live in THIS repo, ``deployment.example.yaml``) into the flat DFE_* env
file ``dfe-ops`` already consumes (``bootstrap/.env``), then prints the derived
``dfe-ops cycle`` invocation with --mode / --stack / --registry taken from the
dial. So a redeploy is a dial edit plus one printed command -- dfe-ops itself is
the orchestrator, unchanged; the dial just feeds it.

    dial  ->  DFE_* env  ->  dfe-ops cycle  ->  running stack + E2E + teardown

The env file is SEEDED from ``bootstrap/local.env.example`` on first render (so
every DFE_* key + its comments are present), then the dial's non-empty values
are merged in place over it. Estate ENDPOINTS and SECRETS stay blank in the
committed template -- hyperi-infra's thin caller injects them from OpenBao at
deploy time, or an operator fills the copied ``.env`` by hand. This renderer
never reads or writes a secret.

Dependency-free (no PyYAML) and stdlib only, matching the dfe-ops rule -- the
dial's k8s slice is scalar / nested-map only, so scripts/yaml_subset.py reads it.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from yaml_subset import YamlSubsetError, at, split_list
from yaml_subset import parse as _parse_yaml_subset

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAL = REPO_ROOT / "deployment.yaml"
DIAL_TEMPLATE = REPO_ROOT / "deployment.example.yaml"
ENV_FILE = REPO_ROOT / "bootstrap" / ".env"
ENV_TEMPLATE = REPO_ROOT / "bootstrap" / "local.env.example"
TOFU_ROOTS = REPO_ROOT / "terraform" / "environments"
VERSIONS_FILE = REPO_ROOT / "versions.yaml"
TFVARS_NAME = "dial.auto.tfvars.json"

# (dial path) -> DFE_* env key. These are the flat env-file keys dfe-ops reads
# (bootstrap/local.env.example is the SSoT for the vocabulary). The three deploy
# levers the dial also carries -- profile, version.pin, registry -- are NOT here:
# dfe-ops takes them as --mode / --stack / --registry flags (cycle forces
# DFE_PROFILE from --mode), so they are printed as the derived command instead.
_ENV_MAP: tuple[tuple[tuple[str, ...], str], ...] = (
    (("target", "existing", "namespace"), "DFE_NAMESPACE"),
    (("target", "existing", "clusterRef"), "DFE_KUBE_CONTEXT"),
    (("k8s", "env"), "DFE_ENV"),
    (("k8s", "cloud"), "DFE_CLOUD"),
    (("k8s", "region"), "DFE_REGION"),
    (("k8s", "domain"), "DFE_DOMAIN"),
    (("k8s", "storage_class"), "DFE_STORAGE_CLASS"),
    (("k8s", "repo_url"), "DFE_REPO_URL"),
    (("k8s", "target_revision"), "DFE_TARGET_REVISION"),
    (("k8s", "workload_identity_annotations"), "DFE_WORKLOAD_IDENTITY_ANNOTATIONS"),
    (("endpoints", "clickhouse_host"), "DFE_CLICKHOUSE_HOST"),
    (("endpoints", "kafka_bootstrap"), "DFE_KAFKA_BOOTSTRAP"),
    (("endpoints", "otel_endpoint"), "DFE_OTEL_ENDPOINT"),
    (("endpoints", "vault_addr"), "DFE_VAULT_ADDR"),
    (("retention", "default_ttl_days"), "DFE_CLICKHOUSE_DEFAULT_TTL_DAYS"),
    # Which store body bootstrap renders, and which variables it then demands:
    # openbao takes an address and an AppRole, aws-sm takes neither.
    (("secrets", "backend"), "DFE_SECRETS_BACKEND"),
    # The in-cluster toolbox pod's own dial facts (helm/charts/dfe-toolbox),
    # carried through unchanged: they ARE the chart's own toolbox.pod.* values
    # (real YAML booleans, an empty-or-numeric string), not a render_dial.py
    # translation, the same way ui.* is left for a real YAML/Helm parse to
    # type-check. bootstrap.sh carries these onto the cluster-secret
    # annotations the same way it already does for the karpenter-pools
    # facts, and argocd/appsets/layer2-platform.yaml reads them back for
    # this chart alone.
    (("toolbox", "pod", "enabled"), "DFE_TOOLBOX_POD_ENABLED"),
    (("toolbox", "pod", "kubeApiAccess"), "DFE_TOOLBOX_POD_KUBE_API_ACCESS"),
    (("toolbox", "pod", "ttlSeconds"), "DFE_TOOLBOX_POD_TTL_SECONDS"),
    # The edge module's whole-module switch, carried onto the cluster secret as
    # both a label and an annotation because an ApplicationSet cluster selector
    # matches labels only. Unset here, bootstrap.sh defaults the module on.
    (("edge", "enabled"), "DFE_EDGE_ENABLED"),
)

# An env assignment, live (`KEY=`) or hash-commented (`# KEY=`). The env file's
# keys are DFE_*, KUBECONFIG, READINESS_TIMEOUT -- all [A-Z][A-Z0-9_]*.
_SETTING_RE = re.compile(r"^[ \t]*(?:#[ \t]?)?(?P<key>[A-Z][A-Z0-9_]*)[ \t]*=")


def _scalar(dial: dict[str, object], path: tuple[str, ...]) -> str | None:
    """Return the non-empty scalar at ``path`` in the parsed dial, else None."""
    node = at(dial, path)
    return node.strip() if isinstance(node, str) and node.strip() else None


def _node(dial: dict[str, object], path: tuple[str, ...]) -> dict[str, object]:
    """Return the mapping at ``path``, or an empty one when absent or not a map.

    A committed dial writes an empty map as the scalar ``{}`` (the restricted
    YAML reader hands that back as a string, not a dict), so that spelling
    counts as empty too, the same as no key at all.
    """
    node = at(dial, path)
    if isinstance(node, str) and node.strip() in ("", "{}"):
        return {}
    return node if isinstance(node, dict) else {}


def _env_updates(dial: dict[str, object]) -> dict[str, str]:
    """Map the dial's k8s fields to the DFE_* keys they set (empty skipped)."""
    updates: dict[str, str] = {}
    for path, env_key in _ENV_MAP:
        value = _scalar(dial, path)
        if value is not None:
            updates[env_key] = value
    # DFE_REGISTRY_HOST is the pull-secret host -- the host part of the registry
    # (the full registry+path is the --registry flag on the derived command).
    registry = _scalar(dial, ("registry",))
    if registry:
        updates["DFE_REGISTRY_HOST"] = registry.split("/", 1)[0]
    return updates


def _merge_env(env_path: Path, updates: dict[str, str]) -> None:
    """Overwrite each mapped key in the env file with the dial value.

    Every OTHER line -- the example's blanks, comments, untouched settings --
    survives verbatim, so the estate secrets/endpoints stay present-but-blank for
    the operator (or the thin caller) to fill. A mapped key is replaced in place;
    a genuinely new key is appended under a labelled header.
    """
    remaining = dict(updates)
    out: list[str] = []
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _SETTING_RE.match(line)
        key = match.group("key") if match else None
        if key in updates:
            if key in remaining:
                out.append(f'{key}="{remaining.pop(key)}"')
            continue
        out.append(line)

    if remaining:
        out.append("")
        out.append("## Set by render_dial.py from deployment.yaml -- do not edit by hand.")
        out.extend(f'{key}="{value}"' for key, value in remaining.items())

    with env_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(out) + "\n")


def _derived_command(dial: dict[str, object], env_path: Path) -> str:
    """Build the `dfe-ops cycle` invocation the dial implies (mode/stack/registry)."""
    mode = _scalar(dial, ("profile",)) or "single"
    stack = _scalar(dial, ("version", "pin")) or ""
    registry = _scalar(dial, ("registry",)) or ""
    rel_env = env_path.relative_to(REPO_ROOT) if env_path.is_relative_to(REPO_ROOT) else env_path
    parts = ["python3 scripts/dfe-ops cycle", f"--mode {mode}"]
    if stack:
        parts.append(f"--stack {stack}")
    if registry:
        parts.append(f"--registry {registry}")
    parts.append(f"--env-file {rel_env}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# The tofu render
# ---------------------------------------------------------------------------

# Which `platform:` key in versions.yaml states the control-plane floor for a
# cloud. The stage arrives with dfe-infra#285; until then the dial carries the
# version and this lookup finds nothing.
_PLATFORM_KEY = {"aws": "eks", "gcp": "gke", "azure": "aks"}


class DialError(ValueError):
    """A dial the tofu render cannot turn into a tfvars file."""


def _seeds(kafka_provider: str) -> dict[str, dict[str, str]]:
    """The credentials the deploy layer puts in the store, for this broker.

    An empty value is generated -- in the secrets module, or in the root for a
    password a managed broker is created with -- and never surfaces, so neither
    the dial nor the rendered tfvars ever carries one. The kafka key's last
    segment is the provider, which is what the kafka chart reads back.
    """
    return {
        f"kafka/{kafka_provider}": {"password": ""},
        "ui/nextauth": {"secret": ""},
    }


def _text(dial: dict[str, object], path: tuple[str, ...], default: str = "") -> str:
    """Return the scalar at ``path``, or ``default`` when it is absent or blank."""
    value = _scalar(dial, path)
    return default if value is None else value


def _required(dial: dict[str, object], path: tuple[str, ...]) -> str:
    """Return the scalar at ``path``, refusing an absent or blank one by name."""
    value = _scalar(dial, path)
    if value is None:
        raise DialError(f"the dial sets no {'.'.join(path)}")
    return value


def _flag(dial: dict[str, object], path: tuple[str, ...], default: bool = False) -> bool:
    """Read a true/false dial field. The dial writes them as strings, like `steps`."""
    value = _scalar(dial, path)
    if value is None:
        return default
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise DialError(f"{'.'.join(path)} must be true or false, got {value!r}")


# Every ui.* boolean, and the chart default it takes when the dial omits it
# (envoy-gateway-config/values.yaml). This renderer writes no DFE_UI_* env key
# today -- the dial's ui: block is copied by hand into a real Helm values
# overlay (docs/deployment/aws.md) -- but a bad value should still be refused
# here, by name, before an operator carries it forward. deployment.example.yaml
# writes this block's booleans unquoted, unlike the rest of the dial, for the
# same reason: a value copied verbatim into a real Helm values file needs to
# already be the type that file expects.
_UI_BOOL_DEFAULTS: dict[tuple[str, ...], bool] = {
    ("ui", "public", "dfe_ui"): True,
    ("ui", "public", "kafbat"): False,
    ("ui", "public", "cruise_control"): False,
    ("ui", "public", "hyperdx"): False,
    ("ui", "public", "argocd"): False,
    ("ui", "public", "links"): False,
    ("ui", "rate_limit", "enabled"): True,
    ("ui", "tls", "hsts"): True,
}


def _ui_flags(dial: dict[str, object]) -> dict[str, bool]:
    """Validate every `ui:` boolean the same way `_flag()` guards `endpoint.public`.

    Returns each field keyed by its dotted path (e.g. "ui.public.kafbat"), so a
    caller can report which UIs the dial marks public without re-deriving the
    path list.
    """
    return {
        ".".join(path): _flag(dial, path, default)
        for path, default in _UI_BOOL_DEFAULTS.items()
    }


# The receiver's own exposure.mode vocabulary (dfe-receiver/values.yaml).
INGEST_MODES = ("public", "internal", "vpn")

# Where the cloud overlays live, read rather than restated: a hardcoded list of
# "the clouds that default to vpn" drifts the moment one overlay changes, and
# the drift shows up as a summary line that contradicts what deployed.
CLOUD_VALUES = REPO_ROOT / "argocd" / "values"

# What each mode costs in kind, for the printed summary line.
_INGEST_MODE_NOTE: dict[str, str] = {
    "public": "LoadBalancer, billed per GB processed -- costed opt-in",
    "internal": "routed through the cluster Gateway, still a LoadBalancer on most clouds",
    "vpn": "no load balancer",
}


def _overlay_ingest_mode(cloud: str) -> str | None:
    """The exposure.mode argocd/values/<cloud>.yaml sets, or None when it sets none."""
    if not cloud:
        return None
    overlay = CLOUD_VALUES / f"{cloud}.yaml"
    try:
        text = overlay.read_text(encoding="utf-8")
    except OSError:
        return None
    in_block = False
    for line in text.splitlines():
        if re.match(r"^exposure:\s*(#.*)?$", line):
            in_block = True
            continue
        if in_block:
            if line.strip() and not line.startswith((" ", "\t", "#")):
                in_block = False
                continue
            match = re.match(r"^\s+mode:\s*([A-Za-z-]+)", line)
            if match:
                return match.group(1)
    return None


def _ingest_mode(dial: dict[str, object]) -> str:
    """Validate ingest.mode the same way _flag() guards a boolean field.

    Anything outside the receiver's own vocabulary is refused by name before an
    operator carries it into a real Helm values overlay. This renderer writes no
    DFE_INGEST_* key and applies nothing: the dial's `ingest:` block is copied by
    hand into a values overlay under `exposure:`, the same convention `ui:`
    follows above. A dial with no ingest: block therefore reports whatever the
    cloud overlay sets, and the chart's own default (public) when it sets none.
    """
    cloud = _text(dial, ("k8s", "cloud"))
    fallback = _overlay_ingest_mode(cloud) or "public"
    value = _text(dial, ("ingest", "mode"), fallback)
    if value not in INGEST_MODES:
        raise DialError(f"ingest.mode must be one of {', '.join(INGEST_MODES)}, got {value!r}")
    return value


def _number(dial: dict[str, object], path: tuple[str, ...]) -> int:
    """Read a whole-number dial field, refusing anything else by name."""
    value = _required(dial, path)
    if not value.isdigit():
        raise DialError(f"{'.'.join(path)} must be a whole number, got {value!r}")
    return int(value)


def _az_count(dial: dict[str, object]) -> int:
    """How many availability zones the VPC spans. Empty takes 3, the usual spread."""
    raw = _scalar(dial, ("network", "az_count"))
    if raw is None:
        return 3
    if not raw.isdigit():
        raise DialError(f"network.az_count must be a whole number, got {raw!r}")
    value = int(raw)
    if value < 2 or value > 6:
        raise DialError(f"network.az_count must be between 2 and 6, got {value}")
    return value


def _platform_version(cloud: str) -> str | None:
    """The control-plane FLOOR versions.yaml states for this cloud, if any.

    The `platform:` stage names the oldest control-plane minor a cluster must
    clear, not a version to install -- see _kubernetes_version, which is what
    turns this floor and the dial's own choice into the version that runs.
    Absent for a cloud with no stage yet, in which case the dial's own field
    is all there is.
    """
    if not VERSIONS_FILE.is_file():
        return None
    try:
        tree = _parse_yaml_subset(
            VERSIONS_FILE.read_text(encoding="utf-8", errors="replace"),
            source=str(VERSIONS_FILE),
        )
    except YamlSubsetError:
        return None
    current = _scalar(tree, ("current",))
    if current is None:
        return None
    for key in (_PLATFORM_KEY.get(cloud, cloud), "kubernetes"):
        value = _scalar(tree, ("stacks", current, "platform", key))
        if value is not None:
            return value.removeprefix(">=").strip()
    return None


def _version_at_least(version: str, floor: str) -> bool:
    """True when dotted-numeric *version* is the same as or newer than *floor*."""
    return tuple(int(part) for part in version.split(".")) >= tuple(
        int(part) for part in floor.split(".")
    )


def _kubernetes_version(dial: dict[str, object], cloud: str) -> str:
    """The Kubernetes version to provision.

    versions.yaml's `platform` stage is a FLOOR a cluster must clear, not a
    pin that silently replaces the dial's own choice: a dial version that
    clears the floor wins, one that falls short is refused by name (so the
    mismatch is visible before a plan runs against an unsupported control
    plane), and a dial naming no version at all takes the floor's minimum.
    A cloud with no platform stage yet falls back to requiring the dial's own
    field, as before that stage existed.
    """
    floor = _platform_version(cloud)
    if floor is None:
        return _required(dial, ("kubernetes_version",))
    version = _scalar(dial, ("kubernetes_version",))
    if version is None:
        return floor
    if _version_at_least(version, floor):
        return version
    raise DialError(
        f"kubernetes_version {version} is below the platform floor {floor} for {cloud}"
    )


MANAGED_KAFKA_PROVIDERS = ("msk", "confluent-cloud", "redpanda-cloud")

# Where the KRaft metadata quorum runs (helm/charts/kafka's controllerPool).
CONTROLLER_POOLS = ("combined", "separate")


def _controller_pool(dial: dict[str, object]) -> str:
    """Validate kafka.controller_pool the same way _ingest_mode() guards its own.

    Anything outside the chart's own vocabulary is refused by name before a
    resolve carries it into a values fragment. combined is the default because
    it is the chart's, and sizing/sizing.yaml locks the answer as
    ``controller_mode`` so moving it on a live cluster needs ``--migrate``.
    """
    value = _text(dial, ("kafka", "controller_pool"), "combined")
    if value not in CONTROLLER_POOLS:
        raise DialError(
            f"kafka.controller_pool must be one of {', '.join(CONTROLLER_POOLS)}, got {value!r}"
        )
    return value


def _landing_topics(dial: dict[str, object]) -> dict[str, dict[str, int]]:
    """Topics tofu must pre-create for a managed body with no bootstrap Job.

    msk's landing topics are chart work -- its own in-cluster bootstrap Job
    creates the same topics the Strimzi path does, from the SAME chart values,
    so this is never read for msk. confluent-cloud and redpanda-cloud have no
    such Job (CONTRACT.md: "the vendor's provider writes the ACLs and the
    topics itself"), so tofu creates them here or dfe-loader crash-loops on a
    bare deploy -- it treats a missing ``*_land`` topic as fatal.
    """
    topics = _node(dial, ("kafka", "landing_topics"))
    built: dict[str, dict[str, int]] = {}
    for name, body in topics.items():
        if isinstance(body, str):
            if body.strip() not in ("", "{}"):
                raise DialError(f"kafka.landing_topics.{name} is a mapping, not {body!r}")
            body = {}
        elif not isinstance(body, dict):
            raise DialError(f"kafka.landing_topics.{name} must be a mapping")
        entry: dict[str, int] = {}
        for field in ("partitions", "retention_ms"):
            raw = _scalar(body, (field,))
            if raw is None:
                continue
            if not raw.isdigit():
                raise DialError(f"kafka.landing_topics.{name}.{field} must be a whole number, got {raw!r}")
            entry[field] = int(raw)
        built[name] = entry
    return built


def _msk_autoscaling(dial: dict[str, object]) -> dict[str, object]:
    """kafka.msk.autoscaling -- MSK's broker-count scaler, `var.autoscaling` for
    the msk module (terraform/modules/managed-kafka/msk/variables.tf).

    Every field the root declares is already `optional(..., default)` with the
    SAME default this reads from the dial's own comment, so a field the dial
    leaves blank is OMITTED here rather than guessed at twice -- the root's own
    default applies, and only a value an operator actually set overrides it.
    `step` has no root-level default at all (the module derives it from the
    subnet count when unset), so it is never invented here either.
    """
    at = ("kafka", "msk", "autoscaling")
    out: dict[str, object] = {}
    enabled = _scalar(dial, (*at, "enabled"))
    if enabled is not None:
        out["enabled"] = _flag(dial, (*at, "enabled"))
    for field_name in ("max_brokers", "step", "per_broker_capacity_mb_s"):
        raw = _scalar(dial, (*at, field_name))
        if raw is None:
            continue
        if not raw.isdigit():
            raise DialError(f"kafka.msk.autoscaling.{field_name} must be a whole number, got {raw!r}")
        out[field_name] = int(raw)
    headroom = _scalar(dial, (*at, "headroom"))
    if headroom is not None:
        try:
            out["headroom"] = float(headroom)
        except ValueError as err:
            raise DialError(
                f"kafka.msk.autoscaling.headroom must be a number, got {headroom!r}"
            ) from err
    return out


def _optional_number(dial: dict[str, object], path: tuple[str, ...], default: int) -> int:
    """Read a whole-number dial field, or `default` when absent or blank."""
    raw = _scalar(dial, path)
    if raw is None:
        return default
    if not raw.isdigit():
        raise DialError(f"{'.'.join(path)} must be a whole number, got {raw!r}")
    return int(raw)


# Tool names toolbox/aws's user-data installs at a versions.yaml-pinned
# version, read from the `toolbox:` stage docker/dfe-toolbox's own
# Dockerfiles pin from too (see _toolbox_tool_versions). jq, kcat and openssl
# are deliberately absent from this stage (versions.yaml's own comment: no
# upstream release cadence worth tracking), and clickhouse-client/psql are
# NOT here either -- they come from the existing services.clickhouse-version
# / services.postgresql pins instead, so the debugging client always matches
# the server it debugs.
_TOOLBOX_TOOLS = (
    "kubectl", "helm", "argocd-cli", "tofu", "yq", "aws-cli",
    "aws-session-manager-plugin",
)


def _toolbox_tool_versions() -> dict[str, str]:
    """The pinned tool versions terraform/modules/toolbox/aws's user-data
    installs, read from versions.yaml -- never a literal in this file or the
    module. Returns whatever is present, possibly empty: an environment whose
    versions.yaml carries no `toolbox:` stage at all degrades gracefully the
    same way _platform_version() does for a stage that has not landed -- the
    module's OWN variable validation is what refuses an enabled toolbox until
    every key is present, not this function.
    """
    if not VERSIONS_FILE.is_file():
        return {}
    try:
        tree = _parse_yaml_subset(
            VERSIONS_FILE.read_text(encoding="utf-8", errors="replace"),
            source=str(VERSIONS_FILE),
        )
    except YamlSubsetError:
        return {}
    current = _scalar(tree, ("current",))
    if current is None:
        return {}

    versions: dict[str, str] = {}
    for tool in _TOOLBOX_TOOLS:
        value = _scalar(tree, ("stacks", current, "toolbox", tool))
        if value is not None:
            versions[tool] = value

    clickhouse = _scalar(tree, ("stacks", current, "services", "clickhouse-version"))
    if clickhouse is not None:
        versions["clickhouse-client"] = clickhouse
    postgres = _scalar(tree, ("stacks", current, "services", "postgresql"))
    if postgres is not None:
        versions["psql"] = postgres
    return versions


def _toolbox(dial: dict[str, object]) -> dict[str, object]:
    """toolbox.* -- the on-demand SSM-managed troubleshooting instance
    (terraform/modules/toolbox/aws/CONTRACT.md). `enabled` is the
    `dfe-ops bastion up`/`down` toggle. `tool_versions` is NEVER read from
    the dial -- it is assembled from versions.yaml here, so a deployer
    cannot pin a stale tool by hand independent of the SSoT everything else
    in this repo pins through.
    """
    return {
        "enabled": _flag(dial, ("toolbox", "enabled")),
        "aws": {
            "instance_type": _text(dial, ("toolbox", "aws", "instance_type"), "t4g.small"),
            "operator_role_arn": _text(dial, ("toolbox", "aws", "operator_role_arn")),
        },
        "ttl_minutes": _optional_number(dial, ("toolbox", "ttl_minutes"), 60),
        "session": {
            "idle_timeout_minutes": _optional_number(
                dial, ("toolbox", "session", "idle_timeout_minutes"), 15
            ),
            "max_duration_minutes": _optional_number(
                dial, ("toolbox", "session", "max_duration_minutes"), 240
            ),
        },
        "session_log_retention_days": _optional_number(
            dial, ("toolbox", "session_log_retention_days"), 90
        ),
        "tool_versions": _toolbox_tool_versions(),
    }


def _kafka(dial: dict[str, object]) -> dict[str, object]:
    """Who runs the brokers, and what tofu creates when the root builds the body.

    strimzi and redpanda run inside the cluster, so the root builds nothing and
    no kafka block below is rendered at all. The three managed bodies --
    msk, confluent-cloud, redpanda-cloud -- apply the SAME canonical tuning
    (num_partitions, log_retention_ms, message_max_bytes:
    terraform/modules/managed-kafka/CONTRACT.md), read from kafka.msk below
    whichever one is selected; none of them is defaulted here, because a
    defaulted value rots into a stale one silently. Only msk's shape, broker
    count, version, SCRAM user and bootstrap Job are msk-only -- the two SaaS
    bodies size, version and tune themselves. landing_topics is read for the
    two SaaS bodies only; msk's bootstrap_job above covers the same ground.
    """
    provider = _required(dial, ("kafka", "provider"))
    if provider not in MANAGED_KAFKA_PROVIDERS:
        return {"provider": provider}

    at = ("kafka", "msk")
    tuning = {
        "num_partitions": _number(dial, (*at, "num_partitions")),
        "log_retention_ms": _number(dial, (*at, "log_retention_ms")),
        "message_max_bytes": _number(dial, (*at, "message_max_bytes")),
    }

    if provider != "msk":
        landing_topics = _landing_topics(dial)
        if not landing_topics:
            raise DialError(
                f"kafka.provider is {provider!r}, which has no bootstrap Job of its own, so "
                f"kafka.landing_topics must name at least one topic -- an empty map ships a "
                f"cluster dfe-loader crash-loops against"
            )
        return {
            "provider": provider,
            **tuning,
            "landing_topics": landing_topics,
        }

    return {
        "provider": provider,
        "msk": {
            "shape_ref": _required(dial, (*at, "shape_ref")),
            "broker_count": _number(dial, (*at, "broker_count")),
            "broker_version": _required(dial, (*at, "broker_version")),
            **tuning,
            "scram_username": _required(dial, (*at, "scram_username")),
            "bootstrap_job": {
                "namespace": _required(dial, (*at, "bootstrap_job", "namespace")),
                "service_account": _required(dial, (*at, "bootstrap_job", "service_account")),
            },
            "autoscaling": _msk_autoscaling(dial),
        },
    }


def _telemetry(dial: dict[str, object], cloud: str) -> dict[str, object]:
    """The telemetry dial for this cloud, defaulted to DFE's own policy.

    DFE's monitoring goes to its own OTel feed and HyperDX, never CloudWatch,
    so sink defaults to otel -- cloudwatch is the opt-in AWS-native path.
    retention_days defaults to 2 under otel (bounding an S3 lifecycle for the
    touchpoints AWS forces into CloudWatch anyway) and 7 under cloudwatch (an
    AWS-native compliance path, kept short rather than left at a service
    default). Read from telemetry.<cloud> so a future gcp or azure root carries
    its own block rather than sharing one.
    """
    sink = _text(dial, ("telemetry", cloud, "sink"), "otel")
    if sink not in ("otel", "cloudwatch"):
        raise DialError(f"telemetry.{cloud}.sink must be otel or cloudwatch, got {sink!r}")

    default_retention = 2 if sink == "otel" else 7
    retention_raw = _scalar(dial, ("telemetry", cloud, "retention_days"))
    if retention_raw is None:
        retention_days = default_retention
    elif retention_raw.isdigit():
        retention_days = int(retention_raw)
    else:
        raise DialError(f"telemetry.{cloud}.retention_days must be a whole number, got {retention_raw!r}")

    return {"sink": sink, "retention_days": retention_days}


LIFECYCLE_VALUES = ("ephemeral", "persistent")


def _tags(dial: dict[str, object], cloud: str) -> dict[str, str]:
    """The governance tag set. Six come from the dial; iac-source names the root."""
    keys = (
        "service-name",
        "service-namespace",
        "environment",
        "owner",
        "cost-center",
        "lifecycle",
    )
    tags = {key: _text(dial, ("tags", key)) for key in keys}
    # tags.lifecycle drives real behaviour downstream (secret recovery windows,
    # the KMS deletion window, whether a managed broker may be destroyed), so
    # it is checked against the two values deployment.example.yaml documents
    # rather than passed through as free text like the other five tags.
    if tags["lifecycle"] and tags["lifecycle"] not in LIFECYCLE_VALUES:
        raise DialError(
            f"tags.lifecycle must be one of {' or '.join(LIFECYCLE_VALUES)}, got {tags['lifecycle']!r}"
        )
    tags["iac-source"] = f"dfe-infra/terraform/environments/{cloud}"
    return tags


def _tofu_vars(dial: dict[str, object]) -> tuple[str, dict[str, object]]:
    """Turn the dial into the variable set the cloud root declares.

    Returns the cloud token and the variables. The shape is the root's
    variables.tf exactly -- a key the root does not declare is an error there,
    and a key it declares and this omits is a prompt on an unattended plan --
    EXCEPT `node_pools` and `resolved_shapes`, which this deliberately never
    emits. `scripts/resolve_sizing.py`'s `build_tfvars` is the single writer of
    both, in its own `sizing.auto.tfvars.json` beside this file's
    `dial.auto.tfvars.json`: it starts from the dial's own `node_pools:` block
    (e.g. `system`) and adds the pools and shapes it derives on top, so there is
    exactly one place either variable is written and the two tfvars producers
    can never silently overwrite one another's map (the correctness review's
    P1-3 -- resolving it needed a single owner, not two files racing to be the
    last one OpenTofu loads).
    """
    cloud = _required(dial, ("target", "provision", "cloud"))
    root = TOFU_ROOTS / cloud
    if not root.is_dir():
        raise DialError(
            f"target.provision.cloud is {cloud!r} and there is no root at {root} -- "
            f"a cloud arrives as a new root over the capability modules"
        )
    region = _required(dial, ("target", "provision", "region"))

    public = _flag(dial, ("endpoint", "public"))
    allowed = list(split_list(_scalar(dial, ("endpoint", "allowed_cidrs"))))
    if public and not allowed:
        raise DialError(
            "endpoint.public is true and endpoint.allowed_cidrs is empty -- name the "
            "addresses allowed to reach the Kubernetes API, or set public to false"
        )

    registry = _text(dial, ("registry",))
    kafka = _kafka(dial)
    return cloud, {
        "provision": {
            "cloud": cloud,
            "account": _required(dial, ("target", "provision", "account")),
            "region": region,
            "cidr": _required(dial, ("target", "provision", "cidr")),
        },
        "name": _required(dial, ("metadata", "name")),
        "env": _required(dial, ("k8s", "env")),
        "profile": _required(dial, ("profile",)),
        "kubernetes_version": _kubernetes_version(dial, cloud),
        "network": {"nat": _required(dial, ("network", "nat")), "az_count": _az_count(dial)},
        "endpoint": {"public": public, "allowed_cidrs": allowed},
        "dns": {
            "private_zone": _required(dial, ("dns", "private_zone")),
            "public_zone": _text(dial, ("dns", "public_zone")),
        },
        "telemetry": _telemetry(dial, cloud),
        "storage_class": _required(dial, ("k8s", "storage_class")),
        "kafka": kafka,
        "secrets": {
            "backend": _required(dial, ("secrets", "backend")),
            "ref": _text(dial, ("secrets", "ref")),
        },
        "seeds": _seeds(str(kafka["provider"])),
        "endpoints": {
            "clickhouse_host": _text(dial, ("endpoints", "clickhouse_host")),
            "kafka_bootstrap": _text(dial, ("endpoints", "kafka_bootstrap")),
            "otel_endpoint": _text(dial, ("endpoints", "otel_endpoint")),
        },
        "repo_url": _required(dial, ("k8s", "repo_url")),
        "target_revision": _required(dial, ("k8s", "target_revision")),
        # The pull-secret host only. A registry credential is a secret, so it
        # reaches tofu from the deployer's environment and never from the dial.
        "registry_host": registry.split("/", 1)[0] if registry else "",
        "registry_user": "",
        "registry_token": "",
        "state": {
            "bucket": _required(dial, ("state", "bucket")),
            "key": _required(dial, ("state", "key")),
            "region": _required(dial, ("state", "region")),
        },
        "tags": _tags(dial, cloud),
        "toolbox": _toolbox(dial),
    }


def _render_tofu(dial: dict[str, object], out: Path | None) -> int:
    """Write the cloud root's tfvars file, and print the plan that consumes it."""
    try:
        cloud, variables = _tofu_vars(dial)
    except (DialError, KeyError) as error:
        print(f"render_dial: {error}", file=sys.stderr)
        return 1

    destination = out or TOFU_ROOTS / cloud / TFVARS_NAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(variables, indent=2) + "\n")
    print(f"render_dial: wrote {destination}", file=sys.stderr)

    root = TOFU_ROOTS / cloud
    rel = root.relative_to(REPO_ROOT) if root.is_relative_to(REPO_ROOT) else root
    print(file=sys.stderr)
    print("Provision this dial with:", file=sys.stderr)
    print(f"  tofu -chdir={rel} init", file=sys.stderr)
    print(f"  tofu -chdir={rel} plan -out=deployment.tfplan", file=sys.stderr)
    print(f"  tofu -chdir={rel} apply deployment.tfplan", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="render_dial.py",
        description="Render the deployment dial's k8s slice into the DFE_* env file.",
    )
    ap.add_argument("--dial", type=Path, default=DIAL, help="dial path (default: deployment.yaml)")
    ap.add_argument(
        "--tofu",
        action="store_true",
        help=f"render the cloud root's tfvars instead (default: "
        f"terraform/environments/<cloud>/{TFVARS_NAME})",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="file to write (default: bootstrap/.env, or the tfvars path under --tofu)",
    )
    args = ap.parse_args()

    if not args.dial.is_file():
        print(
            f"render_dial: no deployment dial at {args.dial} -- copy "
            f"{DIAL_TEMPLATE.name} to deployment.yaml and populate it",
            file=sys.stderr,
        )
        return 1

    dial = _parse_yaml_subset(args.dial.read_text(encoding="utf-8", errors="replace"))
    substrate = _scalar(dial, ("substrate",))
    if substrate != "k8s":
        print(
            f"render_dial: this renderer handles substrate 'k8s', the dial says "
            f"{substrate!r} -- the docker-vm substrate renders via dfe-docker",
            file=sys.stderr,
        )
        return 1

    if args.tofu:
        return _render_tofu(dial, args.out)

    args.out = args.out or ENV_FILE
    # Seed the env file from the committed example on first render, so every
    # DFE_* key + its guidance is present before the dial merges over it.
    if not args.out.is_file():
        if not ENV_TEMPLATE.is_file():
            print(
                f"render_dial: no {ENV_TEMPLATE} to seed the env file from",
                file=sys.stderr,
            )
            return 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ENV_TEMPLATE, args.out)
        print(f"render_dial: seeded {args.out} from {ENV_TEMPLATE.name}", file=sys.stderr)

    updates = _env_updates(dial)
    if updates:
        _merge_env(args.out, updates)
        print(
            f"render_dial: merged {len(updates)} dial key(s) into {args.out}: "
            + ", ".join(sorted(updates)),
            file=sys.stderr,
        )
    else:
        print("render_dial: dial set no k8s keys -- env file unchanged", file=sys.stderr)

    try:
        ui_flags = _ui_flags(dial)
    except DialError as error:
        print(f"render_dial: {error}", file=sys.stderr)
        return 1

    public_uis = [
        name
        for name in ("dfe_ui", "kafbat", "cruise_control", "hyperdx", "argocd", "links")
        if ui_flags[f"ui.public.{name}"]
    ]
    print(file=sys.stderr)
    print(
        "Public UI exposure (ui.public.*): " + (", ".join(public_uis) if public_uis else "none"),
        file=sys.stderr,
    )

    try:
        ingest_mode = _ingest_mode(dial)
    except DialError as error:
        print(f"render_dial: {error}", file=sys.stderr)
        return 1

    print(
        f"Receiver ingest door (ingest.mode): {ingest_mode} -- {_INGEST_MODE_NOTE[ingest_mode]}",
        file=sys.stderr,
    )
    print(
        "  this renderer applies nothing here -- the door is exposure.mode in the"
        " deploy repo's values overlay",
        file=sys.stderr,
    )

    try:
        controller_pool = _controller_pool(dial)
    except DialError as error:
        print(f"render_dial: {error}", file=sys.stderr)
        return 1

    print(
        f"KRaft metadata quorum (kafka.controller_pool): {controller_pool}",
        file=sys.stderr,
    )

    print(file=sys.stderr)
    print("Deploy this dial with:", file=sys.stderr)
    print(f"  {_derived_command(dial, args.out)}", file=sys.stderr)
    print(
        "  # add --kubeconfig <path> if the target is not your current context",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
