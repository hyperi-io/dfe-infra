#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_render_dial_ingest.py
#  Purpose:      Guard the dial's `edge.groups.ingest` block -- the receiver's
#                door reads through the chart's own door vocabulary, the
#                retired spellings are refused by name, a deny list is refused
#                where nothing enforces it, and a /0 warns.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for render_dial.py's ingest exposure group (`_ingest_door`, `ingest_fence`).

    python3 -m pytest scripts/tests/test_render_dial_ingest.py -q

The receiver's door is `exposure.groups.ingest` in the deploy repo, which the
dial carries as `edge.groups.ingest` for an operator to paste. Door loadbalancer
renders the receiver's own load balancer only once `allow` names the senders.
The chart-side render is scripts/tests/test_receiver_ingress.py instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import render_dial  # noqa: E402
from yaml_subset import parse as parse_dial  # noqa: E402


def dial(body: str) -> dict:
    return parse_dial(body, source="test-dial")


def test_an_absent_block_reports_the_cloud_overlays_door() -> None:
    """The door is read from argocd/values, not restated: common.yaml's
    loadbalancer on a cloud, local.yaml's gateway on-prem."""
    for cloud, door in (("aws", "loadbalancer"), ("gcp", "loadbalancer"), ("azure", "loadbalancer"),
                        ("local", "gateway"), ("local-dfe", "gateway"), ("", "loadbalancer")):
        assert render_dial._ingest_door(dial(f"k8s:\n  cloud: {cloud}\n" if cloud else "substrate: k8s\n")) == door
    assert render_dial._values_ingest_door("nosuchcloud") == "loadbalancer"


def test_each_named_door_reads_through() -> None:
    for door in render_dial.INGEST_DOORS:
        assert render_dial._ingest_door(dial(f"edge:\n  groups:\n    ingest:\n      door: {door}\n")) == door


def test_expected_fail_an_unknown_door_is_refused_by_name() -> None:
    with pytest.raises(render_dial.DialError, match=r"edge\.groups\.ingest\.door"):
        render_dial._ingest_door(dial("edge:\n  groups:\n    ingest:\n      door: public\n"))


@pytest.mark.parametrize(
    ("body", "replacement"),
    [
        ("edge:\n  ingest:\n    receiver:\n      mode: public\n", "edge.groups.ingest.door"),
        ("ingest:\n  mode: vpn\n", "edge.groups.ingest.door"),
        ("edge:\n  ingest:\n    receiver:\n      public:\n        loadBalancerSourceRanges: []\n",
         "edge.groups.ingest.allow"),
        ("ingest:\n  public:\n    loadBalancerSourceRanges: [198.51.100.0/24]\n", "edge.groups.ingest.allow"),
    ],
)
def test_expected_fail_the_retired_spellings_name_their_replacement(body: str, replacement: str) -> None:
    with pytest.raises(render_dial.DialError) as refused:
        render_dial._ingest_retired(dial(body))
    assert "is retired" in str(refused.value)
    assert f"set {replacement} instead" in str(refused.value)


def test_expected_fail_a_deny_list_on_the_load_balancer_door_is_refused() -> None:
    parsed = dial("edge:\n  groups:\n    ingest:\n      allow: [198.51.100.0/24]\n      deny: [198.51.100.7/32]\n")
    with pytest.raises(render_dial.DialError, match="nothing enforces it there"):
        render_dial.ingest_fence(parsed, "loadbalancer", True)
    line, warnings = render_dial.ingest_fence(parsed, "gateway", True)
    assert line.startswith("gateway"), line
    assert warnings == []


@pytest.mark.parametrize(
    ("door", "enabled", "allow", "said"),
    [
        ("loadbalancer", True, "[]", "loadbalancer -- closed: no load balancer renders"),
        ("loadbalancer", True, "[198.51.100.0/24]", "admitting 198.51.100.0/24"),
        ("gateway", True, "[]", "closed on an internet-facing gateway and unfenced elsewhere"),
        ("tunnel", True, "[198.51.100.0/24]", "tunnel -- no public door"),
        ("loadbalancer", False, "[198.51.100.0/24]", "off -- no public door"),
    ],
)
def test_the_summary_says_what_the_door_opens(door: str, enabled: bool, allow: str, said: str) -> None:
    line, warnings = render_dial.ingest_fence(dial(f"edge:\n  groups:\n    ingest:\n      allow: {allow}\n"),
                                              door, enabled)
    assert said in line, line
    assert warnings == []


@pytest.mark.parametrize("everything", ["0.0.0.0/0", "::/0"])
def test_expected_fail_every_address_warns(everything: str) -> None:
    line, warnings = render_dial.ingest_fence(
        dial(f"edge:\n  groups:\n    ingest:\n      allow: [198.51.100.0/24, {everything}]\n"), "loadbalancer", True
    )
    assert everything in line
    assert len(warnings) == 1, warnings
    assert "admits every address" in warnings[0], warnings


def test_the_shipped_example_dial_carries_the_cloud_door_closed() -> None:
    """deployment.example.yaml documents a closed load balancer door; prove the
    dial parses to that and to no retired key, not just the comment text."""
    example = parse_dial((REPO_ROOT / "deployment.example.yaml").read_text(encoding="utf-8"),
                         source="deployment.example.yaml")
    render_dial._ingest_retired(example)
    assert render_dial._ingest_door(example) == "loadbalancer"
    line, _ = render_dial.ingest_fence(example, "loadbalancer", True)
    assert "closed" in line, line
