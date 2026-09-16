#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_inert_chart_env.py
#  Purpose:      Pin the absence of chart dials and mounts no app reads, and the
#                bootstrap Job that stopped re-running itself.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for dfe-infra #181, #271, #66 and the residue of #79.

A chart key nothing reads renders green, lints green and passes a server-side
dry-run, because the API server has no opinion about a name no process reads.
That is why this class needs a render test rather than a review: every check
here reads the rendered manifest and asserts what is NOT in it.

**#181 and #66 -- the elastic chart's topic and group dials.** They were offered
in values.yaml as operator dials, rendered env vars, and did nothing: scalo's
`KafkaConfig::from_env` builds its bare fallback from the CANONICAL name alone
(scalo-rs transport/kafka/config.rs:1501), so `KAFKA_GROUP_ID` lands and
`KAFKA_CONSUMER_GROUP` does not, and the two topic names are not KafkaConfig
fields at all. `KAFKA_BOOTSTRAP_SERVERS` IS read, so it stays -- this is the one
app chart where a bare name is the right one (src/service.rs:438).

**#271 -- the Vector binary cache.** dfe-transform-vector#66 removed
`vector.cache_dir` when resolve(), cache_path() and seed_cache() were found to
have no callers; the chart kept setting the env var and mounting the volume.

**#79 -- an unnamed Secret reference.** A `secretKeyRef` whose name renders
empty is an invalid manifest, and the guard that prevents it is per-chart. The
check below derives its chart set from apps.yaml so it covers every app the
manifest declares and needs no hand-kept list.

    python3 scripts/tests/test_inert_chart_env.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
MANIFEST = REPO_ROOT / "apps.yaml"
COMMON_VALUES = REPO_ROOT / "argocd" / "values" / "common.yaml"

# Where a chart may live. culvert and the gateway moved under helm/edge, so a
# sweep rooted at helm/charts alone would drop them without saying so.
CHART_ROOTS = (CHARTS, REPO_ROOT / "helm" / "edge")

# The names scalo's from_env fallback never looks up, so a chart rendering one is
# addressing a namespace nothing reads.
INERT_ELASTIC_ENV = ("KAFKA_SOURCE_TOPIC", "KAFKA_DEST_TOPIC", "KAFKA_CONSUMER_GROUP")

# The values keys that fed them. Offering a dial the app cannot honour invites a
# hand edit the engine then re-syncs away as drift.
INERT_ELASTIC_VALUES = ("sourceTopic", "destTopic", "consumerGroup")


def render(chart: Path, *sets: str) -> list[dict]:
    cmd = ["helm", "template", chart.name, str(chart)]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart.name} {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def chart_dir(app: str) -> Path | None:
    """This app's chart, wherever it lives, or None when it ships none."""
    return next((root / app for root in CHART_ROOTS if (root / app).is_dir()), None)


def pod_specs(docs: list[dict]) -> list[dict]:
    """Every pod spec in these docs, whatever workload kind carries it."""
    specs = []
    for doc in docs:
        spec = doc.get("spec") or {}
        pod = spec.get("template", {}).get("spec")
        if pod is None:
            pod = spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec")
        if pod:
            specs.append(pod)
    return specs


def containers(docs: list[dict]) -> list[dict]:
    return [c for pod in pod_specs(docs) for c in pod.get("containers", [])]


def env_of(docs: list[dict], container: str) -> dict[str, str | None]:
    for c in containers(docs):
        if c["name"] == container:
            return {e["name"]: e.get("value") for e in c.get("env", [])}
    raise SystemExit(f"no container named {container} in the render")


# --- #181 and #66: the elastic chart's env surface ---------------------------


def test_the_elastic_chart_offers_no_routing_dials() -> None:
    env = env_of(render(CHARTS / "dfe-transform-elastic"), "transform")
    for name in INERT_ELASTIC_ENV:
        expect(f"the elastic chart renders no {name}", name not in env, f"got {env.get(name)!r}")


def test_the_inert_elastic_values_went_with_them() -> None:
    """A dial left in values.yaml is still an offer, even with no template reading it."""
    kafka = yaml.safe_load((CHARTS / "dfe-transform-elastic" / "values.yaml").read_text(
        encoding="utf-8"))["kafka"]
    for key in INERT_ELASTIC_VALUES:
        expect(f"kafka.{key} is gone from the elastic values", key not in kafka, f"got {kafka.get(key)!r}")


def test_the_elastic_chart_keeps_the_name_from_env_does_read() -> None:
    """The other half of #66: not every bare KAFKA_* name is a defect."""
    env = env_of(render(CHARTS / "dfe-transform-elastic"), "transform")
    expect(
        "the elastic chart still renders KAFKA_BOOTSTRAP_SERVERS",
        "KAFKA_BOOTSTRAP_SERVERS" in env,
        f"got {sorted(env)}",
    )


# --- #271: the Vector binary cache -------------------------------------------


def test_the_vector_chart_mounts_no_binary_cache() -> None:
    docs = render(CHARTS / "dfe-transform-vector")
    env = env_of(docs, "transform")
    expect(
        "no cache dir is handed to the supervisor",
        "DFE_TRANSFORM_VECTOR_CACHE_DIR" not in env,
        f"got {env.get('DFE_TRANSFORM_VECTOR_CACHE_DIR')!r}",
    )
    volumes = {v["name"] for pod in pod_specs(docs) for v in pod.get("volumes", [])}
    expect("and no cache volume is created", "vector-cache" not in volumes, f"got {sorted(volumes)}")
    mounts = {
        m["mountPath"]
        for c in containers(docs)
        for m in c.get("volumeMounts", [])
    }
    expect("and nothing mounts /var/cache/vector", "/var/cache/vector" not in mounts, f"got {sorted(mounts)}")


def test_the_data_dir_survived_the_removal() -> None:
    """Vector's data dir IS used and shares the read-only-rootfs reason."""
    docs = render(CHARTS / "dfe-transform-vector")
    env = env_of(docs, "transform")
    expect(
        "the data dir is still handed over",
        env.get("DFE_TRANSFORM_VECTOR_DATA_DIR") == "/var/lib/vector",
        f"got {env.get('DFE_TRANSFORM_VECTOR_DATA_DIR')!r}",
    )
    volumes = {v["name"] for pod in pod_specs(docs) for v in pod.get("volumes", [])}
    expect("with its volume", "vector-data" in volumes, f"got {sorted(volumes)}")


# --- #79: the residue ---------------------------------------------------------


def test_no_declared_app_renders_an_unnamed_secret_reference() -> None:
    """An empty secretKeyRef name is an invalid manifest the API server rejects.

    The chart set comes from apps.yaml, the SSoT for what an app is, so an app
    added there is covered without editing this file.

    The protocol-with-no-secret case is the one that bit: the common overlay sets
    `kafka.securityProtocol` for every chart, so a chart gating its SASL block on
    the protocol alone rendered `name: ""` the moment the secret name was cleared.
    """
    combinations = (
        (),
        ("kafka.saslSecretName=",),
        ("kafka.mode=disabled",),
        ("kafka.securityProtocol=SASL_PLAINTEXT", "kafka.saslSecretName="),
    )
    for app in manifest()["apps"]:
        chart = chart_dir(app)
        # Loud, not skipped: a chart that moved out from under this sweep would
        # otherwise drop out of it silently, which is the class this file exists
        # to catch.
        expect(f"{app} has a chart to render", chart is not None, f"none of {CHART_ROOTS}")
        if chart is None:
            continue
        for sets in combinations:
            offenders = [
                f"{app}/{c['name']}/{e['name']}"
                for c in containers(render(chart, *sets))
                for e in c.get("env", [])
                for ref in [(e.get("valueFrom") or {}).get("secretKeyRef") or {}]
                if ref and not ref.get("name")
            ]
            expect(f"{app} names every Secret it reads {sets}", offenders == [], f"got {offenders}")


def test_the_common_overlay_carries_no_unread_sasl_block() -> None:
    """It fanned out to every app chart and no chart read either key."""
    kafka = yaml.safe_load(COMMON_VALUES.read_text(encoding="utf-8"))["kafka"]
    expect("common.yaml has no kafka.sasl block", "sasl" not in kafka, f"got {kafka.get('sasl')!r}")
    expect(
        "and still carries the protocol the charts do read",
        kafka.get("securityProtocol") == "SASL_PLAINTEXT",
        f"got {kafka.get('securityProtocol')!r}",
    )


def test_the_bootstrap_topics_job_is_not_deleted_and_recreated() -> None:
    """A TTL on a tracked resource under selfHeal is a re-run loop, not a cleanup."""
    jobs = [
        d for d in render(CHARTS / "kafka", "kafka.mode=single", "kafka.provider=strimzi")
        if d.get("kind") == "Job"
    ]
    expect("the single tier renders a topics Job", len(jobs) == 1, f"got {len(jobs)}")
    for job in jobs:
        expect(
            "it sets no ttlSecondsAfterFinished",
            "ttlSecondsAfterFinished" not in job["spec"],
            f"got {job['spec'].get('ttlSecondsAfterFinished')!r}",
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
    table declaring `refresh`, and dfe-common.enrichmentTablesConfig never emits
    one, so a mounted table is materialised once and needs the pod roll.
    """
    files = {f["name"]: f for f in manifest()["apps"]["dfe-transform-vrl"]["files"]}
    expect("the vrl enrichment set rolls", files["enrichment"]["reload"] == "roll",
           f"got {files['enrichment']['reload']!r}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    raise SystemExit(main())
