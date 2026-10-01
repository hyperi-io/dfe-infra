#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_gateway_route_hardening.py
#  Purpose:      Prove which HTTPS routes the gateway serves send HSTS, and that a
#                hidden backend path answers 404 at the proxy on both faces.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for the gateway's response hardening.

HSTS was set on the public routes only, so every route on the wildcard listener
-- dfe-ui and the engine included -- could be downgraded by a redirect. Two
switches own it now: ui.tls.hsts for the public listeners, tls.edge.hsts for the
wildcard one. Left empty, tls.edge.hsts is off only where the edge certificate
chains to an internal CA root that is not persisted: every rebuild re-mints that
root, and a browser holding HSTS for the names would get no click-through.

dfe-ui answers /metrics unauthenticated on its app port for the in-cluster scrape,
which reaches the pod directly, and its route matched every path, so the gateway
published the scrape to anyone who could reach it. routes.<key>.hiddenPaths
answers those paths with a 404 from the proxy.

    python3 scripts/tests/test_gateway_route_hardening.py

Needs `helm` on PATH.
"""

import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATEWAY = chart_dir("envoy-gateway-config")
VALUES = REPO_ROOT / "argocd" / "values"

HSTS = "Strict-Transport-Security"
ONE_YEAR = 31536000
NOT_FOUND = "dfe-gateway-not-found"

# Every route renders: the edge login, the bundled deploy repo, OTLP ingress, the
# receiver door, the Cruise Control UI and three UIs on a public hostname.
EVERYTHING = (
    "--set", "domain=dfe.example.test",
    "--set", "appNamespace=dfe-local",
    "--set", "oidc.enabled=true",
    "--set-json",
    'oidc.providers=[{"name":"acme","issuerUrl":"https://id.example.com","clientId":"dfe"}]',
    "--set", "deployRepo.bundled=true",
    "--set", "otel.ingress.enabled=true",
    "--set", "otel.ingress.auth.remoteKey=dfe/test/otel/ingress",
    "--set", "routes.receiver.enabled=true",
    "--set", "kafka.mode=cluster",
    "--set", "ui.public_domain=public.example.test",
    "--set", "ui.public.argocd=true",
    "--set", "ui.public.hyperdx=true",
)

# Every route on the wildcard listener, which tls.edge.hsts governs.
INTERNAL_ROUTES = {"argocd", "cruise-control", "dfe-engine", "dfe-ui", "forgejo", "hyperdx",
                   "kafbat", "links", "otel", "receiver"}

# The edge wildcard issued by the chart's own internal CA (tls.internalCA.issuerName).
INTERNAL_CA = ("--set", "tls.issuerName=dfe-internal-ca")
PERSIST_ON = ("--set", "tls.internalCA.persist.enabled=true")
VAULT = (
    "--set", "tls.issuerName=dfe-estate-pki",
    "--set", "tls.vault.server=https://vault.example.test:8200",
    "--set", "tls.vault.path=pki_tls/sign/example",
    "--set", "tls.vault.appRole.roleId=example-role",
)

# (label, extra args, whether the wildcard listener sends HSTS)
EDGE_HSTS_CASES = (
    ("internal CA, persist off", INTERNAL_CA, False),
    ("internal CA, persist on", (*INTERNAL_CA, *PERSIST_ON), True),
    ("Vault/OpenBao PKI issuer", VAULT, True),
    ("ACME issuer", ("--set", "tls.acme.email=ops@example.test"), True),
    ("explicit true, internal CA persist off", (*INTERNAL_CA, "--set", "tls.edge.hsts=true"), True),
    ("explicit false, internal CA persist on",
     (*INTERNAL_CA, *PERSIST_ON, "--set", "tls.edge.hsts=false"), False),
    ("explicit false, Vault issuer", (*VAULT, "--set", "tls.edge.hsts=false"), False),
)


def helm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "envoy-gateway-config", str(GATEWAY),
         "--namespace", "envoy-gateway-system", "-f", str(VALUES / "common.yaml"), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def render(*args: str) -> list[dict]:
    out = helm(*args)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {args}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def routes(docs: list[dict]) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "HTTPRoute"}


def listener(route: dict) -> str:
    return route["spec"]["parentRefs"][0].get("sectionName", "")


def set_headers(rule: dict) -> dict[str, str]:
    found = {}
    for f in rule.get("filters", []):
        if f["type"] == "ResponseHeaderModifier":
            for header in f["responseHeaderModifier"].get("set", []):
                found[header["name"]] = header["value"]
    return found


def forwarding_rules(route: dict) -> list[dict]:
    """The rules that reach a backend, which are the ones a browser reads."""
    return [rule for rule in route["spec"]["rules"] if rule.get("backendRefs")]


def hsts_is_a_year_with_subdomains(value: str) -> bool:
    parts = [p.strip() for p in value.split(";")]
    ages = [int(p.split("=", 1)[1]) for p in parts if p.startswith("max-age=")]
    return len(ages) == 1 and ages[0] >= ONE_YEAR and "includeSubDomains" in parts


def test_the_render_reaches_every_route() -> None:
    """A sweep over routes that did not render would pass on nothing."""
    names = set(routes(render(*EVERYTHING)))
    want = {"argocd", "cruise-control", "dfe-engine", "dfe-ui", "forgejo", "hyperdx",
            "kafbat", "links", "otel", "receiver", "dfe-ui-public", "dfe-engine-public",
            "argocd-public", "hyperdx-public"}
    expect("every route renders", want <= names, f"missing {sorted(want - names)}")


def test_every_https_route_sends_a_years_hsts() -> None:
    for name, route in routes(render(*EVERYTHING)).items():
        if not listener(route).startswith("https"):
            continue
        for index, rule in enumerate(forwarding_rules(route)):
            value = set_headers(rule).get(HSTS, "")
            expect(f"{name} rule {index} sends {HSTS} for a year with subdomains",
                   hsts_is_a_year_with_subdomains(value), f"got {value!r}")


def test_the_plaintext_redirect_carries_none() -> None:
    """A browser ignores HSTS over plaintext; the redirect is the only answer there."""
    for name, route in routes(render(*EVERYTHING)).items():
        if listener(route) == "http":
            expect(f"{name} on :80 sets no {HSTS}",
                   all(HSTS not in set_headers(r) for r in route["spec"]["rules"]), "")


def test_each_listener_answers_to_its_own_switch() -> None:
    internal_off = routes(render(*EVERYTHING, "--set", "tls.edge.hsts=false"))
    public_off = routes(render(*EVERYTHING, "--set", "ui.tls.hsts=false"))
    for name, route in internal_off.items():
        if listener(route) == "https":
            expect(f"tls.edge.hsts=false: {name} sends none",
                   all(HSTS not in set_headers(r) for r in forwarding_rules(route)), "")
        elif listener(route).startswith("https-public-"):
            expect(f"tls.edge.hsts=false: public {name} still sends it",
                   all(HSTS in set_headers(r) for r in forwarding_rules(route)), "")
    for name, route in public_off.items():
        if listener(route).startswith("https-public-"):
            expect(f"ui.tls.hsts=false: public {name} sends none",
                   all(HSTS not in set_headers(r) for r in forwarding_rules(route)), "")
        elif listener(route) == "https":
            expect(f"ui.tls.hsts=false: internal {name} still sends it",
                   all(HSTS in set_headers(r) for r in forwarding_rules(route)), "")


def test_hyperdx_keeps_its_frame_ancestors_beside_hsts() -> None:
    """One filter of each type per rule, so the two headers share one modifier."""
    rule = forwarding_rules(routes(render(*EVERYTHING))["hyperdx"])[0]
    kinds = [f["type"] for f in rule.get("filters", [])]
    expect("hyperdx has one ResponseHeaderModifier", kinds.count("ResponseHeaderModifier") == 1,
           f"got {kinds}")
    headers = set_headers(rule)
    csp = headers.get("Content-Security-Policy", "")
    expect("carrying the frame-ancestors policy",
           csp.startswith("frame-ancestors 'self' https://dfe."), f"got {headers}")
    expect("and HSTS", HSTS in headers, f"got {headers}")


def test_no_rule_repeats_a_filter_type() -> None:
    for name, route in routes(render(*EVERYTHING)).items():
        for index, rule in enumerate(route["spec"]["rules"]):
            kinds = [f["type"] for f in rule.get("filters", [])]
            expect(f"{name} rule {index} names each filter type once",
                   len(kinds) == len(set(kinds)), f"got {kinds}")


def test_a_quoted_switch_is_refused() -> None:
    out = helm("--set", "domain=dfe.example.test", "--set-string", "tls.edge.hsts=true")
    expect("tls.edge.hsts as a string is refused by name",
           out.returncode != 0 and "tls.edge.hsts" in out.stderr, out.stderr[-300:])


def test_the_wildcard_default_follows_the_edge_issuer() -> None:
    """Off only where a rebuild re-mints the root a browser pinned HSTS against."""
    for label, args, want in EDGE_HSTS_CASES:
        found = routes(render(*EVERYTHING, *args))
        internal = {name: r for name, r in found.items() if listener(r) == "https"}
        expect(f"{label}: every wildcard route renders", set(internal) == INTERNAL_ROUTES,
               f"got {sorted(internal)}")
        for name, route in sorted(internal.items()):
            sends = [HSTS in set_headers(r) for r in forwarding_rules(route)]
            expect(f"{label}: {name} {'sends' if want else 'sends no'} {HSTS}",
                   bool(sends) and all(s == want for s in sends), f"got {sends}")
        for name, route in sorted(found.items()):
            if listener(route).startswith("https-public-"):
                expect(f"{label}: public {name} keeps ui.tls.hsts",
                       all(HSTS in set_headers(r) for r in forwarding_rules(route)), "")


def test_the_default_matches_the_root_persist_render() -> None:
    """Both internal-ca-persist.yaml and edgeHsts call envoy-gateway-config.internalRootPersisted;
    this proves the rendered manifests still agree, not just the shared expression."""
    variants = (
        ("persist off", ()),
        ("persist on", PERSIST_ON),
        ("persist on, no store", (*PERSIST_ON, "--set", "tls.internalCA.persist.secretStoreName=")),
        ("persist on, internal CA off", (*PERSIST_ON, "--set", "tls.internalCA.enabled=false")),
    )
    seen = set()
    for label, args in variants:
        docs = render(*EVERYTHING, *INTERNAL_CA, *args)
        persisted = any(d.get("kind") == "PushSecret" for d in docs)
        seen.add(persisted)
        sends = HSTS in set_headers(forwarding_rules(routes(docs)["dfe-ui"])[0])
        expect(f"{label}: HSTS on the wildcard exactly when the root persists",
               sends == persisted, f"persisted={persisted} sends={sends}")
    expect("the variants cover a persisted and an unpersisted root", seen == {True, False},
           f"got {seen}")


def test_empty_or_null_takes_the_derived_default() -> None:
    for how in (("--set", "tls.edge.hsts="), ("--set", "tls.edge.hsts=null")):
        out = helm(*EVERYTHING, *INTERNAL_CA, *how)
        expect(f"{' '.join(how)} renders", out.returncode == 0, out.stderr[-300:])
        if out.returncode != 0:
            continue
        docs = [d for d in yaml.safe_load_all(out.stdout) if d]
        dfe_ui = routes(docs)["dfe-ui"]
        expect(f"{' '.join(how)}: an unpersisted internal root sends none",
               all(HSTS not in set_headers(r) for r in forwarding_rules(dfe_ui)), "")


def hidden_rules(route: dict) -> list[dict]:
    return [rule for rule in route["spec"]["rules"] if not rule.get("backendRefs")]


def test_dfe_ui_metrics_answers_404_on_both_faces() -> None:
    docs = render(*EVERYTHING)
    found = routes(docs)
    for name in ("dfe-ui", "dfe-ui-public"):
        rules = hidden_rules(found[name])
        expect(f"{name} carries one hiding rule", len(rules) == 1, f"got {rules}")
        if len(rules) != 1:
            continue
        paths = [m["path"] for m in rules[0]["matches"]]
        expect(f"{name} hides /metrics by prefix",
               paths == [{"type": "PathPrefix", "value": "/metrics"}], f"got {paths}")
        refs = [f.get("extensionRef") for f in rules[0]["filters"]]
        expect(f"{name} answers it from the 404 filter",
               refs == [{"group": "gateway.envoyproxy.io", "kind": "HTTPRouteFilter",
                         "name": NOT_FOUND}], f"got {refs}")
    filters = [d for d in docs if d.get("kind") == "HTTPRouteFilter"]
    namespaces = {found[name]["metadata"]["namespace"] for name in ("dfe-ui", "dfe-ui-public")}
    placed = [(f["metadata"]["name"], f["metadata"]["namespace"]) for f in filters]
    expect("one 404 filter, in the routes' namespace",
           placed == [(NOT_FOUND, ns) for ns in sorted(namespaces)], f"got {placed}")
    if filters:
        status = filters[0]["spec"]["directResponse"]["statusCode"]
        expect("which answers 404 itself", status == 404, f"got {filters[0]['spec']}")


def test_only_the_hiding_route_gains_a_rule() -> None:
    for name, route in routes(render(*EVERYTHING)).items():
        # The :80 redirect answers every request itself and forwards nothing.
        if name.startswith("dfe-ui") or listener(route) == "http":
            continue
        expect(f"{name} forwards on every rule", hidden_rules(route) == [], "")


def test_an_empty_list_hides_nothing_and_renders_no_filter() -> None:
    docs = render(*EVERYTHING, "--set-json", "routes.dfeUi.hiddenPaths=[]")
    expect("no hiding rule on dfe-ui", hidden_rules(routes(docs)["dfe-ui"]) == [], "")
    expect("and no filter object", not [d for d in docs if d.get("kind") == "HTTPRouteFilter"], "")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
