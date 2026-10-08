#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         _gate.py
#  Purpose:      The chart-switch gate the test_weave_* files assert: the
#                component matrix, each cell's two renders, the accepted
#                diffs, and the checks that compare the renders.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The chart-switch gate, shared by test_weave_identity.py, test_weave_runtime.py,
test_weave_exposure.py and test_weave_value_map.py.

Each cell of the matrix is one component on one profile and cloud, rendered twice
through scripts/dfe-weave: its 2.2.0 chart, and its thin chart beside dfe-extras
with the integration values under argocd/values/apps. A component joins the gate
as a row of COMPONENTS, an entry in fixtures/weave-accepted-diffs.yaml and an
entry in scripts/weave/value-map.yaml; every test parametrises over COMPONENTS.
A component another appset deploys names it in APPSETS, and instance values its
2.2.0 chart refuses to render without go in INSTANCE.

Renders are cached for the run, so the four files share them. A test that
changes one works on a copy.
"""

import copy
import functools
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from _weave import TESTS, render_app, weave

# The deploy.service of each component the gate covers, in the order they switch.
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

# The instance values a deployer has to write before a component's 2.2.0 chart renders
# at all, per cloud. culvert reads local.yaml's receiver `listeners` unless it restates
# its own, and edge-aws.yaml's external PKI needs its Secret named.
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
# the cluster facts it changes, and the deploy repo's infra/common.yaml.
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
}
DEFAULT = "default"

ACCEPTED = TESTS / "fixtures" / "weave-accepted-diffs.yaml"
ABSENT = "<absent>"
NAMESPACE = "dfe"

# Objects the cluster itself provides in every namespace: kube-controller-manager
# publishes the API server's CA as this ConfigMap.
CLUSTER_PROVIDED = {("ConfigMap", "kube-root-ca.crt")}

# What Kubernetes gives a pod that leaves these out, so leaving one out compares as setting it.
POD_DEFAULTS = {
    "automountServiceAccountToken": True,
    "enableServiceLinks": True,
    "serviceAccountName": "default",
    "terminationGracePeriodSeconds": 30,
}


# ---------------------------------------------------------------------- renders


@dataclass(frozen=True, slots=True)
class Cell:
    """One component's two renders, for one profile, cloud and scenario."""

    service: str
    profile: str
    cloud: str
    scenario: str
    old: list[dict]
    new: list[dict]

    def __str__(self) -> str:
        return f"{self.service} {self.profile}/{self.cloud} ({self.scenario})"

    def mutable(self) -> Cell:
        """A copy whose renders a test may change."""
        return Cell(
            self.service,
            self.profile,
            self.cloud,
            self.scenario,
            copy.deepcopy(self.old),
            copy.deepcopy(self.new),
        )


@functools.cache
def cell(service: str, profile: str, cloud: str, scenario: str = DEFAULT) -> Cell:
    """Both renders of one component, cached for the run."""
    shape = SCENARIOS.get(scenario, {"facts": {}, "infra": None})
    instance = INSTANCE.get(service, {}).get(cloud)
    with tempfile.TemporaryDirectory(prefix="dfe-gate-deploy-") as tmp:
        options: dict = dict(shape["facts"])
        if service in APPSETS:
            options["appset"] = APPSETS[service]
        if shape["infra"] is not None:
            infra = Path(tmp) / "infra"
            infra.mkdir()
            (infra / "common.yaml").write_text(
                yaml.safe_dump(shape["infra"]), encoding="utf-8", newline="\n"
            )
            options["deploy_repo"] = Path(tmp)
        if instance is not None:
            body = {"deploy": {"service": service, "instance": "default"}, **instance}
            values = Path(tmp) / "values"
            values.mkdir()
            (values / f"{service}-default-values.yaml").write_text(
                yaml.safe_dump(body), encoding="utf-8", newline="\n"
            )
            options["deploy_repo"] = Path(tmp)
        old = render_app(service, profile, cloud, "old", **options)
        new = render_app(service, profile, cloud, "new", **options)
    return Cell(service, profile, cloud, scenario, old, new)


def cells(service: str) -> list[Cell]:
    """Every cell of a component: the matrix, then each scenario."""
    found = [cell(service, p, k) for p in PROFILES for k in CLOUDS]
    return found + [cell(service, "slim", "local", s) for s in SCENARIOS]


def object_id(doc: dict) -> str:
    """``Kind/name``, Argo's identity for an object within its namespace."""
    return f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')}"


# --------------------------------------------------------------- accepted diffs


@dataclass(frozen=True, slots=True)
class Diff:
    """One leaf that differs between the renders: ``old`` or ``new`` is ABSENT where missing."""

    object: str
    path: str
    old: object
    new: object

    def __str__(self) -> str:
        return f"{self.object} {self.path}: {_canon(self.old)} -> {_canon(self.new)}"


def _canon(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _glob(pattern: str) -> re.Pattern[str]:
    """``*`` matches any run of characters; everything else is literal, brackets included."""
    return re.compile(".*".join(re.escape(part) for part in pattern.split("*")))


def _pinned(pin: object, value: object) -> bool:
    """Whether a value is the one an entry pins; a string pin with ``*`` in it is a glob."""
    if isinstance(pin, str) and "*" in pin and isinstance(value, str):
        return bool(_glob(pin).fullmatch(value))
    return _canon(pin) == _canon(value)


@dataclass(frozen=True, slots=True)
class Accepted:
    """One accepted diff: object and path globs, the values when pinned, and the reason."""

    object: str
    path: str
    reason: str
    old: object = ABSENT
    new: object = ABSENT
    pins_old: bool = False
    pins_new: bool = False
    scenarios: tuple[str, ...] = ()

    def matches(self, diff: Diff, scenario: str) -> bool:
        """Whether this entry accepts ``diff`` in ``scenario``."""
        if self.scenarios and scenario not in self.scenarios:
            return False
        if not _glob(self.object).fullmatch(diff.object):
            return False
        if not _glob(self.path).fullmatch(diff.path):
            return False
        if self.pins_old and not _pinned(self.old, diff.old):
            return False
        return not (self.pins_new and not _pinned(self.new, diff.new))

    def __str__(self) -> str:
        return f"{self.object} {self.path}"


@dataclass(frozen=True, slots=True)
class Accepts:
    """A component's accepted diffs: objects only the new render has, objects only the
    2.2.0 render has, and leaf diffs."""

    added: dict[str, str]
    diffs: tuple[Accepted, ...]
    removed: dict[str, str] = field(default_factory=dict)


class AcceptedError(Exception):
    """fixtures/weave-accepted-diffs.yaml does not have the shape the gate reads."""


def _entry(service: str, raw: object) -> Accepted:
    if not isinstance(raw, dict):
        raise AcceptedError(f"{service}: a diff entry is not a mapping: {raw!r}")
    unknown = set(raw) - {"object", "path", "old", "new", "reason", "scenarios"}
    if unknown:
        raise AcceptedError(f"{service}: {raw} carries {sorted(unknown)}")
    for key in ("object", "path", "reason"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise AcceptedError(f"{service}: {raw} has no {key}")
    scenarios = tuple(raw.get("scenarios") or ())
    stray = [s for s in scenarios if s not in SCENARIOS and s != DEFAULT]
    if stray:
        raise AcceptedError(f"{service}: {raw['object']} {raw['path']} names scenarios {stray}")
    return Accepted(
        object=raw["object"],
        path=raw["path"],
        reason=raw["reason"],
        old=raw.get("old", ABSENT),
        new=raw.get("new", ABSENT),
        pins_old="old" in raw,
        pins_new="new" in raw,
        scenarios=scenarios,
    )


EVERY = "*"


@functools.cache
def accepted() -> dict[str, Accepts]:
    """The accepted diffs by section: a deploy.service, or ``*`` for every component.

    Raises:
        AcceptedError: The file has an entry the gate cannot read.
    """
    data = yaml.safe_load(ACCEPTED.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise AcceptedError(f"{ACCEPTED} is not a mapping of component to its diffs")
    found = {}
    for service, body in data.items():
        if not isinstance(body, dict) or set(body) - {"added", "removed", "diffs"}:
            raise AcceptedError(f"{service}: takes `added`, `removed` and `diffs` only")
        added = body.get("added") or {}
        removed = body.get("removed") or {}
        for kind, objects in (("added", added), ("removed", removed)):
            if not all(isinstance(r, str) and r.strip() for r in objects.values()):
                raise AcceptedError(f"{service}: every {kind} object needs its reason")
        # A pruned claim comes back empty, so no reason accepts one.
        claims = sorted(o for o in removed if o.startswith("PersistentVolumeClaim/"))
        if claims:
            raise AcceptedError(f"{service}: a claim cannot be accepted as removed: {claims}")
        found[service] = Accepts(
            added=dict(added),
            diffs=tuple(_entry(service, e) for e in body.get("diffs") or []),
            removed=dict(removed),
        )
    return found


def accepts(service: str) -> Accepts:
    """One component's accepted diffs, its own section after the ``*`` one."""
    none = Accepts(added={}, diffs=())
    every, own = accepted().get(EVERY, none), accepted().get(service, none)
    return Accepts(
        added={**every.added, **own.added},
        diffs=every.diffs + own.diffs,
        removed={**every.removed, **own.removed},
    )


# --------------------------------------------------------------------- identity


def identity_problems(
    old: list[dict],
    new: list[dict],
    added: dict[str, str],
    removed: dict[str, str] | None = None,
) -> list[str]:
    """How the new render fails to adopt every 2.2.0 object; empty is a pass.

    Argo adopts a live object by (kind, name), so a lost or renamed object is
    pruned and recreated, and a renamed claim comes back empty. A selector is
    immutable. An object only the new render has must be in ``added``, an object
    only the 2.2.0 render has must be in ``removed``, which Argo then prunes, and
    no (kind, name) may render twice across the Application's two sources. A
    claim is held whatever ``removed`` says.
    """
    gone = set(removed or {})
    facets = weave().diff_docs(old, new)
    problems = []
    lost = [o for o in facets["objects"]["only_old"] if o not in gone]
    if lost:
        problems.append(f"lost or renamed: {lost}")
    if facets["pvcs"]["only_old"]:
        problems.append(f"claims lost or renamed: {facets['pvcs']['only_old']}")
    for key in facets["selector"]["differ"]:
        if key in gone:
            continue
        old_selector = facets["selector"]["old"][key]
        problems.append(f"{key} selector {old_selector} -> {facets['selector']['new'][key]}")
    unlisted = sorted(set(facets["objects"]["only_new"]) - set(added))
    if unlisted:
        problems.append(f"added and not accepted: {unlisted}")
    ids = [object_id(d) for d in new]
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        problems.append(f"rendered twice: {twice}")
    return problems


# ---------------------------------------------------------------------- runtime


class Keyed(dict):
    """A list Kubernetes merges by a key, held by that key so it compares item by item."""


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


def _container(container: dict, configmaps: dict[str, dict], numbers: dict[str, int]) -> dict:
    w = weave()
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
    for probe in w.PROBES:
        if probe in out:
            out[probe] = w._probe(out[probe], numbers)
    return out


def _metadata(doc: dict) -> dict:
    meta = doc.get("metadata") or {}
    out = {k: v for k, v in meta.items() if k in ("labels", "annotations")}
    # Argo applies every object into its Application's namespace either way.
    if meta.get("namespace") not in (None, NAMESPACE):
        out["namespace"] = meta["namespace"]
    return out


def _workload(doc: dict, configmaps: dict[str, dict]) -> dict:
    w = weave()
    spec = dict(doc.get("spec") or {})
    template = spec.pop("template", {}) or {}
    pod = {**POD_DEFAULTS, **(template.get("spec") or {})}
    numbers = w._port_numbers([doc])
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
        spec["strategy"] = spec.get("strategy") or w.DEFAULT_STRATEGY
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
    w = weave()
    configmaps = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "ConfigMap"}
    numbers = w._port_numbers(docs)
    view = {}
    for doc in docs:
        kind = doc.get("kind")
        if kind in w.WORKLOAD_KINDS:
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


def runtime_diffs(old: list[dict], new: list[dict]) -> list[Diff]:
    """Every leaf that differs in an object both renders hold."""
    before, after = runtime_view(old), runtime_view(new)
    found = []
    for obj in sorted(set(before) & set(after)):
        a, b = before[obj], after[obj]
        for path in sorted(set(a) | set(b)):
            left, right = a.get(path, ABSENT), b.get(path, ABSENT)
            if _canon(left) != _canon(right):
                found.append(Diff(obj, path, left, right))
    return found


def unaccepted(diffs: list[Diff], entries: tuple[Accepted, ...], scenario: str) -> list[str]:
    """The diffs no entry accepts."""
    return [str(d) for d in diffs if not any(e.matches(d, scenario) for e in entries)]


def _images(docs: list[dict]) -> dict[str, str]:
    """Each container's image by workload and container, the main one as ``main``."""
    found = {}
    for doc in docs:
        if doc.get("kind") not in weave().WORKLOAD_KINDS:
            continue
        pod = (doc.get("spec", {}).get("template", {}) or {}).get("spec", {}) or {}
        for i, c in enumerate(pod.get("containers") or []):
            found[f"{object_id(doc)} {'main' if i == 0 else c.get('name')}"] = c.get("image", "")
        for c in pod.get("initContainers") or []:
            found[f"{object_id(doc)} init/{c.get('name')}"] = c.get("image", "")
    return found


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
        if doc.get("kind") not in weave().WORKLOAD_KINDS:
            continue
        pod = (doc.get("spec", {}).get("template", {}) or {}).get("spec", {}) or {}
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


def image_problems(old: list[dict], new: list[dict]) -> list[str]:
    """Each container whose image moved beyond its registry; empty is a pass.

    The registry may move with global.registry. The name, tag and digest after it
    may not: a switch that runs other bytes is a release, not a chart change.
    """
    before, after = _images(old), _images(new)
    return [
        f"{key}: {before[key]} -> {after[key]}"
        for key in sorted(set(before) & set(after))
        if before[key].rsplit("/", 1)[-1] != after[key].rsplit("/", 1)[-1]
    ]


def _refs(docs: list[dict]) -> list[tuple[str, str, bool, str]]:
    """Every Secret and ConfigMap a workload names: (kind, name, optional, where)."""
    found = []
    for doc in docs:
        if doc.get("kind") not in weave().WORKLOAD_KINDS:
            continue
        pod = (doc.get("spec", {}).get("template", {}) or {}).get("spec", {}) or {}
        where = object_id(doc)
        for container in (pod.get("initContainers") or []) + (pod.get("containers") or []):
            at = f"{where} {container.get('name')}"
            for entry in container.get("env") or []:
                source = entry.get("valueFrom") or {}
                for key, kind in (("secretKeyRef", "Secret"), ("configMapKeyRef", "ConfigMap")):
                    ref = source.get(key)
                    if ref:
                        found.append(
                            (
                                kind,
                                ref.get("name"),
                                bool(ref.get("optional")),
                                f"{at} env {entry['name']}",
                            )
                        )
            for source in container.get("envFrom") or []:
                for key, kind in (("secretRef", "Secret"), ("configMapRef", "ConfigMap")):
                    ref = source.get(key)
                    if ref:
                        found.append(
                            (kind, ref.get("name"), bool(ref.get("optional")), f"{at} envFrom")
                        )
        for volume in pod.get("volumes") or []:
            for item in [volume, *((volume.get("projected") or {}).get("sources") or [])]:
                if "secret" in item:
                    ref = item["secret"]
                    name = ref.get("secretName") or ref.get("name")
                    found.append(
                        (
                            "Secret",
                            name,
                            bool(ref.get("optional")),
                            f"{where} volume {volume.get('name')}",
                        )
                    )
                if "configMap" in item:
                    ref = item["configMap"]
                    found.append(
                        (
                            "ConfigMap",
                            ref.get("name"),
                            bool(ref.get("optional")),
                            f"{where} volume {volume.get('name')}",
                        )
                    )
        for pull in pod.get("imagePullSecrets") or []:
            name = pull.get("name") if isinstance(pull, dict) else pull
            found.append(("Secret", name, False, f"{where} imagePullSecrets"))
    return found


def unresolved_refs(old: list[dict], new: list[dict]) -> list[str]:
    """Each Secret or ConfigMap the new render names that nothing provides; empty is a pass.

    A reference resolves when it is optional, when the render holds the object (or an
    ExternalSecret writing it), when the cluster provides it, or when 2.2.0 named it
    too, which makes it a stack object some other chart provides.
    """
    provided = {(d.get("kind"), d.get("metadata", {}).get("name")) for d in new}
    provided |= CLUSTER_PROVIDED
    for doc in new:
        if doc.get("kind") == "ExternalSecret":
            target = (doc.get("spec") or {}).get("target") or {}
            provided.add(("Secret", target.get("name") or doc["metadata"]["name"]))
    before = {(kind, name) for kind, name, _, _ in _refs(old)}
    return [
        f"{where} -> {kind} {name}"
        for kind, name, optional, where in _refs(new)
        if not optional and (kind, name) not in provided and (kind, name) not in before
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


# --------------------------------------------------------------------- exposure


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
