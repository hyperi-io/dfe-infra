#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_with_on_a_boolean.py
#  Purpose:      Assert no chart gates a boolean dial on `with`, which renders
#                nothing for false and silently leaves the default in force.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""`{{- with }}` skips a boolean false, so a false dial renders as absent.

Helm's `with` sets the scope only when its argument is non-empty, and `false`
is empty. A template that renders an env var, a flag or a field inside
`{{- with .Values.some.switch }}` therefore renders nothing at all when a
deployment sets that switch to false -- and "absent" and "false" are different
things to every consumer that has its own default. `DFE_CLICKHOUSE_VERIFY` was
written that way and would have left certificate verification on for exactly
the deployment that asked for it off. `if` is the form that renders both.

The engine chart's own case is proven end to end in
test_engine_deployment_facts.py, which renders `clickhouse.verify=false` and
asserts the variable arrives carrying "false". This file holds the class: every
`with` in every first-party chart, checked against the type its own values.yaml
declares, so the next boolean dial cannot be written the same way.

    python3 scripts/tests/test_with_on_a_boolean.py

Runs under pytest too, which is how CI reaches it. No helm needed -- it reads
the templates, because a template that renders nothing is exactly what the
rendered output cannot show.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from _charts import CHART_TREES
from _expect import expect, standalone, summary

_WITH = re.compile(r"{{-?\s*with\s+(?P<expr>.+?)\s*-?}}")
_VALUES_PATH = re.compile(r"\.Values\.(?P<path>[A-Za-z0-9_.]+)")
# The last dotted segment of a `with` argument, which is the key a local
# variable ranges over (`$pub.externalTrafficPolicy`, `$pool.weight`).
_LEAF = re.compile(r"(?P<leaf>[A-Za-z0-9_]+)\s*$")


def _templates() -> list[Path]:
    """Every first-party chart template, excluding vendored dependency copies."""
    found: list[Path] = []
    for tree in CHART_TREES:
        for path in sorted(tree.rglob("templates/*")):
            if path.suffix in (".yaml", ".tpl") and "/charts/" not in str(path.relative_to(tree)):
                found.append(path)
    return found


def _values_for(template: Path) -> dict:
    """The values.yaml of the chart the template belongs to."""
    for parent in template.parents:
        if (parent / "Chart.yaml").exists():
            file = parent / "values.yaml"
            return yaml.safe_load(file.read_text(encoding="utf-8")) or {} if file.exists() else {}
    return {}


def _at(values: dict, dotted: str):
    node = values
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _boolean_keys(node, found: set[str]) -> set[str]:
    """Every key anywhere in a values tree whose value is a boolean."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, bool):
                found.add(key)
            _boolean_keys(value, found)
    elif isinstance(node, list):
        for value in node:
            _boolean_keys(value, found)
    return found


def test_the_sweep_reads_a_real_tree() -> None:
    """A sweep whose globs stopped matching reports the same clean as a clean tree."""
    templates = _templates()
    expect("the sweep reaches every chart tree", len(templates) > 100, f"{len(templates)}")
    expect(
        "and finds the `with` blocks that are there",
        sum(len(_WITH.findall(t.read_text(encoding="utf-8"))) for t in templates) > 50,
        "no `with` matched, so the pattern has stopped working",
    )


def gated_booleans(template_text: str, values: dict) -> list[str]:
    """`with .Values.x.y` lines where values.yaml declares y a boolean."""
    offenders = []
    for number, line in enumerate(template_text.splitlines(), 1):
        for expr in _WITH.findall(line):
            for dotted in _VALUES_PATH.findall(expr):
                if isinstance(_at(values, dotted), bool):
                    offenders.append(f"{number}: with .Values.{dotted}")
    return offenders


def test_the_check_fires_on_a_boolean_dial() -> None:
    """The shape it exists to catch, which is how DFE_CLICKHOUSE_VERIFY was
    written before #277: `false` is empty, so `with` renders the whole block
    away and the consumer's own default stands."""
    found = gated_booleans(
        "            {{- with .Values.clickhouse.verify }}\n"
        "            - name: DFE_CLICKHOUSE_VERIFY\n"
        "              value: {{ . | quote }}\n"
        "            {{- end }}\n",
        {"clickhouse": {"verify": False}},
    )
    expect("the boolean dial is reported", found == ["1: with .Values.clickhouse.verify"], f"{found}")
    expect(
        "and a string dial beside it is not",
        gated_booleans(
            "{{- with .Values.clickhouse.user }}\n", {"clickhouse": {"user": "default"}}
        ) == [],
    )


def test_no_with_block_gates_a_values_boolean() -> None:
    """The direct form: `with .Values.x.y` where values.yaml declares y a bool."""
    offenders = []
    for template in _templates():
        found = gated_booleans(template.read_text(encoding="utf-8"), _values_for(template))
        rel = template.relative_to(Path(_charts_root()))
        offenders += [f"{rel}:{line}" for line in found]
    expect(
        "no chart renders a boolean dial under `with`",
        not offenders,
        "use `if` -- `with` renders nothing for false: " + "; ".join(offenders),
    )


def test_no_with_block_ranges_over_a_boolean_named_elsewhere() -> None:
    """The indirect form: `with $pub.someSwitch`, where the local variable came
    out of a range and only the KEY says what type it is."""
    booleans: set[str] = set()
    for tree in CHART_TREES:
        for file in tree.rglob("values*.yaml"):
            if "/charts/" in str(file.relative_to(tree)):
                continue
            _boolean_keys(yaml.safe_load(file.read_text(encoding="utf-8")) or {}, booleans)
    expect("the values sweep found boolean keys to check against", booleans, "none found")

    offenders = []
    for template in _templates():
        for number, line in enumerate(template.read_text(encoding="utf-8").splitlines(), 1):
            for expr in _WITH.findall(line):
                if ".Values." in expr:
                    continue  # covered by the direct check above
                leaf = _LEAF.search(expr)
                if leaf and leaf.group("leaf") in booleans and not _ranges_over_a_map(expr):
                    rel = template.relative_to(Path(_charts_root()))
                    offenders.append(f"{rel}:{number} with {expr}")
    expect(
        "no chart ranges `with` over a key some values file declares a boolean",
        not offenders,
        "use `if`: " + "; ".join(offenders),
    )


# `ui.public` is a MAP of booleans, so `with` on it is the map test, not a
# boolean one. Named rather than inferred: the exception has to be read.
_MAPS_OF_BOOLEANS = ("$ui.public",)


def _ranges_over_a_map(expr: str) -> bool:
    return expr.strip() in _MAPS_OF_BOOLEANS


def _charts_root() -> str:
    return str(Path(__file__).resolve().parent.parent.parent / "helm")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
