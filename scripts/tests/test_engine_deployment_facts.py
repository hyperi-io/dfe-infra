#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_engine_deployment_facts.py
#  Purpose:      Prove the engine is TOLD its tier and its ui pin, from the one
#                place each is already known, so the console never guesses.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The engine can only report what the deploy hands it.

`GET /api/v1/system/deployment` answers what this deployment IS -- its tier, the
transports it carries, the versions it runs -- and every one of those is a fact
the chart injects. Two are new here: the profile, which comes from the SAME
cluster-secret annotation that selects argocd/values/profile-<x>.yaml, and the
dfe-ui pin, which Helm cannot read off a sibling chart and so is mirrored into
the engine's values under check_versions_drift.py.

    python3 scripts/tests/test_engine_deployment_facts.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENGINE_CHART = REPO_ROOT / "helm" / "charts" / "dfe-engine"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-apps.yaml"
VALUES = REPO_ROOT / "argocd" / "values"

PROFILES = ("slim", "single", "scale", "scale-mesh")

# The cluster-secret annotation that is the SSoT for the tier at render time.
PROFILE_ANNOTATION = "dfe.hyperi.io/profile"


def render(*args: str) -> str:
    cmd = ["helm", "template", "dfe-engine", str(ENGINE_CHART), *args]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for dfe-engine {args}:\n{out.stderr}")
    return out.stdout


def engine_env(*args: str) -> dict[str, str | None]:
    docs = [d for d in yaml.safe_load_all(render(*args)) if d]
    deployment = next(
        d
        for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == "dfe-engine"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container["env"]}


def test_the_profile_reaches_the_engine() -> None:
    for profile in PROFILES:
        env = engine_env("--set", f"profile={profile}")
        expect(
            f"{profile} is passed to the engine",
            env.get("DFE_PROFILE") == profile,
            f"got {env.get('DFE_PROFILE')!r}",
        )


def test_an_unset_profile_renders_no_variable() -> None:
    """Absent beats empty: the engine's default already means unknown."""
    expect(
        "no profile renders no DFE_PROFILE",
        "DFE_PROFILE" not in engine_env(),
        "the variable was rendered with nothing in it",
    )


def test_the_profile_comes_from_the_annotation_that_picks_the_values_file() -> None:
    """Two copies of the tier in one Application would be two things to get wrong."""
    text = APPSET.read_text(encoding="utf-8")
    parameter = re.search(
        r"- name: profile\n\s*value: '\{\{ index \.metadata\.annotations \"([^\"]+)\" \}\}'",
        text,
    )
    expect(
        "the appset passes a profile parameter",
        parameter is not None,
        "layer2-apps.yaml renders no profile parameter",
    )
    if parameter:
        expect(
            "and it reads the same annotation the values file does",
            parameter.group(1) == PROFILE_ANNOTATION,
            f"got {parameter.group(1)!r}",
        )
    expect(
        "which is the annotation profile-<x>.yaml is selected by",
        f'profile-{{{{ index .metadata.annotations "{PROFILE_ANNOTATION}" }}}}.yaml' in text,
        "the values-file selector no longer reads that annotation",
    )


def test_the_ui_pin_reaches_the_engine() -> None:
    env = engine_env()
    expect(
        "the engine is told the ui version",
        bool(env.get("DFE_UI_VERSION")),
        f"got {env.get('DFE_UI_VERSION')!r}",
    )


def test_the_ui_pin_is_the_one_the_ui_chart_deploys() -> None:
    """A stale mirror would have the console report a version nobody is running."""
    ui_chart = (REPO_ROOT / "helm" / "charts" / "dfe-ui" / "Chart.yaml").read_text(
        encoding="utf-8"
    )
    deployed = re.search(r'appVersion:\s*"([^"]+)"', ui_chart)
    expect("the ui chart pins an appVersion", deployed is not None, "no appVersion found")
    if deployed:
        expect(
            "and the engine reports that same version",
            engine_env().get("DFE_UI_VERSION") == deployed.group(1),
            f"engine says {engine_env().get('DFE_UI_VERSION')!r}, ui chart {deployed.group(1)!r}",
        )


def test_every_profile_still_renders_with_the_real_overlays() -> None:
    for profile in PROFILES:
        env = engine_env(
            "-f",
            str(VALUES / "common.yaml"),
            "-f",
            str(VALUES / f"profile-{profile}.yaml"),
            "--set",
            f"profile={profile}",
        )
        expect(
            f"{profile} renders the engine with its tier",
            env.get("DFE_PROFILE") == profile,
            f"got {env.get('DFE_PROFILE')!r}",
        )
        expect(
            f"{profile} still carries the transport facts",
            env.get("DFE_TRANSPORT_DEFAULT") in ("bus", "direct"),
            f"got {env.get('DFE_TRANSPORT_DEFAULT')!r}",
        )


def main() -> int:
    with standalone():
        test_the_profile_reaches_the_engine()
        test_an_unset_profile_renders_no_variable()
        test_the_profile_comes_from_the_annotation_that_picks_the_values_file()
        test_the_ui_pin_reaches_the_engine()
        test_the_ui_pin_is_the_one_the_ui_chart_deploys()
        test_every_profile_still_renders_with_the_real_overlays()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
