#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_composition_fragments.py
#  Purpose:      Prove every app's copy of a shared values fragment is the
#                fragment under argocd/values/apps/_fragments, and that the
#                writer refuses a block it cannot rewrite.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the shared-fragment blocks scripts/composition.py writes.

Helm replaces a list rather than merging it, so a list item several apps share
sits whole in each app's integration values, between markers. Nothing stops a
hand edit inside one copy except the comparison here.

    python3 -m pytest scripts/tests/test_composition_fragments.py -q
"""

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import composition  # noqa: E402

BEGIN = "# BEGIN fragment {name} -- rendered by `python3 scripts/composition.py --write-fragments`"


def _block(name: str, indent: str = "", body: str = "") -> str:
    return f"{indent}{BEGIN.format(name=name)}\n{body}{indent}# END fragment {name}\n"


def test_every_values_block_holds_its_fragment() -> None:
    assert composition.write_fragments(check_only=True) == 0


def test_every_fragment_is_copied_somewhere() -> None:
    used = set()
    for path in composition._app_values_files():
        text = path.read_text(encoding="utf-8")
        used |= {m["name"] for m in composition.FRAGMENT_BEGIN.finditer(text)}
    sources = {p.stem for p in composition.FRAGMENTS.glob("*.yaml")}
    assert sources <= used, f"fragments no app copies: {sorted(sources - used)}"


def test_a_block_takes_its_markers_indentation() -> None:
    text = "keda:\n  extraTriggers:\n" + _block("pressure-trigger", indent="    ")
    rendered = composition.render_fragments(text)
    parsed = yaml.safe_load(rendered)
    assert parsed["keda"]["extraTriggers"] == yaml.safe_load(
        composition.fragment("pressure-trigger")
    )


def test_a_current_block_is_left_as_it_is() -> None:
    text = "initContainers:\n" + _block("wait-for-engine", indent="  ")
    rendered = composition.render_fragments(text)
    assert composition.render_fragments(rendered) == rendered


def test_the_leading_comment_of_a_fragment_is_not_copied() -> None:
    source = (composition.FRAGMENTS / "wait-for-engine.yaml").read_text(encoding="utf-8")
    assert source.startswith("#")
    assert composition.fragment("wait-for-engine").startswith("- name: wait-for-engine\n")


# ---------------------------------------------------------------- expected fails


def test_a_hand_edit_inside_a_block_is_caught() -> None:
    text = "initContainers:\n" + _block("wait-for-engine", indent="  ")
    current = composition.render_fragments(text)
    edited = current.replace("cpu: 10m", "cpu: 20m")
    assert edited != current
    assert composition.render_fragments(edited) == current


def test_a_block_without_its_end_marker_is_refused() -> None:
    text = f"initContainers:\n  {BEGIN.format(name='wait-for-engine')}\n  - name: x\n"
    with pytest.raises(composition.CompositionError, match="no END marker"):
        composition.render_fragments(text, "x.yaml")


def test_an_end_marker_at_another_indentation_does_not_close_a_block() -> None:
    text = (
        f"initContainers:\n  {BEGIN.format(name='wait-for-engine')}\n"
        "# END fragment wait-for-engine\n"
    )
    with pytest.raises(composition.CompositionError, match="no END marker"):
        composition.render_fragments(text, "x.yaml")


def test_a_block_naming_no_fragment_is_refused() -> None:
    with pytest.raises(composition.CompositionError, match="no fragment 'nowhere'"):
        composition.render_fragments(_block("nowhere"), "x.yaml")


def test_check_mode_reports_a_stale_file_and_leaves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apps = tmp_path / "argocd" / "values" / "apps"
    (apps / "_fragments").mkdir(parents=True)
    (apps / "_fragments" / "one.yaml").write_text("# header\n- a: 1\n", encoding="utf-8")
    (apps / "svc").mkdir()
    stale = apps / "svc" / "values.yaml"
    body = "items:\n" + _block("one", indent="  ", body="  - a: 2\n")
    stale.write_text(body, encoding="utf-8")
    monkeypatch.setattr(composition, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(composition, "APPS_VALUES", apps)
    monkeypatch.setattr(composition, "FRAGMENTS", apps / "_fragments")
    assert composition.write_fragments(check_only=True) == 1
    assert stale.read_text(encoding="utf-8") == body
    assert composition.write_fragments() == 0
    assert yaml.safe_load(stale.read_text(encoding="utf-8")) == {"items": [{"a": 1}]}
    assert composition.write_fragments(check_only=True) == 0
