#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_infra_edge_gate.py
#  Purpose:      Prove the edge policy on every infra route admits the
#                oidc.adminGroups claim and nothing else, and that no admin UI
#                without a login of its own renders without that policy.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for the admin gate on the infra-class routes.

An infra route carrying edgePolicy gets one SecurityPolicy from
security-policy-infra.yaml: an OIDC login, then an authorization block that
denies by default and allows only a groups claim naming one of
oidc.adminGroups. A route whose backend has no login of its own renders only
behind that policy, and exposure.infraUisExternal: false takes the whole class
off the edge.

Each case renders the chart and reads the objects, so a template edit that
widens the gate -- an Allow default, a second claim, a wildcard group, a route
published bare -- fails here rather than in a cluster.

    python3 scripts/tests/test_infra_edge_gate.py

Runs standalone or under pytest. Needs `helm` on PATH.
"""

import functools
import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATEWAY = chart_dir("envoy-gateway-config")
VALUES = REPO_ROOT / "argocd" / "values"

# The on-prem cascade on the one profile whose Kafka tier deploys the Cruise
# Control UI, with the bundled deploy repo, so every infra route can render.
CASCADE = (
    "--namespace", "envoy-gateway-system",
    "-f", str(VALUES / "common.yaml"),
    "-f", str(VALUES / "local.yaml"),
    "-f", str(VALUES / "edge-onprem.yaml"),
    "-f", str(VALUES / "profile-scale.yaml"),
    "--set", "appNamespace=dfe",
    "--set", "domain=dfe.example.com",
    "--set", "deployRepo.bundled=true",
)

# Two providers, so a rule written for the login provider alone is caught.
PROVIDERS = [
    {"name": "acme", "issuerUrl": "https://id.example.com", "clientId": "dfe"},
    {"name": "globex", "issuerUrl": "https://sso.example.org", "clientId": "dfe"},
]
OIDC = ("--set", "oidc.enabled=true", "--set-json", f"oidc.providers={json.dumps(PROVIDERS)}")

# The two shapes that leave no edge login: the chart default, and the switch
# alone with no provider, which is what argocd/values/aws.yaml sets.
NO_EDGE_LOGIN = {
    "oidc off": (),
    "oidc on with no provider": ("--set", "oidc.enabled=true"),
}

# Deliberately not the chart default, so a template that hard-codes the default
# groups fails the case that uses these.
OTHER_GROUPS = ["platform-ops", "sre-oncall"]


@functools.cache
def chart_values() -> dict:
    return yaml.safe_load((GATEWAY / "values.yaml").read_text(encoding="utf-8"))


def routes_of_class(route_class: str) -> dict[str, dict]:
    """Every route of one class in the chart's values, by values key."""
    return {
        key: route
        for key, route in chart_values()["routes"].items()
        if route.get("class", "infra") == route_class
    }


def infra_names() -> set[str]:
    return {route["routeName"] for route in routes_of_class("infra").values()}


def edge_policy_names() -> list[str]:
    """routeName of every infra route that takes the edge policy."""
    return sorted(
        route["routeName"] for route in routes_of_class("infra").values() if route.get("edgePolicy")
    )


@functools.cache
def render(*args: str) -> tuple[dict, ...]:
    """The chart under CASCADE and these args. Exits with helm's message on a refusal."""
    out = subprocess.run(
        ["helm", "template", "envoy-gateway-config", str(GATEWAY), *CASCADE, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for the gateway chart:\n{out.stderr}")
    return tuple(d for d in yaml.safe_load_all(out.stdout) if d)


def of_kind(docs: Iterable[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def http_routes(docs: Iterable[dict]) -> dict[str, dict]:
    """Each rendered HTTPRoute by metadata.name."""
    return {d["metadata"]["name"]: d for d in of_kind(docs, "HTTPRoute")}


def targets(policy: dict) -> set[str]:
    """The HTTPRoute names a SecurityPolicy attaches to."""
    return {
        ref.get("name")
        for ref in policy["spec"].get("targetRefs") or []
        if ref.get("kind") == "HTTPRoute"
    }


def policies_on(docs: Iterable[dict], route: dict) -> list[dict]:
    """Every SecurityPolicy that attaches to this HTTPRoute, in its namespace."""
    return [
        policy
        for policy in of_kind(docs, "SecurityPolicy")
        if policy["metadata"]["namespace"] == route["metadata"]["namespace"]
        and route["metadata"]["name"] in targets(policy)
    ]


def admin_policies(*args: str) -> dict[str, dict]:
    """The single policy on each edge-policy route, by route name, with the edge login on.

    A route with no policy, or with more than one, is left out; the case that
    counts them names it, and every other case checks that nothing was left out.
    """
    docs = render(*OIDC, *args)
    served = http_routes(docs)
    found = {}
    for name in edge_policy_names():
        on_route = policies_on(docs, served[name]) if name in served else []
        if len(on_route) == 1:
            found[name] = on_route[0]
    return found


def expect_every_route_gated(policies: dict[str, dict]) -> None:
    missing = sorted(set(edge_policy_names()) - set(policies))
    expect("every edge-policy route yields exactly one policy", not missing, f"missing {missing}")


def expect_admin_groups_only(route: str, policy: dict, groups: list[str]) -> None:
    """Each rule is an Allow whose one principal is the groups claim naming `groups`."""
    spec = policy["spec"]
    rules = (spec.get("authorization") or {}).get("rules") or []
    issuers = sorted(p["name"] for p in (spec.get("jwt") or {}).get("providers") or [])
    principals = [rule.get("principal") or {} for rule in rules]
    by_provider = sorted(p.get("jwt", {}).get("provider", "") for p in principals)
    expect(
        f"{route}: one Allow rule per configured provider",
        by_provider == sorted(p["name"] for p in PROVIDERS),
        f"got {by_provider}",
    )
    expect(f"{route}: every rule names a JWT issuer of this policy",
           set(by_provider) <= set(issuers), f"rules {by_provider}, issuers {issuers}")
    for rule in rules:
        principal = rule.get("principal") or {}
        provider = principal.get("jwt", {}).get("provider", "")
        wanted = {
            "jwt": {
                "provider": provider,
                "claims": [{"name": "groups", "valueType": "StringArray", "values": groups}],
            }
        }
        expect(f"{route}: the {provider} rule allows", rule.get("action") == "Allow",
               f"got {rule.get('action')!r}")
        expect(
            f"{route}: the {provider} rule's only principal is the groups claim",
            principal == wanted,
            f"got {principal}",
        )
        values = [
            value
            for claim in principal.get("jwt", {}).get("claims") or []
            for value in claim.get("values") or []
        ]
        blank = [v for v in values if not isinstance(v, str) or not v.strip() or "*" in v]
        expect(f"{route}: the {provider} rule names at least one group", bool(values), "none")
        expect(f"{route}: the {provider} rule names no blank or wildcard group", not blank,
               f"got {blank}")


# --- the policy on each gated route -------------------------------------------
def test_every_edge_policy_route_renders_under_the_edge_login() -> None:
    """Every later case reads these routes, so a missing one would pass by having none."""
    served = http_routes(render(*OIDC))
    expect("the chart declares edge-policy routes", bool(edge_policy_names()), "none")
    for name in edge_policy_names():
        expect(f"{name} renders under the edge login", name in served, f"got {sorted(served)}")


def test_each_gated_route_has_exactly_one_security_policy() -> None:
    """Two policies naming one HTTPRoute conflict, and only the oldest applies."""
    docs = render(*OIDC)
    served = http_routes(docs)
    for name in edge_policy_names():
        if name not in served:
            continue
        found = sorted(p["metadata"]["name"] for p in policies_on(docs, served[name]))
        expect(f"{name} is targeted by exactly one SecurityPolicy", len(found) == 1, f"got {found}")


def test_every_gated_route_denies_by_default() -> None:
    """Deny is what refuses a token whose groups claim is absent or names no admin group."""
    policies = admin_policies()
    expect_every_route_gated(policies)
    for name, policy in sorted(policies.items()):
        action = (policy["spec"].get("authorization") or {}).get("defaultAction")
        expect(f"{name}: defaultAction is Deny", action == "Deny", f"got {action!r}")


def test_every_rule_admits_the_admin_groups_claim_and_nothing_else() -> None:
    groups = chart_values()["oidc"]["adminGroups"]
    expect("the chart names admin groups", bool(groups), f"got {groups!r}")
    policies = admin_policies()
    expect_every_route_gated(policies)
    for name, policy in sorted(policies.items()):
        expect_admin_groups_only(name, policy, groups)


def test_a_changed_admin_groups_reaches_every_rule() -> None:
    policies = admin_policies("--set-json", f"oidc.adminGroups={json.dumps(OTHER_GROUPS)}")
    expect_every_route_gated(policies)
    for name, policy in sorted(policies.items()):
        expect_admin_groups_only(name, policy, OTHER_GROUPS)


# --- no edge login, no login-less route ---------------------------------------
def test_with_no_edge_login_no_login_less_route_renders() -> None:
    infra = routes_of_class("infra")
    login_less = sorted(r["routeName"] for r in infra.values() if not r.get("ownLogin"))
    own_login = sorted(r["routeName"] for r in infra.values() if r.get("ownLogin"))
    expect("the chart declares login-less infra routes", bool(login_less), "none")
    for shape, args in NO_EDGE_LOGIN.items():
        docs = render(*args)
        served = http_routes(docs)
        bare = [name for name in login_less if name in served]
        expect(f"no login-less route renders [{shape}]", not bare, f"got {bare}")
        missing = [name for name in own_login if name not in served]
        expect(f"every route with its own login still renders [{shape}]", not missing,
               f"missing {missing}")
        gated = sorted(
            p["metadata"]["name"]
            for p in of_kind(docs, "SecurityPolicy")
            if targets(p) & infra_names()
        )
        expect(f"no infra route carries an edge policy [{shape}]", not gated, f"got {gated}")


# --- the kill switch ------------------------------------------------------------
def test_the_kill_switch_renders_no_infra_route() -> None:
    """Rendered with the edge login on, so every infra route would otherwise be served."""
    docs = render(*OIDC, "--set", "exposure.infraUisExternal=false")
    served = http_routes(docs)
    back = sorted(infra_names() & set(served))
    expect("exposure.infraUisExternal: false renders no infra HTTPRoute", not back, f"got {back}")
    gated = sorted(
        p["metadata"]["name"] for p in of_kind(docs, "SecurityPolicy") if targets(p) & infra_names()
    )
    expect("nor any policy for one", not gated, f"got {gated}")
    product = sorted(r["routeName"] for r in routes_of_class("product").values())
    missing = [name for name in product if name not in served]
    expect("the product routes still render", bool(product) and not missing, f"missing {missing}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
