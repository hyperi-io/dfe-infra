#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_app_env_contract.py
#  Purpose:      Prove each dfe-* app chart renders the env var NAMES its app
#                actually reads, and that a declared SASL secret is mounted.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Chart env names vs the apps' own flat-env contracts.

An env var with the wrong name renders green, lints green and passes a
server-side dry-run -- the API server has no opinion about a name no process
reads. The app then runs on its built-in default and reports nothing, so the
failure surfaces as "the archiver is not archiving", never as a config error.
Two live instances of exactly that:

  * dfe-archiver was handed KAFKA_BOOTSTRAP_SERVERS. Its only broker key is
    KAFKA_BROKERS (crates/core/src/config.rs:370).
  * dfe-fetcher was handed the same bare name, when its whole contract is
    prefixed DFE_FETCHER_* (src/config/mod.rs:199, :477).

The same class hides a second way: a values key that reads as wiring a
credential while no template references it. dfe-archiver and dfe-fetcher both
declared kafka.saslSecretName and mounted nothing, so both authenticated to a
SCRAM broker with no credential.

The tables below are the apps' contracts, copied from the apps' own source with
the file:line that states each one. They are checked by hand -- CI has no app
checkout -- so a renamed setting upstream shows up as a failure HERE, which is
the point: it must be a decision, not a silent drift.

    python3 scripts/tests/test_app_env_contract.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"

# Env names each app READS, per its own source. Only names this chart is
# responsible for setting -- not every name the app accepts.
#
#   dfe-archiver   bare KAFKA_*/S3_*/DLQ_*, crates/core/src/config.rs:367-471
#   dfe-fetcher    prefix DFE_FETCHER, src/config/mod.rs:199 + :475-556
#   dfe-loader     prefix DFE_LOADER,  src/config/loader.rs:188 + :197-306
#   dfe-receiver   prefix DFE_RECEIVER, src/config/mod.rs:37 + :404-472
#   dfe-transform-vrl / -vector  prefix DFE_TRANSFORM (NOT per-app),
#                  vrl src/config/loader.rs:479, vector .../loader.rs:492
REQUIRED: dict[str, set[str]] = {
    "dfe-archiver": {
        "KAFKA_BROKERS",
        "KAFKA_SASL_USER",
        "KAFKA_SASL_PASSWORD",
        "KAFKA_SASL_MECHANISM",
        "KAFKA_SECURITY_PROTOCOL",
        "DLQ_TOPIC",
        "DLQ_MODE",
    },
    "dfe-fetcher": {
        "DFE_FETCHER_KAFKA_BROKERS",
        "DFE_FETCHER_KAFKA_SASL_USER",
        "DFE_FETCHER_KAFKA_SASL_PASSWORD",
        "DFE_FETCHER_KAFKA_SASL_MECHANISM",
        "DFE_FETCHER_DLQ_TOPIC",
        "DFE_FETCHER_DLQ_MODE",
    },
    "dfe-loader": {
        "DFE_LOADER_KAFKA_BROKERS",
        "DFE_LOADER_KAFKA_SASL_USERNAME",
        "DFE_LOADER_KAFKA_SASL_PASSWORD",
        "DFE_LOADER_CLICKHOUSE_HOSTS",
        "DFE_LOADER_CLICKHOUSE_DATABASE",
        "DFE_LOADER_DLQ_TOPIC",
        "DFE_LOADER_DLQ_MODE",
    },
    "dfe-receiver": {
        "DFE_RECEIVER_KAFKA_BROKERS",
        "DFE_RECEIVER_KAFKA_SASL_USER",
        "DFE_RECEIVER_KAFKA_SASL_PASSWORD",
        "DFE_RECEIVER_KAFKA_SASL_MECHANISM",
        "DFE_RECEIVER_DLQ_TOPIC",
        "DFE_RECEIVER_DLQ_MODE",
    },
    "dfe-transform-vrl": {
        "DFE_TRANSFORM_SOURCE_BROKERS",
        "DFE_TRANSFORM_SINK_BROKERS",
        "DFE_TRANSFORM_SOURCE_GROUP_ID",
        "DFE_TRANSFORM_SOURCE_SASL_USERNAME",
        "DFE_TRANSFORM_SOURCE_SASL_PASSWORD",
    },
    "dfe-transform-vector": {
        "DFE_TRANSFORM_SOURCE_BROKERS",
        "DFE_TRANSFORM_SINK_BROKERS",
        "DFE_TRANSFORM_SOURCE_SASL_USERNAME",
        "DFE_TRANSFORM_SOURCE_SASL_PASSWORD",
    },
}

# Names that were rendered once, read by nothing, and must not come back. A
# regression here is invisible in a cluster, so it is asserted rather than left
# to review.
RETIRED: dict[str, set[str]] = {
    "dfe-archiver": {"KAFKA_BOOTSTRAP_SERVERS"},
    "dfe-fetcher": {"KAFKA_BOOTSTRAP_SERVERS"},
    "dfe-loader": {"KAFKA_BOOTSTRAP_SERVERS"},
    "dfe-receiver": {"KAFKA_BOOTSTRAP_SERVERS"},
    "dfe-transform-vrl": {"KAFKA_BOOTSTRAP_SERVERS"},
    "dfe-transform-vector": {"KAFKA_BOOTSTRAP_SERVERS"},
}

# Every chart carrying a Kafka SASL dial, mapped to the --set that turns SASL on.
# The credential cannot ride the config blob (the engine masks it there), so a
# declared secret no template mounts means the app reaches the broker with nothing.
# dfe-transform-elastic gates the block on securityProtocol rather than on the
# secret name, so its gate is opened here instead.
SASL_CHARTS: dict[str, tuple[str, ...]] = {
    "dfe-archiver": (),
    "dfe-fetcher": (),
    "dfe-loader": (),
    "dfe-receiver": (),
    "dfe-transform-elastic": ("kafka.securityProtocol=SASL_PLAINTEXT",),
    "dfe-transform-splack": (),
    "dfe-transform-vector": (),
    "dfe-transform-vrl": (),
    "dfe-transform-wasm": (),
}

_failures = 0


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


def render(chart: str, *sets: str) -> list[dict]:
    cmd = ["helm", "template", chart, str(CHARTS / chart)]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def pod_specs(docs: list[dict]) -> list[dict]:
    """Every pod template in the render -- Deployments and Jobs alike."""
    specs = []
    for d in docs:
        if d.get("kind") in {"Deployment", "StatefulSet", "Job"}:
            specs.append(d["spec"]["template"]["spec"])
    return specs


def env_names(docs: list[dict]) -> set[str]:
    names = set()
    for spec in pod_specs(docs):
        for container in spec.get("containers", []) + spec.get("initContainers", []):
            for e in container.get("env", []):
                names.add(e["name"])
    return names


def secret_refs(docs: list[dict]) -> set[str]:
    """Secret names reached through a secretKeyRef in any container env."""
    names = set()
    for spec in pod_specs(docs):
        for container in spec.get("containers", []) + spec.get("initContainers", []):
            for e in container.get("env", []):
                ref = e.get("valueFrom", {}).get("secretKeyRef")
                if ref:
                    names.add(ref["name"])
    return names


def values_of(chart: str) -> dict:
    return yaml.safe_load((CHARTS / chart / "values.yaml").read_text(encoding="utf-8")) or {}


def test_required_names_render() -> None:
    """The names the app reads must all appear, spelled exactly."""
    for chart, required in REQUIRED.items():
        rendered = env_names(render(chart))
        missing = required - rendered
        expect(f"{chart} renders every env name its app reads", missing == set(),
               f"missing {sorted(missing)}")


def test_retired_names_stay_gone() -> None:
    """A name no app reads must not reappear -- it fails silently in a cluster."""
    for chart, retired in RETIRED.items():
        rendered = env_names(render(chart))
        back = retired & rendered
        expect(f"{chart} renders no retired env name", back == set(), f"got {sorted(back)}")


def test_declared_sasl_secret_is_mounted() -> None:
    """A declared kafka.saslSecretName that no template references mounts nothing.

    The shape found in dfe-archiver and dfe-fetcher: values.yaml named
    dfe-kafka-user, an operator saw it accepted, no credential reached the pod.
    """
    for chart, enable in SASL_CHARTS.items():
        declared = (values_of(chart).get("kafka") or {}).get("saslSecretName")
        if not declared:
            continue
        refs = secret_refs(render(chart, *enable))
        expect(f"{chart} mounts the kafka.saslSecretName it declares", declared in refs,
               f"declared {declared!r}, secretKeyRefs {sorted(refs)}")


def test_sasl_dial_is_honoured_when_cleared() -> None:
    """Clearing the dial must drop the whole block, or the value is decoration.

    Asserted on the env NAMES, not on the secret name: an ungated block renders
    `secretKeyRef.name: ""`, which is absent from the ref set just as a dropped
    block is, so a name-based assertion passes while the block is still there --
    and an empty secretKeyRef name is rejected by the API server at apply time.
    """
    sasl_env = {
        "dfe-archiver": {"KAFKA_SASL_USER", "KAFKA_SASL_PASSWORD", "KAFKA_SASL_MECHANISM"},
        "dfe-fetcher": {
            "DFE_FETCHER_KAFKA_SASL_USER",
            "DFE_FETCHER_KAFKA_SASL_PASSWORD",
            "DFE_FETCHER_KAFKA_SASL_MECHANISM",
        },
    }
    for chart, names in sasl_env.items():
        docs = render(chart, "kafka.saslSecretName=")
        left = names & env_names(docs)
        expect(f"{chart} drops the SASL env when the dial is cleared", left == set(),
               f"still renders {sorted(left)}")
        # An unset name parses as YAML null, not "", so test falsiness not equality.
        empty = [n for n in secret_refs(docs) if not n]
        expect(f"{chart} renders no empty secretKeyRef name", not empty,
               f"a secretKeyRef rendered with name {empty}")


def test_archiver_s3_secret_is_opt_in_and_wired() -> None:
    """Empty mounts nothing (ambient identity); set mounts both keys.

    The default was dfe-archiver-s3, a Secret nothing in this repo creates, while
    no template referenced it. Wiring it unguarded would wedge every archiver pod.
    """
    bare = render("dfe-archiver")
    expect("dfe-archiver mounts no S3 secret by default",
           not any(n.startswith(("S3_ACCESS", "S3_SECRET")) for n in env_names(bare)),
           f"got {sorted(env_names(bare))}")

    wired = render("dfe-archiver", "s3.secretName=acme-s3")
    names = env_names(wired)
    expect("dfe-archiver wires S3 credentials when a secret is named",
           {"S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"} <= names,
           f"got {sorted(names)}")
    expect("dfe-archiver reaches the named S3 secret", "acme-s3" in secret_refs(wired),
           f"got {sorted(secret_refs(wired))}")


def test_no_chart_reads_a_dead_postgresql_dial() -> None:
    """`.Values.postgresql` was set in four files and referenced by no template."""
    for chart in ("dfe-engine",):
        expect(f"{chart} declares no postgresql dial", "postgresql" not in values_of(chart),
               "values.yaml still declares postgresql")


def main() -> int:
    test_required_names_render()
    test_retired_names_stay_gone()
    test_declared_sasl_secret_is_mounted()
    test_sasl_dial_is_honoured_when_cleared()
    test_archiver_s3_secret_is_opt_in_and_wired()
    test_no_chart_reads_a_dead_postgresql_dial()
    print(f"\n{_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
