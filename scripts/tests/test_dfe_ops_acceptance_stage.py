#  Project:      dfe-infra
#  File:         scripts/tests/test_dfe_ops_acceptance_stage.py
#  Purpose:      Prove `dfe-ops cycle --acceptance-suite` runs the acceptance suite
#                as its own stage after smoke, that acceptance reads the deploy's
#                terraform outputs, refuses an undeployed edge fence and signs in
#                as the deploy's minted admin, and that the source runner names
#                the rows that failed with the run's secrets redacted.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The cycle's acceptance stage, and what `dfe-ops acceptance` needs to run in it.

A guarded cloud cycle runs unattended against a cluster it created a minute
earlier, so the stage has to find that deployment's namespace from the same
terraform outputs the deploy read, sign in with no password anyone typed, and
leave a destroy behind it whatever it reports.

    python3 -m pytest scripts/tests/test_dfe_ops_acceptance_stage.py -q
"""

import argparse
import importlib.machinery
import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_acceptance_stage", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_acceptance_stage", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_acceptance_stage"] = dfeops
_loader.exec_module(dfeops)

from acceptance import clients  # noqa: E402
from acceptance.onboarding import wizard  # noqa: E402
from acceptance.source import run as source_run  # noqa: E402

# The shape bridge.get_tf_outputs really answers with: name -> (value, sensitive).
TF_OUTPUTS = {
    "DFE_NAMESPACE": ("dfe-aws-test", False),
    "DFE_DOMAIN": ("dfe-test.internal", False),
    "cluster_name": ("dfe-aws-test", False),
}


def _fake_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = type(sys)("bridge")
    bridge.get_tf_outputs = lambda _dir: dict(TF_OUTPUTS)
    monkeypatch.setitem(sys.modules, "bridge", bridge)


# --- the stage inside the cycle ----------------------------------------------------


def _cycle(monkeypatch: pytest.MonkeyPatch, *argv: str, fail: str = "") -> list[tuple[str, list[str]]]:
    """Run cmd_cycle with every stage stubbed; return (stage word, argv) in the order they ran.

    ``fail`` names the subcommand whose stage exits 1.
    """
    ran: list[tuple[str, list[str]]] = []

    def run(cmd: list[str], env: dict | None = None) -> int:
        word = cmd[2]
        ran.append((word, cmd))
        return 1 if word == fail else 0

    monkeypatch.setattr(dfeops, "_resolve_stack", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_run_streaming", run)
    monkeypatch.setenv("DFE_ENGINE_REPO", "/nonexistent/dfe-engine")
    args = dfeops.build_parser().parse_args(
        ["cycle", "--skip-capacity-check", "--skip-preflight", "--stack", "2.2.0-rc.99", *argv]
    )
    dfeops.cmd_cycle(args)
    return ran


def test_no_acceptance_suite_runs_no_acceptance_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    assert [word for word, _ in _cycle(monkeypatch)] == ["stack-deploy", "verify", "teardown"]


def test_the_acceptance_stage_runs_after_smoke_and_before_destroy(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _cycle(monkeypatch, "--acceptance-suite", "source")
    assert [word for word, _ in ran] == ["stack-deploy", "verify", "acceptance", "teardown"]


def test_the_acceptance_stage_gets_the_deploys_target_mode_and_a_shots_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("", encoding="utf-8")
    shots = tmp_path / "shots"
    ran = _cycle(
        monkeypatch, "--acceptance-suite", "source", "--mode", "single",
        "--kubeconfig", str(kubeconfig), "--env-file", "x.env", "--from-terraform", "tf",
        "--acceptance-shots-dir", str(shots),
    )
    cmd = dict(ran)["acceptance"]
    assert cmd[cmd.index("--suite") + 1] == "source"
    assert cmd[cmd.index("--mode") + 1] == "single"
    assert cmd[cmd.index("--kubeconfig") + 1] == str(kubeconfig)
    assert cmd[cmd.index("--env-file") + 1] == "x.env"
    assert cmd[cmd.index("--from-terraform") + 1] == "tf"
    assert cmd[cmd.index("--shots-dir") + 1] == str(shots)
    assert "--insecure" in cmd
    # The suite's own parser has to accept every flag the stage hands it.
    parsed = dfeops.build_parser().parse_args(cmd[2:])
    assert (parsed.suite, parsed.from_terraform, parsed.insecure) == ("source", "tf", True)


def test_the_shots_dir_defaults_under_this_repos_tmp() -> None:
    args = dfeops.build_parser().parse_args(["cycle"])
    assert Path(args.acceptance_shots_dir) == REPO_ROOT / ".tmp" / "acceptance"
    assert args.acceptance_suite is None


def test_the_acceptance_stage_runs_before_the_ui_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ui suite's seed routes reset every account, the admin this stage signs in as included."""
    ran = _cycle(monkeypatch, "--e2e", "--ui-repo", "/nonexistent/dfe-ui", "--acceptance-suite", "source")
    assert [word for word, _ in ran] == ["stack-deploy", "verify", "acceptance", "ui", "teardown"]


def test_a_failed_acceptance_stage_skips_the_rest_and_still_destroys(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _cycle(
        monkeypatch, "--e2e", "--ui-repo", "/nonexistent/dfe-ui", "--acceptance-suite", "source",
        fail="acceptance",
    )
    assert [word for word, _ in ran] == ["stack-deploy", "verify", "acceptance", "teardown"]


def test_a_failed_smoke_stage_runs_no_acceptance(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _cycle(monkeypatch, "--acceptance-suite", "source", fail="verify")
    assert [word for word, _ in ran] == ["stack-deploy", "verify", "teardown"]


def test_a_failed_acceptance_stage_fails_the_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dfeops, "_resolve_stack", lambda _args: 0)
    monkeypatch.setattr(
        dfeops, "_run_streaming", lambda cmd, env=None: 1 if cmd[2] == "acceptance" else 0
    )
    monkeypatch.setenv("DFE_ENGINE_REPO", "/nonexistent/dfe-engine")
    args = dfeops.build_parser().parse_args(
        ["cycle", "--skip-capacity-check", "--skip-preflight", "--stack", "2.2.0-rc.99",
         "--acceptance-suite", "source"]
    )
    assert dfeops.cmd_cycle(args) == 1


def test_expected_fail_no_engine_checkout_refuses_before_anything_deploys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """acceptance cannot start without one, and finding that out after the deploy costs the deploy."""
    ran: list[list[str]] = []
    monkeypatch.setattr(dfeops, "_resolve_stack", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_run_streaming", lambda cmd, env=None: ran.append(cmd) or 0)
    monkeypatch.setattr(dfeops, "_sibling_repo", lambda _name, _env_var: None)
    args = dfeops.build_parser().parse_args(
        ["cycle", "--skip-capacity-check", "--skip-preflight", "--stack", "2.2.0-rc.99",
         "--acceptance-suite", "source"]
    )
    assert dfeops.cmd_cycle(args) == 2
    assert ran == []
    assert "DFE_ENGINE_REPO" in capsys.readouterr().err


def test_expected_fail_an_unknown_suite_is_refused_at_parse_time(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as refused:
        dfeops.build_parser().parse_args(["cycle", "--acceptance-suite", "bogus"])
    assert refused.value.code == 2
    assert "--acceptance-suite" in capsys.readouterr().err


# --- acceptance itself: the deploy's own namespace and login ------------------------


def _acceptance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str, expect: int = 0
) -> tuple[list[tuple], dict[str, str]]:
    """Run cmd_acceptance --suite source with the cluster stubbed out.

    Returns the forwards it started and the env the source runner was handed,
    empty when the runner never started.
    """
    forwards: list[tuple] = []
    seen: dict[str, dict[str, str]] = {}

    class Done:
        returncode = 0

    def fake_run(cmd, *, env=None, **_kwargs):
        if "run.py" in str(cmd[1]):
            seen["env"] = dict(env or {})
        return Done()

    monkeypatch.setattr(dfeops, "_forward", lambda *args, **_k: forwards.append(args))
    monkeypatch.setattr(dfeops, "_forward_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(dfeops, "_taken_ports", lambda _ports: [])
    monkeypatch.setattr(dfeops, "_secret_value", lambda *_a, **_k: "")
    monkeypatch.setattr(dfeops, "_pods_of", lambda *_a, **_k: [])
    monkeypatch.setattr(dfeops, "_resolves", lambda _host: True)
    monkeypatch.setattr(dfeops, "admin_credential", lambda *_a, **_k: ("admin", "minted"))
    monkeypatch.setattr(dfeops.subprocess, "run", fake_run)
    monkeypatch.delenv(dfeops.NEW_ADMIN_PASSWORD_VAR, raising=False)
    monkeypatch.delenv("DFE_NAMESPACE", raising=False)
    monkeypatch.delenv(dfeops.EDGE_FENCE_VAR, raising=False)
    args = dfeops.build_parser().parse_args([
        "acceptance", "--repo", "/nonexistent/dfe-engine", "--suite", "source",
        "--ui-url", "https://dfe.example", "--shots-dir", str(tmp_path / "shots"), *argv,
    ])
    assert dfeops.cmd_acceptance(args) == expect
    return forwards, seen.get("env", {})


def test_with_no_password_given_the_suite_moves_the_admin_to_the_one_ui_would(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without one the forced first-login change has nothing to change to, and the
    run would need a password somebody typed -- which an unattended cycle has not."""
    _forwards, env = _acceptance(monkeypatch, tmp_path)
    assert env[dfeops.NEW_ADMIN_PASSWORD_VAR] == dfeops.ui_admin_password({}, "minted")
    assert env["DFE_E2E_ADMIN_PASSWORD"] == "minted"
    assert env[dfeops.NEW_ADMIN_PASSWORD_VAR] not in ("minted", dfeops.E2E_SEED_PASSWORD)


def test_a_password_the_env_file_names_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / "estate.env"
    env_file.write_text(f'{dfeops.NEW_ADMIN_PASSWORD_VAR}="chosen-by-the-operator"\n', encoding="utf-8")
    _forwards, env = _acceptance(monkeypatch, tmp_path, "--env-file", str(env_file))
    assert env[dfeops.NEW_ADMIN_PASSWORD_VAR] == "chosen-by-the-operator"


def test_terraform_outputs_name_the_namespace_the_forwards_open_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without them the suite forwards into dfe-local, which a cloud deploy has never had."""
    _fake_bridge(monkeypatch)
    forwards, _env = _acceptance(monkeypatch, tmp_path, "--from-terraform", str(tmp_path))
    by_label = {args[0]: args[2] for args in forwards}
    assert by_label["receiver"] == "dfe-aws-test"
    assert by_label["engine"] == "dfe-aws-test"


def test_an_env_file_wins_over_terraform(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_bridge(monkeypatch)
    env_file = tmp_path / "estate.env"
    env_file.write_text('DFE_NAMESPACE="dfe-chosen"\n', encoding="utf-8")
    forwards, _env = _acceptance(
        monkeypatch, tmp_path, "--from-terraform", str(tmp_path), "--env-file", str(env_file)
    )
    assert {args[0]: args[2] for args in forwards}["engine"] == "dfe-chosen"


# --- the edge fence is deployed before a browser starts ---------------------------------

FENCE = "203.0.113.7/32,198.51.100.4/32,10.90.0.0/16"


def _envoy_services(monkeypatch: pytest.MonkeyPatch, *ranges: list[str] | None, rc: int = 0) -> list[list[str]]:
    """Stub the cluster's Envoy LoadBalancer Services, one per `ranges`; return the kubectl calls made."""
    calls: list[list[str]] = []
    items = [
        {"metadata": {"namespace": "envoy-gateway-system", "name": f"envoy-dfe-gateway-{i}"},
         "spec": {"type": "LoadBalancer", **({"loadBalancerSourceRanges": r} if r is not None else {})}}
        for i, r in enumerate(ranges)
    ]
    items.append({"metadata": {"namespace": "envoy-gateway-system", "name": "envoy-dfe-mesh"},
                  "spec": {"type": "ClusterIP"}})

    def fake(base: list[str], *_argv: str, **_kwargs: object) -> tuple[int, dict, str]:
        calls.append(base)
        return (rc, {"items": items}, "") if rc == 0 else (rc, {}, "Forbidden")

    monkeypatch.setattr(dfeops, "_kubectl_json", fake)
    return calls


def test_no_fence_named_reads_no_service(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _envoy_services(monkeypatch, None)
    assert dfeops._edge_fence_problem(["kubectl"], "", "") == ""
    assert calls == []


def test_a_service_carrying_every_fenced_range_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chart adds its own base ranges to the fence, so more is fine and fewer is not."""
    calls = _envoy_services(monkeypatch, [*FENCE.split(","), "192.0.2.0/24"])
    assert dfeops._edge_fence_problem(["kubectl"], FENCE, FENCE) == ""
    assert calls[0][-2:] == ["-l", dfeops.ENVOY_OWNER_LABEL]


def test_expected_fail_a_service_missing_one_fenced_range_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    _envoy_services(monkeypatch, ["203.0.113.7/32", "10.90.0.0/16"])
    problem = dfeops._edge_fence_problem(["kubectl"], FENCE, FENCE)
    assert problem == "envoy-gateway-system/envoy-dfe-gateway-0 lacks 198.51.100.4/32"


def test_expected_fail_an_env_file_emptying_the_terraform_fence_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    _envoy_services(monkeypatch, None)
    problem = dfeops._edge_fence_problem(["kubectl"], "", FENCE)
    assert "envoy-dfe-gateway-0 lacks 10.90.0.0/16, 198.51.100.4/32, 203.0.113.7/32" in problem
    assert "an env file sets DFE_EDGE_ALLOWED_CIDRS empty over the terraform output" in problem


@pytest.mark.parametrize(("ranges", "rc", "said"), [
    ((), 0, "no Envoy Gateway LoadBalancer Service"),
    ((["203.0.113.7/32"],), 1, "could not list the gateway's Services: Forbidden"),
])
def test_expected_fail_a_service_that_cannot_be_read_fails_rather_than_passing(
    monkeypatch: pytest.MonkeyPatch, ranges: tuple, rc: int, said: str
) -> None:
    _envoy_services(monkeypatch, *ranges, rc=rc)
    assert said in dfeops._edge_fence_problem(["kubectl"], FENCE, FENCE)


def test_expected_fail_an_env_file_line_that_empties_the_fence_stops_acceptance_before_the_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """`KEY=` in the env file beats the terraform output, so bootstrap deployed an open gateway."""
    bridge = type(sys)("bridge")
    bridge.get_tf_outputs = lambda _dir: {**TF_OUTPUTS, dfeops.EDGE_FENCE_VAR: (FENCE, False)}
    monkeypatch.setitem(sys.modules, "bridge", bridge)
    _envoy_services(monkeypatch, None)
    env_file = tmp_path / "estate.env"
    env_file.write_text(f"{dfeops.EDGE_FENCE_VAR}=\n", encoding="utf-8")
    forwards, env = _acceptance(
        monkeypatch, tmp_path, "--from-terraform", str(tmp_path), "--env-file", str(env_file), expect=1
    )
    assert (forwards, env) == ([], {})
    assert "EDGE FENCE NOT DEPLOYED" in capsys.readouterr().err


def test_a_deployed_fence_lets_acceptance_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bridge = type(sys)("bridge")
    bridge.get_tf_outputs = lambda _dir: {**TF_OUTPUTS, dfeops.EDGE_FENCE_VAR: (FENCE, False)}
    monkeypatch.setitem(sys.modules, "bridge", bridge)
    _envoy_services(monkeypatch, FENCE.split(","))
    _forwards, env = _acceptance(monkeypatch, tmp_path, "--from-terraform", str(tmp_path))
    assert env["DFE_E2E_ENGINE_USER"] == "admin"


# --- the smoke stage reads the same outputs ------------------------------------------


def test_the_smoke_suite_gets_terraform_outputs_as_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    """A (value, sensitive) pair in the env made subprocess refuse verify outright,
    so a cycle with --from-terraform failed at smoke and never reached acceptance."""
    _fake_bridge(monkeypatch)
    args = argparse.Namespace(from_terraform="tf", env_file=[], kubeconfig="", mode="single")
    env = dfeops._suite_env(args)
    assert env["DFE_NAMESPACE"] == "dfe-aws-test"
    assert all(isinstance(value, str) for value in env.values())
    assert "cluster_name" not in env


# --- the source runner's report ----------------------------------------------------


def test_the_report_names_each_failed_row(tmp_path: Path) -> None:
    """A stage summary says only that acceptance failed; the report says which step."""
    results = [
        wizard.StepResult("landed", "done", "dfe.fb-1 gained 60 of 60 posted rows"),
        wizard.StepResult("observe", "failed", "the HyperDX frame did not load"),
    ]
    text = source_run.report(results, tmp_path / "shots")
    assert "FAILED: observe" in text
    assert "landed" not in text.split("FAILED:", 1)[1]


def test_the_report_lands_beside_the_screenshots(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    results = [wizard.StepResult("observe", "failed", "the HyperDX frame did not load")]
    text = source_run.report(results, shots)
    assert (shots / source_run.STEP_TABLE).read_text(encoding="utf-8") == text + "\n"


def test_a_clean_run_names_no_failure(tmp_path: Path) -> None:
    results = [wizard.StepResult("observe", "done", "Observe search over fb-1: 12 Results")]
    assert "FAILED" not in source_run.report(results, tmp_path)


def test_the_step_table_carries_no_secret_the_run_holds(tmp_path: Path) -> None:
    """The table prints to a job log that can be public, and a detail is free text."""
    results = [
        wizard.StepResult("run", "failed", "RuntimeError: login refused for minted-issued-1"),
        wizard.StepResult("teardown", "failed", "bearer eyJ0.token.sig went stale"),
    ]
    text = source_run.report(results, tmp_path, secrets=("minted-issued-1", "", "eyJ0.token.sig"))
    for copy in (text, (tmp_path / source_run.STEP_TABLE).read_text(encoding="utf-8")):
        assert "minted-issued-1" not in copy
        assert "eyJ0.token.sig" not in copy
        assert copy.count("[redacted]") == 2


def test_a_secret_holding_another_is_redacted_whole(tmp_path: Path) -> None:
    results = [wizard.StepResult("run", "failed", "saw chosen-password-2")]
    text = source_run.report(results, tmp_path, secrets=("chosen", "chosen-password-2"))
    assert "saw [redacted]" in text
    assert "password-2" not in text


# --- the engine client's forced change, and the teardown after a refusal --------------


ISSUED, CHOSEN = "issued-pw", "chosen-pw"


def _issued_engine(monkeypatch: pytest.MonkeyPatch) -> tuple[clients.Engine, list[tuple[str, object]]]:
    """An engine whose admin is on an issued password, answering as the v1.22 engine does."""
    sent: list[tuple[str, object]] = []
    engine = clients.Engine("https://dfe.example", "admin", ISSUED, new_password=CHOSEN)

    def request(method: str, path: str, body: object = None, *, auth: bool = True) -> clients.Reply:
        sent.append((path, body))
        if path == "/auth/login":
            if body["password"] == ISSUED:  # type: ignore[index]
                return clients.Reply(200, {"access_token": "issued-session", "password_change_required": True})
            return clients.Reply(200, {"access_token": "fresh-session"})
        if path == "/auth/accounts/reset-password":
            # ChangeOwnPasswordRequest: a body without current_password is a 422.
            if not isinstance(body, dict) or body.get("current_password") != ISSUED:
                return clients.Reply(422, {"detail": [{"field": "current_password", "type": "missing"}]})
            return clients.Reply(200, {"message": "password reset"})
        # The change ended the session it was made on.
        return clients.Reply(200 if engine.token == "fresh-session" else 401, {"items": []})

    monkeypatch.setattr(engine, "_request", request)
    return engine, sent


def test_the_forced_change_proves_the_issued_password(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, sent = _issued_engine(monkeypatch)
    engine.login()
    assert ("/auth/accounts/reset-password", {"current_password": ISSUED, "new_password": CHOSEN}) in sent
    assert engine.password == CHOSEN


def test_after_the_forced_change_the_next_call_signs_in_again(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, sent = _issued_engine(monkeypatch)
    assert engine.call("GET", "/sources").status == 200
    logins = [body["password"] for path, body in sent if path == "/auth/login"]  # type: ignore[index]
    assert logins == [ISSUED, CHOSEN]


def test_an_engine_that_refuses_the_run_at_teardown_still_leaves_the_report(tmp_path: Path) -> None:
    """A RuntimeError out of the engine client used to escape teardown, so no steps.txt was written."""

    class Refusing:
        def call(self, *_args: object, **_kwargs: object) -> clients.Reply:
            raise RuntimeError("forced password change failed: 422")

    driver = types.SimpleNamespace(results=[wizard.StepResult("landed", "done", "60 of 60 rows")])
    run = types.SimpleNamespace(driver=driver, engine=Refusing(), args=types.SimpleNamespace(keep=False))
    source_run.teardown(run, types.SimpleNamespace(name="fb-1"), "https://dfe.example")
    text = source_run.report(driver.results, tmp_path)
    assert "FAILED: teardown" in text
    assert "forced password change failed: 422" in (tmp_path / source_run.STEP_TABLE).read_text(encoding="utf-8")
