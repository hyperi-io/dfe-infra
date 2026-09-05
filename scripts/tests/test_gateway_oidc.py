#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_gateway_oidc.py
#  Purpose:      Prove the gateway OIDC path renders the shape a private-CA IdP
#                needs, and that a pod can be permitted to reach its own gateway.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for the edge OIDC path and the gateway egress policy.

Both failures behind these checks are silent. A SecurityPolicy with only
`provider.issuer` renders fine and is REJECTED at admission against an IdP on a
private CA, because Envoy Gateway's control plane does the discovery fetch with
no way to be given a CA. And an egress policy that names the Service port 443
instead of the proxy's container port renders fine and drops every packet, because
NetworkPolicy is evaluated after DNAT (dfe-infra #222, #223).

    python3 scripts/tests/test_gateway_oidc.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
GATEWAY = CHARTS / "envoy-gateway-config"
POLICIES = CHARTS / "network-policies"
COMMON = REPO_ROOT / "argocd" / "values" / "common.yaml"

# A provider on the deployment's own CA: the shape the chart has to be able to
# render, and the one the issuer-only policy cannot express.
PRIVATE_CA_PROVIDER = [
    "oidc.enabled=true",
    "oidc.providers[0].name=dex",
    "oidc.providers[0].issuerUrl=https://auth.example.com",
    "oidc.providers[0].clientId=dfe",
    "oidc.providers[0].authorizationEndpoint=https://auth.example.com/auth",
    "oidc.providers[0].tokenEndpoint=https://auth.example.com/token",
    "oidc.providers[0].backendRefs[0].name=dex",
    "oidc.providers[0].backendRefs[0].namespace=dfe-local",
    "oidc.providers[0].backendRefs[0].port=5556",
    "oidc.providers[0].backendSettings.timeout.tcp.connectTimeout=10s",
]

_failures = 0


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


def render(chart: Path, *sets: str, values: Path | None = COMMON) -> list[dict]:
    cmd = ["helm", "template", chart.name, str(chart)]
    if values is not None:
        cmd += ["-f", str(values)]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart.name} {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def login_policies(docs: list[dict]) -> list[dict]:
    """The per-provider interactive-login policies, not the infra group checks."""
    return [
        d for d in of_kind(docs, "SecurityPolicy")
        if d["metadata"]["name"].startswith("dfe-oidc-")
        and not d["metadata"]["name"].endswith("-admin")
    ]


def test_issuer_only_stays_issuer_only() -> None:
    """The default shape must not sprout empty endpoint keys."""
    docs = render(
        GATEWAY,
        "oidc.enabled=true",
        "oidc.providers[0].name=okta",
        "oidc.providers[0].issuerUrl=https://okta.example.com",
        "oidc.providers[0].clientId=dfe",
    )
    policies = login_policies(docs)
    expect("a public IdP renders one policy per target route", len(policies) >= 1,
           f"got {len(policies)}")
    provider = policies[0]["spec"]["oidc"]["provider"]
    expect("a public IdP renders the issuer alone", set(provider) == {"issuer"},
           f"got {sorted(provider)}")


def test_private_ca_provider_skips_discovery() -> None:
    docs = render(GATEWAY, *PRIVATE_CA_PROVIDER)
    policies = login_policies(docs)
    expect("a private-CA provider renders a policy", len(policies) >= 1, f"got {len(policies)}")
    provider = policies[0]["spec"]["oidc"]["provider"]
    expect("both endpoints are named, so discovery is skipped",
           provider.get("authorizationEndpoint") == "https://auth.example.com/auth"
           and provider.get("tokenEndpoint") == "https://auth.example.com/token",
           f"got {provider}")
    expect("the issuer is still declared, because it must equal the token iss",
           provider.get("issuer") == "https://auth.example.com", f"got {provider}")


def test_private_ca_provider_keeps_the_token_exchange_in_cluster() -> None:
    docs = render(GATEWAY, *PRIVATE_CA_PROVIDER)
    provider = login_policies(docs)[0]["spec"]["oidc"]["provider"]
    refs = provider.get("backendRefs", [])
    expect("the token exchange targets the IdP Service", len(refs) == 1, f"got {refs}")
    if refs:
        expect("the backend ref is a Service with a port",
               refs[0]["kind"] == "Service" and refs[0]["port"] == 5556, f"got {refs[0]}")
        expect("the backend ref carries its namespace",
               refs[0]["namespace"] == "dfe-local", f"got {refs[0]}")
    expect("backendSettings reaches the rendered policy",
           provider.get("backendSettings", {}).get("timeout", {}).get("tcp", {}).get(
               "connectTimeout") == "10s",
           f"got {provider.get('backendSettings')}")


def test_no_dead_oidc_switch_survives() -> None:
    """auth.oidcEnabled was declared by the engine chart and read by nothing."""
    hits = [
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / "helm").rglob("*.yaml")
        if "oidcEnabled:" in p.read_text(encoding="utf-8", errors="replace")
    ]
    expect("no chart declares oidcEnabled", hits == [], f"got {hits}")


def test_gateway_egress_names_the_listener_port() -> None:
    docs = render(POLICIES)
    policies = [
        d for d in of_kind(docs, "NetworkPolicy")
        if d["metadata"]["name"] == "allow-gateway-egress"
    ]
    expect("every DFE namespace gets a gateway egress policy", len(policies) == 1,
           f"got {len(policies)}")
    rule = policies[0]["spec"]["egress"][0]
    ports = {p["port"] for p in rule["ports"]}
    expect("the policy names the proxy container ports, not the Service ports",
           ports == {10443, 10080}, f"got {sorted(ports)}")
    expect("443 alone is not what is permitted", 443 not in ports, f"got {sorted(ports)}")


def test_gateway_egress_selects_the_proxy_pods() -> None:
    docs = render(POLICIES)
    policies = [
        d for d in of_kind(docs, "NetworkPolicy")
        if d["metadata"]["name"] == "allow-gateway-egress"
    ]
    to = policies[0]["spec"]["egress"][0]["to"][0]
    expect("the destination is the gateway namespace",
           to["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
           == "envoy-gateway-system", f"got {to}")
    expect("the destination is the managed proxy pods, not the control plane",
           to["podSelector"]["matchLabels"] == {
               "app.kubernetes.io/managed-by": "envoy-gateway",
               "app.kubernetes.io/component": "proxy",
           }, f"got {to.get('podSelector')}")


def test_gateway_egress_can_be_turned_off() -> None:
    docs = render(POLICIES, "gatewayEgress.enabled=false")
    policies = [
        d for d in of_kind(docs, "NetworkPolicy")
        if d["metadata"]["name"] == "allow-gateway-egress"
    ]
    expect("gatewayEgress.enabled=false renders nothing", policies == [],
           f"got {len(policies)}")


def test_external_dns_is_gated_on_a_declared_provider() -> None:
    """external-dns defaults its provider to aws, so an ungated install crash-loops."""
    appset = yaml.safe_load(
        (REPO_ROOT / "argocd" / "appsets" / "layer1-addons.yaml").read_text(encoding="utf-8")
    )
    gated = []
    for gen in appset["spec"]["generators"]:
        charts = {e["chart"] for e in gen["matrix"]["generators"][1]["list"]["elements"]}
        selector = gen["matrix"]["generators"][0]["clusters"]["selector"]
        if "external-dns" in charts:
            gated.append(selector.get("matchExpressions", []))
    expect("external-dns is in exactly one generator", len(gated) == 1, f"got {len(gated)}")
    if gated:
        keys = {(e["key"], e["operator"]) for e in gated[0]}
        expect(
            "an absent dns-provider label excludes external-dns",
            ("dfe.hyperi.io/dns-provider", "Exists") in keys
            and ("dfe.hyperi.io/dns-provider", "NotIn") in keys,
            f"got {gated[0]}",
        )
        none_values = [e["values"] for e in gated[0] if e["operator"] == "NotIn"]
        expect("the none provider is excluded too", none_values and "none" in none_values[0],
               f"got {none_values}")


def main() -> int:
    test_issuer_only_stays_issuer_only()
    test_private_ca_provider_skips_discovery()
    test_private_ca_provider_keeps_the_token_exchange_in_cluster()
    test_no_dead_oidc_switch_survives()
    test_gateway_egress_names_the_listener_port()
    test_gateway_egress_selects_the_proxy_pods()
    test_gateway_egress_can_be_turned_off()
    test_external_dns_is_gated_on_a_declared_provider()
    print(f"\n{_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
