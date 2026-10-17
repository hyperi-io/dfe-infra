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

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import types
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from acceptance.source import cases, fetcher, steps  # noqa: E402
from acceptance.source.run import build_parser, walk  # noqa: E402

PROGRAM = "# a filebeat program\n" * 8
TABLE = "zone,offset\nUTC,0\n"

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_source_cases", str(SCRIPTS / "dfe-ops"))
_spec = importlib.util.spec_from_loader("dfeops_source_cases", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_source_cases"] = dfeops
_loader.exec_module(dfeops)


def parse(*argv: str):
    return build_parser().parse_args(
        ["--ui-url", "https://dfe.example", "--engine-url", "https://dfe.example",
         "--engine-repo", "/nowhere", *argv]
    )


def parse_ops(*argv: str):
    """The acceptance subcommand, which is where an operator names the case."""
    return dfeops.build_parser().parse_args(["acceptance", "--repo", "/nowhere", *argv])


class FakeResponse:
    """One reply the browser saw, as Playwright hands it to a response predicate."""

    def __init__(self, method: str, url: str, body: object) -> None:
        self.request = types.SimpleNamespace(method=method)
        self.url = url
        self.ok = True
        self._body = body

    def json(self):
        return self._body


class FakePage:
    """A console that answers every locator, so a case's create can be walked."""

    #: Where the console's create goes, so a predicate keyed on it is exercised.
    CREATE_URL = "https://dfe.example/api/v1/sources"

    def __init__(self, missing: tuple[str, ...] = (), created_reply: object = None) -> None:
        self.missing = missing
        self.visited: list[str] = []
        self.typed: list[str] = []
        #: The engine's reply to the console's create, as the browser received it.
        self.created_reply = created_reply if created_reply is not None else {}

    @contextlib.contextmanager
    def expect_response(self, predicate, timeout=None):
        """The console's own create reply, refused when the predicate would not match it."""
        response = FakeResponse("POST", self.CREATE_URL, self.created_reply)
        self.visited.append("expect_response")
        yield types.SimpleNamespace(value=response)
        if not predicate(response):
            raise TimeoutError("no response matched the predicate")

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

    # The Observe page: the console embeds HyperDX, and the fake is its own
    # frame -- one that loaded, offers the source, and answers a search.
    url = "https://hyperdx.example/search?embed=1"

    def on(self, event, handler):
        return None

    def remove_listener(self, event, handler):
        return None

    def element_handle(self):
        return self

    def content_frame(self):
        return self

    def wait_for_load_state(self, *a, **k):
        return None

    def filter(self, has_text=""):
        return self._guard(f"filter:{has_text}")

    def input_value(self, **kwargs):
        return ""

    def inner_text(self, **kwargs):
        return "3 Results"


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
    RESTART_HINT: ClassVar[str] = "restart required: make apply SERVICES=dfe-transform-vrl"
    #: The same hint from an engine that predates `make apply`.
    LEGACY_RESTART_HINT: ClassVar[str] = "restart required: docker compose restart dfe-transform-vrl"

    #: The receiver's hint when a source changes where it sends records.
    RECEIVER_HINT: ClassVar[str] = "restart required: make apply SERVICES=dfe-receiver"

    def __init__(self, transform: str = "vrl", variant: str = "", committed: bool = True,
                 restart_required: tuple[str, ...] = (RESTART_HINT,),
                 deploy_restarts: tuple[str, ...] = (),
                 source_restarts: tuple[str, ...] = (),
                 archiver: bool = True,
                 deploy_topics: dict | None = None,
                 deploy_status: int = 200,
                 transports: tuple[str, ...] | None = None) -> None:
        self.base = "https://dfe.example"
        self.verify = False
        self.token = "t"
        self.restart_required = restart_required
        self.deploy_restarts = deploy_restarts
        #: The topic lists the deploy reports, and the status it answers with.
        self.deploy_topics = deploy_topics if deploy_topics is not None else {"topics_ensured": ["a_land"]}
        self.deploy_status = deploy_status
        #: What /system/deployment says the deployment carries. None names no transport.
        self.transports = transports
        #: What a create or an edit of the source reports, as the receiver's routing moves.
        self.source_restarts = source_restarts
        #: Whether this deployment runs a dfe-archiver, as /apps reports it.
        self.archiver = archiver
        attached = {}
        if transform:
            attached["engine"] = transform
        if variant:
            attached["variant"] = variant
        self.source = {"current": "1.0.0", "deployed_version": "1.0.0", "transform": attached}
        self.committed = committed
        self.calls: list[tuple[str, str]] = []
        self.written: dict[str, str] = {}
        self.sent: dict[str, dict] = {}

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
            listed = [{"service": service, "instances": [self.instance]}
                      for service in ("dfe-transform-vrl", "dfe-transform-elastic", "dfe-fetcher")]
            listed.append({"service": "dfe-archiver",
                           "instances": ["default"] if self.archiver else []})
            return reply(200, listed)
        if method == "POST" and path == "/sources":
            self.sent[path] = dict(body or {})
            return reply(201, {"restart_required": list(self.source_restarts)})
        if path == "/hyperdx/sources":
            return reply(200, {"teams": []})
        if method == "POST" and path.endswith("/deploy"):
            if self.deploy_status != 200:
                return reply(self.deploy_status, {"message": "upstream timed out"})
            return reply(200, {"applied": True, **self.deploy_topics,
                               "apps_synced": ["deploy instance"],
                               "restart_required": list(self.deploy_restarts)})
        if path == "/system/deployment" and self.transports is not None:
            return reply(200, {"transports": {"default": self.transports[0],
                                              "available": list(self.transports)}})
        if method == "PUT" and path.startswith("/sources/"):
            self.sent[path] = dict(body or {})
            self.source = {**self.source, "transform": dict((body or {}).get("transform") or {})}
            return reply(200, {**self.source, "restart_required": list(self.source_restarts)})
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
    database = "dfe"

    def table_exists(self, _name):
        return True

    def scalar(self, _sql):
        return 0

    def query(self, _sql):
        return []


def a_run(driver, engine, args, name, transform_repo: Path, receiver: str = ""):
    engine.instance = name
    return cases.Run(
        driver=driver, engine=engine, store=FakeStore(), args=args, name=name, run_id="src-test",
        engine_repo=Path("/nowhere"), transform_repo=transform_repo,
        receiver_url=receiver, verify=False,
    )


class FakeCorpus:
    """dfe-engine's corpus wrapper, without the Elastic-licensed archive behind it."""

    MODULES = ("cisco_umbrella", "cisco_ios", "cisco_meraki")

    def __init__(self) -> None:
        self.asked: list[tuple[tuple[str, ...], int]] = []

    def samples(self, _path, *, modules, limit):
        self.asked.append((tuple(modules), limit))
        return []

    def wrap_all(self, _items, source="", run=""):
        return []


def _as_engine_wrapper(monkeypatch, fake):
    """Install *fake* as ``tests.e2e.filebeat_corpus``, which steps imports off the engine repo."""
    package = types.ModuleType("tests")
    package.__path__ = []
    e2e = types.ModuleType("tests.e2e")
    e2e.__path__ = []
    e2e.filebeat_corpus = fake
    monkeypatch.setitem(sys.modules, "tests", package)
    monkeypatch.setitem(sys.modules, "tests.e2e", e2e)
    # steps prepends the engine repo, and the copy is what gets mutated.
    monkeypatch.setattr(sys, "path", list(sys.path))
    return fake


@pytest.fixture
def corpus_module(monkeypatch):
    """``tests.e2e.filebeat_corpus`` as post_corpus imports it off the engine repo."""
    return _as_engine_wrapper(monkeypatch, FakeCorpus())


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

    def test_elastic_is_the_compiled_in_transform(self):
        case = cases.build(parse("--case", "elastic"))

        assert isinstance(case, cases.ElasticCase)
        assert case.service == "dfe-transform-elastic"

    def test_vector_is_the_supervised_one(self):
        case = cases.build(parse("--case", "vector"))

        assert isinstance(case, cases.VectorCase)
        assert case.service == "dfe-transform-vector"

    def test_each_case_names_its_own_app_and_its_own_wait_rows(self):
        pushed = cases.build(parse())
        elastic = cases.build(parse("--case", "elastic"))
        fetched = cases.build(parse("--case", "cloudwatch"))

        assert (pushed.service, pushed.instance_step, pushed.reporting_step) == (
            "dfe-transform-vrl", "transform-instance", "transform-reporting")
        assert (elastic.service, elastic.instance_step, elastic.reporting_step) == (
            "dfe-transform-elastic", "transform-instance", "transform-reporting")
        assert (fetched.service, fetched.instance_step, fetched.reporting_step) == (
            "dfe-fetcher", "fetcher-instance", "fetcher-reporting")

        vector = cases.build(parse("--case", "vector"))
        assert (vector.service, vector.instance_step, vector.reporting_step) == (
            "dfe-transform-vector", "transform-instance", "transform-reporting")

    def test_a_run_mints_its_own_source_name_so_two_runs_never_collide(self):
        assert cases.build(parse()).name != cases.build(parse()).name

    def test_every_registered_case_is_one_the_runner_accepts(self):
        for name in cases.CASES:
            assert parse("--case", name).case == name

    def test_dfe_ops_offers_the_operator_the_same_cases(self):
        for name in cases.CASES:
            assert parse_ops("--source-case", name).source_case == name

    def test_dfe_ops_refuses_a_case_no_class_answers_to(self):
        with pytest.raises(SystemExit):
            parse_ops("--source-case", "nosuchcase")

    def test_the_minted_names_are_the_ones_the_sweep_removes(self):
        for name in cases.CASES:
            case = cases.build(parse("--case", name))
            assert steps.RUN_NAME.fullmatch(case.name)

    def test_a_source_the_deployment_authored_is_not_a_stray(self):
        """The sweep deletes, so a real source that merely starts like ours must survive."""
        for owned in ("filebeat", "elastic", "elasticsearch", "fbprod", "cwlogs", "onboarding"):
            assert not steps.RUN_NAME.fullmatch(owned)

    def test_the_fetched_case_authors_a_schema_under_its_own_name(self):
        case = cases.build(parse("--case", "cloudwatch", "--aws-service", "cloudtrail"))

        # Never the shipped meta/aws/cloudtrail, which a create would be refused against.
        assert case.schema == f"meta/aws/cloudtrail_{case.name}"

    def test_only_the_pushed_case_has_files_to_write_after_the_deploy(self):
        fetched = cases.build(parse("--case", "cloudwatch"))

        assert fetched.provision(a_run(FakeDriver(FakePage()), FakeEngine(), parse(), "cw1", Path("/"))) == []

    def test_the_companion_checkout_is_the_cases_own(self):
        """Both pushed cases read one corpus archive; a fetched source reads no repo."""
        assert cases.build(parse()).companion_repo == "dfe-transform-vrl"
        assert cases.build(parse("--case", "elastic")).companion_repo == "dfe-transform-vrl"
        assert cases.build(parse("--case", "cloudwatch")).companion_repo == ""


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

    def test_a_playwright_timeout_still_names_the_control_it_waited_on(self):
        error = TimeoutError(
            "Locator.click: Timeout 30000ms exceeded.\n"
            "Call log:\n"
            "  - waiting for get_by_role(\"tab\", name=\"Table Settings\", exact=True)\n"
            "  -   locator resolved to <div class=\"ant-tabs-tab\">...</div>\n"
            "  - attempting click action\n"
        )

        for line in (steps.failure_line(error), steps.refusal(error)):
            assert "\n" not in line
            assert "Timeout 30000ms exceeded" in line
            assert 'get_by_role("tab", name="Table Settings", exact=True)' in line

    def test_the_locator_line_is_bounded(self):
        error = TimeoutError("Locator.click: Timeout 1ms exceeded.\nCall log:\n  - waiting for locator(\"" + "x" * 2000 + "\")")

        assert len(steps.failure_line(error)) <= steps.FAILURE_LINE_MAX

    def test_an_error_with_no_call_log_keeps_its_headline(self):
        assert steps.failure_line(RuntimeError("boom")) == "RuntimeError: boom"
        assert steps.failure_line(RuntimeError("")) == "RuntimeError: "

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

    def test_the_schema_is_picked_on_the_table_settings_tab_by_field(self):
        """The console names the tab Table Settings and fills the header from its defaults."""
        driver, _, _ = self._create()
        visited = driver.page.visited

        assert "tab:Table Settings" in visited
        assert "button:Override Defaults" in visited
        for field in ("schema_meta_schema", "schema_meta_schema_version", "header_type", "header_version"):
            assert f"locator:{steps.field_select(field)}" in visited

    def test_the_meta_schema_is_picked_with_no_define_schema_radio_in_front(self):
        """The console has no Define Schema radio, so the tab opens on the meta schema select."""
        driver, _, _ = self._create(missing=("text:Define Schema",))

        assert driver.status("add-source-meta-schema") == "done"
        assert "text:Define Schema" not in driver.page.visited

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
        driver, engine, case = self._create(
            missing=(f"locator:{steps.field_select('transform_engine')}",)
        )

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


class TestWhatTheCreateHandsTheRestartStep:
    """The console's own create rewrites the receiver's routing, and its reply says so."""

    def _created(self, page, engine):
        driver = FakeDriver(page)
        case = cases.build(parse())
        hints = case.create(a_run(driver, engine, parse(), case.name, Path("/")))
        return driver, case, hints

    def test_the_restart_the_consoles_create_was_told_about_comes_back(self):
        page = FakePage(created_reply={"restart_required": [FakeEngine.RECEIVER_HINT]})

        _, _, hints = self._created(page, FakeEngine())

        assert hints == [FakeEngine.RECEIVER_HINT]
        assert "expect_response" in page.visited

    def test_a_create_that_asked_for_nothing_hands_back_nothing(self):
        _, _, hints = self._created(FakePage(created_reply={"restart_required": []}), FakeEngine())

        assert hints == []

    def test_the_api_fallback_hands_back_what_its_own_write_was_told(self):
        page = FakePage(missing=("tab:Transform",))
        engine = FakeEngine(source_restarts=(FakeEngine.RECEIVER_HINT,))

        driver, _, hints = self._created(page, engine)

        assert driver.status("attach-transform") == "api-fallback"
        assert hints == [FakeEngine.RECEIVER_HINT]

    def test_only_the_consoles_create_is_read_never_a_listing_of_sources(self):
        create = FakeResponse("POST", "https://dfe.example/api/v1/sources", {})
        listing = FakeResponse("GET", "https://dfe.example/api/v1/sources", {})
        one = FakeResponse("POST", "https://dfe.example/api/v1/sources/fb1/deploy", {})

        assert cases.source_created(create)
        assert not cases.source_created(listing)
        assert not cases.source_created(one)

    def test_a_create_reply_with_a_query_string_is_still_the_create(self):
        assert cases.source_created(
            FakeResponse("POST", "https://dfe.example/api/v1/sources/?draft=1", {})
        )

    def test_the_fetched_case_hands_back_its_own_creates_restarts(self):
        engine = FakeEngine(source_restarts=(FakeEngine.RECEIVER_HINT,))
        body = {"source": "cw1", "fetcher": {"config": {"services": [{"name": "cloudtrail"}]}}}

        detail, hints = fetcher.create_source(engine, body)

        assert detail.startswith("created cw1")
        assert hints == [FakeEngine.RECEIVER_HINT]


class TestTheArchiveIsAskedForWhereAnArchiverRuns:
    """A source asking to be archived where nothing archives is one the receiver refuses."""

    def _created(self, engine, missing=()):
        driver = FakeDriver(FakePage(missing=missing))
        case = cases.build(parse())
        case.create(a_run(driver, engine, parse(), case.name, Path("/")))
        return driver, case

    def test_a_deployment_running_an_archiver_gets_the_archive_switch_on(self):
        driver, _ = self._created(FakeEngine(archiver=True))

        assert "switch:" in driver.page.visited
        assert driver.detail("add-source-configuration").endswith("archive on")

    def test_a_deployment_running_none_leaves_the_switch_off_and_says_why(self):
        driver, _ = self._created(FakeEngine(archiver=False))

        assert "switch:" not in driver.page.visited
        detail = driver.detail("add-source-configuration")
        assert "archive off" in detail
        assert "dfe-archiver" in detail

    def test_the_api_fallback_asks_for_the_same_archive_the_console_did(self):
        for archiver in (True, False):
            engine = FakeEngine(archiver=archiver)
            _, case = self._created(engine, missing=("tab:Transform",))

            assert engine.sent[f"/sources/{case.name}"]["archive"] is archiver

    def test_an_archiver_listed_with_no_instance_is_not_one_that_runs(self):
        assert steps.archiver_runs(FakeEngine(archiver=True)) is True
        assert steps.archiver_runs(FakeEngine(archiver=False)) is False


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

    def test_the_console_drives_the_transform_card_it_actually_renders(self, transform_repo):
        driver = FakeDriver(TransformCardPage(TransformCardPage.RUNNING))
        case = cases.build(parse())

        case.provision(a_run(driver, FakeEngine(), parse(), case.name, transform_repo))

        assert driver.status("upload-program") == "done", driver.detail("upload-program")

    def test_expected_fail_a_transform_with_no_instance_yet_offers_no_editor(self, transform_repo):
        driver = FakeDriver(TransformCardPage(TransformCardPage.NOT_YET_RUNNING))
        case = cases.build(parse())

        case.provision(a_run(driver, FakeEngine(), parse(), case.name, transform_repo))

        assert driver.status("upload-program") == "api-fallback"


class TransformCardPage(FakePage):
    """The Processing tab as dfe-ui renders it: one Transform card, a radio per transform.

    No heading names an app and no line names the instance. The transform with an
    instance for the source is the radio whose accessible name reads Running.
    """

    RUNNING = (
        "dfe-transform-vrl Selected Running for this source.",
        "dfe-transform-vector Available Ready to take this source.",
    )
    NOT_YET_RUNNING = (
        "dfe-transform-vrl Selected Starts when this source is deployed.",
        "dfe-transform-vector Available Ready to take this source.",
    )

    def __init__(self, options: tuple[str, ...]) -> None:
        super().__init__()
        self.options = options

    def get_by_role(self, role, name="", exact=False):
        if role == "heading" and name != "Transform":
            raise TimeoutError(f"no heading {name} on the Processing tab")
        if role == "radio":
            # Playwright matches a pattern name against the accessible name by search.
            named = [o for o in self.options if isinstance(name, re.Pattern) and name.search(o)]
            if named != [self.options[0]] or "Running" not in named[0]:
                raise TimeoutError(f"no radio named {name}")
        return super().get_by_role(role, name, exact)

    def get_by_text(self, text, exact=False):
        if text.startswith("Instance "):
            raise TimeoutError(f"no text {text} on the Processing tab")
        return super().get_by_text(text, exact)


class TestTheElasticCase:
    """One compiled-in transform per instance, so the create has a half no console control sets."""

    def _create(self, engine):
        driver = FakeDriver(FakePage())
        case = cases.build(parse("--case", "elastic"))
        case.create(a_run(driver, engine, parse(), case.name, Path("/")))
        return driver, case

    def test_the_variant_is_the_catalogue_entry_for_cisco_ios(self):
        """sources.yaml spells it filebeat.<entry>.<transform>, which apps.yaml renders."""
        assert cases.ElasticCase.VARIANT == "filebeat.cisco_ios.default"

    def test_the_console_sets_the_engine_and_the_api_sets_the_variant(self):
        engine = FakeEngine(transform="elastic")
        driver, case = self._create(engine)

        assert driver.status("attach-transform") == "api-fallback"
        # How far the console got is the finding, so the row names what it set.
        assert "'engine': 'elastic'" in driver.detail("attach-transform")
        assert engine.sent[f"/sources/{case.name}"]["transform"] == {
            "engine": "elastic", "variant": cases.ElasticCase.VARIANT,
        }

    def test_a_console_that_carried_both_needs_no_fallback(self):
        """A console that grows the variant control turns this row green on its own."""
        engine = FakeEngine(transform="elastic", variant=cases.ElasticCase.VARIANT)
        driver, case = self._create(engine)

        assert driver.status("attach-transform") == "done"
        assert ("PUT", f"/sources/{case.name}") not in engine.calls

    def test_there_is_no_program_to_upload_and_the_row_says_why(self):
        driver = FakeDriver(FakePage())
        case = cases.build(parse("--case", "elastic"))

        hints = case.provision(a_run(driver, FakeEngine(), parse(), case.name, Path("/")))

        assert hints == []
        assert driver.rows == ["upload-program"]
        assert driver.status("upload-program") == "skipped"
        assert cases.ElasticCase.VARIANT in driver.detail("upload-program")

    def test_the_proof_is_a_column_the_posted_record_cannot_carry(self):
        case = cases.build(parse("--case", "elastic"))

        assert case.TRANSFORMED_COLUMN == "source_ip"
        assert case._transformed == "source_ip IS NOT NULL"

    def _feed(self, monkeypatch, case):
        asked: dict[str, tuple[str, ...]] = {}

        def routed(receiver_url, verify, engine_repo, corpus_file, store, name, run, deadline,
                   modules=()):
            asked["routed"] = modules
            return f"routed into dfe.{name} after 1 probe pass(es)"

        def posted(receiver_url, verify, engine_repo, corpus_file, run, per_module, modules=()):
            asked["posted"] = modules
            return 7

        monkeypatch.setattr(steps, "wait_routed", routed)
        monkeypatch.setattr(steps, "post_corpus", posted)
        driver = FakeDriver(FakePage())
        case.feed(a_run(driver, FakeEngine(), parse(), case.name, Path("/"), "https://rx.example"))
        return driver, asked

    def test_both_the_probe_and_the_payload_are_narrowed_to_that_module(self, monkeypatch):
        driver, asked = self._feed(monkeypatch, cases.build(parse("--case", "elastic")))

        assert asked == {"routed": ("cisco_ios",), "posted": ("cisco_ios",)}
        assert driver.status("feed") == "done"

    def test_the_filebeat_case_feeds_only_the_module_its_match_names(self, monkeypatch):
        """One equals match routes one dataset, so meraki and umbrella lines would miss it."""
        _, asked = self._feed(monkeypatch, cases.build(parse()))

        assert asked == {"routed": ("cisco_ios",), "posted": ("cisco_ios",)}


class TestTheVectorCase:
    """The same program as the filebeat case, run by a supervised Vector.

    dfe-transform-vector's bundled pipeline embeds dfe-transform-vrl's
    filebeat.vrl byte-identical, so the case is the filebeat one with the
    program written as Vector YAML -- and a divergence in either runtime shows
    up as the same column failing to arrive.
    """

    def test_it_writes_the_pipeline_its_own_app_ships(self):
        case = cases.build(parse("--case", "vector"))

        assert case.PROGRAM == "pipelines/filebeat/filebeat.yaml"
        assert case.ENRICHMENT == "pipelines/filebeat/timezones.csv"
        assert case.PROGRAM_SET == "transforms"

    def test_the_corpus_and_the_program_come_from_different_checkouts(self):
        """One --transform-repo cannot serve both: dfe-transform-vrl ships the
        corpus every pushed case grades against, and the pipeline is the app's."""
        case = cases.build(parse("--case", "vector"))

        assert case.companion_repo == "dfe-transform-vrl"
        assert case.program_repo == "dfe-transform-vector"
        assert cases.FilebeatCase.program_repo == ""
        assert cases.ElasticCase.program_repo == ""

    def test_the_proof_column_is_the_filebeat_one_because_it_is_one_program(self):
        case = cases.build(parse("--case", "vector"))

        assert case.TRANSFORMED_COLUMN == cases.FilebeatCase.TRANSFORMED_COLUMN
        assert case._transformed == "source_ip IS NOT NULL"

    def test_it_feeds_and_matches_the_one_dataset_the_filebeat_case_does(self):
        case = cases.build(parse("--case", "vector"))

        assert case.MODULES == ("cisco_ios",)
        assert (case.MATCH_FIELD, case.MATCH_VALUE) == ("data_stream.dataset", "cisco_ios.log")

    def test_the_transform_body_names_the_engine_and_nothing_else(self):
        """No variant: the pipeline is the file this case writes, not a compiled-in
        transform selected by name."""
        assert cases.build(parse("--case", "vector"))._transform_body() == {"engine": "vector"}

    def test_the_program_is_read_from_the_program_checkout(self, tmp_path):
        """The one behaviour a wrong repo would break silently: the run would
        upload dfe-transform-vrl's .vrl into an app that reads .yaml."""
        corpus_repo = tmp_path / "corpus"
        program_repo = tmp_path / "programs"
        program = program_repo / cases.VectorCase.PROGRAM
        program.parent.mkdir(parents=True)
        program.write_text("transforms: {}\n", encoding="utf-8")
        (program_repo / cases.VectorCase.ENRICHMENT).write_text(TABLE, encoding="utf-8")
        corpus_repo.mkdir()

        driver = FakeDriver(FakePage())
        case = cases.build(parse("--case", "vector"))
        engine = FakeEngine()
        run = a_run(driver, engine, parse("--case", "vector"), case.name, corpus_repo)
        run.program_repo = program_repo
        case.provision(run)

        assert driver.status("upload-program") in ("done", "api-fallback")
        assert "filebeat.yaml" in driver.detail("upload-program")

    def test_without_a_program_repo_a_case_still_reads_the_corpus_one(self):
        """Every other case keeps one checkout, so the seam must default away."""
        run = a_run(FakeDriver(FakePage()), FakeEngine(), parse(), "fbdeadbeef", Path("/corpus"))

        assert run.programs() == Path("/corpus")


class TestTheCorpusFilter:
    """post_corpus reads the archive through dfe-engine's wrapper, which caps per module."""

    def test_a_case_with_no_modules_asks_for_every_one_the_wrapper_names(self, corpus_module):
        steps.post_corpus("https://rx.example", False, Path("/nowhere"), Path("c.tar.gz"),
                          "run", 20)

        assert corpus_module.asked == [(FakeCorpus.MODULES, 20)]

    def test_a_named_module_is_the_only_one_read(self, corpus_module):
        steps.post_corpus("https://rx.example", False, Path("/nowhere"), Path("c.tar.gz"),
                          "run", 20, ("cisco_ios",))

        assert corpus_module.asked == [(("cisco_ios",), 20)]

    def test_the_routing_probe_carries_the_same_filter(self, corpus_module):
        detail = steps.wait_routed("https://rx.example", False, Path("/nowhere"),
                                   Path("c.tar.gz"), FakeStore(), "el1", "src-test", 0.0,
                                   ("cisco_ios",))

        assert corpus_module.asked == [(("cisco_ios",), 1)]
        assert detail.startswith("NOT routed")

    def test_a_routed_probe_names_how_many_reached_the_catch_all_first(self, corpus_module):
        store = RoutingStore(routed=1, strayed=2)

        detail = steps.wait_routed("https://rx.example", False, Path("/nowhere"),
                                   Path("c.tar.gz"), store, "fb1", "src-test", 0.0, ("cisco_ios",))

        assert detail == "routed into dfe.fb1 after 1 probe pass(es), 2 probe(s) landed in dfe.main"
        probes = steps.marked("src-test", steps.PROBE)
        assert any(" FROM main " in sql and probes in sql for sql in store.asked)

    def test_an_unrouted_probe_names_how_many_reached_the_catch_all(self, corpus_module):
        detail = steps.wait_routed("https://rx.example", False, Path("/nowhere"),
                                   Path("c.tar.gz"), RoutingStore(routed=0, strayed=0), "fb1",
                                   "src-test", 0.0, ("cisco_ios",))

        assert detail.startswith("NOT routed into dfe.fb1")
        assert detail.endswith(", 0 probe(s) landed in dfe.main")

    def test_a_catch_all_that_cannot_be_read_is_named_rather_than_raised(self, corpus_module):
        detail = steps.wait_routed("https://rx.example", False, Path("/nowhere"),
                                   Path("c.tar.gz"), RoutingStore(routed=1, strayed=-1), "fb1",
                                   "src-test", 0.0, ("cisco_ios",))

        assert detail.startswith("routed into dfe.fb1")
        assert detail.endswith(", dfe.main could not be read: RuntimeError")


class RoutingStore(FakeStore):
    """A datastore holding *routed* probes in the source's table and *strayed* in dfe.main.

    A negative *strayed* is a dfe.main that refuses the query.
    """

    def __init__(self, routed: int, strayed: int) -> None:
        self.routed, self.strayed = routed, strayed
        self.asked: list[str] = []

    def scalar(self, sql):
        self.asked.append(sql)
        if " FROM main " not in sql:
            return self.routed
        if self.strayed < 0:
            raise RuntimeError("datastore refused the query: 500")
        return self.strayed


@dataclass(frozen=True)
class FakeSample:
    """The fields of the engine wrapper's Sample the envelope and the expected count read."""

    module: str
    line: str
    name: str = ""


class ShippingCorpus:
    """dfe-engine's wrapper over three modules, honouring the module filter."""

    MODULES = ("cisco_umbrella", "cisco_ios", "cisco_meraki")
    LINES: ClassVar[dict[str, list[str]]] = {
        "cisco_ios": ["<189>1: Jan  6 20:52:12: %SYS-5-CONFIG_I: one", "<189>2: Jan  6 20:52:13: %SYS-5-CONFIG_I: two"],
        "cisco_meraki": ["<134>1 1599000000.1 mx events dhcp lease"],
        "cisco_umbrella": ['"2026-01-01 00:00:00","a","b","c","d","e","f"'],
    }

    def __init__(self) -> None:
        #: The source.ip each line's expected event carries, None where the event has none.
        self.expected_ip: dict[str, list[str | None]] = {}
        for module, lines in self.LINES.items():
            self.expected_ip[module] = [f"192.0.2.{n}" for n in range(1, len(lines) + 1)]

    def samples(self, _path, *, modules, limit):
        return [FakeSample(module, line, f"test-{module}.log")
                for module in modules for line in self.LINES[module][:limit]]

    def goldens(self, _path, *, modules):
        """Each module's expected events, keyed by the log's name the way the wrapper keys them."""
        return {
            f"test-{module}.log-expected.json": {
                "expected": [{"source": {"ip": ip}} if ip else {"message": "no address"}
                             for ip in self.expected_ip[module]],
            }
            for module in modules
        }

    def wrap_all(self, items, source="filebeat", run=""):
        return [
            {"message": item.line, "tags": [f"corpus_module:{item.module}", f"e2e_run:{run}"], "_source": source}
            for item in items
        ]


def _archive(path: Path, members: tuple[str, ...]) -> Path:
    """A gzipped tar holding empty files at these paths, laid out as the corpus is."""
    with tarfile.open(path, "w:gz") as tar:
        for name in members:
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"\n"))
    return path


CORPUS_PATHS = (
    "cisco_ios/log/test-asr920.log", "cisco_ios/log/test-asr920.log-expected.json",
    "cisco_ios/manifest.yml", "cisco_meraki/log/test-events.log",
    "cisco_umbrella/log/test-umbrella-dnslogs.log", "LICENSE.txt",
)


@pytest.fixture
def shipping_corpus(monkeypatch):
    return _as_engine_wrapper(monkeypatch, ShippingCorpus())


class TwoLogCorpus:
    """One module read across two logs, the way the wrapper hands them over: file by file."""

    LINES = (("test-a.log", "a0"), ("test-a.log", "a1"), ("test-b.log", "b0"))

    def __init__(self) -> None:
        self.expected: dict[str, dict] = {
            "test-a.log-expected.json": {
                "expected": [{"message": "none"}, {"source": {"ip": "192.0.2.1"}}],
            },
            "test-b.log-expected.json": {"expected": [{"source": {"ip": "192.0.2.2"}}]},
        }

    def samples(self, _path, *, modules, limit):
        return [FakeSample("cisco_ios", line, name) for name, line in self.LINES[:limit]]

    def goldens(self, _path, *, modules):
        return self.expected


class TestTheExpectedCount:
    """A fed line is graded against its own log's expected event, counted from that log's start."""

    @staticmethod
    def _count(per_module: int) -> int:
        return steps.expected_carrying(
            Path("/nowhere"), Path("c.tar.gz"), per_module, ("cisco_ios",), "source.ip",
        )

    def test_each_log_is_read_from_its_own_first_event(self, monkeypatch):
        _as_engine_wrapper(monkeypatch, TwoLogCorpus())

        assert self._count(20) == 2

    def test_only_the_lines_fed_are_counted(self, monkeypatch):
        _as_engine_wrapper(monkeypatch, TwoLogCorpus())

        assert self._count(1) == 0

    def test_a_log_with_no_expected_events_asks_nothing_of_its_lines(self, monkeypatch):
        corpus = _as_engine_wrapper(monkeypatch, TwoLogCorpus())
        del corpus.expected["test-b.log-expected.json"]

        assert self._count(20) == 1


def _walk(body: dict, dotted: str):
    """The receiver's read of a match field: split on `.` and walk the raw payload."""
    node = body
    for part in dotted.split("."):
        node = node[part]
    return node


class TestTheShippersIdentifier:
    """The pushed cases route on data_stream.dataset, the field an Elastic Agent sends."""

    def test_each_module_takes_the_dataset_its_archive_path_names(self, tmp_path):
        archive = _archive(tmp_path / "corpus.tar.gz", CORPUS_PATHS)

        assert steps.corpus_datasets(archive) == {
            "cisco_ios": "cisco_ios.log",
            "cisco_meraki": "cisco_meraki.log",
            "cisco_umbrella": "cisco_umbrella.log",
        }

    def test_expected_fail_a_module_under_two_data_streams_is_refused(self, tmp_path):
        archive = _archive(tmp_path / "corpus.tar.gz", (*CORPUS_PATHS, "cisco_ios/metrics/test-x.log"))

        with pytest.raises(ValueError, match="cisco_ios"):
            steps.corpus_datasets(archive)

    def test_every_record_is_the_agent_envelope_with_the_vendor_line_as_message(self, tmp_path, shipping_corpus):
        archive = _archive(tmp_path / "corpus.tar.gz", CORPUS_PATHS)

        bodies = steps.corpus_bodies(Path("/nowhere"), archive, "src-run", 20, ("cisco_ios",))

        assert [body["message"] for body in bodies] == ShippingCorpus.LINES["cisco_ios"]
        for body in bodies:
            assert set(body) == {"agent", "data_stream", "input", "message", "tags", "_e2e"}
            assert body["agent"] == {"type": "filebeat", "name": steps.AGENT_NAME,
                                     "version": steps.beats.ELASTIC_VERSION}
            assert body["data_stream"] == {"dataset": "cisco_ios.log", "namespace": "default", "type": "logs"}
            assert body["input"] == {"type": "tcp"}
            assert "e2e_run:src-run" in body["tags"]

    def test_every_record_carries_the_run_marker_at_the_root(self, tmp_path, shipping_corpus):
        """Every posted body, corpus or probe, carries its run and role in a root `_e2e` object."""
        archive = _archive(tmp_path / "corpus.tar.gz", CORPUS_PATHS)

        corpus = steps.corpus_bodies(Path("/nowhere"), archive, "src-run", 20, ("cisco_ios",))
        probes = steps.corpus_bodies(Path("/nowhere"), archive, "src-run", 1, ("cisco_ios",), steps.PROBE)

        assert corpus
        assert all(body["_e2e"] == {"run": "src-run", "kind": "corpus"} for body in corpus)
        assert [body["_e2e"] for body in probes] == [{"run": "src-run", "kind": "probe"}]

    def test_the_routing_probe_is_marked_as_a_probe(self, corpus_module, monkeypatch):
        sent: list[tuple[str, str]] = []

        def post(receiver_url, verify, engine_repo, corpus_file, run, per_module, modules=(), kind=""):
            sent.append((run, kind))
            return 1

        monkeypatch.setattr(steps, "post_corpus", post)
        steps.wait_routed("https://rx.example", False, Path("/nowhere"), Path("c.tar.gz"),
                          FakeStore(), "fb1", "src-run", 0.0, ("cisco_ios",))

        assert sent == [("src-run", "probe")]

    def test_the_records_the_filebeat_case_posts_all_carry_the_dataset_it_matches(self, tmp_path, shipping_corpus):
        case = cases.build(parse())
        archive = _archive(tmp_path / "corpus.tar.gz", CORPUS_PATHS)

        bodies = steps.corpus_bodies(Path("/nowhere"), archive, "src-run", 20, case.MODULES)

        assert bodies
        assert all(_walk(body, case.MATCH_FIELD) == case.MATCH_VALUE for body in bodies)

    def test_the_other_modules_would_miss_the_source_which_is_why_they_are_not_fed(self, tmp_path, shipping_corpus):
        case = cases.build(parse())
        archive = _archive(tmp_path / "corpus.tar.gz", CORPUS_PATHS)

        everything = steps.corpus_bodies(Path("/nowhere"), archive, "src-run", 20)
        missing = {_walk(body, case.MATCH_FIELD) for body in everything} - {case.MATCH_VALUE}

        assert missing == {"cisco_meraki.log", "cisco_umbrella.log"}

    def test_the_match_is_the_shippers_identifier_not_one_this_suite_invents(self):
        for name in ("filebeat", "elastic", "vector"):
            case = cases.build(parse("--case", name))
            assert (case.MATCH_FIELD, case.MATCH_VALUE) == ("data_stream.dataset", "cisco_ios.log"), name
            assert case.MODULES == ("cisco_ios",), name

    def test_the_console_is_told_the_field_and_the_dataset(self):
        driver = FakeDriver(FakePage())
        case = cases.build(parse())

        case.create(a_run(driver, FakeEngine(), parse(), case.name, Path("/")))

        # Name, display name and description come first, then the match pair.
        assert driver.page.typed[3:5] == ["data_stream.dataset", "cisco_ios.log"]

    def test_the_logstash_agent_stamps_the_same_dataset(self, tmp_path):
        pair = steps.beats.Pair(network="n", receiver_url="http://rx/ingest", workdir=tmp_path, run_id="src-run")

        steps.beats.write_inputs(pair, ["a"], cases.FilebeatCase.MATCH_VALUE,
                                 steps.MARKER, steps.run_marker("src-run", steps.CORPUS))

        config = (tmp_path / "filebeat.yml").read_text(encoding="utf-8")
        assert "target: data_stream" in config
        assert "dataset: cisco_ios.log" in config

    def test_the_logstash_feed_hands_the_agent_this_runs_corpus_marker(self, monkeypatch, transform_repo):
        told: list[tuple] = []
        monkeypatch.setattr(steps, "wait_routed", lambda *a, **k: "routed into dfe.fb1 after 1 probe pass(es)")
        monkeypatch.setattr(steps, "corpus_lines", lambda *a, **k: ["one"])
        monkeypatch.setattr(cases.beats, "write_inputs", lambda *a, **k: told.append(a))
        monkeypatch.setattr(cases.beats, "start_logstash", lambda pair: (False, "not started here"))
        args = parse("--via", "logstash", "--beats-network", "dfe_default")
        case = cases.build(args)

        case.feed(a_run(FakeDriver(FakePage()), FakeEngine(), args, case.name, transform_repo, "https://rx.example"))

        assert told[0][3:] == ("_e2e", {"run": "src-test", "kind": "corpus"})


class ScriptedStore(FakeStore):
    """A datastore answering the two counts the dataset step asks for."""

    def __init__(self, total: int, carrying: int) -> None:
        self.total, self.carrying = total, carrying
        self.asked: list[str] = []

    def scalar(self, sql):
        self.asked.append(sql)
        return self.carrying if "JSONExtractString" in sql else self.total

    def query(self, sql):
        self.asked.append(sql)
        return []


class TestTheDatasetRow:
    def test_every_row_carrying_the_dataset_is_done(self):
        assert steps.dataset_outcome(ScriptedStore(12, 12), "fb1", "cisco_ios.log") == (
            "done", "12 of 12 rows in dfe.fb1 carry data_stream.dataset cisco_ios.log")

    def test_expected_fail_a_row_without_it_fails_the_step(self):
        status, detail = steps.dataset_outcome(ScriptedStore(12, 11), "fb1", "cisco_ios.log")

        assert status == "failed"
        assert detail.startswith("11 of 12")

    def test_expected_fail_an_empty_table_proves_nothing(self):
        assert steps.dataset_outcome(ScriptedStore(0, 0), "fb1", "cisco_ios.log")[0] == "failed"

    def test_the_rows_are_read_by_the_field_the_receiver_matched(self):
        store = ScriptedStore(1, 1)

        steps.dataset_outcome(store, "fb1", "cisco_ios.log")

        wanted = "JSONExtractString(toString(_json), 'data_stream', 'dataset') = 'cisco_ios.log'"
        assert any(wanted in sql for sql in store.asked)

    def test_no_query_reads_raw_which_a_json_payload_leaves_null(self):
        store = ScriptedStore(1, 1)

        steps.dataset_outcome(store, "fb1", "cisco_ios.log")
        steps.strayed_rows(store, "src-test")
        steps.beats.envelope_evidence(store, "fb1")

        assert store.asked
        assert not [sql for sql in store.asked if "_raw" in sql]

    def test_the_pushed_case_records_it_beside_landed(self, shipping_corpus):
        driver = FakeDriver(FakePage())
        case = cases.build(parse())
        run = a_run(driver, FakeEngine(), parse(), case.name, Path("/"), "https://rx.example")
        run.store = ScriptedStore(5, 5)

        case.prove(run)

        assert driver.rows[:2] == ["landed", "dataset"]
        assert driver.status("dataset") == "done"


CLICKHOUSE = shutil.which("clickhouse")


class LocalClickHouse:
    """ClickHouse itself, through ``clickhouse local``, holding one table of landed rows.

    The loader's default capture puts a JSON payload in ``_json`` and leaves ``_raw``
    NULL, so that is the row every query here has to find its field in. The table
    is the input clickhouse local reads from stdin, so no DDL is written to make it.
    """

    host = "clickhouse-local"
    database = "default"
    STRUCTURE = "_raw Nullable(String), _json JSON(max_dynamic_paths = 2048)"

    def __init__(self, table: str, records: list[dict]) -> None:
        self.table, self.records = table, records

    def _line(self, record: dict) -> str:
        return json.dumps({"_json": record})

    def query(self, sql: str) -> list[list]:
        done = subprocess.run(
            [CLICKHOUSE, "local", "--table", self.table, "--structure", self.STRUCTURE,
             "--input-format", "JSONEachRow", "--output-format", "JSONCompact", "--query", sql],
            input="\n".join(self._line(record) for record in self.records),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=True, timeout=60,
        )
        return json.loads(done.stdout)["data"]

    def scalar(self, sql: str) -> int:
        rows = self.query(sql)
        return int(rows[0][0]) if rows else 0


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary for clickhouse local")
class TestTheQueriesAgainstClickHouse:
    """The suite's own SQL over rows stored the way the loader stores a JSON payload."""

    # One cisco_ios line as the bundled filebeat program emits it, trimmed.
    TRANSFORMED: ClassVar[dict] = {
        "@timestamp": "2026-02-08T04:00:47.272Z",
        "agent": {"name": "dfe-acceptance", "type": "filebeat", "version": "8.0.0"},
        "data_stream": {"dataset": "cisco_ios.log", "namespace": "default", "type": "logs"},
        "event": {"action": "deny", "code": "IPACCESSLOGRP"},
        "message": "list 177 denied igmp 192.168.100.197 -> 224.0.0.22, 1 packet",
        "source": {"address": "192.168.100.197", "ip": "192.168.100.197"},
        "tags": ["preserve_original_event"],
    }

    def test_every_transformed_row_carries_the_dataset_it_was_routed_on(self):
        store = LocalClickHouse("fb1", [self.TRANSFORMED, self.TRANSFORMED])

        assert steps.dataset_outcome(store, "fb1", "cisco_ios.log") == (
            "done", "2 of 2 rows in dfe.fb1 carry data_stream.dataset cisco_ios.log")

    def test_expected_fail_a_row_that_lost_the_dataset_is_counted_out(self):
        lost = {key: value for key, value in self.TRANSFORMED.items() if key != "data_stream"}
        store = LocalClickHouse("fb1", [self.TRANSFORMED, lost])

        assert steps.dataset_outcome(store, "fb1", "cisco_ios.log") == (
            "failed", "1 of 2 rows in dfe.fb1 carry data_stream.dataset cisco_ios.log")

    @staticmethod
    def posted(run: str, kind: str) -> dict:
        """A record as the suite sends it: the agent envelope and this run's marker."""
        body = steps.agent_envelope("<189>a line", [f"e2e_run:{run}"], "cisco_ios.log")
        return {**body, steps.MARKER: steps.run_marker(run, kind)}

    def test_a_record_of_this_run_in_the_catch_all_is_found(self):
        store = LocalClickHouse("main", [self.posted("src-0123", steps.CORPUS)])

        assert steps.strayed_rows(store, "src-0123") == 1

    def test_expected_fail_another_runs_record_is_not_this_runs_stray(self):
        store = LocalClickHouse("main", [self.posted("src-4567", steps.CORPUS)])

        assert steps.strayed_rows(store, "src-0123") == 0

    def test_a_probe_that_reached_the_catch_all_before_routing_is_not_a_stray(self):
        """The first probes land there by design, before the receiver rolls onto the rule."""
        store = LocalClickHouse("main", [self.posted("src-0123", steps.PROBE)])

        assert steps.strayed_rows(store, "src-0123") == 0

    def test_a_corpus_row_is_counted_by_its_marker_and_a_probe_beside_it_is_not(self):
        rows = [
            {**self.TRANSFORMED, steps.MARKER: steps.run_marker("src-0123", kind)}
            for kind in (steps.PROBE, steps.CORPUS, steps.CORPUS)
        ]
        store = LocalClickHouse("fb1", [*rows, self.TRANSFORMED])

        assert steps.wait_rows(store, "fb1", 2, 0.0, steps.marked("src-0123", steps.CORPUS)) == 2
        assert steps.wait_rows(store, "fb1", 1, 0.0, steps.marked("src-0123", steps.PROBE)) == 1

    def test_a_row_off_the_agent_is_found_by_its_file_path(self):
        shipped = {**self.TRANSFORMED, "log": {"file": {"path": steps.beats.AGENT_PATH}}}
        store = LocalClickHouse("fb1", [shipped, self.TRANSFORMED])

        carrying, keys, refused = steps.beats.envelope_evidence(store, "fb1")

        assert (carrying, refused) == (1, "")
        assert keys == sorted(shipped)


class LandedRows(LocalClickHouse):
    """A source table as the loader writes it: the record in ``_json``, one column set off it."""

    STRUCTURE = f"{LocalClickHouse.STRUCTURE}, source_ip Nullable(String)"

    def _line(self, record: dict) -> str:
        return json.dumps(record)


_COUNT = re.compile(r"SELECT count\(\) FROM (\w+)(?: WHERE (.+))?")
_EXTRACT = re.compile(r"JSONExtractString\(toString\(_json\), (.+)\) = '([^']*)'")
_NOT_NULL = re.compile(r"(\w+) IS NOT NULL")
_LIKE = re.compile(r"toString\(_json\) LIKE '%([^%']*)%'")


def _holds(row: dict, condition: str) -> bool:
    """One WHERE condition over one stored row, for the shapes the suite writes."""
    if found := _EXTRACT.fullmatch(condition):
        node: object = row["_json"]
        for key in re.findall(r"'([^']*)'", found.group(1)):
            node = node.get(key) if isinstance(node, dict) else None
        return isinstance(node, str) and node == found.group(2)
    if found := _NOT_NULL.fullmatch(condition):
        return row.get(found.group(1)) is not None
    if found := _LIKE.fullmatch(condition):
        return found.group(1) in json.dumps(row["_json"], separators=(",", ":"))
    raise AssertionError(f"no reading of this condition here: {condition}")


def _count(rows: list[dict], sql: str) -> tuple[str, int]:
    """The table a count names, and how many of *rows* its WHERE keeps."""
    found = _COUNT.fullmatch(sql)
    if not found:
        raise AssertionError(f"no reading of this query here: {sql}")
    conditions = found.group(2).split(" AND ") if found.group(2) else []
    return found.group(1), sum(all(_holds(row, c) for c in conditions) for row in rows)


class Pipeline:
    """Receiver, transform and loader in one, with its own clock.

    A posted record is stored at the next sleep, so a probe posted on one pass
    lands after the check that follows it, which is the timing a deployment
    has. The transform keeps root fields and replaces the tags, as the bundled
    filebeat program does. ``lose`` and ``bare`` name posts by arrival order:
    the first never reaches a table, the second lands without the column only
    the transform sets.
    """

    host = "clickhouse-local"
    database = "default"
    TRANSFORMED_IP = "192.0.2.10"

    def __init__(self, table: str, backend: str, lose: tuple[int, ...] = (),
                 bare: range = range(0)) -> None:
        self.table, self.backend = table, backend
        self.lose, self.bare = lose, bare
        self.received = 0
        self.in_flight: list[tuple[int, dict]] = []
        self.landed: list[dict] = []
        self.clock = 0.0
        self._answers: dict[tuple[str, int], int] = {}

    def urlopen(self, request, timeout=None, context=None):
        arrival = self.received
        self.received += 1
        if arrival not in self.lose:
            self.in_flight.append((arrival, json.loads(request.data)))
        return Accepted()

    def sleep(self, seconds: float) -> None:
        self.clock += seconds
        for arrival, body in self.in_flight:
            transformed = arrival not in self.bare
            record = {**body, "tags": ["preserve_original_event"]}
            if transformed:
                record["source"] = {"ip": self.TRANSFORMED_IP}
            self.landed.append({"_json": record, "source_ip": self.TRANSFORMED_IP if transformed else None})
        self.in_flight = []

    def monotonic(self) -> float:
        return self.clock

    def scalar(self, sql: str) -> int:
        # Nothing reaches the catch-all here, so every other table reads empty.
        table = _count([], sql)[0]
        rows = self.landed if table == self.table else []
        if self.backend == "python":
            return _count(rows, sql)[1]
        key = (sql, len(rows))
        if key not in self._answers:
            self._answers[key] = LandedRows(table, rows).scalar(sql)
        return self._answers[key]

    def query(self, sql: str) -> list[list]:
        return []

    def table_exists(self, _name: str) -> bool:
        return True


class Accepted:
    """The receiver's 200, as urlopen hands it back."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


@pytest.fixture(params=["python", pytest.param("clickhouse", marks=pytest.mark.skipif(
    CLICKHOUSE is None, reason="needs the clickhouse binary for clickhouse local"))])
def backend(request):
    """Every count read in Python everywhere, and by ClickHouse itself where the binary is."""
    return request.param


class TestTheLandedCount:
    """The probes that prove routing land in the same table, so a count of rows cannot be the proof."""

    def _feed_and_prove(self, monkeypatch, transform_repo, backend, **pipeline):
        corpus = transform_repo / cases.FilebeatCase.CORPUS
        corpus.parent.mkdir(parents=True, exist_ok=True)
        _archive(corpus, CORPUS_PATHS)
        driver = FakeDriver(FakePage())
        case = cases.build(parse())
        store = Pipeline(case.name, backend, **pipeline)
        monkeypatch.setattr(steps.urllib.request, "urlopen", store.urlopen)
        monkeypatch.setattr(steps, "time", store)
        run = a_run(driver, FakeEngine(), parse(), case.name, transform_repo, "https://rx.example")
        run.store = store
        case.feed(run)
        case.prove(run)
        return driver, store

    def test_every_corpus_row_landing_is_the_pass(self, monkeypatch, transform_repo, shipping_corpus, backend):
        driver, _ = self._feed_and_prove(monkeypatch, transform_repo, backend)

        assert driver.status("routed") == "done"
        assert driver.status("landed") == "done", driver.detail("landed")
        assert driver.status("transformed") == "done", driver.detail("transformed")

    def test_expected_fail_a_lost_corpus_row_is_not_made_up_by_a_late_probe(
        self, monkeypatch, transform_repo, shipping_corpus, backend
    ):
        """Two probes then the corpus; the second probe lands after routing reads done."""
        driver, store = self._feed_and_prove(monkeypatch, transform_repo, backend, lose=(2,))

        assert store.received == 2 + len(ShippingCorpus.LINES["cisco_ios"])
        assert driver.status("landed") == "failed", driver.detail("landed")
        assert "1 of 2" in driver.detail("landed")

    def test_expected_fail_a_transformed_probe_is_not_a_transformed_corpus_row(
        self, monkeypatch, transform_repo, shipping_corpus, backend
    ):
        driver, _ = self._feed_and_prove(monkeypatch, transform_repo, backend, bare=range(2, 100))

        assert driver.status("landed") == "done", driver.detail("landed")
        assert driver.status("transformed") == "failed", driver.detail("transformed")

    def test_expected_fail_one_transformed_corpus_row_of_two_is_not_the_pass(
        self, monkeypatch, transform_repo, shipping_corpus, backend
    ):
        """The bundled program passes a line it does not classify through, untransformed."""
        driver, _ = self._feed_and_prove(monkeypatch, transform_repo, backend, bare=range(3, 4))

        assert driver.status("landed") == "done", driver.detail("landed")
        detail = driver.detail("transformed")
        assert driver.status("transformed") == "failed", detail
        assert detail.startswith("1 of 2 corpus rows"), detail

    def test_a_line_whose_expected_event_has_no_address_is_not_held_to_one(
        self, monkeypatch, transform_repo, shipping_corpus, backend
    ):
        shipping_corpus.expected_ip["cisco_ios"][1] = None
        driver, _ = self._feed_and_prove(monkeypatch, transform_repo, backend, bare=range(3, 4))

        assert driver.status("transformed") == "done", driver.detail("transformed")
        assert "the corpus expects 1" in driver.detail("transformed")

    def test_expected_fail_a_corpus_expecting_no_address_proves_nothing(
        self, monkeypatch, transform_repo, shipping_corpus, backend
    ):
        shipping_corpus.expected_ip["cisco_ios"] = [None, None]
        driver, _ = self._feed_and_prove(monkeypatch, transform_repo, backend)

        assert driver.status("transformed") == "failed", driver.detail("transformed")


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

    def test_the_service_is_the_one_the_engine_s_hint_names(self, monkeypatch):
        """appconfig.RESTART_HINT names the service as `SERVICES=<service>`."""
        driver, ran = self._apply(monkeypatch, ["docker", "restart"], [FakeEngine.RESTART_HINT])

        assert driver.status("restart") == "done"
        assert ran == [["docker", "restart", self.SERVICE]]

    def test_a_new_instance_s_recreate_hint_names_its_own_service(self, monkeypatch):
        hint = "recreate required: make apply SERVICES=dfe-transform-vrl-fb0147f03c"
        _, ran = self._apply(monkeypatch, ["docker", "restart"], [hint])

        assert ran == [["docker", "restart", "dfe-transform-vrl-fb0147f03c"]]

    def test_an_older_engine_s_hint_ends_in_the_service(self, monkeypatch):
        _, ran = self._apply(monkeypatch, ["docker", "restart"], [FakeEngine.LEGACY_RESTART_HINT])

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

    BANNER = "resolve_profile: Using profile 'slim'...\n" * 2

    def _refused(self, monkeypatch, said: str) -> str:
        def fake_run(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 2, "", said)

        monkeypatch.setattr(steps.subprocess, "run", fake_run)
        driver = FakeDriver(FakePage())
        steps.apply_restarts(driver, ["make", "apply"], [FakeEngine.RESTART_HINT])
        assert driver.status("restart") == "failed"
        return driver.detail("restart")

    def test_a_refusal_names_the_line_that_says_why_not_the_banner_above_it(self, monkeypatch):
        """make prints the resolved profile before anything can fail, on the same stream."""
        detail = self._refused(monkeypatch, self.BANNER + (
            "Error response from daemon: No such image: dfe-transform-vrl\n"
            "make[1]: *** [Makefile:535: apply-services] Error 1\n"
            "make: *** [Makefile:525: apply] Error 2\n"
        ))

        assert detail.endswith("REFUSED: Error response from daemon: No such image: dfe-transform-vrl")
        assert "Using profile" not in detail

    def test_a_refusal_make_itself_raised_is_named(self, monkeypatch):
        detail = self._refused(monkeypatch, self.BANNER + (
            "Makefile:73: *** 'SERVICES' contains names not in the resolved stack: x.  Stop.\n"
            "make: *** [Makefile:525: apply] Error 2\n"
        ))

        assert "'SERVICES' contains names not in the resolved stack" in detail

    def test_a_refusal_that_is_only_make_s_summary_still_names_it(self, monkeypatch):
        detail = self._refused(monkeypatch, "make: *** [Makefile:525: apply] Error 2\n")

        assert detail.endswith("REFUSED: make: *** [Makefile:525: apply] Error 2")

    def test_a_refusal_with_nothing_on_stderr_reads_stdout(self, monkeypatch):
        def fake_run(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 1, "no such service: x\n", "")

        monkeypatch.setattr(steps.subprocess, "run", fake_run)
        driver = FakeDriver(FakePage())
        steps.apply_restarts(driver, ["docker", "restart"], [FakeEngine.RESTART_HINT])

        assert driver.detail("restart").endswith("REFUSED: no such service: x")

    def test_the_refusal_line_is_bounded(self, monkeypatch):
        def fake_run(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 1, "", "x" * 2000)

        monkeypatch.setattr(steps.subprocess, "run", fake_run)
        driver = FakeDriver(FakePage())
        steps.apply_restarts(driver, ["docker", "restart"], [FakeEngine.RESTART_HINT])

        assert len(driver.detail("restart")) <= steps.FAILURE_LINE_MAX + len(self.SERVICE) + 20


class TestTheDeployRow:
    """The engine never fails a deploy on a topic it could not create, so the row does.

    Left to the run step, a refused topic surfaces minutes later as the receiver
    answering 503 for every record, which names neither the topic nor the deploy.
    """

    NAME = "fb0123abcd"

    def _deploy(self, engine: FakeEngine) -> FakeDriver:
        driver = FakeDriver(FakePage())
        steps.record_deploy(driver, engine, self.NAME)
        return driver

    def test_a_topic_the_engine_could_not_create_fails_the_step_by_name(self):
        failed = [f"{self.NAME}_land", f"{self.NAME}_load"]
        engine = FakeEngine(deploy_topics={"topics_ensured": [], "topics_failed": failed},
                            transports=("bus",))

        driver = self._deploy(engine)

        assert driver.status("deploy") == "failed"
        assert all(topic in driver.detail("deploy") for topic in failed)
        assert "engine log" in driver.detail("deploy")

    def test_a_refused_topic_fails_the_step_whatever_the_engine_says_it_carries(self):
        engine = FakeEngine(deploy_topics={"topics_ensured": [f"{self.NAME}_load"],
                                           "topics_failed": [f"{self.NAME}_land"]})

        assert self._deploy(engine).status("deploy") == "failed"

    def test_a_bus_deployment_that_made_no_topic_fails_the_step(self):
        engine = FakeEngine(deploy_topics={"topics_ensured": [], "topics_failed": []},
                            transports=("bus",))

        driver = self._deploy(engine)

        assert driver.status("deploy") == "failed"
        assert "bus" in driver.detail("deploy")

    def test_an_engine_that_names_no_transport_is_not_held_to_a_topic(self):
        engine = FakeEngine(deploy_topics={"topics_ensured": [], "topics_failed": []})

        assert self._deploy(engine).status("deploy") == "done"

    def test_a_brokerless_deployment_makes_no_topic_and_passes(self):
        engine = FakeEngine(deploy_topics={"topics_ensured": [], "topics_failed": []},
                            transports=("direct",))

        assert self._deploy(engine).status("deploy") == "done"

    def test_the_topics_it_made_are_named_on_the_pass(self):
        made = [f"{self.NAME}_land", f"{self.NAME}_load"]
        engine = FakeEngine(deploy_topics={"topics_ensured": made, "topics_failed": []},
                            transports=("bus",))

        driver = self._deploy(engine)

        assert driver.status("deploy") == "done"
        assert all(topic in driver.detail("deploy") for topic in made)

    def test_a_deploy_read_back_after_a_gateway_timeout_is_not_judged_on_topics(self):
        """The read-back sees the deployed version and nothing the deploy reported."""
        engine = FakeEngine(deploy_status=504, transports=("bus",))

        driver = self._deploy(engine)

        assert driver.status("deploy") == "done"
        assert "not reported" in driver.detail("deploy")


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
            "transform-instance", "transform-reporting", "archived", "observe",
        ]
        assert driver.status("observe") == "done"

    def test_the_elastic_case_walks_the_same_rows_with_nothing_to_upload(self, transform_repo):
        driver, engine = FakeDriver(FakePage()), FakeEngine(transform="elastic")
        case = cases.build(parse("--case", "elastic"))
        case.feed = lambda run: None
        case.prove = lambda run: None
        steps_seen = self._walk(case, driver, engine, transform_repo)

        assert steps_seen == [
            "add-source-configuration", "add-source-meta-schema", "add-source", "attach-transform",
            "deploy", "hyperdx-source", "upload-program", "restart", "table",
            "transform-instance", "transform-reporting", "archived", "observe",
        ]
        assert driver.status("upload-program") == "skipped"
        assert driver.status("transform-instance") == "done"

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
        case.create = lambda run: []
        case.feed = lambda run: None
        case.prove = lambda run: None
        order = self._walk(case, driver, engine, transform_repo)

        assert "upload-program" not in order
        assert order == ["deploy", "hyperdx-source", "restart", "table",
                         "fetcher-instance", "fetcher-reporting", "archived", "observe"]

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

    def test_a_restart_the_consoles_create_asked_for_is_heard_too(self, transform_repo):
        """On a direct tier the create rewrites the receiver's destinations, and only it says so."""
        driver = FakeDriver(FakePage(created_reply={"restart_required": [FakeEngine.RECEIVER_HINT]}))
        engine = FakeEngine(restart_required=())
        case = cases.build(parse())
        case.feed = lambda run: None
        case.prove = lambda run: None
        self._walk(case, driver, engine, transform_repo)

        assert driver.status("restart") == "failed"
        assert FakeEngine.RECEIVER_HINT in driver.detail("restart")


class TestTheObserveStep:
    """The console embeds HyperDX from a second origin; the row says which half refused."""

    FRAME = "https://hyperdx.example/search?embed=1"

    def test_rows_in_the_embedded_search_are_the_pass(self):
        assert steps.observe_outcome("fb1", self.FRAME, "", True, "12 Results") == (
            "done", "Observe search over fb1: 12 Results")

    def test_a_frame_the_browser_refused_names_the_console_error(self):
        blocked = "Framing 'http://box:8091/' violates the following Content Security Policy directive"
        status, detail = steps.observe_outcome("fb1", "chrome-error://chromewebdata/", blocked, False, "")

        assert status == "failed"
        assert "did not load" in detail
        assert blocked in detail

    def test_no_frame_at_all_is_a_failed_row_too(self):
        assert steps.observe_outcome("fb1", "", "", False, "")[0] == "failed"

    def test_a_picker_without_the_source_is_a_failed_row(self):
        status, detail = steps.observe_outcome("fb1", self.FRAME, "", False, "")

        assert status == "failed"
        assert "does not offer fb1" in detail

    def test_a_pick_that_raised_names_the_exception(self):
        refused = steps.refusal(TimeoutError("Locator.click: Timeout 30000ms exceeded"))
        status, detail = steps.observe_outcome("fb1", self.FRAME, "", False, refused)

        assert status == "failed"
        assert "does not offer fb1" in detail
        assert "TimeoutError" in detail

    def test_a_count_with_a_thousands_separator_is_a_pass(self):
        assert steps.results_count("1,050 Results") == 1050
        assert steps.observe_outcome("cw1", self.FRAME, "", True, "1,050 Results") == (
            "done", "Observe search over cw1: 1,050 Results")

    def test_zero_rows_is_a_failed_row_that_quotes_the_line(self):
        status, detail = steps.observe_outcome("fb1", self.FRAME, "", True, "0 Results")

        assert status == "failed"
        assert "0 Results" in detail

    def test_a_search_that_never_answered_says_so(self):
        assert "no results line" in steps.observe_outcome("fb1", self.FRAME, "", True, "")[1]

    def test_a_frame_already_open_on_the_source_skips_the_dropdown(self):
        """The frame picks a source of its own; when it is this one there is no option to pick."""
        frame = FakePage(missing=("option:",))
        frame.input_value = lambda **kwargs: "fb1"

        assert steps.search_results(frame, "fb1") == (True, "3 Results")
        assert "option:" not in frame.visited

    def test_a_frame_still_loading_its_sources_is_given_time(self, monkeypatch):
        """The picker is empty until the frame's source list arrives, so the step waits for a value."""
        monkeypatch.setattr(steps.time, "sleep", lambda seconds: None)
        frame = FakePage(missing=("option:",))
        values = iter(["", "", "fb1"])
        frame.input_value = lambda **kwargs: next(values, "fb1")

        assert steps.search_results(frame, "fb1") == (True, "3 Results")
        assert "option:" not in frame.visited


class TestTheReportingVerdict:
    """`reporting` says a container is up; it does not say this instance is working.

    On Compose one container serves the app and every instance of it, so an idle
    app answers the status call the same way a working instance does (#327).
    """

    SERVICE = "dfe-transform-elastic"
    UP = "dfe-transform-elastic/el1 reporting after 0s up"

    class IdleStore(FakeStore):
        """A datastore whose otel gauge answers per metric.

        `info` is the series every scalo app publishes while it runs;
        `pipeline_idle` is the one it publishes only while it holds no work.
        """

        database = "dfe"

        def __init__(self, info: int = 6, samples: int = 0, since: int = 4,
                     raises: bool = False, table: bool = True) -> None:
            self.info, self.samples, self.since = info, samples, since
            self.raises, self.table = raises, table

        def table_exists(self, _name):
            return self.table

        def query(self, sql):
            if self.raises:
                raise OSError("connection refused")
            if "pipeline_idle" in sql:
                return [[self.samples, self.since]]
            return [[self.info, 2]]

    def verdict(self, store, telemetry):
        """The verdict for a status body the engine answered `reporting` to."""
        status = {"reporting": True, "telemetry_name": telemetry, "uptime_seconds": 0}
        return steps.reporting_verdict(store, self.SERVICE, self.UP, status)

    def test_an_instance_doing_work_reads_done(self):
        state, detail = self.verdict(self.IdleStore(samples=0), f"{self.SERVICE}-el1")

        assert state == "done"
        assert "no pipeline_idle sample" in detail

    def test_an_idle_app_is_unproven_not_done(self):
        """The case in the issue: two green rows in front of three real failures."""
        state, detail = self.verdict(self.IdleStore(samples=6), f"{self.SERVICE}-el1")

        assert state == "unproven"
        assert "6 pipeline_idle sample(s)" in detail
        assert "an app holding no work" in detail

    def test_telemetry_the_whole_app_shares_is_read_and_named(self):
        """One container per app on Compose, so no per-instance series exists to
        prefer -- reading nothing there left the row permanently unproven (#340)."""
        state, detail = self.verdict(self.IdleStore(), self.SERVICE)

        assert state == "done"
        assert f"read from {self.SERVICE}" in detail
        assert "one container serves every instance here" in detail

    def test_a_shared_series_holding_no_work_is_still_unproven(self):
        """The shared series is read, not believed: an idle container fails the
        same way a per-instance one does."""
        state, detail = self.verdict(self.IdleStore(samples=6), self.SERVICE)

        assert state == "unproven"
        assert "an app holding no work" in detail

    def test_a_reporting_claim_with_no_series_behind_it_is_unproven(self):
        """The engine reads max(TimeUnix), which ClickHouse answers with the epoch
        rather than NULL when nothing matched, so it says reporting for an instance
        that has never emitted a byte."""
        state, detail = self.verdict(self.IdleStore(info=0), f"{self.SERVICE}-el1")

        assert state == "unproven"
        assert "not backed by telemetry" in detail

    def test_otel_tables_out_of_reach_are_unproven_not_empty(self):
        """A query for a table that is not there reads as an empty result, and an
        empty result is not evidence of anything."""
        state, detail = self.verdict(self.IdleStore(table=False), f"{self.SERVICE}-el1")

        assert state == "unproven"
        assert "otel_metrics_gauge is not in this run's reach" in detail

    def test_a_run_that_cannot_read_the_datastore_is_unproven(self):
        class NoStore(FakeStore):
            host = ""

        state, detail = self.verdict(NoStore(), f"{self.SERVICE}-el1")

        assert state == "unproven"
        assert "no datastore access" in detail

    def test_an_otel_table_that_does_not_answer_is_unproven(self):
        state, detail = self.verdict(self.IdleStore(raises=True), f"{self.SERVICE}-el1")

        assert state == "unproven"
        assert "did not answer" in detail

    def test_an_instance_that_never_reported_still_fails(self):
        """The old row read its own message back: "not reporting after 900s" contains
        "reporting after", so a timed-out wait recorded done."""
        state, detail = steps.reporting_verdict(
            FakeStore(), self.SERVICE, f"{self.SERVICE}/el1 not reporting after 900s; last 200", {}
        )

        assert state == "failed"
        assert "not reporting" in detail

    def test_an_instance_the_engine_never_listed_fails_its_own_row(self):
        driver, engine = FakeDriver(FakePage()), FakeEngine()
        engine.instance = "someone-else"

        steps.record_instance_up(
            driver, engine, self.IdleStore(), self.SERVICE, "el1",
            "transform-instance", 0.0, "transform-reporting", 0.0,
        )

        assert driver.status("transform-instance") == "failed"

    def test_the_row_the_run_lands_goes_through_the_verdict(self):
        """The wait and the verdict are one step, so no caller can record the raw wait."""
        driver, engine = FakeDriver(FakePage()), FakeEngine(transform="elastic")
        engine.instance = "el1"

        steps.record_instance_up(
            driver, engine, self.IdleStore(samples=3), self.SERVICE, "el1",
            "transform-instance", 0.0, "transform-reporting", 0.0,
        )

        assert driver.status("transform-reporting") == "unproven"

    def test_an_unproven_row_is_not_a_failure_either(self):
        from acceptance.onboarding import wizard

        assert wizard.exit_code([wizard.StepResult("transform-reporting", "unproven", "")]) == 0


class TestTheLogstashVariation:
    """The same corpus lines, reaching the receiver the way a deployment sends them."""

    def test_a_run_pushes_the_wrapped_body_by_default(self, monkeypatch, transform_repo):
        posted: list[int] = []
        monkeypatch.setattr(steps, "wait_routed", lambda *a, **k: "routed into dfe.fb1 after 1 probe pass(es)")
        monkeypatch.setattr(steps, "post_corpus", lambda *a, **k: posted.append(1) or 7)
        driver = FakeDriver(FakePage())
        case = cases.build(parse())

        case.feed(a_run(driver, FakeEngine(), parse(), case.name, transform_repo, "https://rx.example"))

        assert posted == [1]
        assert "posted 7 corpus records" in driver.detail("feed")

    def test_the_switch_needs_the_network_the_stack_runs_on(self, monkeypatch, transform_repo):
        monkeypatch.setattr(steps, "wait_routed", lambda *a, **k: "routed into dfe.fb1 after 1 probe pass(es)")
        args = parse("--via", "logstash")
        driver = FakeDriver(FakePage())
        case = cases.build(args)

        case.feed(a_run(driver, FakeEngine(), args, case.name, transform_repo, "https://rx.example"))

        assert driver.status("feed") == "skipped"
        assert "--beats-network" in driver.detail("feed")

    def test_it_stands_the_pair_up_and_reports_what_it_shipped(self, monkeypatch, transform_repo):
        started: list[str] = []
        monkeypatch.setattr(steps, "wait_routed", lambda *a, **k: "routed into dfe.fb1 after 1 probe pass(es)")
        monkeypatch.setattr(steps, "corpus_lines", lambda *a, **k: ["one", "two"])
        monkeypatch.setattr(cases.beats, "write_inputs", lambda *a, **k: None)
        monkeypatch.setattr(cases.beats, "start_logstash",
                            lambda pair: (started.append("logstash"), (True, "logstash up"))[1])
        monkeypatch.setattr(cases.beats, "start_filebeat",
                            lambda pair: (started.append("filebeat"), (True, "filebeat up"))[1])
        monkeypatch.setattr(cases.beats, "published", lambda pair, wanted: (wanted, f"filebeat published {wanted}"))
        args = parse("--via", "logstash", "--beats-network", "dfe_default")
        driver = FakeDriver(FakePage())
        case = cases.build(args)

        case.feed(a_run(driver, FakeEngine(), args, case.name, transform_repo, "https://rx.example"))

        assert started == ["logstash", "filebeat"]
        assert driver.status("feed") == "done"
        assert "lumberjack to logstash" in driver.detail("feed")

    def test_a_logstash_that_never_listened_stops_the_feed(self, monkeypatch, transform_repo):
        monkeypatch.setattr(steps, "wait_routed", lambda *a, **k: "routed into dfe.fb1 after 1 probe pass(es)")
        monkeypatch.setattr(steps, "corpus_lines", lambda *a, **k: ["one"])
        monkeypatch.setattr(cases.beats, "write_inputs", lambda *a, **k: None)
        monkeypatch.setattr(cases.beats, "start_logstash", lambda pair: (False, "never ran its pipeline"))
        args = parse("--via", "logstash", "--beats-network", "dfe_default")
        driver = FakeDriver(FakePage())
        case = cases.build(args)

        case.feed(a_run(driver, FakeEngine(), args, case.name, transform_repo, "https://rx.example"))

        assert driver.status("logstash") == "failed"
        assert driver.status("feed") == "failed"

    def test_the_pair_comes_down_with_the_run(self, monkeypatch, transform_repo):
        """Every container the suite starts is removed in the same run."""
        stopped: list[object] = []
        monkeypatch.setattr(cases.beats, "stop", lambda pair: stopped.append(pair) or "both removed")
        args = parse("--via", "logstash", "--beats-network", "dfe_default")
        case = cases.build(args)
        case._pair = cases.beats.Pair(network="n", receiver_url="u", workdir=transform_repo, run_id="r")

        rows = case.cleanup(a_run(FakeDriver(FakePage()), FakeEngine(), args, case.name, transform_repo))

        assert len(stopped) == 1
        assert [row.slug for row in rows] == ["beats-removed"]

    def test_a_run_that_started_no_pair_has_nothing_to_remove(self, transform_repo):
        args = parse()
        case = cases.build(args)

        assert case.cleanup(a_run(FakeDriver(FakePage()), FakeEngine(), args, case.name, transform_repo)) == []


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
