#  Project:      dfe-infra
#  File:         acceptance/source/syslog.py
#  Purpose:      Frame the dfe-docker demo's vendor syslog, send it to the receiver's
#                listeners, read the receiver's counters, and say which hop lost a line.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""source.syslog -- raw vendor syslog at the receiver's listeners.

The filebeat cases post an Elastic Agent envelope over HTTP, so a drop in the
syslog listeners never shows in them. This sends the vendor lines as a device
does: bare syslog over TCP (newline framed and octet counted, RFC 6587) and UDP,
tagged with the run in the header's hostname. The receiver counts what it took
by transport label ``syslog``, which covers all three, so the counters are read
as a delta over the whole send.
"""

import re
import select
import socket
import subprocess
import time
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "syslog"
#: The overlay paths that switch the receiver's syslog listeners on, which refuse to
#: start without client authentication unless told to accept every client.
SYSLOG_SWITCHES = {
    "config.syslog.enabled": True,
    "config.syslog.accept_unauthenticated": True,
}
SYSLOG_ENV = "DFE_RECEIVER_SYSLOG__ENABLED=true DFE_RECEIVER_SYSLOG__ACCEPT_UNAUTHENTICATED=true"
#: The largest message the receiver's TCP decoder takes: the default of its
#: ``syslog.max_message_size``. A longer one, line or octet counted, is discarded and
#: counted under reason ``oversize`` and the connection carries on. ``max_frame_size``
#: (262144) only decides whether an octet count is trusted, and the oversize message
#: stays under it so the decision is the message limit alone.
MAX_MESSAGE = 65536
OVERSIZE_OVER = 1024
#: Local0.info, the priority dfe-transform-elastic's own replayer gives a bare line.
PRIORITY = "<134>"
TAG = "replay"
#: The vendors the demo sends, which are also the labels the detector stamps as ``_source``.
VENDORS = ("cisco-asa", "fortinet")
#: How long a direct sender watches for the receiver closing a connection after an oversize frame.
PEER_CLOSE_WINDOW = 2.0
#: A pause between datagrams, so a burst does not overrun the socket buffer before the receiver reads.
DATAGRAM_PAUSE = 0.002
SEND_TIMEOUT = 120

#: For a host with no python, bash opens the socket itself. $0 and $1 are host and port.
TCP_SCRIPT = 'exec 3<>"/dev/tcp/$0/$1" && cat >&3'
UDP_SCRIPT = (
    "while IFS= read -r -d '' datagram; do printf '%s' \"$datagram\" > \"/dev/udp/$0/$1\"; done"
)

METRIC_ACCEPTED = "receiver_requests_total"
METRIC_DROPPED = "receiver_records_dropped_total"
METRIC_PARSE_FAILED = "receiver_parse_failures_total"
METRICS_PORT = 9090
#: Every syslog listener counts under this one label, whatever its transport.
TRANSPORT_LABEL = {"transport": "syslog"}


@dataclass(frozen=True, slots=True)
class Transport:
    """One way of getting a line to the receiver."""

    slug: str
    tcp: bool
    octet_counted: bool = False


TRANSPORTS = (
    Transport("tcp-lf", tcp=True),
    Transport("tcp-octet", tcp=True, octet_counted=True),
    Transport("udp", tcp=False),
)


def corpus(vendor: str, count: int) -> list[str]:
    """The first *count* lines of the vendor's demo log, unchanged."""
    text = (FIXTURES / f"{vendor}.log").read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()][:count]


def message(line: str, host: str, stamp: str) -> bytes:
    """One vendor line under an RFC 3164 header whose hostname names the run."""
    return f"{PRIORITY}{stamp} {host} {TAG}: {line}".encode()


def frame(transport: Transport, payload: bytes) -> bytes:
    """*payload* as the transport carries it: octet count, newline, or a bare datagram."""
    if transport.octet_counted:
        return b"%d " % len(payload) + payload
    return payload + b"\n" if transport.tcp else payload


def oversize(host: str, stamp: str) -> bytes:
    """A syslog message past the receiver's frame limit."""
    return message("x" * (MAX_MESSAGE + OVERSIZE_OVER), host, stamp)


def stamp_now() -> str:
    """The RFC 3164 timestamp, as ``Nov  3 12:00:00``."""
    return time.strftime("%b %e %H:%M:%S")


def _closed_within(sock: socket.socket, seconds: float) -> bool:
    """Whether the peer closed the connection inside *seconds*."""
    readable, _, _ = select.select([sock], [], [], seconds)
    if not readable:
        return False
    try:
        return sock.recv(1) == b""
    except OSError:
        return True


def _exec(argv: list[str], payload: bytes) -> str:
    """Run a sender script with *payload* on stdin, and say why it failed if it did."""
    done = subprocess.run(
        argv, input=payload, capture_output=True, check=False, timeout=SEND_TIMEOUT
    )
    why = done.stderr.decode("utf-8", errors="replace").strip()
    return "" if done.returncode == 0 else f"the sender exited {done.returncode}: {why}"


def send_tcp(
    host: str, port: int, frames: Sequence[bytes], prefix: Sequence[str] = (), watch: int = -1
) -> str:
    """Write *frames* down one connection, in order.

    Args:
        host: The receiver, as the sender sees it.
        port: The receiver's TCP syslog port.
        frames: The framed messages.
        prefix: A command prefix that runs bash where the receiver is reachable.
            Empty connects from this process.
        watch: The index of a frame after which the receiver closing the connection is
            reported. Direct sends only.

    Returns:
        What the sender saw of the connection ending early, empty when it did not.
    """
    if prefix:
        return _exec([*prefix, "bash", "-c", TCP_SCRIPT, host, str(port)], b"".join(frames))
    note = ""
    with socket.create_connection((host, port), timeout=10) as sock:
        for index, piece in enumerate(frames):
            try:
                sock.sendall(piece)
            except OSError as exc:
                return f"{note}the connection broke on frame {index + 1} of {len(frames)}: {exc}"
            if index == watch and _closed_within(sock, PEER_CLOSE_WINDOW):
                note = f"the receiver closed the connection after frame {index + 1}. "
    return note.strip()


def send_udp(host: str, port: int, datagrams: Sequence[bytes], prefix: Sequence[str] = ()) -> str:
    """Send each of *datagrams* as its own UDP packet, through *prefix* when one is given."""
    if prefix:
        return _exec(
            [*prefix, "bash", "-c", UDP_SCRIPT, host, str(port)], b"\0".join(datagrams) + b"\0"
        )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for datagram in datagrams:
            sock.sendto(datagram, (host, port))
            time.sleep(DATAGRAM_PAUSE)
    return ""


# --- the receiver's counters -------------------------------------------------

_SERIES = re.compile(r"^(?P<name>[A-Za-z_:][\w:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)$")
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def counter(exposition: str, name: str, **labels: str) -> int:
    """The sum of every series called *name* whose labels include *labels*.

    A namespace prefix on the name is accepted, since the receiver's counters are bare
    unless ``metrics.namespace`` is set. A counter with no series is 0.
    """
    total = 0.0
    for line in exposition.splitlines():
        found = _SERIES.match(line.strip())
        if not found or not (found["name"] == name or found["name"].endswith(f"_{name}")):
            continue
        held = dict(_LABEL.findall(found["labels"] or ""))
        if all(held.get(key) == value for key, value in labels.items()):
            total += float(found["value"])
    return int(total)


def counters_of(bodies: Sequence[str]) -> dict[str, int]:
    """The syslog counters summed over every replica's exposition."""
    wanted = {
        "accepted": (METRIC_ACCEPTED, {}),
        "dropped": (METRIC_DROPPED, {}),
        "oversize": (METRIC_DROPPED, {"reason": "oversize"}),
        "parse_failed": (METRIC_PARSE_FAILED, {}),
    }
    return {
        key: sum(counter(body, name, **labels, **TRANSPORT_LABEL) for body in bodies)
        for key, (name, labels) in wanted.items()
    }


def read_counters(url: str) -> dict[str, int]:
    """The syslog counters off one receiver's metrics endpoint.

    Raises:
        RuntimeError: The endpoint did not answer.
    """
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"the receiver's metrics at {url} did not answer: {exc}") from exc
    return counters_of([body])


def read_pod_counters(kube: Sequence[str], namespace: str, selector: str) -> dict[str, int]:
    """The syslog counters summed over every running receiver pod.

    A pod counts only the connections kube-proxy sent it, so one pod's counters fall
    short whenever the Service has more than one replica.

    Raises:
        RuntimeError: No pod matched, or one did not answer.
    """
    listed = subprocess.run(
        [*kube, "-n", namespace, "get", "pods", "-l", selector,
         "--field-selector", "status.phase=Running", "-o", "name"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    pods = listed.stdout.split() if listed.returncode == 0 else []
    if not pods:
        raise RuntimeError(f"no running receiver pod matched {selector} in {namespace}")
    bodies = []
    for pod in pods:
        read = subprocess.run(
            [*kube, "-n", namespace, "exec", pod, "--",
             "curl", "-sf", f"http://localhost:{METRICS_PORT}/metrics"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            timeout=30,
        )
        if read.returncode != 0:
            raise RuntimeError(f"the receiver's metrics in {pod} did not answer: {read.stderr.strip()}")
        bodies.append(read.stdout)
    return counters_of(bodies)


# --- what the run reports ----------------------------------------------------


def phase_outcome(
    expected: dict[str, int], landed: dict[str, int], hops: str, note: str
) -> tuple[str, str]:
    """The step result for one transport: each table holds exactly what was sent to it.

    Args:
        expected: Rows the transport should have added, by table label.
        landed: Rows it did add, by the same labels.
        hops: Where the rest of the path stands, from :func:`hop_report`.
        note: What the sender saw of the connection, empty when nothing.

    Returns:
        The status and detail for ``Driver.record``.
    """
    counts = ", ".join(
        f"{label} {landed.get(label, 0)} of {want}" for label, want in expected.items()
    )
    exact = all(landed.get(label, 0) == want for label, want in expected.items())
    tail = f"; {note}" if note else ""
    # A sender note is a connection that ended early, which fails the row whatever landed.
    return ("done" if exact and not note else "failed"), f"{counts}{tail}; {hops}"


def hop_report(sent: int, accepted: int | None, landed: int, intake: int, strayed: int) -> str:
    """Where a line is after each hop, so a short count names the hop that lost it.

    Args:
        sent: Good frames written so far.
        accepted: The receiver's accepted count since the run began, None when unread.
        landed: Rows the vendor tables hold, summed.
        intake: Rows in the intake source's own table.
        strayed: Run-tagged rows in the catch-all, which no source's match claimed.

    Returns:
        One line, sender first and the datastore last, ending on the hop at fault. A receiver
        that accepted every line while the tables stayed short is the loader's consumer not
        having picked up the run's fresh topics.
    """
    took = "unread" if accepted is None else str(accepted)
    line = (
        f"sent {sent}, receiver accepted {took}, vendor tables got {landed}, "
        f"intake table holds {intake}, {strayed} run-tagged row(s) reached dfe.main unrouted"
    )
    if accepted is None or landed >= sent:
        return line
    if accepted < sent:
        return f"{line}. Lost before the receiver counted it (network, listener or framing)"
    return (
        f"{line}. Rows stopped at the loader: {accepted} accepted, {landed} in the vendor tables. "
        "Its consumer may not have subscribed the new topic"
    )


def counters_outcome(
    sent: int, oversize_sent: int, before: dict[str, int], after: dict[str, int]
) -> tuple[str, str]:
    """The step result for the receiver's accepted, dropped and oversize deltas.

    The accepted count is every frame the decoder handed on, so it equals the good frames
    sent. A message past the size limit is discarded and counted as dropped with reason
    ``oversize``, so that counter rises by exactly the oversize frames sent and no other
    drop reason moves.
    """
    delta = {key: after[key] - before[key] for key in after}
    detail = (
        f"{METRIC_ACCEPTED}{{transport=syslog}} rose {delta['accepted']}, {sent} good frame(s) sent; "
        f"{METRIC_DROPPED}{{reason=oversize}} rose {delta['oversize']}, "
        f"{oversize_sent} oversize frame(s) sent; {METRIC_DROPPED} rose {delta['dropped']} in all, "
        f"{oversize_sent} expected; {METRIC_PARSE_FAILED} rose {delta['parse_failed']}, 0 expected"
    )
    ok = (
        delta["accepted"] == sent
        and delta["oversize"] == oversize_sent
        and delta["dropped"] == oversize_sent
        and delta["parse_failed"] == 0
    )
    return ("done" if ok else "failed"), detail


# --- the sources the run creates ---------------------------------------------

#: Per vendor, the engine catalogue entry the demo's source file holds and the variant
#: dfe-transform-elastic detects it as. The table is the run's own, named per run.
VENDOR_SOURCES = {
    "cisco-asa": {
        "stream": "cisco_asa",
        "variant": "filebeat.cisco_asa.default",
        "display_name": "cisco_asa.log",
    },
    "fortinet": {
        "stream": "fortinet_fortigate",
        "variant": "filebeat.fortinet.default",
        "display_name": "fortinet_fortigate.log",
    },
}


def vendor_body(vendor: str, name: str) -> dict:
    """The POST body for a vendor's table: matched on the label the detector stamps."""
    held = VENDOR_SOURCES[vendor]
    return {
        "source": name,
        "display_name": held["display_name"],
        "description": f"The log data stream of the Elastic {held['stream']} integration.",
        "from_catalogue_intake": "receiver",
        "from_catalogue_item": vendor,
        "match": {"field": "_source", "operator": "equals", "value": vendor},
        "schema": {
            "additional_fields": f"additional/{held['stream']}/log",
            "derived_schema": f"derived/{held['stream']}/log",
            "meta_schema": "meta/elastic/ecs",
        },
        "transform": {"engine": "elastic", "variant": held["variant"]},
    }


def intake_body(name: str) -> dict:
    """The POST body for the source on the syslog listener, which detects the vendor per line."""
    return {
        "source": name,
        "display_name": "Syslog acceptance intake",
        "description": "Post-deploy source test: raw vendor syslog detected per line.",
        "archive": False,
        "header": {"type": "common-header/timeseries", "version": "1.0.1"},
        "match": {"field": "_source", "operator": "equals", "value": "syslog"},
        "schema": {"meta_schema": "meta/syslog", "meta_schema_version": "1.0.0"},
        "transform": {
            "engine": "elastic",
            "mode": "agentless",
            "detect": {
                "candidates": [held["variant"] for held in VENDOR_SOURCES.values()],
                "unmatched": "passthrough",
            },
        },
    }
