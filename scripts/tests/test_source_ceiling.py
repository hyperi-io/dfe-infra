#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_source_ceiling.py
#  Purpose:      Prove the source suite's record-ceiling step against a real HTTP
#                door and real ClickHouse SQL
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The record-ceiling step: one record over the receiver's door, one under it.

The door here is a local HTTP server that answers 413 past its limit the way
dfe-receiver's RequestBodyLimitLayer does, and the landed rows are read back by
``clickhouse local`` with the suite's own SQL. dfe-ops reads the door off the
receiver's Deployment, which is tested on the JSON kubectl prints.

    python3 -m pytest scripts/tests/test_source_ceiling.py
"""

import json
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acceptance.source import steps
from test_source_cases import CLICKHOUSE, FakeDriver, FakePage, LocalClickHouse, dfeops

RUN = "src-ceiling0001"
DOOR = 64 * 1024


class Door(ThreadingHTTPServer):
    """An ingest endpoint with a body limit, keeping each record it accepts.

    ``drop`` accepts a record and keeps nothing, which is a record lost behind
    the door: the case the step exists to catch. ``early`` refuses an oversize
    body on its Content-Length and reads none of it, as tower-http's
    RequestBodyLimitLayer does in front of dfe-receiver.
    """

    def __init__(self, limit: int, drop: bool = False, early: bool = False) -> None:
        super().__init__(("127.0.0.1", 0), DoorHandler)
        self.limit, self.drop, self.early = limit, drop, early
        self.accepted: list[dict] = []
        self.bodies_read = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/ingest"


class DoorHandler(BaseHTTPRequestHandler):
    server: Door
    protocol_version = "HTTP/1.1"

    def handle_expect_100(self) -> bool:
        length = int(self.headers.get("Content-Length", "0"))
        if self.server.early and length > self.server.limit:
            self.send_response_only(413)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return False
        return super().handle_expect_100()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.bodies_read += 1
        if length > self.server.limit:
            self.send_response(413)
        else:
            if not self.server.drop:
                self.server.accepted.append(json.loads(body))
            self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(door: Door) -> Iterator[Door]:
    thread = threading.Thread(target=door.serve_forever, daemon=True)
    thread.start()
    try:
        yield door
    finally:
        door.shutdown()
        door.server_close()
        thread.join(timeout=5)


@pytest.fixture
def door() -> Iterator[Door]:
    yield from serve(Door(DOOR))


def landed(door: Door) -> LocalClickHouse:
    """dfe.main holding what the door accepted, the way the loader stores a JSON payload."""
    return LocalClickHouse("main", door.accepted)


class TestTheCeilingBody:
    @pytest.mark.parametrize("size", [128, 4096, DOOR - steps.CEILING_MARGIN, 1024 * 1024])
    def test_a_record_is_exactly_the_size_asked_for(self, size: int) -> None:
        assert len(steps.ceiling_body(RUN, steps.CEILING_UNDER, size)) == size

    def test_a_record_names_its_run_and_its_role(self) -> None:
        body = json.loads(steps.ceiling_body(RUN, steps.CEILING_OVER, 256))
        assert body[steps.MARKER] == {"run": RUN, "kind": steps.CEILING_OVER}

    def test_a_record_carries_no_dataset_so_no_source_routes_it(self) -> None:
        assert "data_stream" not in json.loads(steps.ceiling_body(RUN, steps.CEILING_UNDER, 256))

    def test_a_size_smaller_than_the_framing_is_refused(self) -> None:
        with pytest.raises(ValueError, match="framing"):
            steps.ceiling_body(RUN, steps.CEILING_UNDER, 8)


class TestTheCeilingVerdict:
    def test_a_refused_over_and_a_landed_under_pass(self) -> None:
        status, detail = steps.ceiling_outcome(DOOR, 413, 200, 1, 0)
        assert status == "done"
        assert "answered 413" in detail

    def test_a_redelivered_under_still_passes(self) -> None:
        assert steps.ceiling_outcome(DOOR, 413, 200, 2, 0)[0] == "done"

    @pytest.mark.parametrize(
        ("over_status", "under_status", "under_landed", "over_landed"),
        [
            pytest.param(200, 200, 1, 1, id="the-door-admits-the-oversize-record"),
            pytest.param(400, 200, 1, 0, id="refused-but-not-for-size"),
            pytest.param(413, 413, 0, 0, id="the-door-is-lower-than-declared"),
            pytest.param(413, 200, 0, 0, id="admitted-and-lost-behind-the-door"),
            pytest.param(413, 503, 0, 0, id="the-under-record-was-never-taken"),
        ],
    )
    def test_anything_else_fails(
        self, over_status: int, under_status: int, under_landed: int, over_landed: int
    ) -> None:
        verdict = steps.ceiling_outcome(DOOR, over_status, under_status, under_landed, over_landed)
        assert verdict[0] == "failed"


class TestTheStepWithoutADoor:
    def test_no_door_named_is_a_skip_that_says_so(self) -> None:
        driver = FakeDriver(FakePage())
        steps.record_ceiling(driver, None, "http://receiver.invalid/ingest", True, RUN, 0)
        assert driver.status("record-ceiling") == "skipped"
        assert "--receiver-door-bytes" in driver.detail("record-ceiling")

    def test_no_receiver_is_a_skip_that_says_so(self) -> None:
        driver = FakeDriver(FakePage())
        steps.record_ceiling(driver, None, "", True, RUN, DOOR)
        assert driver.status("record-ceiling") == "skipped"
        assert "receiver URL" in driver.detail("record-ceiling")


class TestThePost:
    def test_a_refusal_is_returned_at_once(self, door: Door) -> None:
        assert steps.post_body(door.url, True, b"x" * (DOOR + 1)) == 413

    def test_an_admitted_body_is_kept(self, door: Door) -> None:
        body = steps.ceiling_body(RUN, steps.CEILING_UNDER, 512)
        assert steps.post_body(door.url, True, body) == 200
        assert len(door.accepted) == 1

    def test_a_refusal_before_the_body_is_read_still_reads_as_413(self) -> None:
        """A multi-MiB body the door never reads: the 413 must arrive, not a reset."""
        limit = 4 * 1024 * 1024
        for early in serve(Door(limit, early=True)):
            status = steps.post_body(early.url, True, b"x" * (limit + steps.CEILING_MARGIN))
            assert early.bodies_read == 0
        assert status == 413

    def test_no_receiver_listening_is_an_error_not_a_verdict(self) -> None:
        with pytest.raises(RuntimeError, match="never answered"):
            steps.post_body("http://127.0.0.1:9/ingest", True, b"{}", window=0.0)


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary for clickhouse local")
class TestTheStepAgainstADoor:
    def _walk(self, door: Door, declared: int = DOOR) -> FakeDriver:
        driver = FakeDriver(FakePage())
        steps.record_ceiling(driver, landed(door), door.url, True, RUN, declared, deadline=0)
        return driver

    def test_a_door_at_the_declared_size_passes(self, door: Door) -> None:
        driver = self._walk(door)
        assert driver.status("record-ceiling") == "done", driver.detail("record-ceiling")
        kinds = [record[steps.MARKER]["kind"] for record in door.accepted]
        assert kinds == [steps.CEILING_UNDER]

    def test_a_door_below_the_declared_size_fails(self) -> None:
        for low in serve(Door(DOOR // 2)):
            driver = self._walk(low)
        assert driver.status("record-ceiling") == "failed"
        assert "answered 413, 0 landed in dfe.main" in driver.detail("record-ceiling")

    def test_a_door_above_the_declared_size_fails(self) -> None:
        for high in serve(Door(DOOR * 2)):
            driver = self._walk(high)
        assert driver.status("record-ceiling") == "failed"

    def test_a_record_admitted_and_lost_fails(self) -> None:
        for lossy in serve(Door(DOOR, drop=True)):
            driver = self._walk(lossy)
        assert driver.status("record-ceiling") == "failed"
        assert "answered 200, 0 landed in dfe.main" in driver.detail("record-ceiling")


class TestTheDeployedDoor:
    """dfe-ops reads the door off the receiver's Deployment."""

    @staticmethod
    def _deployment(*env: dict) -> dict:
        return {"spec": {"template": {"spec": {"containers": [{"name": "dfe-receiver", "env": list(env)}]}}}}

    def test_a_literal_env_value_is_the_door(self) -> None:
        deployment = self._deployment({"name": dfeops.RECEIVER_DOOR_VAR, "value": "5242880"})
        assert dfeops.receiver_door(deployment) == 5242880

    def test_an_unset_env_leaves_the_receivers_own_default(self) -> None:
        deployment = self._deployment({"name": "DFE_RECEIVER_LOG_LEVEL", "value": "info"})
        assert dfeops.receiver_door(deployment) == dfeops.RECEIVER_DEFAULT_DOOR

    def test_a_value_from_a_reference_is_not_read_as_a_number(self) -> None:
        reference = {"name": dfeops.RECEIVER_DOOR_VAR, "valueFrom": {"configMapKeyRef": {"name": "x", "key": "y"}}}
        assert dfeops.receiver_door(self._deployment(reference)) == dfeops.RECEIVER_DEFAULT_DOOR

    def test_a_deployment_with_no_containers_leaves_the_default(self) -> None:
        assert dfeops.receiver_door({}) == dfeops.RECEIVER_DEFAULT_DOOR

    def test_the_default_is_the_receivers_ten_mib(self) -> None:
        assert dfeops.RECEIVER_DEFAULT_DOOR == 10 * 1024 * 1024
