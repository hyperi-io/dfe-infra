#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_managed_backend_egress.py
#  Purpose:      Prove every pod that is given a managed backend's address has a
#                network policy opening that backend's ports, on every managed
#                backend the aws root builds.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every client of a managed backend can reach it, on that backend's own ports.

The network-policies chart names each external purpose's clients by hand, and a
client missing from that list is a pod that sits in default-deny with nothing in
the policy set to say why. So the list is checked against what the charts
render: for each managed backend, every app and data chart is rendered as Argo
renders it for that backend's cluster-secret facts, a workload whose env, args
or ConfigMaps carry the backend's address is a client, and network-policies --
rendered from layer2-platform's own Application for the same facts -- has to
open each client's ports in its namespace, or leave the namespace unisolated.

A per-config app is rendered again as one instance bound to a source, under the
<app>-<source> name dfe-engine gives it, so a policy naming the app is held to
reach every instance of it and no other app's.

The ports are offering.yaml's (terraform/modules/managed-backends), the one list
the tofu roots read; the chart's copy is held equal to it here too.

    python3 -m pytest scripts/tests/test_managed_backend_egress.py -q

Needs helm on PATH and the scalo-service library (_weave.library). Every host is
under the reserved .invalid domain.
"""

import functools
import ipaddress
from pathlib import Path

import pytest
import yaml

from _weave import CONTRACTS, helm, render_app, weave

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OFFERING = REPO_ROOT / "terraform" / "modules" / "managed-backends" / "offering.yaml"
CHART_VALUES = REPO_ROOT / "helm" / "charts" / "network-policies" / "values.yaml"
MANIFEST = REPO_ROOT / "apps.yaml"
APPSETS = Path("argocd/appsets")

CELL = ("scale", "aws")
# An address outside every cluster range the chart carves out, standing in for a
# managed backend's public endpoint.
OUTSIDE = ipaddress.ip_address("192.0.2.10")

# A host per role, so a client of the bootstrap path is told apart from an app.
CLIENT_HOST = "b-1.broker.example.invalid"
BOOTSTRAP_HOST = "iam-1.broker.example.invalid"
CLICKHOUSE_HOST = "abc123.clickhouse.example.invalid"

KAFKA_FACTS = {
    "msk": {
        "dfe.hyperi.io/kafka_bootstrap": f"{CLIENT_HOST}:9096,b-2.broker.example.invalid:9096",
        "dfe.hyperi.io/kafka_bootstrap_iam": f"{BOOTSTRAP_HOST}:9098",
    },
    "confluent-cloud": {"dfe.hyperi.io/kafka_bootstrap": f"{CLIENT_HOST}:9092"},
    "redpanda-cloud": {"dfe.hyperi.io/kafka_bootstrap": f"{CLIENT_HOST}:9092"},
}

# ClickHouse Cloud rides beside each broker, so one render set proves both.
CLICKHOUSE_FACTS = {
    "dfe.hyperi.io/clickhouse_mode": "external",
    "dfe.hyperi.io/clickhouse_host": CLICKHOUSE_HOST,
    "dfe.hyperi.io/clickhouse_port": "8443",
    "dfe.hyperi.io/clickhouse_native_port": "9440",
    "dfe.hyperi.io/clickhouse_secure": "true",
    "dfe.hyperi.io/clickhouse_user": "default",
}

POD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")


def offering() -> dict:
    return yaml.safe_load(OFFERING.read_text(encoding="utf-8"))


def facts(provider: str) -> dict[str, str]:
    """The cluster secret bootstrap.sh writes for one managed broker plus ClickHouse Cloud."""
    return {
        "dfe.hyperi.io/kafka_mode": "external",
        "dfe.hyperi.io/kafka_provider": provider,
        "dfe.hyperi.io/kafka_security_protocol": "SASL_SSL",
        **KAFKA_FACTS[provider],
        **CLICKHOUSE_FACTS,
    }


# ------------------------------------------------------------------- rendering


def _target(app: str, provider: str) -> object:
    return weave().Target(app, *CELL, annotations=tuple(sorted(facts(provider).items())))


def render_element(appset: str, element: dict, provider: str) -> list[dict]:
    """One list element of an appset, as its generated Application renders it.

    The deploy repo's overlays are absent, as on a deployment that wrote none.
    """
    w = weave()
    doc = w.load_appset(REPO_ROOT, APPSETS / appset)
    params = {**w.generator_params(_target(element["app"], provider), None), **element}
    app = w.expand_template(doc["spec"]["template"], params, helm())
    source = app["spec"]["sources"][0]
    chart = REPO_ROOT / source["path"]
    chain = []
    for raw in source["helm"].get("valueFiles") or []:
        path = None if raw.startswith("$") else (chart / raw).resolve()
        chain.append({"written": raw, "path": path, "present": path is not None and path.is_file()})
    docs = w.render_source(
        source,
        chart=chart,
        release=element["app"],
        namespace=element["namespace"],
        chain=chain,
        helm=helm(),
    )
    for d in docs:
        d.setdefault("metadata", {}).setdefault("namespace", element["namespace"])
    return docs


def _elements(appset: str) -> list[dict]:
    """The list generator's elements of an appset, every chart it deploys."""
    doc = yaml.safe_load((REPO_ROOT / APPSETS / appset).read_text(encoding="utf-8"))
    found = []
    for generator in doc["spec"]["generators"]:
        for nested in (generator.get("matrix") or {}).get("generators") or []:
            found += (nested.get("list") or {}).get("elements") or []
    return found


@functools.cache
def rendered(provider: str) -> tuple[dict, ...]:
    """Every app and data chart, rendered for one managed broker and ClickHouse Cloud."""
    docs: list[dict] = []
    namespace = weave().Target("x", *CELL).namespace
    for contract in sorted(CONTRACTS.glob("*.json")):
        for d in render_app(contract.stem, *CELL, annotations=facts(provider)):
            d.setdefault("metadata", {}).setdefault("namespace", namespace)
            docs.append(d)
    for element in _elements("layer2-data.yaml"):
        docs += render_element("layer2-data.yaml", element, provider)
    return tuple(docs)


@functools.cache
def policies(provider: str) -> tuple[dict, ...]:
    element = next(e for e in _elements("layer2-platform.yaml") if e["app"] == "network-policies")
    docs = render_element("layer2-platform.yaml", element, provider)
    return tuple(d for d in docs if d.get("kind") == "NetworkPolicy")


# The source each per-config instance below is bound to.
SOURCE = "flowx"


def per_config_apps() -> list[str]:
    """The apps the manifest deploys once per source, each under its own name."""
    apps = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["apps"]
    return sorted(name for name, shape in apps.items() if shape["multiplicity"] == "per_config")


def instance_overlay(app: str) -> dict:
    """The identity dfe-engine writes into a per-config instance's overlay.

    dfe-engine appmgmt/instances.py initial_overlay names the instance <app>-<source>,
    and its dfe-common component that name less the project prefix.
    """
    name = f"{app}-{SOURCE}"
    return {
        "fullnameOverride": name,
        "component": name.removeprefix("dfe-"),
        "otel": {"serviceName": name},
    }


@functools.cache
def rendered_instance(app: str, provider: str) -> tuple[dict, ...]:
    """One per-config instance of ``app``, bound to SOURCE, as its Application renders it."""
    namespace = weave().Target("x", *CELL).namespace
    overlay = instance_overlay(app)
    docs = render_app(app, *CELL, annotations=facts(provider), instance=SOURCE, overlay=overlay)
    for d in docs:
        d.setdefault("metadata", {}).setdefault("namespace", namespace)
    return tuple(docs)


# --------------------------------------------------------------------- clients


def _pod_template(doc: dict) -> dict:
    spec = doc.get("spec") or {}
    if doc.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    return spec.get("template") or {}


def _workload_text(doc: dict, configmaps: dict[tuple[str, str], dict]) -> str:
    """The pod spec and every ConfigMap it reads, as one text to search for an address."""
    pod = _pod_template(doc)
    namespace = doc["metadata"]["namespace"]
    names = set()
    spec = pod.get("spec") or {}
    for container in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        for source in container.get("envFrom") or []:
            names.add((source.get("configMapRef") or {}).get("name"))
        for entry in container.get("env") or []:
            names.add(((entry.get("valueFrom") or {}).get("configMapKeyRef") or {}).get("name"))
    for volume in spec.get("volumes") or []:
        names.add((volume.get("configMap") or {}).get("name"))
        for projected in (volume.get("projected") or {}).get("sources") or []:
            names.add((projected.get("configMap") or {}).get("name"))
    parts = [yaml.safe_dump(pod)]
    parts += [
        yaml.safe_dump(configmaps[(namespace, n)]) for n in names if (namespace, n) in configmaps
    ]
    return "\n".join(parts)


def clients(provider: str, host: str) -> list[tuple[str, dict, str]]:
    """(namespace, pod labels, Kind/name) of every workload given ``host``."""
    return clients_in(rendered(provider), host)


def clients_in(docs: tuple[dict, ...], host: str) -> list[tuple[str, dict, str]]:
    """(namespace, pod labels, Kind/name) of every workload in ``docs`` given ``host``."""
    configmaps = {
        (d["metadata"]["namespace"], d["metadata"]["name"]): d
        for d in docs
        if d.get("kind") == "ConfigMap"
    }
    found = []
    for doc in docs:
        if doc.get("kind") in POD_KINDS and host in _workload_text(doc, configmaps):
            labels = (_pod_template(doc).get("metadata") or {}).get("labels") or {}
            found.append(
                (doc["metadata"]["namespace"], labels, f"{doc['kind']}/{doc['metadata']['name']}")
            )
    return found


# --------------------------------------------------------------------- policies


def _selects(selector: dict, labels: dict) -> bool:
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr["key"], expr["operator"], expr.get("values") or []
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and labels.get(key) in values:
            return False
    return True


def _reaches_outside(rule: dict) -> bool:
    peers = rule.get("to")
    if not peers:
        return True
    for peer in peers:
        block = peer.get("ipBlock")
        if not block or OUTSIDE not in ipaddress.ip_network(block["cidr"]):
            continue
        if not any(OUTSIDE in ipaddress.ip_network(cidr) for cidr in block.get("except") or []):
            return True
    return False


def _opens(rule: dict, port: int) -> bool:
    ports = rule.get("ports")
    if not ports:
        return True
    return any(
        p.get("port") == port or (p.get("endPort") and p["port"] <= port <= p["endPort"])
        for p in ports
    )


def unreachable(provider: str, namespace: str, labels: dict, ports: list[int]) -> list[int]:
    """The ports a pod cannot dial outside the cluster on. Empty for an unisolated pod."""
    selecting = [
        p
        for p in policies(provider)
        if p["metadata"]["namespace"] == namespace
        and "Egress" in (p["spec"].get("policyTypes") or [])
        and _selects(p["spec"].get("podSelector") or {}, labels)
    ]
    if not selecting:
        return []
    rules = [r for p in selecting for r in p["spec"].get("egress") or [] if _reaches_outside(r)]
    return [port for port in ports if not any(_opens(r, port) for r in rules)]


# ----------------------------------------------------------------------- tests

KAFKA_PROVIDERS = tuple(sorted(KAFKA_FACTS))


def test_every_managed_kafka_the_aws_root_builds_is_covered() -> None:
    assert set(KAFKA_PROVIDERS) == set(offering()["clouds"]["aws"]["kafka"])


@pytest.mark.parametrize("provider", KAFKA_PROVIDERS)
def test_every_broker_client_can_dial_the_broker(provider: str) -> None:
    ports = offering()["backends"][provider]["client_ports"]
    found = clients(provider, CLIENT_HOST)
    assert {labels.get("app.kubernetes.io/name") for _, labels, _ in found} >= {
        "dfe-engine",
        "dfe-loader",
    }, found
    blocked = {
        workload: missing
        for namespace, labels, workload in found
        if (missing := unreachable(provider, namespace, labels, ports))
    }
    assert blocked == {}, blocked


def test_the_msk_bootstrap_job_can_dial_the_iam_listener() -> None:
    ports = offering()["backends"]["msk"]["bootstrap_ports"]
    found = clients("msk", BOOTSTRAP_HOST)
    assert found, "no workload is given the IAM endpoint"
    blocked = {
        workload: missing
        for namespace, labels, workload in found
        if (missing := unreachable("msk", namespace, labels, ports))
    }
    assert blocked == {}, blocked


@pytest.mark.parametrize("provider", KAFKA_PROVIDERS)
def test_every_clickhouse_client_can_dial_clickhouse_cloud(provider: str) -> None:
    ports = offering()["backends"]["clickhouse-cloud"]["client_ports"]
    found = clients(provider, CLICKHOUSE_HOST)
    assert {labels.get("app.kubernetes.io/name") for _, labels, _ in found} >= {
        "dfe-engine",
        "dfe-loader",
    }, found
    blocked = {
        workload: missing
        for namespace, labels, workload in found
        if (missing := unreachable(provider, namespace, labels, ports))
    }
    assert blocked == {}, blocked


PER_CONFIG = per_config_apps()
INTERNET = yaml.safe_load(CHART_VALUES.read_text(encoding="utf-8"))["internetEgress"]


def test_the_manifest_has_per_config_apps_to_check() -> None:
    assert PER_CONFIG, "apps.yaml declares no per_config app"


@pytest.mark.parametrize("provider", KAFKA_PROVIDERS)
@pytest.mark.parametrize("app", PER_CONFIG)
def test_a_per_config_instance_can_dial_the_broker(app: str, provider: str) -> None:
    """An instance's app.kubernetes.io/name is <app>-<source>, which no app list names."""
    found = clients_in(rendered_instance(app, provider), CLIENT_HOST)
    assert [workload for _, _, workload in found] == [f"Deployment/{app}-{SOURCE}"], found
    ports = offering()["backends"][provider]["client_ports"]
    blocked = {
        workload: missing
        for namespace, labels, workload in found
        if (missing := unreachable(provider, namespace, labels, ports))
    }
    assert blocked == {}, blocked


@pytest.mark.parametrize("app", sorted(set(PER_CONFIG) & set(INTERNET["apps"])))
def test_a_per_config_instance_takes_its_apps_internet_egress(app: str) -> None:
    ((namespace, labels, _),) = clients_in(rendered_instance(app, "msk"), CLIENT_HOST)
    ports = [p["port"] for p in INTERNET["ports"]]
    assert unreachable("msk", namespace, labels, ports) == []


@pytest.mark.parametrize("app", PER_CONFIG)
def test_a_per_config_instance_takes_no_other_apps_egress(app: str) -> None:
    """The ClickHouse clients' grant reaches no instance of an app that is not one."""
    ((namespace, labels, _),) = clients_in(rendered_instance(app, "msk"), CLIENT_HOST)
    ports = offering()["backends"]["clickhouse-cloud"]["client_ports"]
    assert unreachable("msk", namespace, labels, ports) == ports


@pytest.mark.parametrize("provider", KAFKA_PROVIDERS)
def test_a_provider_opens_its_own_ports_and_no_others(provider: str) -> None:
    """The managed range would reach every listener of every vendor; a provider gets its own."""
    backend = offering()["backends"][provider]
    allowed = set(backend["client_ports"]) | set(backend["bootstrap_ports"])
    opened = {
        port["port"]
        for p in policies(provider)
        if p["metadata"]["name"].startswith("allow-external-kafka-egress-")
        for rule in p["spec"]["egress"]
        for port in rule.get("ports") or []
    }
    assert opened == allowed, sorted(opened)


def test_the_chart_carries_the_offering_ports() -> None:
    """helm cannot read offering.yaml, so the chart's copy is held to it here."""
    egress = yaml.safe_load(CHART_VALUES.read_text(encoding="utf-8"))["externalEgress"]
    backends = offering()["backends"]
    for provider, entry in egress["kafka"]["providers"].items():
        chart = {p["port"] for p in entry["ports"]}
        assert chart == set(backends[provider]["client_ports"]), provider
        chart_bootstrap = {
            p["port"] for ports in (entry.get("appPorts") or {}).values() for p in ports
        }
        assert chart_bootstrap == set(backends[provider]["bootstrap_ports"]), provider
    assert set(egress["kafka"]["providers"]) == {
        name for name, backend in backends.items() if backend["service"] == "kafka"
    }
    clickhouse = {p["port"] for p in egress["clickhouse"]["ports"]}
    assert clickhouse == set(backends["clickhouse-cloud"]["client_ports"])


def test_the_offering_ports_are_whole_tcp_ports() -> None:
    for name, backend in offering()["backends"].items():
        for port in backend["client_ports"] + backend["bootstrap_ports"]:
            assert isinstance(port, int), (name, port)
            assert 0 < port < 65536, (name, port)
