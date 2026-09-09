#  Project:      dfe-infra
#  File:         scripts/tests/test_capacity.py
#  Purpose:      The lane gate's readings and verdict, on fabricated numbers
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the lane gate concludes, checked without a host or a cluster.

Every branch is exercised on readings written here, because the gate has to be
right the first time it fires: the run it refuses is the one that would otherwise
have driven the hypervisor into swap with another lane already on it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import capacity
import profiles

GI = 1024**3


class TestQuantityParsing:
    @pytest.mark.parametrize(
        ("text", "cores"),
        [("4", 4.0), ("3500m", 3.5), ("1500000000n", 1.5), ("2000000u", 2.0)],
    )
    def test_cpu(self, text: str, cores: float) -> None:
        assert capacity.parse_cpu(text) == pytest.approx(cores)

    @pytest.mark.parametrize(
        ("text", "byte_count"),
        [
            ("16Gi", 16 * GI),
            ("32863260Ki", 32863260 * 1024),
            ("104857600k", 104857600 * 1000),
            ("2000000", 2000000),
            ("1500m", 1),
        ],
    )
    def test_memory(self, text: str, byte_count: int) -> None:
        assert capacity.parse_mem(text) == byte_count

    def test_a_quantity_that_is_not_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="plenty"):
            capacity.parse_mem("plenty")


class TestTheDockerReading:
    def test_meminfo_reports_what_a_new_workload_can_have(self) -> None:
        reading = capacity.meminfo_available(
            "MemTotal:       32000000 kB\nMemFree:         1000000 kB\nMemAvailable:   20000000 kB\n"
        )
        assert reading.free == 20000000 * 1024
        assert reading.total == 32000000 * 1024
        assert reading.committed == 12000000 * 1024

    def test_meminfo_without_the_estimate_is_refused(self) -> None:
        # MemFree alone ignores reclaimable page cache, so gating on it would
        # refuse lanes the host could carry.
        with pytest.raises(ValueError, match="MemAvailable"):
            capacity.meminfo_available("MemTotal: 32000000 kB\nMemFree: 1000000 kB\n")

    def test_free_output_is_the_fallback(self) -> None:
        reading = capacity.free_output_available(
            "               total        used        free      shared  buff/cache   available\n"
            "Mem:     34359738368 12884901888  4294967296     1048576 17179869184 20401094656\n"
            "Swap:     8589934592           0  8589934592\n"
        )
        assert reading.free == 20401094656
        assert reading.total == 34359738368

    def test_a_free_with_no_available_column_is_refused(self) -> None:
        with pytest.raises(ValueError, match="available column"):
            capacity.free_output_available(
                "               total        used        free\n"
                "Mem:     34359738368 12884901888  4294967296\n"
            )

    def test_output_with_no_memory_row_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Mem"):
            capacity.free_output_available("total used free available\nSwap: 1 2 3 4\n")


def _node(name: str, allocatable: str, ready: bool = True) -> dict:
    return {
        "metadata": {"name": name},
        "status": {
            "allocatable": {"memory": allocatable},
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


def _pod(node: str, requests: list[str], phase: str = "Running", init: list[str] | None = None) -> dict:
    return {
        "status": {"phase": phase},
        "spec": {
            "nodeName": node,
            "containers": [{"resources": {"requests": {"memory": r}}} for r in requests],
            "initContainers": [{"resources": {"requests": {"memory": r}}} for r in init or []],
        },
    }


class TestTheKubernetesReading:
    def test_headroom_is_allocatable_minus_what_is_requested(self) -> None:
        nodes = {"items": [_node("a", "16Gi"), _node("b", "16Gi")]}
        pods = {"items": [_pod("a", ["4Gi", "2Gi"]), _pod("b", ["1Gi"])]}
        reading = capacity.node_headroom(nodes, pods)
        assert reading.total == 32 * GI
        assert reading.committed == 7 * GI
        assert reading.free == 25 * GI

    def test_a_pod_on_another_cluster_node_is_not_counted(self) -> None:
        nodes = {"items": [_node("a", "16Gi")]}
        pods = {"items": [_pod("a", ["4Gi"]), _pod("elsewhere", ["8Gi"])]}
        assert capacity.node_headroom(nodes, pods).committed == 4 * GI

    def test_a_finished_pod_holds_nothing(self) -> None:
        nodes = {"items": [_node("a", "16Gi")]}
        pods = {"items": [_pod("a", ["4Gi"], phase="Succeeded"), _pod("a", ["1Gi"])]}
        assert capacity.node_headroom(nodes, pods).committed == 1 * GI

    def test_init_containers_are_counted(self) -> None:
        nodes = {"items": [_node("a", "16Gi")]}
        pods = {"items": [_pod("a", ["1Gi"], init=["2Gi"])]}
        assert capacity.node_headroom(nodes, pods).committed == 3 * GI

    def test_a_node_that_is_not_ready_carries_nothing(self) -> None:
        nodes = {"items": [_node("a", "16Gi"), _node("b", "16Gi", ready=False)]}
        assert capacity.node_headroom(nodes, {"items": []}).total == 16 * GI

    def test_a_cluster_with_no_ready_node_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no node is Ready"):
            capacity.node_headroom({"items": [_node("a", "16Gi", ready=False)]}, {"items": []})

    def test_over_requested_headroom_never_goes_negative(self) -> None:
        nodes = {"items": [_node("a", "4Gi")]}
        pods = {"items": [_pod("a", ["8Gi"])]}
        assert capacity.node_headroom(nodes, pods).free == 0


class TestTheVerdict:
    def test_a_lane_that_fits_is_allowed(self) -> None:
        reading = capacity.Reading("k8s", 32 * GI, 8 * GI, 24 * GI, "fabricated")
        ok, line = capacity.verdict(reading, 16 * GI)
        assert ok
        assert "clears" in line

    def test_a_lane_that_does_not_is_refused(self) -> None:
        reading = capacity.Reading("docker", 32 * GI, 28 * GI, 4 * GI, "fabricated")
        ok, line = capacity.verdict(reading, 16 * GI)
        assert not ok
        assert "UNDER" in line

    def test_exactly_the_floor_is_allowed(self) -> None:
        reading = capacity.Reading("k8s", 32 * GI, 16 * GI, 16 * GI, "fabricated")
        assert capacity.verdict(reading, 16 * GI)[0]


class TestTheFloorComesFromTheProfileTable:
    def test_every_mode_declares_one(self) -> None:
        assert all(profiles.lane_floor(mode) > 0 for mode in profiles.MODES)

    def test_a_bigger_tier_asks_for_more(self) -> None:
        assert profiles.lane_floor("scale") > profiles.lane_floor("slim")

    def test_an_unknown_mode_is_refused(self) -> None:
        with pytest.raises(KeyError):
            profiles.lane_floor("enormous")
