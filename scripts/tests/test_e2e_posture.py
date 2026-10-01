#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_e2e_posture.py
#  Purpose:      Prove the e2e posture the dfe-ui suite needs reaches the engine
#                only when a deploy asks for it and only on a dev posture, and
#                that `dfe-ops ui` onboards first and runs every non-docker spec.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The e2e posture, from the flag to the engine pod, and the ui stage on top of it.

The dfe-ui suite seeds through the engine's unauthenticated /api/e2e routes, whose
scripts wipe and reseed every account. So the posture is off unless a deploy asks
for it, refused outside a dev posture by every layer that sees the flag, and forced
off by stack-deploy when the flag is absent.

    python3 -m pytest scripts/tests/test_e2e_posture.py -q

Needs `helm` on PATH; the one kubectl case skips without kubectl.
"""

import argparse
import importlib.machinery
import importlib.util
import io
import shutil
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"
ENGINE_CHART = REPO_ROOT / "helm" / "charts" / "dfe-engine"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-apps.yaml"
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"

ANNOTATION = "dfe.hyperi.io/e2e_server"
DEV_POSTURES = ("dev", "development", "local", "test", "ci")

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_e2e_posture", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_e2e_posture", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_e2e_posture"] = dfeops
_loader.exec_module(dfeops)


# --- the engine chart ----------------------------------------------------------
def render_engine(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", "template", "dfe-engine", str(ENGINE_CHART),
         "--show-only", "templates/deployment.yaml", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def engine_env(*args: str) -> dict[str, str | None]:
    out = render_engine(*args)
    assert out.returncode == 0, out.stderr
    deployment = next(d for d in yaml.safe_load_all(out.stdout) if d)
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container["env"]}


def test_the_engine_ships_with_the_posture_off() -> None:
    env = engine_env("--set", "env=local")
    assert env["DFE_ENV"] == "local"
    assert "DFE_E2E_SERVER" not in env


def test_the_posture_runs_the_engine_as_test_with_the_routes_on() -> None:
    """The seeders accept no posture but test, so the engine is told test."""
    for flag in ("--set", "--set-string"):
        env = engine_env("--set", "env=local", flag, "e2eServer=true")
        assert env["DFE_ENV"] == "test", flag
        assert env["DFE_E2E_SERVER"] == "true", flag


def test_an_explicit_false_is_off() -> None:
    """A cluster secret always carries the key, false included."""
    env = engine_env("--set", "env=local", "--set-string", "e2eServer=false")
    assert env["DFE_ENV"] == "local"
    assert "DFE_E2E_SERVER" not in env


def test_every_dev_posture_may_carry_it() -> None:
    for posture in DEV_POSTURES:
        assert engine_env("--set", f"env={posture}", "--set", "e2eServer=true")["DFE_ENV"] == "test"


def test_a_production_posture_refuses_it() -> None:
    """The routes wipe every account, so the render stops rather than mounting them."""
    for posture in ("production", "staging", "customer-acme"):
        out = render_engine("--set", f"env={posture}", "--set", "e2eServer=true")
        assert out.returncode != 0, posture
        assert "dev posture" in out.stderr, out.stderr


def test_a_value_that_is_not_a_boolean_is_refused() -> None:
    """A truthy string would otherwise switch the routes on."""
    out = render_engine("--set", "env=local", "--set-string", "e2eServer=yes")
    assert out.returncode != 0
    assert "true or false" in out.stderr, out.stderr


# --- the appset ------------------------------------------------------------------
def appset_param(name: str) -> str:
    appset = yaml.safe_load(APPSET.read_text(encoding="utf-8"))
    for source in appset["spec"]["template"]["spec"]["sources"]:
        for entry in source.get("helm", {}).get("parameters", []):
            if entry["name"] == name:
                return entry["value"]
    raise AssertionError(f"layer2-apps.yaml passes no {name} parameter")


def render_expr(expr: str, annotations: dict[str, str]) -> str:
    """One appset parameter string, rendered through helm's template engine."""
    with tempfile.TemporaryDirectory(prefix="appset-e2e-") as tmp:
        chart = Path(tmp)
        (chart / "templates").mkdir()
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: e2e\nversion: 0.0.0\n",
                                          encoding="utf-8")
        (chart / "templates" / "cm.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: e2e\ndata:\n"
            '  value: {{ tpl .Values.expr (dict "metadata" .Values.metadata '
            '"Template" .Template) | quote }}\n',
            encoding="utf-8",
        )
        values = {"expr": expr, "metadata": {"annotations": annotations}}
        (chart / "values.yaml").write_text(yaml.safe_dump(values), encoding="utf-8")
        out = subprocess.run(["helm", "template", "e2e", str(chart)], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", check=False)
    assert out.returncode == 0, out.stderr
    return next(d for d in yaml.safe_load_all(out.stdout) if d)["data"]["value"]


def test_the_appset_hands_the_engine_the_annotation() -> None:
    expr = appset_param("e2eServer")
    assert ANNOTATION in expr
    assert render_expr(expr, {ANNOTATION: "true"}) == "true"
    assert render_expr(expr, {ANNOTATION: "false"}) == "false"


def test_a_secret_written_before_the_key_reads_off() -> None:
    expr = appset_param("e2eServer")
    assert render_expr(expr, {}) == "false"
    assert render_expr(expr, {ANNOTATION: ""}) == "false"


# --- bootstrap -------------------------------------------------------------------
def posture_block() -> str:
    """bootstrap.sh's DFE_E2E_SERVER default and validation, as it ships."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("export DFE_E2E_SERVER="))
    end = next(i for i in range(start, len(lines)) if lines[i] == "esac")
    return "\n".join(lines[start:end + 1])


def run_posture_block(env: dict[str, str]) -> subprocess.CompletedProcess:
    script = f"set -euo pipefail\n{posture_block()}\necho \"posture=${{DFE_E2E_SERVER}}\"\n"
    return subprocess.run(["bash", "-c", script], env={"PATH": "/usr/bin:/bin", **env},
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=False)


def test_bootstrap_defaults_the_posture_off() -> None:
    out = run_posture_block({"DFE_ENV": "local"})
    assert out.returncode == 0, out.stderr
    assert "posture=false" in out.stdout


def test_bootstrap_takes_the_posture_on_a_dev_posture() -> None:
    for posture in DEV_POSTURES:
        out = run_posture_block({"DFE_ENV": posture, "DFE_E2E_SERVER": "true"})
        assert out.returncode == 0, (posture, out.stderr)
        assert "posture=true" in out.stdout


def test_bootstrap_refuses_the_posture_anywhere_else() -> None:
    out = run_posture_block({"DFE_ENV": "production", "DFE_E2E_SERVER": "true"})
    assert out.returncode != 0
    assert "dev posture" in out.stderr


def test_bootstrap_refuses_a_value_that_is_not_a_boolean() -> None:
    out = run_posture_block({"DFE_ENV": "local", "DFE_E2E_SERVER": "yes"})
    assert out.returncode != 0
    assert "true or false" in out.stderr


def test_bootstrap_writes_the_key_onto_the_applied_secret() -> None:
    """Applied with the secret, so a redeploy without the posture writes false over it."""
    body = BOOTSTRAP.read_text(encoding="utf-8")
    assert (
        f'kubectl annotate --local -f - "{ANNOTATION}=${{DFE_E2E_SERVER}}" --overwrite -o yaml'
        in body
    )


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl is not on PATH")
def test_a_local_annotate_needs_no_cluster(tmp_path: Path) -> None:
    """The annotate step runs before anything reaches the API server."""
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "dfe-cluster", "namespace": "argocd",
                     "annotations": {"dfe.hyperi.io/env": "local"}},
        "stringData": {"server": "https://kubernetes.default.svc"},
    }
    out = subprocess.run(
        ["kubectl", "annotate", "--local", "-f", "-", f"{ANNOTATION}=true", "--overwrite",
         "-o", "yaml"],
        input=yaml.safe_dump(secret),
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "KUBECONFIG": str(tmp_path / "absent")},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert out.returncode == 0, out.stderr
    annotations = yaml.safe_load(out.stdout)["metadata"]["annotations"]
    assert annotations[ANNOTATION] == "true"
    assert annotations["dfe.hyperi.io/env"] == "local"


# --- stack-deploy and cycle ------------------------------------------------------
def deploy_args(tmp_path: Path, *, e2e: bool, env_file: list[str] | None = None):
    return argparse.Namespace(
        from_terraform=None, env_file=env_file or [], mode="single", target_revision="",
        kubeconfig="", readiness_timeout=900, access_out=str(tmp_path / "access.md"),
        stack="current", registry=None, skip_e2e=False, dry_run=False, e2e=e2e,
    )


def test_stack_deploy_turns_the_posture_on_only_when_asked(tmp_path: Path) -> None:
    assert dfeops._assemble_env(deploy_args(tmp_path, e2e=True))["DFE_E2E_SERVER"] == "true"
    assert dfeops._assemble_env(deploy_args(tmp_path, e2e=False))["DFE_E2E_SERVER"] == "false"


def test_an_env_file_cannot_turn_the_posture_on(tmp_path: Path) -> None:
    env_file = tmp_path / "estate.env"
    env_file.write_text('DFE_E2E_SERVER="true"\n', encoding="utf-8")
    env = dfeops._assemble_env(deploy_args(tmp_path, e2e=False, env_file=[str(env_file)]))
    assert env["DFE_E2E_SERVER"] == "false"


def test_the_cycle_refuses_a_ui_stage_without_the_posture() -> None:
    args = dfeops.build_parser().parse_args(["cycle", "--ui-repo", "/nonexistent/dfe-ui"])
    assert dfeops.cmd_cycle(args) == 2


def test_the_cycle_deploys_in_the_posture_and_runs_the_ui_stage(monkeypatch) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(dfeops, "_resolve_stack", lambda args: 0)
    monkeypatch.setattr(dfeops, "_run_streaming",
                        lambda cmd, env=None: ran.append(cmd) or 0)
    args = dfeops.build_parser().parse_args([
        "cycle", "--e2e", "--ui-repo", "/nonexistent/dfe-ui", "--keep",
        "--skip-capacity-check", "--skip-preflight", "--stack", "2.2.0-rc.99",
    ])
    assert dfeops.cmd_cycle(args) == 0
    deploy = next(cmd for cmd in ran if "stack-deploy" in cmd)
    assert "--e2e" in deploy
    assert any("ui" in cmd and "--ui-repo" in cmd for cmd in ran)


# --- the ui stage ----------------------------------------------------------------
def test_ui_excludes_the_docker_only_specs_by_default() -> None:
    args = dfeops.build_parser().parse_args(["ui", "--ui-repo", "/nonexistent/dfe-ui"])
    assert args.grep_invert == "@docker-only"
    assert args.grep == ""
    assert not args.skip_onboarding


def test_playwright_argv_carries_the_filters_and_the_passthrough() -> None:
    assert dfeops.playwright_argv("@docker-only", "", ["--", "--reporter=list"]) == [
        "yarn", "playwright", "test", "--grep-invert", "@docker-only", "--reporter=list",
    ]
    assert dfeops.playwright_argv("", "@core", []) == [
        "yarn", "playwright", "test", "--grep", "@core",
    ]


class _Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_e2e_routes_are_read_off_the_engine() -> None:
    def answering(body: bytes):
        return lambda url, timeout: _Reply(body)

    def refusing(url, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    base = "http://localhost:18000"
    assert dfeops.e2e_routes_mounted(base, opener=answering(b'{"enabled": true}'))
    assert not dfeops.e2e_routes_mounted(base, opener=answering(b'{"enabled": false}'))
    assert not dfeops.e2e_routes_mounted(base, opener=answering(b"<html>"))
    assert not dfeops.e2e_routes_mounted(base, opener=refusing)


def _staged_ui(monkeypatch, *, routes: bool, root_rc: int) -> list[tuple[list[str], dict]]:
    """cmd_ui up to its two subprocesses, with the cluster-facing steps answered."""
    calls: list[tuple[list[str], dict]] = []

    def run(cmd, cwd=None, env=None, **_):
        calls.append((cmd, env or {}))
        rc = root_rc if str(dfeops.ONBOARDING_RUNNER) in cmd else 0
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr(dfeops, "_console_urls",
                        lambda kube, ns: ("https://dfe.single.example.test", ""))
    monkeypatch.setattr(dfeops, "_resolves", lambda host: True)
    monkeypatch.setattr(dfeops, "_forward", lambda *a, **k: None)
    monkeypatch.setattr(dfeops, "_forward_ready", lambda *a, **k: True)
    # Forwards are stubbed, so the host's own port state has no bearing on these runs.
    monkeypatch.setattr(dfeops, "_taken_ports", lambda _ports: [])
    monkeypatch.setattr(dfeops, "e2e_routes_mounted", lambda url: routes)
    monkeypatch.setattr(dfeops, "admin_credential", lambda *a, **k: ("admin", "minted-pw"))
    monkeypatch.setattr(dfeops.subprocess, "run", run)
    return calls


def test_ui_refuses_a_deployment_without_the_e2e_routes(monkeypatch) -> None:
    calls = _staged_ui(monkeypatch, routes=False, root_rc=0)
    args = dfeops.build_parser().parse_args(["ui", "--ui-repo", "/nonexistent/dfe-ui"])
    assert dfeops.cmd_ui(args) == 1
    assert calls == []


def test_a_failed_root_step_stops_the_run(monkeypatch) -> None:
    calls = _staged_ui(monkeypatch, routes=True, root_rc=3)
    args = dfeops.build_parser().parse_args(["ui", "--ui-repo", "/nonexistent/dfe-ui"])
    assert dfeops.cmd_ui(args) == 3
    assert len(calls) == 1
    assert str(dfeops.ONBOARDING_RUNNER) in calls[0][0]


def test_the_root_step_signs_in_with_the_minted_password_then_the_suite_runs(monkeypatch) -> None:
    calls = _staged_ui(monkeypatch, routes=True, root_rc=0)
    args = dfeops.build_parser().parse_args(["ui", "--ui-repo", "/nonexistent/dfe-ui"])
    assert dfeops.cmd_ui(args) == 0
    (root_cmd, root_env), (suite_cmd, suite_env) = calls
    assert str(dfeops.ONBOARDING_RUNNER) in root_cmd
    assert root_cmd[root_cmd.index("--ui-url") + 1] == "https://dfe.single.example.test"
    assert root_env[dfeops.FED_ADMIN_PASSWORD_VAR] == "minted-pw"
    assert suite_cmd[:3] == ["yarn", "playwright", "test"]
    assert suite_cmd[suite_cmd.index("--grep-invert") + 1] == "@docker-only"
    assert suite_env["BASE_URL"] == "https://dfe.single.example.test"
    assert suite_env["NEXT_PUBLIC_API_URL"] == "http://localhost:18000"
    assert suite_env["E2E_ADMIN_PASSWORD"] == dfeops.E2E_SEED_PASSWORD


def test_skipping_onboarding_goes_straight_to_the_suite(monkeypatch) -> None:
    calls = _staged_ui(monkeypatch, routes=True, root_rc=1)
    args = dfeops.build_parser().parse_args(
        ["ui", "--ui-repo", "/nonexistent/dfe-ui", "--skip-onboarding"])
    assert dfeops.cmd_ui(args) == 0
    assert [cmd[:3] for cmd, _ in calls] == [["yarn", "playwright", "test"]]


def test_ui_refuses_a_console_host_that_does_not_resolve(monkeypatch) -> None:
    calls = _staged_ui(monkeypatch, routes=True, root_rc=0)
    monkeypatch.setattr(dfeops, "_resolves", lambda host: False)
    args = dfeops.build_parser().parse_args(["ui", "--ui-repo", "/nonexistent/dfe-ui"])
    assert dfeops.cmd_ui(args) == 1
    assert calls == []
