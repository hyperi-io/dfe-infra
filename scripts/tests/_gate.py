#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         _gate.py
#  Purpose:      The thin-chart render matrix: each cell rendered live, its
#                committed golden copy, and the checks the tests run over both.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The thin-chart render matrix, shared by test_thin_renders.py and the tests reading it.

Each cell is one component on one profile and cloud, or one scenario on
slim/local, rendered through scripts/dfe-weave as Argo deploys it: the thin chart
beside dfe-extras, with the integration values under argocd/values/apps. cell()
renders it live, golden() reads the copy committed under fixtures/thin-renders,
and test_thin_renders.py holds the two equal. A test asserting a property of what
deploys reads golden(), which needs neither helm nor the registry. A test turning
a dial renders live through _weave.render_app.

A component joins the matrix as a row of COMPONENTS. A component another appset
deploys names it in APPSETS, and instance values it refuses to render without go
in INSTANCE. published_render() renders a cell's thin chart as the registry serves
it instead of as assembled here.

The goldens hold each image tag and digest versions.yaml pins as a token naming
the pin (PIN_TOKEN), so a release that moves a pin leaves them alone. golden()
puts the current values back.

The leading underscore keeps pytest from collecting this module as a test file.
"""

import contextlib
import copy
import functools
import json
import re
from collections.abc import Iterator
from pathlib import Path

import yaml

from _weave import REPO_ROOT, TESTS, deploy_repo, published_chart, render_app

# The deploy.service of each component the matrix covers.
COMPONENTS = (
    "dfe-ui",
    "hyperdx",
    "dfe-receiver",
    "dfe-loader",
    "dfe-archiver",
    "dfe-transform-vrl",
    "dfe-transform-vector",
    "dfe-transform-elastic",
    "dfe-fetcher",
    "culvert",
    "dfe-engine",
)
PROFILES = ("slim", "single", "scale", "mesh")
CLOUDS = ("local", "aws")
MATRIX = [(c, p, k) for c in COMPONENTS for p in PROFILES for k in CLOUDS]

# The appset a component's Application comes from, where it is not layer2-apps.yaml.
APPSETS: dict[str, Path] = {"culvert": Path("argocd/appsets/layer2-edge.yaml")}

# The instance values a deployer has to write before a component renders at all, per
# cloud. culvert reads local.yaml's receiver `listeners` unless it restates its own,
# and edge-aws.yaml's external PKI needs its Secret named.
INSTANCE: dict[str, dict[str, dict]] = {
    "culvert": {
        "local": {
            "listeners": [
                {"name": "wireguard", "port": 51820, "protocol": "UDP", "exposed": True},
                {"name": "openvpn-udp", "port": 1194, "protocol": "UDP", "exposed": True},
                {"name": "metrics", "port": 9090, "protocol": "TCP", "exposed": False},
            ]
        },
        "aws": {"pki": {"existingSecret": "dfe-culvert-pki"}},
    },
}

# Deployment shapes the profile matrix does not reach, each rendered on slim/local:
# the cluster facts it changes, the deploy repo's infra/common.yaml, and the
# components it renders for where it is not every one.
SCENARIOS: dict[str, dict] = {
    "no-domain": {"facts": {"domain": ""}, "infra": None},
    "prometheus": {"facts": {}, "infra": {"telemetry": {"mode": "prometheus"}}},
    # A ClickHouse CA the deployment supplies as a ConfigMap rather than a Secret.
    "clickhouse-ca-configmap": {
        "facts": {},
        "infra": {
            "clickhouse": {"tls": {"ca": {"secretName": "", "configMapName": "example-ca-bundle"}}}
        },
    },
    "clickhouse-plaintext": {"facts": {}, "infra": {"clickhouse": {"tls": {"enabled": False}}}},
    # The receiver's load balancer door as docs/INGEST-EDGE.md documents it, with
    # RFC 5737 ranges standing in for the deployment's own senders.
    "public-ingest": {
        "facts": {},
        "infra": {
            "exposure": {
                "groups": {
                    "ingest": {
                        "door": "loadbalancer",
                        "allow": ["198.51.100.0/24", "203.0.113.0/24"],
                    }
                },
                "public": {
                    "annotations": {
                        "service.beta.kubernetes.io/aws-load-balancer-scheme": "internet-facing"
                    },
                },
            }
        },
        "components": ("dfe-receiver",),
    },
}
DEFAULT = "default"

# Every cell: the matrix on the default scenario, then each scenario on slim/local.
CELLS = [(s, p, k, DEFAULT) for s, p, k in MATRIX] + [
    (s, "slim", "local", scenario)
    for s in COMPONENTS
    for scenario, shape in SCENARIOS.items()
    if s in shape.get("components", COMPONENTS)
]

GOLDENS = TESTS / "fixtures" / "thin-renders"
VERSIONS = REPO_ROOT / "versions.yaml"

ABSENT = "<absent>"
NAMESPACE = "dfe"

# Objects the cluster itself provides in every namespace: kube-controller-manager
# publishes the API server's CA as this ConfigMap.
CLUSTER_PROVIDED = {("ConfigMap", "kube-root-ca.crt")}

WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")
PROBES = ("startupProbe", "livenessProbe", "readinessProbe")
# Kubernetes fills these in when a probe leaves them out, so they compare as set.
PROBE_DEFAULTS = {
    "initialDelaySeconds": 0,
    "timeoutSeconds": 1,
    "periodSeconds": 10,
    "successThreshold": 1,
    "failureThreshold": 3,
}
# What Kubernetes gives a Deployment that names no strategy.
DEFAULT_STRATEGY = {
    "type": "RollingUpdate",
    "rollingUpdate": {"maxUnavailable": "25%", "maxSurge": "25%"},
}
# What Kubernetes gives a pod that leaves these out, so leaving one out compares as setting it.
POD_DEFAULTS = {
    "automountServiceAccountToken": True,
    "enableServiceLinks": True,
    "serviceAccountName": "default",
    "terminationGracePeriodSeconds": 30,
}


# ---------------------------------------------------------------------- renders


@contextlib.contextmanager
def _cell_options(service: str, cloud: str, scenario: str) -> Iterator[dict]:
    """The render options a cell takes: its facts, appset and deploy repo."""
    shape = SCENARIOS.get(scenario, {"facts": {}, "infra": None})
    instance = INSTANCE.get(service, {}).get(cloud)
    options: dict = dict(shape["facts"])
    if service in APPSETS:
        options["appset"] = APPSETS[service]
    if shape["infra"] is None and instance is None:
        yield options
        return
    with deploy_repo(service, overlay=instance, infra=shape["infra"]) as root:
        options["deploy_repo"] = root
        yield options


@functools.cache
def _rendered(service: str, profile: str, cloud: str, scenario: str) -> tuple[dict, ...]:
    with _cell_options(service, cloud, scenario) as options:
        return tuple(render_app(service, profile, cloud, **options))


def cell(service: str, profile: str, cloud: str, scenario: str = DEFAULT) -> list[dict]:
    """One cell rendered live, rendered once per run; the copy returned is the caller's."""
    return copy.deepcopy(list(_rendered(service, profile, cloud, scenario)))


@functools.cache
def _published(service: str, profile: str, cloud: str, scenario: str) -> tuple[dict, ...]:
    chart = published_chart(service)
    with _cell_options(service, cloud, scenario) as options:
        return tuple(render_app(service, profile, cloud, chart=chart, **options))


def published_render(
    service: str, profile: str, cloud: str, scenario: str = DEFAULT
) -> list[dict]:
    """A cell rendered from the thin chart the registry serves, read as cell() reads its own."""
    return copy.deepcopy(list(_published(service, profile, cloud, scenario)))


def object_id(doc: dict) -> str:
    """``Kind/name``, Argo's identity for an object within its namespace."""
    return f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')}"


# ---------------------------------------------------------------------- goldens

# A pin in a golden file: the versions.yaml key whose current value stood there.
PIN_TOKEN = "<pin:{}>"
PIN_TOKEN_RE = re.compile(r"<pin:([A-Za-z0-9_.-]+)>")
# The pins worth tokenising: digests, and version tags of three numbered parts or more.
PIN_VALUE_RE = re.compile(r"^(sha256:[0-9a-f]{64}|v?\d+\.\d+\.\d+(?:[.+-][0-9A-Za-z.+-]+)?)$")
# The current stack's sections whose pins reach a render.
PIN_SECTIONS = ("apps", "content", "digests", "services", "services-digests")
# A thin chart's version is its image tag less the v, so each app and content tag
# yields a pin of its own under this prefix: the helm.sh/chart label carries it.
CHART_VERSION = "chart-version"
VERSIONED_SECTIONS = ("apps", "content")


def golden_path(service: str, profile: str, cloud: str, scenario: str = DEFAULT) -> Path:
    """Where a cell's golden render is committed."""
    name = f"{profile}-{cloud}" if scenario == DEFAULT else f"{profile}-{cloud}-{scenario}"
    return GOLDENS / service / f"{name}.yaml"


@functools.cache
def pins() -> dict[str, str]:
    """Each pin a render can carry, ``<section>.<key>`` -> value, in the current stack.

    ``chart-version.<key>`` is the thin chart version an ``apps`` or ``content``
    tag names.
    """
    data = yaml.safe_load(VERSIONS.read_text(encoding="utf-8")) or {}
    stack = (data.get("stacks") or {}).get(str(data.get("current", ""))) or {}
    found = {}
    for section in PIN_SECTIONS:
        for key, value in sorted((stack.get(section) or {}).items()):
            if not isinstance(value, str) or not PIN_VALUE_RE.fullmatch(value):
                continue
            found[f"{section}.{key}"] = value
            if section in VERSIONED_SECTIONS and value.startswith("v"):
                found[f"{CHART_VERSION}.{key}"] = value.removeprefix("v")
    return found


def _pin_pattern(value: str) -> re.Pattern[str]:
    """A pin's value standing alone: not inside a longer version, digest or word."""
    return re.compile(rf"(?<![\w.]){re.escape(value)}(?![\w.]*[\w])")


def mask(text: str) -> str:
    """``text`` with every pin value replaced by its token, the longest values first.

    A value two pins share takes the token of the key that sorts first, which
    unmask() reads back to the same value.
    """
    by_value: dict[str, str] = {}
    for key, value in sorted(pins().items()):
        by_value.setdefault(value, key)
    for value in sorted(by_value, key=len, reverse=True):
        text = _pin_pattern(value).sub(PIN_TOKEN.format(by_value[value]), text)
    return text


def unmask(text: str) -> str:
    """``text`` with every token replaced by the current value of the pin it names.

    Raises:
        KeyError: A token names a pin the current stack does not carry.
    """
    current = pins()
    return PIN_TOKEN_RE.sub(lambda m: current[m.group(1)], text)


def golden_text(docs: list[dict]) -> str:
    """A render as its golden file holds it: one YAML document per object, pins tokenised."""
    return mask(yaml.safe_dump_all(docs, sort_keys=True, width=1 << 16))


@functools.cache
def _golden(service: str, profile: str, cloud: str, scenario: str) -> tuple[dict, ...]:
    text = golden_path(service, profile, cloud, scenario).read_text(encoding="utf-8")
    return tuple(d for d in yaml.safe_load_all(unmask(text)) if isinstance(d, dict))


def golden(service: str, profile: str, cloud: str, scenario: str = DEFAULT) -> list[dict]:
    """A cell's committed golden render with today's pins; the copy returned is the caller's."""
    return copy.deepcopy(list(_golden(service, profile, cloud, scenario)))


def goldens(service: str) -> list[tuple[tuple[str, str, str, str], list[dict]]]:
    """Every cell of one component with its golden render: the matrix, then each scenario."""
    return [(c, golden(*c)) for c in CELLS if c[0] == service]


# ---------------------------------------------------------------------- runtime


class Keyed(dict):
    """A list Kubernetes merges by a key, held by that key so it compares item by item."""


def _canon(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _join(path: str, key: str) -> str:
    """``path.key``, or ``path[key]`` for a key that is not one plain word."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else key


def _flatten(node: object, path: str, out: dict[str, object]) -> None:
    """Leaf paths: a Keyed item is ``[key]``, a map key ``.key`` or ``[key]``, a list a leaf."""
    if isinstance(node, Keyed):
        for key, value in node.items():
            _flatten(value, f"{path}[{key}]", out)
    elif isinstance(node, dict) and node:
        for key, value in node.items():
            _flatten(value, _join(path, str(key)), out)
    else:
        out[path] = node


def _pod_spec(doc: dict) -> dict:
    return (doc.get("spec", {}).get("template", {}) or {}).get("spec", {}) or {}


def port_numbers(docs: list[dict]) -> dict[str, int]:
    """Container port name -> number across a render, for resolving named ports."""
    numbers: dict[str, int] = {}
    for doc in docs:
        if doc.get("kind") not in WORKLOAD_KINDS:
            continue
        pod = _pod_spec(doc)
        for container in (pod.get("containers") or []) + (pod.get("initContainers") or []):
            for port in container.get("ports") or []:
                if "name" in port:
                    numbers.setdefault(port["name"], port.get("containerPort"))
    return numbers


def probe_view(probe: dict | None, numbers: dict[str, int]) -> dict | None:
    """A probe with Kubernetes' defaults filled in and its named port resolved."""
    if not probe:
        return None
    out = {**PROBE_DEFAULTS, **copy.deepcopy(probe)}
    for handler in ("httpGet", "tcpSocket", "grpc"):
        port = (out.get(handler) or {}).get("port")
        if isinstance(port, str):
            out[handler]["port"] = numbers.get(port, port)
    return out


def _effective_env(container: dict, configmaps: dict[str, dict]) -> Keyed:
    """The env the kubelet hands the process: envFrom in order, then env, the last name winning."""
    env: dict[str, object] = {}
    for source in container.get("envFrom") or []:
        prefix = source.get("prefix", "")
        if "configMapRef" in source:
            ref = source["configMapRef"].get("name", "")
            data = (configmaps.get(ref) or {}).get("data")
            if data is None:
                env[f"{prefix}*<configMap {ref}>"] = ABSENT
            for name, value in (data or {}).items():
                env[prefix + name] = value
        if "secretRef" in source:
            env[f"{prefix}*<secret {source['secretRef'].get('name')}>"] = "<secret>"
    for entry in container.get("env") or []:
        if "valueFrom" in entry:
            env[entry["name"]] = {"valueFrom": entry["valueFrom"]}
        else:
            env[entry["name"]] = entry.get("value", "")
    return Keyed(sorted(env.items()))


def effective_env(docs: list[dict], workload: str) -> dict[str, object]:
    """The main container's effective env of one workload (``Kind/name``) in a render."""
    configmaps = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "ConfigMap"}
    doc = next(d for d in docs if object_id(d) == workload)
    return dict(_effective_env(_pod_spec(doc)["containers"][0], configmaps))


def _container(container: dict, configmaps: dict[str, dict], numbers: dict[str, int]) -> dict:
    out = {k: v for k, v in container.items() if k not in ("env", "envFrom")}
    out["env"] = _effective_env(container, configmaps)
    if container.get("envFrom"):
        out["envFrom"] = [_canon(source) for source in container["envFrom"]]
    out["ports"] = Keyed(
        (f"{p.get('containerPort')}/{p.get('protocol', 'TCP')}", {"name": p.get("name")})
        for p in container.get("ports") or []
    )
    out["volumeMounts"] = Keyed(
        (m["mountPath"], {k: v for k, v in m.items() if k != "mountPath"})
        for m in container.get("volumeMounts") or []
    )
    for probe in PROBES:
        if probe in out:
            out[probe] = probe_view(out[probe], numbers)
    return out


def _metadata(doc: dict) -> dict:
    meta = doc.get("metadata") or {}
    out = {k: v for k, v in meta.items() if k in ("labels", "annotations")}
    # Argo applies every object into its Application's namespace either way.
    if meta.get("namespace") not in (None, NAMESPACE):
        out["namespace"] = meta["namespace"]
    return out


def _workload(doc: dict, configmaps: dict[str, dict]) -> dict:
    spec = dict(doc.get("spec") or {})
    template = spec.pop("template", {}) or {}
    pod = {**POD_DEFAULTS, **(template.get("spec") or {})}
    numbers = port_numbers([doc])
    containers = pod.pop("containers", None) or []
    pod["containers"] = Keyed(
        ("main" if i == 0 else c.get("name"), _container(c, configmaps, numbers))
        for i, c in enumerate(containers)
    )
    pod["initContainers"] = Keyed(
        (c.get("name"), _container(c, configmaps, numbers))
        for c in pod.pop("initContainers", None) or []
    )
    pod["volumes"] = Keyed(
        (v["name"], {k: x for k, x in v.items() if k != "name"})
        for v in pod.pop("volumes", None) or []
    )
    pulls = pod.pop("imagePullSecrets", None) or []
    pod["imagePullSecrets"] = sorted(p["name"] if isinstance(p, dict) else p for p in pulls)
    if doc.get("kind") == "Deployment":
        spec["strategy"] = spec.get("strategy") or DEFAULT_STRATEGY
    meta = {
        k: v for k, v in (template.get("metadata") or {}).items() if k in ("labels", "annotations")
    }
    spec["template"] = {"metadata": meta, "spec": pod}
    return {"metadata": _metadata(doc), "spec": spec}


def _service(doc: dict, numbers: dict[str, int]) -> dict:
    spec = {"type": "ClusterIP", **(doc.get("spec") or {})}
    ports = {}
    for p in spec.pop("ports", None) or []:
        target = p.get("targetPort", p.get("port"))
        resolved = numbers.get(target, target) if isinstance(target, str) else target
        entry = {k: v for k, v in p.items() if k not in ("port", "protocol", "targetPort")}
        ports[f"{p.get('port')}/{p.get('protocol', 'TCP')}"] = {**entry, "targetPort": resolved}
    spec["ports"] = Keyed(ports)
    return {"metadata": _metadata(doc), "spec": spec}


def runtime_view(docs: list[dict]) -> dict[str, dict[str, object]]:
    """Every object of a render as leaf path -> value, in the form the cluster runs it.

    A workload's main container is ``containers[main]`` whatever its name, its env
    is the effective env (envFrom ConfigMaps of the render, then env), its probes
    carry Kubernetes' defaults with named ports resolved, and lists Kubernetes
    merges by key are compared by that key.
    """
    configmaps = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "ConfigMap"}
    numbers = port_numbers(docs)
    view = {}
    for doc in docs:
        kind = doc.get("kind")
        if kind in WORKLOAD_KINDS:
            body = _workload(doc, configmaps)
        elif kind == "Service":
            body = _service(doc, numbers)
        else:
            body = {"metadata": _metadata(doc)}
            body.update(
                {k: v for k, v in doc.items() if k not in ("apiVersion", "kind", "metadata")}
            )
        body["apiVersion"] = doc.get("apiVersion")
        flat: dict[str, object] = {}
        _flatten(body, "", flat)
        view[object_id(doc)] = flat
    return view


def runtime_diffs(old: list[dict], new: list[dict]) -> list[str]:
    """Every leaf that differs in an object both renders hold, as ``Kind/name path: a -> b``."""
    before, after = runtime_view(old), runtime_view(new)
    found = []
    for obj in sorted(set(before) & set(after)):
        a, b = before[obj], after[obj]
        for path in sorted(set(a) | set(b)):
            left, right = a.get(path, ABSENT), b.get(path, ABSENT)
            if _canon(left) != _canon(right):
                found.append(f"{obj} {path}: {_canon(left)} -> {_canon(right)}")
    return found


def render_differences(left: list[dict], right: list[dict]) -> list[str]:
    """Every way two renders of one cell differ: an object only one holds, then each leaf."""
    ids_left = {object_id(d) for d in left}
    ids_right = {object_id(d) for d in right}
    found = [f"only left: {o}" for o in sorted(ids_left - ids_right)]
    found += [f"only right: {o}" for o in sorted(ids_right - ids_left)]
    return found + runtime_diffs(left, right)


def rendered_twice(docs: list[dict]) -> list[str]:
    """Each ``Kind/name`` the render holds more than once, across the thin chart and dfe-extras."""
    ids = [object_id(d) for d in docs]
    return sorted({i for i in ids if ids.count(i) > 1})


def forward_references(docs: list[dict]) -> list[str]:
    """Each ``$(NAME)`` in an env value that no earlier variable declares; empty is a pass.

    The kubelet expands a reference only to a variable declared before it, in the
    container's envFrom or earlier in its env, and leaves the rest as written. The
    library renders extraEnv in name order, so a reference to a later name breaks
    silently.
    """
    configmaps = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "ConfigMap"}
    found = []
    for doc in docs:
        if doc.get("kind") not in WORKLOAD_KINDS:
            continue
        pod = _pod_spec(doc)
        for container in (pod.get("initContainers") or []) + (pod.get("containers") or []):
            declared = set()
            for source in container.get("envFrom") or []:
                ref = (source.get("configMapRef") or {}).get("name")
                prefix = source.get("prefix", "")
                data = (configmaps.get(ref) or {}).get("data") or {}
                declared |= {prefix + name for name in data}
            for entry in container.get("env") or []:
                for name in re.findall(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)", entry.get("value") or ""):
                    if name not in declared:
                        found.append(
                            f"{object_id(doc)} {container.get('name')} {entry['name']} -> {name}"
                        )
                declared.add(entry["name"])
    return found


def references(docs: list[dict]) -> list[tuple[str, str, bool, str]]:
    """Every Secret and ConfigMap a workload names: (kind, name, optional, where)."""
    found = []
    for doc in docs:
        if doc.get("kind") not in WORKLOAD_KINDS:
            continue
        pod = _pod_spec(doc)
        where = object_id(doc)
        for container in (pod.get("initContainers") or []) + (pod.get("containers") or []):
            at = f"{where} {container.get('name')}"
            for entry in container.get("env") or []:
                source = entry.get("valueFrom") or {}
                for key, kind in (("secretKeyRef", "Secret"), ("configMapKeyRef", "ConfigMap")):
                    ref = source.get(key)
                    if ref:
                        optional = bool(ref.get("optional"))
                        found.append((kind, ref.get("name"), optional, f"{at} env {entry['name']}"))
            for source in container.get("envFrom") or []:
                for key, kind in (("secretRef", "Secret"), ("configMapRef", "ConfigMap")):
                    ref = source.get(key)
                    if ref:
                        found.append(
                            (kind, ref.get("name"), bool(ref.get("optional")), f"{at} envFrom")
                        )
        for volume in pod.get("volumes") or []:
            at = f"{where} volume {volume.get('name')}"
            for item in [volume, *((volume.get("projected") or {}).get("sources") or [])]:
                if "secret" in item:
                    ref = item["secret"]
                    name = ref.get("secretName") or ref.get("name")
                    found.append(("Secret", name, bool(ref.get("optional")), at))
                if "configMap" in item:
                    ref = item["configMap"]
                    found.append(("ConfigMap", ref.get("name"), bool(ref.get("optional")), at))
        for pull in pod.get("imagePullSecrets") or []:
            name = pull.get("name") if isinstance(pull, dict) else pull
            found.append(("Secret", name, False, f"{where} imagePullSecrets"))
    return found


def unresolved_refs(docs: list[dict], elsewhere: set[tuple[str, str]]) -> list[str]:
    """Each Secret or ConfigMap a render names that nothing provides; empty is a pass.

    A reference resolves when it is optional, when the render holds the object (or an
    ExternalSecret writing it), when the cluster provides it, or when ``elsewhere``
    names it: a stack object another Application or bootstrap provides.
    """
    provided = {(d.get("kind"), d.get("metadata", {}).get("name")) for d in docs}
    provided |= CLUSTER_PROVIDED | elsewhere
    for doc in docs:
        if doc.get("kind") == "ExternalSecret":
            target = (doc.get("spec") or {}).get("target") or {}
            provided.add(("Secret", target.get("name") or doc["metadata"]["name"]))
    return [
        f"{where} -> {kind} {name}"
        for kind, name, optional, where in references(docs)
        if not optional and (kind, name) not in provided
    ]


def route_problems(docs: list[dict]) -> list[str]:
    """Each GRPCRoute backend that names a Service port the render does not serve; empty is a pass.

    The mesh trio's route comes from dfe-extras and its backend Service from the
    thin chart, so a port the contract gates off leaves the route sending to
    nothing, with both objects present and applied.
    """
    served: dict[tuple[str, str], set[object]] = {}
    for doc in docs:
        if doc.get("kind") == "Service":
            meta = doc.get("metadata") or {}
            ports = {p.get("port") for p in (doc.get("spec") or {}).get("ports") or []}
            served[(meta.get("namespace") or NAMESPACE, meta.get("name"))] = ports
    found = []
    for doc in docs:
        if doc.get("kind") != "GRPCRoute":
            continue
        namespace = (doc.get("metadata") or {}).get("namespace") or NAMESPACE
        for rule in (doc.get("spec") or {}).get("rules") or []:
            for ref in rule.get("backendRefs") or []:
                if ref.get("group", "") != "" or ref.get("kind", "Service") != "Service":
                    continue
                target = (ref.get("namespace") or namespace, ref.get("name"))
                if target not in served:
                    found.append(f"{object_id(doc)} -> Service {target[1]}, which the render lacks")
                elif ref.get("port") not in served[target]:
                    found.append(
                        f"{object_id(doc)} -> Service {target[1]} port {ref.get('port')}, "
                        f"which it does not serve"
                    )
    return found


def exposed(docs: list[dict]) -> dict[str, dict]:
    """Each Service reachable from outside the cluster: its type and ports."""
    found = {}
    for doc in docs:
        spec = doc.get("spec") or {}
        if doc.get("kind") == "Service" and spec.get("type") in ("LoadBalancer", "NodePort"):
            ports = sorted(
                f"{p.get('port')}/{p.get('protocol', 'TCP')}" for p in spec.get("ports") or []
            )
            found[object_id(doc)] = {"type": spec["type"], "ports": ports}
    return found
