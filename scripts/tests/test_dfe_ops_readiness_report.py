#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_readiness_report.py
#  Purpose:      Guard `dfe-ops readiness-report`: each failure shape a ready
#                count hides -- no node to schedule on, an image that will not
#                pull, an init container still waiting, a NodeClaim that never
#                launched -- reaches the report with its own evidence, and a
#                read that fails is named rather than skipped.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe_ops_readiness_report.py.

    python3 -m pytest scripts/tests/test_dfe_ops_readiness_report.py -q

The section builders take the JSON kubectl returns, so most tests hand them
fabricated objects. The CLI tests put a fake `kubectl` first on PATH that
answers from a JSON fixture; no cluster is involved.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import dfe_ops_readiness_report as report_mod  # noqa: E402

GLOBS = ["dfe-*", "kafka", "otel"]
UNSCHEDULABLE = (
    "0/11 nodes are available: 2 Insufficient cpu, 9 node(s) had untolerated taint "
    "{dfe.hyperi.io/workload: kafka-broker}. preemption: 0/11 nodes are available"
)


def _pod(name: str, namespace: str = "dfe-test", *, node: str = "", phase: str = "Pending",
         conditions: list | None = None, containers: list | None = None,
         init: list | None = None, requests: dict | None = None) -> dict:
    spec: dict = {"containers": [{"name": "app", "resources": {"requests": requests or {}}}]}
    if node:
        spec["nodeName"] = node
    status: dict = {"phase": phase, "conditions": conditions or []}
    if containers is not None:
        status["containerStatuses"] = containers
    if init is not None:
        status["initContainerStatuses"] = init
    return {"metadata": {"name": name, "namespace": namespace}, "spec": spec, "status": status}


def _ready_container(name: str = "app") -> dict:
    return {"name": name, "ready": True, "restartCount": 0, "state": {"running": {}}}


# --- pods ------------------------------------------------------------------------------


def test_an_unscheduled_pod_carries_the_schedulers_own_verdict() -> None:
    """Run 38026303019 printed `status=Pending` for 21 pods and nothing else."""
    pod = _pod("dfe-receiver-6fb4bff97-2pcvr", conditions=[
        {"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": UNSCHEDULABLE},
    ])
    lines = report_mod.pod_lines([pod], GLOBS)
    assert lines[0] == "pod dfe-test/dfe-receiver-6fb4bff97-2pcvr phase=Pending node=<none>"
    assert lines[1] == f"  PodScheduled=False Unschedulable: {UNSCHEDULABLE}"


def test_an_image_that_will_not_pull_says_why() -> None:
    pod = _pod("dfe-ui-79c9dcc758-8tlqd", node="ip-10-90-1-1", containers=[
        {"name": "ui", "ready": False, "restartCount": 0, "state": {"waiting": {
            "reason": "ImagePullBackOff",
            "message": 'Back-off pulling image "ghcr.io/hyperi-io/dfe-ui:v1": no match for platform in manifest',
        }}},
    ])
    lines = report_mod.pod_lines([pod], GLOBS)
    assert lines[0].endswith("node=ip-10-90-1-1")
    assert "container ui waiting ImagePullBackOff: Back-off pulling image" in lines[1]
    assert "no match for platform in manifest" in lines[1]


def test_an_init_container_still_running_and_a_crash_both_show() -> None:
    pod = _pod("dfe-hunt-runner-6d998698cb-zprcm", node="ip-10-90-1-2", init=[
        {"name": "wait-for-engine", "ready": False, "restartCount": 1, "state": {"running": {}},
         "lastState": {"terminated": {"exitCode": 1, "reason": "Error", "message": "engine not up"}}},
        {"name": "migrate", "ready": False, "restartCount": 0, "state": {"waiting": {"reason": "PodInitializing"}}},
    ], containers=[{"name": "runner", "ready": False, "restartCount": 0,
                    "state": {"waiting": {"reason": "PodInitializing"}}}])
    text = "\n".join(report_mod.pod_lines([pod], GLOBS))
    assert "init container wait-for-engine running, not finished" in text
    assert "init container wait-for-engine restarted 1x, last exit 1 (Error): engine not up" in text
    assert "container runner waiting PodInitializing" in text


def test_ready_finished_and_unjudged_pods_are_left_out() -> None:
    pods = [
        _pod("dfe-engine-0", phase="Running", node="n1", containers=[_ready_container()]),
        _pod("migrate-job-x", phase="Succeeded", node="n1"),
        _pod("calico-node-y", namespace="calico-system"),
    ]
    assert report_mod.pod_lines(pods, GLOBS) == []


def test_kube_system_is_read_whatever_the_globs_say() -> None:
    """Karpenter runs there, outside every namespace the gate judges."""
    pod = _pod("karpenter-5d8f7c9b4-abcde", namespace="kube-system", conditions=[
        {"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": "0/11 nodes"},
    ])
    assert report_mod.pod_lines([pod], ["dfe-*"])[0].startswith("pod kube-system/karpenter-")


def test_a_running_pod_with_a_container_not_ready_is_reported() -> None:
    pod = _pod("dfe-loader-1", phase="Running", node="n1",
               containers=[{"name": "loader", "ready": False, "restartCount": 0, "state": {"running": {}}}])
    assert report_mod.pod_lines([pod], GLOBS)[1] == "  container loader running, not ready"


# --- events ----------------------------------------------------------------------------


def _event(reason: str, name: str, *, namespace: str = "dfe-test", kind: str = "Pod",
           when: str = "2026-10-10T05:30:00Z", type_: str = "Warning", count: int = 1,
           message: str = "") -> dict:
    return {
        "metadata": {"namespace": namespace},
        "involvedObject": {"kind": kind, "name": name},
        "reason": reason, "type": type_, "count": count, "lastTimestamp": when,
        "message": message or f"{reason} on {name}",
    }


def test_only_the_newest_warning_per_object_and_reason_is_kept_newest_first() -> None:
    events = [
        _event("FailedScheduling", "dfe-ui-1", when="2026-10-10T05:30:00Z", message="old"),
        _event("FailedScheduling", "dfe-ui-1", when="2026-10-10T05:40:00Z", message="new", count=40),
        _event("Pulled", "dfe-ui-1", type_="Normal"),
        _event("FailedMount", "dfe-engine-1", when="2026-10-10T05:35:00Z"),
        _event("BackOff", "calico-node", namespace="calico-system"),
    ]
    lines = report_mod.event_lines(events, GLOBS)
    assert len(lines) == 2
    assert lines[0] == "2026-10-10T05:40:00Z dfe-test Pod/dfe-ui-1 FailedScheduling x40: new"
    assert "FailedMount" in lines[1]


def test_a_nodeclaim_event_is_kept_wherever_it_landed() -> None:
    """NodeClaims are cluster-scoped, so their events fall into a namespace nobody judges."""
    event = _event("FailedLaunch", "general-abcde", namespace="default", kind="NodeClaim",
                   message="creating instance, UnauthorizedOperation")
    assert "NodeClaim/general-abcde FailedLaunch x1: creating instance" in report_mod.event_lines([event], GLOBS)[0]


def test_an_events_v1_shape_is_read_too() -> None:
    event = {"metadata": {"namespace": "dfe-test", "creationTimestamp": "2026-10-10T05:30:00Z"},
             "regarding": {"kind": "Pod", "name": "dfe-ui-2"}, "reason": "FailedScheduling",
             "type": "Warning", "note": "0/11 nodes", "series": {"count": 7,
                                                                 "lastObservedTime": "2026-10-10T05:41:00Z"}}
    assert report_mod.event_lines([event], GLOBS) == [
        "2026-10-10T05:41:00Z dfe-test Pod/dfe-ui-2 FailedScheduling x7: 0/11 nodes"
    ]


# --- nodes -------------------------------------------------------------------------------


def _node(name: str, *, cpu: str = "1930m", memory: str = "7Gi", taints: list | None = None,
          labels: dict | None = None) -> dict:
    return {
        "metadata": {"name": name, "labels": {"kubernetes.io/arch": "arm64",
                                              "node.kubernetes.io/instance-type": "m9g.large",
                                              **(labels or {})}},
        "spec": {"taints": taints or []},
        "status": {"allocatable": {"cpu": cpu, "memory": memory},
                   "conditions": [{"type": "Ready", "status": "True"}]},
    }


def test_each_node_shows_what_is_requested_against_what_it_can_hold() -> None:
    nodes = [
        _node("system-a", labels={"eks.amazonaws.com/nodegroup": "system",
                                  "eks.amazonaws.com/capacityType": "ON_DEMAND"}),
        _node("broker-a", labels={"dfe.hyperi.io/workload": "kafka-broker"}, taints=[
            {"key": "dfe.hyperi.io/workload", "value": "kafka-broker", "effect": "NoSchedule"}]),
    ]
    pods = [
        _pod("argocd-server", namespace="argocd", node="system-a", phase="Running",
             requests={"cpu": "1500m", "memory": "1Gi"}),
        _pod("done", node="system-a", phase="Succeeded", requests={"cpu": "4", "memory": "4Gi"}),
    ]
    lines = report_mod.node_lines(nodes, pods)
    assert lines[0] == "2 node(s)"
    assert "node system-a ready=True arch=arm64 type=m9g.large capacity=ON_DEMAND pool=system" in lines[1]
    assert "requested cpu 1.50/1.93 memory 1.0/7.0Gi" in lines[1]
    assert "taints=-" in lines[1]
    assert "workload=kafka-broker" in lines[2]
    assert "taints=dfe.hyperi.io/workload=kafka-broker:NoSchedule" in lines[2]


def test_a_large_init_container_is_what_the_scheduler_counts() -> None:
    pod = _pod("p", node="n1", phase="Running", requests={"cpu": "100m", "memory": "128Mi"})
    pod["spec"]["initContainers"] = [{"name": "i", "resources": {"requests": {"cpu": "1", "memory": "1Gi"}}}]
    assert "requested cpu 1.00/1.93 memory 1.0/7.0Gi" in report_mod.node_lines([_node("n1")], [pod])[1]


# --- karpenter, volumes, argo --------------------------------------------------------------


def test_a_nodepool_shows_its_limit_against_what_it_holds() -> None:
    pool = {"metadata": {"name": "general"}, "spec": {"limits": {"cpu": "8", "memory": "32Gi"}},
            "status": {"resources": {"cpu": "8", "memory": "32Gi", "nodes": "2"},
                       "conditions": [{"type": "Ready", "status": "True"}]}}
    assert report_mod.nodepool_lines([pool]) == [
        "nodepool general limits cpu=8 memory=32Gi, holds cpu=8 memory=32Gi nodes=2"
    ]


def test_a_nodeclaim_that_never_launched_names_the_step_and_why() -> None:
    claim = {"metadata": {"name": "general-x7k2p", "labels": {"karpenter.sh/nodepool": "general"}},
             "status": {"conditions": [
                 {"type": "Launched", "status": "False", "reason": "LaunchFailed",
                  "message": "creating instance, UnauthorizedOperation: You are not authorized"},
                 {"type": "Registered", "status": "Unknown", "reason": "AwaitingReconciliation"},
             ]}}
    assert report_mod.nodeclaim_lines([claim]) == [
        "nodeclaim general-x7k2p pool=general type=- node=<none>",
        "  Launched=False LaunchFailed: creating instance, UnauthorizedOperation: You are not authorized",
        "  Registered=Unknown AwaitingReconciliation",
    ]


def test_an_ec2nodeclass_names_the_condition_it_fails() -> None:
    node_class = {"metadata": {"name": "general"}, "status": {"conditions": [
        {"type": "AMIsReady", "status": "False", "reason": "AMINotFound",
         "message": "no AMI for alias al2023@v20260930"},
        {"type": "SubnetsReady", "status": "True"},
    ]}}
    assert report_mod.nodeclass_lines([node_class]) == [
        "ec2nodeclass general not ready",
        "  AMIsReady=False AMINotFound: no AMI for alias al2023@v20260930",
    ]


def test_the_controllers_newest_distinct_errors_are_kept() -> None:
    log = "\n".join([
        json.dumps({"level": "INFO", "message": "starting"}),
        json.dumps({"level": "ERROR", "message": "failed launching nodeclaim", "error": "denied"}),
        json.dumps({"level": "ERROR", "message": "failed launching nodeclaim", "error": "denied"}),
        "not json, but an ERROR all the same",
        *(json.dumps({"level": "ERROR", "message": f"m{i}", "error": "e"}) for i in range(12)),
    ])
    lines = report_mod.log_error_lines(log)
    assert len(lines) == report_mod.LOG_ERRORS_SHOWN
    assert lines[-1] == "m11: e"
    assert "failed launching nodeclaim: denied" not in lines


def test_an_unbound_claim_is_listed_and_a_bound_one_is_not() -> None:
    claims = [
        {"metadata": {"name": "data-0", "namespace": "dfe-test"}, "spec": {"storageClassName": "gp3"},
         "status": {"phase": "Pending"}},
        {"metadata": {"name": "data-1", "namespace": "dfe-test"}, "status": {"phase": "Bound"}},
    ]
    assert report_mod.pvc_lines(claims, GLOBS) == ["pvc dfe-test/data-0 Pending class=gp3"]


def test_an_application_out_of_sync_carries_its_conditions() -> None:
    apps = [
        {"metadata": {"name": "karpenter-pools-dfe-aws-test"}, "status": {
            "sync": {"status": "Unknown"}, "health": {"status": "Missing"},
            "conditions": [{"type": "ComparisonError", "message": "no matches for kind NodePool"}],
            "operationState": {"phase": "Failed", "message": "one or more objects failed to apply"}}},
        {"metadata": {"name": "keda-dfe-aws-test"},
         "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}},
    ]
    assert report_mod.application_lines(apps) == [
        "application karpenter-pools-dfe-aws-test sync=Unknown health=Missing",
        "  ComparisonError: no matches for kind NodePool",
        "  last operation Failed: one or more objects failed to apply",
    ]


def test_a_runaway_message_is_clipped_and_a_long_section_capped() -> None:
    assert len(report_mod._clip("x" * 5000)) == report_mod.LINE_LIMIT
    capped = report_mod._capped([str(i) for i in range(report_mod.SECTION_LIMIT + 5)])
    assert capped[-1] == "... and 5 more"


# --- the CLI against a fake kubectl --------------------------------------------------------

FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
fixture = json.load(open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8"))
if "logs" in args:
    print(fixture.get("logs", ""))
    sys.exit(0)
resource = args[args.index("get") + 1]
if resource in fixture.get("missing", []):
    print(f'error: the server doesn\\'t have a resource type "{resource.split(".")[0]}"', file=sys.stderr)
    sys.exit(1)
if resource in fixture.get("broken", []):
    print("Error from server (Forbidden): forbidden", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"items": fixture.get("items", {}).get(resource, [])}))
"""


def _run_cli(tmp_path: Path, fixture: dict, *, kubectl: bool = True) -> subprocess.CompletedProcess:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if kubectl:
        fake = bindir / "kubectl"
        fake.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
        fake.chmod(0o755)
    fixture_file = tmp_path / "fixture.json"
    fixture_file.write_text(json.dumps(fixture), encoding="utf-8", newline="\n")
    env = {"PATH": f"{bindir}{os.pathsep}{Path(sys.executable).parent}" if kubectl else str(bindir),
           "FAKE_KUBECTL_FIXTURE": str(fixture_file)}
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "dfe-ops"), "readiness-report", "--namespaces", "dfe-*"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, check=False,
    )


def test_the_cli_reads_every_section_and_exits_zero(tmp_path: Path) -> None:
    pod = _pod("dfe-ui-1", conditions=[
        {"type": "PodScheduled", "status": "False", "reason": "Unschedulable", "message": UNSCHEDULABLE}])
    done = _run_cli(tmp_path, {
        "items": {"pods": [pod], "nodes": [_node("system-a")]},
        "logs": json.dumps({"level": "ERROR", "message": "failed launching nodeclaim", "error": "denied"}),
    })
    assert done.returncode == 0, done.stderr
    out = done.stdout
    for title in ("pods not Ready", "newest warning events", "nodes", "karpenter nodepools",
                  "karpenter nodeclaims", "karpenter ec2nodeclasses", "karpenter controller errors",
                  "volume claims not Bound", "argo applications not Synced and Healthy"):
        assert f"--- {title} ---" in out, title
    assert f"PodScheduled=False Unschedulable: {UNSCHEDULABLE}" in out
    assert "no NodeClaim exists: nothing has been launched" in out
    assert "failed launching nodeclaim: denied" in out


def test_a_cluster_with_no_karpenter_says_so_once(tmp_path: Path) -> None:
    done = _run_cli(tmp_path, {"missing": ["nodepools.karpenter.sh"]})
    assert done.returncode == 0, done.stderr
    assert "not installed on this cluster (no nodepools.karpenter.sh)" in done.stdout
    assert "karpenter nodeclaims" not in done.stdout


def test_a_read_that_fails_is_named_not_skipped(tmp_path: Path) -> None:
    done = _run_cli(tmp_path, {"broken": ["events"]})
    assert done.returncode == 0, done.stderr
    assert "--- newest warning events ---\n  could not read: Error from server (Forbidden): forbidden" in done.stdout


def test_no_kubectl_is_a_named_skip_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    args = argparse.Namespace(namespaces="*", kubeconfig="", context="")
    assert report_mod.cmd_readiness_report(args) == 0
    assert "kubectl is not on PATH" in capsys.readouterr().err


@pytest.mark.parametrize("namespace", ["dfe-test", "kube-system"])
def test_the_globs_and_kube_system_are_judged(namespace: str) -> None:
    assert report_mod.judged(namespace, ["dfe-*"])
