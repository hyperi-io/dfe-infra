#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_otel_k8s_cluster_pod_labels.py
#  Purpose:      Prove the gateway's k8s_cluster series carry their pod's part-of
#                and app labels, joined on the pod uid, and nothing else is enriched.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The pod labels on k8s_cluster's per-pod and per-container series.

A sum of k8s.container.cpu_request over a shared cluster counts every product's
containers. The gateway's metrics/k8s_cluster pipeline runs a k8s_attributes
processor that copies each pod's app.kubernetes.io/part-of and dfe.hyperi.io/app
labels onto its series, so a query can keep one product's containers. The
receiver's series arrive on no connection, so the join is the k8s.pod.uid the
receiver already sets, and a series whose pod carries no label passes through
without the attribute.

    python3 -m pytest scripts/tests/test_otel_k8s_cluster_pod_labels.py -q

Needs `helm` on PATH.
"""

import functools
import subprocess

import pytest
import yaml

from _charts import chart_dir

CHART = chart_dir("otel-collector")
PROCESSOR = "k8s_attributes/k8s_cluster"
PIPELINE = "metrics/k8s_cluster"
# Attribute name per pod label: the key itself, which the dfe-schemas usage rollups filter on.
LABEL_ATTRIBUTES = {
    "app.kubernetes.io/part-of": "app.kubernetes.io/part-of",
    "dfe.hyperi.io/app": "dfe.hyperi.io/app",
}
# k8s_cluster's documented allocatable types at 0.162.0 (receiver README).
NODE_ALLOCATABLE = {"cpu", "memory", "ephemeral-storage", "pods"}


def render(*sets: str) -> tuple[dict, ...]:
    """Every document `helm template` renders for the chart with `sets`."""
    cmd = ["helm", "template", "oc", str(CHART)]
    for item in sets:
        cmd += ["--set", item]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    assert out.returncode == 0, f"helm template failed for {sets}:\n{out.stderr}"
    return tuple(doc for doc in yaml.safe_load_all(out.stdout) if doc)


@functools.cache
def default_render() -> tuple[dict, ...]:
    return render()


def collector_config(docs: tuple[dict, ...], key: str) -> dict:
    (configmap,) = [d for d in docs if d["kind"] == "ConfigMap"]
    return yaml.safe_load(configmap["data"][key])


def gateway(docs: tuple[dict, ...]) -> dict:
    return collector_config(docs, "gateway-config.yaml")


def daemonset(docs: tuple[dict, ...]) -> dict:
    return collector_config(docs, "daemonset-config.yaml")


def processor() -> dict:
    return gateway(default_render())["processors"][PROCESSOR]


def only(docs: tuple[dict, ...], kind: str) -> dict:
    (doc,) = [d for d in docs if d["kind"] == kind]
    return doc


def test_the_k8s_cluster_pipeline_enriches_before_it_batches() -> None:
    processors = gateway(default_render())["service"]["pipelines"][PIPELINE]["processors"]
    assert processors[0] == "memory_limiter", processors
    assert PROCESSOR in processors, processors
    assert processors.index(PROCESSOR) < processors.index("batch"), processors


def test_it_joins_on_the_pod_uid_alone() -> None:
    """The receiver's series come from no pod connection, so an IP association matches nothing."""
    assert processor()["pod_association"] == [
        {"sources": [{"from": "resource_attribute", "name": "k8s.pod.uid"}]}
    ]


def test_it_copies_the_pod_labels_under_fixed_attribute_names() -> None:
    rules = processor()["extract"]["labels"]
    assert {rule["key"]: rule["tag_name"] for rule in rules} == LABEL_ATTRIBUTES
    assert all(rule.get("from", "pod") == "pod" for rule in rules), rules
    assert "annotations" not in processor()["extract"]


def test_it_adds_none_of_the_default_metadata() -> None:
    """An omitted list is the processor default: start time, deployment name, image tags."""
    assert processor()["extract"]["metadata"] == ["k8s.pod.uid"]


def test_no_other_pipeline_runs_it() -> None:
    """The daemonset already enriches what it pushes, and that arrives on the gateway's otlp."""
    docs = default_render()
    pipelines = gateway(docs)["service"]["pipelines"]
    running = [name for name, p in pipelines.items() if PROCESSOR in p["processors"]]
    assert running == [PIPELINE]
    assert PROCESSOR not in daemonset(docs)["processors"]


def test_turning_the_receiver_off_drops_the_processor() -> None:
    config = gateway(render("clusterTelemetry.k8sCluster.enabled=false"))
    assert PROCESSOR not in config["processors"]
    assert PIPELINE not in config["service"]["pipelines"]


@pytest.mark.parametrize("project", ["acme", "observability"])
def test_the_processor_names_no_product(project: str) -> None:
    """The label VALUE is the pod's own, so another project renders the same block."""
    assert gateway(render(f"project={project}"))["processors"][PROCESSOR] == processor()


def test_the_gateway_service_account_may_list_and_watch_pods() -> None:
    """Pods are the processor's only informer: no namespace, node or owner rule is configured."""
    docs = default_render()
    role = only(docs, "ClusterRole")
    binding = only(docs, "ClusterRoleBinding")
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    pod_rules = [
        rule
        for rule in role["rules"]
        if "" in rule.get("apiGroups", []) and "pods" in rule.get("resources", [])
    ]
    assert any({"get", "list", "watch"} <= set(rule["verbs"]) for rule in pod_rules), role["rules"]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": role["metadata"]["name"],
    }
    account = deployment["spec"]["template"]["spec"]["serviceAccountName"]
    assert {"kind": "ServiceAccount", "name": account} in [
        {"kind": s["kind"], "name": s["name"]} for s in binding["subjects"]
    ]


def test_allocatable_types_are_ones_a_node_reports() -> None:
    """A type the node does not report, such as storage, is skipped and emits nothing."""
    types = gateway(default_render())["receivers"]["k8s_cluster"]["allocatable_types_to_report"]
    assert set(types) <= NODE_ALLOCATABLE, types


def test_no_config_uses_the_deprecated_processor_name() -> None:
    """0.162.0 renamed k8sattributes to k8s_attributes and logs a warning for the old name."""
    docs = default_render()
    for config in (gateway(docs), daemonset(docs)):
        names = [name.split("/")[0] for name in config["processors"]]
        assert "k8sattributes" not in names, names


def test_no_config_uses_the_deprecated_receiver_name() -> None:
    """0.162.0 renamed sqlquery to sql_query and logs a warning for the old name."""
    docs = default_render()
    for config in (gateway(docs), daemonset(docs)):
        names = [name.split("/")[0] for name in config["receivers"]]
        assert "sqlquery" not in names, names
    pipeline = gateway(docs)["service"]["pipelines"]["metrics/clickhouse"]
    assert pipeline["receivers"] == ["sql_query/clickhouse"]
    assert "sql_query/clickhouse" in gateway(docs)["receivers"]


@pytest.mark.parametrize("project", ["dfe", "acme"])
def test_the_gateway_pod_carries_its_part_of_label_outside_the_selector(project: str) -> None:
    """The processor reads part-of off the pod, and a live Deployment refuses a changed selector."""
    deployment = only(render(f"project={project}"), "Deployment")
    labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/part-of"] == project, labels
    assert deployment["spec"]["selector"] == {
        "matchLabels": {"app.kubernetes.io/name": f"{project}-otel-collector-gateway"}
    }
    assert deployment["spec"]["selector"]["matchLabels"].items() <= labels.items()


@pytest.mark.parametrize("project", ["dfe", "acme"])
def test_the_daemonset_pod_carries_its_part_of_label_outside_the_selector(project: str) -> None:
    """The processor reads part-of off the pod, and a live DaemonSet refuses a changed selector."""
    daemonset_doc = only(render(f"project={project}"), "DaemonSet")
    labels = daemonset_doc["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/part-of"] == project, labels
    assert daemonset_doc["spec"]["selector"] == {
        "matchLabels": {"app.kubernetes.io/name": f"{project}-otel-collector-daemonset"}
    }
    assert daemonset_doc["spec"]["selector"]["matchLabels"].items() <= labels.items()
