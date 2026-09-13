#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_validate_sizing.py
#  Purpose:      Guard the sizing SSoT and its schema: the committed files pass,
#                and a ratio that lost its source, its confidence or its date
#                fails loudly rather than sizing a cluster from nothing.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/validate_sizing.py and the two files it gates.

The gate is only worth having if it refuses a bad file, so every check here
breaks the committed YAML one line at a time and asserts the validator catches
it. The fixtures are derived from the real files rather than hand-written, so
they cannot drift away from what ships.

    python3 -m pytest scripts/tests/test_validate_sizing.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
SIZING = REPO_ROOT / "sizing" / "sizing.yaml"
SHAPES = REPO_ROOT / "shapes" / "compute-shapes.yaml"

sys.path.insert(0, str(SCRIPTS))
import validate_sizing  # noqa: E402
import yaml_subset  # noqa: E402


def _sizing_without(line_start: str, after: str) -> str:
    """The sizing file with the first `line_start` line after `after` removed."""
    out: list[str] = []
    seen_anchor = False
    dropped = False
    for line in SIZING.read_text(encoding="utf-8").splitlines():
        if after in line:
            seen_anchor = True
        if seen_anchor and not dropped and line.strip().startswith(line_start):
            dropped = True
            continue
        out.append(line)
    assert dropped, f"fixture did not remove a {line_start!r} line after {after!r}"
    return "\n".join(out) + "\n"


def _sizing_with(replacement: str, after: str, line_start: str, text: str | None = None) -> str:
    """The sizing file (or a `text` already mutated by a prior call) with the
    first `line_start` line after `after` replaced."""
    out: list[str] = []
    seen_anchor = False
    swapped = False
    source = text if text is not None else SIZING.read_text(encoding="utf-8")
    for line in source.splitlines():
        if after in line:
            seen_anchor = True
        if seen_anchor and not swapped and line.strip().startswith(line_start):
            swapped = True
            indent = " " * (len(line) - len(line.lstrip()))
            out.append(f"{indent}{replacement}")
            continue
        out.append(line)
    assert swapped, f"fixture did not replace a {line_start!r} line after {after!r}"
    return "\n".join(out) + "\n"


def _run(sizing: Path = SIZING, shapes: Path = SHAPES) -> int:
    return validate_sizing.main(["--sizing", str(sizing), "--shapes", str(shapes)])


def test_the_committed_files_pass() -> None:
    assert _run() == 0


def test_a_ratio_with_no_source_is_refused(tmp_path: Path) -> None:
    """The whole point of the file: a number nobody can check does not ship."""
    broken = tmp_path / "sizing.yaml"
    broken.write_text(_sizing_without("source:", "peak_factor:"), encoding="utf-8")
    assert _run(sizing=broken) == 1


def test_the_finding_names_the_ratio_and_the_missing_field(tmp_path: Path, capsys) -> None:
    broken = tmp_path / "sizing.yaml"
    broken.write_text(_sizing_without("source:", "peak_factor:"), encoding="utf-8")
    _run(sizing=broken)
    err = capsys.readouterr().err
    assert "event.peak_factor" in err
    assert "source" in err


def test_an_unknown_confidence_is_refused(tmp_path: Path) -> None:
    broken = tmp_path / "sizing.yaml"
    broken.write_text(
        _sizing_with("confidence: pretty-sure", "peak_factor:", "confidence:"), encoding="utf-8"
    )
    assert _run(sizing=broken) == 1


def test_a_source_that_is_neither_a_url_nor_a_repo_path_is_refused(tmp_path: Path) -> None:
    broken = tmp_path / "sizing.yaml"
    broken.write_text(
        _sizing_with("source: a vendor blog somewhere", "peak_factor:", "source:"), encoding="utf-8"
    )
    assert _run(sizing=broken) == 1


def test_a_read_date_in_the_future_is_refused(tmp_path: Path) -> None:
    """A date nobody could have read on is a copied line, not provenance."""
    broken = tmp_path / "sizing.yaml"
    broken.write_text(_sizing_with("read: 2099-01-01", "peak_factor:", "read:"), encoding="utf-8")
    assert _run(sizing=broken) == 1


def test_a_ratio_value_that_is_not_a_number_is_refused(tmp_path: Path) -> None:
    broken = tmp_path / "sizing.yaml"
    broken.write_text(_sizing_with("value: about two", "peak_factor:", "value:"), encoding="utf-8")
    assert _run(sizing=broken) == 1


def test_an_unmeasured_value_needs_the_measure_only_confidence(tmp_path: Path) -> None:
    """keeper.iops_driver is the ratio still carrying `confidence: measure-only`
    -- merge_cpu_fraction was promoted to a real measured value by the spot
    test, so forcing its value to `unmeasured` no longer isolates this rule."""
    broken = tmp_path / "sizing.yaml"
    with_unmeasured_value = _sizing_with("value: unmeasured", "iops_driver:", "value:")
    body = _sizing_with(
        "confidence: vendor-documented", "iops_driver:", "confidence:", text=with_unmeasured_value
    )
    broken.write_text(body, encoding="utf-8")
    assert _run(sizing=broken) == 1


def test_a_duplicate_key_is_a_finding_not_a_crash(tmp_path: Path) -> None:
    """A second value silently winning is how a wrong number ships."""
    broken = tmp_path / "sizing.yaml"
    body = SIZING.read_text(encoding="utf-8").replace(
        "  data_topic_hours:\n", "  data_topic_hours:\n    value: 48\n", 1
    )
    broken.write_text(body, encoding="utf-8")
    assert _run(sizing=broken) == 1


def test_a_cloud_missing_a_use_case_is_refused(tmp_path: Path) -> None:
    broken = tmp_path / "compute-shapes.yaml"
    body = SHAPES.read_text(encoding="utf-8").replace("      toolbox:\n", "      toolbin:\n", 1)
    broken.write_text(body, encoding="utf-8")
    assert _run(shapes=broken) == 1


def test_a_silent_cap_field_takes_a_number_or_nothing(tmp_path: Path) -> None:
    """The resolver asserts against these, so prose in one is a broken assertion."""
    broken = tmp_path / "compute-shapes.yaml"
    body = SHAPES.read_text(encoding="utf-8").replace(
        '        instance_iops_ceiling: ""\n',
        "        instance_iops_ceiling: whatever the instance gives\n",
        1,
    )
    broken.write_text(body, encoding="utf-8")
    assert _run(shapes=broken) == 1


def test_the_caps_are_still_waiting_to_be_filled() -> None:
    """Empty today, by design -- this fails when the caps land, and it should."""
    shapes = yaml_subset.parse(SHAPES.read_text(encoding="utf-8"), source=str(SHAPES))
    aws = shapes["clouds"]["aws"]["use_cases"]
    assert aws["clickhouse"]["instance_iops_ceiling"] == ""
    assert aws["clickhouse"]["instance_throughput_mib_s_ceiling"] == ""


def test_every_kafka_provider_sizes_its_own_memory() -> None:
    """Redpanda bypasses the page cache, so one shared RAM formula would be wrong."""
    sizing = yaml_subset.parse(SIZING.read_text(encoding="utf-8"), source=str(SIZING))
    for provider in validate_sizing.KAFKA_PROVIDERS:
        assert "ram_formula" in sizing["kafka"][provider], provider


def test_economy_keeps_the_durability_floor() -> None:
    """The cheapest deployment still runs RF3 with two in-sync replicas."""
    sizing = yaml_subset.parse(SIZING.read_text(encoding="utf-8"), source=str(SIZING))
    invariants = sizing["focus"]["invariants"]
    assert invariants["replication_factor"]["value"] == "3"
    assert invariants["min_insync_replicas"]["value"] == "2"


def test_the_headroom_multipliers_clear_every_vendor_floor() -> None:
    """20% headroom sat under MSK's 60% CPU rule; 40% is the floor we start at."""
    sizing = yaml_subset.parse(SIZING.read_text(encoding="utf-8"), source=str(SIZING))
    focus = sizing["focus"]
    headrooms = [float(focus[level]["headroom"]["value"]) for level in validate_sizing.FOCUS_LEVELS]
    assert headrooms == sorted(headrooms)
    assert headrooms[0] >= 0.40


def test_the_generator_cap_is_a_refusal_not_an_extrapolation() -> None:
    """Above the cap the resolver stops, so the message has to be there to print."""
    sizing = yaml_subset.parse(SIZING.read_text(encoding="utf-8"), source=str(SIZING))
    ceilings = sizing["ceilings"]
    assert float(ceilings["generator_cap"]["value"]) == 100000
    assert "professional-services" in ceilings["professional_services_message"]


def test_instance_baseline_throughput_needs_no_number(tmp_path: Path) -> None:
    """The policy stands in for the number the resolver derives at build time."""
    broken = tmp_path / "compute-shapes.yaml"
    body = SHAPES.read_text(encoding="utf-8").replace(
        "            throughput_mib_s: 125\n            throughput_policy: instance-baseline\n",
        "            throughput_mib_s: \"\"\n            throughput_policy: instance-baseline\n",
        1,
    )
    broken.write_text(body, encoding="utf-8")
    assert _run(shapes=broken) == 0


def test_a_throughput_policy_that_is_not_recognised_is_refused(tmp_path: Path) -> None:
    broken = tmp_path / "compute-shapes.yaml"
    body = SHAPES.read_text(encoding="utf-8").replace(
        "            throughput_policy: instance-baseline\n",
        "            throughput_policy: whatever-fits\n",
        1,
    )
    broken.write_text(body, encoding="utf-8")
    assert _run(shapes=broken) == 1


def test_a_volume_with_no_policy_still_needs_a_numeric_throughput(tmp_path: Path) -> None:
    """The numeric form stays valid, and stays required, with no policy set."""
    broken = tmp_path / "compute-shapes.yaml"
    body = SHAPES.read_text(encoding="utf-8").replace(
        "            throughput_mib_s: 500\n", '            throughput_mib_s: ""\n', 1
    )
    broken.write_text(body, encoding="utf-8")
    assert _run(shapes=broken) == 1


def test_the_shape_file_names_no_instance_type() -> None:
    """A concrete type is an answer the resolver computes, never a committed input."""
    body = SHAPES.read_text(encoding="utf-8")
    for banned in ("m9g.", "m8g.", "m7g.large", "r9g.", "i8g.", "t4g."):
        for line in body.splitlines():
            if line.lstrip().startswith("#"):
                continue
            assert banned not in line, f"{banned} named outside a comment"
