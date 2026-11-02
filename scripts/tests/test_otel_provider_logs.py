#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_otel_provider_logs.py
#  Purpose:      Prove the otel-collector chart's provider-logs workloads render
#                per flavour, stay out of the gateway, and agree with the cloud
#                root on the ServiceAccount a Pod Identity role binds.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Render tests for clusterTelemetry.providerLogs.

    python3 scripts/tests/test_otel_provider_logs.py

Needs `helm` on PATH. The rendered collector configs are also run through
`otelcol validate` against the pinned image by hand: that needs a container
runtime, which this file does not.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "otel-collector"
AWS_ROOT = REPO_ROOT / "terraform" / "environments" / "aws"

AWS = (
    "aws.region=us-west-2",
    "aws.eksAuditLogGroup=/aws/eks/example/cluster",
    "aws.cloudTrailLogGroup=/aws/cloudtrail/example-cloudtrail",
)


def helm(flavour: str, *sets: str) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "otel", str(CHART), "--set", f"clusterTelemetry.providerLogs.flavour={flavour}"]
    for value in sets:
        cmd += ["--set", f"clusterTelemetry.providerLogs.{value}"]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def rendered(flavour: str, *sets: str) -> list[dict]:
    out = helm(flavour, *sets)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def named(docs: list[dict], kind: str, suffix: str) -> list[dict]:
    return [d for d in docs if d["kind"] == kind and d["metadata"]["name"].endswith(suffix)]


def collector_config(docs: list[dict], key: str) -> dict:
    (config_map,) = named(docs, "ConfigMap", "-provider-logs-config")
    return yaml.safe_load(config_map["data"][key])


def test_the_default_renders_no_provider_logs_object() -> None:
    docs = rendered("none")
    leftovers = [d["metadata"]["name"] for d in docs if "provider-logs" in d["metadata"]["name"]]
    expect("flavour none renders nothing of its own", not leftovers, repr(leftovers))


def test_the_aws_flavour_polls_the_named_groups_in_a_process_of_its_own() -> None:
    docs = rendered("aws", *AWS)
    (deployment,) = named(docs, "Deployment", "-provider-logs")
    expect("one replica, so no group is read twice", deployment["spec"]["replicas"] == 1)
    config = collector_config(docs, "config.yaml")
    receivers = config["receivers"]
    expect(
        "an aws_cloudwatch receiver per source",
        sorted(receivers) == ["aws_cloudwatch/cloudtrail", "aws_cloudwatch/eks_audit"],
        repr(sorted(receivers)),
    )
    eks = receivers["aws_cloudwatch/eks_audit"]
    expect("region comes from values", eks["region"] == "us-west-2", repr(eks))
    expect(
        "it names exactly the EKS group",
        list(eks["logs"]["groups"]["named"]) == ["/aws/eks/example/cluster"],
        repr(eks),
    )
    expect("and keeps offsets", eks["storage"] == "file_storage/provider_logs", repr(eks))
    pipelines = config["service"]["pipelines"]
    expect(
        "each source has its own pipeline into the gateway",
        all(p["exporters"] == ["otlp/gateway"] for p in pipelines.values()) and len(pipelines) == 2,
        repr(pipelines),
    )
    attrs = {a["key"]: a["value"] for a in config["processors"]["resource/eks_audit"]["attributes"]}
    expect(
        "rows carry the provider and the source",
        attrs["cloud.provider"] == "aws" and attrs["dfe.provider_log.source"] == "eks-audit",
        repr(attrs),
    )


def test_the_aws_flavour_leaves_a_source_out_when_its_group_is_empty() -> None:
    docs = rendered("aws", "aws.region=us-west-2", "aws.eksAuditLogGroup=/aws/eks/example/cluster")
    receivers = collector_config(docs, "config.yaml")["receivers"]
    expect("no CloudTrail receiver without its group", sorted(receivers) == ["aws_cloudwatch/eks_audit"], repr(receivers))


def test_the_gateway_and_node_agent_configs_do_not_change() -> None:
    """A receiver that can exit its process must never be in the gateway."""
    base = {d["metadata"]["name"]: d for d in rendered("none") if d["kind"] == "ConfigMap"}
    for flavour, sets in (("aws", AWS), ("azure", ("azure.connectionSecretName=hub",)), ("rancher", ())):
        config_map = {d["metadata"]["name"]: d for d in rendered(flavour, *sets) if d["kind"] == "ConfigMap"}
        expect(
            f"{flavour}: the main config map is byte-identical to flavour none",
            config_map["dfe-otel-collector-config"]["data"] == base["dfe-otel-collector-config"]["data"],
        )


def test_the_service_account_is_the_one_the_cloud_root_binds() -> None:
    docs = rendered("aws", *AWS)
    (account,) = named(docs, "ServiceAccount", "-provider-logs")
    root = (AWS_ROOT / "provider_logs.tf").read_text(encoding="utf-8")
    bound = re.search(r'provider_logs_service_account\s*=\s*"([^"]+)"', root)
    expect(
        "the chart's account name is the Pod Identity association's",
        bound is not None and bound.group(1) == account["metadata"]["name"],
        f"{bound and bound.group(1)} vs {account['metadata']['name']}",
    )
    (deployment,) = named(docs, "Deployment", "-provider-logs")
    expect(
        "and the pod runs as it",
        deployment["spec"]["template"]["spec"]["serviceAccountName"] == account["metadata"]["name"],
    )
    namespace = re.search(r'provider_logs_namespace\s*=\s*"([^"]+)"', root)
    appset = (REPO_ROOT / "argocd" / "appsets" / "layer2-data.yaml").read_text(encoding="utf-8")
    expect(
        "in the namespace the appset places the collector in",
        namespace is not None and re.search(rf"app: otel-collector\s+namespace: {namespace.group(1)}\b", appset) is not None,
    )


def test_a_flavour_that_names_nothing_to_read_is_refused() -> None:
    cases = {
        "aws no region": ("aws", ("aws.eksAuditLogGroup=/g",), "aws.region is empty"),
        "aws no group": ("aws", ("aws.region=us-west-2",), "no log group is named"),
        "azure no secret": ("azure", (), "azure.connectionSecretName is empty"),
        "gcp no subscription": ("gcp", (), "gcp.subscription is empty"),
        "rancher no path": ("rancher", ("rancher.auditLogPath=",), "rancher.auditLogPath is empty"),
        "unknown flavour": ("splunk", (), "one of none, aws, azure, gcp, rancher"),
    }
    for label, (flavour, sets, message) in cases.items():
        out = helm(flavour, *sets)
        expect(f"{label} fails the render", out.returncode != 0 and message in out.stderr, out.stderr[-300:])


def test_azure_and_gcp_render_their_receiver_from_values() -> None:
    azure = collector_config(rendered("azure", "azure.connectionSecretName=hub"), "config.yaml")
    expect("azure reads the Event Hub", "azure_event_hub" in azure["receivers"], repr(azure["receivers"]))
    deployment = named(rendered("azure", "azure.connectionSecretName=hub"), "Deployment", "-provider-logs")[0]
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    secret = next(e for e in env if e["name"] == "AZURE_EVENT_HUB_CONNECTION")["valueFrom"]["secretKeyRef"]
    expect("its connection string comes from the named Secret", secret["name"] == "hub", repr(secret))
    gcp = collector_config(rendered("gcp", "gcp.subscription=projects/p/subscriptions/s"), "config.yaml")
    expect(
        "gcp pulls the named subscription",
        gcp["receivers"]["googlecloudpubsub"]["subscription"] == "projects/p/subscriptions/s",
        repr(gcp["receivers"]),
    )


def test_rancher_takes_its_path_node_selector_and_tolerations_from_values() -> None:
    docs = rendered("rancher")
    (daemonset,) = named(docs, "DaemonSet", "-provider-logs")
    pod = daemonset["spec"]["template"]["spec"]
    expect("selected onto the control plane by RKE2's label", pod["nodeSelector"] == {"node-role.kubernetes.io/control-plane": "true"}, repr(pod.get("nodeSelector")))
    expect("tolerating every taint by default", pod["tolerations"] == [{"operator": "Exists"}], repr(pod.get("tolerations")))
    config = collector_config(docs, "config.yaml")
    expect(
        "reading RKE2's documented audit log path",
        config["receivers"]["filelog/kube_apiserver_audit"]["include"] == ["/var/lib/rancher/rke2/server/logs/audit.log"],
    )

    custom = rendered(
        "rancher",
        "rancher.auditLogPath=/var/log/kube-audit/audit.log",
        "rancher.nodeSelector.example\\.com/plane=control",
        "rancher.nodeSelector.node-role\\.kubernetes\\.io/control-plane=null",
    )
    (custom_ds,) = named(custom, "DaemonSet", "-provider-logs")
    custom_pod = custom_ds["spec"]["template"]["spec"]
    expect("a deployer's selector replaces the default", custom_pod["nodeSelector"] == {"example.com/plane": "control"}, repr(custom_pod.get("nodeSelector")))
    mount = custom_pod["volumes"][1]["hostPath"]["path"]
    expect("a deployer's path is the mounted directory", mount == "/var/log/kube-audit", mount)
    expect(
        "and the receiver reads it",
        collector_config(custom, "config.yaml")["receivers"]["filelog/kube_apiserver_audit"]["include"] == ["/var/log/kube-audit/audit.log"],
    )


def test_a_missing_audit_log_does_not_stop_the_pod_starting() -> None:
    (daemonset,) = named(rendered("rancher"), "DaemonSet", "-provider-logs")
    audit = next(v for v in daemonset["spec"]["template"]["spec"]["volumes"] if v["name"] == "audit")
    expect(
        "the log's directory is created empty rather than refusing the pod",
        audit["hostPath"]["type"] == "DirectoryOrCreate",
        repr(audit),
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
