#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_cycle_backend.py
#  Purpose:      Guard the per-run backend overlay: it merges without dropping
#                any part of the dial, reads account values from the environment
#                only, and the workflow offers exactly the overlays committed.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/cycle_backend.py.

    python3 -m pytest scripts/tests/test_cycle_backend.py -q
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
EXAMPLE = REPO_ROOT / "deployment.example.yaml"

sys.path.insert(0, str(SCRIPTS))
import cycle_backend  # noqa: E402
import render_dial  # noqa: E402
from test_render_dial_tofu import DIAL  # noqa: E402
from yaml_subset import at  # noqa: E402
from yaml_subset import parse as parse_dial  # noqa: E402


def _in_cluster_broker(dial: str) -> str:
    """The render fixture with its kafka block cut back to an in-cluster broker."""
    head, _, rest = dial.partition("\nkafka:\n")
    _, _, tail = rest.partition("\nstate:\n")
    return f"{head}\nkafka:\n  provider: strimzi\n  controller_pool: combined\n\nstate:\n{tail}"


# The e2e-runner dial's own shape, so the overlay is what turns MSK on. RFC 5737
# addresses and the reserved example domain only.
STRIMZI_DIAL = _in_cluster_broker(DIAL)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "deployment.yaml"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_default_leaves_the_dial_byte_for_byte(tmp_path: Path) -> None:
    path = write(tmp_path, STRIMZI_DIAL)
    assert cycle_backend.apply(path, "default", {}) == []
    assert path.read_text(encoding="utf-8") == STRIMZI_DIAL


def test_msk_turns_the_broker_into_msk_and_renders_for_the_root(tmp_path: Path) -> None:
    path = write(tmp_path, STRIMZI_DIAL)
    changed = cycle_backend.apply(path, "msk", {})

    assert "kafka.provider" in changed
    assert "kafka.msk.broker_version" in changed
    _, variables = render_dial._tofu_vars(parse_dial(path.read_text(encoding="utf-8")))
    assert variables["kafka"]["provider"] == "msk"
    assert variables["kafka"]["msk"]["broker_count"] == 3
    assert variables["kafka"]["msk"]["bootstrap_job"] == {
        "namespace": "strimzi",
        "service_account": "dfe-kafka-bootstrap",
    }
    assert variables["seeds"] == {"kafka/msk": {"password": ""}, "ui/nextauth": {"secret": ""}}


def test_the_merge_keeps_every_key_the_overlay_does_not_name(tmp_path: Path) -> None:
    before = parse_dial(STRIMZI_DIAL)
    path = write(tmp_path, STRIMZI_DIAL)
    cycle_backend.apply(path, "msk", {})
    after = parse_dial(path.read_text(encoding="utf-8"))

    assert after["kafka"].pop("msk")
    after["kafka"]["provider"] = "strimzi"
    assert after == before


def test_the_msk_overlay_is_the_shape_the_example_dial_documents() -> None:
    overlay = cycle_backend.load_overlay("msk")["kafka"]["msk"]
    example = parse_dial(EXAMPLE.read_text(encoding="utf-8"))
    for key in ("shape_ref", "broker_count", "broker_version", "num_partitions",
                "log_retention_ms", "message_max_bytes", "scram_username"):
        assert overlay[key] == at(example, ("kafka", "msk", key)), key
    assert overlay["bootstrap_job"] == at(example, ("kafka", "msk", "bootstrap_job"))


def test_an_unknown_backend_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(cycle_backend.BackendError, match="confluent"):
        cycle_backend.apply(write(tmp_path, STRIMZI_DIAL), "confluent", {})


def test_an_env_value_is_read_from_the_environment_and_never_committed() -> None:
    tree = {"endpoints": {"clickhouse_host": "env:DFE_CYCLE_TEST_HOST", "otel_endpoint": "x"}}
    resolved = cycle_backend.resolve(tree, {"DFE_CYCLE_TEST_HOST": "ch.example.com"})
    assert resolved == {"endpoints": {"clickhouse_host": "ch.example.com", "otel_endpoint": "x"}}


def test_an_unset_env_value_refuses_the_run_naming_the_secret_and_the_key() -> None:
    tree = {"endpoints": {"clickhouse_host": "env:DFE_CYCLE_TEST_HOST"}}
    with pytest.raises(cycle_backend.BackendError) as error:
        cycle_backend.resolve(tree, {"DFE_CYCLE_TEST_HOST": ""})
    assert "DFE_CYCLE_TEST_HOST (for endpoints.clickhouse_host)" in str(error.value)


def test_a_scalar_over_a_map_is_refused() -> None:
    with pytest.raises(cycle_backend.BackendError, match=r"kafka\.msk"):
        cycle_backend.merge({"kafka": {"msk": {"broker_count": "3"}}}, {"kafka": {"msk": "none"}})


def test_a_map_over_a_value_is_refused_unless_the_value_is_an_empty_map() -> None:
    with pytest.raises(cycle_backend.BackendError, match=r"kafka\.msk"):
        cycle_backend.merge({"kafka": {"msk": "set"}}, {"kafka": {"msk": {"a": "1"}}})
    dial: dict[str, object] = {"sizing": {"overrides": "{}"}}
    assert cycle_backend.merge(dial, {"sizing": {"overrides": {"a": "1"}}}) == ["sizing.overrides.a"]


def test_the_dump_reads_back_unchanged_through_the_dials_own_parser() -> None:
    tree = {
        "a": {"b": "", "c": "[22, 443]", "d": '{"k": "v"}', "e": "x # not a comment"},
        "f": {},
    }
    assert parse_dial(cycle_backend.dump(tree)) == {**tree, "f": "{}"}


def test_a_value_the_subset_cannot_carry_is_refused() -> None:
    with pytest.raises(cycle_backend.BackendError, match=r"a\.b"):
        cycle_backend.dump({"a": {"b": "it's \"both\""}})


def test_the_rewritten_dial_is_private(tmp_path: Path) -> None:
    path = write(tmp_path, STRIMZI_DIAL)
    cycle_backend.apply(path, "msk", {})
    assert path.stat().st_mode & 0o077 == 0
