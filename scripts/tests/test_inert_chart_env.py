#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_inert_chart_env.py
#  Purpose:      Pin the absence of deployment dials and mounts no app reads.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for dfe-infra #181, #271, #66 and the residue of #79.

A key nothing reads renders green, lints green and passes a server-side dry-run,
because the API server has no opinion about a name no process reads. That is why
this class needs a render test rather than a review: every check here reads the
rendered manifest and asserts what is NOT in it.

**#181 and #66 -- the elastic deployment's topic and group dials.** They were
offered as operator dials, rendered env vars, and did nothing: scalo's
`KafkaConfig::from_env` builds its bare fallback from the CANONICAL name alone
(scalo-rs 2.13.0 transport/kafka/config.rs:1880), so `KAFKA_GROUP_ID` lands and
`KAFKA_CONSUMER_GROUP` does not, and the two topic names are not KafkaConfig
fields at all. `KAFKA_BOOTSTRAP_SERVERS` IS read, so it stays -- this is the one
app where a bare name is the right one (src/service.rs:894).

**#271 -- the Vector binary cache.** dfe-transform-vector#66 removed
`vector.cache_dir` when resolve(), cache_path() and seed_cache() were found to
have no callers, so nothing sets the env var or mounts the volume.

**#79 -- an unnamed Secret reference.** A `secretKeyRef` whose name renders
empty is an invalid manifest the API server rejects. The check below derives its
app set from apps.yaml so it covers every app the manifest declares and needs no
hand-kept list.

The renders are the goldens under fixtures/thin-renders, which test_thin_renders.py
holds equal to what Argo deploys; the cleared-secret case renders live through
the scalo-service library (_weave.library).

    python3 scripts/tests/test_inert_chart_env.py

Needs `helm` on PATH.
"""

from pathlib import Path

import pytest
import yaml

from _expect import expect, standalone, summary
from _gate import APPSETS, INSTANCE, WORKLOAD_KINDS, effective_env, goldens, references
from _weave import render_app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST = REPO_ROOT / "apps.yaml"
COMMON_VALUES = REPO_ROOT / "argocd" / "values" / "common.yaml"
APP_VALUES = REPO_ROOT / "argocd" / "values" / "apps"

ELASTIC = "dfe-transform-elastic"
VECTOR = "dfe-transform-vector"
BUS_PROFILES = ("single", "scale")

# The names scalo's from_env fallback never looks up, so a deployment rendering one
# is addressing a namespace nothing reads.
INERT_ELASTIC_ENV = ("KAFKA_SOURCE_TOPIC", "KAFKA_DEST_TOPIC", "KAFKA_CONSUMER_GROUP")

# The values keys that fed them. Offering a dial the app cannot honour invites a
# hand edit the engine then re-syncs away as drift.
INERT_ELASTIC_VALUES = ("sourceTopic", "destTopic", "consumerGroup")

VECTOR_DATA_DIR = "/var/lib/vector"
VECTOR_CACHE_DIR = "/var/cache/vector"


def manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def label(where: tuple[str, ...]) -> str:
    return "/".join(where[1:])


def bus_goldens(app: str) -> list[tuple[tuple[str, ...], list[dict]]]:
    return [(c, docs) for c, docs in goldens(app) if c[1] in BUS_PROFILES]


def pod_specs(docs: list[dict]) -> list[dict]:
    return [d["spec"]["template"]["spec"] for d in docs if d.get("kind") in WORKLOAD_KINDS]


def unnamed_refs(docs: list[dict]) -> list[str]:
    """Each Secret or ConfigMap reference whose name renders empty."""
    return [f"{kind} at {where}" for kind, name, _, where in references(docs) if not name]


# --- #181 and #66: the elastic deployment's env surface -----------------------


def test_the_elastic_chart_offers_no_routing_dials() -> None:
    for where, docs in goldens(ELASTIC):
        env = effective_env(docs, f"Deployment/{ELASTIC}")
        for name in INERT_ELASTIC_ENV:
            expect(f"the elastic deployment renders no {name} [{label(where)}]", name not in env,
                   f"got {env.get(name)!r}")


def test_the_inert_elastic_values_went_with_them() -> None:
    """A dial left in an app's values is still an offer, even with nothing reading it."""
    files = sorted((APP_VALUES / ELASTIC).glob("*.yaml"))
    expect("the elastic integration values were found", bool(files), f"none in {APP_VALUES}")
    for path in files:
        kafka = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("kafka") or {}
        for key in INERT_ELASTIC_VALUES:
            expect(f"kafka.{key} is absent from {path.name}", key not in kafka,
                   f"got {kafka.get(key)!r}")


def test_the_elastic_chart_keeps_the_name_from_env_does_read() -> None:
    """The other half of #66: not every bare KAFKA_* name is a defect."""
    for where, docs in bus_goldens(ELASTIC):
        env = effective_env(docs, f"Deployment/{ELASTIC}")
        expect(
            f"the elastic deployment still renders KAFKA_BOOTSTRAP_SERVERS [{label(where)}]",
            "KAFKA_BOOTSTRAP_SERVERS" in env,
            f"got {sorted(env)}",
        )


# --- #271: the Vector binary cache -------------------------------------------


def test_the_vector_chart_mounts_no_binary_cache() -> None:
    for where, docs in goldens(VECTOR):
        at = label(where)
        env = effective_env(docs, f"Deployment/{VECTOR}")
        expect(
            f"no cache dir is handed to the supervisor [{at}]",
            "DFE_TRANSFORM_VECTOR_CACHE_DIR" not in env,
            f"got {env.get('DFE_TRANSFORM_VECTOR_CACHE_DIR')!r}",
        )
        volumes = {v["name"] for pod in pod_specs(docs) for v in pod.get("volumes", [])}
        expect(f"and no cache volume is created [{at}]", "vector-cache" not in volumes,
               f"got {sorted(volumes)}")
        mounts = {
            m["mountPath"]
            for pod in pod_specs(docs)
            for c in pod.get("containers", [])
            for m in c.get("volumeMounts", [])
        }
        expect(f"and nothing mounts {VECTOR_CACHE_DIR} [{at}]", VECTOR_CACHE_DIR not in mounts,
               f"got {sorted(mounts)}")


def test_the_data_dir_survived_the_removal() -> None:
    """Vector's data dir IS used and shares the read-only-rootfs reason."""
    for where, docs in goldens(VECTOR):
        at = label(where)
        env = effective_env(docs, f"Deployment/{VECTOR}")
        expect(
            f"the data dir is still handed over [{at}]",
            env.get("DFE_TRANSFORM_VECTOR_DATA_DIR") == VECTOR_DATA_DIR,
            f"got {env.get('DFE_TRANSFORM_VECTOR_DATA_DIR')!r}",
        )
        (pod,) = pod_specs(docs)
        mount = next(
            (m for m in pod["containers"][0]["volumeMounts"] if m["mountPath"] == VECTOR_DATA_DIR),
            None,
        )
        volume = next(
            (v for v in pod["volumes"] if mount is not None and v["name"] == mount["name"]), None
        )
        expect(f"with a writable volume mounted there [{at}]",
               volume is not None and "emptyDir" in volume, f"got {volume!r}")


# --- #79: the residue ---------------------------------------------------------


def test_no_declared_app_renders_an_unnamed_secret_reference() -> None:
    """An empty secretKeyRef name is an invalid manifest the API server rejects.

    The app set comes from apps.yaml, the SSoT for what an app is, so an app
    added there is covered without editing this file. Every profile and cloud is
    read, the brokerless profiles included.
    """
    for app in manifest()["apps"]:
        cells = goldens(app)
        # Loud, not skipped: an app that dropped out of the render matrix would
        # otherwise drop out of this sweep silently, the class this file catches.
        expect(f"{app} has goldens to read", bool(cells), "none under fixtures/thin-renders")
        for where, docs in cells:
            offenders = unnamed_refs(docs)
            expect(f"{app} names every Secret it reads [{label(where)}]", offenders == [],
                   f"got {offenders}")


@pytest.mark.xfail(
    strict=True,
    reason="secrets.kafka.existingSecret tpl-renders an empty kafka.saslSecretName unnamed",
)
def test_a_cleared_kafka_secret_name_renders_no_unnamed_reference() -> None:
    """The common overlay sets the protocol for every app, so a protocol with no
    Secret name is the case that bites."""
    clearings = (
        {"kafka": {"saslSecretName": ""}},
        {"kafka": {"saslSecretName": "", "securityProtocol": "SASL_PLAINTEXT"}},
    )
    for app in manifest()["apps"]:
        options = {"appset": APPSETS[app]} if app in APPSETS else {}
        for clearing in clearings:
            overlay = {**INSTANCE.get(app, {}).get("local", {}), **clearing}
            docs = render_app(app, "single", "local", overlay=overlay, **options)
            offenders = unnamed_refs(docs)
            expect(f"{app} names every Secret it reads {clearing}", offenders == [],
                   f"got {offenders}")


def test_the_common_overlay_carries_no_unread_sasl_block() -> None:
    """It fanned out to every app and nothing read either key."""
    kafka = yaml.safe_load(COMMON_VALUES.read_text(encoding="utf-8"))["kafka"]
    expect("common.yaml has no kafka.sasl block", "sasl" not in kafka, f"got {kafka.get('sasl')!r}")
    expect(
        "and still carries the protocol the apps do read",
        kafka.get("securityProtocol") == "SASL_PLAINTEXT",
        f"got {kafka.get('securityProtocol')!r}",
    )


# --- #278: the reload cells ---------------------------------------------------


def test_the_archiver_cell_answers_for_the_writes_the_engine_makes() -> None:
    """The app runs a reloader, and every key the engine writes is startup-bound.

    dfe-archiver crates/archiver/src/main.rs:146-163 polls every 5 s, but what it
    re-reads is buffer thresholds, memory limits and the scaling tunables
    (archiver.rs:109-111). The engine writes config.kafka.topics and the
    archive destination, both snapshotted at startup, so the one bit stays false.
    """
    archiver = manifest()["apps"]["dfe-archiver"]
    expect("the archiver cell is false", archiver["hot_reload"] is False,
           f"got {archiver['hot_reload']!r}")
    written = set(archiver["routing"]["values_paths"].values()) | set(archiver["idle_when"])
    expect(
        "and the keys it answers for are the governed ones",
        "config.kafka.topics" in written and "config.archive.destination" in written,
        f"got {sorted(written)}",
    )


def test_the_vrl_enrichment_cell_reports_the_roll_it_needs() -> None:
    """Refresh is opt-in per table and the derived entries carry {name, path} alone.

    dfe-transform-vrl src/enrichment/refresh.rs:29-42 only spawns a task for a
    table declaring `refresh`, and the entries dfe-engine derives for a mounted
    table (src/dfe_engine/appmgmt/files.py) never carry one, so a mounted table
    is materialised once and needs the pod roll.
    """
    files = {f["name"]: f for f in manifest()["apps"]["dfe-transform-vrl"]["files"]}
    expect("the vrl enrichment set rolls", files["enrichment"]["reload"] == "roll",
           f"got {files['enrichment']['reload']!r}")


def main() -> int:
    with standalone():
        test_the_elastic_chart_offers_no_routing_dials()
        test_the_inert_elastic_values_went_with_them()
        test_the_elastic_chart_keeps_the_name_from_env_does_read()
        test_the_vector_chart_mounts_no_binary_cache()
        test_the_data_dir_survived_the_removal()
        test_no_declared_app_renders_an_unnamed_secret_reference()
        test_the_common_overlay_carries_no_unread_sasl_block()
        test_the_archiver_cell_answers_for_the_writes_the_engine_makes()
        test_the_vrl_enrichment_cell_reports_the_roll_it_needs()
        return summary()


if __name__ == "__main__":
    raise SystemExit(main())
