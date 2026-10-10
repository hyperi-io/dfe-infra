#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_helm_lint_triggers.py
#  Purpose:      Guard the paths that start the Helm Lint workflow: every file the
#                upgrade tooling and the other tests read starts it, on a push and on a
#                pull request alike.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the `paths:` filters of .github/workflows/helm-lint.yml.

    python3 -m pytest scripts/tests/test_helm_lint_triggers.py -q

A pull request touching only a file outside those filters runs none of the guards and
merges unchecked. The upgrade inputs come from the constants the upgrade module itself reads,
so a new input it grows is held to the same filter without this file being edited. The rest
come from the globs the other tests walk, and from the top-level names their sources name.
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


# The trees and files the other scripts/tests modules read from outside the filtered
# directories, written as the globs those modules walk. No other workflow runs them.
TEST_READ_GLOBS = (
    ".github/workflows/*.yml",  # test_workflow_log_masks, test_cloud_reaper, test_toolbox_inputs
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


def _test_reads() -> list[str]:
    found = {p for pattern in TEST_READ_GLOBS for p in REPO_ROOT.glob(pattern) if p.is_file()}
    return sorted(path.relative_to(REPO_ROOT).as_posix() for path in found)


@pytest.mark.parametrize("event", EVENTS)
def test_every_file_the_other_tests_read_starts_the_guards(event: str) -> None:
    reads, filters = _test_reads(), _filters(event)
    assert reads, "the globs match nothing, so this test would pass for any filter"
    outside = [path for path in reads if not _covers(filters, path)]
    shown = f"{outside[:8]} and {len(outside) - 8} more" if len(outside) > 8 else f"{outside}"
    assert outside == [], f"read by a test, outside helm-lint.yml's {event} paths: {shown}"


@pytest.mark.parametrize("event", EVENTS)
def test_every_top_level_name_a_test_source_reads_is_in_the_filters(event: str) -> None:
    """A test reading a new top-level path fails here until the filter names it."""
    named = {
        name
        for source in (REPO_ROOT / "scripts" / "tests").glob("*.py")
        for groups in _NAMED_AT_ROOT.findall(source.read_text(encoding="utf-8"))
        for name in groups
        if name and (REPO_ROOT / name).exists()
    }
    assert {"docs", "terraform", "README.md"} <= named, f"the scan found {sorted(named)}"
    filters = _filters(event)
    reached = {n for n in named if any(p == n or p.startswith(f"{n}/") for p in filters)}
    missing = sorted(named - reached)
    assert missing == [], f"named by a test, absent from helm-lint.yml's {event} paths: {missing}"
