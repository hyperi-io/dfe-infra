#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_cloud_cycle_workflow.py
#  Purpose:      Guard the dispatchable cloud cycle: it refuses before creating
#                anything when its environment is unconfigured, assumes the
#                runner by OIDC for four hours, hands the guard an expiry that
#                can only under-read the credential's, never publishes the
#                credential, pins every action and carries no deployment value.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for .github/workflows/cloud-cycle.yml.

    python3 -m pytest scripts/tests/test_cloud_cycle_workflow.py -q

The workflow's own gate and expiry scripts run under bash with a scratch
GITHUB_OUTPUT. Nothing reaches AWS or GitHub.
"""

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import cloud_run  # noqa: E402
import dfe_ops_cloud_guard as guard_mod  # noqa: E402

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "cloud-cycle.yml"
ROLE = "arn:aws:iam::000000000000:role/dfe-e2e/role-dfe-e2e-runner"
CONFIGURED = {
    "DFE_CYCLE_AWS_ROLE": ROLE,
    "DFE_CYCLE_AWS_REGION": "us-west-2",
    "DFE_CYCLE_DIAL": "substrate: k8s\n",
    "DFE_GUARD_ROLE": "role-e2e-guardrail",
    "DFE_GUARD_SWEEPER": "e2e-sweeper",
    "DFE_GUARD_BUDGET_ACTION": "e2e-budget:00000000-0000-0000-0000-000000000000",
    "HAS_ENV_FILE": "true",
}


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers() -> dict:
    doc = _workflow()
    return doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` as true


def _job() -> dict:
    return _workflow()["jobs"]["cycle"]


def _step(step_id: str) -> dict:
    return next(s for s in _job()["steps"] if s.get("id") == step_id)


def _named(name: str) -> dict:
    return next(s for s in _job()["steps"] if s.get("name") == name)


def _session_seconds() -> int:
    return int(_workflow()["env"]["RUNNER_SESSION_SECONDS"])


def _run_script(script: str, env: dict[str, str], tmp_path: Path) -> tuple[subprocess.CompletedProcess, str]:
    """Run one of the workflow's `run:` scripts the way a runner does, stricter."""
    output = tmp_path / "github_output"
    output.touch()
    done = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(output), **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return done, output.read_text(encoding="utf-8")


# --- triggers, pins and what the file may carry ---------------------------------------


def test_the_cycle_only_ever_runs_on_dispatch() -> None:
    triggers = _triggers()
    assert set(triggers) == {"workflow_dispatch"}
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"run_length", "cycle_args", "preflight_only"}


def test_every_action_is_pinned_by_full_commit_sha_with_its_version() -> None:
    lines = [ln for ln in WORKFLOW.read_text(encoding="utf-8").splitlines() if re.search(r"\buses:", ln)]
    assert lines
    for line in lines:
        assert re.search(r"uses:\s*[\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$", line), line


def test_the_file_carries_no_account_or_role() -> None:
    """dfe-infra is public: the account, role and dial come from the environment."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert not re.search(r"\b\d{12}\b", text)
    assert not re.search(r"arn:aws[a-z-]*:iam::\d", text)
    assert "role-dfe-e2e-runner" not in text


def test_no_script_interpolates_an_expression() -> None:
    """Inputs, variables and secrets reach a script through its env only, never
    spliced into the shell text where a crafted input would run."""
    for step in _job()["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")


def test_the_cycle_uses_the_opentofu_and_helm_ci_checks_with() -> None:
    validate = yaml.safe_load((WORKFLOWS / "tf-validate.yml").read_text(encoding="utf-8"))
    lint = yaml.safe_load((WORKFLOWS / "helm-lint.yml").read_text(encoding="utf-8"))
    assert _workflow()["env"]["TOFU_VERSION"] == validate["env"]["TOFU_VERSION"]
    assert _workflow()["env"]["HELM_VERSION"] == lint["env"]["HELM_VERSION"]


# --- the job: its environment, identity and time ----------------------------------------


def test_the_job_is_bound_to_its_environment_and_runs_one_at_a_time() -> None:
    doc, job = _workflow(), _job()
    assert job["environment"] == "e2e-runner"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert doc["permissions"] == {"contents": "read"}
    assert doc["concurrency"] == {"group": "cloud-cycle", "cancel-in-progress": False}


def test_the_runner_is_assumed_by_oidc_for_four_hours_from_variables() -> None:
    creds = _step("creds")
    assert creds["uses"].startswith("aws-actions/configure-aws-credentials@")
    inputs = creds["with"]
    assert inputs["role-to-assume"] == "${{ vars.DFE_CYCLE_AWS_ROLE }}"
    assert inputs["aws-region"] == "${{ vars.DFE_CYCLE_AWS_REGION }}"
    assert inputs["role-duration-seconds"] == "${{ env.RUNNER_SESSION_SECONDS }}"
    assert _session_seconds() == 14400
    assert inputs["allowed-account-ids"] == "${{ steps.gate.outputs.account }}"
    assert inputs["mask-aws-account-id"] is True
    assert "aws-access-key-id" not in inputs


def test_the_job_outlasts_the_credential_it_is_given() -> None:
    assert _job()["timeout-minutes"] * 60 > _session_seconds()


def test_the_default_run_length_and_margin_fit_inside_the_credential() -> None:
    """The guard reads N, Ns, Nm, Nh or Nd only, so a default like 2h30m refuses every run."""
    default = _triggers()["workflow_dispatch"]["inputs"]["run_length"]["default"]
    window = cloud_run.parse_duration(default) + cloud_run.parse_duration(guard_mod.DEFAULT_TEARDOWN_MARGIN)
    assert window < _session_seconds()


def test_the_cycle_runs_the_guard_against_the_aws_root_with_the_env_file() -> None:
    run = _named("Run one guarded cycle")
    assert "scripts/dfe-ops cloud-cycle --tf-dir terraform/environments/aws --run-length" in run["run"]
    assert '--env-file "${RUNNER_TEMP}/dfe-cycle.env"' in run["run"]
    assert run["env"]["RUN_LENGTH"] == "${{ inputs.run_length }}"
    assert run["env"]["CYCLE_ARGS"] == "${{ inputs.cycle_args }}"
    for name in ("DFE_GUARD_ROLE", "DFE_GUARD_SWEEPER", "DFE_GUARD_BUDGET_ACTION", "DFE_GUARD_PERMISSIONS_BOUNDARY",
                 "DFE_GUARD_IAM_PATH", "DFE_GUARD_S3_BUCKET_PREFIX"):
        assert run["env"][name] == f"${{{{ vars.{name} }}}}"


# --- preflight only ----------------------------------------------------------------------------


def test_preflight_only_is_a_boolean_that_defaults_to_the_full_cycle() -> None:
    preflight_only = _triggers()["workflow_dispatch"]["inputs"]["preflight_only"]
    assert preflight_only["type"] == "boolean"
    assert preflight_only["default"] is False


def test_preflight_only_runs_the_read_only_preflight_as_the_runner_and_never_the_cycle() -> None:
    preflight, cycle = _named("Run the preflight only"), _named("Run one guarded cycle")
    assert preflight["if"] == "${{ inputs.preflight_only }}"
    assert cycle["if"] == "${{ !inputs.preflight_only }}"
    names = [s.get("name") for s in _job()["steps"]]
    assert names.index(_step("creds")["name"]) < names.index("Run the preflight only")
    assert "scripts/dfe-ops cloud-preflight --tf-dir terraform/environments/aws --run-length" in preflight["run"]
    for word in ("cloud-cycle", "tofu", "--env-file"):
        assert word not in preflight["run"], word


def test_preflight_only_hands_the_guard_everything_the_cycles_own_preflight_reads() -> None:
    """Only the cycle arguments and the redpanda credential, which preflight never reads, are left out."""
    preflight, cycle = _named("Run the preflight only"), _named("Run one guarded cycle")
    not_read = ("CYCLE_ARGS", "REDPANDA_CLIENT_ID", "REDPANDA_CLIENT_SECRET")
    assert preflight["env"] == {k: v for k, v in cycle["env"].items() if k not in not_read}


@pytest.mark.parametrize(("preflight_only", "sized"), [("true", False), ("false", True)])
def test_only_a_cycle_resolves_sizing_and_both_render_the_dial(
    tmp_path: Path, preflight_only: str, sized: bool
) -> None:
    """Preflight reads the dial's provision, state and tags; sizing feeds the apply alone."""
    render = _named("Render the dial, its sizing and the bootstrap env file")
    assert render["env"]["PREFLIGHT_ONLY"] == "${{ inputs.preflight_only }}"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python3"
    fake.write_text(
        '#!/bin/bash\nprintf \'%s\\n\' "$*" >> "${CALLS}"\nif [[ "$1" == "-c" ]]; then echo us-west-2; fi\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    calls = tmp_path / "calls"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CALLS": str(calls),
        "RUNNER_TEMP": str(tmp_path),
        "DFE_CYCLE_DIAL": "substrate: k8s",
        "DFE_CYCLE_ENV_FILE": "",
        "DFE_CYCLE_AWS_REGION": "us-west-2",
        "PREFLIGHT_ONLY": preflight_only,
    }
    done, _output = _run_script(render["run"], env, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    log = calls.read_text(encoding="utf-8")
    assert "scripts/render_dial.py" in log
    assert ("scripts/resolve_sizing.py" in log) is sized


# --- the credential's expiry -----------------------------------------------------------------


def test_no_step_publishes_or_reads_the_credential() -> None:
    """output-credentials publishes the access key, secret and session token as step
    outputs, masked or not, and is the only way the action publishes an expiry."""
    assert "output-credentials" not in _step("creds")["with"]
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "output-credentials" not in text
    for name in ("aws-access-key-id", "aws-secret-access-key", "aws-session-token", "aws-expiration"):
        assert f"steps.creds.outputs.{name}" not in text, name


def test_the_expiry_is_fixed_before_the_role_is_assumed_and_handed_to_the_guard() -> None:
    ids = [s.get("id") for s in _job()["steps"]]
    assert ids.index("session") < ids.index("creds")
    run = _named("Run one guarded cycle")
    assert run["env"]["AWS_CREDENTIAL_EXPIRATION"] == "${{ steps.session.outputs.expires_at }}"


def test_the_computed_expiry_parses_and_is_never_later_than_the_real_one(tmp_path: Path) -> None:
    """The session starts when the role is assumed, after this step ends, so the
    real expiry is at least the computed one: the guard can only under-read it."""
    session = _session_seconds()
    before = int(time.time())
    done, output = _run_script(_step("session")["run"], {"RUNNER_SESSION_SECONDS": str(session)}, tmp_path)
    assumed_no_earlier_than = time.time()
    assert done.returncode == 0, done.stdout + done.stderr
    key, _, value = output.strip().partition("=")
    assert key == "expires_at"
    assert value.endswith("Z")
    expires, source = guard_mod.AwsGuard("us-west-2").credential_expiry({"AWS_CREDENTIAL_EXPIRATION": value})
    assert source == "AWS_CREDENTIAL_EXPIRATION"
    assert expires == guard_mod._parse_instant(value)
    assert before + session <= expires <= assumed_no_earlier_than + session


@pytest.mark.parametrize("seconds", ["", "4h", "14400.5", "-1", "foo"])
def test_expected_fail_a_session_length_that_is_not_whole_seconds_hands_on_nothing(
    tmp_path: Path, seconds: str
) -> None:
    done, output = _run_script(_step("session")["run"], {"RUNNER_SESSION_SECONDS": seconds}, tmp_path)
    assert done.returncode == 1
    assert done.stdout.startswith("::error::")
    assert output == ""


# --- the gate: refuse before anything is created ---------------------------------------------


def test_a_configured_environment_passes_the_gate_and_masks_the_account(tmp_path: Path) -> None:
    done, output = _run_script(_step("gate")["run"], CONFIGURED, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "::add-mask::000000000000" in done.stdout
    assert output == "account=000000000000\n"


def test_expected_fail_an_unconfigured_environment_refuses_and_names_every_gap(tmp_path: Path) -> None:
    done, output = _run_script(_step("gate")["run"], {}, tmp_path)
    assert done.returncode == 1
    assert done.stdout.startswith("::error::cloud cycle not configured")
    for name in (*(k for k in CONFIGURED if k != "HAS_ENV_FILE"), "DFE_CYCLE_ENV_FILE (secret)"):
        assert name in done.stdout, name
    assert output == ""


@pytest.mark.parametrize("missing", [k for k in CONFIGURED if k != "HAS_ENV_FILE"])
def test_expected_fail_any_one_missing_variable_refuses(tmp_path: Path, missing: str) -> None:
    done, _output = _run_script(_step("gate")["run"], {**CONFIGURED, missing: ""}, tmp_path)
    assert done.returncode == 1
    assert missing in done.stdout


def test_expected_fail_a_missing_env_file_secret_refuses(tmp_path: Path) -> None:
    done, _output = _run_script(_step("gate")["run"], {**CONFIGURED, "HAS_ENV_FILE": "false"}, tmp_path)
    assert done.returncode == 1
    assert "DFE_CYCLE_ENV_FILE" in done.stdout


@pytest.mark.parametrize("role", ["role-dfe-e2e-runner", "arn:aws:iam::12345:role/x", "arn:aws:iam::000000000000:user/x"])
def test_expected_fail_a_role_that_is_not_a_role_arn_refuses_without_echoing_it(tmp_path: Path, role: str) -> None:
    done, output = _run_script(_step("gate")["run"], {**CONFIGURED, "DFE_CYCLE_AWS_ROLE": role}, tmp_path)
    assert done.returncode == 1
    assert "not an IAM role ARN" in done.stdout
    assert role not in done.stdout
    assert output == ""
