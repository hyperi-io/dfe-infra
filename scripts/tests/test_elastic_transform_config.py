#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_elastic_transform_config.py
#  Purpose:      Prove the dfe-transform-elastic config file parses before any env
#                override applies, and that the broker list reaches the app.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for the elastic transform's config file and broker list.

dfe-transform-elastic reads a --config file straight into its struct, where a
named source block must carry every required key, and applies its
DFE_TRANSFORM_ELASTIC_* env only afterwards (src/config/loader.rs:182). The
engine's overlay names no brokers, so every instance on the bus refused to start
with `missing field brokers` until the deployment supplied the key.

apps.yaml declares config.source.brokers deployment-owned through
DFE_TRANSFORM_ELASTIC_SOURCE_BROKERS, so the bus profiles hand the app the
deployment's broker list in that env, over whatever the file says.

    python3 -m pytest scripts/tests/test_elastic_transform_config.py -q

The defaults are read from the goldens under fixtures/thin-renders; the overlays
render live through the scalo-service library (_weave.library).
"""

from pathlib import Path

import yaml

from _gate import CLOUDS, effective_env, golden, object_id
from _weave import render_app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ELASTIC = "dfe-transform-elastic"
BROKERS_ENV = "DFE_TRANSFORM_ELASTIC_SOURCE_BROKERS"
BUS = ("single", "local")
DIRECT_PROFILES = ("slim", "mesh")

# The shape the engine publishes for an instance: routing, no brokers.
ENGINE_OVERLAY = {
    "config": {
        "source": {
            "name": "filebeat.cisco_ios.default",
            "group_id": "dfe-transform-elastic-e1",
            "topics": ["e1_land"],
        },
        "sink": {"topic": "e1_load"},
    }
}

# Every key a named source block must carry for the app's parse, at its idle value.
IDLE_SOURCE = {"name": "", "topics": [], "group_id": "", "brokers": []}


def _render(overlay: dict | None = None, bootstrap: str | None = None) -> list[dict]:
    values = dict(overlay or {})
    if bootstrap is not None:
        values["kafka"] = {"bootstrapServers": bootstrap}
    return render_app(ELASTIC, *BUS, overlay=values)


def _config(docs: list[dict]) -> dict:
    configmap = next(d for d in docs if object_id(d) == f"ConfigMap/{ELASTIC}-config")
    return yaml.safe_load(configmap["data"]["config.yaml"]) or {}


def _env(docs: list[dict]) -> dict[str, object]:
    return effective_env(docs, f"Deployment/{ELASTIC}")


def test_the_bus_brokers_reach_the_app() -> None:
    docs = _render(ENGINE_OVERLAY, bootstrap="a.example.test:9092,b.example.test:9092")
    assert _env(docs)[BROKERS_ENV] == "a.example.test:9092,b.example.test:9092"
    assert "brokers" in _config(docs)["source"]


def test_the_engines_routing_is_left_as_published() -> None:
    docs = _render(ENGINE_OVERLAY, bootstrap="a.example.test:9092")
    config = _config(docs)
    assert config["source"]["name"] == "filebeat.cisco_ios.default"
    assert config["source"]["topics"] == ["e1_land"]
    assert config["sink"] == {"topic": "e1_load"}
    shadowing = sorted(
        name
        for name in _env(docs)
        if name.startswith(("DFE_TRANSFORM_ELASTIC_SOURCE_", "DFE_TRANSFORM_ELASTIC_SINK_"))
        and name != BROKERS_ENV
    )
    assert shadowing == []


def test_the_deployment_owns_the_source_brokers() -> None:
    owned = yaml.safe_load((REPO_ROOT / "apps.yaml").read_text(encoding="utf-8"))["apps"][
        ELASTIC
    ]["deployment_owned"]
    assert owned["config.source.brokers"] == BROKERS_ENV
    overlay = {"config": {"source": {"brokers": ["chosen.example.test:9092"]}}}
    docs = _render(overlay, bootstrap="a.example.test:9092")
    assert _env(docs)[BROKERS_ENV] == "a.example.test:9092"


def test_an_idle_instance_gets_a_complete_idle_source() -> None:
    """A source block missing a required key fails the app's parse; an empty one idles."""
    for profile in ("single", "scale"):
        for cloud in CLOUDS:
            assert _config(golden(ELASTIC, profile, cloud))["source"] == IDLE_SOURCE


def test_the_direct_transport_gets_no_brokers() -> None:
    for profile in DIRECT_PROFILES:
        for cloud in CLOUDS:
            docs = golden(ELASTIC, profile, cloud)
            assert "brokers" not in (_config(docs).get("source") or {})
            env = _env(docs)
            assert BROKERS_ENV not in env
            assert "KAFKA_BOOTSTRAP_SERVERS" not in env
