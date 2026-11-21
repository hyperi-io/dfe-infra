#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_source_flow.py
#  Purpose:      Check the flow case reads exporter datagrams out of a capture, sends
#                them over UDP, creates its sources, and judges the rows each gained.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for acceptance/source/flow.py and the flow case, against a real UDP socket.

python3 -m pytest scripts/tests/test_source_flow.py
"""

import argparse
import os
import socket
import struct
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from acceptance.clients import Reply  # noqa: E402
from acceptance.source import cases, flow  # noqa: E402

#: The receiver's checkout, beside this repo's unless DFE_RECEIVER_REPO names another.
RECEIVER_REPO = Path(
    os.environ.get("DFE_RECEIVER_REPO") or SCRIPTS.parent.parent / "dfe-receiver"
)
PAYLOADS = [b"\x00\x09" + b"\x00" * 20, b"\x00\x05" + b"\x01" * 30]


def capture(payloads, *, order="<", magic=b"\xd4\xc3\xb2\xa1", link=1, protocol=17):
    """A classic pcap holding *payloads* as Ethernet, IPv4 and UDP packets."""
    out = magic + struct.pack(f"{order}HHiIII", 2, 4, 0, 0, 65535, link)
    for payload in payloads:
        udp = struct.pack(">HHHH", 40000, 2055, 8 + len(payload), 0) + payload
        ip = bytes([0x45, 0, 0, 0, 0, 0, 0, 0, 64, protocol]) + b"\x00" * 10 + udp
        packet = b"\x00" * 12 + b"\x08\x00" + ip
        out += struct.pack(f"{order}IIII", 0, 0, len(packet), len(packet)) + packet
    return out


@pytest.mark.parametrize(
    ("order", "magic"),
    [
        ("<", b"\xd4\xc3\xb2\xa1"),
        (">", b"\xa1\xb2\xc3\xd4"),
        ("<", b"\x4d\x3c\xb2\xa1"),
    ],
)
def test_a_pcap_gives_its_udp_payloads_in_order(tmp_path, order, magic):
    path = tmp_path / "flows.pcap"
    path.write_bytes(capture(PAYLOADS, order=order, magic=magic))
    assert flow.datagrams(path) == PAYLOADS


def test_a_bin_is_one_datagram(tmp_path):
    path = tmp_path / "one.bin"
    path.write_bytes(PAYLOADS[0])
    assert flow.datagrams(path) == [PAYLOADS[0]]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (capture(PAYLOADS, link=101), "not a classic Ethernet pcap"),
        (b"\x0a\x0d\x0d\x0a" + b"\x00" * 40, "not a classic Ethernet pcap"),
        (capture(PAYLOADS, protocol=6), "not IPv4 and UDP"),
    ],
)
def test_a_capture_that_is_not_ethernet_ipv4_udp_is_refused(tmp_path, data, message):
    path = tmp_path / "bad.pcap"
    path.write_bytes(data)
    with pytest.raises(ValueError, match=message):
        flow.datagrams(path)


@pytest.mark.skipif(
    not RECEIVER_REPO.is_dir(), reason="no dfe-receiver checkout beside this one"
)
@pytest.mark.parametrize(
    ("name", "count"),
    [
        ("netgauze-pmacct-ipfix-cisco-100.pcap", 6),
        ("netgauze-pmacct-nfv9-cisco-101.pcap", 40),
    ],
)
def test_the_committed_captures_hold_the_datagrams_their_readme_counts(name, count):
    assert len(flow.datagrams(RECEIVER_REPO / flow.FIXTURE_DIR / name)) == count


@pytest.mark.skipif(
    not RECEIVER_REPO.is_dir(), reason="no dfe-receiver checkout beside this one"
)
def test_the_table_lists_every_committed_fixture_and_only_those():
    committed = {
        path.name
        for path in (RECEIVER_REPO / flow.FIXTURE_DIR).iterdir()
        if path.suffix in (".bin", ".pcap")
    }
    assert {fixture.name for fixture in flow.FIXTURES} == committed


def test_send_delivers_each_datagram_in_order():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sink:
        sink.bind(("127.0.0.1", 0))
        sink.settimeout(5)
        flow.send("127.0.0.1", sink.getsockname()[1], PAYLOADS)
        assert [sink.recv(2048) for _ in PAYLOADS] == PAYLOADS


def test_every_fixture_belongs_to_a_source_the_case_creates():
    assert {fixture.source for fixture in flow.FIXTURES} == set(flow.SOURCES)
    assert all(0 <= f.addressed <= f.events for f in flow.FIXTURES)


class Driver:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, str]] = {}

    def record(self, slug: str, status: str, detail: str) -> None:
        self.rows[slug] = (status, detail)


class Engine:
    """The engine routes the case and the listener call, over sources that are or are not there."""

    def __init__(self, present=(), *, opens=True) -> None:
        self.present, self.opens, self.calls = set(present), opens, []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/apps":
            instances = ["dfe-receiver"] if self.opens else []
            return Reply(
                200, {"items": [{"service": "dfe-receiver", "instances": instances}]}
            )
        if path.startswith("/apps/dfe-receiver/"):
            return Reply(200, {})
        if method == "POST" and path == "/sources/from-catalogue":
            self.present |= {item["entry"] for item in body["entries"]}
            return Reply(200, {"created": [], "failed": []})
        if method == "POST" and path.endswith("/deploy"):
            return Reply(200, {"applied": True})
        name = path.rsplit("/", 1)[-1]
        return Reply(200 if name in self.present else 404, {})


class Store:
    """A datastore whose counts come from a table the test sets, by the source's table name."""

    host = "clickhouse.example"

    def __init__(self, counts) -> None:
        self.counts = counts

    def scalar(self, sql: str) -> int:
        for fragment, count in self.counts.items():
            if fragment in sql:
                return count
        return 0


def a_run(engine, store=None):
    driver = Driver()
    args = argparse.Namespace(
        flow_host="", flow_netflow_port=2055, flow_sflow_port=6343, keep=False
    )
    run = cases.Run(
        driver=driver,
        engine=engine,
        store=store or Store({}),
        args=args,
        name="netflow",
        run_id="src-0123456789ab",
        engine_repo=Path("/nowhere"),
        transform_repo=Path("/nowhere"),
        receiver_url="http://receiver.example/ingest",
        verify=False,
    )
    return run, driver


def the_case():
    return cases.FlowCase(argparse.Namespace())


class TestTheCaseCreatesBothSources:
    def test_a_stack_with_neither_gets_both_from_the_catalogue(self):
        engine = Engine()
        run, driver = a_run(engine)
        case = the_case()
        case.create(run)
        posted = [
            c for c in engine.calls if c[:2] == ("POST", "/sources/from-catalogue")
        ]
        assert [e["entry"] for e in posted[0][2]["entries"]] == list(flow.SOURCES)
        assert ("POST", "/sources/sflow/deploy", None) in engine.calls
        assert driver.rows["add-source"][0] == "api-fallback"
        assert case.owns_source

    def test_sources_already_there_are_adopted_not_created(self):
        engine = Engine(present=flow.SOURCES)
        run, driver = a_run(engine)
        case = the_case()
        case.create(run)
        assert not [c for c in engine.calls if c[1] == "/sources/from-catalogue"]
        assert driver.rows["add-source"][0] == "skipped"
        assert not case.owns_source

    def test_a_listener_that_will_not_open_stops_the_run(self):
        run, _ = a_run(Engine(opens=False))
        with pytest.raises(RuntimeError, match="flow listener did not open"):
            the_case().create(run)


class TestTheCaseJudgesTheRowsGained:
    def prove(self, *, before, after):
        run, driver = a_run(Engine(present=flow.SOURCES), Store(after))
        case = the_case()
        case._before = before
        case.LAND_DEADLINE = 0
        case.prove(run)
        return driver.rows

    @staticmethod
    def totals(source, *, rows, addressed, enveloped, main=0):
        events = sum(f.events for f in flow.FIXTURES if f.source == source)
        typed = sum(f.addressed for f in flow.FIXTURES if f.source == source)
        return {
            "rows": rows if rows is not None else events,
            "addressed": addressed if addressed is not None else typed,
            "enveloped": enveloped if enveloped is not None else events,
            "main": main,
        }

    def counts(self, **changes):
        """The tables as they stand after a run that gained exactly what the fixtures yield."""
        found = {}
        for source in flow.SOURCES:
            gained = self.totals(source, rows=None, addressed=None, enveloped=None)
            gained.update(changes.get(source, {}))
            found[source] = gained
        return found

    def run_with(self, **changes):
        gained = self.counts(**changes)
        before = {source: dict.fromkeys(gained[source], 0) for source in flow.SOURCES}
        after = {}
        for source, held in gained.items():
            table = f"`{source}`"
            after[f"SELECT count() FROM {table} WHERE isNotNull(src_ip)"] = held[
                "addressed"
            ]
            after[f"SELECT count() FROM {table} WHERE isNotNull(exporter_ip)"] = held[
                "enveloped"
            ]
            after[f"= '{source}'"] = held["main"]
            after[f"SELECT count() FROM {table}"] = held["rows"]
        return self.prove(before=before, after=after)

    def test_every_row_landed_typed_and_routed_reads_done(self):
        rows = self.run_with()
        assert {slug: status for slug, (status, _) in rows.items()} == {
            f"flow-{source}-{step}": "done"
            for source in flow.SOURCES
            for step in ("landed", "typed")
        }

    @pytest.mark.parametrize("source", flow.SOURCES)
    @pytest.mark.parametrize(
        ("key", "value", "failing"),
        [
            ("rows", 0, "landed"),
            ("main", 1, "landed"),
            ("addressed", -1, "typed"),
            ("enveloped", 0, "typed"),
        ],
    )
    def test_each_way_a_table_falls_short_fails_its_own_step(
        self, source, key, value, failing
    ):
        rows = self.run_with(**{source: {key: value}})
        assert rows[f"flow-{source}-{failing}"][0] == "failed"

    def test_nothing_sent_is_unproven_not_absent(self):
        run, driver = a_run(Engine(present=flow.SOURCES))
        the_case().prove(run)
        assert driver.rows["landed"][0] == "unproven"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
