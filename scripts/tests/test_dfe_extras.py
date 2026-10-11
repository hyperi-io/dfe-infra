#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_extras.py
#  Purpose:      Prove helm/charts/dfe-extras renders, per component and per
#                profile, the DFE-only objects each golden holds, none named as
#                the thin chart names its own, and refuses what no app can run.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""helm/charts/dfe-extras, the DFE-only objects beside every thin chart.

    python3 -m pytest scripts/tests/test_dfe_extras.py -q
    DFE_WEAVE_HELM=<helm 4.2.4> python3 -m pytest scripts/tests/test_dfe_extras.py -q

Argo adopts a live object by (kind, name), so a renamed object is pruned and
recreated: a generated Secret is minted again, a PVC comes back empty. Each
component is rendered as the dfe-extras source of the Application
argocd/appsets/layer2-apps.yaml generates for it (_weave.render_extras), which
needs helm and nothing from a registry. The checks:

- dfe-extras renders exactly the dfe-extras objects of each cell's golden
  (fixtures/thin-renders), object for object and leaf for leaf, so a change to
  this chart fails here before the goldens are rewritten
- no dfe-extras object carries a name the thin chart renders (THIN_SUFFIXES),
  so nothing is rendered twice
- each conditional the profile matrix does not reach renders the object or env
  value it exists to produce
- each value the components cannot run with is refused by name

The thin-chart cases need the scalo-service library (_weave.library).
"""

import copy
import importlib
import re
import subprocess
import sys
from types import ModuleType

import pytest
import yaml

from _gate import APPSETS, CLOUDS, INSTANCE, PROFILES, golden, object_id, render_differences
from _weave import REPO_ROOT, chart_name, helm, render_app, render_extras, weave

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

# The components whose env dfe-extras carries in a <fullname>-env ConfigMap.
ENV_CONFIGMAP = ("dfe-engine", "dfe-hyperdx", "dfe-receiver", "dfe-loader", "culvert")

# Each component's object name prefix where it is not its deploy.service.
FULLNAMES = {"culvert": "dfe-culvert", "hyperdx": "dfe-hyperdx"}

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

# What helm refuses a render with, as dfe-weave raises it.
RenderError = weave().WeaveError

type ObjectId = tuple[str, str]


def fullname(service: str) -> str:
    """The name a component's thin chart gives its own objects."""
    return FULLNAMES.get(service, service)


def thin_ids(name: str) -> set[ObjectId]:
    """Every (kind, name) the thin chart of a component named ``name`` can render."""
    found = set()
    for kind, suffixes in THIN_SUFFIXES.items():
        found |= {(kind, name + suffix) for suffix in suffixes}
    return found


def base_overlay(service: str, cloud: str) -> dict | None:
    """The instance values a component needs before it renders at all."""
    return CULVERT_LISTENERS if (service, cloud) == ("culvert", "local") else None


# ----------------------------------------------------------------- the renders


def render(
    service: str,
    profile: str,
    cloud: str,
    *,
    overlay: dict | None = None,
    infra: dict | None = None,
    annotations: dict[str, str] | None = None,
    **options: object,
) -> list[dict]:
    """dfe-extras for one component, with the deployer's instance values and infra/common.yaml.

    Raises:
        RenderError: helm refused the render.
    """
    return render_extras(
        service,
        profile,
        cloud,
        overlay=overlay,
        infra=infra,
        annotations=annotations or {},
        **options,
    )


def keyed(docs: list[dict]) -> dict[ObjectId, dict]:
    return {(d.get("kind"), d["metadata"]["name"]): d for d in docs}


def env_data(docs: list[dict]) -> dict[str, str]:
    """The data of the one <fullname>-env ConfigMap a render holds; empty where it holds none."""
    found = [d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-env")]
    assert len(found) <= 1, [d["metadata"]["name"] for d in found]
    return (found[0].get("data") or {}) if found else {}


# --------------------------------------------------------------------- the checks


def against_golden(extras: list[dict], whole: list[dict], thin: set[ObjectId]) -> list[str]:
    """How a dfe-extras render departs from the dfe-extras part of a golden; empty is a pass.

    The golden's dfe-extras part is every object the thin chart cannot name.
    """
    part = [d for d in whole if (d.get("kind"), d["metadata"]["name"]) not in thin]
    return render_differences(part, extras)


def collisions(extras: list[dict], service: str) -> list[ObjectId]:
    """Each dfe-extras object named as the thin chart names its own; empty is a pass."""
    guarded = GUARDED_KINDS.get(service, set())
    shared = set(keyed(extras)) & thin_ids(fullname(service))
    return sorted(k for k in shared if k[0] not in guarded)


# ------------------------------------------------------- per component, per profile


@pytest.mark.parametrize("cloud", CLOUDS)
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("service", COMPONENTS)
def test_dfe_extras_renders_its_part_of_each_golden(service: str, profile: str, cloud: str) -> None:
    """Rendered with the instance values and appset the golden's cell takes."""
    options = {"appset": APPSETS[service]} if service in APPSETS else {}
    overlay = INSTANCE.get(service, {}).get(cloud)
    extras = render(service, profile, cloud, overlay=overlay, **options)
    whole = golden(service, profile, cloud)
    guarded = {k for k in keyed(whole) if k[0] in GUARDED_KINDS.get(service, set())}
    thin = thin_ids(fullname(service)) - guarded
    assert against_golden(extras, whole, thin) == []
    assert collisions(extras, service) == []


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
            "unset": ["DFE_ENGINE_JWKS_URL"],
        },
        id="hyperdx-header-dev-and-tls",
    ),
    # Public mode's ingest policy is the thin chart's, from the ports its load balancers serve.
    pytest.param(
        "dfe-receiver", "single", "local",
        {"exposure": {"mode": "public"}},
        {"absent": [("NetworkPolicy", "dfe-receiver-ingest")]},
        id="receiver-public",
    ),
    pytest.param(
        "dfe-receiver", "single", "local",
        {"exposure": {"mode": "vpn"}},
        {"present": [("NetworkPolicy", "dfe-receiver-ingest")]},
        id="receiver-vpn",
    ),
    pytest.param(
        "dfe-receiver", "scale", "aws",
        {"kafka": {"securityProtocol": "SASL_SSL"}},
        {
            "env": {
                "DFE_RECEIVER_KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
                "DFE_RECEIVER_DLQ_TOPIC": "dfe_receiver_dlq",
            },
            "unset": ["DFE_RECEIVER_DLQ_ENABLED", "DFE_RECEIVER_BIND_ADDRESS"],
        },
        id="receiver-tls-broker",
    ),
    pytest.param(
        "dfe-receiver", "mesh", "local",
        {},
        {"env": {"DFE_RECEIVER_DLQ_ENABLED": "false"}, "unset": ["DFE_RECEIVER_KAFKA_BROKERS"]},
        id="receiver-direct",
    ),
    pytest.param(
        "dfe-loader", "single", "aws",
        {"clickhouse": {"user": "loader", "tls": {"enabled": False}}},
        {
            "env": {
                "DFE_LOADER_CLICKHOUSE_HOSTS": "dfe-clickhouse.clickhouse.svc.cluster.local:8123",
                "DFE_LOADER_CLICKHOUSE_USERNAME": "loader",
            },
            "unset": ["SSL_CERT_FILE", "DFE_LOADER_TRANSPORT"],
        },
        id="loader-plaintext-clickhouse",
    ),
    pytest.param(
        "dfe-loader", "slim", "local",
        {"clickhouse": {"tls": {"ca": {"secretName": "", "configMapName": "example-ca-bundle"}}}},
        {
            "env": {
                "DFE_LOADER_TRANSPORT": "grpc",
                "DFE_LOADER_CLICKHOUSE_HOSTS": "dfe-clickhouse.clickhouse.svc.cluster.local:8443",
                "SSL_CERT_FILE": "/etc/dfe-trust/ca-bundle.pem",
            },
            "unset": ["DFE_LOADER_KAFKA_BROKERS", "DFE_LOADER_DLQ_TOPIC"],
        },
        id="loader-ca-from-configmap",
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
    pytest.param(
        "culvert", "single", "aws",
        {"pki": {"mode": "external", "existingSecret": "culvert-pki-example"}, "vpn": {"dns": ["192.0.2.53"]}},
        {
            "env": {
                "CULVERT_PKI_MODE": "external",
                "CULVERT_SECRETS_PROVIDER": "file",
                "CULVERT_SECRETS_CA_CERT_PATH": "/etc/vpn/pki-external/ca.crt",
                "CULVERT_DNS1": "192.0.2.53",
            },
            "unset": ["CULVERT_DNS2"],
        },
        id="culvert-external-pki",
    ),
    pytest.param(
        "culvert", "scale", "local",
        {
            "listeners": [
                *CULVERT_LISTENERS["listeners"],
                {"name": "openvpn-tcp", "port": 1194, "protocol": "TCP", "exposed": True},
                {"name": "oauth2-udp", "port": 9000, "protocol": "TCP", "exposed": True},
            ],
            "vpn": {
                "oidc": {
                    "enabled": True,
                    "issuer": "https://idp.example.com",
                    "clientId": "culvert",
                    "existingSecret": "culvert-oidc-example",
                }
            },
        },
        {
            "env": {
                "CULVERT_TCP_ENABLED": "true",
                "CULVERT_TCP_NETWORK": "100.64.1.0",
                "CULVERT_OAUTH2_ENABLED": "true",
                "CULVERT_OAUTH2_ISSUER": "https://idp.example.com",
            },
        },
        id="culvert-oidc-and-tcp",
    ),
]


@pytest.mark.parametrize(("service", "profile", "cloud", "overlay", "expect"), SCENARIOS)
def test_dfe_extras_follows_each_conditional(
    service: str, profile: str, cloud: str, overlay: dict, expect: dict
) -> None:
    layered = {**(base_overlay(service, cloud) or {}), **overlay}
    docs = render(service, profile, cloud, overlay=layered)
    rendered = keyed(docs)
    assert collisions(docs, service) == []
    for key in expect.get("present", []):
        assert tuple(key) in rendered, f"the scenario renders no {key}"
    for key in expect.get("absent", []):
        assert tuple(key) not in rendered, f"the scenario still renders {key}"
    env = env_data(docs)
    for name, value in expect.get("env", {}).items():
        assert env.get(name) == value, f"{name} is {env.get(name)!r}"
    for name in expect.get("unset", []):
        assert name not in env, f"{name} is set where the component reads it unset"


def test_the_e2e_posture_reaches_the_engine_env() -> None:
    """The appset's e2eServer parameter beats every values file, so the posture is a cluster fact."""
    docs = render("dfe-engine", "slim", "local", annotations={"dfe.hyperi.io/e2e_server": "true"})
    env = keyed(docs)[("ConfigMap", "dfe-engine-env")]["data"]
    assert (env["DFE_ENV"], env["DFE_E2E_SERVER"]) == ("test", "true")


def test_a_managed_broker_on_a_bus_profile_renders() -> None:
    """kafka.mode moved by the deployer to another bus keeps the profile's transport."""
    broker = {"mode": "external", "bootstrapServers": "broker.example.com:9092"}
    infra = {"kafka": {**broker, "messageMaxBytes": 2000000}}
    docs = render("dfe-engine", "scale", "aws", infra=infra)
    env = keyed(docs)[("ConfigMap", "dfe-engine-env")]["data"]
    assert env["DFE_KAFKA_TOPIC_MAX_MESSAGE_BYTES"] == "2000000"
    assert env["DFE_TRANSPORT_DEFAULT"] == "bus"


# ---------------------------------------------------------------- expected fails


def test_a_renamed_object_departs_from_its_golden() -> None:
    """A name dfe-extras moves is a prune and a recreate, so both halves are named."""
    overlay = {"fullnameOverride": "dfe-transform-vrl-renamed"}
    renamed = render("dfe-transform-vrl", "mesh", "aws", overlay=overlay)
    whole = golden("dfe-transform-vrl", "mesh", "aws")
    problems = against_golden(renamed, whole, thin_ids("dfe-transform-vrl"))
    assert "only left: GRPCRoute/dfe-transform-vrl-mesh" in problems, problems
    assert "only right: GRPCRoute/dfe-transform-vrl-renamed-mesh" in problems, problems


def test_an_object_renamed_after_the_render_departs_from_its_golden() -> None:
    docs = render("dfe-engine", "single", "local")
    runner = next(d for d in docs if object_id(d) == "Deployment/dfe-hunt-runner")
    moved = copy.deepcopy(runner)
    moved["metadata"]["name"] = "dfe-hunt-runner-v2"
    docs = [d for d in docs if d is not runner] + [moved]
    problems = against_golden(docs, golden("dfe-engine", "single", "local"), thin_ids("dfe-engine"))
    assert "only left: Deployment/dfe-hunt-runner" in problems, problems
    assert "only right: Deployment/dfe-hunt-runner-v2" in problems, problems


def test_an_object_the_thin_chart_renders_too_is_caught() -> None:
    docs = render("dfe-ui", "single", "local")
    docs.append({"kind": "ServiceAccount", "metadata": {"name": "dfe-ui"}})
    assert collisions(docs, "dfe-ui") == [("ServiceAccount", "dfe-ui")]


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
            render(service, profile, "local", infra={"kafka": {"mode": mode}})
        message = str(refused.value)
        assert f'kafka.mode "{mode}" runs the {runs} transport' in message, message
        assert f"the {profile} profile is built for {built}" in message, message


def test_culvert_policy_switched_on_in_a_layer_fails() -> None:
    with pytest.raises(RenderError, match="same name"):
        render("culvert", "scale", "aws", overlay={"networkPolicy": {"enabled": True}})


def test_internal_mode_refuses_an_exposed_grpc_listener() -> None:
    listeners = {
        "listeners": [
            {"name": "http", "port": 8080, "protocol": "TCP", "exposed": True},
            {"name": "grpc", "port": 8443, "protocol": "TCP", "exposed": True},
        ]
    }
    with pytest.raises(RenderError, match='listener "grpc"') as refused:
        render("dfe-receiver", "slim", "local", overlay=listeners)
    assert 'exposure.mode is "internal"' in str(refused.value)


def test_a_culvert_on_the_receivers_listeners_is_refused() -> None:
    """local.yaml's list is the receiver's, so a culvert that does not restate its own has no tunnel."""
    with pytest.raises(RenderError, match="neither wireguard nor openvpn-udp"):
        render("culvert", "slim", "local")


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        ({"replicas": 2}, "replicas is 2"),
        ({"pki": {"mode": "external"}}, "pki.mode is external with no pki.existingSecret"),
        ({"vpn": {"oidc": {"enabled": True, "existingSecret": "x"}}}, "no listener named oauth2-udp"),
        ({"oidc": {"issuer": "https://idp.example.com"}}, "this chart no longer reads it"),
        (
            {"peers": {"classes": {"admin": {"enabled": True}, "appliance": {"isolation": False}}}},
            "opening every appliance to every other one",
        ),
        (
            {"peers": {"classes": {"admin": {"enabled": True, "adminCIDRs": ["0.0.0.0/0"]}}}},
            "carries 0.0.0.0/0",
        ),
        (
            {"peers": {"classes": {"admin": {"enabled": True, "adminCIDRs": ["100.64.9.0/24"]}}}},
            "names a peer source",
        ),
        (
            {"exposure": {"serviceType": "NodePort", "loadBalancerSourceRanges": ["198.51.100.0/24"]}},
            "only filters on that field for a LoadBalancer",
        ),
    ],
)
def test_culverts_refusals(overlay: dict, message: str) -> None:
    """The thin chart cannot refuse a value, so dfe-extras makes each refusal culvert needs."""
    with pytest.raises(RenderError, match=re.escape(message)):
        render("culvert", "scale", "aws", overlay=overlay)


@pytest.mark.parametrize(
    ("overlay", "protocol"),
    [
        ({"config": {"protocol": "wireguard"}}, "wireguard"),
        ({"config": {"protocol": "both"}}, "both"),
        ({"config": {"protocol": "openvpn"}, "configOverrides": {"protocol": "both"}}, "both"),
    ],
    ids=["wireguard", "both", "override"],
)
def test_a_culvert_protocol_in_its_profile_is_refused(overlay: dict, protocol: str) -> None:
    """The contract would open a second wireguard port beside apps/culvert/values.yaml's."""
    with pytest.raises(RenderError, match=re.escape(f'config.protocol is "{protocol}"')) as refused:
        render("culvert", "scale", "aws", overlay=overlay)
    assert "through CULVERT_PROTOCOL" in str(refused.value)


def test_an_openvpn_profile_protocol_renders() -> None:
    """The control: a protocol that opens no gated port is left alone."""
    assert render("culvert", "scale", "aws", overlay={"config": {"protocol": "openvpn"}})


@pytest.mark.parametrize(
    ("clickhouse", "message"),
    [
        ({"tls": {"verify": False}}, "dfe-loader always verifies"),
        ({"tls": {"ca": {"configMapName": "example-ca-bundle"}}}, "not both"),
        ({"tls": {"port": 8123}}, "turns TLS on only for 8443, 9440"),
    ],
    ids=["no-verify", "two-ca-sources", "plaintext-port"],
)
def test_a_clickhouse_tls_setup_the_loader_cannot_dial_is_refused(
    clickhouse: dict, message: str
) -> None:
    """The loader's refusals move with dfe-loader-env."""
    with pytest.raises(RenderError, match=message):
        render("dfe-loader", "scale", "aws", overlay={"clickhouse": clickhouse})


def test_a_fullname_override_outside_the_project_fails() -> None:
    with pytest.raises(RenderError, match="does not start with"):
        render("dfe-loader", "mesh", "local", overlay={"fullnameOverride": "loader"})


@pytest.mark.parametrize(
    ("infra", "path", "value"),
    [
        ({"project": "acme"}, "project", "acme"),
        ({"global": {"project": "acme"}}, "global.project", "acme"),
        ({"project": ""}, "project", ""),
        ({"global": {"project": ""}}, "global.project", ""),
    ],
    ids=["project", "global-project", "empty-project", "empty-global-project"],
)
@pytest.mark.parametrize("service", COMPONENTS)
def test_a_project_other_than_dfe_is_refused(service: str, infra: dict, path: str, value: str) -> None:
    """Every component's names and the engine's prefix assume dfe, so either key refuses a rename."""
    with pytest.raises(RenderError, match=re.escape(f'dfe-extras: {path} is "{value}"')) as refused:
        render(service, "scale", "aws", infra=infra)
    assert 'assume the project is "dfe"' in str(refused.value)


@pytest.mark.parametrize(
    "infra",
    [None, {"project": "dfe"}, {"global": {"project": "dfe"}}],
    ids=["default", "project", "global-project"],
)
def test_the_project_dfe_renders(infra: dict | None) -> None:
    """The control: the default, and dfe spelled out under either key, are left alone."""
    assert render("dfe-loader", "scale", "aws", infra=infra)


# ------------------------------------------------------------ self-monitoring telemetry

OTLP = "OTEL_EXPORTER_OTLP_ENDPOINT"


def _extras_otlp(docs: list[dict], workload: str) -> list[str]:
    """The OTLP endpoints the one dfe-extras Deployment whose name holds ``workload`` exports to."""
    (deployment,) = [
        d for d in docs if d["kind"] == "Deployment" and workload in d["metadata"]["name"]
    ]
    env = deployment["spec"]["template"]["spec"]["containers"][0].get("env") or []
    return [entry["value"] for entry in env if entry["name"] == OTLP]


@pytest.mark.parametrize("workload", ["hunt-runner", "keda-shim"])
@pytest.mark.parametrize(
    ("infra", "want"),
    [
        ({"telemetry": {"mode": "receiver"}}, r"http://dfe-receiver\.[a-z0-9-]+\.svc\.cluster\.local:4317"),
        (
            {"telemetry": {"mode": "external", "externalEndpoint": "https://otlp.example.net:4318"}},
            r"https://otlp\.example\.net:4318",
        ),
        ({"otel": {"endpoint": "collector.example.net:4317"}}, r"collector\.example\.net:4317"),
        ({"telemetry": {"mode": "prometheus"}}, None),
    ],
    ids=["receiver", "external", "override-as-written", "prometheus"],
)
def test_the_engines_extra_workloads_export_where_the_thin_charts_do(
    workload: str, infra: dict, want: str | None
) -> None:
    """apps/_common.yaml's template, with the receiver endpoint it derives when none is named."""
    got = _extras_otlp(render("dfe-engine", "scale", "local", infra=infra), workload)
    if want is None:
        assert got == []
    else:
        assert len(got) == 1, got
        assert re.fullmatch(want, got[0]), got


@pytest.mark.parametrize("mode", ["hyperdex", "HyperDX", "otlp", "none"])
@pytest.mark.parametrize("service", COMPONENTS)
def test_a_telemetry_mode_no_endpoint_resolves_from_is_refused(service: str, mode: str) -> None:
    """Every component, since each resolves its OTLP endpoint from the one template."""
    with pytest.raises(RenderError, match=re.escape(f'dfe-extras: telemetry.mode is "{mode}"')) as refused:
        render(service, "scale", "aws", overlay=base_overlay(service, "aws"), infra={"telemetry": {"mode": mode}})
    assert "hyperdx, receiver, external, prometheus" in str(refused.value)


@pytest.mark.parametrize(
    "telemetry",
    [
        None,
        {"mode": ""},
        {"mode": "hyperdx"},
        {"mode": "receiver"},
        {"mode": "external", "externalEndpoint": "otlp.example.net:4317"},
        {"mode": "prometheus"},
    ],
    ids=["unset", "empty", "hyperdx", "receiver", "external", "prometheus"],
)
def test_each_known_telemetry_mode_renders(telemetry: dict | None) -> None:
    """The control: unset and empty read as hyperdx, as apps/_common.yaml's template reads them."""
    infra = None if telemetry is None else {"telemetry": telemetry}
    assert render("dfe-engine", "scale", "aws", infra=infra)


def test_receiver_mode_with_the_receivers_otlp_listener_off_fails() -> None:
    with pytest.raises(RenderError, match=re.escape("config.otlp.enabled is not true")):
        render("dfe-receiver", "scale", "aws", infra={"telemetry": {"mode": "receiver"}})


@pytest.mark.parametrize(
    ("overlay", "telemetry"),
    [
        ({"config": {"otlp": {"enabled": True}}}, {"mode": "receiver"}),
        (None, {"mode": "receiver", "receiverEndpoint": "otlp.example.net:4317"}),
    ],
    ids=["listener-on", "named-endpoint"],
)
def test_receiver_mode_renders_once_something_listens(overlay: dict | None, telemetry: dict) -> None:
    assert render("dfe-receiver", "scale", "aws", overlay=overlay, infra={"telemetry": telemetry})


def test_culvert_in_receiver_mode_reaches_the_receivers_otlp_port_not_the_collector() -> None:
    docs = render("culvert", "scale", "aws", infra={"telemetry": {"mode": "receiver"}})
    (policy,) = [d for d in docs if d["kind"] == "NetworkPolicy"]
    otlp = [rule for rule in policy["spec"]["egress"] if {"port": 4317, "protocol": "TCP"} in rule.get("ports", [])]
    receiver = {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "dfe-receiver"}}}
    assert [rule["to"] for rule in otlp] == [[receiver]]


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
    assert [d for d in yaml.safe_load_all(out.stdout) if isinstance(d, dict)] == []


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


@pytest.mark.parametrize(
    "service", ["dfe-ui", "hyperdx", "dfe-receiver", "dfe-loader", "dfe-engine"]
)
@pytest.mark.parametrize("profile", PROFILES)
def test_no_object_is_rendered_by_both_charts(service: str, profile: str) -> None:
    """The thin chart assembled from the committed contract, beside dfe-extras."""
    thin = render_app(service, profile, "aws", extras=False)
    extras = keyed(render(service, profile, "aws"))
    both = {(d.get("kind"), d["metadata"]["name"]) for d in thin} & set(extras)
    assert both == set()
