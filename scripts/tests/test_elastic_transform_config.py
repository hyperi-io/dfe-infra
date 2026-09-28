#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_elastic_transform_config.py
#  Purpose:      Prove the dfe-transform-elastic config file carries the broker
#                list its app requires before any env override applies.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for the elastic transform's config file.

dfe-transform-elastic reads a --config file straight into its struct, where
source.brokers is required, and applies its DFE_TRANSFORM_ELASTIC_* env only
afterwards. The engine's overlay names no brokers, so every instance on the bus
refused to start with `missing field brokers` until the chart supplied them.

    python3 -m pytest scripts/tests/test_elastic_transform_config.py -q
"""

from __future__ import annotations

import subprocess

import yaml

from _charts import chart_dir

CHART = chart_dir("dfe-transform-elastic")

# The shape the engine publishes for an instance: routing, no brokers.
ENGINE_OVERLAY = (
    "config.source.name=filebeat.cisco_ios.default",
    "config.source.group_id=dfe-transform-elastic-e1",
    "config.source.topics[0]=e1_land",
    "config.sink.topic=e1_load",
)


def _config(*sets: str, overlay: tuple[str, ...] = ENGINE_OVERLAY) -> dict:
    cmd = ["helm", "template", "dfe-transform-elastic", str(CHART)]
    for value in (*overlay, *sets):
        cmd += ["--set", value]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
    assert out.returncode == 0, out.stderr
    maps = [
        d for d in yaml.safe_load_all(out.stdout)
        if d and d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-config")
    ]
    assert len(maps) == 1
    return yaml.safe_load(maps[0]["data"]["config.yaml"])


def test_the_bus_brokers_reach_the_file() -> None:
    config = _config("kafka.bootstrapServers=a.example.test:9092\\, b.example.test:9092")
    assert config["source"]["brokers"] == ["a.example.test:9092", "b.example.test:9092"]


def test_the_engines_routing_is_left_as_published() -> None:
    config = _config("kafka.bootstrapServers=a.example.test:9092")
    assert config["source"]["name"] == "filebeat.cisco_ios.default"
    assert config["source"]["topics"] == ["e1_land"]
    assert config["sink"] == {"topic": "e1_load"}


def test_a_broker_list_the_overlay_names_wins() -> None:
    config = _config(
        "kafka.bootstrapServers=a.example.test:9092",
        "config.source.brokers[0]=chosen.example.test:9092",
    )
    assert config["source"]["brokers"] == ["chosen.example.test:9092"]


def test_an_idle_instance_gets_no_source_block() -> None:
    """A source holding only brokers fails the app's parse; no source block idles."""
    config = _config("kafka.bootstrapServers=a.example.test:9092", overlay=())
    assert "source" not in (config or {})


def test_the_direct_transport_gets_no_brokers() -> None:
    config = _config("kafka.mode=disabled", "kafka.bootstrapServers=a.example.test:9092")
    assert "brokers" not in config["source"]
