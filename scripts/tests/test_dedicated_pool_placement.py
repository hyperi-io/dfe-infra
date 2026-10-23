#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dedicated_pool_placement.py
#  Purpose:      Prove each data workload requires its dedicated node pool when
#                the cluster runs one, and renders unchanged when it runs none.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Kafka, Keeper and ClickHouse onto the pools the resolver taints for them.

    python3 -m pytest scripts/tests/test_dedicated_pool_placement.py -q

The resolver taints a pool per data workload and labels its nodes
dfe.hyperi.io/workload=<use case>. The charts tolerate that taint everywhere, so
without a requirement a broker can land on an untainted general node. The
cluster secret's dfe.hyperi.io/node_pools names the pools a cluster runs, and
layer2-data hands the charts the tainted ones, so the requirement renders only
where its pool exists. Every render goes through layer2-data's own Application
(scripts/dfe-weave). Needs helm on PATH.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from _charts import chart_dir
from _weave import data_application, helm, render_data

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
FIXTURES = REPO_ROOT / "scripts" / "tests" / "fixtures" / "sizing"
WORKLOAD = "dfe.hyperi.io/workload"
BASE = "scale.data-charts.values.yaml"

sys.path.insert(0, str(SCRIPTS))
import resolve_sizing  # noqa: E402

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
  controller_pool: {controller_pool}
sizing:
  focus: economy
  storage_model: {storage_model}
  spend_warn_usd_month: 5000
retention:
  default_ttl_days: 90
k8s:
  env: test
  cloud: aws
"""


def _resolve(out: Path, *, controller_pool: str, storage_model: str) -> Path:
    dial = out / "deployment.yaml"
    dial.write_text(
        DIAL.format(controller_pool=controller_pool, storage_model=storage_model),
        encoding="utf-8",
        newline="\n",
    )
    argv = ["--dial", str(dial), "--fixtures", str(FIXTURES), "--out", str(out)]
    assert resolve_sizing.main(argv) == 0
    return out


@pytest.fixture(scope="module")
def separate(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An AWS resolve with a separate KRaft quorum and ClickHouse's cache on its PVC."""
    return _resolve(tmp_path_factory.mktemp("separate"), controller_pool="separate", storage_model="local")


@pytest.fixture(scope="module")
def combined(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The floor AWS resolve: a combined quorum on the brokers."""
    return _resolve(tmp_path_factory.mktemp("combined"), controller_pool="combined", storage_model="auto")


def _deploy(out: Path, tmp_path: Path) -> Path:
    """A deploy repo holding the resolve's sizing, as a seeded bundled repo does."""
    deploy = tmp_path / "deploy"
    (deploy / "sizing").mkdir(parents=True)
    shutil.copy(out / "sizing" / BASE, deploy / "sizing" / BASE)
    return deploy


def _pools(out: Path) -> dict[str, str]:
    """The cluster secret's node_pools annotation, as bootstrap.sh carries the resolve's."""
    return {"dfe.hyperi.io/node_pools": (out / "sizing" / "scale.pools.json").read_text(encoding="utf-8").strip()}


def _render(app: str, deploy: Path, annotations: dict[str, str], cloud: str = "aws", **kw) -> list[dict]:
    if shutil.which(helm()) is None:
        pytest.skip("helm is not on PATH")
    return render_data(app, "scale", cloud, deploy, annotations, **kw)


def _by_name(docs: list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == kind}


def _affinity(pool: dict) -> dict[str, list[str]]:
    """A KafkaNodePool's required node labels, key -> allowed values."""
    terms = (
        pool["spec"].get("template", {}).get("pod", {}).get("affinity", {}).get("nodeAffinity", {})
        .get("requiredDuringSchedulingIgnoredDuringExecution", {}).get("nodeSelectorTerms", [])
    )
    return {e["key"]: e["values"] for term in terms for e in term["matchExpressions"]}


def _selector(cr: dict) -> dict[str, str]:
    return cr["spec"].get("podTemplate", {}).get("nodeSelector", {})


# ------------------------------------------------------------------ pools on


def test_each_kafka_pool_requires_its_own_node_pool(separate: Path, tmp_path: Path) -> None:
    docs = _render("kafka", _deploy(separate, tmp_path), _pools(separate))
    pools = _by_name(docs, "KafkaNodePool")
    assert _affinity(pools["dfe-kafka-pool"]) == {WORKLOAD: ["kafka-broker"]}
    assert _affinity(pools["dfe-kafka-controller"]) == {WORKLOAD: ["kraft-controller"]}


def test_a_combined_quorum_rides_the_broker_pool(combined: Path, tmp_path: Path) -> None:
    """The combined pool carries the controller role on the brokers' own nodes."""
    docs = _render("kafka", _deploy(combined, tmp_path), _pools(combined))
    pools = _by_name(docs, "KafkaNodePool")
    assert set(pools) == {"dfe-kafka-pool"}
    assert _affinity(pools["dfe-kafka-pool"]) == {WORKLOAD: ["kafka-broker"]}


def test_keeper_and_a_pvc_cache_clickhouse_require_their_pools(separate: Path, tmp_path: Path) -> None:
    """Only an instance-store cache required the clickhouse pool before, and Keeper never did."""
    docs = _render("clickhouse-cluster", _deploy(separate, tmp_path), _pools(separate))
    server = next(d for d in docs if d.get("kind") == "ClickHouseCluster")
    keeper = next(d for d in docs if d.get("kind") == "KeeperCluster")
    assert "volumes" not in server["spec"]["podTemplate"], "the cache should sit on the data PVC here"
    assert _selector(server) == {WORKLOAD: "clickhouse"}
    assert _selector(keeper) == {WORKLOAD: "keeper"}


def test_a_deployers_node_selector_still_wins(separate: Path, tmp_path: Path) -> None:
    """A deploy repo's nodeScheduling.nodeSelector is merged over the pool label, as before."""
    deploy = _deploy(separate, tmp_path)
    (deploy / "infra").mkdir()
    overlay = {"nodeScheduling": {"nodeSelector": {WORKLOAD: "dfe", "topology.kubernetes.io/zone": "us-west-2a"}}}
    (deploy / "infra" / "common.yaml").write_text(yaml.safe_dump(overlay), encoding="utf-8")
    keeper = next(
        d for d in _render("clickhouse-cluster", deploy, _pools(separate)) if d.get("kind") == "KeeperCluster"
    )
    assert _selector(keeper) == overlay["nodeScheduling"]["nodeSelector"]
    broker = _by_name(_render("kafka", deploy, _pools(separate)), "KafkaNodePool")["dfe-kafka-pool"]
    assert _affinity(broker) == {k: [v] for k, v in overlay["nodeScheduling"]["nodeSelector"].items()}


def test_only_the_tainted_pools_reach_the_data_charts(separate: Path) -> None:
    """The shared pools carry no taint, so no data workload is required onto one."""
    app, _ = data_application("kafka", "scale", "aws", _pools(separate))
    inline = yaml.safe_load(app["spec"]["sources"][0]["helm"]["values"])
    classes = json.loads(_pools(separate)["dfe.hyperi.io/node_pools"])["classes"]
    tainted = sorted(name for name, body in classes.items() if body.get("taints"))
    assert inline["nodeScheduling"]["dedicatedPools"] == tainted
    assert tainted == ["clickhouse", "kafka-broker", "keeper", "kraft-controller"]


def test_no_other_data_chart_is_handed_the_pools(separate: Path) -> None:
    for app in ("documentdb", "otel-collector", "kafbat", "links"):
        rendered, _ = data_application(app, "scale", "aws", _pools(separate))
        inline = yaml.safe_load(rendered["spec"]["sources"][0]["helm"].get("values") or "") or {}
        assert "nodeScheduling" not in inline, app


def test_redpanda_brokers_require_the_broker_pool() -> None:
    if shutil.which("helm") is None:
        pytest.skip("helm is not on PATH")
    out = subprocess.run(
        [
            "helm", "template", "t", str(chart_dir("kafka")),
            "--show-only", "templates/redpanda.yaml",
            "--set", "appNamespace=dfe",
            "--set", "kafka.mode=cluster",
            "--set", "kafka.provider=redpanda",
            "--set", "kafka.redpanda.acceptLicense=true",
            "--set-json", 'nodeScheduling.dedicatedPools=["kafka-broker"]',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert out.returncode == 0, out.stderr
    redpanda = next(d for d in yaml.safe_load_all(out.stdout) if d and d.get("kind") == "Redpanda")
    assert redpanda["spec"]["clusterSpec"]["nodeSelector"] == {WORKLOAD: "kafka-broker"}


# ----------------------------------------------------------------- pools off


@pytest.mark.parametrize("cloud", ["aws", "local", "azure"])
def test_no_pools_means_no_requirement_a_pod_could_wait_on(cloud: str, separate: Path, tmp_path: Path) -> None:
    """A cluster secret naming no pools leaves placement to nodeScheduling, as it always was."""
    deploy = _deploy(separate, tmp_path)
    expected = yaml.safe_load(
        (REPO_ROOT / "argocd" / "values" / f"{cloud}.yaml").read_text(encoding="utf-8")
    ).get("nodeScheduling", {}).get("nodeSelector", {})
    kafka = _by_name(_render("kafka", deploy, {}, cloud=cloud), "KafkaNodePool")
    for pool in kafka.values():
        assert _affinity(pool) == {k: [v] for k, v in expected.items()}, pool["metadata"]["name"]
    clickhouse = _render("clickhouse-cluster", deploy, {}, cloud=cloud)
    for kind in ("ClickHouseCluster", "KeeperCluster"):
        assert _selector(next(d for d in clickhouse if d.get("kind") == kind)) == expected, kind
