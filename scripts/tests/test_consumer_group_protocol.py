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

This app is the one chart that can pin it from here: it builds its transport
through scalo's KafkaConfig::from_env (dfe-transform-elastic src/service.rs),
which reads KAFKA_CONSUMER_PROTOCOL. The loader, archiver and transform-vrl
build theirs by hand and take the protocol only from a rebuilt image.

    python3 scripts/tests/test_consumer_group_protocol.py

Needs `helm` on PATH.
"""

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ELASTIC = REPO_ROOT / "helm" / "charts" / "dfe-transform-elastic"


def env_of(*sets: str) -> dict[str, str | None]:
    """The transform container's env, rendered with `--set` for each of *sets*."""
    cmd = ["helm", "template", ELASTIC.name, str(ELASTIC)]
    for item in sets:
        cmd += ["--set", item]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {ELASTIC.name} {sets}:\n{out.stderr}")
    deployment = next(
        d for d in yaml.safe_load_all(out.stdout) if d and d.get("kind") == "Deployment"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container.get("env", [])}


def test_the_elastic_consumer_joins_as_classic() -> None:
    env = env_of()
    expect(
        "the elastic chart renders KAFKA_CONSUMER_PROTOCOL=classic",
        env.get("KAFKA_CONSUMER_PROTOCOL") == "classic",
        f"got {env.get('KAFKA_CONSUMER_PROTOCOL')!r}",
    )


def test_an_empty_protocol_leaves_scalo_to_decide() -> None:
    env = env_of("kafka.consumerProtocol=")
    expect(
        "an empty kafka.consumerProtocol renders no env",
        "KAFKA_CONSUMER_PROTOCOL" not in env,
        f"got {env.get('KAFKA_CONSUMER_PROTOCOL')!r}",
    )


def test_the_direct_transport_renders_no_protocol() -> None:
    """With Kafka out of the deployment there is no group to join."""
    env = env_of("kafka.mode=disabled")
    expect(
        "kafka.mode=disabled renders no KAFKA_CONSUMER_PROTOCOL",
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
