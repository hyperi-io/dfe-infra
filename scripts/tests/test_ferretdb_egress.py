#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_ferretdb_egress.py
#  Purpose:      Prove only dfe-hyperdx reaches FerretDB, no app pod reaches its
#                PostgreSQL backend, and the edge tunnel reaches no backing service.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Who can open a connection to FerretDB and to the PostgreSQL backend behind it.

FerretDB's one login is the backend's PostgreSQL superuser and FerretDB enforces no
authorisation, so a pod holding that password owns the store at either port. Policies
are additive, so a single policy proves nothing about a pod. Each check here judges a
connection the way the CNI does, over every policy in the render: some egress rule
selecting the source and some ingress rule selecting the destination must both admit
it.

The sources are every workload the thin-chart goldens render, with their own
NetworkPolicies, plus a pod carrying no labels at all. The destinations are the
ferretdb chart's own pods, in the namespace layer2-data deploys it to.

    python3 scripts/tests/test_ferretdb_egress.py

Needs `helm` on PATH.
"""

import functools
import ipaddress
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from test_keeper_ingress import selects

from _charts import chart_dir
from _expect import expect, standalone, summary
from _gate import CELLS, NAMESPACE, WORKLOAD_KINDS, golden, references

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
COMMON = yaml.safe_load((VALUES / "common.yaml").read_text(encoding="utf-8"))
LAYER2_DATA = REPO_ROOT / "argocd" / "appsets" / "layer2-data.yaml"
FERRETDB_VALUES = yaml.safe_load(
    (chart_dir("ferretdb") / "values.yaml").read_text(encoding="utf-8")
)

NS_LABEL = "kubernetes.io/metadata.name"
PROXY = "dfe-ferretdb"
BACKEND = "dfe-ferretdb-documentdb"
READER = "dfe-hyperdx"

# Every purpose externalEgress opens on a declaration, at once: none may reach either port.
DECLARED = (
    "clickhouse.mode=external",
    "kafka.mode=external",
    "telemetry.mode=external",
    "oidc.enabled=true",
    "vault.address=https://bao.example.com:8200",
    "elasticsearch.endpoint=https://es.example.com:9200",
)
VARIANTS = {"default": (), "every external declared": DECLARED}


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


STORE_NS = deployed_namespace("ferretdb")


@functools.cache
def render_policies(*sets: str) -> tuple[dict, ...]:
    cmd = ["helm", "template", "network-policies", str(chart_dir("network-policies"))]
    cmd += ["-f", str(VALUES / "common.yaml"), "--set", f"dfeNamespaces={{{NAMESPACE}}}"]
    for s in sets:
        cmd += ["--set", s]
    return tuple(d for d in run(cmd) if d.get("kind") == "NetworkPolicy")


@functools.cache
def render_store() -> tuple[dict, ...]:
    cmd = ["helm", "template", "ferretdb", str(chart_dir("ferretdb"))]
    cmd += ["-f", str(VALUES / "common.yaml"), "--namespace", STORE_NS]
    return tuple(run([*cmd, "--set", f"appNamespace={NAMESPACE}"]))


def workload(docs: tuple[dict, ...] | list[dict], name: str) -> dict:
    for doc in docs:
        if doc.get("kind") in WORKLOAD_KINDS and doc["metadata"]["name"] == name:
            return doc
    raise SystemExit(f"no workload {name} in the render")


def container_port(doc: dict, name: str) -> int:
    for container in doc["spec"]["template"]["spec"]["containers"]:
        for port in container.get("ports") or []:
            if port.get("name") == name:
                return port["containerPort"]
    raise SystemExit(f"{doc['metadata']['name']} has no container port {name}")


def template_labels(doc: dict) -> dict[str, str]:
    """A workload's pod labels. A CronJob holds its template one level down."""
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["metadata"].get("labels") or {}


@dataclass(frozen=True, slots=True)
class Store:
    """FerretDB's proxy and backend pods, and the container port each serves."""

    proxy: Pod
    backend: Pod
    mongo_port: int
    pg_port: int


@functools.cache
def store() -> Store:
    docs = render_store()
    proxy, backend = workload(docs, PROXY), workload(docs, BACKEND)
    return Store(
        Pod(PROXY, STORE_NS, template_labels(proxy), address(0)),
        Pod(BACKEND, STORE_NS, template_labels(backend), address(1)),
        container_port(proxy, "mongodb"),
        container_port(backend, "postgres"),
    )


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


def test_the_destinations_are_the_charts_own() -> None:
    s = store()
    expect("FerretDB speaks Mongo wire on 27017", s.mongo_port == 27017, f"got {s.mongo_port}")
    expect("its backend speaks PostgreSQL on 5432", s.pg_port == 5432, f"got {s.pg_port}")
    expect(
        "the backend's labels are not FerretDB's",
        not selects({"matchLabels": s.proxy.labels}, s.backend.labels),
        f"{s.proxy.labels} vs {s.backend.labels}",
    )
    expect(
        "the sources include the user-facing apps",
        {"dfe-receiver", "dfe-ui", READER} <= {pod.name for pod, _ in app_pods()},
        f"got {sorted(pod.name for pod, _ in app_pods())}",
    )


def test_no_app_pod_reaches_the_postgresql_backend() -> None:
    s = store()
    for name, variant in VARIANTS.items():
        found = reaching(variant, s.backend, s.pg_port)
        expect(
            f"{name}: no app-namespace pod reaches {BACKEND}:{s.pg_port}",
            found == set(),
            f"got {sorted(found)}",
        )


def test_only_hyperdx_reaches_ferretdb() -> None:
    s = store()
    for name, variant in VARIANTS.items():
        found = reaching(variant, s.proxy, s.mongo_port)
        expect(
            f"{name}: only {READER} reaches {PROXY}:{s.mongo_port}",
            found == {READER},
            f"got {sorted(found)}",
        )


def test_ferretdb_still_reaches_its_backend() -> None:
    """FerretDB dials the backend inside its own namespace, under that namespace's baseline."""
    s = store()
    expect(
        f"{PROXY} reaches {BACKEND}:{s.pg_port}",
        connects(render_policies(), s.proxy, s.backend, s.pg_port),
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


def test_the_backend_password_reaches_hyperdx_alone() -> None:
    """The app-namespace copy of the superuser password is read by its one client."""
    secret = FERRETDB_VALUES["documentdb"]["auth"]["secretName"]
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
