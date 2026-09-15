#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_rollout.py
#  Purpose:      Prove every DFE app Deployment replaces its pods surge-first,
#                that a config change moves the checksum that triggers it, and
#                that a multi-pod workload carries a disruption budget.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The rollout contract: how a config change reaches a running pod.

Every config change rolls the pod -- one delivery mechanism for every key, so
nothing depends on which apps happen to watch their own config file. That makes
the ROLL the thing that has to be safe. Kubernetes' default allows 25 per cent
unavailable, which at one replica tears the old pod down before the new one is
Ready and leaves the stage absent for a whole container start. dfe-common.rollout
inverts it: maxUnavailable 0, maxSurge 1.

Asserted from renders, not from reading the templates:

  every app Deployment  -> RollingUpdate, maxUnavailable 0, maxSurge 1
  a config change       -> checksum/config moves, so the pods roll
  replicas or KEDA >= 2 -> a PodDisruptionBudget, minAvailable 1 where nothing
                           can shrink the workload to a single pod

    python3 scripts/tests/test_rollout.py

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

# The DFE app charts and a settable key under their opaque `config` blob, used to
# prove a config edit moves the checksum the roll keys on.
APPS = {
    "dfe-receiver": "routing.mode",
    "dfe-loader": "clickhouse.database",
    "dfe-fetcher": "sources.aws.topic",
    "dfe-archiver": "archive.destination",
    "dfe-transform-vrl": "sink.topic",
    "dfe-transform-vector": "sink.topic",
    "dfe-transform-elastic": "source.name",
    "dfe-transform-wasm": "sink.topic",
}

# Workloads that hold a ReadWriteOnce volume cannot surge past themselves, so
# they replace in place instead. Their charts say so at the strategy block.
RECREATE = {
    ("dfe-engine", "dfe-engine"): "the config PVC is ReadWriteOnce",
}

# dfe-transform-wasm is left out: it is an unpublished alpha with no apps.yaml
# entry and no transport dial, so its chart has nothing to branch on.
TRANSPORT_AWARE = sorted(set(APPS) - {"dfe-transform-wasm"})

EXPECTED = {
    "type": "RollingUpdate",
    "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
}


def render(chart: str, *sets: str, typed: bool = False) -> str:
    """Render a chart. `typed` uses --set, so a boolean stays a boolean.

    --set-string is the default because the probe values below are config
    strings, and Helm would otherwise read one that looks like a number or a
    date as that type.
    """
    cmd = ["helm", "template", chart, str(CHARTS / chart)]
    for s in sets:
        cmd += ["--set" if typed else "--set-string", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart} {sets}:\n{out.stderr}")
    return out.stdout


def docs(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if d]


def deployments(text: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs(text) if d.get("kind") == "Deployment"}


def checksum(text: str, name: str) -> str:
    pod = deployments(text)[name]["spec"]["template"]["metadata"]
    return (pod.get("annotations") or {}).get("checksum/config", "")


def test_every_app_deployment_replaces_surge_first() -> None:
    for chart in sorted(APPS):
        for name, dep in sorted(deployments(render(chart)).items()):
            expect(
                f"{chart}/{name} rolls surge-first",
                dep["spec"].get("strategy") == EXPECTED,
                f"got {dep['spec'].get('strategy')!r}",
            )


def test_the_engine_chart_replaces_in_place_where_it_must() -> None:
    """A RWO volume cannot surge; the exception is declared, not accidental."""
    rendered = deployments(render("dfe-engine"))
    for (chart, name), why in RECREATE.items():
        if chart != "dfe-engine":
            continue
        expect(
            f"{name} replaces in place ({why})",
            rendered[name]["spec"].get("strategy") == {"type": "Recreate"},
            f"got {rendered[name]['spec'].get('strategy')!r}",
        )
    # The other two workloads in the same chart hold nothing, so they surge.
    for name in ("dfe-hunt-runner", "dfe-keda-shim"):
        expect(
            f"{name} rolls surge-first",
            rendered[name]["spec"].get("strategy") == EXPECTED,
            f"got {rendered[name]['spec'].get('strategy')!r}",
        )


def test_the_engine_surges_once_its_volume_is_shared() -> None:
    """Recreate is conditional on the PVC, not a property of the app."""
    rendered = deployments(render("dfe-engine", "config.persistence.enabled=false", typed=True))
    expect(
        "dfe-engine rolls surge-first with no PVC",
        rendered["dfe-engine"]["spec"].get("strategy") == EXPECTED,
        f"got {rendered['dfe-engine']['spec'].get('strategy')!r}",
    )


def test_a_config_change_rolls_the_pods() -> None:
    for chart, key in sorted(APPS.items()):
        base = checksum(render(chart), chart)
        moved = checksum(render(chart, f"config.{key}=probe-value"), chart)
        expect(f"{chart} carries a config checksum", base != "", "no checksum/config annotation")
        expect(
            f"{chart}: a change to {key} moves the checksum",
            base != moved,
            "the checksum is unchanged, so the edit would never reach a pod",
        )


def test_a_transport_change_rolls_the_pods() -> None:
    """kafka.mode reaches the pods as env and config, never as a live reload."""
    for chart in TRANSPORT_AWARE:
        bus = deployments(render(chart, "kafka.mode="))[chart]["spec"]["template"]
        direct = deployments(render(chart, "kafka.mode=disabled"))[chart]["spec"]["template"]
        expect(
            f"{chart}: switching transport rolls the pods",
            bus != direct,
            "the pod template is identical on both transports",
        )


def test_a_single_replica_workload_carries_no_budget() -> None:
    """Any budget at one replica blocks every node drain."""
    for chart in sorted(APPS):
        found = [d for d in docs(render(chart)) if d.get("kind") == "PodDisruptionBudget"]
        expect(f"{chart} has no PDB at one replica", found == [], f"got {len(found)}")


def test_a_multi_replica_workload_carries_a_budget() -> None:
    for chart in sorted(APPS):
        text = render(chart, "replicaCount=2", "keda.enabled=true", "keda.minReplicaCount=2")
        found = [d for d in docs(text) if d.get("kind") == "PodDisruptionBudget"]
        expect(f"{chart} has a PDB at two replicas", len(found) == 1, f"got {len(found)}")
        if found:
            expect(
                f"{chart}'s budget keeps one pod up",
                found[0]["spec"].get("minAvailable") == 1,
                f"got {found[0]['spec']!r}",
            )


def test_a_budget_never_blocks_a_drain_the_autoscaler_can_cause() -> None:
    """KEDA floor 1 means the workload can reach one pod, where minAvailable wedges."""
    for chart in sorted(APPS):
        text = render(chart, "replicaCount=2", "keda.enabled=true", "keda.minReplicaCount=1")
        found = [d for d in docs(text) if d.get("kind") == "PodDisruptionBudget"]
        if not found:
            expect(f"{chart} renders a PDB at replicaCount 2", False, "none rendered")
            continue
        expect(
            f"{chart}'s budget is maxUnavailable while KEDA can shrink it to one",
            found[0]["spec"].get("maxUnavailable") == 1,
            f"got {found[0]['spec']!r}",
        )


def main() -> int:
    with standalone():
        test_every_app_deployment_replaces_surge_first()
        test_the_engine_chart_replaces_in_place_where_it_must()
        test_the_engine_surges_once_its_volume_is_shared()
        test_a_config_change_rolls_the_pods()
        test_a_transport_change_rolls_the_pods()
        test_a_single_replica_workload_carries_no_budget()
        test_a_multi_replica_workload_carries_a_budget()
        test_a_budget_never_blocks_a_drain_the_autoscaler_can_cause()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
