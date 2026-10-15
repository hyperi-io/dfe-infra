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
import re
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


# --- a record older than the root it is destroyed with ----------------------------------

AWS_VARIABLES = REPO_ROOT / "terraform" / "environments" / "aws" / "variables.tf"
_VARIABLE_BLOCK = re.compile(r'^variable\s+"([a-z_]+)"\s*\{(.*?)^\}', re.MULTILINE | re.DOTALL)
PRE_SWEEP_RUN = "r20261014t230516z-0a0a0a"


def _required_variables() -> set[str]:
    """Every variable the aws root declares with no default, so a tfvars file must set it."""
    blocks = _VARIABLE_BLOCK.findall(AWS_VARIABLES.read_text(encoding="utf-8"))
    return {name for name, body in blocks if not re.search(r"^  default\s*=", body, re.MULTILINE)}


def _pre_sweep_record(run_id: str = PRE_SWEEP_RUN, **tfvars_overrides: object) -> dict:
    """An expired redpanda-cloud run recorded before the aws root required its sizing inputs.

    The key set is a real record's from that time; the values are the reserved example
    domain, RFC 5737 addresses and the zero account.
    """
    expires_at = NOW - 60
    state = {"bucket": "example-state", "region": "us-west-2", "key": cloud_run.state_key("dfe-e2e-runs", run_id)}
    pool = {"min_size": 1, "max_size": 1, "desired_size": 1, "capacity_type": "ON_DEMAND", "disk_gb": 40}
    tfvars: dict[str, object] = {
        "provision": {"cloud": "aws", "account": "000000000000", "region": "us-west-2", "cidr": "10.90.0.0/16"},
        "name": "dfe-example",
        "env": "test",
        "profile": "scale",
        "kubernetes_version": "1.36",
        "network": {"nat": "single", "az_count": 3},
        "endpoint": {"public": True, "allowed_cidrs": ["198.51.100.10/32"]},
        "dns": {"private_zone": "dfe-example.internal", "public_zone": ""},
        "telemetry": {"sink": "otel", "retention_days": 2},
        "storage_class": "gp3",
        "kafka": {
            "provider": "redpanda-cloud",
            "num_partitions": 12,
            "log_retention_ms": 259200000,
            "message_max_bytes": 16777216,
            "landing_topics": {"main_land": {}},
            "connectivity": "public",
            "redpanda_resource_group_id": "00000000-0000-4000-8000-000000000000",
        },
        "secrets": {"backend": "aws-sm", "ref": ""},
        "clickhouse": {"mode": "in_cluster"},
        "seeds": {"kafka/redpanda-cloud": {"password": ""}, "ui/nextauth": {"secret": ""}},
        "endpoints": {
            "clickhouse_host": "dfe-clickhouse.clickhouse.svc.cluster.local",
            "kafka_bootstrap": "",
            "otel_endpoint": "otel-collector-gateway.otel.svc.cluster.local:4317",
        },
        "repo_url": "https://github.com/example/dfe-infra.git",
        "target_revision": "main",
        "state": state,
        "tags": {
            "service-name": "dfe",
            "service-namespace": "example",
            "environment": "test",
            "owner": "owner@example.com",
            "cost-center": "experiments",
            "lifecycle": "ephemeral",
            "iac-source": "dfe-infra/terraform/environments/aws",
        },
        "toolbox": {
            "enabled": False,
            "aws": {"instance_type": "t4g.small", "operator_role_arn": ""},
            "ttl_minutes": 60,
            "session": {"idle_timeout_minutes": 15, "max_duration_minutes": 240},
            "session_log_retention_days": 90,
            "tool_versions": {"kubectl": "1.36.0"},
        },
        "edge": {
            "enabled": True,
            "tunnel": {
                "address": {"mode": "byo", "instance_type": "t4g.small", "zone": ""},
                "openvpn": True,
                "source_ranges": [],
                "node_ports": {"wireguard": 31820, "openvpn": 31194},
                "admin_peer": {"enabled": True, "reach": [22, 443]},
            },
            "receiver": {"eips": True},
        },
        "edge_trusted_proxy_cidrs": [],
        "node_pools": {
            "system": {"shape_ref": "eks-system", **pool, "labels": {}, "taints": []},
            "clickhouse": {
                "shape_ref": "clickhouse", **pool, "labels": {"dfe.hyperi.io/workload": "clickhouse"},
                "taints": [{"key": "dfe.hyperi.io/workload", "value": "clickhouse", "effect": "NO_SCHEDULE"}],
            },
        },
        "resolved_shapes": {
            "eks-system": {"instance_types": ["m8g.large", "m7g.large"], "arch": "arm64"},
            "clickhouse": {"instance_types": ["r8gd.xlarge"], "arch": "arm64"},
            "ci-burst": {"instance_types": ["c8g.large"], "arch": "arm64"},
        },
        "run": cloud_run.run_tfvar(run_id, expires_at),
        "cloudtrail": {"enabled": False},
        "permissions_boundary": "arn:aws:iam::000000000000:policy/dfe-e2e-boundary",
        "iam_path": "/dfe-e2e/",
        "s3_bucket_prefix": "dfe-e2e-",
        "edge_self_ranges": ["198.51.100.20/32"],
        **tfvars_overrides,
    }
    return cloud_run.build_record(
        run_id=run_id, expires_at=expires_at, keys=cloud_run.RunTagKeys(),
        tf_root="terraform/environments/aws", state=state, tfvars=tfvars,
    )


def _leaves(node: object, path: tuple[str, ...] = ()) -> dict[tuple[str, ...], object]:
    """Every leaf of a tfvars tree by its path; an empty map is a leaf of its own."""
    if not isinstance(node, dict) or not node:
        return {path: node}
    found: dict[tuple[str, ...], object] = {}
    for key, value in node.items():
        found |= _leaves(value, (*path, key))
    return found


def _overlay_destroyed_with(monkeypatch: pytest.MonkeyPatch, record: dict) -> dict:
    """Reap one expired record and return the overlay its `tofu init` ran beside."""
    for name in VENDOR_ENV:
        monkeypatch.setenv(name, "example-credential")
    run_id = str(record["run_id"])
    bucket = FakeBucket({run_id: record}, {cloud_run.state_key("dfe-e2e-runs", run_id)})
    problems, destroyed = _reap(monkeypatch, bucket)
    assert problems == []
    assert destroyed[0][0][0] == "init"
    return destroyed[0][2]


def test_a_record_older_than_the_root_gets_every_variable_the_root_now_requires(monkeypatch: pytest.MonkeyPatch) -> None:
    overlay = _overlay_destroyed_with(monkeypatch, _pre_sweep_record())
    missing = _required_variables() - set(overlay)
    assert not missing, f"the destroy overlay leaves required variables unset: {sorted(missing)}"
    assert overlay["eks_addon_versions"]
    assert isinstance(overlay["network"]["subnet_newbits"], int)
    assert isinstance(overlay["telemetry"]["forced_retention_days"], int)
    assert overlay["kafka"]["redpanda_tier"] == "serverless"
    assert overlay["edge"]["tunnel"]["client_cidr"]
    assert overlay["toolbox"]["session_log_retention_days"] == 90


def test_every_shape_the_root_reads_carries_a_root_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cluster module refuses a node pool, and the root a toolbox, whose shape has none."""
    overlay = _overlay_destroyed_with(monkeypatch, _pre_sweep_record())
    shapes = overlay["resolved_shapes"]
    named = {pool["shape_ref"] for pool in overlay["node_pools"].values()} | {overlay["toolbox"]["aws"]["shape_ref"]}
    for name in named:
        assert shapes[name]["root_volume"]["size_gib"] > 0, name
        assert shapes[name]["instance_types"], name


def test_a_shape_named_by_a_legacy_instance_type_takes_the_records_own_type(monkeypatch: pytest.MonkeyPatch) -> None:
    overlay = _overlay_destroyed_with(monkeypatch, _pre_sweep_record())
    toolbox = overlay["resolved_shapes"][overlay["toolbox"]["aws"]["shape_ref"]]
    assert toolbox["instance_types"] == ["t4g.small"]
    assert toolbox["arch"] == "arm64"


def test_expected_fail_no_shape_is_invented_where_the_record_names_no_instance_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no type of the run's own to carry, the shape stays missing and the destroy refuses by name."""
    record = _pre_sweep_record()
    del record["tfvars"]["toolbox"]["aws"]["instance_type"]
    del record["tfvars"]["edge"]["tunnel"]["address"]["instance_type"]
    overlay = _overlay_destroyed_with(monkeypatch, record)
    assert overlay["toolbox"]["aws"]["shape_ref"] not in overlay["resolved_shapes"]


def test_the_overlay_never_changes_a_value_the_record_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _pre_sweep_record()
    overlay = _overlay_destroyed_with(monkeypatch, record)
    filled = _leaves(overlay)
    changed = [path for path, value in _leaves(record["tfvars"]).items() if filled.get(path, object()) != value]
    assert changed == []
    for identity in ("name", "env", "provision", "state", "run", "tags", "s3_bucket_prefix", "iam_path"):
        assert overlay[identity] == record["tfvars"][identity], identity
    assert overlay["kafka"]["provider"] == "redpanda-cloud"
    assert overlay["toolbox"]["aws"]["instance_type"] == "t4g.small"


def test_an_msk_record_keeps_its_broker_and_gains_the_scaler_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    msk = {
        "shape_ref": "msk-broker", "broker_count": 3, "broker_version": "3.9.x.kraft", "num_partitions": 6,
        "log_retention_ms": 86400000, "message_max_bytes": 8388608, "scram_username": "dfe-kafka-user",
        "bootstrap_job": {"namespace": "strimzi", "service_account": "dfe-kafka-bootstrap"},
    }
    record = _pre_sweep_record(kafka={"provider": "msk", "msk": msk}, seeds={"ui/nextauth": {"secret": ""}})
    overlay = _overlay_destroyed_with(monkeypatch, record)
    filled = overlay["kafka"]["msk"]
    assert {key: filled[key] for key in msk} == msk
    assert {"max_brokers", "per_broker_capacity_mb_s", "headroom", "period_seconds", "evaluation_periods"} <= set(
        filled["autoscaling"]
    )
    assert "redpanda_tier" not in overlay["kafka"]


def test_an_external_clickhouse_record_is_filled_for_its_own_server(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _pre_sweep_record(
        clickhouse={"mode": "external", "port": 8443, "native_port": 9440, "secure": True, "user": "default"},
        endpoints={"clickhouse_host": "ch.example.com", "kafka_bootstrap": "", "otel_endpoint": ""},
    )
    overlay = _overlay_destroyed_with(monkeypatch, record)
    assert overlay["endpoints"]["clickhouse_host"] == "ch.example.com"
    assert overlay["eks_addon_versions"]


def test_a_record_the_renderer_refuses_goes_to_the_destroy_as_stored(capsys: pytest.CaptureFixture) -> None:
    record = _record("run-bare", NOW - 60)
    assert cloud_reaper.destroy_tfvars(record) == record["tfvars"]
    assert "go as stored" in capsys.readouterr().err


def test_merge_under_lets_every_stored_value_win_and_fills_only_what_is_absent() -> None:
    defaults = {"a": {"b": 1, "c": 2}, "list": [1, 2], "map": {"x": 1}, "only_default": 3}
    stored = {"a": {"b": 9}, "list": [7], "map": None, "only_stored": 4}
    assert cloud_reaper.merge_under(defaults, stored) == {
        "a": {"b": 9, "c": 2}, "list": [7], "map": None, "only_default": 3, "only_stored": 4,
    }
    assert cloud_reaper.merge_under({}, stored) == stored
    assert cloud_reaper.merge_under(defaults, {}) == defaults


def test_a_catalogue_shape_carries_a_root_volume_only_where_the_catalogue_fixes_one() -> None:
    catalogue = {"clouds": {"aws": {"use_cases": {
        "toolbox": {"arch": "arm64", "volumes": {"root": {"type": "gp3", "size_gib": "20"}}},
        "msk-broker": {"arch": "arm64", "volumes": {"data": {"type": "managed", "size_gib": ""}}},
        "unsized": {"arch": "arm64", "volumes": {"root": {"type": "gp3", "size_gib": ""}}},
    }}}}
    toolbox = cloud_reaper._catalogue_shape(catalogue, "aws", "toolbox")
    assert toolbox == {"arch": "arm64", "root_volume": {"type": "gp3", "size_gib": 20}}
    assert cloud_reaper._catalogue_shape(catalogue, "aws", "msk-broker") == {"arch": "arm64"}
    assert cloud_reaper._catalogue_shape(catalogue, "aws", "unsized") == {"arch": "arm64"}
    assert cloud_reaper._catalogue_shape(catalogue, "aws", "no-such-use-case") == {}
    assert cloud_reaper._catalogue_shape(catalogue, "gcp", "toolbox") == {}


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
