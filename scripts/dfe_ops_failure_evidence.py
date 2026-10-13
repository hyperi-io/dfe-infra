#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/dfe_ops_failure_evidence.py
#  Purpose:      `dfe-ops failure-evidence` -- the node and pod-network state a
#                failed cycle stage leaves, written to files before the destroy
#                removes the cluster. Registered into dfe-ops the way
#                dfe_ops_readiness_report.py is.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-ops failure-evidence -- the nodes and the pod network as they stood when a stage failed.

    dfe-ops failure-evidence --out DIR [--kubeconfig F] [--context C] [--budget SECONDS]

`dfe-ops cycle` runs it once when a stage fails, before the destroy, so the
evidence outlives the cluster. Each capture is one file under DIR:

  nodes-wide.txt             kubectl get nodes -o wide
  node-conditions.txt        each node's runtime, conditions, capacity and allocatable
  kube-system-events.txt     kube-system events, oldest first
  kube-system-not-ready.txt  kubectl describe of each kube-system pod not Running and Ready
  node-events/<node>.txt     the events of each node whose kubelet reported InvalidDiskCapacity
  aws-node/<pod>.<container>.log
                             each aws-node container's newest lines, the init container
                             included, plus the previous run of any that restarted
  aws-node/<node>.<file>     the CNI's own ipamd.log and plugin.log, newest bytes, through
                             the kubelet's /logs endpoint, else from the aws-node container
  index.txt                  every capture, the command behind it and how it ended

Every capture is bounded in time and bytes, and the run as a whole in time. One
that fails is recorded in index.txt and the rest still run. It reads and never
writes to the cluster, it never reads a Secret, and it exits 0 whatever it finds.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from dfe_ops_readiness_report import kubectl_prefix, pod_ready

CAPTURE_TIMEOUT = 30.0  # seconds one kubectl call may take
BUDGET = 300.0  # seconds for every capture together, so the destroy behind it is not held up
TEXT_BYTES = 1024 * 1024  # bytes one capture file keeps
JSON_BYTES = 16 * 1024 * 1024  # bytes a read that only steers later captures may hold
STDERR_BYTES = 8192
LOG_TAIL = 2000  # lines read from each aws-node container
MAX_AWS_NODE_PODS = 20
MAX_DISK_NODES = 20
MAX_DESCRIBED = 40
CHUNK = 65536
POLL = 0.1
JOIN_TIMEOUT = 5.0

AWS_NODE_LABEL = ("k8s-app", "aws-node")
CNI_CONTAINER = "aws-node"
# The aws-node container mounts the node's /var/log/aws-routed-eni here.
CNI_LOG_MOUNT = "/host/var/log/aws-routed-eni"
CNI_LOGS = ("ipamd.log", "plugin.log")
DISK_REASON = "InvalidDiskCapacity"
# creationTimestamp is set on every event, where lastTimestamp can be empty.
OLDEST_FIRST = "--sort-by=.metadata.creationTimestamp"
INDEX = "index.txt"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_name(text: str) -> str:
    """A path segment from a pod, node or stage name."""
    return _UNSAFE.sub("_", text) or "_"


# --- one bounded command -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    """How one bounded command ended, and the output it kept."""

    output: bytes = b""
    returncode: int | None = None
    error: str = ""
    truncated: bool = False
    timed_out: bool = False
    # Stopped once it had printed all the output a head capture keeps.
    stopped: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out and (self.returncode == 0 or self.stopped)


class _Drain:
    """Reads one pipe to its end on a thread of its own, keeping at most `limit` bytes."""

    def __init__(self, stream: IO[bytes], limit: int, *, tail: bool) -> None:
        self.kept = bytearray()
        self.seen = 0
        self.full = threading.Event()
        self._limit = limit
        self._tail = tail
        self.thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self.thread.start()

    def _read(self, stream: IO[bytes]) -> None:
        with stream:
            while chunk := os.read(stream.fileno(), CHUNK):
                self.seen += len(chunk)
                self.kept += chunk
                if len(self.kept) > self._limit:
                    if self._tail:
                        del self.kept[: len(self.kept) - self._limit]
                    else:
                        del self.kept[self._limit :]
                    self.full.set()


def run_bounded(argv: list[str], *, timeout: float, limit: int, tail: bool = False) -> Outcome:
    """Run `argv`, keep at most `limit` bytes of its output, and stop it at `timeout` seconds.

    Args:
        argv: The command.
        timeout: Seconds before it is stopped.
        limit: Bytes of standard output kept.
        tail: Keep the newest bytes and read to the end; otherwise keep the
            first and stop the command once it has printed them.

    Returns:
        How it ended. A command that could not start carries the reason in `error`.
    """
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as exc:
        return Outcome(error=f"could not start {argv[0]}: {exc.strerror or exc}")
    stdout, stderr = proc.stdout, proc.stderr
    if stdout is None or stderr is None:
        proc.kill()
        proc.wait()
        return Outcome(error=f"{argv[0]} started with no output pipe")
    out = _Drain(stdout, limit, tail=tail)
    err = _Drain(stderr, STDERR_BYTES, tail=True)
    deadline = time.monotonic() + timeout
    timed_out = stopped = False
    while True:
        try:
            proc.wait(timeout=POLL)
            break
        except subprocess.TimeoutExpired:
            pass
        if out.full.is_set() and not tail:
            stopped = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    out.thread.join(JOIN_TIMEOUT)
    err.thread.join(JOIN_TIMEOUT)
    lines = err.kept.decode("utf-8", errors="replace").strip().splitlines()
    return Outcome(
        output=bytes(out.kept),
        returncode=proc.returncode,
        error=lines[-1] if lines else "",
        truncated=out.seen > len(out.kept),
        timed_out=timed_out,
        stopped=stopped,
    )


def verdict(outcome: Outcome | None, budget: float) -> str:
    """One line on how a capture ended, for the index."""
    if outcome is None:
        return f"not run: the {budget:.0f}s budget was spent"
    if outcome.returncode is None:
        return outcome.error
    if outcome.timed_out:
        kept = f", kept the newest {len(outcome.output)} bytes" if outcome.output else ""
        return f"timed out{kept}"
    if outcome.ok:
        return f"ok, truncated to {len(outcome.output)} bytes" if outcome.truncated else "ok"
    return f"exit {outcome.returncode}: {outcome.error or 'no message'}"


# --- what to capture, read off the cluster's own answers ----------------------------


def node_condition_lines(nodes: list[dict]) -> list[str]:
    """Each node's runtime, conditions, capacity, allocatable and taints, as text."""
    lines: list[str] = []
    for node in nodes:
        meta = node.get("metadata") or {}
        status = node.get("status") or {}
        info = status.get("nodeInfo") or {}
        lines.append(f"node {meta.get('name')} created {meta.get('creationTimestamp')}")
        lines.append(
            f"  kubelet {info.get('kubeletVersion')} os {info.get('osImage')} "
            f"kernel {info.get('kernelVersion')} runtime {info.get('containerRuntimeVersion')}"
        )
        for cond in status.get("conditions") or []:
            why = ": ".join(str(p) for p in (cond.get("reason"), cond.get("message")) if p)
            lines.append(
                f"  condition {cond.get('type')}={cond.get('status')} {why} "
                f"(since {cond.get('lastTransitionTime')})"
            )
        for kind in ("capacity", "allocatable"):
            values = status.get(kind) or {}
            lines.append(f"  {kind} " + " ".join(f"{k}={values[k]}" for k in sorted(values)))
        taints = (node.get("spec") or {}).get("taints") or []
        shown = ",".join(f"{t.get('key')}={t.get('value', '')}:{t.get('effect')}" for t in taints)
        lines.append(f"  taints {shown or '-'}")
    return lines


def not_ready_pods(pods: list[dict]) -> list[str]:
    """The name of every pod not Running and Ready, a finished one aside."""
    return sorted(
        (pod.get("metadata") or {}).get("name") or ""
        for pod in pods
        if (pod.get("status") or {}).get("phase") != "Succeeded" and not pod_ready(pod)
    )


def aws_node_pods(pods: list[dict]) -> list[dict]:
    """The aws-node DaemonSet's pods, ordered by the node each runs on."""
    key, value = AWS_NODE_LABEL
    found = [p for p in pods if ((p.get("metadata") or {}).get("labels") or {}).get(key) == value]
    return sorted(found, key=lambda p: (p.get("spec") or {}).get("nodeName") or "")


def containers_of(pod: dict) -> list[tuple[str, int]]:
    """Every init container and container in a pod, in spec order, with its restart count."""
    spec, status = pod.get("spec") or {}, pod.get("status") or {}
    restarts: dict[str, int] = {}
    for kind in ("initContainerStatuses", "containerStatuses"):
        for container in status.get(kind) or []:
            restarts[container.get("name")] = int(container.get("restartCount") or 0)
    declared = [*(spec.get("initContainers") or []), *(spec.get("containers") or [])]
    names = [c.get("name") for c in declared if c.get("name")]
    return [(name, restarts.get(name, 0)) for name in names]


def disk_capacity_nodes(events: list[dict]) -> list[str]:
    """Every node an InvalidDiskCapacity event names."""
    names = set()
    for event in events:
        involved = event.get("involvedObject") or event.get("regarding") or {}
        if (
            event.get("reason") == DISK_REASON
            and involved.get("kind") == "Node"
            and involved.get("name")
        ):
            names.add(involved["name"])
    return sorted(names)


# --- the run ------------------------------------------------------------------------


@dataclass(slots=True)
class Evidence:
    """Every capture of one run, written under `out` and listed in its index as it ends."""

    out: Path
    prefix: list[str]
    budget: float
    deadline: float
    runs: int = 0
    answered: int = 0
    files: set[str] = field(default_factory=set)

    def _kubectl(self, args: list[str], *, limit: int, tail: bool) -> Outcome | None:
        left = self.deadline - time.monotonic()
        if left <= 0:
            return None
        argv = ["kubectl", *self.prefix, *args]
        return run_bounded(argv, timeout=min(CAPTURE_TIMEOUT, left), limit=limit, tail=tail)

    def note(self, name: str, how: str, args: Iterable[str] = ()) -> None:
        """One index entry: where a capture went, how it ended and the command behind it."""
        command = " ".join(args)
        entry = f"{name}: {how}\n" + (f"    kubectl {command}\n" if command else "")
        with (self.out / INDEX).open("a", encoding="utf-8", newline="\n") as index:
            index.write(entry)

    def write(self, name: str, data: bytes) -> None:
        path = self.out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.files.add(name)

    def capture(self, name: str, args: list[str], *, tail: bool = False) -> Outcome | None:
        """`kubectl <args>` into the file `name`; what it kept is written even when it failed."""
        outcome = self._kubectl(args, limit=TEXT_BYTES, tail=tail)
        self.runs += 1
        if outcome and outcome.output:
            self.write(name, outcome.output)
        if outcome and outcome.ok:
            self.answered += 1
        self.note(name, verdict(outcome, self.budget), args)
        return outcome

    def read(self, name: str, args: list[str]) -> list[dict] | None:
        """`kubectl <args> -o json` as its items, or None when it did not answer with them."""
        full = [*args, "-o", "json"]
        outcome = self._kubectl(full, limit=JSON_BYTES, tail=False)
        self.runs += 1
        how = verdict(outcome, self.budget)
        items = None
        if outcome and outcome.truncated:
            how = f"answered with more than {JSON_BYTES} bytes, so it was not parsed"
        elif outcome and outcome.ok:
            try:
                body = json.loads(outcome.output)
                items = [i for i in body.get("items") or [] if isinstance(i, dict)]
            except (json.JSONDecodeError, AttributeError):
                how = "answered with no JSON item list"
        if items is not None:
            self.answered += 1
        self.note(name, how, full)
        return items


def _capture_aws_node(evidence: Evidence, targets: list[dict]) -> None:
    """Each aws-node container's newest lines, then each node's CNI log files."""
    bounds = [f"--tail={LOG_TAIL}", f"--limit-bytes={TEXT_BYTES}", "--timestamps"]
    for pod in targets:
        name = (pod.get("metadata") or {}).get("name") or ""
        for container, restarts in containers_of(pod):
            stem = f"aws-node/{safe_name(name)}.{safe_name(container)}"
            logs = ["-n", "kube-system", "logs", name, "-c", container, *bounds]
            evidence.capture(f"{stem}.log", logs)
            if restarts:
                evidence.capture(f"{stem}.previous.log", [*logs, "--previous"])
    for pod in targets:
        name = (pod.get("metadata") or {}).get("name") or ""
        node = (pod.get("spec") or {}).get("nodeName") or ""
        if not node:
            continue
        for log in CNI_LOGS:
            target = f"aws-node/{safe_name(node)}.{log}"
            kubelet = f"/api/v1/nodes/{node}/proxy/logs/aws-routed-eni/{log}"
            served = evidence.capture(target, ["get", "--raw", kubelet], tail=True)
            if served is None or served.ok:
                continue
            inside = ["exec", name, "-c", CNI_CONTAINER, "--", "cat", f"{CNI_LOG_MOUNT}/{log}"]
            evidence.capture(target, ["-n", "kube-system", *inside], tail=True)


def collect(evidence: Evidence) -> None:
    """Every capture, cluster-wide ones first, so a spent budget costs only the bulkiest."""
    wide = evidence.capture("nodes-wide.txt", ["get", "nodes", "-o", "wide"])
    nodes = evidence.read("node-conditions.txt", ["get", "nodes"])
    if nodes is not None:
        text = "\n".join(node_condition_lines(nodes)) + "\n"
        evidence.write("node-conditions.txt", text.encode("utf-8"))
    pods = evidence.read(
        "(the kube-system pods the captures below are chosen from)",
        ["-n", "kube-system", "get", "pods"],
    )
    if nodes is None and pods is None and not (wide and wide.ok):
        evidence.note("(the rest)", "not run: the API server answered none of the reads above")
        return

    evidence.capture(
        "kube-system-events.txt",
        ["-n", "kube-system", "get", "events", OLDEST_FIRST, "-o", "wide"],
    )

    unready_file = "kube-system-not-ready.txt"
    if pods is None:
        evidence.note(unready_file, "not run: the kube-system pods were not read")
    elif unready := not_ready_pods(pods):
        evidence.capture(
            unready_file, ["-n", "kube-system", "describe", "pods", *unready[:MAX_DESCRIBED]]
        )
    else:
        evidence.note(unready_file, "not run: every kube-system pod is Running and Ready")

    disk_events = f"reason={DISK_REASON},involvedObject.kind=Node"
    events = evidence.read(
        "(the InvalidDiskCapacity node events)",
        ["get", "events", "-A", "--field-selector", disk_events],
    )
    for node in disk_capacity_nodes(events or [])[:MAX_DISK_NODES]:
        on_node = f"involvedObject.kind=Node,involvedObject.name={node}"
        evidence.capture(
            f"node-events/{safe_name(node)}.txt",
            ["get", "events", "-A", "--field-selector", on_node, OLDEST_FIRST],
        )

    if pods is None:
        evidence.note("aws-node/", "not run: the kube-system pods were not read")
        return
    targets = aws_node_pods(pods)[:MAX_AWS_NODE_PODS]
    if not targets:
        evidence.note("aws-node/", "not run: no kube-system pod carries k8s-app=aws-node")
        return
    _capture_aws_node(evidence, targets)


def cmd_failure_evidence(args: argparse.Namespace) -> int:
    """`dfe-ops failure-evidence`: write every capture under --out; exit 0 whatever it finds."""
    out = Path(args.out)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        out.mkdir(parents=True, exist_ok=True)
        header = f"failure evidence, read {stamp}\n"
        (out / INDEX).write_text(header, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"  failure evidence skipped: {out} cannot be written: {exc}", file=sys.stderr)
        return 0
    evidence = Evidence(out, kubectl_prefix(args), args.budget, time.monotonic() + args.budget)
    collect(evidence)
    print(
        f"failure evidence: {evidence.answered} of {evidence.runs} kubectl calls answered, "
        f"{len(evidence.files)} file(s) under {out}; {INDEX} says how each ended"
    )
    return 0


def add_failure_evidence_subparser(sub: argparse._SubParsersAction) -> None:
    """Register `dfe-ops failure-evidence`."""
    fe = sub.add_parser(
        "failure-evidence",
        help="READ-ONLY: write node conditions, kube-system events and unready pods, and the "
        "aws-node and CNI logs to files, for after a cycle stage fails and before the destroy",
        description="Reads the cluster and writes one file per capture under --out: nodes -o "
        "wide, each node's conditions and allocatable, kube-system events oldest first, describe "
        "of each kube-system pod not Running and Ready, the events of each node reporting "
        "InvalidDiskCapacity, every aws-node container's newest log lines, and the CNI's ipamd "
        "and plugin logs. Every capture is bounded in time and bytes, one that fails is recorded "
        "in index.txt and the rest still run, and nothing reads a Secret. Writes nothing to the "
        "cluster; exits 0.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    fe.add_argument("--out", required=True, help="directory the captures and index.txt go to")
    fe.add_argument("--kubeconfig", default="", help="kubeconfig to read; empty is kubectl's own")
    fe.add_argument(
        "--context", default="", help="kubeconfig context to read; empty is its current one"
    )
    fe.add_argument(
        "--budget", type=float, default=BUDGET, help="seconds every capture together may take"
    )
    fe.set_defaults(func=cmd_failure_evidence)
