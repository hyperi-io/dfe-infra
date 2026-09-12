#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_source_cases.py
#  Purpose:      Prove the source suite's case objects -- the step names and
#                statuses each records, and when a console step falls back to
#                the API -- without a browser or a deployment
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for acceptance/source: the cases, and the console-or-API decision.

The runner orders the steps and the cases fill in their own halves, so what is
testable without Chrome is exactly that: which rows a case lands, under which
names, and whether a console control the deployment does not have is recorded as
an api-fallback rather than failing the run. The clients are stubs, so nothing
here opens a socket.

    python3 -m pytest scripts/tests/test_source_cases.py
    python3 scripts/tests/test_source_cases.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from acceptance.source import cases, steps  # noqa: E402
from acceptance.source.run import build_parser, walk  # noqa: E402

PROGRAM = "# a filebeat program\n" * 8
TABLE = "zone,offset\nUTC,0\n"


def parse(*argv: str):
    return build_parser().parse_args(
        ["--ui-url", "https://dfe.example", "--engine-url", "https://dfe.example",
         "--engine-repo", "/nowhere", *argv]
    )


class FakePage:
    """A console that answers every locator, so a case's create can be walked."""

    def __init__(self, missing: tuple[str, ...] = ()) -> None:
        self.missing = missing
        self.visited: list[str] = []
        self.typed: list[str] = []

    def _guard(self, what: str):
        self.visited.append(what)
        if what in self.missing:
            raise TimeoutError(f"{what} is not on this console")
        return self

    # Every locator returns self, so a chain of clicks and fills is one object.
    def get_by_role(self, role, name="", exact=False):
        return self._guard(f"{role}:{name}")

    def get_by_text(self, text, exact=False):
        return self._guard(f"text:{text}")

    def get_by_label(self, label, exact=False):
        return self._guard(f"label:{label}")

    def get_by_placeholder(self, placeholder):
        return self._guard(f"placeholder:{placeholder}")

    def locator(self, selector, has_text=""):
        return self._guard(f"locator:{selector}")

    def goto(self, url, **kwargs):
        self.visited.append(f"goto:{url}")

    def wait_for_url(self, *a, **k):
        return None

    def nth(self, index):
        return self

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    @property
    def keyboard(self):
        return self

    def click(self, **kwargs):
        return None

    def fill(self, value, **kwargs):
        self.typed.append(value)

    def insert_text(self, value):
        self.typed.append(value)

    def press(self, key):
        return None

    def wait_for(self, **kwargs):
        return None

    def count(self):
        return 1


class FakeDriver:
    """Driver.record without the screenshot."""

    def __init__(self, page) -> None:
        self.page = page
        self.ui = "https://dfe.example"
        self.results: list[tuple[str, str, str]] = []

    def record(self, slug, status, detail):
        self.results.append((slug, status, detail))

    def button(self, name):
        return self.page.get_by_role("button", name=name, exact=True)

    @property
    def rows(self):
        return [slug for slug, _, _ in self.results]

    def status(self, slug):
        return next(status for name, status, _ in self.results if name == slug)

    def detail(self, slug):
        return next(detail for name, _, detail in self.results if name == slug)


class FakeEngine:
    """The engine's replies, keyed by method and path.

    A deployment that answers every call the shared steps make, so a walk gets
    all the way to the archive without a cluster.
    """

    ON_DISK: ClassVar[dict[str, int]] = {"filebeat.vrl": len(PROGRAM), "timezones.csv": len(TABLE)}

    #: appmgmt/appconfig.py RESTART_HINT, as a released engine renders it.
    RESTART_HINT: ClassVar[str] = "restart required: docker compose restart dfe-transform-vrl"

    def __init__(self, transform: str = "vrl", committed: bool = True,
                 restart_required: tuple[str, ...] = (RESTART_HINT,),
                 deploy_restarts: tuple[str, ...] = ()) -> None:
        self.base = "https://dfe.example"
        self.verify = False
        self.token = "t"
        self.restart_required = restart_required
        self.deploy_restarts = deploy_restarts
        self.source = {"current": "1.0.0", "deployed_version": "1.0.0",
                       "transform": {"engine": transform} if transform else {}}
        self.committed = committed
        self.calls: list[tuple[str, str]] = []
        self.written: dict[str, str] = {}

    def call(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "PUT" and "/files/" in path:
            self.written[path] = str((body or {}).get("content") or "")
            return reply(200, {"changed": True, "reload": "roll",
                               "restart_required": list(self.restart_required)})
        if method == "GET" and "/files/" in path:
            if not self.committed:
                return reply(404, {"message": "no such file"})
            return reply(200, {"size_bytes": self.ON_DISK[path.rsplit("/", 1)[-1]]})
        if method == "GET" and path.endswith("/status"):
            return reply(200, {"reporting": True, "uptime_seconds": 3, "telemetry_name": "dfe-fetcher-x"})
        if path == "/apps":
            return reply(200, [{"service": service, "instances": [self.instance]}
                               for service in ("dfe-transform-vrl", "dfe-fetcher")])
        if path == "/hyperdx/sources":
            return reply(200, {"teams": []})
        if method == "POST" and path.endswith("/deploy"):
            return reply(200, {"applied": True, "topics_ensured": ["a_land"],
                               "apps_synced": ["deploy instance"],
                               "restart_required": list(self.deploy_restarts)})
        if method == "PUT" and path.startswith("/sources/"):
            self.source = {**self.source, "transform": {"engine": "vrl"}}
            return reply(200, self.source)
        if method == "GET" and path.startswith("/sources/"):
            return reply(200, self.source)
        return reply(200, {})

    #: The source this deployment carries an instance for; set by ``a_run``.
    instance = ""


def reply(status, body):
    from acceptance.clients import Reply

    return Reply(status, body)


class FakeStore:
    """A datastore that has the table and nothing in it."""

    host = "clickhouse.example"

    def table_exists(self, _name):
        return True

    def scalar(self, _sql):
        return 0

    def query(self, _sql):
        return []


def a_run(driver, engine, args, name, transform_repo: Path):
    engine.instance = name
    return cases.Run(
        driver=driver, engine=engine, store=FakeStore(), args=args, name=name, run_id="src-test",
        engine_repo=Path("/nowhere"), transform_repo=transform_repo,
        receiver_url="", verify=False,
    )


@pytest.fixture
def transform_repo(tmp_path):
    program = tmp_path / cases.FilebeatCase.PROGRAM
    program.parent.mkdir(parents=True, exist_ok=True)
    program.write_text(PROGRAM, encoding="utf-8")
    (tmp_path / cases.FilebeatCase.ENRICHMENT).write_text(TABLE, encoding="utf-8")
    return tmp_path


class TestWhichCaseARunGets:
    def test_the_default_is_the_pushed_filebeat_source(self):
        assert isinstance(cases.build(parse()), cases.FilebeatCase)

    def test_cloudwatch_is_the_fetched_one(self):
        case = cases.build(parse("--case", "cloudwatch", "--aws-service", "cloudtrail"))

        assert isinstance(case, cases.FetchedAwsCase)
        assert case.upstream.service == "cloudtrail"

    def test_each_case_names_its_own_app_and_its_own_wait_rows(self):
        pushed, fetched = cases.build(parse()), cases.build(parse("--case", "cloudwatch"))

        assert (pushed.service, pushed.instance_step, pushed.reporting_step) == (
            "dfe-transform-vrl", "transform-instance", "transform-reporting")
        assert (fetched.service, fetched.instance_step, fetched.reporting_step) == (
            "dfe-fetcher", "fetcher-instance", "fetcher-reporting")

    def test_a_run_mints_its_own_source_name_so_two_runs_never_collide(self):
        assert cases.build(parse()).name != cases.build(parse()).name

    def test_the_minted_names_are_the_ones_the_sweep_removes(self):
        for case in (cases.build(parse()), cases.build(parse("--case", "cloudwatch"))):
            assert case.name.startswith(steps.RUN_PREFIXES)

    def test_the_fetched_case_authors_a_schema_under_its_own_name(self):
        case = cases.build(parse("--case", "cloudwatch", "--aws-service", "cloudtrail"))

        # Never the shipped meta/aws/cloudtrail, which a create would be refused against.
        assert case.schema == f"meta/aws/cloudtrail_{case.name}"

    def test_only_the_pushed_case_has_files_to_write_after_the_deploy(self):
        fetched = cases.build(parse("--case", "cloudwatch"))

        assert fetched.provision(a_run(FakeDriver(FakePage()), FakeEngine(), parse(), "cw1", Path("/"))) == []


class TestTheConsoleOrApiDecision:
    def test_a_console_that_did_the_work_reads_done(self):
        assert steps.console_outcome("the console set it", "", "") == ("done", "the console set it")

    def test_a_missing_control_reads_api_fallback_and_says_why(self):
        status, detail = steps.console_outcome("", "the console could not: TimeoutError: no tab", "set by API")

        assert status == "api-fallback"
        assert detail == "set by API (the console could not: TimeoutError: no tab)"

    def test_the_reason_is_one_line_however_long_the_error_was(self):
        reason = steps.refusal(TimeoutError("locator resolved to nothing\nCall log:\n  - waiting"))

        assert "\n" not in reason
        assert reason == "the console could not: TimeoutError: locator resolved to nothing"

    def test_a_fallback_never_fails_the_run_so_an_older_console_still_passes(self):
        from acceptance.onboarding import wizard

        status, detail = steps.console_outcome("", "no control", "did it by API")

        assert wizard.exit_code([wizard.StepResult("attach-transform", status, detail)]) == 0


class TestTheFilebeatCaseCreate:
    def _create(self, missing=()):
        driver = FakeDriver(FakePage(missing=missing))
        engine = FakeEngine()
        case = cases.build(parse())
        case.create(a_run(driver, engine, parse(), case.name, Path("/")))
        return driver, engine, case

    def test_it_lands_the_four_create_rows_in_the_consoles_own_order(self):
        driver, _, _ = self._create()

        assert driver.rows == [
            "add-source-configuration", "add-source-meta-schema", "add-source", "attach-transform"
        ]

    def test_the_transform_goes_on_through_the_console_when_the_tab_is_there(self):
        driver, engine, case = self._create()

        assert driver.status("attach-transform") == "done"
        assert case.service in driver.detail("attach-transform")
        assert ("PUT", f"/sources/{case.name}") not in engine.calls

    def test_a_console_without_the_transform_tab_falls_back_and_says_so(self):
        driver, engine, case = self._create(missing=("tab:Transform",))

        assert driver.status("attach-transform") == "api-fallback"
        assert "the console could not" in driver.detail("attach-transform")
        assert ("PUT", f"/sources/{case.name}") in engine.calls

    def test_a_console_without_the_engine_picker_falls_back_the_same_way(self):
        driver, engine, case = self._create(missing=("label:Transform Engine",))

        assert driver.status("attach-transform") == "api-fallback"
        assert ("PUT", f"/sources/{case.name}") in engine.calls

    def test_the_row_is_read_back_off_the_engine_not_off_the_form(self):
        """A form that accepted the pick but sent no transform must not read done."""
        driver = FakeDriver(FakePage())
        engine = FakeEngine(transform="")
        case = cases.build(parse())

        case.create(a_run(driver, engine, parse(), case.name, Path("/")))

        assert driver.status("attach-transform") == "api-fallback"
        assert "carried transform none" in driver.detail("attach-transform")

    def test_every_create_row_is_done_however_the_transform_was_attached(self):
        for missing in ((), ("tab:Transform",)):
            driver, _, _ = self._create(missing=missing)
            for slug in ("add-source-configuration", "add-source-meta-schema", "add-source"):
                assert driver.status(slug) == "done"


class TestTheFilebeatCaseProvision:
    def _provision(self, transform_repo, missing=(), committed=True):
        driver = FakeDriver(FakePage(missing=missing))
        engine = FakeEngine(committed=committed)
        case = cases.build(parse())
        hints = case.provision(a_run(driver, engine, parse(), case.name, transform_repo))
        return driver, engine, case, hints

    def test_the_console_commits_both_files_and_the_row_reads_done(self, transform_repo):
        driver, _, _, hints = self._provision(transform_repo)

        assert driver.rows == ["upload-program"]
        assert driver.status("upload-program") == "done"
        assert "filebeat.vrl" in driver.detail("upload-program")
        assert "timezones.csv" in driver.detail("upload-program")
        assert hints == []

    def test_it_opens_the_sources_processing_tab_for_the_instance_it_wrote_to(self, transform_repo):
        driver, _, case, _ = self._provision(transform_repo)
        opened = [v for v in driver.page.visited if v.startswith("goto:")]

        assert len(opened) == 1
        assert f"source_name={case.name}" in opened[0]
        assert "tab=processing" in opened[0]

    def test_both_file_sets_the_app_declares_are_used(self, transform_repo):
        driver, _, _, _ = self._provision(transform_repo)

        assert f"tab:{cases.FilebeatCase.PROGRAM_SET}" in driver.page.visited
        assert f"tab:{cases.FilebeatCase.ENRICHMENT_SET}" in driver.page.visited

    def test_the_program_reaches_the_editor_whole(self, transform_repo):
        driver, _, _, _ = self._provision(transform_repo)

        assert PROGRAM in driver.page.typed
        assert TABLE in driver.page.typed

    def test_a_console_without_the_editor_falls_back_to_the_api(self, transform_repo):
        driver, engine, case, _ = self._provision(transform_repo, missing=("button:New file",))

        assert driver.status("upload-program") == "api-fallback"
        assert engine.written[f"/apps/{case.service}/{case.name}/files/transforms/filebeat.vrl"] == PROGRAM
        assert engine.written[f"/apps/{case.service}/{case.name}/files/enrichment/timezones.csv"] == TABLE

    def test_the_fallback_carries_the_restarts_the_writes_asked_for(self, transform_repo):
        _, _, _, hints = self._provision(transform_repo, missing=("button:New file",))

        assert hints == [FakeEngine.RESTART_HINT] * 2

    def test_a_commit_the_engine_never_took_is_a_fallback_not_a_pass(self, transform_repo, monkeypatch):
        """The console's own notice is per panel, so the engine is what is asked."""
        monkeypatch.setattr(cases, "COMMIT_DEADLINE", 0.0)
        driver, _, _, _ = self._provision(transform_repo, committed=False)

        assert driver.status("upload-program") == "api-fallback"
        assert "was not committed" in driver.detail("upload-program")


class TestTheRestartStep:
    """The engine decides, and it names the app in the hint it hands back."""

    SERVICE = "dfe-transform-vrl"

    def _apply(self, monkeypatch, prefix, reported, returncode=0):
        ran: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            ran.append(list(argv))
            return subprocess.CompletedProcess(argv, returncode, "", "no such service")

        monkeypatch.setattr(steps.subprocess, "run", fake_run)
        driver = FakeDriver(FakePage())
        steps.apply_restarts(driver, prefix, reported)
        return driver, ran

    def test_kubernetes_reports_none_because_the_chart_rolls_the_pod(self, monkeypatch):
        driver, ran = self._apply(monkeypatch, [], [])

        assert driver.status("restart") == "skipped"
        assert ran == []

    def test_the_service_is_the_last_word_of_the_engine_s_hint(self, monkeypatch):
        """appconfig.RESTART_HINT ends in the service; the rest of it is the reason."""
        driver, ran = self._apply(monkeypatch, ["docker", "restart"], [FakeEngine.RESTART_HINT])

        assert driver.status("restart") == "done"
        assert ran == [["docker", "restart", self.SERVICE]]

    def test_one_restart_per_app_however_many_writes_reported_it(self, monkeypatch):
        _, ran = self._apply(
            monkeypatch, ["docker", "restart"], [FakeEngine.RESTART_HINT] * 3
        )

        assert ran == [["docker", "restart", self.SERVICE]]

    def test_a_hint_with_no_way_to_apply_it_is_a_failed_row(self, monkeypatch):
        driver, ran = self._apply(monkeypatch, [], [FakeEngine.RESTART_HINT])

        assert driver.status("restart") == "failed"
        assert "--restart-exec" in driver.detail("restart")
        assert ran == []

    def test_a_refused_restart_is_the_finding(self, monkeypatch):
        driver, _ = self._apply(
            monkeypatch, ["docker", "restart"], [FakeEngine.RESTART_HINT], returncode=1
        )

        assert driver.status("restart") == "failed"
        assert "no such service" in driver.detail("restart")


class TestTheOrderTheRunnerWalks:
    def _walk(self, case, driver, engine, transform_repo):
        walk(a_run(driver, engine, parse(), case.name, transform_repo), case, [], [])
        return driver.rows

    def test_the_pushed_case_lands_the_rows_the_proving_runs_did(self, transform_repo):
        driver, engine = FakeDriver(FakePage()), FakeEngine()
        case = cases.build(parse())
        case.feed = lambda run: None
        case.prove = lambda run: None
        steps_seen = self._walk(case, driver, engine, transform_repo)

        assert steps_seen == [
            "add-source-configuration", "add-source-meta-schema", "add-source", "attach-transform",
            "deploy", "hyperdx-source", "upload-program", "restart", "table",
            "transform-instance", "transform-reporting", "archived",
        ]

    def test_the_files_are_written_after_the_deploy_that_makes_the_instance(self, transform_repo):
        driver, engine = FakeDriver(FakePage()), FakeEngine()
        case = cases.build(parse())
        case.feed = lambda run: None
        case.prove = lambda run: None
        order = self._walk(case, driver, engine, transform_repo)

        assert order.index("deploy") < order.index("upload-program") < order.index("restart")

    def test_a_case_with_no_files_skips_that_row_entirely(self, transform_repo):
        driver, engine = FakeDriver(FakePage()), FakeEngine()
        case = cases.build(parse("--case", "cloudwatch", "--aws-service", "cloudtrail"))
        case.create = lambda run: None
        case.feed = lambda run: None
        case.prove = lambda run: None
        order = self._walk(case, driver, engine, transform_repo)

        assert "upload-program" not in order
        assert order == ["deploy", "hyperdx-source", "restart", "table",
                         "fetcher-instance", "fetcher-reporting", "archived"]

    def test_the_restart_step_is_skipped_when_nothing_asked_for_one(self, transform_repo):
        driver, engine = FakeDriver(FakePage()), FakeEngine()
        case = cases.build(parse())
        case.feed = lambda run: None
        case.prove = lambda run: None
        self._walk(case, driver, engine, transform_repo)

        assert driver.status("restart") == "skipped"

    def test_a_deploy_that_asks_for_a_restart_is_heard_too(self, transform_repo):
        """On Compose the deploy renders the config, so its hints arrive before any write."""
        driver = FakeDriver(FakePage())
        engine = FakeEngine(deploy_restarts=(FakeEngine.RESTART_HINT,))
        case = cases.build(parse())
        case.feed = lambda run: None
        case.prove = lambda run: None
        self._walk(case, driver, engine, transform_repo)

        assert driver.status("restart") == "failed"
        assert FakeEngine.RESTART_HINT in driver.detail("restart")


class TestWhatTheRunTidiesUp:
    def test_the_fetched_case_removes_the_schema_it_authored(self):
        driver, engine = FakeDriver(FakePage()), FakeEngine()
        case = cases.build(parse("--case", "cloudwatch", "--aws-service", "cloudtrail"))

        rows = case.cleanup(a_run(driver, engine, parse(), case.name, Path("/")))

        assert [row.slug for row in rows] == ["schema-removed"]
        assert ("DELETE", f"/schemas/definitions/{case.schema}") in engine.calls

    def test_a_kept_source_keeps_the_schema_it_still_reads_from(self):
        driver, engine = FakeDriver(FakePage()), FakeEngine()
        args = parse("--case", "cloudwatch", "--keep")
        case = cases.build(args)

        assert case.cleanup(a_run(driver, engine, args, case.name, Path("/"))) == []

    def test_the_pushed_case_has_nothing_of_its_own_to_remove(self):
        case = cases.build(parse())

        assert case.cleanup(a_run(FakeDriver(FakePage()), FakeEngine(), parse(), case.name, Path("/"))) == []


# --- standalone runner (mirrors the other tests in this dir) ------------------
def main() -> int:
    return pytest.main([__file__, "-q"])


if __name__ == "__main__":
    sys.exit(main())
