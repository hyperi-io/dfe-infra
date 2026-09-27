#  Project:      dfe-infra
#  File:         scripts/tests/test_engine_route_timeout.py
#  Purpose:      Prove the gateway waits long enough for an engine write, on the
#                engine route and on its public twin.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""How long the gateway waits for the engine.

An engine write commits to the deploy repo and reconciles the apps inside the
request. A source create on a single-tier deploy took 18s -- three pushes -- and
the gateway's own 15s default answered 504 "upstream request timeout" after the
source had already been saved, so the console reported a failure for a write
that landed.

    python3 -m pytest scripts/tests/test_engine_route_timeout.py -q

Needs `helm` on PATH.
"""

import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATEWAY = REPO_ROOT / "helm" / "edge" / "gateway"
COMMON = REPO_ROOT / "argocd" / "values" / "common.yaml"

# The console going public takes the engine API with it, on the console's name.
PUBLIC = ("ui.public_domain=public.example.com", "ui.public.dfe_ui=true")


def _routes(*sets: str) -> dict[str, dict]:
    cmd = ["helm", "template", "gateway", str(GATEWAY), "-f", str(COMMON),
           "--set", "domain=dfe.example.com"]
    for value in sets:
        cmd += ["--set", value]
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True)
    return {
        doc["metadata"]["name"]: doc
        for doc in yaml.safe_load_all(done.stdout)
        if doc and doc.get("kind") == "HTTPRoute"
    }


def _timeouts(route: dict) -> list[dict | None]:
    return [rule.get("timeouts") for rule in route["spec"]["rules"]]


def test_the_engine_route_outlasts_a_write() -> None:
    assert _timeouts(_routes()["dfe-engine"]) == [{"request": "60s"}]


def test_the_public_twin_carries_the_same_budget() -> None:
    assert _timeouts(_routes(*PUBLIC)["dfe-engine-public"]) == [{"request": "60s"}]


def test_an_empty_budget_leaves_the_gateway_default() -> None:
    routes = _routes("routes.dfeEngine.requestTimeout=", *PUBLIC)
    assert _timeouts(routes["dfe-engine"]) == [None]
    assert _timeouts(routes["dfe-engine-public"]) == [None]


def test_no_other_route_changes() -> None:
    for name, route in _routes(*PUBLIC).items():
        if name.startswith("dfe-engine"):
            continue
        assert all(t is None for t in _timeouts(route)), name
