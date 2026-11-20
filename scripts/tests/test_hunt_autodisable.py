#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_hunt_autodisable.py
#  Purpose:      Assertions for huntRunner.autodisable on the deployed dfe-engine Application
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for huntRunner.autodisable on the deployed dfe-engine Application.

The hunt runner stops a hunt that keeps tripping a ClickHouse resource limit, and
the engine pod never reads the setting, so only the runner gets the variables.

1. Unset: neither pod gets a DFE_ENGINE_HUNTS_AUTODISABLE_ variable.
2. Set: the runner gets exactly the variables given and the engine pod none.
3. A switch set to false renders "false", because the engine's default is on.
4. A value the engine cannot parse fails the render by its values path.

    python3 scripts/tests/test_hunt_autodisable.py

Needs `helm` on PATH. The engine-side cases render the thin chart, which needs
the scalo-service library (_weave.library).
"""

import sys

from _expect import expect, standalone, summary
from _gate import effective_env
from _weave import render_app, weave

ENGINE = "dfe-engine"
RUNNER = "dfe-hunt-runner"
PREFIX = "DFE_ENGINE_HUNTS_AUTODISABLE_"

# (case, huntRunner.autodisable values, the variables the runner renders)
RENDERS = (
    ("unset", None, {}),
    ("all empty", {"enabled": "", "failures": "", "windowSeconds": ""}, {}),
    (
        "every setting",
        {"enabled": True, "failures": 5, "windowSeconds": 7200},
        {"ENABLED": "true", "FAILURES": "5", "WINDOW_SECONDS": "7200"},
    ),
    ("switch off", {"enabled": False}, {"ENABLED": "false"}),
    ("switch off as a string", {"enabled": "false"}, {"ENABLED": "false"}),
    ("failures only", {"failures": 1}, {"FAILURES": "1"}),
    ("a large window", {"windowSeconds": 1000000}, {"WINDOW_SECONDS": "1000000"}),
)

# (values, the values path the failed render must name)
REFUSED = (
    ({"enabled": "yes"}, "huntRunner.autodisable.enabled"),
    ({"enabled": 1}, "huntRunner.autodisable.enabled"),
    ({"failures": 1.5}, "huntRunner.autodisable.failures"),
    ({"failures": "three"}, "huntRunner.autodisable.failures"),
    ({"failures": 0}, "huntRunner.autodisable.failures"),
    ({"windowSeconds": 0}, "huntRunner.autodisable.windowSeconds"),
    ({"windowSeconds": -5}, "huntRunner.autodisable.windowSeconds"),
    ({"windowSeconds": "1h"}, "huntRunner.autodisable.windowSeconds"),
)


def autodisable_env(docs: list[dict], deployment: str) -> dict[str, object]:
    found = effective_env(docs, f"Deployment/{deployment}")
    return {k[len(PREFIX) :]: v for k, v in found.items() if k.startswith(PREFIX)}


def rendered(autodisable: dict | None) -> list[dict]:
    overlay = {"huntRunner": {"autodisable": autodisable}} if autodisable else None
    return render_app(ENGINE, "slim", "local", overlay=overlay)


def refused(autodisable: dict) -> str:
    try:
        rendered(autodisable)
    except weave().WeaveError as exc:
        return str(exc)
    return ""


def test_the_runner_gets_the_settings_and_the_engine_pod_none() -> None:
    for case, autodisable, expected in RENDERS:
        docs = rendered(autodisable)
        on_runner = autodisable_env(docs, RUNNER)
        on_engine = autodisable_env(docs, ENGINE)
        expect(
            f"{case}: runner renders {expected}", on_runner == expected, f"{on_runner}"
        )
        expect(f"{case}: engine pod renders nothing", on_engine == {}, f"{on_engine}")


def test_a_value_the_engine_cannot_parse_fails_the_render() -> None:
    for autodisable, path in REFUSED:
        message = refused(autodisable)
        expect(
            f"{autodisable} fails the render naming {path}",
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
