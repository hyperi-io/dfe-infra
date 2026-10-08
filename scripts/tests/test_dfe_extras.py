#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_extras.py
#  Purpose:      Prove helm/charts/dfe-extras renders, per component and per
#                profile, exactly the DFE-only objects the 2.2.0 chart renders,
#                with unchanged (kind, name), and none the thin chart renders.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The chart-switch gate for helm/charts/dfe-extras.

    python3 -m pytest scripts/tests/test_dfe_extras.py -q
    DFE_WEAVE_HELM=<helm 4.2.4> python3 -m pytest scripts/tests/test_dfe_extras.py -q

Argo adopts a live object by (kind, name), so a renamed object is pruned and
recreated: a generated Secret is minted again, a PVC comes back empty. Each
component is rendered twice from the Application argocd/appsets/layer2-apps.yaml
generates for it, through scripts/dfe-weave: its 2.2.0 chart, and dfe-extras as
that Application's dfe-extras source, or, where the appset has none, its chart
source re-pointed at helm/charts/dfe-extras with the per-app layers added and
chartName set. The gate:

- dfe-extras renders the objects the 2.2.0 DFE_ONLY_TEMPLATES render, byte for
  byte once parsed, plus the <fullname>-env ConfigMap for a component whose env
  the thin chart reads through envFrom, holding the 2.2.0 container's literal env
- every other 2.2.0 object carries a name the thin chart renders (THIN_SUFFIXES),
  and no dfe-extras object does, so nothing is lost or rendered twice

Which objects are DFE-only is a fact about the 2.2.0 template that rendered
them, not their name, so each 2.2.0 object keeps its `# Source:` template here.
The dfe-ui and dfe-hyperdx thin-chart cases need DFE_WEAVE_LIBRARY and skip
without it.
"""

import copy
import importlib
import json
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from _weave import REPO_ROOT, helm, render_app, weave

EXTRAS_PATH = "helm/charts/dfe-extras"
EXTRAS = REPO_ROOT / EXTRAS_PATH
VALUES = REPO_ROOT / "argocd" / "values"

# Every component the layer 2 appsets deploy, by deploy.service.
COMPONENTS = (
    "dfe-receiver",
    "dfe-loader",
    "dfe-fetcher",
    "dfe-archiver",
    "dfe-transform-vrl",
    "dfe-transform-vector",
    "dfe-transform-elastic",
    "culvert",
    "dfe-ui",
    "hyperdx",
    "dfe-engine",
)
PROFILES = ("slim", "single", "scale", "mesh")
CLOUDS = ("local", "aws")

# The 2.2.0 templates whose objects dfe-extras renders, by chartName. Everything
# else a 2.2.0 chart renders is the thin chart's.
DFE_ONLY_TEMPLATES: dict[str, tuple[str, ...]] = {
    "dfe-receiver": ("networkpolicy-ingest.yaml", "pool-listener.yaml"),
    "dfe-loader": ("pool-listener.yaml",),
    "dfe-archiver": ("pool-listener.yaml",),
    "dfe-transform-vrl": ("pool-listener.yaml",),
    "dfe-transform-vector": ("pool-listener.yaml",),
    "dfe-transform-elastic": ("pool-listener.yaml",),
    "dfe-fetcher": (),
    "culvert": ("networkpolicy.yaml",),
    "dfe-ui": ("externalsecret-nextauth.yaml",),
    "dfe-hyperdx": ("generated-keys.yaml",),
    "dfe-engine": (
        "admin-secret.yaml",
        "app-catalogue.yaml",
        "hunt-runner-clickhouse.yaml",
        "hunt-runner.yaml",
        "jwt-secret.yaml",
        "keda-shim.yaml",
        "seed-accounts-secret.yaml",
    ),
}

# The components whose env dfe-extras carries in a <fullname>-env ConfigMap.
ENV_CONFIGMAP = ("dfe-engine", "dfe-hyperdx")

# What the scalo-service library names objects, per kind, after <fullname>: its
# README's "What the library renders" table, with the fileSets and writable paths
# the components declare (transforms, enrichment; config, cursor, pki, spool).
THIN_SUFFIXES: dict[str, tuple[str, ...]] = {
    "Deployment": ("",),
    "Service": ("", "-public", "-public-udp"),
    "ConfigMap": ("-config", "-transforms", "-enrichment"),
    "ServiceAccount": ("",),
    "PersistentVolumeClaim": ("-config", "-cursor", "-pki", "-spool"),
    "PodDisruptionBudget": ("",),
    "ScaledObject": ("-scaler",),
    "TriggerAuthentication": ("-trigger-auth",),
    "HorizontalPodAutoscaler": ("",),
    "NetworkPolicy": ("",),
}

# A thin-chart name dfe-extras renders too, behind dfe-extras.culvertPolicyGuard,
# which fails the render when a layer switches the thin chart's copy on.
GUARDED_KINDS = {"culvert": {"NetworkPolicy"}}

# argocd/values/local.yaml restates `listeners` for the receiver, and culvert reads
# the same key, so a culvert on local restates its own in its instance file.
CULVERT_LISTENERS = {
    "listeners": [
        {"name": "wireguard", "port": 51820, "protocol": "UDP", "exposed": True},
        {"name": "openvpn-udp", "port": 1194, "protocol": "UDP", "exposed": True},
        {"name": "metrics", "port": 9090, "protocol": "TCP", "exposed": False},
    ]
}

type ObjectId = tuple[str, str]


def chart_name(service: str) -> str:
    """The chartName the appset hands dfe-extras for a deploy.service."""
    return "dfe-hyperdx" if service == "hyperdx" else service


def thin_ids(fullname: str) -> set[ObjectId]:
    """Every (kind, name) the thin chart of a component named ``fullname`` can render."""
    found = set()
    for kind, suffixes in THIN_SUFFIXES.items():
        found |= {(kind, fullname + suffix) for suffix in suffixes}
    return found


def base_overlay(service: str, cloud: str) -> dict | None:
    """The instance values a component needs before 2.2.0 renders it at all."""
    return CULVERT_LISTENERS if (service, cloud) == ("culvert", "local") else None


# ----------------------------------------------------------------- the renders


class RenderError(Exception):
    """A render helm refused, with helm's message."""


@dataclass(slots=True)
class Pair:
    """One component's 2.2.0 render and its dfe-extras render.

    Attributes:
        service: The deploy.service rendered.
        old: The 2.2.0 objects by (kind, name), each with the template that rendered it.
        extras: The dfe-extras objects by (kind, name).
    """

    service: str
    old: dict[ObjectId, tuple[str, dict]] = field(default_factory=dict)
    extras: dict[ObjectId, dict] = field(default_factory=dict)

    def dfe_only(self) -> dict[ObjectId, dict]:
        """The 2.2.0 objects a DFE-only template rendered."""
        templates = DFE_ONLY_TEMPLATES[chart_name(self.service)]
        return {key: doc for key, (source, doc) in self.old.items() if source in templates}

    def thin(self) -> dict[ObjectId, dict]:
        """The 2.2.0 objects the thin chart takes over."""
        dfe_only = self.dfe_only()
        return {key: doc for key, (_, doc) in self.old.items() if key not in dfe_only}

    def workload(self) -> dict:
        """The component's own 2.2.0 Deployment."""
        found = [doc for (kind, _), doc in self.thin().items() if kind == "Deployment"]
        if len(found) != 1:
            raise AssertionError(f"{self.service}: {len(found)} component Deployments, not one")
        return found[0]


def _documents(text: str) -> list[tuple[str, dict]]:
    """Each object of a render, with the template file helm says it came from."""
    found = []
    for chunk in text.split("\n---"):
        source = ""
        for line in chunk.splitlines():
            if line.startswith("# Source: "):
                source = line.removeprefix("# Source: ").rsplit("/", 1)[-1]
        doc = yaml.safe_load(chunk)
        if isinstance(doc, dict):
            found.append((source, doc))
    return found


def _template(source: dict, chart: Path, release: str, namespace: str, chain: list[dict]) -> str:
    """``helm template`` one Application source: value files, inline values, parameters.

    The layering is dfe-weave's render_source; this keeps helm's raw output,
    which carries each object's template.
    """
    w = weave()
    block = source.get("helm") or {}
    cmd = [helm(), "template", release, str(chart), "--namespace", namespace]
    cmd += [arg for layer in chain if layer["present"] for arg in ("--values", str(layer["path"]))]
    with tempfile.TemporaryDirectory(prefix="dfe-extras-values-") as tmp:
        inline = block.get("valuesObject")
        text = yaml.safe_dump(inline) if isinstance(inline, dict) else block.get("values")
        if text:
            inline_file = Path(tmp) / "inline-values.yaml"
            inline_file.write_text(text, encoding="utf-8", newline="\n")
            cmd += ["--values", str(inline_file)]
        for param in block.get("parameters") or []:
            flag = "--set-string" if param.get("forceString") else "--set"
            cmd += [flag, f"{param['name']}={w._argo_set_value(str(param.get('value', '')))}"]
        out = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
    if out.returncode != 0:
        raise RenderError(out.stderr.strip())
    return out.stdout


def _extras_source(sources: list[dict], service: str) -> dict:
    """The Application's dfe-extras source, or its chart source re-pointed there."""
    for source in sources:
        if source.get("path") == EXTRAS_PATH:
            return source
    extras = copy.deepcopy(next(s for s in sources if "helm" in s))
    extras["path"] = EXTRAS_PATH
    extras["helm"].setdefault("parameters", []).append(
        {"name": "chartName", "value": chart_name(service)}
    )
    return extras


@contextmanager
def deploy_repo(service: str, overlay: dict | None, infra: dict | None) -> Iterator[Path | None]:
    """A deploy repo with the instance values and infra/common.yaml, or None for neither."""
    if overlay is None and infra is None:
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="dfe-extras-deploy-") as tmp:
        root = Path(tmp)
        if overlay is not None:
            body = {"deploy": {"service": service, "instance": "default"}, **overlay}
            (root / "values").mkdir()
            instance = root / "values" / f"{service}-default-values.yaml"
            instance.write_text(yaml.safe_dump(body), encoding="utf-8", newline="\n")
        if infra is not None:
            (root / "infra").mkdir()
            common = root / "infra" / "common.yaml"
            common.write_text(yaml.safe_dump(infra), encoding="utf-8", newline="\n")
        yield root


def render_pair(
    service: str,
    profile: str,
    cloud: str,
    *,
    overlay: dict | None = None,
    infra: dict | None = None,
    annotations: dict[str, str] | None = None,
    old: bool = True,
    extras: bool = True,
) -> Pair:
    """Render dfe-extras and the 2.2.0 chart for one component.

    Args:
        service: deploy.service, e.g. ``dfe-engine`` or ``hyperdx``.
        profile: The profile annotation.
        cloud: The cloud annotation.
        overlay: The deploy repo's instance values, the last layer of both renders.
        infra: The deploy repo's infra/common.yaml.
        annotations: Further cluster-secret annotations, which feed the appset's parameters.
        old: Render the 2.2.0 chart.
        extras: Render dfe-extras.

    Returns:
        The renders asked for.

    Raises:
        RenderError: helm refused a render.
    """
    w = weave()
    facts = tuple(sorted((annotations or {}).items()))
    target = w.Target(service, profile, cloud, annotations=facts)
    pair = Pair(service=service)
    with deploy_repo(service, overlay, infra) as repo:
        app = w.application(REPO_ROOT, w.APPSET, target, repo, helm())
        spec = app["spec"]
        sources = spec.get("sources") or [spec["source"]]
        annotations = target.cluster_annotations()
        # The local copy of each repo the Application names, as dfe-weave's render_app maps them.
        layout = w._Layout(
            tree=REPO_ROOT,
            repos={
                annotations[f"{w.ANNOTATION}repo_url"]: REPO_ROOT,
                annotations[f"{w.ANNOTATION}config_repo_url"]: repo,
            },
            refs={s["ref"]: s for s in sources if "ref" in s},
            apps_dir=None,
        )
        namespace = spec.get("destination", {}).get("namespace") or target.namespace
        renders = [(_extras_source(sources, service), True)] if extras else []
        if old:
            chart = next(s for s in sources if "helm" in s and s.get("path") != EXTRAS_PATH)
            renders.append((chart, False))
        for source, is_extras in renders:
            path = source["path"]
            release = source["helm"].get("releaseName") or app["metadata"]["name"]
            raw_files = list(source["helm"].get("valueFiles") or [])
            base = REPO_ROOT / path
            chain = w.value_chain(raw_files, layout, base, REPO_ROOT, target, insert=is_extras)
            text = _template(source, base, release, namespace, chain)
            for template, doc in _documents(text):
                key = (doc.get("kind"), doc["metadata"]["name"])
                if is_extras:
                    pair.extras[key] = doc
                else:
                    pair.old[key] = (template, doc)
    return pair


# --------------------------------------------------------------------- the gate


def literal_env(container: dict) -> dict[str, str]:
    """A container's literal env, as the kubelet reads it: in order, the last name winning.

    An entry the kubelet resolves (valueFrom, or a value carrying $(VAR), which
    envFrom never expands) drops any earlier literal of its name.
    """
    found: dict[str, str] = {}
    for entry in container.get("env") or []:
        value = entry.get("value")
        text = "" if value is None else str(value)
        if "valueFrom" in entry or "$(" in text:
            found.pop(entry["name"], None)
        else:
            found[entry["name"]] = text
    return found


def first_difference(old: object, new: object, path: str = "") -> str:
    """The first path where two parsed objects differ, for a failure message."""
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new), key=str):
            if old.get(key) != new.get(key):
                return first_difference(old.get(key), new.get(key), f"{path}.{key}")
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for index, (a, b) in enumerate(zip(old, new, strict=True)):
            if a != b:
                return first_difference(a, b, f"{path}[{index}]")
    return f"{path or '.'}: 2.2.0 {json.dumps(old)[:200]} / dfe-extras {json.dumps(new)[:200]}"


def gate(pair: Pair) -> list[str]:
    """Every way dfe-extras departs from 2.2.0 for one render; empty is a pass."""
    name = chart_name(pair.service)
    problems = []
    want = pair.dfe_only()
    got = dict(pair.extras)
    workload = pair.workload()
    fullname = workload["metadata"]["name"]
    if name in ENV_CONFIGMAP:
        env = got.pop(("ConfigMap", f"{fullname}-env"), None)
        container = workload["spec"]["template"]["spec"]["containers"][0]
        if env is None:
            problems.append(f"no ConfigMap {fullname}-env")
        elif (env.get("data") or {}) != literal_env(container):
            problems.append(
                f"{fullname}-env "
                + first_difference(literal_env(container), env.get("data") or {}, ".data")
            )
    missing = sorted(set(want) - set(got))
    added = sorted(set(got) - set(want))
    if missing:
        problems.append(f"2.2.0 renders, dfe-extras does not: {missing}")
    if added:
        problems.append(f"dfe-extras renders, 2.2.0 does not: {added}")
    for key in sorted(set(want) & set(got)):
        if want[key] != got[key]:
            problems.append(f"{key} {first_difference(want[key], got[key])}")
    thin = thin_ids(fullname)
    homeless = sorted(set(pair.thin()) - thin)
    if homeless:
        problems.append(f"2.2.0 objects neither DFE-only nor the thin chart's: {homeless}")
    guarded = GUARDED_KINDS.get(name, set())
    twice = sorted(key for key in set(pair.extras) & thin if key[0] not in guarded)
    if twice:
        problems.append(f"dfe-extras renders names the thin chart renders: {twice}")
    return problems


# ------------------------------------------------------- per component, per profile


@pytest.mark.parametrize("cloud", CLOUDS)
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("service", COMPONENTS)
def test_dfe_extras_renders_what_the_2_2_0_chart_renders(
    service: str, profile: str, cloud: str
) -> None:
    pair = render_pair(service, profile, cloud, overlay=base_overlay(service, cloud))
    assert gate(pair) == []


# Conditional objects and env the profile matrix does not reach, each with the
# object or env value it exists to produce, so a scenario cannot pass by rendering
# nothing. Values are illustrative, never a deployment's own.
SCENARIOS = [
    pytest.param(
        "dfe-engine", "scale", "aws",
        {"huntRunner": {"autoscaling": {"enabled": True}}},
        {"present": [("ScaledObject", "dfe-hunt-runner-scaler")]},
        id="engine-hunt-runner-autoscaling",
    ),
    pytest.param(
        "dfe-engine", "slim", "local",
        {"seedAuth": {"seedAccounts": [{"username": "alice", "password": "example-only"}]}},
        {"present": [("Secret", "dfe-engine-seed-accounts")]},
        id="engine-seed-accounts",
    ),
    pytest.param(
        "dfe-engine", "single", "local",
        {
            "auth": {"jwtSecret": "pinned", "adminPassword": "pinned", "breakglassPassword": "pinned"},
            "huntRunner": {"clickhouse": {"password": "pinned"}},
        },
        {
            "present": [
                ("Secret", "dfe-engine-jwt"),
                ("Secret", "dfe-engine-admin"),
                ("Secret", "dfe-engine-breakglass"),
                ("Secret", "hunt-runner-clickhouse"),
            ],
            "absent": [("Password", "dfe-engine-jwt-gen")],
        },
        id="engine-pinned-secrets",
    ),
    pytest.param(
        "dfe-engine", "single", "aws",
        {
            "auth": {"jwtSecretCreate": False, "adminSecretCreate": False, "breakglassSecretCreate": False},
            "huntRunner": {"clickhouse": {"passwordSecretCreate": False}},
        },
        {"absent": [("ExternalSecret", "dfe-engine-jwt"), ("ExternalSecret", "hunt-runner-clickhouse")]},
        id="engine-deployment-supplies-secrets",
    ),
    pytest.param(
        "dfe-engine", "slim", "local",
        {"gitops": {"enabled": False}},
        {"present": [("Deployment", "dfe-hunt-runner")], "unset": ["DFE_GITOPS_ENABLED"]},
        id="engine-without-gitops",
    ),
    pytest.param(
        "dfe-engine", "mesh", "aws",
        {"kedaShim": {"enabled": False}, "huntRunner": {"enabled": False}},
        {"absent": [("Deployment", "dfe-keda-shim"), ("Deployment", "dfe-hunt-runner")]},
        id="engine-shim-and-runner-off",
    ),
    pytest.param(
        "dfe-engine", "scale", "local",
        {
            "clickhouse": {"mode": "external", "tls": {"enabled": True, "ca": {"secretName": "ch-ca"}}},
            "api": {"maxSessionMinutes": 30, "docsEnabled": "false"},
            "auth": {"loginThrottle": {"enabled": "false", "usernameFailures": 3}},
            "authConfig": {"caBundleConfigMap": "ca-bundle"},
            "versionCheck": {"enabled": False},
        },
        {
            "env": {
                "DFE_CLICKHOUSE_CA_CERT": "/etc/dfe/clickhouse-ca/ca.crt",
                "DFE_API_MAX_SESSION_MINUTES": "30",
                "SSL_CERT_FILE": "/config/auth/ca-bundle.pem",
                "VERSION_CHECK__ENABLED": "false",
            }
        },
        id="engine-tls-and-dials",
    ),
    pytest.param(
        "dfe-engine", "single", "local",
        {"fullnameOverride": "dfe-engine"},
        {"present": [("ConfigMap", "dfe-engine-app-catalogue"), ("ConfigMap", "dfe-engine-env")]},
        id="engine-fullname-override-keeps-names",
    ),
    pytest.param(
        "dfe-ui", "slim", "aws",
        {"config": {"secretStoreName": "example-store"}},
        {"present": [("ExternalSecret", "dfe-ui-nextauth")], "absent": [("Password", "dfe-ui-nextauth-gen")]},
        id="ui-secret-store",
    ),
    pytest.param(
        "dfe-ui", "single", "local",
        {"config": {"nextauthSecretName": ""}},
        {"absent": [("ExternalSecret", "dfe-ui-nextauth")]},
        id="ui-no-nextauth-secret",
    ),
    pytest.param(
        "hyperdx", "scale", "aws",
        {"sessionSecret": {"create": False}, "tokenEncryption": {"create": False}},
        {"absent": [("ExternalSecret", "dfe-hyperdx-session"), ("Password", "dfe-hyperdx-token-encryption-gen")]},
        id="hyperdx-keys-supplied",
    ),
    pytest.param(
        "hyperdx", "slim", "local",
        {
            "dfeAuth": {"mode": "header-dev"},
            "localAuthPages": {"enabled": True},
            "defaultConnections": {"enabled": True},
            "clickhouse": {
                "user": "hyperdx",
                "mode": "external",
                "tls": {"enabled": True, "ca": {"secretName": "ch-ca"}},
            },
        },
        {
            "env": {
                "DFE_AUTH_HEADER_EMAIL": "x-oidc-subject",
                "DFE_LOCAL_AUTH_PAGES": "true",
                "CLICKHOUSE_USER": "hyperdx",
                "NODE_EXTRA_CA_CERTS": "/etc/dfe/clickhouse-ca/ca.crt",
            },
            "unset": ["DFE_ENGINE_JWKS_URL", "DEFAULT_CONNECTIONS"],
        },
        id="hyperdx-header-dev-and-tls",
    ),
    pytest.param(
        "dfe-receiver", "single", "local",
        {"exposure": {"mode": "public"}},
        {"present": [("NetworkPolicy", "dfe-receiver-ingest")]},
        id="receiver-public",
    ),
    pytest.param(
        "dfe-receiver", "mesh", "aws",
        {"exposure": {"networkPolicy": {"enabled": False}}},
        {"absent": [("NetworkPolicy", "dfe-receiver-ingest")], "present": [("GRPCRoute", "dfe-receiver-mesh")]},
        id="receiver-policy-off",
    ),
    pytest.param(
        "dfe-transform-vrl", "mesh", "local",
        {"component": "transform-vrl-acme", "imageComponent": "transform-vrl"},
        {"present": [("Service", "dfe-transform-vrl-acme-mesh"), ("GRPCRoute", "dfe-transform-vrl-acme-mesh")]},
        id="transform-instance",
    ),
    pytest.param(
        "dfe-transform-vector", "mesh", "aws",
        {"component": "transform-vector-acme", "fullnameOverride": "dfe-transform-vector-acme"},
        {"present": [("BackendTrafficPolicy", "dfe-transform-vector-acme-mesh")]},
        id="transform-instance-with-fullname-override",
    ),
    pytest.param(
        "dfe-loader", "mesh", "local",
        {"mesh": {"routePolicy": {"enabled": False}}},
        {"present": [("GRPCRoute", "dfe-loader-mesh")], "absent": [("BackendTrafficPolicy", "dfe-loader-mesh")]},
        id="loader-route-policy-off",
    ),
    pytest.param(
        "culvert", "scale", "aws",
        {
            "peers": {"classes": {"admin": {"enabled": True, "adminCIDRs": ["192.0.2.10/32"]}}},
            "networkPolicy": {"extraEgress": [{"to": [{"ipBlock": {"cidr": "198.51.100.0/24"}}]}]},
        },
        {"present": [("NetworkPolicy", "dfe-culvert")]},
        id="culvert-admin-and-extra-egress",
    ),
    pytest.param(
        "culvert", "single", "aws",
        {"networkPolicy": {"enabled": False}},
        {"absent": [("NetworkPolicy", "dfe-culvert")]},
        id="culvert-policy-off",
    ),
]


@pytest.mark.parametrize(("service", "profile", "cloud", "overlay", "expect"), SCENARIOS)
def test_dfe_extras_follows_2_2_0_through_each_conditional(
    service: str, profile: str, cloud: str, overlay: dict, expect: dict
) -> None:
    layered = {**(base_overlay(service, cloud) or {}), **overlay}
    pair = render_pair(service, profile, cloud, overlay=layered)
    assert gate(pair) == []
    for key in expect.get("present", []):
        assert tuple(key) in pair.extras, f"the scenario renders no {key}"
    for key in expect.get("absent", []):
        assert tuple(key) not in pair.extras, f"the scenario still renders {key}"
    env = pair.extras.get(("ConfigMap", f"{chart_name(service)}-env"), {}).get("data") or {}
    for name, value in expect.get("env", {}).items():
        assert env.get(name) == value, f"{name} is {env.get(name)!r}"
    for name in expect.get("unset", []):
        assert name not in env, f"{name} is set where 2.2.0 omits it"


def test_the_e2e_posture_reaches_the_engine_env() -> None:
    """The appset's e2eServer parameter beats every values file, so the posture is a cluster fact."""
    pair = render_pair("dfe-engine", "slim", "local", annotations={"dfe.hyperi.io/e2e_server": "true"})
    assert gate(pair) == []
    env = pair.extras[("ConfigMap", "dfe-engine-env")]["data"]
    assert (env["DFE_ENV"], env["DFE_E2E_SERVER"]) == ("test", "true")


def test_a_managed_broker_on_a_bus_profile_renders() -> None:
    """kafka.mode moved by the deployer to another bus keeps the profile's transport."""
    infra = {"kafka": {"mode": "external", "bootstrapServers": "broker.example.com:9092", "messageMaxBytes": 2000000}}
    pair = render_pair("dfe-engine", "scale", "aws", infra=infra)
    assert gate(pair) == []
    env = pair.extras[("ConfigMap", "dfe-engine-env")]["data"]
    assert env["DFE_KAFKA_TOPIC_MAX_MESSAGE_BYTES"] == "2000000"
    assert env["DFE_TRANSPORT_DEFAULT"] == "bus"


# ---------------------------------------------------------------- expected fails


def test_a_renamed_object_fails_the_gate() -> None:
    """A name dfe-extras moves is a prune and a recreate, so the gate names both halves."""
    renamed = {"fullnameOverride": "dfe-transform-vrl-renamed"}
    pair = render_pair("dfe-transform-vrl", "mesh", "aws", overlay=renamed)
    problems = gate(pair)
    assert any("dfe-transform-vrl-mesh" in p and "does not" in p for p in problems), problems
    assert any("dfe-transform-vrl-renamed-mesh" in p for p in problems), problems


def test_an_object_renamed_after_the_render_fails_the_gate() -> None:
    pair = render_pair("dfe-engine", "single", "local")
    doc = pair.extras.pop(("Deployment", "dfe-hunt-runner"))
    doc = copy.deepcopy(doc)
    doc["metadata"]["name"] = "dfe-hunt-runner-v2"
    pair.extras[("Deployment", "dfe-hunt-runner-v2")] = doc
    problems = gate(pair)
    assert any("('Deployment', 'dfe-hunt-runner')" in p for p in problems), problems
    assert any("('Deployment', 'dfe-hunt-runner-v2')" in p for p in problems), problems


def test_an_object_the_thin_chart_renders_too_fails_the_gate() -> None:
    pair = render_pair("dfe-ui", "single", "local")
    pair.extras[("ServiceAccount", "dfe-ui")] = {"kind": "ServiceAccount", "metadata": {"name": "dfe-ui"}}
    assert any("names the thin chart renders" in p for p in gate(pair))


@pytest.mark.parametrize(
    ("profile", "mode", "runs", "built"),
    [
        ("slim", "cluster", "bus", "direct"),
        ("mesh", "external", "bus", "direct"),
        ("scale", "disabled", "direct", "bus"),
        ("single", "disabled", "direct", "bus"),
    ],
)
def test_a_kafka_mode_the_profile_was_not_built_for_fails(
    profile: str, mode: str, runs: str, built: str
) -> None:
    for service in ("dfe-receiver", "dfe-fetcher"):
        with pytest.raises(RenderError) as refused:
            render_pair(service, profile, "local", infra={"kafka": {"mode": mode}}, old=False)
        message = str(refused.value)
        assert f'kafka.mode "{mode}" runs the {runs} transport' in message, message
        assert f"the {profile} profile is built for {built}" in message, message


def test_culvert_policy_switched_on_in_a_layer_fails() -> None:
    with pytest.raises(RenderError, match="same name"):
        render_pair("culvert", "scale", "aws", overlay={"networkPolicy": {"enabled": True}}, old=False)


@pytest.mark.parametrize(("old", "extras"), [(True, False), (False, True)], ids=["2.2.0", "dfe-extras"])
def test_internal_mode_refuses_an_exposed_grpc_listener(old: bool, extras: bool) -> None:
    """The refusal moves with the receiver's DFE-only objects, so both charts carry it."""
    listeners = {
        "listeners": [
            {"name": "http", "port": 8080, "protocol": "TCP", "exposed": True},
            {"name": "grpc", "port": 8443, "protocol": "TCP", "exposed": True},
        ]
    }
    with pytest.raises(RenderError, match='listener "grpc"') as refused:
        render_pair("dfe-receiver", "slim", "local", overlay=listeners, old=old, extras=extras)
    assert 'exposure.mode is "internal"' in str(refused.value)


@pytest.mark.parametrize(("old", "extras"), [(True, False), (False, True)], ids=["2.2.0", "dfe-extras"])
def test_a_culvert_on_the_receivers_listeners_is_refused(old: bool, extras: bool) -> None:
    """local.yaml's list is the receiver's, so a culvert that does not restate its own has no tunnel."""
    with pytest.raises(RenderError, match="neither wireguard nor openvpn-udp"):
        render_pair("culvert", "slim", "local", old=old, extras=extras)


def test_a_fullname_override_outside_the_project_fails() -> None:
    with pytest.raises(RenderError, match="does not start with"):
        render_pair("dfe-loader", "mesh", "local", overlay={"fullnameOverride": "loader"}, old=False)


# ------------------------------------------------------------ the chart itself


@pytest.mark.parametrize("name", ["", *sorted({chart_name(s) for s in COMPONENTS})])
def test_helm_lint_strict_is_clean(name: str) -> None:
    out = subprocess.run(
        [helm(), "lint", "--strict", str(EXTRAS), "--set", f"chartName={name}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.parametrize("name", ["", "dfe-fetcher", "a-component-with-no-extras"])
def test_a_component_with_no_dfe_only_objects_renders_nothing(name: str) -> None:
    out = subprocess.run(
        [helm(), "template", "x", str(EXTRAS), "--set", f"chartName={name}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert out.returncode == 0, out.stderr
    assert _documents(out.stdout) == []


def test_each_profile_declares_the_transport_its_kafka_mode_runs() -> None:
    """extras.transports restates profile-<p>.yaml's kafka.mode as bus or direct."""
    extras = yaml.safe_load((EXTRAS / "values.yaml").read_text(encoding="utf-8"))["extras"]
    for profile in PROFILES:
        values = yaml.safe_load((VALUES / f"profile-{profile}.yaml").read_text(encoding="utf-8"))
        runs = "direct" if values["kafka"]["mode"] == "disabled" else "bus"
        assert extras["transports"][profile] == runs, profile


def _composition() -> ModuleType:
    """scripts/composition.py, which imports its siblings from scripts/."""
    scripts = str(REPO_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("composition")


def test_the_app_catalogue_is_the_manifest_at_the_repo_root() -> None:
    copied = (EXTRAS / "files" / "apps.yaml").read_text(encoding="utf-8")
    assert copied == _composition().catalogue_copy()


# --------------------------------------------------- against a real thin chart


@pytest.mark.parametrize("service", ["dfe-ui", "hyperdx"])
@pytest.mark.parametrize("profile", PROFILES)
def test_no_object_is_rendered_by_both_charts(service: str, profile: str) -> None:
    """The thin chart assembled from the committed contract, beside dfe-extras."""
    thin = render_app(service, profile, "aws", "new")
    extras = render_pair(service, profile, "aws", old=False).extras
    both = {(d.get("kind"), d["metadata"]["name"]) for d in thin} & set(extras)
    assert both == set()
