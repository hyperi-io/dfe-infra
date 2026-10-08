#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/dfe_ops_edge_probe.py
#  Purpose:      `dfe-ops edge-probe` -- prove from the operator's own machine,
#                from OUTSIDE the cluster, which doors the edge module actually
#                opens: the TLS floor, HSTS, the rate limit, the CIDR filter,
#                the receiver, the otel route, every admin UI route, the product
#                login and the engine API path families that sit on the product's
#                own hostname. Also `dfe-ops admin-probe`, which proves every
#                admin UI the gateway lists loads through it. Split into its own
#                module the way dfe_ops_bastion.py is, and imported into
#                dfe-ops's build_parser() the same way.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-ops edge-probe -- what an outside caller can reach, proven from outside.

    dfe-ops edge-probe [--dial deployment.yaml] [--target <host or address>]

Every check reads its expectation from the dial's `edge:` block and dials the
published name to see whether the deployment agrees. A render assertion cannot
do this: scripts/test-route-exposure.sh proves which objects a chart emits, and
this proves what answers once they are programmed onto a real load balancer.

The probe sends NO credential. Each check is what an unauthenticated caller on
the public internet sees, which is the only thing that can be claimed from here.

`--target` maps the published name onto the address the gateway is programmed
on, for a deployment whose DNS does not resolve here yet -- the same idea
`dfe-ops acceptance` spells as `--resolve HOST:IP`. The name still rides the
Host header and the SNI, so the deployment answers as it would for a browser.

Each check reports PASS, FAIL or SKIP with one evidence line, and the verb exits
non-zero when any check FAILs. A SKIP is not a failure: it is a check whose
precondition this deployment does not meet, and it names which one.

    dfe-ops admin-probe [--namespace dfe-local] [--target <gateway address>] [--wait 0]

The same boundary, pointed at the admin UIs instead: it reads the list the
gateway renders (the engine's dfe-admin-links ConfigMap) and GETs each one
through the gateway address, following redirects. A redirect loop, a 5xx, a 4xx
other than 401/403/404, or no answer FAILs; a redirect off the deployment's domain
is a login handed to an IdP and passes. The readiness gate runs it once every pod
is Ready.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import socket
import ssl
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import SplitResult, urljoin, urlsplit

from kubectl_cli import run_kubectl
from render_dial import _EDGE_ALIASES
from yaml_subset import YamlSubsetError, at, split_list
from yaml_subset import parse as parse_yaml_subset

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAL = REPO_ROOT / "deployment.yaml"
DIAL_TEMPLATE = REPO_ROOT / "deployment.example.yaml"

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

# Nothing here may outlive a person watching it, so every dial carries the same
# bound and a door that black-holes packets reports as unreachable rather than
# hanging the run.
DEFAULT_TIMEOUT = 8.0

HTTPS_PORT = 443
HTTP_PORT = 80

# The receiver's exposed listener on a fresh deploy: http, the JSON ingest door.
# Its push listener is never exposed, so a probe of it would prove nothing about
# the door.
RECEIVER_INGEST_PORTS = (8080,)

# The admin UIs the edge module offers a public hostname, in the order
# render_dial.py's ADMIN_UIS reports them, against the subdomain label each one
# answers on (argocd/values/common.yaml `hostnames:`).
ADMIN_UI_HOSTNAMES = {
    "kafbat": "kafbat",
    "cruise_control": "cruise-control",
    "hyperdx": "hyperdx",
    "argocd": "argocd",
    "links": "links",
    "forgejo": "git",
}
ADMIN_UIS = tuple(ADMIN_UI_HOSTNAMES)

# The product surface, the ingest door and the platform OTLP door, off the same
# canonical hostname map (argocd/values/common.yaml `hostnames`).
PRODUCT_LABEL = "dfe"
RECEIVER_LABEL = "receiver"
OTEL_LABEL = "otel"

# A real OTLP/HTTP path. The collector checks the bearer token before it routes
# or checks the method, so a GET with no token answers 401 when the door is shut
# to strangers and anything else when it is not.
OTEL_PROBE_PATH = "/v1/logs"

# dfe-ui serves its sign-in page here (scripts/acceptance/onboarding/run.py
# drives the same path), so this is the one route that must answer.
LOGIN_PATH = "/login"

# One path per group of the engine's API, on the product's own hostname. Each is
# a real unauthenticated endpoint, so a 404 here is the GATEWAY saying it carries
# no route for the path rather than the engine saying it has no such handler.
#   browser -- the auth family's setup document, which dfe-ui itself reads first
#   cli     -- the queries family, private until cli_families_public opens it
#   spec    -- the OpenAPI document the `dfe` CLI is generated from
#   docs    -- the Swagger surface, never public under any flag
#   jwks    -- the signing keys, public by design so peers can verify DFE tokens
ENGINE_BROWSER_PATH = "/api/v1/auth/setup-status"
ENGINE_CLI_PATH = "/api/v1/queries"
ENGINE_SCIM_PATH = "/api/v1/scim/v2/Users"
ENGINE_SPEC_PATH = "/openapi.json"
ENGINE_DOCS_PATH = "/docs"
ENGINE_JWKS_PATH = "/.well-known/jwks.json"

# A local rate limit counts per route per proxy replica, so proving it costs one
# request per unit of burst. Past this many the probe says so rather than
# spending an operator's morning on a limit sized for a day.
RATE_LIMIT_PROBE_CEILING = 1000

# The protocol names ssl reports, weakest first, so a floor is an index compare.
TLS_ORDER = ("SSLv3", "TLSv1", "TLSv1.1", "TLSv1.2", "TLSv1.3")
# What a handshake must be capped at to sit one step BELOW each floor.
BELOW_FLOOR = {"1.2": "TLSv1.1", "1.3": "TLSv1.2"}
TLS_VERSIONS = {
    "TLSv1.1": ssl.TLSVersion.TLSv1_1,
    "TLSv1.2": ssl.TLSVersion.TLSv1_2,
}


class EdgeProbeError(RuntimeError):
    """The probe cannot proceed -- the message is what dfe-ops prints."""


# --- the dial ----------------------------------------------------------------
# The `edge:` block is the expectation every check is measured against. Defaults
# match render_dial.py's _EDGE_BOOL_DEFAULTS and _EDGE_ENUM_DEFAULTS, so a dial
# that omits a key is probed for what that deployment actually gets.


@dataclass(frozen=True)
class EdgeSettings:
    """The `edge:` block, as the checks need it."""

    enabled: bool = True
    flavour: str = ""
    product_public: bool = True
    domain: str = ""
    tls_min_version: str = "1.2"
    hsts: bool = True
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 300
    rate_limit_unit: str = "Minute"
    allowed_cidrs: tuple[str, ...] = ()
    admin_uis_external: bool = False
    admin_uis_public: dict[str, bool] = field(default_factory=dict)
    receiver_mode: str = ""
    otel_enabled: bool = False
    # k8s.domain -- the internal wildcard the otel route answers on, which is not
    # the public zone edge.product.domain names.
    cluster_domain: str = ""
    engine_with_product: bool = True
    engine_cli_families_public: bool = False
    engine_scim_public: bool = False


def _scalar(tree: dict[str, object], path: tuple[str, ...]) -> str | None:
    """The non-empty scalar at `path`, else at its deprecated spelling.

    The renderer reads the old `ui:` and `ingest:` paths for one release, so a
    dial still on them renders a real deployment. A probe that read only the new
    block would skip every check against it and exit zero, which is the one
    answer a gate must never get from a door it did not look at.
    """
    for candidate in (path, _EDGE_ALIASES.get(path)):
        if candidate is None:
            continue
        node = at(tree, candidate)
        if isinstance(node, str) and node.strip():
            return node.strip()
    return None


def _flag(tree: dict[str, object], path: tuple[str, ...], default: bool) -> bool:
    """One true/false dial field, refusing anything else by name.

    The edge block writes its booleans unquoted so a deployer can paste them into
    a real values file, and the restricted reader hands both spellings back as
    the same string.
    """
    value = _scalar(tree, path)
    if value is None:
        return default
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise EdgeProbeError(f"{'.'.join(path)} must be true or false, got {value!r}")


def _number(tree: dict[str, object], path: tuple[str, ...], default: int) -> int:
    value = _scalar(tree, path)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise EdgeProbeError(f"{'.'.join(path)} must be a whole number, got {value!r}") from error


def parse_edge(tree: dict[str, object]) -> EdgeSettings:
    """Turn a parsed dial into the expectation the checks measure against."""
    return EdgeSettings(
        enabled=_flag(tree, ("edge", "enabled"), True),
        flavour=_scalar(tree, ("edge", "flavour")) or "",
        product_public=_flag(tree, ("edge", "product", "public"), True),
        domain=_scalar(tree, ("edge", "product", "domain")) or "",
        tls_min_version=_scalar(tree, ("edge", "product", "tls", "min_version")) or "1.2",
        hsts=_flag(tree, ("edge", "product", "tls", "hsts"), True),
        rate_limit_enabled=_flag(tree, ("edge", "product", "rate_limit", "enabled"), True),
        rate_limit_requests=_number(tree, ("edge", "product", "rate_limit", "requests"), 300),
        rate_limit_unit=_scalar(tree, ("edge", "product", "rate_limit", "unit")) or "Minute",
        # Through _scalar so the deprecated spelling is read too, and stripped of
        # the brackets an inline list carries, which the dial writes elsewhere.
        allowed_cidrs=split_list(
            (_scalar(tree, ("edge", "product", "allowed_cidrs")) or "").strip().lstrip("[").rstrip("]")
        ),
        admin_uis_external=_flag(tree, ("edge", "admin_uis", "external"), False),
        admin_uis_public={
            ui: _flag(tree, ("edge", "admin_uis", "public", ui), False) for ui in ADMIN_UIS
        },
        receiver_mode=_scalar(tree, ("edge", "ingest", "receiver", "mode")) or "",
        otel_enabled=_flag(tree, ("edge", "ingest", "otel", "enabled"), False),
        cluster_domain=_scalar(tree, ("k8s", "domain")) or "",
        engine_with_product=_flag(tree, ("edge", "engine_api", "with_product"), True),
        engine_cli_families_public=_flag(
            tree, ("edge", "engine_api", "cli_families_public"), False
        ),
        engine_scim_public=_flag(tree, ("edge", "engine_api", "scim_public"), False),
    )


def read_dial(path: Path) -> EdgeSettings:
    """Read one deployment dial, refusing an absent or unparsable one by name."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise EdgeProbeError(
            f"no deployment dial at {path} -- copy {DIAL_TEMPLATE.name} to "
            f"{DIAL.name} and populate its edge: block, or pass --dial"
        ) from error
    try:
        tree = parse_yaml_subset(text, source=str(path))
    except YamlSubsetError as error:
        raise EdgeProbeError(f"{path} cannot be read: {error}") from error
    return parse_edge(tree)


# --- names and addresses -----------------------------------------------------


def product_host(domain: str) -> str:
    """The name dfe-ui and the engine API share, or empty with no public zone."""
    return f"{PRODUCT_LABEL}.{domain}" if domain else ""


def published_host(label: str, domain: str) -> str:
    return f"{label}.{domain}" if domain else ""


def _resolved(host: str) -> set[str]:
    """Every address this machine resolves the host to, empty when it cannot.

    An address resolves to itself, so a `--target` and a published name can be
    compared through this without the caller knowing which it holds.
    """
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return set()


def _resolves(host: str) -> bool:
    """Whether this machine can resolve the host at all."""
    return bool(_resolved(host))


def dial_address(host: str, target: str) -> str:
    """What to dial for `host`: the operator's override, else the name itself.

    A deployment publishes its names through external-dns, and a probe run
    minutes after the apply is the case this exists for -- the gateway is
    programmed and the record has not propagated. `--target` is that address.
    """
    if target:
        return target
    if not host:
        raise EdgeProbeError(
            "the dial sets no edge.product.domain, so there is no published name to "
            "dial -- pass --target <gateway address>"
        )
    if _resolves(host):
        return host
    raise EdgeProbeError(
        f"{host} does not resolve here -- pass --target <gateway address> to dial the "
        f"gateway directly, the way `dfe-ops acceptance --resolve` does"
    )


# --- the network boundary ----------------------------------------------------
# ONE function every check dials through, so a test stubs exactly this and no
# check reaches a real deployment -- the same shape aws_cli.run_aws is for the
# AWS CLI half of dfe_ops_bastion.py.


@dataclass(frozen=True)
class Request:
    """One connection a check makes.

    `host` is the published name, carried in the Host header and the SNI;
    `address` is what is actually dialled, which differs whenever --target maps
    the name onto the gateway. An empty `path` connects and closes without
    sending a request, which is how a port is probed for reachability alone.
    `tls_cap` pins the handshake to one version, to prove a floor refuses below it.
    """

    host: str
    address: str
    port: int = HTTPS_PORT
    path: str = "/"
    tls: bool = True
    tls_cap: str = ""
    timeout: float = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class Answer:
    """What one connection produced.

    `client_capped` separates "this machine cannot even offer that protocol
    version" from "the deployment refused it" -- without it, a client too modern
    to speak the old version would read as proof the floor holds.
    """

    reached: bool
    status: int | None = None
    protocol: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    source: str = ""
    error: str = ""
    client_capped: bool = False


def _tls_context(cap: str = "") -> ssl.SSLContext:
    """The client context one dial uses, optionally pinned below the floor.

    Verification is off by design: the probe dials an address the published name
    may not resolve to yet, and a public chain can be issued by a CA this machine
    does not carry -- neither says anything about which doors answer, which is
    the only question here. `cap` pins both ends of the version range, so the
    handshake offers ONLY the version the floor exists to refuse, and OpenSSL
    will not offer a withdrawn protocol until its security level is lowered.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    if cap:
        version = TLS_VERSIONS[cap]
        context.minimum_version = version
        context.maximum_version = version
        context.set_ciphers("DEFAULT:@SECLEVEL=0")
    return context


def _reach(request: Request) -> Answer:
    """Dial once and report what came back. The ONE network boundary."""
    context = None
    if request.tls:
        try:
            context = _tls_context(request.tls_cap)
        except (ssl.SSLError, ValueError, KeyError) as error:
            return Answer(
                reached=False,
                client_capped=bool(request.tls_cap),
                error=f"this machine cannot offer {request.tls_cap or 'TLS'}: {error}",
            )

    connection: http.client.HTTPConnection | None = None
    try:
        sock = socket.create_connection((request.address, request.port), timeout=request.timeout)
        source = sock.getsockname()[0]
        if context is not None:
            sock = context.wrap_socket(sock, server_hostname=request.host or request.address)
        protocol = (sock.version() or "") if context is not None else ""
        if not request.path:
            sock.close()
            return Answer(reached=True, protocol=protocol, source=source)
        connection = http.client.HTTPConnection(
            request.host or request.address, request.port, timeout=request.timeout
        )
        connection.sock = sock
        connection.request("GET", request.path, headers={"Host": request.host or request.address})
        response = connection.getresponse()
        response.read()
        return Answer(
            reached=True,
            status=response.status,
            protocol=protocol,
            headers={name.lower(): value for name, value in response.getheaders()},
            source=source,
        )
    except (OSError, http.client.HTTPException) as error:
        return Answer(reached=False, error=str(error) or type(error).__name__)
    finally:
        if connection is not None:
            connection.close()


# --- the verdicts ------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One check's verdict and the single line of evidence behind it."""

    name: str
    verdict: str
    evidence: str


def check_names() -> tuple[str, ...]:
    """Every check this verb reports, in the order it reports them."""
    return (
        "tls floor",
        "hsts",
        "rate limit",
        "cidr filter",
        "receiver private",
        "otel ingress",
        *(f"admin ui {ui}" for ui in ADMIN_UIS),
        "ui login",
        *(name for name, _ in ENGINE_PATHS),
    )


def exit_code(checks: Iterable[Check]) -> int:
    """Non-zero when any check FAILs. A SKIP is a precondition, not a fault."""
    return 1 if any(check.verdict == FAIL for check in checks) else 0


def tally(checks: Iterable[Check]) -> dict[str, int]:
    counts = {PASS: 0, FAIL: 0, SKIP: 0}
    for check in checks:
        counts[check.verdict] += 1
    return counts


def tls_at_or_above(negotiated: str, floor: str) -> bool:
    """Whether the negotiated protocol meets the dial's own floor."""
    wanted = f"TLSv{floor}"
    if negotiated not in TLS_ORDER or wanted not in TLS_ORDER:
        return False
    return TLS_ORDER.index(negotiated) >= TLS_ORDER.index(wanted)


def address_in_cidrs(address: str, cidrs: Iterable[str]) -> bool:
    """Whether one address falls inside any of the dial's allowed ranges."""
    try:
        candidate = ipaddress.ip_address(address)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if candidate.version == network.version and candidate in network:
            return True
    return False


def admin_ui_expectation(ui: str, external: bool, opted_in: bool) -> tuple[bool, str]:
    """(must this route be absent, why), for one admin UI.

    The class kill switch BEATS a per-UI flag, so a UI marked public while
    edge.admin_uis.external is false must still answer nothing -- the one case
    where reading the per-UI flag alone gives the wrong expectation.
    """
    if not external:
        return True, (
            "edge.admin_uis.external is false, which takes the whole infra class off the edge"
        )
    if not opted_in:
        return True, f"edge.admin_uis.public.{ui} is false"
    return False, f"edge.admin_uis.public.{ui} is true"


def public_listener_reason(settings: EdgeSettings) -> str:
    """Why there is no public product listener to probe, or empty when there is."""
    if not settings.product_public:
        return "edge.product.public is false -- dfe-ui has no public listener"
    if not settings.domain:
        return "edge.product.domain is empty -- this deployment publishes no public name"
    return ""


# The engine API's path groups, in the order the probe reports them.
ENGINE_PATHS: tuple[tuple[str, str], ...] = (
    ("engine browser family", ENGINE_BROWSER_PATH),
    ("engine cli family", ENGINE_CLI_PATH),
    ("engine scim", ENGINE_SCIM_PATH),
    ("engine openapi", ENGINE_SPEC_PATH),
    ("engine docs", ENGINE_DOCS_PATH),
    ("engine jwks", ENGINE_JWKS_PATH),
)


def engine_path_expectation(name: str, settings: EdgeSettings) -> tuple[bool, str]:
    """(must this path be absent, why), for one group of the engine's API.

    The browser families and the JWKS keys are what a public dfe-ui cannot work
    without, so they must answer. Everything else is off until its own switch
    opens it, and the Swagger surface is off under every combination.
    """
    if name == "engine cli family":
        if settings.engine_cli_families_public:
            return False, "edge.engine_api.cli_families_public is true"
        return True, "edge.engine_api.cli_families_public is false"
    if name == "engine openapi":
        if settings.engine_cli_families_public:
            return False, "edge.engine_api.cli_families_public is true, which opens the spec"
        return True, "edge.engine_api.cli_families_public is false, so the spec is not published"
    if name == "engine scim":
        if settings.engine_scim_public:
            return False, "edge.engine_api.scim_public is true"
        return True, "edge.engine_api.scim_public is false"
    if name == "engine docs":
        return True, "the Swagger surface is never on the public route"
    return False, "the browser cannot use the product without it"


# --- the checks --------------------------------------------------------------


# Every dial here runs with verification off, so a floor that passes says
# nothing about the certificate behind it.
UNVERIFIED = "; chain not verified"


def check_tls_floor(settings: EdgeSettings, host: str, address: str) -> Check:
    """The negotiated protocol meets the floor, and below it is refused."""
    name = "tls floor"
    reason = public_listener_reason(settings)
    if reason:
        return Check(name, SKIP, reason)
    floor = settings.tls_min_version
    answer = _reach(Request(host=host, address=address))
    if not answer.reached:
        return Check(name, FAIL, f"{host} on {address} answered nothing: {answer.error}")
    negotiated = answer.protocol or "no TLS at all"
    if not tls_at_or_above(negotiated, floor):
        return Check(name, FAIL, f"{host} negotiated {negotiated}, under the {floor} floor")
    cap = BELOW_FLOOR.get(floor, "")
    if not cap:
        return Check(
            name, PASS,
            f"{host} negotiated {negotiated}, at or above the {floor} floor{UNVERIFIED}",
        )
    capped = _reach(Request(host=host, address=address, tls_cap=cap))
    if capped.client_capped:
        return Check(
            name, PASS,
            f"{host} negotiated {negotiated}; a {cap} handshake cannot be offered from "
            f"this machine, so the floor is proven from the negotiated protocol alone"
            f"{UNVERIFIED}",
        )
    if capped.reached:
        return Check(
            name, FAIL, f"{host} accepted a handshake capped at {cap}, under the {floor} floor"
        )
    return Check(
        name, PASS,
        f"{host} negotiated {negotiated} and refused a {cap} handshake "
        f"({capped.error}){UNVERIFIED}",
    )


def check_hsts(settings: EdgeSettings, host: str, address: str) -> Check:
    """Strict-Transport-Security on the public listener's own response."""
    name = "hsts"
    reason = public_listener_reason(settings)
    if reason:
        return Check(name, SKIP, reason)
    if not settings.hsts:
        return Check(name, SKIP, "edge.product.tls.hsts is false -- no header is asked for")
    answer = _reach(Request(host=host, address=address))
    if not answer.reached:
        return Check(name, FAIL, f"{host} on {address} answered nothing: {answer.error}")
    header = answer.headers.get("strict-transport-security", "")
    if not header:
        return Check(
            name, FAIL,
            f"{host} answered {answer.status} with no Strict-Transport-Security header",
        )
    return Check(name, PASS, f"{host} answered Strict-Transport-Security: {header}")


def check_rate_limit(settings: EdgeSettings, host: str, address: str) -> Check:
    """A 429 lands once the configured burst is spent."""
    name = "rate limit"
    reason = public_listener_reason(settings)
    if reason:
        return Check(name, SKIP, reason)
    if not settings.rate_limit_enabled:
        return Check(name, SKIP, "edge.product.rate_limit.enabled is false -- no limit to reach")
    burst = settings.rate_limit_requests
    unit = settings.rate_limit_unit
    if burst <= 0:
        return Check(
            name, SKIP, f"edge.product.rate_limit.requests is {burst} -- no burst to spend"
        )
    if burst >= RATE_LIMIT_PROBE_CEILING:
        return Check(
            name, SKIP,
            f"a burst of {burst} in a {unit} is beyond what this probe sends "
            f"({RATE_LIMIT_PROBE_CEILING}), so the limit is left to a load generator",
        )
    for attempt in range(1, burst + 2):
        answer = _reach(Request(host=host, address=address))
        if not answer.reached:
            return Check(
                name, FAIL,
                f"{host} stopped answering after {attempt - 1} requests: {answer.error}",
            )
        if answer.status == 429:
            return Check(
                name, PASS,
                f"{host} answered 429 on request {attempt}, against a burst of {burst} in a {unit}",
            )
    return Check(
        name, FAIL,
        f"{host} answered {burst + 1} requests with no 429, past a burst of {burst} in a {unit}",
    )


# The ranges a machine behind NAT holds on its own side of it: RFC 1918, the
# CGNAT range a carrier hands out, loopback and link-local. Named rather than
# read off `is_global`, which also excludes the documentation ranges an operator
# may legitimately be allow-listing.
NAT_SIDE_RANGES = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                 "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16")
)


def dialled_from_behind_nat(address: str) -> bool:
    """Whether a socket's own endpoint is one the deployment could never see.

    A socket reports its LOCAL endpoint, so behind NAT it is an address the far
    end never sees and weighing it against a public allow-list answers a
    different question from the one asked.
    """
    try:
        candidate = ipaddress.ip_address(address)
    except ValueError:
        return True
    return any(
        candidate.version == network.version and candidate in network
        for network in NAT_SIDE_RANGES
    )


def check_cidr_filter(settings: EdgeSettings, host: str, address: str) -> Check:
    """An address off edge.product.allowed_cidrs gets nothing.

    Three things can make this untestable from here, and the evidence names
    which: an empty allow-list, this machine sitting inside it, or this machine
    dialling from behind NAT, where the address the filter judged is not one
    this process can read. A deployment that answers nothing AT ALL is not
    mistaken for a working filter -- the login check fails in that case, and it
    is the counterweight this one leans on.
    """
    name = "cidr filter"
    reason = public_listener_reason(settings)
    if reason:
        return Check(name, SKIP, reason)
    if not settings.allowed_cidrs:
        return Check(
            name, SKIP,
            "edge.product.allowed_cidrs is empty -- every address may reach the listener",
        )
    listed = ", ".join(settings.allowed_cidrs)
    answer = _reach(Request(host=host, address=address))
    if not answer.reached:
        return Check(
            name, PASS,
            f"{host} on {address} refused this machine, which is off the allow-list "
            f"{listed} ({answer.error})",
        )
    if address_in_cidrs(answer.source, settings.allowed_cidrs):
        return Check(
            name, SKIP,
            f"this machine dialled from {answer.source}, inside the allow-list {listed} -- "
            f"an on-list address proves nothing about an off-list one",
        )
    if dialled_from_behind_nat(answer.source):
        return Check(
            name, SKIP,
            f"this machine's own socket reads {answer.source}, so it sits behind NAT and the "
            f"address weighed against {listed} is not knowable here -- run the probe from a "
            f"host holding a public address",
        )
    return Check(
        name, FAIL,
        f"{host} answered {answer.status} to a connection from {answer.source}, "
        f"which is outside the allow-list {listed}",
    )


def check_receiver_private(settings: EdgeSettings, address: str) -> Check:
    """In vpn mode the receiver publishes no name and answers on none.

    The gateway's own address carries listeners for the HTTP plane only, so a
    refusal there is true in every configuration and proves nothing on its own.
    What does prove something is the receiver's published name: a deployment
    that opened a door for it has a record, and a second load balancer answers
    on a different address entirely. So the name is resolved first, and the
    gateway address is dialled only as the second half.
    """
    name = "receiver private"
    if settings.receiver_mode != "vpn":
        stated = settings.receiver_mode or "unset, so the cloud overlay decides"
        return Check(
            name, SKIP,
            f"edge.ingest.receiver.mode is {stated} -- a door other than the tunnel is intended",
        )

    ports = ", ".join(str(port) for port in RECEIVER_INGEST_PORTS)
    published = published_host(RECEIVER_LABEL, settings.domain)
    if not settings.domain:
        return Check(
            name, SKIP,
            "edge.product.domain is empty, so no receiver name is published and the only "
            f"address to dial is the gateway's, which carries no {ports} listener in any "
            "configuration",
        )
    resolved = _resolved(published)
    if not resolved:
        return Check(
            name, PASS,
            f"{published} does not resolve, so no record was published for a receiver door",
        )
    if resolved <= _resolved(address):
        return Check(
            name, SKIP,
            f"{published} resolves to the gateway's own address, where {ports} is not the "
            "receiver's door in any configuration -- what this check can see is a SEPARATE "
            "load balancer, and there is not one",
        )

    answered = []
    errors = []
    for port in RECEIVER_INGEST_PORTS:
        answer = _reach(Request(host="", address=published, port=port, path="", tls=False))
        if answer.reached:
            answered.append(str(port))
        else:
            errors.append(f"{port}: {answer.error}")
    if answered:
        return Check(
            name, FAIL,
            f"{published} carries its own address and accepted a connection on "
            f"{', '.join(answered)} -- the receiver is reachable without the tunnel",
        )
    return Check(
        name, PASS,
        f"{published} carries its own address and refused {ports} -- {'; '.join(errors)}",
    )


def check_route_absent(name: str, host: str, address: str, why: str) -> Check:
    """One published name answers nothing, or answers 404 with no route behind it.

    A 404 is what the gateway returns for a hostname it carries no route for, so
    it is the same verdict as a refused connection and not a weaker one.
    """
    answer = _reach(Request(host=host, address=address))
    if not answer.reached:
        return Check(name, PASS, f"{host} on {address} answered nothing ({why}): {answer.error}")
    if answer.status == 404:
        return Check(name, PASS, f"{host} answered 404 ({why}) -- no route is programmed for it")
    return Check(name, FAIL, f"{host} answered {answer.status} while {why}")


def check_otel_ingress(settings: EdgeSettings, address: str) -> Check:
    """The platform OTLP door is what edge.ingest.otel.enabled says, on every flavour.

    Off, otel.<cluster domain> carries no route. On, the collector's receiver
    behind it refuses this probe's request, which carries no token, with 401.
    """
    name = "otel ingress"
    if not settings.cluster_domain:
        return Check(
            name, SKIP,
            "k8s.domain is empty -- the dial names no cluster domain, so there is no "
            "otel name to dial",
        )
    host = published_host(OTEL_LABEL, settings.cluster_domain)
    if not settings.otel_enabled:
        return check_route_absent(name, host, address, "edge.ingest.otel.enabled is false")
    answer = _reach(Request(host=host, address=address, path=OTEL_PROBE_PATH))
    where = f"{host}{OTEL_PROBE_PATH}"
    if not answer.reached:
        return Check(
            name, FAIL,
            f"{where} on {address} answered nothing while edge.ingest.otel.enabled is true: "
            f"{answer.error}",
        )
    if answer.status == 401:
        return Check(
            name, PASS,
            f"{where} answered 401 to a request with no token -- the door is open and asks "
            "for the bearer token",
        )
    if answer.status == 404:
        return Check(
            name, FAIL,
            f"{where} answered 404 while edge.ingest.otel.enabled is true -- no route is "
            "programmed for it",
        )
    return Check(
        name, FAIL,
        f"{where} answered {answer.status} to a request with no token -- only 401 shows "
        "the collector checked for one",
    )


def check_admin_ui(settings: EdgeSettings, ui: str, address: str) -> Check:
    """One admin UI route is absent, unless this deployment opted it in."""
    name = f"admin ui {ui}"
    if not settings.domain:
        return Check(name, SKIP, "edge.product.domain is empty -- no admin name is published")
    absent, why = admin_ui_expectation(
        ui, settings.admin_uis_external, settings.admin_uis_public.get(ui, False)
    )
    host = published_host(ADMIN_UI_HOSTNAMES[ui], settings.domain)
    if not absent:
        return Check(
            name, SKIP, f"{host} is opted in -- {why}, so a route that answers is intended"
        )
    return check_route_absent(name, host, address, why)


def check_login(settings: EdgeSettings, host: str, address: str) -> Check:
    """dfe-ui's sign-in page answers -- the one route that must."""
    name = "ui login"
    reason = public_listener_reason(settings)
    if reason:
        return Check(name, SKIP, reason)
    answer = _reach(Request(host=host, address=address, path=LOGIN_PATH))
    if not answer.reached:
        return Check(name, FAIL, f"{host}{LOGIN_PATH} answered nothing: {answer.error}")
    if answer.status is None or answer.status >= 400:
        return Check(name, FAIL, f"{host}{LOGIN_PATH} answered {answer.status}")
    return Check(name, PASS, f"{host}{LOGIN_PATH} answered {answer.status}")


def check_engine_path(
    settings: EdgeSettings, name: str, path: str, host: str, address: str
) -> Check:
    """One group of the engine's API answers, or 404s, as the dial says it should.

    The browser calls the engine at the product's own origin, so this is where a
    missing public engine route shows up: /api/v1 lands on dfe-ui and 404s while
    the UI itself still loads.
    """
    reason = public_listener_reason(settings)
    if reason:
        return Check(name, SKIP, reason)
    if not settings.engine_with_product:
        return Check(
            name, SKIP,
            "edge.engine_api.with_product is false -- the engine answers on no public name",
        )
    absent, why = engine_path_expectation(name, settings)
    answer = _reach(Request(host=host, address=address, path=path))
    if not answer.reached:
        if absent:
            return Check(name, PASS, f"{host}{path} answered nothing ({why}): {answer.error}")
        return Check(name, FAIL, f"{host}{path} answered nothing: {answer.error}")
    if absent:
        if answer.status == 404:
            return Check(
                name, PASS, f"{host}{path} answered 404 ({why}) -- no route is programmed for it"
            )
        return Check(name, FAIL, f"{host}{path} answered {answer.status} while {why}")
    if answer.status == 404:
        return Check(
            name, FAIL,
            f"{host}{path} answered 404 and {why} -- the public hostname carries no route for it",
        )
    return Check(name, PASS, f"{host}{path} answered {answer.status} -- {why}")


def run_checks(settings: EdgeSettings, *, target: str = "") -> list[Check]:
    """Every check, in report order, against one deployment."""
    if not settings.enabled:
        return [
            Check(name, SKIP, "edge.enabled is false -- this deployment opens no door at all")
            for name in check_names()
        ]
    host = product_host(settings.domain)
    try:
        address = dial_address(host, target)
    except EdgeProbeError as error:
        return [Check(name, SKIP, str(error)) for name in check_names()]
    return [
        check_tls_floor(settings, host, address),
        check_hsts(settings, host, address),
        check_rate_limit(settings, host, address),
        check_cidr_filter(settings, host, address),
        check_receiver_private(settings, address),
        check_otel_ingress(settings, address),
        *(check_admin_ui(settings, ui, address) for ui in ADMIN_UIS),
        check_login(settings, host, address),
        *(
            check_engine_path(settings, name, path, host, address)
            for name, path in ENGINE_PATHS
        ),
    ]


# --- the admin UIs, through the gateway --------------------------------------
# helm/edge/gateway/templates/admin-links.yaml renders the list, one entry per
# infra route the gateway serves, into the engine's namespace.

ADMIN_LINKS_CONFIGMAP = "dfe-admin-links"
ADMIN_LINKS_KEY = "admin_links.json"
DEFAULT_GATEWAY = "dfe-gateway"
DEFAULT_ADMIN_NAMESPACE = "dfe-local"
# A browser gives up at about 20; a UI that needs more than this is broken anyway.
MAX_REDIRECTS = 10
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# The 4xx a working UI ends on: a login it asks for, one it refuses, or a root
# page it does not serve. Any other 4xx is the request refused -- an IdP
# rejecting the login it was handed, an unregistered redirect URI.
ADMIN_UI_4XX_PASS = frozenset({401, 403, 404})
ADMIN_PROBE_INTERVAL = 10.0
KUBECTL_TIMEOUT = 60.0


@dataclass(frozen=True)
class AdminLink:
    """One admin UI the gateway serves: its display name and browser URL."""

    name: str
    url: str


def parse_admin_links(text: str) -> list[AdminLink]:
    """The entries of the ConfigMap's one data key, refusing a shape it cannot read."""
    try:
        entries = json.loads(text)
    except json.JSONDecodeError as error:
        raise EdgeProbeError(f"{ADMIN_LINKS_KEY} is not JSON: {error}") from error
    if not isinstance(entries, list):
        raise EdgeProbeError(f"{ADMIN_LINKS_KEY} is not a list: {text[:80]!r}")
    links = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("url"):
            raise EdgeProbeError(f"{ADMIN_LINKS_KEY} carries an entry with no name or url: {entry!r}")
        links.append(AdminLink(name=str(entry["name"]), url=str(entry["url"])))
    return links


def _hop(parts: SplitResult, port: int) -> str:
    """One URL in the form a loop is recognised by: host lowercased, default port dropped."""
    host = (parts.hostname or "").lower()
    default = HTTPS_PORT if parts.scheme == "https" else HTTP_PORT
    shown = f":{port}" if port != default else ""
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{host}{shown}{parts.path or '/'}{query}"


def _on_domain(host: str, domain: str) -> bool:
    """Whether a redirect target is one of this deployment's own names."""
    return host == domain or host.endswith(f".{domain}")


def check_admin_link(link: AdminLink, address: str, max_redirects: int = MAX_REDIRECTS) -> Check:
    """One admin UI loads through the gateway: no redirect loop, no 5xx, no refused 4xx.

    Every hop on the deployment's own domain is dialled at the gateway address
    with its name on the SNI and the Host header, the way a browser reaches it.
    A redirect off that domain is a login handed to an IdP, which is the UI
    working, so the walk ends there.
    """
    name = f"admin ui {link.name}"
    domain = (urlsplit(link.url).hostname or "").lower().partition(".")[2].rstrip(".")
    if not domain:
        return Check(name, SKIP, f"{link.url} carries no domain -- this deployment publishes no name")
    url, hops = link.url, []
    for _ in range(max_redirects + 1):
        parts = urlsplit(url)
        tls = parts.scheme == "https"
        try:
            port = parts.port or (HTTPS_PORT if tls else HTTP_PORT)
        except ValueError:
            return Check(name, FAIL, f"{' -> '.join([*hops, url])} names no usable port")
        here = _hop(parts, port)
        if here in hops:
            loop = " -> ".join([*hops[hops.index(here):], here])
            return Check(name, FAIL, f"redirect loop: {loop}")
        hops.append(here)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        answer = _reach(Request(
            host=parts.hostname or "", address=address, port=port, path=path, tls=tls,
        ))
        trail = " -> ".join(hops)
        if not answer.reached:
            return Check(name, FAIL, f"{trail} answered nothing through {address}: {answer.error}")
        if answer.status is not None and answer.status >= 500:
            return Check(name, FAIL, f"{trail} answered {answer.status}")
        status = answer.status or 0
        if 400 <= status < 500 and status not in ADMIN_UI_4XX_PASS:
            return Check(name, FAIL, f"{trail} answered {status} -- the request was refused")
        if answer.status not in REDIRECT_STATUSES:
            return Check(name, PASS, f"{trail} answered {answer.status}")
        location = answer.headers.get("location", "")
        if not location:
            return Check(name, FAIL, f"{trail} answered {answer.status} with no Location")
        url = urljoin(url, location)
        target = (urlsplit(url).hostname or "").lower()
        if not _on_domain(target, domain):
            return Check(name, PASS, f"{trail} hands the login to {target} ({answer.status})")
    return Check(name, FAIL, f"more than {max_redirects} redirects: {' -> '.join(hops)}")


def _kubectl_json(kube: list[str], *argv: str) -> tuple[dict, str]:
    """(parsed object, error) for one read-only kubectl call."""
    try:
        done = run_kubectl([*kube, *argv, "-o", "json"], timeout=KUBECTL_TIMEOUT)
    except FileNotFoundError:
        return {}, "kubectl is not on PATH"
    except (OSError, subprocess.TimeoutExpired) as error:
        return {}, str(error)
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines()
        return {}, lines[-1] if lines else f"kubectl exited {done.returncode}"
    try:
        parsed = json.loads(done.stdout)
    except json.JSONDecodeError as error:
        return {}, f"kubectl printed no JSON: {error}"
    return (parsed, "") if isinstance(parsed, dict) else ({}, "kubectl printed no object")


def read_admin_links(kube: list[str], namespace: str) -> list[AdminLink] | None:
    """The admin UIs the gateway lists in `namespace`, or None when it lists none there."""
    doc, error = _kubectl_json(kube, "-n", namespace, "get", "configmap", ADMIN_LINKS_CONFIGMAP)
    if "NotFound" in error:
        return None
    if error:
        raise EdgeProbeError(f"cannot read {namespace}/{ADMIN_LINKS_CONFIGMAP}: {error}")
    text = (doc.get("data") or {}).get(ADMIN_LINKS_KEY)
    if text is None:
        raise EdgeProbeError(f"{namespace}/{ADMIN_LINKS_CONFIGMAP} carries no {ADMIN_LINKS_KEY}")
    return parse_admin_links(text)


def gateway_address(kube: list[str], name: str = DEFAULT_GATEWAY) -> str:
    """The address Gateway `name` is programmed on, else any Gateway's, else empty."""
    doc, _error = _kubectl_json(kube, "get", "gateway", "-A")
    addressed = [
        ((item.get("metadata") or {}).get("name"), str(address["value"]))
        for item in doc.get("items") or []
        for address in (item.get("status") or {}).get("addresses") or []
        if address.get("value")
    ]
    named = [value for gateway, value in addressed if gateway == name]
    return (named or [value for _, value in addressed] or [""])[0]


def admin_ui_checks(
    kube: list[str], namespace: str, target: str = "", gateway: str = DEFAULT_GATEWAY
) -> list[Check]:
    """One check per admin UI the gateway lists, or one SKIP naming why none can run."""
    links = read_admin_links(kube, namespace)
    if links is None:
        return [Check("admin uis", SKIP, f"no {ADMIN_LINKS_CONFIGMAP} ConfigMap in {namespace}")]
    if not links:
        return [Check("admin uis", SKIP, f"{ADMIN_LINKS_CONFIGMAP} lists no admin UI")]
    address = target or gateway_address(kube, gateway)
    if not address:
        return [
            Check(f"admin ui {link.name}", SKIP, "no Gateway holds an address to dial")
            for link in links
        ]
    first = urlsplit(links[0].url)
    port = first.port or (HTTPS_PORT if first.scheme == "https" else HTTP_PORT)
    door = _reach(Request(host="", address=address, port=port, path="", tls=False))
    if not door.reached:
        return [
            Check(
                f"admin ui {link.name}", SKIP,
                f"the gateway at {address}:{port} does not answer from this machine "
                f"({door.error}) -- probe from a host that reaches it",
            )
            for link in links
        ]
    return [check_admin_link(link, address) for link in links]


# --- the verbs ---------------------------------------------------------------


def report(verb: str, checks: list[Check]) -> int:
    """Print one line per check and the tally, and return the verb's exit code."""
    for check in checks:
        print(f"  [{check.verdict}] {check.name}: {check.evidence}", file=sys.stderr)
    counts = tally(checks)
    print(
        f"=== {verb}: {counts[PASS]} passed, {counts[FAIL]} failed, "
        f"{counts[SKIP]} skipped ===",
        file=sys.stderr,
    )
    # A run that proved nothing exits zero, which a gate reads as green, so it
    # has to say plainly that no door was looked at.
    if counts[PASS] == 0 and counts[FAIL] == 0:
        print(
            "    NOTHING WAS PROVEN -- every check reported a precondition rather than a "
            "verdict. Read the skips above before treating this as a pass.",
            file=sys.stderr,
        )
    return exit_code(checks)


def cmd_edge_probe(args: argparse.Namespace) -> int:
    try:
        settings = read_dial(Path(args.dial))
    except EdgeProbeError as error:
        print(f"dfe-ops edge-probe: {error}", file=sys.stderr)
        return 1
    return report("edge-probe", run_checks(settings, target=args.target))


def cmd_admin_probe(args: argparse.Namespace) -> int:
    kube = ["--kubeconfig", args.kubeconfig] if args.kubeconfig else []
    if args.context:
        kube += ["--context", args.context]
    deadline = time.monotonic() + max(args.wait, 0)
    while True:
        try:
            checks = admin_ui_checks(kube, args.namespace, args.target, args.gateway)
        except EdgeProbeError as error:
            print(f"dfe-ops admin-probe: {error}", file=sys.stderr)
            return 1
        failing = [check.name for check in checks if check.verdict == FAIL]
        if not failing or time.monotonic() >= deadline:
            return report("admin-probe", checks)
        print(
            f"  ...{', '.join(failing)} not loading yet; re-probing in "
            f"{ADMIN_PROBE_INTERVAL:.0f}s",
            file=sys.stderr,
        )
        time.sleep(ADMIN_PROBE_INTERVAL)


# --- parser ------------------------------------------------------------------


def add_edge_probe_subparser(sub: argparse._SubParsersAction) -> None:
    """Register `dfe-ops edge-probe`."""
    probe = sub.add_parser(
        "edge-probe",
        help="prove from outside the cluster which doors the edge module actually opens",
        description="Reads the deployment dial's edge: block and dials the published names "
                    "from this machine, with no credential, so every verdict is what an "
                    "unauthenticated caller sees. Exits non-zero if any check fails.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    probe.add_argument(
        "--dial", default=str(DIAL),
        help="the deployment dial carrying the edge: block",
    )
    probe.add_argument(
        "--target", default="", metavar="HOST_OR_ADDRESS",
        help="dial this host or gateway address instead of the published name, for a "
             "deployment whose DNS does not resolve here yet",
    )
    probe.set_defaults(func=cmd_edge_probe)


def add_admin_probe_subparser(sub: argparse._SubParsersAction) -> None:
    """Register `dfe-ops admin-probe`."""
    probe = sub.add_parser(
        "admin-probe",
        help="prove every admin UI the gateway lists loads through it: no redirect loop, "
             "no 5xx, no refused 4xx",
        description="Reads the dfe-admin-links ConfigMap and GETs each admin UI through the "
                    "gateway address with its own name on the SNI and Host header, following "
                    "redirects. A loop, a 5xx, a 4xx other than 401/403/404 or no answer "
                    "fails; a redirect off the deployment's domain is a login handed to an IdP "
                    "and passes. Sends no credential. Exits non-zero if any check fails.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    probe.add_argument(
        "--namespace", default=DEFAULT_ADMIN_NAMESPACE,
        help="the engine's namespace, where the gateway writes dfe-admin-links",
    )
    probe.add_argument("--kubeconfig", default="", help="kubeconfig to read; empty is kubectl's own")
    probe.add_argument("--context", default="", help="kubeconfig context to read; empty is its current one")
    probe.add_argument(
        "--gateway", default=DEFAULT_GATEWAY,
        help="the Gateway whose address is dialled; any Gateway's when it holds none",
    )
    probe.add_argument(
        "--target", default="", metavar="ADDRESS",
        help="dial this address instead of the one the Gateway reports",
    )
    probe.add_argument(
        "--wait", type=float, default=0.0, metavar="SECONDS",
        help="re-probe a failing UI until this many seconds have passed",
    )
    probe.set_defaults(func=cmd_admin_probe)


__all__ = [
    "EdgeProbeError",
    "add_admin_probe_subparser",
    "add_edge_probe_subparser",
    "cmd_admin_probe",
    "cmd_edge_probe",
]
