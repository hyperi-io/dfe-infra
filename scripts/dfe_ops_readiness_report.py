#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/dfe_ops_readiness_report.py
#  Purpose:      `dfe-ops readiness-report` -- why each workload the readiness
#                gate failed is not ready: where its pods could not schedule,
#                what each container waits on, the newest warnings, the nodes
#                and Karpenter's own state. Read once, after the gate fails.
#                Registered into dfe-ops the way dfe_ops_edge_probe.py is.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-ops readiness-report -- the evidence a ready count cannot carry.

    dfe-ops readiness-report [--namespaces "argocd dfe-*"] [--kubeconfig F] [--context C]

The readiness gate counts ready replicas, and a count cannot tell a pod with
nowhere to schedule from one that cannot pull its image. The gate runs this once
when it fails, and it prints what tells them apart:

  pods       every pod not Ready in a judged namespace or kube-system: where it
             is scheduled or why it is not, what each container waits on and how
             it last exited
  events     the newest Warning events in those namespaces, and on nodes and
             Karpenter's objects wherever those land
  nodes      each node's architecture, instance and capacity type, workload
             label, taints, and CPU and memory requested against allocatable
  karpenter  every NodePool, NodeClaim and EC2NodeClass with the conditions that
             are not True, and the controller's newest errors
  volumes    every PersistentVolumeClaim not Bound
  argo       every Application not Synced and Healthy, with why

It reads and never writes, and it exits 0 whatever it finds: the gate has
already given the verdict, and this only explains it. Anything it cannot read
is named in its own section rather than skipped.
"""

import argparse
import fnmatch
import json
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field

import capacity
from kubectl_cli import run_kubectl

# Karpenter, the load balancer controller and CoreDNS run here, outside any namespace the deploy owns.
ALWAYS_READ = ("kube-system",)
# Cluster-scoped kinds whose events land in whatever namespace the API server picks.
CLUSTER_EVENT_KINDS = frozenset({"Node", "NodeClaim", "NodePool", "EC2NodeClass"})
KUBECTL_TIMEOUT = 60
LINE_LIMIT = 400  # characters, so one runaway message cannot bury the rest
SECTION_LIMIT = 40  # lines per section
LOG_TAIL = 300  # controller log lines read for its newest errors
LOG_ERRORS_SHOWN = 10


@dataclass(frozen=True, slots=True)
class Reading:
    """One kubectl read: its items, or why there are none."""

    items: list[dict] = field(default_factory=list)
    error: str = ""


def _clip(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= LINE_LIMIT else f"{flat[: LINE_LIMIT - 3]}..."


def _why(reason: object, message: object) -> str:
    """`reason: message`, dropping whichever half is empty."""
    return ": ".join(str(part) for part in (reason, message) if part)


def _capped(lines: list[str]) -> list[str]:
    if len(lines) <= SECTION_LIMIT:
        return lines
    return [*lines[:SECTION_LIMIT], f"... and {len(lines) - SECTION_LIMIT} more"]


def _meta(item: dict) -> dict:
    return item.get("metadata") or {}


def _status(item: dict) -> dict:
    return item.get("status") or {}


def judged(namespace: str, globs: Iterable[str]) -> bool:
    """Whether a namespace is one the report reads: a glob matches it, or it is always read."""
    return namespace in ALWAYS_READ or any(fnmatch.fnmatchcase(namespace, g) for g in globs)


def _failing_conditions(item: dict, wanted: Iterable[str] | None = None) -> list[str]:
    """Each condition not True, as `Type=Status reason: message`, in the order the object lists them."""
    lines = []
    for cond in _status(item).get("conditions") or []:
        if not isinstance(cond, dict) or cond.get("status") == "True":
            continue
        if wanted is not None and cond.get("type") not in wanted:
            continue
        why = _why(cond.get("reason"), cond.get("message"))
        lines.append(_clip(f"{cond.get('type')}={cond.get('status')} {why}"))
    return lines


def _pod_ready(pod: dict) -> bool:
    status = _status(pod)
    containers = status.get("containerStatuses") or []
    return status.get("phase") == "Running" and bool(containers) and all(
        c.get("ready") for c in containers
    )


def _container_lines(status: dict, kind: str) -> list[str]:
    lines = []
    for container in status.get(f"{kind}Statuses") or []:
        name = container.get("name")
        label = "init container" if kind == "initContainer" else "container"
        # An empty state block is still the state, so these test for the key, not its contents.
        state = container.get("state") or {}
        if "waiting" in state:
            waiting = state["waiting"] or {}
            lines.append(_clip(f"{label} {name} waiting {_why(waiting.get('reason'), waiting.get('message'))}"))
        elif "terminated" in state and (state["terminated"] or {}).get("exitCode") not in (0, None):
            code, reason = state["terminated"].get("exitCode"), state["terminated"].get("reason")
            lines.append(_clip(f"{label} {name} exited {code} ({reason})"))
        elif "running" in state and not container.get("ready"):
            lines.append(f"{label} {name} running, not {'ready' if label == 'container' else 'finished'}")
        last = (container.get("lastState") or {}).get("terminated")
        if last and container.get("restartCount"):
            exited = f"last exit {last.get('exitCode')} ({last.get('reason')})"
            lines.append(
                _clip(f"{label} {name} restarted {container.get('restartCount')}x, "
                      f"{_why(exited, last.get('message'))}")
            )
    return lines


def pod_lines(pods: list[dict], globs: Iterable[str]) -> list[str]:
    """Every judged pod that is not Ready, each followed by the evidence for why.

    Args:
        pods: The items of `kubectl get pods -A -o json`.
        globs: Namespace globs to judge; kube-system is read whatever they say.

    Returns:
        Lines to print, the evidence indented under its pod.
    """
    lines: list[str] = []
    for pod in pods:
        meta, status, spec = _meta(pod), _status(pod), pod.get("spec") or {}
        namespace = meta.get("namespace") or ""
        if not judged(namespace, globs) or status.get("phase") == "Succeeded" or _pod_ready(pod):
            continue
        lines.append(
            f"pod {namespace}/{meta.get('name')} phase={status.get('phase')} "
            f"node={spec.get('nodeName') or '<none>'}"
        )
        evidence = _failing_conditions(pod, ("PodScheduled",))
        evidence += _container_lines(status, "initContainer")
        evidence += _container_lines(status, "container")
        if status.get("reason") or status.get("message"):
            evidence.append(_clip(_why(status.get("reason"), status.get("message"))))
        lines += [f"  {line}" for line in evidence]
    return lines


def _event_time(event: dict) -> str:
    series = event.get("series") or {}
    return str(
        series.get("lastObservedTime")
        or event.get("lastTimestamp")
        or event.get("eventTime")
        or _meta(event).get("creationTimestamp")
        or ""
    )


def event_lines(events: list[dict], globs: Iterable[str]) -> list[str]:
    """The newest Warning per object and reason, newest first.

    Args:
        events: The items of `kubectl get events -A -o json`.
        globs: Namespace globs to judge. A node's or Karpenter object's event is
            kept wherever it landed.

    Returns:
        One line per object and reason.
    """
    newest: dict[tuple[str, str, str, str], dict] = {}
    for event in events:
        if event.get("type") != "Warning":
            continue
        involved = event.get("involvedObject") or event.get("regarding") or {}
        namespace = _meta(event).get("namespace") or ""
        if not (judged(namespace, globs) or involved.get("kind") in CLUSTER_EVENT_KINDS):
            continue
        key = (
            namespace, str(involved.get("kind")), str(involved.get("name")), str(event.get("reason"))
        )
        if key not in newest or _event_time(event) >= _event_time(newest[key]):
            newest[key] = event
    ordered = sorted(newest.items(), key=lambda pair: _event_time(pair[1]), reverse=True)
    lines = []
    for (namespace, kind, name, reason), event in ordered:
        count = (event.get("series") or {}).get("count") or event.get("count") or 1
        message = event.get("message") or event.get("note") or ""
        lines.append(
            _clip(f"{_event_time(event)} {namespace or '-'} {kind}/{name} {reason} x{count}: {message}")
        )
    return lines


def _requests(pod: dict) -> tuple[float, int]:
    """CPU cores and memory bytes the scheduler counts for a pod: its containers, or its largest init."""
    spec = pod.get("spec") or {}

    def summed(containers: list[dict]) -> tuple[float, int]:
        cpu, mem = 0.0, 0
        for container in containers:
            wanted = (container.get("resources") or {}).get("requests") or {}
            cpu += capacity.parse_cpu(str(wanted.get("cpu", "0")))
            mem += capacity.parse_mem(str(wanted.get("memory", "0")))
        return cpu, mem

    cpu, mem = summed(spec.get("containers") or [])
    for init in spec.get("initContainers") or []:
        init_cpu, init_mem = summed([init])
        cpu, mem = max(cpu, init_cpu), max(mem, init_mem)
    return cpu, mem


def node_lines(nodes: list[dict], pods: list[dict]) -> list[str]:
    """Each node's identity, placement labels, taints, and requested against allocatable.

    Args:
        nodes: The items of `kubectl get nodes -o json`.
        pods: The items of `kubectl get pods -A -o json`, for what each node already holds.

    Returns:
        One line per node, with a count first.
    """
    held: dict[str, tuple[float, int]] = {}
    for pod in pods:
        node = (pod.get("spec") or {}).get("nodeName")
        if not node or _status(pod).get("phase") in ("Succeeded", "Failed"):
            continue
        try:
            cpu, mem = _requests(pod)
        except ValueError:
            continue
        have_cpu, have_mem = held.get(node, (0.0, 0))
        held[node] = (have_cpu + cpu, have_mem + mem)
    lines = [f"{len(nodes)} node(s)"]
    for node in nodes:
        meta, status = _meta(node), _status(node)
        labels = meta.get("labels") or {}
        name = meta.get("name") or ""
        ready = next(
            (c.get("status") for c in status.get("conditions") or [] if c.get("type") == "Ready"),
            "Unknown",
        )
        allocatable = status.get("allocatable") or {}
        cpu, mem = held.get(name, (0.0, 0))
        try:
            cpu_total = capacity.parse_cpu(str(allocatable.get("cpu", "0")))
            mem_total = capacity.parse_mem(str(allocatable.get("memory", "0")))
            room = (
                f"requested cpu {cpu:.2f}/{cpu_total:.2f} "
                f"memory {mem / 1024**3:.1f}/{mem_total / 1024**3:.1f}Gi"
            )
        except ValueError:
            room = f"allocatable cpu {allocatable.get('cpu')} memory {allocatable.get('memory')}"
        taints = ",".join(
            f"{t.get('key')}={t.get('value', '')}:{t.get('effect')}"
            for t in (node.get("spec") or {}).get("taints") or []
        )
        pool = labels.get("karpenter.sh/nodepool") or labels.get("eks.amazonaws.com/nodegroup") or "-"
        capacity_type = (
            labels.get("karpenter.sh/capacity-type") or labels.get("eks.amazonaws.com/capacityType") or "-"
        )
        lines.append(
            _clip(
                f"node {name} ready={ready} arch={labels.get('kubernetes.io/arch', '-')} "
                f"type={labels.get('node.kubernetes.io/instance-type', '-')} capacity={capacity_type} "
                f"pool={pool} workload={labels.get('dfe.hyperi.io/workload', '-')} {room} "
                f"taints={taints or '-'}"
            )
        )
    return lines


def nodepool_lines(pools: list[dict]) -> list[str]:
    """Each NodePool's limits, what it has bought, and any condition not True."""
    lines = []
    for pool in pools:
        limits = (pool.get("spec") or {}).get("limits") or {}
        used = _status(pool).get("resources") or {}
        lines.append(
            _clip(
                f"nodepool {_meta(pool).get('name')} limits cpu={limits.get('cpu', '-')} "
                f"memory={limits.get('memory', '-')}, holds cpu={used.get('cpu', '0')} "
                f"memory={used.get('memory', '0')} nodes={used.get('nodes', '0')}"
            )
        )
        lines += [f"  {line}" for line in _failing_conditions(pool)]
    return lines


def nodeclaim_lines(claims: list[dict]) -> list[str]:
    """Each NodeClaim, the node it became if any, and the launch step it is stuck on."""
    lines = []
    for claim in claims:
        meta, status = _meta(claim), _status(claim)
        labels = meta.get("labels") or {}
        lines.append(
            _clip(
                f"nodeclaim {meta.get('name')} pool={labels.get('karpenter.sh/nodepool', '-')} "
                f"type={labels.get('node.kubernetes.io/instance-type', '-')} "
                f"node={status.get('nodeName') or '<none>'}"
            )
        )
        lines += [f"  {line}" for line in _failing_conditions(claim)]
    return lines


def nodeclass_lines(classes: list[dict]) -> list[str]:
    """Each EC2NodeClass and the condition -- AMI, subnets, security groups, profile -- it fails."""
    lines = []
    for node_class in classes:
        failing = _failing_conditions(node_class)
        lines.append(f"ec2nodeclass {_meta(node_class).get('name')} {'not ready' if failing else 'ready'}")
        lines += [f"  {line}" for line in failing]
    return lines


def log_error_lines(text: str) -> list[str]:
    """The newest distinct ERROR lines from a controller's JSON log, oldest of them first."""
    seen: dict[str, None] = {}
    for raw in text.splitlines():
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            if "ERROR" in raw:
                seen.pop(raw.strip(), None)
                seen[raw.strip()] = None
            continue
        if not isinstance(entry, dict) or str(entry.get("level", "")).upper() != "ERROR":
            continue
        line = _clip(f"{entry.get('message', '')}: {entry.get('error', '')}")
        seen.pop(line, None)
        seen[line] = None
    return list(seen)[-LOG_ERRORS_SHOWN:]


def pvc_lines(claims: list[dict], globs: Iterable[str]) -> list[str]:
    """Every judged PersistentVolumeClaim that is not Bound."""
    lines = []
    for claim in claims:
        meta = _meta(claim)
        if not judged(meta.get("namespace") or "", globs) or _status(claim).get("phase") == "Bound":
            continue
        lines.append(
            f"pvc {meta.get('namespace')}/{meta.get('name')} {_status(claim).get('phase')} "
            f"class={(claim.get('spec') or {}).get('storageClassName') or '-'}"
        )
    return lines


def application_lines(apps: list[dict]) -> list[str]:
    """Every Argo Application not Synced and Healthy, with its conditions and last operation."""
    lines = []
    for app in apps:
        status = _status(app)
        sync = (status.get("sync") or {}).get("status")
        health = status.get("health") or {}
        if sync == "Synced" and health.get("status") == "Healthy":
            continue
        lines.append(
            _clip(
                f"application {_meta(app).get('name')} sync={sync} health={health.get('status')} "
                f"{health.get('message') or ''}"
            )
        )
        for cond in status.get("conditions") or []:
            lines.append("  " + _clip(_why(cond.get("type"), cond.get("message"))))
        operation = status.get("operationState") or {}
        if operation.get("phase") not in (None, "Succeeded"):
            lines.append("  " + _clip(_why(f"last operation {operation.get('phase')}", operation.get("message"))))
    return lines


# --- reading the cluster ----------------------------------------------------------


def _kubectl_prefix(args: argparse.Namespace) -> list[str]:
    prefix = []
    if args.kubeconfig:
        prefix += ["--kubeconfig", args.kubeconfig]
    if args.context:
        prefix += ["--context", args.context]
    return prefix


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "no message"


def read(prefix: list[str], what: list[str]) -> Reading:
    """`kubectl get <what> -o json`, as its items or the reason there are none."""
    try:
        done = run_kubectl([*prefix, "get", *what, "-o", "json"], timeout=KUBECTL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return Reading(error=f"kubectl get {' '.join(what)} did not answer in {KUBECTL_TIMEOUT}s")
    if done.returncode != 0:
        return Reading(error=_clip(_last_line(done.stderr)))
    try:
        body = json.loads(done.stdout)
    except json.JSONDecodeError:
        return Reading(error=f"kubectl get {' '.join(what)} did not answer with JSON")
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return Reading(error=f"kubectl get {' '.join(what)} answered with no item list")
    return Reading(items=[item for item in items if isinstance(item, dict)])


def _controller_errors(prefix: list[str]) -> list[str]:
    try:
        done = run_kubectl(
            [*prefix, "-n", "kube-system", "logs", "-l", "app.kubernetes.io/name=karpenter",
             f"--tail={LOG_TAIL}", "--all-containers"],
            timeout=KUBECTL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return [f"its logs did not answer in {KUBECTL_TIMEOUT}s"]
    if done.returncode != 0:
        return [f"its logs could not be read: {_clip(_last_line(done.stderr))}"]
    return log_error_lines(done.stdout) or ["no ERROR in its newest log lines"]


def _section(title: str, reading: Reading, lines: list[str], empty: str) -> list[str]:
    if reading.error:
        return [f"--- {title} ---", f"  could not read: {reading.error}"]
    return [f"--- {title} ---", *(f"  {line}" for line in _capped(lines) or [empty])]


def _karpenter(prefix: list[str]) -> list[str]:
    pools = read(prefix, ["nodepools.karpenter.sh"])
    if pools.error and "doesn't have a resource type" in pools.error:
        return ["--- karpenter ---", "  not installed on this cluster (no nodepools.karpenter.sh)"]
    claims = read(prefix, ["nodeclaims.karpenter.sh"])
    classes = read(prefix, ["ec2nodeclasses.karpenter.k8s.aws"])
    return [
        *_section("karpenter nodepools", pools, nodepool_lines(pools.items), "no NodePool exists"),
        *_section("karpenter nodeclaims", claims, nodeclaim_lines(claims.items),
                  "no NodeClaim exists: nothing has been launched"),
        *_section("karpenter ec2nodeclasses", classes, nodeclass_lines(classes.items),
                  "no EC2NodeClass exists"),
        "--- karpenter controller errors ---",
        *(f"  {line}" for line in _controller_errors(prefix)),
    ]


def report(args: argparse.Namespace) -> list[str]:
    """Every section, read from the cluster `args` names."""
    prefix = _kubectl_prefix(args)
    globs = args.namespaces.split()
    pods = read(prefix, ["pods", "-A"])
    events = read(prefix, ["events", "-A"])
    nodes = read(prefix, ["nodes"])
    claims = read(prefix, ["pvc", "-A"])
    apps = read(prefix, ["applications.argoproj.io", "-A"])
    return [
        "=== readiness report: why the workloads above are not ready ===",
        *_section("pods not Ready", pods, pod_lines(pods.items, globs), "every judged pod is Ready"),
        *_section("newest warning events", events, event_lines(events.items, globs), "no Warning event"),
        *_section("nodes", nodes, node_lines(nodes.items, pods.items), "no node"),
        *_karpenter(prefix),
        *_section("volume claims not Bound", claims, pvc_lines(claims.items, globs), "every claim is Bound"),
        *_section("argo applications not Synced and Healthy", apps, application_lines(apps.items),
                  "every Application is Synced and Healthy"),
    ]


def cmd_readiness_report(args: argparse.Namespace) -> int:
    """`dfe-ops readiness-report`: print the report; exit 0 whatever it finds."""
    try:
        lines = report(args)
    except FileNotFoundError:
        print("  readiness report skipped: kubectl is not on PATH", file=sys.stderr)
        return 0
    print("\n".join(lines))
    return 0


def add_readiness_report_subparser(sub: argparse._SubParsersAction) -> None:
    """Register `dfe-ops readiness-report`."""
    rep = sub.add_parser(
        "readiness-report",
        help="READ-ONLY: why each workload is not ready -- scheduling, image pulls, events, nodes, "
             "Karpenter, volumes and Argo -- for after the readiness gate fails",
        description="Reads the cluster once and prints, for every pod not Ready in the judged "
                    "namespaces and kube-system, why: its scheduling verdict and each container's "
                    "wait, then the newest Warning events, every node, Karpenter's NodePools, "
                    "NodeClaims, EC2NodeClasses and controller errors, unbound volume claims, "
                    "and Argo Applications not Synced and Healthy. Writes nothing; exits 0.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    rep.add_argument("--namespaces", default="*",
                     help="space-separated namespace globs to judge; kube-system is always read")
    rep.add_argument("--kubeconfig", default="", help="kubeconfig to read; empty is kubectl's own")
    rep.add_argument("--context", default="", help="kubeconfig context to read; empty is its current one")
    rep.set_defaults(func=cmd_readiness_report)
