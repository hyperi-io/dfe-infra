#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         check_suite_drift.py
#  Purpose:      Keep suite.yaml honest: structurally sound, every edge
#                citation still there, every cited manifest still declaring
#                the producer's package with a version range, every chart and
#                app pin matched to a member or to a declared non-member, and
#                docs/suite-graph.md carrying the diagrams the file
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

FAIL (exit 1): a structural problem, a citation whose file no longer exists, a
cargo-dep or python-dep citation whose manifest no longer declares the
producer's package with a version range, an image-pin edge into dfe-infra
whose producer the current versions.yaml stack no longer pins, a `non_members`
entry that is also a node or that names nothing, or a docs diagram that
differs from what the file generates.

WARN (exit 0): a chart in the tree, a thin chart the stack pins, or a
versions.yaml app pin with no member, or a producer with out-edges and no
diagram block on the docs page -- advisory, because a repo joins the suite
when it tags and its chart may exist first. A name listed under `non_members`
raises no advisory: the reason is recorded there instead.

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

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest_deps
import suite_graph

REPO_ROOT = suite_graph.REPO_ROOT
DOCS_PAGE = REPO_ROOT / "docs" / "suite-graph.md"
CHARTS = REPO_ROOT / "helm" / "charts"
VERSIONS = REPO_ROOT / "versions.yaml"

_CITE = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+)/(?P<path>[^:]+?)(?::(?P<line>\d+)(?:-(?P<end>\d+))?)?$")


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


def declaration_problem(kind: str, manifest: str, package: str) -> str | None:
    """Why a cited manifest does not carry a cargo-dep or python-dep edge, or None when it does.

    The edge means the consumer declares the producer's package BY RANGE, so a
    declaration with no range (a workspace, path or git dependency) does not
    carry it on its own.

    Args:
        kind: The edge kind, one of manifest_deps.PACKAGE_KINDS.
        manifest: The cited manifest's contents.
        package: The name the producer publishes under.

    Returns:
        One clause naming what is wrong, or None.
    """
    try:
        found = manifest_deps.declarations(manifest, kind, package)
    except ValueError as exc:
        return f"the manifest is not readable TOML ({exc})"
    if not found:
        return f"the manifest does not declare {package}"
    if not any(declaration.spec for declaration in found):
        tables = ", ".join(f"[{declaration.table}]" for declaration in found)
        return f"the manifest declares {package} with no version range ({tables})"
    return None


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
            repo, rel, _start, end = _cited(citation)
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
            else:
                target = base / rel
                if not target.exists():
                    fails.append(f"{where}: file is gone")
                    continue
                content = target.read_text(encoding="utf-8", errors="replace")
            if end is not None and len(_split_lines(content)) < end:
                fails.append(f"{where}: file has fewer than {end} lines")
                continue
            kind = edge.get("kind")
            if field != "evidence" or kind not in manifest_deps.PACKAGE_KINDS:
                continue
            package = (graph["nodes"].get(edge["from"]) or {}).get("package")
            if not package:
                fails.append(f"{where}: node {edge['from']} declares no `package`")
            elif problem := declaration_problem(kind, content, package):
                fails.append(f"{where}: {problem}")
    return fails, sorted(no_clone), sorted(no_ref)


# A thin chart's name is its deploy.service, except where the appset maps one.
_THIN_CHART_NAMES = {"hyperdx": "dfe-hyperdx"}


def read_stack() -> tuple[list[str], dict[str, set[str]]]:
    """The current stack's section keys -- apps, content, digests, chart-digests.

    A versions.yaml this cannot read at all is a FAIL, not an advisory: with no
    `current:` pointer or no apps block there is nothing to check against, and
    reporting zero findings would read as a clean check. A missing section
    other than apps reads as empty.
    """
    text = VERSIONS.read_text(encoding="utf-8", errors="replace")
    current = re.search(r'^current:\s*"([^"]+)"', text, re.MULTILINE)
    if not current:
        return ["versions.yaml has no `current:` pointer"], {}
    block = re.search(rf"^  {re.escape(current.group(1))}:\n(.*?)(?=^  [0-9]|\Z)", text, re.MULTILINE | re.DOTALL)
    sections: dict[str, set[str]] = {}
    for name in ("apps", "content", "digests", "chart-digests"):
        section = rf"^    {re.escape(name)}:\n(.*?)(?=^    [a-z]|\Z)"
        found = re.search(section, block.group(1), re.MULTILINE | re.DOTALL) if block else None
        if found:
            keys = re.findall(r"^      ([A-Za-z0-9_.-]+):", found.group(1), re.MULTILINE)
            sections[name] = set(keys)
    if "apps" not in sections:
        return [f"versions.yaml stack {current.group(1)} has no apps: block"], {}
    return [], sections


def check_charts(graph: dict, stack: dict[str, set[str]]) -> tuple[list[str], set[str]]:
    """Every dfe-* chart in the tree and every thin chart the stack pins has a member (advisory).

    Returns the chart names it looked at, so a `non_members` entry naming none
    of them can be reported as stale.
    """
    declared = graph.get("non_members") or {}
    charts = {p.name for p in CHARTS.iterdir() if p.is_dir() and p.name.startswith("dfe-")}
    charts |= {_THIN_CHART_NAMES.get(s, s) for s in stack.get("chart-digests", set())}
    warns = [
        f"chart {chart} has no member in suite.yaml"
        for chart in sorted(charts)
        if chart not in graph["nodes"] and chart not in declared
    ]
    return warns, charts


def check_app_pins(graph: dict, stack: dict[str, set[str]]) -> tuple[list[str], set[str]]:
    """versions.yaml apps: keys with no member -- advisory, the pin may predate the tag."""
    declared = graph.get("non_members") or {}
    keys = stack.get("apps", set())
    warns = [
        f"versions.yaml apps.{key} has no member in suite.yaml"
        for key in sorted(keys)
        if key not in graph["nodes"] and key not in declared
    ]
    return warns, keys


def check_image_pins(graph: dict, stack: dict[str, set[str]]) -> list[str]:
    """Each image-pin edge into dfe-infra names a producer the current stack still pins.

    The pin is a tag under apps: or content: and its digest under digests:, so
    an edge whose producer lost either is describing a pin that is not there.
    """
    tagged = stack.get("apps", set()) | stack.get("content", set())
    fails = []
    for edge in graph["edges"]:
        if edge.get("kind") != "image-pin" or edge.get("to") != "dfe-infra":
            continue
        producer = edge["from"]
        missing = [
            what
            for what, present in (
                ("tag under apps: or content:", producer in tagged),
                ("digest under digests:", producer in stack.get("digests", set())),
            )
            if not present
        ]
        if missing:
            fails.append(
                f"{producer} -> dfe-infra image-pin: the current versions.yaml stack has no "
                f"{' and no '.join(missing)} for {producer}"
            )
    return fails


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
        f, stack = read_stack()
        fails += f
        if not f:
            w, charts = check_charts(graph, stack)
            warns += w
            w, pins = check_app_pins(graph, stack)
            warns += w
            fails += check_image_pins(graph, stack)
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
