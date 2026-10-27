#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_pod_logs.py
#  Purpose:      Guard `dfe-ops pod-logs` and the acceptance stage that owns one: which
#                container runs it reads, the file each lands in, the byte caps, that a
#                pod deleted or a container restarted mid-stage still leaves its log,
#                that every way it is stopped leaves no process behind, that no path
#                but pods and pod logs is asked for, and that a passing stage keeps
#                nothing.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe_ops_pod_logs.py and `dfe-ops acceptance --diagnostics-dir`.

    python3 -m pytest scripts/tests/test_dfe_ops_pod_logs.py -q

The lifecycle tests run the real `dfe-ops pod-logs` as a child process against a fake
`kubectl` first on PATH, whose `proxy` serves scripted pod events and logs over the Unix
socket it is given and records every request. No cluster is involved.
"""

import argparse
import importlib.machinery
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import dfe_ops_failure_evidence as fe  # noqa: E402
import dfe_ops_pod_logs as pl  # noqa: E402

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_pod_logs", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_pod_logs", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_pod_logs"] = dfeops
_loader.exec_module(dfeops)


@pytest.fixture(autouse=True)
def _engine_checkout_is_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about what the stage does after the checkout gates, which have their own tests."""
    monkeypatch.setattr(dfeops, "_checkout_gate", lambda *_args: 0)

SELF_CMD = [sys.executable, str(SCRIPT)]
NAMESPACE = "dfe"
# Credential values planted in the logs. None may reach a file.
PLANTED = ("hunter2-not-evidence", "abc.def.ghi123456", "AKIAABCDEFGHIJKLMNOP")
WAIT = 20.0  # seconds a lifecycle test waits for the follower to reach a state


def _ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(moment: str) -> str:
    return datetime.fromisoformat(moment).strftime("%Y%m%dT%H%M%SZ")


def _pod(name: str, *, init: list[dict] | None = None, main: list[dict] | None = None) -> dict:
    """A pod as the API returns it: name, namespace and container statuses."""
    return {
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": {"containers": [{"name": "c", "env": [{"name": "TOKEN", "value": PLANTED[1]}]}]},
        "status": {"initContainerStatuses": init or [], "containerStatuses": main or []},
    }


def _running(name: str, cid: str, started: str, *, last: dict | None = None) -> dict:
    status = {"name": name, "containerID": cid, "state": {"running": {"startedAt": started}}}
    status["lastState"] = {"terminated": last} if last else {}
    return status


def _ended(cid: str, started: str, finished: str) -> dict:
    return {"containerID": cid, "startedAt": started, "finishedAt": finished, "exitCode": 1}


# --- which runs it reads -----------------------------------------------------------------

SINCE = datetime.now(UTC) - timedelta(seconds=60)


def test_a_restarted_container_is_its_crashed_run_then_its_current_one() -> None:
    crashed = _ended("containerd://aaa", _ago(20), _ago(15))
    pod = _pod("p", main=[_running("app", "containerd://bbb", _ago(5), last=crashed)])
    runs = pl.runs_of(pod, SINCE)
    assert [(run.container_id, run.previous) for run in runs] == [
        ("containerd://aaa", True),
        ("containerd://bbb", False),
    ]
    assert {(run.namespace, run.pod, run.container) for run in runs} == {(NAMESPACE, "p", "app")}


def test_a_run_that_ended_before_the_follower_started_is_not_read() -> None:
    old = _ended("containerd://old", _ago(4000), _ago(3600))
    init = [{"name": "wait-for-engine", "state": {"terminated": old}, "lastState": {}}]
    pod = _pod("p", init=init, main=[_running("app", "containerd://new", _ago(3500), last=old)])
    assert [run.container_id for run in pl.runs_of(pod, SINCE)] == ["containerd://new"]


def test_a_container_terminated_in_the_window_is_read_and_a_waiting_one_is_not() -> None:
    ended = _ended("containerd://done", _ago(30), _ago(10))
    init = [{"name": "wait", "state": {"terminated": ended}, "lastState": {}}]
    waiting = {"name": "app", "state": {"waiting": {"reason": "ContainerCreating"}}}
    runs = pl.runs_of(_pod("p", init=init, main=[waiting]), SINCE)
    assert [(run.container, run.container_id, run.previous) for run in runs] == [
        ("wait", "containerd://done", False)
    ]


def test_a_crash_loop_in_backoff_leaves_its_last_run_to_read() -> None:
    crashed = _ended("containerd://ccc", _ago(20), _ago(5))
    waiting = {
        "name": "app",
        "containerID": "containerd://ccc",
        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
        "lastState": {"terminated": crashed},
    }
    runs = pl.runs_of(_pod("p", main=[waiting]), SINCE)
    assert [(run.container_id, run.previous) for run in runs] == [("containerd://ccc", True)]


def test_a_pod_with_no_name_or_no_status_has_no_run() -> None:
    assert pl.runs_of({"metadata": {"namespace": NAMESPACE}}, SINCE) == []
    assert pl.runs_of({"metadata": {"name": "p", "namespace": NAMESPACE}}, SINCE) == []
    no_id = {"name": "app", "state": {"running": {"startedAt": _ago(5)}}}
    assert pl.runs_of(_pod("p", main=[no_id]), SINCE) == []


def test_a_runs_file_is_named_by_pod_container_and_its_own_start() -> None:
    run = pl.Run(NAMESPACE, "dfe-fetcher-x-1", "dfe-fetcher", "containerd://0123456789abcdef",
                 "2026-10-18T22:48:03Z", False)
    assert pl.run_path(run) == Path(NAMESPACE) / "dfe-fetcher-x-1.dfe-fetcher.20261018T224803Z.log"
    unstarted = pl.Run(NAMESPACE, "p", "c", "containerd://0123456789abcdef", "", True)
    assert pl.run_path(unstarted).name == "p.c.0123456789ab.log"


def test_the_log_request_follows_a_current_run_and_reads_a_previous_one_whole() -> None:
    since = datetime(2026, 10, 18, 22, 0, 0, tzinfo=UTC)
    current = pl.Run(NAMESPACE, "p", "app", "x", "", False)
    query = pl.log_query(current, since)
    assert query.startswith(f"/api/v1/namespaces/{NAMESPACE}/pods/p/log?")
    assert "follow=true" in query
    assert "previous" not in query
    assert "sinceTime=2026-10-18T22%3A00%3A00Z" in query
    assert "timestamps=true" in query
    previous = pl.log_query(pl.Run(NAMESPACE, "p", "app", "x", "", True), since)
    assert "previous=true" in previous
    assert "follow" not in previous


def test_the_proxy_admits_only_pod_lists_and_pod_logs_in_the_named_namespaces() -> None:
    accept = re.compile(pl.accept_paths(["dfe", "dfe-test"]))
    for path in (
        "/api/v1/namespaces/dfe/pods",
        "/api/v1/namespaces/dfe-test/pods",
        "/api/v1/namespaces/dfe/pods/dfe-loader-1/log",
    ):
        assert accept.search(path), path
    for path in (
        "/api/v1/namespaces/dfe/secrets",
        "/api/v1/namespaces/dfe/secrets/dfe-kafka-user",
        "/api/v1/namespaces/dfe/configmaps",
        "/api/v1/namespaces/kube-system/pods",
        "/api/v1/namespaces/dfe-other/pods",
        "/api/v1/namespaces/dfe/pods/p/exec",
        "/api/v1/namespaces/dfe/pods/p/log/x",
        "/api/v1/pods",
        "/apis/apps/v1/namespaces/dfe/deployments",
    ):
        assert not accept.search(path), path


# --- one run's file and the caps --------------------------------------------------------


def test_a_run_keeps_its_head_and_its_newest_tail_with_a_line_between(tmp_path: Path) -> None:
    log = pl.RunLog(tmp_path / "ns" / "p.c.1.log", head=10, tail=10)
    for line in (b"aaaa\n", b"bbbb\n", b"cccc\n", b"dddd\n", b"eeee\n"):
        log.write(line)
    kept = log.close()
    text = (tmp_path / "ns" / "p.c.1.log").read_text(encoding="utf-8")
    assert text == "aaaa\nbbbb\n*** 5 bytes past this run's first 10 not kept ***\ndddd\neeee\n"
    assert kept == len(text.encode())
    assert log.dropped == 5


def test_a_run_that_printed_nothing_leaves_no_file(tmp_path: Path) -> None:
    log = pl.RunLog(tmp_path / "ns" / "p.c.1.log")
    assert log.close() == 0
    assert not (tmp_path / "ns").exists()


def test_a_run_under_its_head_is_kept_whole(tmp_path: Path) -> None:
    log = pl.RunLog(tmp_path / "p.c.1.log", head=100, tail=100)
    log.write(b"one\n")
    log.write(b"two\n")
    assert log.close() == 8
    assert (tmp_path / "p.c.1.log").read_bytes() == b"one\ntwo\n"


def _follower(tmp_path: Path) -> pl.Follower:
    out = tmp_path / "out"
    out.mkdir()
    return pl.Follower(out, tmp_path / "no.sock", SINCE)


def _run(pod: str, cid: str) -> pl.Run:
    return pl.Run(NAMESPACE, pod, "app", cid, _ago(5), False)


def test_a_pod_past_its_byte_budget_is_named_once_and_tried_again_once_it_frees(
    tmp_path: Path,
) -> None:
    follower = _follower(tmp_path)
    fits = pl.POD_BYTES // pl.RUN_BYTES
    claimed = [follower.claim(_run("p", f"id-{n}")) for n in range(fits)]
    assert all(claimed)
    refused = _run("p", "id-over")
    assert follower.claim(refused) is None
    assert follower.claim(refused) is None
    index = (follower.out / fe.INDEX).read_text(encoding="utf-8")
    assert index.count("not followed: its pod's") == 1
    # Another pod has its own budget.
    assert follower.claim(_run("q", "id-q")) is not None
    # A run that kept little hands the rest back, so the refused one now fits.
    follower.release(_run("p", "id-0"), 100)
    assert follower.claim(refused) is not None


def test_a_run_already_followed_is_not_followed_twice(tmp_path: Path) -> None:
    follower = _follower(tmp_path)
    assert follower.claim(_run("p", "id-1")) is not None
    assert follower.claim(_run("p", "id-1")) is None
    assert follower.runs == 1


def test_no_more_than_max_streams_are_open_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pl, "MAX_STREAMS", 2)
    follower = _follower(tmp_path)
    assert follower.claim(_run("a", "1")) is not None
    assert follower.claim(_run("b", "2")) is not None
    assert follower.claim(_run("c", "3")) is None
    assert "2 streams are already open" in (follower.out / fe.INDEX).read_text(encoding="utf-8")
    follower.release(_run("a", "1"), 0)
    assert follower.claim(_run("c", "3")) is not None


def test_two_runs_with_the_same_name_get_two_files(tmp_path: Path) -> None:
    follower = _follower(tmp_path)
    first = follower.claim(pl.Run(NAMESPACE, "p", "c", "containerd://aaaaaaaaaaaa1", "", False))
    second = follower.claim(pl.Run(NAMESPACE, "p", "c", "containerd://aaaaaaaaaaaa2", "", False))
    assert first is not None
    assert second is not None
    assert first != second


# --- the follower, end to end through a fake kubectl proxy -------------------------------

FAKE_KUBECTL = r"""#!{python}
import http.server, json, os, re, socketserver, sys, time, urllib.parse

args = sys.argv[1:]

def record(entry):
    with open(os.environ["FAKE_KUBECTL_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")

record({{"argv": args, "pid": os.getpid()}})
if os.environ.get("FAKE_KUBECTL_EXIT"):
    sys.exit(int(os.environ["FAKE_KUBECTL_EXIT"]))
flags = dict(arg.split("=", 1) for arg in args if arg.startswith("--") and "=" in arg)
accept = re.compile(flags["--accept-paths"])
with open(os.environ["FAKE_PROXY_SCRIPT"], encoding="utf-8") as handle:
    script = json.load(handle)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def answer(self, status, body):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def chunk(self, data):
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        record({{"method": "GET", "path": url.path, "query": query}})
        if not accept.search(url.path):
            self.answer(403, b"Forbidden\n")
            return
        if url.path.endswith("/pods"):
            entry = {{"lines": [json.dumps(event) for event in script["events"]], "hold": True}}
        else:
            key = url.path.split("/")[-2] + "/" + query["container"][0]
            entry = script["logs"].get(key + ("/previous" if "previous" in query else ""))
            if entry is None:
                self.answer(400, json.dumps({{"message": "no log for " + key}}).encode())
                return
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for line in entry["lines"]:
            self.chunk(line.encode() + b"\n")
        if entry.get("hold"):
            time.sleep(3600)
        self.chunk(b"")


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


Server(flags["--unix-socket"], Handler).serve_forever()
"""

CRASHED = "dfe-transform-vrl-flowfetchvrl-556dfc95dc-xb5bz"
DELETED = "dfe-fetcher-flowfetch-5fbd49fd-wrr6w"
OLD = "dfe-receiver-7b5f865fb5-k29p6"


def _scenario() -> tuple[dict, dict[str, str]]:
    """A crash-looped transform, a fetcher deleted mid-stream and a pod older than the window.

    Returns:
        The fake proxy's script, and each followed run's expected file name by role.
    """
    crashed = _ended("containerd://crashed-run", _ago(20), _ago(15))
    current_start = _ago(5)
    transform = _pod(CRASHED, main=[
        _running("dfe-transform-vrl", "containerd://current-run", current_start, last=crashed)
    ])
    init_done = _ended("containerd://init-run", _ago(45), _ago(40))
    fetcher_start = _ago(30)
    fetcher = _pod(
        DELETED,
        init=[{"name": "wait-for-engine", "state": {"terminated": init_done}, "lastState": {}}],
        main=[_running("dfe-fetcher", "containerd://fetcher-run", fetcher_start)],
    )
    old_init = _ended("containerd://old-init", _ago(4000), _ago(3600))
    old = _pod(
        OLD,
        init=[{"name": "wait-for-engine", "state": {"terminated": old_init}, "lastState": {}}],
        main=[{"name": "dfe-receiver", "state": {"waiting": {"reason": "ContainerCreating"}}}],
    )
    events = [{"type": "ADDED", "object": pod} for pod in (transform, fetcher, old)]
    events.append({"type": "MODIFIED", "object": transform})
    logs = {
        f"{CRASHED}/dfe-transform-vrl/previous": {"lines": [
            "2026-10-18T22:48:01Z starting",
            f"2026-10-18T22:48:02Z ERROR SASL authentication failed password={PLANTED[0]}",
            "2026-10-18T22:48:03Z panic: the program did not compile",
        ]},
        f"{CRASHED}/dfe-transform-vrl": {"lines": [
            f"2026-10-18T22:48:20Z DEBUG Authorization: Bearer {PLANTED[1]}",
            "2026-10-18T22:48:21Z starting again",
        ], "hold": True},
        f"{DELETED}/wait-for-engine": {"lines": ["2026-10-18T22:47:00Z the engine answered"]},
        f"{DELETED}/dfe-fetcher": {"lines": [
            "2026-10-18T22:47:10Z polling",
            f"2026-10-18T22:47:11Z key {PLANTED[2]}",
            "2026-10-18T22:47:12Z received SIGTERM, draining",
        ]},
    }
    files = {
        "crashed": f"{CRASHED}.dfe-transform-vrl.{_stamp(crashed['startedAt'])}.log",
        "current": f"{CRASHED}.dfe-transform-vrl.{_stamp(current_start)}.log",
        "deleted": f"{DELETED}.dfe-fetcher.{_stamp(fetcher_start)}.log",
        "init": f"{DELETED}.wait-for-engine.{_stamp(init_done['startedAt'])}.log",
    }
    return {"events": events, "logs": logs}, files


@pytest.fixture
def fake_kubectl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A fake kubectl first and alone on PATH, serving the scenario. Returns its request log."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "kubectl"
    fake.write_text(FAKE_KUBECTL.format(python=sys.executable), encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    script, files = _scenario()
    script_file = tmp_path / "script.json"
    script_file.write_text(json.dumps(script), encoding="utf-8", newline="\n")
    log = tmp_path / "kubectl.log"
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("FAKE_PROXY_SCRIPT", str(script_file))
    monkeypatch.setenv("FAKE_KUBECTL_LOG", str(log))
    monkeypatch.delenv("FAKE_KUBECTL_EXIT", raising=False)
    return log, files


def _requests(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _until(check, what: str) -> None:
    give_up = time.monotonic() + WAIT
    while not check():
        assert time.monotonic() < give_up, f"{what} within {WAIT:g}s"
        time.sleep(0.1)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _proxy_pid(log: Path) -> int:
    return next(entry["pid"] for entry in _requests(log) if "pid" in entry)


def _index(out: Path) -> str:
    return (out / fe.INDEX).read_text(encoding="utf-8")


def _start(tmp_path: Path) -> tuple[pl.Owned, Path]:
    out = tmp_path / "diag" / "20261018T221500Z-acceptance-pod-logs"
    owned = pl.spawn(SELF_CMD, out, [NAMESPACE], ["--kubeconfig", "/k/config"])
    assert owned is not None
    return owned, out


def _files_present(out: Path, files: dict[str, str]) -> bool:
    return all((out / NAMESPACE / name).is_file() for name in files.values())


def test_a_deleted_pod_and_a_crashed_run_each_leave_their_log(
    fake_kubectl: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    _log, files = fake_kubectl
    owned, out = _start(tmp_path)
    try:
        _until(lambda: _files_present(out, files), "every run's file appears")
    finally:
        pl.finish(owned, keep=True)
    assert owned.proc.returncode == 0
    logs = out / NAMESPACE
    crashed = (logs / files["crashed"]).read_text(encoding="utf-8")
    assert "panic: the program did not compile" in crashed
    assert f"password={fe.MASK}" in crashed
    assert "starting again" in (logs / files["current"]).read_text(encoding="utf-8")
    deleted = (logs / files["deleted"]).read_text(encoding="utf-8")
    assert deleted.endswith("received SIGTERM, draining\n")
    assert "the engine answered" in (logs / files["init"]).read_text(encoding="utf-8")
    assert sorted(path.name for path in logs.iterdir()) == sorted(files.values())
    index = _index(out)
    assert f"{NAMESPACE}/{files['deleted']}: the stream ended, " in index
    assert f"{NAMESPACE}/{files['current']}: still open when the follower stopped, " in index
    assert "stopped " in index.splitlines()[-1]
    assert "its owner closed standard input. 4 run(s) followed, 4 file(s)" in index
    for path in out.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert not [planted for planted in PLANTED if planted in text], path


def test_it_asks_for_pod_lists_and_pod_logs_alone_and_the_window_only(
    fake_kubectl: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    log, files = fake_kubectl
    owned, out = _start(tmp_path)
    try:
        _until(lambda: _files_present(out, files), "every run's file appears")
    finally:
        pl.finish(owned, keep=True)
    entries = _requests(log)
    argv = next(entry["argv"] for entry in entries if "argv" in entry)
    assert argv[:3] == ["--kubeconfig", "/k/config", "proxy"]
    assert f"--accept-paths={pl.accept_paths([NAMESPACE])}" in argv
    assert f"--reject-methods={pl.REJECT_METHODS}" in argv
    gets = [entry for entry in entries if entry.get("method")]
    assert {entry["method"] for entry in gets} == {"GET"}
    for entry in gets:
        assert re.fullmatch(rf"/api/v1/namespaces/{NAMESPACE}/pods(/[^/]+/log)?", entry["path"])
        assert OLD not in entry["path"]
    reads = [entry for entry in gets if entry["path"].endswith("/log")]
    previous = [entry for entry in reads if "previous" in entry["query"]]
    crashed_log = f"/api/v1/namespaces/{NAMESPACE}/pods/{CRASHED}/log"
    assert [entry["path"] for entry in previous] == [crashed_log]
    followed = [entry for entry in reads if entry not in previous]
    assert all(entry["query"].get("follow") == ["true"] for entry in followed)
    assert all("sinceTime" in entry["query"] for entry in reads)
    # The MODIFIED event repeats the transform, whose two runs are each read once.
    assert len(reads) == 4


def test_a_passing_stage_keeps_nothing_and_leaves_no_process(
    fake_kubectl: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    log, files = fake_kubectl
    owned, out = _start(tmp_path)
    _until(lambda: _files_present(out, files), "every run's file appears")
    proxy = _proxy_pid(log)
    pl.finish(owned, keep=False)
    assert owned.proc.returncode == 0
    assert not out.exists()
    _until(lambda: not _alive(proxy), "the proxy is gone")


def test_a_sigterm_stops_it_with_every_tail_written(
    fake_kubectl: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    log, files = fake_kubectl
    owned, out = _start(tmp_path)
    try:
        _until(lambda: _files_present(out, files), "every run's file appears")
        owned.proc.send_signal(signal.SIGTERM)
        assert owned.proc.wait(WAIT) == 0
    finally:
        pl.finish(owned, keep=True)
    assert ": SIGTERM. " in _index(out)
    assert "starting again" in (out / NAMESPACE / files["current"]).read_text(encoding="utf-8")
    _until(lambda: not _alive(_proxy_pid(log)), "the proxy is gone")


def test_it_stops_itself_at_max_seconds(
    fake_kubectl: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    log, _files = fake_kubectl
    out = tmp_path / "out"
    started = time.monotonic()
    done = subprocess.run(
        [*SELF_CMD, "pod-logs", "--out", str(out), "--namespace", NAMESPACE, "--max-seconds", "2"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert time.monotonic() - started < 2 + pl.JOIN_TIMEOUT + pl.PROXY_STOP
    assert ": --max-seconds 2 passed. " in _index(out)
    assert not _alive(_proxy_pid(log))


def test_a_proxy_that_never_serves_is_named_and_it_still_exits_zero(
    monkeypatch: pytest.MonkeyPatch, fake_kubectl: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    monkeypatch.setenv("FAKE_KUBECTL_EXIT", "3")
    out = tmp_path / "out"
    done = subprocess.run(
        [*SELF_CMD, "pod-logs", "--out", str(out), "--namespace", NAMESPACE],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "the kubectl proxy exited 3 before it served. 0 run(s) followed" in _index(out)


def test_no_kubectl_is_named_and_it_still_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    out = tmp_path / "out"
    done = subprocess.run(
        [*SELF_CMD, "pod-logs", "--out", str(out), "--namespace", NAMESPACE],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "could not start kubectl" in _index(out)


def test_a_namespace_that_is_not_a_dns_label_is_refused(tmp_path: Path) -> None:
    done = subprocess.run(
        [*SELF_CMD, "pod-logs", "--out", str(tmp_path / "out"), "--namespace", "dfe|.*"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60, check=False,
    )
    assert done.returncode == 2
    assert "not a namespace name" in done.stderr
    assert not (tmp_path / "out").exists()


def test_the_stage_directory_is_timestamped_under_the_root() -> None:
    path = pl.stage_dir("/diag", "acceptance")
    assert path.parent == Path("/diag")
    assert re.fullmatch(r"\d{8}T\d{6}Z-acceptance-pod-logs", path.name)


# --- the acceptance stage owns one under --diagnostics-dir -------------------------------


def _acceptance_args(root: Path, *extra: str) -> argparse.Namespace:
    return dfeops.build_parser().parse_args(
        ["acceptance", "--repo", "/nowhere", "--namespace", NAMESPACE, "--kubeconfig", "/k/config",
         "--diagnostics-dir", str(root), *extra]
    )


def test_a_failed_acceptance_keeps_the_pod_logs_it_followed(
    monkeypatch: pytest.MonkeyPatch, fake_kubectl: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """The edge suite refuses with no allow list, after the follower has started."""
    for name in ("DFE_RUN_INGEST_ALLOW_CIDR", "DFE_KUBE_CONTEXT", "DFE_KUBE_SERVER"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "diag"
    assert dfeops.cmd_acceptance(_acceptance_args(root, "--suite", "edge")) == 1
    (kept,) = root.iterdir()
    assert kept.name.endswith("-acceptance-pod-logs")
    index = _index(kept)
    assert "(the proxy): kubectl --kubeconfig /k/config proxy --unix-socket=" in index
    assert f"(?:{NAMESPACE})/pods" in index
    assert "its owner closed standard input" in index


def test_a_passing_acceptance_keeps_what_its_follower_wrote(
    monkeypatch: pytest.MonkeyPatch, fake_kubectl: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """A green run's pod logs are evidence too. The run is replaced by one that starts the follower."""
    _log, files = fake_kubectl
    root = tmp_path / "diag"
    kept: list[Path] = []

    def passing(args: argparse.Namespace, followers: list[pl.Owned]) -> int:
        out = pl.stage_dir(args.diagnostics_dir, "acceptance")
        owned = pl.spawn(SELF_CMD, out, [NAMESPACE], [])
        assert owned is not None
        followers.append(owned)
        kept.append(out)
        _until(lambda: _files_present(out, files), "every run's file appears")
        return 0

    monkeypatch.setattr(dfeops, "_acceptance", passing)
    assert dfeops.cmd_acceptance(_acceptance_args(root)) == 0
    assert kept
    assert _files_present(kept[0], files)


def test_acceptance_without_the_flag_starts_no_follower(
    monkeypatch: pytest.MonkeyPatch, fake_kubectl: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    log, _files = fake_kubectl
    monkeypatch.delenv("DFE_RUN_INGEST_ALLOW_CIDR", raising=False)
    args = dfeops.build_parser().parse_args(
        ["acceptance", "--repo", "/nowhere", "--namespace", NAMESPACE, "--suite", "edge"]
    )
    assert dfeops.cmd_acceptance(args) == 1
    assert _requests(log) == []
