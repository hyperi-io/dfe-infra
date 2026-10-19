#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/dfe_ops_pod_logs.py
#  Purpose:      `dfe-ops pod-logs` -- every container's log in a namespace, followed for
#                as long as a cycle stage runs, so a pod the stage deletes or restarts
#                still leaves its log. Registered into dfe-ops the way
#                dfe_ops_diagnostics.py is.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-ops pod-logs -- the log of every container run in a namespace, written while it runs.

    dfe-ops pod-logs --out DIR --namespace NS [--namespace NS ...] [--kubeconfig F]
                     [--context C] [--server S] [--max-seconds 21600] [--stop-on-eof]

`dfe-ops acceptance --diagnostics-dir DIR` runs one on the app namespace from the stage's
start to its end, and keeps what it wrote only when the stage fails. The diagnostics bundle
read after a failure sees only the pods still alive, and by then a per-source fetcher or
transform the suite removed, or the crashed run of a container that restarted, is gone:

  index.txt                    every run followed, how its stream ended and what it kept
  <namespace>/<pod>.<container>.<start>.log
                               one container run's log, each line prefixed with its
                               timestamp. <start> is the run's own start in UTC, so a
                               container's runs sort in the order they ran

A run is followed when it is running, or ended after the follower started, so a container
that restarts leaves one file per run, the crashed one included. Each run is read from the
follower's start, less SINCE_MARGIN. A run keeps its first HEAD_BYTES as they arrive and its
newest TAIL_BYTES when it ends, with a line between them naming how much was left out. The
runs of one pod keep POD_BYTES between them, and the whole follower TOTAL_BYTES.

It reads through one `kubectl proxy` on a Unix socket in a directory only this user can
open, whose filter admits a GET of the named namespaces' pod lists and pod logs and nothing
else, so no Secret is reachable through it. A watch finds each container run and each run
is one log stream, every line of it passed through the failure-evidence redactor before it
is written. The pod objects the watch returns are held in memory and never written.

It stops at the end of standard input with --stop-on-eof, which is how the process that
started it stops it, by closing the pipe or by exiting however it exits. It also stops on
SIGINT, SIGTERM or SIGHUP, when the proxy exits, or after --max-seconds, and every stream
still open writes its tail first. It exits 0 whatever it finds.
"""

import argparse
import contextlib
import http.client
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import dfe_ops_failure_evidence as fe
from dfe_ops_readiness_report import kubectl_prefix
from dfe_ops_redact import redact_stream

HEAD_BYTES = 1024 * 1024  # bytes of a run kept from its start, written as they arrive
TAIL_BYTES = 1024 * 1024  # newest bytes of a run kept past its head, written when it ends
MARKER_ROOM = 256  # bytes the line between a run's head and tail may take
RUN_BYTES = HEAD_BYTES + TAIL_BYTES + MARKER_ROOM
POD_BYTES = 8 * 1024 * 1024  # bytes every run of one pod may keep together
TOTAL_BYTES = 128 * 1024 * 1024  # bytes one follower may keep
MAX_STREAMS = 64  # log streams one follower holds open at once
MAX_SECONDS = 6 * 3600.0  # a follower stops itself after this, whoever owns it
# Seconds before the start the first read reaches back, for clock skew between here and the nodes.
SINCE_MARGIN = 60
WATCH_SECONDS = 300  # the API server ends each watch after this, and the reconnect lists every pod
WATCH_PAUSE = 1.0  # seconds between a watch that ended cleanly and the next
MAX_WATCH_FAILURES = 5  # failed watches in a row before a namespace's watch gives up
# etcd caps one object at 1.5 MiB, so one watch event always fits.
WATCH_LINE = 4 * 1024 * 1024
ERROR_BODY = 4096  # bytes of a refusal's body read for its message
PROXY_READY_TIMEOUT = 30.0  # seconds for the proxy to open its socket
PROXY_STOP = 5.0  # seconds the proxy gets to exit before it is killed
JOIN_TIMEOUT = 10.0  # seconds every stream together gets to write its tail at the stop
FOLLOWER_STOP = 30.0  # seconds an owner waits for the follower before it is signalled
FLUSH_SECONDS = 1.0  # a run's file is flushed at most this often
POLL = 1.0
# The proxy's default --accept-hosts admits localhost.
HEADERS = {"Host": "localhost"}
REJECT_METHODS = "POST,PUT,PATCH,DELETE"
NAMESPACE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
SOCKET = "proxy.sock"


def rfc3339(moment: datetime) -> str:
    """`moment` in UTC to the second, the form the pod-log API reads sinceTime in."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def accept_paths(namespaces: list[str]) -> str:
    """The proxy's --accept-paths: each namespace's pod list and watch, and each pod's log.

    Every name is a DNS label, so none carries a character the expression reads.
    """
    return rf"^/api/v1/namespaces/(?:{'|'.join(namespaces)})/pods(?:/[^/]+/log)?$"


def _when(stamp: object) -> datetime | None:
    """An API timestamp as an aware datetime, or None when it is missing or unreadable."""
    try:
        return datetime.fromisoformat(str(stamp)) if stamp else None
    except ValueError:
        return None


# --- which runs to read -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Run:
    """One run of one container, which one log stream reads.

    `previous` is the run before the container's current one, which the API serves with
    previous=true.
    """

    namespace: str
    pod: str
    container: str
    container_id: str
    started: str
    previous: bool


def _ended_since(state: dict, since: datetime) -> bool:
    """Whether a terminated state ended at or after `since`. One with no readable end counts."""
    finished = _when(state.get("finishedAt"))
    return finished is None or finished >= since


def _container_runs(namespace: str, pod: str, status: dict, since: datetime) -> list[Run]:
    """The runs of one container that were alive at or after `since`, the earlier first."""
    container = str(status.get("name") or "")
    if not container:
        return []
    runs = []
    last = (status.get("lastState") or {}).get("terminated") or {}
    if last.get("containerID") and _ended_since(last, since):
        started = str(last.get("startedAt") or "")
        runs.append(Run(namespace, pod, container, str(last["containerID"]), started, True))
    state = status.get("state") or {}
    running, ended = state.get("running"), state.get("terminated")
    current, started = "", ""
    if running is not None:
        current, started = str(status.get("containerID") or ""), str(running.get("startedAt") or "")
    elif ended is not None and _ended_since(ended, since):
        current = str(ended.get("containerID") or status.get("containerID") or "")
        started = str(ended.get("startedAt") or "")
    if current:
        runs.append(Run(namespace, pod, container, current, started, False))
    return runs


def runs_of(pod: dict, since: datetime) -> list[Run]:
    """Every init and main container run in `pod` running, or ended at or after `since`.

    A container waiting to start has no current run, and a run is known by its container
    id, so the same run seen in two pod events is one Run.
    """
    meta, status = pod.get("metadata") or {}, pod.get("status") or {}
    namespace, name = str(meta.get("namespace") or ""), str(meta.get("name") or "")
    if not (namespace and name):
        return []
    found: list[Run] = []
    for kind in ("initContainerStatuses", "containerStatuses"):
        for container in status.get(kind) or []:
            found += _container_runs(namespace, name, container, since)
    return found


def run_stamp(run: Run) -> str:
    """Where a run sorts among its container's runs: its start as UTC `YYYYMMDDTHHMMSSZ`.

    A run with no readable start goes by the first 12 characters of its container id.
    """
    started = _when(run.started)
    if started is not None:
        return started.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return fe.safe_name(run.container_id.rpartition("/")[2][:12])


def run_path(run: Run) -> Path:
    """A run's file under the follower's directory: <namespace>/<pod>.<container>.<start>.log."""
    name = f"{fe.safe_name(run.pod)}.{fe.safe_name(run.container)}.{run_stamp(run)}.log"
    return Path(fe.safe_name(run.namespace)) / name


def log_query(run: Run, since: datetime) -> str:
    """The pod-log request for one run: timestamped, from `since`, followed unless previous."""
    params = {"container": run.container, "timestamps": "true", "sinceTime": rfc3339(since)}
    params["previous" if run.previous else "follow"] = "true"
    pod = urllib.parse.quote(run.pod, safe="")
    return f"/api/v1/namespaces/{run.namespace}/pods/{pod}/log?{urllib.parse.urlencode(params)}"


# --- one run's file ---------------------------------------------------------------------


class RunLog:
    """One run's file: its first `head` bytes written as they arrive, its newest `tail` at the end.

    No file exists until the first byte, so a run that printed nothing leaves none.

    Attributes:
        path: Where the file goes.
        kept: Bytes the file holds.
        dropped: Bytes past the head that fell out of the tail.
    """

    def __init__(self, path: Path, head: int = HEAD_BYTES, tail: int = TAIL_BYTES) -> None:
        self.path = path
        self.kept = 0
        self.dropped = 0
        self._head = head
        self._head_left = head
        self._tail_limit = tail
        self._tail: deque[bytes] = deque()
        self._tail_bytes = 0
        self._tailing = False
        self._handle: BinaryIO | None = None
        self._flushed = time.monotonic()

    def _file(self) -> BinaryIO:
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("ab")
        return self._handle

    def write(self, data: bytes) -> None:
        """Keep one line: in the head while it has room, else in the tail, the oldest dropped."""
        if not self._tailing and len(data) <= self._head_left:
            self._file().write(data)
            self._head_left -= len(data)
            self.kept += len(data)
            return
        # Once one line misses the head every later one goes to the tail, so the order holds.
        self._tailing = True
        self._tail.append(data)
        self._tail_bytes += len(data)
        while self._tail_bytes > self._tail_limit:
            gone = self._tail.popleft()
            self._tail_bytes -= len(gone)
            self.dropped += len(gone)

    def flush(self) -> None:
        """Flush at most once a FLUSH_SECONDS, so a busy container costs no syscall a line."""
        now = time.monotonic()
        if self._handle is not None and now - self._flushed >= FLUSH_SECONDS:
            self._handle.flush()
            self._flushed = now

    def close(self) -> int:
        """Write the tail after a line naming what fell out, close the file, and return `kept`."""
        if self._tail:
            handle = self._file()
            if self.dropped:
                head = self._head
                marker = f"*** {self.dropped} bytes past this run's first {head} not kept ***\n"
                handle.write(marker.encode())
                self.kept += len(marker)
            for chunk in self._tail:
                handle.write(chunk)
                self.kept += len(chunk)
            self._tail.clear()
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        return self.kept


# --- the follower -------------------------------------------------------------------------


class _UnixConnection(http.client.HTTPConnection):
    """HTTP to the kubectl proxy's Unix socket."""

    def __init__(self, path: str) -> None:
        super().__init__("localhost")
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def _refusal(response: http.client.HTTPResponse) -> str:
    """`HTTP <status>: <message>` for an answer that was not 200, the API's own message if any."""
    body = response.read(ERROR_BODY)
    try:
        message = str(json.loads(body).get("message") or "")
    except (ValueError, AttributeError):
        message = body.decode("utf-8", errors="replace").strip()
    return f"HTTP {response.status}: {' '.join((message or response.reason).split())}"


class Follower:
    """The watches and log streams of one `dfe-ops pod-logs` run, over one proxy socket.

    Attributes:
        out: The directory every file goes under.
        socket_path: The proxy's socket.
        since: The earliest moment any run is read from.
        stop: Set once the follower is stopping.
        why: The first reason it was given to stop.
        runs: Runs followed.
        files: Runs that kept at least one byte.
        kept: Bytes kept across every run.
    """

    def __init__(self, out: Path, socket_path: Path, since: datetime) -> None:
        self.out = out
        self.socket_path = socket_path
        self.since = since
        self.stop = threading.Event()
        self.why = ""
        self.runs = 0
        self.files = 0
        self.kept = 0
        self._lock = threading.Lock()
        self._claimed: set[str] = set()
        self._skipped: set[str] = set()
        self._paths: set[Path] = set()
        self._pod_bytes: dict[tuple[str, str], int] = {}
        self._total = 0
        self._open = 0
        self._threads: list[threading.Thread] = []
        self._socks: set[socket.socket] = set()

    def stop_with(self, why: str) -> None:
        """Stop, keeping the first reason given. Takes no lock, so a signal handler can call it."""
        if not self.why:
            self.why = why
        self.stop.set()

    def note(self, line: str) -> None:
        """One line in index.txt, redacted."""
        with self._lock, (self.out / fe.INDEX).open("a", encoding="utf-8", newline="\n") as index:
            index.write(fe.redact(line) + "\n")

    def spawn(self, target: Callable[..., None], *args: object) -> None:
        """Run `target(*args)` on a daemon thread the stop waits for."""
        thread = threading.Thread(target=target, args=args, daemon=True)
        with self._lock:
            self._threads.append(thread)
        thread.start()

    def claim(self, run: Run) -> Path | None:
        """Reserve a stream and RUN_BYTES for `run`, or None when it is followed or does not fit.

        A run that does not fit is named in the index once and left unclaimed, so the next
        listing of its pod tries it again.
        """
        label = f"{run.namespace}/{run.pod} {run.container} run {run_stamp(run)}"
        pod = (run.namespace, run.pod)
        why, fresh, path = "", False, run_path(run)
        with self._lock:
            if run.container_id in self._claimed:
                return None
            if self._open >= MAX_STREAMS:
                why = f"{MAX_STREAMS} streams are already open"
            elif self._pod_bytes.get(pod, 0) + RUN_BYTES > POD_BYTES:
                why = f"its pod's {POD_BYTES} bytes are spent"
            elif self._total + RUN_BYTES > TOTAL_BYTES:
                why = f"the follower's {TOTAL_BYTES} bytes are spent"
            if why:
                fresh = run.container_id not in self._skipped
                self._skipped.add(run.container_id)
            else:
                self._claimed.add(run.container_id)
                self._pod_bytes[pod] = self._pod_bytes.get(pod, 0) + RUN_BYTES
                self._total += RUN_BYTES
                self._open += 1
                self.runs += 1
                if path in self._paths:
                    path = path.with_name(f"{path.stem}-{fe.safe_name(run.container_id[-12:])}.log")
                self._paths.add(path)
        if why:
            if fresh:
                self.note(f"{label}: not followed: {why}")
            return None
        return path

    def release(self, run: Run, kept: int) -> None:
        """Hand back what a finished run reserved past the bytes it kept."""
        pod = (run.namespace, run.pod)
        with self._lock:
            self._pod_bytes[pod] -= RUN_BYTES - kept
            self._total -= RUN_BYTES - kept
            self._open -= 1
            self.kept += kept
            self.files += 1 if kept else 0

    def _connect(self) -> _UnixConnection:
        conn = _UnixConnection(str(self.socket_path))
        conn.connect()
        with self._lock:
            if self.stop.is_set():
                conn.close()
                raise ConnectionAbortedError("the follower is stopping")
            if conn.sock is not None:
                self._socks.add(conn.sock)
        return conn

    def _drop(self, conn: _UnixConnection, sock: socket.socket | None) -> None:
        with self._lock:
            self._socks.discard(sock)
        conn.close()

    def on_pod(self, pod: dict) -> None:
        """Start a stream for each run of `pod` that has none."""
        for run in runs_of(pod, self.since):
            if self.stop.is_set():
                return
            path = self.claim(run)
            if path is not None:
                self.spawn(self.read, run, path)

    def read(self, run: Run, path: Path) -> None:
        """Stream one run into its file until the run ends or the follower stops."""
        log = RunLog(self.out / path)
        how = "the stream ended"
        conn, sock = None, None
        try:
            conn = self._connect()
            sock = conn.sock
            conn.request("GET", log_query(run, self.since), headers=HEADERS)
            response = conn.getresponse()
            if response.status != 200:
                how = f"refused: {_refusal(response)}"
            else:
                redact_stream(response, log)
                if self.stop.is_set():
                    how = "still open when the follower stopped"
        except (OSError, http.client.HTTPException, ValueError) as exc:
            stopped = self.stop.is_set()
            how = "still open when the follower stopped" if stopped else f"broke: {exc!r}"
        finally:
            try:
                kept = log.close()
            except OSError as exc:  # a full disk ends this file, never the follower
                how, kept = f"{how}, then its file could not be finished: {exc}", log.kept
            if conn is not None:
                self._drop(conn, sock)
            self.release(run, kept)
        lost = f", {log.dropped} bytes past its head not kept" if log.dropped else ""
        if kept:
            self.note(f"{path.as_posix()}: {how}, {kept} bytes kept{lost}")
        else:
            self.note(f"{path.as_posix()}: {how}, nothing since {rfc3339(self.since)}, no file")

    def _watch_once(self, namespace: str) -> str:
        """One watch of `namespace`'s pods, fed to on_pod: empty when it ended cleanly, else why."""
        conn, sock = None, None
        query = urllib.parse.urlencode({"watch": "true", "timeoutSeconds": WATCH_SECONDS})
        try:
            conn = self._connect()
            sock = conn.sock
            conn.request("GET", f"/api/v1/namespaces/{namespace}/pods?{query}", headers=HEADERS)
            response = conn.getresponse()
            if response.status != 200:
                return f"refused: {_refusal(response)}"
            while line := response.readline(WATCH_LINE):
                event = json.loads(line)
                kind, item = event.get("type"), event.get("object") or {}
                if kind == "ERROR":
                    return f"ended on an error: {item.get('message') or item}"
                if kind in ("ADDED", "MODIFIED"):
                    self.on_pod(item)
            return ""
        except (OSError, http.client.HTTPException, ValueError) as exc:
            return "" if self.stop.is_set() else f"broke: {exc!r}"
        finally:
            if conn is not None:
                self._drop(conn, sock)

    def watch(self, namespace: str) -> None:
        """Watch `namespace`'s pods until the follower stops, reconnecting as each watch ends.

        Each reconnect lists every pod again, which also retries a run that did not fit.
        """
        failures = 0
        while not self.stop.is_set():
            why = self._watch_once(namespace)
            if self.stop.is_set():
                return
            failures = failures + 1 if why else 0
            if why:
                self.note(f"watch {namespace}: {why}")
            if failures >= MAX_WATCH_FAILURES:
                self.note(f"watch {namespace}: given up after {failures} failures in a row")
                return
            self.stop.wait(min(2.0**failures, 30.0) if failures else WATCH_PAUSE)

    def shutdown(self) -> int:
        """Stop every watch and stream and wait JOIN_TIMEOUT for them to write their tails.

        Returns:
            How many were still running when the wait ended.
        """
        self.stop.set()
        with self._lock:
            socks = list(self._socks)
            threads = list(self._threads)
        for sock in socks:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        deadline = time.monotonic() + JOIN_TIMEOUT
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return sum(thread.is_alive() for thread in threads)


# --- the proxy and the run ----------------------------------------------------------------


def proxy_argv(args: argparse.Namespace, socket_path: Path, namespaces: list[str]) -> list[str]:
    """`kubectl proxy` on `socket_path`, admitting a GET of pods and pod logs in `namespaces`."""
    server = ["--server", args.server] if args.server else []
    return [
        "kubectl", *kubectl_prefix(args), *server, "proxy", f"--unix-socket={socket_path}",
        f"--accept-paths={accept_paths(namespaces)}", f"--reject-methods={REJECT_METHODS}",
    ]


def _wait_ready(follower: Follower, proxy: subprocess.Popen) -> str:
    """Empty once the proxy answers on its socket, else why it never did."""
    give_up = time.monotonic() + PROXY_READY_TIMEOUT
    while time.monotonic() < give_up:
        if follower.stop.is_set():
            return follower.why
        if proxy.poll() is not None:
            return f"the kubectl proxy exited {proxy.returncode} before it served"
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(follower.socket_path))
            return ""
        except OSError:
            follower.stop.wait(0.1)
        finally:
            probe.close()
    return f"the kubectl proxy did not serve within {PROXY_READY_TIMEOUT:g}s"


def _stop_proxy(proxy: subprocess.Popen | None) -> None:
    if proxy is None or proxy.poll() is not None:
        return
    proxy.terminate()
    try:
        proxy.wait(PROXY_STOP)
    except subprocess.TimeoutExpired:
        proxy.kill()
        proxy.wait()


def _follow(
    follower: Follower, proxy: subprocess.Popen, namespaces: list[str], limit: float
) -> None:
    """Watch every namespace until the follower is stopped, the proxy exits or `limit` passes."""
    failed = _wait_ready(follower, proxy)
    if failed:
        follower.stop_with(failed)
        return
    for namespace in namespaces:
        follower.spawn(follower.watch, namespace)
    deadline = time.monotonic() + limit
    while not follower.stop.wait(POLL):
        if proxy.poll() is not None:
            follower.stop_with(f"the kubectl proxy exited {proxy.returncode}")
        elif time.monotonic() >= deadline:
            follower.stop_with(f"--max-seconds {limit:g} passed")


def _stop_at_eof(follower: Follower) -> None:
    """Stop the follower once standard input ends: its owner closed the pipe, or exited.

    It reads the descriptor, not sys.stdin, whose buffer lock a thread still blocked in a read
    holds when the interpreter shuts down, which aborts it.
    """
    with contextlib.suppress(OSError, ValueError):
        while os.read(sys.stdin.fileno(), 4096):
            pass
    follower.stop_with("its owner closed standard input")


def _stop_on_signals(follower: Follower) -> None:
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, lambda _signum, _frame, name=name: follower.stop_with(name))


def cmd_pod_logs(args: argparse.Namespace) -> int:
    """`dfe-ops pod-logs`: follow every container run in the namespaces until stopped. Exits 0."""
    namespaces = list(dict.fromkeys(args.namespace))
    bad = [name for name in namespaces if not NAMESPACE.fullmatch(name)]
    if bad:
        print(f"pod-logs: not a namespace name: {', '.join(bad)}", file=sys.stderr)
        return 2
    out = Path(args.out)
    since = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=SINCE_MARGIN)
    named = ", ".join(namespaces)
    try:
        out.mkdir(parents=True, exist_ok=True)
        header = f"pod logs of {named}: every container run alive from {rfc3339(since)}\n"
        (out / fe.INDEX).write_text(header, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"pod-logs: skipped: {out} cannot be written: {exc}", file=sys.stderr)
        return 0
    work = Path(tempfile.mkdtemp(prefix="dfe-pod-logs-"))
    follower = Follower(out, work / SOCKET, since)
    if not hasattr(socket, "AF_UNIX"):
        follower.note("not run: this platform has no Unix socket for the kubectl proxy")
        shutil.rmtree(work, ignore_errors=True)
        return 0
    _stop_on_signals(follower)
    if args.stop_on_eof:
        threading.Thread(target=_stop_at_eof, args=(follower,), daemon=True).start()
    print(f"pod-logs: following {named} into {out}", file=sys.stderr)
    argv = proxy_argv(args, follower.socket_path, namespaces)
    follower.note(f"(the proxy): kubectl {shlex.join(argv[1:])}")
    proxy = None
    try:
        proxy = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        _follow(follower, proxy, namespaces, args.max_seconds)
    except OSError as exc:
        follower.stop_with(f"could not start kubectl: {exc.strerror or exc}")
    finally:
        still = follower.shutdown()
        _stop_proxy(proxy)
        shutil.rmtree(work, ignore_errors=True)
    open_at_stop = f", {still} stream(s) still open after {JOIN_TIMEOUT:g}s" if still else ""
    summary = (
        f"stopped {rfc3339(datetime.now(UTC))}: {follower.why}. {follower.runs} run(s) followed, "
        f"{follower.files} file(s), {follower.kept} bytes kept{open_at_stop}"
    )
    follower.note(summary)
    print(f"pod-logs: {summary}, under {out}", file=sys.stderr)
    return 0


# --- the stage that owns one --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Owned:
    """A follower process a stage started, and the directory it writes."""

    proc: subprocess.Popen
    out: Path


def stage_dir(root: str, label: str) -> Path:
    """The directory a stage's follower writes under `root`: <UTC time>-<label>-pod-logs."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return Path(root) / f"{stamp}-{fe.safe_name(label)}-pod-logs"


def spawn(self_cmd: list[str], out: Path, namespaces: list[str], kube: list[str]) -> Owned | None:
    """Start `dfe-ops pod-logs` writing `out`, which finish() stops by closing its input.

    It stays in this process group, so whatever stops the stage's group stops it too.

    Args:
        self_cmd: The argv that runs dfe-ops.
        out: The directory it writes.
        namespaces: The namespaces it follows.
        kube: --kubeconfig, --context and --server, aimed at the stage's cluster.

    Returns:
        The process, or None when it could not start, which is said on stderr.
    """
    argv = [*self_cmd, "pod-logs", "--out", str(out), "--stop-on-eof"]
    for namespace in namespaces:
        argv += ["--namespace", namespace]
    try:
        proc = subprocess.Popen([*argv, *kube], stdin=subprocess.PIPE)
    except OSError as exc:
        print(f"  [warn] dfe-ops pod-logs could not start: {exc.strerror or exc}", file=sys.stderr)
        return None
    return Owned(proc, out)


def finish(owned: Owned, *, keep: bool) -> None:
    """Close the follower's input, wait for it to write its tails, and remove `out` unless `keep`.

    A follower that has not exited after FOLLOWER_STOP is sent SIGTERM, then killed.
    """
    proc = owned.proc
    with contextlib.suppress(OSError):
        if proc.stdin is not None:
            proc.stdin.close()
    try:
        proc.wait(FOLLOWER_STOP)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(PROXY_STOP)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if not keep:
        shutil.rmtree(owned.out, ignore_errors=True)


def _seconds(raw: str) -> float:
    """An argparse type: seconds, more than zero."""
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{raw} is not more than zero")
    return value


def add_pod_logs_subparser(sub: argparse._SubParsersAction) -> None:
    """Register `dfe-ops pod-logs`."""
    pl = sub.add_parser(
        "pod-logs",
        help="follow every container's log in a namespace into one file per container run, "
        "until stopped, so a pod deleted or restarted meanwhile still leaves its log",
        description="Writes OUT/<namespace>/<pod>.<container>.<run start>.log for every container "
        "run that is running, or ends, while it follows, and OUT/index.txt saying how each "
        "stream ended. A run keeps its first and newest MiB, a pod 8 MiB and the follower 128 "
        "MiB. Reads through a kubectl proxy on a private Unix socket whose filter admits a GET "
        "of the named namespaces' pods and pod logs and nothing else, and redacts every line. "
        "Stops at the end of standard input with --stop-on-eof, on SIGINT, SIGTERM or SIGHUP, "
        "when the proxy exits, or at --max-seconds. Reads no Secret. Exits 0.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    pl.add_argument("--out", required=True, help="directory the logs and index.txt go in")
    pl.add_argument(
        "--namespace", action="append", required=True, help="namespace to follow (repeatable)"
    )
    pl.add_argument("--kubeconfig", default="", help="kubeconfig to read, empty for kubectl's own")
    pl.add_argument(
        "--context", default="", help="kubeconfig context to read, empty for its current one"
    )
    pl.add_argument(
        "--server",
        default="",
        help="API server address, for a kubeconfig whose server name does not resolve here",
    )
    pl.add_argument(
        "--max-seconds", type=_seconds, default=MAX_SECONDS, help="seconds before it stops itself"
    )
    pl.add_argument(
        "--stop-on-eof",
        action="store_true",
        help="stop at the end of standard input, so the process that started it stops it by "
        "closing the pipe, or by exiting",
    )
    # Its output lands in the stage's log, and a banner costs time at the stage's start.
    pl.set_defaults(func=cmd_pod_logs, banner=False)
