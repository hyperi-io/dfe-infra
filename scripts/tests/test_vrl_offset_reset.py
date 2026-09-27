#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_vrl_offset_reset.py
#  Purpose:      Prove a new vrl source's consumer starts a partition with no
#                committed offset at the log start, not the end.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A partition with no committed offset starts at the log start.

dfe-transform-vrl defaults `source.auto_offset_reset` to `latest`
(src/config/loader.rs), and the value goes straight to librdkafka's
`auto.offset.reset` through scalo's Kafka transport (src/kafka/mod.rs). Each
source's consumer group is born with the source, and KEDA's CPU trigger can
scale the new deployment on its own startup spike, so a partition changes hands
before its first commit and the new owner starts at the log end. Every record
the partition already held is skipped for good, which breaks at-least-once. The
chart renders `earliest` unless the overlay says otherwise.

    python3 scripts/tests/test_vrl_offset_reset.py

Needs `helm` on PATH.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "dfe-transform-vrl"
VALUES = REPO_ROOT / "argocd" / "values"

# The bus profiles, where a Kafka consumer group exists at all.
BUS_PROFILES = ("single", "scale")


def app_config(*args: str) -> dict:
    """The app's config.yaml as the chart renders it."""
    cmd = ["helm", "template", CHART.name, str(CHART), *args]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {args}:\n{out.stderr}")
    doc = next(
        d for d in yaml.safe_load_all(out.stdout)
        if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-config")
    )
    return yaml.safe_load(doc["data"]["config.yaml"]) or {}


def test_a_new_group_starts_at_the_log_start() -> None:
    source = app_config()["source"]
    expect("the bus default is earliest", source.get("auto_offset_reset") == "earliest",
           f"got {source!r}")


def test_every_bus_profile_keeps_it() -> None:
    for profile in BUS_PROFILES:
        source = app_config(
            "-f", str(VALUES / "common.yaml"),
            "-f", str(VALUES / f"profile-{profile}.yaml"),
        )["source"]
        expect(f"{profile} renders earliest", source.get("auto_offset_reset") == "earliest",
               f"got {source!r}")


def test_the_overlay_wins() -> None:
    """The engine's per-instance overlay is the more specific statement."""
    source = app_config("--set", "config.source.auto_offset_reset=latest")["source"]
    expect("an overlay latest survives", source.get("auto_offset_reset") == "latest",
           f"got {source!r}")


def test_the_overlay_keeps_its_other_source_keys() -> None:
    source = app_config("--set", "config.source.group_id=kept")["source"]
    expect("the overlay's own key survives", source.get("group_id") == "kept", f"got {source!r}")
    expect("beside the default", source.get("auto_offset_reset") == "earliest", f"got {source!r}")


def test_empty_leaves_the_app_default() -> None:
    source = app_config("--set", "kafka.autoOffsetReset=")["source"]
    expect("an empty dial renders no key", "auto_offset_reset" not in source, f"got {source!r}")


def test_direct_carries_no_consumer_setting() -> None:
    """On direct the source is a push listener, so there is no group to reset."""
    source = app_config("--set", "kafka.mode=disabled")["source"]
    expect("direct renders no offset reset", "auto_offset_reset" not in source, f"got {source!r}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
