#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_archive_destination.py
#  Purpose:      Prove the archiver gets a destination it can WRITE to, and that
#                nothing in the chart outranks the compiled topic list.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The archiver is idle unless it has somewhere to write and something to read.

Its rootfs is read-only, so a `file://` destination with no writable volume
behind it is an archiver that reports itself healthy and drops every record on
an EROFS. The chart therefore pairs `archive.localPath` with the emptyDir it
mounts there.

Which topics it reads is not the chart's to say: they are compiled from the
sources that asked to be archived and arrive as `config.kafka.topics` in the
engine's overlay. The archiver applies KAFKA_TOPIC_INCLUDE after loading that
file, so a discovery pattern in the chart turns an empty compiled list -- archive
nothing -- into archive every topic the broker holds.

    python3 scripts/tests/test_archive_destination.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

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


def test_the_spool_volume_is_bounded() -> None:
    """The archiver's own brake reads the node's free space, so an unsized
    emptyDir lets a spooling pod fill the node instead of being evicted."""
    spec = container(*ON_THE_BUS)
    spool = next(v for v in spec["volumes"] if v["name"] == "spool")
    expect(
        "the spool emptyDir carries a sizeLimit",
        bool(spool.get("emptyDir", {}).get("sizeLimit")),
        f"got {spool!r}",
    )
    expect(
        "and it is the app's own max_spool_bytes default",
        spool["emptyDir"]["sizeLimit"] == "10Gi",
        f"got {spool['emptyDir']['sizeLimit']!r}, the app defaults to 10GiB",
    )


def test_an_unsized_spool_still_renders_a_volume() -> None:
    """A deployment that wants the node's whole disk clears the dial, and an
    `emptyDir:` with nothing under it is not a volume source at all."""
    spec = container(*ON_THE_BUS, "--set", "spool.sizeLimit=")
    spool = next(v for v in spec["volumes"] if v["name"] == "spool")
    expect("the volume is still an emptyDir", spool.get("emptyDir") == {}, f"got {spool!r}")


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
        # The detail names the env entry only, so no Secret coordinate reaches a CI log.
        expect(f"{name} comes from the kafka user secret",
               ref.get("name") == "dfe-kafka-user" and ref.get("key") == key,
               f"{name} is missing, or reads another Secret or key")


def test_the_chart_sends_no_discovery_pattern() -> None:
    """No overlay yet, so the compiled list is empty -- and empty must archive nothing."""
    env = env_of(container(*ON_THE_BUS))
    expect("no discovery pattern on the bus", "KAFKA_TOPIC_INCLUDE" not in env,
           f"got {env.get('KAFKA_TOPIC_INCLUDE')!r}")
    expect("and no topic list either", "KAFKA_TOPICS" not in env,
           f"got {env.get('KAFKA_TOPICS')!r}")


def test_a_values_pattern_still_renders_no_env() -> None:
    """The dial is gone, not emptied: a leftover values key must not revive the env."""
    env = env_of(container(*ON_THE_BUS, "--set", "kafka.topicInclude={_land$}"))
    expect("a stale topicInclude reaches nothing", "KAFKA_TOPIC_INCLUDE" not in env,
           f"got {env.get('KAFKA_TOPIC_INCLUDE')!r}")


def test_the_overlay_topics_survive_into_the_config() -> None:
    """The compiled list arrives in the config blob with no env shadowing it."""
    spec = container(*ON_THE_BUS, "--set", "config.kafka.topics={okta_land}")
    env = env_of(spec)
    expect("nothing outranks the compiled list", "KAFKA_TOPIC_INCLUDE" not in env,
           f"got {env.get('KAFKA_TOPIC_INCLUDE')!r}")
    configmap = next(d for d in render(*ON_THE_BUS, "--set", "config.kafka.topics={okta_land}")
                     if d.get("kind") == "ConfigMap")
    blob = yaml.safe_load(configmap["data"]["archiver.yaml"])
    expect("and the overlay's topics reach archiver.yaml",
           blob.get("kafka", {}).get("topics") == ["okta_land"],
           f"got {blob.get('kafka', {}).get('topics')!r}")


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
        test_the_spool_volume_is_bounded()
        test_an_unsized_spool_still_renders_a_volume()
        test_the_broker_reaches_the_app()
        test_the_sasl_credential_rides_its_secret()
        test_the_chart_sends_no_discovery_pattern()
        test_a_values_pattern_still_renders_no_env()
        test_the_overlay_topics_survive_into_the_config()
        test_the_direct_transport_reads_no_topics()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
