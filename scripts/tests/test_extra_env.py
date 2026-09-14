#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_extra_env.py
#  Purpose:      Prove the overlay's extraEnv block reaches the container, and
#                reaches it after the env the chart derives.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for dfe-common.extraEnv across the six app charts.

An app's settings surface is wider than the dials its chart declares, so
dfe-engine writes a key the chart does not model into the overlay's `extraEnv`
block. One library helper renders it, called last in every env block -- six
identical copies would be six places to get the ordering wrong.

Four things are checked:

1. A custom key renders as a container env entry, with its value quoted.
2. It renders AFTER the derived entries, which for dfe-transform-vrl are the
   broker addresses and SASL credentials the deployment computes.
3. Every one of the six charts declares the key and renders it.
4. An absent or empty block renders the env the chart rendered before.

    python3 scripts/tests/test_extra_env.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
REGISTRY = "ghcr.io/hyperi-io"

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

CUSTOM = {"extraEnv": {"DFE_HOUSE_KEY": "kept", "DFE_HOUSE_PORT": 8123}}


def render(chart: str, values: dict | None = None) -> dict:
    """The app's Deployment, optionally under an overlay."""
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            "helm",
            "template",
            chart,
            str(CHARTS / chart),
            "--set",
            f"global.registry={REGISTRY}",
            "--show-only",
            "templates/deployment.yaml",
        ]
        if values is not None:
            overlay = Path(tmp) / "overlay.yaml"
            overlay.write_text(yaml.safe_dump(values), encoding="utf-8", newline="\n")
            cmd += ["--values", str(overlay)]
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart}:\n{out.stderr}")
    for doc in yaml.safe_load_all(out.stdout):
        if doc and doc.get("kind") == "Deployment":
            return doc
    return {}


def app_container(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]["containers"][0]


def env_names(doc: dict) -> list[str]:
    return [e["name"] for e in app_container(doc).get("env") or []]


def test_a_custom_key_becomes_container_env() -> None:
    """The block is the operator's half of the app's settings, so it has to land."""
    doc = render("dfe-transform-vrl", CUSTOM)
    env = {e["name"]: e.get("value") for e in app_container(doc).get("env") or []}
    expect(
        "a custom string key reaches the container",
        env.get("DFE_HOUSE_KEY") == "kept",
        f"{env.get('DFE_HOUSE_KEY')}",
    )
    expect(
        "a number is quoted, because env carries strings",
        env.get("DFE_HOUSE_PORT") == "8123",
        f"{env.get('DFE_HOUSE_PORT')!r}",
    )


def test_the_custom_block_renders_after_the_derived_one() -> None:
    """The entries above it are the deployment's own wiring.

    dfe-transform-vrl derives its broker addresses, topics and SASL credentials
    from the profile; a custom key interleaved with those hides a collision.
    """
    doc = render("dfe-transform-vrl", CUSTOM)
    names = env_names(doc)
    derived = [n for n in names if n.startswith("DFE_TRANSFORM_")]
    expect(
        "the derived block is present to be ordered against",
        "DFE_TRANSFORM_SOURCE_BROKERS" in derived,
        f"{names}",
    )
    expect(
        "every custom key sits after every derived one",
        min(names.index(k) for k in ("DFE_HOUSE_KEY", "DFE_HOUSE_PORT"))
        > max(names.index(n) for n in derived),
        f"{names}",
    )


def test_every_app_chart_carries_the_key() -> None:
    """Six charts, one helper: a chart missing it drops the operator's block."""
    for app in APPS:
        values = yaml.safe_load((CHARTS / app / "values.yaml").read_text(encoding="utf-8"))
        expect(
            f"{app} declares extraEnv, defaulting to nothing",
            values.get("extraEnv") == {},
            f"{values.get('extraEnv')!r}",
        )
        expect(
            f"{app} renders a custom key",
            "DFE_HOUSE_KEY" in env_names(render(app, CUSTOM)),
            f"{env_names(render(app, CUSTOM))}",
        )


def test_no_custom_keys_renders_the_env_it_always_did() -> None:
    """The block is opt-in: a deployment setting none must be unchanged."""
    for app in APPS:
        default = env_names(render(app))
        empty = env_names(render(app, {"extraEnv": {}}))
        expect(
            f"{app}: an empty block adds nothing",
            default == empty and "DFE_HOUSE_KEY" not in default,
            f"default={len(default)} empty={len(empty)}",
        )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
