#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_consumer_group_protocol.py
#  Purpose:      Pin the consumer-group protocol the elastic transform joins with.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The elastic transform joins its consumer group as `classic`.

A broker refuses to upgrade a classic group holding a cooperative-sticky member
to the KIP-848 protocol, and answers the newcomer with a fatal GroupIdNotFound.
scalo keeps that consumer, so the pod stays Ready with no partitions. Every DFE
consumer on one protocol is what keeps a group from ever mixing the two.

This app is the one deployment that can pin it from here: it builds its transport
through scalo's KafkaConfig::from_env (dfe-transform-elastic src/service.rs),
which reads KAFKA_CONSUMER_PROTOCOL. The loader, archiver and transform-vrl
build theirs by hand and take the protocol only from a rebuilt image. from_env
ignores a value it cannot parse, an empty one included (scalo-rs 2.14.3
src/transport/kafka/config.rs:2143), so an empty name leaves scalo to decide.

The bus profiles are read from the goldens under fixtures/thin-renders; the dial
renders live through the scalo-service library (_weave.library).

    python3 scripts/tests/test_consumer_group_protocol.py

Needs `helm` on PATH.
"""

import sys

from _expect import expect, standalone, summary
from _gate import CLOUDS, effective_env, golden
from _weave import render_app

ELASTIC = "dfe-transform-elastic"
BUS_PROFILES = ("single", "scale")
DIRECT_PROFILES = ("slim", "mesh")


def env_of(docs: list[dict]) -> dict[str, object]:
    """The transform container's effective env."""
    return effective_env(docs, f"Deployment/{ELASTIC}")


def test_the_elastic_consumer_joins_as_classic() -> None:
    for profile in BUS_PROFILES:
        for cloud in CLOUDS:
            env = env_of(golden(ELASTIC, profile, cloud))
            expect(
                f"the elastic deployment joins as classic [{profile}/{cloud}]",
                env.get("KAFKA_CONSUMER_PROTOCOL") == "classic",
                f"got {env.get('KAFKA_CONSUMER_PROTOCOL')!r}",
            )


def test_an_empty_protocol_leaves_scalo_to_decide() -> None:
    docs = render_app(ELASTIC, "single", "local", overlay={"kafka": {"consumerProtocol": ""}})
    got = env_of(docs).get("KAFKA_CONSUMER_PROTOCOL")
    expect(
        "an empty kafka.consumerProtocol renders no protocol value",
        got in (None, ""),
        f"got {got!r}",
    )


def test_the_direct_transport_renders_no_protocol() -> None:
    """With Kafka out of the deployment there is no group to join."""
    for profile in DIRECT_PROFILES:
        for cloud in CLOUDS:
            env = env_of(golden(ELASTIC, profile, cloud))
            expect(
                f"the {profile} profile renders no KAFKA_CONSUMER_PROTOCOL [{cloud}]",
                "KAFKA_CONSUMER_PROTOCOL" not in env,
                f"got {env.get('KAFKA_CONSUMER_PROTOCOL')!r}",
            )


def main() -> int:
    with standalone():
        test_the_elastic_consumer_joins_as_classic()
        test_an_empty_protocol_leaves_scalo_to_decide()
        test_the_direct_transport_renders_no_protocol()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
