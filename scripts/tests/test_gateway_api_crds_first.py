#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_gateway_api_crds_first.py
#  Purpose:      Prove bootstrap.sh lays the Gateway API CRDs before it installs
#                cert-manager, from the chart and release the Envoy Gateway
#                operator app installs.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""bootstrap.sh step [1d/7]: the Gateway API CRDs land before cert-manager.

cert-manager runs with Gateway API support on, and its controller exits at start
when the CRDs are absent. The operator app ships them only at [7/7], so without
[1d/7] the controller crash-loops through [2/7]'s readiness wait, and the install
passes only when a readiness poll lands between two restarts.

The steps run for real: bootstrap.sh's own lines from [1c/7] to [3/7] run under
bash with a fake `helm` and `kubectl` first on PATH, logging every call.

    python3 -m pytest scripts/tests/test_gateway_api_crds_first.py -q
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from _bootstrap import function_lines

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP_DIR = REPO_ROOT / "bootstrap"
BOOTSTRAP = BOOTSTRAP_DIR / "bootstrap.sh"
OPERATOR_APP = REPO_ROOT / "argocd" / "bootstrap" / "envoy-gateway-app.yaml"

STEPS = ('echo "==> [1c/7]', 'echo "==> [3/7]')
HELPERS = ("run", "dfe_have_crd", "dfe_release_values", "dfe_should_install")
# bootstrap.sh's own lines that name the operator chart and resolve its release.
OPERATOR_PINS = re.compile(r"(?m)^ENVOY_GATEWAY_[A-Z_]+=.*$")
GATEWAYS_CRD = "gateways.gateway.networking.k8s.io"

FAKE_HELM = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": ["helm", *args]}) + "\\n")
fixture = json.load(open(os.environ["FAKE_FIXTURE"], encoding="utf-8"))
if args[:2] == ["show", "crds"]:
    if fixture.get("pull_fails"):
        print("Error: failed to fetch the chart", file=sys.stderr)
        sys.exit(1)
    for name in ("gatewayclasses", "gateways", "httproutes"):
        print("---")
        print("apiVersion: apiextensions.k8s.io/v1")
        print("kind: CustomResourceDefinition")
        print(f"metadata:\\n  name: {name}.gateway.networking.k8s.io")
"""

FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
call = {"argv": ["kubectl", *args]}
if args[:1] == ["apply"] and "-" in args:
    call["crds_on_stdin"] = sys.stdin.read().count("kind: CustomResourceDefinition")
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(call) + "\\n")
fixture = json.load(open(os.environ["FAKE_FIXTURE"], encoding="utf-8"))
if args[:2] == ["get", "crd"]:
    sys.exit(0 if args[2] in fixture.get("crds", []) else 1)
if "deploy" in args:
    sys.exit(1)
"""


def step_script() -> str:
    """bootstrap.sh's helpers, its operator chart pins, and steps [1c/7] to [2/7], as it ships."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    lines = text.splitlines()
    first = next(i for i, ln in enumerate(lines) if ln.startswith(STEPS[0]))
    last = next(i for i in range(first, len(lines)) if lines[i].startswith(STEPS[1]))
    helpers = [line for name in HELPERS for line in function_lines(lines, name)]
    pins = OPERATOR_PINS.findall(text)
    return "\n".join(["set -euo pipefail", *helpers, *pins, *lines[first:last]]) + "\n"


def pinned(key: str) -> str:
    out = subprocess.run(
        [sys.executable, str(BOOTSTRAP_DIR / "read_versions.py"),
         "--file", str(REPO_ROOT / "versions.yaml"), key],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    return out.stdout.strip()


def run_steps(
    tmp_path: Path, fixture: dict, extra_env: dict | None = None
) -> tuple[subprocess.CompletedProcess, list[dict]]:
    """The steps against one fake cluster; returns their output and every call they made."""
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
        "REPO_ROOT": str(REPO_ROOT),
        "CERT_MANAGER_VERSION": pinned("bootstrap.cert-manager"),
        "DFE_LABEL_WORKLOAD_NODES": "false",
        **(extra_env or {}),
    }
    out = subprocess.run(
        ["bash", "-c", step_script()], env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return out, calls


def first_call(calls: list[dict], *prefix: str) -> int | None:
    """Index of the first call whose argv starts with `prefix`, or None."""
    return next((i for i, c in enumerate(calls) if c["argv"][: len(prefix)] == list(prefix)), None)


def crd_applies(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c["argv"][:2] == ["kubectl", "apply"] and "crds_on_stdin" in c]


def operator_source() -> dict:
    return yaml.safe_load(OPERATOR_APP.read_text(encoding="utf-8"))["spec"]


BARE = {"crds": []}


def test_the_gateway_api_crds_are_established_before_cert_manager_installs(tmp_path: Path) -> None:
    out, calls = run_steps(tmp_path, BARE)
    assert out.returncode == 0, out.stderr
    cert_manager = first_call(calls, "helm", "upgrade", "--install", "cert-manager")
    pull = first_call(calls, "helm", "show", "crds")
    wait = first_call(calls, "kubectl", "wait", "--for", "condition=Established")
    assert cert_manager is not None, calls
    assert pull is not None, f"no Gateway API CRDs pulled before cert-manager: {calls}"
    (apply,) = crd_applies(calls)
    assert wait is not None, calls
    assert pull < calls.index(apply) < wait < cert_manager, calls
    assert apply["crds_on_stdin"] > 0, "the chart's CRDs never reached kubectl apply"
    assert f"crd/{GATEWAYS_CRD}" in calls[wait]["argv"]


def test_the_crds_come_from_the_chart_and_release_the_operator_app_installs(tmp_path: Path) -> None:
    source = operator_source()["source"]
    _, calls = run_steps(tmp_path, BARE)
    pull = first_call(calls, "helm", "show", "crds")
    assert pull is not None, calls
    argv = calls[pull]["argv"]
    assert argv[3] == f"oci://{source['repoURL']}/{source['chart']}"
    assert argv[argv.index("--version") + 1] == source["targetRevision"]
    assert source["targetRevision"] == pinned("operators.envoy-gateway")


def test_argo_takes_the_crds_over_as_the_field_manager_that_laid_them(tmp_path: Path) -> None:
    """A second field manager would keep any field a later chart drops alive in the CRD."""
    _, calls = run_steps(tmp_path, BARE)
    (apply,) = crd_applies(calls)
    assert "--server-side" in apply["argv"]
    assert "--field-manager=argocd-controller" in apply["argv"]
    sync_options = operator_source()["syncPolicy"]["syncOptions"]
    assert "ServerSideApply=true" in sync_options
    assert not any(option.startswith("Replace=") for option in sync_options)


def test_a_cluster_that_already_has_them_is_adopted(tmp_path: Path) -> None:
    out, calls = run_steps(tmp_path, {"crds": [GATEWAYS_CRD]})
    assert out.returncode == 0, out.stderr
    assert f"[gateway-api] CRD {GATEWAYS_CRD} present -> ADOPT" in out.stdout, out.stdout
    assert first_call(calls, "helm", "show", "crds") is None, calls
    assert crd_applies(calls) == [], calls
    assert first_call(calls, "helm", "upgrade", "--install", "cert-manager") is not None


def test_force_install_lays_them_over_ones_already_present(tmp_path: Path) -> None:
    out, calls = run_steps(tmp_path, {"crds": [GATEWAYS_CRD]}, {"DFE_FORCE_INSTALL": "true"})
    assert out.returncode == 0, out.stderr
    assert first_call(calls, "helm", "show", "crds") is not None, calls
    assert len(crd_applies(calls)) == 1, calls


def test_a_chart_that_cannot_be_pulled_stops_bootstrap_before_cert_manager(tmp_path: Path) -> None:
    out, calls = run_steps(tmp_path, {"crds": [], "pull_fails": True})
    assert out.returncode != 0, out.stdout
    assert first_call(calls, "helm", "upgrade", "--install", "cert-manager") is None, calls
