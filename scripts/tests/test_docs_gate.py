#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_docs_gate.py
#  Purpose:      Cover docs-site/scripts/gate.py: what it reads as prose in a copy/ YAML file.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the docs gate.

python3 -m pytest scripts/tests/test_docs_gate.py -q
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "docs-site" / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("docs_gate", SCRIPTS / "gate.py")
gate = importlib.util.module_from_spec(spec)
sys.modules["docs_gate"] = gate
spec.loader.exec_module(gate)


@pytest.mark.parametrize(
    ("yaml_text", "prose"),
    [
        ("title: Stand DFE up\n", ["Stand DFE up"]),
        ('text: "One host, your own data."\n', ["One host, your own data."]),
        ("hero:\n  tagline: Every agent in.\n", ["", "Every agent in."]),
        ("- {label: Docs, to: /overview/}\n", ["Docs"]),
        ('link: {label: All releases, href: "https://x"}\n', ["All releases"]),
        ("to: /quickstart/\nhref: https://x\nicon: host\n", ["", "", ""]),
        ("# a comment\n", [""]),
        ("- code: |-\n    git clone x; make up\n- text: After.\n", ["", "", "After."]),
        (
            "cards:\n  - code: |-\n      a; b\n\n      c\n  - text: Next.\n",
            ["", "", "", "", "", "Next."],
        ),
    ],
)
def test_copy_prose_keeps_values_on_their_lines_and_drops_the_rest(
    yaml_text: str, prose: list[str]
) -> None:
    assert gate.copy_prose(yaml_text).split("\n") == prose


@pytest.mark.parametrize(
    ("yaml_text", "rules"),
    [
        ("tagline: A data pipeline you run yourself.\n", set()),
        ("tagline: We leverage it; it should work.\n", {"register", "semicolon", "hedge"}),
        ("- code: |-\n    a; b\n", set()),
        ("- {label: Robust docs, to: /x/}\n", {"register"}),
    ],
)
def test_the_gate_reads_a_copy_file(tmp_path: Path, yaml_text: str, rules: set[str]) -> None:
    copy = tmp_path / "landing.yaml"
    copy.write_text(yaml_text, encoding="utf-8")
    phrases = gate.load_phrases(gate.aggregate.yaml_subset.parse(gate.GATE_FILE.read_text()))
    assert {finding.rule for finding in gate.gate_files([copy], phrases)} == rules
