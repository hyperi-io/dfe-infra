#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/dfe_ops_failure_evidence.py
#  Purpose:      `dfe-ops failure-evidence` -- the node, pod and pod-network
#                state a failed cycle stage leaves, written to files before the
#                destroy removes the cluster. Registered into dfe-ops the way
#                dfe_ops_readiness_report.py is.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-ops failure-evidence -- the nodes, pods and pod network as they stood when a stage failed.

    dfe-ops failure-evidence --out DIR [--kubeconfig F] [--context C] [--budget SECONDS]

`dfe-ops cycle` runs it once when a stage fails, before the destroy, so the
evidence outlives the cluster. Each capture is one file under DIR:

  nodes-wide.txt             kubectl get nodes -o wide
  node-conditions.txt        each node's runtime, conditions, capacity and allocatable
  pods-wide.txt              kubectl get pods -A -o wide
  warning-events.txt         every namespace's Warning events, oldest first, the newest
                             kept when they pass the byte cap
  kube-system-events.txt     kube-system events, oldest first
  services-wide.txt          kubectl get services -A -o wide: each load balancer's address
  gateways-wide.txt          kubectl get gateways -A -o wide (Gateway API)
  kube-system-certificates.txt
                             the cert-manager Certificates in kube-system, ready or not
  kube-system-not-ready.txt  a summary, the events and the container logs of each kube-system pod
                             not Running and Ready
  stuck-pods/<namespace>.txt a summary, the events and the container logs of each pod in that
                             namespace with a container waiting on ImagePullBackOff,
                             ErrImagePull, CrashLoopBackOff or CreateContainerConfigError, or
                             with an init container not yet finished (Init:N/M), then each
                             pod Running but not Ready; the events carry the kubelet's own pull
                             or config error. Past 40 pods in all, the last file closes on a
                             `+N more not-Ready pods` line
  node-events/<node>.txt     the events of each node whose kubelet reported InvalidDiskCapacity
  aws-load-balancer-controller/webhook-ca.txt
                             each of the controller's webhook configurations: its CA injection
                             source and every webhook's caBundle as a digest, never the bundle
  aws-load-balancer-controller/<pod>.<container>.log
                             each controller container's newest lines, plus the previous run of
                             any that restarted
  aws-node/<pod>.<container>.log
                             each aws-node container's newest lines, the init container
                             included, plus the previous run of any that restarted
  aws-node/<node>.<file>     the CNI's own ipamd.log and plugin.log, newest bytes, through
                             the kubelet's /logs endpoint, else from the aws-node container
  index.txt                  every capture, the command behind it and how it ended

A pod summary is built from the pod list already read, and names each field it
keeps: the node, phase, conditions, and for every init and main container its
image, state, restart count and last termination. A container's env, envFrom,
args and command, the pod's volumes and annotations are never read, which is why
no capture runs `kubectl describe`: it prints each container's literal env values,
and the output is uploaded as a CI artefact.

A pod report also carries container logs, since the state and events name a crash but not
its cause. Per container it reads the previous run of one that restarted or sits in
CrashLoopBackOff, the current run of an init container that is running or failed, and the
current run of a main container that is running but not ready. A container that never
started gets one line saying so.

Logs are free text, so every container log, pod summary and events text passes through
`redact` before it is written: credential-named values, URL userinfo, bearer tokens, private
key blocks and common provider token shapes become `***REDACTED***`. A container read keeps
its newest POD_LOG_TAIL lines within POD_LOG_BYTES, and all pod logs of a run stop at
POD_LOG_TOTAL bytes.

Every capture is bounded in time and bytes, and the run as a whole in time. One
that fails is recorded in index.txt and the rest still run. It reads and never
writes to the cluster, it never reads a Secret, and it exits 0 whatever it finds.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Collection, Iterable
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
MAX_UNREADY = 40  # unready kube-system pods summarised
MAX_STUCK = 40  # stuck and not-Ready pods summarised across every namespace together
MAX_CONTROLLER_PODS = 4  # load balancer controller pods whose logs are read
POD_LOG_TAIL = 200  # newest lines read from one container of a stuck or unready pod
POD_LOG_BYTES = 64 * 1024  # newest bytes kept from one such read
POD_LOG_TOTAL = 8 * 1024 * 1024  # bytes all such reads of a run may keep together
POD_LOG_TIMEOUT = 15.0  # seconds one such read may take; a pod on a dead node answers nothing
# Share of the budget the pod log reads leave for the captures that run after them.
POD_LOG_KEEP = 0.3
CHUNK = 65536
POLL = 0.1
JOIN_TIMEOUT = 5.0

AWS_NODE_LABEL = ("k8s-app", "aws-node")
CNI_CONTAINER = "aws-node"
# The aws-node container mounts the node's /var/log/aws-routed-eni here.
CNI_LOG_MOUNT = "/host/var/log/aws-routed-eni"
CNI_LOGS = ("ipamd.log", "plugin.log")
DISK_REASON = "InvalidDiskCapacity"
# The AWS Load Balancer Controller chart labels its pods and its webhook configurations with this.
CONTROLLER_DIR = "aws-load-balancer-controller"
CONTROLLER_LABEL = ("app.kubernetes.io/name", "aws-load-balancer-controller")
WEBHOOK_CONFIGS = "mutatingwebhookconfigurations,validatingwebhookconfigurations"
INJECT_CA = "cert-manager.io/inject-ca-from"
# Named in full: Istio serves a `gateways` resource too, and a bare name resolves to one group.
GATEWAYS = "gateways.gateway.networking.k8s.io"
CERTIFICATES = "certificates.cert-manager.io"
# A container waiting on one of these never starts by itself, and its events carry the reason.
STUCK_REASONS = frozenset(
    {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff", "CreateContainerConfigError"}
)
WARNINGS = "type=Warning"
# creationTimestamp is set on every event, where lastTimestamp can be empty.
OLDEST_FIRST = "--sort-by=.metadata.creationTimestamp"
INDEX = "index.txt"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_name(text: str) -> str:
    """A path segment from a pod, node or stage name."""
    return _UNSAFE.sub("_", text) or "_"


# --- redaction of free text ------------------------------------------------------------
# Stdlib only, as dfe-ops carries no third-party dependency. The key names follow scalo's
# SensitiveDataFilter and the token shapes its secrets scrubber.

MASK = "***REDACTED***"
# A key may carry a prefix (CLIENT_SECRET, x-auth-token, accessToken) but must end on the
# name, so `token_count` stays readable. A bare `auth` must stand alone.
_KEY = (
    r"(?:pass(?:word|wd|phrase)|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"(?:signing|encryption|master|ssh|tls|hmac|session)[_-]?key|private[_-]?key|"
    r"authorization|credentials?|cookie|bearer|(?<![A-Za-z0-9])auth)"
)
# After the key: `=`, `:`, `:=` or `=>`, then a value that is quoted (plain or JSON-escaped),
# a quote left open to the end of the line, or a run up to the next separator.
_ASSIGNMENT = re.compile(
    rf"""({_KEY}(?:\\?["'])?[ \t]*(?:=>|:=|[:=])[ \t]*)"""
    r"""(\\"[^"\\\n]*\\"|"(?:[^"\\\n]|\\.)*"|'[^'\n]*'|["'][^\n]*|[^\s,;"'}\]\\]+)""",
    re.IGNORECASE,
)
# A line with none of these cannot hold a key above, so the per-key patterns skip it.
_KEY_HINTS = ("pass", "pwd", "secret", "token", "key", "credential", "auth", "cookie", "bearer")
# A header's value runs to the end of the line: `Authorization: Token abc` is two words.
_HEADER = re.compile(
    r"""((?:proxy-)?authorization|(?:set-)?cookie)("?[ \t]*[:=][ \t]*)(?!["'\\])[^\n]+""",
    re.IGNORECASE,
)
# The lookahead needs a digit or token punctuation, so prose such as "bearer authentication" stays.
_BEARER = re.compile(
    r"\bbearer[ \t]+(?=[A-Za-z0-9._~+/=-]*[0-9._~+/=-])[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE
)
# scheme://user:password@host and scheme://token@host
_USERINFO = re.compile(r"(://)([^\s/@:]*:)?([^\s/]+)@")
_SHAPES = re.compile(
    "|".join(
        [
            r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",  # AWS access key id
            r"\bgh[opsur]_[0-9A-Za-z]{36,}",  # GitHub
            r"\bgithub_pat_[0-9A-Za-z_]{40,}",
            r"\bglpat-[0-9A-Za-z_-]{20,}",  # GitLab
            r"\bxox[abprs]-[0-9A-Za-z-]{10,}",  # Slack
            r"\bsk-[0-9A-Za-z_-]{20,}",  # OpenAI, Anthropic
            r"\b[rsp]k_(?:live|test)_[0-9A-Za-z]{24,}",  # Stripe
            r"\bnpm_[0-9A-Za-z]{36}\b",
            r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*",  # a JWT
        ]
    )
)
# A tail read can cut a block, leaving an END with no BEGIN or the reverse, and the body goes.
_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL
)
_PEM_ORPHAN_END = re.compile(r"\A.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)
# A flag with its value as the next word: `--password hunter2`.
_FLAG = re.compile(rf"(?<!\S)(--[A-Za-z0-9-]*{_KEY}[ \t]+)(?!-)\S+", re.IGNORECASE)


def redact(text: str) -> str:
    """`text` with every credential-shaped value replaced by `***REDACTED***`.

    Masks a value whose key names a credential (`password=x`, `"token": "x"`, the escaped
    `\\"token\\":\\"x\\"` of a JSON string inside a log line), a header's whole value, the
    userinfo of a URL, a bearer token, a PEM private key block and the common provider token
    shapes. A secret printed bare, with no key or recognised shape, is not found.

    Args:
        text: Free text such as a container log.

    Returns:
        The same text, line structure intact, with each match masked.
    """
    text = _PEM.sub(MASK, text)
    text = _PEM_ORPHAN_END.sub(MASK, text, count=1)
    text = _USERINFO.sub(lambda m: f"{m[1]}{m[2] or ''}{MASK}@", text)
    text = _SHAPES.sub(MASK, text)
    text = _BEARER.sub(f"bearer {MASK}", text)
    lines = text.split("\n")
    for number, line in enumerate(lines):
        lowered = line.lower()
        if not any(hint in lowered for hint in _KEY_HINTS):
            continue
        line = _HEADER.sub(lambda m: f"{m[1]}{m[2]}{MASK}", line)
        line = _FLAG.sub(lambda m: f"{m[1]}{MASK}", line)
        lines[number] = _ASSIGNMENT.sub(_mask_value, line)
    return "\n".join(lines)


def _mask_value(match: re.Match[str]) -> str:
    """The key and separator of an assignment with its value masked, a closed quote kept."""
    key, value = match[1], match[2]
    for quote in ('\\"', '"', "'"):
        if len(value) >= 2 * len(quote) and value.startswith(quote) and value.endswith(quote):
            return f"{key}{quote}{MASK}{quote}"
    return f"{key}{MASK}"


def redact_bytes(data: bytes) -> bytes:
    """`redact` over output read as UTF-8, bytes that are not valid UTF-8 replaced."""
    return redact(data.decode("utf-8", errors="replace")).encode("utf-8")


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


def _labelled(pods: list[dict], label: tuple[str, str]) -> list[dict]:
    """The pods carrying `label`, ordered by the node each runs on."""
    key, value = label
    found = [p for p in pods if ((p.get("metadata") or {}).get("labels") or {}).get(key) == value]
    return sorted(found, key=lambda p: (p.get("spec") or {}).get("nodeName") or "")


def aws_node_pods(pods: list[dict]) -> list[dict]:
    """The aws-node DaemonSet's pods, ordered by the node each runs on."""
    return _labelled(pods, AWS_NODE_LABEL)


def controller_pods(pods: list[dict]) -> list[dict]:
    """The AWS Load Balancer Controller's pods, ordered by the node each runs on."""
    return _labelled(pods, CONTROLLER_LABEL)


def bundle_digest(bundle: str | None) -> str:
    """A caBundle as the first 16 hex of its sha256 and its length, or `absent`."""
    if not bundle:
        return "absent"
    digest = hashlib.sha256(bundle.encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest} ({len(bundle)} base64 chars)"


def webhook_ca_lines(configs: list[dict]) -> list[str]:
    """Each webhook configuration's CA injection source and every webhook's caBundle digest.

    The digests show whether the webhooks agree on one CA and whether cert-manager
    has injected it yet; a bundle is a CA certificate, and only its digest is kept.
    """
    lines: list[str] = []
    digests: set[str] = set()
    for config in configs:
        meta = config.get("metadata") or {}
        lines.append(f"{config.get('kind') or 'WebhookConfiguration'} {meta.get('name')}")
        injected_from = (meta.get("annotations") or {}).get(INJECT_CA)
        lines.append(f"  {INJECT_CA} {injected_from or '-'}")
        for hook in config.get("webhooks") or []:
            digest = bundle_digest((hook.get("clientConfig") or {}).get("caBundle"))
            digests.add(digest)
            lines.append(f"  webhook {hook.get('name')} caBundle {digest}")
    lines.append(f"distinct caBundle values: {len(digests)}")
    return lines


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


def _identity(pod: dict) -> tuple[str, str]:
    """(namespace, name) of a pod, empty where the list does not carry one."""
    meta = pod.get("metadata") or {}
    return meta.get("namespace") or "", meta.get("name") or ""


def _sidecars(pod: dict) -> set[str]:
    """Init containers declared `restartPolicy: Always`: they run for the life of the pod."""
    declared = (pod.get("spec") or {}).get("initContainers") or []
    return {c["name"] for c in declared if c.get("name") and c.get("restartPolicy") == "Always"}


def init_unfinished(pod: dict) -> bool:
    """True while an init container has not exited 0: the pod shows Init:N/M.

    A sidecar init container never exits, so it counts as finished once it has started.
    """
    sidecars = _sidecars(pod)
    for container in (pod.get("status") or {}).get("initContainerStatuses") or []:
        if container.get("name") in sidecars and container.get("started"):
            continue
        finished = (container.get("state") or {}).get("terminated") or {}
        if finished.get("exitCode") != 0:
            return True
    return False


def stuck_pods(pods: list[dict]) -> list[tuple[str, str]]:
    """(namespace, name) of every pod with a stuck-reason wait or an init container unfinished."""
    found = []
    for pod in pods:
        meta, status = pod.get("metadata") or {}, pod.get("status") or {}
        containers = [
            *(status.get("initContainerStatuses") or []),
            *(status.get("containerStatuses") or []),
        ]
        reasons = {((c.get("state") or {}).get("waiting") or {}).get("reason") for c in containers}
        stuck = bool(reasons & STUCK_REASONS) or init_unfinished(pod)
        if stuck and meta.get("namespace") and meta.get("name"):
            found.append((meta["namespace"], meta["name"]))
    return sorted(found)


def running_unready_pods(pods: list[dict], skip: Collection[str] = ()) -> list[tuple[str, str]]:
    """(namespace, name) of every Running pod with a container not ready, in no `skip` namespace.

    A pod being deleted is left out: its readiness drops as it stops, which says nothing
    about why a stage failed.
    """
    found = []
    for pod in pods:
        meta, status = pod.get("metadata") or {}, pod.get("status") or {}
        if status.get("phase") != "Running" or pod_ready(pod) or meta.get("deletionTimestamp"):
            continue
        if meta.get("namespace") and meta.get("name") and meta["namespace"] not in skip:
            found.append((meta["namespace"], meta["name"]))
    return sorted(found)


@dataclass(frozen=True, slots=True)
class LogPlan:
    """One container log a pod report asks for: which container, which run, or why none exists."""

    container: str
    init: bool
    previous: bool = False
    # Why there is nothing to read; empty when a read is worth making.
    absent: str = ""


def log_plans(pod: dict) -> list[LogPlan]:
    """The container logs worth reading for a pod, read off the pod JSON alone.

    An init container is read while it runs or after it fails, a main container or sidecar
    only while it runs and is not ready, and any container that restarted or sits in
    CrashLoopBackOff has its previous run read as well. A container that never started has
    nothing to read, and one that is running and ready or finished cleanly is left out.
    """
    status = pod.get("status") or {}
    sidecars = _sidecars(pod)
    plans: list[LogPlan] = []
    for init, key in ((True, "initContainerStatuses"), (False, "containerStatuses")):
        for seen in status.get(key) or []:
            name = seen.get("name")
            if not name:
                continue
            state = seen.get("state") or {}
            waiting = state.get("waiting") or {}
            ended = state.get("terminated")
            failed = ended is not None and ended.get("exitCode") != 0
            lasting = not init or name in sidecars
            live = "running" in state and (not seen.get("ready") if lasting else True)
            restarted = int(seen.get("restartCount") or 0) > 0
            crashed = bool((seen.get("lastState") or {}).get("terminated"))
            crashing = waiting.get("reason") == "CrashLoopBackOff"
            if live or failed:
                plans.append(LogPlan(name, init))
            if restarted or crashed or crashing:
                plans.append(LogPlan(name, init, previous=True))
            elif "waiting" in state or not state:
                why = "no state reported" if not state else "waiting"
                if waiting.get("reason"):
                    why = f"waiting, reason {waiting['reason']}"
                plans.append(LogPlan(name, init, absent=f"it has not started ({why})"))
    return plans


# --- a pod summary that cannot carry an env value ------------------------------------
# Each function reads the named keys below and nothing else. A pod's env, envFrom, args,
# command, volumes and annotations are not among them, so none can reach a file.


def _text(value: object) -> str:
    """One line from something the cluster reported: runs of whitespace become one space."""
    return " ".join(str(value).split())


def _state_text(state: dict) -> str:
    """A container state as `waiting, reason R, message: M`, or "" when it reports none."""
    for kind in ("waiting", "running", "terminated"):
        if kind not in state:
            continue
        detail = state[kind] or {}
        parts = [kind]
        if detail.get("reason"):
            parts.append(f"reason {_text(detail['reason'])}")
        if detail.get("exitCode") is not None:
            parts.append(f"exit code {_text(detail['exitCode'])}")
        if detail.get("message"):
            parts.append(f"message: {_text(detail['message'])}")
        return ", ".join(parts)
    return ""


def container_lines(pod: dict) -> list[str]:
    """Each init and main container's image, state, restarts and last termination, as text."""
    spec, status = pod.get("spec") or {}, pod.get("status") or {}
    lines: list[str] = []
    for label, declared_key, status_key in (
        ("init container", "initContainers", "initContainerStatuses"),
        ("container", "containers", "containerStatuses"),
    ):
        declared = {c["name"]: c for c in spec.get(declared_key) or [] if c.get("name")}
        reported = {c["name"]: c for c in status.get(status_key) or [] if c.get("name")}
        for name in [*declared, *(n for n in reported if n not in declared)]:
            known, seen = declared.get(name) or {}, reported.get(name) or {}
            lines.append(f"  {label} {name}")
            lines.append(f"    image {_text(known.get('image') or seen.get('image') or '-')}")
            state = _state_text(seen.get("state") or {})
            lines.append(f"    state {state or 'not reported'}")
            lines.append(f"    restartCount {int(seen.get('restartCount') or 0)}")
            if last := _state_text(seen.get("lastState") or {}):
                lines.append(f"    last state {last}")
    return lines


def pod_summary_lines(namespace: str, pod: dict) -> list[str]:
    """A pod's node, phase, conditions and containers, as text that holds no env value."""
    meta, spec, status = pod.get("metadata") or {}, pod.get("spec") or {}, pod.get("status") or {}
    lines = [f"pod {namespace}/{meta.get('name')}", f"  node {spec.get('nodeName') or '-'}"]
    lines.append(f"  phase {status.get('phase') or '-'}")
    if status.get("reason") or status.get("message"):
        why = ": ".join(_text(p) for p in (status.get("reason"), status.get("message")) if p)
        lines.append(f"  status {why}")
    for cond in status.get("conditions") or []:
        why = ": ".join(_text(p) for p in (cond.get("reason"), cond.get("message")) if p)
        lines.append(f"  condition {cond.get('type')}={cond.get('status')} {why}".rstrip())
    return [*lines, *container_lines(pod)]


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
    # Bytes the pod log reads of this run may still keep.
    pod_log_left: int = POD_LOG_TOTAL

    def _kubectl(
        self,
        args: list[str],
        *,
        limit: int,
        tail: bool,
        timeout: float = CAPTURE_TIMEOUT,
        keep: float = 0.0,
    ) -> Outcome | None:
        """Run kubectl, or None when no more than `keep` seconds of the budget are left."""
        left = self.deadline - time.monotonic() - keep
        if left <= 0:
            return None
        argv = ["kubectl", *self.prefix, *args]
        return run_bounded(argv, timeout=min(timeout, left), limit=limit, tail=tail)

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

    def capture(
        self, name: str, args: list[str], *, tail: bool = False, log: bool = False
    ) -> Outcome | None:
        """`kubectl <args>` into the file `name`; what it kept is written even when it failed.

        A `log` capture is free text, so it is redacted before it is written.
        """
        outcome = self._kubectl(args, limit=TEXT_BYTES, tail=tail)
        self.runs += 1
        if outcome and outcome.output:
            self.write(name, redact_bytes(outcome.output) if log else outcome.output)
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

    def pod_logs(self, name: str, namespace: str, pod: dict) -> str:
        """The logs `log_plans` picks for one pod, as report text with every log redacted.

        A read that fails, or that has no instance to read, is one plain line and nothing more.
        """
        pod_name = (pod.get("metadata") or {}).get("name") or ""
        plans = log_plans(pod)
        if not plans:
            return "  logs: no container is running, restarted or failed, so there are none\n"
        sections: list[str] = []
        for plan in plans:
            kind = "init container" if plan.init else "container"
            run = "previous run" if plan.previous else "current run"
            title = f"  logs of {kind} {plan.container}, {run}"
            if plan.absent:
                sections.append(f"{title}: none, {plan.absent}\n")
                continue
            args = ["-n", namespace, "logs", pod_name, "-c", plan.container]
            args += [f"--tail={POD_LOG_TAIL}", "--timestamps"]
            if plan.previous:
                args.append("--previous")
            limit = min(POD_LOG_BYTES, self.pod_log_left)
            outcome = None
            if limit <= 0:
                how = f"not read: the {POD_LOG_TOTAL} byte limit on pod logs was reached"
            else:
                outcome = self._kubectl(
                    args,
                    limit=limit,
                    tail=True,
                    timeout=POD_LOG_TIMEOUT,
                    keep=self.budget * POD_LOG_KEEP,
                )
                self.runs += 1
                how = "not run: the time left is kept for the other captures"
                if outcome is not None:
                    how = verdict(outcome, self.budget)
            text = ""
            if outcome:
                if outcome.ok:
                    self.answered += 1
                self.pod_log_left -= len(outcome.output)
                text = redact_bytes(outcome.output).decode("utf-8")
                if not text and outcome.ok:
                    how = f"{how}, it printed nothing"
                elif text and not text.endswith("\n"):
                    text += "\n"
            self.note(f"{name}, {kind} {plan.container} {run} of {pod_name}", how, args)
            sections.append(f"{title}: {how}\n{text}")
        return "".join(sections)

    def pod_report(self, name: str, namespace: str, pods: list[dict], closing: str = "") -> None:
        """The file `name`: each pod's summary from the pod list, its events, then its logs.

        An events or log call that fails is recorded in the file and the index, and the
        summary stays. The summary and events are redacted too, as a termination message or
        a failed probe's output is container text. `closing` is a last line for the file.
        """
        self.note(
            name, f"{len(pods)} pod(s) summarised from the pod list, each with its events and logs"
        )
        sections: list[str] = []
        for pod in pods:
            pod_name = (pod.get("metadata") or {}).get("name") or ""
            selector = f"involvedObject.name={pod_name}"
            args = ["get", "events", "-n", namespace, "--field-selector", selector]
            args += [OLDEST_FIRST, "-o", "wide"]
            outcome = self._kubectl(args, limit=TEXT_BYTES, tail=False)
            self.runs += 1
            if outcome and outcome.ok:
                self.answered += 1
            how = verdict(outcome, self.budget)
            self.note(f"{name}, events of {pod_name}", how, args)
            events = outcome.output.decode("utf-8", errors="replace") if outcome else ""
            summary = "\n".join(pod_summary_lines(namespace, pod)) + f"\n  events: {how}\n{events}"
            sections.append(redact(summary) + "\n" + self.pod_logs(name, namespace, pod))
        if closing:
            sections.append(closing + "\n")
        self.write(name, "\n".join(sections).encode("utf-8"))


def _capture_logs(evidence: Evidence, directory: str, targets: list[dict]) -> None:
    """Each kube-system container's newest lines, and its previous run if it restarted."""
    bounds = [f"--tail={LOG_TAIL}", f"--limit-bytes={TEXT_BYTES}", "--timestamps"]
    for pod in targets:
        name = (pod.get("metadata") or {}).get("name") or ""
        for container, restarts in containers_of(pod):
            stem = f"{directory}/{safe_name(name)}.{safe_name(container)}"
            logs = ["-n", "kube-system", "logs", name, "-c", container, *bounds]
            evidence.capture(f"{stem}.log", logs, log=True)
            if restarts:
                evidence.capture(f"{stem}.previous.log", [*logs, "--previous"], log=True)


def _capture_load_balancer_controller(evidence: Evidence, pods: list[dict] | None) -> None:
    """The controller's webhook CA digests, then each controller container's newest lines."""
    key, value = CONTROLLER_LABEL
    selector = f"{key}={value}"
    webhook_file = f"{CONTROLLER_DIR}/webhook-ca.txt"
    configs = evidence.read(webhook_file, ["get", WEBHOOK_CONFIGS, "-l", selector])
    if configs:
        evidence.write(webhook_file, ("\n".join(webhook_ca_lines(configs)) + "\n").encode("utf-8"))
    elif configs is not None:
        evidence.note(webhook_file, f"no webhook configuration carries {selector}, nothing written")

    if pods is None:
        evidence.note(f"{CONTROLLER_DIR}/", "not run: the kube-system pods were not read")
        return
    targets = controller_pods(pods)[:MAX_CONTROLLER_PODS]
    if not targets:
        evidence.note(f"{CONTROLLER_DIR}/", f"not run: no kube-system pod carries {selector}")
        return
    _capture_logs(evidence, CONTROLLER_DIR, targets)


def _capture_aws_node(evidence: Evidence, targets: list[dict]) -> None:
    """Each aws-node container's newest lines, then each node's CNI log files."""
    _capture_logs(evidence, "aws-node", targets)
    for pod in targets:
        name = (pod.get("metadata") or {}).get("name") or ""
        node = (pod.get("spec") or {}).get("nodeName") or ""
        if not node:
            continue
        for log in CNI_LOGS:
            target = f"aws-node/{safe_name(node)}.{log}"
            kubelet = f"/api/v1/nodes/{node}/proxy/logs/aws-routed-eni/{log}"
            served = evidence.capture(target, ["get", "--raw", kubelet], tail=True, log=True)
            if served is None or served.ok:
                continue
            inside = ["exec", name, "-c", CNI_CONTAINER, "--", "cat", f"{CNI_LOG_MOUNT}/{log}"]
            evidence.capture(target, ["-n", "kube-system", *inside], tail=True, log=True)


def _capture_stuck(evidence: Evidence, covered: Collection[str] = ()) -> None:
    """A summary, events and logs of every stuck or not-Ready pod in any namespace.

    One file per namespace. Pods stuck on a wait reason or an unfinished init come first, then
    pods Running but not Ready, and MAX_STUCK pods are summarised in all; a not-Ready pod past
    that is named on a closing `+N more` line of the last file and in the index.
    `covered` names namespaces whose not-Ready pods another report already holds.
    """
    pods = evidence.read(
        "(the pods in every namespace the stuck pods are chosen from)",
        ["get", "pods", "-A"],
    )
    if pods is None:
        evidence.note("stuck-pods/", "not run: the pods in every namespace were not read")
        return
    stuck = stuck_pods(pods)
    held = set(stuck)
    unready = [key for key in running_unready_pods(pods, skip=covered) if key not in held]
    if not stuck and not unready:
        waits = ", ".join(sorted(STUCK_REASONS))
        evidence.note(
            "stuck-pods/",
            f"not run: no container is waiting on {waits}, no init is unfinished "
            "and no pod is Running but not Ready",
        )
        return
    if len(stuck) > MAX_STUCK:
        summarised = f"{len(stuck)} pods are stuck, and the first {MAX_STUCK} are summarised"
        evidence.note("stuck-pods/", summarised)
    room = max(0, MAX_STUCK - len(stuck))
    skipped = unready[room:]
    listed = {_identity(pod): pod for pod in pods}
    by_namespace: dict[str, list[dict]] = {}
    for key in [*stuck[:MAX_STUCK], *unready[:room]]:
        by_namespace.setdefault(key[0], []).append(listed[key])
    closing = ""
    if skipped:
        shown = ", ".join(f"{ns}/{name}" for ns, name in skipped[:10])
        more = f", and {len(skipped) - 10} more" if len(skipped) > 10 else ""
        closing = f"+{len(skipped)} more not-Ready pods, not summarised: {shown}{more}"
        evidence.note("stuck-pods/", closing)
    files = list(by_namespace.items())
    for number, (namespace, chosen) in enumerate(files):
        last = number == len(files) - 1
        evidence.pod_report(
            f"stuck-pods/{safe_name(namespace)}.txt", namespace, chosen, closing if last else ""
        )


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

    evidence.capture("pods-wide.txt", ["get", "pods", "-A", "-o", "wide"])
    # The newest warnings are the failed stage's own, so a list past the byte cap keeps its end.
    evidence.capture(
        "warning-events.txt",
        ["get", "events", "-A", "--field-selector", WARNINGS, OLDEST_FIRST, "-o", "wide"],
        tail=True,
    )
    evidence.capture(
        "kube-system-events.txt",
        ["-n", "kube-system", "get", "events", OLDEST_FIRST, "-o", "wide"],
    )
    # A load balancer that was never programmed shows as a Service with no address.
    evidence.capture("services-wide.txt", ["get", "services", "-A", "-o", "wide"])
    evidence.capture("gateways-wide.txt", ["get", GATEWAYS, "-A", "-o", "wide"])
    evidence.capture(
        "kube-system-certificates.txt", ["-n", "kube-system", "get", CERTIFICATES, "-o", "wide"]
    )

    unready_file = "kube-system-not-ready.txt"
    if pods is None:
        evidence.note(unready_file, "not run: the kube-system pods were not read")
    elif unready := not_ready_pods(pods):
        listed = {(pod.get("metadata") or {}).get("name") or "": pod for pod in pods}
        chosen = [listed[name] for name in unready[:MAX_UNREADY]]
        evidence.pod_report(unready_file, "kube-system", chosen)
    else:
        evidence.note(unready_file, "not run: every kube-system pod is Running and Ready")

    # The not-Ready kube-system pods are in kube-system-not-ready.txt whenever that list was read.
    _capture_stuck(evidence, covered=() if pods is None else ("kube-system",))
    _capture_load_balancer_controller(evidence, pods)

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
        help="READ-ONLY: write node conditions, every pod, Services and Gateways, Warning events, "
        "stuck pods and unready pods with their container logs, kube-system events, the load "
        "balancer controller's webhook CA digests and logs, and the aws-node and CNI logs to "
        "files, for after a cycle stage fails and before the destroy",
        description="Reads the cluster and writes one file per capture under --out: nodes -o "
        "wide, each node's conditions and allocatable, pods -A -o wide, every namespace's "
        "Warning events oldest first, kube-system events oldest first, services and Gateways -A "
        "-o wide, the cert-manager Certificates in kube-system, a summary, the events and the "
        "container logs of each kube-system pod not Running and Ready, the same for each pod in "
        "any namespace with a container waiting on ImagePullBackOff, ErrImagePull, "
        "CrashLoopBackOff or CreateContainerConfigError or an init container not yet finished, "
        "then each pod Running but not Ready, 40 pods in all (the previous run of a restarted "
        "container, a running or failed init container, a running container that is not "
        "ready), the AWS Load Balancer Controller's "
        "webhook configurations with each caBundle as a digest and its containers' newest log "
        "lines, the events of each node reporting "
        "InvalidDiskCapacity, every aws-node container's newest log lines, and the CNI's ipamd "
        "and plugin logs. A pod summary leaves out every container's env, args and command, the "
        "pod's volumes and its annotations. Every log is redacted before it is written: values "
        "under credential-named keys, URL userinfo, bearer tokens, private key blocks and common "
        "token shapes become ***REDACTED***. Every capture is bounded in time and bytes, one that "
        "fails is recorded in index.txt and the rest still run, and nothing reads a Secret. "
        "Writes nothing to the cluster; exits 0.",
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
