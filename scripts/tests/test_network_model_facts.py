#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_network_model_facts.py
#  Purpose:      Prove the pod and Service ranges a cloud root built the cluster
#                with reach network-policies' carve-out, and that the carve-out
#                takes only ranges no backend shares.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The cluster's own ranges, from the aws root to the network policies.

network-policies carves networkModel's ranges out of every internet allow, so a
pod granted egress to the outside cannot reach pod or Service space on those
ports. common.yaml's ranges are the on-prem cluster's. An EKS cluster hands
Service addresses out of a range EKS picks and pod addresses out of the VPC, so
the aws root emits both, bootstrap.sh writes them onto the cluster secret, and
layer2-platform.yaml hands them to the chart above any deploy-repo overlay.

The VPC's private subnets also hold the MSK brokers and every PrivateLink
endpoint (terraform/modules/kubernetes-cluster/aws/vpc.tf), so aws.yaml leaves
the pod range open: carving it would cut every in-VPC backend off its clients.
It leaves the Service range open too, because the VPC CNI policy agent enforces
an except as a deny over every other rule on the pod, DNS included.

    python3 -m pytest scripts/tests/test_network_model_facts.py -q

Needs helm on PATH.
"""

import functools
import ipaddress
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from _weave import helm, weave

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
RENDER_ENV = REPO_ROOT / "bootstrap" / "render_env.py"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
AWS_ROOT_OUTPUTS = REPO_ROOT / "terraform" / "environments" / "aws" / "outputs.tf"
CLUSTER_OUTPUTS = REPO_ROOT / "terraform" / "modules" / "kubernetes-cluster" / "aws" / "outputs.tf"
COMMON = REPO_ROOT / "argocd" / "values" / "common.yaml"
APPSET = Path("argocd/appsets/layer2-platform.yaml")

# The example VPC (terraform/environments/aws/example.tfvars.json) and the range
# EKS picked for Services on a cluster in it.
POD = "10.90.0.0/16"
SERVICE = "172.20.0.0/16"
POD_KEY = "dfe.hyperi.io/pod_cidr"
SERVICE_KEY = "dfe.hyperi.io/service_cidr"
AWS_RANGES = {POD_KEY: POD, SERVICE_KEY: SERVICE}

# A broker's interface in a private subnet, inside the pod range.
IN_VPC_BROKER = ipaddress.ip_address("10.90.3.7")

# ClickHouse Cloud and MSK declared, so every external purpose with a carve-out renders.
MANAGED = {
    "dfe.hyperi.io/clickhouse_mode": "external",
    "dfe.hyperi.io/clickhouse_port": "8443",
    "dfe.hyperi.io/clickhouse_native_port": "9440",
    "dfe.hyperi.io/kafka_mode": "external",
    "dfe.hyperi.io/kafka_provider": "msk",
}


def model() -> dict:
    return yaml.safe_load(COMMON.read_text(encoding="utf-8"))["networkModel"]


# ---------------------------------------------------------------- tofu + bootstrap


def test_the_aws_root_reads_both_ranges_off_the_cluster() -> None:
    outputs = AWS_ROOT_OUTPUTS.read_text(encoding="utf-8")
    assert 'output "DFE_POD_CIDR"' in outputs
    assert "module.cluster.network.cidr" in outputs.split('output "DFE_POD_CIDR"')[1].split("output ")[0]
    assert 'output "DFE_SERVICE_CIDR"' in outputs
    assert "module.cluster.service_cidr" in outputs.split('output "DFE_SERVICE_CIDR"')[1].split("output ")[0]
    cluster = CLUSTER_OUTPUTS.read_text(encoding="utf-8")
    assert "one(aws_eks_cluster.this.kubernetes_network_config[*].service_ipv4_cidr)" in cluster


def _bootstrap_block() -> str:
    """bootstrap.sh's own block, run on its own rather than copied here."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = lines.index('export DFE_POD_CIDR="${DFE_POD_CIDR:-}"')
    end = next(i for i, line in enumerate(lines) if line.startswith("export DFE_SERVICE_CIDR_ANNOTATION="))
    return "\n".join(lines[start : end + 1])


def _run_block(**env: str) -> subprocess.CompletedProcess:
    script = _bootstrap_block() + '\nprintf "%s\\n%s\\n" "$DFE_POD_CIDR_ANNOTATION" "$DFE_SERVICE_CIDR_ANNOTATION"'
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def test_both_ranges_write_both_annotations() -> None:
    done = _run_block(DFE_POD_CIDR=POD, DFE_SERVICE_CIDR=SERVICE)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [f'{POD_KEY}: "{POD}"', f'{SERVICE_KEY}: "{SERVICE}"']


def test_a_root_that_names_neither_writes_neither() -> None:
    """The local root emits none, and common.yaml's ranges then stand."""
    done = _run_block()
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["", ""]


@pytest.mark.parametrize(
    "value",
    ["10.90.0.0", "10.90.0.0/16,10.91.0.0/16", "0.0.0.0/0", "10.90.0.0/33", "fd00::/8"],
)
def test_expected_fail_a_range_no_ipblock_can_carve_is_refused(value: str) -> None:
    for name in ("DFE_POD_CIDR", "DFE_SERVICE_CIDR"):
        done = _run_block(**{name: value})
        assert done.returncode == 1, (name, value, done.stdout)
        assert f"{name} must be one IPv4 CIDR" in done.stderr, done.stderr


def test_the_cluster_secret_carries_both_annotations() -> None:
    rendered = subprocess.run(
        [sys.executable, str(RENDER_ENV)],
        input=CLUSTER_SECRET.read_text(encoding="utf-8"),
        env={
            "PATH": "/usr/bin:/bin",
            "DFE_POD_CIDR_ANNOTATION": f'{POD_KEY}: "{POD}"',
            "DFE_SERVICE_CIDR_ANNOTATION": f'{SERVICE_KEY}: "{SERVICE}"',
        },
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    annotations = yaml.safe_load(rendered)["metadata"]["annotations"]
    assert annotations[POD_KEY] == POD
    assert annotations[SERVICE_KEY] == SERVICE


# ------------------------------------------------------------------- the render


@functools.cache
def policies(cloud: str, facts: tuple[tuple[str, str], ...], overlays: tuple[tuple[str, str], ...] = ()) -> tuple[dict, ...]:
    """network-policies as layer2-platform's Application renders it.

    ``overlays`` are deploy-repo files under infra/, as (name, YAML text).
    """
    w = weave()
    doc = w.load_appset(REPO_ROOT, APPSET)
    element = next(
        e
        for generator in doc["spec"]["generators"]
        for nested in generator["matrix"]["generators"]
        for e in (nested.get("list") or {}).get("elements") or []
        if e["app"] == "network-policies"
    )
    target = w.Target(element["app"], "scale", cloud, annotations=facts)
    app = w.expand_template(doc["spec"]["template"], {**w.generator_params(target, None), **element}, helm())
    source = app["spec"]["sources"][0]
    chart = REPO_ROOT / source["path"]
    with tempfile.TemporaryDirectory() as tmp:
        deploy = Path(tmp)
        (deploy / "infra").mkdir()
        for name, text in overlays:
            (deploy / "infra" / name).write_text(text, encoding="utf-8")
        chain = []
        for raw in source["helm"].get("valueFiles") or []:
            path = deploy / raw.removeprefix("$values/") if raw.startswith("$values/") else (chart / raw).resolve()
            chain.append({"written": raw, "path": path, "present": path.is_file()})
        docs = w.render_source(
            source, chart=chart, release=element["app"], namespace=element["namespace"], chain=chain, helm=helm()
        )
    return tuple(d for d in docs if d.get("kind") == "NetworkPolicy")


def carve_outs(rendered: tuple[dict, ...]) -> list[set[str]]:
    """The except list of every internet allow, one set per rule."""
    found = [
        set(peer["ipBlock"].get("except") or [])
        for p in rendered
        for rule in p["spec"].get("egress") or []
        for peer in rule.get("to") or []
        if (peer.get("ipBlock") or {}).get("cidr") == "0.0.0.0/0"
    ]
    assert found, "no internet allow rendered"
    return found


def aws(*overlays: tuple[str, str]) -> list[set[str]]:
    return carve_outs(policies("aws", tuple(sorted({**AWS_RANGES, **MANAGED}.items())), overlays))


def test_an_aws_cluster_carves_link_local_only() -> None:
    expected = {model()["linkLocalCIDR"]}
    assert all(found == expected for found in aws()), aws()


def test_an_aws_carve_out_never_covers_the_service_range() -> None:
    """The VPC CNI policy agent enforces an except as a deny over every other rule on the pod.

    The baseline's DNS allow names ports and no destination, so an except over the
    Service range drops every lookup to the kube-dns ClusterIP inside it
    (aws/aws-network-policy-agent#326).
    """
    service = ipaddress.ip_network(SERVICE)
    for found in aws():
        assert not any(service.overlaps(ipaddress.ip_network(cidr)) for cidr in found), found


def test_a_broker_in_the_vpc_stays_reachable() -> None:
    """MSK brokers and PrivateLink endpoints sit in the private subnets pods draw from."""
    for found in aws():
        assert not any(IN_VPC_BROKER in ipaddress.ip_network(cidr) for cidr in found), found


def test_a_cluster_with_no_facts_keeps_the_common_ranges() -> None:
    m = model()
    expected = {m["podCIDR"], m["serviceCIDR"], m["linkLocalCIDR"]}
    found = carve_outs(policies("local", tuple(sorted(MANAGED.items()))))
    assert all(f == expected for f in found), found


def test_a_deploy_repo_overlay_cannot_move_a_cluster_fact() -> None:
    """The overlay carves both ranges so the facts show, and restates them stale."""
    m = model()
    stale = yaml.safe_dump(
        {
            "networkModel": {
                "podCIDR": m["podCIDR"],
                "serviceCIDR": m["serviceCIDR"],
                "carveOut": ["podCIDR", "serviceCIDR", "linkLocalCIDR"],
            }
        }
    )
    found = aws(("common.yaml", stale), ("network-policies.yaml", stale))
    assert all(f == {POD, SERVICE, m["linkLocalCIDR"]} for f in found), found


def test_the_pod_range_reaches_the_model_for_a_deployment_that_carves_it() -> None:
    carve = yaml.safe_dump({"networkModel": {"carveOut": ["podCIDR", "linkLocalCIDR"]}})
    found = aws(("network-policies.yaml", carve))
    assert all(f == {POD, model()["linkLocalCIDR"]} for f in found), found
