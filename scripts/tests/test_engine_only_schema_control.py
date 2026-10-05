#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_engine_only_schema_control.py
#  Purpose:      Fail the build when a schema or topic definition comes back to
#                this repo. dfe-engine is the only thing that creates either.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The guard for engine-only schema control.

dfe-engine applies every ClickHouse object and every bootstrap Kafka topic at its
own startup, from the pinned dfe-schemas manifest, and gates `/readyz` on the
result. Nothing in dfe-infra creates, alters or drops a database, table, view,
role or topic any more.

That is a property of the repo, not of a review: a Job, a CR, a shell line or an
OpenTofu resource that creates a table renders green, lints green and passes a
server-side dry-run, because neither the API server, helm nor tofu has an opinion
about DDL in a string. So it is swept for, in the five trees that deploy things.

Three assertions, each the reappearance of something this change deleted:

1. No ClickHouse DDL under helm/, argocd/, bootstrap/, scripts/ or terraform/.
2. No topic-creation step in the same trees, and no chart rendering a KafkaTopic.
3. No schema-apply workload: no dfe-schema chart, no dfe-schema Application in an
   appset, no dfe-schema dependency in the umbrella chart.

    python3 scripts/tests/test_engine_only_schema_control.py

Needs `helm` on PATH. Runs under pytest too, which is how CI reaches it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
APPSETS = REPO_ROOT / "argocd" / "appsets"
STACK_CHART = REPO_ROOT / "helm" / "dfe-stack" / "Chart.yaml"

# The trees that deploy or operate something.
SWEPT = ("helm", "argocd", "bootstrap", "scripts", "terraform")

# Files that never carry a deploy step.
SKIP_SUFFIXES = (".tgz", ".png", ".svg", ".lock")

# Creating or destroying a ClickHouse object. ALTER TABLE is matched only where
# it changes the SCHEMA -- `ALTER TABLE ... DELETE` is ClickHouse's mutation form
# of a row delete, which the smoke test uses to clean up its own sample rows.
CLICKHOUSE_DDL = re.compile(
    r"\b(?:CREATE|ATTACH)\s+(?:OR\s+REPLACE\s+)?"
    r"(?:TABLE|DATABASE|VIEW|MATERIALIZED\s+VIEW|DICTIONARY|USER|ROLE|ROW\s+POLICY)\b"
    r"|\bDROP\s+(?:TABLE|DATABASE|VIEW|DICTIONARY|USER|ROLE)\b"
    r"|\bALTER\s+TABLE\b[^\n]*\b(?:ADD|DROP|MODIFY|RENAME)\s+(?:COLUMN|TTL)\b",
    re.IGNORECASE,
)

# Creating a Kafka topic, by any of the four tools this repo has ever used. The
# OpenTofu form matches the resource TYPE, so an ACL resource named "topics" is
# not a hit. It names the Kafka providers rather than any type ending in `topic`,
# because aws_sns_topic and google_pubsub_topic are not this bus.
TOPIC_CREATE = re.compile(
    r"kafka-topics\.sh[^\n]*--create"
    r"|rpk\s+topic\s+create"
    r"|kind:\s*KafkaTopic"
    r"|resource\s+\"(?:[A-Za-z0-9_]*kafka_topic|redpanda_topic)\"",
    re.IGNORECASE,
)

# The acceptance probes. Each creates and drops a throwaway object of its own to
# prove the backing service answers, names nothing DFE declares, and runs against
# a live cluster rather than as a deploy step.
ALLOWED_DDL = {
    "scripts/deploy_matrix.py",
    "scripts/tests/test_engine_only_schema_control.py",
}
ALLOWED_TOPIC_CREATE = {
    "scripts/deploy_matrix.py",
    "scripts/tests/test_engine_only_schema_control.py",
    # Asserts the absence of the topic half, so it quotes the flags it forbids.
    "scripts/tests/test_msk_bootstrap.py",
    # Provisions a managed cloud broker before DFE exists on it, the same shape as
    # the MSK ACL job that stays. Whether the path should go at all is dfe-infra#355.
    "terraform/modules/managed-kafka/",
}

# Every kafka.mode the chart renders, so a topic cannot come back on one tier.
KAFKA_MODES = ("disabled", "single", "cluster", "external")


def swept_files() -> list[Path]:
    out = []
    for tree in SWEPT:
        for path in sorted((REPO_ROOT / tree).rglob("*")):
            if path.is_file() and path.suffix not in SKIP_SUFFIXES:
                out.append(path)
    return out


def allowed_path(rel: str, allowed: set[str]) -> bool:
    """Whether the allow-list covers this path. A trailing slash names a directory."""
    return rel in allowed or any(
        entry.endswith("/") and rel.startswith(entry) for entry in allowed
    )


def offenders(pattern: re.Pattern[str], allowed: set[str]) -> list[str]:
    """Every `path:line` in the swept trees matching the pattern, minus the allow-list."""
    hits = []
    for path in swept_files():
        rel = str(path.relative_to(REPO_ROOT))
        if allowed_path(rel, allowed):
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(body.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{rel}:{number}: {line.strip()[:120]}")
    return hits


def render(chart: Path, *sets: str) -> list[dict]:
    cmd = ["helm", "template", chart.name, str(chart)]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart.name} {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


# --- 1: no ClickHouse DDL ------------------------------------------------------


def test_no_clickhouse_ddl_in_the_deploy_trees() -> None:
    hits = offenders(CLICKHOUSE_DDL, ALLOWED_DDL)
    expect(
        "no CREATE/DROP/ALTER of a ClickHouse object under "
        + ", ".join(f"{t}/" for t in SWEPT),
        hits == [],
        "\n  " + "\n  ".join(hits),
    )


# --- 2: no topic creation ------------------------------------------------------


def test_no_topic_creation_in_the_deploy_trees() -> None:
    hits = offenders(TOPIC_CREATE, ALLOWED_TOPIC_CREATE)
    expect(
        "nothing creates a Kafka topic under " + ", ".join(f"{t}/" for t in SWEPT),
        hits == [],
        "\n  " + "\n  ".join(hits),
    )


def test_the_kafka_chart_renders_no_topic_on_any_mode() -> None:
    """The text sweep cannot see a Job that a mode this repo ships would render."""
    for mode in KAFKA_MODES:
        docs = render(CHARTS / "kafka", f"kafka.mode={mode}", "appNamespace=dfe-local")
        crs = [d["metadata"]["name"] for d in docs if d.get("kind") == "KafkaTopic"]
        expect(f"kafka.mode={mode} renders no KafkaTopic", crs == [], f"got {crs}")
        creators = [
            d["metadata"]["name"]
            for d in docs
            if d.get("kind") == "Job" and "kafka-topics.sh" in yaml.safe_dump(d)
        ]
        expect(f"kafka.mode={mode} renders no topic-creating Job", creators == [], f"got {creators}")


# --- 3: no schema-apply workload ----------------------------------------------


def test_the_schema_chart_is_gone() -> None:
    expect(
        "helm/charts/dfe-schema does not exist",
        not (CHARTS / "dfe-schema").exists(),
        "the chart is back",
    )


def test_no_appset_deploys_a_schema_application() -> None:
    for path in sorted(APPSETS.glob("*.yaml")):
        body = path.read_text(encoding="utf-8")
        expect(
            f"{path.name} names no dfe-schema Application",
            not re.search(r"dfe-schema\b(?!s)", body),
            "still generated",
        )


def test_the_umbrella_chart_has_no_schema_dependency() -> None:
    chart = yaml.safe_load(STACK_CHART.read_text(encoding="utf-8"))
    names = [d["name"] for d in chart.get("dependencies", [])]
    expect("dfe-stack declares no dfe-schema dependency", "dfe-schema" not in names, f"got {names}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
