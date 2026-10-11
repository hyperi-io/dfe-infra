#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_archive_destination.py
#  Purpose:      Prove the archiver gets a destination it can WRITE to, and that
#                nothing in the deployment outranks the compiled topic list.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The archiver is idle unless it has somewhere to write and something to read.

Its rootfs is read-only, so a `file://` destination with no writable volume
behind it is an archiver that reports itself healthy and drops every record on
an EROFS. argocd/values/apps/dfe-archiver/local-dfe.yaml therefore pairs the
cloud's `archive.localPath` with the emptyDir it mounts there.

Which topics it reads is not the deployment's to say: they are compiled from the
sources that asked to be archived and arrive as `config.kafka.topics` in the
engine's overlay. The archiver applies KAFKA_TOPIC_INCLUDE after loading that
file, so a discovery pattern in the env turns an empty compiled list -- archive
nothing -- into archive every topic the broker holds.

The app reads ARCHIVER_DESTINATION through scalo's flat env, which takes an empty
value as unset (dfe-archiver crates/core/src/config.rs:667), so a render with no
local path may carry the name empty. Defaults are read from the goldens under
fixtures/thin-renders; the dials and the local-dfe cloud render live.

    python3 scripts/tests/test_archive_destination.py

Needs `helm` on PATH and the scalo-service library (_weave.library).
"""

import sys
from pathlib import Path

import pytest
import yaml

from _expect import expect, standalone, summary
from _gate import effective_env, golden, goldens, object_id
from _weave import render_app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ARCHIVER = "dfe-archiver"
# The cloud whose values file names a local archive path.
LOCAL_CLOUD = "local-dfe"
LOCAL_PATH = yaml.safe_load(
    (REPO_ROOT / "argocd" / "values" / f"{LOCAL_CLOUD}.yaml").read_text(encoding="utf-8")
)["archive"]["localPath"]
BUS = ("single", "local")
DIRECT_PROFILE = "slim"
BUS_PROFILES = ("single", "scale")


def deployment(docs: list[dict]) -> dict:
    return next(d for d in docs if object_id(d) == f"Deployment/{ARCHIVER}")


def pod(docs: list[dict]) -> dict:
    return deployment(docs)["spec"]["template"]["spec"]


def env_of(docs: list[dict]) -> dict[str, object]:
    return effective_env(docs, f"Deployment/{ARCHIVER}")


def mount_at(docs: list[dict], path: str) -> tuple[dict | None, dict | None]:
    """The main container's mount at `path` and the volume behind it."""
    spec = pod(docs)
    mount = next(
        (m for m in spec["containers"][0]["volumeMounts"] if m["mountPath"] == path), None
    )
    if mount is None:
        return None, None
    volume = next((v for v in spec["volumes"] if v["name"] == mount["name"]), None)
    return mount, volume


def bus_goldens() -> list[tuple[tuple[str, ...], list[dict]]]:
    return [(c, docs) for c, docs in goldens(ARCHIVER) if c[1] in BUS_PROFILES]


def test_local_disk_is_a_destination_and_a_volume() -> None:
    docs = render_app(ARCHIVER, BUS[0], LOCAL_CLOUD)
    env = env_of(docs)
    expect("the destination names the mounted path",
           env.get("ARCHIVER_DESTINATION") == f"file://{LOCAL_PATH}",
           f"got {env.get('ARCHIVER_DESTINATION')!r}")
    mount, volume = mount_at(docs, LOCAL_PATH)
    expect("and it is mounted, not written onto the read-only rootfs",
           mount is not None and not mount.get("readOnly"), f"got {mount!r}")
    expect("backed by an emptyDir", volume is not None and "emptyDir" in volume,
           f"got {volume!r}")


@pytest.mark.xfail(
    strict=True,
    reason="argocd/values/apps/dfe-archiver mounts archive.localPath on local-dfe only",
)
def test_a_local_path_on_any_cloud_gets_its_volume() -> None:
    overlay = {"archive": {"localPath": LOCAL_PATH}}
    docs = render_app(ARCHIVER, BUS[0], "aws", overlay=overlay)
    expect("the overlay's path is the destination",
           env_of(docs).get("ARCHIVER_DESTINATION") == f"file://{LOCAL_PATH}",
           f"got {env_of(docs).get('ARCHIVER_DESTINATION')!r}")
    mount, volume = mount_at(docs, LOCAL_PATH)
    expect("and a writable volume is mounted there", mount is not None and volume is not None,
           f"got mount {mount!r}")


def test_no_local_path_adds_neither() -> None:
    """The product default: no destination, so the archiver reports itself idle."""
    for where, docs in goldens(ARCHIVER):
        at = "/".join(where[1:])
        destination = env_of(docs).get("ARCHIVER_DESTINATION")
        expect(f"no destination env [{at}]", destination in (None, ""), f"got {destination!r}")
        mounts = sorted(m["mountPath"] for m in pod(docs)["containers"][0]["volumeMounts"])
        expect(f"and nothing mounted at the local path [{at}]", LOCAL_PATH not in mounts,
               f"got {mounts}")


def test_the_spool_lands_on_a_writable_volume() -> None:
    """The working directory and the spool directory are both mounted volumes."""
    for where, docs in goldens(ARCHIVER):
        at = "/".join(where[1:])
        container = pod(docs)["containers"][0]
        mounts = {m["mountPath"] for m in container["volumeMounts"]}
        working = container.get("workingDir")
        expect(f"the working directory is a mounted volume [{at}]",
               working in mounts, f"workingDir {working!r} is not one of {sorted(mounts)}")
        spool = env_of(docs).get("ARCHIVER_SPOOL_DIR")
        expect(f"and so is the spool directory [{at}]", spool in mounts,
               f"ARCHIVER_SPOOL_DIR {spool!r} is not one of {sorted(mounts)}")


def test_the_spool_volume_is_bounded() -> None:
    """The archiver's own brake reads the node's free space, so an unsized
    emptyDir lets a spooling pod fill the node instead of being evicted."""
    for where, docs in goldens(ARCHIVER):
        at = "/".join(where[1:])
        _, spool = mount_at(docs, str(env_of(docs).get("ARCHIVER_SPOOL_DIR")))
        limit = ((spool or {}).get("emptyDir") or {}).get("sizeLimit")
        expect(f"the spool emptyDir carries a sizeLimit [{at}]", bool(limit), f"got {spool!r}")
        expect(
            f"and it is the app's own max_spool_bytes default [{at}]",
            limit == "10Gi",
            f"got {limit!r}, the app defaults to 10GiB",
        )


def test_an_unsized_spool_still_renders_a_volume() -> None:
    """A cleared size dial must still leave a volume source, never `emptyDir:` with
    nothing under it, which is not a volume source at all."""
    overlay = {"writablePaths": {"spool": {"sizeLimit": ""}}}
    docs = render_app(ARCHIVER, *BUS, overlay=overlay)
    _, spool = mount_at(docs, str(env_of(docs).get("ARCHIVER_SPOOL_DIR")))
    expect("the volume is still an emptyDir", isinstance((spool or {}).get("emptyDir"), dict),
           f"got {spool!r}")


def test_the_broker_reaches_the_app() -> None:
    """The app reads bare KAFKA_*; BOOTSTRAP_SERVERS left it on localhost:9092."""
    env = env_of(render_app(ARCHIVER, *BUS, overlay={"kafka": {"bootstrapServers": "broker:9092"}}))
    expect("the broker list arrives under the name the app reads",
           env.get("KAFKA_BROKERS") == "broker:9092", f"got {env.get('KAFKA_BROKERS')!r}")
    for where, docs in bus_goldens():
        env = env_of(docs)
        expect(f"and not under one it ignores [{'/'.join(where[1:])}]",
               "KAFKA_BOOTSTRAP_SERVERS" not in env,
               f"got {env.get('KAFKA_BOOTSTRAP_SERVERS')!r}")


def test_the_sasl_credential_rides_its_secret() -> None:
    """The app derives nothing from the provider, so protocol and mechanism are named."""
    for where, docs in bus_goldens():
        at = "/".join(where[1:])
        env = env_of(docs)
        expect(f"the listener protocol is named [{at}]",
               env.get("KAFKA_SECURITY_PROTOCOL") == "SASL_PLAINTEXT",
               f"got {env.get('KAFKA_SECURITY_PROTOCOL')!r}")
        for name, key in (("KAFKA_SASL_USER", "username"),
                          ("KAFKA_SASL_PASSWORD", "password"),
                          ("KAFKA_SASL_MECHANISM", "sasl.mechanism")):
            value = env.get(name)
            ref = (value or {}).get("valueFrom", {}).get("secretKeyRef", {}) if isinstance(
                value, dict) else {}
            # The detail names the env entry only, so no Secret coordinate reaches a CI log.
            expect(f"{name} comes from the kafka user secret [{at}]",
                   ref.get("name") == "dfe-kafka-user" and ref.get("key") == key,
                   f"{name} is missing, or reads another Secret or key")


def test_the_chart_sends_no_discovery_pattern() -> None:
    """No overlay yet, so the compiled list is empty -- and empty must archive nothing."""
    for where, docs in bus_goldens():
        at = "/".join(where[1:])
        env = env_of(docs)
        expect(f"no discovery pattern on the bus [{at}]", "KAFKA_TOPIC_INCLUDE" not in env,
               f"got {env.get('KAFKA_TOPIC_INCLUDE')!r}")
        expect(f"and no topic list either [{at}]", "KAFKA_TOPICS" not in env,
               f"got {env.get('KAFKA_TOPICS')!r}")


def test_a_values_pattern_still_renders_no_env() -> None:
    """The dial is gone, not emptied: a leftover values key must not revive the env."""
    env = env_of(render_app(ARCHIVER, *BUS, overlay={"kafka": {"topicInclude": ["_land$"]}}))
    expect("a stale topicInclude reaches nothing", "KAFKA_TOPIC_INCLUDE" not in env,
           f"got {env.get('KAFKA_TOPIC_INCLUDE')!r}")


def test_the_overlay_topics_survive_into_the_config() -> None:
    """The compiled list arrives in the config blob with no env shadowing it."""
    docs = render_app(ARCHIVER, *BUS, overlay={"config": {"kafka": {"topics": ["okta_land"]}}})
    env = env_of(docs)
    expect("nothing outranks the compiled list", "KAFKA_TOPIC_INCLUDE" not in env,
           f"got {env.get('KAFKA_TOPIC_INCLUDE')!r}")
    configmap = next(d for d in docs if object_id(d) == f"ConfigMap/{ARCHIVER}-config")
    blob = yaml.safe_load(configmap["data"]["archiver.yaml"])
    expect("and the overlay's topics reach archiver.yaml",
           blob.get("kafka", {}).get("topics") == ["okta_land"],
           f"got {blob.get('kafka', {}).get('topics')!r}")


def test_the_direct_transport_reads_no_topics() -> None:
    """A bound listener takes records from a sender, so a topic pattern is noise."""
    env = env_of(render_app(ARCHIVER, DIRECT_PROFILE, LOCAL_CLOUD))
    expect("no topic env on the direct transport", "KAFKA_TOPIC_INCLUDE" not in env,
           f"got {env.get('KAFKA_TOPIC_INCLUDE')!r}")
    expect("but the destination still applies",
           env.get("ARCHIVER_DESTINATION") == f"file://{LOCAL_PATH}",
           f"got {env.get('ARCHIVER_DESTINATION')!r}")
    for profile in (DIRECT_PROFILE, "mesh"):
        env = env_of(golden(ARCHIVER, profile, "aws"))
        expect(f"no topic env on {profile}", "KAFKA_TOPIC_INCLUDE" not in env,
               f"got {env.get('KAFKA_TOPIC_INCLUDE')!r}")


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
