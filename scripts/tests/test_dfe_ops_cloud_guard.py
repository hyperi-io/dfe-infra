#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_cloud_guard.py
#  Purpose:      Guard `dfe-ops cloud-preflight` and `cloud-cycle`: each of the
#                four refusals fires on its own cause and nothing is created
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

    def write_kubeconfig(self, cluster: str, path: Path) -> None:
        if self.kubeconfig_error:
            raise self.kubeconfig_error
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("apiVersion: v1\n", encoding="utf-8")


class FakeRunner:
    """Records each command; `codes` maps a word in the command to its exit code."""

    def __init__(self, codes: dict[str, int] | None = None, on_run: dict | None = None) -> None:
        self.commands: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.codes = codes or {}
        self.on_run = on_run or {}
        self.current = None

    def run(self, cmd: list[str], *, env: dict | None = None, new_session: bool = False) -> int:
        self.commands.append(cmd)
        self.envs.append(env)
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
        "(c) expired run resources", "(d) unfinished runs",
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
                "endpoint", "edge_allowed_cidrs"} & set(overlay)


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


def test_an_endpoint_cidr_fences_the_public_gateway_to_that_address_in_place_of_the_dials(
    root: Path,
) -> None:
    """The gateway's load balancer is public too, and a run's console test reaches it
    from the runner; the root adds the cluster's own NAT addresses beside it."""
    (root / "dial.auto.tfvars.json").write_text(
        json.dumps({**TFVARS, "edge_allowed_cidrs": ["52.0.0.0/8"]}), encoding="utf-8"
    )
    overlay = guard_mod.build_overlay(_config(root, endpoint_cidr=RUNNER_CIDR), "run-1", int(NOW))
    assert overlay["edge_allowed_cidrs"] == [RUNNER_CIDR]


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
        at_apply.append((overlay["edge_allowed_cidrs"], record["tfvars"]["edge_allowed_cidrs"]))

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
