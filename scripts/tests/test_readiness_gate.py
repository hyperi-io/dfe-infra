#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_readiness_gate.py
#  Purpose:      Prove the deploy readiness gate judges the namespaces this
#                deploy owns, and reads a restart recency rather than any string
#                carrying an `m`.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Behaviour tests for bootstrap/smoke-test-readiness.sh.

The gate decides whether a deploy is declared up, so both of its ways of being
wrong cost a real run: a healthy pod whose lifetime restart count is hours old
was reported as live churn (`5h12m` ends in `m`), and a denylist of the
cluster's own namespaces failed the gate on whatever the distribution ships
that kube-system does not cover.

A fake `kubectl` first on PATH answers every query from a JSON fixture, so the
gate's own decisions are what is under test and no cluster is involved.

    python3 scripts/tests/test_readiness_gate.py

No third-party deps and no test runner, matching the script it tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATE = REPO_ROOT / "bootstrap" / "smoke-test-readiness.sh"

# Answers kubectl's four read shapes off FAKE_KUBECTL_FIXTURE; an absent key is
# an empty result, which is what a cluster with none of that kind returns.
FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
if "pods" in args:
    key = "pods"
elif "deployment,statefulset" in args:
    key = "ns_workloads"
elif "deployment" in args:
    key = "deployments"
elif "statefulset" in args:
    key = "statefulsets"
elif "daemonset" in args:
    key = "daemonsets"
else:
    key = None
with open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8") as fh:
    fixture = json.load(fh)
for line in fixture.get(key) or []:
    print(line)
"""

_failures = 0


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


def run_gate(fixture: dict, **env_overrides: str) -> subprocess.CompletedProcess:
    """The gate against a fixed cluster reading, with no wait between polls."""
    with tempfile.TemporaryDirectory() as tmp:
        bindir = Path(tmp)
        kubectl = bindir / "kubectl"
        kubectl.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
        kubectl.chmod(0o755)
        fixture_file = bindir / "fixture.json"
        fixture_file.write_text(json.dumps(fixture), encoding="utf-8", newline="\n")

        env = dict(os.environ)
        env.pop("DFE_NS", None)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["FAKE_KUBECTL_FIXTURE"] = str(fixture_file)
        # One pass: a fixed reading never converges, so a poll loop would only
        # burn the timeout before reaching the same verdict.
        env["READINESS_TIMEOUT"] = "0"
        env["READINESS_INTERVAL"] = "1"
        env.update(env_overrides)
        return subprocess.run(
            ["bash", str(GATE)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )


def test_an_hours_old_restart_count_is_not_live_churn() -> None:
    """The #218 follow-up: `5h12m` also ends in `m`, and matched the churn glob.

    Restart counts are lifetime, so a pod that settled hours ago is healthy and
    a gate that fails on it fails every long-lived deploy.
    """
    out = run_gate({"pods": ["dfe-local dfe-engine-0 1/1 Running 42 (5h12m ago) 6d"]})
    expect(
        "a pod last restarted 5h12m ago passes",
        out.returncode == 0,
        f"rc={out.returncode} {out.stdout}",
    )


def test_a_restart_minutes_ago_is_live_churn() -> None:
    """The case the recency check exists for has to still fail."""
    out = run_gate({"pods": ["dfe-local dfe-engine-0 1/1 Running 42 (2m ago) 6d"]})
    expect(
        "a pod last restarted 2m ago fails",
        out.returncode != 0 and "runaway restarts" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_a_restart_seconds_ago_is_live_churn() -> None:
    """The other recency kubectl emits, so neither unit is checked alone."""
    out = run_gate({"pods": ["dfe-local dfe-engine-0 1/1 Running 42 (45s ago) 6d"]})
    expect(
        "a pod last restarted 45s ago fails",
        out.returncode != 0 and "runaway restarts" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_a_days_old_restart_count_is_not_live_churn() -> None:
    """`2d3h` carries no `m` or `s` terminator and must not be read as one."""
    out = run_gate({"pods": ["dfe-local dfe-engine-0 1/1 Running 42 (2d3h ago) 9d"]})
    expect(
        "a pod last restarted 2d3h ago passes",
        out.returncode == 0,
        f"rc={out.returncode} {out.stdout}",
    )


def test_a_namespace_the_deploy_does_not_own_is_not_judged() -> None:
    """The allowlist replaces a denylist that named kube-system and Rancher only.

    calico-system, tigera-operator, longhorn-system and metallb-system are the
    cluster's, not the deploy's, and each of them failed the gate on a real RKE2.
    """
    foreign = [
        "calico-system calico-node-abc 0/1 CrashLoopBackOff 9 (30s ago) 5d",
        "tigera-operator tigera-operator-1 0/1 Error 3 (20s ago) 5d",
        "longhorn-system longhorn-manager-x 0/1 CrashLoopBackOff 40 (10s ago) 5d",
        "metallb-system speaker-y 0/1 ImagePullBackOff 0 5d",
    ]
    out = run_gate({"pods": [*foreign, "dfe-local dfe-engine-0 1/1 Running 0 6d"]})
    expect(
        "four unhealthy cluster-owned pods do not fail the deploy's gate",
        out.returncode == 0,
        f"rc={out.returncode} {out.stdout}",
    )


def test_a_namespace_the_deploy_does_own_is_judged() -> None:
    """The allowlist is not a way of ignoring everything."""
    out = run_gate({"pods": ["clickhouse dfe-clickhouse-0 0/1 CrashLoopBackOff 9 (30s ago) 5d"]})
    expect(
        "a crashlooping pod in an owned namespace fails",
        out.returncode != 0 and "clickhouse/dfe-clickhouse-0" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_the_app_namespace_joins_the_allowlist() -> None:
    """DFE_NAMESPACE is a deployment's own choice and need not start with dfe-."""
    pods = ["analytics dfe-engine-0 0/1 CrashLoopBackOff 9 (30s ago) 5d"]
    workloads = {"pods": pods, "ns_workloads": ["dfe-engine 1/1 1 1 5d"]}
    unset = run_gate(workloads)
    expect(
        "an unnamed namespace is not judged",
        unset.returncode == 0,
        f"rc={unset.returncode} {unset.stdout}",
    )
    named = run_gate(workloads, DFE_NS="analytics")
    expect(
        "the same namespace is judged once DFE_NS names it",
        named.returncode != 0 and "analytics/dfe-engine-0" in named.stdout,
        f"rc={named.returncode} {named.stdout}",
    )


def test_the_presence_check_still_fires() -> None:
    """A deploy that produced nothing passes every check that judges what exists."""
    out = run_gate({"pods": ["dfe-local dfe-engine-0 1/1 Running 0 6d"]}, DFE_NS="dfe-local")
    expect(
        "an app namespace with no workloads fails",
        out.returncode != 0 and "NO app workloads" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_an_unready_workload_in_an_owned_namespace_fails() -> None:
    """Replica counts are the half a pod scan cannot see."""
    out = run_gate(
        {
            "pods": ["dfe-local dfe-engine-0 1/1 Running 0 6d"],
            "deployments": ["dfe-local dfe-ui 2 1"],
            "ns_workloads": ["dfe-ui 1/2 2 1 5d"],
        },
        DFE_NS="dfe-local",
    )
    expect(
        "a deployment short of desired replicas fails",
        out.returncode != 0 and "deployment dfe-local/dfe-ui 1/2 ready" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'FAILED' if _failures else 'ALL PASSED'} -- {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
