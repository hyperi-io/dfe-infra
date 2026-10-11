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
bus profiles' integration values (argocd/values/apps/dfe-transform-vrl) write
`earliest` into the config file unless the overlay says otherwise.

The profiles are read from the goldens under fixtures/thin-renders; the overlays
render live through the scalo-service library (_weave.library).

    python3 scripts/tests/test_vrl_offset_reset.py

Needs `helm` on PATH.
"""

import sys

import pytest
import yaml

from _expect import expect, standalone, summary
from _gate import CLOUDS, golden, object_id
from _weave import render_app

APP = "dfe-transform-vrl"
EARLIEST = "earliest"
LATEST = "latest"

# The bus profiles, where a Kafka consumer group exists at all, and the direct ones.
BUS_PROFILES = ("single", "scale")
DIRECT_PROFILES = ("slim", "mesh")
DIAL_CELL = ("single", "local")


def app_source(docs: list[dict]) -> dict:
    """The source block of the app's config.yaml as the thin chart renders it."""
    doc = next(d for d in docs if object_id(d) == f"ConfigMap/{APP}-config")
    return (yaml.safe_load(doc["data"]["config.yaml"]) or {}).get("source") or {}


def overlay_source(source: dict) -> dict:
    return app_source(render_app(APP, *DIAL_CELL, overlay={"config": {"source": source}}))


def test_a_new_group_starts_at_the_log_start() -> None:
    source = app_source(golden(APP, *DIAL_CELL))
    expect(f"the bus default is {EARLIEST}", source.get("auto_offset_reset") == EARLIEST,
           f"got {source!r}")


def test_every_bus_profile_keeps_it() -> None:
    for profile in BUS_PROFILES:
        for cloud in CLOUDS:
            source = app_source(golden(APP, profile, cloud))
            expect(f"{profile}/{cloud} renders {EARLIEST}",
                   source.get("auto_offset_reset") == EARLIEST, f"got {source!r}")


def test_the_overlay_wins() -> None:
    """The engine's per-instance overlay is the more specific statement."""
    source = overlay_source({"auto_offset_reset": LATEST})
    expect(f"an overlay {LATEST} survives", source.get("auto_offset_reset") == LATEST,
           f"got {source!r}")


def test_the_overlay_keeps_its_other_source_keys() -> None:
    source = overlay_source({"group_id": "kept"})
    expect("the overlay's own key survives", source.get("group_id") == "kept", f"got {source!r}")
    expect("beside the default", source.get("auto_offset_reset") == EARLIEST, f"got {source!r}")


@pytest.mark.xfail(
    strict=True,
    reason="a null config.source.auto_offset_reset renders null into the file, not no key",
)
def test_empty_leaves_the_app_default() -> None:
    source = overlay_source({"auto_offset_reset": None})
    expect("a cleared offset reset renders no key", "auto_offset_reset" not in source,
           f"got {source!r}")


def test_direct_carries_no_consumer_setting() -> None:
    """On direct the source is a push listener, so there is no group to reset."""
    for profile in DIRECT_PROFILES:
        for cloud in CLOUDS:
            source = app_source(golden(APP, profile, cloud))
            expect(f"{profile}/{cloud} renders no offset reset",
                   "auto_offset_reset" not in source, f"got {source!r}")


def main() -> int:
    with standalone():
        test_a_new_group_starts_at_the_log_start()
        test_every_bus_profile_keeps_it()
        test_the_overlay_wins()
        test_the_overlay_keeps_its_other_source_keys()
        test_direct_carries_no_consumer_setting()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
