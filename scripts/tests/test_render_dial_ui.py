#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_render_dial_ui.py
#  Purpose:      Guard the deprecated `ui:` block -- it still feeds the `edge:`
#                block that replaced it, every boolean reads through the same
#                _flag() path, and a bad value is refused by the OLD name so an
#                un-migrated dial says where the fault is.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the `ui:` block as a one-release alias of `edge:`.

    python3 -m pytest scripts/tests/test_render_dial_ui.py -q

The block moved under `edge.product` and `edge.admin_uis`, and render_dial.py
reads the old paths for one release so a dial written before the move still
renders. This covers the dial-side half: an old value reaches the new path, and
a value that is not exactly true/false is refused here by the name the dial
wrote. The chart-side half (a value that survives the dial as a string rather
than a bool is refused at Helm render time) lives in
scripts/test-route-exposure.sh instead, because that failure mode is a real
Go-template kind difference (bool vs string) that only exists once the block
reaches a real YAML/Helm parse -- render_dial's own restricted YAML reader
(yaml_subset.py) has no such distinction, quoted or not.
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
    flags = render_dial._edge_flags(parse_dial(DIAL_ALL_SET, source="test-dial"))
    assert flags["edge.product.public"] is True
    assert flags["edge.admin_uis.public.kafbat"] is False
    assert flags["edge.product.rate_limit.enabled"] is True
    assert flags["edge.product.tls.hsts"] is True


def test_an_absent_dial_takes_the_charts_own_defaults() -> None:
    """No ui: block and no edge: block -- every field takes its chart default."""
    flags = render_dial._edge_flags(parse_dial("substrate: k8s\n", source="test-dial"))
    assert flags["edge.product.public"] is True
    assert flags["edge.admin_uis.public.kafbat"] is False
    assert flags["edge.admin_uis.public.cruise_control"] is False
    assert flags["edge.admin_uis.public.hyperdx"] is False
    assert flags["edge.admin_uis.public.argocd"] is False
    assert flags["edge.admin_uis.public.links"] is False
    assert flags["edge.product.rate_limit.enabled"] is True
    assert flags["edge.product.tls.hsts"] is True


def test_a_non_boolean_value_is_refused_by_name() -> None:
    """The name quoted is the one the dial wrote, not the one it should have."""
    bad = DIAL_ALL_SET.replace('kafbat: "false"', 'kafbat: "flase"')
    with pytest.raises(render_dial.DialError, match=r"ui\.public\.kafbat"):
        render_dial._edge_flags(parse_dial(bad, source="test-dial"))


def test_a_non_boolean_rate_limit_flag_is_refused_by_name() -> None:
    bad = DIAL_ALL_SET.replace('enabled: "true"', 'enabled: "on"')
    with pytest.raises(render_dial.DialError, match=r"ui\.rate_limit\.enabled"):
        render_dial._edge_flags(parse_dial(bad, source="test-dial"))


def test_the_old_block_is_reported_as_deprecated_by_name() -> None:
    """A dial still on the old block renders, and is told where the key went."""
    lines = render_dial._edge_deprecations(parse_dial(DIAL_ALL_SET, source="test-dial"))
    assert "ui.public.dfe_ui moved to edge.product.public" in " ".join(lines)
    assert "ui.public.kafbat moved to edge.admin_uis.public.kafbat" in " ".join(lines)


def test_the_new_block_silences_the_deprecation_for_that_key_alone() -> None:
    dial = parse_dial(
        DIAL_ALL_SET.replace('dfe_ui: "true"', 'dfe_ui: ""')
        + "edge:\n  product:\n    public: \"true\"\n",
        source="test-dial",
    )
    lines = " ".join(render_dial._edge_deprecations(dial))
    assert "ui.public.dfe_ui" not in lines
    assert "ui.public.kafbat moved to edge.admin_uis.public.kafbat" in lines


# ---------------------------------------------------------------------------
# The engine API on the product's public hostname
# ---------------------------------------------------------------------------
#
# The path families are chart data (routes.dfeEngine), so the dial owns the two
# switches over them and the summary line that says which doors they leave open.


def test_the_engine_api_switches_default_to_the_browsers_families_alone() -> None:
    flags = render_dial._edge_flags(parse_dial("substrate: k8s\n", source="test-dial"))
    assert flags["edge.engine_api.with_product"] is True
    assert flags["edge.engine_api.cli_families_public"] is False
    assert flags["edge.engine_api.scim_public"] is False


def test_the_engine_api_switches_read_through_quoted_or_not() -> None:
    dial = parse_dial(
        "edge:\n"
        "  engine_api:\n"
        '    cli_families_public: "true"\n'
        "    scim_public: true\n",
        source="test-dial",
    )
    flags = render_dial._edge_flags(dial)
    assert flags["edge.engine_api.cli_families_public"] is True
    assert flags["edge.engine_api.scim_public"] is True


@pytest.mark.parametrize("key", ["with_product", "cli_families_public", "scim_public"])
def test_an_engine_api_switch_that_is_not_a_boolean_is_refused_by_name(key: str) -> None:
    dial = parse_dial(
        f"edge:\n  engine_api:\n    {key}: sometimes\n", source="test-dial"
    )
    with pytest.raises(render_dial.DialError, match=rf"edge\.engine_api\.{key}"):
        render_dial._edge_flags(dial)


def test_the_retired_families_key_is_refused_and_names_both_switches() -> None:
    """A dial still carrying it thinks something reads it, so it stops rather
    than rendering with a key that has no reader."""
    dial = parse_dial(
        "edge:\n  engine_api:\n    private_path_families: []\n", source="test-dial"
    )
    with pytest.raises(render_dial.DialError, match=r"private_path_families") as raised:
        render_dial._edge_refusals(
            dial, render_dial._edge_flags(dial), render_dial._edge_enums(dial)
        )
    assert "cli_families_public" in str(raised.value)
    assert "scim_public" in str(raised.value)


@pytest.mark.parametrize(
    ("cli", "scim", "expected"),
    [
        (False, False, "browser families only"),
        (True, False, "browser families, plus the CLI families and /openapi.json"),
        (False, True, "browser families only, plus SCIM"),
        (True, True, "browser families, plus the CLI families and /openapi.json, plus SCIM"),
    ],
)
def test_the_summary_line_says_which_families_answer(
    cli: bool, scim: bool, expected: str
) -> None:
    flags = {
        "edge.engine_api.with_product": True,
        "edge.engine_api.cli_families_public": cli,
        "edge.engine_api.scim_public": scim,
    }
    assert render_dial._engine_api_summary(flags) == expected


def test_the_summary_line_says_the_engine_is_off_the_public_hostname() -> None:
    flags = {
        "edge.engine_api.with_product": False,
        "edge.engine_api.cli_families_public": True,
        "edge.engine_api.scim_public": True,
    }
    assert "no public hostname" in render_dial._engine_api_summary(flags)
