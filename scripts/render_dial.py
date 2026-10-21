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
``.env`` by hand. This renderer never sets a secret, but every rewrite carries
the ones filled in, so the file is written at mode 0600 every time.

Dependency-free (no PyYAML) and stdlib only, matching the dfe-ops rule -- the
dial's k8s slice is scalar / nested-map only, so scripts/yaml_subset.py reads it.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import private_file
from yaml_subset import YamlSubsetError, at, split_list, value_at
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
    # annotations the same way it already does for the node-pools
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
    return updates


def _merge_env(env_path: Path, updates: dict[str, str]) -> None:
    """Overwrite each mapped key in the env file with the dial value.

    Every OTHER line -- the example's blanks, comments, untouched settings --
    survives verbatim, so the estate secrets/endpoints stay present-but-blank for
    the operator (or the thin caller) to fill. A mapped key is replaced in place;
    a genuinely new key is appended under a labelled header. The file carries
    whatever secrets the operator filled in, so the rewrite lands at 0600.
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

    private_file.write_private(env_path, "\n".join(out) + "\n")


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


def _seeds(kafka_provider: str, clickhouse_mode: str = "in_cluster") -> dict[str, dict[str, str]]:
    """The credentials the deploy layer puts in the store, for this broker and datastore.

    An empty value is generated -- in the secrets module, or in the root for a
    password a managed broker is created with -- and never surfaces, so neither
    the dial nor the rendered tfvars ever carries one. The kafka key's last
    segment is the provider, which is what the kafka chart reads back.
    Confluent Cloud's seed carries a username too: its API key is the username,
    the chart reads both, and the root fills both from the key Confluent minted.
    An external ClickHouse's password is never generated either: the root fills
    it from TF_VAR_clickhouse_external_password. MSK takes no kafka seed: its own
    AmazonMSK_ secret is the one copy, and the apps read it through ESO.
    """
    kafka_fields = {"password": ""}
    if kafka_provider == "confluent-cloud":
        kafka_fields = {"username": "", "password": ""}
    seeds: dict[str, dict[str, str]] = {}
    if kafka_provider != "msk":
        seeds[f"kafka/{kafka_provider}"] = kafka_fields
    seeds["ui/nextauth"] = {"secret": ""}
    if clickhouse_mode == "external":
        seeds["clickhouse/external"] = {"password": ""}
    return seeds


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


# The ingest exposure group's doors (argocd/values/common.yaml exposure.groups.ingest).
INGEST_DOORS = ("loadbalancer", "gateway", "tunnel")
INGEST_AT = ("edge", "groups", "ingest")

# Where the cloud overlays live, read rather than restated: a hardcoded list of
# "the clouds whose door is the gateway" drifts the moment one overlay changes,
# and the drift shows up as a summary line that contradicts what deployed.
CLOUD_VALUES = REPO_ROOT / "argocd" / "values"
INGEST_DOOR_PATH = ("exposure", "groups", "ingest", "door")

# The receiver's door and what it opens, for the printed summary line.
_INGEST_DOOR_NOTE: dict[str, str] = {
    "loadbalancer": "the receiver's own load balancer, billed per GB processed",
    "gateway": "receiver.<domain> on the gateway, http only",
    "tunnel": "no public door; senders arrive over culvert or their own peering",
}

# The retired spellings of the receiver's door and allow list, each refused with
# the key that replaced it.
_INGEST_RETIRED: dict[tuple[str, ...], str] = {
    ("edge", "ingest", "receiver", "mode"): "edge.groups.ingest.door",
    ("ingest", "mode"): "edge.groups.ingest.door",
    ("edge", "ingest", "receiver", "public", "loadBalancerSourceRanges"): "edge.groups.ingest.allow",
    ("ingest", "public", "loadBalancerSourceRanges"): "edge.groups.ingest.allow",
}


def _values_ingest_door(cloud: str) -> str | None:
    """exposure.groups.ingest.door as argocd/values/<cloud>.yaml sets it, else common.yaml."""
    for name in ([f"{cloud}.yaml"] if cloud else []) + ["common.yaml"]:
        try:
            text = (CLOUD_VALUES / name).read_text(encoding="utf-8")
        except OSError:
            continue
        door = value_at(text, INGEST_DOOR_PATH)
        if door:
            return door
    return None


def _ingest_door(dial: dict[str, object]) -> str:
    """The receiver's door the dial names, else the one the cloud overlay sets.

    Refused by name outside the chart's own vocabulary, before an operator
    carries it into the deploy repo's infra/common.yaml.
    """
    path = (*INGEST_AT, "door")
    value = _scalar(dial, path) or _values_ingest_door(_text(dial, ("k8s", "cloud"))) or "loadbalancer"
    if value not in INGEST_DOORS:
        raise DialError(f"{'.'.join(path)} must be one of {', '.join(INGEST_DOORS)}, got {value!r}")
    return value


def _ingest_retired(dial: dict[str, object]) -> None:
    """Refuse a retired spelling of the receiver's door or allow list, naming its replacement."""
    for old, new in _INGEST_RETIRED.items():
        if at(dial, old) is not None:
            raise DialError(
                f"{'.'.join(old)} is retired -- the receiver's door is the ingest exposure group: "
                f"set {new} instead, pasted as exposure.groups.ingest in the deploy repo's "
                "infra/common.yaml. Door loadbalancer (was public) renders the receiver's own load "
                "balancer only while edge.groups.ingest.allow names the senders; gateway was "
                "internal; tunnel was vpn"
            )


# The tier-2 row for the receiver's own load balancer, which exists only on door
# loadbalancer with senders named, so main() computes it rather than reading one key.
INGEST_TIER2_KEY = "edge.groups.ingest"


def ingest_opened(dial: dict[str, object], enums: dict[str, str], flags: dict[str, bool]) -> str:
    """The door the receiver's own load balancer renders on, or "" when none renders."""
    door = enums[".".join((*INGEST_AT, "door"))]
    named = _edge_cidrs(dial, (*INGEST_AT, "allow"))
    return door if flags[".".join((*INGEST_AT, "enabled"))] and door == "loadbalancer" and named else ""


def ingest_fence(dial: dict[str, object], door: str, enabled: bool) -> tuple[str, list[str]]:
    """What the ingest group opens, as the receiver and gateway charts render it.

    Returns:
        (the summary line's value, warning lines -- empty unless a /0 is listed).

    Raises:
        DialError: deny is set on door loadbalancer, where nothing enforces it.
    """
    allow = _edge_cidrs(dial, (*INGEST_AT, "allow"))
    deny = _edge_cidrs(dial, (*INGEST_AT, "deny"))
    if enabled and door == "loadbalancer" and deny:
        raise DialError(
            f"edge.groups.ingest.deny is {', '.join(deny)} on door loadbalancer, and nothing "
            "enforces it there: cloud load balancer rules are allow-only and the receiver's "
            "server.ip_filter is read from its config file, which no values layer derives from "
            "the ingest group. Narrow edge.groups.ingest.allow, or use door gateway, where Envoy "
            "refuses the deny list ahead of the allow list"
        )
    if not enabled:
        return "off -- no public door", []
    everything = [cidr for cidr in allow if ipaddress.ip_network(cidr, strict=False).prefixlen == 0]
    warnings = []
    if everything and door != "tunnel":
        warnings.append(
            f"edge.groups.ingest.allow carries {', '.join(everything)}, which admits every "
            "address: the receiver takes data from the whole internet behind its own caller "
            "check only. Name the senders' ranges instead"
        )
    note = _INGEST_DOOR_NOTE[door]
    if door == "tunnel":
        return f"tunnel -- {note}", []
    if allow:
        return f"{door} -- {note}, admitting {', '.join(allow)}", warnings
    if door == "loadbalancer":
        return "loadbalancer -- closed: no load balancer renders until edge.groups.ingest.allow names the senders", []
    return f"gateway -- {note}, closed on an internet-facing gateway and unfenced elsewhere", []


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
# DFE_EDGE_ENABLED and the cluster-secret label, and
# edge.product.trusted_proxy_cidrs, which the --tofu render hands a cloud root.
# The rest is validated, reported and copied by hand into the deploy repo's own
# values overlay -- edge.product.allowed_cidrs as the gateway chart's
# exposure.groups.<group>.allow, which only the deploy repo sets -- the same
# convention the block it replaces followed. A bad value is still refused here,
# by name, before an operator carries it forward.
# deployment.example.yaml writes this block's booleans unquoted, unlike the rest
# of the dial: a value copied verbatim into a real Helm values file has to
# already be the type that file expects, where an unquoted true is a bool and a
# quoted "false" is a non-empty string that is always truthy.

# New path -> the deprecated path that still feeds it. Read for ONE release: a
# dial setting only the old one renders with a deprecation line, a dial setting
# both is refused because the two are one key.
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
    ("edge", "ingest", "receiver", "public", "serviceType"): ("ingest", "public", "serviceType"),
    ("edge", "ingest", "receiver", "public", "loadBalancerIP"):
        ("ingest", "public", "loadBalancerIP"),
    ("edge", "ingest", "receiver", "public", "loadBalancerClass"):
        ("ingest", "public", "loadBalancerClass"),
    ("edge", "ingest", "receiver", "public", "annotations"): ("ingest", "public", "annotations"),
    ("edge", "ingest", "receiver", "vpn", "podLabel"): ("ingest", "vpn", "podLabel"),
    ("edge", "ingest", "receiver", "networkPolicy", "enabled"):
        ("ingest", "networkPolicy", "enabled"),
    ("edge", "ingest", "otel", "enabled"): ("edge", "ingest", "otel", "public"),
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
    ("edge", "ingest", "otel", "enabled"): False,
    ("edge", "groups", "ingest", "enabled"): True,
    ("edge", "aws", "load_balancer_controller"): True,
    # One Elastic IP per public subnet for the receiver's NLB, which takes them
    # at creation only (terraform/modules/edge/aws/receiver.tf).
    ("edge", "aws", "receiver_eips"): True,
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
# The resolved shape the forwarder launches on when the dial names none: the
# toolbox use case, the small arm64 burstable the shape resolver keeps newest.
TUNNEL_FORWARDER_SHAPE = "toolbox"
# The range every appliance's tunnel address is issued out of, when the dial
# names none: dfe-extras' culvert default vpn.clientCIDR, which
# scripts/tests/test_tunnel_forwarder_facts.py holds this in step with.
TUNNEL_CLIENT_CIDR = "100.64.0.0/10"
# The nodePort argocd/values/apps/culvert/values.yaml `defaultListeners` pins for
# each exposed listener. Pinned rather than allocated, because whatever stands in
# front of a NodePort has to be told the number before the Service exists.
TUNNEL_NODE_PORTS = {"wireguard": 31820, "openvpn": 31194}
# The appliance ports an operator's reach-back initiates to, which the toolbox
# security group opens against the tunnel's client range. Mirrors dfe-extras'
# culvert default peers.classes.admin.reach.
TUNNEL_ADMIN_REACH = (22, 443)
# The upstream change the admin PEER shape waits on, and the two dial fields
# that belong to it. Until it lands neither reaches a rendered resource, so a
# value other than these is refused rather than accepted and dropped.
ADMIN_PEER_ISSUE = "hyperi-io/culvert#40"
ADMIN_PEER_TTL_MINUTES = 60
# `edge.ingest.otel` is the `otel.ingress` block of the deploy repo's
# infra/common.yaml verbatim, which the gateway and collector charts both read.
OTEL_AT = ("edge", "ingest", "otel")
# The one value the retired scalar `edge.ingest.otel.auth` may still carry: it
# names the only behaviour there is, so it is read with a deprecation line.
OTEL_AUTH_RETIRED = "required"

_EDGE_ENUMS: dict[tuple[str, ...], tuple[str, ...]] = {
    ("edge", "flavour"): EDGE_FLAVOURS,
    ("edge", "product", "tls", "min_version"): TLS_MIN_VERSIONS,
    ("edge", "product", "rate_limit", "unit"): RATE_LIMIT_UNITS,
    ("edge", "product", "rate_limit", "scope"): RATE_LIMIT_SCOPES,
    ("edge", "product", "waf", "mode"): WAF_MODES,
    (*INGEST_AT, "door"): INGEST_DOORS,
    ("edge", "ingest", "tunnel", "serviceType"): TUNNEL_SERVICE_TYPES,
    ("edge", "ingest", "tunnel", "externalTrafficPolicy"): TRAFFIC_POLICIES,
    ("edge", "ingest", "tunnel", "pki_mode"): PKI_MODES,
    ("edge", "ingest", "tunnel", "address", "mode"): ADDRESS_MODES,
    ("edge", "aws", "cloudfront", "mode"): CLOUDFRONT_MODES,
}

# What each enum takes when the dial omits it. edge.flavour comes from k8s.cloud
# and edge.groups.ingest.door from the cloud overlay, so neither is here.
_EDGE_ENUM_DEFAULTS: dict[tuple[str, ...], str] = {
    ("edge", "product", "tls", "min_version"): "1.2",
    ("edge", "product", "rate_limit", "unit"): "Minute",
    ("edge", "product", "rate_limit", "scope"): "local",
    ("edge", "product", "waf", "mode"): "none",
    ("edge", "ingest", "tunnel", "serviceType"): "LoadBalancer",
    ("edge", "ingest", "tunnel", "externalTrafficPolicy"): "Local",
    ("edge", "ingest", "tunnel", "pki_mode"): "local",
    ("edge", "ingest", "tunnel", "address", "mode"): "byo",
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
    (INGEST_TIER2_KEY, ("loadbalancer",), "L",
     "the receiver's own load balancer, billed per GB processed, against terabytes a day of"
     " ingest"),
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
    ("edge.ingest.otel.enabled", (True,), "",
     "OTLP ingest on otel.<domain> from outside the cluster, bearer token only"),
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


def _otel_auth_scalar(dial: dict[str, object]) -> str | None:
    """The retired scalar form of `edge.ingest.otel.auth`, or None for the map form."""
    return _scalar(dial, (*OTEL_AT, "auth"))


def _edge_deprecations(dial: dict[str, object]) -> list[str]:
    """One line per deprecated path still carrying the value, naming the new one."""
    lines = [
        f"{'.'.join(old)} moved to {'.'.join(new)}; the old path is read for one release"
        for new, old in _EDGE_ALIASES.items()
        if _scalar(dial, new) is None and _scalar(dial, old) is not None
    ]
    if _otel_auth_scalar(dial) == OTEL_AUTH_RETIRED:
        lines.append(
            f"edge.ingest.otel.auth: {OTEL_AUTH_RETIRED} is the only behaviour there is and "
            "is read for one release -- auth is now a map naming the token's store path, "
            "edge.ingest.otel.auth.remoteKey"
        )
    return lines


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
        if path == (*INGEST_AT, "door"):
            out[".".join(path)] = _ingest_door(dial)
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


def _edge_refusals(dial: dict[str, object], flags: dict[str, bool]) -> None:
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
    _ingest_retired(dial)
    _otel_refusals(dial, flags)


def _otel_refusals(dial: dict[str, object], flags: dict[str, bool]) -> None:
    """What both charts refuse for `otel.ingress`, refused here first by the dial's name.

    The dial block is pasted verbatim under otel.ingress in the deploy repo's
    infra/common.yaml, so a value the gateway or the collector would refuse at
    render is caught before it reaches either.
    """
    auth = _otel_auth_scalar(dial)
    if auth is not None and auth != OTEL_AUTH_RETIRED:
        raise DialError(
            f"edge.ingest.otel.auth is {auth!r} -- OTLP ingest has no unauthenticated mode. "
            "auth is a map naming where the deployment's secret store holds the bearer "
            "token: set edge.ingest.otel.auth.remoteKey, or leave edge.ingest.otel.enabled "
            "false"
        )
    remote_key = _scalar(dial, (*OTEL_AT, "auth", "remoteKey"))
    if flags["edge.ingest.otel.enabled"] and remote_key is None:
        raise DialError(
            "edge.ingest.otel.enabled is true and edge.ingest.otel.auth.remoteKey is empty -- "
            "the collector admits only a bearer token, and this names where the deployment's "
            "secret store holds it (property token). The gateway and the collector refuse "
            "the same pair at render"
        )
    port = _scalar(dial, (*OTEL_AT, "port"))
    if port is not None and not (port.isdigit() and 0 < int(port) < 65536):
        raise DialError(f"edge.ingest.otel.port must be a port number 1-65535, got {port!r}")


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
    A path with a deprecated spelling is read through it as well.
    """
    raw, label = _edge_scalar(dial, path)
    if raw is None:
        return []
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


PROXY_HOP_PATH = ("edge", "product", "trusted_proxy_cidrs")


def _proxy_hop_cidrs(dial: dict[str, object]) -> list[str]:
    """edge.product.trusted_proxy_cidrs, the CDN hops, with a /0 of either family refused.

    Every peer inside a /0 is trusted, so Envoy takes the client from the
    leftmost X-Forwarded-For entry, which the caller writes, and the fence
    admits whoever names a listed address in that header.

    Args:
        dial: The parsed dial.

    Returns:
        The parsed ranges, empty when the key is unset.

    Raises:
        DialError: An entry is not a CIDR range, or covers every address.
    """
    ranges = _edge_cidrs(dial, PROXY_HOP_PATH)
    everything = [cidr for cidr in ranges if ipaddress.ip_network(cidr, strict=False).prefixlen == 0]
    if everything:
        raise DialError(
            f"edge.product.trusted_proxy_cidrs carries {', '.join(everything)}, which trusts "
            "every address -- Envoy then takes the client from the leftmost X-Forwarded-For "
            "entry, which the caller writes, so a /0 trusted list makes the fence a header the "
            "client sets. Name only the ranges of the CDN or other L7 proxy in front"
        )
    return ranges


# The flavours whose gateway Service argocd/values/edge-<flavour>.yaml marks
# internet-facing, which is what fences every web route to the allow-list.
INTERNET_FACING_FLAVOURS = ("aws", "gcp", "azure")


def web_fence(cidrs: list[str], flavour: str) -> tuple[str, list[str]]:
    """What edge.product.allowed_cidrs admits once pasted as an exposure group's allow list.

    The gateway chart's groupFence, read from the dial: on an internet-facing
    flavour an empty list refuses every route of the group, and a /0 of either
    family admits every address whatever the address part says.

    Args:
        cidrs: The parsed allow-list.
        flavour: The edge flavour the dial selects.

    Returns:
        (the summary line's value, warning lines -- empty unless a /0 is listed).
    """
    everything = [cidr for cidr in cidrs if cidr.endswith("/0")]
    if everything:
        return f"{', '.join(cidrs)} -- every address", [
            f"edge.product.allowed_cidrs carries {', '.join(everything)}, which admits every "
            "address: dfe-ui, HyperDX, the engine API and every admin UI on the gateway are "
            "open to the whole internet behind their own logins. Name the ranges that need "
            "them instead"
        ]
    if cidrs:
        return ", ".join(cidrs), []
    if flavour in INTERNET_FACING_FLAVOURS:
        return ("empty -- the gateway is internet-facing, so every web route answers 403 to "
                "every address until this names who may reach it"), []
    return "empty -- this flavour's gateway is not internet-facing, so nothing is fenced", []


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


def _no_instance_type(dial: dict[str, object], at: tuple[str, ...], use_case: str) -> None:
    """Refuse an instance type written into the dial where a shape_ref now names one.

    The shape resolver picks the type under the shape's generation policy, so a
    literal here would be read by nothing; a deployer who needs a fixed type
    pins it as a resolver override instead.

    Raises:
        DialError: The dial sets `<at>.instance_type`.
    """
    written = _scalar(dial, (*at, "instance_type"))
    if written is not None:
        raise DialError(
            f"{'.'.join((*at, 'instance_type'))} is {written!r}, and nothing reads it -- the type "
            f"comes off the resolved shape {'.'.join((*at, 'shape_ref'))} names. To pin one, set "
            f"sizing.overrides.{use_case}.instance_type and re-run scripts/resolve_sizing.py."
        )


def _cidr(dial: dict[str, object], path: tuple[str, ...], default: str) -> str:
    """One CIDR from the dial, or `default` when it names none, refused by name when malformed."""
    value = _text(dial, path, default)
    try:
        ipaddress.ip_network(value, strict=True)
    except ValueError as err:
        raise DialError(f"{'.'.join(path)} must be a CIDR range, got {value!r}") from err
    return value


def _edge_tunnel(dial: dict[str, object]) -> dict[str, object]:
    """The tunnel's cloud-side address, the one part of the tunnel tofu builds.

    Everything else under `edge.ingest.tunnel:` is a chart value. The node
    ports are the exception that has to travel: they are pinned in the culvert
    chart's own listeners list because a forwarder has to be told the number,
    and Kubernetes would otherwise allocate one nothing could know in advance.
    The client range travels too, because the toolbox's reach-back egress is
    aimed at it.
    """
    at = ("edge", "ingest", "tunnel")
    _admin_peer_inert(dial, (*at, "admin_peer"))
    shape_ref = _text(dial, (*at, "address", "shape_ref"), TUNNEL_FORWARDER_SHAPE)
    _no_instance_type(dial, (*at, "address"), shape_ref)
    return {
        "address": {
            "mode": _tunnel_address_mode(dial),
            "shape_ref": shape_ref,
            "zone": _text(dial, (*at, "address", "zone")),
        },
        "client_cidr": _cidr(dial, (*at, "client_cidr"), TUNNEL_CLIENT_CIDR),
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
    _ingest_retired(dial)
    return {
        "enabled": _edge_bool(dial, ("edge", "enabled")),
        "tunnel": _edge_tunnel(dial),
        "receiver": {"eips": _edge_bool(dial, ("edge", "aws", "receiver_eips"))},
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


# Bits each subnet adds to the VPC CIDR when the dial names none: sixteen equal
# blocks, a /20 per subnet in a /16.
SUBNET_NEWBITS = 4


def _subnet_newbits(dial: dict[str, object], az_count: int) -> int:
    """How many bits each subnet adds to the VPC CIDR, holding a subnet per zone on each side of the midpoint."""
    value = _optional_number(dial, ("network", "subnet_newbits"), SUBNET_NEWBITS)
    if 2 ** (value - 1) < az_count:
        raise DialError(
            f"network.subnet_newbits is {value}, which cuts {2**value} blocks: too few for "
            f"{az_count} private and {az_count} public subnets, one per zone each side of the midpoint"
        )
    return value


def _platform_version(cloud: str) -> str | None:
    """The control-plane FLOOR versions.yaml states for this cloud, if any.

    The `platform:` stage names the oldest control-plane minor a cluster must
    clear, not a version to install -- see _kubernetes_version, which is what
    turns this floor and the dial's own choice into the version that runs.
    Absent for a cloud with no stage yet, in which case the dial's own field
    is all there is.

    A value is spelt the way bootstrap/check_platform.py's parse_requirement
    reads it: `>=1.34` is a floor alone, and a range `1.34-1.36` is a floor and a
    ceiling. The floor is the low side either way. The ceiling is what
    check_platform.py warns above, and it never refuses a dial.
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
            return _platform_floor(value)
    return None


def _platform_floor(value: str) -> str:
    """The floor of one platform requirement: `>=1.34` and `1.34-1.36` both give 1.34."""
    # A leading `v` is a prefix, not a separator, as check_platform.py reads it.
    bare = value.strip().removeprefix(">=").strip().removeprefix("v")
    low, _, _ceiling = bare.partition("-")
    return low.strip().removeprefix("v")


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

# A SaaS body's reach (terraform/modules/managed-kafka/*/variables.tf connectivity).
KAFKA_CONNECTIVITY = ("private", "public")

# The Confluent cluster types the confluent-cloud body offers; Standard and Dedicated are not among them.
CONFLUENT_TIERS = ("freight", "enterprise", "basic")
# The tier sent when the dial names none: eCKU autoscaling, private networking only.
CONFLUENT_DEFAULT_TIER = "freight"

# The Redpanda Cloud products the redpanda-cloud body accepts; it builds serverless and refuses the rest at plan.
REDPANDA_TIERS = ("serverless", "dedicated", "byoc")
# The tier sent when the dial names none, and the only one the body can build.
REDPANDA_DEFAULT_TIER = "serverless"

# Confluent Cloud's ceiling on a topic's max.message.bytes on a Basic cluster
# (docs.confluent.io/cloud/current/topics/manage.html). The aws root's kafka
# validation and the body's own check carry the same number.
CONFLUENT_BASIC_MAX_MESSAGE_BYTES = 8388608

# The REST endpoint wait when the dial names none: one ingress load balancer of four without the route passes 12 in a row about 3% of the time.
CONFLUENT_REST_READY_DEFAULTS: dict[str, int] = {
    "successes": 12,
    "timeout_seconds": 900,
    "interval_seconds": 5,
}

# Confluent's environment ids and Redpanda's resource group ids, so one cannot stand in for the other.
_CONFLUENT_ENVIRONMENT_ID = re.compile(r"^env-[a-zA-Z0-9]+$")
_REDPANDA_RESOURCE_GROUP_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Where the KRaft metadata quorum runs (helm/charts/kafka's controllerPool).
CONTROLLER_POOLS = ("combined", "separate")


def _controller_pool(dial: dict[str, object]) -> str:
    """Validate kafka.controller_pool the same way _ingest_door() guards its own.

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


# What k8s.disruption.posture may say, pool disruption intent each cloud's pools renderer applies.
DISRUPTION_POSTURES = ("balanced", "conservative")
DISRUPTION_AT = ("k8s", "disruption")
MINUTES_PER_DAY = 1440

# Daily or one weekday only: the drift hold outside any other schedule is not one cron line.
_WINDOW_SCHEDULE = re.compile(r"^(?P<minute>\d{1,2}) (?P<hour>\d{1,2}) \* \* (?P<weekday>\*|[0-6])$")
# Hours and minutes only, the units a window's length is written in.
_WINDOW_DURATION = re.compile(r"^(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?$")


@dataclass(frozen=True, slots=True)
class MaintenanceWindow:
    """When drift may replace a node, opening on a UTC cron line.

    Attributes:
        minute: The minute past the hour the window opens.
        hour: The hour it opens, UTC.
        weekday: The day it opens on, 0 for Sunday, or None for every day.
        minutes: How long it stays open.
    """

    minute: int
    hour: int
    weekday: int | None
    minutes: int

    @property
    def period_minutes(self) -> int:
        """How often the window comes round: a day, or a week when it names a weekday."""
        return MINUTES_PER_DAY * (1 if self.weekday is None else 7)

    @property
    def schedule(self) -> str:
        """The cron line the window opens on."""
        return f"{self.minute} {self.hour} * * {'*' if self.weekday is None else self.weekday}"


@dataclass(frozen=True, slots=True)
class Disruption:
    """The dial's k8s.disruption block, checked.

    Attributes:
        posture: One of DISRUPTION_POSTURES.
        window: The maintenance window, or None when the dial sets none.
    """

    posture: str = "balanced"
    window: MaintenanceWindow | None = None


def _maintenance_window(dial: dict[str, object]) -> MaintenanceWindow | None:
    """k8s.disruption.maintenance_window, or None when neither field is set."""
    at_window = (*DISRUPTION_AT, "maintenance_window")
    schedule = _scalar(dial, (*at_window, "schedule"))
    duration = _scalar(dial, (*at_window, "duration"))
    if schedule is None and duration is None:
        return None
    if schedule is None or duration is None:
        raise DialError(
            f"{'.'.join(at_window)} needs both schedule and duration, or neither -- a window is "
            "when it opens and how long it stays open"
        )
    opens = _WINDOW_SCHEDULE.match(schedule)
    if not opens or int(opens["minute"]) > 59 or int(opens["hour"]) > 23:
        raise DialError(
            f"{'.'.join(at_window)}.schedule must be 'M H * * *' for every day or 'M H * * D' for "
            f"one weekday (0-6, 0 is Sunday), in UTC, got {schedule!r}"
        )
    weekday = None if opens["weekday"] == "*" else int(opens["weekday"])
    length = _WINDOW_DURATION.match(duration)
    minutes = int(length["hours"] or 0) * 60 + int(length["minutes"] or 0) if length else 0
    window = MaintenanceWindow(int(opens["minute"]), int(opens["hour"]), weekday, minutes)
    if not 0 < minutes < window.period_minutes:
        raise DialError(
            f"{'.'.join(at_window)}.duration must be hours and minutes such as 4h or 3h30m, above "
            f"zero and under the {window.period_minutes // 60}h the window repeats on, got {duration!r}"
        )
    return window


def read_disruption(dial: dict[str, object]) -> Disruption:
    """Read k8s.disruption, the pools' disruption intent, refusing a bad posture or window by name.

    Args:
        dial: The parsed deployment dial.

    Returns:
        The posture, balanced when unset, and the maintenance window or None.

    Raises:
        DialError: The posture is outside DISRUPTION_POSTURES, or the window
            is half set, repeats other than daily or on one weekday, or
            lasts zero time or its whole period.
    """
    posture = _text(dial, (*DISRUPTION_AT, "posture"), "balanced")
    if posture not in DISRUPTION_POSTURES:
        raise DialError(
            f"k8s.disruption.posture must be one of {', '.join(DISRUPTION_POSTURES)}, got {posture!r}"
        )
    return Disruption(posture, _maintenance_window(dial))


def disruption_summary(disruption: Disruption) -> tuple[str, list[str]]:
    """The render's one line for k8s.disruption, and a warning when stateful drift can never run."""
    window = disruption.window
    if window is None:
        warnings = []
        if disruption.posture == "conservative":
            warnings.append(
                "k8s.disruption.posture is conservative and sets no maintenance_window, so drift "
                "never replaces a stateful node until a window is set"
            )
        return f"{disruption.posture}, drift any time", warnings
    hours, minutes = divmod(window.minutes, 60)
    length = (f"{hours}h" if hours else "") + (f"{minutes}m" if minutes else "")
    line = f"{disruption.posture}, drift only inside {window.schedule} for {length} (UTC), ci-burst excepted"
    return line, []


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


# The broker scaler's sizing when the dial names none: the broker ceiling, the
# per-broker MB/s the alarm threshold scales with, the margin above full
# utilisation it fires at, and the alarm window -- three 300s periods in breach,
# the 15-minute stabilisation the scaler's missing cooldown relies on.
MSK_AUTOSCALING_DEFAULTS: dict[str, int | float] = {
    "max_brokers": 6,
    "per_broker_capacity_mb_s": 50,
    "headroom": 1.3,
    "period_seconds": 300,
    "evaluation_periods": 3,
}


def _msk_autoscaling(dial: dict[str, object]) -> dict[str, object]:
    """kafka.msk.autoscaling -- MSK's broker-count scaler, `var.autoscaling` for
    the msk module (terraform/modules/managed-kafka/msk/variables.tf).

    The ceiling, the per-broker capacity, the headroom and the alarm window are
    always sent, from the dial or MSK_AUTOSCALING_DEFAULTS, because the root
    declares none of them a default. `enabled` is sent only when the dial sets it, and `step` too: the
    module derives it from the subnet count when unset, so it is never invented
    here.
    """
    at = ("kafka", "msk", "autoscaling")
    out: dict[str, object] = {}
    enabled = _scalar(dial, (*at, "enabled"))
    if enabled is not None:
        out["enabled"] = _flag(dial, (*at, "enabled"))
    step = _scalar(dial, (*at, "step"))
    if step is not None:
        if not step.isdigit():
            raise DialError(f"kafka.msk.autoscaling.step must be a whole number, got {step!r}")
        out["step"] = int(step)
    for field_name in ("max_brokers", "per_broker_capacity_mb_s", "period_seconds", "evaluation_periods"):
        out[field_name] = _optional_number(dial, (*at, field_name), int(MSK_AUTOSCALING_DEFAULTS[field_name]))
    headroom = _scalar(dial, (*at, "headroom"))
    out["headroom"] = MSK_AUTOSCALING_DEFAULTS["headroom"]
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


def _confluent_rest_ready(dial: dict[str, object]) -> dict[str, int]:
    """kafka.confluent_rest_ready -- `var.rest_ready` for the confluent-cloud body.

    Every field is always sent, from the dial or CONFLUENT_REST_READY_DEFAULTS,
    because neither the root nor the body declares a default. The body's own
    validation refuses a wait that cannot pass inside its ceiling, at plan.
    """
    at = ("kafka", "confluent_rest_ready")
    return {
        name: _optional_number(dial, (*at, name), default)
        for name, default in CONFLUENT_REST_READY_DEFAULTS.items()
    }


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


def _stack_pins(section: str) -> dict[str, str]:
    """The pins in the current stack's `section` of versions.yaml, keyed by pin name.

    Returns whatever the current stack pins, possibly empty: the module that reads
    a pin refuses a plan that leaves it unset (the cluster module for eks-addons,
    the msk module for lambda-runtimes), the same way _toolbox_tool_versions
    leaves a missing tool to the toolbox module.
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
    pins = _node(tree, ("stacks", current, section))
    return {name: value.strip() for name, value in pins.items() if isinstance(value, str) and value.strip()}


# The resolved shape the toolbox instance launches on when the dial names none.
TOOLBOX_SHAPE = "toolbox"


def _toolbox(dial: dict[str, object]) -> dict[str, object]:
    """toolbox.* -- the on-demand SSM-managed troubleshooting instance
    (terraform/modules/toolbox/aws/CONTRACT.md). `enabled` is the
    `dfe-ops bastion up`/`down` toggle. `aws.shape_ref` names the resolved
    shape the instance takes its type and root volume from, and an instance
    type written into the dial is refused. `tool_versions` is NEVER read from
    the dial -- it is assembled from versions.yaml here, so a deployer
    cannot pin a stale tool by hand independent of the SSoT everything else
    in this repo pins through.
    """
    shape_ref = _text(dial, ("toolbox", "aws", "shape_ref"), TOOLBOX_SHAPE)
    _no_instance_type(dial, ("toolbox", "aws"), shape_ref)
    return {
        "enabled": _flag(dial, ("toolbox", "enabled")),
        "aws": {
            "shape_ref": shape_ref,
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


# Rule prefix -> (the URL ECR's CreatePullThroughCacheRule takes, the image host), for the
# upstreams ECR caches with no credential.
_REGISTRY_CACHE_UPSTREAMS: dict[str, tuple[str, str]] = {
    "quay": ("quay.io", "quay.io"),
    "k8s": ("registry.k8s.io", "registry.k8s.io"),
    "ecr-public": ("public.ecr.aws", "public.ecr.aws"),
}

# The same, for the upstreams ECR caches only through a Secrets Manager credential the dial names.
_REGISTRY_CACHE_CREDENTIALED: dict[str, tuple[str, str]] = {
    "docker-hub": ("registry-1.docker.io", "docker.io"),
    "ghcr": ("ghcr.io", "ghcr.io"),
}

# Days a cached image is kept after ECR cached it when the dial names none.
REGISTRY_CACHE_EXPIRY_DAYS = 90


def _registry_cache(dial: dict[str, object]) -> dict[str, object]:
    """registry_cache.* -- the pull-through cache, on unless the dial turns it off.

    The upstreams that need no credential are always rendered. A credentialed upstream is
    rendered only when registry_cache.credentials names its secret, so with none its images
    pull straight from the upstream.
    """
    credentials = _node(dial, ("registry_cache", "credentials"))
    unknown = sorted(set(credentials) - set(_REGISTRY_CACHE_CREDENTIALED))
    if unknown:
        raise DialError(
            f"registry_cache.credentials names {', '.join(unknown)}, which the cache takes no "
            f"credential for -- the upstreams that take one are "
            f"{', '.join(_REGISTRY_CACHE_CREDENTIALED)}"
        )
    upstreams: dict[str, dict[str, object]] = {
        prefix: {
            "upstream_url": url,
            "host": host,
            "requires_credential": False,
            "credential_arn": "",
        }
        for prefix, (url, host) in _REGISTRY_CACHE_UPSTREAMS.items()
    }
    for prefix, (url, host) in _REGISTRY_CACHE_CREDENTIALED.items():
        credential_arn = _text(dial, ("registry_cache", "credentials", prefix))
        if credential_arn:
            upstreams[prefix] = {
                "upstream_url": url,
                "host": host,
                "requires_credential": True,
                "credential_arn": credential_arn,
            }
    return {
        "enabled": _flag(dial, ("registry_cache", "enabled"), default=True),
        "namespace": _text(dial, ("registry_cache", "namespace")),
        "expiry_days": _optional_number(
            dial, ("registry_cache", "expiry_days"), REGISTRY_CACHE_EXPIRY_DAYS
        ),
        "upstreams": upstreams,
    }


def _saas_kafka_settings(
    dial: dict[str, object], provider: str, message_max_bytes: int
) -> dict[str, object]:
    """A SaaS body's own keys: its reach, and where and on what tier the cluster lands.

    Each vendor's tier is always sent, CONFLUENT_DEFAULT_TIER or
    REDPANDA_DEFAULT_TIER when the dial names none, and so is Confluent's REST
    endpoint wait. Any other key the dial leaves blank is omitted, so the root's
    own default applies -- private connectivity, and an environment or resource
    group the body creates for itself. Confluent's tier, environment and wait are
    read for confluent-cloud only, Redpanda's tier and resource group for
    redpanda-cloud only.

    Args:
        dial: The parsed dial.
        provider: confluent-cloud or redpanda-cloud.
        message_max_bytes: The size every landing topic is created with.

    Returns:
        The keys to merge into the root's kafka variable.

    Raises:
        DialError: A value outside the body's vocabulary, an id in the wrong
            vendor's shape, or a message size above a Basic cluster's ceiling.
    """
    settings: dict[str, object] = {}
    connectivity = _scalar(dial, ("kafka", "connectivity"))
    if connectivity is not None:
        if connectivity not in KAFKA_CONNECTIVITY:
            raise DialError(
                f"kafka.connectivity must be one of {', '.join(KAFKA_CONNECTIVITY)}, got {connectivity!r}"
            )
        settings["connectivity"] = connectivity

    if provider == "confluent-cloud":
        tier = _text(dial, ("kafka", "confluent_tier"), CONFLUENT_DEFAULT_TIER)
        if tier not in CONFLUENT_TIERS:
            raise DialError(
                f"kafka.confluent_tier must be one of {', '.join(CONFLUENT_TIERS)}, got {tier!r}"
            )
        settings["confluent_tier"] = tier
        if tier == "basic" and message_max_bytes > CONFLUENT_BASIC_MAX_MESSAGE_BYTES:
            raise DialError(
                f"kafka.msk.message_max_bytes is {message_max_bytes}, above the "
                f"{CONFLUENT_BASIC_MAX_MESSAGE_BYTES} a Confluent basic cluster accepts for a "
                f"topic, and Confluent refuses the topics only at apply, after the cluster "
                f"exists. Lower kafka.msk.message_max_bytes to "
                f"{CONFLUENT_BASIC_MAX_MESSAGE_BYTES} or less, or set kafka.confluent_tier to "
                f"freight or enterprise"
            )
        environment = _scalar(dial, ("kafka", "confluent_environment_id"))
        if environment is not None:
            if not _CONFLUENT_ENVIRONMENT_ID.match(environment):
                raise DialError(
                    "kafka.confluent_environment_id must be a Confluent environment id: "
                    "env- followed by letters and digits"
                )
            settings["confluent_environment_id"] = environment
        settings["confluent_rest_ready"] = _confluent_rest_ready(dial)

    if provider == "redpanda-cloud":
        redpanda_tier = _text(dial, ("kafka", "redpanda_tier"), REDPANDA_DEFAULT_TIER)
        if redpanda_tier not in REDPANDA_TIERS:
            raise DialError(
                f"kafka.redpanda_tier must be one of {', '.join(REDPANDA_TIERS)}, got {redpanda_tier!r}"
            )
        settings["redpanda_tier"] = redpanda_tier
        group = _scalar(dial, ("kafka", "redpanda_resource_group_id"))
        if group is not None:
            if not _REDPANDA_RESOURCE_GROUP_ID.match(group):
                raise DialError(
                    "kafka.redpanda_resource_group_id must be a Redpanda resource group id, "
                    "which is a UUID"
                )
            settings["redpanda_resource_group_id"] = group
    return settings


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
    The two SaaS bodies also take the keys _saas_kafka_settings reads.
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
            **_saas_kafka_settings(dial, provider, tuning["message_max_bytes"]),
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


# CloudWatch Logs' own retention floor, which a log group AWS forces into
# existence under the otel sink keeps when the dial names none.
TELEMETRY_FORCED_RETENTION_DAYS = 1

# The retention periods CloudWatch Logs accepts, and refuses anything else.
CLOUDWATCH_RETENTION_DAYS = (
    1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557,
    2922, 3288, 3653,
)


def _telemetry(dial: dict[str, object], cloud: str) -> dict[str, object]:
    """The telemetry dial for this cloud, defaulted to DFE's own policy.

    DFE's monitoring goes to its own OTel feed and HyperDX, never CloudWatch,
    so sink defaults to otel -- cloudwatch is the opt-in AWS-native path.
    retention_days defaults to 2 under otel (bounding an S3 lifecycle for the
    touchpoints AWS forces into CloudWatch anyway) and 7 under cloudwatch (an
    AWS-native compliance path, kept short rather than left at a service
    default). forced_retention_days is the CloudWatch log group AWS forces into
    existence whatever the sink (the EKS audit log under otel, the MSK broker
    scaler's Lambda log), defaulting to CloudWatch's floor. Read from
    telemetry.<cloud> so a future gcp or azure root carries its own block
    rather than sharing one.
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

    forced = _optional_number(dial, ("telemetry", cloud, "forced_retention_days"), TELEMETRY_FORCED_RETENTION_DAYS)
    if forced not in CLOUDWATCH_RETENTION_DAYS:
        raise DialError(
            f"telemetry.{cloud}.forced_retention_days is {forced}, which CloudWatch Logs refuses -- "
            f"one of {', '.join(str(days) for days in CLOUDWATCH_RETENTION_DAYS)}"
        )

    return {"sink": sink, "retention_days": retention_days, "forced_retention_days": forced}


CLICKHOUSE_MODES = ("in_cluster", "external")
CLICKHOUSE_EXTERNAL_FIELDS = ("host", "port", "native_port", "secure", "user")
# ClickHouse Cloud's HTTPS and native TLS ports, and the account every service is created with.
CLICKHOUSE_CLOUD_PORT = 8443
CLICKHOUSE_CLOUD_NATIVE_PORT = 9440
CLICKHOUSE_CLOUD_USER = "default"
# One DNS name, so no scheme, port, path or space rides into a tcp:// or https:// URL built from it.
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def _clickhouse_mode(dial: dict[str, object]) -> str:
    """clickhouse.mode, in_cluster when the dial names none, refused outside the two modes."""
    mode = _text(dial, ("clickhouse", "mode"), "in_cluster")
    if mode not in CLICKHOUSE_MODES:
        raise DialError(f"clickhouse.mode must be one of {', '.join(CLICKHOUSE_MODES)}, got {mode!r}")
    return mode


def _clickhouse_port(dial: dict[str, object], field: str, default: int) -> int:
    """One of the external server's ports, or `default` when the dial names none."""
    raw = _scalar(dial, ("clickhouse", field))
    if raw is None:
        return default
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise DialError(f"clickhouse.{field} must be a port number 1-65535, got {raw!r}")
    return int(raw)


def _clickhouse_host(dial: dict[str, object]) -> str:
    """endpoints.clickhouse_host as the aws root receives it.

    In external mode clickhouse.host is that value, so the root reads the external
    server on the key it already reads for ClickHouse. A host is never echoed in a
    refusal, because a cycle's dial carries an account's own server.

    Raises:
        DialError: external mode with no host, a host that is not a bare name, or
            a different host on endpoints.clickhouse_host.
    """
    endpoint = _text(dial, ("endpoints", "clickhouse_host"))
    if _clickhouse_mode(dial) != "external":
        return endpoint
    host = _text(dial, ("clickhouse", "host"))
    if not host:
        raise DialError(
            "clickhouse.mode is external and clickhouse.host is empty -- name the server the "
            "deployment connects to"
        )
    if not _HOSTNAME.match(host):
        raise DialError("clickhouse.host must be a bare host name, with no scheme, port, path or space")
    if endpoint and endpoint != host:
        raise DialError(
            "endpoints.clickhouse_host and clickhouse.host name different servers -- in external "
            "mode they are one fact, so drop endpoints.clickhouse_host or make the two match"
        )
    return host


def _clickhouse(dial: dict[str, object]) -> dict[str, object]:
    """clickhouse.* -- where ClickHouse runs, as the aws root's clickhouse variable.

    in_cluster renders the mode alone, since nothing else is read for it. external
    takes ClickHouse Cloud's ports, TLS and account where the dial names none; its
    host travels on endpoints.clickhouse_host (_clickhouse_host).

    Raises:
        DialError: An unknown mode, a password in the dial, an external field set
            beside in_cluster, or an external field outside its vocabulary.
    """
    if _scalar(dial, ("clickhouse", "password")) is not None:
        raise DialError(
            "clickhouse.password is never a dial field, because the dial is rendered into files -- "
            "set TF_VAR_clickhouse_external_password where the plan runs"
        )
    mode = _clickhouse_mode(dial)
    if mode == "in_cluster":
        stray = [field for field in CLICKHOUSE_EXTERNAL_FIELDS if _scalar(dial, ("clickhouse", field)) is not None]
        if stray:
            raise DialError(
                f"clickhouse.{stray[0]} is read only with clickhouse.mode external, and the dial says "
                "in_cluster -- delete it, or set the mode to external"
            )
        return {"mode": mode}
    _clickhouse_host(dial)
    return {
        "mode": mode,
        "port": _clickhouse_port(dial, "port", CLICKHOUSE_CLOUD_PORT),
        "native_port": _clickhouse_port(dial, "native_port", CLICKHOUSE_CLOUD_NATIVE_PORT),
        "secure": _flag(dial, ("clickhouse", "secure"), default=True),
        "user": _text(dial, ("clickhouse", "user"), CLICKHOUSE_CLOUD_USER),
    }


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

    kafka = _kafka(dial)
    clickhouse = _clickhouse(dial)
    az_count = _az_count(dial)
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
        **({"eks_addon_versions": _stack_pins("eks-addons")} if cloud == "aws" else {}),
        **({"lambda_runtimes": _stack_pins("lambda-runtimes")} if cloud == "aws" else {}),
        "network": {
            "nat": _required(dial, ("network", "nat")),
            "az_count": az_count,
            "subnet_newbits": _subnet_newbits(dial, az_count),
        },
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
        "clickhouse": clickhouse,
        "seeds": _seeds(str(kafka["provider"]), str(clickhouse["mode"])),
        "endpoints": {
            "clickhouse_host": _clickhouse_host(dial),
            "kafka_bootstrap": _text(dial, ("endpoints", "kafka_bootstrap")),
            "otel_endpoint": _text(dial, ("endpoints", "otel_endpoint")),
        },
        "repo_url": _required(dial, ("k8s", "repo_url")),
        "target_revision": _required(dial, ("k8s", "target_revision")),
        "state": {
            "bucket": _required(dial, ("state", "bucket")),
            "key": _required(dial, ("state", "key")),
            "region": _required(dial, ("state", "region")),
        },
        "tags": _tags(dial, cloud),
        "toolbox": _toolbox(dial),
        **({"registry_cache": _registry_cache(dial)} if cloud == "aws" else {}),
        "edge": _edge(dial),
        # A variable of its own, beside the guarded run's edge_self_ranges, which
        # this render never writes because the dial carries no such fact.
        "edge_trusted_proxy_cidrs": _proxy_hop_cidrs(dial),
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

    # Every refusal runs before the env file is touched: a later step reads
    # whatever file is there, so a refused dial must leave none behind.
    try:
        conflicts = _edge_alias_conflicts(dial)
        if conflicts:
            raise DialError("; ".join(conflicts))
        deprecated = _edge_deprecations(dial)
        edge_flags = _edge_flags(dial)
        edge_enums = _edge_enums(dial)
        _edge_refusals(dial, edge_flags)
        fence, fence_warnings = web_fence(
            _edge_cidrs(dial, ("edge", "product", "allowed_cidrs")), edge_enums["edge.flavour"]
        )
        ingest_door = edge_enums["edge.groups.ingest.door"]
        ingest_on = edge_flags["edge.groups.ingest.enabled"]
        ingest_line, ingest_warnings = ingest_fence(dial, ingest_door, ingest_on)
        _proxy_hop_cidrs(dial)
        controller_pool = _controller_pool(dial)
        disruption_line, disruption_warnings = disruption_summary(read_disruption(dial))
    except DialError as error:
        print(f"render_dial: {error}", file=sys.stderr)
        return 1

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
        private_file.write_private(args.out, ENV_TEMPLATE.read_text(encoding="utf-8"))
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

    for line in deprecated:
        print(f"render_dial: deprecated -- {line}", file=sys.stderr)

    public = ["dfe-ui"] if edge_flags["edge.product.public"] else []
    public += [name for name in ADMIN_UIS if edge_flags[f"edge.admin_uis.public.{name}"]]
    tier2 = _edge_tier2_on(
        {**edge_enums, INGEST_TIER2_KEY: ingest_opened(dial, edge_enums, edge_flags)}, edge_flags
    )
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
        "  web allow-list (edge.product.allowed_cidrs, pasted as exposure.groups.<group>.allow): "
        f"{fence}",
        file=sys.stderr,
    )
    for line in fence_warnings:
        print(f"render_dial: WARNING -- {line}", file=sys.stderr)
    print(
        "  receiver ingest door (edge.groups.ingest, pasted as exposure.groups.ingest): "
        f"{ingest_line}",
        file=sys.stderr,
    )
    for line in ingest_warnings:
        print(f"render_dial: WARNING -- {line}", file=sys.stderr)
    print(
        "  fleet tunnel (edge.ingest.tunnel.enabled): "
        + ("on" if edge_flags["edge.ingest.tunnel.enabled"]
           else "off -- the module offers the tunnel, the deployment turns it on"),
        file=sys.stderr,
    )
    token_path = _scalar(dial, (*OTEL_AT, "auth", "remoteKey"))
    print(
        "  OTLP ingest from outside the cluster (edge.ingest.otel.enabled): "
        + (f"on -- bearer token from {token_path}; paste the block under otel.ingress in"
           " the deploy repo's infra/common.yaml"
           if edge_flags["edge.ingest.otel.enabled"]
           else "off -- in-cluster senders use the collector's Service"),
        file=sys.stderr,
    )
    print(
        "  tier 2 opt-ins that are ON: " + (", ".join(tier2) if tier2 else "none"),
        file=sys.stderr,
    )
    print(
        "  this renderer applies none of it but edge.product.trusted_proxy_cidrs, which"
        " --tofu hands a cloud root -- paste the rest into the deploy repo's values"
        " overlay, the allow lists as exposure.groups in its infra/common.yaml",
        file=sys.stderr,
    )

    print(
        f"KRaft metadata quorum (kafka.controller_pool): {controller_pool}",
        file=sys.stderr,
    )
    print(f"Pool disruption (k8s.disruption): {disruption_line}", file=sys.stderr)
    for line in disruption_warnings:
        print(f"render_dial: WARNING -- {line}", file=sys.stderr)

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
