#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_clickhouse_replica_spread.py
#  Purpose:      Prove the ClickHouse and Keeper spread constraints select the
#                pods the operator actually labels: a server counts its own
#                shard's replicas, a keeper counts the keepers and nothing else.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The replica spread has to count the pods it is meant to keep apart.

The operator builds the server and keeper pods, so a spread constraint can only
select on the labels the operator writes. A selector built from the chart's own
labels parses, renders, passes a dry-run and counts zero pods: the scheduler
never sees a skew, and two replicas of one shard can land on one node.

So this test holds the labels the pinned operator writes, evaluates each rendered
selector the way the scheduler does -- matchLabels plus the incoming pod's own
value for each matchLabelKeys key -- and asserts it selects exactly the pods it
must keep apart. The fixture is tied to the operator pin in versions.yaml, so an
operator bump fails here until someone re-reads its labels.

    python3 scripts/tests/test_clickhouse_replica_spread.py

Needs `helm` on PATH.
"""

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

# The two profiles that run the operator; the rest render single mode.
CLUSTER_PROFILES = ("scale", "mesh")

# --- fixture: what the ClickHouse operator writes on the pods it builds -------
# Recorded from github.com/ClickHouse/clickhouse-operator at tag v0.0.7, the
# source behind the pinned OCI chart clickhouse-operator-helm 0.0.7.
OPERATOR_VERSION = "0.0.7"


def server_pod_labels(cluster: str, shard: int, replica: int) -> dict[str, str]:
    """Labels on a ClickHouseCluster server pod.

    internal/controller/clickhouse/templates.go:246-251 merges spec.labels, the
    replica id labels (api/v1alpha1/clickhousecluster_types.go:363-368) and these
    four, later maps winning; SpecificName() is <name>-clickhouse (line 404-406).
    """
    specific = f"{cluster}-clickhouse"
    return {
        "clickhouse.com/shard-id": str(shard),
        "clickhouse.com/replica-id": str(replica),
        "app": specific,
        "app.kubernetes.io/instance": specific,
        "clickhouse.com/role": "clickhouse-server",
        "app.kubernetes.io/name": "clickhouse-server",
    }


def keeper_pod_labels(keeper: str, replica: int) -> dict[str, str]:
    """Labels on a KeeperCluster pod.

    internal/controller/keeper/templates.go:278-282 merges spec.labels,
    replicaLabels() (line 337-341: the replica id from
    api/v1alpha1/keepercluster_types.go:221-225, plus `app`) and these three;
    SpecificName() is <name>-keeper (keepercluster_types.go:250-252).
    """
    specific = f"{keeper}-keeper"
    return {
        "clickhouse.com/keeper-replica-id": str(replica),
        "app": specific,
        "clickhouse.com/role": "clickhouse-keeper",
        "app.kubernetes.io/name": "clickhouse-keeper",
        "app.kubernetes.io/instance": specific,
    }


# --- render -------------------------------------------------------------------
@cache
def render(profile: str, sets: tuple[str, ...] = ()) -> tuple[dict, ...]:
    """The chart under the appset's value cascade for one profile."""
    cmd = ["helm", "template", "clickhouse-cluster", str(chart_dir("clickhouse-cluster"))]
    for f in ("common.yaml", "local.yaml", f"profile-{profile}.yaml"):
        cmd += ["-f", str(VALUES / f)]
    cmd += ["--set", "appNamespace=dfe-local"]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {profile} {sets}:\n{out.stderr}")
    return tuple(d for d in yaml.safe_load_all(out.stdout) if d)


def one(docs: tuple[dict, ...], kind: str) -> dict:
    matches = [d for d in docs if d.get("kind") == kind]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one {kind}, got {len(matches)}")
    return matches[0]


def pods_of(docs: tuple[dict, ...]) -> list[Pod]:
    """Every pod the operator would build from the rendered CRs.

    The constraints are the CR's podTemplate ones verbatim: the operator adds its
    own only when podTemplate.topologyZoneKey or nodeHostnameKey is set
    (clickhouse/templates.go:356-426, keeper/templates.go:419-478), which
    check_spread() refuses.
    """
    chc = one(docs, "ClickHouseCluster")
    kc = one(docs, "KeeperCluster")
    pods = []
    spec = chc["spec"]
    tsc = (spec.get("podTemplate") or {}).get("topologySpreadConstraints") or []
    for shard in range(int(spec["shards"])):
        for replica in range(int(spec["replicas"])):
            labels = {**(spec.get("labels") or {}),
                      **server_pod_labels(chc["metadata"]["name"], shard, replica)}
            pods.append((f"server {shard}-{replica}", labels, tsc))
    kspec = kc["spec"]
    ktsc = (kspec.get("podTemplate") or {}).get("topologySpreadConstraints") or []
    for replica in range(int(kspec["replicas"])):
        labels = {**(kspec.get("labels") or {}),
                  **keeper_pod_labels(kc["metadata"]["name"], replica)}
        pods.append((f"keeper {replica}", labels, ktsc))
    return pods


def effective_selector(constraint: dict, incoming: dict[str, str]) -> dict[str, str] | None:
    """The labels a constraint counts, as the scheduler sees them for one pod.

    matchLabels, plus the incoming pod's value for each matchLabelKeys key it
    carries (the API server merges these into labelSelector at pod creation).
    None for a selector shape this evaluator does not model, which the caller
    reports rather than passing.
    """
    selector = constraint.get("labelSelector") or {}
    if set(selector) - {"matchLabels"}:
        return None
    merged = dict(selector.get("matchLabels") or {})
    for key in constraint.get("matchLabelKeys") or []:
        if key in incoming:
            merged[key] = incoming[key]
    return merged


def peers(pod: str, labels: dict[str, str], pods: list[Pod]) -> set[str]:
    """The pods one pod must be kept apart from: its own shard, or every keeper."""
    if pod.startswith("server"):
        shard = labels["clickhouse.com/shard-id"]
        return {n for n, lab, _ in pods
                if n.startswith("server") and lab["clickhouse.com/shard-id"] == shard}
    return {n for n, _, _ in pods if n.startswith("keeper")}


def check_spread(label: str, docs: tuple[dict, ...]) -> None:
    for kind in ("ClickHouseCluster", "KeeperCluster"):
        template = one(docs, kind)["spec"].get("podTemplate") or {}
        own = sorted({"topologyZoneKey", "nodeHostnameKey"} & set(template))
        expect(f"{label}: the {kind} asks the operator for no spread of its own", not own,
               f"podTemplate sets {own}, so the operator adds constraints this test does not see")
    pods = pods_of(docs)
    for name, labels, constraints in pods:
        c = hostname_constraint(label, name, constraints)
        if c is None:
            continue
        selector = effective_selector(c, labels)
        expect(f"{label}: {name} selector is matchLabels only", selector is not None,
               f"labelSelector {c.get('labelSelector')!r}")
        if selector is not None:
            got = counted(selector, pods)
            want = peers(name, labels, pods)
            expect(f"{label}: {name} spread counts exactly its peers", got == want,
                   f"selector {selector} counts {sorted(got)}, peers are {sorted(want)}")
        assert_constraint_shape(label, name, c)


def test_fixture_matches_the_pinned_operator() -> None:
    """A fixture recorded for one operator is not evidence about the next."""
    doc = yaml.safe_load(VERSIONS.read_text(encoding="utf-8"))
    pinned = doc["stacks"][doc["current"]]["operators"]["clickhouse-operator"]
    expect("operator pin matches the version the label fixture was read from",
           pinned == OPERATOR_VERSION,
           f"versions.yaml pins {pinned}; re-read the pod labels at that tag and update "
           f"server_pod_labels, keeper_pod_labels and OPERATOR_VERSION")


def test_cluster_profiles_spread_what_the_operator_labels() -> None:
    for profile in CLUSTER_PROFILES:
        check_spread(f"profile-{profile}", render(profile))


def test_each_shard_spreads_on_its_own() -> None:
    """Two shards: a server constraint counts its own shard, never the other one."""
    check_spread("two shards", render("scale", ("clickhouse.shardsCount=2",)))


def test_spread_off_renders_none() -> None:
    docs = render("scale", ("clickhouse.podSpread.enabled=false",))
    for kind in ("ClickHouseCluster", "KeeperCluster"):
        spec = one(docs, kind)["spec"]
        tsc = (spec.get("podTemplate") or {}).get("topologySpreadConstraints")
        expect(f"podSpread.enabled=false renders no spread on the {kind}", not tsc, f"got {tsc}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
