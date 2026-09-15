#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_release.py
#  Purpose:      Prove the `ship` verb: that it chains open -> merge -> wait ->
#                latest -> digest in that order and prints one SHIPPED line, that
#                the in-flight guard stops before the merge, and that a bad squash
#                subject is refused before any gh call.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe-release.py's ship chain.

Every gh call is answered by a recorder standing in for subprocess.run -- no
network, and nothing here can merge, dispatch or publish anything.

The guard is the test that matters. The publish workflow's concurrency group is
keyed on the branch, so merging while a release run is in flight on main cancels
it, and the release silently never lands. `ship` has to stop rather than merge.

Runs offline. Under pytest, and standalone via the main() runner at the bottom
(matching the other tests in this dir).

    python3 -m pytest scripts/tests/test_dfe_release.py
    python3 scripts/tests/test_dfe_release.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name: str, filename: str | None = None):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (filename or f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


registry_pins = _load("registry_pins")
release = _load("dfe_release", "dfe-release.py")

REPO = "dfe-loader"
TAG = "v1.18.22"
DIGEST = "sha256:" + "a" * 64


def _completed(rc: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


class FakeGh:
    """Stands in for subprocess.run: records every gh argv and answers it.

    Stateful where reality is: the branch has no open PR until `pr create` has
    run, and the workflow's latest run only changes id once the merge has landed.
    """

    def __init__(self, *, in_flight: bool = False, run_list_fails: bool = False):
        self.calls: list[list[str]] = []
        self.in_flight = in_flight
        self.run_list_fails = run_list_fails
        self.pr_created = False

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        return self._answer(list(cmd))

    def _answer(self, cmd: list[str]):
        if cmd[:3] == ["gh", "pr", "view"]:
            if "number,state" in cmd:
                if not self.pr_created:
                    return _completed(1, "", "no pull requests found for branch")
                return _completed(0, json.dumps({"number": 7, "state": "OPEN"}))
            return _completed(0, json.dumps({"title": "fix: land it", "body": "why it lands\n"}))

        if cmd[:3] == ["gh", "pr", "create"]:
            self.pr_created = True
            return _completed(0, "https://example.invalid/pr/7")

        if cmd[:3] == ["gh", "pr", "merge"]:
            return _completed(0, "Merged pull request #7")

        if cmd[:3] == ["gh", "run", "list"]:
            if self.run_list_fails:
                return _completed(1, "", "HTTP 403: read denied")
            if "--status" in cmd:
                if self.in_flight and cmd[cmd.index("--status") + 1] == "in_progress":
                    return _completed(0, json.dumps([{
                        "databaseId": 99,
                        "status": "in_progress",
                        "url": "https://example.invalid/run/99",
                    }]))
                return _completed(0, "[]")
            merged = any(c[:3] == ["gh", "pr", "merge"] for c in self.calls)
            run_id = 200 if merged else 100
            return _completed(0, json.dumps([{
                "databaseId": run_id,
                "status": "completed",
                "conclusion": "success",
                "headBranch": "main",
                "createdAt": "2026-01-01T00:00:00Z",
                "url": f"https://example.invalid/run/{run_id}",
            }]))

        if cmd[:2] == ["gh", "api"]:
            path = next((c for c in cmd if c.startswith("/")), "")
            if path.endswith("/releases/latest"):
                return _completed(0, TAG + "\n")
            if "/packages/container/" in path:
                return _completed(0, json.dumps([{
                    "name": DIGEST,
                    "created_at": "2026-01-01T00:00:00Z",
                    "metadata": {"container": {"tags": [TAG]}},
                }]))

        return _completed(0, "")


def _install(monkeypatch, fake: FakeGh) -> FakeGh:
    """Route every gh call in the driver and in registry_pins at the recorder."""
    registry_pins.package_versions.cache_clear()
    monkeypatch.setattr(release.subprocess, "run", fake)
    monkeypatch.setattr(release.time, "sleep", lambda _seconds: None)
    return fake


def _ship_args(**over) -> argparse.Namespace:
    ns = argparse.Namespace(
        repo=REPO,
        branch="fix/a-thing",
        title="fix: a thing",
        body_file=None,
        publish=True,
        workflow=release.DEFAULT_WORKFLOW,
        timeout=60,
    )
    for key, value in over.items():
        setattr(ns, key, value)
    return ns


def _steps(calls: list[list[str]]) -> list[str]:
    """The chain each recorded gh call belongs to."""
    labels = []
    for c in calls:
        if c[:3] == ["gh", "pr", "create"]:
            labels.append("open")
        elif c[:3] == ["gh", "pr", "merge"]:
            labels.append("merge")
        elif c[:3] == ["gh", "run", "list"] and "--status" not in c:
            labels.append("wait")
        elif c[:2] == ["gh", "api"] and any(x.endswith("/releases/latest") for x in c):
            labels.append("latest")
        elif c[:2] == ["gh", "api"] and any("/packages/container/" in x for x in c):
            labels.append("digest")
    return labels


def _in_order(labels: list[str], expected: list[str]) -> bool:
    """True when every expected step appears, in order, among the labels."""
    remaining = list(expected)
    for label in labels:
        if remaining and label == remaining[0]:
            remaining.pop(0)
    return not remaining


# --- the happy path -----------------------------------------------------------
def test_ship_chains_the_five_verbs_and_prints_one_line(monkeypatch, capsys, tmp_path):
    body = tmp_path / "body.md"
    body.write_text("why it lands\n", encoding="utf-8", newline="\n")
    fake = _install(monkeypatch, FakeGh())

    rc = release.cmd_ship(_ship_args(body_file=str(body)))
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out == f"SHIPPED {REPO} {TAG} {DIGEST}\n"
    assert _in_order(_steps(fake.calls), ["open", "merge", "wait", "latest", "digest"]), (
        _steps(fake.calls)
    )


def test_ship_squashes_with_the_release_trailer(monkeypatch, capsys, tmp_path):
    """--publish is what releases: the trailer has to reach the squash message."""
    body = tmp_path / "body.md"
    body.write_text("why it lands\n", encoding="utf-8", newline="\n")
    fake = _install(monkeypatch, FakeGh())

    release.cmd_ship(_ship_args(body_file=str(body)))
    capsys.readouterr()

    merge = next(c for c in fake.calls if c[:3] == ["gh", "pr", "merge"])
    assert "--squash" in merge
    assert release.PUBLISH_TRAILER in merge[merge.index("--body") + 1]
    assert merge[merge.index("--subject") + 1] == "fix: a thing"


def test_ship_waits_for_the_run_the_merge_triggered(monkeypatch, capsys, tmp_path):
    """Without the after-id check it would report the PREVIOUS run's conclusion."""
    body = tmp_path / "body.md"
    body.write_text("why it lands\n", encoding="utf-8", newline="\n")
    fake = _install(monkeypatch, FakeGh())

    release.cmd_ship(_ship_args(body_file=str(body)))
    capsys.readouterr()

    waits = [
        i
        for i, c in enumerate(fake.calls)
        if c[:3] == ["gh", "run", "list"] and "--status" not in c
    ]
    merged_at = next(i for i, c in enumerate(fake.calls) if c[:3] == ["gh", "pr", "merge"])
    assert any(i < merged_at for i in waits), "the pre-merge run id is never recorded"
    assert any(i > merged_at for i in waits), "nothing waits after the merge"


def test_ship_reuses_an_already_open_pr(monkeypatch, capsys):
    fake = FakeGh()
    fake.pr_created = True  # the branch already has an open PR
    _install(monkeypatch, fake)

    rc = release.cmd_ship(_ship_args(body_file=None))
    captured = capsys.readouterr()

    assert rc == 0
    assert not any(c[:3] == ["gh", "pr", "create"] for c in fake.calls)
    assert captured.out == f"SHIPPED {REPO} {TAG} {DIGEST}\n"


# --- the guard ----------------------------------------------------------------
def test_in_flight_publish_stops_before_the_merge(monkeypatch, capsys, tmp_path):
    body = tmp_path / "body.md"
    body.write_text("why it lands\n", encoding="utf-8", newline="\n")
    fake = _install(monkeypatch, FakeGh(in_flight=True))

    rc = release.cmd_ship(_ship_args(body_file=str(body)))
    captured = capsys.readouterr()

    assert rc == 3
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in fake.calls)
    assert captured.out == ""
    assert "a publish is in flight on main" in captured.err
    assert "concurrency group" in captured.err


def test_an_unreadable_run_list_does_not_merge(monkeypatch, capsys, tmp_path):
    """Unknown state is not the same as clear -- it must not merge on a guess."""
    body = tmp_path / "body.md"
    body.write_text("why it lands\n", encoding="utf-8", newline="\n")
    fake = _install(monkeypatch, FakeGh(run_list_fails=True))

    rc = release.cmd_ship(_ship_args(body_file=str(body)))
    captured = capsys.readouterr()

    assert rc == 1
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in fake.calls)
    assert "HTTP 403" in captured.err


# --- refusals before any gh call ----------------------------------------------
def test_a_capitalised_title_is_refused_before_any_gh_call(monkeypatch, capsys):
    fake = _install(monkeypatch, FakeGh())

    rc = release.cmd_ship(_ship_args(title="Fix: a thing"))
    captured = capsys.readouterr()

    assert rc == 2
    assert fake.calls == []
    assert "lowercase" in captured.err


def test_an_overlong_title_is_refused_before_any_gh_call(monkeypatch, capsys):
    fake = _install(monkeypatch, FakeGh())

    rc = release.cmd_ship(_ship_args(title="fix: " + "x" * 100))
    captured = capsys.readouterr()

    assert rc == 2
    assert fake.calls == []
    assert "100 characters" in captured.err


def test_a_repo_outside_the_allowlist_is_refused(monkeypatch, capsys):
    fake = _install(monkeypatch, FakeGh())
    raised = False
    try:
        release.cmd_ship(_ship_args(repo="some-other-repo"))
    except SystemExit as exc:
        raised = exc.code == 2
    capsys.readouterr()

    assert raised
    assert fake.calls == []


def test_no_open_pr_and_no_body_file_is_refused(monkeypatch, capsys):
    fake = _install(monkeypatch, FakeGh())

    rc = release.cmd_ship(_ship_args(body_file=None))
    captured = capsys.readouterr()

    assert rc == 2
    assert not any(c[:3] == ["gh", "pr", "create"] for c in fake.calls)
    assert "--body-file" in captured.err


# --- the refactor keeps the existing verbs intact -----------------------------
def test_title_validation_accepts_a_conventional_subject():
    assert release._title_problem("fix: a thing") is None
    assert release._title_problem("feat(api): add a thing") is None
    assert release._title_problem("Fix: a thing") is not None
    assert release._title_problem("") is not None
    assert release._title_problem("fix: " + "x" * 95) is not None


def test_wait_still_reports_the_latest_run_when_given_no_after_id(monkeypatch, capsys):
    """cmd_wait must be unchanged by the after-id the ship chain needs."""
    _install(monkeypatch, FakeGh())
    args = argparse.Namespace(
        repo=REPO, workflow=release.DEFAULT_WORKFLOW, interval=1, timeout=5
    )
    rc = release.cmd_wait(args)
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out.startswith(f"{release.DEFAULT_WORKFLOW} success (")


def test_digest_resolves_a_tag_through_the_paginating_reader(monkeypatch, capsys):
    _install(monkeypatch, FakeGh())
    rc = release.cmd_digest(argparse.Namespace(repo=REPO, version=TAG))
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out.strip() == DIGEST


# --- standalone runner (mirrors the other tests in this dir) ------------------
def main() -> int:
    import contextlib
    import io
    import tempfile

    class _MP:
        """A minimal monkeypatch: records and restores attribute sets."""

        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        def restore(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)

    class _Caps:
        """A minimal capsys: reads the redirected buffers and drains them."""

        def __init__(self, out, err):
            self._out, self._err = out, err

        def readouterr(self):
            result = type(
                "R", (), {"out": self._out.getvalue(), "err": self._err.getvalue()}
            )()
            for buf in (self._out, self._err):
                buf.truncate(0)
                buf.seek(0)
            return result

    failures = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        mp = _MP()
        out_buf, err_buf = io.StringIO(), io.StringIO()
        params = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        with tempfile.TemporaryDirectory() as td:
            kw = {}
            if "monkeypatch" in params:
                kw["monkeypatch"] = mp
            if "tmp_path" in params:
                kw["tmp_path"] = Path(td)
            if "capsys" in params:
                kw["capsys"] = _Caps(out_buf, err_buf)
            try:
                with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                    fn(**kw)
                print(f"PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"FAIL  {name}  {exc}")
            finally:
                mp.restore()
    print(f"\n{'FAILED' if failures else 'ALL PASSED'} -- {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
