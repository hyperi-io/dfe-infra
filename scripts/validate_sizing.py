#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/validate_sizing.py
#  Purpose:      Refuse sizing/sizing.yaml and shapes/compute-shapes.yaml unless
#                every ratio carries its provenance and every shape entry carries
#                a policy the resolver can act on. Stdlib only.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""validate_sizing -- the schema gate on the two sizing SSoT files.

The resolver derives every deployment from these two files, so a number that
arrived without a source is a number nobody can check later, and a number nobody
can check is how a wrong cluster gets built with confidence. This refuses both
files unless:

    every ratio carries value, unit, applies_to, source, read, confidence and
    spot_test, with the source a URL or a repo path and the confidence one of
    the declared words

    every cloud key carries all nine use cases, and every use case carries a
    generation and price policy the resolver can act on, plus at least one
    volume profile with its silent-cap fields present

Run it over the committed files, or point it at others:

    python3 scripts/validate_sizing.py
    python3 scripts/validate_sizing.py --sizing /path/to/sizing.yaml

Findings print one per line with the path that failed, and the exit status is 1
when there is at least one. Validation is dataclasses and explicit checks, like
scripts/profiles.py -- no third-party schema library, matching the repo rule.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from yaml_subset import YamlSubsetError, parse, split_list

REPO_ROOT = Path(__file__).resolve().parent.parent
SIZING_FILE = REPO_ROOT / "sizing" / "sizing.yaml"
SHAPES_FILE = REPO_ROOT / "shapes" / "compute-shapes.yaml"

# A map carrying ANY of these is a ratio, and then it must carry all of them.
# That is what makes a half-written entry an error rather than a silent default.
RATIO_FIELDS = ("value", "unit", "applies_to", "source", "read", "confidence", "spot_test")

CONFIDENCE = (
    "vendor-documented",
    "benchmark-named-hardware",
    "rule-of-thumb",
    "dfe-core-evidence",
    "measured",
    "measure-only",
)

# measure-only is the one confidence that carries no number: no constant exists,
# so the resolver has to read the value off the running system.
UNMEASURED = "unmeasured"

# Units whose value is an expression or a named policy rather than a number.
STRING_UNITS = ("formula", "policy")

APPLIES_TO = (
    "strimzi",
    "redpanda",
    "msk-express",
    "msk-standard",
    "confluent-cloud",
    "redpanda-cloud",
    "all-providers",
    "kafka",
    "kraft-controller",
    "clickhouse",
    "keeper",
    "loader",
    "event",
    "deployment",
    "all-workloads",
)

SOURCE_URL = re.compile(r"^https://\S+$")
SOURCE_REPO_PATH = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.<>*-]+)+(:\d+(-\d+)?)?$")
ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

SIZING_SECTIONS = (
    "kafka",
    "kraft_controller",
    "clickhouse",
    "keeper",
    "compression",
    "event",
    "focus",
    "floors",
    "ceilings",
    "retention",
    "partitions",
    "locked",
)
KAFKA_PROVIDERS = ("strimzi", "redpanda", "msk-express")
# Memory is sized per provider because the providers do not share a memory
# model: Redpanda bypasses the page cache the JVM brokers live on.
KAFKA_PER_PROVIDER = ("ram_formula", "disk_formula")
FOCUS_LEVELS = ("economy", "balanced", "performance")
FOCUS_FIELDS = ("headroom", "generation_policy", "storage_profile")
# What economy never trades away, whatever the headroom multiplier says.
FOCUS_INVARIANTS = (
    "replication_factor",
    "min_insync_replicas",
    "separate_controllers",
    "broker_loss_cpu_slack",
)
LOCKED_FIELDS = (
    "partition_count",
    "storage_model",
    "msk_broker_type",
    "controller_mode",
    "cloud_token",
    "az_count",
)

CLOUDS = ("aws", "gcp", "azure", "onprem")
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
USE_CASE_FIELDS = (
    "family",
    "modifiers",
    "arch",
    "generation_policy",
    "generation_pin",
    "price_policy",
    "price_step_max_pct",
    "price_generations",
    "size",
    "source",
    "instance_iops_ceiling",
    "instance_throughput_mib_s_ceiling",
    "volumes",
)
VOLUME_FIELDS = (
    "type",
    "size_gib",
    "size_formula",
    "iops",
    "throughput_mib_s",
    "instance_store",
    "size_derived_iops_ceiling",
    "size_derived_throughput_mib_s_ceiling",
)
# An optional sibling of `throughput_mib_s`: the resolver derives the real
# figure from the instance's baseline, so this policy stands in for a number.
THROUGHPUT_POLICIES = ("instance-baseline",)
# The fields the silent-cap work fills. Empty is legal, a number is legal,
# anything else means someone wrote prose where an assertion has to read a limit.
CEILING_FIELDS = (
    "instance_iops_ceiling",
    "instance_throughput_mib_s_ceiling",
    "size_derived_iops_ceiling",
    "size_derived_throughput_mib_s_ceiling",
)
GENERATION_POLICIES = ("newest", "pinned", "newest-with-modifiers")
GENERATION_PINNED = ("pinned", "newest-with-modifiers")
PRICE_POLICIES = ("newest", "newest-within", "cheapest-of")
INSTANCE_STORE = ("none", "raid0", "required")
# Volume classes the cloud sizes for us, so the IOPS and throughput fields are
# empty by design rather than by omission.
PROVIDER_SIZED_VOLUMES = ("managed", "nvme-instance-store", "local-ssd", "local-nvme")
STATUSES = ("populated", "stub")
ARCHES = ("arm64", "amd64")


@dataclass(frozen=True, slots=True)
class Ratio:
    """One sizing ratio and the provenance the schema demands of it."""

    path: str
    value: str
    unit: str
    applies_to: str
    source: str
    read: str
    confidence: str
    spot_test: str

    def problems(self, today: date) -> list[str]:
        """Everything wrong with this entry, as reader-facing lines."""
        found: list[str] = []
        if self.unit in STRING_UNITS:
            if not self.value:
                found.append(f"{self.path}: unit {self.unit} needs an expression as its value")
        elif self.confidence == "measure-only":
            if self.value != UNMEASURED:
                found.append(f"{self.path}: confidence measure-only carries value {UNMEASURED!r}")
        elif not _is_number(self.value):
            found.append(f"{self.path}: value {self.value!r} is not a number")
        if self.value == UNMEASURED and self.confidence != "measure-only":
            found.append(f"{self.path}: value {UNMEASURED!r} needs confidence measure-only")
        if not self.unit:
            found.append(f"{self.path}: no unit")
        if self.confidence not in CONFIDENCE:
            found.append(f"{self.path}: confidence {self.confidence!r} is not one of {'/'.join(CONFIDENCE)}")
        if not SOURCE_URL.match(self.source) and not SOURCE_REPO_PATH.match(self.source):
            found.append(f"{self.path}: source {self.source!r} is neither a URL nor a repo path")
        found.extend(self._date_problems(today))
        for token in split_list(self.applies_to) or ("",):
            if token not in APPLIES_TO:
                found.append(f"{self.path}: applies_to {token!r} is not a known provider or workload")
        if len(self.spot_test.split()) < 3:
            found.append(f"{self.path}: spot_test must say what would measure it")
        return found

    def _date_problems(self, today: date) -> list[str]:
        """The `read` date has to be a real past date, not a year or a plan."""
        match = ISO_DATE.match(self.read)
        if not match:
            return [f"{self.path}: read {self.read!r} is not an ISO date"]
        try:
            when = date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            return [f"{self.path}: read {self.read!r} is not a real date"]
        if when > today:
            return [f"{self.path}: read {self.read} is in the future"]
        return []


def _is_number(value: str) -> bool:
    """Whether the scalar parses as a number the resolver can do arithmetic on."""
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _looks_like_ratio(node: dict[str, object]) -> bool:
    return any(field in node for field in RATIO_FIELDS)


def _ratio_problems(path: str, node: dict[str, object], today: date) -> list[str]:
    """Validate one ratio map, naming every field it is missing."""
    missing = [field for field in RATIO_FIELDS if not isinstance(node.get(field), str)]
    if missing:
        return [f"{path}: ratio is missing {', '.join(missing)}"]
    ratio = Ratio(
        path=path,
        value=str(node["value"]),
        unit=str(node["unit"]),
        applies_to=str(node["applies_to"]),
        source=str(node["source"]),
        read=str(node["read"]),
        confidence=str(node["confidence"]),
        spot_test=str(node["spot_test"]),
    )
    return ratio.problems(today)


def _walk_ratios(node: object, path: str, today: date, found: list[str]) -> int:
    """Validate every ratio under `node`, returning how many were seen."""
    if not isinstance(node, dict):
        return 0
    if _looks_like_ratio(node):
        found.extend(_ratio_problems(path, node, today))
        return 1
    seen = 0
    for key, child in node.items():
        seen += _walk_ratios(child, f"{path}.{key}" if path else str(key), today, found)
    return seen


def _require_keys(node: object, path: str, keys: tuple[str, ...], found: list[str]) -> bool:
    """Report every key missing from a map, and whether the map was one at all."""
    if not isinstance(node, dict):
        found.append(f"{path}: expected a map")
        return False
    missing = [key for key in keys if key not in node]
    if missing:
        found.append(f"{path}: missing {', '.join(missing)}")
    return not missing


def validate_sizing(tree: dict[str, object], today: date) -> tuple[list[str], int]:
    """Check the sizing SSoT. Returns the findings and the ratio count."""
    found: list[str] = []
    _require_keys(tree, "sizing", ("schema_version", *SIZING_SECTIONS), found)

    kafka = tree.get("kafka")
    if _require_keys(kafka, "sizing.kafka", KAFKA_PROVIDERS, found) and isinstance(kafka, dict):
        for provider in KAFKA_PROVIDERS:
            _require_keys(kafka[provider], f"sizing.kafka.{provider}", KAFKA_PER_PROVIDER, found)

    focus = tree.get("focus")
    if _require_keys(focus, "sizing.focus", (*FOCUS_LEVELS, "invariants"), found) and isinstance(
        focus, dict
    ):
        for level in FOCUS_LEVELS:
            _require_keys(focus[level], f"sizing.focus.{level}", FOCUS_FIELDS, found)
        _require_keys(focus["invariants"], "sizing.focus.invariants", FOCUS_INVARIANTS, found)

    ceilings = tree.get("ceilings")
    _require_keys(
        ceilings, "sizing.ceilings", ("generator_cap", "professional_services_message"), found
    )
    if isinstance(ceilings, dict) and not str(ceilings.get("professional_services_message", "")):
        found.append("sizing.ceilings.professional_services_message: empty")

    locked = tree.get("locked")
    if _require_keys(locked, "sizing.locked", LOCKED_FIELDS, found) and isinstance(locked, dict):
        for field in LOCKED_FIELDS:
            if not str(locked[field]).strip():
                found.append(f"sizing.locked.{field}: a locked field carries the reason it is locked")

    ratios = _walk_ratios(tree, "", today, found)
    return found, ratios


def _volume_problems(path: str, volume: object, populated: bool) -> list[str]:
    """Validate one volume profile, including its silent-cap placeholders."""
    found: list[str] = []
    if not _require_keys(volume, path, VOLUME_FIELDS, found) or not isinstance(volume, dict):
        return found
    disk_type = str(volume["type"])
    if populated and not disk_type:
        found.append(f"{path}.type: a populated cloud names the volume class")
    if populated and str(volume["size_formula"]) == "fixed" and not _is_number(str(volume["size_gib"])):
        found.append(f"{path}.size_gib: a fixed-size volume states its size")
    policy = str(volume.get("throughput_policy", ""))
    if policy and policy not in THROUGHPUT_POLICIES:
        found.append(f"{path}.throughput_policy: {policy!r} is not one of {'/'.join(THROUGHPUT_POLICIES)}")
    if populated and disk_type not in PROVIDER_SIZED_VOLUMES:
        if not _is_number(str(volume["iops"])):
            found.append(f"{path}.iops: provision it, or the cloud default bites")
        if policy != "instance-baseline" and not _is_number(str(volume["throughput_mib_s"])):
            found.append(f"{path}.throughput_mib_s: provision it, or the cloud default bites")
    if str(volume["instance_store"]) not in INSTANCE_STORE:
        found.append(f"{path}.instance_store: not one of {'/'.join(INSTANCE_STORE)}")
    found.extend(_ceiling_problems(path, volume))
    return found


def _ceiling_problems(path: str, node: dict[str, object]) -> list[str]:
    """A silent-cap field is empty (not yet read) or a number. Never prose."""
    found: list[str] = []
    for field in CEILING_FIELDS:
        if field not in node:
            continue
        raw = str(node[field]).strip()
        if raw and not _is_number(raw):
            found.append(f"{path}.{field}: a cap is a number read from the provider, or empty")
    return found


def _use_case_problems(path: str, entry: object, cloud_arch: str, populated: bool) -> list[str]:
    """Validate one use case: the policies, the architecture and its volumes."""
    found: list[str] = []
    if not _require_keys(entry, path, USE_CASE_FIELDS, found) or not isinstance(entry, dict):
        return found

    arch = str(entry["arch"])
    if arch not in ARCHES:
        found.append(f"{path}.arch: {arch!r} is not one of {'/'.join(ARCHES)}")
    elif arch != cloud_arch:
        found.append(f"{path}.arch: {arch} against the cloud's {cloud_arch}")

    generation_policy = str(entry["generation_policy"])
    pin = str(entry["generation_pin"]).strip()
    if generation_policy not in GENERATION_POLICIES:
        found.append(f"{path}.generation_policy: {generation_policy!r} is not a policy")
    elif generation_policy in GENERATION_PINNED and not _is_number(pin):
        found.append(f"{path}.generation_pin: {generation_policy} needs the generation it holds")
    elif generation_policy == "newest" and pin:
        found.append(f"{path}.generation_pin: always-newest carries no pin")

    price_policy = str(entry["price_policy"])
    step = str(entry["price_step_max_pct"]).strip()
    generations = str(entry["price_generations"]).strip()
    if price_policy not in PRICE_POLICIES:
        found.append(f"{path}.price_policy: {price_policy!r} is not a policy")
    elif price_policy == "newest-within" and not _is_number(step):
        found.append(f"{path}.price_step_max_pct: newest-within needs the step it stays inside")
    elif price_policy == "cheapest-of" and not _is_number(generations):
        found.append(f"{path}.price_generations: cheapest-of needs how many generations to compare")

    if populated:
        for field in ("family", "size", "source"):
            if not str(entry[field]).strip():
                found.append(f"{path}.{field}: a populated cloud states it")

    found.extend(_ceiling_problems(path, entry))

    volumes = entry["volumes"]
    if not isinstance(volumes, dict) or not volumes:
        found.append(f"{path}.volumes: at least one volume profile")
        return found
    for name, volume in volumes.items():
        found.extend(_volume_problems(f"{path}.volumes.{name}", volume, populated))
    return found


def validate_shapes(tree: dict[str, object]) -> tuple[list[str], int]:
    """Check the shape SSoT. Returns the findings and the use-case count."""
    found: list[str] = []
    _require_keys(tree, "shapes", ("schema_version", "use_cases", "clouds"), found)

    declared = tree.get("use_cases")
    if _require_keys(declared, "shapes.use_cases", USE_CASES, found) and isinstance(declared, dict):
        for name, description in declared.items():
            if not str(description).strip():
                found.append(f"shapes.use_cases.{name}: say what the workload is")

    clouds = tree.get("clouds")
    if not _require_keys(clouds, "shapes.clouds", CLOUDS, found) or not isinstance(clouds, dict):
        return found, 0

    entries = 0
    for cloud, body in clouds.items():
        path = f"shapes.clouds.{cloud}"
        if not _require_keys(body, path, ("status", "arch", "use_cases"), found):
            continue
        if not isinstance(body, dict):
            continue
        status = str(body["status"])
        arch = str(body["arch"])
        if status not in STATUSES:
            found.append(f"{path}.status: {status!r} is not one of {'/'.join(STATUSES)}")
        if arch not in ARCHES:
            found.append(f"{path}.arch: {arch!r} is not one of {'/'.join(ARCHES)}")
        # On-prem nodes are x86_64; the clouds are ARM by default, which is the
        # whole point of the generation policies above.
        expected_arch = "amd64" if cloud == "onprem" else "arm64"
        if arch != expected_arch:
            found.append(f"{path}.arch: {cloud} runs {expected_arch}")
        use_cases = body["use_cases"]
        if not _require_keys(use_cases, f"{path}.use_cases", USE_CASES, found):
            continue
        if not isinstance(use_cases, dict):
            continue
        for name, entry in use_cases.items():
            entries += 1
            found.extend(
                _use_case_problems(
                    f"{path}.use_cases.{name}", entry, arch, status == "populated"
                )
            )
    return found, entries


def _load(path: Path) -> tuple[dict[str, object], list[str]]:
    """Parse one SSoT file, turning a parse error into a finding."""
    try:
        return parse(path.read_text(encoding="utf-8"), source=str(path)), []
    except OSError as err:
        return {}, [f"{path}: cannot read -- {err}"]
    except YamlSubsetError as err:
        return {}, [f"{err}"]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments, defaulting to sys.argv[1:].

    Returns:
        0 when both files pass, 1 when anything failed.
    """
    parser = argparse.ArgumentParser(
        prog="validate_sizing.py",
        description="Refuse the sizing and shape SSoT files unless every entry carries its provenance.",
    )
    parser.add_argument("--sizing", type=Path, default=SIZING_FILE, help="sizing.yaml to check")
    parser.add_argument("--shapes", type=Path, default=SHAPES_FILE, help="compute-shapes.yaml to check")
    args = parser.parse_args(argv)

    today = date.today()
    findings: list[str] = []

    sizing, errors = _load(args.sizing)
    findings.extend(errors)
    ratios = 0
    if sizing:
        problems, ratios = validate_sizing(sizing, today)
        findings.extend(problems)

    shapes, errors = _load(args.shapes)
    findings.extend(errors)
    entries = 0
    if shapes:
        problems, entries = validate_shapes(shapes)
        findings.extend(problems)

    if findings:
        for finding in findings:
            print(f"validate_sizing: {finding}", file=sys.stderr)
        print(f"validate_sizing: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print(f"validate_sizing: {ratios} ratios, {entries} shape entries, all sourced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
