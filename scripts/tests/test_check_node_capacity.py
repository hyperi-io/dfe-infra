#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_node_capacity.py
#  Purpose:      Guard the on-prem sizing gate: quantity parsing, the
#                demanded-vs-allocatable comparison, the override path and the
#                missing-file skip.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/check_node_capacity.py.

    python3 -m pytest scripts/tests/test_check_node_capacity.py -q

No live cluster is needed: `kubectl_get_nodes` is monkeypatched to answer one
of the two fixture documents below (ENOUGH_NODES, NOT_ENOUGH_NODES) rather than
shelling out.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import check_node_capacity as cnc  # noqa: E402

# A demand every node in ENOUGH_NODES can carry: 6 vCPU and 24 GiB total.
DEMAND = {
    "kafka-broker": {"count": 3, "cpu": 2, "memory_gib": 8, "disk_gb": 40},
}

# Two nodes at 4 vCPU / 16 GiB allocatable each -- 8 vCPU / 32 GiB total,
# comfortably above DEMAND's 6 vCPU / 24 GiB.
ENOUGH_NODES = {
    "items": [
        {"status": {"allocatable": {"cpu": "4", "memory": "16Gi"}}},
        {"status": {"allocatable": {"cpu": "4000m", "memory": "16Gi"}}},
    ]
}

# One node at 2 vCPU / 8 GiB allocatable -- well under DEMAND's 6 vCPU / 24 GiB.
NOT_ENOUGH_NODES = {"items": [{"status": {"allocatable": {"cpu": "2", "memory": "8Gi"}}}]}


# ---------------------------------------------------------------------------
# Quantity parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("16", 16.0),
        ("3920m", 3.92),
        ("0", 0.0),
        ("1Ki", 1024.0),
        ("1Mi", 2**20),
        ("1Gi", 2**30),
        ("65932988Ki", 65932988 * 1024),
        ("1G", 10**9),
        ("2.5", 2.5),
    ],
)
def test_parse_quantity_reads_every_suffix_kubectl_emits(raw: str, expected: float) -> None:
    assert cnc.parse_quantity(raw) == pytest.approx(expected)


def test_parse_quantity_refuses_an_unknown_suffix() -> None:
    with pytest.raises(cnc.CapacityError, match="unknown suffix"):
        cnc.parse_quantity("16Zz")


def test_parse_quantity_refuses_something_that_is_not_a_quantity_at_all() -> None:
    with pytest.raises(cnc.CapacityError, match="not a Kubernetes resource quantity"):
        cnc.parse_quantity("not-a-number")


# ---------------------------------------------------------------------------
# Cluster allocatable and total demand
# ---------------------------------------------------------------------------


def test_cluster_allocatable_sums_every_node() -> None:
    cpu, mem_gib = cnc.cluster_allocatable(ENOUGH_NODES)
    assert cpu == pytest.approx(8.0)
    assert mem_gib == pytest.approx(32.0)


def test_cluster_allocatable_treats_no_items_as_an_empty_cluster() -> None:
    assert cnc.cluster_allocatable({}) == (0.0, 0.0)


def test_total_demand_multiplies_count_by_per_node_and_sums_use_cases() -> None:
    demand = {
        "kafka-broker": {"count": 3, "cpu": 2, "memory_gib": 8, "disk_gb": 40},
        "keeper": {"count": 3, "cpu": 1, "memory_gib": 3, "disk_gb": 20},
    }
    cpu, mem = cnc.total_demand(demand)
    assert cpu == pytest.approx(3 * 2 + 3 * 1)
    assert mem == pytest.approx(3 * 8 + 3 * 3)


# ---------------------------------------------------------------------------
# read_demand -- the nodes.json contract
# ---------------------------------------------------------------------------


def test_read_demand_accepts_what_build_node_requirements_writes(tmp_path: Path) -> None:
    path = tmp_path / "scale.nodes.json"
    path.write_text(json.dumps(DEMAND), encoding="utf-8")
    assert cnc.read_demand(path) == DEMAND


def test_read_demand_refuses_a_use_case_missing_a_field(tmp_path: Path) -> None:
    path = tmp_path / "scale.nodes.json"
    path.write_text(json.dumps({"kafka-broker": {"count": 3, "cpu": 2}}), encoding="utf-8")
    with pytest.raises(cnc.CapacityError, match="missing one of"):
        cnc.read_demand(path)


def test_read_demand_refuses_a_document_that_is_not_a_map(tmp_path: Path) -> None:
    path = tmp_path / "scale.nodes.json"
    path.write_text(json.dumps(["not", "a", "map"]), encoding="utf-8")
    with pytest.raises(cnc.CapacityError, match="expected a map"):
        cnc.read_demand(path)


# ---------------------------------------------------------------------------
# main() -- the missing-file skip, the refusal, and the override
# ---------------------------------------------------------------------------


def _nodes_file(tmp_path: Path, demand: dict[str, dict[str, float]] = DEMAND) -> Path:
    path = tmp_path / "scale.nodes.json"
    path.write_text(json.dumps(demand), encoding="utf-8")
    return path


def test_a_missing_nodes_file_is_a_clean_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fail_if_called(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("kubectl should never be shelled out to when the nodes file is absent")

    monkeypatch.setattr(cnc, "kubectl_get_nodes", _fail_if_called)
    status = cnc.main(["--nodes-file", str(tmp_path / "scale.nodes.json")])
    assert status == 0
    assert not called


def test_enough_capacity_exits_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cnc, "kubectl_get_nodes", lambda _kubeconfig: ENOUGH_NODES)
    nodes_file = _nodes_file(tmp_path)
    assert cnc.main(["--nodes-file", str(nodes_file)]) == 0


def test_not_enough_capacity_refuses_without_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cnc, "kubectl_get_nodes", lambda _kubeconfig: NOT_ENOUGH_NODES)
    nodes_file = _nodes_file(tmp_path)
    assert cnc.main(["--nodes-file", str(nodes_file)]) == 1
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "kafka-broker" in err


def test_not_enough_capacity_with_override_flag_warns_and_exits_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cnc, "kubectl_get_nodes", lambda _kubeconfig: NOT_ENOUGH_NODES)
    nodes_file = _nodes_file(tmp_path)
    assert cnc.main(["--nodes-file", str(nodes_file), "--override"]) == 0
    assert "WARNING" in capsys.readouterr().out


def test_not_enough_capacity_with_env_override_warns_and_exits_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cnc, "kubectl_get_nodes", lambda _kubeconfig: NOT_ENOUGH_NODES)
    monkeypatch.setenv("DFE_SIZING_OVERRIDE", "1")
    nodes_file = _nodes_file(tmp_path)
    assert cnc.main(["--nodes-file", str(nodes_file)]) == 0
    assert "WARNING" in capsys.readouterr().out


def test_kubeconfig_is_passed_through_to_kubectl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def _capture(kubeconfig: str | None) -> dict[str, object]:
        seen["kubeconfig"] = kubeconfig
        return ENOUGH_NODES

    monkeypatch.setattr(cnc, "kubectl_get_nodes", _capture)
    nodes_file = _nodes_file(tmp_path)
    kubeconfig = str(tmp_path / "kube.conf")
    cnc.main(["--nodes-file", str(nodes_file), "--kubeconfig", kubeconfig])
    assert seen["kubeconfig"] == kubeconfig


def test_kubectl_not_on_path_is_a_refusal_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _missing(_kubeconfig: str | None) -> object:
        raise cnc.CapacityError("kubectl is not on PATH")

    monkeypatch.setattr(cnc, "kubectl_get_nodes", _missing)
    nodes_file = _nodes_file(tmp_path)
    assert cnc.main(["--nodes-file", str(nodes_file)]) == 1
    assert "kubectl is not on PATH" in capsys.readouterr().err
