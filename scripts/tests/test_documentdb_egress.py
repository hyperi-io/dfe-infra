#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_documentdb_egress.py
#  Purpose:      Prove only dfe-hyperdx reaches the DocumentDB gateway, nothing outside
#                the cluster's own instances and CloudNativePG reaches the rest of an
#                instance, and the edge tunnel reaches neither the backing services
#                nor the gateway.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Who can open a connection to a DocumentDB instance, and on which port.

Each instance pod runs PostgreSQL, which holds the documentdb superuser, with the
DocumentDB gateway beside it. Policies are additive, so a single policy proves nothing
about a pod. Each check here judges a connection the way the CNI does, over every
policy in the render: some egress rule selecting the source and some ingress rule
selecting the destination must both admit it.

The sources are every workload the thin-chart goldens render, with their own
NetworkPolicies, plus a pod carrying no labels at all, plus a pod in each namespace
data-ingress-policy admits that carries no egress policy of its own. CloudNativePG
creates the instance pods at runtime, so the destinations are modelled from the
documentdb chart's DocumentDB resource and the label CloudNativePG writes on every
instance of its cluster.

    python3 scripts/tests/test_documentdb_egress.py

Needs `helm` on PATH.
"""

import functools
import ipaddress
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from test_keeper_ingress import selects

from _charts import chart_dir
from _expect import expect, standalone, summary
from _gate import CELLS, NAMESPACE, WORKLOAD_KINDS, golden, references

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
COMMON = yaml.safe_load((VALUES / "common.yaml").read_text(encoding="utf-8"))
HYPERDX = yaml.safe_load((VALUES / "apps" / "hyperdx" / "values.yaml").read_text(encoding="utf-8"))
LAYER2_DATA = REPO_ROOT / "argocd" / "appsets" / "layer2-data.yaml"
DOCUMENTDB_VALUES = yaml.safe_load(
    (chart_dir("documentdb") / "values.yaml").read_text(encoding="utf-8")
)
POLICY_VALUES = yaml.safe_load(
    (chart_dir("network-policies") / "values.yaml").read_text(encoding="utf-8")
)
ACCESS = POLICY_VALUES["documentdbAccess"]

NS_LABEL = "kubernetes.io/metadata.name"
READER = "dfe-hyperdx"
# CloudNativePG's instance manager listens here, and PostgreSQL on its usual port.
STATUS_PORT = 8000
PG_PORT = 5432

# Every purpose externalEgress opens on a declaration, at once: none may reach an instance.
DECLARED = (
    "clickhouse.mode=external",
    "kafka.mode=external",
    "telemetry.mode=external",
    "oidc.enabled=true",
    "vault.address=https://bao.example.com:8200",
    "elasticsearch.endpoint=https://es.example.com:9200",
)
VARIANTS = {"default": (), "every external declared": DECLARED}
# Emptying the Keeper opt-out must not put the instances back under data-ingress-policy.
INGRESS_VARIANTS = {"default": (), "the Keeper opt-out empty": ("operatorIngressScoped=null",)}


@dataclass(frozen=True, slots=True)
class Pod:
    """A pod as NetworkPolicy sees it."""

    name: str
    namespace: str
    labels: dict[str, str]
    ip: ipaddress.IPv4Address


def run(cmd: list[str]) -> list[dict]:
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"{' '.join(cmd[:3])} failed:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def address(index: int) -> ipaddress.IPv4Address:
    """A pod address inside the cluster's pod range, distinct per index."""
    return ipaddress.ip_network(COMMON["networkModel"]["podCIDR"])[index + 10]


def deployed_namespace(app: str) -> str:
    """The namespace layer2-data deploys a data chart into."""
    doc = yaml.safe_load(LAYER2_DATA.read_text(encoding="utf-8"))
    for generator in doc["spec"]["generators"][0]["matrix"]["generators"]:
        for element in (generator.get("list") or {}).get("elements") or []:
            if element["app"] == app:
                return element["namespace"]
    raise SystemExit(f"layer2-data deploys no {app}")


STORE_NS = deployed_namespace("documentdb")


@functools.cache
def render_policies(*sets: str) -> tuple[dict, ...]:
    cmd = ["helm", "template", "network-policies", str(chart_dir("network-policies"))]
    cmd += ["-f", str(VALUES / "common.yaml"), "--set", f"dfeNamespaces={{{NAMESPACE}}}"]
    for s in sets:
        cmd += ["--set", s]
    return tuple(d for d in run(cmd) if d.get("kind") == "NetworkPolicy")


@functools.cache
def render_store() -> tuple[dict, ...]:
    cmd = ["helm", "template", "documentdb", str(chart_dir("documentdb"))]
    cmd += ["-f", str(VALUES / "common.yaml"), "--namespace", STORE_NS]
    return tuple(run([*cmd, "--set", f"appNamespace={NAMESPACE}"]))


def template_labels(doc: dict) -> dict[str, str]:
    """A workload's pod labels. A CronJob holds its template one level down."""
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["metadata"].get("labels") or {}


def cluster_name() -> str:
    """The name the chart gives its DocumentDB, which CloudNativePG labels its pods with."""
    found = [d["metadata"]["name"] for d in render_store() if d.get("kind") == "DocumentDB"]
    if len(found) != 1:
        raise SystemExit(f"expected one DocumentDB in the render, got {found}")
    return found[0]


def instance(index: int) -> Pod:
    """One instance pod of the cluster, as CloudNativePG labels it."""
    labels = {ACCESS["clusterLabel"]: cluster_name()}
    return Pod(f"{cluster_name()}-{index + 1}", STORE_NS, labels, address(index))


def gateway_port() -> int:
    """The port hyperdx dials, read from its connection string."""
    return urlsplit(HYPERDX["mongodb"]["uri"]).port


@functools.cache
def app_pods() -> tuple[tuple[Pod, tuple[dict, ...]], ...]:
    """Each workload the goldens render in the app namespace, with its cell's own policies.

    A pod carrying no labels at all stands for anything nobody declared."""
    found: dict[tuple[str, str], tuple[Pod, tuple[dict, ...]]] = {}
    for cell in CELLS:
        docs = golden(*cell)
        own = []
        for doc in docs:
            if doc.get("kind") == "NetworkPolicy":
                doc["metadata"].setdefault("namespace", NAMESPACE)
                own.append(doc)
        for doc in docs:
            if doc.get("kind") not in WORKLOAD_KINDS:
                continue
            labels = template_labels(doc)
            key = (doc["metadata"]["name"], yaml.safe_dump(labels, sort_keys=True))
            if key not in found:
                pod = Pod(doc["metadata"]["name"], NAMESPACE, labels, address(100 + len(found)))
                found[key] = (pod, tuple(own))
    nobody = Pod("unlabelled", NAMESPACE, {}, address(99))
    return (*found.values(), (nobody, ()))


def peer_matches(peer: dict, policy_ns: str, pod: Pod) -> bool:
    """Whether one entry of a rule's `to` or `from` names this pod."""
    if "ipBlock" in peer:
        block = peer["ipBlock"]
        carved = any(pod.ip in ipaddress.ip_network(c) for c in block.get("except") or [])
        return pod.ip in ipaddress.ip_network(block["cidr"]) and not carved
    namespaces = peer.get("namespaceSelector")
    if namespaces is None:
        in_namespace = pod.namespace == policy_ns
    else:
        in_namespace = selects(namespaces, {NS_LABEL: pod.namespace})
    return in_namespace and selects(peer.get("podSelector") or {}, pod.labels)


def port_matches(ports: list[dict] | None, port: int) -> bool:
    """An absent or empty list admits every port."""
    if not ports:
        return True
    for entry in ports:
        if entry.get("protocol", "TCP") != "TCP":
            continue
        number = entry.get("port")
        if number is None:
            return True
        if not isinstance(number, int):
            raise SystemExit(f"named port {number!r} is not modelled here")
        if number <= port <= entry.get("endPort", number):
            return True
    return False


def admits(policies: tuple[dict, ...], pod: Pod, egress: bool, peer: Pod, port: int) -> bool:
    """Whether the policies selecting `pod` admit traffic with `peer` in that direction.

    A pod no policy of that direction selects is not isolated, so everything passes."""
    kind, key, side = ("Egress", "egress", "to") if egress else ("Ingress", "ingress", "from")
    isolating = []
    for policy in policies:
        spec = policy["spec"]
        types = spec.get("policyTypes") or ["Ingress", *(["Egress"] if "egress" in spec else [])]
        if policy["metadata"]["namespace"] != pod.namespace or kind not in types:
            continue
        if selects(spec.get("podSelector") or {}, pod.labels):
            isolating.append(policy)
    if not isolating:
        return True
    for policy in isolating:
        for rule in policy["spec"].get(key) or []:
            peers = rule.get(side)
            named = not peers or any(
                peer_matches(p, policy["metadata"]["namespace"], peer) for p in peers
            )
            if named and port_matches(rule.get("ports"), port):
                return True
    return False


def connects(policies: tuple[dict, ...], source: Pod, dest: Pod, port: int) -> bool:
    leaves = admits(policies, source, True, dest, port)
    return leaves and admits(policies, dest, False, source, port)


def reaching(variant: tuple[str, ...], dest: Pod, port: int) -> set[str]:
    """The app-namespace workloads that can open a connection to `dest` on `port`."""
    base = render_policies(*variant)
    return {pod.name for pod, own in app_pods() if connects((*base, *own), pod, dest, port)}


def policy_value(key: str) -> object:
    """A network-policies value as Argo renders it: common.yaml over the chart default."""
    return COMMON.get(key, POLICY_VALUES[key])


def operator_pod() -> Pod:
    return Pod("cloudnativepg", policy_value("operatorNamespaces")["cnpg"], {}, address(64))


def test_the_destinations_are_the_charts_own() -> None:
    name = cluster_name()
    expect(
        "documentdbAccess names the chart's DocumentDB",
        ACCESS["cluster"] == name,
        f"{ACCESS['cluster']} vs {name}",
    )
    expect(
        "documentdbAccess deploys where layer2-data puts the chart",
        ACCESS["namespace"] == STORE_NS,
        f"{ACCESS['namespace']} vs {STORE_NS}",
    )
    host = urlsplit(HYPERDX["mongodb"]["uri"]).hostname
    expect(
        "hyperdx dials the cluster's own Service",
        host == f"documentdb-service-{name}.{STORE_NS}.svc.cluster.local",
        f"got {host}",
    )
    expect(
        "documentdbAccess opens the port hyperdx dials",
        [p["port"] for p in ACCESS["ports"]] == [gateway_port()],
        f"{ACCESS['ports']} vs {gateway_port()}",
    )
    expect(
        "the sources include the user-facing apps",
        {"dfe-receiver", "dfe-ui", READER} <= {pod.name for pod, _ in app_pods()},
        f"got {sorted(pod.name for pod, _ in app_pods())}",
    )


def test_no_app_pod_reaches_postgresql_or_the_status_port() -> None:
    dest = instance(0)
    for name, variant in VARIANTS.items():
        for port in (PG_PORT, STATUS_PORT):
            found = reaching(variant, dest, port)
            expect(
                f"{name}: no app-namespace pod reaches {dest.name}:{port}",
                found == set(),
                f"got {sorted(found)}",
            )


def test_only_hyperdx_reaches_the_gateway() -> None:
    dest = instance(0)
    for name, variant in VARIANTS.items():
        found = reaching(variant, dest, gateway_port())
        expect(
            f"{name}: only {READER} reaches {dest.name}:{gateway_port()}",
            found == {READER},
            f"got {sorted(found)}",
        )


def test_the_instances_reach_each_other() -> None:
    """A replica streams from the primary inside the store's own namespace."""
    base = render_policies()
    expect(
        f"{instance(1).name} reaches {instance(0).name}:{PG_PORT}",
        connects(base, instance(1), instance(0), PG_PORT),
    )


def test_cloudnativepg_reaches_the_status_port_alone() -> None:
    base = render_policies()
    operator, dest = operator_pod(), instance(0)
    expect(
        f"CloudNativePG reaches {dest.name}:{STATUS_PORT}",
        connects(base, operator, dest, STATUS_PORT),
    )
    for port in (PG_PORT, gateway_port()):
        expect(
            f"CloudNativePG cannot reach {dest.name}:{port}",
            not connects(base, operator, dest, port),
        )


def backing_destinations() -> list[tuple[str, int]]:
    """(namespace, port) for every rule of the namespace-wide backing egress policy."""
    policy = next(
        p for p in render_policies() if p["metadata"]["name"] == "allow-dfe-backing-egress"
    )
    found = []
    for rule in policy["spec"]["egress"]:
        ns = rule["to"][0]["namespaceSelector"]["matchLabels"][NS_LABEL]
        found += [(ns, p["port"]) for p in rule["ports"]]
    return found


def test_the_backing_egress_opens_nothing_in_the_store_namespace() -> None:
    found = [d for d in backing_destinations() if d[0] == STORE_NS]
    expect(f"allow-dfe-backing-egress opens nothing in {STORE_NS}", found == [], f"got {found}")


def test_the_tunnel_reaches_no_backing_service() -> None:
    """culvert SNATs its clients onto its own address, so its egress is theirs."""
    pods = {pod.name: (pod, own) for pod, own in app_pods()}
    tunnel, tunnel_own = pods["dfe-culvert"]
    other, other_own = pods["unlabelled"]
    base = render_policies()
    for index, (ns, port) in enumerate(backing_destinations()):
        dest = Pod(f"{ns}-pod", ns, {}, address(50 + index))
        expect(
            f"dfe-culvert cannot reach {ns}:{port}",
            not connects((*base, *tunnel_own), tunnel, dest, port),
        )
        expect(
            f"a pod without the opt-out still reaches {ns}:{port}",
            connects((*base, *other_own), other, dest, port),
        )


def gateway_rule() -> dict:
    """The one rule of the app namespace's egress to the gateway's proxy pods."""
    policy = next(p for p in render_policies() if p["metadata"]["name"] == "allow-gateway-egress")
    return policy["spec"]["egress"][0]


def outside_pods() -> list[Pod]:
    """A pod in each namespace data-ingress-policy admits that has no egress policy of its
    own, and a pod beside the instances that is not one of them."""
    proxy_labels = gateway_rule()["to"][0]["podSelector"]["matchLabels"]
    return [
        Pod("gateway proxy", str(policy_value("gatewayNamespace")), proxy_labels, address(60)),
        Pod("collector", str(policy_value("otelNamespace")), {}, address(61)),
        Pod(
            "clickhouse operator", policy_value("operatorNamespaces")["clickhouse"], {}, address(62)
        ),
        Pod("cnpg neighbour", STORE_NS, {}, address(63)),
    ]


def test_no_other_namespace_reaches_an_instance() -> None:
    dest = instance(0)
    for name, variant in INGRESS_VARIANTS.items():
        base = render_policies(*variant)
        for pod in outside_pods():
            for port in (gateway_port(), PG_PORT, STATUS_PORT):
                expect(
                    f"{name}: the {pod.name} in {pod.namespace} cannot reach {dest.name}:{port}",
                    not connects(base, pod, dest, port),
                )
        expect(
            f"{name}: {READER} still reaches {dest.name}:{gateway_port()}",
            READER in reaching(variant, dest, gateway_port()),
        )
        expect(
            f"{name}: {instance(1).name} still reaches {dest.name}:{PG_PORT}",
            connects(base, instance(1), dest, PG_PORT),
        )


def test_the_tunnel_reaches_no_gateway_proxy() -> None:
    """docs/deployment/edge-vpn.md: the opt-out leaves culvert's own policy as its whole egress."""
    pods = {pod.name: (pod, own) for pod, own in app_pods()}
    tunnel, tunnel_own = pods["dfe-culvert"]
    other, other_own = pods["unlabelled"]
    rule = gateway_rule()
    labels = rule["to"][0]["podSelector"]["matchLabels"]
    proxy = Pod("gateway proxy", str(policy_value("gatewayNamespace")), labels, address(70))
    base = render_policies()
    for entry in rule["ports"]:
        port = entry["port"]
        expect(
            f"dfe-culvert cannot reach the gateway proxy on {port}",
            not connects((*base, *tunnel_own), tunnel, proxy, port),
        )
        expect(
            f"a pod without the opt-out still reaches the gateway proxy on {port}",
            connects((*base, *other_own), other, proxy, port),
        )


def test_the_store_login_reaches_hyperdx_alone() -> None:
    """The app-namespace copy of the store's login is read by its one client."""
    secret = DOCUMENTDB_VALUES["documentdb"]["auth"]["secretName"]
    holders = set()
    for cell in CELLS:
        for kind, name, _optional, where in references(golden(*cell)):
            if kind == "Secret" and name == secret:
                holders.add(where.split()[0])
    expect(
        f"only {READER} names {secret}",
        holders == {f"Deployment/{READER}"},
        f"got {sorted(holders)}",
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
