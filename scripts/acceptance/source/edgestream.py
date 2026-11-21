#  Project:      dfe-infra
#  File:         acceptance/source/edgestream.py
#  Purpose:      The syslog events an EdgeStream Hub forwards, built and posted as
#                the hub posts them, for the source that routes on the hub's field.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""source.edgestream -- an EdgeStream Hub's syslog feed, as the hub posts it.

The hub wraps each syslog-ng RFC 5424 event in an ``@edgestream`` object and
posts a gzip JSON array to the receiver. Nothing in it names a DFE source, so the
catalogue's ``edgestream-hub-syslog`` source matches the hub's
``@edgestream.event.src_type``. ``fixtures/edgestream/hub-syslog.json`` holds
events of that shape. The values are made up.
"""

import copy
import gzip
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from acceptance.source import steps

#: The catalogue entry, and so the source and its table, which the engine creates.
CATALOGUE_ENTRY = "edgestream-hub-syslog"
#: The match the engine must read the entry back with: a field the hub sends.
MATCH = {
    "field": "@edgestream.event.src_type",
    "operator": "equals",
    "value": "syslog_ng",
}
#: A source type the hub sends that this source must leave for ``main``.
OTHER_SRC_TYPE = "vector"
OTHER = "other-src-type"
FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "edgestream" / "hub-syslog.json"
)
#: How the hub stamps a time: seconds, in UTC, with an explicit offset.
STAMP = "%Y-%m-%dT%H:%M:%S+00:00"
#: Columns the hub's sample shows it fills, and the ones the RFC 5424 fields add.
SAMPLE_COLUMNS = (
    "host",
    "host_from",
    "facility",
    "message",
    "event_name",
    "event_src_type",
)
HUB_COLUMNS = ("hub_org_id", "hub_site_id")
RFC5424_COLUMNS = ("severity", "program")


def recorded_events() -> list[dict]:
    """The fixture's events, each a syslog-ng event inside the hub's ``@edgestream`` object."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def stamped(
    events: Sequence[dict], run: str, kind: str, src_type: str = ""
) -> list[dict]:
    """Copies of *events* marked as this run's *kind*, each with a different event time.

    The times run back from the current time a second apiece, so a row's ``_timestamp`` says
    whether it came from the event or from the load.

    Args:
        events: The events to copy.
        run: The run's id, which the marker carries.
        kind: What the marker says the event is, as ``steps.marked`` reads it.
        src_type: The hub's ``event.src_type`` to set, the event's when empty.

    Returns:
        The copies, in the order given.
    """
    newest = datetime.now(UTC).replace(microsecond=0)
    copies = []
    for index, event in enumerate(events):
        stamp = (newest - timedelta(seconds=index)).strftime(STAMP)
        duplicate = copy.deepcopy(event)
        duplicate["@timestamp"] = stamp
        duplicate["@edgestream"]["hub"]["timestamp"] = stamp
        duplicate[steps.MARKER] = steps.run_marker(run, kind)
        if src_type:
            duplicate["@edgestream"]["event"]["src_type"] = src_type
        copies.append(duplicate)
    return copies


def event_times(events: Sequence[dict]) -> list[int]:
    """The Unix second each event's ``@timestamp`` names."""
    parsed = [datetime.strptime(event["@timestamp"], STAMP) for event in events]
    return [int(moment.replace(tzinfo=UTC).timestamp()) for moment in parsed]


def post(receiver_url: str, verify: bool, events: Sequence[dict]) -> int:
    """POST *events* as one gzip JSON array, as the hub does, and return the status.

    Raises:
        RuntimeError: When the receiver never answers, as ``steps.post_body`` raises.
    """
    body = gzip.compress(json.dumps(list(events)).encode())
    return steps.post_body(
        receiver_url, verify, body, headers=("Content-Encoding: gzip",)
    )
