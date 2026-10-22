#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_sizing_reaches_data_charts.py
#  Purpose:      Prove a fresh scale deploy runs ClickHouse, Keeper and Kafka at
#                the resolver's sizes, that the deploy repo's own overlays still
#                win over them, and that every sized request fits its node.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The resolver's sizing, from its artefact to the data charts a fresh deploy runs.

    python3 -m pytest scripts/tests/test_sizing_reaches_data_charts.py -q

layer2-data reads the deploy repo's sizing/<profile>.data-charts.values.yaml under
its infra/ overlays. A bundled deploy repo gets the file from the Forgejo setup
Job, in the commit that gives the repo its branch. An external one carries it in
the resolver's sizing/, which the deployer commits. Each hop is run rather than
restated: the resolver, the Job script, bootstrap.sh's own block, then
layer2-data's Application rendered the way Argo renders it (scripts/dfe-weave)
over a deploy repo laid out on disk.

Needs helm on PATH, and kubectl for bootstrap's ConfigMap render. Every host is
under a reserved example domain.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from _forgejo_job import run_job
from _weave import data_application, helm, render_data

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
FIXTURES = REPO_ROOT / "scripts" / "tests" / "fixtures" / "sizing"
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
FORGEJO_VALUES = REPO_ROOT / "helm" / "charts" / "forgejo" / "values.yaml"
APPSET = Path("argocd/appsets/layer2-data.yaml")

sys.path.insert(0, str(SCRIPTS))
import resolve_sizing  # noqa: E402

BASE = "scale.data-charts.values.yaml"
EXTERNAL_REPO = "https://git.example.com/acme/dfe-deploy.git"
AWS_FACTS = {
    "dfe.hyperi.io/clickhouse_object_store_endpoint": "https://example-bucket.s3.us-west-2.amazonaws.com/dfe/",
    "dfe.hyperi.io/region": "us-west-2",
}

DIAL = """\
apiVersion: dfe.hyperi.io/v1
kind: DeployContext
substrate: k8s
metadata:
  name: dfe-test
target:
  provision:
    cloud: aws
    region: us-west-2
profile: scale
kafka:
  provider: strimzi
{kafka_extra}sizing:
  focus: {focus}
{sizing_extra}  spend_warn_usd_month: 5000
retention:
  default_ttl_days: 90
k8s:
  env: test
  cloud: aws
"""

# A broker sized to every core of an m8g.xlarge, the shape the floor broker takes.
FOUR_CORE_BROKER = "  overrides:\n    kafka-broker:\n      cpu: 4\n"


def _resolve(
    out: Path,
    *,
    focus: str = "economy",
    estimate: int | None = None,
    kafka_extra: str = "",
    sizing_extra: str = "",
) -> int:
    """Resolve one AWS dial against the captured catalogue into ``out``."""
    if estimate is not None:
        sizing_extra = f"  ingest_gb_per_day: {estimate}\n" + sizing_extra
    dial = out / "deployment.yaml"
    dial.write_text(
        DIAL.format(focus=focus, kafka_extra=kafka_extra, sizing_extra=sizing_extra),
        encoding="utf-8",
        newline="\n",
    )
    return resolve_sizing.main(["--dial", str(dial), "--fixtures", str(FIXTURES), "--out", str(out)])


@pytest.fixture(scope="module")
def resolved(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The floor AWS resolve's --out."""
    out = tmp_path_factory.mktemp("resolve")
    assert _resolve(out) == 0
    return out


def _base(out: Path) -> dict:
    return yaml.safe_load((out / "sizing" / BASE).read_text(encoding="utf-8"))


def _shapes(out: Path) -> dict:
    return json.loads((out / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))["resolved_shapes"]


def _facts(out: Path) -> dict[str, str]:
    """A fresh AWS deploy's cluster secret, the storage keys it carries for #827 included."""
    storage = (out / "sizing" / "scale.clickhouse-storage.json").read_text(encoding="utf-8").strip()
    return {**AWS_FACTS, "dfe.hyperi.io/clickhouse_storage": storage}


def _seed(out: Path) -> dict[str, str]:
    """The sizing ConfigMap bootstrap.sh writes from this resolve, by file name."""
    return {BASE: (out / "sizing" / BASE).read_text(encoding="utf-8")}


# --------------------------------------------------------------------- the render


def _application(app: str, facts: dict[str, str]) -> tuple[dict, dict]:
    """layer2-data's Application for one chart on a scale AWS cluster, and its list element."""
    if shutil.which(helm()) is None:
        pytest.skip("helm is not on PATH")
    return data_application(app, "scale", "aws", facts)


def data_docs(app: str, deploy: Path, facts: dict[str, str] | None = None) -> list[dict]:
    """One layer2-data chart on a scale AWS cluster, rendered over the deploy repo at ``deploy``."""
    if shutil.which(helm()) is None:
        pytest.skip("helm is not on PATH")
    return render_data(app, "scale", "aws", deploy, facts)


def _kind(docs: list[dict], kind: str) -> dict:
    return next(d for d in docs if d.get("kind") == kind)


def _sized(deploy: Path, facts: dict[str, str]) -> dict[str, dict]:
    """The spec of each sized object, by kind, as the deploy repo at ``deploy`` renders them."""
    clickhouse = data_docs("clickhouse-cluster", deploy, facts)
    kafka = data_docs("kafka", deploy, facts)
    return {
        "ClickHouseCluster": _kind(clickhouse, "ClickHouseCluster")["spec"],
        "KeeperCluster": _kind(clickhouse, "KeeperCluster")["spec"],
        "KafkaNodePool": _kind(kafka, "KafkaNodePool")["spec"],
        "Kafka": _kind(kafka, "Kafka")["spec"],
    }


def _resources(block: dict) -> dict:
    """A rendered resources block with every quantity as the string the base writes."""
    return {side: {k: str(v) for k, v in quantities.items()} for side, quantities in block.items()}


def _assert_sized(spec: dict[str, dict], base: dict) -> None:
    """Each sized object carries the base's replicas, resources and storage."""
    ch, keeper, kafka = base["clickhouse"], base["clickhouse"]["keeper"], base["kafka"]
    server = spec["ClickHouseCluster"]
    assert server["replicas"] == ch["replicas"]
    assert _resources(server["containerTemplate"]["resources"]) == ch["resources"]
    assert server["dataVolumeClaimSpec"]["resources"]["requests"]["storage"] == ch["storage"]["size"]
    assert server["settings"]["extraConfig"]["max_server_memory_usage_to_ram_ratio"] == (
        ch["serverProfile"]["maxServerMemoryUsageToRamRatio"]
    )
    quorum = spec["KeeperCluster"]
    assert quorum["replicas"] == keeper["replicas"]
    assert _resources(quorum["containerTemplate"]["resources"]) == keeper["resources"]
    assert quorum["dataVolumeClaimSpec"]["resources"]["requests"]["storage"] == keeper["storage"]["size"]
    pool = spec["KafkaNodePool"]
    assert pool["replicas"] == kafka["replicas"]
    assert _resources(pool["resources"]) == kafka["resources"]
    assert pool["storage"]["volumes"][0]["size"] == kafka["storage"]["size"]
    hours = kafka["retention"]["assumedConsumerDowntimeH"] + kafka["retention"]["archiverLagH"]
    assert spec["Kafka"]["kafka"]["config"]["log.retention.ms"] == hours * 3600 * 1000


# ------------------------------------------------------------- the resolver's artefact


def test_the_base_carries_the_sizing_and_none_of_the_first_sync_keys(resolved: Path) -> None:
    """The storage keys ride the cluster secret, the pools the node-pools chart, and the
    message size is one chain with apps that never read this file."""
    base = _base(resolved)
    fragment = yaml.safe_load((resolved / "sizing" / "scale.values.yaml").read_text(encoding="utf-8"))
    assert set(base) == {"kafka", "clickhouse"}
    assert {"objectStore", "storageModel"}.isdisjoint(base["clickhouse"])
    assert "messageMaxBytes" not in base["kafka"]
    for chart_key, excluded in resolve_sizing.DATA_CHART_EXCLUDED.items():
        assert base[chart_key] == {k: v for k, v in fragment[chart_key].items() if k not in excluded}


# -------------------------------------------------- the bundled repo's first commit


def test_a_fresh_bundled_repo_gets_the_sizing_in_its_first_commit(tmp_path: Path, resolved: Path) -> None:
    """Created empty, the repo has no branch until its first commit, and that commit is the sizing."""
    job = run_job(tmp_path, seed_apps=("dfe-loader",), fresh=True, sizing=_seed(resolved))
    assert job.returncode == 0, job.output
    assert job.writes[0]["path"] == "user/repos"
    assert job.writes[0]["content"]["auto_init"] is False
    assert job.writes[1] == {"method": "POST", "path": f"sizing/{BASE}", "content": _seed(resolved)[BASE]}
    assert [w["path"] for w in job.writes[2:]] == ["values/dfe-loader-default-values.yaml", ".seeded-apps"]


def test_a_deploy_repos_own_sizing_is_left_as_it_is(tmp_path: Path, resolved: Path) -> None:
    own = "kafka:\n  replicas: 6\n"
    job = run_job(tmp_path, repo_files={f"sizing/{BASE}": own}, sizing=_seed(resolved))
    assert job.returncode == 0, job.output
    assert not [w for w in job.writes if w["path"].startswith("sizing/")]
    assert (job.repo / "sizing" / BASE).read_text(encoding="utf-8") == own


def test_a_failed_sizing_write_stops_before_the_repo_gets_a_branch(tmp_path: Path, resolved: Path) -> None:
    """An app seed would then be the repo's first commit, and layer2-data would sync without the sizing."""
    job = run_job(
        tmp_path, seed_apps=("dfe-loader",), fresh=True, sizing=_seed(resolved), refuse="sizing/"
    )
    assert job.returncode != 0
    assert [w["path"] for w in job.writes] == ["user/repos"]


def test_a_deploy_with_no_resolve_seeds_no_sizing(tmp_path: Path) -> None:
    job = run_job(tmp_path, seed_apps=("dfe-loader",), fresh=True)
    assert job.returncode == 0, job.output
    assert [w["path"] for w in job.writes] == [
        "user/repos",
        "values/dfe-loader-default-values.yaml",
        ".seeded-apps",
    ]


def test_one_name_from_the_resolver_to_the_appset() -> None:
    """The resolver's artefact, bootstrap's read, the Job's ConfigMap and layer2-data's value file."""
    script = BOOTSTRAP.read_text(encoding="utf-8")
    seed = yaml.safe_load(FORGEJO_VALUES.read_text(encoding="utf-8"))["deployRepo"]["sizingSeed"]
    assert f"create configmap {seed['configMapName']}" in script
    assert 'DFE_SIZING_BASE_FILE="${DFE_SIZING_DIR}/sizing/${DFE_PROFILE}.data-charts.values.yaml"' in script
    appset = (REPO_ROOT / APPSET).read_text(encoding="utf-8")
    assert '/sizing/{{ index .metadata.annotations "dfe.hyperi.io/profile" }}.data-charts.values.yaml' in appset
    assert (SCRIPTS / "resolve_sizing.py").read_text(encoding="utf-8").count('.data-charts.values.yaml"') == 1


def _configmap_block() -> str:
    """bootstrap.sh's own ConfigMap lines, run on their own rather than copied here."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "sizing_seed_args=()")
    end = next(i for i, line in enumerate(lines) if "Deploy-repo sizing seed" in line)
    return "\n".join(line.strip() for line in lines[start:end])


def _run_configmap_block(base_file: Path) -> dict:
    """The ConfigMap bootstrap applies, with apply printing what it was handed."""
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl is not on PATH, so bootstrap's dry-run render cannot run")
    shim = 'kubectl() { if [[ "$1" == apply ]]; then cat; else command kubectl "$@"; fi; }\n'
    done = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", shim + _configmap_block()],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "DFE_SIZING_BASE_FILE": str(base_file),
            "KUBECONFIG": "/nonexistent",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return next(d for d in yaml.safe_load_all(done.stdout) if d)


def test_bootstrap_carries_the_resolve_into_the_configmap(resolved: Path) -> None:
    configmap = _run_configmap_block(resolved / "sizing" / BASE)
    assert configmap["metadata"] == {"name": "dfe-deploy-repo-sizing", "namespace": "forgejo"}
    assert configmap["data"] == _seed(resolved)


def test_bootstrap_empties_the_configmap_when_nothing_was_resolved(tmp_path: Path) -> None:
    """An earlier resolve's file would otherwise seed a deploy repo created later."""
    configmap = _run_configmap_block(tmp_path / "sizing" / BASE)
    assert "data" not in configmap


# ------------------------------------------------------------------- the three proofs


def test_a_fresh_bundled_deploy_runs_the_data_charts_at_the_resolvers_sizes(
    tmp_path: Path, resolved: Path
) -> None:
    """Nothing hand-copied: the deploy repo is whatever the setup Job made of an empty one."""
    job = run_job(tmp_path / "job", seed_apps=("dfe-loader",), fresh=True, sizing=_seed(resolved))
    assert job.returncode == 0, job.output
    sized = _sized(job.repo, _facts(resolved))
    _assert_sized(sized, _base(resolved))

    # The base is what moved them: the chart defaults ask for less than the floor shape gives.
    with tempfile.TemporaryDirectory() as empty:
        defaults = _sized(Path(empty), _facts(resolved))
    for kind in ("ClickHouseCluster", "KeeperCluster"):
        assert sized[kind]["containerTemplate"]["resources"] != defaults[kind]["containerTemplate"]["resources"]
    assert sized["KafkaNodePool"]["resources"] != defaults["KafkaNodePool"]["resources"]


def test_a_deploy_repo_overlay_beats_the_sizing(tmp_path: Path, resolved: Path) -> None:
    """infra/ is where a deployer and the engine's backing-services API write, and it wins."""
    deploy = tmp_path / "deploy"
    (deploy / "sizing").mkdir(parents=True)
    shutil.copy(resolved / "sizing" / BASE, deploy / "sizing" / BASE)
    (deploy / "infra").mkdir()
    server = {"requests": {"cpu": "3", "memory": "12Gi"}, "limits": {"cpu": "3", "memory": "12Gi"}}
    overlays = {
        "clickhouse-cluster.yaml": {"clickhouse": {"replicas": 5, "resources": server}},
        "kafka.yaml": {"kafka": {"storage": {"size": "200Gi"}}},
        "common.yaml": {"clickhouse": {"keeper": {"storage": {"size": "40Gi"}}}},
    }
    for name, body in overlays.items():
        (deploy / "infra" / name).write_text(yaml.safe_dump(body), encoding="utf-8")
    sized = _sized(deploy, _facts(resolved))
    base = _base(resolved)
    assert sized["ClickHouseCluster"]["replicas"] == 5
    assert _resources(sized["ClickHouseCluster"]["containerTemplate"]["resources"]) == server
    assert sized["KafkaNodePool"]["storage"]["volumes"][0]["size"] == "200Gi"
    assert sized["KeeperCluster"]["dataVolumeClaimSpec"]["resources"]["requests"]["storage"] == "40Gi"
    # A key no overlay names still comes from the base.
    assert _resources(sized["KafkaNodePool"]["resources"]) == base["kafka"]["resources"]
    keeper = sized["KeeperCluster"]["containerTemplate"]["resources"]
    assert _resources(keeper) == base["clickhouse"]["keeper"]["resources"]


def test_an_external_deploy_repo_with_the_resolvers_sizing_renders_the_same(
    tmp_path: Path, resolved: Path
) -> None:
    """An external repo is read through the same ref, holding the sizing/ the deployer committed."""
    facts = {**_facts(resolved), "dfe.hyperi.io/config_repo_url": EXTERNAL_REPO}
    app, _ = _application("kafka", facts)
    assert app["spec"]["sources"][1] == {"repoURL": EXTERNAL_REPO, "targetRevision": "main", "ref": "values"}

    external = tmp_path / "external"
    shutil.copytree(resolved / "sizing", external / "sizing")
    job = run_job(tmp_path / "job", fresh=True, sizing=_seed(resolved))
    assert job.returncode == 0, job.output
    assert _sized(external, facts) == _sized(job.repo, _facts(resolved))


# --------------------------------------------------------------- every request lands


def _reserve_core() -> resolve_sizing.Core:
    """A Core carrying sizing.yaml's own node-reserve ratios and nothing else."""
    sizing = resolve_sizing._load(resolve_sizing.SIZING_FILE)

    def floor(name: str) -> float:
        return resolve_sizing._ratio(sizing, "floors", name).number

    core = resolve_sizing.Core(
        tier="scale",
        focus="economy",
        headroom=0.4,
        estimated=False,
        ingest_gb_per_day=0.0,
        avg_mb_s=0.0,
        peak_mb_s=0.0,
        peak_factor=2.0,
        required_mb_s=0.0,
        carried_mb_s=0.0,
    )
    core.daemonset_cpu_m = int(floor("daemonset_cpu_millicores"))
    core.daemonset_memory_mib = int(floor("daemonset_memory_mib"))
    core.node_memory_overhead = floor("node_memory_overhead_fraction")
    core.node_max_pods = int(floor("node_max_pods"))
    return core


@pytest.mark.parametrize(
    ("memory_gib", "allocatable_mib"), [(4.0, 1711), (8.0, 5500), (16.0, 13078), (64.0, 58543)]
)
def test_the_memory_a_node_leaves_a_pod(memory_gib: float, allocatable_mib: int) -> None:
    """7.5% to the kernel, nodeadm's 11 MiB a pod at 110 pods plus 255, 100 to eviction, 512 to DaemonSets."""
    assert resolve_sizing._allocatable_memory_mib(memory_gib, _reserve_core()) == allocatable_mib


def _millicores(quantity: str) -> int:
    return int(quantity[:-1]) if quantity.endswith("m") else int(float(quantity) * 1000)


def _mebibytes(quantity: str) -> int:
    return int(quantity.removesuffix("Gi")) * 1024


# Where each dedicated pool's pod requests sit in the base.
REQUESTS = {
    "kafka-broker": ("kafka", "resources"),
    "kraft-controller": ("kafka", "controllerPool", "resources"),
    "clickhouse": ("clickhouse", "resources"),
    "keeper": ("clickhouse", "keeper", "resources"),
}

DIALS = {
    "floor": {},
    "floor-separate-controllers": {"kafka_extra": "  controller_pool: separate\n"},
    "four-core-broker": {"sizing_extra": FOUR_CORE_BROKER},
    "economy-1000": {"estimate": 1000},
    "economy-10000": {"estimate": 10000},
    "economy-100000": {"estimate": 100000},
    "performance-100000": {"focus": "performance", "estimate": 100000},
}


@pytest.mark.parametrize("dial", sorted(DIALS))
def test_every_sized_request_fits_what_its_node_leaves(tmp_path: Path, dial: str) -> None:
    """A request at a node's whole vCPU or memory never schedules, and nothing reports it."""
    assert _resolve(tmp_path, **DIALS[dial]) in (0, 1)
    base, shapes = _base(tmp_path), _shapes(tmp_path)
    catalogue = resolve_sizing.fetch_fixtures(FIXTURES, "us-west-2")
    core = _reserve_core()
    checked = 0
    for use_case, path in REQUESTS.items():
        block: object = base
        for key in path:
            block = block.get(key) if isinstance(block, dict) else None
        if not isinstance(block, dict) or use_case not in shapes:
            continue
        node = catalogue.types[shapes[use_case]["instance_types"][0]]
        cpu_room = node.vcpu * 1000 - resolve_sizing._kube_reserved_cpu_m(node.vcpu) - core.daemonset_cpu_m
        memory_room = resolve_sizing._allocatable_memory_mib(node.memory_gib, core)
        requests = block["requests"]
        assert _millicores(requests["cpu"]) <= cpu_room, (use_case, node.name, requests)
        assert _mebibytes(requests["memory"]) <= memory_room, (use_case, node.name, requests)
        assert requests["memory"] == block["limits"]["memory"], use_case
        checked += 1
    assert checked >= 3, dial


def test_the_floor_broker_takes_the_shape_that_holds_its_memory(resolved: Path) -> None:
    """8 GiB of an m8g.large is more than it leaves a pod, so the floor broker takes an xlarge."""
    assert _shapes(resolved)["kafka-broker"]["instance_types"][0] == "m8g.xlarge"
    assert _base(resolved)["kafka"]["resources"]["requests"] == {"cpu": "2", "memory": "8Gi"}


def test_an_override_onto_a_node_too_small_for_the_memory_is_refused(tmp_path: Path) -> None:
    override = "  overrides:\n    kafka-broker:\n      instance_type: m8g.large\n"
    assert _resolve(tmp_path, sizing_extra=override) == 1
    report = (tmp_path / "sizing" / "scale.report.md").read_text(encoding="utf-8")
    assert "| A8 | kafka-broker |" in report


# ------------------------------------------------------------------ the thread dials


def _broker_config(out: Path) -> dict:
    """The Kafka CR's broker config, rendered over a deploy repo holding this resolve's base."""
    deploy = out / "deploy"
    (deploy / "sizing").mkdir(parents=True)
    shutil.copy(out / "sizing" / BASE, deploy / "sizing" / BASE)
    return _kind(data_docs("kafka", deploy), "Kafka")["spec"]["kafka"]["config"]


def test_a_broker_on_every_core_of_its_node_keeps_the_thread_dials(tmp_path: Path) -> None:
    """4 vCPU on an m8g.xlarge schedules at 3620m, and the dials read the 4-core limit."""
    assert _resolve(tmp_path, sizing_extra=FOUR_CORE_BROKER) == 0
    assert _shapes(tmp_path)["kafka-broker"]["instance_types"][0] == "m8g.xlarge"
    resources = _base(tmp_path)["kafka"]["resources"]
    assert resources["requests"]["cpu"] == "3620m"
    assert resources["limits"]["cpu"] == "4"
    config = _broker_config(tmp_path)
    assert config["num.io.threads"] == 16
    assert config["num.network.threads"] == 8


def test_a_two_core_broker_runs_without_the_thread_dials(resolved: Path, tmp_path: Path) -> None:
    out = tmp_path / "floor"
    shutil.copytree(resolved, out)
    assert "num.io.threads" not in _broker_config(out)
