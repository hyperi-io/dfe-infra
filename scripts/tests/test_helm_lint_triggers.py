#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_helm_lint_triggers.py
#  Purpose:      Guard the paths that run the Helm Lint checks: every file the upgrade
#                tooling and the other tests read is in the filter, and every workflow
#                holding a check starts on every pull request so the check reports.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the PATHS filter of .github/workflows/helm-lint.yml and the triggers around it.

    python3 -m pytest scripts/tests/test_helm_lint_triggers.py -q

A pull request touching only a file outside PATHS skips every guard and merges unchecked.
The upgrade inputs come from the constants the upgrade module itself reads, so a new input it
grows is held to the same filter without this file being edited. The rest come from the globs
the other tests walk, and from the top-level names their sources name.

The filter lives in each workflow's `changes` job rather than in `on.<event>.paths`: a
workflow a path filter never starts reports no check at all, and a required check that never
reports blocks the merge, where a job skipped by its `if:` reports and passes.
"""

import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(1, str(REPO_ROOT / "bootstrap"))
import dfe_ops_upgrade as u  # noqa: E402

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "helm-lint.yml"
# The workflows whose jobs are required checks on main, each filtered by its `changes` job.
GATED = ("helm-lint.yml", "e2e-ui.yml", "tf-validate.yml")
RUN_UNLESS_NOTHING_RELEVANT = "!cancelled() && needs.changes.outputs.relevant != 'false'"

# What `dfe-ops upgrade` and its tests read from the checkout; constraints/ is what
# `dfe-stack compat-check --strict` reads for the target stack.
UPGRADE_INPUTS = (
    u.UPGRADE_ORDER,
    u.VERSIONS_FILE,
    u.DFE_STACK,
    u.ARGOCD_LOGIN,
    Path(u.__file__),
    Path(u.helm_releases.__file__),
    Path(u.argocd_release.__file__),
    REPO_ROOT / "bootstrap" / "bootstrap.sh",
    next((REPO_ROOT / "constraints").glob("*.yaml")),
)


# The trees and files the other scripts/tests modules read from outside the filtered
# directories, written as the globs those modules walk. No other workflow runs them.
TEST_READ_GLOBS = (
    ".github/workflows/*.yml",  # test_workflow_log_masks, test_toolbox_inputs
    "docs/**/*.md",  # test_no_cost_figures
    "terraform/**/*.tf",  # test_no_cost_figures, the drift check's sweep
    "sizing/**/*.yaml",  # test_validate_sizing, test_no_cost_figures
    "shapes/**/*.yaml",  # test_validate_sizing, test_no_cost_figures
    "shapes/**/*.json",  # test_no_cost_figures
    "docker/dfe-toolbox/base/*",  # test_toolbox_inputs
    "README.md",  # test_no_cost_figures
    "renovate.json",  # test_dfe_stack
    ".gitignore",  # test_check_versions_drift
    "tests/e2e-ui/specs/engine/oidc-rba.spec.ts",  # test_tester_idp
)

# `REPO_ROOT / "name"` and `REPO_ROOT.glob("name/...")` in a test source.
_NAMED_AT_ROOT = re.compile(
    r"""REPO_ROOT\s*/\s*["']([^"'/]+)["']|REPO_ROOT\.glob\(["']([^"'/*]+)[/"']"""
)


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    return doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` as true


def _filters() -> list[str]:
    """The patterns in the `changes` job's PATHS, the way its script reads them: `#` lines are notes."""
    text = _load(WORKFLOW.name)["jobs"]["changes"]["env"]["PATHS"]
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def _covers(patterns: list[str], path: str) -> bool:
    """Whether a filter names `path` as a file or under a `dir/**`, the two forms the script reads."""
    return any(path.startswith(p.removesuffix("**")) if p.endswith("/**") else path == p for p in patterns)


def test_the_filters_use_only_the_two_forms_the_script_reads() -> None:
    assert _filters(), "PATHS is empty, so every test below would pass"
    assert [p for p in _filters() if "*" in p.removesuffix("/**")] == []


@pytest.mark.parametrize("name", GATED)
def test_a_gated_workflow_starts_on_every_pull_request_and_every_push_to_main(name: str) -> None:
    triggers = _triggers(_load(name))
    assert triggers["pull_request"] is None, f"{name} filters pull_request: {triggers['pull_request']}"
    assert triggers["push"] == {"branches": ["main"]}, f"{name} filters push: {triggers['push']}"


@pytest.mark.parametrize("name", GATED)
def test_every_check_in_a_gated_workflow_runs_unless_its_filter_found_nothing(name: str) -> None:
    jobs = _load(name)["jobs"]
    assert "PATHS" in jobs["changes"]["env"], f"{name}'s changes job carries no PATHS"
    for job_id, job in jobs.items():
        if job_id == "changes":
            continue
        assert job.get("needs") == "changes", f"{name}:{job_id} does not need the changes job"
        condition = str(job.get("if", ""))
        assert RUN_UNLESS_NOTHING_RELEVANT in condition, f"{name}:{job_id} if: {condition}"


@pytest.mark.parametrize("path", UPGRADE_INPUTS, ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_every_file_the_upgrade_tooling_reads_starts_the_guards(path: Path) -> None:
    assert path.is_file(), path
    relative = path.relative_to(REPO_ROOT).as_posix()
    assert _covers(_filters(), relative), f"{relative} is not in helm-lint.yml's PATHS"


def _test_reads() -> list[str]:
    found = {p for pattern in TEST_READ_GLOBS for p in REPO_ROOT.glob(pattern) if p.is_file()}
    return sorted(path.relative_to(REPO_ROOT).as_posix() for path in found)


def test_every_file_the_other_tests_read_starts_the_guards() -> None:
    reads, filters = _test_reads(), _filters()
    assert reads, "the globs match nothing, so this test would pass for any filter"
    outside = [path for path in reads if not _covers(filters, path)]
    shown = f"{outside[:8]} and {len(outside) - 8} more" if len(outside) > 8 else f"{outside}"
    assert outside == [], f"read by a test, outside helm-lint.yml's PATHS: {shown}"


def test_every_top_level_name_a_test_source_reads_is_in_the_filters() -> None:
    """A test reading a new top-level path fails here until the filter names it."""
    named = {
        name
        for source in (REPO_ROOT / "scripts" / "tests").glob("*.py")
        for groups in _NAMED_AT_ROOT.findall(source.read_text(encoding="utf-8"))
        for name in groups
        if name and (REPO_ROOT / name).exists()
    }
    assert {"docs", "terraform", "README.md"} <= named, f"the scan found {sorted(named)}"
    filters = _filters()
    reached = {n for n in named if any(p == n or p.startswith(f"{n}/") for p in filters)}
    missing = sorted(named - reached)
    assert missing == [], f"named by a test, absent from helm-lint.yml's PATHS: {missing}"
