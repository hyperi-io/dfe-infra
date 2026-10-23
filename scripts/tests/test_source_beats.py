#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_source_beats.py
#  Purpose:      Prove the filebeat (and logstash) the source suite stands up is
#                configured to reach this run's own source, reads the agent's own
#                counts, pins both images by digest, and judges the stored rows.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for acceptance/source/beats.py, without docker.

What the pair DOES is proven by running it against a deployment; what is
testable here is everything decided before a container starts: the two configs,
the counts read back out of filebeat's own log, and the envelope assertion made
against a landed row.

    python3 -m pytest scripts/tests/test_source_beats.py
    python3 scripts/tests/test_source_beats.py
"""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from acceptance.source import beats  # noqa: E402

DATASET = "cisco_ios.log"
RUN = "src-0123456789ab"

# One of filebeat's own periodic snapshots, trimmed to the branch that carries
# the count: it reports a delta per interval, so a run's total is their sum.
SNAPSHOT = json.dumps({
    "log.level": "info",
    "log.logger": "monitoring",
    "message": "Non-zero metrics in the last 30s",
    "monitoring": {"metrics": {"libbeat": {"output": {"events": {"acked": 9, "batches": 1}}}}},
})
WARNING = json.dumps({
    "log.level": "warn",
    "log.logger": "input.filestream.scanner",
    "message": "1 file is too small to be ingested, files need to be at least 1024 in size",
})


@pytest.fixture
def pair(tmp_path):
    return beats.Pair(
        network="dfe_default", receiver_url="http://dfe-receiver:8080/ingest",
        workdir=tmp_path / "beats", run_id=RUN,
    )


def write_inputs(pair: beats.Pair, lines: list[str]):
    """The pair's inputs, marked the way the filebeat case marks its corpus."""
    return beats.write_inputs(pair, lines, DATASET, "_e2e", {"run": RUN, "kind": "corpus"})


class TestWhatTheContainersAreTold:
    def test_the_corpus_is_a_file_on_disk_for_the_agent_to_tail(self, pair):
        corpus = write_inputs(pair, ["one line", "two line"])

        assert (corpus / "cisco_ios.log").read_text(encoding="utf-8") == "one line\ntwo line\n"

    def test_filebeat_ships_to_this_runs_own_logstash(self, pair):
        write_inputs(pair, ["a"])
        config = (pair.workdir / "filebeat.yml").read_text(encoding="utf-8")

        assert f'hosts: ["{pair.logstash}:5044"]' in config
        # The path it tails is the path every event it ships carries, so the two
        # are one constant and the envelope proof can look for it.
        assert f"- {beats.AGENT_PATH}" in config

    def test_the_fingerprint_is_cut_to_what_a_short_corpus_allows(self, pair):
        """filestream ingests nothing at all from a file under its fingerprint length."""
        write_inputs(pair, ["a"])
        config = (pair.workdir / "filebeat.yml").read_text(encoding="utf-8")

        assert "prospector.scanner.fingerprint.length: 64" in config

    def test_the_agent_stamps_the_data_stream_the_source_is_matched_on(self, pair):
        """An integration policy sets it on a real agent; a bare filestream input does not."""
        write_inputs(pair, ["a"])
        config = (pair.workdir / "filebeat.yml").read_text(encoding="utf-8")

        assert "      - add_fields:\n          target: data_stream\n" in config
        assert f"            dataset: {DATASET}\n" in config

    def test_the_stamped_namespace_is_one_a_transform_would_never_write(self, pair):
        """dfe-transform-elastic writes `default` where a record names none, so the
        stored row can only carry this one if the stamp survived."""
        write_inputs(pair, ["a"])
        config = (pair.workdir / "filebeat.yml").read_text(encoding="utf-8")

        assert f"            namespace: {beats.DATA_STREAM_NAMESPACE}\n" in config
        assert beats.DATA_STREAM_NAMESPACE != "default"

    def test_the_agent_tags_every_event_with_this_run(self, pair):
        write_inputs(pair, ["a"])
        config = (pair.workdir / "filebeat.yml").read_text(encoding="utf-8")

        assert f'tags: ["e2e_run:{RUN}"]' in config

    def test_the_agent_marks_every_event_as_this_runs_corpus_at_the_root(self, pair):
        """The counts read this marker, and the transform keeps root fields where it replaces tags."""
        write_inputs(pair, ["a"])
        config = (pair.workdir / "filebeat.yml").read_text(encoding="utf-8")

        assert (
            "      - add_fields:\n"
            "          target: _e2e\n"
            "          fields:\n"
            f'            run: "{RUN}"\n'
            '            kind: "corpus"\n'
        ) in config

    def test_logstash_adds_nothing_to_the_event_filebeat_built(self, pair):
        write_inputs(pair, ["a"])
        config = (pair.workdir / "logstash.conf").read_text(encoding="utf-8")

        assert "filter" not in config
        assert "_source" not in config

    def test_logstash_posts_its_events_at_the_receiver_a_batch_at_a_time(self, pair):
        """json_batch posts one JSON array per pipeline batch, which the receiver splits per element."""
        write_inputs(pair, ["a"])
        config = (pair.workdir / "logstash.conf").read_text(encoding="utf-8")

        assert 'url => "http://dfe-receiver:8080/ingest"' in config
        assert 'format => "json_batch"' in config
        assert "beats { port => 5044 }" in config

    def test_two_runs_never_share_a_container(self, tmp_path):
        first = beats.Pair(network="n", receiver_url="u", workdir=tmp_path, run_id="src-a")
        second = beats.Pair(network="n", receiver_url="u", workdir=tmp_path, run_id="src-b")

        assert first.filebeat != second.filebeat
        assert first.logstash != second.logstash

    def test_both_images_are_pinned_by_digest(self):
        """The repo pins every external image by digest, a test harness included."""
        for image in (beats.FILEBEAT_IMAGE, beats.LOGSTASH_IMAGE):
            assert f":{beats.SHIPPER_VERSION}@sha256:" in image
            assert len(image.rsplit("sha256:", 1)[-1]) == 64


@pytest.fixture
def direct(tmp_path):
    """A --via beats run: filebeat alone, shipping to the receiver's own listener."""
    return beats.Pair(
        network="dfe_default", receiver_url="http://dfe-receiver:8080/ingest",
        workdir=tmp_path / "beats", run_id=RUN, lumberjack="dfe-receiver:5044",
    )


class TestShippingStraightToTheReceiver:
    def test_filebeat_ships_to_the_receivers_lumberjack_listener(self, direct):
        write_inputs(direct, ["a"])
        config = (direct.workdir / "filebeat.yml").read_text(encoding="utf-8")

        assert 'output.logstash:\n  hosts: ["dfe-receiver:5044"]\n' in config
        assert direct.logstash not in config

    def test_the_operator_stamps_the_same_data_stream_either_way(self, pair, direct):
        """The stamp is the operator's config, not the route, so both modes send the same one."""
        for one in (pair, direct):
            write_inputs(one, ["a"])

        def stamp(one: beats.Pair) -> str:
            config = (one.workdir / "filebeat.yml").read_text(encoding="utf-8")
            return config[config.index("processors:"):config.index("output.logstash:")]

        assert stamp(pair) == stamp(direct)

    def test_no_logstash_config_is_written_for_a_container_that_never_runs(self, direct):
        write_inputs(direct, ["a"])

        assert not (direct.workdir / "logstash.conf").exists()

    def test_teardown_removes_only_the_container_it_started(self, direct, pair):
        assert direct.containers == (direct.filebeat,)
        assert pair.containers == (pair.filebeat, pair.logstash)


class TestPuttingTheReceiverBack:
    PATHS = tuple(beats.LUMBERJACK_SWITCHES)

    def test_an_overlay_that_set_nothing_is_cleared_back_to_the_app_defaults(self):
        config = {"fields": [{"path": "config.lumberjack.enabled", "is_set": False, "value": False}]}

        before = beats.held(config, self.PATHS)

        assert before == {}
        assert beats.restore_plan(before, self.PATHS) == ({}, list(self.PATHS))

    def test_a_value_the_overlay_already_set_is_written_back_rather_than_cleared(self):
        config = {"fields": [
            {"path": "config.lumberjack.enabled", "is_set": True, "value": False},
            {"path": "config.lumberjack.bind_address", "is_set": True, "value": "0.0.0.0:5044"},
        ]}

        before = beats.held(config, self.PATHS)

        assert before == {"config.lumberjack.enabled": False}
        assert beats.restore_plan(before, self.PATHS) == (
            {"config.lumberjack.enabled": False}, ["config.lumberjack.accept_unauthenticated"])

    def test_a_path_the_contract_does_not_declare_is_read_where_the_engine_reports_it(self):
        config = {"fields": [], "unknown": [{"path": "config.lumberjack.accept_unauthenticated", "value": True}]}

        assert beats.held(config, self.PATHS) == {"config.lumberjack.accept_unauthenticated": True}

    def test_a_body_that_is_not_the_config_view_holds_nothing(self):
        assert beats.held("bad gateway", self.PATHS) == {}


class TestWhatFilebeatSaysItDid:
    def test_the_acked_count_is_read_out_of_its_own_snapshots(self):
        assert beats._published_events(f"{SNAPSHOT}\n{SNAPSHOT}\n") == 18

    def test_a_log_with_no_snapshot_yet_counts_nothing(self):
        assert beats._published_events(f"{WARNING}\n") == 0

    def test_a_line_that_is_not_json_is_skipped_rather_than_raising(self):
        assert beats._published_events("starting filebeat\n" + SNAPSHOT) == 9

    def test_the_last_warning_is_what_it_says_when_it_ships_nothing(self):
        assert "too small to be ingested" in beats._last_warning(f"{SNAPSHOT}\n{WARNING}\n")

    def test_no_warning_reads_empty_rather_than_inventing_one(self):
        assert beats._last_warning(SNAPSHOT) == ""


class FakeStore:
    """A datastore holding rows that carry the agent's file path, or none."""

    def __init__(self, raw: str | None = None, carrying: int = 1) -> None:
        self.raw, self.carrying = raw, carrying
        self.asked: list[str] = []

    def scalar(self, sql):
        self.asked.append(sql)
        return self.carrying

    def query(self, sql):
        self.asked.append(sql)
        return [[self.raw]] if self.raw is not None else []


class TestTheEnvelopeThatLanded:
    LANDED = json.dumps({
        "@timestamp": "2026-09-15T21:20:00Z", "data_stream": {"dataset": DATASET}, "message": "...",
        "log": {"file": {"path": beats.AGENT_PATH}, "offset": 0},
        "agent": {"type": "filebeat"}, "ecs": {"version": "8.0.0"}, "tags": ["beats_input_raw_event"],
    })

    def test_it_counts_the_rows_that_came_off_the_agent_and_names_one(self):
        store = FakeStore(self.LANDED, carrying=12)

        carrying, keys, refused = beats.envelope_evidence(store, "fb1")

        assert refused == ""
        assert carrying == 12
        assert keys == ["@timestamp", "agent", "data_stream", "ecs", "log", "message", "tags"]

    def test_the_rows_are_found_by_the_path_only_an_agent_sets(self):
        store = FakeStore(self.LANDED)

        beats.envelope_evidence(store, "fb1")

        assert all(beats.AGENT_PATH in sql for sql in store.asked)

    def test_a_table_with_no_such_row_is_not_evidence_of_this_path(self):
        """A transform rewrites each record, so a table of them has to be asked."""
        carrying, keys, refused = beats.envelope_evidence(FakeStore(carrying=0), "fb1")

        assert (carrying, keys) == (0, [])
        assert "none of them came off the agent" in refused

    def test_a_raw_that_is_not_json_is_the_finding(self):
        _, keys, refused = beats.envelope_evidence(FakeStore("not json"), "fb1")

        assert keys == []
        assert "not JSON" in refused


class KeptStore:
    """A datastore answering the agent-row count, the kept count, and one row that was not kept."""

    def __init__(self, carrying: int, kept: int, landed: tuple[str, str] = ("dfe-receiver", "default")) -> None:
        self.carrying, self.kept, self.landed = carrying, kept, landed
        self.asked: list[str] = []

    def scalar(self, sql):
        self.asked.append(sql)
        return self.kept if "'agent', 'type'" in sql else self.carrying

    def query(self, sql):
        self.asked.append(sql)
        return [list(self.landed)]


class TestTheFieldsTheShipperAndOperatorSent:
    def test_every_agent_row_keeping_both_is_the_pass(self):
        status, detail = beats.kept_outcome(KeptStore(carrying=12, kept=12), "el1")

        assert status == "done"
        assert detail.startswith("12 of 12 agent row(s) kept agent.type filebeat")
        assert f"data_stream.namespace {beats.DATA_STREAM_NAMESPACE} (operator-stamped" in detail

    def test_expected_fail_a_transform_writing_its_own_names_what_landed(self):
        """What dfe-transform-elastic wrote over a forwarded Beats document before it kept them."""
        status, detail = beats.kept_outcome(KeptStore(carrying=12, kept=0), "el1")

        assert status == "failed"
        assert detail.startswith("0 of 12")
        assert "agent.type 'dfe-receiver' and data_stream.namespace 'default'" in detail

    def test_expected_fail_one_row_of_many_is_not_the_pass(self):
        assert beats.kept_outcome(KeptStore(carrying=12, kept=11), "el1")[0] == "failed"

    def test_expected_fail_a_table_with_no_agent_row_proves_nothing(self):
        status, detail = beats.kept_outcome(KeptStore(carrying=0, kept=0), "el1")

        assert status == "failed"
        assert "none of them came off the agent" in detail

    def test_only_the_rows_off_the_agent_are_judged(self):
        """The routing probes land in the same table and carry the POSTed envelope."""
        store = KeptStore(carrying=1, kept=1)

        beats.kept_outcome(store, "el1")

        assert all(beats.AGENT_PATH in sql for sql in store.asked)


def main() -> int:
    return pytest.main([__file__, "-q"])


if __name__ == "__main__":
    sys.exit(main())
