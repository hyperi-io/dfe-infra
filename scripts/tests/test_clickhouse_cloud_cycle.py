#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_clickhouse_cloud_cycle.py
#  Purpose:      Prove a guarded cloud run against an external ClickHouse opens
#                its service's access list after the apply and closes it on
#                every way out, and that the reaper sweeps what a run left.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The ClickHouse Cloud access list across `dfe-ops cloud-cycle` and the reaper.

    python3 -m pytest scripts/tests/test_clickhouse_cloud_cycle.py -q

The cycle's cloud is test_dfe_ops_cloud_guard's FakeGuard and its children a
FakeRunner; the reaper's sweep talks to test_clickhouse_cloud_access's local
API. Every address is an RFC 5737 documentation address.
"""

import json
import os
import signal
import sys
from pathlib import Path

import pytest
import test_clickhouse_cloud_access as access_tests
import test_dfe_ops_cloud_guard as guard_tests
from test_clickhouse_cloud_access import NOW as API_NOW
from test_clickhouse_cloud_access import OPERATOR, FakeCloud, _env
from test_dfe_ops_cloud_guard import NOW, TFVARS, FakeGuard, FakeRunner, _args
from test_dfe_ops_cloud_guard import RUNNER_CIDR as RUNNER

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import cloud_reaper  # noqa: E402
import cloud_run  # noqa: E402
import dfe_ops_cloud_guard as guard_mod  # noqa: E402

# The fixtures this file shares with the modules that own them.
root = guard_tests.root
cloud = access_tests.cloud

ACCESS_SCRIPT = "clickhouse_cloud_access.py"
EGRESS = "198.51.100.1/32,198.51.100.2/32"
CH_ENV = {
    "DFE_CYCLE_CH_SERVICE_ID": "placeholder-service",
    "DFE_CYCLE_CH_API_KEY_ID": "placeholder-key-id",
    "DFE_CYCLE_CH_API_KEY_SECRET": "placeholder-key-secret",
    "TF_VAR_clickhouse_external_password": "placeholder-password",
}


def _external(root_dir: Path) -> None:
    tfvars = {**TFVARS, "clickhouse": {"mode": "external"}}
    (root_dir / "dial.auto.tfvars.json").write_text(json.dumps(tfvars), encoding="utf-8")


def _verbs(runner: FakeRunner) -> list[str]:
    """Each command's verb, the access list's open and close among them."""
    found = []
    for cmd, verb in zip(runner.commands, runner.words(), strict=True):
        script = next((part for part in cmd if part.endswith(ACCESS_SCRIPT)), "")
        found.append(cmd[cmd.index(script) + 1] if script else verb)
    return found


def _cycle(root_dir: Path, monkeypatch: pytest.MonkeyPatch, runner: FakeRunner, env: dict[str, str],
           **overrides: object) -> int:
    for name in CH_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    outputs = {"cluster_name": "dfe-example", "DFE_EGRESS_CIDRS": EGRESS}
    monkeypatch.setattr(guard_mod, "_make_guard", lambda config: FakeGuard())
    monkeypatch.setattr(guard_mod, "Runner", lambda: runner)
    monkeypatch.setattr(guard_mod, "_tofu_output", lambda tf_dir, name: outputs[name])
    monkeypatch.setattr(guard_mod.time, "time", lambda: NOW)
    return guard_mod.cmd_cloud_cycle(_args(root_dir, run_id="run-1", **overrides))


def _command(runner: FakeRunner, verb: str) -> tuple[list[str], dict]:
    index = _verbs(runner).index(verb)
    return runner.commands[index], runner.envs[index] or {}


def test_an_in_cluster_run_never_touches_an_access_list(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FakeRunner()
    assert _cycle(root, monkeypatch, runner, {}) == 0
    assert _verbs(runner) == ["init", "apply", "cycle", "destroy"]


def test_an_external_run_opens_after_the_apply_and_closes_before_the_destroy(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _external(root)
    runner = FakeRunner()
    assert _cycle(root, monkeypatch, runner, CH_ENV) == 0
    assert _verbs(runner) == ["init", "apply", "open", "cycle", "close", "destroy"]


def test_the_run_opens_its_nat_egress_and_its_runner_described_by_its_expiry(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _external(root)
    runner = FakeRunner()
    _cycle(root, monkeypatch, runner, CH_ENV, endpoint_cidr=RUNNER)
    command, env = _command(runner, "open")
    window = guard_mod.resolve_config(_args(root, endpoint_cidr=RUNNER), {}).window
    assert command[command.index("--run-id") + 1] == "run-1"
    assert command[command.index("--expires-at") + 1] == str(int(NOW) + window)
    # The addresses travel in the child's environment, so the echoed command line carries none.
    assert env["DFE_CH_ACCESS_CIDRS"] == f"{EGRESS},{RUNNER}"
    assert not any("198.51.100" in part or RUNNER.split("/")[0] in part for part in command)
    close, _ = _command(runner, "close")
    assert close[close.index("--run-id") + 1] == "run-1"


def test_with_no_runner_address_the_run_opens_its_nat_egress_alone(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _external(root)
    runner = FakeRunner()
    _cycle(root, monkeypatch, runner, CH_ENV)
    _command_line, env = _command(runner, "open")
    assert env["DFE_CH_ACCESS_CIDRS"] == EGRESS


@pytest.mark.parametrize("missing", sorted(CH_ENV))
def test_expected_fail_an_external_run_missing_a_setting_creates_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, missing: str
) -> None:
    _external(root)
    runner = FakeRunner()
    env = {name: value for name, value in CH_ENV.items() if name != missing}
    assert _cycle(root, monkeypatch, runner, env) == 2
    assert runner.commands == []
    printed = capsys.readouterr().err
    assert missing in printed
    assert not any(value in printed for value in env.values())


def test_a_failed_open_skips_the_cycle_and_still_closes_and_destroys(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _external(root)
    runner = FakeRunner(codes={"open": 1})
    assert _cycle(root, monkeypatch, runner, CH_ENV) == 1
    assert _verbs(runner) == ["init", "apply", "open", "close", "teardown", "destroy"]


def test_a_failed_apply_opens_nothing_and_closes_nothing(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _external(root)
    runner = FakeRunner(codes={"apply": 1})
    assert _cycle(root, monkeypatch, runner, CH_ENV) == 1
    assert _verbs(runner) == ["init", "apply", "destroy"]


def test_expected_fail_a_close_that_fails_fails_the_run_after_the_destroy(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _external(root)
    runner = FakeRunner(codes={"close": 1})
    assert _cycle(root, monkeypatch, runner, CH_ENV) == 1
    assert _verbs(runner) == ["init", "apply", "open", "cycle", "close", "destroy"]
    assert "ACCESS LIST NOT CLOSED" in capsys.readouterr().err


def test_sigterm_during_the_cycle_still_closes_the_access_list(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _external(root)
    runner = FakeRunner(on_run={"cycle": lambda: os.kill(os.getpid(), signal.SIGTERM)})
    assert _cycle(root, monkeypatch, runner, CH_ENV) == 143
    assert _verbs(runner) == ["init", "apply", "open", "cycle", "close", "teardown", "destroy"]


# --- the reaper ----------------------------------------------------------------------------


REAPER_ENV = {
    "DFE_REAPER_AWS_ROLE": "arn:aws:iam::000000000000:role/dfe-e2e/reaper",
    "DFE_REAPER_AWS_REGIONS": "us-west-2",
}


def _reap(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> int:
    monkeypatch.setattr(cloud_reaper, "reap_runs", lambda config, now: [])
    monkeypatch.setattr(cloud_reaper, "sweep_region", lambda config, region: 0)
    monkeypatch.setattr(cloud_reaper.time, "time", lambda: API_NOW)
    return cloud_reaper.cmd_reap({**REAPER_ENV, **env})


def test_the_reaper_sweeps_expired_run_entries_and_spares_the_rest(
    monkeypatch: pytest.MonkeyPatch, cloud: tuple[FakeCloud, str]
) -> None:
    state, url = cloud
    live = {"source": "198.51.100.3/32", "description": f"dfe-run:run-live:{API_NOW + 60}"}
    state.entries += [{"source": "198.51.100.1/32", "description": f"dfe-run:run-old:{API_NOW - 60}"}, live]
    assert _reap(monkeypatch, _env(url)) == 0
    assert state.entries == [OPERATOR, live]


def test_a_reaper_with_no_clickhouse_settings_sweeps_no_access_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    assert _reap(monkeypatch, {}) == 0
    assert "ClickHouse" in capsys.readouterr().out


def test_expected_fail_a_half_configured_clickhouse_sweep_fails_the_reap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    assert _reap(monkeypatch, {"DFE_CYCLE_CH_SERVICE_ID": "placeholder-service"}) == 1
    printed = capsys.readouterr().out
    assert "DFE_CYCLE_CH_API_KEY_ID" in printed
    assert "placeholder-service" not in printed


def test_expected_fail_an_api_failure_fails_the_reap_without_naming_the_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, cloud: tuple[FakeCloud, str]
) -> None:
    state, url = cloud
    state.fail_status = 503
    assert _reap(monkeypatch, _env(url)) == 1
    printed = capsys.readouterr().out
    assert "503" in printed
    assert "00000000-0000-0000-0000-00000000000b" not in printed


def _record(clickhouse_mode: str) -> dict:
    state = {"bucket": "example-state", "region": "us-west-2", "key": cloud_run.state_key("dfe-e2e-runs", "run-x")}
    tfvars = {"state": state, "kafka": {"provider": "strimzi"}, "clickhouse": {"mode": clickhouse_mode}}
    return {"run_id": "run-x", "tfvars": tfvars}


def test_an_external_runs_destroy_gets_a_placeholder_password_when_the_runner_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store keeps the value it was created with, so a destroy never reads it."""
    monkeypatch.delenv("TF_VAR_clickhouse_external_password", raising=False)
    env = cloud_reaper.destroy_env(_record("external"))
    assert env["TF_VAR_clickhouse_external_password"] == cloud_reaper.PLACEHOLDER_VALUE


def test_an_external_runs_destroy_keeps_the_runners_own_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TF_VAR_clickhouse_external_password", "placeholder-password")
    env = cloud_reaper.destroy_env(_record("external"))
    assert env["TF_VAR_clickhouse_external_password"] == "placeholder-password"


def test_an_in_cluster_runs_destroy_gets_no_clickhouse_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TF_VAR_clickhouse_external_password", raising=False)
    assert "TF_VAR_clickhouse_external_password" not in cloud_reaper.destroy_env(_record("in_cluster"))
