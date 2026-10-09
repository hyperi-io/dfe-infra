#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_suite_drift.py
#  Purpose:      Cover check_suite_drift.py's verdicts -- what FAILs, what is
#                only advisory, and what --strict does about a citation that
#                was never checked.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/check_suite_drift.py.

A drift check whose patterns have stopped matching prints the same OK as one
that is working, so each verdict gets a case that produces it. Every case
builds a whole tiny repo layout in a temp directory -- the two scripts, a
suite.yaml, a versions.yaml, two charts, the docs page and the member clones --
and runs the check there as a real subprocess. Nothing is mocked and no module
attribute is patched: the check reads its own repo root off its own location,
so a temp copy is the only honest way to give it a different tree.

The member clone (app-b) is a REAL git repo -- one commit, with
refs/remotes/origin/main pointed at it to stand in for a fetch -- because the
check now reads that ref via `git show`, not the clone's working tree. A case
that wants to prove the ref is what gets read, not the checkout, commits one
Cargo.toml and then dirties the working tree with a different one afterward.

    python3 scripts/tests/test_check_suite_drift.py

No third-party deps and no test runner, matching the tools it tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import suite_graph  # noqa: E402

GIT = shutil.which("git")

SUITE = '''\
schema: "1"
verified: "2026-09-03"
edge_kinds:
  cargo-dep:
    means: "range"
    check: "does it admit"
    gates: [cargo-build]
  image-pin:
    means: "tag plus digest"
    check: "bump and re-resolve"
    gates: [drift-check]
runtime_kinds:
  http-api:
    means: "the consumer calls the producer's API at run time"
nodes:
  lib-a:
    repo: org/lib-a
    package: liba
    role: library
    language: rust
    audience: general
    maturity: ga
    support: standard
    licence: Apache-2.0
    classification: oss
    classification_source: in-repo
    default_in_pass: true
    artefacts:
      - kind: crate
        registry: crates.io
        public: true
  app-b:
    repo: org/app-b
    role: service
    language: rust
    audience: suite
    maturity: ga
    support: standard
    licence: BUSL-1.1
    classification: product
    classification_source: org-property
    default_in_pass: true
    artefacts:
      - kind: container
        registry: ghcr
        public: false
  dfe-infra:
    repo: org/dfe-infra
    role: infra
    language: python
    audience: suite
    maturity: ga
    support: standard
    licence: BUSL-1.1
    classification: product
    classification_source: org-property
    default_in_pass: true
    artefacts:
      - kind: git-tag
        registry: github
        public: false
edges:
  - from: lib-a
    to: app-b
    kind: cargo-dep
    type: potential
    evidence: "app-b/Cargo.toml:6"
  - from: app-b
    to: dfe-infra
    kind: image-pin
    type: lockstep
    evidence: "dfe-infra/helm/charts/dfe-app-b/Chart.yaml:6"
lanes:
  - name: libraries
    members: [lib-a]
    why: "first"
  - name: consumers
    members: [app-b]
    why: "second"
  - name: deployment
    members: [dfe-infra]
    why: "last"
'''

VERSIONS = '''\
current: "1.0.0"
stacks:
  1.0.0:
    apps:
      app-b:
        tag: "v1"
'''

CHART = '''\
apiVersion: v2
name: dfe-app-b
description: a chart
type: application
version: 0.1.0
appVersion: "v1"
'''

CARGO = '''\
[package]
name = "app-b"

[dependencies]
tokio = "1"
liba = { version = ">=1.0, <2" }
'''

PAGE_HEAD = "# page\n\n"

# The same lib-a -> app-b dependency declared a second way, as a Python
# distribution. The cited line is the fourth, the quoted requirement.
PYPROJECT = '''\
[project]
name = "app-b"
dependencies = [
    "liba>=1.0,<2",
]
'''

PY_KIND = """\
  python-dep:
    means: "range"
    check: "does it admit"
    gates: [pytest]
"""

PY_EDGE = """\
  - from: lib-a
    to: app-b
    kind: python-dep
    type: potential
    evidence: "app-b/pyproject.toml:4"
"""

PY_SUITE = SUITE.replace("  image-pin:\n", PY_KIND + "  image-pin:\n", 1).replace(
    "lanes:\n", PY_EDGE + "lanes:\n", 1
)


def build(
    root: Path,
    *,
    suite: str = SUITE,
    versions: str = VERSIONS,
    cargo: str = CARGO,
    clones: bool = True,
    orphan_chart: bool = True,
    stale_docs: bool = False,
    worktree_cargo: str | None = None,
    with_ref: bool = True,
    clone_files: dict[str, str] | None = None,
) -> Path:
    """A whole tiny dfe-infra beside its member clones, ready to check.

    `worktree_cargo`, when given, overwrites app-b's Cargo.toml AFTER the
    commit -- an uncommitted change standing in for a feature branch or a
    clone behind origin, to prove the check reads `ref` and not the checkout.
    `with_ref` false leaves refs/remotes/origin/main unset, standing in for a
    clone that has never been fetched. `clone_files` adds files to app-b's
    commit, keyed by path in the clone.
    """
    infra = root / "infra"
    (infra / "scripts").mkdir(parents=True)
    for name in ("suite_graph.py", "check_suite_drift.py"):
        shutil.copyfile(SCRIPTS / name, infra / "scripts" / name)
    _write(infra / "suite.yaml", suite)
    _write(infra / "versions.yaml", versions)
    _write(infra / "helm/charts/dfe-app-b/Chart.yaml", CHART)
    if orphan_chart:
        _write(infra / "helm/charts/dfe-orphan/Chart.yaml", CHART)

    graph = suite_graph.load(infra / "suite.yaml")
    body = "stale\n" if stale_docs else None
    page = PAGE_HEAD + (
        "<!-- suite-graph:begin producer:lib-a -->\n"
        + (body or "")
        + "<!-- suite-graph:end producer:lib-a -->\n"
        "<!-- suite-graph:begin producer:app-b -->\n"
        "<!-- suite-graph:end producer:app-b -->\n"
    )
    if not stale_docs:
        page = suite_graph.render_docs(graph, page)
    _write(infra / "docs/suite-graph.md", page)

    if clones:
        _make_clone(
            root / "repos" / "app-b",
            cargo,
            worktree_cargo=worktree_cargo,
            with_ref=with_ref,
            clone_files=clone_files or {},
        )
    return infra


def _make_clone(
    clone: Path,
    cargo: str,
    *,
    worktree_cargo: str | None,
    with_ref: bool,
    clone_files: dict[str, str],
) -> None:
    """A real app-b repo: one commit, refs/remotes/origin/main standing in for a fetch."""
    clone.mkdir(parents=True)
    _run_git(clone, "init", "-q", "-b", "main")
    _run_git(clone, "config", "user.email", "test@example.invalid")
    _run_git(clone, "config", "user.name", "Test")
    _write(clone / "Cargo.toml", cargo)
    _run_git(clone, "add", "Cargo.toml")
    for name, text in clone_files.items():
        _write(clone / name, text)
        _run_git(clone, "add", name)
    _run_git(clone, "commit", "-q", "-m", "initial")
    if with_ref:
        _run_git(clone, "update-ref", "refs/remotes/origin/main", "HEAD")
    if worktree_cargo is not None:
        _write(clone / "Cargo.toml", worktree_cargo)


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [GIT, *args], cwd=cwd, check=True, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def check(infra: Path, *args: str) -> tuple[int, str]:
    """Run the copied check with a PATH holding git but never `maid`."""
    restricted = infra.parent / "restricted-path"
    restricted.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = str(Path(GIT).parent) if GIT else str(restricted)
    proc = subprocess.run(
        [sys.executable, str(infra / "scripts" / "check_suite_drift.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _one_case(
    name: str, expect_code: int, needle: str, *, extra_args: tuple[str, ...] = (), **kwargs
) -> None:
    """One built tree, one check run, one verdict."""
    no_app_b_block = kwargs.pop("no_app_b_block", False)
    with tempfile.TemporaryDirectory(prefix="suite-drift-") as tmp:
        root = Path(tmp)
        infra = build(root, **kwargs)
        if no_app_b_block:
            page = infra / "docs" / "suite-graph.md"
            text = page.read_text(encoding="utf-8", errors="replace")
            head, _, _ = text.partition("<!-- suite-graph:begin producer:app-b -->")
            page.write_text(head, encoding="utf-8", newline="\n")
        code, output = check(infra, "--repos", str(root / "repos"), *extra_args)
        expect(name, code == expect_code and needle in output, f"exit {code}\n{output}")


def main() -> int:
    with standalone():
        with tempfile.TemporaryDirectory(prefix="suite-drift-") as tmp:
            root = Path(tmp)
            infra = build(root)
            repos = str(root / "repos")

            code, output = check(infra, "--repos", repos)
            expect("a sound tree with the clones present is OK",
                   code == 0 and "OK -- suite.yaml" in output, f"exit {code}\n{output}")
            expect("a chart with no member is advisory, not a failure",
                   "WARN  chart dfe-orphan" in output, output)
            expect("maid missing is reported, not swallowed",
                   "SKIPPED  mermaid syntax" in output, output)

            code, output = check(infra, "--repos", repos, "--strict")
            expect("--strict with maid absent and the clones present still exits 0",
                   code == 0, f"exit {code}\n{output}")
            expect("maid absent is not counted as a repo skip",
                   "0 repo(s) skipped" in output, output)

            code, output = check(infra, "--repos", str(root / "nowhere"), "--strict")
            expect("--strict with the clones absent exits 2",
                   code == 2 and "never checked" in output, f"exit {code}\n{output}")

            code, output = check(infra, "--repos", str(root / "nowhere"))
            expect("without --strict the same skip exits 0 and says so",
                   code == 0 and "SKIPPED  citations into app-b" in output,
                   f"exit {code}\n{output}")

        _one_case(
            "a citation correct on origin/main but wrong in the working tree is OK",
            0,
            "OK -- suite.yaml",
            worktree_cargo=CARGO.replace('liba = { version = ">=1.0, <2" }', 'tokio2 = "1"'),
        )
        _one_case(
            "a citation wrong on origin/main but correct in the working tree FAILs",
            1,
            "does not declare liba",
            cargo=CARGO.replace('liba = { version = ">=1.0, <2" }', 'tokio2 = "1"'),
            worktree_cargo=CARGO,
        )
        _one_case(
            "--ref '' reads the working tree, the old behaviour",
            0,
            "OK -- suite.yaml",
            cargo=CARGO.replace('liba = { version = ">=1.0, <2" }', 'tokio2 = "1"'),
            worktree_cargo=CARGO,
            extra_args=("--ref", ""),
        )
        _one_case(
            "a clone with no origin/main is SKIPPED, naming the ref",
            0,
            "SKIPPED  citations into app-b: origin/main not found -- git fetch first",
            with_ref=False,
        )
        _one_case(
            "--strict over a clone with no origin/main exits 2",
            2,
            "never checked",
            with_ref=False,
            extra_args=("--strict",),
        )

        _one_case(
            "an in-repo citation whose file is gone FAILs",
            1,
            "file is gone",
            suite=SUITE.replace(
                'evidence: "dfe-infra/helm/charts/dfe-app-b/Chart.yaml:6"',
                'evidence: "dfe-infra/helm/charts/dfe-app-b/values.yaml:6"',
            ),
        )
        _one_case(
            "a citation past the end of its file FAILs",
            1,
            "fewer than 99 lines",
            suite=SUITE.replace("Cargo.toml:6", "Cargo.toml:99"),
        )
        _one_case(
            "a cited line that declares another package FAILs",
            1,
            "does not declare liba",
            cargo=CARGO.replace('liba = { version = ">=1.0, <2" }', 'tokio2 = "1"'),
        )
        _one_case(
            "a commented-out declaration is not a declaration",
            1,
            "does not declare liba",
            cargo=CARGO.replace("liba =", "# liba ="),
        )
        _one_case(
            "a python-dep citation on its requirement line is OK",
            0,
            "OK -- suite.yaml",
            suite=PY_SUITE,
            clone_files={"pyproject.toml": PYPROJECT},
        )
        _one_case(
            "a python-dep citation whose line moved FAILs",
            1,
            "app-b/pyproject.toml:4: the cited line does not declare liba",
            suite=PY_SUITE,
            clone_files={
                "pyproject.toml": PYPROJECT.replace(
                    "dependencies = [\n", 'dependencies = [\n    "tokio>=1",\n'
                )
            },
        )
        _one_case(
            "a python-dep line naming a longer distribution is not the package",
            1,
            "does not declare liba",
            suite=PY_SUITE,
            clone_files={"pyproject.toml": PYPROJECT.replace("liba>=", "liba-extra>=")},
        )
        _one_case(
            "a producer with no package declared FAILs rather than guessing",
            1,
            "declares no `package`",
            suite=SUITE.replace("    package: liba\n", ""),
        )
        _one_case(
            "docs diagrams out of sync FAIL",
            1,
            "diagrams differ from suite.yaml",
            stale_docs=True,
        )
        _one_case(
            "a versions.yaml with no apps block is a failure, not an advisory",
            1,
            "has no apps: block",
            versions='current: "1.0.0"\nstacks:\n  1.0.0:\n    images:\n      x: "y"\n',
        )
        _one_case(
            "a versions.yaml app with no member is advisory",
            0,
            "WARN  versions.yaml apps.ghost",
            versions=VERSIONS + "      ghost:\n        tag: \"v1\"\n",
        )
        _one_case(
            "a producer with no diagram block is advisory",
            0,
            "has out-edges and no diagram block",
            stale_docs=False,
            no_app_b_block=True,
        )
        _one_case(
            "a chart declared a non-member raises no advisory",
            0,
            "0 advisory",
            suite=SUITE + 'non_members:\n  dfe-orphan: "no release tag yet"\n',
        )
        _one_case(
            "a non-member that is also a node FAILs",
            1,
            "is also a node",
            suite=SUITE + 'non_members:\n  app-b: "no release tag yet"\n',
        )
        _one_case(
            "a non-member naming no chart and no pin FAILs as stale",
            1,
            "outlived the advisory",
            suite=SUITE + 'non_members:\n  dfe-gone: "no release tag yet"\n',
        )

        # Held at zero on the committed tree: a new chart joins the graph or
        # records in suite.yaml why it does not.
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check_suite_drift.py")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        expect("the committed suite.yaml raises no advisory",
               "0 advisory" in proc.stdout, proc.stdout + proc.stderr)
        expect("and still passes", proc.returncode == 0, f"exit {proc.returncode}")
        return summary()


if __name__ == "__main__":
    raise SystemExit(main())
