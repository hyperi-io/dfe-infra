#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_engine_deployment_facts.py
#  Purpose:      Prove the engine is TOLD its tier, its ui pin and the app
#                manifest, from the one place each is already known, so the
#                console never guesses.
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

`GET /api/v1/apps` answers the same way. The app manifest is dfe-infra's
apps.yaml, and Helm reads only files inside a chart, so the chart carries the
copy scripts/composition.py renders and mounts it -- which is what makes a new
or changed app a manifest edit rather than an engine release.

    python3 scripts/tests/test_engine_deployment_facts.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENGINE_CHART = REPO_ROOT / "helm" / "charts" / "dfe-engine"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-apps.yaml"
VALUES = REPO_ROOT / "argocd" / "values"

PROFILES = ("slim", "single", "scale", "mesh")

# The cluster-secret annotation that is the SSoT for the tier at render time.
PROFILE_ANNOTATION = "dfe.hyperi.io/profile"

# Where the chart mounts the manifest, and what it points the engine at.
CATALOGUE_CONFIGMAP = "dfe-engine-app-catalogue"
CATALOGUE_FILE = "/etc/dfe-engine/catalogue/apps.yaml"


def render(*args: str, chart: Path = ENGINE_CHART) -> str:
    cmd = ["helm", "template", "dfe-engine", str(chart), *args]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for dfe-engine {args}:\n{out.stderr}")
    return out.stdout


def documents(*args: str, chart: Path = ENGINE_CHART) -> list[dict]:
    return [d for d in yaml.safe_load_all(render(*args, chart=chart)) if d]


def pod_template(*args: str, chart: Path = ENGINE_CHART) -> dict:
    deployment = next(
        d
        for d in documents(*args, chart=chart)
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == "dfe-engine"
    )
    return deployment["spec"]["template"]


def engine_env(*args: str) -> dict[str, str | None]:
    container = pod_template(*args)["spec"]["containers"][0]
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


def test_the_manifest_reaches_the_engine() -> None:
    """Without the mount the engine answers off the snapshot in its image."""
    for profile in PROFILES:
        args = (
            "-f",
            str(VALUES / "common.yaml"),
            "-f",
            str(VALUES / f"profile-{profile}.yaml"),
        )
        catalogue = next(
            (
                d
                for d in documents(*args)
                if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == CATALOGUE_CONFIGMAP
            ),
            None,
        )
        expect(
            f"{profile} renders the app catalogue",
            catalogue is not None,
            f"no ConfigMap {CATALOGUE_CONFIGMAP}",
        )
        if catalogue:
            expect(
                f"{profile} mounts the manifest this repo declares",
                catalogue["data"]["apps.yaml"]
                == (ENGINE_CHART / "files" / "apps.yaml").read_text(encoding="utf-8"),
                "the rendered catalogue is not the chart's copy of apps.yaml",
            )
        expect(
            f"{profile} points the engine at the mounted file",
            engine_env(*args).get("DFE_APP_CATALOGUE_FILE") == CATALOGUE_FILE,
            f"got {engine_env(*args).get('DFE_APP_CATALOGUE_FILE')!r}",
        )


def test_the_engine_reads_the_manifest_off_that_configmap() -> None:
    """An env var naming a path nothing mounted fails the engine at startup."""
    template = pod_template()
    volume = next(
        (v for v in template["spec"]["volumes"] if v["name"] == "app-catalogue"), None
    )
    expect(
        "the catalogue volume is the rendered ConfigMap",
        volume is not None and volume["configMap"]["name"] == CATALOGUE_CONFIGMAP,
        f"got {volume!r}",
    )
    mount = next(
        (
            m
            for m in template["spec"]["containers"][0]["volumeMounts"]
            if m["name"] == "app-catalogue"
        ),
        None,
    )
    expect(
        "and it is mounted where the env var looks",
        mount is not None and mount["mountPath"] == str(Path(CATALOGUE_FILE).parent),
        f"got {mount!r}",
    )


def test_a_manifest_edit_moves_the_pod_template() -> None:
    """The engine reads the manifest once at startup, so a ConfigMap-only change
    would sit in etcd unread until something else rolled the pod."""
    with tempfile.TemporaryDirectory(prefix="dfe-engine-chart-") as tmp:
        edited = Path(tmp) / "dfe-engine"
        shutil.copytree(ENGINE_CHART, edited)
        manifest = edited / "files" / "apps.yaml"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "scale_deployed: true", "scale_deployed: false", 1
            ),
            encoding="utf-8",
        )
        before = pod_template()["metadata"]["annotations"].get("checksum/app-catalogue")
        after = pod_template(chart=edited)["metadata"]["annotations"].get(
            "checksum/app-catalogue"
        )
        expect("the pod template carries a catalogue checksum", bool(before), "no annotation")
        expect(
            "and a manifest edit moves it",
            before != after,
            "the checksum is unchanged, so the edit would never reach a pod",
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
        test_the_manifest_reaches_the_engine()
        test_the_engine_reads_the_manifest_off_that_configmap()
        test_a_manifest_edit_moves_the_pod_template()
        test_every_profile_still_renders_with_the_real_overlays()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
