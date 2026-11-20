#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_hunt_smoothing.py
#  Purpose:      Assertions for huntRunner.smoothing on the deployed dfe-engine Application
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for huntRunner.smoothing on the deployed dfe-engine Application.

The runner moves rate-hunt offsets when hunts.smoothing.enabled is on, and the
engine API reads the same switch to report next due, so both pods must get the
same settings or the console shows a schedule the runner is not keeping.

1. Off by default: neither pod gets a DFE_ENGINE_HUNTS_SMOOTHING_ variable.
2. Set: both pods get the switch and every tunable given, and nothing else.
3. A value the engine cannot parse fails the render by its values path.

    python3 scripts/tests/test_hunt_smoothing.py

Needs `helm` on PATH. The engine-side cases render the thin chart, which needs
the scalo-service library (_weave.library).
"""

import sys

from _expect import expect, standalone, summary
from _gate import effective_env
from _weave import render_app, weave

ENGINE = "dfe-engine"
RUNNER = "dfe-hunt-runner"
PREFIX = "DFE_ENGINE_HUNTS_SMOOTHING_"
ON = {
    "enabled": True,
    "cycleSeconds": 120,
    "costQuantile": 0.9,
    "windowDays": 3,
    "cooldownCycles": 0,
    "memoryCapBytes": 2000000000,
}
ON_ENV = {
    "ENABLED": "true",
    "CYCLE_SECONDS": "120",
    "COST_QUANTILE": "0.9",
    "WINDOW_DAYS": "3",
    "COOLDOWN_CYCLES": "0",
    "MEMORY_CAP_BYTES": "2000000000",
}


def smoothing_env(docs: list[dict], deployment: str) -> dict[str, object]:
    found = effective_env(docs, f"Deployment/{deployment}")
    return {k[len(PREFIX) :]: v for k, v in found.items() if k.startswith(PREFIX)}


def rendered(smoothing: dict | None) -> list[dict]:
    overlay = {"huntRunner": {"smoothing": smoothing}} if smoothing else None
    return render_app(ENGINE, "slim", "local", overlay=overlay)


def refused(smoothing: dict) -> str:
    try:
        rendered(smoothing)
    except weave().WeaveError as exc:
        return str(exc)
    return ""


def test_off_by_default_neither_pod_gets_the_settings() -> None:
    docs = rendered(None)
    for deployment in (ENGINE, RUNNER):
        found = smoothing_env(docs, deployment)
        expect(
            f"{deployment}: no smoothing variable by default", found == {}, f"{found}"
        )


def test_set_values_reach_both_pods_alike() -> None:
    docs = rendered(ON)
    for deployment in (ENGINE, RUNNER):
        found = smoothing_env(docs, deployment)
        expect(
            f"{deployment}: the switch and each set tunable",
            found == ON_ENV,
            f"{found}",
        )


def test_a_value_the_engine_cannot_parse_fails_the_render() -> None:
    for smoothing, path in (
        ({"enabled": "yes"}, "huntRunner.smoothing.enabled"),
        ({"costQuantile": "high"}, "huntRunner.smoothing.costQuantile"),
        ({"windowDays": 1.5}, "huntRunner.smoothing.windowDays"),
        ({"bucketSeconds": 0}, "huntRunner.smoothing.bucketSeconds"),
    ):
        message = refused(smoothing)
        expect(
            f"{smoothing} fails the render naming {path}",
            path in message,
            message or "rendered",
        )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
