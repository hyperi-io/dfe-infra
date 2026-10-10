#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_helm_lint_triggers.py
#  Purpose:      Guard the paths that start the Helm Lint workflow: every file the
#                upgrade tooling reads starts it, on a push and on a pull request alike.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the `paths:` filters of .github/workflows/helm-lint.yml.

    python3 -m pytest scripts/tests/test_helm_lint_triggers.py -q

A pull request touching only a file outside those filters runs none of the guards and
merges unchecked. The files come from the constants the upgrade module itself reads, so a
new input it grows is held to the same filter without this file being edited.
"""

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(1, str(REPO_ROOT / "bootstrap"))
import dfe_ops_upgrade as u  # noqa: E402

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "helm-lint.yml"
EVENTS = ("push", "pull_request")

# What `dfe-ops upgrade` and its tests read from the checkout; constraints/ is what
# `dfe-stack compat-check --strict` reads for the target stack.
UPGRADE_INPUTS = (
    u.UPGRADE_ORDER,
    u.VERSIONS_FILE,
    u.DFE_STACK,
    u.ARGOCD_LOGIN,
    u.VALUE_MAP,
    Path(u.__file__),
    Path(u.helm_releases.__file__),
    Path(u.argocd_release.__file__),
    REPO_ROOT / "bootstrap" / "bootstrap.sh",
    next((REPO_ROOT / "constraints").glob("*.yaml")),
)


def _filters(event: str) -> list[str]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` as true
    return triggers[event]["paths"]


def _covers(patterns: list[str], path: str) -> bool:
    """Whether a filter names `path` as a file or under a `dir/**`, the two forms this workflow uses."""
    return any(path.startswith(p.removesuffix("**")) if p.endswith("/**") else path == p for p in patterns)


@pytest.mark.parametrize("event", EVENTS)
def test_the_filters_use_only_the_two_forms_this_file_reads(event: str) -> None:
    assert [p for p in _filters(event) if "*" in p.removesuffix("/**")] == []


def test_a_push_and_a_pull_request_run_on_the_same_paths() -> None:
    assert sorted(_filters("push")) == sorted(_filters("pull_request"))


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize("path", UPGRADE_INPUTS, ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_every_file_the_upgrade_tooling_reads_starts_the_guards(event: str, path: Path) -> None:
    assert path.is_file(), path
    relative = path.relative_to(REPO_ROOT).as_posix()
    assert _covers(_filters(event), relative), f"{relative} is not in helm-lint.yml's {event} paths"
