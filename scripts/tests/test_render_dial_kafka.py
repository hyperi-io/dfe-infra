#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_render_dial_kafka.py
#  Purpose:      Guard the dial's kafka.controller_pool -- the value reads
#                through the kafka chart's own controllerPool vocabulary, and
#                anything else is refused by name before a resolve carries it
#                into a values fragment.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for render_dial.py's kafka.controller_pool validation.

    python3 -m pytest scripts/tests/test_render_dial_kafka.py -q

Where the KRaft metadata quorum runs is a decision a deployment makes once:
moving it on a live cluster re-forms the quorum, which is why sizing.yaml locks
the answer as `controller_mode`. The resolver's own half -- which fragment each
value produces -- is scripts/tests/test_resolve_sizing.py instead.
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


def test_an_absent_field_takes_the_charts_own_default() -> None:
    """A dial with no kafka block deploys the chart's combined default, so that
    is what the renderer reports."""
    dial = parse_dial("substrate: k8s\n", source="test-dial")
    assert render_dial._controller_pool(dial) == "combined"


@pytest.mark.parametrize("value", ["combined", "separate"])
def test_each_declared_value_is_accepted(value: str) -> None:
    dial = parse_dial(f"kafka:\n  controller_pool: {value}\n", source="test-dial")
    assert render_dial._controller_pool(dial) == value


def test_a_value_outside_the_vocabulary_is_refused_by_name() -> None:
    dial = parse_dial("kafka:\n  controller_pool: dedicated\n", source="test-dial")
    with pytest.raises(render_dial.DialError, match=r"kafka\.controller_pool must be one of"):
        render_dial._controller_pool(dial)


def test_the_committed_template_carries_a_declared_value() -> None:
    """The template is what an operator copies, so its own answer has to pass
    the validator every copy of it will be read through."""
    dial = parse_dial(
        render_dial.DIAL_TEMPLATE.read_text(encoding="utf-8"), source=str(render_dial.DIAL_TEMPLATE)
    )
    assert render_dial._controller_pool(dial) in render_dial.CONTROLLER_POOLS
