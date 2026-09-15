#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_source_beats.py
#  Purpose:      Prove the filebeat and logstash pair the source suite stands up
#                is configured to reach this run's own source, reads the agent's
#                own counts, and pins both images by digest.
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

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from acceptance.source import beats  # noqa: E402

SOURCE = "fb1234abcd"
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


class TestWhatTheContainersAreTold:
    def test_the_corpus_is_a_file_on_disk_for_the_agent_to_tail(self, pair):
        corpus = beats.write_inputs(pair, ["one line", "two line"], SOURCE)

        assert (corpus / "cisco_ios.log").read_text(encoding="utf-8") == "one line\ntwo line\n"

    def test_filebeat_ships_to_this_runs_own_logstash(self, pair):
        beats.write_inputs(pair, ["a"], SOURCE)
        config = (pair.workdir / "filebeat.yml").read_text(encoding="utf-8")

        assert f'hosts: ["{pair.logstash}:5044"]' in config
        # The path it tails is the path every event it ships carries, so the two
        # are one constant and the envelope proof can look for it.
        assert f"- {beats.AGENT_PATH}" in config

    def test_the_fingerprint_is_cut_to_what_a_short_corpus_allows(self, pair):
        """filestream ingests nothing at all from a file under its fingerprint length."""
        beats.write_inputs(pair, ["a"], SOURCE)
        config = (pair.workdir / "filebeat.yml").read_text(encoding="utf-8")

        assert "prospector.scanner.fingerprint.length: 64" in config

    def test_logstash_adds_the_field_the_source_is_matched_on(self, pair):
        beats.write_inputs(pair, ["a"], SOURCE)
        config = (pair.workdir / "logstash.conf").read_text(encoding="utf-8")

        assert f'add_field => {{ "_source" => "{SOURCE}" }}' in config

    def test_logstash_posts_its_whole_event_at_the_receiver(self, pair):
        beats.write_inputs(pair, ["a"], SOURCE)
        config = (pair.workdir / "logstash.conf").read_text(encoding="utf-8")

        assert 'url => "http://dfe-receiver:8080/ingest"' in config
        assert 'format => "json"' in config
        assert "beats { port => 5044 }" in config

    def test_two_runs_never_share_a_container(self, tmp_path):
        first = beats.Pair(network="n", receiver_url="u", workdir=tmp_path, run_id="src-a")
        second = beats.Pair(network="n", receiver_url="u", workdir=tmp_path, run_id="src-b")

        assert first.filebeat != second.filebeat
        assert first.logstash != second.logstash

    def test_both_images_are_pinned_by_digest(self):
        """The repo pins every external image by digest, a test harness included."""
        for image in (beats.FILEBEAT_IMAGE, beats.LOGSTASH_IMAGE):
            assert f":{beats.ELASTIC_VERSION}@sha256:" in image
            assert len(image.rsplit("sha256:", 1)[-1]) == 64


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
        "@timestamp": "2026-09-15T21:20:00Z", "_source": SOURCE, "message": "...",
        "log": {"file": {"path": beats.AGENT_PATH}, "offset": 0},
        "agent": {"type": "filebeat"}, "ecs": {"version": "8.0.0"}, "tags": ["beats_input_raw_event"],
    })

    def test_it_counts_the_rows_that_came_off_the_agent_and_names_one(self):
        store = FakeStore(self.LANDED, carrying=12)

        carrying, keys, refused = beats.envelope_evidence(store, "fb1")

        assert refused == ""
        assert carrying == 12
        assert keys == ["@timestamp", "_source", "agent", "ecs", "log", "message", "tags"]

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


def main() -> int:
    return pytest.main([__file__, "-q"])


if __name__ == "__main__":
    sys.exit(main())
