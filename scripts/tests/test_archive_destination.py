#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_archive_destination.py
#  Purpose:      Prove the archiver gets a destination it can WRITE to and a
#                topic pattern to read, or stays idle by design.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The archiver is idle unless it has somewhere to write and something to read.

Its rootfs is read-only, so a `file://` destination with no writable volume
behind it is an archiver that reports itself healthy and drops every record on
an EROFS. The chart therefore pairs `archive.localPath` with the emptyDir it
mounts there, and `kafka.topicInclude` carries the landing-topic pattern the app
manifest promises ("reads the LANDING topics, discovered").

    python3 scripts/tests/test_archive_destination.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ARCHIVER = REPO_ROOT / "helm" / "charts" / "dfe-archiver"
LOCAL_PATH = "/var/lib/dfe/archive"
ON_THE_BUS = ("--set", "kafka.mode=cluster", "--set", "kafka.bootstrapServers=broker:9092")
LOCAL_DISK = ("--set", f"archive.localPath={LOCAL_PATH}")


def render(*args: str) -> list[dict]:
    cmd = ["helm", "template", ARCHIVER.name, str(ARCHIVER), *args]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {args}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def container(*args: str) -> dict:
    deployment = next(d for d in render(*args) if d.get("kind") == "Deployment")
    return deployment["spec"]["template"]["spec"]


def env_of(spec: dict) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for e in spec["containers"][0]["env"]}


def test_local_disk_is_a_destination_and_a_volume() -> None:
    spec = container(*ON_THE_BUS, *LOCAL_DISK)
    env = env_of(spec)
    expect("the destination names the mounted path",
           env.get("ARCHIVER_DESTINATION") == f"file://{LOCAL_PATH}",
           f"got {env.get('ARCHIVER_DESTINATION')!r}")
    mount = next(
        (m for m in spec["containers"][0]["volumeMounts"] if m["name"] == "archive"), None
    )
    expect("and it is mounted, not written onto the read-only rootfs",
           mount is not None and mount["mountPath"] == LOCAL_PATH, f"got {mount!r}")
    volume = next((v for v in spec["volumes"] if v["name"] == "archive"), None)
    expect("backed by an emptyDir", volume is not None and "emptyDir" in volume,
           f"got {volume!r}")


def test_no_local_path_adds_neither() -> None:
    """The product default: no destination, so the archiver reports itself idle."""
    spec = container(*ON_THE_BUS)
    expect("no destination env", "ARCHIVER_DESTINATION" not in env_of(spec),
           f"got {env_of(spec).get('ARCHIVER_DESTINATION')!r}")
    names = [v["name"] for v in spec["volumes"]]
    expect("and no archive volume", "archive" not in names, f"got {names}")


def test_the_spool_lands_on_a_writable_volume() -> None:
    """The spool path is relative and hardcoded, so the working directory carries it."""
    spec = container(*ON_THE_BUS, *LOCAL_DISK)
    working = spec["containers"][0].get("workingDir")
    mounts = {m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
    expect("the working directory is a mounted volume",
           working in mounts, f"workingDir {working!r} is not one of {sorted(mounts)}")


def test_the_broker_reaches_the_app() -> None:
    """The app reads bare KAFKA_*; BOOTSTRAP_SERVERS left it on localhost:9092."""
    env = env_of(container(*ON_THE_BUS))
    expect("the broker list arrives under the name the app reads",
           env.get("KAFKA_BROKERS") == "broker:9092", f"got {env.get('KAFKA_BROKERS')!r}")
    expect("and not under one it ignores", "KAFKA_BOOTSTRAP_SERVERS" not in env,
           f"got {env.get('KAFKA_BOOTSTRAP_SERVERS')!r}")


def test_the_sasl_credential_rides_its_secret() -> None:
    """The app derives nothing from the provider, so protocol and mechanism are named."""
    spec = container(*ON_THE_BUS, "--set", "kafka.securityProtocol=SASL_PLAINTEXT")
    env = {e["name"]: e for e in spec["containers"][0]["env"]}
    expect("the listener protocol is named",
           env.get("KAFKA_SECURITY_PROTOCOL", {}).get("value") == "SASL_PLAINTEXT",
           f"got {env.get('KAFKA_SECURITY_PROTOCOL')!r}")
    for name, key in (("KAFKA_SASL_USER", "username"),
                      ("KAFKA_SASL_PASSWORD", "password"),
                      ("KAFKA_SASL_MECHANISM", "sasl.mechanism")):
        ref = env.get(name, {}).get("valueFrom", {}).get("secretKeyRef", {})
        expect(f"{name} comes from the kafka user secret",
               ref.get("name") == "dfe-kafka-user" and ref.get("key") == key, f"got {ref!r}")


def test_the_landing_topics_are_discovered() -> None:
    env = env_of(container(*ON_THE_BUS))
    expect("the default pattern is the platform's landing convention",
           env.get("KAFKA_TOPIC_INCLUDE") == "_land$", f"got {env.get('KAFKA_TOPIC_INCLUDE')!r}")


def test_an_empty_pattern_sends_no_env() -> None:
    """Empty is an archiver that subscribes to nothing, not one that reads every topic."""
    env = env_of(container(*ON_THE_BUS, "--set", "kafka.topicInclude=null"))
    expect("no pattern renders no env", "KAFKA_TOPIC_INCLUDE" not in env,
           f"got {env.get('KAFKA_TOPIC_INCLUDE')!r}")


def test_the_direct_transport_reads_no_topics() -> None:
    """A bound listener takes records from a sender, so a topic pattern is noise."""
    env = env_of(container("--set", "kafka.mode=disabled", *LOCAL_DISK))
    expect("no topic env on the direct transport", "KAFKA_TOPIC_INCLUDE" not in env,
           f"got {env.get('KAFKA_TOPIC_INCLUDE')!r}")
    expect("but the destination still applies",
           env.get("ARCHIVER_DESTINATION") == f"file://{LOCAL_PATH}",
           f"got {env.get('ARCHIVER_DESTINATION')!r}")


def main() -> int:
    with standalone():
        test_local_disk_is_a_destination_and_a_volume()
        test_no_local_path_adds_neither()
        test_the_spool_lands_on_a_writable_volume()
        test_the_broker_reaches_the_app()
        test_the_sasl_credential_rides_its_secret()
        test_the_landing_topics_are_discovered()
        test_an_empty_pattern_sends_no_env()
        test_the_direct_transport_reads_no_topics()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
