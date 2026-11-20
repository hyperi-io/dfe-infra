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
        (
            "tagline: We leverage it; it should work.\n",
            {"register", "semicolon", "hedge"},
        ),
        ("- code: |-\n    a; b\n", set()),
        ("- {label: Robust docs, to: /x/}\n", {"register"}),
    ],
)
def test_the_gate_reads_a_copy_file(
    tmp_path: Path, yaml_text: str, rules: set[str]
) -> None:
    copy = tmp_path / "landing.yaml"
    copy.write_text(yaml_text, encoding="utf-8")
    phrases = gate.load_phrases(
        gate.aggregate.yaml_subset.parse(gate.GATE_FILE.read_text())
    )
    assert {finding.rule for finding in gate.gate_files([copy], phrases)} == rules


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        pytest.param(
            "Index engines such as Elasticsearch build an index.",
            False,
            id="product_name",
        ),
        pytest.param(
            "Index engines such as ElasticSearch build an index.", True, id="misspelt"
        ),
        pytest.param(
            "Your Elastic ingest pipelines come across.", False, id="company_and_stack"
        ),
    ],
)
def test_the_gate_flags_only_the_misspelt_product_name(
    line: str, flagged: bool
) -> None:
    rules = {rule for rule, _, _ in gate.check_line(line, {})}
    assert ("elasticsearch" in rules) is flagged


def test_the_gate_skips_generated_section_indexes_and_api_pages(tmp_path: Path) -> None:
    """Their text repeats titles the gate already reads on each page they list."""
    import json

    build = tmp_path / "2.2.1"
    for relative, text in {
        "svc/index.md": f"{gate.aggregate.SECTION_INDEX_MARK}\n# svc\n",
        "own/index.md": "# Own landing\n",
        "own/page.md": "# Page\n",
        "api/op.md": "# Generated\n",
    }.items():
        path = build / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    site = {"builds": [{"name": "2.2.1", "openapi": {"output": "2.2.1/api"}}]}
    (tmp_path / gate.aggregate.SITE_FILE).write_text(json.dumps(site), encoding="utf-8")
    assert [page for _, page, _ in gate.pages_in(tmp_path)] == [
        "own/index.md",
        "own/page.md",
    ]
