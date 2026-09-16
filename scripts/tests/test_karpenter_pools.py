#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_karpenter_pools.py
#  Purpose:      Prove the optional minVcpu/minMemoryGib pool floor renders the
#                right Karpenter requirement, and that omitting it changes
#                nothing for every pool that does not set it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""karpenter.pools.<name>.{minVcpu,minMemoryGib} -- gate-3-correctness.md P3.

families + generation bound WHICH instance line Karpenter may launch, never
which SIZE on it: a workload sized up for its volume's EBS throughput baseline
still satisfies a smaller instance's cpu/memory REQUEST, which is all
Karpenter's own bin-packing looks at. minVcpu/minMemoryGib close that gap.

    python3 scripts/tests/test_karpenter_pools.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "karpenter-pools"

# The cluster facts and root-volume fields validate.yaml refuses to render
# without -- fixed here so every case below only has to state the pool.
BASE_SETS = (
    "karpenter.cluster.discoveryTag=dfe-test",
    "karpenter.cluster.instanceProfile=dfe-test",
    "karpenter.cluster.kmsKeyId=arn:aws:kms:us-west-2:000000000000:key/abc",
    "karpenter.nodeClass.rootVolume.sizeGi=40",
    "karpenter.nodeClass.rootVolume.iops=3000",
    "karpenter.nodeClass.rootVolume.throughputMibS=125",
)

BASE_POOL = {
    "families": ["r9gd"],
    "arch": "arm64",
    "capacityTypes": ["on-demand"],
    "consolidation": {"policy": "WhenEmpty", "after": "10m"},
    "budgetNodes": "1",
    "expireAfter": "Never",
    "limits": {"cpu": "32", "memory": "128Gi"},
    "generationGt": "7",
}


def render(pool: dict) -> dict:
    """The rendered NodePool for one pool named `clickhouse`."""
    cmd = ["helm", "template", "t", str(CHART), "--show-only", "templates/nodepool.yaml"]
    for s in BASE_SETS:
        cmd += ["--set", s]
    cmd += ["--set-json", f"karpenter.pools={json.dumps({'clickhouse': pool})}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed:\n{out.stderr}")
    return yaml.safe_load(out.stdout)


def requirements(doc: dict) -> list[dict]:
    return doc["spec"]["template"]["spec"]["requirements"]


def test_no_floor_set_adds_no_requirement() -> None:
    reqs = requirements(render(BASE_POOL))
    keys = {r["key"] for r in reqs}
    expect(
        "no instance-cpu requirement without minVcpu",
        "karpenter.k8s.aws/instance-cpu" not in keys,
        f"got {sorted(keys)}",
    )
    expect(
        "no instance-memory requirement without minMemoryGib",
        "karpenter.k8s.aws/instance-memory" not in keys,
        f"got {sorted(keys)}",
    )


def test_min_vcpu_renders_a_gt_floor_one_below_the_stated_value() -> None:
    reqs = requirements(render({**BASE_POOL, "minVcpu": 16}))
    cpu = next(r for r in reqs if r["key"] == "karpenter.k8s.aws/instance-cpu")
    expect("the operator is Gt", cpu["operator"] == "Gt", cpu)
    expect("Gt 15 expresses >= 16 (Karpenter has no Ge)", cpu["values"] == ["15"], cpu)


def test_min_memory_gib_converts_to_mib_and_renders_a_gt_floor() -> None:
    reqs = requirements(render({**BASE_POOL, "minMemoryGib": 128}))
    mem = next(r for r in reqs if r["key"] == "karpenter.k8s.aws/instance-memory")
    expect("the operator is Gt", mem["operator"] == "Gt", mem)
    expect("128 GiB - 1 MiB, in MiB", mem["values"] == [str(128 * 1024 - 1)], mem)


def test_both_floors_render_together_without_disturbing_family_or_generation() -> None:
    reqs = requirements(render({**BASE_POOL, "minVcpu": 8, "minMemoryGib": 32}))
    keys = [r["key"] for r in reqs]
    expect(
        "family, generation and both floors all render",
        {
            "karpenter.k8s.aws/instance-family",
            "karpenter.k8s.aws/instance-generation",
            "karpenter.k8s.aws/instance-cpu",
            "karpenter.k8s.aws/instance-memory",
        }
        <= set(keys),
        f"got {keys}",
    )


def test_a_dedicated_pool_renders_its_taint_and_a_shared_one_renders_none() -> None:
    """The resolver taints the dedicated pools; the chart is what puts the taint
    on the node Karpenter launches."""
    taint = {"key": "dfe.hyperi.io/workload", "value": "clickhouse", "effect": "NoSchedule"}
    tainted = render({**BASE_POOL, "taints": [taint]})["spec"]["template"]["spec"]
    expect("a pool's taint reaches the NodePool", tainted.get("taints") == [taint], tainted)
    shared = render(BASE_POOL)["spec"]["template"]["spec"]
    expect("a pool with no taint renders none", "taints" not in shared, shared)


def test_the_appset_carries_the_pools_through_from_the_cluster_secret() -> None:
    """A chart whose `pools` stays at its own empty default renders no NodePool,
    so every workload the managed groups cannot fit stays Pending."""
    appset = REPO_ROOT / "argocd" / "appsets" / "layer2-platform.yaml"
    doc = yaml.safe_load(appset.read_text(encoding="utf-8"))
    block = doc["spec"]["template"]["spec"]["sources"][0]["helm"]["values"]
    expect("the appset reads the karpenter_pools annotation",
           'dfe.hyperi.io/karpenter_pools' in block, block)
    expect("and lands it on karpenter.pools",
           "$pools | fromJson | toYaml | nindent" in block, block)
    expect("parsed rather than spliced, so a non-JSON annotation fails the render",
           "pools: {{ $pools }}" not in block, block)
    expect("only for the karpenter-pools app", 'eq .app "karpenter-pools"' in block, block)
    # The nindent is relative to this values STRING, not to the appset file, so
    # prove the emitted block parses back as the chart's own `pools` map.
    indent = int(block.split("$pools | fromJson | toYaml | nindent ", 1)[1].split()[0].rstrip("}- "))
    pools = {"clickhouse": BASE_POOL}
    body = yaml.safe_dump(pools, default_flow_style=False, sort_keys=True).rstrip("\n")
    emitted = "\n".join(" " * indent + line for line in body.splitlines())
    parsed = yaml.safe_load(f"karpenter:\n  cluster:\n    discoveryTag: t\n  pools:\n{emitted}\n")
    expect("the pool map parses back at that indent", parsed["karpenter"]["pools"] == pools, parsed)


def main() -> int:
    with standalone():
        test_no_floor_set_adds_no_requirement()
        test_min_vcpu_renders_a_gt_floor_one_below_the_stated_value()
        test_min_memory_gib_converts_to_mib_and_renders_a_gt_floor()
        test_both_floors_render_together_without_disturbing_family_or_generation()
        test_a_dedicated_pool_renders_its_taint_and_a_shared_one_renders_none()
        test_the_appset_carries_the_pools_through_from_the_cluster_secret()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
