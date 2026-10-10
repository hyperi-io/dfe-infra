"""Tests for tools/scalo_fleet.py -- the PR-merge fleet release tool.

Real git repositories in temp dirs with a local bare remote, and real stand-in
binaries the tool really execs (no mocks, per the project policy). Nothing here
touches the network or GitHub: every test runs with a scratch PATH whose ``gh``
and ``cargo`` fail by default, so an unexpected call fails offline rather than
reaching out. The one seam is a fake clock for the registry wait, so its
15-minute ceiling can be proven without spending it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import ClassVar

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# The CLI is `scripts/dfe-fleet` -- a hyphen is not an importable module name,
# so it loads by path, the same way test_propagate_stack_pin.py does.
_CLI = REPO_ROOT / "scripts" / "dfe-fleet"
_SPEC = importlib.util.spec_from_file_location(
    "dfe_fleet_cli", _CLI, loader=SourceFileLoader("dfe_fleet_cli", str(_CLI))
)
sf = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sf)

from dfe_suite.landing import dispatch_release
from dfe_suite.rebuild import (
    _check_unoptimized_caller,
    _gate_exclusions,
    _gate_features,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(_GIT_ENV)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def _identify(repo: Path) -> None:
    """Give a temp checkout a local identity.

    The tool's own ``git`` helper inherits the ambient environment, and CI
    runners have no global git identity, so the repo has to carry one.
    """
    _git(repo, "config", "user.name", _GIT_ENV["GIT_AUTHOR_NAME"])
    _git(repo, "config", "user.email", _GIT_ENV["GIT_AUTHOR_EMAIL"])


def _fake_bin(
    directory: Path,
    name: str,
    *,
    stdout: str = "",
    status: int = 0,
    routes: Sequence[tuple[str, str]] = (),
) -> Path:
    """Write an executable stand-in for a binary into a scratch PATH directory.

    A real script the tool really execs, so the fleet's own subprocess path is
    exercised rather than replaced. Every call's argv is appended to
    ``<name>.argv`` beside it, so a test can assert what was asked for.

    Args:
        directory: Scratch directory that is on the front of PATH.
        name: The binary to stand in for, e.g. ``gh``.
        stdout: What the stand-in prints when no route matched, verbatim.
        status: The exit code it returns when no route matched.
        routes: ``(argv fragment, stdout)`` pairs tried in order first, so one
            stand-in answers ``run list`` and ``run view`` differently. The
            fragment matches when those words appear consecutively in the argv.

    Returns:
        The script path.
    """
    lines = ["#!/bin/sh", f'printf "%s\\n" "$@" >> "{directory / (name + ".argv")}"']
    if routes:
        lines.append('case " $* " in')
        for fragment, answer in routes:
            lines.append(f'  *" {fragment} "*)')
            lines.extend(["    cat <<'FAKE_EOF'", answer, "FAKE_EOF", "    exit 0 ;;"])
        lines.append("esac")
    if stdout:
        lines.extend(["cat <<'FAKE_EOF'", stdout, "FAKE_EOF"])
    lines.append(f"exit {status}")
    script = directory / name
    script.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    script.chmod(0o755)
    return script


def _fake_argv(directory: Path, name: str) -> list[str]:
    """Every argument the stand-in binary was called with, in order."""
    record = directory / f"{name}.argv"
    if not record.exists():
        return []
    return record.read_text(encoding="utf-8").splitlines()


def _set_env(saved: dict[str, str]) -> None:
    """Put the environment back exactly as it was."""
    os.environ.clear()
    os.environ.update(saved)


@unittest.skipIf(os.name == "nt", "the PATH stand-ins are POSIX shell scripts")
class OnPathTestCase(unittest.TestCase):
    """A scratch directory on the front of PATH, holding stand-in binaries.

    ``gh`` and ``cargo`` are installed FAILING, so a path that reaches for
    either without the test having said what it should answer breaks offline
    instead of hitting the network. A test that needs one overwrites it.

    Everything is registered with ``addCleanup`` as it is created, so a failure
    part-way through setUp still restores the environment.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="scalo-fleet-path-")
        self.addCleanup(tmp.cleanup)
        self.bindir = Path(tmp.name)
        self.addCleanup(_set_env, dict(os.environ))
        os.environ["PATH"] = f"{self.bindir}{os.pathsep}{os.environ.get('PATH', '')}"
        _fake_bin(self.bindir, "gh", status=1)
        _fake_bin(self.bindir, "cargo", status=1)


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
        """Where this stand-in claims to publish."""
        return "crates.io"


class _FakeClock:
    """A monotonic clock that only moves when the code under test sleeps.

    The registry wait spends up to 15 real minutes, so the ceiling can only be
    proven by owning the clock. Nothing else in the tool takes this seam.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        """The current fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Spend fake time instead of real time."""
        self.slept.append(seconds)
        self.now += seconds


class CommitMessageTests(unittest.TestCase):
    """The squash message is the publish trigger, so its shape is load-bearing."""

    def test_trailer_sits_on_its_own_line(self) -> None:
        body = sf.commit_body(note="Rebuild on scalo 2.11.0.", publish=True)
        lines = body.splitlines()
        assert "Publish: true" in lines

    def test_trailer_matches_the_ci_gate_pattern(self) -> None:
        # predict-version greps `^[[:space:]]*Publish:[[:space:]]*true[[:space:]]*$`
        import re

        gate = re.compile(
            r"^[ \t]*Publish:[ \t]*true[ \t]*$", re.IGNORECASE | re.MULTILINE
        )
        message = "fix: a thing\n\n" + sf.commit_body(note="Note.", publish=True)
        assert re.search(gate, message)

    def test_no_trailer_when_not_publishing(self) -> None:
        body = sf.commit_body(note="Note.", publish=False)
        assert "Publish" not in body

    def test_blank_line_separates_note_from_trailer(self) -> None:
        # Without the blank line git does not treat it as a trailer.
        body = sf.commit_body(note="Note.", publish=True)
        assert "Note.\n\nPublish: true" in body


class SlugTests(unittest.TestCase):
    """Branch names have to be deterministic, so a re-run reuses its own PR."""

    def test_subject_becomes_a_git_safe_branch_fragment(self) -> None:
        assert sf._slugify("fix: the WorkBatch recv model") == "fix-the-workbatch-recv-model"

    def test_same_input_gives_the_same_slug(self) -> None:
        assert sf._slugify("fix: x") == sf._slugify("fix: x")

    def test_long_subject_is_bounded(self) -> None:
        assert len(sf._slugify("word " * 80)) <= 40

    def test_punctuation_only_still_yields_a_name(self) -> None:
        assert sf._slugify("!!! ???") == "change"

    def test_empty_subject_still_yields_a_name(self) -> None:
        assert sf._slugify("") == "change"


class PrNumberTests(unittest.TestCase):
    """gh prints the new PR's URL; the list endpoint is eventually consistent."""

    def test_number_is_read_from_the_url(self) -> None:
        assert sf._pr_number_from_url("https://github.com/hyperi-io/scalo-rs/pull/4213\n") == 4213

    def test_no_url_is_not_a_number(self) -> None:
        assert sf._pr_number_from_url("Warning: something else entirely") is None

    def test_empty_output_is_not_a_number(self) -> None:
        assert sf._pr_number_from_url("") is None


class RedCheckTests(unittest.TestCase):
    """A red check must abort the merge; a pending or skipped one must not."""

    def test_failed_check_run_is_red(self) -> None:
        rollup = [{"name": "Test", "status": "COMPLETED", "conclusion": "FAILURE"}]
        assert sf._red_checks(rollup) == ["Test"]

    def test_errored_status_context_is_red(self) -> None:
        rollup = [{"context": "legacy/build", "state": "ERROR"}]
        assert sf._red_checks(rollup) == ["legacy/build"]

    def test_cancelled_and_timed_out_are_red(self) -> None:
        rollup = [
            {"name": "A", "conclusion": "CANCELLED"},
            {"name": "B", "conclusion": "TIMED_OUT"},
        ]
        assert sf._red_checks(rollup) == ["A", "B"]

    def test_pending_success_and_skipped_are_not_red(self) -> None:
        rollup = [
            {"name": "Quality", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "Build", "status": "IN_PROGRESS"},
            {"name": "Optional", "status": "COMPLETED", "conclusion": "SKIPPED"},
            {"name": "Neutral", "status": "COMPLETED", "conclusion": "NEUTRAL"},
        ]
        assert sf._red_checks(rollup) == []

    def test_empty_rollup_is_not_red(self) -> None:
        # Empty means "no checks reported yet", never "nothing failed, merge it".
        assert sf._red_checks([]) == []

    def test_junk_rollup_does_not_explode(self) -> None:
        assert sf._red_checks("not a list") == []
        assert sf._red_checks([None, 7, "x"]) == []


class RunSelectionTests(unittest.TestCase):
    """Which run for this sha, once gh has filtered the workflow server-side."""

    # What `gh run list --workflow ci.yml` answers with: newest first, ids
    # ascending with age. TWO runs on one sha is the ship escape hatch -- the
    # push run that landed it, then the run `hyperi-ci publish` dispatched.
    ROWS: ClassVar[list[dict]] = [
        {"databaseId": 33054900002, "headSha": "abc123", "workflowName": "CI"},
        {"databaseId": 33054359645, "headSha": "abc123", "workflowName": "CI"},
        {"databaseId": 33054000001, "headSha": "def456", "workflowName": "CI"},
    ]

    def test_the_newest_run_for_the_sha_wins(self) -> None:
        found = sf._select_run(self.ROWS, "abc123")
        assert found is not None
        assert found.id == 33054900002

    def test_the_snapshotted_run_is_excluded_so_the_dispatch_is_followed(self) -> None:
        found = sf._select_run(self.ROWS, "abc123", after_run_id=33054359645)
        assert found is not None
        assert found.id == 33054900002

    def test_only_the_snapshotted_run_means_the_dispatch_has_not_landed(self) -> None:
        # The dispatched run has not registered yet: the pre-existing green run
        # must NOT be handed back as if it were the release.
        rows = [self.ROWS[1], self.ROWS[2]]
        assert sf._select_run(rows, "abc123", after_run_id=33054359645) is None

    def test_a_run_for_another_sha_is_never_selected(self) -> None:
        found = sf._select_run(self.ROWS, "def456")
        assert found is not None
        assert found.id == 33054000001

    def test_the_workflow_name_rides_along_for_the_log_line(self) -> None:
        found = sf._select_run(self.ROWS, "abc123")
        assert found is not None
        assert found.workflow == "CI"

    def test_a_row_without_a_run_id_is_skipped_not_fatal(self) -> None:
        rows = [{"headSha": "abc123", "workflowName": "CI"}, self.ROWS[1]]
        found = sf._select_run(rows, "abc123")
        assert found is not None
        assert found.id == 33054359645

    def test_a_row_with_an_unusable_run_id_is_skipped(self) -> None:
        rows = [{"databaseId": "not-a-number", "headSha": "abc123"}]
        assert sf._select_run(rows, "abc123") is None

    def test_no_run_for_the_sha_yet(self) -> None:
        assert sf._select_run(self.ROWS, "999999") is None

    def test_junk_payload_does_not_explode(self) -> None:
        assert sf._select_run("not a list", "abc123") is None
        assert sf._select_run([None, 7, "x"], "abc123") is None


class FindRunForShaTests(OnPathTestCase):
    """What the tool asks gh for, and which run it takes back."""

    PAYLOAD = (
        '[{"databaseId":33054900002,"headSha":"abc123","workflowName":"CI"},'
        '{"databaseId":33054359645,"headSha":"abc123","workflowName":"CI"}]'
    )

    def test_the_workflow_file_and_branch_are_filtered_by_gh(self) -> None:
        # The workflow FILE, not the display name: `name:` can be edited, the
        # path cannot drift, and gh does the filtering server-side.
        _fake_bin(self.bindir, "gh", stdout=self.PAYLOAD)
        sf.find_run_for_sha("hyperi-io/dfe-engine", "abc123")
        argv = _fake_argv(self.bindir, "gh")
        assert argv[argv.index("--workflow") + 1] == "ci.yml"
        assert argv[argv.index("--branch") + 1] == "main"
        assert "databaseId,headSha,workflowName" in argv

    def test_the_newest_run_is_taken_back(self) -> None:
        _fake_bin(self.bindir, "gh", stdout=self.PAYLOAD)
        assert sf.find_run_for_sha("hyperi-io/dfe-engine", "abc123") == 33054900002

    def test_a_snapshotted_run_is_refused(self) -> None:
        _fake_bin(self.bindir, "gh", stdout=self.PAYLOAD)
        assert sf.find_run_for_sha("hyperi-io/dfe-engine", "abc123", after_run_id=33054900002) is None


class PollRunTests(OnPathTestCase):
    """Following the run is where a red job is caught and named."""

    GREEN = (
        '{"status":"completed","conclusion":"success",'
        '"jobs":[{"name":"publish","conclusion":"success"}]}'
    )
    RED = (
        '{"status":"in_progress","conclusion":null,'
        '"jobs":[{"name":"publish","conclusion":"failure"}]}'
    )

    def test_a_green_run_returns(self) -> None:
        _fake_bin(self.bindir, "gh", stdout=self.GREEN)
        sf.poll_run("hyperi-io/dfe-engine", 33054359645, timeout=60)

    def test_a_red_job_is_named_and_stops_the_release(self) -> None:
        _fake_bin(self.bindir, "gh", stdout=self.RED)
        with pytest.raises(sf.FleetError) as caught:
            sf.poll_run("hyperi-io/dfe-engine", 33054359645, timeout=60)
        message = str(caught.value)
        assert "publish" in message
        assert "RED" in message
        assert "published NOTHING" not in message


class ArtefactWaitTests(OnPathTestCase):
    """The wait is registry patience, bounded by one named ceiling (#83)."""

    SLUG = "hyperi-io/dfe-engine"
    RUN = 33054359645

    def _wait(self, registry: _ScriptedRegistry, clock: _FakeClock) -> str:
        return sf.await_artefact(
            registry,
            "2.29.17",
            slug=self.SLUG,
            run_id=self.RUN,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    def test_a_moved_registry_is_the_release(self) -> None:
        clock = _FakeClock()
        assert self._wait(_ScriptedRegistry("2.29.18"), clock) == "2.29.18"
        assert clock.slept == []

    def test_a_registry_that_moves_in_the_third_window_is_still_the_release(
        self,
    ) -> None:
        # 300s windows at a 15s cadence: 20 reads a window, so an index that
        # only catches up on read 42 has cost two full windows of patience.
        clock = _FakeClock()
        registry = _ScriptedRegistry(*["2.29.17"] * 41, "2.29.18")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            after = self._wait(registry, clock)
        assert after == "2.29.18"
        assert buffer.getvalue().count("the index lags the publish") == 2

    def test_a_registry_that_never_moves_fails_at_the_ceiling(self) -> None:
        clock = _FakeClock()
        with pytest.raises(sf.FleetError) as caught:
            self._wait(_ScriptedRegistry("2.29.17"), clock)
        message = str(caught.value)
        assert "published NOTHING" in message
        # The verdict is worded off what happened: the run was already green.
        assert "already green" in message
        assert str(self.RUN) in message

    def test_the_wait_is_bounded_by_the_artefact_ceiling(self) -> None:
        clock = _FakeClock()
        with pytest.raises(sf.FleetError):
            self._wait(_ScriptedRegistry("2.29.17"), clock)
        assert clock.now <= sf.ARTEFACT_TIMEOUT
        assert clock.now > sf.ARTEFACT_TIMEOUT - sf.REGISTRY_POLL_SECONDS

    def test_the_wait_never_re_polls_the_run(self) -> None:
        # The caller has already followed the run to green, so a `gh run view`
        # here can only ever see a finished run and answer instantly.
        clock = _FakeClock()
        with pytest.raises(sf.FleetError):
            self._wait(_ScriptedRegistry("2.29.17"), clock)
        assert _fake_argv(self.bindir, "gh") == []


class FollowReleaseTests(OnPathTestCase):
    """The release tail end to end: find the run, follow it, judge the registry."""

    RUNS = '[{"databaseId":33054359645,"headSha":"abc123","workflowName":"CI"}]'
    GREEN = (
        '{"status":"completed","conclusion":"success",'
        '"jobs":[{"name":"publish","conclusion":"success"}]}'
    )

    def test_the_selected_run_is_followed_and_the_registry_has_the_last_word(
        self,
    ) -> None:
        _fake_bin(
            self.bindir,
            "gh",
            routes=[("run list", self.RUNS), ("run view", self.GREEN)],
            status=1,
        )
        registry = _ScriptedRegistry("2.29.18")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            sf.follow_release(
                slug="hyperi-io/dfe-engine",
                sha="abc123",
                artefact=registry,
                before="2.29.17",
                timeout=60,
            )
        argv = _fake_argv(self.bindir, "gh")
        # The run id from the list is what the view was asked about: the slug
        # and run id really are threaded through, not defaulted.
        assert argv[argv.index("view") + 1] == "33054359645"
        assert "SHIPPED: scalo 2.29.17 -> 2.29.18" in buffer.getvalue()


class RebuildRsTests(OnPathTestCase):
    """A Rust rebuild regenerates the Dockerfile and docs/ artefacts, and no chart."""

    def setUp(self) -> None:
        super().setUp()
        root_tmp = tempfile.TemporaryDirectory(prefix="scalo-fleet-rebuild-")
        self.addCleanup(root_tmp.cleanup)
        root = Path(root_tmp.name)
        origin = root / "origin.git"
        _git(root, "init", "--bare", "-b", "main", str(origin))
        seed = root / "seed"
        _git(root, "clone", str(origin), str(seed))
        _identify(seed)
        (seed / "Cargo.toml").write_text('[package]\nname = "app"\n', encoding="utf-8")
        _git(seed, "add", "Cargo.toml")
        _git(seed, "commit", "-m", "chore: seed")
        _git(seed, "push", "origin", "main")

        self.repo = root / "dfe-receiver"
        _git(root, "clone", str(origin), str(self.repo))
        _identify(self.repo)

        # A cargo that answers everything, so the run reaches the end of the
        # regeneration steps rather than stopping on the toolchain.
        _fake_bin(self.bindir, "cargo", stdout="FROM scratch")
        os.environ["SCALO_REBUILD_TARGET"] = str(root / "target")

    def _args(self, *extra: str) -> object:
        return sf.build_parser().parse_args(
            ["rebuild-rs", str(self.repo), "2.11.0", *extra]
        )

    def test_a_rebuild_regenerates_the_dockerfile_and_config_artefacts(self) -> None:
        code = sf.cmd_rebuild_rs(self._args("--no-tests"))
        argv = _fake_argv(self.bindir, "cargo")
        assert code == 0
        assert "emit-dockerfile" in argv
        assert "config-schema" in argv

    def test_the_dry_run_names_the_dockerfile_and_docs_artefacts_only(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            sf.cmd_rebuild_rs(self._args("-n"))
        output = buffer.getvalue()
        assert "the Dockerfile and docs/ artefacts" in output
        assert "chart/" not in output


# dfe-fetcher's workspace as `cargo metadata --no-deps` reports it, cut to the
# features that matter: the db crate's `odbc` links unixODBC, and the app's
# `db-odbc` and `full` both turn it on.
_FETCHER_METADATA = {
    "packages": [
        {
            "name": "dfe-fetcher",
            "features": {
                "default": [],
                "jemalloc": ["dep:tikv-jemallocator"],
                "db-odbc": ["dfe-fetcher-db/odbc"],
                "db-clickhouse": ["dfe-fetcher-db/clickhouse"],
                "file": [],
                "file-tail": ["file", "dfe-fetcher-file/tail"],
                "full": ["jemalloc", "db-odbc", "db-clickhouse", "file-tail"],
            },
            "dependencies": [
                {"name": "dfe-fetcher-db", "rename": None},
                {"name": "dfe-fetcher-file", "rename": None},
                {"name": "tikv-jemallocator", "rename": None},
            ],
        },
        {
            "name": "dfe-fetcher-db",
            "features": {
                "default": [],
                "odbc": ["dep:odbc-api"],
                "clickhouse": ["dep:clickhouse"],
            },
            "dependencies": [],
        },
        {
            "name": "dfe-fetcher-file",
            "features": {"default": [], "tail": ["dep:file-source"]},
            "dependencies": [],
        },
    ]
}

_FETCHER_GATE = [
    "--no-default-features",
    "--features",
    "dfe-fetcher/db-clickhouse,dfe-fetcher/default,dfe-fetcher/file,dfe-fetcher/file-tail,"
    "dfe-fetcher/jemalloc,dfe-fetcher-db/clickhouse,dfe-fetcher-db/default,"
    "dfe-fetcher-file/default,dfe-fetcher-file/tail",
]

_METADATA_CALL = "metadata --format-version 1 --no-deps"


class GateFeaturesTests(OnPathTestCase):
    """The local gate builds every feature but those a suite node keeps out."""

    def setUp(self) -> None:
        super().setUp()
        repo_tmp = tempfile.TemporaryDirectory(prefix="scalo-fleet-features-")
        self.addCleanup(repo_tmp.cleanup)
        self.repo = Path(repo_tmp.name)

    def _metadata(self, payload: dict) -> None:
        _fake_bin(self.bindir, "cargo", routes=[(_METADATA_CALL, json.dumps(payload))], status=1)

    def test_nothing_excluded_is_all_features_and_asks_cargo_nothing(self) -> None:
        assert _gate_features(self.repo, []) == ["--all-features"]
        assert _fake_argv(self.bindir, "cargo") == []

    def test_the_excluded_feature_and_every_feature_that_turns_it_on_are_dropped(self) -> None:
        # `full` is the umbrella: listing it would link unixODBC all over again.
        self._metadata(_FETCHER_METADATA)
        assert _gate_features(self.repo, ["dfe-fetcher-db/odbc"]) == _FETCHER_GATE
        assert _contains(_fake_argv(self.bindir, "cargo"), _METADATA_CALL.split()) != -1

    def test_a_feature_reaching_it_through_a_renamed_weak_dependency_is_dropped(self) -> None:
        # A feature names a dependency by its Cargo.toml key, which a rename changes.
        payload = {
            "packages": [
                {
                    "name": "app",
                    "features": {"default": [], "sql": ["db?/odbc"], "fast": []},
                    "dependencies": [{"name": "dfe-fetcher-db", "rename": "db"}],
                },
                _FETCHER_METADATA["packages"][1],
            ]
        }
        self._metadata(payload)
        features = _gate_features(self.repo, ["dfe-fetcher-db/odbc"])[2].split(",")
        assert "app/sql" not in features
        assert "app/fast" in features

    def test_an_exclusion_naming_no_feature_is_refused(self) -> None:
        # A stale entry must not quietly exclude nothing.
        self._metadata(_FETCHER_METADATA)
        with pytest.raises(sf.FleetError) as caught:
            _gate_features(self.repo, ["dfe-fetcher-db/odbc-api"])
        assert "dfe-fetcher-db/odbc-api" in str(caught.value)

    def test_metadata_that_does_not_describe_a_workspace_is_refused(self) -> None:
        _fake_bin(self.bindir, "cargo", stdout="not json")
        with pytest.raises(sf.FleetError) as caught:
            _gate_features(self.repo, ["dfe-fetcher-db/odbc"])
        assert "cargo metadata --format-version 1 --no-deps" in str(caught.value)

    def test_the_suite_graph_keeps_odbc_out_of_the_fetcher_gate_only(self) -> None:
        # The committed suite.yaml, read the way a rebuild reads it.
        assert _gate_exclusions("dfe-fetcher") == ["dfe-fetcher-db/odbc"]
        assert _gate_exclusions("dfe-loader") == []
        assert _gate_exclusions("not-a-suite-member-xyzzy") == []


class RebuildRsGateFeaturesTests(OnPathTestCase):
    """A suite node's exclusion reaches clippy and nextest alike."""

    def setUp(self) -> None:
        super().setUp()
        root_tmp = tempfile.TemporaryDirectory(prefix="scalo-fleet-gate-")
        self.addCleanup(root_tmp.cleanup)
        root = Path(root_tmp.name)
        origin = root / "origin.git"
        _git(root, "init", "--bare", "-b", "main", str(origin))
        seed = root / "seed"
        _git(root, "clone", str(origin), str(seed))
        _identify(seed)
        (seed / "Cargo.toml").write_text('[package]\nname = "app"\n', encoding="utf-8")
        _git(seed, "add", "Cargo.toml")
        _git(seed, "commit", "-m", "chore: seed")
        _git(seed, "push", "origin", "main")

        # Named for the suite node whose committed exclusion is under test.
        self.repo = root / "dfe-fetcher"
        _git(root, "clone", str(origin), str(self.repo))
        _identify(self.repo)
        _fake_bin(
            self.bindir,
            "cargo",
            stdout="FROM scratch",
            routes=[(_METADATA_CALL, json.dumps(_FETCHER_METADATA))],
        )
        _fake_bin(self.bindir, "cargo-nextest")
        os.environ["SCALO_REBUILD_TARGET"] = str(root / "target")

    def _rebuild(self, *extra: str) -> str:
        args = sf.build_parser().parse_args(["rebuild-rs", str(self.repo), "2.11.0", *extra])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            assert sf.cmd_rebuild_rs(args) == 0
        return buffer.getvalue()

    def test_clippy_and_the_test_suite_build_without_the_excluded_feature(self) -> None:
        output = self._rebuild()
        argv = _fake_argv(self.bindir, "cargo")
        clippy = ["clippy", "--workspace", "--all-targets", *_FETCHER_GATE, "--", "-D", "warnings"]
        assert _contains(argv, clippy) != -1
        assert _contains(argv, ["nextest", "run", "--workspace", *_FETCHER_GATE]) != -1
        assert "--all-features" not in argv
        assert "(suite.yaml keeps dfe-fetcher-db/odbc out)" in output


class FindRepoTests(unittest.TestCase):
    """No hardcoded disk layout: macOS and Linux both have to resolve."""

    def setUp(self) -> None:
        self._saved = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)

    def test_env_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "scalo-rs"
            (repo / ".git").mkdir(parents=True)
            os.environ["SCALO_RS_DIR"] = str(repo)
            assert sf.find_repo("scalo-rs") == repo

    def test_env_override_must_be_a_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SCALO_RS_DIR"] = tmp  # exists, but no .git
            with pytest.raises(sf.FleetError):
                sf.find_repo("scalo-rs")

    def test_projects_root_is_searched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "scalo-py"
            (repo / ".git").mkdir(parents=True)
            os.environ.pop("SCALO_PY_DIR", None)
            os.environ["HYPERI_PROJECTS_ROOT"] = tmp
            assert sf.find_repo("scalo-py") == repo

    def test_a_directory_without_git_does_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "not-a-repo-xyzzy").mkdir()
            os.environ.pop("NOT_A_REPO_XYZZY_DIR", None)
            os.environ["HYPERI_PROJECTS_ROOT"] = tmp
            with pytest.raises(sf.FleetError) as caught:
                sf.find_repo("not-a-repo-xyzzy")
            # The message has to name what was tried, or it is unactionable.
            assert "NOT_A_REPO_XYZZY_DIR" in str(caught.value)


class LandOnMainTests(unittest.TestCase):
    """The git half of land_via_pr, against a real bare remote."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="scalo-fleet-test-")
        root = Path(self._tmp.name)
        self.origin = root / "origin.git"
        _git(root, "init", "--bare", "-b", "main", str(self.origin))

        seed = root / "seed"
        _git(root, "clone", str(self.origin), str(seed))
        _identify(seed)
        (seed / "README.md").write_text("base\n", encoding="utf-8")
        _git(seed, "add", "README.md")
        _git(seed, "commit", "-m", "chore: seed")
        _git(seed, "push", "origin", "main")

        self.work = root / "work"
        _git(root, "clone", str(self.origin), str(self.work))
        _identify(self.work)

        # Someone else lands on main while we are mid-change.
        (seed / "THEIRS.md").write_text("theirs\n", encoding="utf-8")
        _git(seed, "add", "THEIRS.md")
        _git(seed, "commit", "-m", "fix: someone else")
        _git(seed, "push", "origin", "main")
        self.seed = seed

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _land(self, branch: str = "release/the-fix") -> None:
        """The sequence land_via_pr runs before it hands over to gh."""
        sf.git("switch", "-C", branch, cwd=self.work)
        sf.git(
            "commit",
            "-m",
            "fix: the thing",
            "-m",
            sf.commit_body(note="Release scalo.", publish=True),
            cwd=self.work,
        )
        sf.git("fetch", "origin", "main", "--tags", "--quiet", cwd=self.work)
        sf.git("rebase", "--autostash", "origin/main", cwd=self.work)
        sf.git("push", "--set-upstream", "origin", branch, cwd=self.work)

    def test_staged_fix_is_detected(self) -> None:
        assert not sf.has_staged_changes(self.work)
        (self.work / "FIX.md").write_text("the fix\n", encoding="utf-8")
        _git(self.work, "add", "FIX.md")
        assert sf.has_staged_changes(self.work)

    def test_staged_fix_reaches_the_commit_and_wip_survives(self) -> None:
        (self.work / "FIX.md").write_text("the fix\n", encoding="utf-8")
        _git(self.work, "add", "FIX.md")
        (self.work / "README.md").write_text("base\nunrelated wip\n", encoding="utf-8")

        self._land()

        touched = _git(self.work, "show", "--name-only", "--format=", "HEAD").split()
        assert touched == ["FIX.md"]
        assert "unrelated wip" in (self.work / "README.md").read_text(encoding="utf-8")

    def test_branch_rebases_onto_the_newer_main(self) -> None:
        (self.work / "FIX.md").write_text("the fix\n", encoding="utf-8")
        _git(self.work, "add", "FIX.md")
        self._land()
        assert (self.work / "THEIRS.md").exists()

    def test_the_commit_carries_the_publish_trailer(self) -> None:
        (self.work / "FIX.md").write_text("the fix\n", encoding="utf-8")
        _git(self.work, "add", "FIX.md")
        self._land()
        message = _git(self.work, "log", "-1", "--format=%B")
        assert message.splitlines()[0] == "fix: the thing"
        assert "Publish: true" in message.splitlines()

    def test_main_is_never_pushed_to(self) -> None:
        (self.work / "FIX.md").write_text("the fix\n", encoding="utf-8")
        _git(self.work, "add", "FIX.md")
        self._land()
        # The remote's main still ends at the other person's commit: the fleet
        # lands through a PR, never a push.
        assert _git(self.origin, "log", "-1", "--format=%s", "main") == "fix: someone else"

    def test_branch_reaches_the_remote(self) -> None:
        (self.work / "FIX.md").write_text("the fix\n", encoding="utf-8")
        _git(self.work, "add", "FIX.md")
        self._land()
        assert "release/the-fix" in _git(self.origin, "branch", "--list", "release/*")

    def test_diverged_repush_is_rejected_before_a_lease_replaces_it(self) -> None:
        (self.work / "FIX.md").write_text("the fix\n", encoding="utf-8")
        _git(self.work, "add", "FIX.md")
        self._land()

        # An interrupted run re-runs and rewrites its own branch.
        (self.work / "FIX.md").write_text("the fix, take two\n", encoding="utf-8")
        _git(self.work, "add", "FIX.md")
        _git(self.work, "commit", "--amend", "--no-edit")

        plain = subprocess.run(
            ["git", "push", "--set-upstream", "origin", "release/the-fix"],
            cwd=str(self.work),
            capture_output=True,
            text=True,
            check=False,
        )
        assert plain.returncode != 0, "a diverged push must not fast-forward"

        sf.git("fetch", "origin", "release/the-fix", "--quiet", cwd=self.work)
        sf.git(
            "push",
            "--force-with-lease",
            "--set-upstream",
            "origin",
            "release/the-fix",
            cwd=self.work,
        )
        assert _git(self.origin, "log", "-1", "--format=%s", "release/the-fix") == "fix: the thing"

    def test_head_sha_and_branch_report_the_truth(self) -> None:
        (self.work / "FIX.md").write_text("the fix\n", encoding="utf-8")
        _git(self.work, "add", "FIX.md")
        self._land()
        assert sf.current_branch(self.work) == "release/the-fix"
        assert sf.head_sha(self.work) == _git(self.work, "rev-parse", "HEAD")


class SyncMainTests(unittest.TestCase):
    """rebuild-* syncs before it changes anything, and must not lose WIP."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="scalo-fleet-sync-")
        root = Path(self._tmp.name)
        origin = root / "origin.git"
        _git(root, "init", "--bare", "-b", "main", str(origin))
        seed = root / "seed"
        _git(root, "clone", str(origin), str(seed))
        _identify(seed)
        (seed / "a.txt").write_text("1\n", encoding="utf-8")
        _git(seed, "add", "a.txt")
        _git(seed, "commit", "-m", "chore: seed")
        _git(seed, "push", "origin", "main")
        self.work = root / "work"
        _git(root, "clone", str(origin), str(self.work))
        _identify(self.work)
        (seed / "b.txt").write_text("2\n", encoding="utf-8")
        _git(seed, "add", "b.txt")
        _git(seed, "commit", "-m", "fix: theirs")
        _git(seed, "push", "origin", "main")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sync_fast_forwards_and_keeps_wip(self) -> None:
        (self.work / "a.txt").write_text("1\nmy wip\n", encoding="utf-8")
        sf.sync_main(self.work, dry_run=False)
        assert (self.work / "b.txt").exists()
        assert "my wip" in (self.work / "a.txt").read_text(encoding="utf-8")

    def test_sync_returns_to_main_from_another_branch(self) -> None:
        _git(self.work, "switch", "-c", "some-side-branch")
        sf.sync_main(self.work, dry_run=False)
        assert sf.current_branch(self.work) == "main"

    def test_dry_run_changes_nothing(self) -> None:
        before = sf.head_sha(self.work)
        sf.sync_main(self.work, dry_run=True)
        assert sf.head_sha(self.work) == before
        assert not (self.work / "b.txt").exists()

    def test_a_worktree_holding_main_is_named_not_left_to_git(self) -> None:
        # scalo-rs, dfe-engine and dfe-fetcher all had main checked out in a
        # worktree while the tool ran against the primary checkout.
        _git(self.work, "switch", "-c", "some-side-branch")
        holder = Path(self._tmp.name) / "holder"
        _git(self.work, "worktree", "add", str(holder), "main")
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                with pytest.raises(sf.FleetError) as caught:
                    sf.sync_main(self.work, dry_run=dry_run)
                message = str(caught.value)
                # macOS parks temp dirs behind a /var -> /private/var symlink.
                assert str(holder) in message or str(holder.resolve()) in message
                assert "Run the tool against" in message
                assert "already used by worktree" not in message
        assert sf.current_branch(self.work) == "some-side-branch"

    def test_running_against_the_worktree_that_holds_main_syncs(self) -> None:
        _git(self.work, "switch", "-c", "some-side-branch")
        holder = Path(self._tmp.name) / "holder"
        _git(self.work, "worktree", "add", str(holder), "main")
        sf.sync_main(holder, dry_run=False)
        assert (holder / "b.txt").exists()


class PyConstraintTests(unittest.TestCase):
    """Extras are load-bearing and a `>=` floor is not a pin."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="scalo-fleet-py-")
        self.repo = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _pyproject(self, text: str) -> None:
        (self.repo / "pyproject.toml").write_text(text, encoding="utf-8")

    def test_extras_are_preserved_when_the_floor_moves(self) -> None:
        self._pyproject(
            'dependencies = ["scalo[expression,http,metrics]>=2.29.7", "httpx>=0.27"]\n'
        )
        sf._bump_py_constraint(self.repo, "2.29.10")
        text = (self.repo / "pyproject.toml").read_text(encoding="utf-8")
        assert '"scalo[expression,http,metrics]>=2.29.10"' in text
        assert '"httpx>=0.27"' in text

    def test_a_bare_scalo_constraint_also_moves(self) -> None:
        self._pyproject('dependencies = ["scalo>=2.0.0"]\n')
        sf._bump_py_constraint(self.repo, "2.29.10")
        assert '"scalo>=2.29.10"' in (self.repo / "pyproject.toml").read_text(encoding="utf-8")

    def test_a_missing_constraint_is_a_hard_failure(self) -> None:
        self._pyproject('dependencies = ["httpx>=0.27"]\n')
        with pytest.raises(sf.FleetError):
            sf._bump_py_constraint(self.repo, "2.29.10")

    def test_lock_assertion_accepts_the_expected_version(self) -> None:
        (self.repo / "uv.lock").write_text(
            '[[package]]\nname = "scalo"\nversion = "2.29.10"\n', encoding="utf-8"
        )
        sf._assert_locked(self.repo, "2.29.10")

    def test_lock_assertion_rejects_a_stale_resolution(self) -> None:
        (self.repo / "uv.lock").write_text(
            '[[package]]\nname = "scalo"\nversion = "2.29.7"\n', encoding="utf-8"
        )
        with pytest.raises(sf.FleetError) as caught:
            sf._assert_locked(self.repo, "2.29.10")
        assert "2.29.7" in str(caught.value)

    def test_lock_assertion_rejects_a_missing_package(self) -> None:
        (self.repo / "uv.lock").write_text(
            '[[package]]\nname = "httpx"\nversion = "0.27.0"\n', encoding="utf-8"
        )
        with pytest.raises(sf.FleetError):
            sf._assert_locked(self.repo, "2.29.10")


class RequireToolsTests(unittest.TestCase):
    """Fail before doing any work, naming every gap at once."""

    def test_a_present_tool_passes(self) -> None:
        sf.require_tools("git")

    def test_missing_tools_are_all_named(self) -> None:
        with pytest.raises(sf.FleetError) as caught:
            sf.require_tools(
                "git", "definitely-not-a-binary-xyzzy", "also-not-here-xyzzy"
            )
        message = str(caught.value)
        assert "definitely-not-a-binary-xyzzy" in message
        assert "also-not-here-xyzzy" in message


class CliTests(unittest.TestCase):
    """The dry-run flag has to work either side of the subcommand."""

    def test_dry_run_before_the_subcommand(self) -> None:
        args = sf.build_parser().parse_args(["-n", "ship-rs"])
        assert args.dry_run

    def test_dry_run_after_the_subcommand(self) -> None:
        args = sf.build_parser().parse_args(["ship-rs", "-n"])
        assert args.dry_run

    def test_subcommand_without_the_flag_keeps_the_global_default(self) -> None:
        args = sf.build_parser().parse_args(["ship-rs"])
        assert not args.dry_run

    def test_rebuild_rs_takes_no_tests_and_rebuild_py_does_not(self) -> None:
        rs = sf.build_parser().parse_args(["rebuild-rs", "/x", "1.0.0", "--no-tests"])
        assert rs.no_tests
        with pytest.raises(SystemExit):
            sf.build_parser().parse_args(["rebuild-py", "/x", "1.0.0", "--no-tests"])

    def test_rebuild_rs_takes_release_unoptimized_and_rebuild_py_does_not(self) -> None:
        # dfe-engine is Python and has no optimisation stage to consent to skipping.
        rs = sf.build_parser().parse_args(["rebuild-rs", "/x", "1.0.0", "--release-unoptimized"])
        assert rs.release_unoptimized
        assert not sf.build_parser().parse_args(["rebuild-rs", "/x", "1.0.0"]).release_unoptimized
        with pytest.raises(SystemExit):
            sf.build_parser().parse_args(["rebuild-py", "/x", "1.0.0", "--release-unoptimized"])

    def test_a_subcommand_is_required(self) -> None:
        with pytest.raises(SystemExit):
            sf.build_parser().parse_args([])


# ---------------------------------------------------------------------------
# Unoptimised release: the consent preflight, the trailer-less land, and the
# consented dispatch
# ---------------------------------------------------------------------------

_CONSENT = ("skip-optimize", "release-unoptimized")

_CALLER_HEAD = """\
name: CI

on:
  push:
    branches: ["**"]
  pull_request:
    branches: [main]
  workflow_dispatch:
    inputs:
      from-head:
        type: string
        required: false
        default: ""
      bump:
        type: string
        required: false
        default: "auto"
"""

_CALLER_JOB = """\

jobs:
  ci:
    uses: example-org/ci/.github/workflows/rust-ci.yml@main
    with:
      from-head: ${{ inputs.from-head || '' }}
      bump: ${{ inputs.bump || 'auto' }}
"""

_CALLER_SECRETS = """\
    secrets:
      CARGO_REGISTRY_TOKEN: ${{ secrets.CARGO_REGISTRY_TOKEN }}
"""


def _commented(text: str) -> str:
    """The same lines, each turned into a YAML comment at its own indent."""
    return "".join(
        f"{line[: len(line) - len(line.lstrip())]}# {line.lstrip()}\n"
        for line in text.splitlines()
    )


def _caller_yml(
    *,
    declared: Sequence[str] = _CONSENT,
    forwarded: Sequence[str] = _CONSENT,
    commented: Sequence[str] = (),
    values: dict[str, str] | None = None,
) -> str:
    """A caller ci.yml shaped like the fleet's, with the consent inputs varied.

    Args:
        declared: Consent inputs declared under workflow_dispatch.
        forwarded: Consent inputs passed in the reusable workflow's with:.
        commented: Consent inputs present in BOTH places, but only as comments.
        values: A with: value to use instead of ``${{ inputs.<name> || '' }}``.
    """
    values = values or {}
    inputs = ""
    passes = ""
    for name in _CONSENT:
        block = f'      {name}:\n        type: string\n        required: false\n        default: ""\n'
        line = f"      {name}: " + values.get(name, "${{ inputs." + name + " || '' }}") + "\n"
        if name in commented:
            inputs += _commented(block)
            passes += _commented(line)
            continue
        if name in declared:
            inputs += block
        if name in forwarded:
            passes += line
    return _CALLER_HEAD + inputs + _CALLER_JOB + passes + _CALLER_SECRETS


def _contains(seq: Sequence[str], sub: Sequence[str]) -> int:
    """Where ``sub`` first sits contiguously inside ``seq``, or -1."""
    width = len(sub)
    for start in range(len(seq) - width + 1):
        if list(seq[start : start + width]) == list(sub):
            return start
    return -1


def _dispatching_gh(
    directory: Path, *, sha: str, push_run: int, dispatch_run: int, push_appears: int = 1
) -> None:
    """A gh stand-in for a whole land-then-dispatch release.

    The same argv log as ``_fake_bin``. ``workflow run`` leaves a marker, and
    ``run list`` and ``release list`` answer from it: before the dispatch the
    sha carries only the merge's push run and the release is the old tag, after
    it the dispatched run has registered and the release has moved. Anything
    unrouted fails, so ``repo view`` falls back to the org and the dir name.

    Args:
        directory: Scratch directory that is on the front of PATH.
        sha: The head commit every run belongs to.
        push_run: The merge's push run id.
        dispatch_run: The id the dispatched run registers under.
        push_appears: The ``run list`` call on which the push run first shows.
    """
    marker = directory / "gh.dispatched"
    count = directory / "gh.run-lists"
    row = '{{"databaseId":{id},"headSha":"' + sha + '","workflowName":"CI"}}'
    before_rows = "[" + row.format(id=push_run) + "]"
    after_rows = "[" + row.format(id=dispatch_run) + "," + row.format(id=push_run) + "]"
    green = (
        '{"status":"completed","conclusion":"success",'
        '"jobs":[{"name":"publish","conclusion":"success"}]}'
    )
    clean = '{"mergeStateStatus":"CLEAN","mergeable":"MERGEABLE","statusCheckRollup":[]}'
    lines = [
        "#!/bin/sh",
        f'printf "%s\\n" "$@" >> "{directory / "gh.argv"}"',
        'case " $* " in',
        f'  *" workflow run "*) : > "{marker}"; exit 0 ;;',
        '  *" run list "*)',
        f'    echo x >> "{count}"',
        f'    if [ -e "{marker}" ]; then echo \'{after_rows}\'',
        # Unquoted: BSD wc pads the count with spaces, which word splitting drops.
        f'    elif [ $(wc -l < "{count}") -ge {push_appears} ]; then echo \'{before_rows}\'',
        "    else echo '[]'; fi",
        "    exit 0 ;;",
        f"  *\" run view \"*) echo '{green}'; exit 0 ;;",
        '  *" release list "*)',
        f'    if [ -e "{marker}" ]; then echo \'[{{"tagName":"v1.0.1"}}]\'; '
        "else echo '[{\"tagName\":\"v1.0.0\"}]'; fi",
        "    exit 0 ;;",
        "  *\" pr list \"*) echo '[]'; exit 0 ;;",
        '  *" pr create "*) echo "https://github.com/example-org/dfe-app/pull/7"; exit 0 ;;',
        f"  *\" pr view \"*) echo '{clean}'; exit 0 ;;",
        '  *" pr merge "*) exit 0 ;;',
        "esac",
        "exit 1",
    ]
    script = directory / "gh"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    script.chmod(0o755)


class UnoptimizedCallerTests(unittest.TestCase):
    """The preflight reads the caller's working tree, parsed, before anything runs."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="scalo-fleet-caller-")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.workflow = self.repo / ".github" / "workflows" / "ci.yml"
        self.workflow.parent.mkdir(parents=True)

    def _check(self, text: str) -> str:
        """The preflight's refusal for a caller with this body."""
        self.workflow.write_text(text, encoding="utf-8")
        with pytest.raises(sf.FleetError) as caught:
            _check_unoptimized_caller(self.repo)
        message = str(caught.value)
        assert str(self.workflow) in message
        return message

    def test_a_caller_that_declares_and_forwards_both_passes(self) -> None:
        self.workflow.write_text(_caller_yml(), encoding="utf-8")
        assert _check_unoptimized_caller(self.repo) == self.workflow

    def test_a_caller_with_neither_input_names_all_four_missing_pieces(self) -> None:
        message = self._check(_caller_yml(declared=(), forwarded=()))
        for name in _CONSENT:
            assert f"on.workflow_dispatch.inputs.{name} is not declared" in message
            assert f"jobs.ci.with.{name} does not pass inputs.{name}" in message

    def test_a_missing_input_is_named_and_nothing_else(self) -> None:
        message = self._check(_caller_yml(declared=("skip-optimize",)))
        assert "on.workflow_dispatch.inputs.release-unoptimized is not declared" in message
        assert "inputs.skip-optimize is not declared" not in message
        assert "does not pass" not in message

    def test_an_input_declared_but_not_forwarded_is_named(self) -> None:
        message = self._check(_caller_yml(forwarded=("skip-optimize",)))
        assert "jobs.ci.with.release-unoptimized does not pass inputs.release-unoptimized" in (
            message
        )
        assert "is not declared" not in message

    def test_commented_out_lines_are_absent_not_present(self) -> None:
        # A grep for the input names would find all four of these lines.
        text = _caller_yml(commented=_CONSENT)
        assert "# release-unoptimized:" in text
        message = self._check(text)
        for name in _CONSENT:
            assert f"on.workflow_dispatch.inputs.{name} is not declared" in message
            assert f"jobs.ci.with.{name} does not pass inputs.{name}" in message

    def test_a_constant_in_with_is_not_forwarding(self) -> None:
        # 'true' there would make the consent permanent rather than per-run.
        message = self._check(_caller_yml(values={"release-unoptimized": "'true'"}))
        assert "jobs.ci.with.release-unoptimized does not pass" in message

    def test_a_misspelt_input_reference_is_not_forwarding(self) -> None:
        # A near-miss name is a different input, so the consent never arrives.
        misspelt = "${{ inputs.release_unoptimized || '' }}"
        message = self._check(_caller_yml(values={"release-unoptimized": misspelt}))
        assert "jobs.ci.with.release-unoptimized does not pass" in message

    def test_a_list_of_triggers_declares_no_inputs(self) -> None:
        text = _caller_yml(declared=()).replace(
            _CALLER_HEAD, "name: CI\n\non: [push, workflow_dispatch]\n"
        )
        message = self._check(text)
        assert "does not parse" not in message
        assert "on.workflow_dispatch.inputs.skip-optimize is not declared" in message

    def test_no_reusable_workflow_job_is_named(self) -> None:
        text = _caller_yml().replace(
            "    uses: example-org/ci/.github/workflows/rust-ci.yml@main\n",
            "    runs-on: ubuntu-latest\n",
        )
        message = self._check(text)
        assert "no job calls a reusable workflow" in message

    def test_a_missing_caller_is_refused(self) -> None:
        with pytest.raises(sf.FleetError) as caught:
            _check_unoptimized_caller(self.repo)
        assert "is not there" in str(caught.value)

    def test_a_caller_that_does_not_parse_is_refused_not_a_traceback(self) -> None:
        message = self._check("on:\n  workflow_dispatch: [\n")
        assert "does not parse as YAML" in message


class RebuildRsUnoptimizedTests(OnPathTestCase):
    """--release-unoptimized lands with no trailer, then dispatches with consent."""

    SLUG = "example-org/dfe-app"
    PUSH_RUN = 33054359645
    DISPATCH_RUN = 33054900002
    DISPATCH = (
        "workflow",
        "run",
        "ci.yml",
        "-R",
        SLUG,
        "--ref",
        "main",
        "-f",
        "from-head=true",
        "-f",
        "skip-optimize=true",
        "-f",
        "release-unoptimized=true",
    )
    BRANCH = "scalo/rebuild-2-11-0"

    def setUp(self) -> None:
        super().setUp()
        root_tmp = tempfile.TemporaryDirectory(prefix="scalo-fleet-unopt-")
        self.addCleanup(root_tmp.cleanup)
        root = Path(root_tmp.name)
        self.origin = root / "origin.git"
        _git(root, "init", "--bare", "-b", "main", str(self.origin))
        seed = root / "seed"
        _git(root, "clone", str(self.origin), str(seed))
        _identify(seed)
        (seed / "Cargo.toml").write_text('[package]\nname = "app"\n', encoding="utf-8")
        (seed / "Dockerfile").write_text("FROM old\n", encoding="utf-8")
        workflow = seed / ".github" / "workflows" / "ci.yml"
        workflow.parent.mkdir(parents=True)
        # Committed WITHOUT the consent, the shape every consumer has on main.
        workflow.write_text(_caller_yml(declared=(), forwarded=()), encoding="utf-8")
        _git(seed, "add", "Cargo.toml", "Dockerfile", ".github/workflows/ci.yml")
        _git(seed, "commit", "-m", "chore: seed")
        _git(seed, "push", "origin", "main")
        self.main_sha = _git(self.origin, "rev-parse", "main")

        self.repo = root / "dfe-app"
        _git(root, "clone", str(self.origin), str(self.repo))
        _identify(self.repo)
        self.caller = self.repo / ".github" / "workflows" / "ci.yml"

        # Every cargo call succeeds, and the stdout emit writes a new Dockerfile,
        # so the rebuild has a tracked change to land.
        _fake_bin(self.bindir, "cargo", stdout="FROM scratch")
        _dispatching_gh(
            self.bindir,
            sha=self.main_sha,
            push_run=self.PUSH_RUN,
            dispatch_run=self.DISPATCH_RUN,
        )
        os.environ["SCALO_REBUILD_TARGET"] = str(root / "target")

    def _consent_in_the_tree(self) -> None:
        """The operator's edit, made in the working tree before the run."""
        self.caller.write_text(_caller_yml(), encoding="utf-8")

    def _rebuild(self, *extra: str) -> tuple[int, str]:
        """Run the CLI handler; ``self.output`` keeps what it said even if it raises."""
        args = sf.build_parser().parse_args(
            ["--org", "example-org", "rebuild-rs", str(self.repo), "2.11.0", "--no-tests", *extra]
        )
        self.output = io.StringIO()
        with contextlib.redirect_stdout(self.output):
            code = sf.cmd_rebuild_rs(args)
        return code, self.output.getvalue()

    def _landed_message(self) -> str:
        """The commit message the PR branch carried to the remote."""
        return _git(self.origin, "log", "-1", "--format=%B", self.BRANCH)

    def test_the_release_is_dispatched_with_the_consent_and_followed(self) -> None:
        self._consent_in_the_tree()
        code, output = self._rebuild("--release-unoptimized")
        assert code == 0, output
        argv = _fake_argv(self.bindir, "gh")

        # Landed with no trailer: neither the branch commit nor any gh call
        # (the squash message rides on `gh pr merge --body`) carries one.
        assert "Publish: true" not in self._landed_message()
        assert not any("Publish: true" in arg for arg in argv)
        assert _contains(argv, ["pr", "merge"]) != -1
        assert "carries no release trailer" in output

        # The caller edit made in the tree is folded into the release commit.
        touched = _git(self.origin, "show", "--name-only", "--format=", self.BRANCH).split()
        assert ".github/workflows/ci.yml" in touched

        # Exactly that dispatch, and no other input: -f appears only there.
        dispatched = _contains(argv, self.DISPATCH)
        assert dispatched != -1
        inputs = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-f"]
        assert inputs == ["from-head=true", "skip-optimize=true", "release-unoptimized=true"]

        # The merge's push run is waited for BEFORE the dispatch, so it cannot
        # register later and cancel the release in the concurrency group.
        listed = _contains(argv, ["run", "list"])
        assert listed != -1
        assert listed < dispatched

        # The dispatched run is the one followed, never the push run.
        assert _contains(argv, ["run", "view", str(self.DISPATCH_RUN)]) != -1
        assert _contains(argv, ["run", "view", str(self.PUSH_RUN)]) == -1
        assert f"SHIPPED: {self.SLUG} v1.0.0 -> v1.0.1" in output

        # The explicit flag still works, and the log says it is why.
        assert (
            "release path: consented workflow_dispatch -- --release-unoptimized given, "
            "though .hyperi-ci.yaml does not set build.skip_optimize"
        ) in output

    def _ci_config(self, skip_optimize: str) -> None:
        """The consumer's .hyperi-ci.yaml, with ``build.skip_optimize`` as given."""
        (self.repo / ".hyperi-ci.yaml").write_text(
            f"build:\n  skip_optimize: {skip_optimize}\n  type: app\n", encoding="utf-8"
        )

    def test_skip_optimize_takes_the_consented_path_unasked(self) -> None:
        # No --release-unoptimized: the consumer's own CI config is the consent.
        self._ci_config("true")
        self._consent_in_the_tree()
        code, output = self._rebuild("--no-watch")
        assert code == 0, output
        argv = _fake_argv(self.bindir, "gh")
        assert _contains(argv, self.DISPATCH) != -1
        assert "Publish: true" not in self._landed_message()
        assert (
            "release path: consented workflow_dispatch -- .hyperi-ci.yaml sets "
            "build.skip_optimize: true"
        ) in output

    def test_skip_optimize_is_refused_before_the_bump_when_the_caller_cannot_consent(
        self,
    ) -> None:
        self._ci_config("true")
        with pytest.raises(sf.FleetError) as caught:
            self._rebuild("--no-watch")
        assert "cannot carry the consent" in str(caught.value)
        assert _fake_argv(self.bindir, "cargo") == []
        assert _git(self.origin, "branch", "--list", "scalo/*") == ""

    def test_only_a_yaml_true_selects_the_consented_path(self) -> None:
        # The dispatch skips optimisation, so a string that reads as true must
        # not choose it.
        for value in ("false", '"true"', "yes-please"):
            with self.subTest(skip_optimize=value):
                self._ci_config(value)
                code, output = self._rebuild("-n")
                assert code == 0, output
                assert "release path: publish trailer on the squash merge" in output
                assert "preflight" not in output

    def test_both_the_flag_and_the_config_are_named_when_both_apply(self) -> None:
        self._ci_config("true")
        self._consent_in_the_tree()
        code, output = self._rebuild("--release-unoptimized", "-n")
        assert code == 0, output
        assert (
            "release path: consented workflow_dispatch -- --release-unoptimized given, "
            "and .hyperi-ci.yaml sets build.skip_optimize: true"
        ) in output

    def test_a_push_run_that_registers_late_is_waited_for_before_the_dispatch(self) -> None:
        # Dispatching first would let the push run, registering after it, cancel
        # the release in the workflow's concurrency group. Costs one 5s poll.
        _dispatching_gh(
            self.bindir,
            sha=self.main_sha,
            push_run=self.PUSH_RUN,
            dispatch_run=self.DISPATCH_RUN,
            push_appears=2,
        )
        self._consent_in_the_tree()
        code, output = self._rebuild("--release-unoptimized", "--no-watch")
        assert code == 0, output
        argv = _fake_argv(self.bindir, "gh")
        dispatched = _contains(argv, self.DISPATCH)
        assert dispatched != -1
        lists_before = [
            i for i in range(dispatched) if argv[i : i + 2] == ["run", "list"]
        ]
        assert len(lists_before) == 2
        assert f"run {self.PUSH_RUN} already sits on this sha" in output

    def test_no_watch_dispatches_and_returns(self) -> None:
        self._consent_in_the_tree()
        code, output = self._rebuild("--release-unoptimized", "--no-watch")
        assert code == 0, output
        argv = _fake_argv(self.bindir, "gh")
        assert _contains(argv, self.DISPATCH) != -1
        assert _contains(argv, ["run", "view"]) == -1
        assert "not watching (--no-watch)" in output

    def test_the_default_path_keeps_the_trailer_and_never_dispatches(self) -> None:
        # No consent in the caller either: the default path never reads it.
        code, output = self._rebuild("--no-watch")
        assert code == 0, output
        argv = _fake_argv(self.bindir, "gh")
        assert "Publish: true" in self._landed_message().splitlines()
        assert _contains(argv, ["workflow", "run"]) == -1
        assert _contains(argv, ["run", "list"]) == -1
        assert "preflight" not in output
        assert "release path: publish trailer on the squash merge" in output

    def test_a_caller_without_the_consent_stops_before_the_bump(self) -> None:
        with pytest.raises(sf.FleetError) as caught:
            self._rebuild("--release-unoptimized")
        message = str(caught.value)
        # macOS parks temp dirs behind a /var -> /private/var symlink.
        assert str(self.caller) in message or str(self.caller.resolve()) in message
        assert "on.workflow_dispatch.inputs.release-unoptimized is not declared" in message
        # Refused before any cargo step and before anything reached the remote.
        assert _fake_argv(self.bindir, "cargo") == []
        assert _git(self.origin, "branch", "--list", "scalo/*") == ""

    def test_the_dry_run_names_every_new_step(self) -> None:
        self._consent_in_the_tree()
        before = sf.head_sha(self.repo)
        code, output = self._rebuild("--release-unoptimized", "-n")
        assert code == 0, output
        assert (
            "preflight: .github/workflows/ci.yml declares and forwards "
            "skip-optimize and release-unoptimized"
        ) in output
        assert f"would land it on {self.SLUG} main via a PR without the release trailer" in output
        assert "gh " + " ".join(self.DISPATCH) in output
        assert "would follow the dispatched run until the GitHub release moves" in output
        # A dry run: the checkout and the remote are untouched.
        assert sf.head_sha(self.repo) == before
        assert _fake_argv(self.bindir, "cargo") == []
        assert _contains(_fake_argv(self.bindir, "gh"), ["workflow", "run"]) == -1

    def test_the_dry_run_still_refuses_a_caller_without_the_consent(self) -> None:
        with pytest.raises(sf.FleetError) as caught:
            self._rebuild("--release-unoptimized", "-n")
        assert "cannot carry the consent" in str(caught.value)
        output = self.output.getvalue()
        assert "DRY RUN" in output
        assert "would pin" not in output


class DispatchReleaseTests(OnPathTestCase):
    """The shared dispatch tail, in ship's shape: the sha has sat on main a while."""

    SLUG = "example-org/dfe-app"

    def test_the_run_already_on_the_sha_is_refused_and_the_dispatch_followed(self) -> None:
        _dispatching_gh(self.bindir, sha="abc123", push_run=100, dispatch_run=200)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            dispatch_release(
                repo=self.bindir,
                slug=self.SLUG,
                sha="abc123",
                dispatch=["gh", "workflow", "run", "ci.yml"],
                artefact=sf.Artefact(kind="ghrelease", name=self.SLUG),
                before="v1.0.0",
                timeout=60,
            )
        argv = _fake_argv(self.bindir, "gh")
        assert "run 100 already sits on this sha" in buffer.getvalue()
        assert _contains(argv, ["run", "view", "200"]) != -1
        assert _contains(argv, ["run", "view", "100"]) == -1
        assert f"SHIPPED: {self.SLUG} v1.0.0 -> v1.0.1" in buffer.getvalue()


class RunAppearHintTests(OnPathTestCase):
    """A run that never registers names the command that releases the sha by hand."""

    SLUG = "example-org/dfe-app"
    CONSENT: ClassVar[list[str]] = ["gh", *RebuildRsUnoptimizedTests.DISPATCH]

    def _dispatch(self, *, just_merged: bool) -> str:
        """Dispatch the consented release with no time for any run to register."""
        with pytest.raises(sf.FleetError) as caught, contextlib.redirect_stdout(io.StringIO()):
            dispatch_release(
                repo=self.bindir,
                slug=self.SLUG,
                sha="abc123",
                dispatch=self.CONSENT,
                artefact=sf.Artefact(kind="ghrelease", name=self.SLUG),
                before="v1.0.0",
                timeout=60,
                just_merged=just_merged,
                appear_timeout=0,
            )
        return str(caught.value)

    def test_a_trailer_release_still_names_hyperi_ci_publish(self) -> None:
        with pytest.raises(sf.FleetError) as caught, contextlib.redirect_stdout(io.StringIO()):
            sf.await_run(self.SLUG, "abc123", timeout=0)
        assert "`hyperi-ci publish` from a checkout sitting on that commit" in str(caught.value)

    def test_a_missing_push_run_names_the_consented_dispatch(self) -> None:
        # hyperi-ci publish carries no consent, so naming it here sends the
        # operator to a release hyperi-ci's Build refuses.
        message = self._dispatch(just_merged=True)
        assert f"`{' '.join(self.CONSENT)}`" in message
        assert "-f release-unoptimized=true" in message
        assert "hyperi-ci publish" not in message
        assert _contains(_fake_argv(self.bindir, "gh"), ["workflow", "run"]) == -1

    def test_a_dispatched_run_that_never_registers_names_the_dispatch(self) -> None:
        _dispatching_gh(self.bindir, sha="abc123", push_run=100, dispatch_run=200)
        message = self._dispatch(just_merged=False)
        assert "ignoring runs up to 100" in message
        assert f"`{' '.join(self.CONSENT)}`" in message
        assert "hyperi-ci publish" not in message


if __name__ == "__main__":
    unittest.main()
