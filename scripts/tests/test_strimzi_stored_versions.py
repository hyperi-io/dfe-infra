#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_strimzi_stored_versions.py
#  Purpose:      Prove a fresh install (preflight and stack-deploy) refuses
#                Strimzi CRDs stored at a version the 1.x operator no longer
#                serves, that a teardown removes the Strimzi CRDs once no
#                Strimzi resource is left, and that stack-deploy refuses a kube
#                context that differs from the env file's.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Strimzi CRDs that outlive their install.

A cluster that once ran a 0.x Strimzi keeps its CRDs after a teardown, still
listing v1beta2 in status.storedVersions. The 1.x operator chart cannot apply
over them, so its Application goes Unknown, no broker starts and the readiness
gate times out -- while the cluster preflight passed.

The preflight and stack-deploy halves drive `dfe-ops preflight` and `dfe-ops
stack-deploy` through their one subprocess seam, `_run_text`. The teardown half
runs bootstrap/destroy.sh against a fake `kubectl` first on PATH that logs every
call. No cluster is involved.

    python3 scripts/tests/test_strimzi_stored_versions.py
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple
from unittest import mock

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DESTROY = REPO_ROOT / "bootstrap" / "destroy.sh"

sys.path.insert(0, str(REPO_ROOT / "bootstrap"))
import argocd_release  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("dfeops_strimzi", str(REPO_ROOT / "scripts" / "dfe-ops"))
_spec = importlib.util.spec_from_loader("dfeops_strimzi", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_strimzi"] = dfeops
_loader.exec_module(dfeops)

import dfe_ops_upgrade  # noqa: E402

STALE = "kafkamirrormaker2s.kafka.strimzi.io"
OTHER_CRD = "certificates.cert-manager.io"


# --- the cluster preflight ----------------------------------------------------
def _crd(name: str, stored: list[str]) -> dict:
    return {"metadata": {"name": name}, "status": {"storedVersions": stored}}


def _strimzi_crds(stale: str = "", stored: list[str] | None = None) -> list[dict]:
    """Every Strimzi CRD the 1.x operator needs on v1, except `stale`, plus a
    cert-manager CRD left behind the same way."""
    crds = [_crd(name, (stored or []) if name == stale else ["v1"]) for name in dfe_ops_upgrade.STRIMZI_CRDS]
    return [*crds, _crd(OTHER_CRD, ["v1"])]


def _cluster(crds: list[dict]) -> Callable[..., tuple[int, str, str]]:
    """A `_run_text` that answers every kubectl read preflight makes."""
    node = {
        "metadata": {"name": "node-0", "labels": {}},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "allocatable": {"cpu": "64", "memory": "256Gi"},
        },
    }
    default_sc = {
        "metadata": {
            "name": "standard",
            "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
        }
    }
    answers = {
        ("version",): {"serverVersion": {"gitVersion": "v1.35.1", "major": "1", "minor": "35"}},
        ("get", "nodes"): {"items": [node, node, node]},
        ("get", "storageclass"): {"items": [default_sc]},
        ("get", "deployments"): {"items": [{"metadata": {"name": "metallb-controller"}}]},
        ("get", "services"): {"items": []},
        ("get", "crd"): {"items": crds},
    }

    def run_text(cmd: list[str], env: dict | None = None) -> tuple[int, str, str]:
        argv = cmd[cmd.index("kubectl") + 1 :]
        while argv[:1] == ["--kubeconfig"] or argv[:1] == ["--context"]:
            argv = argv[2:]
        if argv[:2] == ["auth", "can-i"]:
            return 0, "yes\n", ""
        for key, doc in answers.items():
            if tuple(argv[: len(key)]) == key:
                return 0, json.dumps(doc), ""
        return 1, "", f"unexpected kubectl call: {cmd}"

    return run_text


def run_preflight(crds: list[dict]) -> tuple[int, str]:
    """(exit code, stderr) of `dfe-ops preflight` against a cluster carrying `crds`."""
    args = dfeops.build_parser().parse_args([
        "preflight", "--mode", "scale", "--kubeconfig", "/nonexistent/kubeconfig",
        "--min-k8s", "1.30", "--registry", "registry.example.invalid",
    ])
    err = io.StringIO()
    with (
        mock.patch.object(dfeops, "_run_text", _cluster(crds)),
        mock.patch.object(dfeops.shutil, "which", return_value="/usr/bin/kubectl"),
        mock.patch.object(argocd_release, "read", return_value=(False, "no release")),
        mock.patch.object(urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")),
        contextlib.redirect_stderr(err),
    ):
        rc = dfeops.cmd_preflight(args)
    return rc, err.getvalue()


def test_preflight_fails_on_a_crd_stored_at_a_removed_version() -> None:
    rc, out = run_preflight(_strimzi_crds(STALE, ["v1beta2"]))
    expect("preflight fails", rc == 1, f"rc={rc}\n{out}")
    expect(
        "it names the CRD and what it stores",
        f"{STALE} (stored: v1beta2)" in out,
        out,
    )
    expect(
        "it gives the exact delete for the stale CRD",
        f"delete crd {STALE}" in out,
        out,
    )
    expect(
        "the remedy deletes nothing outside Strimzi",
        f"delete crd {OTHER_CRD}" not in out and "kafkas.kafka.strimzi.io (stored" not in out,
        out,
    )


def test_preflight_passes_when_every_stored_version_is_served() -> None:
    rc, out = run_preflight(_strimzi_crds())
    expect("preflight passes", rc == 0, f"rc={rc}\n{out}")
    expect("and says what it read", "Strimzi CRD(s) store v1 only" in out, out)


def test_preflight_passes_with_no_strimzi_crds() -> None:
    rc, out = run_preflight([_crd(OTHER_CRD, ["v1"])])
    expect("preflight passes on a cluster with no Strimzi", rc == 0, f"rc={rc}\n{out}")


# --- stack-deploy -------------------------------------------------------------
class StackDeploy(NamedTuple):
    rc: int
    out: str
    bootstrap_ran: bool
    gate_ran: bool
    kubectl: list[list[str]]
    kubeconfig: str


def run_stack_deploy(
    crds: list[dict] | None,
    *flags: str,
    env_file: str = "",
    environ: dict[str, str] | None = None,
    current_context: str | None = None,
    kubectl_on_path: bool = True,
) -> StackDeploy:
    """`dfe-ops stack-deploy` past its offline pre-flight, against a cluster carrying
    `crds` (None: a cluster whose CRD list cannot be read). bootstrap.sh is a stub
    that records it ran.

    `env_file` is the text of an --env-file and `environ` extra process environment.
    `current_context` is what the kubeconfig's current context reads as (None: unset).
    """
    kubectl: list[list[str]] = []
    ran = {"bootstrap": False, "gate": False}
    cluster = _cluster(crds or [])
    real_gate = dfeops._strimzi_deploy_gate

    def run_text(cmd: list[str], env: dict | None = None) -> tuple[int, str, str]:
        kubectl.append(cmd)
        if cmd[-2:] == ["config", "current-context"]:
            if current_context is None:
                return 1, "", "error: current-context is not set"
            return 0, f"{current_context}\n", ""
        if crds is None:
            return 1, "", "connection refused"
        return cluster(cmd, env)

    def bootstrap(cmd: list[str], *, env: dict | None = None) -> int:
        ran["bootstrap"] = True
        return 0

    def gate(kubeconfig: str) -> bool:
        ran["gate"] = True
        return real_gate(kubeconfig)

    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("DFE_")}
    err = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp:
        kubeconfig = Path(tmp) / "kubeconfig"
        kubeconfig.write_text("", encoding="utf-8", newline="\n")
        env_flags: list[str] = []
        if env_file:
            env_path = Path(tmp) / "deploy.env"
            env_path.write_text(env_file, encoding="utf-8", newline="\n")
            env_flags = ["--env-file", str(env_path)]
        args = dfeops.build_parser().parse_args([
            "stack-deploy", "--mode", "single", "--stack", "2.2.0-rc.99",
            "--kubeconfig", str(kubeconfig), "--access-out", str(Path(tmp) / "access.md"),
            *env_flags, *flags,
        ])
        with (
            mock.patch.dict(os.environ, {**clean_env, **(environ or {})}, clear=True),
            mock.patch.object(dfeops, "_resolve_stack", return_value=0),
            mock.patch.object(dfeops, "_offline_preflight", return_value=0),
            mock.patch.object(dfeops, "_bootstrap_required", return_value=()),
            mock.patch.object(dfeops, "_require_script", return_value=Path(tmp) / "bootstrap.sh"),
            mock.patch.object(dfeops, "_run_streaming", bootstrap),
            mock.patch.object(dfeops, "_run_text", run_text),
            mock.patch.object(dfeops, "_strimzi_deploy_gate", gate),
            mock.patch.object(
                dfeops.shutil, "which", return_value="/usr/bin/kubectl" if kubectl_on_path else None
            ),
            contextlib.redirect_stderr(err),
        ):
            rc = dfeops.cmd_stack_deploy(args)
    return StackDeploy(rc, err.getvalue(), ran["bootstrap"], ran["gate"], kubectl, str(kubeconfig))


def test_stack_deploy_stops_on_a_crd_stored_at_a_removed_version() -> None:
    run = run_stack_deploy(_strimzi_crds(STALE, ["v1beta2"]))
    expect("stack-deploy fails", run.rc == 1, f"rc={run.rc}\n{run.out}")
    expect("bootstrap.sh never runs", not run.bootstrap_ran, run.out)
    expect(
        "it names the CRD and what it stores",
        f"{STALE} (stored: v1beta2)" in run.out,
        run.out,
    )
    expect(
        "it gives the exact delete, aimed at the kubeconfig the deploy uses",
        f"kubectl --kubeconfig {run.kubeconfig} delete crd {STALE}" in run.out,
        run.out,
    )
    expect(
        "the remedy deletes nothing outside Strimzi",
        f"delete crd {OTHER_CRD}" not in run.out and "kafkas.kafka.strimzi.io (stored" not in run.out,
        run.out,
    )
    expect(
        "the check read the CRD list and changed nothing",
        any(argv[3:5] == ["get", "crd"] for argv in run.kubectl)
        and all("delete" not in argv for argv in run.kubectl),
        f"{run.kubectl}",
    )
    expect(
        "every read aims at the deploy's kubeconfig",
        all(argv[1:3] == ["--kubeconfig", run.kubeconfig] for argv in run.kubectl),
        f"{run.kubectl}",
    )


def test_stack_deploy_goes_on_when_every_stored_version_is_served() -> None:
    run = run_stack_deploy(_strimzi_crds())
    expect("stack-deploy passes", run.rc == 0, f"rc={run.rc}\n{run.out}")
    expect("bootstrap.sh runs", run.bootstrap_ran, run.out)
    expect("and the run says what it read", "Strimzi CRD(s) store v1 only" in run.out, run.out)


def test_stack_deploy_warns_and_goes_on_when_the_crds_cannot_be_listed() -> None:
    """bootstrap.sh fails on its own first kubectl call, and says why."""
    run = run_stack_deploy(None)
    expect("stack-deploy passes", run.rc == 0, f"rc={run.rc}\n{run.out}")
    expect("bootstrap.sh runs", run.bootstrap_ran, run.out)
    expect("the skipped check is said out loud", "Strimzi stored-version check skipped" in run.out, run.out)


def test_stack_deploy_check_only_contacts_no_cluster() -> None:
    run = run_stack_deploy(_strimzi_crds(STALE, ["v1beta2"]), "--check-only")
    expect("check-only passes", run.rc == 0, f"rc={run.rc}\n{run.out}")
    expect("no kubectl call is made", run.kubectl == [], f"{run.kubectl}")
    expect("bootstrap.sh never runs", not run.bootstrap_ran, run.out)


# --- stack-deploy: the kube context -------------------------------------------
CONTEXT_FILE = 'DFE_KUBE_CONTEXT="ctx-a"\n'


def _context_reads(run: StackDeploy) -> list[list[str]]:
    return [argv for argv in run.kubectl if argv[-2:] == ["config", "current-context"]]


def _deployed_nothing(run: StackDeploy) -> bool:
    return not run.gate_ran and not run.bootstrap_ran


def test_stack_deploy_refuses_a_context_other_than_the_env_files() -> None:
    run = run_stack_deploy(_strimzi_crds(), env_file=CONTEXT_FILE, current_context="ctx-b")
    expect("stack-deploy fails", run.rc == 1, f"rc={run.rc}\n{run.out}")
    expect("it names the context the env wants", "DFE_KUBE_CONTEXT is ctx-a" in run.out, run.out)
    expect("and the one the kubeconfig is on", "current context is ctx-b" in run.out, run.out)
    expect(
        "it gives the switch, aimed at the deploy's kubeconfig",
        f"kubectl --kubeconfig {run.kubeconfig} config use-context ctx-a" in run.out
        and "fix DFE_KUBE_CONTEXT in the env file" in run.out,
        run.out,
    )
    expect("neither the Strimzi gate nor bootstrap.sh runs", _deployed_nothing(run), run.out)
    expect(
        "the one cluster call is a read of the deploy's kubeconfig, and nothing is switched",
        run.kubectl == [["kubectl", "--kubeconfig", run.kubeconfig, "config", "current-context"]],
        f"{run.kubectl}",
    )


def test_stack_deploy_refuses_a_context_other_than_the_environments() -> None:
    run = run_stack_deploy(
        _strimzi_crds(), environ={"DFE_KUBE_CONTEXT": "ctx-a"}, current_context="ctx-b"
    )
    expect("stack-deploy fails", run.rc == 1, f"rc={run.rc}\n{run.out}")
    expect(
        "it names both contexts",
        "DFE_KUBE_CONTEXT is ctx-a" in run.out and "current context is ctx-b" in run.out,
        run.out,
    )
    expect("neither the Strimzi gate nor bootstrap.sh runs", _deployed_nothing(run), run.out)


def test_stack_deploy_goes_on_when_the_context_is_the_env_files() -> None:
    run = run_stack_deploy(_strimzi_crds(), env_file=CONTEXT_FILE, current_context="ctx-a")
    expect("stack-deploy passes", run.rc == 0, f"rc={run.rc}\n{run.out}")
    expect("the context was read once", len(_context_reads(run)) == 1, f"{run.kubectl}")
    expect("the Strimzi gate runs", run.gate_ran, run.out)
    expect("bootstrap.sh runs", run.bootstrap_ran, run.out)


def test_stack_deploy_reads_no_context_when_the_env_names_none() -> None:
    """The shipped env template carries DFE_KUBE_CONTEXT="": unset, not a context named ""."""
    for label, env_file in (
        ("no DFE_KUBE_CONTEXT", ""),
        ("an empty DFE_KUBE_CONTEXT", 'DFE_KUBE_CONTEXT=""\n'),
    ):
        run = run_stack_deploy(_strimzi_crds(), env_file=env_file, current_context="ctx-b")
        expect(f"{label}: stack-deploy passes", run.rc == 0, f"rc={run.rc}\n{run.out}")
        expect(f"{label}: no context is read", _context_reads(run) == [], f"{run.kubectl}")
        expect(f"{label}: the gate runs", run.gate_ran, run.out)
        expect(f"{label}: bootstrap.sh runs", run.bootstrap_ran, run.out)


def test_stack_deploy_check_only_skips_the_context_check() -> None:
    run = run_stack_deploy(
        _strimzi_crds(), "--check-only", env_file=CONTEXT_FILE, current_context="ctx-b"
    )
    expect("check-only passes", run.rc == 0, f"rc={run.rc}\n{run.out}")
    expect("no kubectl call is made", run.kubectl == [], f"{run.kubectl}")
    expect("bootstrap.sh never runs", not run.bootstrap_ran, run.out)


def test_stack_deploy_refuses_when_the_current_context_cannot_be_read() -> None:
    """A guard that could not run has not passed."""
    run = run_stack_deploy(_strimzi_crds(), env_file=CONTEXT_FILE, current_context=None)
    expect("stack-deploy fails", run.rc == 1, f"rc={run.rc}\n{run.out}")
    expect(
        "it says what kubectl answered",
        "DFE_KUBE_CONTEXT is ctx-a" in run.out and "error: current-context is not set" in run.out,
        run.out,
    )
    expect("neither the Strimzi gate nor bootstrap.sh runs", _deployed_nothing(run), run.out)


def test_stack_deploy_refuses_when_kubectl_is_missing_and_a_context_is_named() -> None:
    run = run_stack_deploy(_strimzi_crds(), env_file=CONTEXT_FILE, kubectl_on_path=False)
    expect("stack-deploy fails", run.rc == 1, f"rc={run.rc}\n{run.out}")
    expect("it says kubectl is missing", "kubectl is not on PATH" in run.out, run.out)
    expect("no kubectl call is attempted", run.kubectl == [], f"{run.kubectl}")
    expect("neither the Strimzi gate nor bootstrap.sh runs", _deployed_nothing(run), run.out)


# --- the teardown -------------------------------------------------------------
FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
fixture = json.load(open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8"))
if args == ["get", "crd", "-o", "name"]:
    for name in fixture["crds"]:
        print("customresourcedefinition.apiextensions.k8s.io/" + name)
elif len(args) > 1 and args[0] == "get" and args[1] in fixture["crds"]:
    for line in fixture["held"].get(args[1], []):
        print(line)
sys.exit(0)
"""

TEARDOWN_CRDS = [
    "kafkas.kafka.strimzi.io",
    STALE,
    "strimzipodsets.core.strimzi.io",
    OTHER_CRD,
    "applications.argoproj.io",
]


def run_teardown(held: dict[str, list[str]]) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    """destroy.sh --force against a cluster holding `held` Strimzi resources:
    (the run, every kubectl argv it issued, in order)."""
    with tempfile.TemporaryDirectory() as tmp:
        bindir = Path(tmp)
        kubectl = bindir / "kubectl"
        kubectl.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
        kubectl.chmod(0o755)
        # helm uninstalls answer nothing, and the settle pauses are not under test.
        for name in ("helm", "sleep"):
            stub = bindir / name
            stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
            stub.chmod(0o755)
        fixture = bindir / "fixture.json"
        fixture.write_text(json.dumps({"crds": TEARDOWN_CRDS, "held": held}), encoding="utf-8", newline="\n")
        log = bindir / "kubectl.log"
        log.touch()
        env = dict(os.environ)
        env.pop("DFE_DRY_RUN", None)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["FAKE_KUBECTL_FIXTURE"] = str(fixture)
        env["FAKE_KUBECTL_LOG"] = str(log)
        result = subprocess.run(
            ["bash", str(DESTROY), "--force"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
        calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    return result, calls


def _first(calls: list[list[str]], prefix: list[str]) -> int:
    return next((i for i, argv in enumerate(calls) if argv[: len(prefix)] == prefix), -1)


def test_teardown_deletes_the_strimzi_crds_after_the_kafka_resources() -> None:
    result, calls = run_teardown({})
    expect("destroy.sh exits 0", result.returncode == 0, result.stderr)
    kafka = _first(calls, ["-n", "strimzi", "delete", "kafka", "--all"])
    namespace = _first(calls, ["delete", "ns", "strimzi"])
    crd = _first(calls, ["delete", "crd"])
    expect("the Kafka resources are deleted", kafka >= 0, f"{calls}")
    expect("a CRD delete is issued", crd >= 0, f"{calls}")
    expect(
        "after the Kafka resources and the strimzi namespace are gone",
        0 <= kafka < crd and 0 <= namespace < crd,
        f"kafka={kafka} namespace={namespace} crd={crd}",
    )
    named = {arg for arg in calls[crd][2:] if not arg.startswith("-")} if crd >= 0 else set()
    expect(
        "it names exactly the strimzi.io CRDs",
        named == {c for c in TEARDOWN_CRDS if c.endswith(".strimzi.io")},
        f"{named}",
    )
    expect(
        "and no other CRD is ever deleted",
        all(argv[:2] != ["delete", "crd"] or OTHER_CRD not in argv for argv in calls),
        f"{calls}",
    )


def test_teardown_keeps_strimzi_crds_another_tenant_still_uses() -> None:
    """Deleting a CRD deletes every resource of its kind on the cluster."""
    result, calls = run_teardown({"kafkas.kafka.strimzi.io": ["kafka.kafka.strimzi.io/other-tenant"]})
    expect("destroy.sh exits 0", result.returncode == 0, result.stderr)
    expect("no CRD is deleted", _first(calls, ["delete", "crd"]) == -1, f"{calls}")
    expect(
        "and the run says which CRD kept them",
        re.search(r"\bkafkas\.kafka\.strimzi\.io\b", result.stdout) is not None,
        result.stdout,
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
