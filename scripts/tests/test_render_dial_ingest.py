#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_render_dial_ingest.py
#  Purpose:      Guard the dial's `ingest:` block -- ingest.mode reads through
#                the receiver's own exposure.mode vocabulary, and a value
#                outside it is refused by name before an operator carries it
#                into a real Helm values overlay.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for render_dial.py's `ingest:` block validation (`_ingest_mode`).

    python3 -m pytest scripts/tests/test_render_dial_ingest.py -q

Q51: the receiver's ingress door on AWS/GCP/Azure must never be an
unauthenticated internet-facing LoadBalancer by default -- every cloud front
door bills by the GB and DFE pushes terabytes a day through the receiver.
This covers the dial-side half of that: `ingest.mode` reads public/
internal/vpn and nothing else, the fallback is the mode the cloud overlay
applies (the chart's own default off cloud), and the render prints which mode
a dial carries. The chart-side
render (which Service kind each mode produces) is
scripts/tests/test_receiver_ingress.py instead.
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


def test_an_absent_block_takes_the_charts_own_default_off_cloud() -> None:
    """No ingest: block and no cloud overlay -- dfe-receiver's own values.yaml
    default (public) is what deploys, so that is what the summary reports."""
    mode = render_dial._ingest_mode(parse_dial("substrate: k8s\n", source="test-dial"))
    assert mode == "public"


def test_an_absent_block_reports_the_cloud_overlays_vpn() -> None:
    """On aws, gcp and azure the overlay sets exposure.mode: vpn, so a dial
    that omits the block deploys with no load balancer and must say so."""
    for cloud in render_dial._VPN_DEFAULT_CLOUDS:
        dial = parse_dial(f"k8s:\n  cloud: {cloud}\n", source="test-dial")
        assert render_dial._ingest_mode(dial) == "vpn"


def test_each_named_mode_reads_through() -> None:
    for value in render_dial.INGEST_MODES:
        dial = parse_dial(f"ingest:\n  mode: {value}\n", source="test-dial")
        assert render_dial._ingest_mode(dial) == value


def test_an_unknown_mode_is_refused_by_name() -> None:
    dial = parse_dial("ingest:\n  mode: internet\n", source="test-dial")
    with pytest.raises(render_dial.DialError, match=r"ingest\.mode"):
        render_dial._ingest_mode(dial)


def test_the_shipped_example_dial_sets_the_safe_cloud_default() -> None:
    """deployment.example.yaml documents vpn as the ingest.mode a cloud deploy
    should carry -- prove the dial actually parses to that, not just the
    comment text saying so."""
    example = parse_dial(
        (REPO_ROOT / "deployment.example.yaml").read_text(encoding="utf-8"),
        source="deployment.example.yaml",
    )
    assert render_dial._ingest_mode(example) == "vpn"
