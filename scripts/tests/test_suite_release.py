#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_suite_release.py
#  Purpose:      Prove the release half of scripts/suite: the ship path with a
#                staged fix and with nothing staged, the Rust and Python
#                rebuilds, the registry-moved assertion, and that --dry-run
#                touches nothing.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/suite -- ship, rebuild, and the landing primitives.

Every git, gh, cargo, uv and hyperi-ci call is answered by a recorder standing
in for subprocess.run, and both registries are answered by a stand-in for the
JSON read -- no network, and nothing here can push, merge or publish anything.

The assertions that matter: main is never pushed to, the squash message carries
the release trailer, a green run alone is never accepted as a release, and a
dry run reaches no mutating command at all.

Runs offline. Under pytest, and standalone via the shared runner:

    python3 -m pytest scripts/tests/test_suite_release.py
    python3 scripts/tests/test_suite_release.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner import run_module  # noqa: E402
from suite import artefacts, landing, proc, rebuild, ship  # noqa: E402

SLUG = "hyperi-io/scalo-rs"
BRANCH_SHA = "b" * 40
MAIN_SHA = "c" * 40
RUN_BEFORE = 100
RUN_AFTER = 200


@contextmanager
def _env(**values: str):
    """Set environment variables for one test and put the old ones back."""
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _completed(rc: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


class FakeExec:
    """Stands in for subprocess.run: records every argv and answers it.

    Stateful where reality is: the branch has no open PR until `pr create` has
    run, and the GitHub release tag only moves once the merge has landed -- so
    a test that never merges cannot accidentally pass the shipped-it assertion.
    """

    def __init__(self, *, staged: bool = True, slug: str = SLUG, tagged: str = "") -> None:
        self.calls: list[list[str]] = []
        self.staged = staged
        self.slug = slug
        self.tagged = tagged
        self.pr_created = False
        self.dockerfile = "FROM scratch\n"

    # -- helpers the assertions read ---------------------------------------
    def ran(self, *fragment: str) -> bool:
        """True when some recorded argv holds these words consecutively."""
        width = len(fragment)
        return any(
            tuple(call[i : i + width]) == fragment
            for call in self.calls
            for i in range(len(call) - width + 1)
        )

    def argv_for(self, *fragment: str) -> list[str]:
        """The first recorded argv holding these words consecutively."""
        width = len(fragment)
        for call in self.calls:
            for i in range(len(call) - width + 1):
                if tuple(call[i : i + width]) == fragment:
                    return call
        raise AssertionError(f"no recorded call ran {' '.join(fragment)}: {self.calls}")

    @property
    def merged(self) -> bool:
        """True once a squash merge has been recorded."""
        return any(c[:3] == ["gh", "pr", "merge"] for c in self.calls)

    @property
    def dispatched(self) -> bool:
        """True once the from-head release dispatch has been recorded."""
        return any(c[:2] == ["hyperi-ci", "publish"] for c in self.calls)

    # -- the recorder itself -----------------------------------------------
    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return self._answer(list(argv))

    def _answer(self, cmd: list[str]):
        if cmd[:1] == ["git"]:
            return self._git(cmd)
        if cmd[:1] == ["gh"]:
            return self._gh(cmd)
        if cmd[:1] == ["cargo"]:
            if "emit-dockerfile" in cmd:
                return _completed(0, self.dockerfile)
            return _completed(0)
        return _completed(0)

    def _git(self, cmd: list[str]):
        if cmd[1:4] == ["diff", "--cached", "--quiet"]:
            return _completed(1 if self.staged else 0)
        if cmd[1:3] == ["rev-parse", "HEAD"]:
            return _completed(0, MAIN_SHA if self.merged else BRANCH_SHA)
        if cmd[1:3] == ["rev-parse", "--abbrev-ref"]:
            return _completed(0, "main")
        if cmd[1:2] == ["describe"]:
            return _completed(0, self.tagged) if self.tagged else _completed(1)
        return _completed(0)

    def _gh(self, cmd: list[str]):
        if cmd[:3] == ["gh", "repo", "view"]:
            return _completed(0, self.slug)
        if cmd[:3] == ["gh", "pr", "view"]:
            if "number,state" in cmd:
                if not self.pr_created:
                    return _completed(1, "", "no pull requests found for branch")
                return _completed(0, json.dumps({"number": 7, "state": "OPEN"}))
            if "mergeStateStatus,mergeable,statusCheckRollup" in cmd:
                return _completed(
                    0,
                    json.dumps(
                        {
                            "mergeStateStatus": "CLEAN",
                            "mergeable": "MERGEABLE",
                            "statusCheckRollup": [
                                {"name": "Quality", "conclusion": "SUCCESS"}
                            ],
                        }
                    ),
                )
            return _completed(0, json.dumps({"title": "fix: land it", "body": "why\n"}))
        if cmd[:3] == ["gh", "pr", "create"]:
            self.pr_created = True
            return _completed(0, "https://example.invalid/pr/7\n")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return _completed(0, "Merged pull request #7")
        if cmd[:3] == ["gh", "run", "list"]:
            # The dispatch registers a SECOND run on the same sha; the merge
            # registers one on the new main sha. Either way the id has to move,
            # or the wait would follow the run that was already there.
            run_id = RUN_AFTER if (self.merged or self.dispatched) else RUN_BEFORE
            sha = MAIN_SHA if self.merged else BRANCH_SHA
            return _completed(
                0,
                json.dumps(
                    [{"databaseId": run_id, "headSha": sha, "workflowName": "CI"}]
                ),
            )
        if cmd[:3] == ["gh", "run", "view"]:
            return _completed(
                0,
                json.dumps(
                    {
                        "status": "completed",
                        "conclusion": "success",
                        "jobs": [{"name": "publish", "conclusion": "success"}],
                    }
                ),
            )
        if cmd[:3] == ["gh", "release", "list"]:
            tag = "v1.0.1" if self.merged else "v1.0.0"
            return _completed(0, json.dumps([{"tagName": tag}]))
        return _completed(0)


class FakeRegistry:
    """The two package registries, answering the version that a publish moves.

    It moves once the merge or the from-head dispatch has been recorded, and
    not before -- so a test that never publishes cannot pass the shipped-it
    assertion, and one that does is not left waiting out the 15-minute ceiling.
    """

    def __init__(self, exec_recorder: FakeExec, *, before: str, after: str) -> None:
        self._exec = exec_recorder
        self._before, self._after = before, after
        self.reads = 0

    def __call__(self, url: str, **kwargs) -> object:
        self.reads += 1
        published = self._exec.merged or self._exec.dispatched
        version = self._after if published else self._before
        if "crates.io" in url:
            return {"crate": {"max_stable_version": version}}
        return {"info": {"version": version}}


def _install(monkeypatch, recorder: FakeExec, registry: FakeRegistry | None = None) -> FakeExec:
    """Route every command, registry read, tool probe and sleep at the stand-ins."""
    monkeypatch.setattr(proc.subprocess, "run", recorder)
    monkeypatch.setattr(proc.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(landing.time, "sleep", lambda _seconds: None)
    if registry is not None:
        monkeypatch.setattr(artefacts, "http_json", registry)
    return recorder


def _checkout(tmp_path: Path, name: str) -> Path:
    """A directory shaped enough like a checkout for the guards to pass."""
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


# --- the landing primitives ---------------------------------------------------
def test_the_trailer_sits_on_its_own_line_and_matches_the_ci_gate():
    """The squash message is the publish trigger, so its shape is load-bearing."""
    import re

    body = landing.commit_body(note="Release scalo.", publish=True)
    gate = re.compile(r"^[ \t]*Publish:[ \t]*true[ \t]*$", re.IGNORECASE | re.MULTILINE)

    assert "Publish: true" in body.splitlines()
    assert "Release scalo.\n\nPublish: true" in body
    assert gate.search(f"fix: a thing\n\n{body}")
    assert "Publish" not in landing.commit_body(note="Note.", publish=False)


def test_a_branch_name_is_deterministic_and_bounded():
    assert landing.slugify("fix: the WorkBatch recv model") == "fix-the-workbatch-recv-model"
    assert landing.slugify("fix: x") == landing.slugify("fix: x")
    assert len(landing.slugify("word " * 80)) <= 40
    assert landing.slugify("!!! ???") == "change"
    assert landing.slugify("") == "change"


def test_the_pr_number_is_read_from_the_url_gh_prints():
    assert landing.pr_number_from_url("https://github.com/x/y/pull/4213\n") == 4213
    assert landing.pr_number_from_url("Warning: something else") is None
    assert landing.pr_number_from_url("") is None


def test_only_a_definitely_failed_check_is_red():
    """A pending or skipped check must not abort the merge; a failed one must."""
    assert landing.red_checks([{"name": "Test", "conclusion": "FAILURE"}]) == ["Test"]
    assert landing.red_checks([{"context": "legacy/build", "state": "ERROR"}]) == [
        "legacy/build"
    ]
    assert landing.red_checks(
        [
            {"name": "Quality", "conclusion": "SUCCESS"},
            {"name": "Build", "status": "IN_PROGRESS"},
            {"name": "Optional", "conclusion": "SKIPPED"},
        ]
    ) == []
    # Empty means "no checks reported yet", never "nothing failed, merge it".
    assert landing.red_checks([]) == []
    assert landing.red_checks("not a list") == []


def test_the_run_for_the_sha_is_the_newest_above_the_snapshot():
    rows = [
        {"databaseId": 33054900002, "headSha": "abc123", "workflowName": "CI"},
        {"databaseId": 33054359645, "headSha": "abc123", "workflowName": "CI"},
        {"databaseId": 33054000001, "headSha": "def456", "workflowName": "CI"},
    ]
    assert landing.select_run(rows, "abc123").id == 33054900002
    assert landing.select_run(rows, "abc123", after_run_id=33054359645).id == 33054900002
    # The dispatched run has not registered yet: the pre-existing green run must
    # NOT be handed back as if it were the release.
    assert landing.select_run(rows[1:], "abc123", after_run_id=33054359645) is None
    assert landing.select_run(rows, "999999") is None
    assert landing.select_run("not a list", "abc123") is None


def test_a_red_job_stops_the_release_and_is_named(monkeypatch):
    recorder = FakeExec()
    _install(monkeypatch, recorder)
    monkeypatch.setattr(
        recorder,
        "_gh",
        lambda cmd: _completed(
            0,
            json.dumps(
                {
                    "status": "in_progress",
                    "jobs": [{"name": "publish", "conclusion": "failure"}],
                }
            ),
        ),
    )
    raised = ""
    try:
        landing.poll_run(SLUG, 1, timeout=60)
    except proc.FleetError as exc:
        raised = str(exc)

    assert "RED" in raised
    assert "publish" in raised
    # A red job is a different verdict from a run that published nothing.
    assert "published NOTHING" not in raised


# --- ship: a staged fix lands through a PR ------------------------------------
def test_ship_with_a_staged_fix_lands_through_a_pr_and_proves_the_registry_moved(
    monkeypatch, capsys, tmp_path
):
    recorder = FakeExec(staged=True)
    registry = FakeRegistry(recorder, before="2.10.14", after="2.11.0")
    _install(monkeypatch, recorder, registry)

    rc = ship.ship_library(
        ship.SHIP_RS, repo_dir=_checkout(tmp_path, "scalo-rs"), subject="fix: a thing"
    )
    captured = capsys.readouterr()

    assert rc == 0
    assert recorder.ran("gh", "pr", "create")
    assert recorder.ran("gh", "pr", "merge")
    assert "SHIPPED: scalo 2.10.14 -> 2.11.0" in captured.out


def test_ship_never_pushes_to_main(monkeypatch, capsys, tmp_path):
    """Main is protected on every repo: a change reaches it only through a PR."""
    recorder = FakeExec(staged=True)
    _install(monkeypatch, recorder, FakeRegistry(recorder, before="2.10.14", after="2.11.0"))

    ship.ship_library(
        ship.SHIP_RS, repo_dir=_checkout(tmp_path, "scalo-rs"), subject="fix: a thing"
    )
    capsys.readouterr()

    pushes = [c for c in recorder.calls if c[:2] == ["git", "push"]]
    assert pushes, "nothing was pushed at all"
    assert not any(c[-1] == "main" for c in pushes), pushes


def test_ship_squashes_with_the_release_trailer_on_the_commit_it_validated(
    monkeypatch, capsys, tmp_path
):
    recorder = FakeExec(staged=True)
    _install(monkeypatch, recorder, FakeRegistry(recorder, before="2.10.14", after="2.11.0"))

    ship.ship_library(
        ship.SHIP_RS, repo_dir=_checkout(tmp_path, "scalo-rs"), subject="fix: a thing"
    )
    capsys.readouterr()

    merge = recorder.argv_for("gh", "pr", "merge")
    assert "--squash" in merge
    assert merge[merge.index("--subject") + 1] == "fix: a thing"
    assert landing.PUBLISH_TRAILER in merge[merge.index("--body") + 1]
    assert merge[merge.index("--match-head-commit") + 1] == BRANCH_SHA


def test_ship_with_a_staged_fix_and_no_subject_is_refused(monkeypatch, capsys, tmp_path):
    recorder = FakeExec(staged=True)
    _install(monkeypatch, recorder, FakeRegistry(recorder, before="2.10.14", after="2.11.0"))

    raised = ""
    try:
        ship.ship_library(ship.SHIP_RS, repo_dir=_checkout(tmp_path, "scalo-rs"))
    except proc.FleetError as exc:
        raised = str(exc)
    capsys.readouterr()

    assert "no commit subject" in raised
    assert not recorder.ran("git", "commit")


# --- ship: nothing staged takes the dispatch escape hatch ---------------------
def test_ship_with_nothing_staged_dispatches_and_refuses_the_run_already_on_the_sha(
    monkeypatch, capsys, tmp_path
):
    """A green run that predates the dispatch is not the release, so it is snapshotted."""
    recorder = FakeExec(staged=False)
    _install(monkeypatch, recorder, FakeRegistry(recorder, before="2.10.14", after="2.11.0"))

    rc = ship.ship_library(ship.SHIP_PY, repo_dir=_checkout(tmp_path, "scalo-py"))
    captured = capsys.readouterr()

    assert rc == 0
    assert recorder.ran("hyperi-ci", "publish")
    assert not recorder.ran("gh", "pr", "create")
    assert f"run {RUN_BEFORE} already sits on this sha" in captured.out


def test_ship_with_nothing_staged_and_head_already_released_does_nothing(
    monkeypatch, capsys, tmp_path
):
    recorder = FakeExec(staged=False, tagged="v2.10.14")
    _install(monkeypatch, recorder, FakeRegistry(recorder, before="2.10.14", after="2.11.0"))

    rc = ship.ship_library(ship.SHIP_PY, repo_dir=_checkout(tmp_path, "scalo-py"))
    captured = capsys.readouterr()

    assert rc == 0
    assert "nothing to do" in captured.out
    assert not recorder.ran("hyperi-ci", "publish")


def test_ship_dry_run_touches_nothing(monkeypatch, capsys, tmp_path):
    recorder = FakeExec(staged=True)
    _install(monkeypatch, recorder, FakeRegistry(recorder, before="2.10.14", after="2.11.0"))

    rc = ship.ship_library(
        ship.SHIP_RS,
        repo_dir=_checkout(tmp_path, "scalo-rs"),
        subject="fix: a thing",
        dry_run=True,
    )
    captured = capsys.readouterr()

    assert rc == 0
    assert "[dry-run] would branch" in captured.out
    for forbidden in (
        ("git", "commit"),
        ("git", "push"),
        ("gh", "pr", "create"),
        ("gh", "pr", "merge"),
    ):
        assert not recorder.ran(*forbidden), forbidden


# --- rebuild: Rust ------------------------------------------------------------
def _rust_consumer(tmp_path: Path) -> Path:
    """A Rust consumer with a committed chart its own contract test gates."""
    repo = _checkout(tmp_path, "dfe-loader")
    (repo / "chart").mkdir()
    (repo / "chart" / "Chart.yaml").write_text("name: dfe-loader\n", encoding="utf-8")
    contract = repo / "tests" / "integration"
    contract.mkdir(parents=True)
    (contract / "helm_contract.rs").write_text("// values sync\n", encoding="utf-8")
    return repo


def test_rebuild_rust_pins_the_crate_precisely_and_regenerates_the_dockerfile(
    monkeypatch, capsys, tmp_path
):
    recorder = FakeExec(staged=True, slug="hyperi-io/dfe-loader")
    _install(monkeypatch, recorder)
    repo = _rust_consumer(tmp_path)

    with _env(DFE_SUITE_REBUILD_TARGET=str(tmp_path / "target")):
        rc = rebuild.rebuild_rust(repo, "2.11.0")
    captured = capsys.readouterr()

    assert rc == 0
    update = recorder.argv_for("cargo", "update")
    assert update[update.index("-p") + 1] == "scalo"
    assert update[update.index("--precise") + 1] == "2.11.0"
    assert (repo / "Dockerfile").read_text(encoding="utf-8") == "FROM scratch\n"
    assert "SHIPPED: hyperi-io/dfe-loader v1.0.0 -> v1.0.1" in captured.out


def test_rebuild_rust_leaves_a_gated_chart_to_its_own_contract_test(
    monkeypatch, capsys, tmp_path
):
    """A chart the app's gates already test is maintained, not stale."""
    recorder = FakeExec(staged=True, slug="hyperi-io/dfe-loader")
    _install(monkeypatch, recorder)
    repo = _rust_consumer(tmp_path)

    with _env(DFE_SUITE_REBUILD_TARGET=str(tmp_path / "target")):
        rebuild.rebuild_rust(repo, "2.11.0")
    captured = capsys.readouterr()

    assert "values-syncs chart/" in captured.out
    assert not recorder.ran("emit-chart")
    assert (repo / "chart" / "Chart.yaml").read_text(encoding="utf-8") == "name: dfe-loader\n"


def test_a_committed_chart_with_no_emitter_and_no_gate_is_refused(tmp_path):
    """Refusing beats leaving a chart stale and unchecked -- --no-chart is the opt-out."""
    repo = _checkout(tmp_path, "dfe-receiver")
    (repo / "chart").mkdir()
    (repo / "chart" / "Chart.yaml").write_text("name: dfe-receiver\n", encoding="utf-8")
    (repo / "tests").mkdir()
    # A comment mentioning the contract is the ABSENCE of the gate, not the gate.
    (repo / "tests" / "deployment.rs").write_text(
        "#[test]\nfn other() {}\n// TODO: add a helm_contract test\n", encoding="utf-8"
    )

    assert rebuild.chart_contract_test(repo) is None


def test_a_contract_test_is_found_as_a_file_a_function_or_a_module(tmp_path):
    by_name = _checkout(tmp_path, "a")
    (by_name / "tests").mkdir()
    (by_name / "tests" / "helm_contract.rs").write_text("// x\n", encoding="utf-8")
    assert rebuild.chart_contract_test(by_name) is not None

    by_fn = _checkout(tmp_path, "b")
    (by_fn / "tests").mkdir()
    (by_fn / "tests" / "deployment.rs").write_text(
        "#[test]\nfn helm_contract_values_match_defaults() {}\n", encoding="utf-8"
    )
    assert rebuild.chart_contract_test(by_fn) is not None

    by_mod = _checkout(tmp_path, "c")
    (by_mod / "src").mkdir()
    (by_mod / "src" / "lib.rs").write_text(
        "#[cfg(test)]\nmod helm_contract;\n", encoding="utf-8"
    )
    assert rebuild.chart_contract_test(by_mod) is not None


def test_rebuild_rust_dry_run_touches_nothing(monkeypatch, capsys, tmp_path):
    recorder = FakeExec(staged=True, slug="hyperi-io/dfe-loader")
    _install(monkeypatch, recorder)
    repo = _rust_consumer(tmp_path)

    rc = rebuild.rebuild_rust(repo, "2.11.0", dry_run=True)
    captured = capsys.readouterr()

    assert rc == 0
    assert "the Dockerfile, chart/ and docs/ artefacts" in captured.out
    assert "nothing changed, nothing pushed" in captured.out
    assert not (repo / "Dockerfile").exists()
    assert not recorder.ran("cargo", "update")
    assert not recorder.ran("gh", "pr", "merge")


def test_rebuild_rust_dry_run_drops_the_chart_when_it_is_skipped(monkeypatch, capsys, tmp_path):
    recorder = FakeExec(staged=True, slug="hyperi-io/dfe-loader")
    _install(monkeypatch, recorder)

    rebuild.rebuild_rust(
        _rust_consumer(tmp_path), "2.11.0", dry_run=True, emit_chart_files=False
    )
    captured = capsys.readouterr()

    assert "the Dockerfile and docs/ artefacts" in captured.out
    assert "chart/" not in captured.out


# --- rebuild: Python ----------------------------------------------------------
def _python_consumer(tmp_path: Path, *, locked: str) -> Path:
    repo = _checkout(tmp_path, "dfe-engine")
    (repo / "pyproject.toml").write_text(
        'dependencies = ["scalo[expression,http,metrics]>=2.29.7", "httpx>=0.27"]\n',
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text(
        f'[[package]]\nname = "scalo"\nversion = "{locked}"\n', encoding="utf-8"
    )
    return repo


def test_rebuild_python_moves_the_floor_keeps_the_extras_and_relocks(
    monkeypatch, capsys, tmp_path
):
    recorder = FakeExec(staged=True, slug="hyperi-io/dfe-engine")
    _install(monkeypatch, recorder)
    repo = _python_consumer(tmp_path, locked="2.29.10")

    rc = rebuild.rebuild_python(repo, "2.29.10")
    captured = capsys.readouterr()

    assert rc == 0
    text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    # Extras are load-bearing: dropping one fails at runtime, past every gate.
    assert '"scalo[expression,http,metrics]>=2.29.10"' in text
    assert '"httpx>=0.27"' in text
    lock = recorder.argv_for("uv", "lock")
    assert lock[lock.index("--upgrade-package") + 1] == "scalo"
    assert "lock confirms scalo 2.29.10" in captured.out


def test_rebuild_python_refuses_a_lock_that_resolved_something_else(
    monkeypatch, capsys, tmp_path
):
    """`>=` is a floor, not a pin: a stale lock would ship the wrong dependency."""
    recorder = FakeExec(staged=True, slug="hyperi-io/dfe-engine")
    _install(monkeypatch, recorder)
    repo = _python_consumer(tmp_path, locked="2.29.7")

    raised = ""
    try:
        rebuild.rebuild_python(repo, "2.29.10")
    except proc.FleetError as exc:
        raised = str(exc)
    capsys.readouterr()

    assert "resolved scalo 2.29.7, expected 2.29.10" in raised
    assert not recorder.ran("gh", "pr", "merge")


def test_a_missing_constraint_is_a_hard_failure(tmp_path):
    repo = _checkout(tmp_path, "dfe-engine")
    (repo / "pyproject.toml").write_text('dependencies = ["httpx>=0.27"]\n', encoding="utf-8")

    raised = ""
    try:
        rebuild.bump_py_constraint(repo, "2.29.10")
    except proc.FleetError as exc:
        raised = str(exc)

    assert "no 'scalo...>=X.Y.Z' constraint" in raised


def test_rebuild_python_dry_run_touches_nothing(monkeypatch, capsys, tmp_path):
    recorder = FakeExec(staged=True, slug="hyperi-io/dfe-engine")
    _install(monkeypatch, recorder)
    repo = _python_consumer(tmp_path, locked="2.29.7")

    rc = rebuild.rebuild_python(repo, "2.29.10", dry_run=True)
    captured = capsys.readouterr()

    assert rc == 0
    assert "nothing changed, nothing pushed" in captured.out
    assert '>=2.29.7"' in (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert not recorder.ran("uv", "lock")


# --- the registry has the last word -------------------------------------------
class _Clock:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _ScriptedRegistry:
    """A registry stand-in for await_artefact: scripted versions, no network."""

    def __init__(self, *versions: str) -> None:
        self.name = "scalo"
        self._versions = list(versions)

    def live(self) -> str:
        """The next scripted version, holding on the last one forever."""
        if len(self._versions) > 1:
            return self._versions.pop(0)
        return self._versions[0]

    def describe(self) -> str:
        return "crates.io"


def test_a_registry_that_moves_is_the_release(capsys):
    clock = _Clock()
    after = artefacts.await_artefact(
        _ScriptedRegistry("2.11.0"),
        "2.10.14",
        slug=SLUG,
        run_id=1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    capsys.readouterr()

    assert after == "2.11.0"
    assert clock.now == 0.0


def test_a_registry_that_never_moves_fails_at_the_ceiling_however_green_the_run(capsys):
    """The publish stage can be gated out and the run still ends green."""
    clock = _Clock()
    raised = ""
    try:
        artefacts.await_artefact(
            _ScriptedRegistry("2.10.14"),
            "2.10.14",
            slug=SLUG,
            run_id=42,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
    except proc.FleetError as exc:
        raised = str(exc)
    capsys.readouterr()

    assert "published NOTHING" in raised
    assert "already green" in raised
    assert "42" in raised
    assert clock.now <= artefacts.ARTEFACT_TIMEOUT


def test_an_unreadable_registry_refuses_to_start(monkeypatch):
    """Without a baseline the shipped-it assertion is a no-op that reports success."""

    def _explode(url: str, **kwargs):
        raise proc.FleetError("GET failed")

    monkeypatch.setattr(artefacts, "http_json", _explode)
    raised = ""
    try:
        artefacts.Artefact(kind="crates", name="scalo").baseline()
    except proc.FleetError as exc:
        raised = str(exc)

    assert "Refusing to start" in raised


if __name__ == "__main__":
    sys.exit(run_module(globals()))
