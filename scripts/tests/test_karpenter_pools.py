#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_karpenter_pools.py
#  Purpose:      Prove the optional minVcpu/minMemoryGib pool floor renders the
#                right Karpenter requirement, and that omitting it changes
#                nothing for every pool that does not set it, that a test
#                run's tags hold every pool to the run's instance sizes, and
#                that every pool bounds how long a drain waits.
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

import base64
import json
import re
import subprocess
import sys
import tempfile
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


def nodepool_run(
    pool: dict, tags: dict[str, str] | None = None, sizes: list[str] | None = None, extra: tuple[str, ...] = ()
) -> subprocess.CompletedProcess:
    """helm template of the NodePool for one pool named `clickhouse`, with run tags, sizes and `extra` when given."""
    cmd = ["helm", "template", "t", str(CHART), "--show-only", "templates/nodepool.yaml"]
    for s in BASE_SETS:
        cmd += ["--set", s]
    cmd += ["--set-json", f"karpenter.pools={json.dumps({'clickhouse': pool})}"]
    if tags is not None:
        cmd += ["--set-json", f"karpenter.tags={json.dumps(tags)}"]
    if sizes is not None:
        cmd += ["--set-json", f"karpenter.runInstanceSizes={json.dumps(sizes)}"]
    cmd += list(extra)
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def render(
    pool: dict, tags: dict[str, str] | None = None, sizes: list[str] | None = None, extra: tuple[str, ...] = ()
) -> dict:
    """The rendered NodePool for one pool named `clickhouse`."""
    out = nodepool_run(pool, tags, sizes, extra)
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


def nodeclass_run(tags: dict[str, str] | None, user_data: str | None = None) -> subprocess.CompletedProcess:
    """helm template of the EC2NodeClass for one pool, with karpenter.tags and the node script when given."""
    cmd = ["helm", "template", "t", str(CHART), "--show-only", "templates/ec2nodeclass.yaml"]
    for s in BASE_SETS:
        cmd += ["--set", s]
    cmd += ["--set-json", f"karpenter.pools={json.dumps({'clickhouse': BASE_POOL})}"]
    if tags is not None:
        cmd += ["--set-json", f"karpenter.tags={json.dumps(tags)}"]
    if user_data is not None:
        cmd += ["--set-string", f"karpenter.cluster.userData={user_data}"]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def render_nodeclass(tags: dict[str, str] | None, user_data: str | None = None) -> dict:
    """The rendered EC2NodeClass for one pool."""
    out = nodeclass_run(tags, user_data)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed:\n{out.stderr}")
    return yaml.safe_load(out.stdout)


NODE_SCRIPT = "#!/bin/bash\nset -euo pipefail\necho mirrors\n"


def test_the_registry_cache_script_reaches_every_node_class_decoded() -> None:
    """Karpenter takes a plain shell script in userData and merges its own NodeConfig after it."""
    encoded = base64.b64encode(NODE_SCRIPT.encode()).decode()
    spec = render_nodeclass(None, encoded)["spec"]
    expect("the node class carries the script as plain text", spec.get("userData") == NODE_SCRIPT, spec.get("userData"))


def test_no_registry_cache_renders_no_user_data() -> None:
    spec = render_nodeclass(None)["spec"]
    expect("an empty karpenter.cluster.userData renders no userData", "userData" not in spec, spec.get("userData"))


def appset_cluster_facts(annotations: dict[str, str]) -> dict:
    """karpenter.cluster as layer2-platform.yaml's values block renders it for one cluster secret.

    The block reads cluster facts at .metadata and .app, which a chart template reaches under .Values.
    """
    appset = REPO_ROOT / "argocd" / "appsets" / "layer2-platform.yaml"
    block = yaml.safe_load(appset.read_text(encoding="utf-8"))["spec"]["template"]["spec"]["sources"][0]["helm"]["values"]
    text = re.sub(r"(?<![\w$)\]])\.(metadata|app)\b", r".Values.\1", block)
    with tempfile.TemporaryDirectory() as tmp:
        chart = Path(tmp) / "block"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: block\nversion: 0.1.0\n", encoding="utf-8")
        (chart / "templates" / "values.yaml").write_text(text, encoding="utf-8")
        context = Path(tmp) / "context.yaml"
        context.write_text(
            yaml.safe_dump({"metadata": {"annotations": annotations}, "app": "karpenter-pools"}), encoding="utf-8"
        )
        out = subprocess.run(
            ["helm", "template", "t", str(chart), "-f", str(context)], capture_output=True, text=True, check=False
        )
    if out.returncode != 0:
        raise SystemExit(f"the values block does not render:\n{out.stderr}")
    return yaml.safe_load(out.stdout)["karpenter"]["cluster"]


def test_the_appset_hands_the_node_script_to_the_chart_unchanged() -> None:
    encoded = base64.b64encode(NODE_SCRIPT.encode()).decode()
    facts = appset_cluster_facts({"dfe.hyperi.io/node_user_data": encoded})
    expect("the annotation lands on karpenter.cluster.userData", facts.get("userData") == encoded, facts)
    facts = appset_cluster_facts({})
    expect("an older cluster secret renders an empty userData", facts.get("userData") == "", facts)


def test_expected_fail_a_script_that_is_not_base64_is_refused() -> None:
    """sprig's b64dec renders its own error text in place of a script it cannot decode."""
    out = nodeclass_run(None, "#!/bin/bash")
    expect("helm refuses the render", out.returncode != 0, out.stdout)
    expect("and names the value", "karpenter.cluster.userData is not base64" in out.stderr, out.stderr)


def test_run_tags_reach_every_instance_karpenter_launches() -> None:
    """default_tags never reaches what Karpenter creates, so a guardrail that
    refuses an untagged RunInstances needs the run's tags on the node class."""
    tags = {"dfe-e2e": "run-1", "expires-at": "2026-10-08T12:00:00Z"}
    spec = render_nodeclass(tags)["spec"]
    expect("the node class carries karpenter.tags", spec.get("tags") == tags, spec.get("tags"))


def test_no_run_tags_render_no_tags_field() -> None:
    spec = render_nodeclass(None)["spec"]
    expect("an empty karpenter.tags renders no tags field", "tags" not in spec, spec.get("tags"))


RUN_TAGS = {"dfe-e2e": "run-1", "expires-at": "2026-10-08T12:00:00Z"}
SIZE_KEY = "karpenter.k8s.aws/instance-size"


def test_a_test_run_holds_every_pool_to_the_run_sizes() -> None:
    """families + generation never bound the size, so without this Karpenter can
    pick one a test account's boundary denies at RunInstances."""
    reqs = requirements(render(BASE_POOL, tags=RUN_TAGS))
    sizes = [r for r in reqs if r["key"] == SIZE_KEY]
    expect("one instance-size requirement during a run", len(sizes) == 1, reqs)
    expect("it admits only the listed sizes", sizes[0]["operator"] == "In", sizes[0])
    expect("the chart default is medium to 2xlarge",
           sizes[0]["values"] == ["medium", "large", "xlarge", "2xlarge"], sizes[0])
    keys = {r["key"] for r in reqs}
    expect("family and generation still render beside it",
           {"karpenter.k8s.aws/instance-family", "karpenter.k8s.aws/instance-generation"} <= keys, keys)


def test_expected_fail_no_run_tags_bound_no_size() -> None:
    """Every deployment that is not a test run keeps the unbounded pool it had."""
    keys = {r["key"] for r in requirements(render(BASE_POOL))}
    expect("no instance-size requirement outside a run", SIZE_KEY not in keys, keys)


def test_a_deployment_overrides_the_run_sizes_or_turns_them_off() -> None:
    reqs = requirements(render(BASE_POOL, tags=RUN_TAGS, sizes=["large", "4xlarge"]))
    sizes = next(r for r in reqs if r["key"] == SIZE_KEY)
    expect("an overridden list reaches the requirement", sizes["values"] == ["large", "4xlarge"], sizes)
    keys = {r["key"] for r in requirements(render(BASE_POOL, tags=RUN_TAGS, sizes=[]))}
    expect("an empty list renders no instance-size requirement", SIZE_KEY not in keys, keys)


# spec.template.spec.terminationGracePeriod's pattern in the karpenter.sh_nodepools CRD
# at karpenter-provider-aws v1.14.1, the release versions.yaml pins.
GRACE_PATTERN = re.compile(r"^([0-9]+(s|m|h))+$")


def grace(doc: dict) -> str | None:
    return doc["spec"]["template"]["spec"].get("terminationGracePeriod")


def test_every_pool_bounds_its_drain_by_default() -> None:
    """Unset, a drain a PodDisruptionBudget or do-not-disrupt pod blocks never ends."""
    value = grace(render(BASE_POOL))
    expect("the chart default reaches the NodePool", value == "24h", value)
    expect("in the form the CRD admits", bool(GRACE_PATTERN.match(value or "")), value)


def test_a_deployment_or_a_pool_sets_its_own_drain_bound() -> None:
    chart_wide = ("--set", "karpenter.terminationGracePeriod=2h")
    value = grace(render(BASE_POOL, extra=chart_wide))
    expect("karpenter.terminationGracePeriod reaches every pool", value == "2h", value)
    own = grace(render({**BASE_POOL, "terminationGracePeriod": "15m"}, extra=chart_wide))
    expect("a pool's own value wins over the chart's", own == "15m", own)


def test_expected_fail_a_drain_bound_karpenter_would_refuse_fails_the_render() -> None:
    """The CRD refuses these at admission, which fails the whole pools sync."""
    for value in ("", "1d", "Never", "24"):
        out = nodepool_run(BASE_POOL, extra=("--set-string", f"karpenter.terminationGracePeriod={value}"))
        expect(f"{value!r} is refused", out.returncode != 0, out.stdout)
        expect(f"and named for {value!r}", "resolves terminationGracePeriod" in out.stderr, out.stderr)


def main() -> int:
    with standalone():
        test_every_pool_bounds_its_drain_by_default()
        test_a_deployment_or_a_pool_sets_its_own_drain_bound()
        test_expected_fail_a_drain_bound_karpenter_would_refuse_fails_the_render()
        test_no_floor_set_adds_no_requirement()
        test_min_vcpu_renders_a_gt_floor_one_below_the_stated_value()
        test_min_memory_gib_converts_to_mib_and_renders_a_gt_floor()
        test_both_floors_render_together_without_disturbing_family_or_generation()
        test_a_dedicated_pool_renders_its_taint_and_a_shared_one_renders_none()
        test_the_appset_carries_the_pools_through_from_the_cluster_secret()
        test_run_tags_reach_every_instance_karpenter_launches()
        test_no_run_tags_render_no_tags_field()
        test_the_registry_cache_script_reaches_every_node_class_decoded()
        test_no_registry_cache_renders_no_user_data()
        test_the_appset_hands_the_node_script_to_the_chart_unchanged()
        test_expected_fail_a_script_that_is_not_base64_is_refused()
        test_a_test_run_holds_every_pool_to_the_run_sizes()
        test_expected_fail_no_run_tags_bound_no_size()
        test_a_deployment_overrides_the_run_sizes_or_turns_them_off()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
