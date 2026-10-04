#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_otel_hostfs.py
#  Purpose:      Prove the collector daemonset's host_metrics reads the node's
#                filesystem through a read-only /hostfs, not the container's.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""host_metrics reports the node only when root_path and the mount agree.

Without root_path the filesystem and disk scrapers read the collector
container's own overlay, and every pod logs a warning saying so (#232). The fix
is two halves in two templates -- root_path in the config, the node root
mounted at that path in the daemonset -- so both are rendered and held equal,
and the mount is held read-only.

    python3 scripts/tests/test_otel_hostfs.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "otel-collector"


def rendered() -> list[dict]:
    cmd = ["helm", "template", "otel", str(CHART)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for otel-collector:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def daemonset_pod(docs: list[dict]) -> dict:
    for doc in docs:
        if doc.get("kind") == "DaemonSet":
            return doc["spec"]["template"]["spec"]
    raise SystemExit("no DaemonSet rendered")


def daemonset_config(docs: list[dict]) -> dict:
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and "daemonset-config.yaml" in doc.get("data", {}):
            return yaml.safe_load(doc["data"]["daemonset-config.yaml"])
    raise SystemExit("no daemonset config rendered")


def host_metrics(docs: list[dict]) -> dict:
    return daemonset_config(docs)["receivers"].get("host_metrics", {})


def test_root_path_is_where_the_node_root_is_mounted() -> None:
    docs = rendered()
    root_path = host_metrics(docs).get("root_path")
    pod = daemonset_pod(docs)
    sources = {v["name"]: v.get("hostPath", {}).get("path") for v in pod["volumes"]}
    node_root = {
        m["mountPath"]: m
        for m in pod["containers"][0]["volumeMounts"]
        if sources.get(m["name"]) == "/"
    }
    expect("host_metrics sets root_path", bool(root_path), repr(root_path))
    expect(
        "the node root is mounted at root_path",
        root_path in node_root,
        f"{root_path!r} vs {sorted(node_root)}",
    )
    mount = node_root.get(root_path, {})
    expect("the node root is mounted read-only", mount.get("readOnly") is True, repr(mount))


def test_the_filesystem_scraper_skips_container_overlays() -> None:
    fs = host_metrics(rendered()).get("scrapers", {}).get("filesystem") or {}
    fs_types = fs.get("exclude_fs_types", {}).get("fs_types", [])
    expect("overlay mounts are excluded", "overlay" in fs_types, repr(fs_types))


def test_the_deprecated_receiver_name_is_gone() -> None:
    """`hostmetrics` is the receiver's deprecated type name; `host_metrics` replaced it."""
    config = daemonset_config(rendered())
    receivers = config["service"]["pipelines"]["metrics"]["receivers"]
    expect("hostmetrics is not configured", "hostmetrics" not in config["receivers"], "")
    expect("the metrics pipeline reads host_metrics", "host_metrics" in receivers, repr(receivers))


def main() -> int:
    with standalone():
        test_root_path_is_where_the_node_root_is_mounted()
        test_the_filesystem_scraper_skips_container_overlays()
        test_the_deprecated_receiver_name_is_gone()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
