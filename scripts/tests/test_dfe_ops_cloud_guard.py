#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_cloud_guard.py
#  Purpose:      Guard `dfe-ops cloud-preflight` and `cloud-cycle`: each of the
#                three refusals fires on its own cause and nothing is created
#                after one, the run gets its own state key and tags, and every
#                way out of a run -- success, failure, exception, SIGTERM --
#                tears it down, keeping the run record when the destroy fails.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe_ops_cloud_guard.py.

    python3 -m pytest scripts/tests/test_dfe_ops_cloud_guard.py -q

The cloud is a FakeGuard and every child process a FakeRunner, so no AWS call
is made and no tofu runs. The one real process is a short python sleep the
Runner is asked to stop.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import cloud_run  # noqa: E402
import cloud_sweep  # noqa: E402
import dfe_ops_cloud_guard as guard_mod  # noqa: E402

NOW = 1791460800.0
ACCOUNT = "000000000000"
BUDGET_ACTION = "e2e-budget:00000000-0000-0000-0000-000000000000"
TFVARS = {
    "provision": {"cloud": "aws", "account": ACCOUNT, "region": "us-west-2", "cidr": "10.90.0.0/16"},
    "state": {"bucket": "example-state", "key": "deployments/example.tfstate", "region": "us-west-2"},
    "tags": {"lifecycle": "ephemeral", "service-name": "dfe"},
    "name": "dfe-example",
}


class FakeGuard:
    """Answers every preflight read from attributes a test sets."""

    def __init__(self) -> None:
        self.account = ACCOUNT
        self.missing: set[str] = set()
        self.sweeper_state = "Active"
        self.expiry: float | None = NOW + 10 * 3600
        self.expiry_error: str | None = None
        self.leftovers: list[cloud_sweep.Resource] = []
        self.records: dict[str, str] = {}
        self.deleted: list[str] = []
        self.kubeconfig_error: Exception | None = None

    def _maybe_missing(self, what: str, value: str) -> str:
        if what in self.missing:
            raise cloud_sweep.CloudSweepError(f"aws {what} failed: NotFound")
        return value

    def caller_account(self) -> str:
        return self.account

    def role(self, identifier: str) -> str:
        return self._maybe_missing("role", f"arn:aws:iam::{ACCOUNT}:role/{identifier}")

    def sweeper(self, identifier: str) -> str:
        return self._maybe_missing("sweeper", self.sweeper_state)

    def budget_action(self, account: str, identifier: str) -> str:
        if ":" not in identifier:
            raise guard_mod.GuardError(f"budget action {identifier!r} is not <budget-name>:<action-id>")
        return self._maybe_missing("budget", "STANDBY")

    def policy(self, arn: str) -> str:
        return self._maybe_missing("policy", arn)

    def credential_expiry(self, env: object) -> tuple[float | None, str]:
        if self.expiry_error:
            raise guard_mod.GuardError(self.expiry_error)
        return self.expiry, "fake"

    def expired_resources(self, keys: object, now: float, exclude_bucket: str) -> list:
        return self.leftovers

    def put_record(self, bucket: str, region: str, key: str, body: str) -> None:
        self.records[key] = body

    def delete_record(self, bucket: str, region: str, key: str) -> None:
        self.deleted.append(key)
        self.records.pop(key, None)

    def write_kubeconfig(self, cluster: str, path: Path) -> None:
        if self.kubeconfig_error:
            raise self.kubeconfig_error
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("apiVersion: v1\n", encoding="utf-8")


class FakeRunner:
    """Records each command; `codes` maps a word in the command to its exit code."""

    def __init__(self, codes: dict[str, int] | None = None, on_run: dict | None = None) -> None:
        self.commands: list[list[str]] = []
        self.codes = codes or {}
        self.on_run = on_run or {}
        self.current = None

    def run(self, cmd: list[str], *, env: object = None, new_session: bool = False) -> int:
        self.commands.append(cmd)
        for word, action in self.on_run.items():
            if word in cmd:
                action()
        for word, code in self.codes.items():
            if word in cmd:
                return code
        return 0

    def stop_current(self, timeout: float = 0) -> None:
        return None

    def words(self) -> list[str]:
        """The verb of each command: init, apply, cycle, teardown or destroy."""
        verbs = ("init", "apply", "cycle", "teardown", "destroy")
        return [next((w for w in verbs if w in cmd), cmd[0]) for cmd in self.commands]


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo-shaped tree holding one root with a rendered JSON dial."""
    monkeypatch.setattr(guard_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(guard_mod, "RUNS_DIR", tmp_path / ".tmp" / "cloud-runs")
    tf_dir = tmp_path / "terraform" / "environments" / "aws"
    tf_dir.mkdir(parents=True)
    (tf_dir / "dial.auto.tfvars.json").write_text(json.dumps(TFVARS), encoding="utf-8")
    for name in ("DFE_GUARD_ROLE", "DFE_GUARD_SWEEPER", "DFE_GUARD_BUDGET_ACTION", "DFE_GUARD_PERMISSIONS_BOUNDARY",
                 "DFE_GUARD_IAM_PATH", "DFE_GUARD_S3_BUCKET_PREFIX", "DFE_GUARD_INSPECTOR_EXCLUSION",
                 "DFE_RUN_STATE_PREFIX", "DFE_RUN_TAG_KEY", "DFE_RUN_EXPIRY_KEY", "DFE_RUN_EXPIRY_FORMAT"):
        monkeypatch.delenv(name, raising=False)
    return tf_dir


def _args(tf_dir: Path, **overrides: object) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    guard_mod.add_cloud_guard_subparsers(sub)
    argv = ["cloud-cycle", "--tf-dir", str(tf_dir), "--run-length", "2h", "--teardown-margin", "30m",
            "--guard-role", "e2e-runner", "--guard-sweeper", "e2e-sweeper", "--guard-budget-action", BUDGET_ACTION]
    args = parser.parse_args([*argv, "--", "--mode", "single"])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _config(tf_dir: Path, **overrides: object) -> guard_mod.GuardConfig:
    return guard_mod.resolve_config(_args(tf_dir, **overrides), {})


def _preflight(tf_dir: Path, guard: FakeGuard, **overrides: object) -> guard_mod.PreflightReport:
    return guard_mod.run_preflight(_config(tf_dir, **overrides), guard, now=NOW, env={})


def _failed(report: guard_mod.PreflightReport) -> list[str]:
    return [c.name for c in report.checks if not c.ok]


# --- configuration ---------------------------------------------------------------------


def test_a_fully_guarded_account_passes_every_check(root: Path) -> None:
    report = _preflight(root, FakeGuard())
    assert report.ok, report.checks
    assert [c.name for c in report.checks] == [
        "account", "(a) guardrail role", "(a) sweeper", "(a) budget action", "(b) credential",
        "(c) expired run resources",
    ]


def test_expected_fail_a_persistent_dial_is_refused_before_any_check(root: Path) -> None:
    (root / "dial.auto.tfvars.json").write_text(
        json.dumps({**TFVARS, "tags": {"lifecycle": "persistent"}}), encoding="utf-8"
    )
    with pytest.raises(guard_mod.GuardError, match="ephemeral"):
        _config(root)


def test_expected_fail_an_hcl_tfvars_file_is_refused_by_name(root: Path) -> None:
    (root / "extra.auto.tfvars").write_text('name = "x"\n', encoding="utf-8")
    with pytest.raises(guard_mod.GuardError, match="HCL"):
        _config(root)


def test_expected_fail_a_root_outside_the_repository_is_refused(tmp_path: Path, root: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    with pytest.raises(guard_mod.GuardError, match="inside this repository"):
        _config(outside)


def test_expected_fail_a_dial_with_no_state_bucket_is_refused(root: Path) -> None:
    (root / "dial.auto.tfvars.json").write_text(json.dumps({**TFVARS, "state": {}}), encoding="utf-8")
    with pytest.raises(guard_mod.GuardError, match=r"state\.bucket"):
        _config(root)


def test_tfvars_load_in_opentofus_order_and_skip_the_runs_own_overlay(root: Path) -> None:
    (root / "zz-later.auto.tfvars.json").write_text(json.dumps({"name": "later"}), encoding="utf-8")
    (root / guard_mod.OVERLAY_NAME).write_text(json.dumps({"name": "stale-overlay"}), encoding="utf-8")
    assert guard_mod.load_tfvars(root)["name"] == "later"


def test_flags_win_over_the_environment(root: Path) -> None:
    config = guard_mod.resolve_config(_args(root), {"DFE_GUARD_ROLE": "from-env"})
    assert config.role == "e2e-runner"
    config = guard_mod.resolve_config(_args(root, guard_role=None), {"DFE_GUARD_ROLE": "from-env"})
    assert config.role == "from-env"


# --- (a) the guardrails ------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["role", "sweeper", "budget"])
def test_refusal_a_a_guardrail_that_cannot_be_read_fails_its_check(root: Path, missing: str) -> None:
    guard = FakeGuard()
    guard.missing = {missing}
    report = _preflight(root, guard)
    assert not report.ok
    assert len(_failed(report)) == 1
    assert _failed(report)[0].startswith("(a)")


@pytest.mark.parametrize("flag", ["guard_role", "guard_sweeper", "guard_budget_action"])
def test_refusal_a_an_unconfigured_guardrail_fails_rather_than_skipping(root: Path, flag: str) -> None:
    report = _preflight(root, FakeGuard(), **{flag: None})
    assert not report.ok
    assert any("not configured" in c.detail for c in report.checks if not c.ok)


def test_refusal_a_a_failed_sweeper_fails(root: Path) -> None:
    guard = FakeGuard()
    guard.sweeper_state = "Failed"
    assert _failed(_preflight(root, guard)) == ["(a) sweeper"]


def test_refusal_a_a_malformed_budget_action_fails(root: Path) -> None:
    assert _failed(_preflight(root, FakeGuard(), guard_budget_action="no-separator")) == ["(a) budget action"]


def test_a_configured_boundary_must_exist_too(root: Path) -> None:
    guard = FakeGuard()
    guard.missing = {"policy"}
    report = _preflight(root, guard, permissions_boundary=f"arn:aws:iam::{ACCOUNT}:policy/dfe-e2e-boundary")
    assert _failed(report) == ["(a) permissions boundary"]


def test_expected_fail_the_wrong_account_fails(root: Path) -> None:
    guard = FakeGuard()
    guard.account = "111111111111"
    assert _failed(_preflight(root, guard)) == ["account"]


# --- (b) the credential ----------------------------------------------------------------------


def test_refusal_b_a_credential_expiring_inside_the_window_fails(root: Path) -> None:
    guard = FakeGuard()
    guard.expiry = NOW + 2 * 3600 + 30 * 60 - 1  # one second short of run length + margin
    assert _failed(_preflight(root, guard)) == ["(b) credential"]


def test_a_credential_expiring_exactly_at_the_window_passes(root: Path) -> None:
    guard = FakeGuard()
    guard.expiry = NOW + 2 * 3600 + 30 * 60
    assert _preflight(root, guard).ok


def test_a_credential_with_no_expiry_passes(root: Path) -> None:
    guard = FakeGuard()
    guard.expiry = None
    assert _preflight(root, guard).ok


def test_refusal_b_an_unreadable_credential_fails_rather_than_passing(root: Path) -> None:
    guard = FakeGuard()
    guard.expiry_error = "could not resolve the active credential"
    assert _failed(_preflight(root, guard)) == ["(b) credential"]


def test_credential_expiry_reads_the_environment_first(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the CLI must not be asked when the environment answers")

    monkeypatch.setattr(guard_mod.aws_cli, "run_aws", boom)
    expires, source = guard_mod.AwsGuard("us-west-2").credential_expiry(
        {"AWS_CREDENTIAL_EXPIRATION": "2026-10-08T12:00:00Z"}
    )
    assert (expires, source) == (NOW, "AWS_CREDENTIAL_EXPIRATION")


def test_credential_expiry_keeps_only_the_expiration_from_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"Version": 1, "AccessKeyId": "AKIDEXAMPLE", "SecretAccessKey": "not-a-real-secret",
            "Expiration": "2026-10-08T12:00:00+00:00"}
    monkeypatch.setattr(
        guard_mod.aws_cli, "run_aws",
        lambda args, **_: subprocess.CompletedProcess(args, 0, stdout=json.dumps(body), stderr=""),
    )
    expires, _source = guard_mod.AwsGuard("us-west-2").credential_expiry({})
    assert expires == NOW


def test_an_unparseable_cli_answer_never_echoes_what_it_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        guard_mod.aws_cli, "run_aws",
        lambda args, **_: subprocess.CompletedProcess(args, 0, stdout="SecretAccessKey=not-a-real-secret", stderr=""),
    )
    with pytest.raises(guard_mod.GuardError) as excinfo:
        guard_mod.AwsGuard("us-west-2").credential_expiry({})
    assert "not-a-real-secret" not in str(excinfo.value)


# --- (c) leftovers -----------------------------------------------------------------------------


def test_refusal_c_expired_run_resources_fail_and_name_the_sweep_that_removes_them(root: Path) -> None:
    guard = FakeGuard()
    guard.leftovers = [cloud_sweep.Resource(kind="ec2-instance", id="i-1", name="i-1", created=None, tagged=True)]
    report = _preflight(root, guard)
    assert _failed(report) == ["(c) expired run resources"]
    detail = report.checks[-1].detail
    assert "ec2-instance i-1" in detail
    assert "cloud_sweep.py" in detail
    assert "--expired --grace 0 --delete" in detail


# --- the run's overlay ----------------------------------------------------------------------------


def test_the_overlay_gives_the_run_its_own_state_key_tags_and_no_cloudtrail(root: Path) -> None:
    overlay = guard_mod.build_overlay(_config(root), "run-1", int(NOW))
    assert overlay["state"] == {"bucket": "example-state", "region": "us-west-2",
                                "key": "dfe-e2e-runs/run-1/terraform.tfstate"}
    assert overlay["run"] == cloud_run.run_tfvar("run-1", int(NOW))
    assert overlay["cloudtrail"] == {"enabled": False}
    assert not {"permissions_boundary", "iam_path", "s3_bucket_prefix", "inspector_ec2_exclusion"} & set(overlay)


def test_the_overlay_carries_every_configured_guardrail_input(root: Path) -> None:
    config = _config(root, permissions_boundary=f"arn:aws:iam::{ACCOUNT}:policy/b", iam_path="/dfe-e2e/",
                     s3_bucket_prefix="dfe-e2e-", inspector_exclusion=True)
    overlay = guard_mod.build_overlay(config, "run-1", int(NOW))
    assert overlay["permissions_boundary"] == f"arn:aws:iam::{ACCOUNT}:policy/b"
    assert overlay["iam_path"] == "/dfe-e2e/"
    assert overlay["s3_bucket_prefix"] == "dfe-e2e-"
    assert overlay["inspector_ec2_exclusion"] is True


# --- cloud-cycle: nothing created on a refusal, torn down on every exit ------------------------------


def _cycle(root: Path, monkeypatch: pytest.MonkeyPatch, guard: FakeGuard, runner: FakeRunner, **overrides: object) -> int:
    monkeypatch.setattr(guard_mod, "_make_guard", lambda config: guard)
    monkeypatch.setattr(guard_mod, "Runner", lambda: runner)
    monkeypatch.setattr(guard_mod, "_tofu_output", lambda tf_dir, name: "dfe-example")
    monkeypatch.setattr(guard_mod.time, "time", lambda: NOW)
    return guard_mod.cmd_cloud_cycle(_args(root, run_id="run-1", **overrides))


def test_a_refused_preflight_creates_nothing(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard, runner = FakeGuard(), FakeRunner()
    guard.missing = {"role"}
    assert _cycle(root, monkeypatch, guard, runner) == 2
    assert runner.commands == []
    assert guard.records == {}
    assert not (root / guard_mod.OVERLAY_NAME).exists()


def test_a_successful_run_applies_cycles_destroys_and_clears_its_record(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard, runner = FakeGuard(), FakeRunner()
    seen_overlay: list[dict] = []
    runner.on_run = {"apply": lambda: seen_overlay.append(json.loads((root / guard_mod.OVERLAY_NAME).read_text()))}
    assert _cycle(root, monkeypatch, guard, runner) == 0
    assert runner.words() == ["init", "apply", "cycle", "destroy"]
    assert seen_overlay[0]["state"]["key"] == "dfe-e2e-runs/run-1/terraform.tfstate"
    assert guard.deleted == ["dfe-e2e-runs/run-1/run.json"]
    assert not (root / guard_mod.OVERLAY_NAME).exists()
    cycle = runner.commands[2]
    assert "--from-terraform" in cycle
    assert "--kubeconfig" in cycle
    assert cycle[-2:] == ["--mode", "single"]


def test_the_run_record_is_written_before_the_apply(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard, runner = FakeGuard(), FakeRunner()
    records_at_apply: list[int] = []
    runner.on_run = {"apply": lambda: records_at_apply.append(len(guard.records))}
    _cycle(root, monkeypatch, guard, runner)
    assert records_at_apply == [1]
    assert guard.records == {}  # cleared by the successful destroy


def test_a_record_that_cannot_be_written_stops_before_tofu_and_cleans_up(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no record the reaper could not finish the run, so nothing is applied."""
    guard, runner = FakeGuard(), FakeRunner()

    def refuse(*_args: object) -> None:
        raise cloud_sweep.CloudSweepError("aws s3api put-object failed: AccessDenied")

    guard.put_record = refuse  # type: ignore[method-assign]
    assert _cycle(root, monkeypatch, guard, runner) == 1
    assert runner.commands == []
    assert not (root / guard_mod.OVERLAY_NAME).exists()


def test_a_failed_apply_is_still_destroyed(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FakeRunner(codes={"apply": 1})
    assert _cycle(root, monkeypatch, FakeGuard(), runner) == 1
    assert runner.words() == ["init", "apply", "destroy"]


def test_an_exception_mid_run_is_still_destroyed(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard, runner = FakeGuard(), FakeRunner()
    guard.kubeconfig_error = cloud_sweep.CloudSweepError("aws eks update-kubeconfig failed")
    assert _cycle(root, monkeypatch, guard, runner) == 1
    assert runner.words() == ["init", "apply", "destroy"]


def test_sigterm_during_the_cycle_removes_workloads_then_destroys(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent killed mid-run: the cycle never reached its own destroy, so the
    workloads go first and then the infrastructure, and the run says it was cut."""
    runner = FakeRunner(on_run={"cycle": lambda: os.kill(os.getpid(), signal.SIGTERM)})
    assert _cycle(root, monkeypatch, FakeGuard(), runner) == 143
    assert runner.words() == ["init", "apply", "cycle", "teardown", "destroy"]
    assert signal.getsignal(signal.SIGTERM) is not guard_mod._raise_interrupted


def test_a_failed_destroy_keeps_the_record_and_overlay_for_the_reaper(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard, runner = FakeGuard(), FakeRunner(codes={"destroy": 1})
    assert _cycle(root, monkeypatch, guard, runner) == 1
    assert guard.deleted == []
    assert "dfe-e2e-runs/run-1/run.json" in guard.records
    assert (root / guard_mod.OVERLAY_NAME).exists()


@pytest.mark.parametrize("reserved", ["--keep", "--kubeconfig=x", "--from-terraform"])
def test_expected_fail_cycle_args_that_skip_the_destroy_or_clash_are_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch, reserved: str
) -> None:
    runner = FakeRunner()
    code = _cycle(root, monkeypatch, FakeGuard(), runner, cycle_args=["--", "--mode", "single", reserved])
    assert code == 2
    assert runner.commands == []


@pytest.mark.parametrize("provider", ["gcp", "azure"])
def test_an_unbuilt_provider_refuses_by_name(root: Path, provider: str, capsys: pytest.CaptureFixture) -> None:
    assert guard_mod.cmd_cloud_preflight(_args(root, provider=provider)) == 2
    assert f"no {provider} checks" in capsys.readouterr().err


def test_the_runner_stops_the_child_in_flight() -> None:
    runner = guard_mod.Runner()
    runner.current = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    child = runner.current
    runner.stop_current(timeout=5)
    assert child.poll() is not None
    assert runner.current is None
