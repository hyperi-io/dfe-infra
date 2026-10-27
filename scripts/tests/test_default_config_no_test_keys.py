#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_default_config_no_test_keys.py
#  Purpose:      Fail when a default a customer deploys carries a switch, route
#                or name that exists only for the acceptance harness.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests that the default customer config carries no test-only keys.

The acceptance suites open an unauthenticated lumberjack listener, mount the
engine's /api/e2e seed routes and create sources under run-minted names. Each is
switched on for one run and put back. None of it may be a default. Two sets are
scanned:

- every thin-chart render of the default scenario, on slim, single and scale and on
  both clouds, ConfigMap files and env included (the committed goldens, so no helm)
- the values files a deploy layers: every chart's values.yaml and argocd/values

The e2eServer switch also has a chart-level guard (dfe-engine.e2eServer), which
refuses it outside a dev posture. The scan holds the default off in the first place.

    python3 -m pytest scripts/tests/test_default_config_no_test_keys.py -q
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from acceptance.source import steps as source_steps  # noqa: E402

from _charts import CHART_TREES, REPO_ROOT  # noqa: E402
from _gate import CLOUDS, COMPONENTS, DEFAULT, configmap_files, golden, object_id  # noqa: E402

DEFAULT_PROFILES = ("slim", "single", "scale")

TRUTHY = {"true", "1", "yes", "on"}

# A config key is matched on its last dotted segment, so `config.lumberjack.accept_unauthenticated` counts.
UNAUTHENTICATED_KEYS = {"accept_unauthenticated", "acceptunauthenticated"}
E2E_SWITCH_KEYS = {"e2eserver", "e2e_server"}
# The same switches as an environment variable carries them.
E2E_ENV_SUFFIXES = ("ACCEPT_UNAUTHENTICATED", "E2E_SERVER")

E2E_ROUTE = "/api/e2e"
# A run-minted source name (fb1a2b3c4d), or a word that names the harness itself.
HARNESS_NAME = re.compile(
    rf"(?<![A-Za-z0-9])(?:{source_steps.RUN_NAME.pattern}|e2e|src-test|dfe-acceptance)(?![A-Za-z0-9])"
)


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in TRUTHY


def _parsed(text: str) -> object:
    """A ConfigMap file read as YAML (JSON included), or None when it is not."""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def findings(node: object, where: str = "") -> Iterator[str]:
    """Each test-only key, route or name in *node*, with where it was found."""
    if isinstance(node, dict):
        name = node.get("name")
        if isinstance(name, str) and "value" in node:
            upper = name.upper()
            if upper.endswith(E2E_ENV_SUFFIXES) and _truthy(node["value"]):
                yield f"{where}: env {name}={node['value']}"
            if upper == "DFE_ENV" and str(node["value"]).strip().lower() == "test":
                yield f"{where}: env DFE_ENV=test (the e2e posture)"
        for key, value in node.items():
            leaf = str(key).lower().rsplit(".", 1)[-1]
            if leaf in UNAUTHENTICATED_KEYS and _truthy(value):
                yield f"{where}: {key}: {value} (unauthenticated listener)"
            if leaf in E2E_SWITCH_KEYS and _truthy(value):
                yield f"{where}: {key}: {value} (e2e posture)"
            yield from findings(value, f"{where}.{key}" if where else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from findings(value, f"{where}[{index}]")
    elif isinstance(node, str):
        if E2E_ROUTE in node:
            yield f"{where}: {E2E_ROUTE} seed route"
        match = HARNESS_NAME.search(node)
        if match:
            yield f"{where}: harness name {match.group(0)!r}"


def doc_findings(doc: dict) -> list[str]:
    """The findings in one rendered object, its ConfigMap files read as config."""
    where = object_id(doc)
    found = list(findings(doc, where))
    if doc.get("kind") == "ConfigMap":
        for filename, text in configmap_files(doc).items():
            parsed = _parsed(text)
            if isinstance(parsed, (dict, list)):
                found += findings(parsed, f"{where}:{filename}")
            else:
                found += findings(text, f"{where}:{filename}")
    return found


CELLS = [(s, p, c) for s in COMPONENTS for p in DEFAULT_PROFILES for c in CLOUDS]


@pytest.mark.parametrize(("service", "profile", "cloud"), CELLS, ids=[f"{s}-{p}-{c}" for s, p, c in CELLS])
def test_the_default_render_carries_no_test_only_keys(service: str, profile: str, cloud: str) -> None:
    found = [line for doc in golden(service, profile, cloud, DEFAULT) for line in doc_findings(doc)]
    assert not found, "\n".join(found)


def _values_files() -> list[Path]:
    files = [REPO_ROOT / "argocd" / "values" / "common.yaml"]
    files += sorted((REPO_ROOT / "argocd" / "values").rglob("*.yaml"))
    for tree in CHART_TREES:
        files += sorted(tree.glob("**/values.yaml"))
    seen: dict[Path, None] = {}
    for path in files:
        seen[path] = None
    return list(seen)


@pytest.mark.parametrize("path", _values_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_the_values_a_deploy_layers_carry_no_test_only_keys(path: Path) -> None:
    documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8", errors="replace")))
    found = [line for doc in documents for line in findings(doc, path.name)]
    assert not found, "\n".join(found)


@pytest.mark.parametrize(
    ("node", "needle"),
    [
        ({"config": {"lumberjack": {"accept_unauthenticated": True}}}, "accept_unauthenticated"),
        ({"config.lumberjack.accept_unauthenticated": "true"}, "unauthenticated listener"),
        ({"e2eServer": True}, "e2eServer"),
        ({"env": [{"name": "DFE_E2E_SERVER", "value": "true"}]}, "DFE_E2E_SERVER"),
        ({"env": [{"name": "DFE_RECEIVER_LUMBERJACK__ACCEPT_UNAUTHENTICATED", "value": "true"}]}, "ACCEPT_UNAUTHENTICATED"),
        ({"env": [{"name": "DFE_ENV", "value": "test"}]}, "DFE_ENV=test"),
        ({"paths": ["/api/e2e/seed"]}, "seed route"),
        ({"topics": ["dfe.fb1a2b3c4d"]}, "harness name"),
        ({"source": "e2e"}, "harness name"),
        ({"agent": "dfe-acceptance"}, "harness name"),
    ],
)
def test_expected_fail_each_test_only_key_is_found(node: object, needle: str) -> None:
    """The scan is only worth its green if each kind of key turns it red."""
    assert any(needle in line for line in findings(node, "x"))


@pytest.mark.parametrize(
    "node",
    [
        {"e2eServer": False},
        {"accept_unauthenticated": "false"},
        {"env": [{"name": "DFE_ENV", "value": "dev"}]},
        {"checksum": "1d0dae2ec7f24825128c3a83bf29294e420daf9e9d8fc1aad15e3f2d35912822"},
        {"source": "elastic", "topic": "dfe.land", "name": "fbprod"},
    ],
)
def test_an_ordinary_default_is_not_a_finding(node: object) -> None:
    assert list(findings(node, "x")) == []


def test_a_golden_carrying_a_test_only_key_is_caught() -> None:
    """The scan reads ConfigMap files, not only env: lumberjack open inside a mounted config fails it."""
    doc = {
        "kind": "ConfigMap", "metadata": {"name": "dfe-receiver"},
        "data": {"config.yaml": "lumberjack:\n  enabled: true\n  accept_unauthenticated: true\n"},
    }
    assert any("accept_unauthenticated" in line for line in doc_findings(doc))
