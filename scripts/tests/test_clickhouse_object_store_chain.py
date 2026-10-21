#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_clickhouse_object_store_chain.py
#  Purpose:      Prove a fresh AWS deploy reaches cached-object on an instance-store
#                cache with no deploy-repo file, and that a deploy-repo object
#                store still stands where the cluster secret names none.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The ClickHouse object store, from the resolver and the aws root to the chart.

    python3 -m pytest scripts/tests/test_clickhouse_object_store_chain.py -q

Two facts reach clickhouse-cluster on the cluster secret, because wave 4 renders
before any deploy repo holds an overlay: the bucket URL the aws root outputs, and
the storage model and cache placement the resolver decides. Each hop is run
rather than restated -- the resolver, bootstrap.sh's own block, the cluster
secret template, then layer2-data's Application rendered the way Argo renders it
(scripts/dfe-weave) over a deploy repo that is empty or holds an overlay.

Needs helm on PATH. Every host is under a reserved example domain.
"""

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from _weave import helm, weave

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESOLVE_SIZING = REPO_ROOT / "scripts" / "resolve_sizing.py"
FIXTURES = REPO_ROOT / "scripts" / "tests" / "fixtures" / "sizing"
SIZING = REPO_ROOT / "sizing" / "sizing.yaml"
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
RENDER_ENV = REPO_ROOT / "bootstrap" / "render_env.py"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
AWS_ROOT_OUTPUTS = REPO_ROOT / "terraform" / "environments" / "aws" / "outputs.tf"
APPSET = Path("argocd/appsets/layer2-data.yaml")

ENDPOINT = "https://example-bucket.s3.us-west-2.amazonaws.com/dfe/"
REGION = "us-west-2"
MINIO = "https://minio.example.com/dfe-clickhouse/"
STORAGE_KEY = "dfe.hyperi.io/clickhouse_storage"
ENDPOINT_KEY = "dfe.hyperi.io/clickhouse_object_store_endpoint"

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
sizing:
  focus: economy
  spend_warn_usd_month: 5000
retention:
  default_ttl_days: 90
k8s:
  env: test
  cloud: aws
"""


# ------------------------------------------------------------------- the resolver


@pytest.fixture(scope="module")
def resolved(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The floor AWS resolve's --out, against the captured catalogue."""
    out = tmp_path_factory.mktemp("resolve")
    dial = out / "deployment.yaml"
    dial.write_text(DIAL, encoding="utf-8", newline="\n")
    done = subprocess.run(
        [
            sys.executable,
            str(RESOLVE_SIZING),
            "--dial",
            str(dial),
            "--fixtures",
            str(FIXTURES),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return out


def _storage(out: Path) -> str:
    return (out / "sizing" / "scale.clickhouse-storage.json").read_text(encoding="utf-8").strip()


def _cache_share() -> float:
    return float(
        yaml.safe_load(SIZING.read_text(encoding="utf-8"))["clickhouse"][
            "instance_store_cache_share"
        ]["value"]
    )


def test_the_resolver_writes_the_cache_placement_it_decided(resolved: Path) -> None:
    """The artefact is the overlay's own objectStore block, so the two cannot disagree."""
    fragment = yaml.safe_load(
        (resolved / "sizing" / "scale.values.yaml").read_text(encoding="utf-8")
    )
    storage = json.loads(_storage(resolved))
    assert storage["objectStore"] == fragment["clickhouse"]["objectStore"]
    assert storage["objectStore"]["cache"] == {"volume": "instance-store"}
    # The model rides with the cache, so no overlay can strand it.
    assert storage["storageModel"] == "cached-object"


def test_the_cache_leaves_room_on_the_array_it_shares(resolved: Path) -> None:
    """RAID0 carries containerd and the kubelet too, which evicts at 10% free."""
    shapes = json.loads(
        (resolved / "shapes" / "resolved" / "aws-us-west-2.json").read_text(encoding="utf-8")
    )
    device_gib = shapes["clickhouse"]["volumes"]["cache"]["size_gib"]
    want = math.floor(device_gib * _cache_share())
    assert json.loads(_storage(resolved))["objectStore"]["cacheSize"] == f"{want}Gi"
    assert want < device_gib * 0.9


def test_the_clickhouse_shape_asks_its_node_group_for_raid0(resolved: Path) -> None:
    """The managed node group and the Karpenter pool assemble the NVMe the same way."""
    tfvars = json.loads((resolved / "sizing.auto.tfvars.json").read_text(encoding="utf-8"))
    pools = json.loads((resolved / "sizing" / "scale.pools.json").read_text(encoding="utf-8"))[
        "classes"
    ]
    assert tfvars["resolved_shapes"]["clickhouse"]["instance_store_policy"] == "RAID0"
    assert pools["clickhouse"]["aws"]["nodeClass"]["instanceStorePolicy"] == "RAID0"
    assert "instance_store_policy" not in tfvars["resolved_shapes"]["kafka-broker"]


# --------------------------------------------------------- tofu, bootstrap, secret


def test_the_aws_root_outputs_the_bucket_the_cluster_module_creates() -> None:
    outputs = AWS_ROOT_OUTPUTS.read_text(encoding="utf-8")
    block = outputs.split('output "DFE_CLICKHOUSE_OBJECT_STORE_ENDPOINT"')[1].split("output ")[0]
    assert "module.cluster.clickhouse_object_store_endpoint" in block


def _bootstrap_block() -> str:
    """bootstrap.sh's own storage block, run on its own rather than copied here."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = next(
        i for i, line in enumerate(lines) if line.startswith("DFE_CLICKHOUSE_STORAGE_FILE=")
    )
    end = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("export DFE_CLICKHOUSE_STORAGE_ANNOTATION=")
    )
    return "\n".join(lines[start : end + 1])


def _run_block(**env: str) -> subprocess.CompletedProcess:
    script = _bootstrap_block() + '\nprintf "\\n%s" "$DFE_CLICKHOUSE_STORAGE_ANNOTATION"'
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_bootstrap_carries_the_resolve_onto_the_annotation(resolved: Path) -> None:
    done = _run_block(DFE_SIZING_DIR=str(resolved), DFE_PROFILE="scale")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1] == f"{STORAGE_KEY}: '{_storage(resolved)}'"


def test_a_profile_the_resolver_never_sized_writes_no_annotation(resolved: Path) -> None:
    done = _run_block(DFE_SIZING_DIR=str(resolved), DFE_PROFILE="single")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "\n"


def test_the_cluster_secret_carries_the_storage_as_json(resolved: Path) -> None:
    rendered = subprocess.run(
        [sys.executable, str(RENDER_ENV)],
        input=CLUSTER_SECRET.read_text(encoding="utf-8"),
        env={
            "PATH": "/usr/bin:/bin",
            "DFE_CLICKHOUSE_STORAGE_ANNOTATION": f"{STORAGE_KEY}: '{_storage(resolved)}'",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    annotations = yaml.safe_load(rendered)["metadata"]["annotations"]
    assert json.loads(annotations[STORAGE_KEY]) == json.loads(_storage(resolved))


# --------------------------------------------------------------------- the render


def clickhouse_docs(cloud: str, facts: dict[str, str], overlay: str | None = None) -> list[dict]:
    """clickhouse-cluster as layer2-data's Application renders it.

    ``overlay`` is the deploy repo's infra/clickhouse-cluster.yaml. None leaves
    the deploy repo empty, as a fresh one is.
    """
    w = weave()
    doc = w.load_appset(REPO_ROOT, APPSET)
    element = next(
        e
        for generator in doc["spec"]["generators"]
        for nested in generator["matrix"]["generators"]
        for e in (nested.get("list") or {}).get("elements") or []
        if e["app"] == "clickhouse-cluster"
    )
    target = w.Target(element["app"], "scale", cloud, annotations=tuple(sorted(facts.items())))
    app = w.expand_template(
        doc["spec"]["template"], {**w.generator_params(target, None), **element}, helm()
    )
    source = app["spec"]["sources"][0]
    chart = REPO_ROOT / source["path"]
    with tempfile.TemporaryDirectory() as tmp:
        deploy = Path(tmp)
        (deploy / "infra").mkdir()
        if overlay is not None:
            (deploy / "infra" / "clickhouse-cluster.yaml").write_text(overlay, encoding="utf-8")
        chain = []
        for raw in source["helm"].get("valueFiles") or []:
            path = (
                deploy / raw.removeprefix("$values/")
                if raw.startswith("$values/")
                else (chart / raw).resolve()
            )
            chain.append({"written": raw, "path": path, "present": path.is_file()})
        return w.render_source(
            source,
            chart=chart,
            release=element["app"],
            namespace=element["namespace"],
            chain=chain,
            helm=helm(),
        )


def cluster(docs: list[dict]) -> dict:
    return next(d for d in docs if d.get("kind") == "ClickHouseCluster")


def disks(docs: list[dict]) -> dict:
    return (
        cluster(docs)["spec"]["settings"]["extraConfig"]
        .get("storage_configuration", {})
        .get("disks", {})
    )


def aws_facts(resolved: Path) -> dict[str, str]:
    return {ENDPOINT_KEY: ENDPOINT, "dfe.hyperi.io/region": REGION, STORAGE_KEY: _storage(resolved)}


def test_a_fresh_aws_deploy_runs_cached_object_on_its_instance_store(resolved: Path) -> None:
    """No deploy-repo file at all: everything arrives on the cluster secret."""
    docs = clickhouse_docs("aws", aws_facts(resolved))
    cache_size = json.loads(_storage(resolved))["objectStore"]["cacheSize"]
    found = disks(docs)
    assert set(found) == {"s3_object", "s3_object_cache"}, found
    assert found["s3_object"]["endpoint"] == ENDPOINT
    assert found["s3_object"]["region"] == REGION
    assert 0 < _bytes(found["s3_object_cache"]["max_size"]) < _bytes(cache_size)
    pod = cluster(docs)["spec"]["podTemplate"]
    assert pod["volumes"] == [{"name": "object-cache", "emptyDir": {"sizeLimit": cache_size}}]
    assert pod["nodeSelector"]["dfe.hyperi.io/workload"] == "clickhouse"


def test_the_aws_bucket_is_reached_through_pod_identity(resolved: Path) -> None:
    """aws.yaml turns Pod Identity on, so no static key can shadow the role."""
    docs = clickhouse_docs("aws", aws_facts(resolved))
    assert "env" not in cluster(docs)["spec"]["containerTemplate"]
    assert not [
        d
        for d in docs
        if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == "dfe-clickhouse-s3"
    ]


def test_a_deploy_repo_overlay_cannot_move_what_the_first_sync_fixed(resolved: Path) -> None:
    overlay = yaml.safe_dump(
        {
            "clickhouse": {
                "objectStore": {"endpoint": MINIO, "cacheSize": "10Gi", "cache": {"volume": "pvc"}}
            }
        }
    )
    docs = clickhouse_docs("aws", aws_facts(resolved), overlay)
    assert disks(docs)["s3_object"]["endpoint"] == ENDPOINT
    limit = cluster(docs)["spec"]["podTemplate"]["volumes"][0]["emptyDir"]["sizeLimit"]
    assert limit == json.loads(_storage(resolved))["objectStore"]["cacheSize"]


def _bytes(quantity: object) -> int:
    """A Kubernetes quantity, or a ClickHouse byte count, in bytes."""
    text = str(quantity)
    for suffix, factor in (("Ti", 2**40), ("Gi", 2**30), ("Mi", 2**20), ("Ki", 2**10)):
        if text.endswith(suffix):
            return int(text.removesuffix(suffix)) * factor
    return int(text)


def test_the_cache_stops_short_of_the_volume_it_lives_on(resolved: Path) -> None:
    """The kubelet evicts a pod whose emptyDir passes its sizeLimit, and block rounding
    and the cache's own metadata sit on top of what max_size counts."""
    docs = clickhouse_docs("aws", aws_facts(resolved))
    limit = cluster(docs)["spec"]["podTemplate"]["volumes"][0]["emptyDir"]["sizeLimit"]
    max_size = disks(docs)["s3_object_cache"]["max_size"]
    assert _bytes(max_size) < _bytes(limit), (max_size, limit)


@pytest.mark.parametrize("cloud", ["aws", "gcp", "azure"])
def test_a_deploy_repo_storage_model_cannot_strand_the_cache(cloud: str, resolved: Path) -> None:
    """The secret carries the model with the cache placement it decided, so an overlay's
    `local` cannot leave an instance-store cache with no object store to front."""
    overlay = yaml.safe_dump({"clickhouse": {"storageModel": "local"}})
    facts = aws_facts(resolved) if cloud == "aws" else {STORAGE_KEY: _storage(resolved)}
    docs = clickhouse_docs(cloud, facts, overlay)
    if cloud == "aws":
        assert set(disks(docs)) == {"s3_object", "s3_object_cache"}
    else:
        assert disks(docs) == {}


@pytest.mark.parametrize("cloud", ["gcp", "azure"])
def test_the_storage_keys_land_only_beside_a_bucket(cloud: str, resolved: Path) -> None:
    """GCP and Azure shapes resolve an NVMe cache too, and no bucket reaches their chart yet."""
    docs = clickhouse_docs(cloud, {STORAGE_KEY: _storage(resolved)})
    assert disks(docs) == {}
    assert "volumes" not in cluster(docs)["spec"].get("podTemplate", {})


def test_an_on_prem_deploy_repo_endpoint_survives() -> None:
    """No bucket on the cluster secret, so the deploy repo's MinIO is the object store."""
    overlay = yaml.safe_dump({"clickhouse": {"objectStore": {"endpoint": MINIO}}})
    found = disks(clickhouse_docs("local", {}, overlay))
    assert set(found) == {"s3_object", "s3_object_cache"}, found
    assert found["s3_object"]["endpoint"] == MINIO
    assert "region" not in found["s3_object"]


def test_an_on_prem_deploy_with_no_object_store_stays_local() -> None:
    assert disks(clickhouse_docs("local", {})) == {}


def test_an_external_server_takes_no_storage_from_the_secret(resolved: Path) -> None:
    """The supplied ClickHouse owns its storage, so neither fact reaches the chart."""
    facts = {
        **aws_facts(resolved),
        "dfe.hyperi.io/clickhouse_mode": "external",
        "dfe.hyperi.io/clickhouse_host": "ch.example.com",
        "dfe.hyperi.io/clickhouse_port": "8443",
        "dfe.hyperi.io/clickhouse_native_port": "9440",
        "dfe.hyperi.io/clickhouse_secure": "true",
        "dfe.hyperi.io/clickhouse_user": "default",
    }
    docs = clickhouse_docs("aws", facts)
    assert not [d for d in docs if d.get("kind") == "ClickHouseCluster"]
