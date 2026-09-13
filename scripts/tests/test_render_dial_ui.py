#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_render_dial_ui.py
#  Purpose:      Guard the dial's `ui:` block -- every boolean reads through
#                the same _flag() path as endpoint.public, and a value that is
#                not exactly true/false is refused by name before an operator
#                carries it into a real Helm values overlay.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for render_dial.py's `ui:` block validation (`_ui_flags`).

    python3 -m pytest scripts/tests/test_render_dial_ui.py -q

This covers the dial-side half of the ui: block validation: a value that is
not exactly true/false is refused here, by name. The chart-side half (a value
that survives the dial as a string rather than a bool is refused at Helm
render time) lives in scripts/test-route-exposure.sh instead, because that
failure mode is a real Go-template kind difference (bool vs string) that only
exists once the dial's ui: block reaches a real YAML/Helm parse -- render_dial's
own restricted YAML reader (yaml_subset.py) has no such distinction, quoted or
not.
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

DIAL_ALL_SET = """
ui:
  public:
    dfe_ui: "true"
    kafbat: "false"
    cruise_control: "false"
    hyperdx: "false"
    argocd: "false"
    links: "false"
  rate_limit:
    enabled: "true"
  tls:
    hsts: "true"
"""


def test_every_field_reads_through_flag() -> None:
    """Present fields coerce to real bools, quoted or not -- the dial's reader
    treats "true"/"false" and true/false identically (yaml_subset.py has no
    type distinction), unlike the Helm values file this is copied into."""
    flags = render_dial._ui_flags(parse_dial(DIAL_ALL_SET, source="test-dial"))
    assert flags["ui.public.dfe_ui"] is True
    assert flags["ui.public.kafbat"] is False
    assert flags["ui.rate_limit.enabled"] is True
    assert flags["ui.tls.hsts"] is True


def test_an_absent_dial_takes_the_charts_own_defaults() -> None:
    """No ui: block at all -- every field takes envoy-gateway-config's default."""
    flags = render_dial._ui_flags(parse_dial("substrate: k8s\n", source="test-dial"))
    assert flags["ui.public.dfe_ui"] is True
    assert flags["ui.public.kafbat"] is False
    assert flags["ui.public.cruise_control"] is False
    assert flags["ui.public.hyperdx"] is False
    assert flags["ui.public.argocd"] is False
    assert flags["ui.public.links"] is False
    assert flags["ui.rate_limit.enabled"] is True
    assert flags["ui.tls.hsts"] is True


def test_a_non_boolean_value_is_refused_by_name() -> None:
    bad = DIAL_ALL_SET.replace('kafbat: "false"', 'kafbat: "flase"')
    with pytest.raises(render_dial.DialError, match=r"ui\.public\.kafbat"):
        render_dial._ui_flags(parse_dial(bad, source="test-dial"))


def test_a_non_boolean_rate_limit_flag_is_refused_by_name() -> None:
    bad = DIAL_ALL_SET.replace('enabled: "true"', 'enabled: "on"')
    with pytest.raises(render_dial.DialError, match=r"ui\.rate_limit\.enabled"):
        render_dial._ui_flags(parse_dial(bad, source="test-dial"))
