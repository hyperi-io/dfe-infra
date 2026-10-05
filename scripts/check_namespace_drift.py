#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/check_namespace_drift.py
#  Purpose:      Drift guard: resolve the operator-namespace SSoT in
#                argocd/values/common.yaml against where the appsets actually
#                install each operator, so a rename cannot silently strand a
#                NetworkPolicy selector.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Validate the operator-namespace SSoT against the appsets (issue #136).

A NetworkPolicy namespaceSelector naming a namespace that does not exist is
valid YAML, matches nothing, and raises no error anywhere -- so the operator
loses its data-plane access and the only symptom is a cluster that never goes
Ready. That is exactly how #136 survived four months: the selector was written
against `clickhouse-operator-system` while the appset installed the operator
into `clickhouse-operator`.

The fix made argocd/values/common.yaml `operatorNamespaces` the SSoT. This
check closes the loop in both directions:

  1. every SSoT entry matches the destination namespace of its operator chart
     in the appsets, and
  2. the network-policies chart defaults agree with the SSoT, so a standalone
     render is not quietly weaker than a deployed one.

Usage:
    python3 scripts/check_namespace_drift.py

Needs PyYAML (same dependency as the sibling validators).
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("ERROR: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMON_VALUES = REPO_ROOT / "argocd" / "values" / "common.yaml"
APPSET_DIR = REPO_ROOT / "argocd" / "appsets"
NETPOL_VALUES = REPO_ROOT / "helm" / "charts" / "network-policies" / "values.yaml"

# SSoT operator key -> the chart that installs it. The chart name is the join
# key because it is what the appset element carries; the namespace is the very
# thing under test and so cannot be used to match.
OPERATOR_CHARTS = {
    "clickhouse": "clickhouse-operator-helm",
    "strimzi": "strimzi-kafka-operator",
}


def _load_yaml(path: Path) -> object:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _walk_chart_elements(node: object, found: dict[str, set[str]]) -> None:
    """Collect chart -> destination namespaces from every appset list element."""
    if isinstance(node, dict):
        chart, namespace = node.get("chart"), node.get("namespace")
        # The templated `{{ .chart }}` pair in the generator body consumes the
        # list rather than belonging to it, so it is not a declaration.
        if (
            isinstance(chart, str)
            and isinstance(namespace, str)
            and "{{" not in chart
            and "{{" not in namespace
        ):
            found.setdefault(chart, set()).add(namespace)
        for value in node.values():
            _walk_chart_elements(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk_chart_elements(item, found)


def _appset_namespaces() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(APPSET_DIR.glob("*.yaml")):
        with open(path, encoding="utf-8") as fh:
            for doc in yaml.safe_load_all(fh):
                _walk_chart_elements(doc, found)
    return found


def _operator_namespaces(parsed: object, source: Path) -> dict[str, str]:
    if not isinstance(parsed, dict):
        raise SystemExit(f"FAIL: {source} did not parse as a mapping")
    ns = parsed.get("operatorNamespaces")
    if not isinstance(ns, dict):
        raise SystemExit(
            f"FAIL: {source} has no `operatorNamespaces` mapping "
            "(a list here is the pre-#136 shape -- it cannot be cross-checked)"
        )
    return ns


def compare(
    ssot: dict[str, str],
    netpol: dict[str, str],
    appsets: dict[str, set[str]],
) -> list[str]:
    """Return one message per disagreement; empty means the three agree."""
    failures: list[str] = []

    for operator, namespace in sorted(ssot.items()):
        chart = OPERATOR_CHARTS.get(operator)
        if chart is None:
            failures.append(
                f"{operator}: no chart mapped in OPERATOR_CHARTS -- add it, or the "
                "entry is unverifiable and buys nothing"
            )
            continue
        installed = appsets.get(chart)
        if not installed:
            failures.append(f"{operator}: chart `{chart}` is in no appset element")
        elif installed != {namespace}:
            failures.append(
                f"{operator}: common.yaml says `{namespace}`, appsets install "
                f"`{chart}` into {sorted(installed)}"
            )

    for operator, namespace in sorted(netpol.items()):
        expected = ssot.get(operator)
        if expected is None:
            failures.append(
                f"{operator}: in the network-policies defaults but not in the common.yaml SSoT"
            )
        elif expected != namespace:
            failures.append(
                f"{operator}: network-policies default `{namespace}` != SSoT `{expected}`"
            )

    for operator in sorted(set(ssot) - set(netpol)):
        failures.append(
            f"{operator}: in the common.yaml SSoT but not in the network-policies "
            "defaults, so a standalone render omits its ingress rule"
        )

    return failures


def main() -> int:
    ssot = _operator_namespaces(_load_yaml(COMMON_VALUES), COMMON_VALUES)
    netpol = _operator_namespaces(_load_yaml(NETPOL_VALUES), NETPOL_VALUES)
    failures = compare(ssot, netpol, _appset_namespaces())

    if failures:
        print("FAIL: operator-namespace drift", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        f"PASS: {len(ssot)} operator namespaces agree across common.yaml, "
        "the appsets and the network-policies defaults"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
