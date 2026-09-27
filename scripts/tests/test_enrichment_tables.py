#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_enrichment_tables.py
#  Purpose:      Prove the tables dfe-transform-vrl mounts are also REGISTERED in
#                the config it renders, so the VRL program compiles, and that
#                dfe-transform-vector's land at the path its transform files name.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A mounted enrichment table has to be named in the config as well.

Delivering the file is only half of it. A VRL program calling
`find_enrichment_table_records!(table: "timezones", ...)` against a table the
config never declares fails to COMPILE -- `error[E111] enrichment tables not
loaded` -- and the app exits 1, which is the CrashLoopBackOff the bundled
filebeat pipeline hits on a deploy. The engine cannot write the entry, because the
mount directory belongs to the chart, so dfe-common.enrichmentTablesConfig
derives it and the ConfigMap appends it.

    python3 scripts/tests/test_enrichment_tables.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
VRL = CHARTS / "dfe-transform-vrl"
VECTOR = CHARTS / "dfe-transform-vector"

# One mounted table, the shape dfe-engine writes into the per-instance overlay.
MOUNTED = (
    "--set", "enrichmentTables[0].name=timezones.csv",
    "--set", "enrichmentTables[0].content=x",
)

# The path the bundled filebeat transform file names for its table
# (dfe-transform-vector pipelines/filebeat/filebeat.yaml `enrichment_tables`).
BUNDLED_VECTOR_TIMEZONES = "/etc/dfe-transform-vector/data/timezones.csv"

# The same table declared by the engine's config blob, key column and all.
DECLARED = (
    "--set", "config.enrichment_tables[0].name=timezones",
    "--set", "config.enrichment_tables[0].path=/etc/dfe-transform-vrl-enrichment/timezones.csv",
    "--set", "config.enrichment_tables[0].key_columns[0]=abbreviation",
)


def render(chart: Path, *args: str) -> list[dict]:
    cmd = ["helm", "template", chart.name, str(chart), *args]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart.name} {args}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def config(chart: Path, *args: str) -> dict:
    doc = next(
        d for d in render(chart, *args)
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-config")
    )
    return yaml.safe_load(doc["data"]["config.yaml"]) or {}


def test_a_mounted_table_is_registered() -> None:
    tables = config(VRL, *MOUNTED).get("enrichment_tables")
    expect("one mounted table renders one entry", tables == [
        {"name": "timezones", "path": "/etc/dfe-transform-vrl-enrichment/timezones.csv"},
    ], f"got {tables!r}")


def test_the_registered_path_is_where_the_file_lands() -> None:
    """A path the mount does not serve fails to compile the same way."""
    docs = render(VRL, *MOUNTED)
    entry = yaml.safe_load(
        next(d for d in docs
             if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-config")
             )["data"]["config.yaml"]
    )["enrichment_tables"][0]
    data = next(
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-enrichment")
    )["data"]
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    mount = next(
        m for m in deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        if m["name"] == "enrichment"
    )
    expect("the entry points into the mount", entry["path"] == f"{mount['mountPath']}/timezones.csv",
           f"entry {entry['path']} vs mount {mount['mountPath']}")
    expect("and the file is the ConfigMap key served there", list(data) == ["timezones.csv"],
           f"got {list(data)}")


def test_no_tables_adds_no_key() -> None:
    """An instance with nothing mounted keeps the engine's blob verbatim."""
    cfg = config(VRL)
    expect("no mounted table adds no enrichment_tables key",
           "enrichment_tables" not in cfg, f"got {cfg.get('enrichment_tables')!r}")


def test_a_declared_table_is_left_alone() -> None:
    """The engine's own entry wins -- a derived duplicate would drop key_columns."""
    tables = config(VRL, *MOUNTED, *DECLARED).get("enrichment_tables")
    expect("a declared table is not derived a second time", tables == [
        {
            "name": "timezones",
            "path": "/etc/dfe-transform-vrl-enrichment/timezones.csv",
            "key_columns": ["abbreviation"],
        },
    ], f"got {tables!r}")


def test_the_vector_chart_registers_nothing() -> None:
    """dfe-transform-vector has no enrichment field in its config schema at all
    (dfe-transform-vector docs/config-schema.yaml), so an entry there is inert."""
    cfg = config(VECTOR, *MOUNTED)
    expect("dfe-transform-vector renders no enrichment_tables key",
           "enrichment_tables" not in cfg, f"got {cfg.get('enrichment_tables')!r}")


def vector_mounts(*args: str) -> dict[str, dict]:
    """The transform container's volume mounts, keyed by volume name."""
    deployment = next(d for d in render(VECTOR, *args) if d.get("kind") == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {m["name"]: m for m in container["volumeMounts"]}


def test_the_vector_tables_land_where_the_bundled_pipeline_reads_them() -> None:
    """A transform file names its table by absolute path, so Vector exits 78 on
    `No such file or directory` when the file lands anywhere else."""
    mount = vector_mounts(*MOUNTED)["enrichment"]["mountPath"]
    expect("the vector enrichment set mounts at the pipeline's data dir",
           f"{mount}/timezones.csv" == BUNDLED_VECTOR_TIMEZONES,
           f"mounted at {mount}, the pipeline reads {BUNDLED_VECTOR_TIMEZONES}")


def test_no_vector_volume_mounts_inside_another() -> None:
    """A volume mounted under another directory volume shadows it by mount order."""
    mounts = [m for m in vector_mounts(*MOUNTED).values() if "subPath" not in m]
    nested = [
        (outer["mountPath"], inner["mountPath"])
        for outer in mounts
        for inner in mounts
        if inner["mountPath"].startswith(outer["mountPath"].rstrip("/") + "/")
    ]
    expect("no vector volume sits inside another directory volume", not nested, f"got {nested}")


def test_the_vector_config_file_still_reaches_the_cmd_path() -> None:
    """The image CMD reads /etc/dfe-transform-vector/config.yaml."""
    config_mount = vector_mounts(*MOUNTED)["config"]
    expect("the config file mounts at the CMD path",
           (config_mount["mountPath"], config_mount.get("subPath"))
           == ("/etc/dfe-transform-vector/config.yaml", "config.yaml"),
           f"got {config_mount}")


def main() -> int:
    with standalone():
        test_a_mounted_table_is_registered()
        test_the_registered_path_is_where_the_file_lands()
        test_no_tables_adds_no_key()
        test_a_declared_table_is_left_alone()
        test_the_vector_chart_registers_nothing()
        test_the_vector_tables_land_where_the_bundled_pipeline_reads_them()
        test_no_vector_volume_mounts_inside_another()
        test_the_vector_config_file_still_reaches_the_cmd_path()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
