#  Project:      dfe-infra
#  File:         scripts/tests/test_dfe_ops_ui_stage.py
#  Purpose:      Prove `dfe-ops ui` runs every dfe-ui spec a k8s lane can run, on
#                time budgets sized for a deployed stack that an operator can override.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Which dfe-ui specs `dfe-ops ui` runs, and on what time budgets.

The suite's own defaults are Playwright's: 30 s a test, 5 s an assertion. A
deployed stack seeds through the engine's git-backed store and serves reads
through the gateway, so those defaults time out logins and seeding hooks that
would have finished. The suite reads E2E_*_TIMEOUT_MS from its environment, and
this stage is what sets them.

    python3 -m pytest scripts/tests/test_dfe_ops_ui_stage.py -q
"""

import argparse
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_ui_stage", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_ui_stage", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_ui_stage"] = dfeops
_loader.exec_module(dfeops)

BUDGET_VARS = ("E2E_TEST_TIMEOUT_MS", "E2E_EXPECT_TIMEOUT_MS", "E2E_NAV_TIMEOUT_MS")


def _parse(*extra: str) -> argparse.Namespace:
    # The onboarding root step drives a real browser, so these runs start at the suite.
    return dfeops.build_parser().parse_args(
        ["ui", "--ui-repo", "/nonexistent/dfe-ui", "--ui-url", "https://dfe.example",
         "--skip-onboarding", *extra]
    )


def _suite_run(monkeypatch, args: argparse.Namespace) -> tuple[list[str], dict[str, str]]:
    """Run cmd_ui with the cluster and the browser stubbed out; return the suite's argv and env."""
    seen: dict[str, object] = {}

    class Done:
        returncode = 0

    def fake_run(cmd, *, cwd=None, env=None, **_kwargs):
        seen["cmd"] = cmd
        seen["env"] = env
        return Done()

    monkeypatch.setattr(dfeops, "_forward", lambda *_a, **_k: None)
    monkeypatch.setattr(dfeops, "_forward_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(dfeops, "_resolves", lambda *_a, **_k: True)
    monkeypatch.setattr(dfeops, "e2e_routes_mounted", lambda *_a, **_k: True)
    monkeypatch.setattr(dfeops.subprocess, "run", fake_run)
    for name in BUDGET_VARS:
        monkeypatch.delenv(name, raising=False)

    assert dfeops.cmd_ui(args) == 0
    return seen["cmd"], seen["env"]


def _suite_env(monkeypatch, args: argparse.Namespace) -> dict[str, str]:
    return _suite_run(monkeypatch, args)[1]


def test_the_defaults_are_the_deployed_stack_budgets() -> None:
    args = _parse()

    assert args.test_timeout_ms == dfeops.UI_TEST_TIMEOUT_MS == 120_000
    assert args.expect_timeout_ms == dfeops.UI_EXPECT_TIMEOUT_MS == 30_000
    assert args.nav_timeout_ms == dfeops.UI_NAV_TIMEOUT_MS == 30_000


def test_every_budget_reaches_the_suite(monkeypatch) -> None:
    env = _suite_env(monkeypatch, _parse())

    assert env["E2E_TEST_TIMEOUT_MS"] == "120000"
    assert env["E2E_EXPECT_TIMEOUT_MS"] == "30000"
    assert env["E2E_NAV_TIMEOUT_MS"] == "30000"


def test_the_budgets_exceed_the_suites_own_defaults(monkeypatch) -> None:
    # Playwright's own: 30000 a test, 5000 an assertion, 0 (unbounded) a navigation.
    env = _suite_env(monkeypatch, _parse())

    assert int(env["E2E_TEST_TIMEOUT_MS"]) > 30_000
    assert int(env["E2E_EXPECT_TIMEOUT_MS"]) > 5_000
    assert int(env["E2E_NAV_TIMEOUT_MS"]) > 0


def test_an_operator_override_wins(monkeypatch) -> None:
    args = _parse(
        "--test-timeout-ms", "90000", "--expect-timeout-ms", "15000", "--nav-timeout-ms", "0"
    )

    env = _suite_env(monkeypatch, args)

    assert env["E2E_TEST_TIMEOUT_MS"] == "90000"
    assert env["E2E_EXPECT_TIMEOUT_MS"] == "15000"
    assert env["E2E_NAV_TIMEOUT_MS"] == "0"


def test_the_default_run_is_every_spec_but_the_docker_only_ones(monkeypatch) -> None:
    # No dfe-ui spec carries an @acceptance tag, so a --grep on it selected nothing.
    cmd, _env = _suite_run(monkeypatch, _parse())

    assert cmd[:3] == ["yarn", "playwright", "test"]
    assert "--grep" not in cmd
    assert cmd[cmd.index("--grep-invert") + 1] == "@docker-only"


def test_a_grep_narrows_the_run_and_the_docker_only_skip_stays(monkeypatch) -> None:
    cmd, _env = _suite_run(monkeypatch, _parse("--grep", "Sources"))

    assert cmd[cmd.index("--grep") + 1] == "Sources"
    assert cmd[cmd.index("--grep-invert") + 1] == "@docker-only"


def test_an_empty_grep_invert_runs_everything(monkeypatch) -> None:
    cmd, _env = _suite_run(monkeypatch, _parse("--grep-invert", ""))

    assert "--grep-invert" not in cmd


def test_the_suite_gets_a_password_for_the_forced_change(monkeypatch) -> None:
    # The seeders issue the admin its password, so every spec's login changes it first.
    monkeypatch.delenv(dfeops.NEW_ADMIN_PASSWORD_VAR, raising=False)
    env = _suite_env(monkeypatch, _parse())

    assert env["E2E_ADMIN_PASSWORD"] == dfeops.E2E_SEED_PASSWORD
    assert len(env["E2E_ADMIN_NEW_PASSWORD"]) >= 12
    assert env["E2E_ADMIN_NEW_PASSWORD"] != dfeops.E2E_SEED_PASSWORD


def test_a_non_numeric_budget_is_refused_at_parse_time(capsys) -> None:
    with pytest.raises(SystemExit) as refused:
        _parse("--test-timeout-ms", "two-minutes")

    assert refused.value.code == 2
    assert "--test-timeout-ms" in capsys.readouterr().err
