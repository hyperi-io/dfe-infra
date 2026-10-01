#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_argocd_upgrade.py
#  Purpose:      Prove bootstrap.sh upgrades the Argo CD release it installed,
#                with the pinned chart and every install flag, and leaves an
#                Argo it adopted untouched.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""bootstrap.sh step [6/7]: install, upgrade our own, or adopt.

An adopted Argo used to be every Argo already running, the one bootstrap had
installed on the previous run included, so a flag or a chart pin added to the
install never reached a live cluster. The step now asks
bootstrap/argocd_release.py whether the release is its own first.

The decision runs for real: the step's own lines, cut out of bootstrap.sh, run
under bash with a fake `helm` and `kubectl` first on PATH answering from a
fixture, so what is under test is the script as it ships.

    python3 -m pytest scripts/tests/test_argocd_upgrade.py -q
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP_DIR = REPO_ROOT / "bootstrap"
BOOTSTRAP = BOOTSTRAP_DIR / "bootstrap.sh"
COMMON_VALUES = REPO_ROOT / "argocd" / "values" / "common.yaml"

STEP_START = 'echo "==> [6/7] ArgoCD'
STEP_END = "# argocd-secret exists only once Argo"
ARGO_INSTALL = "helm upgrade --install argocd argo/argo-cd"
HELPERS = ("run", "dfe_have_crd", "dfe_should_install")

DOMAIN = "single.dfe.test"
CACHE_HOST = "valkey.argocd.svc.cluster.local"
# The user-supplied values a release bootstrap.sh installed carries.
OUR_VALUES = {"redis": {"enabled": False}, "externalRedis": {"host": CACHE_HOST, "port": 6379}}


def _load_release_module():
    spec = importlib.util.spec_from_file_location("argocd_release", BOOTSTRAP_DIR / "argocd_release.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["argocd_release"] = module
    spec.loader.exec_module(module)
    return module


argocd_release = _load_release_module()


def pinned_version() -> str:
    out = subprocess.run(
        [sys.executable, str(BOOTSTRAP_DIR / "read_versions.py"),
         "--file", str(REPO_ROOT / "versions.yaml"), "bootstrap.argocd"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return out.stdout.strip()


# --- the verdict -------------------------------------------------------------
def release(chart: str = "argo-cd-9.5.21") -> list[dict]:
    return [{"name": "argocd", "namespace": "argocd", "status": "deployed", "chart": chart}]


def test_the_release_this_bootstrap_installs_is_ours() -> None:
    ours, why = argocd_release.verdict(release(), OUR_VALUES, "valkey")
    assert ours, why
    assert CACHE_HOST in why


def test_a_stock_helm_install_of_the_same_name_and_chart_is_not_ours() -> None:
    """`helm install argocd argo/argo-cd -n argocd` is the upstream quick start."""
    ours, why = argocd_release.verdict(release(), None, "valkey")
    assert not ours
    assert "did not install it" in why


def test_an_argo_wired_to_another_cache_is_not_ours() -> None:
    values = {"redis": {"enabled": False}, "externalRedis": {"host": "redis.shared.svc"}}
    assert not argocd_release.verdict(release(), values, "valkey")[0]


def test_the_bundled_redis_left_on_is_not_ours() -> None:
    values = {"externalRedis": {"host": CACHE_HOST}}
    assert not argocd_release.verdict(release(), values, "valkey")[0]


def test_a_release_named_argocd_on_another_chart_is_not_ours() -> None:
    ours, why = argocd_release.verdict(release("platform-argo-2.0.0"), OUR_VALUES, "valkey")
    assert not ours
    assert "platform-argo-2.0.0" in why


def test_no_release_is_not_ours() -> None:
    ours, why = argocd_release.verdict([], None, "valkey")
    assert not ours
    assert "no helm release argocd" in why


def test_a_renamed_cache_service_is_followed() -> None:
    """DFE_VALKEY_SERVICE renames the Service; the wiring moves with it."""
    values = {"redis": {"enabled": False}, "externalRedis": {"host": "cache.argocd.svc.cluster.local"}}
    assert argocd_release.verdict(release(), values, "cache")[0]
    assert not argocd_release.verdict(release(), values, "valkey")[0]


@pytest.mark.parametrize(
    ("chart", "name"),
    [("argo-cd-10.9.0", "argo-cd"), ("argo-cd-10.0.0-rc1", "argo-cd"), ("argocd", "argocd")],
)
def test_the_chart_name_is_read_off_helm_lists_name_dash_version(chart: str, name: str) -> None:
    assert argocd_release.chart_name(chart) == name


# --- the install command and the verdict cannot drift apart ------------------
def install_command() -> str:
    body = BOOTSTRAP.read_text(encoding="utf-8")
    start = body.find(ARGO_INSTALL)
    assert start >= 0, f"bootstrap.sh no longer carries {ARGO_INSTALL!r}"
    return body[start:body.find("--wait", start)]


def install_values(valkey: str) -> dict:
    """The --set values the install passes, typed the way helm types them."""
    values: dict = {}
    for key, raw in re.findall(r'--set "?([A-Za-z.]+)=([^"\s\\]+)"?', install_command()):
        value = {"true": True, "false": False}.get(raw, raw.replace("${VALKEY_SVC}", valkey))
        node = values
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = int(value) if isinstance(value, str) and value.isdigit() else value
    return values


def test_the_install_bootstrap_runs_is_one_it_then_recognises() -> None:
    """A change to the cache wiring in the install must move the verdict with it."""
    values = install_values("valkey")
    assert values["externalRedis"]["host"] == CACHE_HOST
    ours, why = argocd_release.verdict(release(), values, "valkey")
    assert ours, why


def test_argo_is_told_the_name_the_gateway_publishes_it_under() -> None:
    label = yaml.safe_load(COMMON_VALUES.read_text(encoding="utf-8"))["hostnames"]["argocd"]
    assert f'--set-string "global.domain={label}.${{DFE_DOMAIN}}"' in install_command()


# --- the step, run under bash against a fake cluster --------------------------
FAKE_HELM = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write("helm " + " ".join(args) + "\\n")
fixture = json.load(open(os.environ["FAKE_FIXTURE"], encoding="utf-8"))
if args[:1] == ["list"]:
    print(json.dumps(fixture.get("releases", [])))
elif args[:2] == ["get", "values"]:
    if not fixture.get("releases"):
        print("Error: release: not found", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(fixture.get("values")))
"""

FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write("kubectl " + " ".join(args) + "\\n")
fixture = json.load(open(os.environ["FAKE_FIXTURE"], encoding="utf-8"))
if "crd" in args:
    sys.exit(0 if fixture.get("crd") else 1)
if "deploy" in args:
    sys.exit(0 if fixture.get("server") else 1)
"""


def _function(lines: list[str], name: str) -> list[str]:
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    if lines[start].rstrip().endswith("}"):
        return [lines[start]]
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return lines[start:end + 1]


def step_script() -> str:
    """bootstrap.sh's own helpers and step [6/7], as it ships."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(STEP_START))
    end = next(i for i in range(start, len(lines)) if lines[i].startswith(STEP_END))
    helpers = [line for name in HELPERS for line in _function(lines, name)]
    return "\n".join(["set -euo pipefail", *helpers, *lines[start:end]]) + "\n"


def run_step(tmp_path: Path, fixture: dict) -> tuple[subprocess.CompletedProcess, list[str]]:
    """The step against one fake cluster; returns its output and every call it made."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    for name, body in (("helm", FAKE_HELM), ("kubectl", FAKE_KUBECTL)):
        path = bindir / name
        path.write_text(body, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    (tmp_path / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
    log = tmp_path / "calls.log"
    log.touch()
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_FIXTURE": str(tmp_path / "fixture.json"),
        "FAKE_LOG": str(log),
        "SCRIPT_DIR": str(BOOTSTRAP_DIR),
        "VALKEY_SVC": "valkey",
        "ARGOCD_VERSION": pinned_version(),
        "DFE_DOMAIN": DOMAIN,
    }
    out = subprocess.run(
        ["bash", "-c", step_script()], env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return out, log.read_text(encoding="utf-8").splitlines()


def helm_upgrades(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("helm upgrade --install argocd argo/argo-cd")]


OURS = {"releases": release(), "values": OUR_VALUES, "crd": True, "server": True}


def test_our_own_release_is_upgraded_in_place(tmp_path: Path) -> None:
    out, calls = run_step(tmp_path, OURS)
    assert out.returncode == 0, out.stderr
    assert "UPGRADE in place" in out.stdout
    assert "ADOPT" not in out.stdout
    assert len(helm_upgrades(calls)) == 1, calls


def test_the_upgrade_carries_the_pinned_chart_and_every_install_flag(tmp_path: Path) -> None:
    _, calls = run_step(tmp_path, OURS)
    (upgrade,) = helm_upgrades(calls)
    argv = upgrade.split()
    assert argv[argv.index("--version") + 1] == pinned_version()
    assert "configs.params.server\\.insecure=true" in argv
    assert f"global.domain=argocd.{DOMAIN}" in argv
    assert f"externalRedis.host={CACHE_HOST}" in argv
    assert "redis.enabled=false" in argv


def test_the_upgrade_is_the_same_command_as_a_fresh_install(tmp_path: Path) -> None:
    """One command for both, so an upgrade can never lag the install by a flag."""
    _, upgraded = run_step(tmp_path / "ours", OURS)
    _, installed = run_step(tmp_path / "fresh", {"crd": False, "server": False})
    assert helm_upgrades(upgraded) == helm_upgrades(installed)


def test_a_foreign_stock_argo_is_adopted_and_never_touched(tmp_path: Path) -> None:
    fixture = {**OURS, "values": None}
    out, calls = run_step(tmp_path, fixture)
    assert out.returncode == 0, out.stderr
    assert helm_upgrades(calls) == [], calls
    assert "ADOPT existing" in out.stdout
    assert "Using existing ArgoCD" in out.stdout


def test_an_argo_no_helm_release_owns_is_adopted_and_never_touched(tmp_path: Path) -> None:
    out, calls = run_step(tmp_path, {"releases": [], "crd": True, "server": True})
    assert out.returncode == 0, out.stderr
    assert helm_upgrades(calls) == [], calls
    assert "Using existing ArgoCD" in out.stdout


def test_a_bare_cluster_gets_the_install(tmp_path: Path) -> None:
    out, calls = run_step(tmp_path, {"crd": False, "server": False})
    assert out.returncode == 0, out.stderr
    assert "not detected -> INSTALL" in out.stdout
    assert len(helm_upgrades(calls)) == 1, calls
