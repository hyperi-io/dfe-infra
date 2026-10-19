#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_node_pools.py
#  Purpose:      Prove the optional minVcpu/minMemoryGib pool floor renders the
#                right Karpenter requirement, and that omitting it changes
#                nothing for every pool that does not set it, that a test
#                run's tags hold every pool to the run's instance sizes, that
#                every pool bounds how long a drain waits, that its
#                disruption budgets render in the form the CRD takes, and that
#                the chart refuses a renderer, a class shape or a nested key it
#                does not carry, and a Karpenter cluster with no classes.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""pools.classes.<name>.{minVcpu,minMemoryGib}, and the karpenter-aws renderer around them.

families + generation bound WHICH instance line Karpenter may launch, never
which SIZE on it: a workload sized up for its volume's EBS throughput baseline
still satisfies a smaller instance's cpu/memory REQUEST, which is all
Karpenter's own bin-packing looks at. minVcpu/minMemoryGib close that gap.

    python3 scripts/tests/test_node_pools.py

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
CHART = REPO_ROOT / "helm" / "charts" / "node-pools"
NODEPOOL = "templates/karpenter/nodepool.yaml"
NODECLASS = "templates/karpenter/ec2nodeclass.yaml"

# The renderer, the cluster facts and the root-volume fields the guards refuse
# to render without -- fixed here so every case below only has to state the pool.
BASE_SETS = (
    "pools.renderer=karpenter-aws",
    "pools.aws.cluster.discoveryTag=dfe-test",
    "pools.aws.cluster.instanceProfile=dfe-test",
    "pools.aws.cluster.kmsKeyId=arn:aws:kms:us-west-2:000000000000:key/abc",
    "pools.aws.nodeClass.rootVolume.sizeGi=40",
    "pools.aws.nodeClass.rootVolume.iops=3000",
    "pools.aws.nodeClass.rootVolume.throughputMibS=125",
)

BASE_POOL = {
    "arch": "arm64",
    "capacityTypes": ["on-demand"],
    "consolidation": {"policy": "WhenEmpty", "after": "10m"},
    "budgets": [
        {"nodes": "0", "reasons": ["Underutilized"]},
        {"nodes": "1", "reasons": ["Empty"]},
        {"nodes": "1", "reasons": ["Drifted"]},
    ],
    "expireAfter": "Never",
    "limits": {"cpu": "32", "memory": "128Gi"},
    "aws": {"families": ["r9gd"], "generationGt": "7"},
}


def helm(cmd: list[str]) -> subprocess.CompletedProcess:
    """One helm invocation, its output read as UTF-8."""
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def nodepool_run(
    pool: dict, tags: dict[str, str] | None = None, sizes: list[str] | None = None, extra: tuple[str, ...] = ()
) -> subprocess.CompletedProcess:
    """helm template of the NodePool for one pool named `clickhouse`, with run tags, sizes and `extra` when given."""
    cmd = ["helm", "template", "t", str(CHART), "--show-only", NODEPOOL]
    for s in BASE_SETS:
        cmd += ["--set", s]
    cmd += ["--set-json", f"pools.classes={json.dumps({'clickhouse': pool})}"]
    if tags is not None:
        cmd += ["--set-json", f"pools.aws.tags={json.dumps(tags)}"]
    if sizes is not None:
        cmd += ["--set-json", f"pools.aws.runInstanceSizes={json.dumps(sizes)}"]
    cmd += list(extra)
    return helm(cmd)


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
    expect("Gt 15 expresses >= 16", cpu["values"] == ["15"], cpu)


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


def appset_values_block() -> str:
    """The `values` string layer2-platform.yaml hands every chart it deploys."""
    appset = REPO_ROOT / "argocd" / "appsets" / "layer2-platform.yaml"
    doc = yaml.safe_load(appset.read_text(encoding="utf-8"))
    return doc["spec"]["template"]["spec"]["sources"][0]["helm"]["values"]


def test_the_appset_carries_the_pools_through_from_the_cluster_secret() -> None:
    """A chart whose `classes` stays at its own empty default renders no NodePool,
    so every workload the managed groups cannot fit stays Pending."""
    block = appset_values_block()
    expect("the appset reads the node_pools annotation",
           'dfe.hyperi.io/node_pools' in block, block)
    expect("and lands it on the pools root",
           "with $pools | fromJson" in block and "toYaml . | nindent" in block, block)
    expect("parsed rather than spliced, so a non-JSON annotation fails the render",
           "pools: {{ $pools }}" not in block, block)
    expect("only for the karpenter-pools app", 'eq .app "karpenter-pools"' in block, block)
    # The nindent is relative to this values STRING, not to the appset file, so
    # prove the emitted block parses back beside pools.aws.
    indent = int(block.split("toYaml . | nindent ", 1)[1].split()[0].rstrip("}- "))
    pools = {"classes": {"clickhouse": BASE_POOL}, "renderer": "karpenter-aws"}
    body = yaml.safe_dump(pools, default_flow_style=False, sort_keys=True).rstrip("\n")
    emitted = "\n".join(" " * indent + line for line in body.splitlines())
    parsed = yaml.safe_load(f"pools:\n  aws:\n    cluster:\n      discoveryTag: t\n{emitted}\n")
    expect("the renderer and classes parse back at that indent",
           {k: v for k, v in parsed["pools"].items() if k != "aws"} == pools, parsed)


def nodeclass_run(tags: dict[str, str] | None, user_data: str | None = None) -> subprocess.CompletedProcess:
    """helm template of the EC2NodeClass for one pool, with pools.aws.tags and the node script when given."""
    cmd = ["helm", "template", "t", str(CHART), "--show-only", NODECLASS]
    for s in BASE_SETS:
        cmd += ["--set", s]
    cmd += ["--set-json", f"pools.classes={json.dumps({'clickhouse': BASE_POOL})}"]
    if tags is not None:
        cmd += ["--set-json", f"pools.aws.tags={json.dumps(tags)}"]
    if user_data is not None:
        cmd += ["--set-string", f"pools.aws.cluster.userData={user_data}"]
    return helm(cmd)


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
    expect("an empty pools.aws.cluster.userData renders no userData", "userData" not in spec, spec.get("userData"))


def appset_values(annotations: dict[str, str]) -> dict:
    """layer2-platform.yaml's values block, rendered for the karpenter-pools app on one cluster secret.

    The block reads cluster facts at .metadata and .app, which a chart template reaches under .Values.
    """
    text = re.sub(r"(?<![\w$)\]])\.(metadata|app)\b", r".Values.\1", appset_values_block())
    with tempfile.TemporaryDirectory() as tmp:
        chart = Path(tmp) / "block"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: block\nversion: 0.1.0\n", encoding="utf-8")
        (chart / "templates" / "values.yaml").write_text(text, encoding="utf-8")
        context = Path(tmp) / "context.yaml"
        context.write_text(
            yaml.safe_dump({"metadata": {"annotations": annotations}, "app": "karpenter-pools"}), encoding="utf-8"
        )
        out = helm(["helm", "template", "t", str(chart), "-f", str(context)])
    if out.returncode != 0:
        raise SystemExit(f"the values block does not render:\n{out.stderr}")
    return yaml.safe_load(out.stdout)


def test_the_appset_hands_the_node_script_to_the_chart_unchanged() -> None:
    encoded = base64.b64encode(NODE_SCRIPT.encode()).decode()
    facts = appset_values({"dfe.hyperi.io/node_user_data": encoded})["pools"]["aws"]["cluster"]
    expect("the annotation lands on pools.aws.cluster.userData", facts.get("userData") == encoded, facts)
    facts = appset_values({})["pools"]["aws"]["cluster"]
    expect("an older cluster secret renders an empty userData", facts.get("userData") == "", facts)


def test_the_appset_puts_the_run_tags_and_the_resolved_pools_where_the_chart_reads_them() -> None:
    pools = {"renderer": "karpenter-aws", "classes": {"clickhouse": BASE_POOL}}
    tags = {"dfe-e2e": "run-1", "expires-at": "2026-10-08T12:00:00Z"}
    rendered = appset_values(
        {"dfe.hyperi.io/node_pools": json.dumps(pools), "dfe.hyperi.io/run_tags": json.dumps(tags)}
    )["pools"]
    expect("the run tags land on pools.aws.tags", rendered["aws"].get("tags") == tags, rendered)
    expect("the renderer and classes land on the pools root",
           {k: rendered[k] for k in ("renderer", "classes")} == pools, rendered)


def test_expected_fail_a_script_that_is_not_base64_is_refused() -> None:
    """sprig's b64dec renders its own error text in place of a script it cannot decode."""
    out = nodeclass_run(None, "#!/bin/bash")
    expect("helm refuses the render", out.returncode != 0, out.stdout)
    expect("and names the value", "pools.aws.cluster.userData is not base64" in out.stderr, out.stderr)


def test_run_tags_reach_every_instance_karpenter_launches() -> None:
    """default_tags never reaches what Karpenter creates, so a guardrail that
    refuses an untagged RunInstances needs the run's tags on the node class."""
    tags = {"dfe-e2e": "run-1", "expires-at": "2026-10-08T12:00:00Z"}
    spec = render_nodeclass(tags)["spec"]
    expect("the node class carries pools.aws.tags", spec.get("tags") == tags, spec.get("tags"))


def test_no_run_tags_render_no_tags_field() -> None:
    spec = render_nodeclass(None)["spec"]
    expect("an empty pools.aws.tags renders no tags field", "tags" not in spec, spec.get("tags"))


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
    chart_wide = ("--set", "pools.terminationGracePeriod=2h")
    value = grace(render(BASE_POOL, extra=chart_wide))
    expect("pools.terminationGracePeriod reaches every pool", value == "2h", value)
    own = grace(render({**BASE_POOL, "terminationGracePeriod": "15m"}, extra=chart_wide))
    expect("a pool's own value wins over the chart's", own == "15m", own)


def test_expected_fail_a_drain_bound_karpenter_would_refuse_fails_the_render() -> None:
    """The CRD refuses these at admission, which fails the whole pools sync."""
    for value in ("", "1d", "Never", "24"):
        out = nodepool_run(BASE_POOL, extra=("--set-string", f"pools.terminationGracePeriod={value}"))
        expect(f"{value!r} is refused", out.returncode != 0, out.stdout)
        expect(f"and named for {value!r}", "resolves terminationGracePeriod" in out.stderr, out.stderr)


# A stateful pool's budgets with a weekly window's drift hold after them.
WINDOWED_BUDGETS = [
    *BASE_POOL["budgets"],
    {"nodes": "0", "reasons": ["Drifted"], "schedule": "0 6 * * 6", "duration": "164h"},
]


def budgets(doc: dict) -> list[dict]:
    return doc["spec"]["disruption"]["budgets"]


def test_every_budget_renders_in_order_with_its_reasons_and_schedule() -> None:
    rendered = budgets(render({**BASE_POOL, "budgets": WINDOWED_BUDGETS}))
    expect("the budgets reach the NodePool as written", rendered == WINDOWED_BUDGETS, rendered)


def test_a_count_written_as_a_number_renders_as_the_string_the_crd_takes() -> None:
    """The CRD types nodes as a string, so a bare 1 in a hand-written overlay is refused at admission."""
    rendered = budgets(render({**BASE_POOL, "budgets": [{"nodes": 1}]}))
    expect("a reason-blind count renders as a string", rendered == [{"nodes": "1"}], rendered)


# Each case is one budget list the NodePool CRD or Karpenter's own reading would
# get wrong, with the phrase the guard names it by.
REFUSED_BUDGETS = (
    ([], "budgets is empty"),
    ([{"nodes": "110%"}], "budgets[0].nodes is"),
    ([{"nodes": "ten"}], "budgets[0].nodes is"),
    ([{"reasons": ["Drifted"]}], "budgets[0].nodes is"),
    ([{"nodes": "1", "reasons": ["Expired"]}], "budgets[0] names reason \"Expired\""),
    ([{"nodes": "1"}, {"nodes": "0", "schedule": "0 6 * * *"}], "budgets[1] sets schedule"),
    ([{"nodes": "1"}, {"nodes": "0", "duration": "20h"}], "budgets[1] sets duration"),
    ([{"nodes": "1"}, {"nodes": "0", "schedule": "0 6 * * *", "duration": "1d"}], "budgets[1].duration is"),
    (
        [
            {"nodes": "0", "reasons": ["Underutilized"]},
            {"nodes": "1", "reasons": ["Empty"]},
            {"nodes": "0", "reasons": ["Drifted"], "schedule": "0 6 * * 6", "duration": "164h"},
        ],
        "no always-on budget for Drifted",
    ),
    ([{"nodes": "1", "reason": ["Empty"]}], "budgets[0].reason is not a budget field"),
    ({"nodes": "1"}, "budgets is not a list"),
)


def test_expected_fail_a_budget_list_karpenter_would_misread_fails_the_render() -> None:
    for pool_budgets, said in REFUSED_BUDGETS:
        out = nodepool_run({**BASE_POOL, "budgets": pool_budgets})
        expect(f"{pool_budgets!r} is refused", out.returncode != 0, out.stdout)
        expect(f"and named for {pool_budgets!r}", said in out.stderr, out.stderr)


def test_no_classes_render_nothing_whatever_the_renderer() -> None:
    """A cloud with no pools leaves the classes empty, and the chart renders no object for it."""
    for sets in ((), ("--set", "pools.renderer=karpenter-aws")):
        out = helm(["helm", "template", "t", str(CHART), *sets])
        expect(f"the chart renders with {sets or 'its defaults'}", out.returncode == 0, out.stderr)
        docs = [d for d in yaml.safe_load_all(out.stdout) if d]
        expect(f"and renders no object with {sets or 'its defaults'}", docs == [], docs)


def test_expected_fail_a_karpenter_cluster_with_no_classes_fails_the_render() -> None:
    """Argo prunes what a render leaves out, so no classes beside the cluster facts would delete every NodePool."""
    for sets in (
        ("--set", "pools.aws.cluster.discoveryTag=dfe-test"),
        ("--set", "pools.aws.cluster.discoveryTag=dfe-test", "--set", "pools.renderer=karpenter-aws"),
    ):
        out = helm(["helm", "template", "t", str(CHART), *sets])
        expect(f"the render fails with {sets}", out.returncode != 0, out.stdout)
        expect(f"and names the annotation the classes arrive on, with {sets}",
               "dfe.hyperi.io/node_pools" in out.stderr, out.stderr)


# The three Karpenter facts bootstrap.sh writes on every AWS cluster secret.
CLUSTER_FACTS = {
    "dfe.hyperi.io/karpenter_discovery_tag": "dfe-test",
    "dfe.hyperi.io/karpenter_instance_profile": "dfe-test",
    "dfe.hyperi.io/karpenter_kms_key_id": "arn:aws:kms:us-west-2:000000000000:key/abc",
}


def appset_render(annotations: dict[str, str]) -> subprocess.CompletedProcess:
    """The chart rendered from layer2-platform.yaml's values block on one cluster secret."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "appset-values.json"
        path.write_text(json.dumps(appset_values(annotations)), encoding="utf-8")
        return helm(["helm", "template", "t", str(CHART), "-f", str(path)])


def test_expected_fail_a_cluster_secret_without_the_pool_annotation_fails_the_sync() -> None:
    """A secret that carries no node_pools, or only the annotation's previous name, renders no NodePool unguarded."""
    root = {"rootVolume": {"sizeGi": 40, "iops": 3000, "throughputMibS": 125}}
    pool = {**BASE_POOL, "aws": {**BASE_POOL["aws"], "nodeClass": root}}
    pools = json.dumps({"renderer": "karpenter-aws", "classes": {"clickhouse": pool}})
    for label, annotations in (
        ("absent", CLUSTER_FACTS),
        ("under its previous name", {**CLUSTER_FACTS, "dfe.hyperi.io/karpenter_pools": pools}),
    ):
        out = appset_render(annotations)
        expect(f"an annotation {label} fails the render", out.returncode != 0, out.stdout)
        expect(f"and says why, {label}", "rendering no NodePool would delete every live one" in out.stderr,
               out.stderr)
    out = appset_render({**CLUSTER_FACTS, "dfe.hyperi.io/node_pools": pools})
    expect("the annotation under its own name renders the pool", out.returncode == 0, out.stderr)
    kinds = sorted(d["kind"] for d in yaml.safe_load_all(out.stdout) if d)
    expect("one NodePool and one EC2NodeClass", kinds == ["EC2NodeClass", "NodePool"], kinds)


PRUNE_REFUSAL = "rendering no NodePool would delete every live one"


def test_expected_fail_an_empty_pool_annotation_reaches_the_prune_guard() -> None:
    """An empty map on the secret must reach the chart's prune guard, not break the values YAML."""
    annotations = {**CLUSTER_FACTS, "dfe.hyperi.io/node_pools": "{}"}
    pools = appset_values(annotations)["pools"]
    expect("the values block parses, with no renderer or classes", sorted(pools) == ["aws"], pools)
    out = appset_render(annotations)
    expect("the chart refuses the render", out.returncode != 0, out.stdout)
    expect("on its own prune guard", PRUNE_REFUSAL in out.stderr, out.stderr)


def test_expected_fail_any_karpenter_fact_without_classes_fails_the_render() -> None:
    """A secret holding only some of the facts still runs Karpenter, and an empty render prunes."""
    facts = {
        "instanceProfile": "dfe-test",
        "kmsKeyId": "arn:aws:kms:us-west-2:000000000000:key/abc",
    }
    for kept in (("instanceProfile",), ("kmsKeyId",), ("instanceProfile", "kmsKeyId")):
        sets: list[str] = []
        for fact in kept:
            sets += ["--set", f"pools.aws.cluster.{fact}={facts[fact]}"]
        for renderer in ((), ("--set", "pools.renderer=karpenter-aws")):
            out = helm(["helm", "template", "t", str(CHART), *sets, *renderer])
            case = f"{kept} {renderer or 'no renderer'}"
            expect(f"the render fails with {case}", out.returncode != 0, out.stdout)
            named = f"pools.aws.cluster.{kept[0]} is" in out.stderr
            expect(f"and names the fact and the annotation, with {case}",
                   named and "dfe.hyperi.io/node_pools" in out.stderr, out.stderr)
    no_tag = dict(CLUSTER_FACTS)
    del no_tag["dfe.hyperi.io/karpenter_discovery_tag"]
    out = appset_render(no_tag)
    expect("a secret without its discovery tag fails the sync", out.returncode != 0, out.stdout)
    expect("on the prune guard", PRUNE_REFUSAL in out.stderr, out.stderr)


# Each case is one values shape the chart must refuse rather than render around,
# with the phrase its guard names it by. A misplaced field renders nothing silently.
REFUSED_SHAPES = (
    (("--set", "pools.renderer=karpenter-awz"), 'pools.renderer is "karpenter-awz"'),
    (("--set", "pools.renderer=gke"), 'pools.renderer is "gke"'),
    (("--set", "pools.renderer="), "pools.renderer is empty"),
    (("--set-json", 'pools.classes.clickhouse.families=["r9gd"]'), "clickhouse.families is an AWS field"),
    (("--set", "pools.classes.clickhouse.generationGt=7"), "clickhouse.generationGt is an AWS field"),
    (("--set", "pools.classes.clickhouse.nodeClass.instanceStorePolicy=RAID0"), "clickhouse.nodeClass is an AWS field"),
    (("--set", "pools.classes.clickhouse.aws.arch=arm64"), "clickhouse.aws.arch is not a karpenter-aws field"),
    (("--set", "pools.classes.clickhouse.capacity=on-demand"), "clickhouse.capacity is not a pool class field"),
    (("--set", "pools.clickhouse.arch=arm64"), "pools.clickhouse is not a pools setting"),
    (("--set", "pools.aws.subnets=private"), "pools.aws.subnets is not a karpenter-aws setting"),
    (("--set", "pools.classes.clickhouse.aws=null"), "clickhouse.aws is empty"),
    (("--set", "pools.classes.clickhouse.limits.memry=64Gi"), "clickhouse.limits.memry is not a pool limit"),
    (
        ("--set", "pools.classes.clickhouse.consolidation.afterr=10m"),
        "clickhouse.consolidation.afterr is not a consolidation field",
    ),
    (
        ("--set-json", 'pools.classes.clickhouse.taints=[{"key":"k","value":"v","effect":"NoSchedule","operator":"Equal"}]'),
        "clickhouse.taints[0].operator is not a taint field",
    ),
    (("--set-json", 'pools.classes.clickhouse.taints={"key":"k"}'), "clickhouse.taints is not a list"),
    (("--set", "pools.aws.cluster.kmsKey=abc"), "pools.aws.cluster.kmsKey is not a cluster fact"),
    (
        ("--set", "pools.aws.nodeClass.rootvolume.sizeGi=40"),
        "pools.aws.nodeClass.rootvolume is not an EC2NodeClass setting",
    ),
    (
        ("--set", "pools.aws.nodeClass.rootVolume.size=40"),
        "pools.aws.nodeClass.rootVolume.size is not a root volume setting",
    ),
    (
        ("--set", "pools.aws.nodeClass.metadata.httpToken=required"),
        "pools.aws.nodeClass.metadata.httpToken is not an instance metadata setting",
    ),
    (
        ("--set", "pools.classes.clickhouse.aws.nodeClass.amiAlis=al2023@v20261003"),
        "pools.classes.clickhouse.aws.nodeClass.amiAlis is not an EC2NodeClass setting",
    ),
    (
        ("--set", "pools.classes.clickhouse.aws.nodeClass.rootVolume.iop=3000"),
        "pools.classes.clickhouse.aws.nodeClass.rootVolume.iop is not a root volume setting",
    ),
    (("--set", "karpenter.nodeClass.amiAlias=al2023@v20261003"), "karpenter is not a values root"),
)


def test_expected_fail_a_renderer_or_a_class_shape_the_chart_does_not_carry_fails_the_render() -> None:
    for extra, said in REFUSED_SHAPES:
        out = nodepool_run(BASE_POOL, extra=extra)
        expect(f"{extra!r} is refused", out.returncode != 0, out.stdout)
        expect(f"and named for {extra!r}", said in out.stderr, out.stderr)


def test_expected_fail_an_unknown_renderer_fails_even_with_no_class() -> None:
    """A typo in the renderer is refused before it can render nothing in place of the pools."""
    out = helm(["helm", "template", "t", str(CHART), "--set", "pools.renderer=karpenter"])
    expect("the render fails", out.returncode != 0, out.stdout)
    expect("and names the renderer", 'pools.renderer is "karpenter"' in out.stderr, out.stderr)


def main() -> int:
    with standalone():
        test_every_budget_renders_in_order_with_its_reasons_and_schedule()
        test_a_count_written_as_a_number_renders_as_the_string_the_crd_takes()
        test_expected_fail_a_budget_list_karpenter_would_misread_fails_the_render()
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
        test_the_appset_puts_the_run_tags_and_the_resolved_pools_where_the_chart_reads_them()
        test_expected_fail_a_script_that_is_not_base64_is_refused()
        test_a_test_run_holds_every_pool_to_the_run_sizes()
        test_expected_fail_no_run_tags_bound_no_size()
        test_a_deployment_overrides_the_run_sizes_or_turns_them_off()
        test_no_classes_render_nothing_whatever_the_renderer()
        test_expected_fail_a_karpenter_cluster_with_no_classes_fails_the_render()
        test_expected_fail_a_cluster_secret_without_the_pool_annotation_fails_the_sync()
        test_expected_fail_an_empty_pool_annotation_reaches_the_prune_guard()
        test_expected_fail_any_karpenter_fact_without_classes_fails_the_render()
        test_expected_fail_a_renderer_or_a_class_shape_the_chart_does_not_carry_fails_the_render()
        test_expected_fail_an_unknown_renderer_fails_even_with_no_class()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
