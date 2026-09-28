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
committed template -- the operator's own tooling injects them from the
deployment's secrets backend at deploy time, or an operator fills the copied
``.env`` by hand. This renderer
never reads or writes a secret.

Dependency-free (no PyYAML) and stdlib only, matching the dfe-ops rule -- the
dial's k8s slice is scalar / nested-map only, so scripts/yaml_subset.py reads it.
"""

from __future__ import annotations

import argparse
import ipaddress
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


def _coerce_flag(value: str | None, label: str, default: bool) -> bool:
    """Turn one dial scalar into a bool, refusing anything else by the name given."""
    if value is None:
        return default
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise DialError(f"{label} must be true or false, got {value!r}")


def _flag(dial: dict[str, object], path: tuple[str, ...], default: bool = False) -> bool:
    """Read a true/false dial field. The dial writes them as strings, like `steps`."""
    return _coerce_flag(_scalar(dial, path), ".".join(path), default)


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
    """Validate the receiver's door the same way _flag() guards a boolean field.

    Anything outside the receiver's own vocabulary is refused by name before an
    operator carries it into a real Helm values overlay. This renderer writes no
    DFE_INGEST_* key and applies nothing: the dial's `edge.ingest.receiver:`
    block is copied by hand into a values overlay under `exposure:`, the same
    convention the rest of the edge block follows. A dial that sets neither that
    nor the deprecated `ingest.mode` therefore reports whatever the cloud
    overlay sets, and the chart's own default (public) when it sets none.
    """
    cloud = _text(dial, ("k8s", "cloud"))
    fallback = _overlay_ingest_mode(cloud) or "public"
    value, label = _edge_scalar(dial, ("edge", "ingest", "receiver", "mode"))
    value = value or fallback
    if value not in INGEST_MODES:
        raise DialError(f"{label} must be one of {', '.join(INGEST_MODES)}, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# The edge module's dial block
# ---------------------------------------------------------------------------
#
# ONE `edge:` block replaces the top-level `ui:` and `ingest:` blocks. The
# module is the two charts under helm/edge -- the public gateway and the fleet
# tunnel -- so the doors a deployment opens read in one place instead of two
# blocks named after the things behind them.
#
# NOTHING HERE IS APPLIED BY THIS RENDERER except edge.enabled, which reaches
# DFE_EDGE_ENABLED and the cluster-secret label. The rest is validated, reported
# and copied by hand into the deploy repo's own values overlay, the same
# convention the block it replaces followed. A bad value is still refused here,
# by name, before an operator carries it forward. deployment.example.yaml writes
# this block's booleans unquoted, unlike the rest of the dial: a value copied
# verbatim into a real Helm values file has to already be the type that file
# expects, where an unquoted true is a bool and a quoted "false" is a non-empty
# string that is always truthy.

# New path -> the deprecated top-level path that still feeds it. Read for ONE
# release: a dial setting only the old one renders with a deprecation line, a
# dial setting both is refused because the two are one key.
_EDGE_ALIASES: dict[tuple[str, ...], tuple[str, ...]] = {
    ("edge", "product", "public"): ("ui", "public", "dfe_ui"),
    ("edge", "product", "domain"): ("ui", "public_domain"),
    ("edge", "product", "allowed_cidrs"): ("ui", "allowed_cidrs"),
    ("edge", "product", "trusted_proxy_cidrs"): ("ui", "trusted_proxy_cidrs"),
    ("edge", "product", "rate_limit", "enabled"): ("ui", "rate_limit", "enabled"),
    ("edge", "product", "rate_limit", "requests"): ("ui", "rate_limit", "requests"),
    ("edge", "product", "rate_limit", "unit"): ("ui", "rate_limit", "unit"),
    ("edge", "product", "rate_limit", "scope"): ("ui", "rate_limit", "scope"),
    ("edge", "product", "waf", "mode"): ("ui", "waf", "mode"),
    ("edge", "product", "waf", "plan"): ("ui", "waf", "plan"),
    ("edge", "product", "waf", "managed_rules"): ("ui", "waf", "managed_rules"),
    ("edge", "product", "tls", "min_version"): ("ui", "tls", "min_version"),
    ("edge", "product", "tls", "hsts"): ("ui", "tls", "hsts"),
    ("edge", "admin_uis", "public", "kafbat"): ("ui", "public", "kafbat"),
    ("edge", "admin_uis", "public", "cruise_control"): ("ui", "public", "cruise_control"),
    ("edge", "admin_uis", "public", "hyperdx"): ("ui", "public", "hyperdx"),
    ("edge", "admin_uis", "public", "argocd"): ("ui", "public", "argocd"),
    ("edge", "admin_uis", "public", "links"): ("ui", "public", "links"),
    ("edge", "ingest", "receiver", "mode"): ("ingest", "mode"),
    ("edge", "ingest", "receiver", "public", "serviceType"): ("ingest", "public", "serviceType"),
    ("edge", "ingest", "receiver", "public", "loadBalancerIP"):
        ("ingest", "public", "loadBalancerIP"),
    ("edge", "ingest", "receiver", "public", "loadBalancerClass"):
        ("ingest", "public", "loadBalancerClass"),
    ("edge", "ingest", "receiver", "public", "annotations"): ("ingest", "public", "annotations"),
    ("edge", "ingest", "receiver", "public", "loadBalancerSourceRanges"):
        ("ingest", "public", "loadBalancerSourceRanges"),
    ("edge", "ingest", "receiver", "vpn", "podLabel"): ("ingest", "vpn", "podLabel"),
    ("edge", "ingest", "receiver", "networkPolicy", "enabled"):
        ("ingest", "networkPolicy", "enabled"),
}

# Every edge.* boolean, and the value the deployment takes when the dial omits
# it -- the chart default, except where an aliased key carries `ui:`'s.
_EDGE_BOOL_DEFAULTS: dict[tuple[str, ...], bool] = {
    ("edge", "enabled"): True,
    ("edge", "product", "public"): True,
    ("edge", "product", "rate_limit", "enabled"): True,
    ("edge", "product", "tls", "hsts"): True,
    ("edge", "engine_api", "with_product"): True,
    ("edge", "engine_api", "cli_families_public"): False,
    ("edge", "engine_api", "scim_public"): False,
    ("edge", "admin_uis", "external"): False,
    ("edge", "admin_uis", "public", "kafbat"): False,
    ("edge", "admin_uis", "public", "cruise_control"): False,
    ("edge", "admin_uis", "public", "hyperdx"): False,
    ("edge", "admin_uis", "public", "argocd"): False,
    ("edge", "admin_uis", "public", "links"): False,
    ("edge", "admin_uis", "public", "forgejo"): False,
    # The gateway chart's own default is false; every cloud overlay sets it true
    # (argocd/values/aws.yaml), which is what a cloud deploy actually gets.
    ("edge", "admin_uis", "oidc", "enabled"): False,
    ("edge", "ingest", "tunnel", "enabled"): False,
    ("edge", "ingest", "tunnel", "admin_peer", "enabled"): True,
    # Follows the culvert chart's own listeners list, which exposes WireGuard
    # and OpenVPN over UDP; false opens 51820 alone at every layer.
    ("edge", "ingest", "tunnel", "openvpn"): True,
    ("edge", "ingest", "otel", "public"): False,
    ("edge", "aws", "load_balancer_controller"): True,
}

# The flavour each k8s.cloud fact selects, matching the expression
# argocd/appsets/layer2-edge.yaml uses to pick argocd/values/edge-<flavour>.yaml.
_CLOUD_FLAVOUR: dict[str, str] = {"local": "onprem", "local-dfe": "onprem", "rancher": "onprem"}

EDGE_FLAVOURS = ("aws", "gcp", "azure", "onprem")
# The gateway's own public-TLS floor; unquoted 1.2 parses as a float and is
# rejected at render, so the dial writes it quoted.
TLS_MIN_VERSIONS = ("1.2", "1.3")
RATE_LIMIT_UNITS = ("Second", "Minute", "Hour", "Day", "Month", "Year")
# local counts per route per proxy replica; global needs Redis and an Envoy
# Gateway install change, so the chart refuses it.
RATE_LIMIT_SCOPES = ("local",)
# Any other mode terminates TLS above Envoy and moves the public certificate to
# the cloud's own store, which no chart here renders.
WAF_MODES = ("none",)
CLOUDFRONT_MODES = ("none",)
TUNNEL_SERVICE_TYPES = ("LoadBalancer", "NodePort", "ClusterIP")
TRAFFIC_POLICIES = ("Cluster", "Local")
# local mints the CA in the pod, so a restart without a durable volume
# invalidates every issued client config; external takes it from a Secret.
PKI_MODES = ("local", "external")
# byo is an address the deployer already has in front of the tunnel; forwarder
# is the tier-2 instance that holds one, and arrives with terraform/modules/edge.
ADDRESS_MODES = ("byo", "forwarder")
# The forwarder moves every tunnel byte and does nothing else, so it is sized by
# baseline network bandwidth rather than by anything else: this is the shape
# shapes/compute-shapes.yaml already names for a small always-on AWS box (use
# case `toolbox` -- family t, arch arm64, generation newest, size small), and
# within one burstable family the baseline rises with the size.
TUNNEL_FORWARDER_TYPE = "t4g.small"
# The nodePort helm/edge/culvert/values.yaml pins for each exposed listener.
# Pinned rather than allocated, because whatever stands in front of a NodePort
# has to be told the number before the Service exists.
TUNNEL_NODE_PORTS = {"wireguard": 31820, "openvpn": 31194}
# The appliance ports an operator's reach-back initiates to, which the toolbox
# security group opens against the tunnel's client range. Mirrors
# helm/edge/culvert/values.yaml peers.classes.admin.reach.
TUNNEL_ADMIN_REACH = (22, 443)
# The upstream change the admin PEER shape waits on, and the two dial fields
# that belong to it. Until it lands neither reaches a rendered resource, so a
# value other than these is refused rather than accepted and dropped.
ADMIN_PEER_ISSUE = "hyperi-io/culvert#40"
ADMIN_PEER_TTL_MINUTES = 60
OTEL_AUTH = ("required", "none")

_EDGE_ENUMS: dict[tuple[str, ...], tuple[str, ...]] = {
    ("edge", "flavour"): EDGE_FLAVOURS,
    ("edge", "product", "tls", "min_version"): TLS_MIN_VERSIONS,
    ("edge", "product", "rate_limit", "unit"): RATE_LIMIT_UNITS,
    ("edge", "product", "rate_limit", "scope"): RATE_LIMIT_SCOPES,
    ("edge", "product", "waf", "mode"): WAF_MODES,
    ("edge", "ingest", "receiver", "mode"): INGEST_MODES,
    ("edge", "ingest", "tunnel", "serviceType"): TUNNEL_SERVICE_TYPES,
    ("edge", "ingest", "tunnel", "externalTrafficPolicy"): TRAFFIC_POLICIES,
    ("edge", "ingest", "tunnel", "pki_mode"): PKI_MODES,
    ("edge", "ingest", "tunnel", "address", "mode"): ADDRESS_MODES,
    ("edge", "ingest", "otel", "auth"): OTEL_AUTH,
    ("edge", "aws", "cloudfront", "mode"): CLOUDFRONT_MODES,
}

# What each enum takes when the dial omits it. edge.flavour comes from k8s.cloud
# and edge.ingest.receiver.mode from the cloud overlay, so neither is here.
_EDGE_ENUM_DEFAULTS: dict[tuple[str, ...], str] = {
    ("edge", "product", "tls", "min_version"): "1.2",
    ("edge", "product", "rate_limit", "unit"): "Minute",
    ("edge", "product", "rate_limit", "scope"): "local",
    ("edge", "product", "waf", "mode"): "none",
    ("edge", "ingest", "tunnel", "serviceType"): "LoadBalancer",
    ("edge", "ingest", "tunnel", "externalTrafficPolicy"): "Local",
    ("edge", "ingest", "tunnel", "pki_mode"): "local",
    ("edge", "ingest", "tunnel", "address", "mode"): "byo",
    ("edge", "ingest", "otel", "auth"): "required",
    ("edge", "aws", "cloudfront", "mode"): "none",
}

# The admin UIs the module offers a public hostname, in the order the summary
# reports them.
ADMIN_UIS = ("kafbat", "cruise_control", "hyperdx", "argocd", "links", "forgejo")

# Every tier-2 key, the values that turn it on, the cost bucket it carries when
# it is, and the pricing model behind that bucket. Buckets are relative to the
# deployment's own compute and never a rate
# (docs/deployment/aws.md#how-costs-are-described). An EMPTY bucket is a key that
# is tier 2 by EXPOSURE rather than by spend -- it opens a door on a hostname the
# deployment already publishes, and adds no cloud resource at all.
_EDGE_TIER2: tuple[tuple[str, tuple[object, ...], str, str], ...] = (
    ("edge.ingest.receiver.mode", ("public",), "L",
     "a load balancer billed per GB processed, against terabytes a day of ingest"),
    ("edge.ingest.tunnel.address.mode", ("forwarder",), "XS",
     "one small instance and one static address, both billed hourly"),
    ("edge.product.waf.mode", ("cloudfront",), "S",
     "managed rules billed per request, on a CDN in front of the gateway"),
    ("edge.aws.cloudfront.mode", ("cloudfront",), "S",
     "a distribution billed per GB served"),
    ("edge.engine_api.cli_families_public", (True,), "",
     "the CLI path families and /openapi.json on the product's own public hostname"),
    ("edge.engine_api.scim_public", (True,), "",
     "/api/v1/scim/v2 on the product's own public hostname, for an IdP that"
     " provisions from outside"),
)


def _edge_scalar(dial: dict[str, object], path: tuple[str, ...]) -> tuple[str | None, str]:
    """The scalar at an edge path, or its deprecated alias's, with the name used.

    The name comes back so a refusal quotes the path the dial actually wrote
    rather than the one it should have.
    """
    value = _scalar(dial, path)
    if value is not None:
        return value, ".".join(path)
    old = _EDGE_ALIASES.get(path)
    if old is not None:
        legacy = _scalar(dial, old)
        if legacy is not None:
            return legacy, ".".join(old)
    return None, ".".join(path)


def _edge_alias_conflicts(dial: dict[str, object]) -> list[str]:
    """Paths the dial sets on BOTH the new block and its deprecated spelling."""
    return [
        f"{'.'.join(new)} and {'.'.join(old)} are the same key and the dial sets both"
        for new, old in _EDGE_ALIASES.items()
        if _scalar(dial, new) is not None and _scalar(dial, old) is not None
    ]


def _edge_deprecations(dial: dict[str, object]) -> list[str]:
    """One line per deprecated path still carrying the value, naming the new one."""
    return [
        f"{'.'.join(old)} moved to {'.'.join(new)}; the old path is read for one release"
        for new, old in _EDGE_ALIASES.items()
        if _scalar(dial, new) is None and _scalar(dial, old) is not None
    ]


def _edge_flags(dial: dict[str, object]) -> dict[str, bool]:
    """Validate every `edge:` boolean, reading the deprecated block where it must.

    Returns each field keyed by its dotted path, so a caller reports which doors
    a dial opens without re-deriving the path list.
    """
    flags: dict[str, bool] = {}
    for path, default in _EDGE_BOOL_DEFAULTS.items():
        value, label = _edge_scalar(dial, path)
        flags[".".join(path)] = _coerce_flag(value, label, default)
    return flags


def _edge_flavour(dial: dict[str, object]) -> str:
    """The flavour the dial names, or the one k8s.cloud selects when it names none."""
    cloud = _text(dial, ("k8s", "cloud"))
    derived = _CLOUD_FLAVOUR.get(cloud, cloud)
    return _text(dial, ("edge", "flavour"), derived)


def _edge_enums(dial: dict[str, object]) -> dict[str, str]:
    """Validate every `edge:` enum against the vocabulary its chart accepts."""
    out: dict[str, str] = {}
    for path, choices in _EDGE_ENUMS.items():
        if path == ("edge", "ingest", "receiver", "mode"):
            out[".".join(path)] = _ingest_mode(dial)
            continue
        if path == ("edge", "flavour"):
            value, label = _edge_flavour(dial), "edge.flavour"
        else:
            value, label = _edge_scalar(dial, path)
            value = value or _EDGE_ENUM_DEFAULTS[path]
        if value and value not in choices:
            raise DialError(f"{label} must be one of {', '.join(choices)}, got {value!r}")
        out[".".join(path)] = value
    return out


def _edge_refusals(
    dial: dict[str, object], flags: dict[str, bool], enums: dict[str, str]
) -> None:
    """The combinations the module does not offer, refused by name.

    A dial that asks for one of these is asking for something no chart renders,
    which is worse than an error because it looks applied.
    """
    families, label = _edge_scalar(dial, ("edge", "engine_api", "private_path_families"))
    if families is not None:
        raise DialError(
            f"{label} is retired and nothing reads it -- the engine team named the path "
            "families, so the chart carries them as data and the dial carries the two "
            "switches over them: edge.engine_api.cli_families_public opens the CLI "
            "families and /openapi.json, edge.engine_api.scim_public opens "
            "/api/v1/scim/v2. Delete the key"
        )
    if flags["edge.ingest.otel.public"] and enums["edge.ingest.otel.auth"] != "required":
        raise DialError(
            "edge.ingest.otel.public is true with edge.ingest.otel.auth "
            f"{enums['edge.ingest.otel.auth']!r} -- a public OTLP door with no auth "
            "accepts telemetry from anyone, so authentication is the gate on making it public"
        )


def _edge_bool(dial: dict[str, object], path: tuple[str, ...]) -> bool:
    """One `edge:` boolean, validated against its own default."""
    value, label = _edge_scalar(dial, path)
    return _coerce_flag(value, label, _EDGE_BOOL_DEFAULTS[path])


def _tunnel_address_mode(dial: dict[str, object]) -> str:
    """The tunnel's address mode, refused by name outside its vocabulary.

    Checked here as well as in _edge_enums, because the tofu render is reached
    without the summary pass that runs the rest of the edge validation.
    """
    path = ("edge", "ingest", "tunnel", "address", "mode")
    value, label = _edge_scalar(dial, path)
    value = value or _EDGE_ENUM_DEFAULTS[path]
    if value not in ADDRESS_MODES:
        raise DialError(f"{label} must be one of {', '.join(ADDRESS_MODES)}, got {value!r}")
    return value


def _edge_ports(dial: dict[str, object], path: tuple[str, ...], default: tuple[int, ...]) -> list[int]:
    """A dial list of whole numbers written inline, as `reach: [22, 443]`.

    The restricted reader hands a flow list back as its own text, so the
    brackets come off here rather than through a second YAML parser.
    """
    raw = _scalar(dial, path)
    if raw is None:
        return list(default)
    label = ".".join(path)
    ports: list[int] = []
    for item in split_list(raw.strip().lstrip("[").rstrip("]")):
        if not item.isdigit() or not 0 < int(item) < 65536:
            raise DialError(f"{label} must be whole port numbers between 1 and 65535, got {item!r}")
        ports.append(int(item))
    return ports


def _edge_cidrs(dial: dict[str, object], path: tuple[str, ...]) -> list[str]:
    """A dial list of CIDRs written inline, as `loadBalancerSourceRanges: [10.0.0.0/8]`.

    The brackets come off the way `_edge_ports` takes them off, and each entry is
    parsed here: a range OpenTofu cannot read must not reach a security group,
    and `[]` left unstripped is one bogus entry where the operator meant none.
    """
    raw = _scalar(dial, path)
    if raw is None:
        return []
    label = ".".join(path)
    ranges: list[str] = []
    for item in split_list(raw.strip().lstrip("[").rstrip("]")):
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError as error:
            raise DialError(f"{label} must be CIDR ranges, got {item!r}") from error
        # A bare address parses as a /32 here and is refused by the security
        # group API, so the prefix is required where the mistake is still cheap.
        if "/" not in item:
            raise DialError(f"{label} needs a prefix length on every range, got {item!r}")
        ranges.append(item)
    return ranges


def _admin_peer_inert(dial: dict[str, object], at: tuple[str, ...]) -> None:
    """Refuse the two admin-peer fields that reach nothing yet.

    Both describe the admin PEER -- one minted per session with a one-way
    isolation exception -- which culvert cannot carry until its peer classes
    land, so a dial setting either gets a value it can read back and a
    deployment that behaves as though it had not. Refused by name for the same
    reason the culvert chart refuses exposure.loadBalancerSourceRanges on a
    NodePort: a field that silently does nothing is worse than one that stops.

    Checked from _edge_tunnel, so the tofu render refuses it as well as the
    summary pass.
    """
    ttl = _optional_number(dial, (*at, "ttl_minutes"), ADMIN_PEER_TTL_MINUTES)
    if ttl != ADMIN_PEER_TTL_MINUTES:
        raise DialError(
            f"{'.'.join((*at, 'ttl_minutes'))} is {ttl}, and nothing reads it -- it bounds the"
            f" admin PEER, which waits on {ADMIN_PEER_ISSUE} and which `dfe-ops bastion join`"
            f" refuses until then. Leave it at {ADMIN_PEER_TTL_MINUTES} and end the session with"
            " `dfe-ops bastion down`, which is what actually closes the reach-back today."
        )
    peer_cidr = _text(dial, (*at, "peer_cidr"))
    if peer_cidr:
        raise DialError(
            f"{'.'.join((*at, 'peer_cidr'))} is {peer_cidr!r}, and nothing reads it -- it carves"
            f" the range an admin PEER is issued into, which waits on {ADMIN_PEER_ISSUE}. Leave it"
            " empty: the reach-back that works today routes the tunnel's client range at the"
            " culvert pod and issues no peer at all."
        )


def _edge_tunnel(dial: dict[str, object]) -> dict[str, object]:
    """The tunnel's cloud-side address, the one part of the tunnel tofu builds.

    Everything else under `edge.ingest.tunnel:` is a chart value. The node
    ports are the exception that has to travel: they are pinned in the culvert
    chart's own listeners list because a forwarder has to be told the number,
    and Kubernetes would otherwise allocate one nothing could know in advance.
    """
    at = ("edge", "ingest", "tunnel")
    _admin_peer_inert(dial, (*at, "admin_peer"))
    return {
        "address": {
            "mode": _tunnel_address_mode(dial),
            "instance_type": _text(dial, (*at, "address", "instance_type"), TUNNEL_FORWARDER_TYPE),
            "zone": _text(dial, (*at, "address", "zone")),
        },
        "openvpn": _edge_bool(dial, (*at, "openvpn")),
        "source_ranges": _edge_cidrs(dial, (*at, "loadBalancerSourceRanges")),
        "node_ports": {
            "wireguard": _optional_number(dial, (*at, "node_ports", "wireguard"), TUNNEL_NODE_PORTS["wireguard"]),
            "openvpn": _optional_number(dial, (*at, "node_ports", "openvpn"), TUNNEL_NODE_PORTS["openvpn"]),
        },
        # The reach-back is a security-group rule on the toolbox, so its two
        # fields travel to tofu while the rest of the class stays a chart value.
        "admin_peer": {
            "enabled": _edge_bool(dial, (*at, "admin_peer", "enabled")),
            "reach": _edge_ports(dial, (*at, "admin_peer", "reach"), TUNNEL_ADMIN_REACH),
        },
    }


def _edge(dial: dict[str, object]) -> dict[str, object]:
    """The edge module's tofu slice -- what decides whether a cloud resource
    is created, and no more.

    The rest of the `edge:` block is validated and reported here but copied by
    hand into a values overlay, the same convention the block it replaced
    followed (terraform/modules/edge/aws).
    """
    return {
        "enabled": _edge_bool(dial, ("edge", "enabled")),
        "tunnel": _edge_tunnel(dial),
    }


def _edge_tier2_on(enums: dict[str, str], flags: dict[str, bool] | None = None) -> list[str]:
    """Each tier-2 key the dial turns on, with its bucket and pricing model.

    Enums and booleans are read through one mapping, because a key is tier 2 for
    what it opens rather than for the shape of its value. A row with no bucket
    costs nothing and says so, rather than printing a bucket it does not have.
    """
    settings: dict[str, object] = {**enums, **(flags or {})}
    lines: list[str] = []
    for path, on_values, bucket, model in _EDGE_TIER2:
        value = settings.get(path)
        if value not in on_values:
            continue
        # A boolean is reported in the dial's own spelling, not Python's.
        shown = str(value).lower() if isinstance(value, bool) else value
        cost = f"bucket {bucket}, {model}" if bucket else f"no spend, {model}"
        lines.append(f"{path}: {shown} -- {cost}")
    return lines


def _engine_api_summary(flags: dict[str, bool]) -> str:
    """Which of the engine's path families answer on the product's public hostname.

    The families themselves are chart data (helm/edge/gateway values.yaml
    routes.dfeEngine), so this reports what the two switches open and never
    restates the lists -- a dial that named them would drift on every router the
    engine adds.
    """
    if not flags["edge.engine_api.with_product"]:
        return "off -- the engine answers on no public hostname"
    opened = ["browser families only"]
    if flags["edge.engine_api.cli_families_public"]:
        opened = ["browser families", "plus the CLI families and /openapi.json"]
    if flags["edge.engine_api.scim_public"]:
        opened.append("plus SCIM")
    return ", ".join(opened)


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
    # The Apache Kafka CLI tarball the toolbox installs in place of kcat, which
    # has no package on the instance's distribution. It follows the same broker
    # version the deployment runs, for the same reason clickhouse-client and
    # psql follow their servers: a client that skews off its server is a
    # debugging tool that lies.
    kafka = _scalar(tree, ("stacks", current, "services", "kafka-version"))
    if kafka is not None:
        versions["kafka-cli"] = kafka
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
        "edge": _edge(dial),
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
        conflicts = _edge_alias_conflicts(dial)
        if conflicts:
            raise DialError("; ".join(conflicts))
        deprecated = _edge_deprecations(dial)
        edge_flags = _edge_flags(dial)
        edge_enums = _edge_enums(dial)
        _edge_refusals(dial, edge_flags, edge_enums)
    except DialError as error:
        print(f"render_dial: {error}", file=sys.stderr)
        return 1

    for line in deprecated:
        print(f"render_dial: deprecated -- {line}", file=sys.stderr)

    public = ["dfe-ui"] if edge_flags["edge.product.public"] else []
    public += [name for name in ADMIN_UIS if edge_flags[f"edge.admin_uis.public.{name}"]]
    ingest_mode = edge_enums["edge.ingest.receiver.mode"]
    tier2 = _edge_tier2_on(edge_enums, edge_flags)
    engine_api = _engine_api_summary(edge_flags)

    print(file=sys.stderr)
    print(
        f"EDGE MODULE ({edge_enums['edge.flavour'] or 'no flavour'}) -- "
        + ("on" if edge_flags["edge.enabled"] else "off, no door renders at all"),
        file=sys.stderr,
    )
    print(
        "  public hostnames (edge.product.public, edge.admin_uis.public.*): "
        + (", ".join(public) if public else "none"),
        file=sys.stderr,
    )
    print(
        f"  engine API on the product hostname (edge.engine_api.*): {engine_api}",
        file=sys.stderr,
    )
    print(
        "  admin UI kill switch (edge.admin_uis.external): "
        + ("on" if edge_flags["edge.admin_uis.external"] else "off, every infra route is withdrawn"),
        file=sys.stderr,
    )
    print(
        f"  receiver ingest door (edge.ingest.receiver.mode): {ingest_mode}"
        f" -- {_INGEST_MODE_NOTE[ingest_mode]}",
        file=sys.stderr,
    )
    print(
        "  fleet tunnel (edge.ingest.tunnel.enabled): "
        + ("on" if edge_flags["edge.ingest.tunnel.enabled"]
           else "off -- the module offers the tunnel, the deployment turns it on"),
        file=sys.stderr,
    )
    print(
        "  tier 2 opt-ins that are ON: " + (", ".join(tier2) if tier2 else "none"),
        file=sys.stderr,
    )
    print(
        "  this renderer applies none of it -- paste the block into the deploy"
        " repo's values overlay",
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
