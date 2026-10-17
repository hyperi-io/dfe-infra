#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_failure_evidence.py
#  Purpose:      Guard `dfe-ops failure-evidence` and the cycle hook that runs it:
#                the kubectl calls it makes, that one failing capture never stops
#                the rest, that every capture is bounded, that no call reads the
#                data of a Secret or describes a pod, that no env value reaches a
#                file, that an ESO re-mint leaves evidence, and that a failed
#                stage writes evidence before destroy.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe_ops_failure_evidence.py and its place in `dfe-ops cycle`.

    python3 -m pytest scripts/tests/test_dfe_ops_failure_evidence.py -q

Most tests swap the module's bounded runner for a fake that answers each
kubectl call from a fixture and records what was asked. The bounded runner
itself runs real child processes, and the CLI test puts a fake `kubectl` first
on PATH; no cluster is involved.
"""

import argparse
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import dfe_ops_failure_evidence as fe  # noqa: E402

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_failure_evidence", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_failure_evidence", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_failure_evidence"] = dfeops
_loader.exec_module(dfeops)


def _node(name: str) -> dict:
    return {
        "metadata": {"name": name, "creationTimestamp": "2026-01-01T00:00:00Z"},
        "spec": {"taints": [{"key": "dfe.example/workload", "value": "kafka", "effect": "NoSchedule"}]},
        "status": {
            "nodeInfo": {
                "kubeletVersion": "v1.36.0",
                "osImage": "Amazon Linux 2023",
                "kernelVersion": "6.18.0",
                "containerRuntimeVersion": "containerd://2.1.0",
            },
            "conditions": [
                {"type": "MemoryPressure", "status": "False", "reason": "KubeletHasSufficientMemory",
                 "lastTransitionTime": "2026-01-01T00:01:00Z"},
                {"type": "Ready", "status": "False", "reason": "KubeletNotReady",
                 "message": "container runtime network not ready",
                 "lastTransitionTime": "2026-01-01T00:02:00Z"},
            ],
            "capacity": {"cpu": "2", "memory": "8000000Ki", "pods": "29"},
            "allocatable": {"cpu": "1930m", "memory": "6900000Ki", "pods": "29"},
        },
    }


def _aws_node_pod(name: str, node: str, *, restarts: int = 0) -> dict:
    return {
        "metadata": {"name": name, "labels": {"k8s-app": "aws-node"}},
        "spec": {
            "nodeName": node,
            "initContainers": [{"name": "aws-vpc-cni-init"}],
            "containers": [{"name": "aws-node"}, {"name": "aws-eks-nodeagent"}],
        },
        "status": {
            "phase": "Running",
            "initContainerStatuses": [{"name": "aws-vpc-cni-init", "restartCount": 0}],
            "containerStatuses": [
                {"name": "aws-node", "ready": True, "restartCount": restarts},
                {"name": "aws-eks-nodeagent", "ready": True, "restartCount": 0},
            ],
        },
    }


def _plain_pod(name: str, *, ready: bool, phase: str = "Running") -> dict:
    return {
        "metadata": {"name": name, "labels": {"k8s-app": name.split("-")[0]}},
        "spec": {"nodeName": "node-a", "containers": [{"name": "main"}]},
        "status": {
            "phase": phase,
            "containerStatuses": [{"name": "main", "ready": ready, "restartCount": 0}],
        },
    }


def _controller_pod(name: str, node: str, *, restarts: int = 0) -> dict:
    labels = {"app.kubernetes.io/name": "aws-load-balancer-controller"}
    return {
        "metadata": {"name": name, "labels": labels},
        "spec": {"nodeName": node, "containers": [{"name": "aws-load-balancer-controller"}]},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {"name": "aws-load-balancer-controller", "ready": True, "restartCount": restarts}
            ],
        },
    }


PODS = [
    _aws_node_pod("aws-node-b2", "node-b"),
    _aws_node_pod("aws-node-a1", "node-a", restarts=2),
    _plain_pod("coredns-1", ready=False),
    _plain_pod("ebs-csi-node-1", ready=False, phase="Pending"),
    _plain_pod("kube-proxy-1", ready=True),
    _plain_pod("migrate-1", ready=False, phase="Succeeded"),
    _controller_pod("lbc-b", "node-b"),
    _controller_pod("lbc-a", "node-a", restarts=1),
]

# A CA bundle as the API server holds it: base64 of a PEM certificate, never written to a file.
BUNDLE = "LS0tLS1CRUdJTi" + "QUJD" * 40
WEBHOOK_CONFIGS = [
    {
        "kind": "MutatingWebhookConfiguration",
        "metadata": {
            "name": "aws-load-balancer-webhook",
            "annotations": {
                "cert-manager.io/inject-ca-from": "kube-system/aws-load-balancer-serving-cert",
                "kubectl.kubernetes.io/last-applied-configuration": json.dumps(
                    {"webhooks": [{"clientConfig": {"caBundle": BUNDLE}}]}
                ),
            },
        },
        "webhooks": [
            {"name": "mpod.elbv2.k8s.aws", "clientConfig": {"caBundle": BUNDLE}},
            {"name": "mtargetgroupbinding.elbv2.k8s.aws", "clientConfig": {"caBundle": BUNDLE}},
        ],
    },
    {
        "kind": "ValidatingWebhookConfiguration",
        "metadata": {"name": "aws-load-balancer-webhook"},
        "webhooks": [{"name": "vtargetgroupbinding.elbv2.k8s.aws", "clientConfig": {}}],
    },
]
WEBHOOK_READ = (
    "get mutatingwebhookconfigurations,validatingwebhookconfigurations "
    "-l app.kubernetes.io/name=aws-load-balancer-controller -o json"
)
DISK_EVENTS = [
    {"reason": "InvalidDiskCapacity", "involvedObject": {"kind": "Node", "name": "node-a"}},
    {"reason": "InvalidDiskCapacity", "involvedObject": {"kind": "Node", "name": "node-a"}},
    {"reason": "BackOff", "involvedObject": {"kind": "Pod", "name": "coredns-1"}},
]

SENTINEL = "SENTINEL-NOT-EVIDENCE"
EXTERNAL_SECRETS_READ = "get externalsecrets.external-secrets.io -A -o json"
ESO_SELECTOR = "app.kubernetes.io/name=external-secrets"
ESO_PODS_READ = f"get pods -A -l {ESO_SELECTOR} -o json"
SECRET_COLUMNS = (
    "custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,"
    "CREATED:.metadata.creationTimestamp,RESOURCEVERSION:.metadata.resourceVersion,"
    "IMMUTABLE:.immutable"
)
# The fields a Secret may be read through: its identity and three facts about its metadata.
SECRET_FIELDS = {
    ".metadata.namespace",
    ".metadata.name",
    ".metadata.creationTimestamp",
    ".metadata.resourceVersion",
    ".immutable",
}


def _external_secret(
    namespace: str,
    name: str,
    *,
    generator: str = "",
    target: str = "",
    immutable: bool = False,
    message: str = "secret synced",
) -> dict:
    """An ExternalSecret as the API returns it, with fields the evidence has to leave out."""
    if generator:
        source = {"generatorRef": {"kind": "Password", "name": generator}}
    else:
        source = {"storeRef": {"name": "vault", "kind": "ClusterSecretStore"}}
    written = {"immutable": immutable, "template": {"data": {"key": f"{SENTINEL}-template"}}}
    if target:
        written["name"] = target
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "creationTimestamp": "2026-10-14T01:00:00Z",
            "generation": 2,
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": SENTINEL},
        },
        "spec": {
            "refreshPolicy": "CreatedOnce",
            "refreshInterval": "0",
            "target": written,
            "dataFrom": [{"sourceRef": source}],
        },
        "status": {
            "syncedResourceVersion": "2-5d41402a",
            "refreshTime": "2026-10-14T01:00:05Z",
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "reason": "SecretSynced",
                    "message": message,
                    "lastTransitionTime": "2026-10-14T01:00:05Z",
                }
            ],
        },
    }


EXTERNAL_SECRET_ITEMS = [
    _external_secret("kafka", "dfe-kafka-user", generator="dfe-kafka-user-gen", immutable=True),
    _external_secret("dfe", "dfe-kafka-user"),
    _external_secret("dfe", "engine-jwt", generator="engine-jwt-gen", target="dfe-engine-jwt"),
    _external_secret(
        "clickhouse", "clickhouse-admin-password", generator="clickhouse-admin-gen", immutable=True
    ),
]


def _eso_pod(name: str, *, restarts: int = 0) -> dict:
    labels = {"app.kubernetes.io/name": "external-secrets"}
    return {
        "metadata": {"name": name, "namespace": "external-secrets", "labels": labels},
        "spec": {"nodeName": "node-a", "containers": [{"name": "external-secrets"}]},
        "status": {
            "phase": "Running",
            "containerStatuses": [
                {"name": "external-secrets", "ready": True, "restartCount": restarts}
            ],
        },
    }


ESO_PODS = [_eso_pod("external-secrets-b2"), _eso_pod("external-secrets-a1", restarts=1)]


def _waiting_pod(
    namespace: str,
    name: str,
    *,
    reason: str = "",
    init_reason: str = "",
    init_running: bool = False,
) -> dict:
    """A pod whose init and main containers wait on the reasons given.

    With no `init_reason` the init container has finished, or is still running (Init:0/1)
    when `init_running` is set. A main container with no `reason` runs, not ready.
    """

    def status(container: str, waiting: str) -> dict:
        state = (
            {"waiting": {"reason": waiting, "message": f"{waiting} detail"}}
            if waiting
            else {"running": {}}
        )
        return {"name": container, "ready": False, "restartCount": 0, "state": state}

    init = status("copyutil", init_reason)
    if not init_reason and not init_running:
        init["ready"] = True
        init["state"] = {"terminated": {"reason": "Completed", "exitCode": 0}}
    pending = reason or init_reason or init_running
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "initContainers": [{"name": "copyutil", "image": "registry.example/copyutil:1"}],
            "containers": [{"name": "main", "image": "registry.example/main:1"}],
        },
        "status": {
            "phase": "Pending" if pending else "Running",
            "initContainerStatuses": [init],
            "containerStatuses": [status("main", reason)],
        },
    }


ALL_PODS = [
    _waiting_pod("argocd", "argocd-server-1", reason="ImagePullBackOff"),
    _waiting_pod("argocd", "argocd-dex-server-1", init_reason="ImagePullBackOff"),
    _waiting_pod("dfe", "dfe-loader-1", reason="CrashLoopBackOff"),
    _waiting_pod("dfe", "dfe-engine-1", reason="CreateContainerConfigError"),
    _waiting_pod("monitoring", "collector-1", reason="ErrImagePull"),
    _waiting_pod("dfe", "dfe-ui-1", reason="ContainerCreating"),
    _waiting_pod("dfe", "dfe-receiver-1"),
    _waiting_pod("dfe", "dfe-archiver-1", reason="PodInitializing", init_running=True),
]


def _with_private_fields(pod: dict) -> dict:
    """A copy of `pod` whose env, envFrom, args, command, volumes and annotations hold SENTINEL."""
    copy = json.loads(json.dumps(pod))
    copy["metadata"]["annotations"] = {"example.dev/note": f"{SENTINEL}-annotation"}
    copy["spec"]["volumes"] = [{"name": "creds", "secret": {"secretName": f"{SENTINEL}-volume"}}]
    for container in [*copy["spec"].get("initContainers", []), *copy["spec"]["containers"]]:
        container["env"] = [{"name": "API_TOKEN", "value": SENTINEL}]
        container["envFrom"] = [{"secretRef": {"name": f"{SENTINEL}-envfrom"}}]
        container["args"] = [f"--token={SENTINEL}-args"]
        container["command"] = ["run", f"{SENTINEL}-command"]
    return copy


def _pod_events(namespace: str, pod: str) -> str:
    """The events call for one pod, as the joined kubectl arguments."""
    return (
        f"get events -n {namespace} --field-selector involvedObject.name={pod} "
        "--sort-by=.metadata.creationTimestamp -o wide"
    )


def _items(items: list[dict]) -> fe.Outcome:
    return fe.Outcome(output=json.dumps({"items": items}).encode(), returncode=0)


def _answer(args: list[str]) -> fe.Outcome:
    """What a healthy-enough cluster says to each call the module makes."""
    joined = " ".join(args)
    if joined == "get nodes -o json":
        return _items([_node("node-a"), _node("node-b")])
    if joined == "-n kube-system get pods -o json":
        return _items(PODS)
    if joined == "get pods -A -o json":
        return _items(ALL_PODS)
    if joined.startswith("get events -A --field-selector reason=InvalidDiskCapacity"):
        return _items(DISK_EVENTS)
    if joined == WEBHOOK_READ:
        return _items(WEBHOOK_CONFIGS)
    if joined == EXTERNAL_SECRETS_READ:
        return _items(EXTERNAL_SECRET_ITEMS)
    if joined == ESO_PODS_READ:
        return _items(ESO_PODS)
    return fe.Outcome(output=f"output of {joined}\n".encode(), returncode=0)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    fail: tuple[str, ...] = (),
    budget: float = 60.0,
    answer=_answer,
) -> tuple[list[list[str]], Path]:
    """collect() against a fake runner; returns every kubectl argv after `kubectl`, and the out dir.

    A call whose joined args contain any string in `fail` exits 1 with no output.
    """
    calls: list[list[str]] = []

    def fake(argv: list[str], *, timeout: float, limit: int, tail: bool = False) -> fe.Outcome:
        assert argv[0] == "kubectl"
        assert 0 < timeout <= fe.CAPTURE_TIMEOUT
        if f"--tail={fe.POD_LOG_TAIL}" in argv:
            assert tail is True
            assert 0 < limit <= fe.POD_LOG_BYTES
        else:
            assert limit in (fe.TEXT_BYTES, fe.JSON_BYTES)
        calls.append(argv[1:])
        joined = " ".join(argv[1:])
        if any(f in joined for f in fail):
            return fe.Outcome(returncode=1, error="Error from server (InternalError): boom")
        return answer(argv[1:])

    monkeypatch.setattr(fe, "run_bounded", fake)
    out = tmp_path / "evidence"
    out.mkdir()
    (out / fe.INDEX).write_text("", encoding="utf-8")
    fe.collect(fe.Evidence(out, [], budget, time.monotonic() + budget))
    return calls, out


# --- what it reads off the cluster ---------------------------------------------------


def test_node_lines_carry_conditions_capacity_allocatable_and_runtime() -> None:
    lines = fe.node_condition_lines([_node("node-a")])
    assert lines[0] == "node node-a created 2026-01-01T00:00:00Z"
    assert "runtime containerd://2.1.0" in lines[1]
    assert (
        "  condition Ready=False KubeletNotReady: container runtime network not ready "
        "(since 2026-01-01T00:02:00Z)" in lines
    )
    assert "  capacity cpu=2 memory=8000000Ki pods=29" in lines
    assert "  allocatable cpu=1930m memory=6900000Ki pods=29" in lines
    assert "  taints dfe.example/workload=kafka:NoSchedule" in lines


def test_unready_pods_leave_out_ready_and_finished_ones() -> None:
    assert fe.not_ready_pods(PODS) == ["coredns-1", "ebs-csi-node-1"]


def test_aws_node_pods_are_found_by_label_and_ordered_by_node() -> None:
    names = [p["metadata"]["name"] for p in fe.aws_node_pods(PODS)]
    assert names == ["aws-node-a1", "aws-node-b2"]


def test_containers_come_in_spec_order_with_their_restarts() -> None:
    assert fe.containers_of(PODS[1]) == [("aws-vpc-cni-init", 0), ("aws-node", 2), ("aws-eks-nodeagent", 0)]


def test_disk_capacity_nodes_are_named_once() -> None:
    assert fe.disk_capacity_nodes(DISK_EVENTS) == ["node-a"]


def test_stuck_pods_are_an_init_or_main_container_waiting_reason_or_an_unfinished_init() -> None:
    assert fe.stuck_pods(ALL_PODS) == [
        ("argocd", "argocd-dex-server-1"),
        ("argocd", "argocd-server-1"),
        ("dfe", "dfe-archiver-1"),
        ("dfe", "dfe-engine-1"),
        ("dfe", "dfe-loader-1"),
        ("monitoring", "collector-1"),
    ]


def test_an_init_container_that_exited_zero_is_finished_and_any_other_state_is_not() -> None:
    def pod(*states: dict) -> dict:
        seen = [{"name": f"i{n}", "state": s} for n, s in enumerate(states)]
        return {"status": {"initContainerStatuses": seen}}

    done = {"terminated": {"exitCode": 0}}
    assert not fe.init_unfinished({})
    assert not fe.init_unfinished(pod(done, done))
    assert fe.init_unfinished(pod(done, {"running": {}}))
    assert fe.init_unfinished(pod({"terminated": {"exitCode": 1}}))
    assert fe.init_unfinished(pod({"waiting": {"reason": "PodInitializing"}}))
    assert fe.init_unfinished(pod({}))


def test_a_started_sidecar_init_container_does_not_make_a_pod_stuck() -> None:
    mesh = _pod(
        "dfe",
        "meshed-1",
        init=[_container("proxy", state=RUNNING, ready=True), _container("setup", state=EXITED_ZERO)],
        main=[_container("app", state=RUNNING, ready=True)],
    )
    mesh["spec"]["initContainers"][0]["restartPolicy"] = "Always"
    mesh["status"]["initContainerStatuses"][0]["started"] = True
    assert not fe.init_unfinished(mesh)
    assert fe.stuck_pods([mesh]) == []
    assert fe.log_plans(mesh) == []
    mesh["status"]["initContainerStatuses"][0]["ready"] = False
    assert fe.log_plans(mesh) == [fe.LogPlan("proxy", init=True)]
    mesh["status"]["initContainerStatuses"][0]["started"] = False
    assert fe.init_unfinished(mesh)


def test_a_name_becomes_one_safe_path_segment() -> None:
    assert fe.safe_name("deploy+e2e") == "deploy_e2e"
    assert fe.safe_name("../../etc") == ".._.._etc"
    assert fe.safe_name("") == "_"


def test_a_pod_summary_has_the_node_phase_conditions_and_every_containers_state() -> None:
    pod = {
        "metadata": {"name": "dfe-loader-1", "namespace": "dfe"},
        "spec": {
            "nodeName": "node-a",
            "initContainers": [{"name": "migrate", "image": "registry.example/migrate:1"}],
            "containers": [
                {"name": "main", "image": "registry.example/loader:2"},
                {"name": "sidecar", "image": "registry.example/side:3"},
            ],
        },
        "status": {
            "phase": "Pending",
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "ContainersNotReady",
                    "message": "containers with unready status: [main]",
                },
                {"type": "PodScheduled", "status": "True"},
            ],
            "initContainerStatuses": [
                {
                    "name": "migrate",
                    "restartCount": 0,
                    "state": {"terminated": {"reason": "Completed", "exitCode": 0}},
                }
            ],
            "containerStatuses": [
                {
                    "name": "main",
                    "restartCount": 4,
                    "state": {
                        "waiting": {
                            "reason": "CrashLoopBackOff",
                            "message": "back-off 2m40s restarting failed container",
                        }
                    },
                    "lastState": {
                        "terminated": {"reason": "Error", "exitCode": 137, "message": "one\ntwo"}
                    },
                }
            ],
        },
    }
    assert fe.pod_summary_lines("dfe", pod) == [
        "pod dfe/dfe-loader-1",
        "  node node-a",
        "  phase Pending",
        "  condition Ready=False ContainersNotReady: containers with unready status: [main]",
        "  condition PodScheduled=True",
        "  init container migrate",
        "    image registry.example/migrate:1",
        "    state terminated, reason Completed, exit code 0",
        "    restartCount 0",
        "  container main",
        "    image registry.example/loader:2",
        "    state waiting, reason CrashLoopBackOff, "
        "message: back-off 2m40s restarting failed container",
        "    restartCount 4",
        "    last state terminated, reason Error, exit code 137, message: one two",
        "  container sidecar",
        "    image registry.example/side:3",
        "    state not reported",
        "    restartCount 0",
    ]


def test_a_pod_summary_carries_the_pods_own_reason_for_an_eviction() -> None:
    pod = {
        "metadata": {"name": "worker-1"},
        "spec": {"nodeName": "node-a", "containers": [{"name": "main", "image": "r.example/w:1"}]},
        "status": {
            "phase": "Failed",
            "reason": "Evicted",
            "message": "The node was low on resource: ephemeral-storage.",
        },
    }
    lines = fe.pod_summary_lines("dfe", pod)
    assert "  status Evicted: The node was low on resource: ephemeral-storage." in lines


# --- the calls it makes --------------------------------------------------------------


def test_every_capture_the_brief_names_is_issued(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls, _out = _run(monkeypatch, tmp_path)
    issued = [" ".join(c) for c in calls]
    assert issued[:3] == ["get nodes -o wide", "get nodes -o json", "-n kube-system get pods -o json"]
    assert "get pods -A -o wide" in issued
    assert (
        "get events -A --field-selector type=Warning --sort-by=.metadata.creationTimestamp -o wide"
        in issued
    )
    assert "get pods -A -o json" in issued
    for namespace, pod in (
        ("argocd", "argocd-dex-server-1"),
        ("argocd", "argocd-server-1"),
        ("dfe", "dfe-engine-1"),
        ("dfe", "dfe-loader-1"),
        ("monitoring", "collector-1"),
        ("kube-system", "coredns-1"),
        ("kube-system", "ebs-csi-node-1"),
    ):
        assert _pod_events(namespace, pod) in issued
    assert "-n kube-system get events --sort-by=.metadata.creationTimestamp -o wide" in issued
    assert (
        "get events -A --field-selector involvedObject.kind=Node,involvedObject.name=node-a "
        "--sort-by=.metadata.creationTimestamp" in issued
    )
    bounds = f"--tail={fe.LOG_TAIL} --limit-bytes={fe.TEXT_BYTES} --timestamps"
    for pod in ("aws-node-a1", "aws-node-b2"):
        for container in ("aws-vpc-cni-init", "aws-node", "aws-eks-nodeagent"):
            assert f"-n kube-system logs {pod} -c {container} {bounds}" in issued
    for node in ("node-a", "node-b"):
        for log in ("ipamd.log", "plugin.log", "network-policy-agent.log"):
            assert f"get --raw /api/v1/nodes/{node}/proxy/logs/aws-routed-eni/{log}" in issued


def test_only_a_restarted_or_crashlooping_container_has_its_previous_run_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, _out = _run(monkeypatch, tmp_path)
    previous = sorted(" ".join(c) for c in calls if "--previous" in c)
    assert len(previous) == 4
    assert previous[0].startswith("-n dfe logs dfe-loader-1 -c main ")
    assert previous[1].startswith("-n external-secrets logs external-secrets-a1 -c external-secrets ")
    assert previous[2].startswith("-n kube-system logs aws-node-a1 -c aws-node ")
    assert previous[3].startswith("-n kube-system logs lbc-a -c aws-load-balancer-controller ")


def test_the_container_is_asked_for_a_cni_log_only_when_the_kubelet_did_not_serve_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unserved = "nodes/node-b/proxy/logs/aws-routed-eni/ipamd.log"
    calls, out = _run(monkeypatch, tmp_path, fail=(unserved,))
    execs = [" ".join(c) for c in calls if "exec" in c]
    assert execs == [
        f"-n kube-system exec aws-node-b2 -c aws-node -- cat {fe.CNI_LOG_MOUNT}/ipamd.log"
    ]
    assert (out / "aws-node" / "node-b.ipamd.log").read_bytes().startswith(b"output of -n kube-system exec")


def test_the_network_policy_agent_log_falls_back_to_the_container_like_the_cni_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unserved = "nodes/node-a/proxy/logs/aws-routed-eni/network-policy-agent.log"
    calls, out = _run(monkeypatch, tmp_path, fail=(unserved,))
    execs = [" ".join(c) for c in calls if "exec" in c]
    assert execs == [
        f"-n kube-system exec aws-node-a1 -c aws-node -- cat {fe.CNI_LOG_MOUNT}/network-policy-agent.log"
    ]
    landed = out / "aws-node" / "node-a.network-policy-agent.log"
    assert landed.read_bytes().startswith(b"output of -n kube-system exec")


def test_an_empty_answer_writes_no_file_and_the_index_says_empty_not_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An aws-eks-nodeagent container can answer its log read with no bytes at all."""
    silent = "logs aws-node-a1 -c aws-eks-nodeagent"

    def answer(args: list[str]) -> fe.Outcome:
        if silent in " ".join(args):
            return fe.Outcome(output=b"", returncode=0)
        return _answer(args)

    _calls, out = _run(monkeypatch, tmp_path, answer=answer)
    name = "aws-node/aws-node-a1.aws-eks-nodeagent.log"
    assert not (out / name).exists()
    index = (out / fe.INDEX).read_text(encoding="utf-8")
    assert f"{name}: empty (0 bytes), no file written\n" in index
    assert f"{name}: ok\n" not in index
    assert "aws-node/aws-node-b2.aws-eks-nodeagent.log: ok\n" in index
    assert (out / "aws-node" / "aws-node-b2.aws-eks-nodeagent.log").is_file()


def test_the_files_land_where_the_index_says(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _calls, out = _run(monkeypatch, tmp_path)
    for name in (
        "nodes-wide.txt",
        "node-conditions.txt",
        "pods-wide.txt",
        "warning-events.txt",
        "stuck-pods/argocd.txt",
        "stuck-pods/dfe.txt",
        "stuck-pods/monitoring.txt",
        "kube-system-events.txt",
        "kube-system-not-ready.txt",
        "node-events/node-a.txt",
        "aws-node/aws-node-a1.aws-node.log",
        "aws-node/aws-node-a1.aws-node.previous.log",
        "aws-node/aws-node-a1.aws-vpc-cni-init.log",
        "aws-node/node-a.ipamd.log",
        "aws-node/node-b.plugin.log",
        "aws-node/node-b.network-policy-agent.log",
    ):
        assert (out / name).is_file(), name
    assert "condition Ready=False" in (out / "node-conditions.txt").read_text(encoding="utf-8")
    index = (out / fe.INDEX).read_text(encoding="utf-8")
    assert "nodes-wide.txt: ok\n    kubectl get nodes -o wide\n" in index
    assert (
        "stuck-pods/argocd.txt: 2 pod(s) summarised from the pod list, each with its events"
        " and logs\n"
        "stuck-pods/argocd.txt, events of argocd-dex-server-1: ok\n"
        f"    kubectl {_pod_events('argocd', 'argocd-dex-server-1')}\n"
    ) in index
    report = (out / "stuck-pods" / "argocd.txt").read_text(encoding="utf-8")
    assert report.startswith("pod argocd/argocd-dex-server-1\n")
    assert "    state waiting, reason ImagePullBackOff, message: ImagePullBackOff detail\n" in report
    assert f"  events: ok\noutput of {_pod_events('argocd', 'argocd-server-1')}\n" in report
    unready = (out / "kube-system-not-ready.txt").read_text(encoding="utf-8")
    assert "pod kube-system/coredns-1\n" in unready
    assert "pod kube-system/ebs-csi-node-1\n" in unready
    assert "pod kube-system/kube-proxy-1\n" not in unready


def test_the_warnings_keep_their_newest_end_and_the_rest_their_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tails: dict[str, bool] = {}

    def fake(argv: list[str], *, timeout: float, limit: int, tail: bool = False) -> fe.Outcome:
        tails[" ".join(argv[1:])] = tail
        return _answer(argv[1:])

    monkeypatch.setattr(fe, "run_bounded", fake)
    out = tmp_path / "evidence"
    out.mkdir()
    fe.collect(fe.Evidence(out, [], 60.0, time.monotonic() + 60.0))
    warnings = [call for call, tail in tails.items() if "type=Warning" in call]
    assert warnings
    assert all(tails[call] for call in warnings)
    assert tails["get pods -A -o wide"] is False
    assert tails[_pod_events("argocd", "argocd-dex-server-1")] is False


def _stuck_pod_events(calls: list[list[str]]) -> list[list[str]]:
    """The per-pod events calls made outside kube-system."""
    return [c for c in calls if c[:3] == ["get", "events", "-n"] and c[3] != "kube-system"]


def test_stuck_pods_past_the_cap_are_summarised_up_to_it_and_the_index_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    many = [
        _waiting_pod(f"ns-{i % 3}", f"pod-{i:03d}", reason="ImagePullBackOff")
        for i in range(fe.MAX_STUCK + 5)
    ]

    def answer(args: list[str]) -> fe.Outcome:
        return _items(many) if " ".join(args) == "get pods -A -o json" else _answer(args)

    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    events = _stuck_pod_events(calls)
    assert len(events) == fe.MAX_STUCK
    assert sorted({c[3] for c in events}) == ["ns-0", "ns-1", "ns-2"]
    summarised = [
        sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("pod "))
        for path in sorted((out / "stuck-pods").iterdir())
    ]
    assert summarised == [15, 15, 10]
    index = (out / fe.INDEX).read_text(encoding="utf-8")
    assert f"{fe.MAX_STUCK + 5} pods are stuck, and the first {fe.MAX_STUCK} are summarised" in index


def test_no_stuck_pod_is_a_named_skip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    healthy = [
        _running("dfe", "dfe-receiver-1", ready=True),
        _waiting_pod("dfe", "dfe-ui-1", reason="ContainerCreating"),
    ]

    def answer(args: list[str]) -> fe.Outcome:
        return _items(healthy) if " ".join(args) == "get pods -A -o json" else _answer(args)

    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert _stuck_pod_events(calls) == []
    assert not (out / "stuck-pods").exists()
    index = (out / fe.INDEX).read_text(encoding="utf-8")
    assert "stuck-pods/: not run: no container is waiting on CrashLoopBackOff, " in index
    assert "no init is unfinished and no pod is Running but not Ready" in index


def test_one_failing_capture_does_not_stop_the_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, out = _run(
        monkeypatch,
        tmp_path,
        fail=("get events --sort-by", "type=Warning", "logs aws-node-a1 -c aws-vpc-cni-init"),
    )
    issued = [" ".join(c) for c in calls]
    bounds = f"--tail={fe.LOG_TAIL} --limit-bytes={fe.TEXT_BYTES} --timestamps"
    assert _pod_events("kube-system", "coredns-1") in issued
    assert _pod_events("kube-system", "ebs-csi-node-1") in issued
    assert _pod_events("argocd", "argocd-dex-server-1") in issued
    assert _pod_events("argocd", "argocd-server-1") in issued
    assert "warning-events.txt: exit 1: Error from server (InternalError): boom" in (
        out / fe.INDEX
    ).read_text(encoding="utf-8")
    assert f"-n kube-system logs aws-node-a1 -c aws-node {bounds}" in issued
    assert f"-n kube-system logs aws-node-b2 -c aws-node {bounds}" in issued
    assert "get --raw /api/v1/nodes/node-b/proxy/logs/aws-routed-eni/plugin.log" in issued
    index = (out / fe.INDEX).read_text(encoding="utf-8")
    assert "kube-system-events.txt: exit 1: Error from server (InternalError): boom" in index
    assert "aws-node/aws-node-a1.aws-vpc-cni-init.log: exit 1:" in index
    assert not (out / "kube-system-events.txt").exists()


def test_a_cluster_that_answers_nothing_is_read_three_times_and_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, out = _run(monkeypatch, tmp_path, fail=("get",))
    assert [" ".join(c) for c in calls] == [
        "get nodes -o wide", "get nodes -o json", "-n kube-system get pods -o json"
    ]
    assert "not run: the API server answered none of the reads above" in (out / fe.INDEX).read_text(
        encoding="utf-8"
    )


def test_an_unreadable_pod_list_still_captures_the_cluster_wide_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, out = _run(monkeypatch, tmp_path, fail=("get pods",))
    issued = [" ".join(c) for c in calls]
    assert any("get events --sort-by" in c for c in issued)
    assert any("--field-selector type=Warning" in c for c in issued)
    assert not any(" logs " in f" {c} " for c in issued)
    assert not any(" describe " in f" {c} " for c in issued)
    index = (out / fe.INDEX).read_text(encoding="utf-8")
    assert "kube-system-not-ready.txt: not run: the kube-system pods were not read" in index
    assert "stuck-pods/: not run: the pods in every namespace were not read" in index
    assert "aws-node/: not run: the kube-system pods were not read" in index
    assert "aws-load-balancer-controller/: not run: the kube-system pods were not read" in index


def test_an_unreadable_cluster_wide_pod_list_still_captures_kube_system(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, out = _run(monkeypatch, tmp_path, fail=("get pods -A",))
    issued = [" ".join(c) for c in calls]
    assert _pod_events("kube-system", "coredns-1") in issued
    assert _pod_events("kube-system", "ebs-csi-node-1") in issued
    assert _stuck_pod_events(calls) == []
    index = (out / fe.INDEX).read_text(encoding="utf-8")
    assert "pods-wide.txt: exit 1: Error from server (InternalError): boom" in index
    assert "stuck-pods/: not run: the pods in every namespace were not read" in index
    assert (out / "aws-node" / "node-a.ipamd.log").is_file()


def test_a_spent_budget_runs_nothing_more_and_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls, out = _run(monkeypatch, tmp_path, budget=-1.0)
    assert calls == []
    assert "nodes-wide.txt: not run: the -1s budget was spent" in (out / fe.INDEX).read_text(
        encoding="utf-8"
    )


def _secret_read_problems(call: list[str]) -> list[str]:
    """Why `call` could print a Secret's data; empty when it reads one only through SECRET_FIELDS."""
    words = [w.lower() for w in call]
    problems = [f"names Secret data: {w}" for w in words if "stringdata" in w or ".data" in w]
    resources = {part for w in words for part in w.replace("/", ",").split(",")}
    if not resources & {"secret", "secrets"}:
        return problems
    at = words.index("get") if "get" in words else None
    if at is None or words[at + 1 : at + 2] != ["secret"]:
        problems.append("names a Secret other than as `get secret NAME`")
    elif at + 2 >= len(words) or words[at + 2].startswith("-"):
        problems.append("`get secret` names no single Secret")
    if {"-a", "--all-namespaces", "-l", "--selector"} & set(words):
        problems.append("lists Secrets instead of naming one")
    outputs = [call[i + 1] for i, w in enumerate(words[:-1]) if w in ("-o", "--output")]
    if len(outputs) != 1 or not outputs[0].startswith("custom-columns="):
        problems.append(f"prints a Secret as {outputs}, not as custom-columns")
        return problems
    paths = {column.split(":", 1)[-1] for column in outputs[0].split("=", 1)[1].split(",")}
    if not paths <= SECRET_FIELDS:
        problems.append(f"prints {sorted(paths - SECRET_FIELDS)}, which is not an allowed field")
    return problems


def test_no_call_reads_the_data_of_a_secret_or_a_pods_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, _out = _run(monkeypatch, tmp_path, fail=("proxy/logs",))
    secret_reads = [call for call in calls if "secret" in call]
    assert len(secret_reads) == 3, secret_reads
    for call in calls:
        words = [w.lower() for w in call]
        assert _secret_read_problems(call) == [], call
        assert "yaml" not in words, call
        assert "describe" not in words, call
        if "exec" in words:
            command = words[words.index("--") + 1 :]
            assert command[0] == "cat", call
            assert command[1].startswith(fe.CNI_LOG_MOUNT + "/"), call


@pytest.mark.parametrize(
    "call",
    [
        ["-n", "a", "get", "secret", "x", "-o", "yaml"],
        ["-n", "a", "get", "secret", "x", "-o", "json"],
        ["-n", "a", "get", "secret", "x"],
        ["-n", "a", "get", "secret", "x", "-o", "jsonpath={.data.password}"],
        ["-n", "a", "get", "secret", "x", "-o", "custom-columns=P:.data.password"],
        ["-n", "a", "get", "secret", "x", "-o", "custom-columns=S:.stringData"],
        ["-n", "a", "get", "secret", "x", "-o", "custom-columns=A:.metadata.annotations"],
        ["-n", "a", "get", "secret", "x", "-o", "custom-columns=T:.type"],
        ["-n", "a", "get", "secrets", "-o", "custom-columns=N:.metadata.name"],
        ["get", "secret", "-A", "-o", "custom-columns=N:.metadata.name"],
        ["-n", "a", "get", "secret", "-l", "k=v", "-o", "custom-columns=N:.metadata.name"],
        ["-n", "a", "get", "secret/x", "-o", "custom-columns=N:.metadata.name"],
        ["-n", "a", "get", "cm,secret", "x", "-o", "custom-columns=N:.metadata.name"],
    ],
)
def test_the_secret_guard_flags_a_call_that_could_print_secret_data(call: list[str]) -> None:
    assert _secret_read_problems(call), call


def test_the_secret_guard_passes_the_reads_the_collector_makes() -> None:
    events = "get events -A --field-selector involvedObject.kind=ExternalSecret,type=Normal"
    assert _secret_read_problems(["-n", "a", "get", "secret", "x", "-o", SECRET_COLUMNS]) == []
    assert _secret_read_problems(EXTERNAL_SECRETS_READ.split()) == []
    assert _secret_read_problems(events.split()) == []


@pytest.mark.parametrize(
    "fail",
    [(), ("get events",), ("get pods -A",), ("-n kube-system get pods",), ("proxy/logs",), ("get",)],
    ids=["healthy", "events-fail", "pod-list-fails", "kube-system-fails", "no-cni-log", "all-fail"],
)
def test_no_describe_is_issued_whatever_the_cluster_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fail: tuple[str, ...]
) -> None:
    calls, _out = _run(monkeypatch, tmp_path, fail=fail)
    assert calls
    assert not any("describe" in word.lower() for call in calls for word in call)


def test_a_pods_env_args_command_volumes_and_annotations_reach_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kube_system = [_with_private_fields(pod) for pod in PODS]
    everywhere = [_with_private_fields(pod) for pod in ALL_PODS]

    def answer(args: list[str]) -> fe.Outcome:
        joined = " ".join(args)
        if joined == "-n kube-system get pods -o json":
            return _items(kube_system)
        if joined == "get pods -A -o json":
            return _items(everywhere)
        return _answer(args)

    _calls, out = _run(monkeypatch, tmp_path, answer=answer)
    written = sorted(path for path in out.rglob("*") if path.is_file())
    assert out / "kube-system-not-ready.txt" in written
    assert out / "stuck-pods" / "dfe.txt" in written
    for path in written:
        assert SENTINEL not in path.read_text(encoding="utf-8", errors="replace"), path
    # The summary was built from these very pods, so the check above is not vacuous.
    stuck = (out / "stuck-pods" / "dfe.txt").read_text(encoding="utf-8")
    assert "pod dfe/dfe-loader-1\n" in stuck
    assert "    image registry.example/main:1\n" in stuck


def test_a_failing_events_call_keeps_that_pods_summary_and_is_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _calls, out = _run(monkeypatch, tmp_path, fail=("involvedObject.name=argocd-server-1",))
    report = (out / "stuck-pods" / "argocd.txt").read_text(encoding="utf-8")
    assert "pod argocd/argocd-server-1\n" in report
    assert "  events: exit 1: Error from server (InternalError): boom\n" in report
    assert f"  events: ok\noutput of {_pod_events('argocd', 'argocd-dex-server-1')}\n" in report
    index = (out / fe.INDEX).read_text(encoding="utf-8")
    assert (
        "stuck-pods/argocd.txt, events of argocd-server-1: exit 1: "
        "Error from server (InternalError): boom\n"
    ) in index


def test_a_spent_budget_still_writes_the_summary_and_runs_no_events_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(fe, "run_bounded", lambda argv, **_kw: calls.append(argv))
    out = tmp_path / "evidence"
    out.mkdir()
    evidence = fe.Evidence(out, [], 300.0, time.monotonic() - 1)
    evidence.pod_report("pods.txt", "dfe", [ALL_PODS[2]])
    assert calls == []
    report = (out / "pods.txt").read_text(encoding="utf-8")
    assert "pod dfe/dfe-loader-1\n" in report
    assert "  events: not run: the 300s budget was spent\n" in report


# --- redaction of free text -----------------------------------------------------------

# Built from parts so no literal here looks like a credential to a secret scanner.
FAKE = "hunter2-FAKE-" + "not-a-credential"
AWS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
GITHUB_TOKEN = "ghp_" + "a1B2" * 9
JWT = "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "abc-DEF_123"
PEM_BEGIN = "-----BEGIN " + "RSA PRIVATE KEY-----"
PEM_END = "-----END " + "RSA PRIVATE KEY-----"

# (log text, the part of it that must not survive)
SECRET_LINES = [
    (f"DB_PASSWORD={FAKE}", FAKE),
    (f'{{"level":"error","password":"{FAKE}","msg":"x"}}', FAKE),
    (f'{{"msg":"{{\\"token\\":\\"{FAKE}\\"}}"}}', FAKE),
    (f'E1014 main.go:1] "msg"="boom" "client_secret"="{FAKE}" "other"="kept"', FAKE),
    (f"connect to postgres://dfe:{FAKE}@db.example:5432/x failed", FAKE),
    (f"fetch https://{FAKE}@git.example/x.git", FAKE),
    (f"Authorization: Bearer {FAKE}", FAKE),
    (f"authorization: Token {FAKE} trailing words", FAKE),
    (f"Cookie: sid={FAKE}; other=1", FAKE),
    (f"curl --password {FAKE} --user bob", FAKE),
    (f"AWS_SECRET_ACCESS_KEY={FAKE}", FAKE),
    (f"accessToken: {FAKE}", FAKE),
    (f"password='{FAKE}' rest", FAKE),
    (f'password="open quote {FAKE}', FAKE),
    (f"token => {FAKE}", FAKE),
    (f'"auth": "{FAKE}"', FAKE),
    # A value printed as a type wrapping a string: Rust Debug, a Python repr.
    (f'sasl_password: Some("{FAKE}")', FAKE),
    (f'password: Password("{FAKE}")', FAKE),
    (f"password: SecretStr('{FAKE}')", FAKE),
    (f"key {AWS_KEY_ID} leaked", AWS_KEY_ID),
    (f"gh {GITHUB_TOKEN} leaked", GITHUB_TOKEN),
    (f"jwt {JWT} end", JWT),
    (f"{PEM_BEGIN}\n{FAKE}\nMORE\n{PEM_END}\nafter", FAKE),
    (f"{FAKE}\nBODY\n{PEM_END}\nafter", FAKE),
    (f"before\n{PEM_BEGIN}\n{FAKE}\n", FAKE),
]


@pytest.mark.parametrize(("text", "planted"), SECRET_LINES)
def test_redact_masks_each_credential_shape(text: str, planted: str) -> None:
    redacted = fe.redact(text)
    assert planted not in redacted
    assert fe.MASK in redacted


def test_redact_keeps_the_line_structure_and_the_text_around_a_credential() -> None:
    assert fe.redact(f"before\npassword={FAKE} rest\nafter\n") == (
        f"before\npassword={fe.MASK} rest\nafter\n"
    )
    assert fe.redact(f'{{"password":"{FAKE}","msg":"x"}}') == (
        f'{{"password":"{fe.MASK}","msg":"x"}}'
    )
    assert fe.redact(f"{PEM_BEGIN}\n{FAKE}\n{PEM_END}\nafter") == f"{fe.MASK}\nafter"
    # The wrapper's closing bracket and what follows it stay.
    assert fe.redact(f'sasl_password: Some("{FAKE}"), retries: 3') == (
        f"sasl_password: {fe.MASK}), retries: 3"
    )


@pytest.mark.parametrize(
    "text",
    [
        "token_count=5 tokenizer: bert",
        "restartCount=3 oauth: enabled",
        "keys: 4 key_count=2 monkey=1",
        'I1014 12:00:00.000 reconciler.go:12] "Starting" worker=3',
        'Successfully pulled image "quay.io/jetstack/cert-manager-controller:v1.21.2"',
        "https://github.com/hyperi-io/dfe-infra/pull/1",
        "basic authentication required, bearer authentication too",
    ],
)
def test_redact_leaves_ordinary_log_text_alone(text: str) -> None:
    assert fe.redact(text) == text


def test_redact_bytes_replaces_what_is_not_utf8_and_still_masks() -> None:
    out = fe.redact_bytes(b"\xff password=" + FAKE.encode() + b"\n")
    assert FAKE.encode() not in out
    assert out.decode("utf-8").endswith(f"password={fe.MASK}\n")


# --- the container logs of a stuck or unready pod -------------------------------------


def _container(name: str, *, state: dict, ready: bool = False, restarts: int = 0, last=None) -> dict:
    seen = {"name": name, "ready": ready, "restartCount": restarts, "state": state}
    if last:
        seen["lastState"] = {"terminated": last}
    return seen


def _pod(namespace: str, name: str, *, init: list[dict] = (), main: list[dict] = ()) -> dict:
    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "initContainers": [{"name": c["name"], "image": "registry.example/i:1"} for c in init],
            "containers": [{"name": c["name"], "image": "registry.example/m:1"} for c in main],
        },
        "status": {
            "phase": "Pending",
            "initContainerStatuses": list(init),
            "containerStatuses": list(main),
        },
    }


EXITED_ZERO = {"terminated": {"reason": "Completed", "exitCode": 0}}
RUNNING = {"running": {}}
CRASHED = {"reason": "Error", "exitCode": 1}
# A controller in CrashLoopBackOff after 5 restarts, as a failed MSK cycle left it.
CERT_MANAGER = _pod(
    "cert-manager",
    "cert-manager-58cbcbdbf4-tt6f5",
    main=[
        _container(
            "cert-manager-controller",
            state={"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 2m40s"}},
            restarts=5,
            last=CRASHED,
        )
    ],
)
# Init:0/1 as a failed MSK cycle left it: the init container runs again after one crash.
ARCHIVER = _pod(
    "dfe-test",
    "dfe-archiver-69ff4c69d9-9l7kk",
    init=[_container("wait-for-kafka", state=RUNNING, restarts=1, last=CRASHED)],
    main=[_container("dfe-archiver", state={"waiting": {"reason": "PodInitializing"}})],
)
# Init:0/2: the first init container cannot pull its image, so nothing has run.
LOADER = _pod(
    "dfe-test",
    "dfe-loader-5ffb5569cd-2vjs4",
    init=[
        _container("copy-config", state={"waiting": {"reason": "ImagePullBackOff"}}),
        _container("wait-db", state={"waiting": {"reason": "PodInitializing"}}),
    ],
    main=[_container("dfe-loader", state={"waiting": {"reason": "PodInitializing"}})],
)
STALLED_DNS = _pod(
    "kube-system",
    "coredns-2",
    main=[
        _container("coredns", state=RUNNING, ready=False),
        _container("metrics", state=RUNNING, ready=True),
    ],
)
STALLED_DNS["status"]["phase"] = "Running"


def test_a_crashlooping_container_is_read_for_its_previous_run_only() -> None:
    assert fe.log_plans(CERT_MANAGER) == [
        fe.LogPlan("cert-manager-controller", init=False, previous=True)
    ]


def test_a_running_init_container_that_crashed_once_is_read_for_both_runs() -> None:
    assert fe.log_plans(ARCHIVER) == [
        fe.LogPlan("wait-for-kafka", init=True),
        fe.LogPlan("wait-for-kafka", init=True, previous=True),
        fe.LogPlan(
            "dfe-archiver",
            init=False,
            absent="it has not started (waiting, reason PodInitializing)",
        ),
    ]


def test_a_container_that_never_started_has_nothing_to_read() -> None:
    plans = fe.log_plans(LOADER)
    assert [p.container for p in plans] == ["copy-config", "wait-db", "dfe-loader"]
    assert all(p.absent and not p.previous for p in plans)
    assert plans[0].absent == "it has not started (waiting, reason ImagePullBackOff)"


def test_a_running_main_container_is_read_only_while_it_is_not_ready() -> None:
    assert fe.log_plans(STALLED_DNS) == [fe.LogPlan("coredns", init=False)]


def test_a_finished_init_container_and_a_clean_exit_are_left_out_a_failed_one_is_read() -> None:
    pod = _pod(
        "dfe",
        "job-1",
        init=[
            _container("done", state=EXITED_ZERO, ready=True),
            _container("failed", state={"terminated": CRASHED}),
        ],
        main=[_container("main", state=EXITED_ZERO)],
    )
    assert fe.log_plans(pod) == [fe.LogPlan("failed", init=True)]
    assert fe.log_plans({}) == []


def _cluster(
    *, everywhere: list[dict] | None = None, kube_system: list[dict] | None = None, logs=None
):
    """An `answer` for `_run`: the pod lists given, and `logs` as {text in the call: answer}."""
    logs = logs or {}

    def answer(args: list[str]) -> fe.Outcome:
        joined = " ".join(args)
        if joined == "get pods -A -o json" and everywhere is not None:
            return _items(everywhere)
        if joined == "-n kube-system get pods -o json" and kube_system is not None:
            return _items(kube_system)
        for needle, result in logs.items():
            if needle in joined:
                if isinstance(result, fe.Outcome):
                    return result
                return fe.Outcome(output=result.encode(), returncode=0)
        return _answer(args)

    return answer


def _read(out: Path, name: str) -> str:
    return (out / name).read_text(encoding="utf-8")


def _log_calls(calls: list[list[str]]) -> list[str]:
    return [" ".join(c) for c in calls if "logs" in c and f"--tail={fe.POD_LOG_TAIL}" in c]


def test_a_crashlooping_pod_report_carries_the_crashed_runs_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    crash = '2026-10-14T15:36:00Z E1014 main.go:1] "msg"="boom: no matches for kind"\n'
    answer = _cluster(
        everywhere=[CERT_MANAGER],
        logs={"cert-manager-controller --tail": crash},
    )
    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    pod = "cert-manager-58cbcbdbf4-tt6f5"
    assert _log_calls(calls) == [
        f"-n cert-manager logs {pod} -c cert-manager-controller --tail=200 --timestamps --previous"
    ]
    report = _read(out, "stuck-pods/cert-manager.txt")
    assert f"  logs of container cert-manager-controller, previous run: ok\n{crash}" in report
    assert report.index("  events: ok") < report.index("  logs of container")
    assert (
        f"stuck-pods/cert-manager.txt, container cert-manager-controller previous run of {pod}: ok\n"
        f"    kubectl -n cert-manager logs {pod} -c cert-manager-controller "
        "--tail=200 --timestamps --previous\n"
    ) in _read(out, fe.INDEX)


def test_a_pod_stuck_in_init_is_found_and_its_init_container_logs_are_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answer = _cluster(
        everywhere=[ARCHIVER],
        logs={"-c wait-for-kafka --tail=200 --timestamps --previous": "crashed run\n",
              "-c wait-for-kafka": "waiting for broker\n"},
    )
    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    pod = "dfe-archiver-69ff4c69d9-9l7kk"
    assert _log_calls(calls) == [
        f"-n dfe-test logs {pod} -c wait-for-kafka --tail=200 --timestamps",
        f"-n dfe-test logs {pod} -c wait-for-kafka --tail=200 --timestamps --previous",
    ]
    report = _read(out, "stuck-pods/dfe-test.txt")
    assert "  logs of init container wait-for-kafka, current run: ok\nwaiting for broker\n" in report
    assert "  logs of init container wait-for-kafka, previous run: ok\ncrashed run\n" in report
    assert (
        "  logs of container dfe-archiver, current run: none, "
        "it has not started (waiting, reason PodInitializing)\n"
    ) in report


def test_a_container_that_never_started_is_one_line_and_no_kubectl_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, out = _run(monkeypatch, tmp_path, answer=_cluster(everywhere=[LOADER]))
    assert _log_calls(calls) == []
    report = _read(out, "stuck-pods/dfe-test.txt")
    assert (
        "  logs of init container copy-config, current run: none, "
        "it has not started (waiting, reason ImagePullBackOff)\n"
    ) in report
    assert "pod dfe-test/dfe-loader-5ffb5569cd-2vjs4\n" in report


def test_a_container_with_no_previous_instance_is_one_plain_line_and_the_rest_still_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gone = fe.Outcome(
        returncode=1,
        error='Error from server (BadRequest): previous terminated container '
        '"cert-manager-controller" in pod "cert-manager-58cbcbdbf4-tt6f5" not found',
    )
    answer = _cluster(
        everywhere=[CERT_MANAGER, ARCHIVER],
        logs={"cert-manager-controller": gone, "-c wait-for-kafka": "waiting for broker\n"},
    )
    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert len(_log_calls(calls)) == 3
    report = _read(out, "stuck-pods/cert-manager.txt")
    assert (
        "  logs of container cert-manager-controller, previous run: exit 1: Error from server "
        '(BadRequest): previous terminated container "cert-manager-controller" in pod '
        '"cert-manager-58cbcbdbf4-tt6f5" not found\n'
    ) in report
    assert "waiting for broker" in _read(out, "stuck-pods/dfe-test.txt")
    assert "pod cert-manager/cert-manager-58cbcbdbf4-tt6f5\n" in report


def test_a_failed_read_keeps_what_it_printed_and_names_the_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cut = fe.Outcome(output=b"first lines\n", returncode=1, error="unexpected EOF")
    answer = _cluster(everywhere=[CERT_MANAGER], logs={"cert-manager-controller --tail": cut})
    _calls, out = _run(monkeypatch, tmp_path, answer=answer)
    report = _read(out, "stuck-pods/cert-manager.txt")
    assert "previous run: exit 1: unexpected EOF\nfirst lines\n" in report


def test_a_running_unready_kube_system_container_has_its_current_logs_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answer = _cluster(kube_system=[*PODS, STALLED_DNS], logs={"-c coredns": "plugin/ready: not ready\n"})
    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert "-n kube-system logs coredns-2 -c coredns --tail=200 --timestamps" in _log_calls(calls)
    assert not any("-c metrics" in line for line in _log_calls(calls))
    unready = _read(out, "kube-system-not-ready.txt")
    assert "  logs of container coredns, current run: ok\nplugin/ready: not ready\n" in unready


def test_a_pod_with_no_container_status_says_there_is_nothing_to_read(tmp_path: Path) -> None:
    evidence = fe.Evidence(tmp_path, [], 60.0, time.monotonic() + 60)
    text = evidence.pod_logs("f.txt", "dfe", {"metadata": {"name": "unscheduled-1"}})
    assert text == "  logs: no container is running, restarted or failed, so there are none\n"
    assert evidence.runs == 0


def test_pod_logs_are_redacted_before_they_are_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    leaky = (
        f"2026-10-14T15:36:00Z DB_PASSWORD={FAKE}\n"
        f'2026-10-14T15:36:01Z {{"client_secret":"{FAKE}","msg":"connect"}}\n'
        f"2026-10-14T15:36:02Z dial postgres://dfe:{FAKE}@db.example/x\n"
        f"2026-10-14T15:36:03Z using {GITHUB_TOKEN} and {JWT}\n"
        f"2026-10-14T15:36:04Z Authorization: Bearer {FAKE}\n"
        "2026-10-14T15:36:05Z connection refused to db.example:5432\n"
    )
    pod = json.loads(json.dumps(CERT_MANAGER))
    last = pod["status"]["containerStatuses"][0]["lastState"]["terminated"]
    last["message"] = f"fatal: postgres://dfe:{FAKE}@db.example/x"
    answer = _cluster(everywhere=[pod], logs={"cert-manager-controller --tail": leaky})
    _calls, out = _run(monkeypatch, tmp_path, answer=answer)
    report = _read(out, "stuck-pods/cert-manager.txt")
    assert "connection refused to db.example:5432\n" in report
    assert report.count(fe.MASK) >= 5
    for path in out.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            for planted in (FAKE, GITHUB_TOKEN, JWT):
                assert planted not in text, path


def test_the_kube_system_container_and_cni_logs_are_redacted_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answer = _cluster(
        logs={
            "logs aws-node-a1 -c aws-node ": f"level=error password={FAKE}\n",
            "logs lbc-a -c aws-load-balancer-controller ": f'{{"token":"{FAKE}"}}\n',
            "proxy/logs/aws-routed-eni/ipamd.log": f"api_key: {FAKE}\n",
        }
    )
    _calls, out = _run(monkeypatch, tmp_path, answer=answer)
    files = [
        "aws-node/aws-node-a1.aws-node.log",
        "aws-node/aws-node-a1.aws-node.previous.log",
        "aws-load-balancer-controller/lbc-a.aws-load-balancer-controller.log",
        "aws-node/node-a.ipamd.log",
    ]
    for name in files:
        text = _read(out, name)
        assert FAKE not in text, name
        assert fe.MASK in text, name
    assert _read(out, "aws-node/node-b.plugin.log").startswith("output of get --raw")


def test_pod_logs_stop_at_the_total_for_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    limits: list[int] = []

    def fake(argv: list[str], *, timeout: float, limit: int, tail: bool = False) -> fe.Outcome:
        limits.append(limit)
        data = b"x" * 100
        return fe.Outcome(output=data[-limit:], returncode=0, truncated=len(data) > limit)

    monkeypatch.setattr(fe, "run_bounded", fake)
    pod = _pod("ns", "p", main=[_container(n, state=RUNNING) for n in ("a", "b", "c")])
    evidence = fe.Evidence(tmp_path, [], 300.0, time.monotonic() + 300, pod_log_left=150)
    text = evidence.pod_logs("f.txt", "ns", pod)
    assert limits == [150, 50]
    assert evidence.pod_log_left == 0
    assert f"  logs of container c, current run: not read: the {fe.POD_LOG_TOTAL} byte limit" in text
    assert "  logs of container b, current run: ok, truncated to 50 bytes\n" in text


def test_pod_logs_leave_the_last_share_of_the_budget_to_the_other_captures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(fe, "run_bounded", lambda argv, **_kw: calls.append(argv))
    pod = _pod("ns", "p", main=[_container("a", state=RUNNING)])
    kept = 300.0 * fe.POD_LOG_KEEP
    evidence = fe.Evidence(tmp_path, [], 300.0, time.monotonic() + kept - 1)
    text = evidence.pod_logs("f.txt", "ns", pod)
    assert calls == []
    assert "  logs of container a, current run: not run: the time left is kept" in text


def test_a_noisy_pod_is_cut_to_its_newest_bytes_and_redacted_end_to_end(tmp_path: Path) -> None:
    prefix = "--kubeconfig /k/config"
    noise = "".join(f"2026-10-14T15:36:00Z line {i:05d} {'x' * 80}\n" for i in range(5000))
    noise += f"2026-10-14T15:36:01Z last line password={FAKE}\n"
    fixture = {
        "json": {
            f"{prefix} get nodes -o json": [_node("node-a")],
            f"{prefix} -n kube-system get pods -o json": [],
            f"{prefix} get pods -A -o json": [CERT_MANAGER],
        },
        "log_text": noise,
    }
    done, out = _run_cli(tmp_path, fixture)
    assert done.returncode == 0, done.stderr
    assert len(noise) > 8 * fe.POD_LOG_BYTES
    report = _read(out, "stuck-pods/cert-manager.txt")
    assert len(report.encode("utf-8")) < fe.POD_LOG_BYTES + 8192
    assert "last line password=" + fe.MASK in report
    assert "line 04999" in report
    assert "line 00000" not in report
    pod = "cert-manager-58cbcbdbf4-tt6f5"
    assert f"previous run of {pod}: ok, truncated to {fe.POD_LOG_BYTES}" in _read(out, fe.INDEX)
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    read = f"-n cert-manager logs {pod} -c cert-manager-controller --tail=200 --timestamps"
    assert f"{prefix} {read} --previous" in calls
    for path in out.rglob("*"):
        if path.is_file():
            assert FAKE not in path.read_text(encoding="utf-8", errors="replace"), path


# --- pods Running but not Ready, in any namespace -------------------------------------


def _running(namespace: str, name: str, *, ready: bool = False, deleting: bool = False) -> dict:
    """A Running pod with one container, ready or not, and an init container that finished."""
    pod = _pod(
        namespace,
        name,
        init=[_container("setup", state=EXITED_ZERO, ready=True)],
        main=[_container("main", state=RUNNING, ready=ready)],
    )
    pod["status"]["phase"] = "Running"
    if deleting:
        pod["metadata"]["deletionTimestamp"] = "2026-10-14T15:00:00Z"
    return pod


# Running 0/1 with /readyz answering 503: no wait reason, no restart, the cause only in its log.
ENGINE = _pod(
    "dfe-test",
    "dfe-engine-7c9d6b5f4-x2k8p",
    main=[_container("dfe-engine", state=RUNNING, ready=False)],
)
ENGINE["status"]["phase"] = "Running"
READYZ = "2026-10-14T19:40:11Z readyz: 503 schema store unreachable: dial tcp 10.90.4.7:9000\n"


def test_a_running_pod_that_is_not_ready_is_found_but_not_a_finished_or_deleted_one() -> None:
    pods = [
        ENGINE,
        _running("dfe-test", "ready-1", ready=True),
        _running("dfe-test", "terminating-1", deleting=True),
        _pod("dfe-test", "pending-1", main=[_container("main", state=RUNNING)]),
        _running("kube-system", "coredns-9"),
    ]
    assert fe.running_unready_pods(pods) == [
        ("dfe-test", "dfe-engine-7c9d6b5f4-x2k8p"),
        ("kube-system", "coredns-9"),
    ]
    assert fe.running_unready_pods(pods, skip=("kube-system",)) == [
        ("dfe-test", "dfe-engine-7c9d6b5f4-x2k8p")
    ]


def test_a_running_pod_with_a_failing_readiness_probe_has_its_log_in_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answer = _cluster(everywhere=[ENGINE], logs={"-c dfe-engine --tail": READYZ})
    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    pod = "dfe-engine-7c9d6b5f4-x2k8p"
    assert _log_calls(calls) == [f"-n dfe-test logs {pod} -c dfe-engine --tail=200 --timestamps"]
    assert _pod_events("dfe-test", pod) in [" ".join(c) for c in calls]
    report = _read(out, "stuck-pods/dfe-test.txt")
    assert f"pod dfe-test/{pod}\n" in report
    assert f"  logs of container dfe-engine, current run: ok\n{READYZ}" in report


def _pod_names(report: str) -> list[str]:
    return [line.split("/", 1)[1] for line in report.splitlines() if line.startswith("pod ")]


def test_not_ready_pods_follow_the_stuck_ones_and_the_report_stays_at_the_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stuck = [_waiting_pod(f"ns-{i % 3}", f"stuck-{i:02d}", reason="ImagePullBackOff") for i in range(30)]
    unready = [_running("aa-first", f"slow-{i:02d}") for i in range(20)]
    answer = _cluster(everywhere=[*unready, *stuck])
    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    events = _stuck_pod_events(calls)
    assert len(events) == fe.MAX_STUCK
    first = _read(out, "stuck-pods/aa-first.txt")
    assert _pod_names(first) == [f"slow-{i:02d}" for i in range(10)]
    stuck_names = [n for ns in ("ns-0", "ns-1", "ns-2") for n in _pod_names(_read(out, f"stuck-pods/{ns}.txt"))]
    assert sorted(stuck_names) == [f"stuck-{i:02d}" for i in range(30)]
    names = ", ".join(f"aa-first/slow-{i:02d}" for i in range(10, 20))
    closing = f"+10 more not-Ready pods, not summarised: {names}"
    assert _read(out, "stuck-pods/aa-first.txt").endswith(closing + "\n")
    index = _read(out, fe.INDEX)
    assert f"stuck-pods/: {closing}\n" in index
    assert "pods are stuck, and the first" not in index


def test_the_closing_line_names_ten_pods_and_counts_the_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unready = [_running("dfe", f"slow-{i:02d}") for i in range(fe.MAX_STUCK + 25)]
    calls, out = _run(monkeypatch, tmp_path, answer=_cluster(everywhere=unready))
    assert len(_stuck_pod_events(calls)) == fe.MAX_STUCK
    report = _read(out, "stuck-pods/dfe.txt")
    assert _pod_names(report) == [f"slow-{i:02d}" for i in range(fe.MAX_STUCK)]
    names = ", ".join(f"dfe/slow-{i:02d}" for i in range(fe.MAX_STUCK, fe.MAX_STUCK + 10))
    assert report.endswith(f"+25 more not-Ready pods, not summarised: {names}, and 15 more\n")


def test_a_full_stuck_set_leaves_no_room_for_a_not_ready_pod(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stuck = [_waiting_pod("zz", f"stuck-{i:02d}", reason="ErrImagePull") for i in range(fe.MAX_STUCK + 5)]
    unready = [_running("aa", "slow-1"), _running("aa", "slow-2")]
    calls, out = _run(monkeypatch, tmp_path, answer=_cluster(everywhere=[*unready, *stuck]))
    assert len(_stuck_pod_events(calls)) == fe.MAX_STUCK
    assert not (out / "stuck-pods" / "aa.txt").exists()
    assert _read(out, "stuck-pods/zz.txt").endswith(
        "+2 more not-Ready pods, not summarised: aa/slow-1, aa/slow-2\n"
    )
    index = _read(out, fe.INDEX)
    assert f"{fe.MAX_STUCK + 5} pods are stuck, and the first {fe.MAX_STUCK} are summarised" in index


def test_a_pod_stuck_on_a_wait_reason_is_reported_once_not_again_as_not_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    crashing = _waiting_pod("dfe", "dfe-loader-1", reason="CrashLoopBackOff")
    crashing["status"]["phase"] = "Running"
    calls, out = _run(monkeypatch, tmp_path, answer=_cluster(everywhere=[crashing]))
    assert len(_stuck_pod_events(calls)) == 1
    assert _pod_names(_read(out, "stuck-pods/dfe.txt")) == ["dfe-loader-1"]


def test_a_kube_system_pod_is_not_summarised_twice_when_its_own_report_holds_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answer = _cluster(everywhere=[STALLED_DNS], kube_system=[STALLED_DNS])
    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert not (out / "stuck-pods" / "kube-system.txt").exists()
    assert "pod kube-system/coredns-2\n" in _read(out, "kube-system-not-ready.txt")
    assert _log_calls(calls) == ["-n kube-system logs coredns-2 -c coredns --tail=200 --timestamps"]


def test_a_kube_system_pod_is_summarised_in_the_stuck_report_when_its_own_list_was_not_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answer = _cluster(everywhere=[STALLED_DNS])
    calls, out = _run(monkeypatch, tmp_path, answer=answer, fail=("-n kube-system get pods",))
    assert "pod kube-system/coredns-2\n" in _read(out, "stuck-pods/kube-system.txt")
    assert not (out / "kube-system-not-ready.txt").exists()
    assert len(_log_calls(calls)) == 1


# --- the load balancer controller, its webhooks and the Services it programs ---------


def test_a_bundle_is_kept_as_a_digest_and_never_whole() -> None:
    assert fe.bundle_digest(None) == "absent"
    assert fe.bundle_digest("") == "absent"
    digest = fe.bundle_digest(BUNDLE)
    assert digest.startswith("sha256:")
    assert digest.endswith(f"({len(BUNDLE)} base64 chars)")
    assert BUNDLE not in digest
    # The same bundle gives the same digest, so two webhooks can be compared by it.
    assert fe.bundle_digest(BUNDLE) == digest
    assert fe.bundle_digest(BUNDLE + "QQ") != digest


def test_the_webhook_summary_names_each_injection_source_and_digest() -> None:
    digest = fe.bundle_digest(BUNDLE)
    assert fe.webhook_ca_lines(WEBHOOK_CONFIGS) == [
        "MutatingWebhookConfiguration aws-load-balancer-webhook",
        "  cert-manager.io/inject-ca-from kube-system/aws-load-balancer-serving-cert",
        f"  webhook mpod.elbv2.k8s.aws caBundle {digest}",
        f"  webhook mtargetgroupbinding.elbv2.k8s.aws caBundle {digest}",
        "ValidatingWebhookConfiguration aws-load-balancer-webhook",
        "  cert-manager.io/inject-ca-from -",
        "  webhook vtargetgroupbinding.elbv2.k8s.aws caBundle absent",
        "distinct caBundle values: 2",
    ]


def test_services_gateways_certificates_and_the_controller_are_captured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, out = _run(monkeypatch, tmp_path)
    issued = [" ".join(c) for c in calls]
    assert "get services -A -o wide" in issued
    assert "get gateways.gateway.networking.k8s.io -A -o wide" in issued
    assert "-n kube-system get certificates.cert-manager.io -o wide" in issued
    assert WEBHOOK_READ in issued
    bounds = f"--tail={fe.LOG_TAIL} --limit-bytes={fe.TEXT_BYTES} --timestamps"
    for pod in ("lbc-a", "lbc-b"):
        assert f"-n kube-system logs {pod} -c aws-load-balancer-controller {bounds}" in issued
    for name in (
        "services-wide.txt",
        "gateways-wide.txt",
        "kube-system-certificates.txt",
        "aws-load-balancer-controller/webhook-ca.txt",
        "aws-load-balancer-controller/lbc-a.aws-load-balancer-controller.log",
        "aws-load-balancer-controller/lbc-a.aws-load-balancer-controller.previous.log",
        "aws-load-balancer-controller/lbc-b.aws-load-balancer-controller.log",
    ):
        assert (out / name).is_file(), name
    summary = (out / "aws-load-balancer-controller" / "webhook-ca.txt").read_text(encoding="utf-8")
    assert summary.splitlines() == fe.webhook_ca_lines(WEBHOOK_CONFIGS)


def test_no_file_carries_a_ca_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The bundle sits in each webhook and the last-applied annotation; neither reaches a file."""
    _calls, out = _run(monkeypatch, tmp_path)
    written = [path for path in out.rglob("*") if path.is_file()]
    assert out / "aws-load-balancer-controller" / "webhook-ca.txt" in written
    for path in written:
        assert BUNDLE not in path.read_text(encoding="utf-8", errors="replace"), path


def test_a_cluster_without_the_controller_says_so_and_reads_no_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    others = [pod for pod in PODS if not pod["metadata"]["name"].startswith("lbc-")]

    def answer(args: list[str]) -> fe.Outcome:
        joined = " ".join(args)
        if joined == "-n kube-system get pods -o json":
            return _items(others)
        if joined == WEBHOOK_READ:
            return _items([])
        return _answer(args)

    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert not any("logs lbc-" in " ".join(c) for c in calls)
    assert not (out / "aws-load-balancer-controller").exists()
    index = (out / fe.INDEX).read_text(encoding="utf-8")
    selector = "app.kubernetes.io/name=aws-load-balancer-controller"
    assert (
        "aws-load-balancer-controller/webhook-ca.txt: "
        f"no webhook configuration carries {selector}, nothing written" in index
    )
    assert f"aws-load-balancer-controller/: not run: no kube-system pod carries {selector}" in index


# --- ExternalSecrets, the Secrets generators write, and the ESO controller ------------


def test_an_external_secret_is_generator_backed_only_through_a_data_from_generator() -> None:
    kafka, store_copy, jwt, clickhouse = EXTERNAL_SECRET_ITEMS
    assert fe.generator_backed(kafka)
    assert fe.generator_backed(jwt)
    assert fe.generator_backed(clickhouse)
    assert not fe.generator_backed(store_copy)
    inline = {"spec": {"dataFrom": [{"sourceRef": {"generator": {"kind": "Password"}}}]}}
    assert fe.generator_backed(inline)
    from_data = {"spec": {"data": [{"sourceRef": {"generatorRef": {"kind": "Password"}}}]}}
    assert not fe.generator_backed(from_data)
    assert not fe.generator_backed({})
    assert not fe.generator_backed({"spec": {"dataFrom": [None, {}]}})


def test_the_secrets_a_generator_writes_are_named_by_target_once_each() -> None:
    assert fe.generated_secrets(EXTERNAL_SECRET_ITEMS) == [
        ("clickhouse", "clickhouse-admin-password"),
        ("dfe", "dfe-engine-jwt"),
        ("kafka", "dfe-kafka-user"),
    ]
    twice = [*EXTERNAL_SECRET_ITEMS, EXTERNAL_SECRET_ITEMS[0]]
    assert fe.generated_secrets(twice) == fe.generated_secrets(EXTERNAL_SECRET_ITEMS)
    assert fe.target_secret(EXTERNAL_SECRET_ITEMS[1]) == ("dfe", "dfe-kafka-user")
    assert fe.generated_secrets([]) == []
    assert fe.generated_secrets([{"spec": EXTERNAL_SECRET_ITEMS[0]["spec"]}]) == []


def test_the_status_lines_carry_target_policy_sync_and_conditions() -> None:
    lines = fe.external_secret_lines(EXTERNAL_SECRET_ITEMS)
    created = "created 2026-10-14T01:00:00Z generation 2"
    assert lines[:4] == [
        f"externalsecret clickhouse/clickhouse-admin-password {created}",
        "  writes Secret clickhouse-admin-password, refreshPolicy CreatedOnce, refreshInterval 0, "
        "immutable true, generator yes",
        "  syncedResourceVersion 2-5d41402a refreshTime 2026-10-14T01:00:05Z",
        "  condition Ready=True SecretSynced: secret synced (since 2026-10-14T01:00:05Z)",
    ]
    assert f"externalsecret dfe/engine-jwt {created}" in lines
    assert (
        "  writes Secret dfe-engine-jwt, refreshPolicy CreatedOnce, refreshInterval 0, "
        "immutable false, generator yes" in lines
    )
    copy = lines.index(f"externalsecret dfe/dfe-kafka-user {created}")
    assert lines[copy + 1].endswith("immutable false, generator no")
    never_synced = {"metadata": {"name": "x", "namespace": "dfe"}, "spec": {}}
    assert fe.external_secret_lines([never_synced]) == [
        "externalsecret dfe/x created - generation -",
        "  writes Secret x, refreshPolicy -, refreshInterval -, immutable false, generator no",
        "  syncedResourceVersion - refreshTime -",
    ]


def test_every_external_secret_capture_is_issued_and_lands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, out = _run(monkeypatch, tmp_path)
    issued = [" ".join(c) for c in calls]
    assert EXTERNAL_SECRETS_READ in issued
    for namespace, secret in (
        ("clickhouse", "clickhouse-admin-password"),
        ("dfe", "dfe-engine-jwt"),
        ("kafka", "dfe-kafka-user"),
    ):
        assert f"-n {namespace} get secret {secret} -o {SECRET_COLUMNS}" in issued
        assert (out / "generated-secrets" / f"{namespace}.{secret}.txt").is_file()
    assert sum(1 for c in issued if " get secret " in f" {c} ") == 3
    assert (
        "get events -A --field-selector involvedObject.kind=ExternalSecret,type=Normal "
        "--sort-by=.metadata.creationTimestamp -o wide" in issued
    )
    assert ESO_PODS_READ in issued
    bounds = f"--tail={fe.LOG_TAIL} --limit-bytes={fe.TEXT_BYTES} --timestamps"
    for pod in ("external-secrets-a1", "external-secrets-b2"):
        assert f"-n external-secrets logs {pod} -c external-secrets {bounds}" in issued
        assert (out / "external-secrets" / f"{pod}.external-secrets.log").is_file()
    previous = [c for c in issued if "--previous" in c and "external-secrets" in c]
    restarted = f"-n external-secrets logs external-secrets-a1 -c external-secrets {bounds}"
    assert previous == [f"{restarted} --previous"]
    assert (out / "external-secrets" / "normal-events.txt").is_file()
    status = _read(out, "external-secrets/status.txt")
    assert status.splitlines() == fe.external_secret_lines(EXTERNAL_SECRET_ITEMS)
    index = _read(out, fe.INDEX)
    assert (
        f"generated-secrets/kafka.dfe-kafka-user.txt: ok\n    kubectl -n kafka get secret "
        f"dfe-kafka-user -o {SECRET_COLUMNS}\n" in index
    )
    assert f"external-secrets/status.txt: ok\n    kubectl {EXTERNAL_SECRETS_READ}\n" in index


def test_the_externalsecret_events_keep_their_newest_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tails: dict[str, bool] = {}

    def fake(argv: list[str], *, timeout: float, limit: int, tail: bool = False) -> fe.Outcome:
        tails[" ".join(argv[1:])] = tail
        return _answer(argv[1:])

    monkeypatch.setattr(fe, "run_bounded", fake)
    out = tmp_path / "evidence"
    out.mkdir()
    fe.collect(fe.Evidence(out, [], 60.0, time.monotonic() + 60.0))
    events = [call for call in tails if "involvedObject.kind=ExternalSecret" in call]
    assert len(events) == 1
    assert tails[events[0]] is True


def test_what_an_external_secret_template_and_annotations_hold_reaches_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _calls, out = _run(monkeypatch, tmp_path)
    written = sorted(path for path in out.rglob("*") if path.is_file())
    assert out / "external-secrets" / "status.txt" in written
    for path in written:
        assert SENTINEL not in path.read_text(encoding="utf-8", errors="replace"), path


def test_a_credential_in_an_external_secret_condition_is_masked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    leaky = _external_secret("dfe", "x", message=f"provider said password={FAKE} and stopped")

    def answer(args: list[str]) -> fe.Outcome:
        return _items([leaky]) if " ".join(args) == EXTERNAL_SECRETS_READ else _answer(args)

    _calls, out = _run(monkeypatch, tmp_path, answer=answer)
    status = _read(out, "external-secrets/status.txt")
    assert FAKE not in status
    assert "provider said password=***REDACTED*** and stopped" in status


def test_an_unreadable_external_secret_list_is_named_and_the_rest_still_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, out = _run(monkeypatch, tmp_path, fail=("get externalsecrets",))
    issued = [" ".join(c) for c in calls]
    assert not any(" get secret " in f" {c} " for c in issued)
    assert ESO_PODS_READ in issued
    assert any("involvedObject.kind=ExternalSecret" in c for c in issued)
    assert (out / "external-secrets" / "external-secrets-a1.external-secrets.log").is_file()
    index = _read(out, fe.INDEX)
    assert "external-secrets/status.txt: exit 1: Error from server (InternalError): boom" in index
    assert "generated-secrets/: not run: the ExternalSecrets were not read" in index
    assert not (out / "external-secrets" / "status.txt").exists()


def test_no_generator_backed_external_secret_is_a_named_skip_and_reads_no_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def answer(args: list[str]) -> fe.Outcome:
        copies = [EXTERNAL_SECRET_ITEMS[1]]
        return _items(copies) if " ".join(args) == EXTERNAL_SECRETS_READ else _answer(args)

    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert not any("secret" in call for call in calls)
    index = _read(out, fe.INDEX)
    assert "generated-secrets/: not run: no ExternalSecret takes its values from a generator" in index
    assert "externalsecret dfe/dfe-kafka-user " in _read(out, "external-secrets/status.txt")


def test_no_externalsecret_at_all_writes_no_status_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def answer(args: list[str]) -> fe.Outcome:
        return _items([]) if " ".join(args) == EXTERNAL_SECRETS_READ else _answer(args)

    _calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert not (out / "external-secrets" / "status.txt").exists()
    index = _read(out, fe.INDEX)
    assert "external-secrets/status.txt: no ExternalSecret exists, nothing written" in index
    assert "generated-secrets/: not run: no ExternalSecret takes its values from a generator" in index


def test_a_secret_that_cannot_be_read_is_recorded_and_the_others_still_are(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, out = _run(monkeypatch, tmp_path, fail=("get secret dfe-engine-jwt",))
    index = _read(out, fe.INDEX)
    assert (
        "generated-secrets/dfe.dfe-engine-jwt.txt: exit 1: Error from server (InternalError): boom"
        in index
    )
    assert not (out / "generated-secrets" / "dfe.dfe-engine-jwt.txt").exists()
    assert (out / "generated-secrets" / "kafka.dfe-kafka-user.txt").is_file()
    assert (out / "generated-secrets" / "clickhouse.clickhouse-admin-password.txt").is_file()
    assert sum(1 for c in calls if "secret" in c) == 3


def test_generated_secrets_past_the_cap_are_named_and_not_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    many = [
        _external_secret("dfe", f"gen-{i:03d}", generator=f"gen-{i:03d}")
        for i in range(fe.MAX_GENERATED + 12)
    ]

    def answer(args: list[str]) -> fe.Outcome:
        return _items(many) if " ".join(args) == EXTERNAL_SECRETS_READ else _answer(args)

    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert sum(1 for c in calls if "secret" in c) == fe.MAX_GENERATED
    index = _read(out, fe.INDEX)
    first_left = f"dfe/gen-{fe.MAX_GENERATED:03d}"
    assert f"generated-secrets/: +12 more, not read: {first_left}, " in index
    assert ", and 2 more\n" in index


def test_a_cluster_without_the_controller_says_so_and_reads_no_eso_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def answer(args: list[str]) -> fe.Outcome:
        return _items([]) if " ".join(args) == ESO_PODS_READ else _answer(args)

    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert not any("external-secrets-" in " ".join(c) for c in calls if "logs" in c)
    assert not any(path.name.startswith("external-secrets-") for path in out.rglob("*"))
    index = _read(out, fe.INDEX)
    assert f"external-secrets/: not run: no pod carries {ESO_SELECTOR}" in index


def test_an_unreadable_controller_pod_list_is_named_and_reads_no_eso_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, out = _run(monkeypatch, tmp_path, fail=(ESO_SELECTOR,))
    assert not any("logs external-secrets-" in " ".join(c) for c in calls)
    index = _read(out, fe.INDEX)
    assert "external-secrets/: not run: the external-secrets controller pods were not read" in index
    assert (out / "external-secrets" / "status.txt").is_file()


# --- the bounded runner, against real child processes --------------------------------


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_a_head_capture_keeps_its_limit_and_stops_a_command_that_would_print_forever() -> None:
    started = time.monotonic()
    outcome = fe.run_bounded(
        _python("import sys\nwhile True: sys.stdout.write('x' * 4096)"), timeout=20, limit=10_000
    )
    assert (outcome.ok, outcome.stopped, outcome.truncated) == (True, True, True)
    assert outcome.output == b"x" * 10_000
    assert time.monotonic() - started < 15


def test_a_tail_capture_keeps_the_newest_bytes() -> None:
    code = "import sys\nfor i in range(2000): sys.stdout.write(f'line {i:04d}\\n')"
    outcome = fe.run_bounded(_python(code), timeout=20, limit=100, tail=True)
    assert (outcome.ok, outcome.truncated) == (True, True)
    assert len(outcome.output) == 100
    assert outcome.output.endswith(b"line 1999\n")


def test_a_command_that_hangs_is_stopped_at_its_timeout() -> None:
    started = time.monotonic()
    hangs = _python("import time\nprint('started', flush=True)\ntime.sleep(60)")
    outcome = fe.run_bounded(hangs, timeout=1, limit=1000)
    assert (outcome.timed_out, outcome.ok) == (True, False)
    assert outcome.output == b"started\n"
    assert time.monotonic() - started < 10
    assert fe.verdict(outcome, 300) == "timed out, kept the newest 8 bytes"


def test_a_failing_command_reports_its_exit_and_last_stderr_line() -> None:
    fails = _python("import sys\nsys.stderr.write('a\\nlast line\\n')\nsys.exit(3)")
    outcome = fe.run_bounded(fails, timeout=20, limit=1000)
    assert (outcome.ok, outcome.returncode, outcome.error) == (False, 3, "last line")
    assert fe.verdict(outcome, 300) == "exit 3: last line"


def test_a_missing_binary_is_named_rather_than_raised(tmp_path: Path) -> None:
    outcome = fe.run_bounded([str(tmp_path / "no-such-kubectl")], timeout=5, limit=10)
    assert (outcome.returncode, outcome.ok) == (None, False)
    missing = tmp_path / "no-such-kubectl"
    assert fe.verdict(outcome, 300).startswith(f"could not start {missing}")


# --- the CLI against a fake kubectl ---------------------------------------------------

FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\\n")
fixture = json.load(open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8"))
joined = " ".join(args)
if joined in fixture.get("json", {}):
    print(json.dumps({"items": fixture["json"][joined]}))
    sys.exit(0)
if any(word in joined for word in fixture.get("broken", [])):
    print("Error from server (Forbidden): forbidden", file=sys.stderr)
    sys.exit(1)
if "logs" in args and "log_text" in fixture:
    sys.stdout.write(fixture["log_text"])
    sys.exit(0)
print("captured: " + joined)
"""


def _run_cli(
    tmp_path: Path, fixture: dict, *, kubectl: bool = True
) -> tuple[subprocess.CompletedProcess, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if kubectl:
        fake = bindir / "kubectl"
        fake.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
        fake.chmod(0o755)
    fixture_file = tmp_path / "fixture.json"
    fixture_file.write_text(json.dumps(fixture), encoding="utf-8", newline="\n")
    out = tmp_path / "out"
    env = {
        "PATH": f"{bindir}{os.pathsep}{Path(sys.executable).parent}" if kubectl else str(bindir),
        "FAKE_KUBECTL_FIXTURE": str(fixture_file),
        "FAKE_KUBECTL_LOG": str(tmp_path / "calls.log"),
    }
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "failure-evidence", "--out", str(out), "--kubeconfig", "/k/config"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    return done, out


def test_the_cli_writes_its_captures_and_exits_zero_with_one_read_forbidden(tmp_path: Path) -> None:
    prefix = "--kubeconfig /k/config"
    fixture = {
        "json": {
            f"{prefix} get nodes -o json": [_node("node-a")],
            f"{prefix} -n kube-system get pods -o json": [
                _with_private_fields(pod) for pod in PODS[:3]
            ],
        },
        "broken": ["get events -A --field-selector reason="],
    }
    done, out = _run_cli(tmp_path, fixture)
    assert done.returncode == 0, done.stderr
    assert "file(s) under" in done.stdout
    wide = (out / "nodes-wide.txt").read_text(encoding="utf-8")
    assert wide.startswith(f"captured: {prefix} get nodes -o wide")
    assert (out / "aws-node" / "aws-node-a1.aws-vpc-cni-init.log").is_file()
    index = (out / fe.INDEX).read_text(encoding="utf-8")
    assert "(the InvalidDiskCapacity node events): exit 1: Error from server (Forbidden): forbidden" in index
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert all(call.startswith(prefix) for call in calls)
    assert not any("describe" in call for call in calls)
    assert f"{prefix} {_pod_events('kube-system', 'coredns-1')}" in calls
    unready = (out / "kube-system-not-ready.txt").read_text(encoding="utf-8")
    assert "pod kube-system/coredns-1\n" in unready
    assert f"captured: {prefix} {_pod_events('kube-system', 'coredns-1')}" in unready
    for path in out.rglob("*"):
        if path.is_file():
            assert SENTINEL not in path.read_text(encoding="utf-8", errors="replace"), path


def test_the_cli_with_no_kubectl_records_why_and_exits_zero(tmp_path: Path) -> None:
    done, out = _run_cli(tmp_path, {}, kubectl=False)
    assert done.returncode == 0, done.stderr
    index = (out / fe.INDEX).read_text(encoding="utf-8")
    assert "nodes-wide.txt: could not start kubectl" in index
    assert "not run: the API server answered none of the reads above" in index


# --- the cycle runs it when a stage fails, before the destroy -------------------------


def _cycle(
    monkeypatch: pytest.MonkeyPatch, *argv: str, fail: str = "", evidence_rc: int = 0
) -> tuple[int, list[list[str]]]:
    ran: list[list[str]] = []

    def run(cmd: list[str], env: dict | None = None) -> int:
        ran.append(cmd)
        if cmd[2] == "failure-evidence":
            return evidence_rc
        return 1 if cmd[2] == fail else 0

    monkeypatch.setattr(dfeops, "_resolve_stack", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_run_streaming", run)
    args = dfeops.build_parser().parse_args(
        ["cycle", "--skip-capacity-check", "--skip-preflight", "--stack", "2.2.0-rc.99", *argv]
    )
    return dfeops.cmd_cycle(args), ran


def test_a_failed_deploy_writes_evidence_under_the_shots_dir_before_the_destroy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("", encoding="utf-8")
    shots = tmp_path / "shots"
    rc, ran = _cycle(
        monkeypatch, "--kubeconfig", str(kubeconfig), "--acceptance-shots-dir", str(shots),
        fail="stack-deploy",
    )
    assert rc == 1
    assert [cmd[2] for cmd in ran] == ["stack-deploy", "failure-evidence", "teardown"]
    evidence = ran[1]
    out = Path(evidence[evidence.index("--out") + 1])
    assert out.parent == shots / dfeops.EVIDENCE_SUBDIR
    assert out.name.endswith("-deploy_e2e")
    assert evidence[evidence.index("--kubeconfig") + 1] == str(kubeconfig)
    # The subcommand's own parser has to accept every flag the cycle hands it.
    parsed = dfeops.build_parser().parse_args(evidence[2:])
    assert (parsed.out, parsed.kubeconfig) == (str(out), str(kubeconfig))


def test_evidence_dir_names_where_the_evidence_goes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _rc, ran = _cycle(monkeypatch, "--evidence-dir", str(tmp_path / "ev"), fail="verify")
    evidence = next(cmd for cmd in ran if cmd[2] == "failure-evidence")
    assert Path(evidence[evidence.index("--out") + 1]).parent == tmp_path / "ev"
    assert [cmd[2] for cmd in ran] == ["stack-deploy", "verify", "failure-evidence", "teardown"]


def test_a_passing_cycle_writes_no_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    rc, ran = _cycle(monkeypatch)
    assert rc == 0
    assert "failure-evidence" not in [cmd[2] for cmd in ran]


def test_evidence_that_fails_changes_neither_the_verdict_nor_the_destroy(monkeypatch: pytest.MonkeyPatch) -> None:
    rc, ran = _cycle(monkeypatch, fail="stack-deploy", evidence_rc=2)
    assert rc == 1
    assert [cmd[2] for cmd in ran] == ["stack-deploy", "failure-evidence", "teardown"]


def test_the_evidence_dir_defaults_under_the_shots_dir() -> None:
    args = dfeops.build_parser().parse_args(["cycle", "--acceptance-shots-dir", "/x/acceptance"])
    assert dfeops._evidence_root(args) == Path("/x/acceptance") / "failure-evidence"


def test_failure_evidence_needs_an_out_dir(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as refused:
        dfeops.build_parser().parse_args(["failure-evidence"])
    assert refused.value.code == 2
    assert "--out" in capsys.readouterr().err


def test_an_unwritable_out_dir_is_a_named_skip(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    args = argparse.Namespace(out=str(blocker / "sub"), kubeconfig="", context="", budget=1.0)
    assert fe.cmd_failure_evidence(args) == 0
    assert "cannot be written" in capsys.readouterr().err
