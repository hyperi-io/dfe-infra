#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_should_install.py
#  Purpose:      Prove bootstrap.sh adopts an operator installed from its upstream
#                manifest, whatever the manifest names its deployment
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for bootstrap.sh's dfe_should_install.

The gate decides adopt-or-install from the CRD and the operator's deployment. The
Helm chart names the deployment one thing and the upstream plain-manifest install
another (MetalLB's is `controller`, the chart's `metallb-controller`), so a
manifest install read as a lingering CRD and got a second controller beside it.
Each case runs the gate as bootstrap.sh calls it for the operator, against a fake
kubectl holding the cluster in a fixture.

    python3 -m pytest scripts/tests/test_should_install.py -q
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from _bootstrap import function_lines

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
HELPERS = ("dfe_have_crd", "dfe_have_ready_deploy", "dfe_should_install")

FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
fixture = json.load(open(os.environ["FAKE_FIXTURE"], encoding="utf-8"))
if args[:2] == ["get", "crd"]:
    sys.exit(0 if args[2] in fixture["crds"] else 1)
if args[0] == "-n" and args[2:4] == ["get", "deploy"]:
    ns = args[1]
    here = [d for d in fixture["deployments"] if d["ns"] == ns]
    if "-l" not in args:
        sys.exit(0 if any(d["name"] == args[4] for d in here) else 1)
    wanted = dict(pair.split("=", 1) for pair in args[args.index("-l") + 1].split(","))
    for d in here:
        if all(d["labels"].get(k) == v for k, v in wanted.items()):
            print(d.get("ready", ""))
    sys.exit(0)
sys.exit(1)
"""

# The `dfe_should_install ...` call each operator's step makes, read out of bootstrap.sh.
def gate_call(name: str) -> str:
    text = BOOTSTRAP.read_text(encoding="utf-8").replace("\\\n", " ")
    match = re.search(rf"if (dfe_should_install {re.escape(name)} [^;]+); then", text)
    assert match, name
    return re.sub(r"\s+", " ", match.group(1))


def run_gate(tmp_path: Path, name: str, fixture: dict, env: dict | None = None):
    """rc and output of the gate for operator `name` against `fixture`."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    kubectl = bindir / "kubectl"
    kubectl.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
    kubectl.chmod(0o755)
    (tmp_path / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    helpers = [line for helper in HELPERS for line in function_lines(lines, helper)]
    script = "\n".join(["set -uo pipefail", *helpers, f"{gate_call(name)}; echo \"rc=$?\""])
    out = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_FIXTURE": str(tmp_path / "fixture.json"),
            **(env or {}),
        },
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    rc = int(re.search(r"rc=(\d+)", out.stdout).group(1))
    return rc, out.stdout


INSTALL, ADOPT = 0, 1
METALLB_CRD = "ipaddresspools.metallb.io"
CERT_MANAGER_CRD = "certificates.cert-manager.io"
ESO_CRD = "clustersecretstores.external-secrets.io"

CASES = [
    pytest.param(
        "metallb", [METALLB_CRD],
        [{"ns": "metallb-system", "name": "metallb-controller", "labels": {}, "ready": "1"}],
        ADOPT, id="metallb_chart_named_deployment",
    ),
    pytest.param(
        "metallb", [METALLB_CRD],
        [{"ns": "metallb-system", "name": "controller",
          "labels": {"app": "metallb", "component": "controller"}, "ready": "1"}],
        ADOPT, id="metallb_upstream_manifest",
    ),
    pytest.param(
        "metallb", [METALLB_CRD],
        [{"ns": "metallb-system", "name": "controller",
          "labels": {"app.kubernetes.io/name": "metallb",
                     "app.kubernetes.io/component": "controller"}, "ready": "2"}],
        ADOPT, id="metallb_renamed_chart_release",
    ),
    pytest.param(
        "cert-manager", [CERT_MANAGER_CRD],
        [{"ns": "cert-manager", "name": "cm-controller",
          "labels": {"app.kubernetes.io/name": "cert-manager"}, "ready": "1"}],
        ADOPT, id="cert_manager_manifest_under_another_name",
    ),
    pytest.param(
        "external-secrets", [ESO_CRD],
        [{"ns": "external-secrets", "name": "eso",
          "labels": {"app.kubernetes.io/name": "external-secrets"}, "ready": "1"}],
        ADOPT, id="external_secrets_manifest_under_another_name",
    ),
    pytest.param(
        "metallb", [METALLB_CRD],
        [{"ns": "metallb-system", "name": "controller",
          "labels": {"app": "metallb", "component": "controller"}, "ready": ""}],
        INSTALL, id="labelled_deployment_with_no_ready_replica",
    ),
    pytest.param(
        "metallb", [METALLB_CRD],
        [{"ns": "kube-system", "name": "controller",
          "labels": {"app": "metallb", "component": "controller"}, "ready": "1"}],
        INSTALL, id="labelled_deployment_in_another_namespace",
    ),
    pytest.param(
        "metallb", [METALLB_CRD],
        [{"ns": "metallb-system", "name": "speaker-ui", "labels": {"app": "other"}, "ready": "1"}],
        INSTALL, id="unrelated_deployment_in_the_namespace",
    ),
    pytest.param("metallb", [METALLB_CRD], [], INSTALL, id="lingering_crd_and_nothing_else"),
    pytest.param(
        "cert-manager", [], [{"ns": "cert-manager", "name": "cert-manager", "labels": {}, "ready": "1"}],
        INSTALL, id="no_crd",
    ),
]


@pytest.mark.parametrize(("name", "crds", "deployments", "want"), CASES)
def test_the_gate_adopts_a_running_operator_whatever_installed_it(
    tmp_path, name, crds, deployments, want
):
    rc, _ = run_gate(tmp_path, name, {"crds": crds, "deployments": deployments})

    assert rc == want


def test_a_forced_install_wins_over_a_running_operator(tmp_path):
    fixture = {
        "crds": [METALLB_CRD],
        "deployments": [{"ns": "metallb-system", "name": "controller",
                         "labels": {"app": "metallb", "component": "controller"}, "ready": "1"}],
    }

    rc, out = run_gate(tmp_path, "metallb", fixture, env={"DFE_FORCE_INSTALL": "true"})

    assert rc == INSTALL
    assert "DFE_FORCE_INSTALL" in out


def test_the_adoption_names_why_it_skipped_the_install(tmp_path):
    fixture = {
        "crds": [METALLB_CRD],
        "deployments": [{"ns": "metallb-system", "name": "controller",
                         "labels": {"app": "metallb", "component": "controller"}, "ready": "1"}],
    }

    _, out = run_gate(tmp_path, "metallb", fixture)

    assert "ADOPT" in out
    assert "under another name" in out
