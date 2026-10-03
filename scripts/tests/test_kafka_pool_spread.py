#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_kafka_pool_spread.py
#  Purpose:      Prove the KafkaNodePool spread constraints select the pods
#                Strimzi actually labels: the broker pool counts brokers, the
#                controller pool counts controllers, and neither counts anything else.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The broker spread has to count the brokers and nothing else.

Strimzi builds the Kafka pods, so a spread constraint can only select on the
labels Strimzi writes. `strimzi.io/cluster` alone is on every pod Strimzi runs for
the cluster -- brokers, dedicated controllers, the entity operator and Cruise
Control -- so it spreads them as one heap, and a node can still end up holding
two brokers while the count looks even.

So this test holds the labels the pinned Strimzi writes, evaluates each rendered
selector the way the scheduler does, and asserts it selects exactly the pods
it must keep apart. The fixture is tied to the Strimzi pin in versions.yaml, so an
operator bump fails here until someone re-reads its labels.

    python3 scripts/tests/test_kafka_pool_spread.py

Needs `helm` on PATH.
"""

import re
import subprocess
import sys
from functools import cache
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary
from _spread import Pod, assert_constraint_shape, counted, hostname_constraint

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
VERSIONS = REPO_ROOT / "versions.yaml"

# --- fixture: what Strimzi writes on the pods it builds -----------------------
# Recorded from github.com/strimzi/strimzi-kafka-operator at tag 1.2.0, the
# release behind the pinned strimzi-kafka-operator chart 1.2.0.
STRIMZI_VERSION = "1.2.0"

# Custom-resource labels Strimzi does NOT copy onto the pods it builds -- the
# default of STRIMZI_LABELS_EXCLUSION_PATTERN, operator-common/.../Labels.java:105-106,
# which the chart leaves unset (values.yaml labelsExclusionPattern: ""). Upstream
# leaves the dots unescaped; escaping them changes nothing for a real label key.
EXCLUDED = re.compile(r"(^app\.kubernetes\.io/(?!part-of).*|^kustomize\.toolkit\.fluxcd\.io.*)")


def copied(cr_labels: dict[str, str]) -> dict[str, str]:
    """The custom resource's labels that reach its pods (Labels.java:162-174)."""
    return {k: v for k, v in cr_labels.items() if not EXCLUDED.fullmatch(k)}


def pool_pod_labels(
    cluster: str, pool: str, pool_labels: dict[str, str], node_id: int, roles: set[str]
) -> dict[str, str]:
    """Labels on a KafkaNodePool pod.

    cluster-operator/.../model/KafkaPool.java:101-114 builds the pool labels from
    the KafkaNodePool's own; KafkaCluster.java:1280 adds the two role labels;
    WorkloadUtils.java:267 adds the pod name and the StrimziPodSet controller.
    """
    component = f"{cluster}-{pool}"
    return {
        **copied(pool_labels),
        "strimzi.io/kind": "Kafka",
        "strimzi.io/name": f"{cluster}-kafka",
        "strimzi.io/cluster": cluster,
        "strimzi.io/component-type": "kafka",
        "strimzi.io/pool-name": pool,
        "app.kubernetes.io/name": "kafka",
        "app.kubernetes.io/instance": cluster,
        "app.kubernetes.io/part-of": f"strimzi-{cluster}",
        "app.kubernetes.io/managed-by": "strimzi-cluster-operator",
        "strimzi.io/broker-role": "true" if "broker" in roles else "false",
        "strimzi.io/controller-role": "true" if "controller" in roles else "false",
        "strimzi.io/pod-name": f"{component}-{node_id}",
        "strimzi.io/controller": "strimzipodset",
        "strimzi.io/controller-name": component,
    }


def component_pod_labels(cluster: str, kafka_labels: dict[str, str], kind: str) -> dict[str, str]:
    """Labels on an entity-operator or Cruise Control pod.

    AbstractModel.java:113-123 calls Labels.generateDefaultLabels (Labels.java:
    492-506) with the component names from EntityOperator.java:115 and
    CruiseControl.java:167.
    """
    return {
        **copied(kafka_labels),
        "strimzi.io/kind": "Kafka",
        "strimzi.io/name": f"{cluster}-{kind}",
        "strimzi.io/cluster": cluster,
        "strimzi.io/component-type": kind,
        "app.kubernetes.io/name": kind,
        "app.kubernetes.io/instance": cluster,
        "app.kubernetes.io/part-of": f"strimzi-{cluster}",
        "app.kubernetes.io/managed-by": "strimzi-cluster-operator",
    }


# --- render -------------------------------------------------------------------
@cache
def render(sets: tuple[str, ...] = ()) -> tuple[dict, ...]:
    """The chart under the scale profile's value cascade, the one that runs Strimzi."""
    cmd = ["helm", "template", "kafka", str(chart_dir("kafka"))]
    for f in ("common.yaml", "local.yaml", "profile-scale.yaml"):
        cmd += ["-f", str(VALUES / f)]
    cmd += ["--set", "appNamespace=dfe-local"]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {sets}:\n{out.stderr}")
    return tuple(d for d in yaml.safe_load_all(out.stdout) if d)


def of_kind(docs: tuple[dict, ...], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def pods_of(docs: tuple[dict, ...]) -> list[Pod]:
    """Every pod Strimzi would build from the rendered Kafka and KafkaNodePool CRs.

    A pool pod carries the pool's template.pod.topologySpreadConstraints verbatim
    (WorkloadUtils.java:292); Strimzi adds none of its own.
    """
    (kafka,) = of_kind(docs, "Kafka")
    cluster = kafka["metadata"]["name"]
    pods: list[Pod] = []
    node_id = 0
    for pool in of_kind(docs, "KafkaNodePool"):
        meta, spec = pool["metadata"], pool["spec"]
        tsc = ((spec.get("template") or {}).get("pod") or {}).get("topologySpreadConstraints") or []
        for _ in range(int(spec["replicas"])):
            labels = pool_pod_labels(cluster, meta["name"], meta.get("labels") or {}, node_id,
                                     set(spec["roles"]))
            pods.append((labels["strimzi.io/pod-name"], labels, tsc))
            node_id += 1
    kafka_labels = kafka["metadata"].get("labels") or {}
    others = ["entity-operator"]
    if "cruiseControl" in kafka["spec"]:
        others.append("cruise-control")
    for kind in others:
        pods.append((f"{cluster}-{kind}", component_pod_labels(cluster, kafka_labels, kind), []))
    return pods


def peers(labels: dict[str, str], pods: list[Pod]) -> set[str]:
    """The pods one pool pod must be kept apart from: every pod sharing its role.

    A pool with the broker role spreads against every broker; a controller-only
    pool against every controller. A combined pool's brokers ARE its controllers.
    """
    role = ("strimzi.io/broker-role" if labels["strimzi.io/broker-role"] == "true"
            else "strimzi.io/controller-role")
    return {n for n, lab, _ in pods if lab.get(role) == "true"}


def check_spread(label: str, docs: tuple[dict, ...]) -> None:
    pods = pods_of(docs)
    pool_pods = [p for p in pods if "strimzi.io/pool-name" in p[1]]
    expect(f"{label}: the render has pool pods to check", len(pool_pods) > 1,
           f"got {len(pool_pods)}")
    for name, labels, constraints in pool_pods:
        c = hostname_constraint(label, name, constraints)
        if c is None:
            continue
        selector = c.get("labelSelector") or {}
        only_labels = set(selector) == {"matchLabels"} and not c.get("matchLabelKeys")
        expect(f"{label}: {name} selector is matchLabels only", only_labels,
               f"labelSelector {selector!r}, matchLabelKeys {c.get('matchLabelKeys')!r}")
        if only_labels:
            got = counted(selector["matchLabels"], pods)
            want = peers(labels, pods)
            expect(f"{label}: {name} spread counts exactly its peers", got == want,
                   f"selector {selector['matchLabels']} counts {sorted(got)}, "
                   f"peers are {sorted(want)}")
        assert_constraint_shape(label, name, c)


def test_fixture_matches_the_pinned_strimzi() -> None:
    """A fixture recorded for one operator is not evidence about the next."""
    doc = yaml.safe_load(VERSIONS.read_text(encoding="utf-8"))
    pinned = doc["stacks"][doc["current"]]["operators"]["strimzi-kafka-operator"]
    expect("Strimzi pin matches the version the label fixture was read from",
           pinned == STRIMZI_VERSION,
           f"versions.yaml pins {pinned}; re-read the pod labels at that tag and update "
           f"pool_pod_labels, component_pod_labels and STRIMZI_VERSION")


def test_combined_pool_spreads_its_brokers() -> None:
    """The chart default: one pool carrying both roles."""
    check_spread("combined", render())


def test_dedicated_pools_spread_apart() -> None:
    """controllerPool on: brokers count brokers, controllers count controllers."""
    check_spread("dedicated controllers", render(("kafka.controllerPool.enabled=true",)))


def test_the_other_strimzi_pods_are_in_the_count() -> None:
    """The exact-peers checks only prove exclusion if these pods are there to exclude."""
    names = {n for n, _, _ in pods_of(render())}
    for other in ("dfe-kafka-entity-operator", "dfe-kafka-cruise-control"):
        expect(f"the default render runs {other} beside the pool", other in names,
               f"pods {sorted(names)}")


def test_spread_off_renders_none() -> None:
    docs = render(("kafka.podSpread.enabled=false", "kafka.controllerPool.enabled=true"))
    for pool in of_kind(docs, "KafkaNodePool"):
        pod = ((pool["spec"].get("template") or {}).get("pod") or {})
        tsc = pod.get("topologySpreadConstraints")
        expect(f"podSpread.enabled=false renders no spread on {pool['metadata']['name']}",
               not tsc, f"got {tsc}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
