#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_cloud_reaper.py
#  Purpose:      Guard the scheduled reaper: unconfigured it is a notice and a
#                green run that touches nothing, configured it destroys only
#                expired runs whose record points at their own state, and the
#                workflow pins every action, holds no secret and gates the
#                reaping job on the role variable.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/cloud_reaper.py and .github/workflows/cloud-reaper.yml.

    python3 -m pytest scripts/tests/test_cloud_reaper.py -q

The S3 calls go to a fake `aws_cli.run_aws` and `tofu` to a fake `run_tofu`;
the one real process is bash running the workflow's own gate script.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import cloud_reaper  # noqa: E402
import cloud_run  # noqa: E402
import cloud_sweep  # noqa: E402

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "cloud-reaper.yml"
ROLE = "arn:aws:iam::000000000000:role/dfe-e2e/reaper"
NOW = 1791460800
ENV = {"DFE_REAPER_AWS_ROLE": ROLE, "DFE_REAPER_AWS_REGIONS": "us-west-2", "DFE_REAPER_STATE_BUCKET": "example-state"}


def _no_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an unconfigured reaper must make no AWS call")

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", boom)


# --- configuration -------------------------------------------------------------------


def test_no_role_is_no_configuration() -> None:
    assert cloud_reaper.load_config({}) is None
    assert cloud_reaper.load_config({"DFE_REAPER_AWS_ROLE": "  "}) is None


def test_the_account_defaults_to_the_role_arns_own() -> None:
    config = cloud_reaper.load_config(ENV)
    assert config is not None
    assert config.account == "000000000000"
    assert config.state_region == "us-west-2"
    assert config.state_prefix == cloud_run.DEFAULT_STATE_PREFIX


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"DFE_REAPER_AWS_ROLE": ROLE}, "REGIONS"),
        ({**ENV, "DFE_REAPER_AWS_ROLE": "reaper"}, "role ARN"),
        ({**ENV, "DFE_REAPER_AWS_ROLE": "arn:aws:iam::000000000000:user/reaper"}, "role ARN"),
        ({**ENV, "DFE_REAPER_AWS_ACCOUNT": "12345"}, "12-digit"),
        ({**ENV, "DFE_RUN_STATE_PREFIX": "/abs"}, "prefix"),
    ],
)
def test_expected_fail_a_half_configured_reaper_is_refused(env: dict, match: str) -> None:
    with pytest.raises(cloud_reaper.ReaperConfigError, match=match):
        cloud_reaper.load_config(env)


# --- the no-op path ----------------------------------------------------------------------


def test_reap_unconfigured_is_a_notice_and_exit_zero_with_no_aws_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _no_aws(monkeypatch)
    assert cloud_reaper.cmd_reap({}) == 0
    assert capsys.readouterr().out.startswith("::notice::")


def test_plan_unconfigured_publishes_enabled_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _no_aws(monkeypatch)
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert cloud_reaper.cmd_plan({}) == 0
    assert output.read_text(encoding="utf-8") == "enabled=false\n"
    assert "::notice::" in capsys.readouterr().out


def test_plan_configured_publishes_the_account_and_first_region(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert cloud_reaper.cmd_plan({**ENV, "DFE_REAPER_AWS_REGIONS": "us-west-2, eu-west-1"}) == 0
    assert output.read_text(encoding="utf-8").splitlines() == ["enabled=true", "account=000000000000", "region=us-west-2"]


def test_plan_misconfigured_fails_loudly(capsys: pytest.CaptureFixture) -> None:
    assert cloud_reaper.cmd_plan({"DFE_REAPER_AWS_ROLE": ROLE}) == 2
    assert "::error::" in capsys.readouterr().out


# --- per-run destroys -----------------------------------------------------------------------


def _record(run_id: str, expires_at: int, **state_overrides: str) -> dict:
    state = {"bucket": "example-state", "region": "us-west-2", "key": cloud_run.state_key("dfe-e2e-runs", run_id),
             **state_overrides}
    return cloud_run.build_record(
        run_id=run_id,
        expires_at=expires_at,
        keys=cloud_run.RunTagKeys(),
        tf_root="terraform/environments/aws",
        state=state,
        tfvars={"state": state, "run": cloud_run.run_tfvar(run_id, expires_at), "tags": {"lifecycle": "ephemeral"}},
    )


class FakeBucket:
    """The state bucket, answering the s3api calls the reaper makes."""

    def __init__(self, records: dict[str, dict], states: set[str]) -> None:
        self.records = {cloud_run.record_key("dfe-e2e-runs", run_id): body for run_id, body in records.items()}
        self.states = states
        self.deleted: list[str] = []

    def run_aws(self, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        verb = args[1]
        key = args[args.index("--key") + 1] if "--key" in args else ""
        if verb == "list-objects-v2":
            contents = [{"Key": k} for k in self.records] + [{"Key": s} for s in self.states]
            return _ok({"Contents": contents})
        if verb == "get-object":
            Path(args[args.index("--key") + 2]).write_text(json.dumps(self.records[key]), encoding="utf-8")
            return _ok({})
        if verb == "head-object":
            return _ok({}) if key in self.states else _fail("An error occurred (404) when calling HeadObject: Not Found")
        if verb == "delete-object":
            self.deleted.append(key)
            return _ok({})
        raise AssertionError(f"unexpected aws call {args}")


def _ok(body: object) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["aws"], 0, stdout=json.dumps(body), stderr="")


def _fail(stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["aws"], 1, stdout="", stderr=stderr)


def _reap(monkeypatch: pytest.MonkeyPatch, bucket: FakeBucket, tofu_code: int = 0) -> tuple[list[str], list[tuple]]:
    destroyed: list[tuple] = []

    def fake_tofu(args: list[str], cwd: Path, env: dict) -> int:
        destroyed.append((tuple(args), cwd, json.loads((cwd / cloud_reaper.OVERLAY_NAME).read_text())))
        return tofu_code

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", bucket.run_aws)
    monkeypatch.setattr(cloud_reaper, "run_tofu", fake_tofu)
    config = cloud_reaper.load_config(ENV)
    assert config is not None
    return cloud_reaper.reap_runs(config, NOW), destroyed


def test_an_expired_run_is_destroyed_against_its_own_state_and_its_record_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    state = cloud_run.state_key("dfe-e2e-runs", "run-old")
    bucket = FakeBucket({"run-old": _record("run-old", NOW - 60)}, {state})
    problems, destroyed = _reap(monkeypatch, bucket)
    assert problems == []
    assert [d[0][0] for d in destroyed] == ["init", "destroy"]
    assert destroyed[0][1].parts[-3:] == ("terraform", "environments", "aws")
    assert destroyed[0][2]["state"]["key"] == state
    assert bucket.deleted == ["dfe-e2e-runs/run-old/run.json"]


def test_expected_fail_an_unexpired_run_is_never_destroyed(monkeypatch: pytest.MonkeyPatch) -> None:
    state = cloud_run.state_key("dfe-e2e-runs", "run-live")
    bucket = FakeBucket({"run-live": _record("run-live", NOW + 60)}, {state})
    problems, destroyed = _reap(monkeypatch, bucket)
    assert problems == []
    assert destroyed == []
    assert bucket.deleted == []


def test_expected_fail_a_record_pointing_at_another_state_is_never_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = _record("run-bad", NOW - 60, key="production/terraform.tfstate")
    bucket = FakeBucket({"run-bad": bad}, {"production/terraform.tfstate"})
    problems, destroyed = _reap(monkeypatch, bucket)
    assert destroyed == []
    assert bucket.deleted == []
    assert any("refused" in p for p in problems)


def test_a_failed_destroy_keeps_the_record_for_the_next_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    state = cloud_run.state_key("dfe-e2e-runs", "run-old")
    bucket = FakeBucket({"run-old": _record("run-old", NOW - 60)}, {state})
    problems, _destroyed = _reap(monkeypatch, bucket, tofu_code=1)
    assert bucket.deleted == []
    assert any("tofu destroy failed" in p for p in problems)


def test_an_expired_run_with_no_state_only_loses_its_record(monkeypatch: pytest.MonkeyPatch) -> None:
    bucket = FakeBucket({"run-empty": _record("run-empty", NOW - 60)}, set())
    problems, destroyed = _reap(monkeypatch, bucket)
    assert problems == []
    assert destroyed == []
    assert bucket.deleted == ["dfe-e2e-runs/run-empty/run.json"]


def test_the_sweep_runs_expired_only_with_no_grace_and_spares_the_state_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(cloud_sweep, "main", lambda argv: seen.append(argv) or 0)
    config = cloud_reaper.load_config(ENV)
    assert config is not None
    assert cloud_reaper.sweep_region(config, "us-west-2") == 0
    argv = seen[0]
    assert argv[argv.index("--grace") + 1] == "0"
    assert {"--expired", "--delete", "--yes"} <= set(argv)
    assert argv[argv.index("--account") + 1] == "000000000000"
    assert argv[argv.index("--exclude-bucket") + 1] == "example-state"
    assert "--include-untagged" not in argv


def test_reap_exits_one_while_anything_expired_remains(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(cloud_reaper, "reap_runs", lambda config, now: [])
    monkeypatch.setattr(cloud_reaper, "sweep_region", lambda config, region: 1)
    assert cloud_reaper.cmd_reap(ENV) == 1
    assert "::error::us-west-2" in capsys.readouterr().out


# --- the workflow ----------------------------------------------------------------------------


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_runs_every_fifteen_minutes_and_on_dispatch() -> None:
    doc = _workflow()
    triggers = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` as true
    assert triggers["schedule"] == [{"cron": "*/15 * * * *"}]
    assert "workflow_dispatch" in triggers


def test_every_action_is_pinned_by_full_commit_sha() -> None:
    uses = re.findall(r"uses:\s*(\S+)", WORKFLOW.read_text(encoding="utf-8"))
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref


def test_the_workflow_holds_no_secret() -> None:
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")


def test_the_reaping_job_is_gated_on_the_role_and_bound_to_its_environment() -> None:
    jobs = _workflow()["jobs"]
    reap = jobs["reap"]
    assert reap["needs"] == "gate"
    assert reap["if"] == "needs.gate.outputs.enabled == 'true'"
    assert reap["environment"] == "e2e-reaper"
    assert reap["permissions"]["id-token"] == "write"
    assert "id-token" not in _workflow().get("permissions", {})


def test_the_gate_reads_the_role_from_the_reapers_environment() -> None:
    """A variable scoped to an environment reads as unset in any job outside it, so
    a gate outside it reports an unconfigured reaper with the role in place."""
    jobs = _workflow()["jobs"]
    assert jobs["gate"]["environment"] == jobs["reap"]["environment"]
    assert "id-token" not in jobs["gate"].get("permissions", {})


def _gate_script() -> tuple[str, dict]:
    step = _workflow()["jobs"]["gate"]["steps"][0]
    return step["run"], step["env"]


def test_the_gate_is_handed_a_boolean_so_the_role_never_reaches_its_env_dump() -> None:
    _script, step_env = _gate_script()
    assert step_env == {"HAS_ROLE": "${{ vars.DFE_REAPER_AWS_ROLE != '' }}"}


@pytest.mark.parametrize("has_role", ["false", "", None])
def test_the_gate_with_no_role_is_a_notice_and_a_green_run(tmp_path: Path, has_role: str | None) -> None:
    script, _env = _gate_script()
    output = tmp_path / "github_output"
    env = {"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(output)}
    if has_role is not None:
        env["HAS_ROLE"] = has_role
    done = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("::notice::")
    assert output.read_text(encoding="utf-8") == "enabled=false\n"


def test_the_gate_with_a_role_enables_the_reaping_job(tmp_path: Path) -> None:
    script, _env = _gate_script()
    output = tmp_path / "github_output"
    env = {"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(output), "HAS_ROLE": "true"}
    done = subprocess.run(["bash", "-euo", "pipefail", "-c", script], env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)
    assert done.returncode == 0, done.stderr
    assert output.read_text(encoding="utf-8") == "enabled=true\n"


# --- the mask: first, so no later step prints the account or the role ----------------------------


def _mask_run(env: dict[str, str]) -> subprocess.CompletedProcess:
    script = _workflow()["jobs"]["reap"]["steps"][0]["run"]
    return subprocess.run(["bash", "-euo", "pipefail", "-c", script], env={"PATH": "/usr/bin:/bin", **env},
                          capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def test_the_reaping_jobs_first_step_masks_and_nothing_runs_before_it() -> None:
    """A mask covers only output written after the step that registers it."""
    steps = _workflow()["jobs"]["reap"]["steps"]
    assert steps[0]["name"] == "Mask the AWS account and the role"
    assert "::add-mask::" in steps[0]["run"]
    assert "uses" not in steps[0]
    for step in steps[1:]:
        assert "::add-mask::" not in step.get("run", ""), step.get("name")


def test_the_mask_registers_the_role_and_the_account_it_names() -> None:
    done = _mask_run({"DFE_REAPER_AWS_ROLE": ROLE})
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.splitlines() == [f"::add-mask::{ROLE}", "::add-mask::000000000000"]


def test_the_mask_registers_an_account_variable_that_differs_from_the_roles() -> None:
    done = _mask_run({"DFE_REAPER_AWS_ROLE": ROLE, "DFE_REAPER_AWS_ACCOUNT": "111111111111"})
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.splitlines() == [f"::add-mask::{ROLE}", "::add-mask::111111111111", "::add-mask::000000000000"]


def test_the_mask_with_nothing_set_registers_nothing() -> None:
    done = _mask_run({})
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout == ""


def test_the_reaper_and_ci_validate_on_the_same_opentofu() -> None:
    validate = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "tf-validate.yml").read_text(encoding="utf-8"))
    assert _workflow()["env"]["TOFU_VERSION"] == validate["env"]["TOFU_VERSION"]
