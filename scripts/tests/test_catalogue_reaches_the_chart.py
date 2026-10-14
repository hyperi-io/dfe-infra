#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_catalogue_reaches_the_chart.py
#  Purpose:      Prove every overlay path dfe-engine writes from apps.yaml is a key
#                the chart argocd/appsets/layer2-apps.yaml deploys reads.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every overlay path the engine writes from apps.yaml reaches the deployed chart.

    python3 -m pytest scripts/tests/test_catalogue_reaches_the_chart.py -q

The engine reads apps.yaml from the copy the charts mount, at the revision the
appset deploys. A path in it the deployed chart does not read is a write the
engine commits and no pod sees, so each is rendered through the chart the appset
deploys, with a probe value, and the probe looked for in the objects and in the
files their ConfigMaps mount:

- each file set's values_path, holding one probe file
- the config block, which carries every config.-rooted path the engine writes:
  routing blocks, source bindings, table entries, reload settings, variants

links_path is the exception: the engine's own record of where linked content
came from, which no chart reads.

The chart is the appset's own first source, the thin chart, beside dfe-extras.
The render needs the scalo-service library (_weave.library).
"""

import functools
import json

import pytest
import yaml

from _gate import configmap_files
from _weave import REPO_ROOT, render_app

MANIFEST = REPO_ROOT / "apps.yaml"
PROFILE = "single"
CLOUD = "local"
PROBE = "dfe-catalogue-probe"


@functools.cache
def _apps() -> dict[str, dict]:
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    return {name: raw or {} for name, raw in doc["apps"].items()}


def _nested(path: str, value: object) -> dict:
    """``value`` at a dot-path, as the engine's set_path writes it."""
    doc: dict = {}
    node = doc
    *parents, leaf = path.split(".")
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value
    return doc


def _render(service: str, overlay: dict) -> str:
    """The objects the appset's chart renders for an instance overlay, and their files, as text.

    A file under ``binaryData`` is base64 in the object, so its decoded text is added.
    """
    docs = render_app(service, PROFILE, CLOUD, overlay=overlay)
    files = [configmap_files(d) for d in docs if d.get("kind") == "ConfigMap"]
    return json.dumps([*docs, *files])


def _file_set_cases() -> list:
    return [
        pytest.param(service, fs, id=f"{service}-{fs['name']}")
        for service, app in _apps().items()
        for fs in app.get("files") or []
    ]


@pytest.mark.parametrize(("service", "file_set"), _file_set_cases())
def test_a_file_set_the_engine_writes_reaches_the_chart(service: str, file_set: dict) -> None:
    probe = f"{PROBE}-{service}-{file_set['name']}"
    name = f"probe{file_set['suffixes'][0]}"
    overlay = _nested(file_set["values_path"], [{"name": name, "content": f"{probe}\n"}])
    assert probe in _render(service, overlay)


@pytest.mark.parametrize(
    "service", sorted(name for name, app in _apps().items() if app.get("consumes"))
)
def test_the_config_block_the_engine_writes_reaches_the_chart(service: str) -> None:
    probe = f"{PROBE}-{service}-config"
    assert probe in _render(service, {"config": {"dfeCatalogueProbe": probe}})


def _engine_written_paths(app: dict) -> list[str]:
    """Every overlay path apps.yaml tells the engine to write for one app."""
    paths: list[str] = []
    for fs in app.get("files") or []:
        paths += [fs[key] for key in ("values_path", "dir_setting", "entries_path") if fs.get(key)]
    paths += list((app.get("routing") or {}).get("values_paths", {}).values())
    paths += list(app.get("source_binding") or {})
    paths += [app[key] for key in ("reload_setting", "variant_path") if app.get(key)]
    return paths


@pytest.mark.parametrize("service", sorted(_apps()))
def test_every_path_the_engine_writes_is_one_a_probe_covers(service: str) -> None:
    """A path outside both probe shapes would reach the chart unchecked."""
    app = _apps()[service]
    values_paths = {fs["values_path"] for fs in app.get("files") or []}
    uncovered = [
        path
        for path in _engine_written_paths(app)
        if not path.startswith("config.") and path not in values_paths
    ]
    assert uncovered == []
