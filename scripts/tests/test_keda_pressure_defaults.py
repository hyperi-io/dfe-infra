#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_keda_pressure_defaults.py
#  Purpose:      Prove every app chart whose app sets the gauge renders the
#                ScalingPressure trigger by default, at a shim address derived
#                from the release namespace.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Render assertions for the KEDA pressure trigger defaults.

Two defects this pins. A shim address naming namespace `dfe` literally renders,
for a deployment anywhere else, a metric URL that never answers -- and a KEDA
external metric that errors stops the HPA scaling at all, CPU trigger included
(dfe-infra#301). And an app shipped with the trigger off leaves the composite the
apps emit driving nothing (dfe-infra#302, dfe-infra#303).

Each app's ScaledObject comes from its thin chart, its triggers from its
integration values under argocd/values/apps (the pressure-trigger fragment), and
the hunt runner's from dfe-extras. What deploys by default is read off the
goldens; a namespace or a dial is rendered.

    python3 scripts/tests/test_keda_pressure_defaults.py

Needs `helm` on PATH; the rendered cases need the scalo-service library
(_weave.library). No test runner, matching the other checks here.
"""

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary
from _gate import CLOUDS, PROFILES, golden
from _weave import render_app, render_extras

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
VALUES = REPO_ROOT / "argocd" / "values"

# Every app whose ScaledObject carries the pressure trigger. dfe-fetcher is absent
# on purpose: it renders no ScaledObject. dfe-transform-vector is CPU_ONLY_APPS.
PRESSURE_APPS = [
    "dfe-archiver",
    "dfe-loader",
    "dfe-receiver",
    "dfe-transform-elastic",
    "dfe-transform-vrl",
]

# Apps that ship the cpu trigger alone, because the app sets no scaling_pressure
# gauge for the pressure trigger to read.
CPU_ONLY_APPS = [
    "dfe-transform-vector",
]

# The brokerless minimum tier, which runs on cpu alone.
CPU_ONLY_TIER = "slim"
PRESSURE_PROFILES = [p for p in PROFILES if p != CPU_ONLY_TIER]


def render(chart: str, *args: str, namespace: str = "dfe") -> list[dict]:
    cmd = ["helm", "template", "kt", str(CHARTS / chart), "--namespace", namespace, *args]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart} {args}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def kinds(docs: list[dict], scaler_name: str) -> list[str]:
    return sorted(t.get("type") for t in triggers(docs, scaler_name))


def triggers(docs: list[dict], scaler_name: str) -> list[dict]:
    for doc in docs:
        if doc.get("kind") == "ScaledObject" and doc["metadata"]["name"] == scaler_name:
            return doc.get("spec", {}).get("triggers", [])
    raise SystemExit(f"no ScaledObject {scaler_name} rendered")


def metrics_api_url(docs: list[dict], scaler_name: str) -> str:
    for trigger in triggers(docs, scaler_name):
        if trigger.get("type") == "metrics-api":
            return trigger["metadata"]["url"]
    raise SystemExit(f"{scaler_name} rendered no metrics-api trigger")


def test_every_app_chart_ships_the_pressure_trigger() -> None:
    for app in PRESSURE_APPS:
        for profile in PRESSURE_PROFILES:
            for cloud in CLOUDS:
                got = kinds(golden(app, profile, cloud), f"{app}-scaler")
                expect(
                    f"{app} {profile}/{cloud} renders both the cpu and metrics-api triggers",
                    got == ["cpu", "metrics-api"],
                    f"got {got}",
                )


def test_a_chart_whose_app_sets_no_gauge_ships_cpu_alone() -> None:
    for app in CPU_ONLY_APPS:
        for profile in PROFILES:
            for cloud in CLOUDS:
                got = kinds(golden(app, profile, cloud), f"{app}-scaler")
                expect(f"{app} {profile}/{cloud} renders the cpu trigger alone", got == ["cpu"],
                       f"got {got}")


def test_the_shim_address_follows_the_release_namespace() -> None:
    for app in PRESSURE_APPS:
        docs = render_app(app, "single", "local", namespace="dfe-local")
        url = metrics_api_url(docs, f"{app}-scaler")
        expect(
            f"{app}'s shim URL names the release namespace, not a literal dfe",
            "dfe-keda-shim.dfe-local.svc.cluster.local:8080" in url,
            url,
        )


def test_an_explicit_shim_address_still_wins() -> None:
    """A shim outside the release namespace stays reachable through the override."""
    elsewhere = {"keda": {"pressure": {"shimAddress": "shim.elsewhere.svc.cluster.local:9090"}}}
    docs = render_app("dfe-loader", "single", "local", namespace="dfe-local", overlay=elsewhere)
    url = metrics_api_url(docs, "dfe-loader-scaler")
    expect(
        "keda.pressure.shimAddress overrides the derived address",
        url.startswith("http://shim.elsewhere.svc.cluster.local:9090/"),
        url,
    )


def test_pressure_can_still_be_turned_off() -> None:
    """The slim tier turns it off by clearing keda.extraTriggers in each app's
    profile-slim.yaml, so a deployment's own instance values reach the same dial."""
    docs = render_app("dfe-loader", "single", "local", overlay={"keda": {"extraTriggers": []}})
    got = kinds(docs, "dfe-loader-scaler")
    expect("keda.extraTriggers=[] leaves the cpu trigger alone", got == ["cpu"], f"got {got}")


def test_no_values_file_sets_a_pressure_switch() -> None:
    """The trigger is a keda.extraTriggers entry and nothing reads keda.pressure.enabled,
    so a value there would read as turning pressure off and change nothing."""
    for path in sorted(VALUES.rglob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        keda = (doc.get("keda") or {}) if isinstance(doc, dict) else {}
        expect(
            f"{path.relative_to(REPO_ROOT)} sets no keda.pressure.enabled",
            "enabled" not in (keda.get("pressure") or {}),
            f"got {keda.get('pressure')!r}",
        )


def test_the_hunt_runner_shim_address_follows_the_namespace_too() -> None:
    scaled = {"huntRunner": {"autoscaling": {"enabled": True}}}
    docs = render_extras("dfe-engine", "slim", "local", namespace="dfe-local", overlay=scaled)
    url = metrics_api_url(docs, "dfe-hunt-runner-scaler")
    expect(
        "the hunt-runner backlog trigger names the release namespace",
        "dfe-keda-shim.dfe-local.svc.cluster.local:8080" in url,
        url,
    )


def overlay(name: str) -> dict:
    return yaml.safe_load((VALUES / f"{name}.yaml").read_text())


def test_only_the_brokerless_minimum_tier_turns_pressure_off() -> None:
    """slim runs no broker and caps at 2 replicas, so cpu is the whole signal."""
    for profile in PROFILES:
        for app in PRESSURE_APPS:
            got = kinds(golden(app, profile, "local"), f"{app}-scaler")
            want = ["cpu"] if profile == CPU_ONLY_TIER else ["cpu", "metrics-api"]
            expect(f"{app} on {profile} pressure posture", got == want, f"got {got}")


def test_the_scale_ceiling_divides_the_partition_count() -> None:
    """An uneven ceiling leaves the busy consumers setting the group's lag alone."""
    scale = overlay("profile-scale")
    ceiling = scale["keda"]["maxReplicaCount"]
    # The count is DERIVED (kafka.sizing), so it is read off the rendered broker
    # rather than a profile literal -- the two used to be a pair that could
    # disagree, which is why the literal went.
    docs = render(
        "kafka",
        "-f",
        str(VALUES / "common.yaml"),
        "-f",
        str(VALUES / "profile-scale.yaml"),
        "--set",
        "appNamespace=dfe",
    )
    kafka_cr = next(d for d in docs if d.get("kind") == "Kafka")
    partitions = int(kafka_cr["spec"]["kafka"]["config"]["num.partitions"])
    expect(
        "profile-scale's KEDA ceiling divides the broker's derived num.partitions",
        partitions % ceiling == 0,
        f"ceiling={ceiling}, partitions={partitions}",
    )


def main() -> int:
    with standalone():
        test_every_app_chart_ships_the_pressure_trigger()
        test_a_chart_whose_app_sets_no_gauge_ships_cpu_alone()
        test_the_shim_address_follows_the_release_namespace()
        test_an_explicit_shim_address_still_wins()
        test_pressure_can_still_be_turned_off()
        test_no_values_file_sets_a_pressure_switch()
        test_the_hunt_runner_shim_address_follows_the_namespace_too()
        test_only_the_brokerless_minimum_tier_turns_pressure_off()
        test_the_scale_ceiling_divides_the_partition_count()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
