#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         check_suite_drift.py
#  Purpose:      Keep suite.yaml honest: structurally sound, every in-repo
#                edge citation still there and still naming the producer's
#                package, every chart and app pin matched to a member or to a
#                declared non-member, and docs/suite-graph.md carrying the
#                diagrams the file
#                generates. The docs check is MARKERS-DRIVEN -- it renders
#                the blocks the page already carries -- so a producer with no
#                diagram block is an advisory rather than a failure.
#                Cross-repo citations are read from each member clone's
#                fetched `--ref` (default origin/main), never the clone's
#                working tree, so a clone sitting on a feature branch or
#                behind origin cannot produce a false pass or a false fail.
#                Reported SKIPPED, loudly, when the clone or the ref is not
#                there to read.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Drift-check for suite.yaml, in the check_versions_drift.py mould.

    python3 scripts/check_suite_drift.py [--repos DIR] [--ref REF] [--strict]

FAIL (exit 1): a structural problem, an in-repo citation that no longer exists
or no longer names the producer's package, a chart or pin that suite.yaml says
exists and does not, a `non_members` entry that is also a node or that names
nothing, or a docs diagram that differs from what the file generates.

WARN (exit 0): a chart or versions.yaml app pin with no member, or a producer
with out-edges and no diagram block on the docs page -- advisory, because a
repo joins the suite when it tags and its chart may exist first. A name listed
under `non_members` raises no advisory: the reason is recorded there instead.

SKIPPED: a citation into a member repo that is not on disk, or whose `--ref`
(default origin/main) the clone does not have. Exit 0 by default so the
helm-lint job stays green on a bare checkout; exit 2 under --strict so a run
that had the clones cannot pass on a check that never looked at them. A skip
is printed either way -- a silent skip is how a check reports green while
failing.

`--strict` is for an OPERATOR'S pre-release run, on a box with the member
clones on disk. It is NOT for a CI workflow: a runner has no member clones, so
every cross-repo citation is skipped there and --strict would exit 2 every
time.

Member clones are looked for under --repos (default: the parent of this repo,
which is where /projects/<name> puts them). A cross-repo citation is read from
the clone's `--ref` (default origin/main) via `git show`, not the working
tree -- a clone on a feature branch or behind origin cannot false-pass or
false-fail a citation the main branch does not have. This check does NOT
fetch: the operator runs `git fetch origin main` in each member clone first.
Pass `--ref ""` to read the working tree instead, the old behaviour. dfe-infra's
own citations always read this repo's working tree -- it is the repo under
check, so a `--ref` on it would check whether this change has landed on main,
which is backwards.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import suite_graph

REPO_ROOT = suite_graph.REPO_ROOT
DOCS_PAGE = REPO_ROOT / "docs" / "suite-graph.md"
CHARTS = REPO_ROOT / "helm" / "charts"
VERSIONS = REPO_ROOT / "versions.yaml"

_CITE = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+)/(?P<path>[^:]+?)(?::(?P<line>\d+)(?:-(?P<end>\d+))?)?$")

# The kinds whose citation is a DECLARATION of the producer's package. The
# cited line has to name that package, or the range the tooling reads there
# belongs to something else.
_PACKAGE_KINDS = ("cargo-dep", "python-dep")

_PY_STRING = re.compile(r'"([^"]*)"')


def _cited(evidence: str) -> tuple[str, str, int | None, int | None]:
    m = _CITE.match(evidence)
    if not m:
        raise ValueError(f"unreadable citation {evidence!r}")
    start = m.group("line")
    end = m.group("end") or start
    return (
        m.group("repo"),
        m.group("path"),
        int(start) if start else None,
        int(end) if end else None,
    )


def _split_lines(text: str) -> list[str]:
    """Physical lines, newline-separators stripped -- the unit both readers produce."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _line_at(lines: list[str], number: int) -> str:
    return lines[number - 1] if 0 < number <= len(lines) else ""


def _ref_exists(git: str, clone: Path, ref: str) -> bool:
    proc = subprocess.run(
        [git, "-C", str(clone), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0


def _git_show(git: str, clone: Path, ref: str, rel: str) -> tuple[bool, str]:
    """The blob at ref:rel -- (False, "") when that path does not exist at ref."""
    proc = subprocess.run(
        [git, "-C", str(clone), "show", f"{ref}:{rel}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode == 0, proc.stdout


def declares_package(kind: str, line: str, package: str) -> bool:
    """Whether a cited line declares ``package`` in that kind's manifest language.

    A commented-out line declares nothing, whatever it says.
    """
    if line.lstrip().startswith("#"):
        return False
    if kind == "cargo-dep":
        return bool(re.search(rf"^\s*{re.escape(package)}\s*(?:=|\.workspace\b)", line))
    for candidate in _PY_STRING.findall(line):
        text = candidate.strip()
        if not text.startswith(package):
            continue
        rest = text[len(package) :]
        if not rest or rest[0] in "[ <>=!~":
            return True
    return False


def check_citations(
    graph: dict, repos: Path, ref: str, git: str | None
) -> tuple[list[str], list[str], list[str]]:
    """In-repo citations read dfe-infra's working tree; cross-repo ones read `ref`.

    `ref` empty keeps the old working-tree read for cross-repo citations too.
    Returns (fails, repos with no clone on disk, repos whose clone lacks `ref`).
    """
    fails: list[str] = []
    no_clone: set[str] = set()
    no_ref: set[str] = set()
    ref_present: dict[str, bool] = {}
    for edge in graph["edges"] + graph.get("runtime_edges", []):
        for field in ("evidence", "source"):
            citation = edge.get(field)
            if not citation:
                continue
            repo, rel, start, end = _cited(citation)
            cross_repo = repo != "dfe-infra"
            base = repos / repo if cross_repo else REPO_ROOT
            if cross_repo and not (base / ".git").exists():
                no_clone.add(repo)
                continue
            where = f"{edge['from']} -> {edge['to']} {field} {citation}"
            if cross_repo and ref:
                if not git:
                    no_ref.add(repo)
                    continue
                if repo not in ref_present:
                    ref_present[repo] = _ref_exists(git, base, ref)
                if not ref_present[repo]:
                    no_ref.add(repo)
                    continue
                found, content = _git_show(git, base, ref, rel)
                if not found:
                    fails.append(f"{where}: file is gone")
                    continue
                lines = _split_lines(content)
            else:
                target = base / rel
                if not target.exists():
                    fails.append(f"{where}: file is gone")
                    continue
                lines = _split_lines(target.read_text(encoding="utf-8", errors="replace"))
            if end is not None and len(lines) < end:
                fails.append(f"{where}: file has fewer than {end} lines")
                continue
            kind = edge.get("kind")
            if field != "evidence" or kind not in _PACKAGE_KINDS or start is None:
                continue
            package = (graph["nodes"].get(edge["from"]) or {}).get("package")
            if not package:
                fails.append(f"{where}: node {edge['from']} declares no `package`")
            elif not declares_package(kind, _line_at(lines, start), package):
                fails.append(f"{where}: the cited line does not declare {package}")
    return fails, sorted(no_clone), sorted(no_ref)


def check_charts(graph: dict) -> tuple[list[str], list[str], set[str]]:
    """Every chart an edge cites exists; every dfe-* chart has a member (advisory).

    Returns the chart names it looked at, so a `non_members` entry naming none
    of them can be reported as stale.
    """
    fails: list[str] = []
    warns: list[str] = []
    declared = graph.get("non_members") or {}
    cited_charts: set[str] = set()
    for edge in graph["edges"]:
        m = re.match(r"^dfe-infra/helm/charts/([^/]+)/", edge.get("evidence", ""))
        if m:
            cited_charts.add(m.group(1))
            if not (CHARTS / m.group(1)).is_dir():
                fails.append(f"{edge['from']} -> {edge['to']}: chart {m.group(1)} is gone")
    charts = {p.name for p in CHARTS.iterdir() if p.is_dir() and p.name.startswith("dfe-")}
    for chart in sorted(charts):
        if chart not in cited_charts and chart not in declared:
            warns.append(f"chart {chart} has no member in suite.yaml")
    return fails, warns, charts


def check_app_pins(graph: dict) -> tuple[list[str], list[str], set[str]]:
    """versions.yaml apps: keys with no member -- advisory, the pin may predate the tag.

    A versions.yaml this cannot read at all is a FAIL, not an advisory: with no
    `current:` pointer or no apps block there is nothing to be advisory about,
    and reporting zero advisories would read as a clean check.
    """
    text = VERSIONS.read_text(encoding="utf-8", errors="replace")
    current = re.search(r'^current:\s*"([^"]+)"', text, re.MULTILINE)
    if not current:
        return ["versions.yaml has no `current:` pointer"], [], set()
    block = re.search(rf"^  {re.escape(current.group(1))}:\n(.*?)(?=^  [0-9]|\Z)", text, re.MULTILINE | re.DOTALL)
    apps = re.search(r"^    apps:\n(.*?)(?=^    [a-z]|\Z)", block.group(1), re.MULTILINE | re.DOTALL) if block else None
    if not apps:
        return [f"versions.yaml stack {current.group(1)} has no apps: block"], [], set()
    declared = graph.get("non_members") or {}
    warns = []
    keys = set(re.findall(r"^      ([A-Za-z0-9_.-]+):", apps.group(1), re.MULTILINE))
    for key in sorted(keys):
        if key not in graph["nodes"] and key not in declared:
            warns.append(f"versions.yaml apps.{key} has no member in suite.yaml")
    return [], warns, keys


def check_non_members(graph: dict, named: set[str]) -> list[str]:
    """A silenced advisory must still be about something that exists.

    An entry that is also a node contradicts the file, and one matching no
    chart and no app pin silences an advisory nothing can raise any more.
    """
    fails: list[str] = []
    for name, why in sorted((graph.get("non_members") or {}).items()):
        if not isinstance(why, str) or not why.strip():
            fails.append(f"non_members {name}: no reason recorded")
        if name in graph["nodes"]:
            fails.append(f"non_members {name}: is also a node, so the file disagrees with itself")
        elif name not in named:
            fails.append(
                f"non_members {name}: names no chart and no versions.yaml app pin -- "
                f"the entry has outlived the advisory it silences"
            )
    return fails


def check_producer_diagrams(graph: dict) -> list[str]:
    """Producers with out-edges and no diagram block on the page -- advisory.

    The docs check renders the blocks the page already carries, so a producer
    nobody added a block for is invisible to it rather than wrong.
    """
    if not DOCS_PAGE.exists():
        return []
    text = DOCS_PAGE.read_text(encoding="utf-8", errors="replace")
    drawn = {
        name.split(":", 1)[1]
        for name in suite_graph.doc_block_names(text)
        if name.startswith("producer:")
    }
    producers = {edge["from"] for edge in graph["edges"]}
    return [
        f"producer {name} has out-edges and no diagram block in "
        f"{DOCS_PAGE.relative_to(REPO_ROOT)}"
        for name in sorted(producers - drawn)
    ]


def check_docs(graph: dict) -> tuple[list[str], list[str]]:
    """The page's diagrams match the file, and parse where a Mermaid linter is on the host."""
    rel = DOCS_PAGE.relative_to(REPO_ROOT)
    if not DOCS_PAGE.exists():
        return [f"{rel} is missing"], []
    text = DOCS_PAGE.read_text(encoding="utf-8", errors="replace")
    try:
        fresh = suite_graph.render_docs(graph, text)
    except ValueError as exc:
        return [f"{rel}: {exc}"], []
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
    parser.add_argument("--ref", default="origin/main",
                        help="git ref to read cross-repo citations from (default: origin/main); "
                             "pass --ref '' to read each clone's working tree instead")
    parser.add_argument("--strict", action="store_true", help="exit 2 when any citation had to be skipped")
    args = parser.parse_args()

    graph = suite_graph.load()
    fails = [f"structure: {p}" for p in suite_graph.validate(graph)]
    warns: list[str] = []
    no_clone: list[str] = []
    no_ref: list[str] = []
    tool_skipped: list[str] = []
    git = shutil.which("git") if args.ref else None
    if not fails:
        f, no_clone, no_ref = check_citations(graph, args.repos.resolve(), args.ref, git)
        fails += f
        f, w, charts = check_charts(graph)
        fails += f
        warns += w
        f, w, pins = check_app_pins(graph)
        fails += f
        warns += w
        fails += check_non_members(graph, charts | pins)
        warns += check_producer_diagrams(graph)
        f, s = check_docs(graph)
        fails += f
        tool_skipped += s

    for line in warns:
        print(f"WARN  {line}")
    for line in fails:
        print(f"FAIL  {line}")
    for line in tool_skipped:
        print(f"SKIPPED  {line}")
    if no_clone:
        print(f"SKIPPED  citations into {', '.join(no_clone)}: clone not found under {args.repos}")
    if no_ref:
        reason = f"{args.ref} not found -- git fetch first" if git else "git is not on PATH"
        print(f"SKIPPED  citations into {', '.join(no_ref)}: {reason}")
    skipped = no_clone + no_ref
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
