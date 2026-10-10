#  Project:      dfe-infra
#  File:         scripts/tests/test_edge_web_fence.py
#  Purpose:      Prove every web route on an internet-facing gateway is refused
#                unless the client is on ui.allowed_cidrs, that an empty list
#                refuses every address, that an allow-all list renders with a
#                warning, and that ingest routes and on-prem renders are untouched.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The web fence on an internet-facing gateway, as the chart renders it.

Envoy Gateway applies the most specific SecurityPolicy to a route and merges
nothing, so the policy that decides a route is its own when it has one and the
Gateway-wide dfe-edge-fence otherwise. Each case renders the chart, works out
that effective policy for every HTTPRoute, and asserts what it admits.

    python3 -m pytest scripts/tests/test_edge_web_fence.py -q

Needs `helm` on PATH.
"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from _charts import chart_dir

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATEWAY = chart_dir("envoy-gateway-config")
VALUES = REPO_ROOT / "argocd" / "values"

ALLOWED = ["203.0.113.7/32", "198.51.100.0/24"]
TRUSTED = "10.90.128.0/20"


# --set splits on commas, so a comma-separated scalar goes through --set-json.
def allow(value: str) -> tuple[str, ...]:
    return ("--set-json", f"ui.allowed_cidrs={json.dumps(value)}", "--set", f"ui.trusted_proxy_cidrs={TRUSTED}")


FENCE = allow(",".join(ALLOWED))
PROVIDERS = [{"name": "acme", "issuerUrl": "https://id.example.com", "clientId": "dfe"}]
OIDC = ("--set", "oidc.enabled=true", "--set-json", f"oidc.providers={json.dumps(PROVIDERS)}")
OTEL_ON = ("--set", "otel.ingress.enabled=true", "--set", "otel.ingress.auth.remoteKey=dfe/test/otel/ingress")
PUBLIC = ("--set", "ui.public_domain=example.com")
# Every web route the chart can serve, the admin UIs and the public faces included.
EVERYTHING = (*PUBLIC, *OIDC, *OTEL_ON, "--set", "exposure.infraUisExternal=true",
              "--set", "deployRepo.bundled=true", "--set", "ui.public.kafbat=true")

INGEST = {"otel", "receiver"}
# The :80 listener's redirect answers with a 301 and serves nothing.
REDIRECT = "dfe-gateway-http-redirect"


def cascade(flavour: str) -> tuple[str, ...]:
    """The values layer2-edge.yaml layers for one cloud flavour, then the profile."""
    cloud = {"onprem": "local"}.get(flavour, flavour)
    return (
        "--namespace", "envoy-gateway-system",
        "-f", str(VALUES / "common.yaml"),
        "-f", str(VALUES / f"{cloud}.yaml"),
        "-f", str(VALUES / f"edge-{flavour}.yaml"),
        "-f", str(VALUES / "profile-scale.yaml"),
        "--set", "appNamespace=dfe",
        "--set", "domain=dfe.example.com",
    )


def helm(flavour: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "envoy-gateway-config", str(GATEWAY), *cascade(flavour), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def render(flavour: str, *args: str) -> list[dict]:
    out = helm(flavour, *args)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def notes(flavour: str, *args: str) -> str:
    """The chart's NOTES, which `helm template` never prints and a client dry run does."""
    out = subprocess.run(
        ["helm", "install", "envoy-gateway-config", str(GATEWAY), "--dry-run=client",
         "--kubeconfig", "/dev/null", *cascade(flavour), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.partition("\nNOTES:\n")[2]


def of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def gateway_fence(docs: list[dict]) -> dict | None:
    found = [p for p in of_kind(docs, "SecurityPolicy") if p["metadata"]["name"] == "dfe-edge-fence"]
    assert len(found) <= 1, found
    return found[0] if found else None


def route_policies(docs: list[dict], route: dict) -> list[dict]:
    """Every SecurityPolicy naming this HTTPRoute, in its own namespace."""
    name, ns = route["metadata"]["name"], route["metadata"]["namespace"]
    return [
        p for p in of_kind(docs, "SecurityPolicy")
        if p["metadata"]["namespace"] == ns
        and any(ref.get("kind") == "HTTPRoute" and ref.get("name") == name for ref in p["spec"]["targetRefs"])
    ]


def effective(docs: list[dict], route: dict) -> dict | None:
    """The policy Envoy Gateway applies to a route: its own, else the Gateway's."""
    own = route_policies(docs, route)
    assert len(own) <= 1, f"{route['metadata']['name']} has {len(own)} policies; only the oldest would apply"
    return own[0] if own else gateway_fence(docs)


def web_routes(docs: list[dict]) -> list[dict]:
    return [
        r for r in of_kind(docs, "HTTPRoute")
        if r["metadata"]["name"] not in INGEST | {REDIRECT}
    ]


def admitted(policy: dict) -> list[list[str]]:
    """The clientCIDRs each Allow rule names; a rule naming none admits every address."""
    auth = policy["spec"].get("authorization")
    assert auth is not None, f"{policy['metadata']['name']} carries no authorization"
    assert auth.get("defaultAction") == "Deny", f"{policy['metadata']['name']}: {auth}"
    return [
        (rule.get("principal") or {}).get("clientCIDRs") or []
        for rule in auth.get("rules") or []
        if rule.get("action") == "Allow"
    ]


def envoy_service(docs: list[dict]) -> dict:
    (proxy,) = of_kind(docs, "EnvoyProxy")
    return proxy["spec"]["provider"]["kubernetes"]["envoyService"]


# --- no list: every web route refuses every address ---------------------------
@pytest.mark.parametrize("flavour", ["aws", "gcp", "azure"])
def test_an_internet_facing_gateway_with_no_list_refuses_every_web_route(flavour: str) -> None:
    docs = render(flavour, *EVERYTHING)
    routes = web_routes(docs)
    names = {r["metadata"]["name"] for r in routes}
    assert {"dfe-ui", "dfe-engine", "hyperdx", "argocd", "dfe-ui-public", "dfe-engine-public",
            "kafbat-public"} <= names, sorted(names)
    for route in routes:
        policy = effective(docs, route)
        assert policy is not None, route["metadata"]["name"]
        assert admitted(policy) == [], (route["metadata"]["name"], policy["metadata"]["name"])


def test_no_list_leaves_the_load_balancer_open_and_says_deny() -> None:
    """Envoy is the wall then, so ingest on the same Service still reaches its own auth."""
    service = envoy_service(render("aws"))
    assert "loadBalancerSourceRanges" not in service, service
    assert service["annotations"]["dfe.hyperi.io/edge-fence"] == "deny"
    assert gateway_fence(render("aws"))["metadata"]["annotations"]["dfe.hyperi.io/edge-fence"] == "deny"


def test_no_list_says_the_public_web_is_closed() -> None:
    text = notes("aws")
    assert "PUBLIC WEB CLOSED" in text, text
    assert "WARNING" not in text, text


# --- a list: every web route admits exactly the list ---------------------------
@pytest.mark.parametrize("flavour", ["aws", "gcp", "azure"])
def test_a_list_admits_exactly_its_ranges_on_every_web_route(flavour: str) -> None:
    docs = render(flavour, *EVERYTHING, *FENCE)
    for route in web_routes(docs):
        policy = effective(docs, route)
        rules = admitted(policy)
        assert rules, (route["metadata"]["name"], policy["metadata"]["name"])
        for cidrs in rules:
            assert cidrs == ALLOWED, (route["metadata"]["name"], policy["metadata"]["name"], cidrs)


def test_a_list_also_fences_the_load_balancer() -> None:
    service = envoy_service(render("aws", *FENCE))
    assert service["loadBalancerSourceRanges"] == ALLOWED
    assert service["annotations"]["dfe.hyperi.io/edge-fence"] == "listed"


def test_the_edge_login_keeps_its_group_check_beside_the_address() -> None:
    """An OIDC policy replaces the Gateway fence, so it has to carry both principals."""
    docs = render("aws", *EVERYTHING, *FENCE)
    (argocd,) = [r for r in of_kind(docs, "HTTPRoute") if r["metadata"]["name"] == "argocd"]
    (policy,) = route_policies(docs, argocd)
    (rule,) = policy["spec"]["authorization"]["rules"]
    assert rule["principal"]["clientCIDRs"] == ALLOWED
    assert rule["principal"]["jwt"]["claims"][0]["name"] == "groups"


def test_a_public_route_with_no_group_check_names_no_bare_jwt_principal() -> None:
    """The CRD refuses a jwt principal with no claim, so the address is the whole rule."""
    docs = render("aws", *PUBLIC, *OIDC, *FENCE)
    for policy in of_kind(docs, "SecurityPolicy"):
        for rule in (policy["spec"].get("authorization") or {}).get("rules") or []:
            jwt = (rule.get("principal") or {}).get("jwt")
            if jwt is not None:
                assert jwt.get("claims") or jwt.get("scopes"), policy["metadata"]["name"]


# --- allow-all: renders, and says so ---------------------------------------------
@pytest.mark.parametrize("value", ["0.0.0.0/0", "::/0", "203.0.113.7/32,0.0.0.0/0"])
def test_an_allow_all_list_renders_and_is_named(value: str) -> None:
    args = allow(value)
    docs = render("aws", *args)
    assert envoy_service(docs)["annotations"]["dfe.hyperi.io/edge-fence"] == "allow-all"
    text = notes("aws", *args)
    assert "WARNING: ui.allowed_cidrs admits every address" in text, text


# --- ingest is not a web surface --------------------------------------------------
def test_ingest_routes_answer_to_their_own_auth_not_the_fence() -> None:
    docs = render("aws", *OTEL_ON, "--set", "routes.receiver.enabled=true")
    for name in INGEST:
        (route,) = [r for r in of_kind(docs, "HTTPRoute") if r["metadata"]["name"] == name]
        (policy,) = route_policies(docs, route)
        assert policy["spec"]["authorization"] == {"defaultAction": "Allow"}, policy


# --- off the internet nothing changes ---------------------------------------------
def test_an_on_prem_gateway_renders_no_fence() -> None:
    docs = render("onprem", *OIDC, *OTEL_ON)
    names = {p["metadata"]["name"] for p in of_kind(docs, "SecurityPolicy")}
    assert "dfe-edge-fence" not in names, sorted(names)
    assert not {n for n in names if n.startswith("dfe-ingest-")}, sorted(names)
    assert "dfe.hyperi.io/edge-fence" not in (envoy_service(docs).get("annotations") or {})
    assert notes("onprem").strip() == ""


def test_a_quoted_internet_facing_switch_is_refused_by_name() -> None:
    out = helm("onprem", "--set-string", "envoyGateway.service.internetFacing=false")
    assert out.returncode != 0, "a quoted switch rendered"
    assert "envoyGateway.service.internetFacing is false (a string)" in out.stderr, out.stderr


# --- two OIDC providers: one policy per public route -----------------------------
PROVIDERS_TWO = [*PROVIDERS, {"name": "globex", "issuerUrl": "https://sso.example.org", "clientId": "dfe-globex"}]
OIDC_TWO = ("--set", "oidc.enabled=true", "--set-json", f"oidc.providers={json.dumps(PROVIDERS_TWO)}")
EVERYTHING_TWO = (*PUBLIC, *OIDC_TWO, *OTEL_ON, "--set", "exposure.infraUisExternal=true",
                  "--set", "deployRepo.bundled=true", "--set", "ui.public.argocd=true")


def public_policy(docs: list[dict], route: str) -> dict:
    (found,) = [r for r in of_kind(docs, "HTTPRoute") if r["metadata"]["name"] == route]
    (policy,) = route_policies(docs, found)
    return policy


@pytest.mark.parametrize("fence", [(), FENCE], ids=["no list", "list"])
def test_two_providers_leave_every_web_route_one_policy_and_fenced(fence: tuple[str, ...]) -> None:
    """effective() refuses a route carrying two policies, which is what one per provider was."""
    docs = render("aws", *EVERYTHING_TWO, *fence)
    want = [ALLOWED] if fence else []
    for route in web_routes(docs):
        rules = admitted(effective(docs, route))
        assert all(cidrs == ALLOWED for cidrs in rules) if fence else rules == [], route["metadata"]["name"]
        assert bool(rules) == bool(want), route["metadata"]["name"]


def test_every_provider_stays_an_issuer_and_the_login_provider_owns_the_redirect() -> None:
    docs = render("aws", *EVERYTHING_TWO, *FENCE, "--set", "oidc.loginProvider=globex")
    for route in ("dfe-engine-public", "argocd-public"):
        policy = public_policy(docs, route)
        assert policy["metadata"]["name"] == f"dfe-public-oidc-globex-{route.removesuffix('-public')}"
        assert policy["spec"]["oidc"]["clientID"] == "dfe-globex"
        issuers = sorted(p["name"] for p in policy["spec"]["jwt"]["providers"])
        assert issuers == ["acme", "globex"], (route, issuers)
    rules = public_policy(docs, "argocd-public")["spec"]["authorization"]["rules"]
    assert sorted(rule["principal"]["jwt"]["provider"] for rule in rules) == ["acme", "globex"]
    for rule in rules:
        assert rule["principal"]["clientCIDRs"] == ALLOWED
        assert rule["principal"]["jwt"]["claims"][0]["name"] == "groups"


# --- the :80 redirect is not fenced -----------------------------------------------
@pytest.mark.parametrize("fence", [(), FENCE], ids=["no list", "list"])
def test_the_http_redirect_answers_everyone_with_its_301(fence: tuple[str, ...]) -> None:
    """It serves nothing but the move to https, where the fence holds."""
    docs = render("aws", *fence)
    (redirect,) = [r for r in of_kind(docs, "HTTPRoute") if r["metadata"]["name"] == REDIRECT]
    (policy,) = route_policies(docs, redirect)
    assert policy["spec"]["authorization"] == {"defaultAction": "Allow"}, policy
    assert redirect["spec"]["rules"][0]["filters"][0]["type"] == "RequestRedirect"
    assert "backendRefs" not in redirect["spec"]["rules"][0]


# --- a malformed entry fails the render, not the sync ------------------------------
@pytest.mark.parametrize("entry", [
    "203.0.113.7", "203.0.113.7/33", "300.1.1.1/32", "203.0.113.0/24junk",
    "2001:db8::/129", "2001:db8::", "fe80::1%eth0/64", "not-a-cidr",
])
def test_a_malformed_allow_list_entry_is_refused_by_name(entry: str) -> None:
    out = helm("aws", *allow(f"198.51.100.0/24,{entry}"))
    assert out.returncode != 0, f"{entry} rendered"
    assert f'ui.allowed_cidrs carries "{entry}", which is not a CIDR range' in out.stderr, out.stderr


def test_a_malformed_trusted_proxy_entry_is_refused_by_name() -> None:
    out = helm("onprem", "--set", "ui.allowed_cidrs=198.51.100.0/24", "--set", "ui.trusted_proxy_cidrs=10.0.0.1")
    assert out.returncode != 0, "a bare trusted proxy address rendered"
    assert 'ui.trusted_proxy_cidrs carries "10.0.0.1"' in out.stderr, out.stderr


@pytest.mark.parametrize("entry", [
    "203.0.113.7/32", "10.0.0.0/8", "0.0.0.0/0", "2001:db8::/64", "::/0", "::ffff:203.0.113.7/128",
])
def test_every_cidr_shape_envoy_gateway_accepts_renders(entry: str) -> None:
    assert helm("aws", *allow(entry)).returncode == 0, entry


# --- HyperDX beside the console ---------------------------------------------------
@pytest.mark.parametrize("flavour", ["aws", "gcp", "azure"])
def test_hyperdx_renders_wherever_the_console_does(flavour: str) -> None:
    """The Observe page frames it, so the kill switch that removes the admin UIs leaves it."""
    names = {r["metadata"]["name"] for r in of_kind(render(flavour), "HTTPRoute")}
    assert {"dfe-ui", "hyperdx"} <= names, sorted(names)
    assert not {"argocd", "kafbat", "links"} & names, sorted(names)
