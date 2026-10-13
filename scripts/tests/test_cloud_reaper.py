#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_cloud_reaper.py
#  Purpose:      Guard the scheduled reaper: unconfigured it is a notice and a
#                green run that touches nothing, configured it destroys only
#                expired runs whose record points at their own state, and a
#                run holding a SaaS broker is destroyed with that vendor's
#                credential or not at all.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/cloud_reaper.py.

    python3 -m pytest scripts/tests/test_cloud_reaper.py -q

The S3 calls go to a fake `aws_cli.run_aws` and `tofu` to a fake `run_tofu`.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import cloud_reaper  # noqa: E402
import cloud_run  # noqa: E402
import cloud_sweep  # noqa: E402

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


def _reap(
    monkeypatch: pytest.MonkeyPatch, bucket: FakeBucket, tofu_code: int = 0, state: dict | None = None
) -> tuple[list[str], list[tuple]]:
    destroyed: list[tuple] = []

    def fake_tofu(args: list[str], cwd: Path, env: dict) -> int:
        destroyed.append((tuple(args), cwd, json.loads((cwd / cloud_reaper.OVERLAY_NAME).read_text())))
        return tofu_code

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", bucket.run_aws)
    monkeypatch.setattr(cloud_reaper, "run_tofu", fake_tofu)
    monkeypatch.setattr(cloud_reaper, "state_json", lambda cwd, env: state or {})
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


EMPTY_STATE = {"format_version": "1.0"}
VPC_LEFT = {"values": {"root_module": {"child_modules": [{"resources": [
    {"address": "module.cluster.aws_vpc.this", "mode": "managed", "type": "aws_vpc", "values": {"id": "vpc-0abc"}},
]}]}}}
DATA_ONLY = {"values": {"root_module": {"resources": [
    {"address": "data.aws_caller_identity.current", "mode": "data", "type": "aws_caller_identity", "values": {}},
]}}}


def _reap_after_failed_destroy(
    monkeypatch: pytest.MonkeyPatch, after_refresh: dict, refresh_code: int = 0
) -> tuple[list[str], list[tuple], FakeBucket]:
    """A destroy that exits 1, then a refresh-only apply exiting `refresh_code` that leaves `after_refresh`."""
    state = cloud_run.state_key("dfe-e2e-runs", "run-old")
    bucket = FakeBucket({"run-old": _record("run-old", NOW - 60)}, {state})
    answers = iter([EMPTY_STATE, after_refresh])
    monkeypatch.setattr(cloud_reaper, "state_json", lambda cwd, env: next(answers))
    destroyed: list[tuple] = []

    def fake_tofu(args: list[str], cwd: Path, env: dict) -> int:
        destroyed.append(tuple(args))
        return {"destroy": 1, "apply": refresh_code}.get(args[0], 0)

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", bucket.run_aws)
    monkeypatch.setattr(cloud_reaper, "run_tofu", fake_tofu)
    config = cloud_reaper.load_config(ENV)
    assert config is not None
    return cloud_reaper.reap_runs(config, NOW), destroyed, bucket


def test_a_destroy_that_loses_the_last_resource_to_the_sweep_still_reaps(monkeypatch: pytest.MonkeyPatch) -> None:
    problems, calls, bucket = _reap_after_failed_destroy(monkeypatch, EMPTY_STATE)
    assert problems == []
    assert [c[0] for c in calls] == ["init", "destroy", "apply"]
    assert calls[-1] == ("apply", "-refresh-only", "-auto-approve", "-input=false")
    assert bucket.deleted == ["dfe-e2e-runs/run-old/run.json"]


def test_data_sources_left_in_the_state_count_as_nothing_left(monkeypatch: pytest.MonkeyPatch) -> None:
    problems, _calls, bucket = _reap_after_failed_destroy(monkeypatch, DATA_ONLY)
    assert problems == []
    assert bucket.deleted == ["dfe-e2e-runs/run-old/run.json"]


def test_expected_fail_a_failed_destroy_with_a_resource_left_keeps_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    problems, _calls, bucket = _reap_after_failed_destroy(monkeypatch, VPC_LEFT)
    assert bucket.deleted == []
    assert any("tofu destroy failed" in p for p in problems)


def test_expected_fail_a_refresh_that_fails_keeps_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    problems, _calls, bucket = _reap_after_failed_destroy(monkeypatch, EMPTY_STATE, refresh_code=1)
    assert bucket.deleted == []
    assert any("tofu destroy failed" in p for p in problems)


def test_expected_fail_an_unreadable_state_after_the_refresh_keeps_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    problems, _calls, bucket = _reap_after_failed_destroy(monkeypatch, {})
    assert bucket.deleted == []
    assert any("tofu destroy failed" in p for p in problems)


SECRET_GONE = "arn:aws:secretsmanager:us-west-2:000000000000:secret:dfe/test/ui/nextauth-AbCdEf"
SECRET_LIVE = "arn:aws:secretsmanager:us-west-2:000000000000:secret:dfe/test/kafka/msk-AbCdEf"


def _state_with_secrets() -> dict:
    """A `tofu show -json` body: one secret removed outside tofu, one still held, in a child module."""
    def secret(address: str, arn: str) -> list[dict]:
        return [
            {"address": address, "type": "aws_secretsmanager_secret", "values": {"arn": arn}},
            {"address": f"{address}_version", "type": "aws_secretsmanager_secret_version",
             "values": {"secret_id": arn, "secret_string": "never-printed"}},
        ]

    return {"values": {"root_module": {
        "resources": [{"address": "aws_s3_bucket.logs", "type": "aws_s3_bucket", "values": {"arn": "arn:aws:s3:::x"}}],
        "child_modules": [{"resources": [
            *secret("module.secrets.aws_secretsmanager_secret.nextauth", SECRET_GONE),
            *secret("module.secrets.aws_secretsmanager_secret.kafka", SECRET_LIVE),
        ]}],
    }}}


class FakeBucketAndSecrets(FakeBucket):
    """The state bucket, plus DescribeSecret answering NotFound for SECRET_GONE alone."""

    def run_aws(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if args[:2] == ["secretsmanager", "describe-secret"]:
            arn = args[args.index("--secret-id") + 1]
            if arn == SECRET_GONE:
                return _fail("An error occurred (ResourceNotFoundException) when calling the DescribeSecret operation")
            return _ok({"ARN": arn})
        return super().run_aws(args, **kwargs)


def test_a_secret_removed_outside_tofu_leaves_the_state_before_the_destroy(monkeypatch: pytest.MonkeyPatch) -> None:
    state = cloud_run.state_key("dfe-e2e-runs", "run-old")
    bucket = FakeBucketAndSecrets({"run-old": _record("run-old", NOW - 60)}, {state})
    problems, calls = _reap(monkeypatch, bucket, state=_state_with_secrets())
    assert problems == []
    assert [c[0] for c in calls] == [
        ("init", "-input=false", "-reconfigure"),
        ("state", "rm", "module.secrets.aws_secretsmanager_secret.nextauth"),
        ("state", "rm", "module.secrets.aws_secretsmanager_secret.nextauth_version"),
        ("destroy", "-auto-approve", "-input=false"),
    ]
    assert bucket.deleted == ["dfe-e2e-runs/run-old/run.json"]


def test_a_secret_scheduled_for_deletion_counts_as_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", lambda args, **_k: _ok({"DeletedDate": "2026-10-10T08:00:00Z"}))
    addresses = cloud_reaper.stale_state_addresses(_state_with_secrets())
    assert len(addresses) == 4


def test_a_secret_whose_state_cannot_be_read_is_never_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", lambda args, **_k: _fail("An error occurred (AccessDeniedException)"))
    with pytest.raises(cloud_sweep.CloudSweepError, match="AccessDenied"):
        cloud_reaper.stale_state_addresses(_state_with_secrets())


class FakeBucketDescribeDenied(FakeBucket):
    def run_aws(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if args[:2] == ["secretsmanager", "describe-secret"]:
            return _fail("An error occurred (AccessDeniedException) when calling the DescribeSecret operation")
        return super().run_aws(args, **kwargs)


def test_an_unreadable_secret_leaves_the_state_alone_and_still_destroys(monkeypatch: pytest.MonkeyPatch) -> None:
    state = cloud_run.state_key("dfe-e2e-runs", "run-old")
    bucket = FakeBucketDescribeDenied({"run-old": _record("run-old", NOW - 60)}, {state})
    problems, calls = _reap(monkeypatch, bucket, state=_state_with_secrets())
    assert problems == []
    assert [c[0][0] for c in calls] == ["init", "destroy"]


def test_no_state_answer_prunes_nothing() -> None:
    assert cloud_reaper.stale_state_addresses({}) == []


# --- vendor credentials ------------------------------------------------------------------

VENDOR_ENV = ("CONFLUENT_CLOUD_API_KEY", "CONFLUENT_CLOUD_API_SECRET", "REDPANDA_CLIENT_ID", "REDPANDA_CLIENT_SECRET")


def _saas_record(run_id: str, provider: str) -> dict:
    """An expired run record whose tfvars select one Kafka provider."""
    record = _record(run_id, NOW - 60)
    record["tfvars"]["kafka"] = {"provider": provider}
    return record


def _reap_recording_env(
    monkeypatch: pytest.MonkeyPatch, record: dict, env: dict[str, str]
) -> tuple[list[str], list[dict], FakeBucket]:
    """Reap one run with only `env` set of the vendor variables, keeping each tofu call's vendor variables.

    Only the four vendor names are kept, never the whole environment, so a failed
    assertion cannot print the host's or the runner's other variables.
    """
    for name in VENDOR_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    run_id = str(record["run_id"])
    bucket = FakeBucket({run_id: record}, {cloud_run.state_key("dfe-e2e-runs", run_id)})
    seen: list[dict] = []

    def fake_tofu(args: list[str], cwd: Path, tofu_env: dict) -> int:
        seen.append({name: tofu_env[name] for name in VENDOR_ENV if name in tofu_env})
        return 0

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", bucket.run_aws)
    monkeypatch.setattr(cloud_reaper, "run_tofu", fake_tofu)
    monkeypatch.setattr(cloud_reaper, "state_json", lambda cwd, tofu_env: {})
    config = cloud_reaper.load_config(ENV)
    assert config is not None
    return cloud_reaper.reap_runs(config, NOW), seen, bucket


def test_a_run_holding_confluent_resources_destroys_with_the_runners_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    problems, seen, bucket = _reap_recording_env(
        monkeypatch,
        _saas_record("run-cc", "confluent-cloud"),
        {"CONFLUENT_CLOUD_API_KEY": "example-key", "CONFLUENT_CLOUD_API_SECRET": "example-secret"},
    )
    assert problems == []
    assert seen
    assert all(env["CONFLUENT_CLOUD_API_KEY"] == "example-key" for env in seen)
    assert all(env["CONFLUENT_CLOUD_API_SECRET"] == "example-secret" for env in seen)
    assert bucket.deleted == ["dfe-e2e-runs/run-cc/run.json"]


@pytest.mark.parametrize(
    ("provider", "env", "missing"),
    [
        ("confluent-cloud", {}, ["CONFLUENT_CLOUD_API_KEY", "CONFLUENT_CLOUD_API_SECRET"]),
        ("confluent-cloud", {"CONFLUENT_CLOUD_API_KEY": "example-key"}, ["CONFLUENT_CLOUD_API_SECRET"]),
        ("redpanda-cloud", {}, ["REDPANDA_CLIENT_ID", "REDPANDA_CLIENT_SECRET"]),
        ("redpanda-cloud", {"REDPANDA_CLIENT_ID": "", "REDPANDA_CLIENT_SECRET": "example-secret"}, ["REDPANDA_CLIENT_ID"]),
    ],
)
def test_expected_fail_a_run_holding_saas_resources_without_the_credential_is_left_for_the_next_schedule(
    monkeypatch: pytest.MonkeyPatch, provider: str, env: dict[str, str], missing: list[str]
) -> None:
    """With no vendor credential tofu cannot reach the broker; the reaper names what is missing instead of running it."""
    problems, seen, bucket = _reap_recording_env(monkeypatch, _saas_record("run-saas", provider), env)
    assert seen == []
    assert bucket.deleted == []
    assert len(problems) == 1
    assert provider in problems[0]
    assert all(name in problems[0] for name in missing)
    assert "unused-placeholder" not in problems[0]


def test_a_run_holding_redpanda_resources_destroys_with_the_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    problems, seen, bucket = _reap_recording_env(
        monkeypatch,
        _saas_record("run-rp", "redpanda-cloud"),
        {"REDPANDA_CLIENT_ID": "example-client", "REDPANDA_CLIENT_SECRET": "example-secret"},
    )
    assert problems == []
    assert seen
    assert all(env["REDPANDA_CLIENT_ID"] == "example-client" for env in seen)
    assert all(env["REDPANDA_CLIENT_SECRET"] == "example-secret" for env in seen)
    assert bucket.deleted == ["dfe-e2e-runs/run-rp/run.json"]


@pytest.mark.parametrize("blank", [{}, {"REDPANDA_CLIENT_ID": "", "REDPANDA_CLIENT_SECRET": ""}])
def test_a_run_holding_no_saas_broker_still_configures_the_redpanda_provider(
    monkeypatch: pytest.MonkeyPatch, blank: dict[str, str]
) -> None:
    """The root configures the redpanda provider on every destroy, and a runner secret that is unset arrives blank."""
    problems, seen, bucket = _reap_recording_env(monkeypatch, _saas_record("run-msk", "msk"), blank)
    assert problems == []
    assert seen
    assert all(env["REDPANDA_CLIENT_ID"] and env["REDPANDA_CLIENT_SECRET"] for env in seen)
    assert all("CONFLUENT_CLOUD_API_KEY" not in env for env in seen)
    assert bucket.deleted == ["dfe-e2e-runs/run-msk/run.json"]


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
