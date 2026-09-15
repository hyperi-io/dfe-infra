#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_keda_pressure_defaults.py
#  Purpose:      Prove every app chart renders the ScalingPressure trigger by
#                default, at a shim address derived from the release namespace.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Render assertions for the KEDA pressure trigger defaults.

Two defects this pins. The library default named namespace `dfe` literally, so a
deployment anywhere else rendered a metric URL that never answered -- and a KEDA
external metric that errors stops the HPA scaling at all, CPU trigger included
(dfe-infra#301). And the app charts shipped the trigger off, so the composite the
apps emit drove nothing (dfe-infra#302, dfe-infra#303).

    python3 scripts/tests/test_keda_pressure_defaults.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"

# Every app chart whose ScaledObject carries the pressure trigger. dfe-fetcher is
# absent on purpose: it sets keda.enabled false and renders no ScaledObject.
PRESSURE_CHARTS = [
    "dfe-archiver",
    "dfe-loader",
    "dfe-receiver",
    "dfe-transform-elastic",
    "dfe-transform-splack",
    "dfe-transform-vector",
    "dfe-transform-vrl",
    "dfe-transform-wasm",
]


def render(chart: str, *args: str, namespace: str = "dfe") -> list[dict]:
    cmd = ["helm", "template", "kt", str(CHARTS / chart), "--namespace", namespace, *args]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart} {args}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


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
    for chart in PRESSURE_CHARTS:
        rendered = triggers(render(chart), f"{chart}-scaler")
        kinds = sorted(t.get("type") for t in rendered)
        expect(
            f"{chart} renders both the cpu and metrics-api triggers by default",
            kinds == ["cpu", "metrics-api"],
            f"got {kinds}",
        )


def test_the_shim_address_follows_the_release_namespace() -> None:
    for chart in PRESSURE_CHARTS:
        url = metrics_api_url(render(chart, namespace="dfe-local"), f"{chart}-scaler")
        expect(
            f"{chart}'s shim URL names the release namespace, not a literal dfe",
            "dfe-keda-shim.dfe-local.svc.cluster.local:8080" in url,
            url,
        )


def test_an_explicit_shim_address_still_wins() -> None:
    """A shim outside the release namespace stays reachable through the override."""
    url = metrics_api_url(
        render(
            "dfe-loader",
            "--set",
            "keda.pressure.shimAddress=shim.elsewhere.svc.cluster.local:9090",
            namespace="dfe-local",
        ),
        "dfe-loader-scaler",
    )
    expect(
        "keda.pressure.shimAddress overrides the derived address",
        url.startswith("http://shim.elsewhere.svc.cluster.local:9090/"),
        url,
    )


def test_pressure_can_still_be_turned_off() -> None:
    """profile-slim turns it off, so the dial has to reach the rendered triggers."""
    rendered = triggers(
        render("dfe-loader", "--set", "keda.pressure.enabled=false"), "dfe-loader-scaler"
    )
    kinds = sorted(t.get("type") for t in rendered)
    expect(
        "keda.pressure.enabled=false leaves the cpu trigger alone",
        kinds == ["cpu"],
        f"got {kinds}",
    )


def test_the_hunt_runner_shim_address_follows_the_namespace_too() -> None:
    docs = render(
        "dfe-engine", "--set", "huntRunner.autoscaling.enabled=true", namespace="dfe-local"
    )
    url = metrics_api_url(docs, "dfe-hunt-runner-scaler")
    expect(
        "the hunt-runner backlog trigger names the release namespace",
        "dfe-keda-shim.dfe-local.svc.cluster.local:8080" in url,
        url,
    )


def overlay(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / "argocd" / "values" / f"{name}.yaml").read_text())


def test_only_the_brokerless_minimum_tier_turns_pressure_off() -> None:
    """slim runs no broker and caps at 2 replicas, so cpu is the whole signal."""
    for name in ("slim", "single", "mesh", "scale"):
        off = overlay(f"profile-{name}").get("keda", {}).get("pressure", {}).get("enabled")
        expect(
            f"profile-{name} pressure posture",
            (off is False) if name == "slim" else (off is None),
            f"keda.pressure.enabled={off!r}",
        )


def test_the_scale_ceiling_divides_the_partition_count() -> None:
    """An uneven ceiling leaves the busy consumers setting the group's lag alone."""
    scale = overlay("profile-scale")
    ceiling = scale["keda"]["maxReplicaCount"]
    partitions = scale["kafka"]["defaultTopic"]["partitions"]
    expect(
        "profile-scale's KEDA ceiling divides defaultTopic.partitions",
        partitions % ceiling == 0,
        f"ceiling={ceiling}, partitions={partitions}",
    )


def test_the_umbrella_profile_carries_the_same_ceiling() -> None:
    """helm/dfe-stack/profiles/scale.yaml mirrors the Argo overlay by declaration."""
    umbrella = yaml.safe_load(
        (REPO_ROOT / "helm" / "dfe-stack" / "profiles" / "scale.yaml").read_text()
    )
    expect(
        "the umbrella scale profile matches the Argo overlay's KEDA ceiling",
        umbrella["dfe-loader"]["keda"]["maxReplicaCount"]
        == overlay("profile-scale")["keda"]["maxReplicaCount"],
        str(umbrella["dfe-loader"]["keda"]),
    )


def main() -> int:
    with standalone():
        test_every_app_chart_ships_the_pressure_trigger()
        test_the_shim_address_follows_the_release_namespace()
        test_an_explicit_shim_address_still_wins()
        test_pressure_can_still_be_turned_off()
        test_the_hunt_runner_shim_address_follows_the_namespace_too()
        test_only_the_brokerless_minimum_tier_turns_pressure_off()
        test_the_scale_ceiling_divides_the_partition_count()
        test_the_umbrella_profile_carries_the_same_ceiling()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
