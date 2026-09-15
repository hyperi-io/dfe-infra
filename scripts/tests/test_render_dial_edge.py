#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_render_dial_edge.py
#  Purpose:      Guard the dial's `edge:` block -- every boolean and every enum
#                is refused by name, the deprecated blocks feed it for one
#                release, a dial setting both spellings of one key is refused,
#                and the summary names each tier-2 opt-in with its bucket.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for render_dial.py's `edge:` block.

    python3 -m pytest scripts/tests/test_render_dial_edge.py -q

One block names every door a deployment opens, and this renderer applies none
of it except `edge.enabled`. So the value of the block is what it REFUSES: a
boolean that is not a boolean, an enum outside the vocabulary its chart accepts,
a combination no chart renders, and a dial that says one thing twice.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import render_dial  # noqa: E402
from yaml_subset import parse as parse_dial  # noqa: E402

EXAMPLE = REPO_ROOT / "deployment.example.yaml"


def dial(body: str) -> dict[str, object]:
    return parse_dial(body, source="test-dial")


def example() -> dict[str, object]:
    return parse_dial(EXAMPLE.read_text(encoding="utf-8"), source=EXAMPLE.name)


# ---------------------------------------------------------------------------
# Booleans
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(render_dial._EDGE_BOOL_DEFAULTS))
def test_every_boolean_is_refused_by_name(path: tuple[str, ...]) -> None:
    """Each one reads through the same _flag() path as endpoint.public."""
    body = "\n".join(
        f"{'  ' * depth}{key}:" + (' "yes"' if depth == len(path) - 1 else "")
        for depth, key in enumerate(path)
    )
    with pytest.raises(render_dial.DialError, match=re.escape(".".join(path))):
        render_dial._edge_flags(dial(body + "\n"))


def test_each_boolean_reads_through_quoted_or_not() -> None:
    parsed = dial(
        "edge:\n"
        "  enabled: false\n"
        "  ingest:\n"
        "    tunnel:\n"
        '      enabled: "true"\n'
    )
    flags = render_dial._edge_flags(parsed)
    assert flags["edge.enabled"] is False
    assert flags["edge.ingest.tunnel.enabled"] is True


def test_the_tunnel_is_off_until_a_deployment_asks() -> None:
    """The module OFFERS the tunnel; the engine turns it on by writing a file."""
    assert render_dial._edge_flags(dial("substrate: k8s\n"))["edge.ingest.tunnel.enabled"] is False
    assert render_dial._edge_flags(example())["edge.ingest.tunnel.enabled"] is False


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(render_dial._EDGE_ENUMS))
def test_every_enum_refuses_a_value_outside_its_vocabulary(path: tuple[str, ...]) -> None:
    body = "\n".join(
        f"{'  ' * depth}{key}:" + (" nosuchvalue" if depth == len(path) - 1 else "")
        for depth, key in enumerate(path)
    )
    with pytest.raises(render_dial.DialError, match=re.escape(".".join(path))):
        render_dial._edge_enums(dial(body + "\n"))


@pytest.mark.parametrize("path", sorted(render_dial._EDGE_ENUMS))
def test_every_named_value_of_every_enum_reads_through(path: tuple[str, ...]) -> None:
    for value in render_dial._EDGE_ENUMS[path]:
        body = "\n".join(
            f"{'  ' * depth}{key}:" + (f" {value}" if depth == len(path) - 1 else "")
            for depth, key in enumerate(path)
        )
        assert render_dial._edge_enums(dial(body + "\n"))[".".join(path)] == value


@pytest.mark.parametrize(("cloud", "flavour"), [("aws", "aws"), ("gcp", "gcp"),
                                                ("azure", "azure"), ("local", "onprem"),
                                                ("rancher", "onprem")])
def test_the_flavour_defaults_to_the_one_the_cloud_fact_selects(cloud: str, flavour: str) -> None:
    """The appset derives it the same way, so a dial that states none still reports it."""
    assert render_dial._edge_flavour(dial(f"k8s:\n  cloud: {cloud}\n")) == flavour


def test_the_receivers_door_still_falls_back_to_the_cloud_overlay() -> None:
    """A dial naming no mode reports whatever argocd/values/<cloud>.yaml applies."""
    for cloud, mode in (("aws", "vpn"), ("gcp", "vpn"), ("azure", "vpn"), ("local", "internal")):
        enums = render_dial._edge_enums(dial(f"k8s:\n  cloud: {cloud}\n"))
        assert enums["edge.ingest.receiver.mode"] == mode, cloud


# ---------------------------------------------------------------------------
# The one-release aliases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("new", "old"), sorted(render_dial._EDGE_ALIASES.items()))
def test_every_alias_feeds_its_new_path(new: tuple[str, ...], old: tuple[str, ...]) -> None:
    body = "\n".join(
        f"{'  ' * depth}{key}:" + (" fromtheoldpath" if depth == len(old) - 1 else "")
        for depth, key in enumerate(old)
    )
    value, label = render_dial._edge_scalar(dial(body + "\n"), new)
    assert value == "fromtheoldpath"
    assert label == ".".join(old)


@pytest.mark.parametrize(("new", "old"), sorted(render_dial._EDGE_ALIASES.items()))
def test_setting_both_spellings_of_one_key_is_refused(
    new: tuple[str, ...], old: tuple[str, ...]
) -> None:
    """They are one key, so a dial that sets both has two answers and no winner."""
    body = "\n".join(
        f"{'  ' * depth}{key}:" + (" old" if depth == len(old) - 1 else "")
        for depth, key in enumerate(old)
    ) + "\n" + "\n".join(
        f"{'  ' * depth}{key}:" + (" new" if depth == len(new) - 1 else "")
        for depth, key in enumerate(new)
    )
    conflicts = render_dial._edge_alias_conflicts(dial(body + "\n"))
    assert any(".".join(new) in c and ".".join(old) in c for c in conflicts), conflicts


def test_a_dial_on_the_new_block_alone_reports_no_deprecation() -> None:
    assert render_dial._edge_deprecations(example()) == []
    assert render_dial._edge_alias_conflicts(example()) == []


# ---------------------------------------------------------------------------
# What the module does not offer
# ---------------------------------------------------------------------------


def test_a_named_private_path_family_is_refused() -> None:
    """No route splits a path family off the product route, so a value here
    would look applied and do nothing."""
    parsed = dial("edge:\n  engine_api:\n    private_path_families: [hunt]\n")
    with pytest.raises(render_dial.DialError, match=r"private_path_families"):
        render_dial._edge_refusals(
            parsed, render_dial._edge_flags(parsed), render_dial._edge_enums(parsed)
        )


def test_an_empty_private_path_family_list_is_accepted() -> None:
    parsed = dial("edge:\n  engine_api:\n    private_path_families: []\n")
    render_dial._edge_refusals(
        parsed, render_dial._edge_flags(parsed), render_dial._edge_enums(parsed)
    )


def test_a_public_otel_door_with_no_auth_is_refused() -> None:
    """Authentication is the gate on making the OTLP door public."""
    parsed = dial("edge:\n  ingest:\n    otel:\n      public: true\n      auth: none\n")
    with pytest.raises(render_dial.DialError, match=r"edge\.ingest\.otel\.public"):
        render_dial._edge_refusals(
            parsed, render_dial._edge_flags(parsed), render_dial._edge_enums(parsed)
        )


def test_a_public_otel_door_with_auth_required_is_accepted() -> None:
    parsed = dial("edge:\n  ingest:\n    otel:\n      public: true\n      auth: required\n")
    render_dial._edge_refusals(
        parsed, render_dial._edge_flags(parsed), render_dial._edge_enums(parsed)
    )


# ---------------------------------------------------------------------------
# The printed summary
# ---------------------------------------------------------------------------


def test_the_shipped_example_turns_on_no_tier_two_key() -> None:
    """Tier 1 is the default posture; every costed door is the deployer's act."""
    assert render_dial._edge_tier2_on(render_dial._edge_enums(example())) == []


def test_a_tier_two_key_is_named_with_its_bucket_and_never_a_rate() -> None:
    parsed = dial("edge:\n  ingest:\n    receiver:\n      mode: public\n")
    lines = render_dial._edge_tier2_on(render_dial._edge_enums(parsed))
    assert len(lines) == 1, lines
    assert "edge.ingest.receiver.mode: public" in lines[0]
    assert "bucket L" in lines[0]
    assert "per GB processed" in lines[0]
    assert not re.search(r"\$|USD|\d+\s*/\s*(month|hour)", lines[0]), lines[0]


def test_every_tier_two_row_names_a_key_the_enums_carry() -> None:
    """A row keyed on a path no enum validates would never fire."""
    known = {".".join(p) for p in render_dial._EDGE_ENUMS}
    assert {row[0] for row in render_dial._EDGE_TIER2} <= known


def test_the_render_prints_the_edge_summary(tmp_path: Path) -> None:
    (tmp_path / "deployment.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8").replace(
            "      mode: vpn              #", "      mode: public           #"
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_dial.py"),
         "--dial", str(tmp_path / "deployment.yaml"),
         "--out", str(tmp_path / "bootstrap.env")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "EDGE MODULE (aws) -- on" in result.stderr
    assert "public hostnames (edge.product.public, edge.admin_uis.public.*): dfe-ui" in result.stderr
    assert "fleet tunnel (edge.ingest.tunnel.enabled): off" in result.stderr
    assert "tier 2 opt-ins that are ON: edge.ingest.receiver.mode: public" in result.stderr
    assert "bucket L" in result.stderr


def test_a_dial_setting_both_spellings_fails_the_render(tmp_path: Path) -> None:
    (tmp_path / "deployment.yaml").write_text(
        EXAMPLE.read_text(encoding="utf-8") + "\nui:\n  public:\n    dfe_ui: false\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_dial.py"),
         "--dial", str(tmp_path / "deployment.yaml"),
         "--out", str(tmp_path / "bootstrap.env")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "edge.product.public and ui.public.dfe_ui" in result.stderr


def test_the_shipped_example_parses_and_validates() -> None:
    parsed = example()
    assert render_dial._edge_alias_conflicts(parsed) == []
    flags = render_dial._edge_flags(parsed)
    enums = render_dial._edge_enums(parsed)
    render_dial._edge_refusals(parsed, flags, enums)
    assert flags["edge.enabled"] is True
    assert enums["edge.flavour"] == "aws"
    assert enums["edge.ingest.tunnel.pki_mode"] == "external"
    assert enums["edge.ingest.receiver.mode"] == "vpn"
