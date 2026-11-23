#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_source_edgestream.py
#  Purpose:      Check the EdgeStream Hub events the source suite posts have the
#                hub's shape, carry this run's marker and different event times,
#                and reach a receiver as one gzip JSON array.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for acceptance/source/edgestream.py, against a real local HTTP server.

python3 -m pytest scripts/tests/test_source_edgestream.py
"""

import gzip
import json
import shutil
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from acceptance.clients import Reply  # noqa: E402
from acceptance.source import cases, edgestream, steps  # noqa: E402

RUN = "src-0123456789ab"


class Receiver(BaseHTTPRequestHandler):
    """Keeps the last request it was sent and answers with the status the server holds."""

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        self.server.seen = (dict(self.headers), self.rfile.read(length))
        self.send_response(self.server.status)
        self.end_headers()

    def log_message(self, *args) -> None:
        return


@pytest.fixture
def receiver():
    server = HTTPServer(("127.0.0.1", 0), Receiver)
    server.seen = None
    server.status = 202
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def test_every_recorded_event_is_a_hub_syslog_event():
    events = edgestream.recorded_events()
    assert len(events) >= 2
    for event in events:
        hub = event["@edgestream"]
        assert hub["event"]["src_type"] == edgestream.MATCH["value"]
        assert hub["hub"]["timestamp"] == event["@timestamp"]
        assert {"facility", "host", "host_from", "message"} <= set(event)
    assert len({event["message"] for event in events}) == len(events)


def test_stamped_marks_and_spaces_the_events_without_touching_the_originals():
    recorded = edgestream.recorded_events()
    before = json.dumps(recorded)
    events = edgestream.stamped(recorded, RUN, steps.CORPUS)
    assert json.dumps(recorded) == before
    assert [event[steps.MARKER] for event in events] == [
        {"run": RUN, "kind": steps.CORPUS}
    ] * len(recorded)
    seconds = edgestream.event_times(events)
    assert seconds == sorted(seconds, reverse=True)
    assert len(set(seconds)) == len(events)
    assert all(
        event["@edgestream"]["hub"]["timestamp"] == event["@timestamp"]
        for event in events
    )


def test_stamped_can_name_another_source_type():
    events = edgestream.stamped(
        edgestream.recorded_events()[:1], RUN, edgestream.OTHER, "vector"
    )
    assert events[0]["@edgestream"]["event"]["src_type"] == "vector"


@pytest.fixture
def zone(monkeypatch, request):
    """The process's local zone, set for one test and put back after it."""
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield request.param
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize(
    "zone", ["UTC", "Australia/Sydney", "America/Los_Angeles"], indirect=True
)
def test_event_times_are_utc_whatever_the_local_zone(zone):
    seconds = edgestream.event_times([{"@timestamp": "2026-11-21T01:28:18+00:00"}])
    assert seconds == [int(datetime(2026, 11, 21, 1, 28, 18, tzinfo=UTC).timestamp())]


@pytest.mark.skipif(shutil.which("curl") is None, reason="post_body posts through curl")
def test_post_sends_one_gzip_json_array(receiver):
    events = edgestream.stamped(edgestream.recorded_events(), RUN, steps.CORPUS)
    url = f"http://127.0.0.1:{receiver.server_port}/ingest"
    status = edgestream.post(url, True, events)
    headers, body = receiver.seen
    assert status == 202
    assert headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(body)) == events


@pytest.mark.skipif(shutil.which("curl") is None, reason="post_body posts through curl")
def test_post_reports_a_refusal_without_retrying(receiver):
    receiver.status = 415
    events = edgestream.stamped(edgestream.recorded_events()[:1], RUN, steps.CORPUS)
    url = f"http://127.0.0.1:{receiver.server_port}/ingest"
    assert edgestream.post(url, True, events) == 415


class Driver:
    """Driver.record without a browser."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, str]] = {}

    def record(self, slug: str, status: str, detail: str) -> None:
        self.rows[slug] = (status, detail)


class Engine:
    """The two engine routes the case calls, with a source that is there or is not."""

    def __init__(
        self, *, exists: bool, match: dict, failed: list | None = None
    ) -> None:
        self.exists, self.match, self.failed = exists, match, failed or []
        self.calls: list[tuple[str, str]] = []

    def call(self, method: str, path: str, body: object = None) -> Reply:
        self.calls.append((method, path))
        if method == "POST" and path == "/sources/from-catalogue":
            self.exists = self.exists or not self.failed
            return Reply(200, {"created": [], "failed": self.failed})
        status = 200 if self.exists else 404
        return Reply(status, {"match": self.match})


class Store:
    """A datastore answering each count from the first fragment of the SQL it matches."""

    host = "clickhouse.example"

    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts

    def scalar(self, sql: str) -> int:
        return next((n for fragment, n in self.counts.items() if fragment in sql), 0)


def a_run(engine: Engine, store: Store | None = None) -> tuple[cases.Run, Driver]:
    driver = Driver()
    args = cases.argparse.Namespace()
    run = cases.Run(
        driver=driver,
        engine=engine,
        store=store or Store({}),
        args=args,
        name=edgestream.CATALOGUE_ENTRY,
        run_id=RUN,
        engine_repo=Path("/nowhere"),
        transform_repo=Path("/nowhere"),
        receiver_url="http://receiver.example/ingest",
        verify=False,
    )
    return run, driver


def the_case() -> cases.EdgestreamHubCase:
    return cases.EdgestreamHubCase(cases.argparse.Namespace())


class TestTheCaseCreatesTheCatalogueSource:
    def test_it_creates_the_entry_and_reads_the_shippers_match_back(self):
        engine = Engine(exists=False, match=edgestream.MATCH)
        run, driver = a_run(engine)
        case = the_case()
        case.create(run)
        assert ("POST", "/sources/from-catalogue") in engine.calls
        assert driver.rows["add-source"][0] == "api-fallback"
        assert driver.rows["source-match"][0] == "done"
        assert case.owns_source

    def test_a_source_already_on_the_stack_is_adopted_and_never_recreated(self):
        engine = Engine(exists=True, match=edgestream.MATCH)
        run, driver = a_run(engine)
        case = the_case()
        case.create(run)
        assert ("POST", "/sources/from-catalogue") not in engine.calls
        assert driver.rows["add-source"][0] == "skipped"
        assert not case.owns_source

    def test_an_engine_that_matches_the_source_label_fails_the_match_row(self):
        label = {
            "field": "_source",
            "operator": "equals",
            "value": "edgestream-hub-syslog",
        }
        run, driver = a_run(Engine(exists=False, match=label))
        the_case().create(run)
        assert driver.rows["source-match"][0] == "failed"

    def test_a_refused_create_raises_with_the_engines_reason(self):
        failed = [
            {"entry": "edgestream-hub-syslog", "code": "conflict", "message": "taken"}
        ]
        run, _ = a_run(Engine(exists=False, match=edgestream.MATCH, failed=failed))
        with pytest.raises(RuntimeError, match="taken"):
            the_case().create(run)


class TestTheCaseProvesTheRows:
    EVENTS = 6

    def prove(self, counts: dict[str, int]) -> dict[str, tuple[str, str]]:
        run, driver = a_run(Engine(exists=True, match=edgestream.MATCH), Store(counts))
        case = the_case()
        case._events = edgestream.stamped(
            edgestream.recorded_events(), RUN, steps.CORPUS
        )
        case.LAND_DEADLINE = case.OTHER_DEADLINE = 0
        case.prove(run)
        return driver.rows

    @staticmethod
    def counts(*, landed=6, strayed=0, typed=6, timed=6, other_main=1, other_table=0):
        return {
            "FROM main WHERE JSONExtractString(toString(_json), '_e2e', 'run') = "
            f"'{RUN}' AND JSONExtractString(toString(_json), '_e2e', 'kind') = "
            f"'{steps.CORPUS}'": strayed,
            "toUnixTimestamp": timed,
            "isNotNull": typed,
            f"FROM `main` WHERE JSONExtractString(toString(_json), '_e2e', 'run') = "
            f"'{RUN}' AND JSONExtractString(toString(_json), '_e2e', 'kind') = "
            f"'{edgestream.OTHER}'": other_main,
            f"`edgestream-hub-syslog` WHERE JSONExtractString(toString(_json), '_e2e', "
            f"'run') = '{RUN}' AND JSONExtractString(toString(_json), '_e2e', 'kind') = "
            f"'{edgestream.OTHER}'": other_table,
            "FROM `edgestream-hub-syslog`": landed,
        }

    def test_every_row_typed_timed_and_bounded_reads_done(self):
        rows = self.prove(self.counts())
        assert {slug: status for slug, (status, _) in rows.items()} == {
            "landed": "done",
            "typed-columns": "done",
            "rfc5424-columns": "done",
            "event-time": "done",
            "other-src-type": "done",
        }

    @pytest.mark.parametrize(
        ("changes", "failing"),
        [
            ({"landed": 5}, "landed"),
            ({"strayed": 1}, "landed"),
            ({"typed": 5}, "typed-columns"),
            ({"timed": 0}, "event-time"),
            ({"other_main": 0}, "other-src-type"),
            ({"other_table": 1}, "other-src-type"),
        ],
    )
    def test_each_way_a_row_goes_wrong_fails_its_own_step(self, changes, failing):
        rows = self.prove(self.counts(**changes))
        assert rows[failing][0] == "failed"

    def test_nothing_posted_is_unproven_not_absent(self):
        run, driver = a_run(Engine(exists=True, match=edgestream.MATCH))
        the_case().prove(run)
        assert driver.rows["landed"][0] == "unproven"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
