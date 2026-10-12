#  Project:      dfe-infra
#  File:         scripts/tests/test_edge_web_fence.py
#  Purpose:      Prove every web route on an internet-facing gateway admits only
#                its own exposure group's allow list, refuses that group's deny
#                list first, and refuses every address while the list is empty;
#                that the load balancer is the union of the enabled web groups;
#                that only a named CDN's X-Forwarded-For is believed; that the
#                ingest routes answer to the ingest group; and that on-prem
#                renders keep their own behaviour.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The web fence on an internet-facing gateway, as the chart renders it.

Envoy Gateway applies the most specific SecurityPolicy to a route and merges
nothing, so the policy that decides a route is its own when it has one and the
Gateway-wide dfe-edge-fence otherwise. Each case renders the chart, works out
that effective policy for every HTTPRoute, and asserts what it admits.

A route's class picks its group: product -> console, api -> api, infra ->
admin. passes() judges a request the way Envoy does: the listener names the
client, then the route's policy evaluates its rules in order and the first
match decides (api/v1alpha1/authorization_types.go at v1.9.2).

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

CONSOLE = ["203.0.113.7/32", "198.51.100.0/24"]
API = ["192.0.2.64/27"]
ADMIN = ["192.0.2.128/28"]
SELF = ["203.0.113.200/32"]
# One address inside each list, and one on none of them.
ON_CONSOLE, ON_API, ON_ADMIN, OFF_ALL = "198.51.100.20", "192.0.2.70", "192.0.2.130", "192.0.2.10"
# A CDN's edge ranges, one per family.
CDN = ["192.0.2.0/25", "2001:db8:cd::/48"]
# Untrusted hops beside the load balancer: a host in its subnet, such as the
# tunnel forwarder, and any other caller off the list.
FORWARDER = "192.0.2.200"
STRANGER = "192.0.2.150"


def group_list(group: str, field: str, value: list[str]) -> tuple[str, ...]:
    return ("--set-json", f"exposure.groups.{group}.{field}={json.dumps(value)}")


def allow(group: str, value: list[str]) -> tuple[str, ...]:
    return group_list(group, "allow", value)


def deny(group: str, value: list[str]) -> tuple[str, ...]:
    return group_list(group, "deny", value)


def trust(value: list[str]) -> tuple[str, ...]:
    return ("--set-json", f"ui.trusted_proxy_cidrs={json.dumps(','.join(value))}")


def self_ranges(value: list[str]) -> tuple[str, ...]:
    return ("--set-json", f"exposure.selfRanges={json.dumps(value)}")


FENCE = allow("console", CONSOLE)
PROVIDERS = [{"name": "acme", "issuerUrl": "https://id.example.com", "clientId": "dfe"}]
OIDC = ("--set", "oidc.enabled=true", "--set-json", f"oidc.providers={json.dumps(PROVIDERS)}")
OTEL_ON = ("--set", "otel.ingress.enabled=true", "--set", "otel.ingress.auth.remoteKey=dfe/test/otel/ingress")
PUBLIC = ("--set", "ui.public_domain=example.com")
CLI_PUBLIC = ("--set", "ui.engine_api.cli_families_public=true")
ADMIN_ON = ("--set", "exposure.groups.admin.enabled=true", *allow("admin", ADMIN),
            "--set", "deployRepo.bundled=true", "--set", "exposure.groups.admin.tools.kafbat=true")
# Every web route the chart can serve on its product and api faces, public included.
PRODUCT = (*PUBLIC, *OIDC, *OTEL_ON, *CLI_PUBLIC)
# Every group's own list, the admin UIs and every public face included.
EVERY_GROUP = (*PRODUCT, *ADMIN_ON, *FENCE, *allow("api", API))

INGEST = {"otel", "receiver"}
# The receiver route renders from the ingest group's door alone.
RECEIVER_ON = ("--set", "exposure.groups.ingest.door=gateway")
RECEIVER_BEARER = (*RECEIVER_ON, "--set", "routes.receiver.authMode=bearer")
SENDERS = ["192.0.2.32/28"]
ON_SENDERS, DENIED_SENDER = "192.0.2.40", "192.0.2.41"
# The :80 listener's redirect answers with a 301 and serves nothing.
REDIRECT = "dfe-gateway-http-redirect"

GROUP_OF = {
    "dfe-ui": "console", "dfe-engine": "console", "hyperdx": "console",
    "dfe-engine-cli": "api",
    "argocd": "admin", "kafbat": "admin", "links": "admin", "forgejo": "admin", "cruise-control": "admin",
}


def group_of(route_name: str) -> str:
    return GROUP_OF[route_name.removesuffix("-public")]


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


def route(docs: list[dict], name: str) -> dict:
    (found,) = [r for r in of_kind(docs, "HTTPRoute") if r["metadata"]["name"] == name]
    return found


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
    docs = render(flavour, *PRODUCT)
    routes = web_routes(docs)
    names = {r["metadata"]["name"] for r in routes}
    assert {"dfe-ui", "dfe-engine", "dfe-engine-cli", "hyperdx", "dfe-ui-public", "dfe-engine-public",
            "dfe-engine-cli-public"} <= names, sorted(names)
    for r in routes:
        policy = effective(docs, r)
        assert policy is not None, r["metadata"]["name"]
        assert admitted(policy) == [], (r["metadata"]["name"], policy["metadata"]["name"])


@pytest.mark.parametrize("flavour", ["aws", "gcp", "azure"])
def test_every_rendered_web_route_carries_its_own_policy(flavour: str) -> None:
    """So the Gateway-level fence holds only a route this chart did not render."""
    docs = render(flavour, *EVERY_GROUP)
    for r in web_routes(docs):
        assert len(route_policies(docs, r)) == 1, r["metadata"]["name"]
    fence = gateway_fence(docs)
    assert fence["spec"]["authorization"] == {"defaultAction": "Deny"}, fence


def test_no_list_leaves_the_load_balancer_open_and_says_deny() -> None:
    """Envoy is the wall then, refusing every web route on its own policy."""
    docs = render("aws")
    service = envoy_service(docs)
    assert "loadBalancerSourceRanges" not in service, service
    assert service["annotations"]["dfe.hyperi.io/edge-fence"] == "console=deny,api=deny,admin=off"
    assert gateway_fence(docs)["metadata"]["annotations"]["dfe.hyperi.io/edge-fence"] == (
        "console=deny,api=deny,admin=off"
    )


def test_no_list_says_each_group_is_closed() -> None:
    text = notes("aws")
    assert "CONSOLE CLOSED" in text, text
    assert "API CLOSED" in text, text
    assert "ADMIN" not in text, text
    assert "WARNING" not in text, text


# --- each group admits its own list and nothing else ---------------------------
@pytest.mark.parametrize("flavour", ["aws", "gcp", "azure"])
def test_each_web_route_admits_exactly_its_own_groups_list(flavour: str) -> None:
    docs = render(flavour, *EVERY_GROUP)
    want = {"console": CONSOLE, "api": API, "admin": ADMIN}
    seen = set()
    for r in web_routes(docs):
        name = r["metadata"]["name"]
        group = group_of(name)
        seen.add(group)
        rules = admitted(effective(docs, r))
        assert rules, name
        for cidrs in rules:
            assert cidrs == want[group], (name, group, cidrs)
    assert seen == {"console", "api", "admin"}, seen


@pytest.mark.parametrize(("address", "group"), [
    (ON_CONSOLE, "console"), (ON_API, "api"), (ON_ADMIN, "admin"),
], ids=["console-address", "api-address", "admin-address"])
def test_expected_fail_an_address_on_one_list_is_refused_on_every_other_groups_route(
    address: str, group: str
) -> None:
    """Address-only routes: the CLI route, kafbat, and every console route."""
    docs = render("aws", *EVERY_GROUP)
    for name in ("dfe-ui", "hyperdx", "dfe-engine-cli", "dfe-engine-cli-public", "kafbat", "kafbat-public",
                 "dfe-ui-public"):
        assert passes(docs, name, address) is (group_of(name) == group), (name, address)


def test_an_off_list_address_passes_no_web_route() -> None:
    docs = render("aws", *EVERY_GROUP)
    for name in ("dfe-ui", "hyperdx", "dfe-engine-cli", "kafbat", "dfe-ui-public", "dfe-engine-cli-public"):
        assert not passes(docs, name, OFF_ALL), name


def test_the_edge_login_keeps_its_group_check_beside_the_admin_address() -> None:
    """An OIDC policy replaces the Gateway fence, so it has to carry both principals."""
    docs = render("aws", *EVERY_GROUP)
    for name in ("argocd", "links", "forgejo"):
        (policy,) = route_policies(docs, route(docs, name))
        (rule,) = policy["spec"]["authorization"]["rules"]
        assert rule["principal"]["clientCIDRs"] == ADMIN, name
        assert rule["principal"]["jwt"]["claims"][0]["name"] == "groups", name


def test_the_engine_edge_login_carries_the_console_list_not_the_api_list() -> None:
    docs = render("aws", *EVERY_GROUP)
    for name in ("dfe-engine", "dfe-engine-public"):
        (policy,) = route_policies(docs, route(docs, name))
        assert "oidc" in policy["spec"], name
        assert admitted(policy) == [CONSOLE], name


def test_a_public_route_with_no_group_check_names_no_bare_jwt_principal() -> None:
    """The CRD refuses a jwt principal with no claim, so the address is the whole rule."""
    docs = render("aws", *PUBLIC, *OIDC, *FENCE)
    for policy in of_kind(docs, "SecurityPolicy"):
        for rule in (policy["spec"].get("authorization") or {}).get("rules") or []:
            jwt = (rule.get("principal") or {}).get("jwt")
            if jwt is not None:
                assert jwt.get("claims") or jwt.get("scopes"), policy["metadata"]["name"]


# --- deny goes ahead of allow, on its own group alone -----------------------------
DENIED = "198.51.100.66"


@pytest.mark.parametrize("flavour", ["aws", "onprem"])
def test_expected_fail_a_denied_address_inside_the_allow_list_is_refused(flavour: str) -> None:
    docs = render(flavour, *PUBLIC, *FENCE, *deny("console", [f"{DENIED}/32"]), *allow("api", API))
    for name in ("dfe-ui", "hyperdx", "dfe-ui-public"):
        policy = effective(docs, route(docs, name))
        first = policy["spec"]["authorization"]["rules"][0]
        assert first == {"name": "deny-listed-cidrs", "action": "Deny",
                         "principal": {"clientCIDRs": [f"{DENIED}/32"]}}, (name, first)
        assert not passes(docs, name, DENIED), name
        assert passes(docs, name, ON_CONSOLE), name


def test_a_deny_list_reaches_only_its_own_groups_routes() -> None:
    docs = render("aws", *EVERY_GROUP, *deny("console", [f"{DENIED}/32"]))
    for r in web_routes(docs):
        name = r["metadata"]["name"]
        rules = effective(docs, r)["spec"]["authorization"].get("rules") or []
        denies = [rule for rule in rules if rule["action"] == "Deny"]
        assert bool(denies) is (group_of(name) == "console"), (name, rules)


def test_a_deny_list_on_an_open_group_admits_everyone_else() -> None:
    """Off the internet an empty allow list is no fence, and the deny list still applies."""
    docs = render("onprem", *deny("console", [f"{DENIED}/32"]))
    policy = effective(docs, route(docs, "dfe-ui"))
    assert policy["spec"]["authorization"]["defaultAction"] == "Allow", policy
    assert not passes(docs, "dfe-ui", DENIED)
    assert passes(docs, "dfe-ui", OFF_ALL)
    assert route_policies(docs, route(docs, "dfe-engine-cli")) == []


# --- the load balancer is the union of the enabled web groups ---------------------
def test_the_load_balancer_carries_the_union_of_the_enabled_web_groups() -> None:
    extra = ["198.18.0.0/24"]
    docs = render("aws", *EVERY_GROUP, *self_ranges(SELF),
                  "--set-json", f"envoyGateway.service.loadBalancerSourceRanges={json.dumps(extra)}")
    ranges = envoy_service(docs)["loadBalancerSourceRanges"]
    assert sorted(ranges) == sorted({*extra, *CONSOLE, *API, *ADMIN, *SELF}), ranges


def test_expected_fail_a_disabled_groups_list_stays_off_the_load_balancer() -> None:
    docs = render("aws", *FENCE, *allow("admin", ADMIN))
    ranges = envoy_service(docs)["loadBalancerSourceRanges"]
    assert sorted(ranges) == sorted(CONSOLE), ranges
    assert not set(ADMIN) & set(ranges)


def test_a_deny_list_never_reaches_the_load_balancer() -> None:
    docs = render("aws", *FENCE, *deny("console", [f"{DENIED}/32"]))
    assert f"{DENIED}/32" not in envoy_service(docs)["loadBalancerSourceRanges"]


def test_self_ranges_alone_close_the_load_balancer_to_everyone_else() -> None:
    docs = render("aws", *self_ranges(SELF))
    assert envoy_service(docs)["loadBalancerSourceRanges"] == SELF


def test_an_open_group_leaves_the_load_balancer_open() -> None:
    """allowPublic needs every address at L4, so the web groups add no range."""
    docs = render("aws", *FENCE, *self_ranges(SELF), *OIDC,
                  "--set", "exposure.groups.admin.enabled=true", "--set", "exposure.groups.admin.allowPublic=true")
    assert "loadBalancerSourceRanges" not in envoy_service(docs)


def test_an_explicit_service_range_still_narrows_an_open_group() -> None:
    extra = ["198.18.0.0/24"]
    docs = render("onprem", *FENCE,
                  "--set-json", f"envoyGateway.service.loadBalancerSourceRanges={json.dumps(extra)}")
    assert sorted(envoy_service(docs)["loadBalancerSourceRanges"]) == sorted([*extra, *CONSOLE])


# --- selfRanges: every web group admits the cluster's own egress -------------------
def test_self_ranges_join_every_web_groups_allow_list() -> None:
    docs = render("aws", *EVERY_GROUP, *self_ranges(SELF))
    for name in ("dfe-ui", "dfe-engine-cli", "kafbat", "dfe-ui-public", "dfe-engine-cli-public"):
        assert passes(docs, name, SELF[0].split("/")[0]), name


def test_self_ranges_do_not_make_an_empty_group_listed() -> None:
    """The deployer's list decides what a group admits; the cluster's egress is not one."""
    args = (*self_ranges(SELF),)
    docs = render("aws", *args)
    assert envoy_service(docs)["annotations"]["dfe.hyperi.io/edge-fence"] == "console=deny,api=deny,admin=off"
    assert "CONSOLE CLOSED" in notes("aws", *args)
    assert passes(docs, "dfe-ui", SELF[0].split("/")[0])
    assert not passes(docs, "dfe-ui", OFF_ALL)


@pytest.mark.parametrize("entry", ["0.0.0.0/0", "::/0"])
def test_expected_fail_a_self_range_of_every_address_is_refused(entry: str) -> None:
    out = helm("aws", *self_ranges([entry]))
    assert out.returncode != 0, f"{entry} rendered"
    assert "exposure.selfRanges carries" in out.stderr, out.stderr
    assert "every address" in out.stderr, out.stderr


# --- allow-all: renders, and says so ---------------------------------------------
@pytest.mark.parametrize("value", [["0.0.0.0/0"], ["::/0"], ["203.0.113.7/32", "0.0.0.0/0"]])
def test_an_allow_all_list_renders_and_is_named(value: list[str]) -> None:
    args = allow("console", value)
    docs = render("aws", *args)
    assert envoy_service(docs)["annotations"]["dfe.hyperi.io/edge-fence"] == "console=allow-all,api=deny,admin=off"
    text = notes("aws", *args)
    assert "WARNING: exposure.groups.console.allow admits every address" in text, text


def test_allow_public_opens_the_admin_routes_behind_the_group_check() -> None:
    args = (*OIDC, "--set", "exposure.groups.admin.enabled=true", "--set", "exposure.groups.admin.allowPublic=true")
    docs = render("aws", *args)
    (policy,) = route_policies(docs, route(docs, "argocd"))
    (rule,) = policy["spec"]["authorization"]["rules"]
    assert "clientCIDRs" not in rule["principal"], rule
    assert rule["principal"]["jwt"]["claims"][0]["name"] == "groups"
    # kafbat takes no edge login, so its own policy has to admit every address
    # rather than fall through to the Gateway-level fence that refuses them all.
    (kafbat,) = route_policies(docs, route(docs, "kafbat"))
    assert kafbat["spec"]["authorization"] == {"defaultAction": "Allow"}, kafbat
    assert passes(docs, "kafbat", OFF_ALL)
    assert "WARNING: exposure.groups.admin.allowPublic admits every address" in notes("aws", *args)


# --- the ingest routes follow the ingest group ------------------------------------
@pytest.mark.parametrize("flavour", ["aws", "gcp", "azure"])
def test_expected_fail_an_ingest_route_with_no_list_refuses_every_address(flavour: str) -> None:
    """Empty is closed on an internet-facing gateway, the console's list included."""
    docs = render(flavour, *OTEL_ON, *RECEIVER_BEARER, *FENCE)
    for name in INGEST:
        (policy,) = route_policies(docs, route(docs, name))
        assert policy["metadata"]["annotations"]["dfe.hyperi.io/exposure-group"] == "ingest", policy
        assert admitted(policy) == [], policy
        assert not passes(docs, name, ON_CONSOLE), name
        assert not passes(docs, name, OFF_ALL), name


def test_an_ingest_route_admits_its_own_list_and_refuses_its_deny_list_first() -> None:
    docs = render("aws", *OTEL_ON, *RECEIVER_BEARER, *FENCE, *allow("ingest", SENDERS),
                  *deny("ingest", [f"{DENIED_SENDER}/32"]))
    for name in INGEST:
        assert passes(docs, name, ON_SENDERS), name
        assert not passes(docs, name, DENIED_SENDER), name
        assert not passes(docs, name, ON_CONSOLE), name
    # The ingest list reaches no web route.
    assert not passes(docs, "dfe-ui", ON_SENDERS)


def test_the_ingest_list_joins_the_load_balancer_only_while_an_ingest_route_renders() -> None:
    lists = (*FENCE, *allow("ingest", SENDERS))
    off = envoy_service(render("aws", *lists))["loadBalancerSourceRanges"]
    assert SENDERS[0] not in off, off
    on = envoy_service(render("aws", *OTEL_ON, *lists))["loadBalancerSourceRanges"]
    assert set(SENDERS + CONSOLE) <= set(on), on


def test_expected_fail_the_ingest_group_off_takes_both_ingest_routes_away() -> None:
    docs = render("aws", *OTEL_ON, *RECEIVER_BEARER, "--set", "exposure.groups.ingest.enabled=false")
    names = {r["metadata"]["name"] for r in of_kind(docs, "HTTPRoute")}
    assert not INGEST & names, sorted(names)


@pytest.mark.parametrize("door", ["loadbalancer", "tunnel"])
def test_expected_fail_the_receiver_route_renders_on_the_gateway_door_alone(door: str) -> None:
    """Every other door is the receiver's own Service or none, so a route here would be a second door."""
    docs = render("onprem", "--set", f"exposure.groups.ingest.door={door}", "--set", "routes.receiver.enabled=true")
    assert "receiver" not in {r["metadata"]["name"] for r in of_kind(docs, "HTTPRoute")}


def test_an_on_prem_ingest_deny_list_refuses_only_its_own_addresses() -> None:
    docs = render("onprem", *OTEL_ON, *deny("ingest", [f"{DENIED_SENDER}/32"]))
    assert not passes(docs, "receiver", DENIED_SENDER)
    assert passes(docs, "receiver", OFF_ALL)
    assert passes(docs, "otel", OFF_ALL)


def test_the_notes_say_the_ingest_routes_are_closed_and_warn_on_every_address() -> None:
    assert "INGEST CLOSED" in notes("aws", *OTEL_ON)
    assert "INGEST CLOSED" not in notes("aws")
    warned = notes("aws", *OTEL_ON, *allow("ingest", ["0.0.0.0/0"]))
    assert "WARNING: exposure.groups.ingest.allow admits every address (0.0.0.0/0)" in warned, warned


# --- off the internet: empty is no fence, a list fences its own group --------------
def test_an_on_prem_gateway_renders_no_fence() -> None:
    docs = render("onprem", *OIDC, *OTEL_ON)
    names = {p["metadata"]["name"] for p in of_kind(docs, "SecurityPolicy")}
    assert "dfe-edge-fence" not in names, sorted(names)
    assert not {n for n in names if n.startswith(("dfe-ingest-", "dfe-console-", "dfe-api-", "dfe-admin-"))}, \
        sorted(names)
    assert "dfe.hyperi.io/edge-fence" not in (envoy_service(docs).get("annotations") or {})
    assert notes("onprem").strip() == ""


def test_an_on_prem_list_fences_its_own_group_and_leaves_the_others_open() -> None:
    docs = render("onprem", *FENCE)
    assert not passes(docs, "dfe-ui", OFF_ALL)
    assert passes(docs, "dfe-ui", ON_CONSOLE)
    assert route_policies(docs, route(docs, "dfe-engine-cli")) == []
    assert route_policies(docs, route(docs, "kafbat")) == []
    assert envoy_service(docs)["annotations"]["dfe.hyperi.io/edge-fence"] == "console=listed,api=open,admin=open"


def test_a_quoted_internet_facing_switch_is_refused_by_name() -> None:
    out = helm("onprem", "--set-string", "envoyGateway.service.internetFacing=false")
    assert out.returncode != 0, "a quoted switch rendered"
    assert "envoyGateway.service.internetFacing is false (a string)" in out.stderr, out.stderr


# --- two OIDC providers: one policy per public route -----------------------------
PROVIDERS_TWO = [*PROVIDERS, {"name": "globex", "issuerUrl": "https://sso.example.org", "clientId": "dfe-globex"}]
OIDC_TWO = ("--set", "oidc.enabled=true", "--set-json", f"oidc.providers={json.dumps(PROVIDERS_TWO)}")
EVERYTHING_TWO = (*PUBLIC, *OIDC_TWO, *OTEL_ON, "--set", "exposure.groups.admin.enabled=true",
                  *allow("admin", ADMIN), "--set", "deployRepo.bundled=true",
                  "--set", "exposure.groups.admin.tools.argocd=true")


def public_policy(docs: list[dict], name: str) -> dict:
    (policy,) = route_policies(docs, route(docs, name))
    return policy


@pytest.mark.parametrize("fence", [(), FENCE], ids=["no console list", "console list"])
def test_two_providers_leave_every_web_route_one_policy_and_fenced(fence: tuple[str, ...]) -> None:
    """effective() refuses a route carrying two policies, which is what one per provider was."""
    docs = render("aws", *EVERYTHING_TWO, *fence)
    for r in web_routes(docs):
        name = r["metadata"]["name"]
        rules = admitted(effective(docs, r))
        group = group_of(name)
        if group == "admin":
            assert rules, name
            assert all(cidrs == ADMIN for cidrs in rules), name
        elif group == "console" and fence:
            assert rules, name
            assert all(cidrs == CONSOLE for cidrs in rules), name
        else:
            assert rules == [], name


def test_every_provider_stays_an_issuer_and_the_login_provider_owns_the_redirect() -> None:
    docs = render("aws", *EVERYTHING_TWO, *FENCE, "--set", "oidc.loginProvider=globex")
    for name in ("dfe-engine-public", "argocd-public"):
        policy = public_policy(docs, name)
        assert policy["metadata"]["name"] == f"dfe-public-oidc-globex-{name.removesuffix('-public')}"
        assert policy["spec"]["oidc"]["clientID"] == "dfe-globex"
        issuers = sorted(p["name"] for p in policy["spec"]["jwt"]["providers"])
        assert issuers == ["acme", "globex"], (name, issuers)
    rules = public_policy(docs, "argocd-public")["spec"]["authorization"]["rules"]
    assert sorted(rule["principal"]["jwt"]["provider"] for rule in rules) == ["acme", "globex"]
    for rule in rules:
        assert rule["principal"]["clientCIDRs"] == ADMIN
        assert rule["principal"]["jwt"]["claims"][0]["name"] == "groups"


# --- the :80 redirect is not fenced -----------------------------------------------
@pytest.mark.parametrize("fence", [(), FENCE], ids=["no list", "list"])
def test_the_http_redirect_answers_everyone_with_its_301(fence: tuple[str, ...]) -> None:
    """It serves nothing but the move to https, where the fence holds."""
    docs = render("aws", *fence)
    redirect = route(docs, REDIRECT)
    (policy,) = route_policies(docs, redirect)
    assert policy["spec"]["authorization"] == {"defaultAction": "Allow"}, policy
    assert redirect["spec"]["rules"][0]["filters"][0]["type"] == "RequestRedirect"
    assert "backendRefs" not in redirect["spec"]["rules"][0]


# --- a malformed entry fails the render, not the sync ------------------------------
@pytest.mark.parametrize("entry", [
    "203.0.113.7", "203.0.113.7/33", "300.1.1.1/32", "203.0.113.0/24junk",
    "2001:db8::/129", "2001:db8::", "fe80::1%eth0/64", "not-a-cidr",
])
@pytest.mark.parametrize(("group", "field"), [("console", "allow"), ("api", "deny"), ("admin", "allow")])
def test_a_malformed_group_list_entry_is_refused_by_name(entry: str, group: str, field: str) -> None:
    out = helm("aws", *group_list(group, field, ["198.51.100.0/24", entry]))
    assert out.returncode != 0, f"{entry} rendered"
    assert f'exposure.groups.{group}.{field} carries "{entry}", which is not a CIDR range' in out.stderr, out.stderr


def test_a_malformed_trusted_proxy_entry_is_refused_by_name() -> None:
    out = helm("onprem", *FENCE, "--set", "ui.trusted_proxy_cidrs=10.0.0.1")
    assert out.returncode != 0, "a bare trusted proxy address rendered"
    assert 'ui.trusted_proxy_cidrs carries "10.0.0.1"' in out.stderr, out.stderr


@pytest.mark.parametrize("entry", [
    "203.0.113.7/32", "10.0.0.0/8", "0.0.0.0/0", "2001:db8::/64", "::/0", "::ffff:203.0.113.7/128",
])
def test_every_cidr_shape_envoy_gateway_accepts_renders(entry: str) -> None:
    assert helm("aws", *allow("console", [entry])).returncode == 0, entry


# --- HyperDX beside the console ---------------------------------------------------
@pytest.mark.parametrize("flavour", ["aws", "gcp", "azure"])
def test_hyperdx_renders_wherever_the_console_does(flavour: str) -> None:
    """The Observe page frames it, so the switch that removes the admin UIs leaves it."""
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
    """Whether an address-only SecurityPolicy lets a request from this address through.

    Rules are evaluated in order and the first match decides; no match takes the
    default action.
    """
    auth = policy["spec"]["authorization"]
    ip = ipaddress.ip_address(address)
    for rule in auth.get("rules") or []:
        principal = rule.get("principal") or {}
        assert principal.keys() == {"clientCIDRs"}, (policy["metadata"]["name"], rule)
        if any(ip.version == ipaddress.ip_network(cidr).version and ip in ipaddress.ip_network(cidr)
               for cidr in principal["clientCIDRs"]):
            return rule["action"] == "Allow"
    return auth["defaultAction"] == "Allow"


def passes(docs: list[dict], route_name: str, peer: str, forwarded: tuple[str, ...] = ()) -> bool:
    """One request to an address-only route: its listener names the client, its policy judges it."""
    r = route(docs, route_name)
    (parent,) = r["spec"]["parentRefs"]
    address = client_address(listener_policy(docs, parent["sectionName"]), peer, list(forwarded))
    policy = effective(docs, r)
    if policy is None:
        return True
    if "authorization" not in policy["spec"]:
        return True
    return admits(policy, address)


@pytest.mark.parametrize("flavour", ["aws", "gcp", "azure"])
def test_an_allow_list_renders_with_no_trusted_proxy_range(flavour: str) -> None:
    """The public listeners judge the TCP peer, so a list needs no proxy range beside it."""
    docs = render(flavour, *PUBLIC, *FENCE)
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
    docs = render("aws", *PUBLIC, *FENCE)
    for name in ("dfe-ui-public",):
        assert not passes(docs, name, peer, forwarded), (name, peer, forwarded)
        assert passes(docs, name, "203.0.113.7", forwarded), name


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
    docs = render("aws", *PUBLIC, *FENCE, *trust(CDN))
    detection = listener_policy(docs, "https-public-dfe-ui")["spec"]["clientIPDetection"]
    assert detection == {"xForwardedFor": {"trustedCIDRs": CDN}}
    assert passes(docs, "dfe-ui-public", peer, forwarded) is admitted


@pytest.mark.parametrize("cdn", [[], CDN], ids=["no cdn", "cdn"])
def test_internal_listeners_never_read_the_forwarded_header(cdn: list[str]) -> None:
    docs = render("aws", *PUBLIC, *FENCE, *(trust(cdn) if cdn else ()))
    assert "clientIPDetection" not in listener_policy(docs, "https")["spec"]
    (public,) = [p for p in of_kind(docs, "ClientTrafficPolicy") if p["metadata"]["name"] == "dfe-public-edge"]
    assert [ref.get("sectionName") for ref in public["spec"]["targetRefs"]] == ["https-public-dfe-ui"]
    assert not passes(docs, "dfe-ui", "192.0.2.10", ("203.0.113.7",))
    assert not passes(docs, "dfe-ui", FORWARDER, ("203.0.113.7",))
    assert passes(docs, "dfe-ui", "203.0.113.7")


def identity(doc: dict) -> tuple[str, str, str]:
    return doc["kind"], doc["metadata"]["name"], doc["metadata"].get("namespace", "")


def test_naming_a_cdn_changes_nothing_but_the_public_listener_policy() -> None:
    base = render("aws", *PUBLIC, *FENCE)
    with_cdn = render("aws", *PUBLIC, *FENCE, *trust(CDN))
    changed = sorted(identity(d) for d in with_cdn if d not in base)
    assert changed == [("ClientTrafficPolicy", "dfe-public-edge", "envoy-gateway-system")], changed
    assert len(with_cdn) == len(base)


def test_the_notes_name_the_cdn_opt_in() -> None:
    plain = " ".join(notes("aws", *PUBLIC, *FENCE).split())
    assert "ignore X-Forwarded-For" in plain, plain
    assert "goes in ui.trusted_proxy_cidrs" in plain, plain
    named = " ".join(notes("aws", *PUBLIC, *FENCE, *trust(CDN)).split())
    assert f"ui.trusted_proxy_cidrs ({','.join(CDN)})" in named, named


# --- the connection limit holds on every listener ----------------------------------
def test_every_listener_carries_the_connection_limit() -> None:
    """A listener-scoped policy replaces the Gateway one, so both restate it."""
    docs = render("aws", *PUBLIC, *FENCE)
    for policy in of_kind(docs, "ClientTrafficPolicy"):
        if policy["metadata"]["name"] in ("dfe-edge-tls", "dfe-public-edge"):
            assert policy["spec"]["connection"] == {"connectionLimit": {"value": 10000}}, policy


@pytest.mark.parametrize("value", ["0", "-5", "1.5"])
def test_expected_fail_a_connection_limit_below_one_or_fractional_is_refused(value: str) -> None:
    out = helm("aws", "--set-json", f"envoyGateway.connectionLimit.value={value}")
    assert out.returncode != 0, f"{value} rendered"
    assert "envoyGateway.connectionLimit.value" in out.stderr, out.stderr


# --- internal names on an internet-facing Service take the rate limit ---------------
def test_every_internal_web_route_on_an_internet_facing_service_is_rate_limited() -> None:
    docs = render("aws", *EVERY_GROUP)
    limited = {
        ref["name"] for policy in of_kind(docs, "BackendTrafficPolicy")
        for ref in policy["spec"]["targetRefs"] if "rateLimit" in policy["spec"]
    }
    for r in web_routes(docs):
        assert r["metadata"]["name"] in limited, r["metadata"]["name"]
    assert not INGEST & limited


def test_internal_routes_off_the_internet_take_no_rate_limit() -> None:
    names = {p["metadata"]["name"] for p in of_kind(render("onprem"), "BackendTrafficPolicy")}
    assert not {n for n in names if n.startswith("dfe-internal-")}, sorted(names)


# --- every web route counts per client address, public ones behind a shared wall ----
FAMILIES = ["0.0.0.0/0", "::/0"]
PER_CLIENT_DEFAULT = {"requests": 1200, "unit": "Minute"}
SHARED_DEFAULT = {"requests": 6000, "unit": "Minute"}
EVERY_PATH = [{"path": {"type": "PathPrefix", "value": "/"}}]
PER_CLIENT_SELECTORS = [[{"sourceCIDR": {"type": "Distinct", "value": family}}] for family in FAMILIES]
WALL = (0, "every caller")


def rate_limit(docs: list[dict], prefix: str, route: str) -> dict:
    (policy,) = [
        p for p in of_kind(docs, "BackendTrafficPolicy")
        if p["metadata"]["name"].startswith(prefix)
        and [ref["name"] for ref in p["spec"]["targetRefs"]] == [route]
    ]
    return policy


def internal_rate_limit(docs: list[dict], route: str) -> dict:
    return rate_limit(docs, "dfe-internal-", route)


def public_rate_limit(docs: list[dict], route: str) -> dict:
    return rate_limit(docs, "dfe-public-", route)


def rules_of(policy: dict) -> list[dict]:
    return policy["spec"]["rateLimit"]["local"]["rules"]


def buckets(policy: dict, client: str) -> set[tuple[int, str]]:
    """Every bucket a request from `client` spends a token from.

    Follows Envoy Gateway v1.9.2 on Envoy v1.39.2. Every rule whose selector
    matches is spent, and a Distinct sourceCIDR rule keeps one bucket per address
    (internal/xds/translator/ratelimit.go). A rule with no selector becomes the
    route's default bucket, which is spent only when no other rule matched,
    because local_ratelimit.go sets AlwaysConsumeDefaultTokenBucket to false.
    """
    address = ipaddress.ip_address(client)
    spent: set[tuple[int, str]] = set()
    default = None
    for index, rule in enumerate(rules_of(policy)):
        selectors = rule.get("clientSelectors", [])
        if not selectors:
            default = index
            continue
        (selector,) = selectors
        if "path" in selector:
            assert selector == EVERY_PATH[0], selector
            spent.add((index, "every caller"))
            continue
        assert set(selector) == {"sourceCIDR"}, selector
        source = selector["sourceCIDR"]
        network = ipaddress.ip_network(source["value"])
        if address.version == network.version and address in network:
            spent.add((index, client if source.get("type") == "Distinct" else source["value"]))
    if not spent and default is not None:
        spent.add((default, "every caller"))
    return spent


def test_every_internal_rate_limit_counts_per_client_address() -> None:
    docs = render("aws", *EVERY_GROUP)
    internal = [r["metadata"]["name"] for r in web_routes(docs) if not r["metadata"]["name"].endswith("-public")]
    assert {"hyperdx", "dfe-engine-cli", "kafbat"} <= set(internal), internal
    for name in internal:
        rules = internal_rate_limit(docs, name)["spec"]["rateLimit"]["local"]["rules"]
        assert [rule["clientSelectors"] for rule in rules] == [
            [{"sourceCIDR": {"type": "Distinct", "value": family}}] for family in FAMILIES
        ], name
        assert all(rule["limit"] == PER_CLIENT_DEFAULT for rule in rules), rules


def test_one_hyperdx_caller_cannot_empty_the_route_for_another() -> None:
    """HyperDX on cloud answers only on its internal name, so this bucket is all of its traffic."""
    policy = internal_rate_limit(render("aws", *FENCE), "hyperdx")
    first, second = buckets(policy, ON_CONSOLE), buckets(policy, "203.0.113.7")
    assert first, "an IPv4 caller is unlimited"
    assert second, "an IPv4 caller is unlimited"
    assert not first & second, (first, second)
    assert buckets(policy, "2001:db8::1"), "an IPv6 caller is unlimited"
    assert all(rule["limit"] == PER_CLIENT_DEFAULT for rule in policy["spec"]["rateLimit"]["local"]["rules"])


def test_expected_fail_an_exact_source_rule_puts_every_caller_in_one_bucket() -> None:
    """The control for the model above: a shared source rule is caught as shared."""
    policy = internal_rate_limit(render("aws", *FENCE), "hyperdx")
    for rule in rules_of(policy):
        rule["clientSelectors"][0]["sourceCIDR"]["type"] = "Exact"
    assert buckets(policy, ON_CONSOLE) & buckets(policy, "203.0.113.7")


def test_every_public_route_counts_per_client_behind_a_shared_wall() -> None:
    docs = render("aws", *EVERY_GROUP)
    public = [r["metadata"]["name"] for r in web_routes(docs) if r["metadata"]["name"].endswith("-public")]
    assert {"dfe-ui-public", "dfe-engine-public", "dfe-engine-cli-public", "kafbat-public"} <= set(public), public
    for name in public:
        rules = rules_of(public_rate_limit(docs, name))
        assert [rule["clientSelectors"] for rule in rules] == [EVERY_PATH, *PER_CLIENT_SELECTORS], name
        assert [rule["limit"] for rule in rules] == [SHARED_DEFAULT, PER_CLIENT_DEFAULT, PER_CLIENT_DEFAULT], name


def test_public_callers_share_the_wall_and_nothing_else() -> None:
    """dfe-engine-public carries every console user's browser API calls."""
    policy = public_rate_limit(render("aws", *PUBLIC, *FENCE), "dfe-engine-public")
    first, second = buckets(policy, ON_CONSOLE), buckets(policy, "203.0.113.7")
    assert first & second == {WALL}, (first, second)
    assert len(first) == len(second) == 2, (first, second)
    ipv6 = buckets(policy, "2001:db8::1")
    assert WALL in ipv6, ipv6
    assert len(ipv6) == 2, ipv6


def test_expected_fail_a_wall_with_no_selector_is_never_spent_beside_per_client_rules() -> None:
    """Why the wall is a path rule: as Envoy's default bucket it would hold back nobody."""
    policy = public_rate_limit(render("aws", *PUBLIC, *FENCE), "dfe-engine-public")
    rules_of(policy)[0].pop("clientSelectors")
    assert not buckets(policy, ON_CONSOLE) & buckets(policy, "203.0.113.7")
    for rendered in of_kind(render("aws", *EVERY_GROUP), "BackendTrafficPolicy"):
        if "rateLimit" in rendered["spec"]:
            assert all(rule.get("clientSelectors") for rule in rules_of(rendered)), rendered["metadata"]["name"]


def test_each_budget_is_its_own_key() -> None:
    docs = render("aws", *PUBLIC, *FENCE, "--set", "ui.rate_limit.per_client_requests=50", "--set", "ui.rate_limit.requests=500")
    internal = [rule["limit"] for rule in rules_of(internal_rate_limit(docs, "hyperdx"))]
    assert internal == [{"requests": 50, "unit": "Minute"}] * 2, internal
    public = [rule["limit"] for rule in rules_of(public_rate_limit(docs, "dfe-ui-public"))]
    assert public == [{"requests": 500, "unit": "Minute"}, *[{"requests": 50, "unit": "Minute"}] * 2], public


@pytest.mark.parametrize("field", ["requests", "per_client_requests"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "4294967296", '"1200"', "null"])
def test_expected_fail_a_budget_outside_envoys_range_is_refused(field: str, value: str) -> None:
    out = helm("aws", "--set-json", f"ui.rate_limit.{field}={value}")
    assert out.returncode != 0, f"{field}={value} rendered"
    assert f"ui.rate_limit.{field} is" in out.stderr, out.stderr


def limits(shared: int, per_client: int) -> tuple[str, ...]:
    return ("--set", f"ui.rate_limit.requests={shared}", "--set", f"ui.rate_limit.per_client_requests={per_client}")


@pytest.mark.parametrize(("shared", "per_client"), [(600, 1200), (1200, 1200)])
def test_expected_fail_a_wall_at_or_below_one_callers_budget_is_refused(shared: int, per_client: int) -> None:
    """Envoy spends the lowest fill rate first, so that wall is spent first and one caller can empty it."""
    out = helm("aws", *limits(shared, per_client))
    assert out.returncode != 0, f"requests={shared} with per_client_requests={per_client} rendered"
    assert "the shared bucket must be larger than one caller's" in out.stderr, out.stderr


def test_a_wall_above_one_callers_budget_renders() -> None:
    rules = rules_of(public_rate_limit(render("aws", *PUBLIC, *FENCE, *limits(1201, 1200)), "dfe-ui-public"))
    assert [rule["limit"]["requests"] for rule in rules] == [1201, 1200, 1200], rules


# --- an engine route always names its paths --------------------------------------
ENGINE_EMPTY = {
    "dfeEngine": ("--set-json", "routes.dfeEngine.browserFamilies=[]", "--set-json", "routes.dfeEngine.browserPaths=[]"),
    "dfeEngineCli": (
        "--set-json", "routes.dfeEngineCli.cliFamilies=[]", "--set-json", "routes.dfeEngineCli.cliPaths=[]",
        "--set", "api.docsEnabled=false",
    ),
}
ENGINE_ROUTE = {"dfeEngine": "dfe-engine", "dfeEngineCli": "dfe-engine-cli"}


@pytest.mark.parametrize("flavour", ["aws", "onprem"])
@pytest.mark.parametrize("key", sorted(ENGINE_EMPTY))
def test_expected_fail_an_engine_route_with_no_internal_path_is_refused(flavour: str, key: str) -> None:
    """A rule with no matches is PathPrefix /: every path on the dfe hostname, in this route's group."""
    out = helm(flavour, *ENGINE_EMPTY[key])
    assert out.returncode != 0, f"{key} rendered with no path on {flavour}"
    assert f"routes.{key} matches no path on its internal route" in out.stderr, out.stderr


@pytest.mark.parametrize("key", sorted(ENGINE_EMPTY))
def test_an_engine_route_switched_off_may_carry_no_path(key: str) -> None:
    docs = render("onprem", *ENGINE_EMPTY[key], "--set", f"routes.{key}.enabled=false")
    assert ENGINE_ROUTE[key] not in {r["metadata"]["name"] for r in of_kind(docs, "HTTPRoute")}


# --- the admin allowPublic refusal says which routes the edge login fronts ---------
def test_the_allow_public_refusal_names_the_routes_the_edge_login_fronts() -> None:
    routes = yaml.safe_load((GATEWAY / "values.yaml").read_text(encoding="utf-8"))["routes"]
    infra = {key: r for key, r in routes.items() if r.get("class", "infra") == "infra"}
    fronted = sorted(key for key, r in infra.items() if r.get("edgePolicy"))
    own_only = sorted(key for key, r in infra.items() if not r.get("edgePolicy") and r.get("ownLogin"))
    assert "kafbat" in own_only, own_only
    out = helm("aws", "--set", "exposure.groups.admin.enabled=true", "--set", "exposure.groups.admin.allowPublic=true",
               "--set", "oidc.enabled=false")
    assert out.returncode != 0, "admin allowPublic rendered with no edge OIDC provider"
    plain = " ".join(out.stderr.split())
    assert f"edgePolicy true ({', '.join(fronted)})" in plain, plain
    assert f"edgePolicy false ({', '.join(own_only)}) stay behind their own login" in plain, plain


# --- the receiver route on an internet-facing Service ------------------------------
@pytest.mark.parametrize("mode", [None, "none", "mtls", "both"])
def test_the_receiver_route_is_refused_on_an_internet_facing_service_without_caller_auth(mode: str | None) -> None:
    out = helm("aws", *RECEIVER_ON, *(("--set", f"routes.receiver.authMode={mode}") if mode else ()))
    assert out.returncode != 0, f"authMode {mode} rendered"
    assert "exposure.groups.ingest.door is gateway on an internet-facing Service" in out.stderr, out.stderr


@pytest.mark.parametrize("flavour", ["aws", "gcp", "azure"])
@pytest.mark.parametrize("mode", ["header", "bearer", "Bearer"])
def test_the_receiver_route_renders_once_its_caller_auth_is_declared(flavour: str, mode: str) -> None:
    docs = render(flavour, *RECEIVER_ON, "--set", f"routes.receiver.authMode={mode}")
    assert "receiver" in {r["metadata"]["name"] for r in of_kind(docs, "HTTPRoute")}


def test_the_receiver_route_is_the_on_prem_default_door() -> None:
    """local.yaml sets door gateway, so the route renders with no switch of its own."""
    docs = render("onprem")
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
    out = helm(flavour, *PUBLIC, *FENCE, *trust(trusted))
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
