#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_cloud_cycle_workflow.py
#  Purpose:      Guard the dispatchable cloud cycle: it refuses before creating
#                anything when its environment is unconfigured, assumes the
#                runner by OIDC for four hours, hands the guard an expiry that
#                can only under-read the credential's, never publishes the
#                credential, masks the account and role before any later step
#                prints them, pins every action and carries no deployment value.
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
    assert set(triggers["workflow_dispatch"]["inputs"]) == {
        "run_length", "cycle_args", "preflight_only", "acceptance_suite", "repeat",
    }


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
    assert inputs["allowed-account-ids"] == "${{ steps.mask.outputs.account }}"
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


def test_the_mode_is_the_dials_so_the_cycle_arguments_carry_no_default(tmp_path: Path) -> None:
    """A second default beside the dial's profile is how a scale cluster gets a single deploy."""
    cycle_args = _triggers()["workflow_dispatch"]["inputs"]["cycle_args"]
    assert cycle_args["default"] == ""
    assert cycle_args["required"] is False
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python3"
    fake.write_text('#!/bin/bash\nprintf \'%s\\n\' "$@" > "${CALLS}"\n', encoding="utf-8")
    fake.chmod(0o755)
    calls = tmp_path / "calls"
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "CALLS": str(calls), "RUNNER_TEMP": str(tmp_path),
           "RUN_LENGTH": "150m", "CYCLE_ARGS": "", "ACCEPTANCE_SUITE": "none"}
    done, _output = _run_script(_named("Run one guarded cycle")["run"], env, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    argv = calls.read_text(encoding="utf-8").splitlines()
    assert argv[argv.index("--"):] == [
        "--", "--acceptance-shots-dir", f"{tmp_path}/acceptance", "--env-file", f"{tmp_path}/dfe-cycle.env",
    ]
    assert "--mode" not in argv


# --- the runner's own address, the one the cluster API opens to -------------------------------------


def _fake_curl(tmp_path: Path, answers: list[tuple[str, int]]) -> dict[str, str]:
    """A curl on PATH that records its arguments and gives each call its own answer and exit."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "curl"
    fake.write_text(
        '#!/bin/bash\nprintf \'%s\\n\' "$*" >> "${CALLS}"\nn=$(( $(wc -l < "${CALLS}") ))\n'
        'answer="ANSWER_${n}"\ncode="EXIT_${n}"\nprintf \'%s\\n\' "${!answer:-}"\nexit "${!code:-0}"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "CALLS": str(tmp_path / "calls")}
    for n, (answer, code) in enumerate(answers, start=1):
        env[f"ANSWER_{n}"] = answer
        env[f"EXIT_{n}"] = str(code)
    return env


def test_two_services_that_agree_give_the_runner_address_as_a_slash_32(tmp_path: Path) -> None:
    env = _fake_curl(tmp_path, [("20.1.2.3", 0), ("20.1.2.3", 0)])
    done, output = _run_script(_step("egress")["run"], env, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert output == "cidr=20.1.2.3/32\n"
    calls = (tmp_path / "calls").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert all("-4" in call.split() for call in calls)
    assert calls[0].split()[-1].split("/")[2] != calls[1].split()[-1].split("/")[2]


@pytest.mark.parametrize(("answers", "says"), [
    ([("20.1.2.3", 0), ("20.9.9.9", 0)], "disagree"),
    ([("", 22)], "did not answer"),
    ([("20.1.2.3", 0), ("", 28)], "did not answer"),
    ([("<html>blocked</html>", 0)], "one IPv4 address"),
    ([("2600:1f14::1", 0)], "one IPv4 address"),
    ([("20.1.2.3 20.1.2.4", 0)], "one IPv4 address"),
])
def test_expected_fail_an_address_the_services_do_not_agree_on_hands_on_nothing(
    tmp_path: Path, answers: list[tuple[str, int]], says: str
) -> None:
    env = _fake_curl(tmp_path, answers)
    done, output = _run_script(_step("egress")["run"], env, tmp_path)
    assert done.returncode == 1
    assert done.stdout.startswith("::error::")
    assert says in done.stdout
    assert output == ""


def test_the_address_is_resolved_before_any_tool_installs_and_reaches_the_guard() -> None:
    """A preflight-only run resolves it too, so it proves the lookup without creating anything."""
    names = [s.get("name") for s in _job()["steps"]]
    egress = _step("egress")
    assert "if" not in egress
    assert names.index(egress["name"]) < names.index("Install OpenTofu")
    for step in ("Run the preflight only", "Run one guarded cycle"):
        # Keyed by the guard's own constant, so renaming either side fails here.
        assert _named(step)["env"][guard_mod.ENDPOINT_CIDR_ENV] == "${{ steps.egress.outputs.cidr }}"


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
    not_read = ("CYCLE_ARGS", "ACCEPTANCE_SUITE", "REDPANDA_CLIENT_ID", "REDPANDA_CLIENT_SECRET")
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


def test_a_configured_environment_passes_the_gate(tmp_path: Path) -> None:
    done, output = _run_script(_step("gate")["run"], CONFIGURED, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert output == ""


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


# --- the mask: first, so no later step prints the account or the role ----------------------------


def test_the_mask_is_the_jobs_first_step_and_nothing_before_it_names_the_role() -> None:
    """A mask covers only output written after the step that registers it."""
    steps = _job()["steps"]
    assert steps[0]["id"] == "mask"
    assert "::add-mask::" in steps[0]["run"]
    assert set(steps[0]["env"]) == {"DFE_CYCLE_AWS_ROLE"}
    for step in steps[1:]:
        assert "::add-mask::" not in step.get("run", ""), step.get("name")


def test_the_role_reaches_the_gate_and_the_credentials_only_after_the_mask() -> None:
    names = [s.get("name") for s in _job()["steps"]]
    mask = names.index(_step("mask")["name"])
    for step_id in ("gate", "creds"):
        assert names.index(_step(step_id)["name"]) > mask


def test_the_mask_registers_the_role_and_its_account_and_hands_the_account_on(tmp_path: Path) -> None:
    done, output = _run_script(_step("mask")["run"], {"DFE_CYCLE_AWS_ROLE": ROLE}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.splitlines() == [f"::add-mask::{ROLE}", "::add-mask::000000000000"]
    assert output == "account=000000000000\n"


@pytest.mark.parametrize("role", ["", None])
def test_an_unset_role_masks_nothing_and_leaves_the_refusal_to_the_gate(tmp_path: Path, role: str | None) -> None:
    env = {} if role is None else {"DFE_CYCLE_AWS_ROLE": role}
    done, output = _run_script(_step("mask")["run"], env, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout == ""
    assert output == ""


@pytest.mark.parametrize("role", ["role-dfe-e2e-runner", "arn:aws:iam::12345:role/x", "arn:aws:iam::000000000000:user/x"])
def test_expected_fail_a_role_that_is_not_a_role_arn_refuses_without_echoing_it(tmp_path: Path, role: str) -> None:
    done, output = _run_script(_step("mask")["run"], {"DFE_CYCLE_AWS_ROLE": role}, tmp_path)
    assert done.returncode == 1
    # The raw value is registered first, so the refusal cannot be the line that prints it.
    assert done.stdout.splitlines()[0] == f"::add-mask::{role}"
    printed = [line for line in done.stdout.splitlines() if not line.startswith("::add-mask::")]
    assert any("not an IAM role ARN" in line for line in printed)
    assert all(role not in line for line in printed)
    assert output == ""


# --- repeat: several cycles, one after another ---------------------------------------------------


def test_repeat_is_one_to_three_and_defaults_to_one() -> None:
    repeat = _triggers()["workflow_dispatch"]["inputs"]["repeat"]
    assert repeat["type"] == "choice"
    assert repeat["options"] == ["1", "2", "3"]
    assert repeat["default"] == "1"


def test_each_leg_runs_alone_and_a_failed_leg_does_not_cancel_the_rest() -> None:
    strategy = _job()["strategy"]
    assert strategy["max-parallel"] == 1
    assert strategy["fail-fast"] is False
    leg = strategy["matrix"]["leg"]
    for count, legs in (("3", "[1, 2, 3]"), ("2", "[1, 2]")):
        assert f"inputs.repeat == '{count}' && '{legs}'" in leg
    assert leg.rstrip(" }").endswith("|| '[1]')")


def test_no_job_level_group_can_cancel_a_waiting_leg() -> None:
    """A concurrency group holds one pending job, so leg 3 joining a job-level group
    cancels leg 2 while it waits. The run-level group never sees the legs."""
    assert "concurrency" not in _job()


def test_each_leg_assumes_the_role_under_its_own_session_name() -> None:
    assert _step("creds")["with"]["role-session-name"].endswith("-${{ matrix.leg }}")


# --- the acceptance stage -----------------------------------------------------------------------


def test_the_acceptance_suite_defaults_to_source_and_none_turns_it_off() -> None:
    suite = _triggers()["workflow_dispatch"]["inputs"]["acceptance_suite"]
    assert suite["type"] == "choice"
    assert suite["default"] == "source"
    assert "none" in suite["options"]
    # Every other option is a suite dfe-ops cycle's own parser accepts.
    for option in (o for o in suite["options"] if o != "none"):
        assert guard_mod._parse_cycle(["--acceptance-suite", option]).acceptance_suite == option


def _cycle_argv(tmp_path: Path, cycle_args: str, suite: str) -> list[str]:
    """The arguments the cycle step hands dfe-ops cloud-cycle after its `--`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python3"
    fake.write_text('#!/bin/bash\nprintf \'%s\\n\' "$@" > "${CALLS}"\n', encoding="utf-8")
    fake.chmod(0o755)
    calls = tmp_path / "calls"
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "CALLS": str(calls), "RUNNER_TEMP": str(tmp_path),
           "RUN_LENGTH": "150m", "CYCLE_ARGS": cycle_args, "ACCEPTANCE_SUITE": suite}
    done, _output = _run_script(_named("Run one guarded cycle")["run"], env, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    argv = calls.read_text(encoding="utf-8").splitlines()
    return argv[argv.index("--") + 1:]


def test_the_cycle_runs_the_source_suite_when_nothing_else_is_named(tmp_path: Path) -> None:
    argv = _cycle_argv(tmp_path, "", "source")
    parsed = guard_mod._parse_cycle(argv)
    assert parsed.acceptance_suite == "source"
    assert parsed.acceptance_shots_dir == f"{tmp_path}/acceptance"


def test_none_runs_no_acceptance_stage(tmp_path: Path) -> None:
    assert guard_mod._parse_cycle(_cycle_argv(tmp_path, "", "none")).acceptance_suite is None


def test_the_jobs_shots_dir_and_env_file_win_over_the_cycle_arguments(tmp_path: Path) -> None:
    """The input's description says so; the upload step reads only the job's directory."""
    argv = _cycle_argv(tmp_path, "--acceptance-shots-dir /elsewhere --env-file other.env", "source")
    parsed = guard_mod._parse_cycle(argv)
    assert parsed.acceptance_shots_dir == f"{tmp_path}/acceptance"
    assert parsed.env_file[-1] == f"{tmp_path}/dfe-cycle.env"
    description = _triggers()["workflow_dispatch"]["inputs"]["cycle_args"]["description"]
    assert "then --acceptance-shots-dir and --env-file after these, so the job's own" in description


@pytest.mark.parametrize("named", ["--acceptance-suite flows", "--acceptance-suite=flows", "--acceptance-su flows"])
def test_a_suite_the_cycle_arguments_name_wins_over_the_input(tmp_path: Path, named: str) -> None:
    argv = _cycle_argv(tmp_path, f"--e2e {named}", "source")
    assert guard_mod._parse_cycle(argv).acceptance_suite == "flows"
    assert "source" not in argv


INSTALL_STEP = "Install the acceptance suite's Python dependencies, hashes required"


def test_the_suite_is_set_up_before_the_credential_clock_starts() -> None:
    """The checkouts and the browser install take minutes the run's credential does not have to cover."""
    names = [s.get("name") for s in _job()["steps"]]
    session = names.index(_step("session")["name"])
    for name in ("Check out the engine and the transform the stack pins", INSTALL_STEP):
        assert names.index(name) < session, name
        assert _named(name)["if"] == "${{ !inputs.preflight_only }}"


def test_no_step_installs_a_browser() -> None:
    """Under CI=true `playwright install chrome` runs as root, removes the image's Chrome and
    installs a .deb it downloads with no signature or hash check."""
    assert "playwright install" not in WORKFLOW.read_text(encoding="utf-8")


def test_the_suites_drive_the_runner_images_chrome_and_refuse_without_it() -> None:
    """Playwright's chrome channel launches /opt/google/chrome/chrome on Linux, where the image installs it."""
    script = _named(INSTALL_STEP)["run"]
    assert "if ! /opt/google/chrome/chrome --version; then" in script
    assert "Nothing was created." in script
    from acceptance.onboarding import run as onboarding_run
    from acceptance.source import run as source_run

    urls = ["--ui-url", "https://dfe.example", "--engine-url", "https://dfe.example"]
    assert source_run.build_parser().parse_args([*urls, "--engine-repo", "/x"]).channel == "chrome"
    assert onboarding_run.build_parser().parse_args(urls).channel == "chrome"
    # dfe-ops hands neither runner a channel of its own.
    assert "--channel" not in (REPO_ROOT / "scripts" / "dfe-ops").read_text(encoding="utf-8")


def test_the_python_dependencies_install_with_hashes() -> None:
    script = _named(INSTALL_STEP)["run"]
    assert "uv pip install --require-hashes -r scripts/acceptance/requirements.txt" in script
    requirements = (REPO_ROOT / "scripts" / "acceptance" / "requirements.txt").read_text(encoding="utf-8")
    pins = [line for line in requirements.splitlines() if line and not line.startswith(("#", " "))]
    assert pins
    for pin in pins:
        assert re.match(r"^[A-Za-z0-9._-]+==[^ ]+ \\$", pin), pin
    for wanted in ("playwright==", "httpx=="):
        assert any(pin.startswith(wanted) for pin in pins), wanted


def _versions_pin(name: str) -> str:
    """The current stack's tag for one app, read straight off versions.yaml."""
    root = yaml.safe_load((REPO_ROOT / "versions.yaml").read_text(encoding="utf-8"))
    return root["stacks"][root["current"]]["apps"][name]


def _run_checkout(tmp_path: Path, python3: str = "") -> tuple[subprocess.CompletedProcess, Path, Path]:
    """The checkout step against this repo's real dfe-stack, with git recording and doing nothing."""
    workspace = tmp_path / "work" / "dfe-infra"
    workspace.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text('#!/bin/bash\nprintf \'%s\\n\' "$*" >> "${CALLS}"\n', encoding="utf-8")
    git.chmod(0o755)
    if python3:
        fake = bin_dir / "python3"
        fake.write_text(python3, encoding="utf-8")
        fake.chmod(0o755)
    github_env = tmp_path / "github_env"
    github_env.touch()
    calls = tmp_path / "calls"
    done = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _named("Check out the engine and the transform the stack pins")["run"]],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "CALLS": str(calls), "RUNNER_TEMP": str(tmp_path),
             "GITHUB_WORKSPACE": str(workspace), "GITHUB_ENV": str(github_env)},
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return done, calls, github_env


def test_the_engine_and_the_transform_are_cloned_at_the_current_stacks_tags(tmp_path: Path) -> None:
    done, calls, github_env = _run_checkout(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    clones = calls.read_text(encoding="utf-8").splitlines()
    parent = tmp_path / "work"
    for repo, line in zip(("dfe-engine", "dfe-transform-vrl"), clones, strict=True):
        words = line.split()
        assert words[words.index("--branch") + 1] == _versions_pin(repo), line
        assert f"https://github.com/hyperi-io/{repo}.git" in words
        assert words[-1] == f"{parent}/{repo}"
    assert github_env.read_text(encoding="utf-8").splitlines() == [
        f"DFE_ENGINE_REPO={parent}/dfe-engine",
        f"DFE_TRANSFORM_VRL_REPO={parent}/dfe-transform-vrl",
    ]


def test_expected_fail_a_pin_that_is_not_a_release_tag_clones_nothing(tmp_path: Path) -> None:
    passthrough = '#!/bin/bash\nif [[ "$1" == "-c" ]]; then echo latest; exit 0; fi\nexec /usr/bin/python3 "$@"\n'
    done, calls, github_env = _run_checkout(tmp_path, python3=passthrough)
    assert done.returncode == 1
    assert "not a release tag" in done.stdout
    assert not calls.exists()
    assert github_env.read_text(encoding="utf-8") == ""


def test_the_screenshots_are_kept_only_from_a_failed_leg() -> None:
    """The repository is public, so a green run's screenshots would be published for nothing."""
    upload = _named("Keep the acceptance screenshots and step table")
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert "failure()" in upload["if"]
    assert "always()" not in upload["if"]
    assert upload["with"]["retention-days"] == 7
    assert upload["with"]["path"] == "${{ runner.temp }}/acceptance"
    # One artifact name per leg: a second upload under the same name fails the leg.
    assert "${{ matrix.leg }}" in upload["with"]["name"]
    # The cycle step writes where this uploads from.
    assert '--acceptance-shots-dir "${RUNNER_TEMP}/acceptance"' in _named("Run one guarded cycle")["run"]
    names = [s.get("name") for s in _job()["steps"]]
    assert names.index("Run one guarded cycle") < names.index(upload["name"])
