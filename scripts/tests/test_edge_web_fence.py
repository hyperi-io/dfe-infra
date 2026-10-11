#  Project:      dfe-infra
#  File:         scripts/tests/test_edge_web_fence.py
#  Purpose:      Prove every web route on an internet-facing gateway is refused
#                unless the client is on ui.allowed_cidrs, that an empty list
#                refuses every address, that an allow-all list renders with a
#                warning, that only a named CDN's X-Forwarded-For is believed,
#                and that ingest routes and on-prem renders are untouched.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The web fence on an internet-facing gateway, as the chart renders it.

Envoy Gateway applies the most specific SecurityPolicy to a route and merges
nothing, so the policy that decides a route is its own when it has one and the
Gateway-wide dfe-edge-fence otherwise. Each case renders the chart, works out
that effective policy for every HTTPRoute, and asserts what it admits.

The forwarded-header cases go one step further: client_address() derives the
address that policy is checked against from the ClientTrafficPolicy on the
listener, following Envoy Gateway v1.9.2's translation, so a forged header is
judged against the rendered policies rather than against a description of them.

    python3 -m pytest scripts/tests/test_edge_web_fence.py -q

Needs `helm` on PATH.
"""

import ipaddress
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
# A CDN's edge ranges, one per family.
CDN = ["192.0.2.0/25", "2001:db8:cd::/48"]
# Untrusted hops beside the load balancer: a host in its subnet, such as the
# tunnel forwarder, and any other caller off the list.
FORWARDER = "192.0.2.200"
STRANGER = "192.0.2.150"


# --set splits on commas, so a comma-separated scalar goes through --set-json.
def allow(value: str) -> tuple[str, ...]:
    return ("--set-json", f"ui.allowed_cidrs={json.dumps(value)}")


def trust(value: list[str]) -> tuple[str, ...]:
    return ("--set-json", f"ui.trusted_proxy_cidrs={json.dumps(','.join(value))}")


FENCE = allow(",".join(ALLOWED))
PROVIDERS = [{"name": "acme", "issuerUrl": "https://id.example.com", "clientId": "dfe"}]
OIDC = ("--set", "oidc.enabled=true", "--set-json", f"oidc.providers={json.dumps(PROVIDERS)}")
OTEL_ON = ("--set", "otel.ingress.enabled=true", "--set", "otel.ingress.auth.remoteKey=dfe/test/otel/ingress")
PUBLIC = ("--set", "ui.public_domain=example.com")
# Every web route the chart can serve, the admin UIs and the public faces included.
EVERYTHING = (*PUBLIC, *OIDC, *OTEL_ON, "--set", "exposure.infraUisExternal=true",
              "--set", "deployRepo.bundled=true", "--set", "ui.public.kafbat=true")

INGEST = {"otel", "receiver"}
RECEIVER_ON = ("--set", "routes.receiver.enabled=true")
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
    docs = render("aws", *OTEL_ON, *RECEIVER_ON, "--set", "routes.receiver.authMode=bearer")
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


# --- the client address the fence judges ------------------------------------------
GATEWAY_NAME = "dfe-gateway"


def listener_policy(docs: list[dict], section: str) -> dict:
    """The ClientTrafficPolicy on one listener: the one naming its sectionName, else the Gateway's."""
    named, whole = [], []
    for policy in of_kind(docs, "ClientTrafficPolicy"):
        for ref in policy["spec"]["targetRefs"]:
            if ref.get("kind") != "Gateway" or ref.get("name") != GATEWAY_NAME:
                continue
            if "sectionName" not in ref:
                whole.append(policy)
            elif ref["sectionName"] == section:
                named.append(policy)
    assert len(named) <= 1, (section, named)
    assert len(whole) == 1, whole
    return (named or whole)[0]


def client_address(policy: dict, peer: str, forwarded: list[str]) -> str:
    """The address Envoy checks clientCIDRs against on a listener carrying this policy.

    Envoy Gateway v1.9.2 (internal/xds/translator/listener.go): with no
    clientIPDetection the connection manager sets use_remote_address and no
    trusted hops, so the TCP peer is the client whatever X-Forwarded-For says.
    xForwardedFor.trustedCIDRs installs Envoy's xff extension instead: the header
    is read only when the peer is inside the list, right to left, and the first
    entry outside it is the client, or the leftmost when every entry is inside.
    """
    detection = policy["spec"].get("clientIPDetection")
    if detection is None:
        return peer
    assert detection.keys() == {"xForwardedFor"}, detection
    assert detection["xForwardedFor"].keys() == {"trustedCIDRs"}, detection
    trusted = [ipaddress.ip_network(cidr) for cidr in detection["xForwardedFor"]["trustedCIDRs"]]

    def inside(address: str) -> bool:
        return any(ipaddress.ip_address(address) in net for net in trusted)

    if not inside(peer) or not forwarded:
        return peer
    for entry in reversed(forwarded):
        if not inside(entry):
            return entry
    return forwarded[0]


def admits(policy: dict, address: str) -> bool:
    """Whether an address-only SecurityPolicy lets a request from this address through."""
    auth = policy["spec"]["authorization"]
    ip = ipaddress.ip_address(address)
    for rule in auth.get("rules") or []:
        principal = rule.get("principal") or {}
        assert principal.keys() == {"clientCIDRs"}, (policy["metadata"]["name"], rule)
        if any(ip in ipaddress.ip_network(cidr) for cidr in principal["clientCIDRs"]):
            return rule["action"] == "Allow"
    return auth["defaultAction"] == "Allow"


def passes(docs: list[dict], route_name: str, peer: str, forwarded: tuple[str, ...] = ()) -> bool:
    """One request to a route: its listener names the client, its policy judges it."""
    (route,) = [r for r in of_kind(docs, "HTTPRoute") if r["metadata"]["name"] == route_name]
    (parent,) = route["spec"]["parentRefs"]
    address = client_address(listener_policy(docs, parent["sectionName"]), peer, list(forwarded))
    return admits(effective(docs, route), address)


@pytest.mark.parametrize("flavour", ["aws", "gcp", "azure"])
def test_an_allow_list_renders_with_no_trusted_proxy_range(flavour: str) -> None:
    """The public listeners judge the TCP peer, so a list needs no proxy range beside it."""
    docs = render(flavour, *PUBLIC, *allow(",".join(ALLOWED)))
    assert "clientIPDetection" not in listener_policy(docs, "https-public-dfe-ui")["spec"]


@pytest.mark.parametrize(("peer", "forwarded"), [
    (FORWARDER, ("203.0.113.7",)),
    (STRANGER, ("203.0.113.7",)),
    (STRANGER, ("198.51.100.20", "203.0.113.7")),
    ("2001:db8:ff::9", ("203.0.113.7",)),
], ids=["forwarder", "stranger", "stranger-two-entries", "stranger-v6"])
def test_a_forged_forwarded_for_from_an_untrusted_hop_does_not_pass_a_public_listener(
    peer: str, forwarded: tuple[str, ...]
) -> None:
    docs = render("aws", *PUBLIC, *allow(",".join(ALLOWED)))
    for route in ("dfe-ui-public", "dfe-engine-public"):
        assert not passes(docs, route, peer, forwarded), (route, peer, forwarded)
        assert passes(docs, route, "203.0.113.7", forwarded), route


@pytest.mark.parametrize(("peer", "forwarded", "admitted"), [
    ("192.0.2.10", ("203.0.113.7",), True),
    ("2001:db8:cd::1", ("198.51.100.20",), True),
    ("192.0.2.10", (STRANGER,), False),
    ("192.0.2.10", ("203.0.113.7", STRANGER), False),
    ("192.0.2.10", ("203.0.113.7", "192.0.2.11"), True),
    (FORWARDER, ("203.0.113.7",), False),
], ids=["cdn-v4", "cdn-v6", "cdn-unlisted-client", "cdn-forged-leftmost", "cdn-chain", "forwarder"])
def test_a_trusted_cdn_hop_passes_its_client_through_and_nothing_else(
    peer: str, forwarded: tuple[str, ...], admitted: bool
) -> None:
    """The CDN appends the client it saw, so an entry written before it is never the one read."""
    docs = render("aws", *PUBLIC, *allow(",".join(ALLOWED)), *trust(CDN))
    detection = listener_policy(docs, "https-public-dfe-ui")["spec"]["clientIPDetection"]
    assert detection == {"xForwardedFor": {"trustedCIDRs": CDN}}
    assert passes(docs, "dfe-ui-public", peer, forwarded) is admitted


@pytest.mark.parametrize("cdn", [[], CDN], ids=["no cdn", "cdn"])
def test_internal_listeners_never_read_the_forwarded_header(cdn: list[str]) -> None:
    docs = render("aws", *PUBLIC, *allow(",".join(ALLOWED)), *(trust(cdn) if cdn else ()))
    assert "clientIPDetection" not in listener_policy(docs, "https")["spec"]
    (public,) = [p for p in of_kind(docs, "ClientTrafficPolicy") if p["metadata"]["name"] == "dfe-public-edge"]
    assert [ref.get("sectionName") for ref in public["spec"]["targetRefs"]] == ["https-public-dfe-ui"]
    assert not passes(docs, "dfe-ui", "192.0.2.10", ("203.0.113.7",))
    assert not passes(docs, "dfe-ui", FORWARDER, ("203.0.113.7",))
    assert passes(docs, "dfe-ui", "203.0.113.7")


def identity(doc: dict) -> tuple[str, str, str]:
    return doc["kind"], doc["metadata"]["name"], doc["metadata"].get("namespace", "")


def test_naming_a_cdn_changes_nothing_but_the_public_listener_policy() -> None:
    base = render("aws", *PUBLIC, *allow(",".join(ALLOWED)))
    with_cdn = render("aws", *PUBLIC, *allow(",".join(ALLOWED)), *trust(CDN))
    changed = sorted(identity(d) for d in with_cdn if d not in base)
    assert changed == [("ClientTrafficPolicy", "dfe-public-edge", "envoy-gateway-system")], changed
    assert len(with_cdn) == len(base)


def test_the_notes_name_the_cdn_opt_in() -> None:
    plain = " ".join(notes("aws", *PUBLIC, *allow(",".join(ALLOWED))).split())
    assert "ignore X-Forwarded-For" in plain, plain
    assert "goes in ui.trusted_proxy_cidrs" in plain, plain
    named = " ".join(notes("aws", *PUBLIC, *allow(",".join(ALLOWED)), *trust(CDN)).split())
    assert f"ui.trusted_proxy_cidrs ({','.join(CDN)})" in named, named


# --- the receiver route on an internet-facing Service ------------------------------
@pytest.mark.parametrize("mode", [None, "none", "mtls", "both"])
def test_the_receiver_route_is_refused_on_an_internet_facing_service_without_caller_auth(mode: str | None) -> None:
    out = helm("aws", *RECEIVER_ON, *(("--set", f"routes.receiver.authMode={mode}") if mode else ()))
    assert out.returncode != 0, f"authMode {mode} rendered"
    assert "routes.receiver.enabled is true on an internet-facing Service" in out.stderr, out.stderr


@pytest.mark.parametrize("flavour", ["aws", "gcp", "azure"])
@pytest.mark.parametrize("mode", ["header", "bearer", "Bearer"])
def test_the_receiver_route_renders_once_its_caller_auth_is_declared(flavour: str, mode: str) -> None:
    docs = render(flavour, *RECEIVER_ON, "--set", f"routes.receiver.authMode={mode}")
    assert "receiver" in {r["metadata"]["name"] for r in of_kind(docs, "HTTPRoute")}


def test_the_receiver_route_off_the_internet_is_unchanged() -> None:
    docs = render("onprem", *RECEIVER_ON)
    assert "receiver" in {r["metadata"]["name"] for r in of_kind(docs, "HTTPRoute")}


def test_an_unknown_receiver_auth_mode_is_refused_by_name() -> None:
    out = helm("onprem", "--set", "routes.receiver.authMode=token")
    assert out.returncode != 0, "an unknown mode rendered"
    assert 'routes.receiver.authMode is "token"' in out.stderr, out.stderr


# --- a trusted range of every address -----------------------------------------------
def test_a_trusted_slash_zero_would_let_the_caller_name_the_client() -> None:
    """Why it is refused: every peer is trusted, so the leftmost entry is the client."""
    policy = {"spec": {"clientIPDetection": {"xForwardedFor": {"trustedCIDRs": ["0.0.0.0/0"]}}}}
    assert client_address(policy, STRANGER, ["203.0.113.7"]) == "203.0.113.7"
    assert client_address(policy, STRANGER, ["203.0.113.7", "192.0.2.11"]) == "203.0.113.7"


@pytest.mark.parametrize("flavour", ["aws", "gcp", "azure"])
@pytest.mark.parametrize("trusted", [
    ["0.0.0.0/0"], ["::/0"], ["192.0.2.0/25", "::/0"], ["192.0.2.0/25", "0.0.0.0/0", "2001:db8:cd::/48"],
], ids=["v4", "v6", "v6-among-ranges", "v4-among-ranges"])
def test_a_trusted_range_of_every_address_is_refused(flavour: str, trusted: list[str]) -> None:
    out = helm(flavour, *PUBLIC, *allow(",".join(ALLOWED)), *trust(trusted))
    assert out.returncode != 0, f"{trusted} rendered"
    assert "which trusts every address" in out.stderr, out.stderr
    assert "a /0 trusted list makes the fence a header the client sets" in out.stderr, out.stderr


# --- internetFacing held against the cloud's own markers ----------------------------
NOT_FACING = ("--set", "envoyGateway.service.internetFacing=false")
GKE_INTERNAL = {"networking.gke.io/load-balancer-type": "Internal"}
AKS_INTERNAL = {"service.beta.kubernetes.io/azure-load-balancer-internal": "true"}


def annotate(annotations: dict[str, str]) -> tuple[str, ...]:
    return ("--set-json", f"envoyGateway.service.annotations={json.dumps(annotations)}")


def test_an_aws_internet_facing_scheme_refuses_internet_facing_false() -> None:
    """edge-aws.yaml sets the scheme, so an overlay flipping the switch alone is caught."""
    for extra in ((), RECEIVER_ON):
        out = helm("aws", *NOT_FACING, *extra)
        assert out.returncode != 0, f"rendered with {extra}"
        assert "envoyGateway.service.internetFacing is false" in out.stderr, out.stderr
        assert "service.beta.kubernetes.io/aws-load-balancer-scheme: internet-facing" in out.stderr, out.stderr


@pytest.mark.parametrize(("flavour", "annotations", "named"), [
    ("gcp", {}, "networking.gke.io/load-balancer-type: Internal"),
    ("gcp", {"networking.gke.io/load-balancer-type": "External"}, "networking.gke.io/load-balancer-type: Internal"),
    ("azure", {}, "service.beta.kubernetes.io/azure-load-balancer-internal"),
    ("azure", {"service.beta.kubernetes.io/azure-load-balancer-internal": "false"},
     "service.beta.kubernetes.io/azure-load-balancer-internal"),
], ids=["gcp-no-marker", "gcp-external", "azure-no-marker", "azure-false"])
def test_a_public_by_default_load_balancer_refuses_internet_facing_false(
    flavour: str, annotations: dict[str, str], named: str
) -> None:
    out = helm(flavour, *NOT_FACING, *(annotate(annotations) if annotations else ()))
    assert out.returncode != 0, f"{flavour} {annotations} rendered"
    assert "envoyGateway.service.internetFacing is false" in out.stderr, out.stderr
    assert named in out.stderr, out.stderr


@pytest.mark.parametrize(("flavour", "annotations"), [
    ("aws", {"service.beta.kubernetes.io/aws-load-balancer-scheme": "internal"}),
    ("gcp", GKE_INTERNAL),
    ("gcp", {"cloud.google.com/load-balancer-type": "Internal"}),
    ("azure", AKS_INTERNAL),
], ids=["aws-internal", "gcp-internal", "gcp-internal-legacy", "azure-internal"])
def test_an_internal_load_balancer_renders_with_internet_facing_false(flavour: str, annotations: dict[str, str]) -> None:
    docs = render(flavour, *NOT_FACING, *annotate(annotations))
    assert gateway_fence(docs) is None


@pytest.mark.parametrize("flavour", ["gcp", "azure"])
def test_a_service_that_is_not_a_load_balancer_is_not_held_to_the_marker(flavour: str) -> None:
    render(flavour, *NOT_FACING, "--set", "envoyGateway.service.type=ClusterIP")
