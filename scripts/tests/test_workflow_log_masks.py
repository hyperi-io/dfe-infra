#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_workflow_log_masks.py
#  Purpose:      Guard every workflow job that assumes an AWS role or passes a
#                role or account variable to a step: its first step registers
#                the masks, and nothing runs or prints before it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the account and role masks across .github/workflows.

    python3 -m pytest scripts/tests/test_workflow_log_masks.py -q

dfe-infra is public, so every Actions log is world-readable. The runner prints a
step's env, inputs and script with their variables expanded, and a `::add-mask::`
covers only the output written after the step that registers it. A job that
assumes a role therefore masks the account and the role in its first step,
before any later step prints them. Nothing here reaches GitHub or AWS.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
# A variable that carries an account id, expanded as it is. `vars.X != ''` is a
# boolean and prints nothing, so only the bare form counts.
BARE_ROLE_OR_ACCOUNT = re.compile(r"\$\{\{\s*vars\.\w*(?:ROLE|ACCOUNT|BOUNDARY)\w*\s*\}\}")
ASSUMES_A_ROLE = "aws-actions/configure-aws-credentials@"


def _workflows() -> dict[str, dict]:
    return {path.name: yaml.safe_load(path.read_text(encoding="utf-8")) for path in sorted(WORKFLOWS.glob("*.yml"))}


def _carries(node: object) -> bool:
    return bool(BARE_ROLE_OR_ACCOUNT.search(json.dumps(node)))


def _assumes_a_role(job: dict) -> bool:
    return any(str(step.get("uses", "")).startswith(ASSUMES_A_ROLE) for step in job.get("steps", []))


def _handlers() -> list[tuple[str, str]]:
    """Every (workflow file, job id) that assumes a role or passes a role or account variable on."""
    return [
        (name, job_id)
        for name, doc in _workflows().items()
        for job_id, job in doc.get("jobs", {}).items()
        if _carries(job) or _assumes_a_role(job)
    ]


def _steps(name: str, job_id: str) -> list[dict]:
    return _workflows()[name]["jobs"][job_id]["steps"]


def test_the_jobs_that_assume_a_role_are_found() -> None:
    """The discovery above would pass vacuously if the pattern stopped matching."""
    assert {("cloud-cycle.yml", "cycle"), ("cloud-reaper.yml", "reap")} <= set(_handlers())


@pytest.mark.parametrize(("name", "job_id"), _handlers())
def test_the_masking_step_comes_first_so_no_step_before_it_can_print_the_role_or_account(
    name: str, job_id: str
) -> None:
    steps = _steps(name, job_id)
    masking = [i for i, step in enumerate(steps) if "::add-mask::" in step.get("run", "")]
    assert masking, f"{name}:{job_id} never masks"
    assert masking[0] == 0, f"{name}:{job_id} runs {masking[0]} step(s) before it masks"


@pytest.mark.parametrize(("name", "job_id"), _handlers())
def test_the_masking_step_is_handed_the_role_and_account_variables_and_nothing_else(name: str, job_id: str) -> None:
    """Its own env dump is printed before it can mask, so whatever else it carried would print in the clear."""
    env = _steps(name, job_id)[0].get("env", {})
    for key, value in env.items():
        assert BARE_ROLE_OR_ACCOUNT.fullmatch(str(value)), f"{name}:{job_id} hands {key} to the masking step"


@pytest.mark.parametrize("name", sorted(_workflows()))
def test_no_workflow_level_env_carries_a_role_or_account(name: str) -> None:
    """Workflow env reaches every step of every job, the masking step included."""
    assert not _carries(_workflows()[name].get("env", {}))
