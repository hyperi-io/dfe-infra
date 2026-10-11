#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_extra_env.py
#  Purpose:      Prove the overlay's extraEnv block reaches the container, and
#                that a name the deployment derives always overrides it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for the scalo-service extraEnv map across the six app deployments.

An app's settings surface is wider than the dials its deployment declares, so
dfe-engine writes a key nothing models into the overlay's `extraEnv` map. The
scalo-service library renders it before the names it derives itself, and drops
an entry whose name it derives, so one name never appears twice in a container.

Four things are checked:

1. A custom key renders as a container env entry, with its value a string.
2. A custom key sharing a name the library derives is dropped, so the derived
   value is the one the container gets.
3. Every one of the six thin charts declares the map and renders it.
4. An absent or empty map renders the env the deployment renders without it.

Defaults are read from the goldens under fixtures/thin-renders; the overlays render
live through the scalo-service library (_weave.library).

    python3 scripts/tests/test_extra_env.py

Needs `helm` on PATH.
"""

import json
import sys

import pytest

from _expect import expect, standalone, summary
from _gate import effective_env, golden, object_id
from _weave import assembled_chart, render_app

# Every app whose settings surface the engine writes; the same six the engine
# mounts a container contract for.
APPS = (
    "dfe-receiver",
    "dfe-loader",
    "dfe-archiver",
    "dfe-fetcher",
    "dfe-transform-vrl",
    "dfe-transform-vector",
)

DIAL_CELL = ("single", "local")

CUSTOM = {"extraEnv": {"DFE_HOUSE_KEY": "kept", "DFE_HOUSE_PORT": 8123}}

# A name the library derives for dfe-transform-vrl from its contract's kafka group.
DERIVED = "DFE_TRANSFORM_SOURCE_SASL_PASSWORD"
COLLIDING = {"extraEnv": {DERIVED: "custom"}}

# A name argocd/values/apps/dfe-transform-vrl derives into the same map.
INTEGRATION = "DFE_TRANSFORM_SOURCE_BROKERS"


def deployment(docs: list[dict], app: str) -> dict:
    return next(d for d in docs if object_id(d) == f"Deployment/{app}")


def app_env(docs: list[dict], app: str) -> list[dict]:
    """The app container's env list as the pod template carries it."""
    return deployment(docs, app)["spec"]["template"]["spec"]["containers"][0].get("env") or []


def render(app: str, overlay: dict) -> list[dict]:
    return render_app(app, *DIAL_CELL, overlay=overlay)


def test_a_custom_key_becomes_container_env() -> None:
    """The block is the operator's half of the app's settings, so it has to land."""
    env = {e["name"]: e.get("value") for e in app_env(render("dfe-transform-vrl", CUSTOM),
                                                      "dfe-transform-vrl")}
    expect(
        "a custom string key reaches the container",
        env.get("DFE_HOUSE_KEY") == "kept",
        f"{env.get('DFE_HOUSE_KEY')}",
    )
    expect(
        "a number becomes a string, because env carries strings",
        env.get("DFE_HOUSE_PORT") == "8123",
        f"{env.get('DFE_HOUSE_PORT')!r}",
    )


def test_a_colliding_custom_key_never_reaches_the_container() -> None:
    """A custom key sharing a derived name never reaches the container.

    Two entries of one name fail a server-side apply, so the library drops the
    custom one and the Secret reference it derives is what the pod reads.
    """
    entries = [e for e in app_env(render("dfe-transform-vrl", COLLIDING), "dfe-transform-vrl")
               if e["name"] == DERIVED]
    expect("the name renders once", len(entries) == 1, f"got {len(entries)}")
    kept = entries[0] if entries else {}
    expect(
        "and it is the derived Secret reference, not the custom override",
        "secretKeyRef" in (kept.get("valueFrom") or {}) and kept.get("value") != "custom",
        f"got {kept.get('value')!r}",
    )


@pytest.mark.xfail(
    strict=True,
    reason="argocd/values/apps derives names into extraEnv, where an overlay key replaces them",
)
def test_a_custom_key_never_replaces_an_integration_value() -> None:
    docs = render("dfe-transform-vrl", {"extraEnv": {INTEGRATION: "custom"}})
    got = effective_env(docs, "Deployment/dfe-transform-vrl").get(INTEGRATION)
    expect(f"the deployment's {INTEGRATION} survives an overlay key of that name",
           got != "custom", f"got {got!r}")


def test_every_app_chart_carries_the_key() -> None:
    """Six thin charts, one library: a chart missing it drops the operator's block."""
    for app in APPS:
        schema = json.loads((assembled_chart(app) / "values.schema.json").read_text(
            encoding="utf-8"))
        declared = schema.get("properties", {}).get("extraEnv", {})
        expect(
            f"{app} declares extraEnv as a map",
            declared.get("type") == "object",
            f"{declared!r}",
        )
        names = [e["name"] for e in app_env(render(app, CUSTOM), app)]
        expect(f"{app} renders a custom key", "DFE_HOUSE_KEY" in names, f"{names}")


def test_no_custom_keys_renders_the_env_it_always_did() -> None:
    """The block is opt-in: a deployment setting none must be unchanged."""
    for app in APPS:
        default = sorted(effective_env(golden(app, *DIAL_CELL), f"Deployment/{app}"))
        empty = sorted(effective_env(render(app, {"extraEnv": {}}), f"Deployment/{app}"))
        expect(
            f"{app}: an empty block adds nothing",
            default == empty and "DFE_HOUSE_KEY" not in default,
            f"default={len(default)} empty={len(empty)}",
        )


def main() -> int:
    with standalone():
        test_a_custom_key_becomes_container_env()
        test_a_colliding_custom_key_never_reaches_the_container()
        test_every_app_chart_carries_the_key()
        test_no_custom_keys_renders_the_env_it_always_did()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
