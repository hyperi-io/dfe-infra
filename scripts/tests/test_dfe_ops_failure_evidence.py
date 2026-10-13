#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_failure_evidence.py
#  Purpose:      Guard `dfe-ops failure-evidence` and the cycle hook that runs it:
#                the kubectl calls it makes, that one failing capture never stops
#                the rest, that every capture is bounded, that no call reads a
#                Secret, and that a failed stage writes evidence before destroy.
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


PODS = [
    _aws_node_pod("aws-node-b2", "node-b"),
    _aws_node_pod("aws-node-a1", "node-a", restarts=2),
    _plain_pod("coredns-1", ready=False),
    _plain_pod("ebs-csi-node-1", ready=False, phase="Pending"),
    _plain_pod("kube-proxy-1", ready=True),
    _plain_pod("migrate-1", ready=False, phase="Succeeded"),
]
DISK_EVENTS = [
    {"reason": "InvalidDiskCapacity", "involvedObject": {"kind": "Node", "name": "node-a"}},
    {"reason": "InvalidDiskCapacity", "involvedObject": {"kind": "Node", "name": "node-a"}},
    {"reason": "BackOff", "involvedObject": {"kind": "Pod", "name": "coredns-1"}},
]


def _waiting_pod(namespace: str, name: str, *, reason: str = "", init_reason: str = "") -> dict:
    """A pod whose init container and main container wait on the reasons given, or run."""

    def status(container: str, waiting: str) -> dict:
        state = (
            {"waiting": {"reason": waiting, "message": f"{waiting} detail"}}
            if waiting
            else {"running": {}}
        )
        return {"name": container, "ready": False, "restartCount": 0, "state": state}

    return {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"initContainers": [{"name": "copyutil"}], "containers": [{"name": "main"}]},
        "status": {
            "phase": "Pending" if reason or init_reason else "Running",
            "initContainerStatuses": [status("copyutil", init_reason)],
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
]


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


def test_stuck_pods_are_found_by_an_init_or_main_container_waiting_reason() -> None:
    assert fe.stuck_pods(ALL_PODS) == [
        ("argocd", "argocd-dex-server-1"),
        ("argocd", "argocd-server-1"),
        ("dfe", "dfe-engine-1"),
        ("dfe", "dfe-loader-1"),
        ("monitoring", "collector-1"),
    ]


def test_a_name_becomes_one_safe_path_segment() -> None:
    assert fe.safe_name("deploy+e2e") == "deploy_e2e"
    assert fe.safe_name("../../etc") == ".._.._etc"
    assert fe.safe_name("") == "_"


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
    assert "-n argocd describe pods argocd-dex-server-1 argocd-server-1" in issued
    assert "-n dfe describe pods dfe-engine-1 dfe-loader-1" in issued
    assert "-n monitoring describe pods collector-1" in issued
    assert "-n kube-system get events --sort-by=.metadata.creationTimestamp -o wide" in issued
    assert "-n kube-system describe pods coredns-1 ebs-csi-node-1" in issued
    assert (
        "get events -A --field-selector involvedObject.kind=Node,involvedObject.name=node-a "
        "--sort-by=.metadata.creationTimestamp" in issued
    )
    bounds = f"--tail={fe.LOG_TAIL} --limit-bytes={fe.TEXT_BYTES} --timestamps"
    for pod in ("aws-node-a1", "aws-node-b2"):
        for container in ("aws-vpc-cni-init", "aws-node", "aws-eks-nodeagent"):
            assert f"-n kube-system logs {pod} -c {container} {bounds}" in issued
    for node in ("node-a", "node-b"):
        for log in ("ipamd.log", "plugin.log"):
            assert f"get --raw /api/v1/nodes/{node}/proxy/logs/aws-routed-eni/{log}" in issued


def test_only_a_restarted_container_has_its_previous_run_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, _out = _run(monkeypatch, tmp_path)
    previous = [" ".join(c) for c in calls if "--previous" in c]
    assert len(previous) == 1
    assert previous[0].startswith("-n kube-system logs aws-node-a1 -c aws-node ")


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
    ):
        assert (out / name).is_file(), name
    assert "condition Ready=False" in (out / "node-conditions.txt").read_text(encoding="utf-8")
    index = (out / fe.INDEX).read_text(encoding="utf-8")
    assert "nodes-wide.txt: ok\n    kubectl get nodes -o wide\n" in index
    assert "stuck-pods/argocd.txt: ok\n    kubectl -n argocd describe pods " in index


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
    assert tails["-n argocd describe pods argocd-dex-server-1 argocd-server-1"] is False


def test_stuck_pods_past_the_cap_are_described_up_to_it_and_the_index_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    many = [
        _waiting_pod(f"ns-{i % 3}", f"pod-{i:03d}", reason="ImagePullBackOff")
        for i in range(fe.MAX_STUCK + 5)
    ]

    def answer(args: list[str]) -> fe.Outcome:
        return _items(many) if " ".join(args) == "get pods -A -o json" else _answer(args)

    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    described = [c for c in calls if "describe" in c and c[1] != "kube-system"]
    named = [name for c in described for name in c[c.index("pods") + 1 :]]
    assert len(named) == fe.MAX_STUCK
    assert [c[1] for c in described] == ["ns-0", "ns-1", "ns-2"]
    index = (out / fe.INDEX).read_text(encoding="utf-8")
    assert f"{fe.MAX_STUCK + 5} pods are stuck, and the first {fe.MAX_STUCK} are described" in index


def test_no_stuck_pod_is_a_named_skip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    healthy = [
        _waiting_pod("dfe", "dfe-receiver-1"),
        _waiting_pod("dfe", "dfe-ui-1", reason="ContainerCreating"),
    ]

    def answer(args: list[str]) -> fe.Outcome:
        return _items(healthy) if " ".join(args) == "get pods -A -o json" else _answer(args)

    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert not any("describe" in c and c[1] != "kube-system" for c in calls)
    index = (out / fe.INDEX).read_text(encoding="utf-8")
    assert "stuck-pods/: not run: no container is waiting on CrashLoopBackOff, " in index


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
    assert "-n kube-system describe pods coredns-1 ebs-csi-node-1" in issued
    assert "-n argocd describe pods argocd-dex-server-1 argocd-server-1" in issued
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


def test_an_unreadable_cluster_wide_pod_list_still_captures_kube_system(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, out = _run(monkeypatch, tmp_path, fail=("get pods -A",))
    issued = [" ".join(c) for c in calls]
    assert "-n kube-system describe pods coredns-1 ebs-csi-node-1" in issued
    assert not any(c.startswith("-n argocd ") for c in issued)
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


def test_no_call_reads_a_secret_or_a_pods_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls, _out = _run(monkeypatch, tmp_path, fail=("proxy/logs",))
    for call in calls:
        words = [w.lower() for w in call]
        assert not any("secret" in w for w in words), call
        assert "yaml" not in words, call
        if "exec" in words:
            command = words[words.index("--") + 1 :]
            assert command[0] == "cat", call
            assert command[1].startswith(fe.CNI_LOG_MOUNT + "/"), call


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
            f"{prefix} -n kube-system get pods -o json": PODS[:3],
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
