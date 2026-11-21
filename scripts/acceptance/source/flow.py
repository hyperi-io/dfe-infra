#  Project:      dfe-infra
#  File:         acceptance/source/flow.py
#  Purpose:      Read the flow exporters' datagrams out of dfe-receiver's committed
#                fixtures and send them to the receiver's flow listener.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""source.flow -- NetFlow, IPFIX and sFlow datagrams, as exporters send them.

dfe-receiver commits real captures under ``tests/fixtures/flow/external``: two
``.pcap`` files and the rest ``.bin`` files holding one UDP payload each. Its
``tests/integration/flow_exploded.rs`` decodes each file with a fresh decoder and
holds the events it yields to the counts in ``FIXTURES``, so a table here that
disagrees with the receiver fails there first.
"""

import socket
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

#: Where the receiver's repo keeps them, below its root.
FIXTURE_DIR = Path("tests") / "fixtures" / "flow" / "external"
#: The catalogue sources the flow listener's records land under, NetFlow and IPFIX then sFlow.
SOURCES = ("netflow", "sflow")
#: The overlay paths that switch the receiver's flow listener on, one event per flow record.
SWITCHES = {"config.flow.enabled": True, "config.flow.output.mode": "exploded"}
ENV = "DFE_RECEIVER_FLOW_ENABLED=true DFE_RECEIVER_FLOW_OUTPUT_MODE=exploded"
#: A pause between datagrams, so a burst does not overrun the receiver's socket buffer.
DATAGRAM_PAUSE = 0.005
#: libpcap's magic as it reads in a file of each byte order and time precision.
PCAP_MAGIC = {
    b"\xd4\xc3\xb2\xa1": "<",
    b"\xa1\xb2\xc3\xd4": ">",
    b"\x4d\x3c\xb2\xa1": "<",
    b"\xa1\xb2\x3c\x4d": ">",
}
ETHERNET = 1
UDP = 17
ETHERNET_HEADER = 14
UDP_HEADER = 8


@dataclass(frozen=True, slots=True)
class Fixture:
    """One committed capture, the source its records land under, and what it yields."""

    name: str
    source: str
    #: Events the receiver renders for the file in ``exploded`` mode.
    events: int
    #: How many of those carry a source address, which a firewall or options record may not.
    addressed: int


#: The captures in the order they are sent, each with what the receiver renders for it. A
#: template an exporter sent stays in force for the files after it, so the order is part of
#: the counts. The numbers are dfe-receiver's tests/integration/flow_exploded.rs.
FIXTURES = (
    Fixture("netgauze-pmacct-ipfix-cisco-100.pcap", "netflow", 12, 12),
    Fixture("netgauze-pmacct-nfv9-cisco-101.pcap", "netflow", 51, 51),
    Fixture("telegraf-netflow-v5.bin", "netflow", 8, 8),
    Fixture("telegraf-netflow-v9.bin", "netflow", 8, 8),
    Fixture("telegraf-netflow-v9-options.bin", "netflow", 8, 0),
    Fixture("telegraf-ipfix-example.bin", "netflow", 0, 0),
    Fixture("telegraf-ipfix-pen-35632.bin", "netflow", 0, 0),
    Fixture("telegraf-sflow-v5.bin", "sflow", 1, 0),
    Fixture("telegraf-sflow-issue-15918.bin", "sflow", 7, 6),
    Fixture("telegraf-sflow-issue-18876.bin", "sflow", 0, 0),
)


def datagrams(path: Path) -> list[bytes]:
    """The UDP payloads in one fixture: the file itself for a ``.bin``, each packet of a ``.pcap``.

    Raises:
        ValueError: When a ``.pcap`` is not a classic Ethernet capture, or holds a packet
            that is not IPv4 and UDP.
    """
    data = path.read_bytes()
    if path.suffix != ".pcap":
        return [data]
    order = PCAP_MAGIC.get(data[:4])
    if order is None or struct.unpack_from(f"{order}I", data, 20)[0] != ETHERNET:
        raise ValueError(f"{path.name} is not a classic Ethernet pcap")
    found, offset = [], 24
    while offset + 16 <= len(data):
        captured = struct.unpack_from(f"{order}I", data, offset + 8)[0]
        packet = data[offset + 16 : offset + 16 + captured]
        offset += 16 + captured
        ip = packet[ETHERNET_HEADER:]
        header = (ip[0] & 0x0F) * 4
        if ip[0] >> 4 != 4 or ip[9] != UDP:
            raise ValueError(f"{path.name} holds a packet that is not IPv4 and UDP")
        found.append(ip[header + UDP_HEADER :])
    return found


def send(host: str, port: int, payloads: Sequence[bytes]) -> None:
    """Send each of *payloads* to the flow listener as its own UDP datagram."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for payload in payloads:
            sock.sendto(payload, (host, port))
            time.sleep(DATAGRAM_PAUSE)
