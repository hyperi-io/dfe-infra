#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/resolve_sizing.py
#  Purpose:      Turn a deployment dial into a sized deployment -- node counts,
#                CPU, RAM, disk, IOPS, partitions and retention -- from the two
#                sizing SSoT files plus the cloud's live API.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Resolve a deployment's size from one throughput estimate and one focus dial.

The operator turns two dials -- ``sizing.ingest_gb_per_day`` (or nothing, and the
tyre-kick floor applies) and ``sizing.focus`` -- and everything else is derived
here. Tofu and the charts RECEIVE numbers; they never compute them.

Two stages, because the ratios are target-agnostic and the shapes are not:

    core      sizing/sizing.yaml + the dial -> brokers, per-broker vCPU / RAM /
              disk / IOPS / MB/s, partitions, retention, ClickHouse replicas /
              RAM / disk, Keeper, the KRaft controller floor. No cloud in sight.
    target    shapes/compute-shapes.yaml + the cloud's live API -> the concrete
              instance type, its price, its generation and the silent caps it
              imposes, asserted against the volume profile.

Five artefacts come out, all under ``--out`` (the repo root by default) -- six
on a ``cloud: onprem`` dial, which also gets ``sizing/<tier>.nodes.json``:

    shapes/resolved/<cloud>-<region>.json   the committed shape answer, merged
                                   over what is already there so another
                                   resolve's entries survive; an API change is
                                   a reviewed diff. Keyed by region -- instance
                                   generation availability differs by region,
                                   so one region's answer is never another's.
    sizing.auto.tfvars.json        the tofu inputs, only keys the root declares.
                                   At the ROOT of --out, beside
                                   render_dial.py --tofu's own
                                   dial.auto.tfvars.json -- tofu auto-loads
                                   *.auto.tfvars.json only from the root
                                   module directory, never a subdirectory.
                                   This script is the single writer of
                                   node_pools: it merges the dial's own
                                   node_pools.system with what it derives, so
                                   the two producers never collide on it.
    sizing/<tier>.values.yaml      the chart values overlay
    sizing/<tier>.report.md        what was sized, from which ratio, at which
                                   confidence, at what price, and where the
                                   ceiling is
    sizing/resolved.yaml           the machine-comparable state a re-resolve
                                   diffs against -- see --previous below. Only
                                   carries a value for the locked fields this
                                   script itself derives (partition_count,
                                   cloud_token, msk_broker_type, and az_count on
                                   a populated cloud) -- storage_model and
                                   controller_mode are deployer-set chart
                                   values this script never touches, so they
                                   carry no entry and are never compared here.
    sizing/<tier>.nodes.json       on-prem only: the same node demand as the
                                   report's own table, machine-readable for
                                   scripts/check_node_capacity.py to check
                                   against a live cluster before bootstrap
                                   converges.

A deployment repo commits ``sizing/resolved.yaml``, so a re-size can be checked
against what is already live. Pass the committed copy back with ``--previous``
and a field named in ``sizing.yaml``'s ``locked:`` section that moved between
the two runs is a LOCKED CHANGE: the resolver refuses to write anything and
exits 3, naming the field, the old and new value, and the reason, unless
``--migrate`` is also passed -- which accepts the change and writes the
artefacts as normal. ``--migrate`` with no ``--previous`` is a usage error, and
a ``--previous`` file with no locked-field difference resolves exactly as
without one.

    python3 scripts/resolve_sizing.py --dial deployment.yaml --live
    python3 scripts/resolve_sizing.py --dial deployment.yaml \
        --fixtures scripts/tests/fixtures/sizing --cloud aws --out /tmp/out
    python3 scripts/resolve_sizing.py --dial deployment.yaml \
        --fixtures scripts/tests/fixtures/sizing --cloud aws \
        --previous sizing/resolved.yaml --migrate
    python3 scripts/resolve_sizing.py capture --region us-west-2 \
        --fixtures scripts/tests/fixtures/sizing

Exit status is 0 on a resolve, 1 on a bad dial or a failed assertion, 2 when
the estimate is above the generator cap -- a refusal, not a failure, and it
prints the professional-services message with the cap and the provider named
-- and 3 when ``--previous`` finds a locked field changed and ``--migrate``
was not passed.

UNITS, stated once because mixing them is how a sizing goes quietly wrong. GB and
MB are decimal (10^9 and 10^6 bytes), which is what the vendors quote throughput
and daily volume in. GiB is 2^30 bytes, which is what Kubernetes, EBS and the
charts take. Every field name says which.

Stdlib only, like every other script here: the SSoT files are read through
scripts/yaml_subset.py, and the AWS API is reached by shelling out to the `aws`
CLI the operator already has.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from aws_cli import run_aws
from yaml_subset import YamlSubsetError, at
from yaml_subset import parse as parse_yaml_subset

REPO_ROOT = Path(__file__).resolve().parent.parent
SIZING_FILE = REPO_ROOT / "sizing" / "sizing.yaml"
SHAPES_FILE = REPO_ROOT / "shapes" / "compute-shapes.yaml"

# Only the scale tier is sized. single and slim are one node of everything with
# fixed shapes, and nothing here touches them.
SCALE_TIER = "scale"
TIER_REFUSAL = (
    "sizing resolves the scale tier only -- {tier} is one node of everything with a fixed "
    "shape, and nothing in sizing.yaml applies to it"
)

# An estimate above the generator cap is refused rather than extrapolated, and a
# refusal is not a crash: it is the product boundary, so it gets its own status.
EXIT_REFUSED = 2

# A locked field moved between --previous and this run and --migrate was not
# passed: also a refusal, not a failure, and its own status so a caller can
# tell a cap breach (2) from a locked-field change (3) without parsing stderr.
EXIT_LOCKED_CHANGE = 3

# Where a per-target knob matrix (blocked / renamed / managed, each with its
# reason) is read from when one exists. Absent, the report says so and prints the
# locked fields sizing.yaml already carries instead of implying nothing was
# dropped.
TARGETS_DIR = REPO_ROOT / "sizing" / "targets"

# Which Kafka provider key in sizing.yaml a dial's kafka.provider selects.
# confluent-cloud and redpanda-cloud keep their own dial spelling: neither
# derives a shape (see SAAS_KAFKA_PROVIDERS below), so there is no sizing.yaml
# section to alias them onto the way msk aliases onto msk-express.
KAFKA_PROVIDERS = {
    "strimzi": "strimzi",
    "redpanda": "redpanda",
    "msk": "msk-express",
    "msk-express": "msk-express",
    "confluent-cloud": "confluent-cloud",
    "redpanda-cloud": "redpanda-cloud",
}

# The two fully vendor-managed Kafka bodies: no broker count, memory or disk is
# ours to derive, so size_core skips both the kafka-broker and kraft-controller
# nodes and run_resolve never asks a cloud's shape entries for either -- there
# is no EC2 (or equivalent) instance to select at all, unlike msk-express,
# which still picks a broker size from MSK's own compute-family namespace.
SAAS_KAFKA_PROVIDERS = ("confluent-cloud", "redpanda-cloud")

# Values the charts declare and the resolver needs as INPUTS to a derivation.
# They live in the chart because that is where they are consumed; each one is
# overridable from the dial, and the report prints the figure it used with this
# source beside it.
#
# The source cites the VALUES KEY PATH, never a line number: a line number
# drifts the moment a comment above it grows or shrinks, and nothing re-checks
# it against the file, so a stale citation reads as evidence while pointing at
# an unrelated comment. The key path is what the test in
# test_resolve_sizing.py can actually assert exists in the chart's values.yaml.
CHART_DEFAULTS = {
    # kafka.sizing.consumerCeiling -- a classic consumer group assigns at
    # most one consumer per partition, so partitions never go below this.
    "consumer_ceiling": (10, "helm/charts/kafka/values.yaml#kafka.sizing.consumerCeiling"),
    # kafka.sizing.minPartitionsPerBroker -- 3 brokers give 12, which 3, 6 and
    # 12 brokers all divide evenly.
    "min_partitions_per_broker": (
        4,
        "helm/charts/kafka/values.yaml#kafka.sizing.minPartitionsPerBroker",
    ),
    # kafka.sizing.perPartitionCapMbS -- one partition has one leader, so this
    # is what a single partition can absorb. Used where sizing.yaml states no
    # per-provider cap of its own.
    "per_partition_cap_mb_s": (
        15,
        "helm/charts/kafka/values.yaml#kafka.sizing.perPartitionCapMbS",
    ),
    # kafka.retention.usableFraction -- the rest of the PVC is index,
    # snapshot and in-flight segment space.
    "usable_fraction": (0.85, "helm/charts/kafka/values.yaml#kafka.retention.usableFraction"),
    # kafka.broker.networkMbS -- broker NIC throughput in MB/s, full
    # duplex, and what stops Cruise Control filling the NIC during a rebalance.
    "broker_network_mb_s": (1250, "helm/charts/kafka/values.yaml#kafka.broker.networkMbS"),
    # kafka.storage.size -- the floor a broker PVC never goes
    # below, so a deployment with no estimate still asks for a real volume.
    "kafka_broker_disk_gib": (20, "helm/charts/kafka/values.yaml#kafka.storage.size"),
    # clickhouse.storage.size -- the same floor for a ClickHouse replica.
    "clickhouse_disk_gib": (
        50,
        "helm/charts/clickhouse-cluster/values.yaml#clickhouse.storage.size",
    ),
}

# Use cases whose IO is CONTINUOUS, so a burstable instance size cannot carry
# them. A shape entry that declares `sustained` of its own overrides this.
SUSTAINED_USE_CASES = ("kafka-broker", "clickhouse", "keeper")

# Which use cases a scale deployment sizes. msk-broker joins them only when the
# dial names a managed provider.
CORE_USE_CASES = ("eks-system", "general", "kafka-broker", "kraft-controller", "clickhouse", "keeper")

# Every workload class compute-shapes.yaml declares, which is what a deployer may
# name in an override.
USE_CASES = (
    "eks-system",
    "general",
    "kafka-broker",
    "kraft-controller",
    "clickhouse",
    "keeper",
    "ci-burst",
    "msk-broker",
    "toolbox",
)

# Which core requirement a shape entry reads when it is not its own name.
BROKER_DEMAND = {"msk-broker": "kafka-broker"}

# Workloads that never become a FIXED-size EKS managed node group: msk-broker
# because the vendor runs it and tofu asks for it by the vendor's own unit;
# eks-system, general and ci-burst because they take Karpenter's dynamic
# capacity instead -- ci-burst most of all, since a fixed node group cannot
# express "spot, with a 100% disruption budget" the way a Karpenter NodePool
# can (see compute-shapes.yaml's ci-burst entry).
NOT_A_NODE_POOL = ("eks-system", "general", "msk-broker", "ci-burst")

# An instance type name: family letters, generation digits, the processor and
# modifier letters, then the size -- m9g.large, r8gd.2xlarge, c8g.metal-24xl.
# A name this cannot read is skipped and counted, never guessed at, and too many
# skips fail the selection loudly rather than downgrading a pool quietly.
TYPE_NAME = re.compile(
    r"^(?P<family>[a-z]+)(?P<generation>\d+)(?P<suffix>[a-z]*)\.(?P<size>[a-z0-9-]+)$"
)
MAX_UNPARSED_FRACTION = 0.05

# The Pricing API's physicalProcessor is the ONLY field that names the Graviton
# generation; EC2's ProcessorInfo does not carry it. The parsed name is what
# ranks, and this cross-checks it.
GRAVITON_PROCESSOR = re.compile(r"AWS Graviton(?P<generation>\d+)?")

# gp3's size-derived maxima, from the EBS volume-type documentation. There is no
# describe-volume-types API, so these are doc-sourced constants with provenance
# rather than a live read -- the one place in the resolver that is true.
GP3_MAX_IOPS = 80000
GP3_IOPS_PER_GIB = 500
GP3_MAX_THROUGHPUT_MIB_S = 2000
GP3_MIB_S_PER_IOPS = 0.25
# The free baseline, and the lowest gp3 can be provisioned at -- so an instance
# whose own baseline is under this cannot carry a gp3 volume at its rated speed.
GP3_MIN_THROUGHPUT_MIB_S = 125
GP3_SOURCE = "https://docs.aws.amazon.com/ebs/latest/userguide/general-purpose.html"

# On-demand monthly hours. The gp3 storage price is NOT here: it is per region
# (compute-shapes.yaml's clouds.<cloud>.storage_pricing), read by
# _gp3_price_per_gib_month below, alongside the live per-region compute price
# the A5 spend guard already uses.
HOURS_PER_MONTH = 730

BYTES_PER_GIB = 1024**3
BYTES_PER_GB = 10**9
BYTES_PER_MB = 10**6
SECONDS_PER_DAY = 86400


class ResolveError(ValueError):
    """A dial, a fixture or an API answer the resolver cannot size from."""


class AboveCapError(ValueError):
    """The estimate is above the generator cap. A product boundary, not a fault."""


# ---------------------------------------------------------------------------
# Reading the SSoT files
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict[str, object]:
    """Parse one restricted-YAML SSoT file, naming the file in any error."""
    try:
        return parse_yaml_subset(path.read_text(encoding="utf-8"), source=str(path))
    except OSError as err:
        raise ResolveError(f"cannot read {path}: {err}") from err
    except YamlSubsetError as err:
        raise ResolveError(str(err)) from err


def _at(tree: object, *path: str) -> object:
    """Walk a parsed tree, answering None rather than raising on a missing key."""
    return at(tree, path)


def _scalar(tree: object, *path: str) -> str | None:
    """The non-empty scalar at `path`, else None."""
    node = _at(tree, *path)
    return node.strip() if isinstance(node, str) and node.strip() else None


@dataclass(frozen=True, slots=True)
class Ratio:
    """One sizing ratio, carried with the provenance the report prints."""

    path: str
    value: str
    unit: str
    source: str
    read: str
    confidence: str

    @property
    def number(self) -> float:
        """The ratio as a number, refusing the ones that carry an expression."""
        try:
            return float(self.value)
        except ValueError as err:
            raise ResolveError(f"{self.path}: {self.value!r} is not a number") from err


def _ratio(sizing: dict[str, object], *path: str) -> Ratio:
    """Read one ratio and its provenance, refusing a half-written entry."""
    node = _at(sizing, *path)
    if not isinstance(node, dict) or "value" not in node:
        raise ResolveError(f"sizing.yaml has no ratio at {'.'.join(path)}")
    return Ratio(
        path=".".join(path),
        value=str(node["value"]),
        unit=str(node.get("unit", "")),
        source=str(node.get("source", "")),
        read=str(node.get("read", "")),
        confidence=str(node.get("confidence", "")),
    )


# ---------------------------------------------------------------------------
# The dial
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Dial:
    """The fields the resolver reads out of a deployment dial."""

    path: Path
    tier: str
    cloud: str
    region: str
    target: str
    kafka_provider: str
    ingest_gb_per_day: float | None
    focus: str
    peak_factor: float | None
    avg_event_bytes: float | None
    compression_ratio: float | None
    retention_ttl_days: float | None
    hunt_window_days: float | None
    archiver_lag_hours: float | None
    spend_warn_usd_month: float | None
    allow_undersized: bool
    name: str
    # How many AZs the VPC spans (network.az_count, 2-6). Feeds the catalogue's
    # own zone slice (fetch_live/fetch_fixtures) and the broker-count multiple,
    # so a 4- or 5-AZ deployment gets a cluster that actually spreads across
    # them rather than the 3 every deployment got regardless of the dial.
    az_count: int = 3
    overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    # The dial's own `node_pools:` block (e.g. `system`) -- groups a deployer
    # sizes directly rather than a ratio deriving. render_dial.py no longer
    # writes these into tofu; this script merges them with what it derives, so
    # there is a single writer of the `node_pools` tofu variable.
    node_pools: dict[str, dict[str, object]] = field(default_factory=dict)


def _dial_number(tree: object, *path: str) -> float | None:
    """A numeric dial field, refusing anything that is not a number by name."""
    raw = _scalar(tree, *path)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError as err:
        raise ResolveError(f"{'.'.join(path)} must be a number, got {raw!r}") from err


def read_dial(path: Path, cloud: str | None = None, target: str | None = None) -> Dial:
    """Read the dial's sizing slice, with the two test overrides applied over it."""
    if not path.is_file():
        raise ResolveError(f"no deployment dial at {path}")
    tree = _load(path)
    provision_cloud = _scalar(tree, "target", "provision", "cloud") or _scalar(tree, "k8s", "cloud")
    allow = (_scalar(tree, "sizing", "allow_undersized") or "false").lower()
    if allow not in ("true", "false"):
        raise ResolveError(f"sizing.allow_undersized must be true or false, got {allow!r}")
    provider = (_scalar(tree, "kafka", "provider") or "strimzi").lower()
    if provider not in KAFKA_PROVIDERS:
        raise ResolveError(
            f"kafka.provider {provider!r} is not one of {', '.join(sorted(KAFKA_PROVIDERS))}"
        )
    kafka_provider = KAFKA_PROVIDERS[provider]
    # k8s.cloud's own token for an unprovisioned/existing cluster is `local`
    # (deployment.example.yaml's default); compute-shapes.yaml has no `local`
    # key, only `onprem`, so the resolve fails outright unless the operator
    # knows to pass --cloud onprem by hand. Map it here so the default
    # invocation produces the on-prem node-requirements file the preflight in
    # bootstrap.sh looks for.
    resolved_cloud = cloud or provision_cloud or "onprem"
    if resolved_cloud == "local":
        resolved_cloud = "onprem"
    # The target overlay defaults to the cloud's own key -- what we run on AWS
    # is the aws overlay -- EXCEPT when Kafka itself is a fully vendor-managed
    # SaaS body: confluent-cloud.yaml and redpanda-cloud.yaml carry the knobs
    # that govern the BROKER, which is what a re-size actually needs to see
    # when there is no cloud-side shape to report on for it.
    default_target = kafka_provider if kafka_provider in SAAS_KAFKA_PROVIDERS else resolved_cloud
    az_count_raw = _dial_number(tree, "network", "az_count")
    az_count = int(az_count_raw) if az_count_raw is not None else 3
    if not 2 <= az_count <= 6:
        raise ResolveError(
            f"network.az_count must be between 2 and 6 -- below 2 there is no redundancy to speak "
            f"of, and no AWS region offers more than 6 -- got {az_count}"
        )
    return Dial(
        path=path,
        tier=_scalar(tree, "profile") or "single",
        cloud=resolved_cloud,
        region=_scalar(tree, "target", "provision", "region") or _scalar(tree, "k8s", "region") or "",
        target=target or default_target,
        kafka_provider=kafka_provider,
        ingest_gb_per_day=_dial_number(tree, "sizing", "ingest_gb_per_day"),
        focus=_scalar(tree, "sizing", "focus") or "economy",
        peak_factor=_dial_number(tree, "sizing", "peak_factor"),
        avg_event_bytes=_dial_number(tree, "sizing", "avg_event_bytes"),
        compression_ratio=_dial_number(tree, "sizing", "compression_ratio"),
        retention_ttl_days=_dial_number(tree, "retention", "default_ttl_days"),
        hunt_window_days=_dial_number(tree, "sizing", "hunt_window_days"),
        archiver_lag_hours=_dial_number(tree, "sizing", "archiver_lag_hours"),
        spend_warn_usd_month=_dial_number(tree, "sizing", "spend_warn_usd_month"),
        allow_undersized=allow == "true",
        name=_scalar(tree, "metadata", "name") or "dfe",
        az_count=az_count,
        overrides=_read_overrides(tree),
        node_pools=_read_node_pools(tree),
    )


# What a deployer may set by hand, per use case, over whatever the ratios derive.
# cpu, memory, disk_gb and replicas replace the abstract requirement, so the
# shape selection sees the deployer's number; instance_type, iops and
# throughput_mibs replace the target's answer after it is picked.
NODE_OVERRIDES = ("cpu", "memory", "disk_gb", "replicas")
SHAPE_OVERRIDES = ("instance_type", "iops", "throughput_mibs")


def _read_overrides(tree: dict[str, object]) -> dict[str, dict[str, str]]:
    """Read sizing.overrides, refusing a field or a use case nothing acts on."""
    node = _at(tree, "sizing", "overrides")
    if node is None:
        return {}
    if isinstance(node, str):
        # The committed dial writes an empty map as `{}`, which the restricted
        # YAML reader hands back as that scalar.
        if node.strip() in ("", "{}"):
            return {}
        raise ResolveError(f"sizing.overrides is a map keyed by use case, not {node!r}")
    if not isinstance(node, dict):
        raise ResolveError("sizing.overrides is a map keyed by use case")
    known = set(NODE_OVERRIDES) | set(SHAPE_OVERRIDES)
    out: dict[str, dict[str, str]] = {}
    for use_case, fields in node.items():
        if use_case not in USE_CASES:
            raise ResolveError(
                f"sizing.overrides.{use_case}: not a use case -- one of {', '.join(USE_CASES)}"
            )
        if not isinstance(fields, dict):
            raise ResolveError(f"sizing.overrides.{use_case}: a map of fields, not a scalar")
        for name, value in fields.items():
            if name not in known:
                raise ResolveError(
                    f"sizing.overrides.{use_case}.{name}: not a field -- one of "
                    f"{', '.join(sorted(known))}"
                )
            if not isinstance(value, str) or not value.strip():
                raise ResolveError(f"sizing.overrides.{use_case}.{name}: empty")
        out[use_case] = {name: str(value).strip() for name, value in fields.items()}
    return out


# The fields render_dial.py's own _node_pools used to read from the dial's
# node_pools block, ported here so this script can merge them with the pools
# it derives -- see build_tfvars and P1-3 in the correctness review.
_NODE_POOL_FIELDS = ("min_size", "max_size", "desired_size", "disk_gb")


def _read_node_pools(tree: dict[str, object]) -> dict[str, dict[str, object]]:
    """Read the dial's own node_pools block -- groups a deployer sizes by hand.

    `system` is the one every cloud dial carries today (the cluster needs
    something to run before Karpenter's own controller can), but nothing here
    assumes the name: whatever the deployer declares is merged with what
    build_tfvars derives, keyed by pool name.
    """
    node = _at(tree, "node_pools")
    if node is None:
        return {}
    if not isinstance(node, dict):
        raise ResolveError("node_pools is a map keyed by pool name")
    out: dict[str, dict[str, object]] = {}
    for name, body in node.items():
        if not isinstance(body, dict):
            raise ResolveError(f"node_pools.{name}: a map of fields, not a scalar")
        shape_ref = _scalar(body, "shape_ref")
        if not shape_ref:
            raise ResolveError(f"node_pools.{name}.shape_ref is required")
        capacity_type = _scalar(body, "capacity_type") or "ON_DEMAND"
        pool: dict[str, object] = {"shape_ref": shape_ref, "capacity_type": capacity_type}
        for field_name in _NODE_POOL_FIELDS:
            raw = _scalar(body, field_name)
            if raw is None:
                raise ResolveError(f"node_pools.{name}.{field_name} is required")
            try:
                pool[field_name] = int(float(raw))
            except ValueError as err:
                raise ResolveError(
                    f"node_pools.{name}.{field_name} must be a number, got {raw!r}"
                ) from err
        pool["labels"] = {}
        pool["taints"] = []
        out[name] = pool
    return out


# ---------------------------------------------------------------------------
# Core -- target-agnostic requirements
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Node:
    """One node class's abstract requirement, before any cloud names a shape."""

    use_case: str
    count: int
    vcpu: int
    ram_gib: int
    disk_gib: int
    iops: int
    throughput_mib_s: int
    why: str


@dataclass(slots=True)
class Core:
    """Everything the ratios derive, with the provenance of what drove each one."""

    tier: str
    focus: str
    headroom: float
    estimated: bool
    ingest_gb_per_day: float
    avg_mb_s: float
    peak_mb_s: float
    peak_factor: float
    required_mb_s: float
    carried_mb_s: float
    nodes: dict[str, Node] = field(default_factory=dict)
    partitions: int = 0
    retention_hours: float = 0.0
    # The two components retention_hours sums -- carried separately so
    # build_values can emit them the way the chart adds them itself, rather
    # than double-counting archiver_lag_hours if a deploy-repo overlay ever
    # sets kafka.retention.archiverLagH on its own.
    consumer_downtime_hours: float = 0.0
    archiver_lag_hours: float = 0.0
    retention_ms: int = 0
    retention_bytes: int = 0
    message_chain_bytes: int = 0
    clickhouse_compressed_gib: float = 0.0
    clickhouse_frequent_gib: float = 0.0
    clickhouse_ram_long_band_gib: float = 0.0
    clickhouse_memory_ratio: float = 0.8
    keeper_parts_per_s: float = 0.0
    # What a quiet root volume asks of the instance -- not what gp3's own free
    # minimum provisions it at. See sizing.yaml's floors.root_volume_demand_*
    # and _resolve_volumes.
    root_volume_demand_mib_s: float = 0.0
    root_volume_demand_iops: int = 0
    notes: list[str] = field(default_factory=list)
    used: list[Ratio] = field(default_factory=list)
    overridden: list[Override] = field(default_factory=list)


@dataclass(slots=True)
class Override:
    """One value the deployer replaced, and what the ratios had derived for it."""

    use_case: str
    what: str
    derived: str
    applied: str


def _ceil_to_multiple(value: int, multiple: int) -> int:
    """Round up to a whole multiple, which is what keeps RF3 even across AZs."""
    return max(multiple, math.ceil(value / multiple) * multiple)


def size_core(sizing: dict[str, object], dial: Dial) -> Core:
    """Derive every target-agnostic number from the ratios and the two dials.

    Args:
        sizing: The parsed sizing SSoT.
        dial: The deployment dial.

    Returns:
        The core requirement, carrying the ratios it used so the report can cite
        each number's source and confidence.

    Raises:
        AboveCapError: The estimate is above the generator cap or the provider
            ceiling, so the resolver refuses rather than extrapolating.
        ResolveError: A ratio the derivation needs is missing.
    """
    if dial.tier != SCALE_TIER:
        raise ResolveError(TIER_REFUSAL.format(tier=dial.tier))

    used: list[Ratio] = []

    def take(*path: str) -> float:
        ratio = _ratio(sizing, *path)
        used.append(ratio)
        return ratio.number

    focus = dial.focus
    if _at(sizing, "focus", focus) is None:
        raise ResolveError(f"sizing.focus {focus!r} is not a level sizing.yaml declares")
    headroom = take("focus", focus, "headroom")

    provider = dial.kafka_provider
    cap = take("ceilings", "generator_cap")
    provider_ceiling = None
    if _at(sizing, "ceilings", provider) is not None:
        provider_ceiling = take("ceilings", provider)

    peak_factor = dial.peak_factor if dial.peak_factor is not None else take("event", "peak_factor")
    estimated = dial.ingest_gb_per_day is not None

    if estimated:
        gb_per_day = float(dial.ingest_gb_per_day)
        binding = min(cap, provider_ceiling) if provider_ceiling is not None else cap
        if gb_per_day > binding:
            message = str(_at(sizing, "ceilings", "professional_services_message") or "")
            named = (
                provider
                if provider_ceiling is not None and binding == provider_ceiling
                else "the generator"
            )
            raise AboveCapError(
                f"{gb_per_day:,.0f} GB/day is above the {binding:,.0f} GB/day ceiling for {named}. "
                f"{message}"
            )
    else:
        # No estimate means the tyre-kick floor, never a throughput band. What it
        # happens to carry is reported below, never targeted.
        gb_per_day = 0.0

    avg_mb_s = gb_per_day * BYTES_PER_GB / SECONDS_PER_DAY / BYTES_PER_MB
    peak_mb_s = avg_mb_s * peak_factor
    required_mb_s = peak_mb_s * (1 + headroom)

    floor_brokers = int(take("floors", "kafka_brokers"))
    floor_broker_vcpu = int(take("floors", "kafka_broker_vcpu"))
    floor_broker_ram = int(take("floors", "kafka_broker_ram_gib"))
    # Every use case carries a mandatory gp3 root volume nothing here ever
    # sizes from a workload -- see _resolve_volumes for why its DEMAND is this
    # small, honest guess rather than gp3's own free minimum.
    root_volume_demand_mib_s = take("floors", "root_volume_demand_mib_s")
    root_volume_demand_iops = int(take("floors", "root_volume_demand_iops"))
    # network.az_count feeds the broker-count multiple as well as the
    # catalogue's own zone slice (see fetch_live/fetch_fixtures): a deployment
    # spanning 4-6 AZs gets a cluster that spreads one-per-zone, rather than
    # always rounding to a multiple of 3 whatever the dial's az_count says.
    broker_count_multiple = max(floor_brokers, dial.az_count)

    nodes: dict[str, Node] = {}
    notes: list[str] = []

    # --- Kafka brokers -------------------------------------------------------
    if provider in SAAS_KAFKA_PROVIDERS:
        # Confluent Cloud and Redpanda Cloud sell eCKUs / throughput, not
        # instances: broker count, memory and disk are the vendor's, so there
        # is nothing to step up here -- only partitions and retention below
        # are ours to derive.
        mb_s_per_vcpu = 0.0
        vcpu_cap = 0
        brokers = broker_count_multiple
        broker_vcpu = 0
        notes.append(
            f"{provider} manages broker count, memory and storage itself -- sizing.yaml carries "
            "no shape for it, so only partitions and retention are ours to derive."
        )
    else:
        if provider == "msk-express":
            mb_s_per_vcpu = take("kafka", provider, "mb_s_per_vcpu")
            vcpu_cap = 48  # the largest Express size AWS sells, express.m7g.12xlarge
            notes.append(
                "MSK Express manages broker memory and storage, so only the broker count and "
                "size are ours to derive -- sizing.yaml records both formulas as provider-managed."
            )
        elif provider == "strimzi":
            mb_s_per_vcpu = take("kafka", provider, "mb_s_per_vcpu")
            # Above this the EBS volume binds before the CPU does and the per-vCPU
            # rate halves, so a bigger broker buys nothing: the cluster grows by
            # BROKER, which is what cookie-cutter scale-out means.
            vcpu_cap = int(take("kafka", provider, "disk_bound_above_vcpu"))
        else:
            mb_s_per_vcpu = take("kafka", provider, "mb_s_per_core")
            vcpu_cap = 32

        total_broker_vcpu = max(
            broker_count_multiple * floor_broker_vcpu, math.ceil(required_mb_s / mb_s_per_vcpu)
        )
        brokers = broker_count_multiple
        while math.ceil(total_broker_vcpu / brokers) > vcpu_cap:
            brokers += broker_count_multiple
        brokers = _ceil_to_multiple(brokers, broker_count_multiple)
        broker_vcpu = max(floor_broker_vcpu, math.ceil(total_broker_vcpu / brokers))

    per_broker_peak_mb_s = peak_mb_s / brokers
    rf_disk = (
        3.0
        if provider in SAAS_KAFKA_PROVIDERS
        else take("kafka", provider, "replication_disk_factor")
    )

    # Partitions come BEFORE broker memory, because Redpanda's RAM formula counts
    # partition replicas and a JVM broker's does not.
    consumer_ceiling, consumer_source = CHART_DEFAULTS["consumer_ceiling"]
    min_per_broker, per_broker_source = CHART_DEFAULTS["min_partitions_per_broker"]
    usable_fraction, usable_source = CHART_DEFAULTS["usable_fraction"]
    if _at(sizing, "kafka", provider, "per_partition_mb_s_cap") is not None:
        per_partition_cap = take("kafka", provider, "per_partition_mb_s_cap")
        cap_source = f"sizing.kafka.{provider}.per_partition_mb_s_cap"
    else:
        per_partition_cap, cap_source = CHART_DEFAULTS["per_partition_cap_mb_s"]
    default_partitions = int(take("partitions", "default_count"))

    terms = [consumer_ceiling, brokers * min_per_broker, default_partitions]
    if per_partition_cap:
        terms.append(math.ceil(peak_mb_s / per_partition_cap))
    partitions = _ceil_to_multiple(max(terms), brokers)
    partitions_per_broker = max(1, math.ceil(partitions * rf_disk / brokers))

    if provider == "strimzi":
        heap_gb = take("kafka", provider, "heap_gb")
        page_cache_s = take("kafka", provider, "page_cache_seconds")
        broker_ram = max(floor_broker_ram, math.ceil(heap_gb + per_broker_peak_mb_s * page_cache_s / 1024))
    elif provider == "redpanda":
        # Redpanda bypasses the page cache and carries per-replica state instead,
        # so its memory grows with PARTITIONS as well as with cores.
        ram_per_core = take("kafka", provider, "ram_gb_per_core")
        ram_mb_per_replica = take("kafka", provider, "ram_mb_per_partition_replica")
        broker_ram = max(
            floor_broker_ram,
            math.ceil(broker_vcpu * ram_per_core + partitions_per_broker * ram_mb_per_replica / 1024),
        )
    else:
        broker_ram = 0

    # ONE assumption, two components -- how long a consumer may be down, plus
    # dfe-archiver's own lag on top -- carried SEPARATELY into the values
    # overlay (see build_values). The chart ADDS them itself
    # (kafka.retention.assumedConsumerDowntimeH + archiverLagH), so summing
    # them here as well would double the archiver term the moment a
    # deploy-repo overlay ever sets archiverLagH on its own.
    consumer_downtime_hours = take("retention", "assumed_consumer_downtime_hours")
    archiver_lag_hours = dial.archiver_lag_hours if dial.archiver_lag_hours is not None else 0.0
    retention_hours = consumer_downtime_hours + archiver_lag_hours
    if provider in ("msk-express", *SAAS_KAFKA_PROVIDERS):
        broker_disk_gib = 0
    else:
        disk_headroom = take("kafka", provider, "disk_headroom")
        # STORAGE IS SIZED FROM THE DAILY VOLUME, not the peak: a retention
        # window of a day or more spans the whole diurnal curve, so peak times
        # retention would count the evening twice. Ingest is what the peak sizes.
        broker_disk_mb = retention_hours * avg_mb_s * 3600 * rf_disk / brokers / disk_headroom
        broker_disk_gib = max(
            CHART_DEFAULTS["kafka_broker_disk_gib"][0],
            math.ceil(broker_disk_mb * BYTES_PER_MB / BYTES_PER_GIB),
        )

    # No EC2 (or equivalent) instance exists for a fully vendor-managed body,
    # so there is no broker Node to size -- run_resolve never asks a cloud's
    # shape entries for kafka-broker either, and build_tfvars emits no pool.
    if provider not in SAAS_KAFKA_PROVIDERS:
        broker_throughput_mib_s = math.ceil(
            per_broker_peak_mb_s
            * (1 + headroom)
            * take("kafka", provider, "replication_network_factor")
            * BYTES_PER_MB
            / (1024 * 1024)
        )
        nodes["kafka-broker"] = Node(
            use_case="kafka-broker",
            count=brokers,
            vcpu=broker_vcpu,
            ram_gib=broker_ram,
            disk_gib=broker_disk_gib,
            iops=0,
            throughput_mib_s=broker_throughput_mib_s,
            why=(
                f"{required_mb_s:,.0f} MB/s required at {peak_factor:g}x peak and {headroom:.0%} "
                f"headroom, over {mb_s_per_vcpu:g} MB/s per vCPU, capped at {vcpu_cap} vCPU a broker"
            ),
        )

    # --- KRaft controllers ---------------------------------------------------
    if provider == "msk-express":
        notes.append("MSK Express runs its own metadata quorum; no controller pool is ours to size.")
    elif provider in SAAS_KAFKA_PROVIDERS:
        notes.append(f"{provider} runs its own metadata quorum; no controller pool is ours to size.")
    else:
        nodes["kraft-controller"] = Node(
            use_case="kraft-controller",
            count=int(take("floors", "kraft_controllers")),
            vcpu=int(take("kraft_controller", "vcpu_floor")),
            ram_gib=int(take("kraft_controller", "ram_gib_floor")),
            disk_gib=int(take("kraft_controller", "disk_gib_floor")),
            iops=0,
            throughput_mib_s=0,
            why="sized by PARTITION COUNT, not throughput -- the floor holds until partitions grow",
        )

    # --- ClickHouse ----------------------------------------------------------
    compression = (
        dial.compression_ratio
        if dial.compression_ratio is not None
        else take("compression", "wire_to_disk_default")
    )
    ttl_days = dial.retention_ttl_days if dial.retention_ttl_days is not None else 90.0
    hunt_days = (
        dial.hunt_window_days
        if dial.hunt_window_days is not None
        else take("clickhouse", "hunt_window_days")
    )
    avg_event_bytes = (
        dial.avg_event_bytes if dial.avg_event_bytes is not None else take("event", "p50_event_bytes")
    )

    compressed_gib = gb_per_day / compression * ttl_days * BYTES_PER_GB / BYTES_PER_GIB
    frequent_gib = gb_per_day / compression * hunt_days * BYTES_PER_GB / BYTES_PER_GIB

    # RAM sizes from the FREQUENT-ACCESS band over the hunt window, because DFE's
    # hunts run over recent data. The long-retention band over the whole dataset
    # is printed beside it, as sizing.yaml's own comment asks.
    frequent_ratio = take("clickhouse", "ram_to_compressed_frequent_access_max")
    long_ratio = take("clickhouse", "ram_to_compressed_long_retention_min")
    memory_usable = take("clickhouse", "server_memory_usage_to_ram_ratio")
    ch_replicas = int(take("floors", "clickhouse_replicas"))
    floor_ch_ram = int(take("floors", "clickhouse_ram_gib"))
    floor_ch_vcpu = int(take("floors", "clickhouse_vcpu"))

    ch_ram = max(floor_ch_ram, math.ceil(frequent_gib / frequent_ratio / memory_usable))
    ram_long_band = compressed_gib / long_ratio

    rows_per_s = (avg_mb_s * BYTES_PER_MB / avg_event_bytes) if avg_event_bytes else 0.0
    rows_per_core = take("clickhouse", "ingest_rows_per_s_per_core_native")
    parse_factor = take("clickhouse", "jsoneachrow_parse_factor")
    ingest_cores = math.ceil(rows_per_s / rows_per_core) if rows_per_core else 0
    ch_vcpu = max(floor_ch_vcpu, math.ceil((ingest_cores * (1 + headroom)) / ch_replicas))
    notes.append(
        f"ClickHouse ingest is sized on the Native rate ({rows_per_core:,.0f} rows/s a core). A "
        f"loader inserting JSONEachRow costs {parse_factor:g}x that, so ingest cores become "
        f"{math.ceil(ingest_cores * parse_factor)} -- the single highest-value spot test."
    )

    ch_headroom = take("clickhouse", "disk_headroom")
    ch_disk_gib = max(
        CHART_DEFAULTS["clickhouse_disk_gib"][0], math.ceil(compressed_gib / ch_headroom)
    )
    nodes["clickhouse"] = Node(
        use_case="clickhouse",
        count=ch_replicas,
        vcpu=ch_vcpu,
        ram_gib=ch_ram,
        disk_gib=ch_disk_gib,
        iops=0,
        throughput_mib_s=0,
        why=(
            f"{compressed_gib:,.0f} GiB compressed over {ttl_days:g} days at {compression:g}:1, "
            f"RAM from the frequent-access band over a {hunt_days:g}-day hunt window"
        ),
    )
    if estimated:
        notes.append(
            f"One shard, {ch_replicas} replicas: every replica carries the WHOLE dataset, so the "
            f"{ch_disk_gib:,} GiB above is per replica on storageModel local. cached-object puts "
            f"the parts in the object store and sizes the PVC for the cache instead. The "
            f"long-retention RAM band over the whole dataset would want "
            f"{ram_long_band:,.0f} GiB a replica."
        )

    # --- Keeper --------------------------------------------------------------
    flush_bytes = take("keeper", "loader_flush_bytes")
    keeper_iops = int(take("keeper", "iops_target"))
    parts_per_s = (avg_mb_s * BYTES_PER_MB / flush_bytes) if flush_bytes else 0.0
    nodes["keeper"] = Node(
        use_case="keeper",
        count=int(take("floors", "keeper_replicas")),
        vcpu=1,
        ram_gib=int(take("floors", "keeper_ram_gib")),
        disk_gib=max(20, math.ceil(keeper_iops / GP3_IOPS_PER_GIB)),
        iops=keeper_iops,
        throughput_mib_s=0,
        # Keeper fsyncs every Raft append and appends track PARTS CREATED, so
        # the loader's flush size and Keeper's storage profile are one decision.
        why=(
            f"{parts_per_s:,.1f} parts a second at a {flush_bytes / 1024 / 1024:g} MiB loader flush; "
            f"capacity is irrelevant and IOPS is everything"
        ),
    )

    # --- Retention -----------------------------------------------------------
    retention_ms = int(retention_hours * 3600 * 1000)
    retention_bytes = (
        int(broker_disk_gib * BYTES_PER_GIB * usable_fraction / partitions_per_broker)
        if broker_disk_gib
        else 0
    )
    message_chain_bytes = int(take("event", "message_chain_bytes"))

    # What the floor happens to carry, reported and never targeted.
    carried_mb_s = brokers * broker_vcpu * mb_s_per_vcpu / (1 + headroom) / peak_factor

    core = Core(
        tier=dial.tier,
        focus=focus,
        headroom=headroom,
        estimated=estimated,
        ingest_gb_per_day=gb_per_day,
        avg_mb_s=avg_mb_s,
        peak_mb_s=peak_mb_s,
        peak_factor=peak_factor,
        required_mb_s=required_mb_s,
        carried_mb_s=carried_mb_s,
        nodes=nodes,
        partitions=partitions,
        retention_hours=retention_hours,
        consumer_downtime_hours=consumer_downtime_hours,
        archiver_lag_hours=archiver_lag_hours,
        retention_ms=retention_ms,
        retention_bytes=retention_bytes,
        message_chain_bytes=message_chain_bytes,
        clickhouse_compressed_gib=compressed_gib,
        clickhouse_frequent_gib=frequent_gib,
        clickhouse_ram_long_band_gib=ram_long_band,
        clickhouse_memory_ratio=memory_usable,
        keeper_parts_per_s=parts_per_s,
        root_volume_demand_mib_s=root_volume_demand_mib_s,
        root_volume_demand_iops=root_volume_demand_iops,
        notes=notes,
        used=used,
    )
    if not estimated:
        core.notes.insert(
            0,
            "NO ESTIMATE: the tyre-kick floor applies -- 3 brokers, 3 ClickHouse replicas plus "
            f"Keeper at economy. It carries about {carried_mb_s:,.0f} MB/s "
            f"({carried_mb_s * SECONDS_PER_DAY * BYTES_PER_MB / BYTES_PER_GB:,.0f} GB/day), which "
            "is REPORTED, not targeted.",
        )
    _apply_node_overrides(core, dial.overrides)
    core.notes.append(
        f"Partitions {partitions} = max(consumer ceiling {consumer_ceiling} [{consumer_source}], "
        f"{brokers} brokers x {min_per_broker} [{per_broker_source}], default {default_partitions}"
        + (
            f", peak / {per_partition_cap:g} MB/s a partition [{cap_source}]"
            if per_partition_cap
            else ""
        )
        + f") rounded up to a multiple of {brokers}. Retention bytes use "
        f"{usable_fraction:g} of the PVC [{usable_source}]."
    )
    return core


def _whole(use_case: str, what: str, raw: str) -> int:
    """A whole-number override, refusing anything else by name."""
    try:
        return int(float(raw))
    except ValueError as err:
        raise ResolveError(
            f"sizing.overrides.{use_case}.{what}: {raw!r} is not a number"
        ) from err


def _apply_node_overrides(core: Core, overrides: dict[str, dict[str, str]]) -> None:
    """Replace the derived per-node requirement with whatever the deployer set.

    The shape selection runs afterwards, so an overridden cpu or memory is what
    the instance has to meet. Nothing here skips an assertion: an override that
    breaks a ceiling fails exactly like a derived value would.

    size_core derives a Node for only four of the nine use cases
    (kafka-broker, kraft-controller, clickhouse, keeper) -- eks-system,
    general, ci-burst, msk-broker and toolbox never get one. A cpu/memory/
    disk_gb/replicas override for one of those five has nothing to replace, so
    it is refused BY NAME rather than accepted and silently dropped -- the
    shape-level fields (instance_type, iops, throughput_mibs) still apply to
    all nine through _apply_shape_overrides, which needs no Node.
    """
    for use_case, fields in overrides.items():
        node = core.nodes.get(use_case)
        node_fields = sorted(name for name in fields if name in NODE_OVERRIDES)
        if node is None:
            if node_fields:
                raise ResolveError(
                    f"sizing.overrides.{use_case}: {', '.join(node_fields)} -- this use case has no "
                    "derived node to override (only kafka-broker, kraft-controller, clickhouse and "
                    "keeper do); instance_type, iops and throughput_mibs still apply here"
                )
            continue
        for what, raw in fields.items():
            if what not in NODE_OVERRIDES:
                continue
            attribute = {"cpu": "vcpu", "memory": "ram_gib", "disk_gb": "disk_gib", "replicas": "count"}[
                what
            ]
            derived = getattr(node, attribute)
            applied = _whole(use_case, what, raw)
            setattr(node, attribute, applied)
            core.overridden.append(Override(use_case, what, str(derived), str(applied)))


# ---------------------------------------------------------------------------
# The AWS catalogue -- live or from fixtures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstanceType:
    """One candidate type, with everything the selection and the assertions need."""

    name: str
    family: str
    generation: int
    modifiers: str
    size: str
    vcpu: int
    memory_gib: float
    baseline_iops: int
    baseline_throughput_mib_s: float
    maximum_iops: int
    maximum_throughput_mib_s: float
    instance_store_gb: int
    price_usd_hour: float | None = None
    physical_processor: str = ""


@dataclass(slots=True)
class Catalogue:
    """The cloud's answers, however they were fetched."""

    region: str
    azs: tuple[str, ...]
    types: dict[str, InstanceType]
    offerings: dict[str, set[str]]
    msk_families: tuple[str, ...]
    msk_prices: dict[str, float]
    unparsed: int
    seen: int
    source: str
    captured: str

    def offered_everywhere(self, name: str) -> bool:
        """Whether every AZ the VPC will use offers this type.

        AZs diverge inside one region -- us-west-2d offers 327 of 403 ARM types --
        so a type offered in three of four is a node group that cannot place.
        """
        return all(name in self.offerings.get(az, set()) for az in self.azs)


def _aws(args: list[str]) -> object:
    """Run one aws CLI call and parse its JSON, naming the call on failure."""
    try:
        done = run_aws([*args, "--output", "json"], timeout=600)
    except FileNotFoundError as err:
        raise ResolveError("the aws CLI is not on PATH -- resolve from fixtures instead") from err
    if done.returncode != 0:
        tail = done.stderr.strip().splitlines()[-1:] or ["no stderr"]
        raise ResolveError(f"aws {' '.join(args[:2])} failed: {tail[0]}")
    return json.loads(done.stdout or "{}")


def _parse_name(name: str) -> tuple[str, int, str, str] | None:
    """Split a type name into family, generation, modifiers and size, or None.

    None is the fail-safe answer: the caller skips and counts it rather than
    guessing a generation. A readable name with no Graviton letter -- the a1
    family -- keeps its whole suffix as its modifiers and is excluded later by
    the family filter, so it is not a skip.
    """
    match = TYPE_NAME.match(name)
    if not match:
        return None
    suffix = match["suffix"]
    modifiers = suffix.replace("g", "", 1) if "g" in suffix else suffix
    return match["family"], int(match["generation"]), modifiers, match["size"]


def _instance_from_api(body: dict[str, object]) -> InstanceType | None:
    """Build one InstanceType from a describe-instance-types entry."""
    name = str(body.get("InstanceType", ""))
    parsed = _parse_name(name)
    if parsed is None:
        return None
    family, generation, modifiers, size = parsed
    ebs_info = body.get("EbsInfo")
    ebs = ebs_info.get("EbsOptimizedInfo", {}) if isinstance(ebs_info, dict) else {}
    store = body.get("InstanceStorageInfo")
    return InstanceType(
        name=name,
        family=family,
        generation=generation,
        modifiers=modifiers,
        size=size,
        vcpu=int(body.get("VCpuInfo", {}).get("DefaultVCpus", 0)),
        memory_gib=float(body.get("MemoryInfo", {}).get("SizeInMiB", 0)) / 1024,
        baseline_iops=int(ebs.get("BaselineIops", 0) or 0),
        baseline_throughput_mib_s=float(ebs.get("BaselineThroughputInMBps", 0) or 0),
        maximum_iops=int(ebs.get("MaximumIops", 0) or 0),
        maximum_throughput_mib_s=float(ebs.get("MaximumThroughputInMBps", 0) or 0),
        instance_store_gb=int(store.get("TotalSizeInGB", 0) or 0) if isinstance(store, dict) else 0,
    )


def fetch_live(region: str, az_count: int) -> Catalogue:
    """Read the EC2, offerings and Pricing answers straight from AWS.

    Args:
        region: The AWS region to read.
        az_count: How many zones to keep, in the API's own order -- the VPC
            takes `slice(data.aws_availability_zones.available.names, 0,
            var.network.az_count)` (terraform/modules/kubernetes-cluster/aws/
            vpc.tf:15), so the resolver asserts the chosen type is offered in
            exactly those zones, in that same order, never a sorted one.
    """
    zones = _aws(
        [
            "ec2", "describe-availability-zones", "--region", region,
            "--filters", "Name=state,Values=available",
            "--query", "AvailabilityZones[].ZoneName",
        ]
    )
    all_azs = tuple(str(z) for z in zones)
    if len(all_azs) < az_count:
        raise ResolveError(
            f"{region} offers only {len(all_azs)} availability zones, fewer than the "
            f"{az_count} network.az_count asks for"
        )
    azs = all_azs[:az_count]

    raw = _aws(
        [
            "ec2", "describe-instance-types", "--region", region,
            # SINGULAR. The plural processor-info.supported-architectures is
            # rejected outright, and a rejected filter reads like an empty
            # result rather than a mistake.
            "--filters", "Name=processor-info.supported-architecture,Values=arm64",
        ]
    )
    types: dict[str, InstanceType] = {}
    seen = unparsed = 0
    for body in raw.get("InstanceTypes", []) if isinstance(raw, dict) else []:
        seen += 1
        built = _instance_from_api(body)
        if built is None:
            unparsed += 1
            continue
        types[built.name] = built

    offerings: dict[str, set[str]] = {}
    for az in azs:
        answer = _aws(
            [
                "ec2", "describe-instance-type-offerings", "--region", region,
                "--location-type", "availability-zone",
                "--filters", f"Name=location,Values={az}",
                "--query", "InstanceTypeOfferings[].InstanceType",
            ]
        )
        offerings[az] = {str(name) for name in answer} if isinstance(answer, list) else set()

    prices, processors = _fetch_ec2_prices(region)
    for name, price in prices.items():
        if name in types:
            current = types[name]
            types[name] = InstanceType(
                name=current.name,
                family=current.family,
                generation=current.generation,
                modifiers=current.modifiers,
                size=current.size,
                vcpu=current.vcpu,
                memory_gib=current.memory_gib,
                baseline_iops=current.baseline_iops,
                baseline_throughput_mib_s=current.baseline_throughput_mib_s,
                maximum_iops=current.maximum_iops,
                maximum_throughput_mib_s=current.maximum_throughput_mib_s,
                instance_store_gb=current.instance_store_gb,
                price_usd_hour=price,
                physical_processor=processors.get(name, ""),
            )

    msk_families, msk_prices = _fetch_msk_prices(region)
    return Catalogue(
        region=region,
        azs=azs,
        types=types,
        offerings=offerings,
        msk_families=msk_families,
        msk_prices=msk_prices,
        unparsed=unparsed,
        seen=seen,
        source="aws-api",
        captured=datetime.now(UTC).date().isoformat(),
    )


def _price_from_product(product: dict[str, object]) -> tuple[str, float, str] | None:
    """Pull (instance type, on-demand USD/hour, physicalProcessor) from a product."""
    attributes = product.get("product", {}).get("attributes", {})
    name = str(attributes.get("instanceType", ""))
    if not name:
        return None
    on_demand = product.get("terms", {}).get("OnDemand", {})
    for term in on_demand.values():
        for dimension in term.get("priceDimensions", {}).values():
            usd = dimension.get("pricePerUnit", {}).get("USD")
            if usd is None:
                continue
            price = float(usd)
            if price > 0:
                return name, price, str(attributes.get("physicalProcessor", ""))
    return None


def _fetch_ec2_prices(region: str) -> tuple[dict[str, float], dict[str, str]]:
    """On-demand Linux prices and the physicalProcessor that names the generation.

    The Pricing API lives in us-east-1 whatever region is being priced, and its
    physicalProcessor is the ONLY field that says `AWS Graviton5 Processor`.
    """
    raw = _aws(
        [
            "pricing", "get-products", "--region", "us-east-1", "--service-code", "AmazonEC2",
            "--filters",
            f"Type=TERM_MATCH,Field=regionCode,Value={region}",
            "Type=TERM_MATCH,Field=operatingSystem,Value=Linux",
            "Type=TERM_MATCH,Field=tenancy,Value=Shared",
            "Type=TERM_MATCH,Field=preInstalledSw,Value=NA",
            "Type=TERM_MATCH,Field=capacitystatus,Value=Used",
            "Type=TERM_MATCH,Field=marketoption,Value=OnDemand",
        ]
    )
    prices: dict[str, float] = {}
    processors: dict[str, str] = {}
    for blob in raw.get("PriceList", []) if isinstance(raw, dict) else []:
        product = json.loads(blob) if isinstance(blob, str) else blob
        found = _price_from_product(product)
        if found is None:
            continue
        name, price, processor = found
        prices[name] = price
        processors[name] = processor
    return prices, processors


def _fetch_msk_prices(region: str) -> tuple[tuple[str, ...], dict[str, float]]:
    """MSK broker types, which live in MSK's own computeFamily namespace."""
    raw = _aws(
        [
            "pricing", "get-products", "--region", "us-east-1", "--service-code", "AmazonMSK",
            "--filters", f"Type=TERM_MATCH,Field=regionCode,Value={region}",
        ]
    )
    prices: dict[str, float] = {}
    for blob in raw.get("PriceList", []) if isinstance(raw, dict) else []:
        product = json.loads(blob) if isinstance(blob, str) else blob
        attributes = product.get("product", {}).get("attributes", {})
        family = str(attributes.get("computeFamily", "") or attributes.get("instanceType", ""))
        if not family:
            continue
        for term in product.get("terms", {}).get("OnDemand", {}).values():
            for dimension in term.get("priceDimensions", {}).values():
                usd = dimension.get("pricePerUnit", {}).get("USD")
                if usd is None:
                    continue
                price = float(usd)
                if price > 0:
                    prices.setdefault(family, price)
    return tuple(sorted(prices)), prices


def fetch_fixtures(directory: Path, region: str, az_count: int = 3) -> Catalogue:
    """Read the captured answers for one region instead of calling AWS.

    The fixture's own `azs` list is kept in whatever order it was captured
    in (fetch_live no longer sorts it), sliced to `az_count` -- a fixture
    captured with fewer zones than a dial's network.az_count asks for cannot
    validate offerings for a zone it never recorded, so that is refused by
    name rather than silently resolving against too few zones.
    """
    path = directory / f"aws-catalogue-{region}.json"
    if not path.is_file():
        raise ResolveError(
            f"no captured catalogue at {path} -- run 'python3 scripts/resolve_sizing.py capture "
            f"--region {region} --fixtures {directory}' first"
        )
    doc = json.loads(path.read_text(encoding="utf-8"))
    types = {name: InstanceType(**body) for name, body in doc["types"].items()}
    all_azs = tuple(doc["azs"])
    if len(all_azs) < az_count:
        raise ResolveError(
            f"{path} carries only {len(all_azs)} availability zones, fewer than the {az_count} "
            "network.az_count asks for -- recapture with more zones before resolving here"
        )
    return Catalogue(
        region=doc["region"],
        azs=all_azs[:az_count],
        types=types,
        offerings={az: set(names) for az, names in doc["offerings"].items()},
        msk_families=tuple(doc.get("msk_families", ())),
        msk_prices=dict(doc.get("msk_prices", {})),
        unparsed=int(doc.get("unparsed", 0)),
        seen=int(doc.get("seen", len(types))),
        source=doc.get("source", "fixture"),
        captured=doc.get("captured", ""),
    )


# An account id, an ARN or a private address in a committed fixture is a leak
# when the repo goes public, so the capture refuses to write one rather than
# trusting a later pass to find it.
ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")
ARN = re.compile(r"arn:aws[a-z-]*:")
PRIVATE_IP = re.compile(r"\b(?:10|192\.168)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


def _scrub_findings(body: str) -> list[str]:
    """Everything in a fixture that must not reach a public repo."""
    found: list[str] = []
    checks = (
        (ACCOUNT_ID, "an account id"),
        (ARN, "an ARN"),
        (PRIVATE_IP, "a private address"),
    )
    for pattern, what in checks:
        match = pattern.search(body)
        if match:
            found.append(f"{what} ({match.group(0)})")
    return found


def write_fixture(catalogue: Catalogue, directory: Path) -> Path:
    """Write the catalogue as a fixture, scrubbed of anything account-specific.

    Named by region, so a fixtures directory can carry more than one captured
    region and a resolve picks the one the dial asks for.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"aws-catalogue-{catalogue.region}.json"
    doc = {
        "_provenance": (
            f"captured from the AWS EC2 and Pricing APIs in {catalogue.region} on "
            f"{catalogue.captured} by scripts/resolve_sizing.py capture; scrubbed"
        ),
        "region": catalogue.region,
        "azs": list(catalogue.azs),
        "captured": catalogue.captured,
        "source": "aws-api",
        "seen": catalogue.seen,
        "unparsed": catalogue.unparsed,
        "types": {name: asdict(body) for name, body in sorted(catalogue.types.items())},
        "offerings": {az: sorted(names) for az, names in sorted(catalogue.offerings.items())},
        "msk_families": list(catalogue.msk_families),
        "msk_prices": dict(sorted(catalogue.msk_prices.items())),
    }
    body = json.dumps(doc, indent=2, sort_keys=False)
    leaked = _scrub_findings(body)
    if leaked:
        raise ResolveError(f"the capture carries {leaked[0]} -- scrub it before committing")
    path.write_text(body + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Target -- picking the concrete shape
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Choice:
    """One use case's resolved shape, with the price and the policy that chose it."""

    use_case: str
    instance_type: str
    fallbacks: list[str]
    vcpu: int
    memory_gib: float
    generation: int
    generation_policy: str
    price_policy: str
    price_usd_hour: float
    physical_processor: str
    instance_store_gb: int
    baseline_iops: int
    baseline_throughput_mib_s: float
    maximum_iops: int
    maximum_throughput_mib_s: float
    volumes: dict[str, dict[str, object]]
    count: int


def _focus_policies(sizing: dict[str, object], focus: str) -> tuple[str, str]:
    """The generation and storage policies this focus level asks for."""
    generation = str(_at(sizing, "focus", focus, "generation_policy", "value") or "newest")
    storage = str(_at(sizing, "focus", focus, "storage_profile", "value") or "baseline")
    return generation, storage


def select_shape(
    use_case: str,
    entry: dict[str, object],
    demand: Node,
    catalogue: Catalogue,
    focus_generation: str,
    notes: list[str],
    root_volume_demand_mib_s: float,
    root_volume_demand_iops: int,
) -> Choice:
    """Pick the concrete type for one use case, from the YAML plus the live API.

    Args:
        use_case: The workload class.
        entry: Its compute-shapes entry.
        demand: The core requirement it has to meet.
        catalogue: The cloud's answers.
        focus_generation: The focus level's generation policy.
        notes: Selection notes, appended to in place for the report.
        root_volume_demand_mib_s: What a quiet root volume asks of the
            instance -- sizing.yaml's floors.root_volume_demand_mib_s.
        root_volume_demand_iops: Its IOPS counterpart.

    Returns:
        The chosen type, its fallback ladder and the price that chose it.

    Raises:
        ResolveError: No candidate meets the demand, a candidate carries no
            price, or too many type names could not be parsed to rank.
    """
    family = str(entry.get("family", ""))
    modifiers = tuple(m for m in str(entry.get("modifiers", "")).split(",") if m.strip())
    policy = str(entry.get("generation_policy", "newest"))
    pin = str(entry.get("generation_pin", "")).strip()
    price_policy = str(entry.get("price_policy", "newest"))
    step_max = str(entry.get("price_step_max_pct", "")).strip()
    price_generations = str(entry.get("price_generations", "")).strip()
    floor_size = str(entry.get("size", ""))

    if catalogue.seen and catalogue.unparsed / catalogue.seen > MAX_UNPARSED_FRACTION:
        raise ResolveError(
            f"{catalogue.unparsed} of {catalogue.seen} instance-type names could not be parsed "
            f"({catalogue.unparsed / catalogue.seen:.0%}, threshold {MAX_UNPARSED_FRACTION:.0%}) -- "
            "the naming scheme moved, and ranking on a guessed generation would downgrade a pool"
        )

    # The floor size sets the minimum shape whatever the demand says, so a
    # tyre-kick deploy still gets a broker that runs the canonical profile.
    floor = [t for t in catalogue.types.values() if t.family == family and t.size == floor_size]
    floor_vcpu = min((t.vcpu for t in floor), default=0)
    floor_memory = min((t.memory_gib for t in floor), default=0.0)

    want_vcpu = max(demand.vcpu, floor_vcpu)
    want_memory = max(float(demand.ram_gib), floor_memory)

    candidates = [
        t
        for t in catalogue.types.values()
        if t.family == family
        and all(m in t.modifiers for m in modifiers)
        and t.vcpu >= want_vcpu
        and t.memory_gib >= want_memory
        and catalogue.offered_everywhere(t.name)
    ]
    if not candidates:
        raise ResolveError(
            f"{use_case}: no {family}-family type with modifiers {modifiers or '()'} carries "
            f"{want_vcpu} vCPU and {want_memory:.0f} GiB in every one of {', '.join(catalogue.azs)}"
        )

    # The generation policy first, then the size, then the price policy across
    # the generations that remain. Focus only widens a `newest`: a pinned or
    # modifier-bound policy is a hard requirement and wins.
    generations = sorted({t.generation for t in candidates}, reverse=True)
    if policy == "pinned":
        if not pin:
            raise ResolveError(f"{use_case}: generation_policy pinned with no generation_pin")
        generations = [g for g in generations if g == int(pin)]
    elif policy == "newest-with-modifiers":
        floor_generation = int(pin) if pin else 0
        generations = [g for g in generations if g >= floor_generation] or generations
    elif policy == "newest" and focus_generation.startswith("newest-within"):
        price_policy = "newest-within"
        step_max = focus_generation.split(":", 1)[1] if ":" in focus_generation else step_max

    if not generations:
        raise ResolveError(f"{use_case}: generation policy {policy} left no candidate")

    # One size across the generations, so a price step compares like with like.
    def smallest_at(generation: int) -> InstanceType | None:
        at_generation = [t for t in candidates if t.generation == generation]
        return min(at_generation, key=lambda t: (t.vcpu, t.memory_gib, t.name), default=None)

    ladder = [t for t in (smallest_at(g) for g in generations) if t is not None]
    unpriced = [t.name for t in ladder if t.price_usd_hour is None]
    if unpriced:
        raise ResolveError(
            f"{use_case}: {', '.join(unpriced)} has no on-demand price in the Pricing API -- "
            "a candidate without a price is not a candidate, and it must not win by default"
        )

    chosen = ladder[0]
    if price_policy == "newest-within" and len(ladder) > 1 and step_max:
        step = float(step_max)
        older = ladder[1]
        rise = (chosen.price_usd_hour - older.price_usd_hour) / older.price_usd_hour * 100
        if rise > step:
            notes.append(
                f"{use_case}: {chosen.name} is {rise:.1f}% dearer than {older.name}, above the "
                f"{step:g}% step -- took {older.name}"
            )
            chosen = older
    elif price_policy == "cheapest-of" and price_generations:
        window = ladder[: int(price_generations)]
        chosen = min(window, key=lambda t: t.price_usd_hour)

    _cross_check_generation(use_case, chosen, notes)
    chosen, volumes = _size_up_to_carry_the_volumes(
        use_case,
        entry,
        demand,
        chosen,
        candidates,
        notes,
        root_volume_demand_mib_s,
        root_volume_demand_iops,
    )

    # `ladder` is the smallest type PER GENERATION that meets the raw demand,
    # built before the volume step-up above ran. A fallback smaller than the
    # (possibly stepped-up) chosen type -- or one whose own baseline cannot
    # sustain the same volume profile -- would let EKS place the pool on a
    # size the step-up existed to avoid the moment the chosen type is short of
    # capacity, silently halving the node the step-up just bought.
    fallbacks = [
        t.name
        for t in ladder
        if t.name != chosen.name
        and t.vcpu >= chosen.vcpu
        and t.memory_gib >= chosen.memory_gib
        and _volumes_fit(
            _resolve_volumes(entry, demand, t, root_volume_demand_mib_s, root_volume_demand_iops), t
        )
    ]

    return Choice(
        use_case=use_case,
        instance_type=chosen.name,
        fallbacks=fallbacks,
        vcpu=chosen.vcpu,
        memory_gib=chosen.memory_gib,
        generation=chosen.generation,
        generation_policy=policy,
        price_policy=price_policy,
        price_usd_hour=float(chosen.price_usd_hour or 0.0),
        physical_processor=chosen.physical_processor,
        instance_store_gb=chosen.instance_store_gb,
        baseline_iops=chosen.baseline_iops,
        baseline_throughput_mib_s=chosen.baseline_throughput_mib_s,
        maximum_iops=chosen.maximum_iops,
        maximum_throughput_mib_s=chosen.maximum_throughput_mib_s,
        volumes=volumes,
        count=demand.count,
    )


def select_msk_shape(
    use_case: str,
    entry: dict[str, object],
    demand: Node,
    catalogue: Catalogue,
    root_volume_demand_mib_s: float,
    root_volume_demand_iops: int,
) -> Choice:
    """Pick the MSK broker type, which lives in MSK's own compute-family namespace.

    `express.m7g.large` is not an EC2 type: it is two generations behind the EC2
    family of the same name, it has its own price line, and storage and memory
    are the provider's. So the smallest size that carries the demanded vCPU is
    chosen from the Pricing API's computeFamily list rather than from EC2.
    """
    family = str(entry.get("family", "m"))
    pin = str(entry.get("generation_pin", "")).strip()
    floor_size = str(entry.get("size", "large"))
    prefix = f"express.{family}{pin}g."
    offered = {
        name.removeprefix(prefix): price
        for name, price in catalogue.msk_prices.items()
        if name.startswith(prefix)
    }
    if not offered:
        raise ResolveError(
            f"{use_case}: the Pricing API lists no {prefix}* compute family -- MSK's broker "
            f"namespace is its own, and an unpriced broker type is not a candidate"
        )
    # EC2 sizes the vCPU count for us: express.m7g.large is the m7g.large shape.
    sizes = sorted(
        (
            (catalogue.types[f"{family}{pin}g.{size}"].vcpu, size, price)
            for size, price in offered.items()
            if f"{family}{pin}g.{size}" in catalogue.types
        )
    )
    floor_vcpu = next(
        (vcpu for vcpu, size, _ in sizes if size == floor_size), sizes[0][0] if sizes else 0
    )
    want = max(demand.vcpu, floor_vcpu)
    fit = [(vcpu, size, price) for vcpu, size, price in sizes if vcpu >= want] or sizes[-1:]
    vcpu, size, price = fit[0]
    reference = catalogue.types[f"{family}{pin}g.{size}"]
    return Choice(
        use_case=use_case,
        instance_type=f"{prefix}{size}",
        fallbacks=[f"{prefix}{other}" for _, other, _ in fit[1:3]],
        vcpu=vcpu,
        memory_gib=reference.memory_gib,
        generation=int(pin) if pin else 0,
        generation_policy=str(entry.get("generation_policy", "pinned")),
        price_policy=str(entry.get("price_policy", "newest")),
        price_usd_hour=price,
        physical_processor=reference.physical_processor,
        instance_store_gb=0,
        # Express manages its own storage, so it publishes no EBS ceiling of ours
        # to assert against and the volume half of the assertions has nothing to
        # read.
        baseline_iops=0,
        baseline_throughput_mib_s=0.0,
        maximum_iops=0,
        maximum_throughput_mib_s=0.0,
        volumes=_resolve_volumes(
            entry, demand, reference, root_volume_demand_mib_s, root_volume_demand_iops
        ),
        count=demand.count,
    )


def _apply_shape_overrides(
    choice: Choice, overrides: dict[str, dict[str, str]], core: Core, catalogue: Catalogue
) -> None:
    """Replace the resolved type, IOPS or throughput with the deployer's answer.

    An overridden instance type takes that type's real vCPU, memory, price and
    ceilings from the API, so the assertions still run against what AWS will
    actually give -- an override is a different answer, never an exemption.
    """
    fields = overrides.get(choice.use_case)
    if not fields:
        return
    for what, raw in fields.items():
        if what not in SHAPE_OVERRIDES:
            continue
        if what == "instance_type":
            replacement = catalogue.types.get(raw)
            if replacement is None:
                raise ResolveError(
                    f"sizing.overrides.{choice.use_case}.instance_type: {raw!r} is not offered in "
                    f"{catalogue.region}"
                )
            if not catalogue.offered_everywhere(raw):
                raise ResolveError(
                    f"sizing.overrides.{choice.use_case}.instance_type: {raw!r} is missing from at "
                    f"least one of {', '.join(catalogue.azs)}, so a node pool cannot place"
                )
            core.overridden.append(
                Override(choice.use_case, "instance_type", choice.instance_type, raw)
            )
            choice.instance_type = replacement.name
            choice.fallbacks = []
            choice.vcpu = replacement.vcpu
            choice.memory_gib = replacement.memory_gib
            choice.generation = replacement.generation
            choice.price_usd_hour = float(replacement.price_usd_hour or 0.0)
            choice.physical_processor = replacement.physical_processor
            choice.instance_store_gb = replacement.instance_store_gb
            choice.baseline_iops = replacement.baseline_iops
            choice.baseline_throughput_mib_s = replacement.baseline_throughput_mib_s
            choice.maximum_iops = replacement.maximum_iops
            choice.maximum_throughput_mib_s = replacement.maximum_throughput_mib_s
            continue
        key = "iops" if what == "iops" else "throughput_mib_s"
        demand_key = "demand_iops" if what == "iops" else "demand_throughput_mib_s"
        for name, volume in choice.volumes.items():
            if volume.get("type") != "gp3" or name != "data":
                continue
            core.overridden.append(
                Override(choice.use_case, what, str(volume.get(key)), raw)
            )
            applied = _whole(choice.use_case, what, raw)
            volume[key] = applied
            # They asked for it: an override above what the workload was going
            # to demand anyway RAISES the demand to match, so A3's baseline
            # check and A7's own-ceiling check both see what the deployer
            # actually wants provisioned, not what size_core quietly derived.
            volume[demand_key] = max(float(volume.get(demand_key, 0) or 0), float(applied))


def _size_up_to_carry_the_volumes(
    use_case: str,
    entry: dict[str, object],
    demand: Node,
    chosen: InstanceType,
    candidates: list[InstanceType],
    notes: list[str],
    root_volume_demand_mib_s: float,
    root_volume_demand_iops: int,
) -> tuple[InstanceType, dict[str, dict[str, object]]]:
    """Step up within the family until the instance sustains the DEMANDED IO.

    An instance caps the real traffic to every volume behind it, and the cloud
    clamps silently, so a profile the instance cannot sustain is fixed by
    taking a bigger instance rather than by reporting it. A3 then fires only
    when no size in the family can carry the demand at all. What a volume is
    merely provisioned for -- gp3's own free ceiling, or a bigger one the
    deployer asked for -- is not traffic the instance ever has to carry at
    once, so it plays no part in this step-up; see _resolve_volumes and A7.
    """
    ladder = sorted(
        (t for t in candidates if t.generation == chosen.generation and t.vcpu >= chosen.vcpu),
        key=lambda t: (t.vcpu, t.memory_gib, t.name),
    )
    volumes = _resolve_volumes(
        entry, demand, chosen, root_volume_demand_mib_s, root_volume_demand_iops
    )
    for candidate in ladder:
        built = _resolve_volumes(
            entry, demand, candidate, root_volume_demand_mib_s, root_volume_demand_iops
        )
        if _volumes_fit(built, candidate):
            if candidate.name != chosen.name:
                rise = (candidate.price_usd_hour or 0) - (chosen.price_usd_hour or 0)
                notes.append(
                    f"{use_case}: stepped up from {chosen.name} to {candidate.name} so the "
                    f"instance sustains the volume profile -- "
                    f"{chosen.baseline_iops:,} IOPS / {chosen.baseline_throughput_mib_s:,.0f} MiB/s "
                    f"becomes {candidate.baseline_iops:,} / "
                    f"{candidate.baseline_throughput_mib_s:,.0f}, at "
                    f"${rise:+.5f} an hour a node"
                )
            return candidate, built
    return chosen, volumes


def _volumes_fit(volumes: dict[str, dict[str, object]], candidate: InstanceType) -> bool:
    """Whether the instance's sustained baseline carries the node's DEMANDED IO.

    Sums `demand_iops` / `demand_throughput_mib_s`, never the provisioned
    `iops` / `throughput_mib_s` -- a volume's own ceiling is not traffic the
    workload drives, and summing every volume's ceiling as though it were is
    what put a tyre-kick deployment on an instance sized for nothing anyone
    asked for. See _resolve_volumes for where a volume's demand comes from.
    """
    total_iops = sum(
        int(v.get("demand_iops", 0) or 0) for v in volumes.values() if v.get("type") == "gp3"
    )
    total_throughput = sum(
        float(v.get("demand_throughput_mib_s", 0) or 0)
        for v in volumes.values()
        if v.get("type") == "gp3"
    )
    if candidate.baseline_iops and total_iops > candidate.baseline_iops:
        return False
    return not (
        candidate.baseline_throughput_mib_s and total_throughput > candidate.baseline_throughput_mib_s
    )


def _cross_check_generation(use_case: str, chosen: InstanceType, notes: list[str]) -> None:
    """Check the parsed generation against the Pricing API's physicalProcessor.

    EC2's ProcessorInfo carries no generation, so the name is what ranks and this
    is the only cross-check there is. A disagreement is reported, never silently
    preferred one way or the other.
    """
    match = GRAVITON_PROCESSOR.search(chosen.physical_processor or "")
    if not match or not match["generation"]:
        notes.append(
            f"{use_case}: {chosen.name} has no Graviton generation in its physicalProcessor "
            f"({chosen.physical_processor!r}) -- ranked on the name alone"
        )
        return
    if int(match["generation"]) < 1:
        notes.append(f"{use_case}: {chosen.name} reports an implausible Graviton generation")


def _resolve_volumes(
    entry: dict[str, object],
    demand: Node,
    chosen: InstanceType,
    root_volume_demand_mib_s: float,
    root_volume_demand_iops: int,
) -> dict[str, dict[str, object]]:
    """Turn the shape's volume profiles into concrete sizes, IOPS and throughput.

    Every gp3 volume carries two figures, kept apart on purpose:

    `iops` / `throughput_mib_s` is what gets PROVISIONED -- the CreateVolume
    numbers a StorageClass actually sets, and the ceiling A1/A2 check the
    volume against. It is the shape's own literal (or the deployer's own
    override, see _apply_shape_overrides) -- a generous, fixed ceiling the
    workload is free to leave unused, never bumped up here to chase demand.

    `demand_iops` / `demand_throughput_mib_s` is what the WORKLOAD actually
    asks of the volume -- what A3 sums against the instance's sustained
    baseline. The "data" volume is the one Node.iops / Node.throughput_mib_s
    targets, so its demand is that real, workload-derived figure, decoupled
    from whatever it is provisioned for; a demand above the provisioned
    ceiling is refused by name (A7), never silently raised the way sizing
    used to. Every OTHER gp3 volume -- root, always, since nothing here ever
    sizes root from a workload -- carries `root_volume_demand_mib_s` /
    `root_volume_demand_iops` instead: sizing.yaml's own small, honest guess
    at what a quiet root disk actually asks of the instance (the OS,
    container images and logs, never data), never gp3's own free 3,000 IOPS /
    125 MiB/s minimum -- that free minimum is what CreateVolume provisions
    the volume at, not traffic the instance has to carry, and treating it as
    demand is the same counting error the "data" volume's own A3 fix already
    corrected. The volume's own provisioned figure still caps it (`min`
    below): the floor can never claim a demand above what the volume is
    actually provisioned for.
    """
    out: dict[str, dict[str, object]] = {}
    volumes = entry.get("volumes")
    if not isinstance(volumes, dict):
        return out
    for name, profile in volumes.items():
        if not isinstance(profile, dict):
            continue
        disk_type = str(profile.get("type", ""))
        formula = str(profile.get("size_formula", ""))
        if disk_type == "nvme-instance-store":
            out[name] = {
                "type": disk_type,
                # InstanceStorageInfo.TotalSizeInGB is AWS's own DECIMAL GB, not
                # GiB -- the field this dict carries is size_gib, so convert at
                # the read rather than copy a GB number under a GiB name. A
                # 950 GB device becomes 884Gi, which is the device's real GiB
                # capacity -- see build_values' clickhouse.objectStore.cacheSize.
                "size_gib": int(chosen.instance_store_gb * BYTES_PER_GB / BYTES_PER_GIB),
                "source": "instance-provided",
            }
            continue
        if disk_type == "managed":
            out[name] = {"type": disk_type, "source": "provider-managed"}
            continue
        size = str(profile.get("size_gib", "")).strip()
        if formula == "fixed" and size:
            size_gib = int(float(size))
        elif name == "data":
            size_gib = demand.disk_gib
        else:
            size_gib = int(float(size)) if size else 0
        iops = int(float(str(profile.get("iops", "") or 0)))
        throughput = float(str(profile.get("throughput_mib_s", "") or 0))
        out[name] = {
            "type": disk_type,
            "size_gib": size_gib,
            "iops": iops,
            "throughput_mib_s": throughput,
            "demand_iops": int(demand.iops) if name == "data" else 0,
            "demand_throughput_mib_s": float(demand.throughput_mib_s) if name == "data" else 0.0,
            "source": formula,
        }
    _derive_baseline_throughput(volumes, out, chosen)
    for name, volume in out.items():
        # AFTER the instance-baseline derivation above, which is the one thing
        # that can still move a non-data volume's own provisioned throughput
        # (root's) -- the demand floor is capped at whatever ended up
        # provisioned, never above it, so A7 can never fire on the floor
        # itself.
        if volume.get("type") == "gp3" and name != "data":
            volume["demand_iops"] = min(root_volume_demand_iops, int(volume.get("iops", 0) or 0))
            volume["demand_throughput_mib_s"] = min(
                root_volume_demand_mib_s, float(volume.get("throughput_mib_s", 0) or 0)
            )
    return out


def _derive_baseline_throughput(
    profiles: dict[str, object], built: dict[str, dict[str, object]], chosen: InstanceType
) -> None:
    """Give every `instance-baseline` volume what the instance has left to give.

    A volume with this policy takes the instance's sustained baseline less what
    the other volumes already claim, floored at gp3's own 125 MiB/s minimum --
    which is why a size whose baseline is below 125 cannot carry a gp3 root at
    all, and why the step-up above exists.
    """
    claimed = sum(
        float(volume.get("throughput_mib_s", 0) or 0)
        for name, volume in built.items()
        if volume.get("type") == "gp3"
        and str((profiles.get(name) or {}).get("throughput_policy", "")) != "instance-baseline"
    )
    spare = max(0.0, chosen.baseline_throughput_mib_s - claimed)
    for name, volume in built.items():
        profile = profiles.get(name)
        if not isinstance(profile, dict):
            continue
        if str(profile.get("throughput_policy", "")) != "instance-baseline":
            continue
        # The volume's own ratio still binds: gp3 gives 0.25 MiB/s per
        # provisioned IOPS, so spare instance bandwidth it cannot reach is not
        # bandwidth this volume has.
        ceiling = min(
            GP3_MAX_THROUGHPUT_MIB_S, GP3_MIB_S_PER_IOPS * int(volume.get("iops", 0) or 0)
        )
        volume["throughput_mib_s"] = float(
            max(GP3_MIN_THROUGHPUT_MIB_S, min(math.floor(spare), ceiling))
        )
        volume["source"] = "instance-baseline"


# ---------------------------------------------------------------------------
# The silent-cap assertions, A1-A7
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Finding:
    """One assertion result -- the rule, what it was about, and what it said."""

    rule: str
    use_case: str
    message: str
    fatal: bool


def _gp3_price_per_gib_month(shapes: dict[str, object], cloud: str, region: str) -> float:
    """The gp3 USD/GiB-month for one region, from compute-shapes.yaml.

    There is no describe-price API for EBS the way there is for EC2 and MSK, so
    this is a doc-sourced constant per region rather than a live read -- and it
    refuses a region the file does not name rather than silently costing the
    deployment at another region's rate.
    """
    price = _at(shapes, "clouds", cloud, "storage_pricing", region, "gp3_usd_per_gib_month")
    if price is None:
        raise ResolveError(
            f"compute-shapes.yaml has no clouds.{cloud}.storage_pricing.{region} -- add "
            f"gp3_usd_per_gib_month, source and read for {region} before resolving there"
        )
    try:
        return float(str(price))
    except ValueError as err:
        raise ResolveError(
            f"clouds.{cloud}.storage_pricing.{region}.gp3_usd_per_gib_month is not a number"
        ) from err


def assert_caps(
    choice: Choice,
    entry: dict[str, object],
    catalogue: Catalogue,
    spend_warn_usd_month: float,
    focus: str = "economy",
    *,
    gp3_usd_per_gib_month: float,
) -> list[Finding]:
    """Assert the volume profile against every cap the cloud will not mention.

    A1 size-derived IOPS, A2 throughput from IOPS, A3 the workload's DEMANDED
    IO against the instance BASELINE, A4 the burst cliff, A5 the spend guard,
    A6 the StorageClass parameters, A7 a demand above what a volume is
    provisioned for. The cloud clamps or bursts silently; these do not.

    A3 sums `demand_iops` / `demand_throughput_mib_s`, never what a volume is
    merely provisioned for: a gp3 ceiling is money the deployer is free to
    leave unused, and treating every volume's ceiling as traffic the instance
    must carry AT ONCE is what put a tyre-kick deployment (near-zero real
    demand) on an instance sized for two idle volumes' free minimums rather
    than for anything it does. See _resolve_volumes for where a volume's
    demand comes from -- sizing.yaml's small root-volume floor for a volume
    nothing here ever asks a workload about, the real Node figure for "data".

    A4 is FATAL only at focus performance, which promises the sustained
    maximum; below it A3 already holds the DEMAND to the instance's baseline,
    so a burstable size is a warning that names the smallest sustained one.
    """
    findings: list[Finding] = []
    total_demand_iops = 0
    total_demand_throughput = 0.0

    for name, volume in choice.volumes.items():
        if volume.get("type") != "gp3":
            continue
        size_gib = int(volume.get("size_gib", 0) or 0)
        iops = int(volume.get("iops", 0) or 0)
        throughput = float(volume.get("throughput_mib_s", 0) or 0)
        demand_iops = int(volume.get("demand_iops", 0) or 0)
        demand_throughput = float(volume.get("demand_throughput_mib_s", 0) or 0)
        total_demand_iops += demand_iops
        total_demand_throughput += demand_throughput

        size_ceiling = min(GP3_MAX_IOPS, GP3_IOPS_PER_GIB * size_gib)
        if iops > size_ceiling:
            findings.append(
                Finding(
                    "A1",
                    choice.use_case,
                    f"{name}: {iops:,} IOPS on a {size_gib:,} GiB gp3 is above the size-derived "
                    f"ceiling of {size_ceiling:,} (min(80,000, 500 x GiB), {GP3_SOURCE}) -- "
                    f"CreateVolume rejects it",
                    True,
                )
            )
        throughput_ceiling = min(GP3_MAX_THROUGHPUT_MIB_S, GP3_MIB_S_PER_IOPS * iops)
        if throughput > throughput_ceiling:
            findings.append(
                Finding(
                    "A2",
                    choice.use_case,
                    f"{name}: {throughput:,.0f} MiB/s needs "
                    f"{math.ceil(throughput / GP3_MIB_S_PER_IOPS):,} provisioned IOPS; the profile "
                    f"asks for {iops:,}, which caps it at {throughput_ceiling:,.0f} MiB/s",
                    True,
                )
            )
        # A workload that wants more of a volume than it is provisioned for is
        # a deployer decision, not the resolver's to make quietly: name the
        # override that raises the ceiling to match, rather than raising it
        # on their behalf and hiding what actually got provisioned.
        if demand_iops > iops:
            findings.append(
                Finding(
                    "A7",
                    choice.use_case,
                    f"{name}: the workload demands {demand_iops:,} IOPS, above the {iops:,} "
                    f"provisioned -- raise sizing.overrides.{choice.use_case}.iops",
                    True,
                )
            )
        if demand_throughput > throughput:
            findings.append(
                Finding(
                    "A7",
                    choice.use_case,
                    f"{name}: the workload demands {demand_throughput:,.0f} MiB/s, above the "
                    f"{throughput:,.0f} provisioned -- raise "
                    f"sizing.overrides.{choice.use_case}.throughput_mibs",
                    True,
                )
            )

    if choice.baseline_iops and total_demand_iops > choice.baseline_iops:
        findings.append(
            Finding(
                "A3",
                choice.use_case,
                f"the workload demands {total_demand_iops:,} IOPS and {choice.instance_type} "
                f"sustains {choice.baseline_iops:,} (it reaches {choice.maximum_iops:,} for 30 "
                f"minutes once a day) -- the instance is the ceiling, not the volume",
                True,
            )
        )
    if choice.baseline_throughput_mib_s and total_demand_throughput > choice.baseline_throughput_mib_s:
        findings.append(
            Finding(
                "A3",
                choice.use_case,
                f"the workload demands {total_demand_throughput:,.0f} MiB/s and "
                f"{choice.instance_type} sustains {choice.baseline_throughput_mib_s:,.0f} (it "
                f"reaches {choice.maximum_throughput_mib_s:,.0f} for 30 minutes once a day)",
                True,
            )
        )

    declared = str(entry.get("sustained", "")).strip().lower()
    is_sustained = declared == "true" if declared else choice.use_case in SUSTAINED_USE_CASES
    if is_sustained and choice.maximum_iops > choice.baseline_iops > 0:
        smallest = _smallest_sustained(catalogue, choice)
        findings.append(
            Finding(
                "A4",
                choice.use_case,
                f"{choice.instance_type} bursts to {choice.maximum_iops:,} IOPS / "
                f"{choice.maximum_throughput_mib_s:,.0f} MiB/s for 30 minutes once every 24 hours "
                f"and sustains {choice.baseline_iops:,} / {choice.baseline_throughput_mib_s:,.0f}. "
                f"{choice.use_case} writes continuously, so the burst is not capacity. The smallest "
                f"size that sustains its maximum is {smallest or 'none in this family'}",
                focus == "performance",
            )
        )

    storage_gib = sum(
        int(v.get("size_gib", 0) or 0) for v in choice.volumes.values() if v.get("type") == "gp3"
    )
    monthly = (
        choice.price_usd_hour * HOURS_PER_MONTH + storage_gib * gp3_usd_per_gib_month
    ) * choice.count
    if spend_warn_usd_month and monthly > spend_warn_usd_month:
        findings.append(
            Finding(
                "A5",
                choice.use_case,
                f"{choice.count} x {choice.instance_type} plus {storage_gib:,} GiB of gp3 each is "
                f"about ${monthly:,.0f} a month on demand, above the ${spend_warn_usd_month:,.0f} "
                f"the dial warns at",
                False,
            )
        )

    for name, volume in choice.volumes.items():
        if volume.get("type") != "gp3":
            continue
        if not volume.get("throughput_mib_s"):
            findings.append(
                Finding(
                    "A6",
                    choice.use_case,
                    f"{name}: the StorageClass sets no throughput, so the EBS CSI driver takes its "
                    f"125 MiB/s default whatever the volume size",
                    True,
                )
            )
        # Checked against the SHAPE ENTRY compute-shapes.yaml declares, not the
        # built `volume` dict: _resolve_volumes only ever copies
        # type/size_gib/iops/throughput_mib_s/source into that dict, so it can
        # never carry either key and this check would never fire against it.
        # A future compute-shapes.yaml edit naming either field by mistake --
        # thinking it is a real EBS CSI StorageClass parameter -- is what this
        # is meant to catch.
        raw_profile = _at(entry, "volumes", name)
        if isinstance(raw_profile, dict) and (
            "iopsPerGB" in raw_profile or "allowAutoIOPSPerGBIncrease" in raw_profile
        ):
            findings.append(
                Finding(
                    "A6",
                    choice.use_case,
                    f"{name}: the StorageClass must set iops, never iopsPerGB, and never "
                    f"allowAutoIOPSPerGBIncrease -- both are silently clamped or silently dearer",
                    True,
                )
            )
    return findings


def _smallest_sustained(catalogue: Catalogue, choice: Choice) -> str | None:
    """The smallest size in this family whose baseline IS its maximum."""
    family = choice.instance_type.split(".", 1)[0]
    same = [
        t
        for t in catalogue.types.values()
        if t.name.startswith(f"{family}.") and t.baseline_iops and t.baseline_iops >= t.maximum_iops
    ]
    if not same:
        return None
    return min(same, key=lambda t: t.vcpu).name


# ---------------------------------------------------------------------------
# Emitting the five artefacts
# ---------------------------------------------------------------------------


def _storage_class(choice: Choice, name: str, volume: dict[str, object]) -> dict[str, object]:
    """The StorageClass parameters for one volume -- A6's three rules, applied."""
    return {
        "name": f"dfe-{choice.use_case}-{name}",
        "type": volume.get("type"),
        # Both set explicitly: the EBS CSI driver's unset defaults are 125 MiB/s
        # and the baseline IOPS, whatever the volume's size allows.
        "iops": volume.get("iops"),
        "throughput": volume.get("throughput_mib_s"),
    }


def _shape_volumes(choice: Choice) -> dict[str, dict[str, object]]:
    """The volume profiles without the deployment's own sizes or demand.

    This file is committed and diffed to show API and policy drift, so a
    number derived from one customer's estimate does not belong in it -- that
    goes to the tfvars and the chart values. A fixed or instance-provided
    size is a property of the shape and stays; `demand_iops` /
    `demand_throughput_mib_s` are always deployment-specific (the workload's
    own Node figures, or root's derived floor for THIS instance choice) and
    never belong here either, the same as a formula-derived size_gib.
    """
    out: dict[str, dict[str, object]] = {}
    for name, volume in choice.volumes.items():
        kept = dict(volume)
        if volume.get("source") not in ("fixed", "instance-provided"):
            kept.pop("size_gib", None)
        kept.pop("demand_iops", None)
        kept.pop("demand_throughput_mib_s", None)
        out[name] = kept
    return out


def merge_resolved(
    path: Path, choices: dict[str, Choice], catalogue: Catalogue, cloud: str
) -> dict[str, object]:
    """Merge this resolve into the committed answer, preserving what it did not size.

    The file is committed and diffed, so a resolve that only touched six use
    cases must not delete a seventh another resolve wrote.
    """
    existing: dict[str, object] = {}
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, object] = {key: value for key, value in existing.items() if not key.startswith("_")}
    for use_case, choice in choices.items():
        out[use_case] = {
            # render_dial.py reads these two and nothing else; everything below
            # is provenance for whoever reviews the diff.
            "instance_types": [choice.instance_type, *choice.fallbacks],
            "arch": "arm64",
            "generation": choice.generation,
            "generation_policy": choice.generation_policy,
            "price_policy": choice.price_policy,
            "price_usd_hour": round(choice.price_usd_hour, 5),
            "physical_processor": choice.physical_processor,
            "vcpu": choice.vcpu,
            "memory_gib": round(choice.memory_gib, 2),
            "instance_store_gb": choice.instance_store_gb,
            "ceilings": {
                "baseline_iops": choice.baseline_iops,
                "baseline_throughput_mib_s": choice.baseline_throughput_mib_s,
                "maximum_iops": choice.maximum_iops,
                "maximum_throughput_mib_s": choice.maximum_throughput_mib_s,
            },
            "volumes": _shape_volumes(choice),
            "storage_classes": {
                name: _storage_class(choice, name, volume)
                for name, volume in choice.volumes.items()
                if volume.get("type") == "gp3"
            },
        }
    ordered: dict[str, object] = {
        "_provenance": {
            "written_by": "scripts/resolve_sizing.py",
            "cloud": cloud,
            "region": catalogue.region,
            "availability_zones": list(catalogue.azs),
            "source": catalogue.source,
            "captured": catalogue.captured,
            "resolved": datetime.now(UTC).date().isoformat(),
        }
    }
    ordered.update(dict(sorted(out.items())))
    return ordered


def build_tfvars(core: Core, choices: dict[str, Choice], dial: Dial) -> dict[str, object]:
    """The tofu inputs -- only the two keys the AWS root's variables.tf declares.

    node_pools and resolved_shapes are the sizing answer; every other variable is
    the dial's, and a key the root does not declare is an error there.

    This script is the SINGLE writer of node_pools: it starts from the dial's
    own node_pools block (dial.node_pools, e.g. `system` -- a group a deployer
    sizes by hand rather than a ratio deriving) and adds the pools it derives
    on top. render_dial.py no longer emits node_pools at all, so there is
    exactly one place the two tfvars producers could otherwise collide on this
    variable, and this is it.
    """
    pools: dict[str, object] = dict(dial.node_pools)
    for use_case, choice in choices.items():
        if use_case in NOT_A_NODE_POOL:
            continue
        root = choice.volumes.get("root", {})
        pools[use_case] = {
            "shape_ref": use_case,
            "min_size": choice.count,
            "max_size": choice.count,
            "desired_size": choice.count,
            "capacity_type": "ON_DEMAND",
            "disk_gb": int(root.get("size_gib", 40) or 40),
            "labels": {"dfe.hyperi.io/workload": use_case},
            "taints": [],
        }
    return {
        "node_pools": pools,
        "resolved_shapes": {
            use_case: {
                "instance_types": [choice.instance_type, *choice.fallbacks],
                "arch": "arm64",
            }
            for use_case, choice in choices.items()
        },
    }


def build_node_requirements(core: Core) -> dict[str, dict[str, int]]:
    """The on-prem node demand, machine-readable -- the same numbers the
    report's "On-prem node requirements" table prints, for a preflight to read
    without scraping markdown.

    Returns:
        `{use_case: {count, cpu, memory_gib, disk_gb}}`, one entry per node
        class this deployment sizes. On-prem nodes are never created by this
        resolver, so this is a DEMAND: something else (bootstrap.sh via
        scripts/check_node_capacity.py) refuses to converge when the
        cluster's real nodes fall short of it.
    """
    return {
        node.use_case: {
            "count": node.count,
            "cpu": node.vcpu,
            "memory_gib": node.ram_gib,
            "disk_gb": node.disk_gib,
        }
        for node in core.nodes.values()
    }


def _yaml_block(tree: dict[str, object], indent: int = 0) -> list[str]:
    """Render nested maps, scalars and lists -- real YAML, for Helm to read.

    The SSoT files this repo READS carry no lists (scripts/yaml_subset.py), but
    what this function WRITES is consumed by Helm, which needs a real sequence
    for a field like Karpenter's `families` -- a comma scalar there is not a
    list, and Go's `range` over a string walks its characters, not its items.
    """
    lines: list[str] = []
    pad = " " * indent
    for key, value in tree.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.extend(_yaml_block(value, indent + 2))
        elif isinstance(value, (list, tuple)):
            lines.append(f"{pad}{key}:")
            lines.extend(_yaml_list(value, indent + 2))
        elif isinstance(value, bool):
            lines.append(f"{pad}{key}: {str(value).lower()}")
        elif isinstance(value, str):
            lines.append(f'{pad}{key}: "{value}"')
        else:
            lines.append(f"{pad}{key}: {value}")
    return lines


def _yaml_list(items: tuple[object, ...] | list[object], indent: int) -> list[str]:
    """Render a YAML sequence -- the list half `_yaml_block` cannot do alone."""
    pad = " " * indent
    lines: list[str] = []
    for item in items:
        if isinstance(item, dict):
            rendered = _yaml_block(item, indent + 2)
            lines.append(f"{pad}-")
            lines.extend(rendered)
        elif isinstance(item, str):
            lines.append(f'{pad}- "{item}"')
        else:
            lines.append(f"{pad}- {item}")
    return lines


# ---------------------------------------------------------------------------
# Karpenter -- the dynamic node pools, rendered from the same shape entries
# ---------------------------------------------------------------------------

# Karpenter's own accepted capacity-type labels -- never a resolver invention.
CAPACITY_TYPE_ON_DEMAND = ("on-demand",)
CAPACITY_TYPE_SPOT_FIRST = ("spot", "on-demand")

# One consolidation posture per workload shape: a pet holds still, elastic
# infra consolidates promptly, and ci-burst matches the aggressive posture
# compute-shapes.yaml's own comment already commits to ("a 100% disruption
# budget with 60s consolidation is only safe here").
_STATEFUL_CONSOLIDATION = {"policy": "WhenEmpty", "after": "10m"}
_ELASTIC_CONSOLIDATION = {"policy": "WhenEmptyOrUnderutilized", "after": "5m"}
_CI_BURST_CONSOLIDATION = {"policy": "WhenEmptyOrUnderutilized", "after": "60s"}

# The pets: a fixed EKS managed node group already carries their floor count
# (build_tfvars), and Karpenter's own budget stays conservative to match --
# never more than one at a time, mirroring eks.tf's own max_unavailable = 1.
STATEFUL_USE_CASES = ("kafka-broker", "kraft-controller", "clickhouse", "keeper")

_GENERATION_DIGIT = re.compile(r"\d+")


def _dedupe(items: list[str]) -> list[str]:
    """Items in first-seen order, once each.

    A resolved choice's own fallback ladder can repeat a family across sizes
    (the same generation at two vCPU counts never happens, but two different
    generations naming the same family letter can), and Karpenter's
    instance-family requirement wants each family named once.
    """
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _karpenter_node_class(entry: dict[str, object], choice: Choice) -> dict[str, object]:
    """This pool's own EC2NodeClass override -- its resolved root volume, and
    RAID0 where the shape's own volume profile asks for one.

    Every pool overrides the chart's own (empty) nodeClass.rootVolume default
    rather than sharing one: the root disk a workload actually carries is what
    select_shape and _size_up_to_carry_the_volumes resolved for IT, and a
    shared default would paper over a real difference between pools.
    """
    root = choice.volumes.get("root")
    node_class: dict[str, object] = {}
    if isinstance(root, dict) and root.get("size_gib"):
        node_class["rootVolume"] = {
            "sizeGi": int(root.get("size_gib", 0) or 0),
            "type": str(root.get("type", "gp3")),
            "iops": int(root.get("iops", 0) or 0),
            "throughputMibS": int(root.get("throughput_mib_s", 0) or 0),
        }
    volumes = entry.get("volumes")
    stores = (
        {str(v.get("instance_store", "")) for v in volumes.values() if isinstance(v, dict)}
        if isinstance(volumes, dict)
        else set()
    )
    if stores & {"raid0", "required"}:
        # RAID0 is the only value Karpenter's EC2NodeClass accepts -- see
        # helm/charts/karpenter-pools/values.yaml's own comment on the field.
        node_class["instanceStorePolicy"] = "RAID0"
    return node_class


def build_karpenter_pools(
    choices: dict[str, Choice], cloud_entry: dict[str, object]
) -> dict[str, object]:
    """Karpenter's NodePool/EC2NodeClass shaping, for every EC2-sourced choice.

    compute-shapes.yaml's own header promises this: "Karpenter NodePool
    constraints render from the same entries, so a new generation arrives by
    drift with no plan change." msk-broker is the one choice this does not
    cover: MSK runs its own brokers, and there is no EC2 instance for
    Karpenter to place.

    Args:
        choices: Every use case this resolve picked a concrete shape for.
        cloud_entry: The cloud's shapes.clouds.<cloud> map, for each use
            case's generation policy and volume profiles.

    Returns:
        `{name: pool}`, ready to nest under `karpenter.pools`; empty when
        there is nothing for Karpenter to place.

    Raises:
        ResolveError: A chosen instance type carries no generation digit -- the
            same fail-safe select_shape applies to a name it cannot parse,
            rather than emitting a pool Karpenter's own render guard refuses.
    """
    pools: dict[str, object] = {}
    for use_case, choice in choices.items():
        if use_case == "msk-broker":
            continue
        families = _dedupe(
            [choice.instance_type.split(".", 1)[0]]
            + [name.split(".", 1)[0] for name in choice.fallbacks]
        )
        generations: list[int] = []
        for family in families:
            match = _GENERATION_DIGIT.search(family)
            if not match:
                raise ResolveError(
                    f"{use_case}: resolved family {family!r} carries no generation digit -- "
                    "a Karpenter pool cannot floor a generation it cannot read"
                )
            generations.append(int(match.group()))

        raw_entry = _at(cloud_entry, "use_cases", use_case)
        entry = raw_entry if isinstance(raw_entry, dict) else {}
        policy = str(entry.get("generation_policy", choice.generation_policy))
        pin = str(entry.get("generation_pin", "")).strip()

        if use_case == "ci-burst":
            consolidation = dict(_CI_BURST_CONSOLIDATION)
            capacity_types = list(CAPACITY_TYPE_SPOT_FIRST)
            budget_nodes, expire_after = "100%", "Never"
        elif use_case in STATEFUL_USE_CASES:
            consolidation = dict(_STATEFUL_CONSOLIDATION)
            capacity_types = list(CAPACITY_TYPE_ON_DEMAND)
            budget_nodes, expire_after = "1", "Never"
        else:
            consolidation = dict(_ELASTIC_CONSOLIDATION)
            capacity_types = list(CAPACITY_TYPE_ON_DEMAND)
            budget_nodes, expire_after = "25%", "720h"

        pool: dict[str, object] = {
            "families": families,
            "arch": str(entry.get("arch") or "arm64"),
            "capacityTypes": capacity_types,
            "consolidation": consolidation,
            "budgetNodes": budget_nodes,
            "expireAfter": expire_after,
            "labels": {"dfe.hyperi.io/workload": use_case},
            # Twice the sized fleet: headroom for Karpenter to grow the pool
            # under load without an unbounded spend risk on a runaway workload
            # -- the NodePool CRD demands a limit, and this is ours to set.
            "limits": {
                "cpu": str(int(choice.vcpu * max(choice.count, 1) * 2)),
                "memory": f"{round(choice.memory_gib * max(choice.count, 1) * 2)}Gi",
            },
        }
        if policy == "pinned" and pin:
            pool["generationIn"] = [pin]
        else:
            pool["generationGt"] = str(min(generations) - 1)
        node_class = _karpenter_node_class(entry, choice)
        if node_class:
            pool["nodeClass"] = node_class
        pools[use_case] = pool
    return pools


def build_values(
    core: Core,
    provider: str,
    choices: dict[str, Choice] | None = None,
    cloud_entry: dict[str, object] | None = None,
) -> dict[str, object]:
    """The chart values overlay -- only keys the charts declare today.

    `choices` and `cloud_entry` are None on a cloud with no live shape
    resolution (onprem, or a stub cloud) -- Karpenter does not run there, and
    the overlay carries no `karpenter` key at all rather than an empty one.
    """
    broker = core.nodes.get("kafka-broker")
    controller = core.nodes.get("kraft-controller")
    clickhouse = core.nodes.get("clickhouse")
    keeper = core.nodes.get("keeper")

    kafka: dict[str, object] = {}
    if broker and provider != "msk-express":
        kafka["replicas"] = broker.count
        kafka["messageMaxBytes"] = core.message_chain_bytes
        kafka["sizing"] = {"peakMbS": round(core.peak_mb_s, 1)}
        kafka["retention"] = {
            # Two SEPARATE fields, matching the chart's own
            # (assumedConsumerDowntimeH + archiverLagH) x 3600000 formula --
            # core.retention_hours is their SUM, used for the disk-sizing math
            # above, and emitting it under assumedConsumerDowntimeH alone would
            # double the archiver term the moment archiverLagH is ever also set.
            "assumedConsumerDowntimeH": int(core.consumer_downtime_hours),
            "archiverLagH": int(core.archiver_lag_hours),
            "usableFraction": CHART_DEFAULTS["usable_fraction"][0],
        }
        kafka["resources"] = {
            "requests": {"cpu": str(broker.vcpu), "memory": f"{broker.ram_gib}Gi"},
            "limits": {"cpu": str(broker.vcpu), "memory": f"{broker.ram_gib}Gi"},
        }
        kafka["storage"] = {"size": f"{broker.disk_gib}Gi"}
        kafka["broker"] = {"networkMbS": CHART_DEFAULTS["broker_network_mb_s"][0]}
        # validate.yaml refuses a KEDA ceiling below kafka.replicas outright --
        # the chart's own static default (6) is right at the floor and wrong
        # the moment the resolved broker count passes it. Twice the resolved
        # count, same headroom convention build_karpenter_pools already uses
        # for its own NodePool limits, capped at the partition count so a
        # broker added at the ceiling still gets a share (partitions is always
        # a multiple of brokers, so it is never below broker.count either).
        kafka["autoscaling"] = {"maxBrokers": min(core.partitions, broker.count * 2)}
    if controller:
        kafka["controllerPool"] = {
            "replicas": controller.count,
            "resources": {
                "requests": {"cpu": str(controller.vcpu), "memory": f"{controller.ram_gib}Gi"},
                "limits": {"cpu": str(controller.vcpu * 2), "memory": f"{controller.ram_gib}Gi"},
            },
            "storage": {"size": f"{controller.disk_gib}Gi"},
        }

    ch: dict[str, object] = {}
    if clickhouse:
        ch["replicas"] = clickhouse.count
        ch["resources"] = {
            "requests": {"cpu": str(clickhouse.vcpu), "memory": f"{clickhouse.ram_gib}Gi"},
            "limits": {"cpu": str(clickhouse.vcpu), "memory": f"{clickhouse.ram_gib}Gi"},
        }
        ch["storage"] = {"size": f"{clickhouse.disk_gib}Gi"}
        # The same ratio the RAM above was sized with, so the server's own limit
        # and the container's cannot drift apart.
        ch["serverProfile"] = {"maxServerMemoryUsageToRamRatio": core.clickhouse_memory_ratio}
        # An r9gd-class AWS shape resolves a "cache" volume from
        # compute-shapes.yaml's clickhouse.volumes.cache (nvme-instance-store):
        # its local NVMe is what the chart's cache.volume: instance-store dial
        # mounts, so the expensive default -- a large gp3 cache on the data PVC
        # -- is never what a populated cloud emits. A choice with no such
        # volume (no populated cloud does this for clickhouse today) falls back
        # to the chart's own pvc default. See docs/deployment/storage.md.
        ch_choice = choices.get("clickhouse") if choices else None
        cache = ch_choice.volumes.get("cache") if ch_choice else None
        if isinstance(cache, dict) and cache.get("size_gib"):
            # _storage.tpl's own guard refuses cache.volume: instance-store
            # unless storageModel is cached-object -- the model derives the
            # object store, and a fragment that sets the cache but not the
            # model fails the chart's render outright (helm/charts/
            # clickhouse-cluster/templates/_storage.tpl).
            ch["objectStore"] = {
                "cache": {"volume": "instance-store"},
                "cacheSize": f"{cache['size_gib']}Gi",
            }
            ch["storageModel"] = "cached-object"
        else:
            # No object-store cache to place -- local is the model this cloud
            # (or on-prem) resolves to unless a deploy-repo overlay says
            # otherwise; the resolver never derives tiered-block.
            ch["objectStore"] = {"cache": {"volume": "pvc"}}
            ch["storageModel"] = "local"
    if keeper:
        ch["keeper"] = {
            "replicas": keeper.count,
            "storage": {"size": f"{keeper.disk_gib}Gi"},
            "resources": {
                "requests": {"cpu": "500m", "memory": f"{keeper.ram_gib}Gi"},
                "limits": {"cpu": "1", "memory": f"{keeper.ram_gib}Gi"},
            },
        }

    out: dict[str, object] = {}
    if kafka:
        out["kafka"] = kafka
    if ch:
        out["clickhouse"] = ch
    if choices and cloud_entry is not None:
        pools = build_karpenter_pools(choices, cloud_entry)
        if pools:
            out["karpenter"] = {"pools": pools}
    return out


def build_resolved(core: Core, dial: Dial, catalogue: Catalogue | None) -> dict[str, object]:
    """The committed resolved-state document a re-resolve diffs against.

    Carries just enough identity to say what this run resolved -- tier, focus,
    cloud, region -- plus a ``locked`` section: a current value for every
    ``sizing.yaml`` ``locked:`` field this script itself derives. Two of the
    six named there, ``storage_model`` and ``controller_mode``, are
    deployer-set chart-values overrides this script never computes, so they
    carry no entry -- a re-size can only be compared on what a resolve
    actually produces, never on what a deployer later hand-sets.
    """
    locked: dict[str, object] = {
        "partition_count": core.partitions,
        "cloud_token": dial.cloud,
        "msk_broker_type": dial.kafka_provider,
    }
    if catalogue is not None:
        locked["az_count"] = len(catalogue.azs)
    return {
        "tier": core.tier,
        "focus": core.focus,
        "cloud": dial.cloud,
        "region": catalogue.region if catalogue is not None else dial.region,
        "estimated": core.estimated,
        "ingest_gb_per_day": round(core.ingest_gb_per_day, 1),
        "resolved": datetime.now(UTC).date().isoformat(),
        "locked": locked,
    }


@dataclass(frozen=True, slots=True)
class LockedChange:
    """One locked field whose value moved between a previous resolve and this one."""

    field: str
    old: str
    new: str
    reason: str


def find_locked_changes(
    sizing: dict[str, object], previous: dict[str, object], resolved: dict[str, object]
) -> list[LockedChange]:
    """Which of sizing.yaml's locked fields moved between two resolved documents.

    Only a field present in BOTH documents' ``locked`` section is compared: one
    absent from either side is a field this script does not derive (see
    ``build_resolved``) and there is nothing to diff it against, so it is
    skipped rather than reported as a change.
    """
    locked = _at(sizing, "locked")
    if not isinstance(locked, dict):
        return []
    previous_locked = _at(previous, "locked")
    resolved_locked = _at(resolved, "locked")
    if not isinstance(previous_locked, dict) or not isinstance(resolved_locked, dict):
        return []
    changes: list[LockedChange] = []
    for name, reason in locked.items():
        if name not in previous_locked or name not in resolved_locked:
            continue
        old_value = str(previous_locked[name])
        new_value = str(resolved_locked[name])
        if old_value != new_value:
            changes.append(
                LockedChange(field=name, old=old_value, new=new_value, reason=str(reason))
            )
    return changes


def _ratio_table(core: Core) -> list[str]:
    """Every ratio the derivation used, with its confidence and its source."""
    lines = ["| Ratio | Value | Confidence | Source |", "|---|---|---|---|"]
    seen: set[str] = set()
    for ratio in core.used:
        if ratio.path in seen:
            continue
        seen.add(ratio.path)
        lines.append(
            f"| `{ratio.path}` | {ratio.value} {ratio.unit} | {ratio.confidence} | "
            f"{ratio.source} ({ratio.read}) |"
        )
    return lines


# The three states a target overlay may put a knob in -- see sizing/targets/.
TARGET_KNOB_STATES = ("blocked", "renamed", "managed")


def _target_overlay_section(overlay_path: Path, target: str) -> list[str]:
    """The per-target knob matrix, read from sizing/targets/<target>.yaml.

    Every knob a target's own provider or contract changes carries one of
    three states -- blocked, renamed or managed -- with the one-sentence
    reason a re-size and this report both trust. Absent, the section says so
    rather than implying nothing was dropped on that target's account.
    """
    if not overlay_path.is_file():
        return [
            "",
            "A per-target knob matrix -- `blocked` / `renamed` / `managed` with the reason for each"
            f" -- is read from `{overlay_path.relative_to(REPO_ROOT)}` when one exists. There is none"
            f" for `{target}`, so the locked fields above are the whole list and nothing was dropped"
            " on a target's account.",
        ]
    overlay = _load(overlay_path)
    status = str(overlay.get("status", "")).strip()
    lines = [
        "",
        f"Per-target knob matrix from `{overlay_path.relative_to(REPO_ROOT)}`"
        + (f" -- **{status}**." if status else "."),
        "",
    ]
    knobs = {name: body for name, body in overlay.items() if isinstance(body, dict)}
    if not knobs:
        lines.append("No knobs recorded for this target.")
        return lines
    lines += ["| Knob | State | Reason |", "|---|---|---|"]
    for name, body in sorted(knobs.items()):
        state = str(body.get("state", "")).strip() or "?"
        reason = str(body.get("reason", "")).strip()
        lines.append(f"| `{name}` | {state} | {reason} |")
    return lines


def build_report(
    core: Core,
    dial: Dial,
    choices: dict[str, Choice],
    findings: list[Finding],
    sizing: dict[str, object],
    catalogue: Catalogue | None,
) -> str:
    """The human-readable sizing report -- what was sized, and on what evidence."""
    lines = [
        f"# Sizing report -- {dial.name}, {core.tier} tier, {core.focus} focus",
        "",
        f"Resolved {datetime.now(UTC).date().isoformat()} from `sizing/sizing.yaml` and "
        f"`shapes/compute-shapes.yaml` for cloud `{dial.cloud}`"
        + (f", region `{catalogue.region}`" if catalogue else "")
        + ".",
        "",
        "## What it was sized for",
        "",
    ]
    if core.estimated:
        lines += [
            f"- Estimate: **{core.ingest_gb_per_day:,.0f} GB/day** (`sizing.ingest_gb_per_day`), "
            f"which is {core.avg_mb_s:,.1f} MB/s on average.",
            f"- Peak factor **{core.peak_factor:g}x** -- log volume is diurnal, so ingest is sized "
            f"for {core.peak_mb_s:,.1f} MB/s and storage from the daily volume.",
            f"- Focus **{core.focus}**: {core.headroom:.0%} headroom over the sized peak, so the "
            f"brokers are built for {core.required_mb_s:,.1f} MB/s.",
        ]
    else:
        lines += [
            "- **No estimate.** The tyre-kick floor applies: the smallest shape that runs the "
            "canonical profile without an OOM.",
            f"- It carries about **{core.carried_mb_s:,.1f} MB/s** "
            f"({core.carried_mb_s * SECONDS_PER_DAY * BYTES_PER_MB / BYTES_PER_GB:,.0f} GB/day). "
            "That is REPORTED, not targeted -- it is what the floor happens to do.",
        ]
    lines += [
        "",
        "The estimate sizes INGEST. It cannot size query capacity: hunts and engine queries are "
        "the other half of ClickHouse CPU, and the focus dial is what buys their headroom.",
        "",
        "## The nodes",
        "",
        "| Workload | Count | vCPU | RAM GiB | Disk GiB | Why |",
        "|---|---|---|---|---|---|",
    ]
    overridden_use_cases = {override.use_case for override in core.overridden}
    for node in core.nodes.values():
        why = node.why
        if node.use_case in overridden_use_cases:
            why = f"{why} -- overridden by the deployer, see below"
        lines.append(
            f"| {node.use_case} | {node.count} | {node.vcpu} | {node.ram_gib or '--'} | "
            f"{node.disk_gib or '--'} | {why} |"
        )

    if choices:
        lines += [
            "",
            "## The shapes, and what they cost",
            "",
            "| Workload | Type | vCPU | RAM GiB | Generation | Policy | USD/hour | USD/month |",
            "|---|---|---|---|---|---|---|---|",
        ]
        total = 0.0
        for choice in choices.values():
            monthly = choice.price_usd_hour * HOURS_PER_MONTH * choice.count
            total += monthly
            lines.append(
                f"| {choice.use_case} | `{choice.instance_type}` | {choice.vcpu} | "
                f"{choice.memory_gib:,.0f} | {choice.generation} "
                f"({choice.physical_processor or 'unnamed'}) | {choice.generation_policy} / "
                f"{choice.price_policy} | {choice.price_usd_hour:.5f} | {monthly:,.0f} |"
            )
        lines.append(f"| **total compute** | | | | | | | **{total:,.0f}** |")
        lines += [
            "",
            "Prices are on-demand Linux from the AWS Pricing API, which is the only source that "
            "names the Graviton generation -- EC2's `ProcessorInfo` does not. A candidate with no "
            "price fails the selection rather than winning it silently.",
        ]

    lines += [
        "",
        "## Partitions and retention",
        "",
        f"- Partitions: **{core.partitions}**, a multiple of the broker count so 3, 6 and 12 "
        "brokers all divide it evenly. Increase-only: a keyed topic cannot be repartitioned.",
        f"- Retention: **{core.retention_hours:g} hours** "
        f"(`log.retention.ms = {core.retention_ms:,}`), from how long a consumer may be down plus "
        "dfe-archiver's lag. Kafka is the buffer; the archive is object storage.",
    ]
    if core.retention_bytes:
        lines.append(
            f"- `log.retention.bytes = {core.retention_bytes:,}` per partition, so time retention "
            "cannot overrun the PVC."
        )
    lines += [
        f"- The message chain is ONE number, **{core.message_chain_bytes:,} bytes**: broker, topic, "
        "producer and consumer move together or a large event is lost at whichever link stayed "
        "behind.",
        "",
        "## Where the ceiling is",
        "",
    ]
    cap = _ratio(sizing, "ceilings", "generator_cap")
    lines.append(
        f"- The generator is capped at **{float(cap.value):,.0f} GB/day**. Above it the resolver "
        "REFUSES and prints the professional-services message; it never extrapolates."
    )
    provider_ceiling = _at(sizing, "ceilings", dial.kafka_provider)
    if isinstance(provider_ceiling, dict):
        lines.append(
            f"- `{dial.kafka_provider}` is documented to {float(provider_ceiling['value']):,.0f} "
            f"GB/day ({provider_ceiling.get('source')})."
        )
    lines.append(f"- {_at(sizing, 'ceilings', 'professional_services_message')}")

    lines += ["", "## The knobs this target manages or locks", ""]
    locked = _at(sizing, "locked")
    if isinstance(locked, dict):
        lines += ["| Locked field | Why a re-size refuses it |", "|---|---|"]
        for name, reason in locked.items():
            lines.append(f"| `{name}` | {reason} |")
    overlay_path = TARGETS_DIR / f"{dial.target}.yaml"
    lines += _target_overlay_section(overlay_path, dial.target)

    if dial.cloud == "onprem":
        lines += ["", "## On-prem node requirements", ""]
        lines += [
            "We do not create these nodes, so this is a DEMAND. Bootstrap refuses to converge "
            "when the cluster's real nodes fall short of it.",
            "",
            "| Node class | How many | vCPU each | RAM GiB each | Disk GiB each |",
            "|---|---|---|---|---|",
        ]
        for node in core.nodes.values():
            lines.append(
                f"| {node.use_case} | {node.count} | {node.vcpu} | {node.ram_gib or '--'} | "
                f"{node.disk_gib or '--'} |"
            )
        lines += [
            "",
            (
                "`sizing.allow_undersized: true` OVERRIDES the refusal. THE WARNING STANDS: the "
                "deployment will run on nodes smaller than the ratios demand, and the first thing "
                "to give is the broker under peak, not a graceful degradation."
            )
            if dial.allow_undersized
            else (
                "Bootstrap REFUSES when a node falls short, with this table printed. "
                "`sizing.allow_undersized: true` overrides it, and the warning is stated in this "
                "report rather than buried in a log line."
            ),
        ]

    if core.overridden:
        lines += [
            "",
            "## Overridden by the deployer",
            "",
            "Set by hand in `sizing.overrides`, over what the ratios derived. Every one of these "
            "still went through the assertions below -- an override is a different answer, not an "
            "exemption from the ceilings.",
            "",
            "| Workload | Field | Derived | Applied |",
            "|---|---|---|---|",
        ]
        for override in core.overridden:
            lines.append(
                f"| {override.use_case} | `{override.what}` | {override.derived} | "
                f"**{override.applied}** |"
            )

    lines += ["", "## Assertions", ""]
    if findings:
        lines += ["| Rule | Workload | Finding |", "|---|---|---|"]
        for finding in findings:
            lines.append(
                f"| {finding.rule}{'' if finding.fatal else ' (warn)'} | {finding.use_case} | "
                f"{finding.message} |"
            )
    else:
        lines.append(
            "None. Every volume profile sits inside its size-derived ceiling and inside the "
            "instance's sustained baseline, and no sustained workload landed on a burstable size."
        )

    lines += ["", "## Every ratio this used", ""]
    lines += _ratio_table(core)
    lines += [
        "",
        "A ratio at `rule-of-thumb` or `benchmark-named-hardware` is a labelled estimate, not a "
        "measurement. `sizing.yaml` names the ten-minute spot test that would promote each one.",
        "",
    ]
    for note in core.notes:
        lines.append(f"> {note}")
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# The two subcommands
# ---------------------------------------------------------------------------


def run_resolve(args: argparse.Namespace) -> int:
    """Resolve one dial into the five artefacts, or refuse an unmigrated locked change."""
    sizing = _load(args.sizing)
    shapes = _load(args.shapes)
    dial = read_dial(args.dial, cloud=args.cloud, target=args.target)

    core = size_core(sizing, dial)

    cloud_entry = _at(shapes, "clouds", dial.cloud)
    if not isinstance(cloud_entry, dict):
        raise ResolveError(f"compute-shapes.yaml has no cloud key {dial.cloud!r}")
    populated = str(cloud_entry.get("status", "")) == "populated"

    catalogue: Catalogue | None = None
    choices: dict[str, Choice] = {}
    findings: list[Finding] = []
    notes: list[str] = []

    if populated:
        # The dial's own region wins: --region is a test/CI override, never the
        # normal path, and a dial naming no region at all falls back to the
        # captured default rather than refusing outright.
        region = args.region or dial.region or "us-west-2"
        if args.live:
            catalogue = fetch_live(region, dial.az_count)
        elif args.fixtures:
            catalogue = fetch_fixtures(args.fixtures, region, dial.az_count)
        else:
            raise ResolveError(
                f"{dial.cloud} is a populated cloud, so the resolve needs an API: pass --live or "
                f"--fixtures <dir>"
            )
        gp3_price = _gp3_price_per_gib_month(shapes, dial.cloud, catalogue.region)
        focus_generation, _storage_profile = _focus_policies(sizing, core.focus)
        wanted = list(CORE_USE_CASES)
        if dial.kafka_provider == "msk-express":
            wanted = [u for u in wanted if u not in ("kafka-broker", "kraft-controller")]
            wanted.append("msk-broker")
        elif dial.kafka_provider in SAAS_KAFKA_PROVIDERS:
            # No EC2 instance to select for a vendor-managed body -- neither
            # kafka-broker nor msk-broker (that name is MSK's own namespace)
            # -- so no broker node pool is emitted at all.
            wanted = [u for u in wanted if u not in ("kafka-broker", "kraft-controller")]
        # ci-burst is not a core requirement -- size_core derives no node for it,
        # so it always resolves off the shape's own floor size -- but every
        # populated cloud carries retryable build/test work, so it always joins
        # the resolve rather than needing its own dial knob.
        wanted.append("ci-burst")
        for use_case in wanted:
            entry = _at(cloud_entry, "use_cases", use_case)
            if not isinstance(entry, dict):
                raise ResolveError(f"{dial.cloud} has no {use_case} shape entry")
            # The core sizes ONE broker requirement, under the self-hosted name.
            # A managed broker is the same requirement asked of a vendor, so the
            # msk-broker shape reads it rather than falling back to the floor.
            demand = core.nodes.get(BROKER_DEMAND.get(use_case, use_case)) or Node(
                use_case, 1, 0, 0, 0, 0, 0, "cluster overhead"
            )
            source = str(entry.get("source", ""))
            if source == "msk-computefamily":
                choice = select_msk_shape(
                    use_case,
                    entry,
                    demand,
                    catalogue,
                    core.root_volume_demand_mib_s,
                    core.root_volume_demand_iops,
                )
            elif source == "ec2":
                choice = select_shape(
                    use_case,
                    entry,
                    demand,
                    catalogue,
                    focus_generation,
                    notes,
                    core.root_volume_demand_mib_s,
                    core.root_volume_demand_iops,
                )
            else:
                notes.append(
                    f"{use_case} resolves against {source}, not an API this resolver reads -- its "
                    f"unit is the vendor's own and the target overlay carries it"
                )
                continue
            _apply_shape_overrides(choice, dial.overrides, core, catalogue)
            choices[use_case] = choice
            findings.extend(
                assert_caps(
                    choice,
                    entry,
                    catalogue,
                    dial.spend_warn_usd_month if dial.spend_warn_usd_month is not None else 0.0,
                    core.focus,
                    gp3_usd_per_gib_month=gp3_price,
                )
            )

    core.notes.extend(notes)

    resolved_doc = build_resolved(core, dial, catalogue)
    locked_changes: list[LockedChange] = []
    if args.previous is not None:
        previous_doc = _load(args.previous)
        locked_changes = find_locked_changes(sizing, previous_doc, resolved_doc)

    if locked_changes and not args.migrate:
        for change in locked_changes:
            print(
                f"resolve_sizing: LOCKED {change.field}: {change.old} -> {change.new} "
                f"({change.reason})",
                file=sys.stderr,
            )
        print(
            "resolve_sizing: pass --migrate to accept these changes and write the artefacts anyway",
            file=sys.stderr,
        )
        return EXIT_LOCKED_CHANGE
    if locked_changes:
        for change in locked_changes:
            print(
                f"resolve_sizing: MIGRATING {change.field}: {change.old} -> {change.new} "
                f"({change.reason})",
                file=sys.stderr,
            )

    out = args.out
    (out / "sizing").mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if catalogue is not None:
        resolved_dir = out / "shapes" / "resolved"
        resolved_dir.mkdir(parents=True, exist_ok=True)
        resolved_path = resolved_dir / f"{dial.cloud}-{catalogue.region}.json"
        merged = merge_resolved(resolved_path, choices, catalogue, dial.cloud)
        resolved_path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        written.append(resolved_path)

        # At the ROOT of --out, beside render_dial.py --tofu's own
        # dial.auto.tfvars.json -- OpenTofu auto-loads *.auto.tfvars* only
        # from the root module directory, never a subdirectory, and this is
        # the single writer of the node_pools variable the two files used to
        # collide on (build_tfvars merges dial.node_pools with what it
        # derives). Named sizing.auto.tfvars.json, never <tier>.auto.tfvars
        # .json, so tofu's own alphabetical load order is stable regardless
        # of which tier was resolved.
        tfvars_path = out / "sizing.auto.tfvars.json"
        tfvars_path.write_text(
            json.dumps(build_tfvars(core, choices, dial), indent=2) + "\n", encoding="utf-8"
        )
        written.append(tfvars_path)

    values_path = out / "sizing" / f"{core.tier}.values.yaml"
    values = build_values(
        core, dial.kafka_provider, choices if populated else None, cloud_entry if populated else None
    )
    header = [
        "## Written by scripts/resolve_sizing.py -- do not edit by hand.",
        f"## {core.tier} tier, {core.focus} focus, "
        + (f"{core.ingest_gb_per_day:,.0f} GB/day." if core.estimated else "no estimate (the floor)."),
        "## Every key here exists in the chart it names; the resolver emits no key it invented.",
        "",
    ]
    values_path.write_text("\n".join([*header, *_yaml_block(values)]) + "\n", encoding="utf-8")
    written.append(values_path)

    report_path = out / "sizing" / f"{core.tier}.report.md"
    report_path.write_text(build_report(core, dial, choices, findings, sizing, catalogue), encoding="utf-8")
    written.append(report_path)

    resolved_yaml_path = out / "sizing" / "resolved.yaml"
    resolved_yaml_header = [
        "## Written by scripts/resolve_sizing.py -- do not edit by hand.",
        "## The machine-comparable state a re-resolve diffs with --previous.",
        "",
    ]
    resolved_yaml_path.write_text(
        "\n".join([*resolved_yaml_header, *_yaml_block(resolved_doc)]) + "\n", encoding="utf-8"
    )
    written.append(resolved_yaml_path)

    if dial.cloud == "onprem":
        # We do not create these nodes, so this is a DEMAND --
        # scripts/check_node_capacity.py is the something-else that reads it
        # against a live `kubectl get nodes` and refuses bootstrap when the
        # cluster falls short (sizing.allow_undersized overrides the refusal,
        # same as the report's own on-prem section).
        nodes_path = out / "sizing" / f"{core.tier}.nodes.json"
        nodes_path.write_text(
            json.dumps(build_node_requirements(core), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        written.append(nodes_path)

    for path in written:
        print(f"resolve_sizing: wrote {path}", file=sys.stderr)
    for finding in findings:
        print(f"resolve_sizing: {finding.rule} {finding.use_case}: {finding.message}", file=sys.stderr)
    # --migrate (already printed MIGRATING above, and reported here as a
    # normal finding if it also happens to be fatal) accepts the LOCKED DIFF,
    # never the caps: a fatal A1/A2/A3 (a volume profile the cloud will
    # reject or silently clamp) still exits 1 on a migration run exactly as
    # it would on any other, so the artefacts having been written is never
    # read as "the assertions passed".
    return 1 if any(f.fatal for f in findings) else 0


def run_capture(args: argparse.Namespace) -> int:
    """Fetch the live AWS answers once, scrub them and write the fixture."""
    # The broadest az_count the tofu root accepts (network.az_count's own 2-6
    # range), so a fixture captured once serves any dial's az_count without
    # needing a re-capture -- fetch_fixtures slices down to what a resolve
    # actually asks for.
    catalogue = fetch_live(args.region, az_count=6)
    path = write_fixture(catalogue, args.fixtures)
    print(
        f"resolve_sizing: captured {len(catalogue.types)} instance types "
        f"({catalogue.unparsed} unparsed of {catalogue.seen}), {len(catalogue.msk_prices)} MSK "
        f"families and {len(catalogue.azs)} zones into {path}",
        file=sys.stderr,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The CLI. `resolve` is the default, so the common call names no subcommand."""
    parser = argparse.ArgumentParser(
        prog="resolve_sizing.py",
        description="Size a deployment from one throughput estimate and one focus dial.",
    )
    sub = parser.add_subparsers(dest="command")

    resolve = sub.add_parser("resolve", help="resolve a dial into the five artefacts")
    resolve.add_argument("--dial", type=Path, required=True, help="the deployment dial")
    resolve.add_argument("--sizing", type=Path, default=SIZING_FILE, help="sizing.yaml to read")
    resolve.add_argument("--shapes", type=Path, default=SHAPES_FILE, help="compute-shapes.yaml to read")
    resolve.add_argument("--cloud", default=None, help="override the dial's target.provision.cloud")
    resolve.add_argument("--target", default=None, help="override the target overlay key")
    resolve.add_argument("--region", default=None, help="override the dial's region")
    resolve.add_argument("--live", action="store_true", help="resolve against the live cloud API")
    resolve.add_argument("--fixtures", type=Path, default=None, help="resolve against captured answers")
    resolve.add_argument("--out", type=Path, default=REPO_ROOT, help="where the artefacts are written")
    resolve.add_argument(
        "--previous",
        type=Path,
        default=None,
        help="a prior sizing/resolved.yaml to diff locked fields against",
    )
    resolve.add_argument(
        "--migrate",
        action="store_true",
        help="accept a locked-field change against --previous and write the artefacts anyway",
    )
    resolve.set_defaults(func=run_resolve, _parser=resolve)

    capture = sub.add_parser("capture", help="capture the live API answers into a fixtures directory")
    capture.add_argument("--region", default="us-west-2", help="the region to capture")
    capture.add_argument("--fixtures", type=Path, required=True, help="the fixtures directory")
    capture.set_defaults(func=run_capture)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments, defaulting to sys.argv[1:].

    Returns:
        0 on a clean resolve, 1 on a bad dial or a failed assertion, 2 when the
        estimate is above the generator cap, 3 when --previous finds a locked
        field changed and --migrate was not passed.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0].startswith("-"):
        args.insert(0, "resolve")
    parsed = build_parser().parse_args(args)
    if not hasattr(parsed, "func"):
        build_parser().print_help(sys.stderr)
        return 1
    if getattr(parsed, "migrate", False) and not getattr(parsed, "previous", None):
        parsed._parser.error("--migrate requires --previous")
    try:
        return parsed.func(parsed)
    except AboveCapError as refusal:
        print(f"resolve_sizing: REFUSED -- {refusal}", file=sys.stderr)
        return EXIT_REFUSED
    except ResolveError as error:
        print(f"resolve_sizing: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
