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
Ready and leaves the stage absent for a whole container start. The scalo-service
library inverts it: maxUnavailable 0, maxSurge 1, unless the pod holds a claim.

Asserted from the goldens under fixtures/thin-renders, which test_thin_renders.py
holds equal to what Argo deploys, and from live renders for the dials:

  every app Deployment  -> RollingUpdate, maxUnavailable 0, maxSurge 1
  a claim on the pod    -> Recreate, since the replacement cannot mount it
  a config change       -> checksum/config moves, so the pods roll
  env from a ConfigMap  -> Reloader watches that ConfigMap
  replicas or KEDA >= 2 -> a PodDisruptionBudget, minAvailable 1 where nothing
                           can shrink the workload to a single pod

    python3 scripts/tests/test_rollout.py

The dial cases need `helm` on PATH and the scalo-service library (_weave.library).
"""

import sys

from _expect import expect, standalone, summary
from _gate import CELLS, cell, golden, goldens, object_id
from _weave import render_app

# The DFE app components and a settable key under their opaque `config` blob, used
# to prove a config edit moves the checksum the roll keys on.
APPS = {
    "dfe-receiver": "routing.mode",
    "dfe-loader": "clickhouse.database",
    "dfe-fetcher": "sources.aws.topic",
    "dfe-archiver": "archive.destination",
    "dfe-transform-vrl": "sink.topic",
    "dfe-transform-vector": "sink.topic",
    "dfe-transform-elastic": "source.name",
}

ENGINE = "dfe-engine"
# dfe-extras renders these beside the engine; they hold no claim, so they surge.
ENGINE_SIDE_WORKLOADS = ("dfe-hunt-runner", "dfe-keda-shim")

# A bus profile and a direct one, for the transport switch.
BUS, DIRECT = "single", "slim"

# Where the dial cases render: the tier that runs one pod by default.
DIAL_CELL = ("slim", "local")

RELOAD_CONFIGMAPS = "configmap.reloader.stakater.com/reload"

EXPECTED = {
    "type": "RollingUpdate",
    "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
}
RECREATE = {"type": "Recreate"}


def deployments(docs: list[dict]) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Deployment"}


def checksum(docs: list[dict], name: str) -> str:
    pod = deployments(docs)[name]["spec"]["template"]["metadata"]
    return (pod.get("annotations") or {}).get("checksum/config", "")


def budgets(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d.get("kind") == "PodDisruptionBudget"]


def floor(docs: list[dict], name: str) -> int:
    """The fewest pods the workload can run: KEDA's minimum, else its replica count."""
    scaler = next((d for d in docs if d.get("kind") == "ScaledObject"), None)
    if scaler is not None:
        return int(scaler["spec"].get("minReplicaCount", 1))
    return int(deployments(docs)[name]["spec"].get("replicas", 1))


def label(where: tuple[str, ...]) -> str:
    return "/".join(where[1:])


def app_cells(app: str) -> list[tuple[str, str, str, str]]:
    return [c for c in CELLS if c[0] == app]


def test_every_app_deployment_replaces_surge_first() -> None:
    for app in sorted(APPS):
        for where, docs in goldens(app):
            for name, dep in sorted(deployments(docs).items()):
                expect(
                    f"{name} on {label(where)} rolls surge-first",
                    dep["spec"].get("strategy") == EXPECTED,
                    f"got {dep['spec'].get('strategy')!r}",
                )


def test_the_engine_chart_replaces_in_place_where_it_must() -> None:
    """A ReadWriteOnce claim cannot surge; the workloads beside it hold none."""
    for where, docs in goldens(ENGINE):
        rendered = deployments(docs)
        claims = {
            d["metadata"]["name"]: d["spec"].get("accessModes")
            for d in docs
            if d.get("kind") == "PersistentVolumeClaim"
        }
        expect(
            f"{ENGINE} on {label(where)} holds its config on a ReadWriteOnce claim",
            claims == {"dfe-engine-config": ["ReadWriteOnce"]},
            f"got {claims!r}",
        )
        expect(
            f"{ENGINE} on {label(where)} replaces in place",
            rendered[ENGINE]["spec"].get("strategy") == RECREATE,
            f"got {rendered[ENGINE]['spec'].get('strategy')!r}",
        )
        for name in ENGINE_SIDE_WORKLOADS:
            expect(
                f"{name} on {label(where)} rolls surge-first",
                name in rendered and rendered[name]["spec"].get("strategy") == EXPECTED,
                f"got {rendered.get(name, {}).get('spec', {}).get('strategy')!r}",
            )


def test_a_claim_is_what_makes_a_workload_replace_in_place() -> None:
    """Recreate follows the claim, not the app; the engine's contract pins it on top.

    dfe-fetcher's cursor path is a claim the integration values turn off. Turned
    on, the pod holds it and replaces in place. The engine is a singleton in its
    contract, so it keeps Recreate even with its config volume off the claim.
    """
    profile, cloud = DIAL_CELL
    cursor = {"writablePaths": {"cursor": {"persistence": {"enabled": True}}}}
    held = deployments(render_app("dfe-fetcher", profile, cloud, overlay=cursor))
    expect(
        "dfe-fetcher replaces in place once its cursor is a claim",
        held["dfe-fetcher"]["spec"].get("strategy") == RECREATE,
        f"got {held['dfe-fetcher']['spec'].get('strategy')!r}",
    )
    shared = deployments(golden("dfe-fetcher", profile, cloud))
    expect(
        "and surges while it is not",
        shared["dfe-fetcher"]["spec"].get("strategy") == EXPECTED,
        f"got {shared['dfe-fetcher']['spec'].get('strategy')!r}",
    )
    no_claim = {"writablePaths": {"config": {"persistence": {"enabled": False}}}}
    engine = render_app(ENGINE, profile, cloud, overlay=no_claim)
    expect(
        f"{ENGINE} mounts no claim with its config volume off it",
        not [d for d in engine if d.get("kind") == "PersistentVolumeClaim"],
        "a claim still rendered",
    )
    expect(
        f"{ENGINE} still replaces in place as a singleton",
        deployments(engine)[ENGINE]["spec"].get("strategy") == RECREATE,
        f"got {deployments(engine)[ENGINE]['spec'].get('strategy')!r}",
    )


def test_a_config_change_rolls_the_pods() -> None:
    profile, cloud = DIAL_CELL
    for app, key in sorted(APPS.items()):
        base = checksum(cell(app, profile, cloud), app)
        overlay: dict = {}
        node = overlay.setdefault("config", {})
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = "probe-value"
        moved = checksum(render_app(app, profile, cloud, overlay=overlay), app)
        expect(f"{app} carries a config checksum", base != "", "no checksum/config annotation")
        expect(
            f"{app}: a change to {key} moves the checksum",
            base != moved,
            "the checksum is unchanged, so the edit would never reach a pod",
        )


def test_a_transport_change_rolls_the_pods() -> None:
    """The transport reaches the pods as env and config, never as a live reload.

    The pod template carries what the thin chart renders; env a ConfigMap feeds
    through envFrom changes no template, so Reloader has to watch that ConfigMap.
    """
    for app in sorted(APPS):
        for cloud in ("local", "aws"):
            bus_docs = golden(app, BUS, cloud)
            direct_docs = golden(app, DIRECT, cloud)
            bus = deployments(bus_docs)[app]["spec"]["template"]
            direct = deployments(direct_docs)[app]["spec"]["template"]
            expect(
                f"{app} on {cloud}: switching transport rolls the pods",
                bus != direct,
                "the pod template is identical on both transports",
            )
            for docs, profile in ((bus_docs, BUS), (direct_docs, DIRECT)):
                dep = deployments(docs)[app]
                sources = dep["spec"]["template"]["spec"]["containers"][0].get("envFrom") or []
                fed = sorted(
                    s["configMapRef"]["name"] for s in sources if "configMapRef" in s
                )
                watched = (dep["metadata"].get("annotations") or {}).get(RELOAD_CONFIGMAPS, "")
                missing = [name for name in fed if name not in watched.split(",")]
                expect(
                    f"{app} on {profile}/{cloud}: Reloader watches every ConfigMap feeding env",
                    not missing,
                    f"unwatched {missing}",
                )


def test_a_single_replica_workload_carries_no_budget() -> None:
    """Any budget at one replica blocks every node drain."""
    for app in sorted(APPS):
        for where in app_cells(app):
            docs = golden(*where)
            if floor(docs, app) > 1:
                continue
            found = budgets(docs)
            expect(
                f"{app} on {label(where)} has no PDB at one replica",
                found == [],
                f"got {[object_id(d) for d in found]}",
            )


def test_a_multi_replica_workload_carries_a_budget() -> None:
    for app in sorted(APPS):
        for where in app_cells(app):
            docs = golden(*where)
            if floor(docs, app) < 2:
                continue
            found = budgets(docs)
            expect(
                f"{app} on {label(where)} keeps one pod up",
                len(found) == 1 and found[0]["spec"].get("minAvailable") == 1,
                f"got {[d['spec'] for d in found]!r}",
            )
    profile, cloud = DIAL_CELL
    overlay = {"replicaCount": 2, "keda": {"enabled": True, "minReplicaCount": 2}}
    for app in sorted(APPS):
        found = budgets(render_app(app, profile, cloud, overlay=overlay))
        expect(f"{app} has a PDB at two replicas", len(found) == 1, f"got {len(found)}")
        if found:
            expect(
                f"{app}'s budget keeps one pod up",
                found[0]["spec"].get("minAvailable") == 1,
                f"got {found[0]['spec']!r}",
            )


def test_a_budget_never_blocks_a_drain_the_autoscaler_can_cause() -> None:
    """KEDA floor 1 means the workload can reach one pod, where minAvailable wedges."""
    profile, cloud = DIAL_CELL
    overlay = {"replicaCount": 2, "keda": {"enabled": True, "minReplicaCount": 1}}
    for app in sorted(APPS):
        found = budgets(render_app(app, profile, cloud, overlay=overlay))
        if not found:
            expect(f"{app} renders a PDB at replicaCount 2", False, "none rendered")
            continue
        expect(
            f"{app}'s budget is maxUnavailable while KEDA can shrink it to one",
            found[0]["spec"].get("maxUnavailable") == 1,
            f"got {found[0]['spec']!r}",
        )


def main() -> int:
    with standalone():
        test_every_app_deployment_replaces_surge_first()
        test_the_engine_chart_replaces_in_place_where_it_must()
        test_a_claim_is_what_makes_a_workload_replace_in_place()
        test_a_config_change_rolls_the_pods()
        test_a_transport_change_rolls_the_pods()
        test_a_single_replica_workload_carries_no_budget()
        test_a_multi_replica_workload_carries_a_budget()
        test_a_budget_never_blocks_a_drain_the_autoscaler_can_cause()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
