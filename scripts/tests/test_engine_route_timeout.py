#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_engine_route_timeout.py
#  Purpose:      Prove the engine API routes carry a request budget longer than
#                Envoy's 15s default, on the internal route and the public one.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for the engine route's request timeout.

A source create or deploy commits and pushes the deploy repo and syncs routing
before it answers. On a one-node cluster that ran past Envoy Gateway's 15s route
default: the gateway answered 504 `upstream request timeout` while the engine
went on to save the source, so the console reported a failure for a write that
landed.

    python3 -m pytest scripts/tests/test_engine_route_timeout.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from _charts import chart_dir

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATEWAY = chart_dir("envoy-gateway-config")
COMMON = REPO_ROOT / "argocd" / "values" / "common.yaml"


def _render(*sets: str) -> list[dict]:
    cmd = ["helm", "template", "envoy-gateway-config", str(GATEWAY), "-f", str(COMMON)]
    for value in ("domain=dfe.example.test", *sets):
        cmd += ["--set", value]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
    assert out.returncode == 0, out.stderr
    return [doc for doc in yaml.safe_load_all(out.stdout) if doc]


def _route(docs: list[dict], name: str) -> dict:
    routes = [d for d in docs if d.get("kind") == "HTTPRoute" and d["metadata"]["name"] == name]
    assert len(routes) == 1, [d["metadata"]["name"] for d in docs if d.get("kind") == "HTTPRoute"]
    return routes[0]


def test_the_engine_route_outlasts_the_proxy_default() -> None:
    rules = _route(_render(), "dfe-engine")["spec"]["rules"]
    assert [rule.get("timeouts") for rule in rules] == [{"request": "120s"}]


def test_the_public_engine_route_carries_the_same_budget() -> None:
    public = _route(_render("ui.public_domain=public.example.test"), "dfe-engine-public")
    assert [rule.get("timeouts") for rule in public["spec"]["rules"]] == [{"request": "120s"}]


def test_an_empty_budget_leaves_the_proxy_default() -> None:
    rules = _route(_render("routes.dfeEngine.requestTimeout="), "dfe-engine")["spec"]["rules"]
    assert all("timeouts" not in rule for rule in rules)


def test_no_other_route_gains_a_budget() -> None:
    for doc in _render():
        if doc.get("kind") != "HTTPRoute" or doc["metadata"]["name"].startswith("dfe-engine"):
            continue
        assert all("timeouts" not in rule for rule in doc["spec"]["rules"]), doc["metadata"]["name"]
