#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_enrichment_tables.py
#  Purpose:      Prove the tables dfe-transform-vrl mounts are REGISTERED at the
#                path they land on, so the VRL program compiles, and that
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
filebeat pipeline hits on a deploy. The engine writes each table's entry beside
its file, `{name, path}` under the file set's `entries_path`, with the path under
the set's `mount_path` (dfe-engine src/dfe_engine/appmgmt/files.py). Both come
from apps.yaml, so the mount path there has to be the one the thin chart mounts
the set at, from argocd/values/apps/dfe-transform-vrl/values.yaml.

The overlays below carry what the engine writes. Defaults are read from the
goldens under fixtures/thin-renders; the overlays render live through the
scalo-service library (_weave.library).

    python3 scripts/tests/test_enrichment_tables.py

Needs `helm` on PATH.
"""

import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary
from _gate import CLOUDS, configmap_files, golden, object_id
from _weave import render_app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VRL = "dfe-transform-vrl"
VECTOR = "dfe-transform-vector"
DIAL_CELL = ("single", "local")

# The path the bundled filebeat transform file names for its table
# (dfe-transform-vector pipelines/filebeat/filebeat.yaml `enrichment_tables`).
BUNDLED_VECTOR_TIMEZONES = "/etc/dfe-transform-vector/data/timezones.csv"

TABLE_FILE = {"name": "timezones.csv", "content": "x"}


def file_set(app: str, name: str) -> dict:
    """One file set an app's apps.yaml entry declares."""
    manifest = yaml.safe_load((REPO_ROOT / "apps.yaml").read_text(encoding="utf-8"))
    return next(f for f in manifest["apps"][app]["files"] if f["name"] == name)


VRL_MOUNT = file_set(VRL, "enrichment")["mount_path"]

# One mounted table, the shape dfe-engine writes into the per-instance overlay.
MOUNTED = {
    "fileSets": {"enrichment": {"files": [TABLE_FILE]}},
    "config": {"enrichment_tables": [{"name": "timezones", "path": f"{VRL_MOUNT}/timezones.csv"}]},
}

# The same table declared with its key column, as an engine entry the operator edited.
DECLARED = {
    "fileSets": {"enrichment": {"files": [TABLE_FILE]}},
    "config": {
        "enrichment_tables": [
            {
                "name": "timezones",
                "path": f"{VRL_MOUNT}/timezones.csv",
                "key_columns": ["abbreviation"],
            }
        ]
    },
}

VECTOR_MOUNTED = {"fileSets": {"enrichment": {"files": [TABLE_FILE]}}}


def config(docs: list[dict], app: str) -> dict:
    doc = next(d for d in docs if object_id(d) == f"ConfigMap/{app}-config")
    return yaml.safe_load(configmap_files(doc)["config.yaml"]) or {}


def mounts(docs: list[dict], app: str) -> dict[str, dict]:
    """The app container's volume mounts, keyed by volume name."""
    deployment = next(d for d in docs if object_id(d) == f"Deployment/{app}")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {m["name"]: m for m in container["volumeMounts"]}


def test_a_mounted_table_is_registered() -> None:
    entries = file_set(VRL, "enrichment").get("entries_path")
    expect("the catalogue has the engine register each vrl table in its config",
           entries == "config.enrichment_tables", f"got {entries!r}")
    docs = render_app(VRL, *DIAL_CELL, overlay=MOUNTED)
    expect("the set mounts where the catalogue tells the engine it lands",
           mounts(docs, VRL)["fileset-enrichment"]["mountPath"] == VRL_MOUNT,
           f"got {mounts(docs, VRL)['fileset-enrichment']!r}, catalogue {VRL_MOUNT}")
    tables = config(docs, VRL).get("enrichment_tables")
    expect("one mounted table renders one entry", tables == [
        {"name": "timezones", "path": f"{VRL_MOUNT}/timezones.csv"},
    ], f"got {tables!r}")


def test_the_registered_path_is_where_the_file_lands() -> None:
    """A path the mount does not serve fails to compile the same way."""
    docs = render_app(VRL, *DIAL_CELL, overlay=MOUNTED)
    entry = config(docs, VRL)["enrichment_tables"][0]
    data = configmap_files(next(d for d in docs if object_id(d) == f"ConfigMap/{VRL}-enrichment"))
    mount = mounts(docs, VRL)["fileset-enrichment"]
    expect("the entry points into the mount",
           entry["path"] == f"{mount['mountPath']}/timezones.csv",
           f"entry {entry['path']} vs mount {mount['mountPath']}")
    expect("and the file is the ConfigMap key served there", list(data) == ["timezones.csv"],
           f"got {list(data)}")


def test_no_tables_adds_no_key() -> None:
    """An instance with nothing mounted keeps the engine's blob verbatim."""
    for profile in ("slim", "single", "scale", "mesh"):
        for cloud in CLOUDS:
            cfg = config(golden(VRL, profile, cloud), VRL)
            expect(f"no mounted table adds no enrichment_tables key [{profile}/{cloud}]",
                   "enrichment_tables" not in cfg, f"got {cfg.get('enrichment_tables')!r}")


def test_a_declared_table_is_left_alone() -> None:
    """The engine's own entry wins -- a derived duplicate would drop key_columns."""
    tables = config(render_app(VRL, *DIAL_CELL, overlay=DECLARED), VRL).get("enrichment_tables")
    expect("a declared table is not derived a second time", tables == [
        {
            "name": "timezones",
            "path": f"{VRL_MOUNT}/timezones.csv",
            "key_columns": ["abbreviation"],
        },
    ], f"got {tables!r}")


def test_the_vector_chart_registers_nothing() -> None:
    """dfe-transform-vector has no enrichment field in its config schema at all
    (dfe-transform-vector docs/config-schema.yaml), so an entry there is inert."""
    entries = file_set(VECTOR, "enrichment").get("entries_path")
    expect("the catalogue names no entries for the vector set", not entries, f"got {entries!r}")
    cfg = config(render_app(VECTOR, *DIAL_CELL, overlay=VECTOR_MOUNTED), VECTOR)
    expect("dfe-transform-vector renders no enrichment_tables key",
           "enrichment_tables" not in cfg, f"got {cfg.get('enrichment_tables')!r}")


def vector_mounts() -> dict[str, dict]:
    return mounts(render_app(VECTOR, *DIAL_CELL, overlay=VECTOR_MOUNTED), VECTOR)


def test_the_vector_tables_land_where_the_bundled_pipeline_reads_them() -> None:
    """A transform file names its table by absolute path, so Vector exits 78 on
    `No such file or directory` when the file lands anywhere else."""
    mount = vector_mounts()["fileset-enrichment"]["mountPath"]
    expect("the vector enrichment set mounts at the pipeline's data dir",
           f"{mount}/timezones.csv" == BUNDLED_VECTOR_TIMEZONES,
           f"mounted at {mount}, the pipeline reads {BUNDLED_VECTOR_TIMEZONES}")
    catalogued = file_set(VECTOR, "enrichment")["mount_path"]
    expect("and where the catalogue says it lands", mount == catalogued,
           f"mounted at {mount}, catalogue {catalogued}")


def test_no_vector_volume_mounts_inside_another() -> None:
    """A volume mounted under another directory volume shadows it by mount order."""
    found = [m for m in vector_mounts().values() if "subPath" not in m]
    nested = [
        (outer["mountPath"], inner["mountPath"])
        for outer in found
        for inner in found
        if inner["mountPath"].startswith(outer["mountPath"].rstrip("/") + "/")
    ]
    expect("no vector volume sits inside another directory volume", not nested, f"got {nested}")


def test_the_vector_config_file_still_reaches_the_cmd_path() -> None:
    """The image CMD reads /etc/dfe-transform-vector/config.yaml."""
    config_mount = vector_mounts()["config"]
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
