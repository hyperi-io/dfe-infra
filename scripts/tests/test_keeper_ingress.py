#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_keeper_ingress.py
#  Purpose:      Prove cluster-mode Keeper takes its ingress from the operator's own
#                NetworkPolicy alone: the KeeperCluster turns that policy on, and
#                data-ingress-policy selects every data-namespace pod except Keeper.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Who can reach cluster-mode Keeper.

Keeper's client port takes a create at the znode root from any client that reaches it,
and policies are additive. While data-ingress-policy selects the keeper pods, every DFE,
data and operator namespace reaches every port on them, whatever a narrower policy says.

So the KeeperCluster turns on the operator's managed policy, which admits 2181, 2281 and
9123 from the operator and the ClickHouse clusters referencing the keeper, and raft 9234
from the other keepers. data-ingress-policy leaves the keeper pods out by the role label
the operator writes, which the fixture below ties to the operator pin in versions.yaml.

    python3 scripts/tests/test_keeper_ingress.py

Needs `helm` on PATH.
"""

import functools
import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
VERSIONS = REPO_ROOT / "versions.yaml"

CLUSTER_PROFILES = ("scale", "mesh")
CLOUDS = ("local", "aws")

# The role label clickhouse-operator v0.0.8 writes on the pods it builds
# (internal/controllerutil/labels.go:17 and :26-28).
OPERATOR_VERSION = "0.0.8"
ROLE = "clickhouse.com/role"
KEEPER_POD = {ROLE: "clickhouse-keeper", "app": "dfe-keeper-keeper"}
SERVER_POD = {ROLE: "clickhouse-server", "app": "dfe-clickhouse-clickhouse"}
# cnpg and strimzi pods carry no ClickHouse role at all.
UNROLED_POD: dict[str, str] = {}


def run(cmd: list[str]) -> tuple[dict, ...]:
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"{' '.join(cmd[:3])} failed:\n{out.stderr}")
    return tuple(d for d in yaml.safe_load_all(out.stdout) if d)


@functools.cache
def render_clickhouse(profile: str, cloud: str) -> tuple[dict, ...]:
    cmd = ["helm", "template", "clickhouse-cluster", str(chart_dir("clickhouse-cluster"))]
    for f in ("common.yaml", f"{cloud}.yaml", f"profile-{profile}.yaml"):
        cmd += ["-f", str(VALUES / f)]
    return run([*cmd, "--set", "appNamespace=dfe-local"])


def render_policies(*sets: str) -> tuple[dict, ...]:
    cmd = ["helm", "template", "network-policies", str(chart_dir("network-policies"))]
    cmd += ["-f", str(VALUES / "common.yaml")]
    for s in sets:
        cmd += ["--set", s]
    return run(cmd)


def data_ingress(docs: tuple[dict, ...]) -> dict[str, dict]:
    """data-ingress-policy, keyed by the namespace it renders in."""
    return {
        d["metadata"]["namespace"]: d
        for d in docs
        if d.get("kind") == "NetworkPolicy" and d["metadata"]["name"] == "data-ingress-policy"
    }


def selects(selector: dict, labels: dict[str, str]) -> bool:
    """Whether a NetworkPolicy podSelector selects a pod carrying these labels."""
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for term in selector.get("matchExpressions") or []:
        key, values = term["key"], set(term.get("values") or [])
        match term["operator"]:
            case "In":
                matched = labels.get(key) in values
            case "NotIn":
                matched = labels.get(key) not in values
            case "Exists":
                matched = key in labels
            case "DoesNotExist":
                matched = key not in labels
            case other:
                raise SystemExit(f"selector operator {other!r} is not modelled here")
        if not matched:
            return False
    return True


def test_fixture_matches_the_pinned_operator() -> None:
    """A fixture recorded for one operator is not evidence about the next."""
    doc = yaml.safe_load(VERSIONS.read_text(encoding="utf-8"))
    pinned = doc["stacks"][doc["current"]]["operators"]["clickhouse-operator"]
    expect(
        "operator pin matches the version the role labels were read from",
        pinned == OPERATOR_VERSION,
        f"versions.yaml pins {pinned}; re-read the role labels and the keeper NetworkPolicy "
        "(internal/controller/keeper/templates.go) at that tag",
    )


def test_cluster_mode_keeper_takes_the_operators_policy() -> None:
    for profile in CLUSTER_PROFILES:
        for cloud in CLOUDS:
            keepers = [d for d in render_clickhouse(profile, cloud) if d.get("kind") == "KeeperCluster"]
            expect(f"{profile}/{cloud}: one KeeperCluster renders", len(keepers) == 1, f"got {len(keepers)}")
            policy = (keepers[0]["spec"].get("networkPolicy") or {}).get("policy") if keepers else None
            expect(
                f"{profile}/{cloud}: the KeeperCluster turns on the operator's NetworkPolicy",
                policy == "Enabled",
                f"got {policy!r}",
            )


def test_data_ingress_leaves_keeper_to_the_operator() -> None:
    policies = data_ingress(render_policies())
    expect("data-ingress-policy renders in the clickhouse namespace", "clickhouse" in policies, f"got {sorted(policies)}")
    for ns, policy in sorted(policies.items()):
        selector = policy["spec"]["podSelector"]
        expect(f"{ns}: data-ingress-policy leaves the keeper pods out", not selects(selector, KEEPER_POD), f"selector {selector}")
        expect(f"{ns}: data-ingress-policy still selects the ClickHouse servers", selects(selector, SERVER_POD), f"selector {selector}")
        expect(f"{ns}: data-ingress-policy still selects a pod with no ClickHouse role", selects(selector, UNROLED_POD), f"selector {selector}")


def test_an_empty_opt_out_selects_every_pod() -> None:
    for ns, policy in sorted(data_ingress(render_policies("operatorIngressScoped=null")).items()):
        selector = policy["spec"]["podSelector"]
        expect(f"{ns}: with the opt-out empty, data-ingress-policy selects every pod", selector == {}, f"selector {selector}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
