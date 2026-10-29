#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_external_egress.py
#  Purpose:      Prove the derived external-service egress opens for exactly the
#                declared mode, on exactly the ports that mode speaks, and stays
#                shut otherwise.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Per-mode assertions for network-policies external-service egress.

An over-wide egress policy renders green and passes a server-side dry-run -- the
API server has no opinion about a port an app never uses. So the assertion that
matters is set EQUALITY: this mode, these apps, these ports, and nothing else.
The negative half matters equally, because a policy that renders when no external
dependency was declared is a hole nobody asked for.

    python3 scripts/tests/test_external_egress.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _charts import CHART_TREES
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "network-policies"
COMMON = REPO_ROOT / "argocd" / "values" / "common.yaml"
APP_LABEL = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))["appLabel"]

CH_PORTS = {8443, 9440}
KAFKA_PORTS = {9092, 9093, 9094, 9095, 9096, 9098}
CH_APPS = {
    "dfe-loader",
    "dfe-engine",
    "dfe-hunt-runner",
    "dfe-keda-shim",
    "dfe-hyperdx",
}
KAFKA_APPS = {
    # The engine creates the bootstrap and source topics, and its readiness waits on them.
    "dfe-engine",
    "dfe-receiver",
    "dfe-loader",
    "dfe-fetcher",
    "dfe-archiver",
    "dfe-transform-vrl",
    "dfe-transform-vector",
    "dfe-transform-elastic",
    # dfe-transform-splack and dfe-transform-wasm are coming: alpha, unpublished,
    # uncomment when they ship and the chart's kafka.apps list carries them again.
    # "dfe-transform-splack",
    # "dfe-transform-wasm",
    # The MSK bootstrap Job, in the strimzi namespace rather than beside the apps.
    "dfe-kafka",
}
# The engine writes what it mints to the store, and the receiver reads its bearer tokens there.
SECRETS_APPS = {"dfe-engine", "dfe-receiver"}


def render(*sets: str) -> list[dict]:
    """Render the chart with the shared SSoT values plus --set overrides."""
    cmd = ["helm", "template", "network-policies", str(CHART), "-f", str(COMMON)]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def external(docs: list[dict], purpose: str | None = None) -> list[dict]:
    """The derived policies only -- internetEgress and the baseline are not ours."""
    prefix = f"allow-external-{purpose}-egress-" if purpose else "allow-external-"
    return [
        d
        for d in docs
        if d.get("kind") == "NetworkPolicy"
        and d["metadata"]["name"].startswith(prefix)
    ]


def app_of(policy: dict) -> str:
    """The app a policy selects by app.kubernetes.io/name or the app label, "*" for every pod."""
    labels = policy["spec"]["podSelector"].get("matchLabels") or {}
    if not labels:
        return "*"
    return labels.get("app.kubernetes.io/name") or labels[APP_LABEL]


def apps_of(policies: list[dict]) -> set[str]:
    return {app_of(p) for p in policies}


def ports_of(policies: list[dict]) -> set[int]:
    ports = set()
    for p in policies:
        for rule in p["spec"]["egress"]:
            for port in rule.get("ports", []):
                ports.add(port["port"])
    return ports


def namespaces_of(policies: list[dict]) -> set[str]:
    return {p["metadata"]["namespace"] for p in policies}


def test_all_internal_opens_nothing() -> None:
    """The shipped SSoT declares no external dependency, so nothing may render."""
    policies = external(render())
    expect("all-internal renders no external egress", policies == [], f"got {len(policies)}")


def test_clickhouse_external_on() -> None:
    policies = external(render("clickhouse.mode=external"), "clickhouse")
    expect("external CH renders the CH clients", apps_of(policies) == CH_APPS,
           f"got {sorted(apps_of(policies))}")
    expect("external CH grants only the CH ports", ports_of(policies) == CH_PORTS,
           f"got {sorted(ports_of(policies))}")
    expect(
        "external CH opens the app namespace and no other",
        namespaces_of(policies) == {"dfe-local"},
        f"got {sorted(namespaces_of(policies))}",
    )


def test_clickhouse_external_excludes_the_internet_egress_apps() -> None:
    """The failure the per-purpose model exists to stop: a fetcher on CH ports."""
    policies = external(render("clickhouse.mode=external"), "clickhouse")
    leaked = apps_of(policies) & {"dfe-fetcher", "dfe-archiver"}
    expect("external CH does not reach the fetcher or archiver", leaked == set(), f"got {leaked}")


def test_clickhouse_internal_modes_open_nothing() -> None:
    for mode in ("cluster", "single"):
        policies = external(render(f"clickhouse.mode={mode}"), "clickhouse")
        expect(f"clickhouse.mode={mode} renders no CH egress", policies == [],
               f"got {len(policies)}")


def test_kafka_external_on() -> None:
    policies = external(render("kafka.mode=external"), "kafka")
    expect("external Kafka renders the Kafka clients", apps_of(policies) == KAFKA_APPS,
           f"got {sorted(apps_of(policies))}")
    expect("external Kafka grants only the broker ports", ports_of(policies) == KAFKA_PORTS,
           f"got {sorted(ports_of(policies))}")
    expect(
        "external Kafka reaches the MSK bootstrap Job's namespace",
        namespaces_of(policies) == {"dfe-local", "strimzi"},
        f"got {sorted(namespaces_of(policies))}",
    )


def ports_by_app(policies: list[dict]) -> dict[str, set[int]]:
    found: dict[str, set[int]] = {}
    for p in policies:
        found.setdefault(app_of(p), set()).update(
            port["port"] for rule in p["spec"]["egress"] for port in rule.get("ports", [])
        )
    return found


def test_msk_opens_scram_to_the_apps_and_iam_to_the_bootstrap_job_alone() -> None:
    policies = external(render("kafka.mode=external", "kafka.external.provider=msk"), "kafka")
    by_app = ports_by_app(policies)
    expect("every app gets SCRAM alone",
           all(ports == {9096} for app, ports in by_app.items() if app != "dfe-kafka"),
           f"got {by_app}")
    expect("the bootstrap Job gets IAM alone", by_app.get("dfe-kafka") == {9098}, f"got {by_app}")
    expect("the clients are the generic list's", set(by_app) == KAFKA_APPS, f"got {sorted(by_app)}")


def test_confluent_opens_9092_and_reaches_no_bootstrap_job() -> None:
    policies = external(render("kafka.mode=external", "kafka.external.provider=confluent-cloud"), "kafka")
    expect("Confluent's one Kafka port", ports_of(policies) == {9092}, f"got {sorted(ports_of(policies))}")
    expect("no policy lands in strimzi, where no Job dials Confluent",
           namespaces_of(policies) == {"dfe-local"}, f"got {sorted(namespaces_of(policies))}")


def test_redpanda_opens_its_seed_and_broker_ports() -> None:
    policies = external(render("kafka.mode=external", "kafka.external.provider=redpanda-cloud"), "kafka")
    expect("Redpanda's seed and broker ports", ports_of(policies) == {9092, 9093, 9094, 9095},
           f"got {sorted(ports_of(policies))}")
    expect("no policy lands in strimzi", namespaces_of(policies) == {"dfe-local"},
           f"got {sorted(namespaces_of(policies))}")


def test_an_in_cluster_provider_keeps_the_generic_range() -> None:
    """A provider with no entry is a broker the deployer brought, so the range stays."""
    policies = external(render("kafka.mode=external", "kafka.external.provider=strimzi"), "kafka")
    expect("the generic range", ports_of(policies) == KAFKA_PORTS, f"got {sorted(ports_of(policies))}")


def test_kafka_external_does_not_open_clickhouse() -> None:
    """Purposes are independent: one declaration must not open another's ports."""
    docs = render("kafka.mode=external")
    expect("external Kafka renders no CH egress", external(docs, "clickhouse") == [],
           "CH policies rendered")


def test_kafka_internal_modes_open_nothing() -> None:
    for mode in ("disabled", "single", "cluster"):
        policies = external(render(f"kafka.mode={mode}"), "kafka")
        expect(f"kafka.mode={mode} renders no Kafka egress", policies == [],
               f"got {len(policies)}")


def test_oidc_is_engine_only_on_443() -> None:
    policies = external(render("oidc.enabled=true"), "oidc")
    expect("external OIDC is the engine alone", apps_of(policies) == {"dfe-engine"},
           f"got {sorted(apps_of(policies))}")
    expect("external OIDC grants only 443", ports_of(policies) == {443},
           f"got {sorted(ports_of(policies))}")


def test_secret_backend_derives_from_the_address() -> None:
    policies = external(render("vault.address=https://bao.example.com:8200"), "secrets")
    expect("secret backend is the engine and the receiver's bearer tokens alone",
           apps_of(policies) == SECRETS_APPS, f"got {sorted(apps_of(policies))}")
    expect("secret backend grants the OpenBao and KMS ports",
           ports_of(policies) == {8200, 443}, f"got {sorted(ports_of(policies))}")


def test_an_aws_store_opens_the_engine_and_receiver_on_https_alone() -> None:
    """Secrets Manager and KMS answer on 443 only, so the OpenBao port stays shut."""
    policies = external(render("secrets.provider=aws"), "secrets")
    expect("an aws store is the engine and the receiver alone", apps_of(policies) == SECRETS_APPS,
           f"got {sorted(apps_of(policies))}")
    expect("an aws store grants only 443", ports_of(policies) == {443},
           f"got {sorted(ports_of(policies))}")


def test_elasticsearch_does_not_reach_the_transform() -> None:
    """dfe-transform-elastic is Kafka-to-Kafka and has no Elasticsearch client."""
    policies = external(render("elasticsearch.endpoint=https://es.example.com:9200"),
                        "elasticsearch")
    expect("elasticsearch is the engine alone", apps_of(policies) == {"dfe-engine"},
           f"got {sorted(apps_of(policies))}")
    expect("elasticsearch grants only the index-server ports",
           ports_of(policies) == {9200, 443}, f"got {sorted(ports_of(policies))}")


def test_external_otel_is_namespace_wide() -> None:
    """Every DFE pod emits OTLP, so this purpose selects the namespace, not a list."""
    policies = external(render("telemetry.mode=external"), "otel")
    expect("external OTel selects every pod", apps_of(policies) == {"*"},
           f"got {sorted(apps_of(policies))}")
    expect("external OTel grants the OTLP ports", ports_of(policies) == {4317, 4318, 443},
           f"got {sorted(ports_of(policies))}")


def test_otel_endpoint_override_also_declares() -> None:
    policies = external(render("otel.endpoint=otlp.example.com:4317"), "otel")
    expect("a set otel.endpoint declares an external destination", len(policies) == 1,
           f"got {len(policies)}")


def test_pinned_cidr_drops_the_shared_carve_out() -> None:
    """Kubernetes rejects an `except` outside its own `cidr` -- proven live."""
    policies = external(
        render("clickhouse.mode=external", "externalEgress.clickhouse.allowCIDRs={203.0.113.7/32}"),
        "clickhouse",
    )
    blocks = [r["to"][0]["ipBlock"] for p in policies for r in p["spec"]["egress"]]
    expect("a pinned purpose keeps only its own CIDR",
           all(b["cidr"] == "203.0.113.7/32" for b in blocks), f"got {blocks}")
    expect("a pinned purpose emits no out-of-range except",
           all("except" not in b for b in blocks), f"got {blocks}")


def test_default_carve_out_survives() -> None:
    policies = external(render("clickhouse.mode=external"), "clickhouse")
    blocks = [r["to"][0]["ipBlock"] for p in policies for r in p["spec"]["egress"]]
    expect(
        "the unpinned default keeps the lateral-movement carve-out",
        all(set(b.get("except", [])) == {"198.18.0.0/16", "198.19.0.0/16", "169.254.0.0/16"}
            for b in blocks),
        f"got {blocks}",
    )


def test_the_network_model_is_declared_once() -> None:
    """Every carve-out reads networkModel; a second literal is a range that drifts."""
    offenders = []
    for values in sorted(v for tree in CHART_TREES for v in tree.glob("*/values.yaml")):
        in_model = False
        for number, line in enumerate(values.read_text(encoding="utf-8").splitlines(), 1):
            if not line.startswith(" "):
                in_model = line.startswith("networkModel:")
            if in_model:
                continue
            if "198.18.0.0/16" in line or "198.19.0.0/16" in line:
                offenders.append(f"{values.relative_to(REPO_ROOT)}:{number}")
    expect("the cluster ranges are named once per chart", offenders == [], f"got {offenders}")


def test_a_deployment_can_move_the_ranges() -> None:
    """A cluster on different ranges changes networkModel and nothing else."""
    policies = external(
        render(
            "clickhouse.mode=external",
            "networkModel.podCIDR=10.42.0.0/16",
            "networkModel.serviceCIDR=10.43.0.0/16",
        ),
        "clickhouse",
    )
    blocks = [r["to"][0]["ipBlock"] for p in policies for r in p["spec"]["egress"]]
    expect(
        "the carve-out follows the model",
        all(set(b.get("except", [])) == {"10.42.0.0/16", "10.43.0.0/16", "169.254.0.0/16"}
            for b in blocks),
        f"got {blocks}",
    )


def test_internet_egress_is_unchanged() -> None:
    """Who gets internet egress, and on which ports, must not move when external egress lands."""
    docs = render("clickhouse.mode=external", "kafka.mode=external")
    internet = [
        d for d in docs
        if d.get("kind") == "NetworkPolicy"
        and d["metadata"]["name"].startswith("allow-internet-egress-")
    ]
    expect("internetEgress names the fetcher, archiver and hunt runner",
           apps_of(internet) == {"dfe-fetcher", "dfe-archiver", "dfe-hunt-runner"},
           f"got {sorted(apps_of(internet))}")
    expect("internetEgress still grants 443 and 80", ports_of(internet) == {443, 80},
           f"got {sorted(ports_of(internet))}")


def test_every_app_policy_has_a_twin_selecting_its_instances() -> None:
    """A per-config instance's app.kubernetes.io/name is <app>-<source>, so the app label selects it."""
    docs = render("kafka.mode=external", "clickhouse.mode=external", "telemetry.mode=external")
    policies = {
        d["metadata"]["name"]: d
        for d in docs
        if d.get("kind") == "NetworkPolicy"
        and d["metadata"]["name"].startswith(("allow-external-", "allow-internet-egress-"))
    }
    by_name = [
        p for p in policies.values()
        if "app.kubernetes.io/name" in (p["spec"]["podSelector"].get("matchLabels") or {})
    ]
    expect("the purposes render policies by name", len(by_name) > 1, f"got {len(by_name)}")
    for policy in by_name:
        name = policy["metadata"]["name"]
        twin = policies.get(f"{name}-instances")
        expect(f"{name} has an instances twin", twin is not None)
        if twin is None:
            continue
        expect(f"{name}-instances selects the app by {APP_LABEL}",
               twin["spec"]["podSelector"] == {"matchLabels": {APP_LABEL: app_of(policy)}},
               f"got {twin['spec']['podSelector']}")
        expect(f"{name}-instances opens what {name} opens",
               (twin["metadata"]["namespace"], twin["spec"]["egress"])
               == (policy["metadata"]["namespace"], policy["spec"]["egress"]))
    otel = sorted(n for n in policies if n.startswith("allow-external-otel-"))
    expect("the namespace-wide policy renders once", otel == ["allow-external-otel-egress-all"],
           f"got {otel}")


def test_an_empty_app_label_renders_the_name_policies_alone() -> None:
    names = [
        d["metadata"]["name"]
        for d in render("kafka.mode=external", "appLabel=")
        if d.get("kind") == "NetworkPolicy"
    ]
    expect("the name policies still render", "allow-external-kafka-egress-dfe-fetcher" in names,
           f"got {names}")
    expect("and no instances twin", not [n for n in names if n.endswith("-instances")],
           f"got {names}")


def main() -> int:
    with standalone():
        test_all_internal_opens_nothing()
        test_clickhouse_external_on()
        test_clickhouse_external_excludes_the_internet_egress_apps()
        test_clickhouse_internal_modes_open_nothing()
        test_kafka_external_on()
        test_msk_opens_scram_to_the_apps_and_iam_to_the_bootstrap_job_alone()
        test_confluent_opens_9092_and_reaches_no_bootstrap_job()
        test_redpanda_opens_its_seed_and_broker_ports()
        test_an_in_cluster_provider_keeps_the_generic_range()
        test_kafka_external_does_not_open_clickhouse()
        test_kafka_internal_modes_open_nothing()
        test_oidc_is_engine_only_on_443()
        test_secret_backend_derives_from_the_address()
        test_an_aws_store_opens_the_engine_and_receiver_on_https_alone()
        test_elasticsearch_does_not_reach_the_transform()
        test_external_otel_is_namespace_wide()
        test_otel_endpoint_override_also_declares()
        test_pinned_cidr_drops_the_shared_carve_out()
        test_default_carve_out_survives()
        test_the_network_model_is_declared_once()
        test_a_deployment_can_move_the_ranges()
        test_internet_egress_is_unchanged()
        test_every_app_policy_has_a_twin_selecting_its_instances()
        test_an_empty_app_label_renders_the_name_policies_alone()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
