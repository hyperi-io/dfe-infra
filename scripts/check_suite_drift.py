#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         check_suite_drift.py
#  Purpose:      Keep suite.yaml honest: structurally sound, every in-repo
#                edge citation still there, every chart and app pin matched
#                to a member, and docs/suite-graph.md carrying the diagrams
#                the file generates. Cross-repo citations are checked when
#                the member clones are present and reported SKIPPED, loudly,
#                when they are not.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Drift-check for suite.yaml, in the check_versions_drift.py mould.

    python3 scripts/check_suite_drift.py [--repos DIR] [--strict]

FAIL (exit 1): a structural problem, an in-repo citation that no longer exists,
a chart or pin that suite.yaml says exists and does not, or a docs diagram
that differs from what the file generates.

WARN (exit 0): a chart or versions.yaml app pin with no member -- advisory,
because a repo joins the suite when it tags and its chart may exist first.

SKIPPED: a citation into a member repo that is not on disk. Exit 0 by default
so the helm-lint job stays green on a bare checkout; exit 2 under --strict so
a release run cannot pass on a check that never ran. A skip is printed either
way -- a silent skip is how a check reports green while failing.

Member clones are looked for under --repos (default: the parent of this repo,
which is where /projects/<name> puts them).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import suite_graph  # noqa: E402

REPO_ROOT = suite_graph.REPO_ROOT
DOCS_PAGE = REPO_ROOT / "docs" / "suite-graph.md"
CHARTS = REPO_ROOT / "helm" / "charts"
VERSIONS = REPO_ROOT / "versions.yaml"

_CITE = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+)/(?P<path>[^:]+?)(?::(?P<line>\d+)(?:-(?P<end>\d+))?)?$")


def _cited(evidence: str) -> tuple[str, str, int | None]:
    m = _CITE.match(evidence)
    if not m:
        raise ValueError(f"unreadable citation {evidence!r}")
    end = m.group("end") or m.group("line")
    return m.group("repo"), m.group("path"), int(end) if end else None


def _line_count(path: Path) -> int:
    with path.open(encoding="utf-8", errors="replace") as fh:
        return sum(1 for _ in fh)


def check_citations(graph: dict, repos: Path) -> tuple[list[str], list[str]]:
    """In-repo citations must exist; cross-repo ones are checked when the clone is present."""
    fails: list[str] = []
    skipped: set[str] = set()
    for edge in graph["edges"] + graph.get("runtime_edges", []):
        evidence = edge.get("evidence")
        if not evidence:
            continue
        repo, rel, line = _cited(evidence)
        base = REPO_ROOT if repo == "dfe-infra" else repos / repo
        if repo != "dfe-infra" and not (base / ".git").exists():
            skipped.add(repo)
            continue
        target = base / rel
        where = f"{edge['from']} -> {edge['to']} cites {evidence}"
        if not target.exists():
            fails.append(f"{where}: file is gone")
        elif line is not None and _line_count(target) < line:
            fails.append(f"{where}: file has fewer than {line} lines")
    return fails, sorted(skipped)


def check_charts(graph: dict) -> tuple[list[str], list[str]]:
    """Every chart an edge cites exists; every dfe-* chart has a member (advisory)."""
    fails: list[str] = []
    warns: list[str] = []
    cited_charts: set[str] = set()
    for edge in graph["edges"]:
        m = re.match(r"^dfe-infra/helm/charts/([^/]+)/", edge.get("evidence", ""))
        if m:
            cited_charts.add(m.group(1))
            if not (CHARTS / m.group(1)).is_dir():
                fails.append(f"{edge['from']} -> {edge['to']}: chart {m.group(1)} is gone")
    for chart in sorted(p.name for p in CHARTS.iterdir() if p.is_dir() and p.name.startswith("dfe-")):
        if chart not in cited_charts and chart != "dfe-common" and chart != "dfe-schema":
            warns.append(f"chart {chart} has no member in suite.yaml")
    return fails, warns


def check_app_pins(graph: dict) -> list[str]:
    """versions.yaml apps: keys with no member -- advisory, the pin may predate the tag."""
    text = VERSIONS.read_text(encoding="utf-8", errors="replace")
    current = re.search(r'^current:\s*"([^"]+)"', text, re.MULTILINE)
    if not current:
        return ["versions.yaml has no `current:` pointer"]
    block = re.search(rf"^  {re.escape(current.group(1))}:\n(.*?)(?=^  [0-9]|\Z)", text, re.MULTILINE | re.DOTALL)
    apps = re.search(r"^    apps:\n(.*?)(?=^    [a-z]|\Z)", block.group(1), re.MULTILINE | re.DOTALL) if block else None
    if not apps:
        return [f"versions.yaml stack {current.group(1)} has no apps: block"]
    warns = []
    for key in re.findall(r"^      ([A-Za-z0-9_.-]+):", apps.group(1), re.MULTILINE):
        if key not in graph["nodes"]:
            warns.append(f"versions.yaml apps.{key} has no member in suite.yaml")
    return warns


def check_docs(graph: dict) -> tuple[list[str], list[str]]:
    """The page's diagrams match the file, and parse where a Mermaid linter is on the host."""
    rel = DOCS_PAGE.relative_to(REPO_ROOT)
    if not DOCS_PAGE.exists():
        return [f"{rel} is missing"], []
    text = DOCS_PAGE.read_text(encoding="utf-8", errors="replace")
    fresh = suite_graph.render_docs(graph, text)
    if fresh != text:
        return [f"{rel} diagrams differ from suite.yaml -- run `dfe-stack suite --render-docs`"], []
    maid = shutil.which("maid")
    if not maid:
        return [], ["mermaid syntax: `maid` is not on PATH"]
    proc = subprocess.run([maid, str(DOCS_PAGE)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip().splitlines()
        return [f"{rel}: maid rejects a diagram -- {detail[0] if detail else 'see `maid`'}"], []
    return [], []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repos", type=Path, default=REPO_ROOT.parent,
                        help="directory holding the member clones (default: this repo's parent)")
    parser.add_argument("--strict", action="store_true", help="exit 2 when any citation had to be skipped")
    args = parser.parse_args()

    graph = suite_graph.load()
    fails = [f"structure: {p}" for p in suite_graph.validate(graph)]
    warns: list[str] = []
    skipped: list[str] = []
    if not fails:
        f, skipped = check_citations(graph, args.repos.resolve())
        fails += f
        f, w = check_charts(graph)
        fails += f
        warns += w
        warns += check_app_pins(graph)
        f, s = check_docs(graph)
        fails += f
        skipped += s

    for line in warns:
        print(f"WARN  {line}")
    for line in fails:
        print(f"FAIL  {line}")
    repos_skipped = [s for s in skipped if not s.startswith("mermaid syntax")]
    for s in skipped:
        if s.startswith("mermaid syntax"):
            print(f"SKIPPED  {s}")
    if repos_skipped:
        print(f"SKIPPED  citations into {', '.join(repos_skipped)}: clone not found under {args.repos}")
    if fails:
        print(f"FAIL -- {len(fails)} problem(s) in suite.yaml")
        return 1
    print(f"OK -- suite.yaml: {len(graph['nodes'])} members, {len(graph['edges'])} edges, "
          f"{len(warns)} advisory, {len(skipped)} repo(s) skipped")
    if skipped and args.strict:
        print("FAIL -- --strict and the cross-repo citations were never checked")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
