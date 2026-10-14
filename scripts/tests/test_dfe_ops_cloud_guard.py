#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_cloud_guard.py
#  Purpose:      Guard `dfe-ops cloud-preflight` and `cloud-cycle`: each of the
#                four refusals fires on its own cause and nothing is created
#                after one, the run gets its own state key and tags, and every
#                way out of a run -- success, failure, exception, SIGTERM, the
#                run length running out -- tears it down, keeping the run
#                record when the destroy fails.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe_ops_cloud_guard.py.

    python3 -m pytest scripts/tests/test_dfe_ops_cloud_guard.py -q

The cloud is a FakeGuard and every child process a FakeRunner, so no AWS call
is made and no tofu runs. The real processes are short python children the
Runner is asked to stop, or stops at a deadline a few seconds out.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
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
    "profile": "single",
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
        self.sweeps: list[list[str]] = []
        self.sweep_code: int | list[int] = 0
        self.sweep_error: Exception | None = None

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

    def run_resources(self, keys: object, exclude_bucket: str) -> list:
        return self.leftovers

    def run_records(self, bucket: str, region: str, prefix: str) -> list[str]:
        if "records" in self.missing:
            raise cloud_sweep.CloudSweepError("aws s3api list-objects-v2 failed: AccessDenied")
        return [key for key in self.records if key.startswith(f"{prefix}/")]

    def put_record(self, bucket: str, region: str, key: str, body: str) -> None:
        self.records[key] = body

    def delete_record(self, bucket: str, region: str, key: str) -> None:
        self.deleted.append(key)
        self.records.pop(key, None)

    def sweep(self, args: list[str]) -> int:
        """Record the sweep; a list of sweep codes answers the sweeps in order, its last repeating."""
        self.sweeps.append(args)
        if self.sweep_error:
            raise self.sweep_error
        if isinstance(self.sweep_code, list):
            return self.sweep_code.pop(0) if len(self.sweep_code) > 1 else self.sweep_code[0]
        return self.sweep_code

    def write_kubeconfig(self, cluster: str, path: Path) -> None:
        if self.kubeconfig_error:
            raise self.kubeconfig_error
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("apiVersion: v1\n", encoding="utf-8")


class FakeRunner:
    """Records each command; `codes` maps a word in the command to its exit code.

    A list of codes answers that word's runs in order, its last code repeating.
    A command carrying a word in `stalls` runs past its deadline, so it raises
    the Runner's DeadlineError once recorded.
    """

    def __init__(self, codes: dict[str, int | list[int]] | None = None, on_run: dict | None = None,
                 stalls: set[str] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.sessions: list[bool] = []
        self.deadlines: list[float | None] = []
        self.codes = codes or {}
        self.on_run = on_run or {}
        self.stalls = stalls or set()
        self.current = None

    def run(self, cmd: list[str], *, env: dict | None = None, new_session: bool = False,
            deadline: float | None = None, stop_timeout: float = 0) -> int:
        self.commands.append(cmd)
        self.envs.append(env)
        self.sessions.append(new_session)
        self.deadlines.append(None if deadline is None else deadline - guard_mod.time.monotonic())
        for word, action in self.on_run.items():
            if word in cmd:
                action()
        if self.stalls & set(cmd):
            raise guard_mod.DeadlineError(f"{cmd[0]} ran past its deadline and was stopped")
        for word, code in self.codes.items():
            if word in cmd:
                if isinstance(code, list):
                    return code.pop(0) if len(code) > 1 else code[0]
                return code
        return 0

    def stop_current(self, timeout: float = 0) -> None:
        return None

    def words(self) -> list[str]:
        """The verb of each command: init, apply, cycle, teardown or destroy."""
        verbs = ("init", "apply", "cycle", "teardown", "destroy")
        return [next((w for w in verbs if w in cmd), cmd[0]) for cmd in self.commands]


DEPLOY_TOOLS = ("kubectl", "helm", "python3", "openssl", "aws")


def _tools(directory: Path, tools: tuple[str, ...] = DEPLOY_TOOLS) -> Path:
    """A directory holding one executable stand-in per tool, for a PATH check (e) reads."""
    directory.mkdir(parents=True, exist_ok=True)
    for tool in tools:
        stand_in = directory / tool
        stand_in.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stand_in.chmod(0o755)
    return directory


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo-shaped tree holding one root with a rendered JSON dial, on a PATH with every deploy tool."""
    monkeypatch.setenv("PATH", f"{_tools(tmp_path / 'tools')}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(guard_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(guard_mod, "RUNS_DIR", tmp_path / ".tmp" / "cloud-runs")
    tf_dir = tmp_path / "terraform" / "environments" / "aws"
    tf_dir.mkdir(parents=True)
    (tf_dir / "dial.auto.tfvars.json").write_text(json.dumps(TFVARS), encoding="utf-8")
    for name in ("DFE_GUARD_ROLE", "DFE_GUARD_SWEEPER", "DFE_GUARD_BUDGET_ACTION", "DFE_GUARD_PERMISSIONS_BOUNDARY",
                 "DFE_GUARD_IAM_PATH", "DFE_GUARD_S3_BUCKET_PREFIX", "DFE_GUARD_INSPECTOR_EXCLUSION",
                 "DFE_RUN_STATE_PREFIX", "DFE_RUN_TAG_KEY", "DFE_RUN_EXPIRY_KEY", "DFE_RUN_EXPIRY_FORMAT",
                 "DFE_RUN_ENDPOINT_CIDR"):
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
        "(c) expired run resources", "(d) unfinished runs", "(e) deploy tools",
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
    (root / "later.auto.tfvars.json").write_text(json.dumps({"name": "later"}), encoding="utf-8")
    (root / guard_mod.OVERLAY_NAME).write_text(json.dumps({"name": "stale-overlay"}), encoding="utf-8")
    assert guard_mod.load_tfvars(root)["name"] == "later"


@pytest.mark.parametrize("name", ["zz-later.auto.tfvars.json", "zzz.auto.tfvars.json", "zz-dfe-run2.auto.tfvars.json"])
def test_expected_fail_an_auto_tfvars_file_sorting_after_the_overlay_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """OpenTofu loads auto tfvars in name order, so a later file would quietly
    replace the run's endpoint, state key or tags."""
    (root / name).write_text(json.dumps({"endpoint": {"public": True, "allowed_cidrs": ["52.0.0.0/8"]}}),
                             encoding="utf-8")
    with pytest.raises(guard_mod.GuardError, match="sorts after"):
        guard_mod.load_tfvars(root)
    guard, runner = FakeGuard(), FakeRunner()
    assert _cycle(root, monkeypatch, guard, runner) == 2
    assert runner.commands == []
    assert guard.records == {}


def test_an_empty_environment_variable_reads_as_unset(root: Path) -> None:
    """A workflow hands an unset repository variable over as an empty string. Read
    as a value, an empty state prefix would refuse every run."""
    env = {cloud_run.STATE_PREFIX_ENV: "", "DFE_GUARD_IAM_PATH": "", "DFE_GUARD_PERMISSIONS_BOUNDARY": " ",
           guard_mod.ENDPOINT_CIDR_ENV: ""}
    config = guard_mod.resolve_config(_args(root), env)
    assert config.state_prefix == cloud_run.DEFAULT_STATE_PREFIX
    assert (config.iam_path, config.permissions_boundary, config.endpoint_cidr) == ("", "", "")


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


def test_expected_fail_a_session_credential_with_no_known_expiry_is_refused(root: Path) -> None:
    """A session credential always expires, so passing one whose expiry is unread
    lets an unattended run lose its identity mid-apply."""
    guard = FakeGuard()
    guard.expiry = None
    report = guard_mod.run_preflight(_config(root), guard, now=NOW, env={"AWS_SESSION_TOKEN": "not-a-real-token"})
    assert _failed(report) == ["(b) credential"]
    detail = next(c.detail for c in report.checks if c.name == "(b) credential")
    assert "AWS_CREDENTIAL_EXPIRATION" in detail
    assert "not-a-real-token" not in detail


def test_expected_fail_the_cli_resolving_a_session_credential_with_no_expiry_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {"Version": 1, "AccessKeyId": "AKIDEXAMPLE", "SecretAccessKey": "not-a-real-secret",
            "SessionToken": "not-a-real-token"}
    monkeypatch.setattr(
        guard_mod.aws_cli, "run_aws",
        lambda args, **_: subprocess.CompletedProcess(args, 0, stdout=json.dumps(body), stderr=""),
    )
    with pytest.raises(guard_mod.GuardError, match="session credential") as excinfo:
        guard_mod.AwsGuard("us-west-2").credential_expiry({})
    assert "not-a-real-token" not in str(excinfo.value)
    assert "not-a-real-secret" not in str(excinfo.value)


def test_a_long_term_key_with_no_expiry_still_reads_as_no_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"Version": 1, "AccessKeyId": "AKIDEXAMPLE", "SecretAccessKey": "not-a-real-secret"}
    monkeypatch.setattr(
        guard_mod.aws_cli, "run_aws",
        lambda args, **_: subprocess.CompletedProcess(args, 0, stdout=json.dumps(body), stderr=""),
    )
    assert guard_mod.AwsGuard("us-west-2").credential_expiry({}) == (None, "a credential with no expiry")


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


def _run_resource(name: str, expires_at: int, run_id: str = "r-prev") -> cloud_sweep.Resource:
    return cloud_sweep.Resource(
        kind="ec2-instance", id=name, name=name, created=None, tagged=True,
        tags=cloud_run.run_tags(run_id, expires_at),
    )


def _check(report: guard_mod.PreflightReport, name: str) -> guard_mod.Check:
    return next(c for c in report.checks if c.name == name)


def test_refusal_c_expired_run_resources_fail_and_name_the_sweep_that_removes_them(root: Path) -> None:
    guard = FakeGuard()
    guard.leftovers = [_run_resource("i-1", int(NOW) - 60)]
    report = _preflight(root, guard)
    assert _failed(report) == ["(c) expired run resources"]
    detail = _check(report, "(c) expired run resources").detail
    assert "ec2-instance i-1" in detail
    assert "cloud_sweep.py" in detail
    assert "--expired --grace 0 --delete" in detail


def test_refusal_d_a_live_run_resource_refuses_and_says_when_it_expires(root: Path) -> None:
    """A leg whose destroy failed leaves resources that have not expired yet; (c) alone passes them."""
    guard = FakeGuard()
    guard.leftovers = [_run_resource("i-2", int(NOW) + 3600)]
    report = _preflight(root, guard)
    assert _failed(report) == ["(d) unfinished runs"]
    detail = _check(report, "(d) unfinished runs").detail
    assert "ec2-instance i-2 (until 2026-10-08T13:00:00Z)" in detail
    assert "tofu destroy" in detail


def test_refusal_d_a_run_tag_with_no_readable_expiry_refuses_since_no_sweep_removes_it(root: Path) -> None:
    guard = FakeGuard()
    guard.leftovers = [
        cloud_sweep.Resource(kind="eip", id="eipalloc-1", name="192.0.2.7", created=None, tagged=True,
                             tags={"dfe-e2e": "r-prev"}),
        cloud_sweep.Resource(kind="vpc", id="vpc-1", name="vpc-1", created=None, tagged=True,
                             tags={"dfe-e2e": "r-prev", "expires-at": "soon"}),
    ]
    detail = _check(_preflight(root, guard), "(d) unfinished runs").detail
    assert "eip 192.0.2.7 (no-expiry)" in detail
    assert "vpc vpc-1 (malformed-expiry)" in detail


def test_refusal_d_a_run_record_refuses_even_with_no_resource_left(root: Path) -> None:
    """A record goes only when its run's destroy succeeds, so one still there names an unfinished run."""
    guard = FakeGuard()
    guard.records = {"dfe-e2e-runs/r-prev/run.json": "{}"}
    report = _preflight(root, guard)
    assert _failed(report) == ["(d) unfinished runs"]
    assert "run record dfe-e2e-runs/r-prev/run.json" in _check(report, "(d) unfinished runs").detail


def test_an_untagged_resource_and_a_record_under_another_prefix_refuse_nothing(root: Path) -> None:
    guard = FakeGuard()
    guard.leftovers = [cloud_sweep.Resource(kind="vpc", id="vpc-9", name="vpc-9", created=None, tagged=False)]
    guard.records = {"elsewhere/r-prev/run.json": "{}"}
    assert _preflight(root, guard).ok


def test_refusal_d_run_records_that_cannot_be_listed_fail_rather_than_pass(root: Path) -> None:
    guard = FakeGuard()
    guard.missing = {"records"}
    report = _preflight(root, guard)
    assert _failed(report) == ["(d) unfinished runs"]
    assert "AccessDenied" in _check(report, "(d) unfinished runs").detail


def test_refusal_d_resources_that_cannot_be_listed_fail_both_checks(root: Path) -> None:
    guard = FakeGuard()

    def refuse(*_args: object) -> list:
        raise cloud_sweep.CloudSweepError("aws resourcegroupstaggingapi get-resources failed: Throttling")

    guard.run_resources = refuse  # type: ignore[method-assign]
    assert _failed(_preflight(root, guard)) == ["(c) expired run resources", "(d) unfinished runs"]


# --- (e) every tool the deploy and its teardown run is on the run's PATH ------------------------


def _path_without(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *absent: str) -> None:
    """PATH as the run's children get it, holding every deploy tool but `absent`."""
    kept = tuple(t for t in DEPLOY_TOOLS if t not in absent)
    monkeypatch.setenv("PATH", str(_tools(tmp_path / "partial-tools", kept)))


def test_refusal_e_a_tool_missing_from_the_runs_path_fails_and_names_what_needs_it(
    root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deploy that finds a tool missing has already paid for the cluster the apply built."""
    _path_without(monkeypatch, tmp_path, "openssl")
    report = _preflight(root, FakeGuard())
    assert _failed(report) == ["(e) deploy tools"]
    detail = _check(report, "(e) deploy tools").detail
    assert detail.startswith("not on PATH: openssl: mints the bundled deploy repo's admin password")
    assert "kubectl" not in detail


def test_refusal_e_an_aws_run_needs_the_aws_cli_its_teardown_sweeps_with(
    root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _path_without(monkeypatch, tmp_path, "aws", "helm")
    detail = _check(_preflight(root, FakeGuard()), "(e) deploy tools").detail
    assert "helm: every chart install" in detail
    assert "aws: destroy.sh step 9" in detail


def test_the_deploy_tools_check_names_what_it_found(root: Path) -> None:
    check = _check(_preflight(root, FakeGuard()), "(e) deploy tools")
    assert check.ok
    assert check.detail == "deploy tools on PATH: kubectl helm python3 openssl aws"


def test_a_tool_check_that_cannot_run_fails_rather_than_passing(
    root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(guard_mod, "CHECK_TOOLS", tmp_path / "gone" / "check_tools.py")
    check = _check(_preflight(root, FakeGuard()), "(e) deploy tools")
    assert not check.ok
    assert check.detail.startswith("check_tools.py exited 2: ")


def test_expected_fail_a_runner_missing_a_deploy_tool_creates_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _path_without(monkeypatch, tmp_path, "kubectl")
    guard, runner = FakeGuard(), FakeRunner()
    assert _cycle(root, monkeypatch, guard, runner) == 2
    assert runner.commands == []
    assert guard.records == {}
    assert not (root / guard_mod.OVERLAY_NAME).exists()
    err = capsys.readouterr().err
    assert "FAIL  (e) deploy tools: not on PATH: kubectl: every cluster call" in err
    assert "cloud-cycle REFUSED: nothing was created." in err


# --- (c) and (d) when the tagging API lists what EC2 has already deleted ------------------------

NAT_ID = "nat-0123456789abcdef0"
ENDPOINT_ID = "vpce-0123456789abcdef0"
INSTANCE_ID = "i-0123456789abcdef0"
NAT_ARN = f"arn:aws:ec2:us-west-2:{ACCOUNT}:natgateway/{NAT_ID}"
ENDPOINT_ARN = f"arn:aws:ec2:us-west-2:{ACCOUNT}:vpc-endpoint/{ENDPOINT_ID}"
INSTANCE_ARN = f"arn:aws:ec2:us-west-2:{ACCOUNT}:instance/{INSTANCE_ID}"
ID_FLAGS = ("--nat-gateway-ids", "--vpc-endpoint-ids", "--instance-ids")


def _aws_ok(body: object) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["aws"], 0, stdout=json.dumps(body), stderr="")


def _aws_fail(stderr: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["aws"], 254, stdout="", stderr=stderr)


def _nat(state: str) -> subprocess.CompletedProcess:
    return _aws_ok({"NatGateways": [{"NatGatewayId": NAT_ID, "State": state}]})


def _endpoint(state: str) -> subprocess.CompletedProcess:
    return _aws_ok({"VpcEndpoints": [{"VpcEndpointId": ENDPOINT_ID, "State": state}]})


def _instance(state: str) -> subprocess.CompletedProcess:
    return _aws_ok(_instances(state))


def _instances(state: str) -> dict:
    return {"Reservations": [{"Instances": [{"InstanceId": INSTANCE_ID, "State": {"Name": state}}]}]}


INSTANCE_NOT_FOUND = _aws_fail(
    "An error occurred (InvalidInstanceID.NotFound) when calling the DescribeInstances "
    f"operation: The instance ID '{INSTANCE_ID}' does not exist"
)
NAT_NOT_FOUND = _aws_fail(
    f"An error occurred (NatGatewayNotFound) when calling the DescribeNatGateways operation: {NAT_ID}"
)
ENDPOINT_NOT_FOUND = _aws_fail(
    "An error occurred (InvalidVpcEndpointId.NotFound) when calling the DescribeVpcEndpoints "
    f"operation: The VpcEndpoint Id '{ENDPOINT_ID}' does not exist"
)


class AwsRegionGuard(FakeGuard):
    """FakeGuard's account reads, with the region's run resources and the teardown's sweep
    going through the real AwsGuard."""

    def run_resources(self, keys: object, exclude_bucket: str) -> list:
        return guard_mod.AwsGuard("us-west-2").run_resources(keys, exclude_bucket)

    def sweep(self, args: list[str]) -> int:
        return guard_mod.AwsGuard("us-west-2").sweep(args)


def _stale_region(
    monkeypatch: pytest.MonkeyPatch,
    arns: list[str],
    described: dict[str, subprocess.CompletedProcess],
    *,
    expires_at: int,
    listed: dict[str, object] | None = None,
) -> list[list[str]]:
    """Stand in for the aws CLI in a region whose tagging API lists `arns` as run-tagged.

    `described` answers a describe call by the resource id it names. `listed` answers an
    unfiltered ec2 describe call, keyed by its subcommand, and every other call answers an
    empty region. Returns the calls made.
    """
    calls: list[list[str]] = []
    tags = [{"Key": k, "Value": v} for k, v in cloud_run.run_tags("r-prev", expires_at).items()]
    mappings = [{"ResourceARN": arn, "Tags": tags} for arn in arns]
    unfiltered = listed or {}

    def answer(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        if args[:2] == ["resourcegroupstaggingapi", "get-resources"]:
            return _aws_ok({"ResourceTagMappingList": mappings})
        for flag in ID_FLAGS:
            if flag in args:
                return described[args[args.index(flag) + 1]]
        if args[0] == "ec2" and args[1] in unfiltered:
            return _aws_ok(unfiltered[args[1]])
        return _aws_ok({})

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", answer)
    return calls


# A deleted NAT gateway and a terminated instance stay in the unfiltered listing a while, which the
# listers skip, so only the tagging API's hit reaches the guard.
LISTED_DELETED_NAT = {
    "describe-nat-gateways": {"NatGateways": [{"NatGatewayId": NAT_ID, "State": "deleted"}]}
}
LISTED_TERMINATED_INSTANCE = {"describe-instances": _instances("terminated")}
GONE_FROM_EC2 = [
    pytest.param(NAT_ARN, _nat("deleted"), LISTED_DELETED_NAT, id="nat-gateway-deleted"),
    pytest.param(NAT_ARN, NAT_NOT_FOUND, None, id="nat-gateway-no-longer-listed"),
    pytest.param(ENDPOINT_ARN, _endpoint("Deleted"), None, id="vpc-endpoint-deleted"),
    pytest.param(ENDPOINT_ARN, ENDPOINT_NOT_FOUND, None, id="vpc-endpoint-not-found"),
    pytest.param(INSTANCE_ARN, _instance("terminated"), LISTED_TERMINATED_INSTANCE, id="instance-terminated"),
    pytest.param(INSTANCE_ARN, INSTANCE_NOT_FOUND, None, id="instance-not-found"),
]


@pytest.mark.parametrize(("arn", "answer", "listed"), GONE_FROM_EC2)
def test_refusal_d_ignores_what_the_tagging_api_lists_after_ec2_deleted_it(
    root: Path, monkeypatch: pytest.MonkeyPatch, arn: str, answer: subprocess.CompletedProcess,
    listed: dict[str, object] | None,
) -> None:
    """The tagging API lags a delete, and the run's expiry is still ahead, so (d) would call it unfinished."""
    _stale_region(
        monkeypatch, [arn], {arn.rsplit("/", 1)[-1]: answer}, expires_at=int(NOW) + 3600, listed=listed
    )
    report = _preflight(root, AwsRegionGuard())
    assert report.ok, [c.detail for c in report.checks if not c.ok]


@pytest.mark.parametrize(("arn", "answer", "listed"), GONE_FROM_EC2)
def test_refusal_c_ignores_an_expired_resource_the_tagging_api_lists_after_ec2_deleted_it(
    root: Path, monkeypatch: pytest.MonkeyPatch, arn: str, answer: subprocess.CompletedProcess,
    listed: dict[str, object] | None,
) -> None:
    """(c) sends the operator to a sweep that has no delete for a tagging-API hit, so it must not name one."""
    _stale_region(
        monkeypatch, [arn], {arn.rsplit("/", 1)[-1]: answer}, expires_at=int(NOW) - 60, listed=listed
    )
    report = _preflight(root, AwsRegionGuard())
    assert report.ok, [c.detail for c in report.checks if not c.ok]


@pytest.mark.parametrize(
    ("arn", "answer", "name"),
    [
        pytest.param(NAT_ARN, _nat("available"), NAT_ID, id="nat-gateway-available"),
        pytest.param(NAT_ARN, _nat("deleting"), NAT_ID, id="nat-gateway-still-deleting"),
        pytest.param(ENDPOINT_ARN, _endpoint("Available"), ENDPOINT_ID, id="vpc-endpoint-available"),
        pytest.param(ENDPOINT_ARN, _endpoint("Deleting"), ENDPOINT_ID, id="vpc-endpoint-still-deleting"),
        pytest.param(INSTANCE_ARN, _instance("running"), INSTANCE_ID, id="instance-running"),
        pytest.param(INSTANCE_ARN, _instance("shutting-down"), INSTANCE_ID, id="instance-shutting-down"),
    ],
)
def test_refusal_d_still_refuses_a_tagged_resource_ec2_still_holds(
    root: Path, monkeypatch: pytest.MonkeyPatch, arn: str, answer: subprocess.CompletedProcess, name: str
) -> None:
    _stale_region(monkeypatch, [arn], {name: answer}, expires_at=int(NOW) + 3600)
    report = _preflight(root, AwsRegionGuard())
    assert _failed(report) == ["(d) unfinished runs"]
    assert f"tagged:ec2 {name} (until 2026-10-08T13:00:00Z)" in _check(report, "(d) unfinished runs").detail


@pytest.mark.parametrize("reason", ["UnauthorizedOperation", "RequestLimitExceeded"])
def test_refusal_d_counts_a_resource_live_when_its_deletion_cannot_be_read(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, reason: str
) -> None:
    denied = _aws_fail(f"An error occurred ({reason}) when calling the DescribeNatGateways operation")
    _stale_region(monkeypatch, [NAT_ARN], {NAT_ID: denied}, expires_at=int(NOW) + 3600)
    report = _preflight(root, AwsRegionGuard())
    assert _failed(report) == ["(d) unfinished runs"]
    assert f"tagged:ec2 {NAT_ID}" in _check(report, "(d) unfinished runs").detail
    assert reason in capsys.readouterr().err


def test_a_run_tagged_resource_of_another_class_is_never_judged_gone(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stale_region(
        monkeypatch, [f"arn:aws:rds:us-west-2:{ACCOUNT}:db:run-db"], {}, expires_at=int(NOW) + 3600
    )
    assert _failed(_preflight(root, AwsRegionGuard())) == ["(d) unfinished runs"]
    described_by_id = [call for call in calls if any(flag in call for flag in ID_FLAGS)]
    assert described_by_id == []


def test_a_second_leg_after_a_failed_destroy_creates_nothing(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leg 1's teardown failed, so its record and unexpired resources stay; leg 2 stops at preflight."""
    guard, runner = FakeGuard(), FakeRunner(codes={"destroy": 1})
    assert _cycle(root, monkeypatch, guard, runner) == 1
    assert list(guard.records) == ["dfe-e2e-runs/run-1/run.json"]
    guard.leftovers = [_run_resource("i-leg-1", int(NOW) + 3600, run_id="run-1")]
    second = FakeRunner()
    assert _cycle(root, monkeypatch, guard, second) == 2
    assert second.commands == []
    assert list(guard.records) == ["dfe-e2e-runs/run-1/run.json"]


def test_the_run_records_are_listed_under_the_prefix_in_the_state_region(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    listing = {"Contents": [{"Key": "dfe-e2e-runs/r-prev/run.json"}, {"Key": "dfe-e2e-runs/r-prev/terraform.tfstate"}]}

    def record(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(listing), stderr="")

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", record)
    keys = guard_mod.AwsGuard("eu-west-1").run_records("example-state", "us-west-2", "dfe-e2e-runs")
    assert keys == ["dfe-e2e-runs/r-prev/run.json"]
    (call,) = calls
    assert call[call.index("--prefix") + 1] == "dfe-e2e-runs/"
    assert call[call.index("--region") + 1] == "us-west-2"


# --- the run's overlay ----------------------------------------------------------------------------


def test_the_overlay_gives_the_run_its_own_state_key_tags_and_no_cloudtrail(root: Path) -> None:
    overlay = guard_mod.build_overlay(_config(root), "run-1", int(NOW))
    assert overlay["state"] == {"bucket": "example-state", "region": "us-west-2",
                                "key": "dfe-e2e-runs/run-1/terraform.tfstate"}
    assert overlay["run"] == cloud_run.run_tfvar("run-1", int(NOW))
    assert overlay["cloudtrail"] == {"enabled": False}
    assert not {"permissions_boundary", "iam_path", "s3_bucket_prefix", "inspector_ec2_exclusion",
                "endpoint", "edge_self_ranges"} & set(overlay)


def test_the_overlay_carries_every_configured_guardrail_input(root: Path) -> None:
    config = _config(root, permissions_boundary=f"arn:aws:iam::{ACCOUNT}:policy/b", iam_path="/dfe-e2e/",
                     s3_bucket_prefix="dfe-e2e-", inspector_exclusion=True)
    overlay = guard_mod.build_overlay(config, "run-1", int(NOW))
    assert overlay["permissions_boundary"] == f"arn:aws:iam::{ACCOUNT}:policy/b"
    assert overlay["iam_path"] == "/dfe-e2e/"
    assert overlay["s3_bucket_prefix"] == "dfe-e2e-"
    assert overlay["inspector_ec2_exclusion"] is True


# --- the API endpoint a run opens to the machine running it ----------------------------------------

RUNNER_CIDR = "20.1.2.3/32"


def test_an_endpoint_cidr_opens_the_api_to_that_address_alone(root: Path) -> None:
    """The run's address replaces the dial's list rather than joining it."""
    (root / "dial.auto.tfvars.json").write_text(
        json.dumps({**TFVARS, "endpoint": {"public": False, "allowed_cidrs": ["52.0.0.0/8"]}}),
        encoding="utf-8",
    )
    overlay = guard_mod.build_overlay(_config(root, endpoint_cidr=RUNNER_CIDR), "run-1", int(NOW))
    assert overlay["endpoint"] == {"public": True, "allowed_cidrs": [RUNNER_CIDR]}


def test_an_endpoint_cidr_makes_the_runner_one_of_the_gateways_self_ranges(root: Path) -> None:
    """The gateway's load balancer is public too, and a run's console test reaches it
    from the runner; the root adds the cluster's own NAT addresses beside it."""
    overlay = guard_mod.build_overlay(_config(root, endpoint_cidr=RUNNER_CIDR), "run-1", int(NOW))
    assert overlay["edge_self_ranges"] == [RUNNER_CIDR]
    assert "edge_allowed_cidrs" not in overlay


def test_the_endpoint_cidr_comes_from_the_environment_and_the_flag_wins(root: Path) -> None:
    env = {guard_mod.ENDPOINT_CIDR_ENV: RUNNER_CIDR}
    assert guard_mod.resolve_config(_args(root), env).endpoint_cidr == RUNNER_CIDR
    flagged = guard_mod.resolve_config(_args(root, endpoint_cidr="20.9.9.9/32"), env)
    assert flagged.endpoint_cidr == "20.9.9.9/32"


def test_a_bare_address_reads_as_its_own_slash_32() -> None:
    assert guard_mod.endpoint_cidr("20.1.2.3") == RUNNER_CIDR


@pytest.mark.parametrize("raw", [
    "0.0.0.0/0",          # the internet
    "20.1.2.0/24",        # wider than one address
    "20.1.2.3/24",        # host bits set under a wider prefix
    "20.1.2.3/33",
    "10.0.0.1/32",        # private
    "100.64.0.1/32",      # carrier-grade NAT
    "203.0.113.7/32",     # documentation range
    "127.0.0.1",
    "2600:1f14::1/128",   # IPv6
    "not-an-address",
])
def test_expected_fail_anything_but_one_public_ipv4_address_is_refused(root: Path, raw: str) -> None:
    with pytest.raises(guard_mod.GuardError, match="endpoint CIDR"):
        _config(root, endpoint_cidr=raw)


def test_a_refused_endpoint_cidr_creates_nothing(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard, runner = FakeGuard(), FakeRunner()
    assert _cycle(root, monkeypatch, guard, runner, endpoint_cidr="0.0.0.0/0") == 2
    assert runner.commands == []
    assert guard.records == {}
    assert not (root / guard_mod.OVERLAY_NAME).exists()


def test_the_run_applies_and_records_the_api_open_to_its_own_address(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reaper destroys from the record, so it carries the endpoint the apply used."""
    guard, runner = FakeGuard(), FakeRunner()
    at_apply: list[tuple[dict, dict]] = []

    def capture() -> None:
        overlay = json.loads((root / guard_mod.OVERLAY_NAME).read_text(encoding="utf-8"))
        record = json.loads(next(iter(guard.records.values())))
        at_apply.append((overlay["endpoint"], record["tfvars"]["endpoint"]))
        at_apply.append((overlay["edge_self_ranges"], record["tfvars"]["edge_self_ranges"]))

    runner.on_run = {"apply": capture}
    assert _cycle(root, monkeypatch, guard, runner, endpoint_cidr=RUNNER_CIDR) == 0
    expected = {"public": True, "allowed_cidrs": [RUNNER_CIDR]}
    assert at_apply == [(expected, expected), ([RUNNER_CIDR], [RUNNER_CIDR])]
    assert runner.words() == ["init", "apply", "cycle", "destroy"]
    assert not (root / guard_mod.OVERLAY_NAME).exists()


@pytest.mark.parametrize(("endpoint", "cidr", "names"), [
    (None, RUNNER_CIDR, f"public to {RUNNER_CIDR} alone"),
    ({"public": True, "allowed_cidrs": ["52.0.0.0/8"]}, None, "public to 52.0.0.0/8 as the dial sets"),
    ({"public": False, "allowed_cidrs": []}, None, "private only"),
])
def test_preflight_says_who_can_reach_the_api(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    endpoint: dict | None, cidr: str | None, names: str,
) -> None:
    if endpoint is not None:
        (root / "dial.auto.tfvars.json").write_text(
            json.dumps({**TFVARS, "endpoint": endpoint}), encoding="utf-8"
        )
    monkeypatch.setattr(guard_mod, "_make_guard", lambda config: FakeGuard())
    monkeypatch.setattr(guard_mod.time, "time", lambda: NOW)
    assert guard_mod.cmd_cloud_preflight(_args(root, endpoint_cidr=cidr)) == 0
    assert names in capsys.readouterr().err


# --- cloud-preflight: an external ClickHouse needs its settings before anything is read -------------

CH_ENV = {
    "DFE_CYCLE_CH_SERVICE_ID": "placeholder-service",
    "DFE_CYCLE_CH_API_KEY_ID": "placeholder-key-id",
    "DFE_CYCLE_CH_API_KEY_SECRET": "placeholder-key-secret",
    "TF_VAR_clickhouse_external_password": "placeholder-password",
}


def _clickhouse_dial(tf_dir: Path, mode: str | None) -> None:
    tfvars = TFVARS if mode is None else {**TFVARS, "clickhouse": {"mode": mode}}
    (tf_dir / "dial.auto.tfvars.json").write_text(json.dumps(tfvars), encoding="utf-8")


def _clickhouse_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    """Leave exactly `env` of the four ClickHouse settings set, whatever the shell holds."""
    for name in CH_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def _preflight_command(root: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(guard_mod, "_make_guard", lambda config: FakeGuard())
    monkeypatch.setattr(guard_mod.time, "time", lambda: NOW)
    return guard_mod.cmd_cloud_preflight(_args(root))


@pytest.mark.parametrize("missing", sorted(CH_ENV))
def test_expected_fail_preflight_of_an_external_dial_missing_a_setting_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, missing: str
) -> None:
    _clickhouse_dial(root, "external")
    env = {name: value for name, value in CH_ENV.items() if name != missing}
    _clickhouse_env(monkeypatch, env)
    assert _preflight_command(root, monkeypatch) == 2
    printed = capsys.readouterr().err
    assert printed.startswith("cloud-preflight refused: clickhouse.mode is external")
    assert missing in printed
    assert [name for name in env if name in printed] == []
    assert not any(value in printed for value in env.values())
    assert "PASS" not in printed


@pytest.mark.parametrize("blank", ["", "   "])
def test_expected_fail_preflight_reads_a_blank_clickhouse_setting_as_unset(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, blank: str
) -> None:
    _clickhouse_dial(root, "external")
    _clickhouse_env(monkeypatch, {**CH_ENV, "DFE_CYCLE_CH_SERVICE_ID": blank})
    assert _preflight_command(root, monkeypatch) == 2
    assert "DFE_CYCLE_CH_SERVICE_ID" in capsys.readouterr().err


def test_preflight_of_an_external_dial_with_every_setting_passes(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _clickhouse_dial(root, "external")
    _clickhouse_env(monkeypatch, CH_ENV)
    assert _preflight_command(root, monkeypatch) == 0
    assert "cloud-preflight passed." in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["in_cluster", None])
def test_preflight_of_an_in_cluster_dial_needs_no_clickhouse_setting(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, mode: str | None
) -> None:
    _clickhouse_dial(root, mode)
    _clickhouse_env(monkeypatch, {})
    assert _preflight_command(root, monkeypatch) == 0
    assert "cloud-preflight passed." in capsys.readouterr().err


def test_preflight_and_cycle_refuse_an_external_dial_in_the_same_words(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _clickhouse_dial(root, "external")
    _clickhouse_env(monkeypatch, {})
    assert _preflight_command(root, monkeypatch) == 2
    preflight = capsys.readouterr().err
    runner = FakeRunner()
    assert _cycle(root, monkeypatch, FakeGuard(), runner) == 2
    cycle = capsys.readouterr().err
    assert runner.commands == []
    assert all(name in preflight for name in CH_ENV)
    assert preflight.replace("cloud-preflight refused", "cloud-cycle refused", 1) == cycle


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
    # The cluster secret may not exist yet, so destroy.sh gets the root's facts to sweep a test run by.
    teardown = runner.commands[runner.words().index("teardown")]
    assert teardown[teardown.index("--from-terraform") + 1] == str(root.resolve())


# --- the run length is a deadline, and the teardown keeps its margin -------------------------------
# _args sets a 2h run length and a 30m margin.


def test_the_cycle_runs_in_a_session_of_its_own_with_the_run_length_as_its_deadline(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner()
    assert _cycle(root, monkeypatch, FakeGuard(), runner) == 0
    cycle = runner.words().index("cycle")
    assert runner.sessions[cycle] is True
    assert 2 * 3600 - 60 < runner.deadlines[cycle] <= 2 * 3600
    assert runner.deadlines[runner.words().index("apply")] is None


def test_a_cycle_past_its_run_length_is_stopped_and_torn_down_inside_the_margin(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Run 38026303019: the cycle's own destroy hung for 3.5h and the job limit
    cancelled it, which gives a teardown seconds. Stopped at the run length, the
    workloads and the infrastructure both still go, with the margin ahead of them."""
    guard, runner = FakeGuard(), FakeRunner(stalls={"cycle"})
    assert _cycle(root, monkeypatch, guard, runner) == guard_mod.DEADLINE_EXIT
    assert runner.words() == ["init", "apply", "cycle", "teardown", "destroy"]
    assert guard.deleted == ["dfe-e2e-runs/run-1/run.json"]
    assert not (root / guard_mod.OVERLAY_NAME).exists()
    assert "reached its 7200s run length" in capsys.readouterr().err


def test_the_workload_teardown_is_held_to_half_the_margin_and_tofu_destroy_still_runs(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    guard, runner = FakeGuard(), FakeRunner(stalls={"cycle", "teardown"})
    assert _cycle(root, monkeypatch, guard, runner) == guard_mod.DEADLINE_EXIT
    assert runner.words() == ["init", "apply", "cycle", "teardown", "destroy"]
    teardown = runner.words().index("teardown")
    assert runner.sessions[teardown] is True
    assert 30 * 60 * guard_mod.WORKLOAD_TEARDOWN_SHARE - 60 < runner.deadlines[teardown] <= 900
    assert runner.deadlines[runner.words().index("destroy")] is None
    assert guard.deleted == ["dfe-e2e-runs/run-1/run.json"]
    assert "passed its 900s share of the teardown margin" in capsys.readouterr().err


def test_an_apply_that_ends_past_the_run_length_skips_the_cycle(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed apply strands its state lock, so the apply is never cut short; the cycle is skipped."""
    clock = [1000.0]
    monkeypatch.setattr(guard_mod.time, "monotonic", lambda: clock[0])

    def slow_apply() -> None:
        clock[0] += 2 * 3600 + 1

    runner = FakeRunner(on_run={"apply": slow_apply})
    assert _cycle(root, monkeypatch, FakeGuard(), runner) == guard_mod.DEADLINE_EXIT
    assert runner.words() == ["init", "apply", "teardown", "destroy"]


def test_a_failed_destroy_after_the_deadline_still_reports_the_destroy(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, runner = FakeGuard(), FakeRunner(codes={"destroy": 1}, stalls={"cycle"})
    assert _cycle(root, monkeypatch, guard, runner) == 1
    assert "dfe-e2e-runs/run-1/run.json" in guard.records


def test_every_child_of_a_run_lands_in_the_dials_region_not_the_shells(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call that names no region follows the shell's default, and an account
    fenced to one region denies it there."""
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    runner = FakeRunner(on_run={"cycle": lambda: os.kill(os.getpid(), signal.SIGTERM)})
    assert _cycle(root, monkeypatch, FakeGuard(), runner) == 143
    assert runner.words() == ["init", "apply", "cycle", "teardown", "destroy"]
    for cmd, env in zip(runner.commands, runner.envs, strict=True):
        assert env is not None, cmd
        assert (env["AWS_REGION"], env["AWS_DEFAULT_REGION"]) == ("us-west-2", "us-west-2"), cmd
    assert runner.envs[3]["KUBECONFIG"].endswith("kubeconfig")


def test_the_cycle_hands_its_admin_probe_the_runners_address(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner is one of the gateway's self ranges, so a door that ignores it is down, not fenced."""
    monkeypatch.delenv(guard_mod.PROBE_SOURCE_ENV, raising=False)
    runner = FakeRunner()
    assert _cycle(root, monkeypatch, FakeGuard(), runner, endpoint_cidr=RUNNER_CIDR) == 0
    cycle_env = runner.envs[runner.words().index("cycle")]
    assert cycle_env is not None
    assert cycle_env[guard_mod.PROBE_SOURCE_ENV] == RUNNER_CIDR.split("/")[0]


def test_a_cycle_with_no_endpoint_cidr_names_no_probe_source(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(guard_mod.PROBE_SOURCE_ENV, raising=False)
    runner = FakeRunner()
    assert _cycle(root, monkeypatch, FakeGuard(), runner) == 0
    assert all(guard_mod.PROBE_SOURCE_ENV not in (env or {}) for env in runner.envs)


def test_the_preflight_s3_listing_is_sent_to_the_dials_region(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The S3 client the sweep makes for check (c) names the run's region, whatever
    the shell's default -- the org fence denies ListBuckets sent anywhere else."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    calls: list[list[str]] = []

    def record(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", record)
    config = _config(root)
    assert guard_mod.AwsGuard(config.region).run_resources(config.keys, config.state_bucket) == []
    list_buckets = [c for c in calls if c[:2] == ["s3api", "list-buckets"]]
    assert len(list_buckets) == 1
    assert list_buckets[0][list_buckets[0].index("--region") + 1] == "us-west-2"
    assert all(c[c.index("--region") + 1] == "us-west-2" for c in calls)


def test_a_failed_destroy_keeps_the_record_and_overlay_for_the_reaper(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard, runner = FakeGuard(), FakeRunner(codes={"destroy": 1})
    assert _cycle(root, monkeypatch, guard, runner) == 1
    assert guard.deleted == []
    assert "dfe-e2e-runs/run-1/run.json" in guard.records
    assert (root / guard_mod.OVERLAY_NAME).exists()


# --- what still carries the run's own tag once its destroy succeeds ------------------------------
# _cycle runs as run-1, so its run tag is dfe-e2e=run-1 and its expiry the 2h run length plus 30m.

RUN_EXPIRES_AT = int(NOW) + 2 * 3600 + 30 * 60
SWEEP_ARGS = [
    "--provider", "aws", "--region", "us-west-2", "--tag-filter", "dfe-e2e=run-1",
    "--delete", "--account", ACCOUNT, "--exclude-bucket", "example-state",
]


def test_a_successful_destroy_then_sweeps_the_runs_own_tag_in_its_region_and_account(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ESO pushes the CA secrets outside tofu with the run's tags, so a destroy alone leaves them."""
    guard, runner = FakeGuard(), FakeRunner()
    order: list[str] = []
    runner.on_run = {"destroy": lambda: order.append("destroy")}
    real_sweep = guard.sweep
    guard.sweep = lambda args: order.append("sweep") or real_sweep(args)  # type: ignore[method-assign]
    assert _cycle(root, monkeypatch, guard, runner) == 0
    assert order == ["destroy", "sweep"]
    assert guard.sweeps == [[*SWEEP_ARGS, "--yes"]]
    parsed = cloud_sweep.build_parser().parse_args(guard.sweeps[0])
    assert cloud_sweep.parse_tag_filter(parsed.tag_filter) == {"dfe-e2e": "run-1"}
    assert (parsed.expired, parsed.include_untagged) == (False, False)
    assert guard.deleted == ["dfe-e2e-runs/run-1/run.json"]


def test_a_renamed_run_tag_key_reaches_the_teardown_sweep(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DFE_RUN_TAG_KEY", "ci-run")
    guard = FakeGuard()
    assert _cycle(root, monkeypatch, guard, FakeRunner()) == 0
    (args,) = guard.sweeps
    assert args[args.index("--tag-filter") + 1] == "ci-run=run-1"


# What the teardown sweeps between a failed destroy and its second attempt.
ENI_SWEEP_ARGS = [*SWEEP_ARGS, "--kind", "eni", "--yes"]


def test_expected_fail_a_failed_destroy_sweeps_only_the_runs_network_interfaces(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With state left, a full sweep would delete what tofu still holds and fail the reaper's destroy."""
    guard = FakeGuard()
    assert _cycle(root, monkeypatch, guard, FakeRunner(codes={"destroy": 1})) == 1
    assert guard.sweeps == [ENI_SWEEP_ARGS]


def test_a_destroy_held_by_a_leaked_interface_sweeps_it_and_destroys_again(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The VPC CNI's interface outlived its node and held a subnet, which failed two destroys in a row."""
    guard, runner = FakeGuard(), FakeRunner(codes={"destroy": [1, 0]})
    order: list[str] = []
    runner.on_run = {"destroy": lambda: order.append("destroy")}
    real_sweep = guard.sweep

    def sweep(args: list[str]) -> int:
        order.append("interface sweep" if "--kind" in args else "run sweep")
        return real_sweep(args)

    guard.sweep = sweep  # type: ignore[method-assign]
    assert _cycle(root, monkeypatch, guard, runner) == 0
    assert order == ["destroy", "interface sweep", "destroy", "run sweep"]
    assert guard.sweeps == [ENI_SWEEP_ARGS, [*SWEEP_ARGS, "--yes"]]
    parsed = cloud_sweep.build_parser().parse_args(guard.sweeps[0])
    assert parsed.kind == ["eni"]
    assert cloud_sweep.parse_tag_filter(parsed.tag_filter) == {"dfe-e2e": "run-1"}
    assert (parsed.expired, parsed.include_untagged, parsed.delete, parsed.yes) == (False, False, True, True)
    assert guard.deleted == ["dfe-e2e-runs/run-1/run.json"]
    assert not (root / guard_mod.OVERLAY_NAME).exists()
    err = capsys.readouterr().err
    assert "tofu destroy failed; deleting the network interfaces that carry dfe-e2e=run-1" in err
    assert "TEARDOWN FAILED" not in err


def test_expected_fail_a_destroy_that_fails_again_is_reported_once_and_keeps_the_record(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    guard, runner = FakeGuard(), FakeRunner(codes={"destroy": 1})
    assert _cycle(root, monkeypatch, guard, runner) == 1
    assert runner.words() == ["init", "apply", "cycle", "destroy", "destroy"]
    assert guard.sweeps == [ENI_SWEEP_ARGS]
    assert guard.deleted == []
    assert "dfe-e2e-runs/run-1/run.json" in guard.records
    assert (root / guard_mod.OVERLAY_NAME).exists()
    assert capsys.readouterr().err.count("TEARDOWN FAILED for run run-1") == 1


def test_an_interface_sweep_that_cannot_finish_still_destroys_again(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    guard, runner = FakeGuard(), FakeRunner(codes={"destroy": [1, 0]})
    guard.sweep_error = cloud_sweep.CloudSweepError("aws ec2 describe-network-interfaces failed: Throttling")
    assert _cycle(root, monkeypatch, guard, runner) == 1  # the leftover sweep after it cannot list either
    assert runner.words() == ["init", "apply", "cycle", "destroy", "destroy"]
    assert guard.deleted == ["dfe-e2e-runs/run-1/run.json"]
    err = capsys.readouterr().err
    assert "the network interface sweep could not finish: aws ec2 describe-network-interfaces failed" in err
    assert "TEARDOWN FAILED" not in err


def test_an_interface_ec2_will_not_delete_is_reported_and_the_destroy_still_runs_again(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    guard, runner = FakeGuard(), FakeRunner(codes={"destroy": [1, 0]})
    guard.sweep_code = [1, 0]
    assert _cycle(root, monkeypatch, guard, runner) == 0
    assert runner.words() == ["init", "apply", "cycle", "destroy", "destroy"]
    assert "network interfaces carrying dfe-e2e=run-1 are still there, named above" in capsys.readouterr().err


def test_a_run_that_created_nothing_sweeps_nothing(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard = FakeGuard()

    def refuse(*_args: object) -> None:
        raise cloud_sweep.CloudSweepError("aws s3api put-object failed: AccessDenied")

    guard.put_record = refuse  # type: ignore[method-assign]
    assert _cycle(root, monkeypatch, guard, FakeRunner()) == 1
    assert guard.sweeps == []


@pytest.mark.parametrize("code", [1, 2])
def test_expected_fail_a_sweep_that_leaves_anything_fails_the_run_and_names_the_sweep(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, code: int
) -> None:
    """The destroy succeeded, so the record goes; what is left keeps the run's tags for the reaper.
    Exit 2 stays the refusal that created nothing, so a refused sweep still fails the run with 1."""
    guard = FakeGuard()
    guard.sweep_code = code
    assert _cycle(root, monkeypatch, guard, FakeRunner()) == 1
    err = capsys.readouterr().err
    assert "TEARDOWN INCOMPLETE for run run-1" in err
    assert f"python3 scripts/cloud_sweep.py {' '.join(SWEEP_ARGS)}" in err
    assert guard.deleted == ["dfe-e2e-runs/run-1/run.json"]
    assert not (root / guard_mod.OVERLAY_NAME).exists()


def test_expected_fail_a_sweep_that_cannot_list_fails_the_run(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    guard = FakeGuard()
    guard.sweep_error = cloud_sweep.CloudSweepError(
        "aws resourcegroupstaggingapi get-resources did not answer within 60s"
    )
    assert _cycle(root, monkeypatch, guard, FakeRunner()) == 1
    err = capsys.readouterr().err
    assert "TEARDOWN INCOMPLETE for run run-1" in err
    assert "did not answer within 60s" in err


class FakeRegion:
    """The aws CLI for one region holding secrets, instances and launch templates, each with its tags.

    A forced secret delete leaves the secret listed as scheduled for one more listing,
    as the background purge does, and the tagging API goes on listing everything the
    region ever held, as it does after a delete. Every other call answers empty.
    """

    def __init__(self) -> None:
        self.secrets: dict[str, dict] = {}
        self.instances: dict[str, dict] = {}
        self.launch_templates: dict[str, dict] = {}
        self.ever_tagged: dict[str, list[dict[str, str]]] = {}
        self.purging: dict[str, int] = {}
        self.calls: list[list[str]] = []
        self.deleted: list[str] = []

    def add_secret(self, name: str, tags: dict[str, str]) -> str:
        arn = f"arn:aws:secretsmanager:us-west-2:{ACCOUNT}:secret:{name}-AbCdEf"
        tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
        self.secrets[arn] = {"ARN": arn, "Name": name, "Tags": tag_list}
        self.ever_tagged[arn] = tag_list
        return arn

    def add_instance(self, instance_id: str, tags: dict[str, str]) -> str:
        tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
        self.instances[instance_id] = {"InstanceId": instance_id, "State": {"Name": "running"}, "Tags": tag_list}
        self.ever_tagged[f"arn:aws:ec2:us-west-2:{ACCOUNT}:instance/{instance_id}"] = tag_list
        return instance_id

    def add_launch_template(self, template_id: str, name: str, tags: dict[str, str]) -> str:
        tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
        self.launch_templates[template_id] = {"LaunchTemplateId": template_id, "LaunchTemplateName": name, "Tags": tag_list}
        self.ever_tagged[f"arn:aws:ec2:us-west-2:{ACCOUNT}:launch-template/{template_id}"] = tag_list
        return template_id

    def _describe_launch_templates(self, args: list[str]) -> subprocess.CompletedProcess:
        if "--launch-template-ids" not in args:
            return _aws_ok({"LaunchTemplates": list(self.launch_templates.values())})
        named = args[args.index("--launch-template-ids") + 1]
        if named not in self.launch_templates:
            return _aws_fail(
                "An error occurred (InvalidLaunchTemplateId.NotFound) when calling the DescribeLaunchTemplates "
                f"operation: {named}"
            )
        return _aws_ok({"LaunchTemplates": [self.launch_templates[named]]})

    def _list_secrets(self) -> list[dict]:
        for arn in [a for a, left in self.purging.items() if left == 0]:
            del self.secrets[arn], self.purging[arn]
        listed = [dict(s) for s in self.secrets.values()]
        self.purging = {arn: left - 1 for arn, left in self.purging.items()}
        return listed

    def _tagged(self, args: list[str]) -> list[dict]:
        wanted = []
        for i, arg in enumerate(args):
            if arg == "--tag-filters":
                key, _, values = args[i + 1].partition(",Values=")
                wanted.append((key.removeprefix("Key="), values or None))
        mappings = []
        for arn, tags in self.ever_tagged.items():
            held = {t["Key"]: t["Value"] for t in tags}
            if all(k in held and (v is None or held[k] == v) for k, v in wanted):
                mappings.append({"ResourceARN": arn, "Tags": tags})
        return mappings

    def __call__(self, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append(args)
        if args[:2] == ["sts", "get-caller-identity"]:
            return _aws_ok({"Account": ACCOUNT})
        if args[:2] == ["secretsmanager", "list-secrets"]:
            return _aws_ok({"SecretList": self._list_secrets()})
        if args[:2] == ["secretsmanager", "delete-secret"]:
            arn = args[args.index("--secret-id") + 1]
            assert "--force-delete-without-recovery" in args, args
            self.deleted.append(arn)
            if arn in self.secrets:
                self.secrets[arn]["DeletedDate"] = "2026-10-08T12:00:00+00:00"
                self.purging[arn] = 1
            return _aws_ok({"ARN": arn})
        if args[:2] == ["ec2", "terminate-instances"]:
            instance_id = args[args.index("--instance-ids") + 1]
            self.deleted.append(instance_id)
            self.instances[instance_id]["State"] = {"Name": "terminated"}
            return _aws_ok({})
        if args[:2] == ["ec2", "describe-instances"]:
            named = args[args.index("--instance-ids") + 1] if "--instance-ids" in args else None
            found = [i for i in self.instances.values() if named in (None, i["InstanceId"])]
            return _aws_ok({"Reservations": [{"Instances": found}]})
        if args[:2] == ["ec2", "describe-launch-templates"]:
            return self._describe_launch_templates(args)
        if args[:2] == ["ec2", "delete-launch-template"]:
            template_id = args[args.index("--launch-template-id") + 1]
            self.deleted.append(template_id)
            del self.launch_templates[template_id]
            return _aws_ok({})
        if args[:2] == ["resourcegroupstaggingapi", "get-resources"]:
            return _aws_ok({"ResourceTagMappingList": self._tagged(args)})
        return _aws_ok({})


@pytest.fixture
def region(monkeypatch: pytest.MonkeyPatch) -> FakeRegion:
    fake = FakeRegion()
    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake)
    monkeypatch.setattr(cloud_sweep.time, "sleep", lambda _seconds: None)
    return fake


def test_the_teardown_sweep_deletes_this_runs_resources_and_never_touches_another_runs(
    root: Path, region: FakeRegion
) -> None:
    mine = cloud_run.run_tags("run-1", RUN_EXPIRES_AT)
    crt = region.add_secret("dfe/test/pki/internal-ca-crt", mine)
    key = region.add_secret("dfe/test/pki/internal-ca-key", mine)
    node = region.add_instance("i-0run1", mine)
    others = [
        region.add_secret("dfe/other/pki/internal-ca-crt", cloud_run.run_tags("run-2", RUN_EXPIRES_AT)),
        # A longer id that starts with this one's, which a prefix match would take.
        region.add_secret("dfe/longer/pki/internal-ca-crt", cloud_run.run_tags("run-10", RUN_EXPIRES_AT)),
        region.add_secret("dfe/shared/db", {"service-name": "dfe", "environment": "test"}),
        region.add_instance("i-0run2", cloud_run.run_tags("run-2", RUN_EXPIRES_AT)),
    ]
    config = _config(root)
    assert guard_mod.AwsGuard(config.region).sweep([*guard_mod.run_sweep_args(config, "run-1"), "--yes"]) == 0
    assert sorted(region.deleted) == sorted([crt, key, node])
    named = [arg for call in region.calls for arg in call]
    assert [other for other in others if other in named] == []
    assert {crt, key}.isdisjoint(region.secrets)
    assert set(others[:3]) <= set(region.secrets)
    assert region.instances["i-0run2"]["State"] == {"Name": "running"}


class PipeOut:
    """A stdout that, like a pipe, keeps what is written until it is flushed."""

    def __init__(self) -> None:
        self.held: list[str] = []
        self.flushed: list[str] = []

    def write(self, text: str) -> int:
        self.held.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushed.extend(self.held)
        self.held.clear()


@pytest.mark.parametrize("fails", [False, True])
def test_the_sweeps_own_lines_are_out_before_the_teardown_writes_the_next(
    monkeypatch: pytest.MonkeyPatch, fails: bool,
) -> None:
    """Held until exit, they printed after TEARDOWN FAILED and read as interfaces left behind."""
    out = PipeOut()
    monkeypatch.setattr(sys, "stdout", out)

    def sweep(_args: list[str]) -> int:
        print("deleted eni eni-0run1")
        if fails:
            raise cloud_sweep.CloudSweepError("listing failed")
        return 0

    monkeypatch.setattr(cloud_sweep, "main", sweep)
    guard = guard_mod.AwsGuard("us-west-2")
    if fails:
        with pytest.raises(cloud_sweep.CloudSweepError):
            guard.sweep([])
    else:
        assert guard.sweep([]) == 0
    assert out.held == []
    assert "deleted eni eni-0run1" in "".join(out.flushed)


def test_after_the_teardown_the_next_preflight_finds_nothing_of_the_run(
    root: Path, monkeypatch: pytest.MonkeyPatch, region: FakeRegion
) -> None:
    """ESO pushed the CA secrets with the run's tags, tofu destroy left them, and the next run's
    (d) refused on them until the in-cloud sweeper's grace ran out."""
    pushed: list[str] = []

    def eso_pushes() -> None:
        tags = cloud_run.run_tags("run-1", RUN_EXPIRES_AT)
        pushed.append(region.add_secret("dfe/test/pki/internal-ca-crt", tags))
        pushed.append(region.add_secret("dfe/test/pki/internal-ca-key", tags))

    assert _cycle(root, monkeypatch, AwsRegionGuard(), FakeRunner(on_run={"cycle": eso_pushes})) == 0
    report = guard_mod.run_preflight(_config(root), AwsRegionGuard(), now=NOW, env={})
    assert report.ok, [c.detail for c in report.checks if not c.ok]
    assert sorted(region.deleted) == sorted(pushed)


KARPENTER_LT_NAME = "karpenter.k8s.aws/1234567890123456789"


def test_a_launch_template_karpenter_left_is_swept_so_the_next_preflight_finds_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch, region: FakeRegion
) -> None:
    """Karpenter puts the run's tags on its launch templates, and a teardown that removes Karpenter
    before its EC2NodeClasses finish deleting leaves them behind."""
    left: list[str] = []

    def karpenter_launches() -> None:
        tags = {**cloud_run.run_tags("run-1", RUN_EXPIRES_AT), "karpenter.k8s.aws/ec2nodeclass": "general"}
        left.append(region.add_launch_template("lt-0run1", KARPENTER_LT_NAME, tags))

    assert _cycle(root, monkeypatch, AwsRegionGuard(), FakeRunner(on_run={"cycle": karpenter_launches})) == 0
    report = guard_mod.run_preflight(_config(root), AwsRegionGuard(), now=NOW, env={})
    assert report.ok, [c.detail for c in report.checks if not c.ok]
    assert region.deleted == left


def test_refusal_c_names_an_expired_launch_template_by_the_class_its_sweep_deletes(
    root: Path, region: FakeRegion
) -> None:
    """(c) hands the operator a sweep command, so what it names has to be something that sweep removes."""
    region.add_launch_template("lt-0prev", KARPENTER_LT_NAME, cloud_run.run_tags("r-prev", int(NOW) - 60))
    report = _preflight(root, AwsRegionGuard())
    assert _failed(report) == ["(c) expired run resources"]
    assert f"launch-template {KARPENTER_LT_NAME}" in _check(report, "(c) expired run resources").detail


def test_refusal_d_a_run_secret_only_scheduled_for_deletion_still_holds_its_name(
    root: Path, region: FakeRegion
) -> None:
    """A secret pending deletion keeps its name, so the next run's push of that name would fail."""
    arn = region.add_secret("dfe/test/pki/internal-ca-crt", cloud_run.run_tags("r-prev", int(NOW) + 3600))
    region.secrets[arn]["DeletedDate"] = "2026-10-08T11:00:00+00:00"
    report = _preflight(root, AwsRegionGuard())
    assert _failed(report) == ["(d) unfinished runs"]
    detail = _check(report, "(d) unfinished runs").detail
    assert "secret dfe/test/pki/internal-ca-crt (until 2026-10-08T13:00:00Z)" in detail


@pytest.mark.parametrize("reserved", ["--keep", "--kubeconfig=x", "--from-terraform", "--ke", "--kubec=x", "--from"])
def test_expected_fail_cycle_args_that_skip_the_destroy_or_clash_are_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch, reserved: str
) -> None:
    """argparse expands an unambiguous prefix, so `--ke` reaches dfe-ops cycle as --keep."""
    runner = FakeRunner()
    code = _cycle(root, monkeypatch, FakeGuard(), runner, cycle_args=["--", "--mode", "single", reserved])
    assert code == 2
    assert runner.commands == []


@pytest.mark.parametrize("given", [
    ["-keep"],                         # one dash: no such option
    ["--require-label"],               # dangling, no value
    ["--e2e", "--"],                   # a stray `--` turns the appended --mode into a positional
    ["--", "--mode", "single"],        # everything after a stray `--` is a positional
    ["--mode"],
    ["--mode", "bogus"],
    ["-h"],                            # the parser would print help and exit, running nothing
])
def test_expected_fail_what_the_cycles_own_parser_rejects_is_refused_before_the_apply(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, given: list[str]
) -> None:
    """Each of these would otherwise exit 2 from dfe-ops cycle after the cluster was paid for."""
    guard, runner = FakeGuard(), FakeRunner()
    assert _cycle(root, monkeypatch, guard, runner, cycle_args=["--", *given]) == 2
    assert runner.commands == []
    assert guard.records == {}
    assert not (root / guard_mod.OVERLAY_NAME).exists()
    assert "dfe-ops cycle would" in capsys.readouterr().err


def test_the_cycle_runs_the_exact_arguments_its_parser_accepted(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FakeRunner()
    assert _cycle(root, monkeypatch, FakeGuard(), runner, cycle_args=["--", "--e2e", "--env-file", "x.env"]) == 0
    argv = runner.commands[2][runner.commands[2].index("cycle") + 1:]
    assert guard_mod._parse_cycle(argv).mode == "single"
    assert argv[:2] == ["--from-terraform", str(root)]
    assert argv[2] == "--kubeconfig"
    assert argv[4:] == ["--e2e", "--env-file", "x.env", "--mode", "single"]


def test_the_acceptance_stage_flag_passes_the_cycles_own_parser(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workflow adds --acceptance-suite source to every run, so a parser that
    refused it would refuse every cloud cycle before the apply."""
    runner = FakeRunner()
    given = ["--acceptance-suite", "source", "--env-file", "x.env"]
    assert _cycle(root, monkeypatch, FakeGuard(), runner, cycle_args=["--", *given]) == 0
    argv = runner.commands[2][runner.commands[2].index("cycle") + 1:]
    assert argv[4:] == [*given, "--mode", "single"]
    assert guard_mod._parse_cycle(argv).acceptance_suite == "source"


# --- the edge suite needs the address it sends from before anything is created -----------------

INGEST_ALLOW_VAR = "DFE_RUN_INGEST_ALLOW_CIDR"
# A documentation range only: this repository is public.
INGEST_SENDER = "203.0.113.7/32"


def _ingest_allow(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(INGEST_ALLOW_VAR, raising=False)
    else:
        monkeypatch.setenv(INGEST_ALLOW_VAR, value)


@pytest.mark.parametrize(("value", "why"), [
    (None, "is unset"),
    ("", "is unset"),
    ("   ", "is unset"),
    ("0.0.0.0/0", "every address"),
    ("::/0", "every address"),
    ("not-an-address", "is not a network"),
])
def test_expected_fail_an_edge_cycle_without_a_usable_send_address_creates_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    value: str | None, why: str
) -> None:
    """Found missing after the apply, it costs the cluster the apply built."""
    _ingest_allow(monkeypatch, value)
    guard, runner = FakeGuard(), FakeRunner()
    assert _cycle(root, monkeypatch, guard, runner, cycle_args=["--", "--acceptance-suite", "edge"]) == 2
    printed = capsys.readouterr().err
    assert printed.startswith("cloud-cycle refused: the edge acceptance stage cannot run: ")
    assert INGEST_ALLOW_VAR in printed
    assert why in printed
    assert "PASS" not in printed
    assert runner.commands == []
    assert guard.records == {}
    assert not (root / guard_mod.OVERLAY_NAME).exists()


def test_an_edge_cycle_with_a_send_address_in_the_environment_runs(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ingest_allow(monkeypatch, INGEST_SENDER)
    runner = FakeRunner()
    assert _cycle(root, monkeypatch, FakeGuard(), runner, cycle_args=["--", "--acceptance-suite", "edge"]) == 0
    assert runner.words() == ["init", "apply", "cycle", "destroy"]


def test_an_edge_cycle_reads_the_send_address_from_the_env_file_it_passes(
    root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ingest_allow(monkeypatch, None)
    env_file = tmp_path / "run.env"
    env_file.write_text(f"{INGEST_ALLOW_VAR}={INGEST_SENDER}\n", encoding="utf-8")
    runner = FakeRunner()
    given = ["--acceptance-suite", "edge", "--env-file", str(env_file)]
    assert _cycle(root, monkeypatch, FakeGuard(), runner, cycle_args=["--", *given]) == 0
    assert runner.words() == ["init", "apply", "cycle", "destroy"]


def test_expected_fail_an_edge_cycle_whose_env_file_cannot_be_read_creates_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    _ingest_allow(monkeypatch, INGEST_SENDER)
    runner = FakeRunner()
    given = ["--acceptance-suite", "edge", "--env-file", str(tmp_path / "absent.env")]
    assert _cycle(root, monkeypatch, FakeGuard(), runner, cycle_args=["--", *given]) == 2
    assert "the edge acceptance stage cannot run" in capsys.readouterr().err
    assert runner.commands == []


@pytest.mark.parametrize("given", [["--acceptance-suite", "source"], ["--acceptance-suite", "all"]])
def test_a_cycle_on_another_suite_never_asks_for_the_send_address(
    root: Path, monkeypatch: pytest.MonkeyPatch, given: list[str]
) -> None:
    _ingest_allow(monkeypatch, None)
    runner = FakeRunner()
    assert _cycle(root, monkeypatch, FakeGuard(), runner, cycle_args=["--", *given]) == 0
    assert runner.words() == ["init", "apply", "cycle", "destroy"]


# --- the cycle's mode is the dial's profile -------------------------------------------------------


def test_with_no_mode_given_the_cycle_runs_in_the_dials_profile(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FakeRunner()
    assert _cycle(root, monkeypatch, FakeGuard(), runner, cycle_args=["--", "--env-file", "x.env"]) == 0
    cycle = runner.commands[2]
    assert cycle[-4:] == ["--env-file", "x.env", "--mode", "single"]
    assert cycle.count("--mode") == 1


@pytest.mark.parametrize("given", [["--mode", "single"], ["--mode=single"], ["--mod", "single"]])
def test_a_mode_that_matches_the_dial_passes_through_once(
    root: Path, monkeypatch: pytest.MonkeyPatch, given: list[str]
) -> None:
    runner = FakeRunner()
    assert _cycle(root, monkeypatch, FakeGuard(), runner, cycle_args=["--", *given]) == 0
    assert runner.commands[2][-len(given):] == given
    assert "--mode" not in runner.commands[2][:-len(given)]


@pytest.mark.parametrize("given", [
    ["--mode", "scale"],
    ["--mode=scale"],
    ["--mo", "scale"],
    ["--mode", "single", "--mode", "scale"],
])
def test_expected_fail_a_mode_that_is_not_the_dials_profile_creates_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, given: list[str]
) -> None:
    guard, runner = FakeGuard(), FakeRunner()
    assert _cycle(root, monkeypatch, guard, runner, cycle_args=["--", *given]) == 2
    assert runner.commands == []
    assert guard.records == {}
    assert not (root / guard_mod.OVERLAY_NAME).exists()
    assert "one dial, one profile" in capsys.readouterr().err


def test_expected_fail_a_dial_profile_the_cycle_has_no_mode_for_creates_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Only the composed command carries the dial's profile, so only its parse sees this."""
    (root / "dial.auto.tfvars.json").write_text(json.dumps({**TFVARS, "profile": "docker-single"}), encoding="utf-8")
    guard, runner = FakeGuard(), FakeRunner()
    assert _cycle(root, monkeypatch, guard, runner, cycle_args=["--"]) == 2
    assert runner.commands == []
    assert guard.records == {}
    assert "invalid choice: 'docker-single'" in capsys.readouterr().err


def test_expected_fail_a_dial_with_no_profile_has_no_mode_and_creates_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tfvars = {k: v for k, v in TFVARS.items() if k != "profile"}
    (root / "dial.auto.tfvars.json").write_text(json.dumps(tfvars), encoding="utf-8")
    guard, runner = FakeGuard(), FakeRunner()
    assert _cycle(root, monkeypatch, guard, runner, cycle_args=["--"]) == 2
    assert runner.commands == []
    assert guard.records == {}


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


def _gone(pid: int, within: float = 5.0) -> bool:
    """Whether `pid` has exited inside `within` seconds; a zombie awaiting its reaper counts as gone."""
    give_up = time.monotonic() + within
    stat = Path(f"/proc/{pid}/stat")
    while time.monotonic() < give_up:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if stat.is_file() and stat.read_text(encoding="utf-8").rsplit(")", 1)[-1].split()[0] == "Z":
            return True
        time.sleep(0.1)
    return False


# The child starts a grandchild, writes its pid, and outlives any deadline a test sets.
SPAWNS_A_GRANDCHILD = (
    "import pathlib, subprocess, sys, time\n"
    "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "pathlib.Path(sys.argv[1]).write_text(str(grandchild.pid), encoding='utf-8')\n"
    "time.sleep(60)\n"
)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_the_runner_stops_a_child_at_its_deadline_with_its_whole_tree(tmp_path: Path) -> None:
    """The cycle's stages are its children, so stopping only the cycle would leave
    a destroy.sh and its kubectl running beside the guard's own teardown."""
    pid_file = tmp_path / "grandchild.pid"
    runner = guard_mod.Runner()
    started = time.monotonic()
    with pytest.raises(guard_mod.DeadlineError):
        runner.run([sys.executable, "-c", SPAWNS_A_GRANDCHILD, str(pid_file)], new_session=True,
                   deadline=time.monotonic() + 2, stop_timeout=10)
    assert time.monotonic() - started < 15
    assert runner.current is None
    assert _gone(int(pid_file.read_text(encoding="utf-8")))


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process groups are POSIX")
def test_a_child_that_ignores_sigterm_is_killed_once_its_stop_timeout_passes(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    ignores = (
        "import pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8')\n"
        "time.sleep(60)\n"
    )
    runner = guard_mod.Runner()
    started = time.monotonic()
    with pytest.raises(guard_mod.DeadlineError):
        runner.run([sys.executable, "-c", ignores, str(ready)], new_session=True,
                   deadline=time.monotonic() + 2, stop_timeout=1)
    assert ready.exists()
    assert time.monotonic() - started < 10


def test_a_child_that_ends_before_its_deadline_returns_its_own_code() -> None:
    runner = guard_mod.Runner()
    code = runner.run([sys.executable, "-c", "raise SystemExit(3)"], new_session=True,
                      deadline=time.monotonic() + 30)
    assert code == 3
    assert (runner.current, runner.group) == (None, None)
