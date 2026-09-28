#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_edge_probe.py
#  Purpose:      Guard `dfe-ops edge-probe` -- every check reads its expectation
#                from the dial, each one reports PASS, FAIL or SKIP against a
#                stubbed listener, each SKIP-by-design path says which
#                precondition it missed, and any FAIL exits non-zero.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe_ops_edge_probe.py.

    python3 -m pytest scripts/tests/test_dfe_ops_edge_probe.py -q

The HTTP half runs against a stub listener on loopback -- a real server on a
port the kernel picks, so the real network boundary is exercised and no test
reaches a deployment. A stub carries no certificate, so the two TLS facts
(the negotiated protocol, and the refusal of a handshake capped below the floor)
are the only things supplied by hand.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import dfe_ops_edge_probe as probe  # noqa: E402

# Captured before any test patches the boundary, so a helper that wraps it
# cannot end up calling its own replacement.
_REAL_REACH = probe._reach


# ---------------------------------------------------------------------------
# The dial under test
# ---------------------------------------------------------------------------

DIAL_TEXT = """\
edge:
  enabled: true
  flavour: aws
  product:
    public: true
    domain: example.test
    tls:
      min_version: "1.2"
      hsts: true
    rate_limit:
      enabled: true
      requests: "3"
      unit: Minute
      scope: local
    allowed_cidrs: ""
    trusted_proxy_cidrs: ""
  admin_uis:
    external: false
    public:
      kafbat: false
      cruise_control: false
      hyperdx: false
      argocd: false
      links: false
      forgejo: false
  ingest:
    receiver:
      mode: vpn
    otel:
      public: false
      auth: required
"""

PRODUCT = "dfe.example.test"


def _settings(**overrides: object) -> probe.EdgeSettings:
    """The dial above, with the one field a test is about changed."""
    return replace(probe.parse_edge(probe.parse_yaml_subset(DIAL_TEXT)), **overrides)


def _dial_file(tmp_path: Path, text: str = DIAL_TEXT) -> Path:
    path = tmp_path / "deployment.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _args(dial: Path, target: str = "127.0.0.1") -> argparse.Namespace:
    return argparse.Namespace(dial=str(dial), target=target)


# ---------------------------------------------------------------------------
# The stub listener
# ---------------------------------------------------------------------------


class StubGateway:
    """One deployment's public listener, as far as an outside caller can see it.

    An unknown (host, path) answers 404, which is what an absent route looks
    like from here. `burst` starts answering 429 past that many requests to the
    SAME route, the way a local rate limit counts.
    """

    def __init__(self) -> None:
        self.port = 0
        self.routes: dict[tuple[str, str], int] = {}
        self.headers: dict[str, str] = {}
        self.locations: dict[tuple[str, str], str] = {}
        self.burst = 0
        self.seen: list[tuple[str, str]] = []

    def answer(self, host: str, path: str) -> tuple[int, dict[str, str]]:
        self.seen.append((host, path))
        if self.burst and self.seen.count((host, path)) > self.burst:
            return 429, dict(self.headers)
        headers = dict(self.headers)
        if (host, path) in self.locations:
            headers["Location"] = self.locations[(host, path)]
        return self.routes.get((host, path), 404), headers


class _StubHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status, headers = self.server.stub.answer(self.headers.get("Host", ""), self.path)
        body = b"stub\n"
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        """A test run prints its own results; the stub's access log is noise."""


@pytest.fixture
def gateway() -> Iterator[StubGateway]:
    """A listener on a port the kernel picks, shut down with the test."""
    stub = StubGateway()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    server.stub = stub
    stub.port = server.server_address[1]
    # A short poll interval, because serve_forever only notices a shutdown
    # between polls and the default would put half a second on every test.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    yield stub
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def closed_port() -> int:
    """A port nothing listens on, for the checks that must find a refusal."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _at_stub(stub: StubGateway, closed: dict[int, int] | None = None) -> Callable:
    """Send every dial at the stub, with the real boundary still in the loop.

    Only the address and the TLS half are rewritten: a stub server carries no
    certificate, so it is spoken to in plaintext.
    """
    ports = closed or {}

    def reach(request: probe.Request) -> probe.Answer:
        port = ports.get(request.port, stub.port)
        return _REAL_REACH(replace(request, address="127.0.0.1", port=port, tls=False))

    return reach


def _at_stub_over_tls(
    stub: StubGateway,
    closed: dict[int, int] | None = None,
    *,
    negotiated: str = "TLSv1.3",
    capped_refused: bool = True,
) -> Callable:
    """The stub, plus the two TLS facts a plaintext stub cannot produce."""
    plain = _at_stub(stub, closed)

    def reach(request: probe.Request) -> probe.Answer:
        if request.tls_cap:
            if capped_refused:
                return probe.Answer(reached=False, error="tlsv1 alert protocol version")
            return probe.Answer(reached=True, protocol=request.tls_cap)
        answer = plain(request)
        if request.tls and answer.reached:
            return replace(answer, protocol=negotiated)
        return answer

    return reach


# ---------------------------------------------------------------------------
# The dial
# ---------------------------------------------------------------------------


def test_parse_edge_reads_the_dials_own_key_names() -> None:
    settings = probe.parse_edge(probe.parse_yaml_subset(DIAL_TEXT))
    assert settings.enabled is True
    assert settings.flavour == "aws"
    assert settings.domain == "example.test"
    assert settings.tls_min_version == "1.2"
    assert settings.hsts is True
    assert settings.rate_limit_requests == 3
    assert settings.rate_limit_unit == "Minute"
    assert settings.admin_uis_external is False
    assert settings.admin_uis_public == dict.fromkeys(probe.ADMIN_UIS, False)
    assert settings.receiver_mode == "vpn"
    assert settings.otel_public is False


def test_parse_edge_takes_the_deployments_own_defaults_for_an_absent_block() -> None:
    """A dial with no edge: block is probed for what that deployment gets."""
    settings = probe.parse_edge({})
    assert settings.enabled is True
    assert settings.product_public is True
    assert settings.tls_min_version == "1.2"
    assert settings.hsts is True
    assert settings.admin_uis_external is False
    assert settings.receiver_mode == ""


def test_parse_edge_refuses_a_boolean_that_is_neither_true_nor_false() -> None:
    text = "edge:\n  product:\n    tls:\n      hsts: yes-please\n"
    with pytest.raises(probe.EdgeProbeError, match=r"edge\.product\.tls\.hsts"):
        probe.parse_edge(probe.parse_yaml_subset(text))


def test_read_dial_names_the_missing_file_and_the_template(tmp_path: Path) -> None:
    with pytest.raises(probe.EdgeProbeError, match="no deployment dial"):
        probe.read_dial(tmp_path / "absent.yaml")


def test_read_dial_reads_a_real_file(tmp_path: Path) -> None:
    assert probe.read_dial(_dial_file(tmp_path)).domain == "example.test"


# ---------------------------------------------------------------------------
# Names, addresses and verdict arithmetic
# ---------------------------------------------------------------------------


def test_dial_address_prefers_the_target_over_any_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """--target exists for a name DNS does not carry yet, so it never asks."""
    monkeypatch.setattr(probe, "_resolves", lambda host: pytest.fail("DNS was consulted"))
    assert probe.dial_address(PRODUCT, "198.51.100.7") == "198.51.100.7"


def test_dial_address_refuses_a_name_that_does_not_resolve_and_has_no_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "_resolves", lambda host: False)
    with pytest.raises(probe.EdgeProbeError, match="--target"):
        probe.dial_address(PRODUCT, "")


def test_dial_address_refuses_when_the_dial_publishes_no_name() -> None:
    with pytest.raises(probe.EdgeProbeError, match=r"edge\.product\.domain"):
        probe.dial_address("", "")


def test_product_host_is_the_canonical_label_under_the_public_zone() -> None:
    assert probe.product_host("example.test") == PRODUCT
    assert probe.product_host("") == ""


def test_tls_at_or_above_accepts_the_floor_and_everything_above_it() -> None:
    assert probe.tls_at_or_above("TLSv1.2", "1.2")
    assert probe.tls_at_or_above("TLSv1.3", "1.2")
    assert not probe.tls_at_or_above("TLSv1.1", "1.2")
    assert not probe.tls_at_or_above("TLSv1.2", "1.3")
    assert not probe.tls_at_or_above("", "1.2")


def test_address_in_cidrs_matches_only_a_range_that_carries_it() -> None:
    assert probe.address_in_cidrs("203.0.113.9", ("203.0.113.0/24",))
    assert not probe.address_in_cidrs("198.51.100.9", ("203.0.113.0/24",))
    assert not probe.address_in_cidrs("not-an-address", ("203.0.113.0/24",))
    assert not probe.address_in_cidrs("203.0.113.9", ("nonsense",))


def test_admin_ui_expectation_has_the_kill_switch_beat_a_per_ui_opt_in() -> None:
    absent, why = probe.admin_ui_expectation("argocd", external=False, opted_in=True)
    assert absent is True
    assert "edge.admin_uis.external is false" in why


def test_admin_ui_expectation_reports_an_opted_in_route_as_intended() -> None:
    absent, why = probe.admin_ui_expectation("argocd", external=True, opted_in=True)
    assert absent is False
    assert "edge.admin_uis.public.argocd is true" in why


def test_exit_code_is_non_zero_only_when_a_check_failed() -> None:
    passed = probe.Check("a", probe.PASS, "")
    skipped = probe.Check("b", probe.SKIP, "")
    failed = probe.Check("c", probe.FAIL, "")
    assert probe.exit_code([passed, skipped]) == 0
    assert probe.exit_code([passed, failed, skipped]) == 1


# ---------------------------------------------------------------------------
# 1 -- the TLS floor
# ---------------------------------------------------------------------------


def test_tls_floor_passes_when_the_protocol_meets_it_and_below_is_refused(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[(PRODUCT, "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub_over_tls(gateway))
    check = probe.check_tls_floor(_settings(), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.PASS
    assert "TLSv1.3" in check.evidence
    assert "TLSv1.1" in check.evidence


def test_tls_floor_fails_when_a_handshake_capped_below_it_is_accepted(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[(PRODUCT, "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub_over_tls(gateway, capped_refused=False))
    check = probe.check_tls_floor(_settings(), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.FAIL
    assert "capped at TLSv1.1" in check.evidence


def test_tls_floor_fails_when_the_negotiated_protocol_is_under_the_floor(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[(PRODUCT, "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub_over_tls(gateway, negotiated="TLSv1.1"))
    check = probe.check_tls_floor(_settings(), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.FAIL
    assert "under the 1.2 floor" in check.evidence


def test_tls_floor_fails_against_a_listener_that_negotiates_no_tls_at_all(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stub speaks plaintext, which is the fault this check exists to catch."""
    gateway.routes[(PRODUCT, "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_tls_floor(_settings(), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.FAIL
    assert "no TLS at all" in check.evidence


def test_tls_floor_passes_on_the_negotiated_protocol_when_this_machine_cannot_cap(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client too modern to offer the old version must not read as proof."""
    plain = _at_stub_over_tls(gateway)

    def reach(request: probe.Request) -> probe.Answer:
        if request.tls_cap:
            return probe.Answer(reached=False, client_capped=True, error="no protocols available")
        return plain(request)

    gateway.routes[(PRODUCT, "/")] = 200
    monkeypatch.setattr(probe, "_reach", reach)
    check = probe.check_tls_floor(_settings(), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.PASS
    assert "cannot be offered from this machine" in check.evidence


def test_tls_floor_skips_when_the_product_has_no_public_listener() -> None:
    check = probe.check_tls_floor(_settings(product_public=False), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.SKIP
    assert "edge.product.public is false" in check.evidence


# ---------------------------------------------------------------------------
# 2 -- HSTS
# ---------------------------------------------------------------------------


def test_hsts_passes_when_the_header_is_on_the_response(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[(PRODUCT, "/")] = 200
    gateway.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_hsts(_settings(), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.PASS
    assert "includeSubDomains" in check.evidence


def test_hsts_fails_when_the_header_is_absent(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[(PRODUCT, "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_hsts(_settings(), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.FAIL
    assert "no Strict-Transport-Security" in check.evidence


def test_hsts_skips_when_the_dial_does_not_ask_for_it() -> None:
    check = probe.check_hsts(_settings(hsts=False), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.SKIP
    assert "edge.product.tls.hsts is false" in check.evidence


# ---------------------------------------------------------------------------
# 3 -- the rate limit
# ---------------------------------------------------------------------------


def test_rate_limit_passes_when_a_429_lands_past_the_configured_burst(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[(PRODUCT, "/")] = 200
    gateway.burst = 3
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_rate_limit(_settings(), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.PASS
    assert "429 on request 4" in check.evidence


def test_rate_limit_fails_when_the_burst_is_spent_and_nothing_answers_429(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[(PRODUCT, "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_rate_limit(_settings(), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.FAIL
    assert "no 429" in check.evidence


def test_rate_limit_sends_no_more_than_the_burst_plus_one(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop is bounded by the dial, so a probe cannot run away."""
    gateway.routes[(PRODUCT, "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    probe.check_rate_limit(_settings(), PRODUCT, "127.0.0.1")
    assert gateway.seen.count((PRODUCT, "/")) == 4


def test_rate_limit_skips_when_the_limit_is_disabled() -> None:
    check = probe.check_rate_limit(_settings(rate_limit_enabled=False), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.SKIP
    assert "edge.product.rate_limit.enabled is false" in check.evidence


def test_rate_limit_skips_a_burst_beyond_what_this_probe_sends() -> None:
    settings = _settings(rate_limit_requests=probe.RATE_LIMIT_PROBE_CEILING + 1)
    check = probe.check_rate_limit(settings, PRODUCT, "127.0.0.1")
    assert check.verdict == probe.SKIP
    assert "beyond what this probe sends" in check.evidence


# ---------------------------------------------------------------------------
# 4 -- the CIDR filter
# ---------------------------------------------------------------------------


def test_cidr_filter_skips_when_the_dial_sets_no_allow_list() -> None:
    check = probe.check_cidr_filter(_settings(), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.SKIP
    assert "edge.product.allowed_cidrs is empty" in check.evidence


def test_cidr_filter_skips_when_this_machine_is_on_the_list(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An on-list address proves nothing, and the evidence says which skip it is."""
    gateway.routes[(PRODUCT, "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_cidr_filter(_settings(allowed_cidrs=("127.0.0.0/8",)), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.SKIP
    assert "inside the allow-list" in check.evidence


def _sourced_at(gateway: StubGateway, source: str) -> Callable:
    """The stub, answering as though the dial left from a given address.

    Substituted because a real socket here always reports loopback, which is
    neither of the two addresses these branches turn on.
    """
    plain = _at_stub(gateway)

    def reach(request: probe.Request) -> probe.Answer:
        return replace(plain(request), source=source)

    return reach


def test_cidr_filter_fails_when_an_off_list_public_address_gets_an_answer(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[(PRODUCT, "/")] = 200
    monkeypatch.setattr(probe, "_reach", _sourced_at(gateway, "198.51.100.9"))
    check = probe.check_cidr_filter(
        _settings(allowed_cidrs=("203.0.113.0/24",)), PRODUCT, "127.0.0.1"
    )
    assert check.verdict == probe.FAIL
    assert "outside the allow-list" in check.evidence


def test_cidr_filter_skips_rather_than_failing_a_deployment_probed_from_behind_nat(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's laptop reads its own private address, so the address the
    filter actually judged is not knowable here -- FAIL would block a healthy
    deployment on the one check that proves the allow-list bites."""
    gateway.routes[(PRODUCT, "/")] = 200
    monkeypatch.setattr(probe, "_reach", _sourced_at(gateway, "192.168.1.20"))
    check = probe.check_cidr_filter(
        _settings(allowed_cidrs=("203.0.113.0/24",)), PRODUCT, "127.0.0.1"
    )
    assert check.verdict == probe.SKIP
    assert "behind NAT" in check.evidence


def test_cidr_filter_passes_when_the_listener_refuses_this_machine(
    gateway: StubGateway, closed_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway, {probe.HTTPS_PORT: closed_port}))
    check = probe.check_cidr_filter(
        _settings(allowed_cidrs=("203.0.113.0/24",)), PRODUCT, "127.0.0.1"
    )
    assert check.verdict == probe.PASS
    assert "refused this machine" in check.evidence


# ---------------------------------------------------------------------------
# 5 -- the receiver
# ---------------------------------------------------------------------------


def _resolving(**names: str) -> Callable:
    """DNS as this probe reads it: host -> the one address it answers with.

    A literal address resolves to itself, matching getaddrinfo, so a --target
    and a published name compare through the same call.
    """
    def resolved(host: str) -> set[str]:
        try:
            return {str(ipaddress.ip_address(host))}
        except ValueError:
            pass
        key = host.split(".", 1)[0]
        return {names[key]} if key in names else set()

    return resolved


def test_receiver_passes_when_no_record_was_published_for_it(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name that does not resolve is a door that was never opened, and it is
    the only thing provable from outside when the receiver holds a ClusterIP."""
    monkeypatch.setattr(probe, "_resolved", _resolving(dfe="203.0.113.10"))
    monkeypatch.setattr(probe, "_reach", lambda request: pytest.fail("a dial was made"))
    check = probe.check_receiver_private(_settings(), "203.0.113.10")
    assert check.verdict == probe.PASS
    assert "does not resolve" in check.evidence


def test_receiver_skips_rather_than_passing_on_the_gateways_own_address(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway carries the HTTP plane's listeners only, so its refusal on
    8080 and 8443 is true in every configuration and proves nothing."""
    monkeypatch.setattr(
        probe, "_resolved", _resolving(dfe="203.0.113.10", receiver="203.0.113.10")
    )
    monkeypatch.setattr(probe, "_reach", lambda request: pytest.fail("a dial was made"))
    check = probe.check_receiver_private(_settings(), "203.0.113.10")
    assert check.verdict == probe.SKIP
    assert "not the receiver's door in any configuration" in check.evidence


def test_receiver_passes_when_its_own_load_balancer_refuses_every_ingest_port(
    gateway: StubGateway, closed_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        probe, "_resolved", _resolving(dfe="203.0.113.10", receiver="203.0.113.11")
    )
    closed = dict.fromkeys(probe.RECEIVER_INGEST_PORTS, closed_port)
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway, closed))
    check = probe.check_receiver_private(_settings(), "203.0.113.10")
    assert check.verdict == probe.PASS
    assert "8080, 8443" in check.evidence


def test_receiver_fails_when_its_own_load_balancer_accepts_a_connection(
    gateway: StubGateway, closed_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One open port is the whole finding: the tunnel is not the only way in."""
    monkeypatch.setattr(
        probe, "_resolved", _resolving(dfe="203.0.113.10", receiver="203.0.113.11")
    )
    closed = {probe.RECEIVER_INGEST_PORTS[1]: closed_port}
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway, closed))
    check = probe.check_receiver_private(_settings(), "203.0.113.10")
    assert check.verdict == probe.FAIL
    assert "8080" in check.evidence


def test_receiver_skips_when_no_public_domain_is_named(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "_reach", lambda request: pytest.fail("a dial was made"))
    check = probe.check_receiver_private(_settings(domain=""), "203.0.113.10")
    assert check.verdict == probe.SKIP
    assert "no receiver name is published" in check.evidence


def test_receiver_skips_when_the_mode_is_not_vpn() -> None:
    check = probe.check_receiver_private(_settings(receiver_mode="public"), "127.0.0.1")
    assert check.verdict == probe.SKIP
    assert "edge.ingest.receiver.mode is public" in check.evidence


def test_receiver_skips_and_says_so_when_the_dial_sets_no_mode() -> None:
    check = probe.check_receiver_private(_settings(receiver_mode=""), "127.0.0.1")
    assert check.verdict == probe.SKIP
    assert "the cloud overlay decides" in check.evidence


# ---------------------------------------------------------------------------
# 6 -- the otel route
# ---------------------------------------------------------------------------


def test_otel_passes_on_a_404(gateway: StubGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_otel_private(_settings(), "127.0.0.1")
    assert check.verdict == probe.PASS
    assert "otel.example.test answered 404" in check.evidence


def test_otel_passes_when_the_connection_is_refused(
    gateway: StubGateway, closed_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway, {probe.HTTPS_PORT: closed_port}))
    check = probe.check_otel_private(_settings(), "127.0.0.1")
    assert check.verdict == probe.PASS


def test_otel_fails_when_the_route_answers(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[("otel.example.test", "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_otel_private(_settings(), "127.0.0.1")
    assert check.verdict == probe.FAIL
    assert "while edge.ingest.otel.public is false" in check.evidence


def test_otel_skips_on_the_onprem_flavour() -> None:
    check = probe.check_otel_private(_settings(flavour="onprem"), "127.0.0.1")
    assert check.verdict == probe.SKIP
    assert "edge.flavour is onprem" in check.evidence


def test_otel_skips_when_the_dial_opts_the_door_in() -> None:
    check = probe.check_otel_private(_settings(otel_public=True), "127.0.0.1")
    assert check.verdict == probe.SKIP
    assert "edge.ingest.otel.public is true" in check.evidence


# ---------------------------------------------------------------------------
# 7 -- the admin UI routes
# ---------------------------------------------------------------------------


def test_admin_ui_passes_when_an_unopted_route_404s(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_ui(_settings(), "argocd", "127.0.0.1")
    assert check.verdict == probe.PASS
    assert "argocd.example.test answered 404" in check.evidence


def test_admin_ui_fails_when_an_unopted_route_answers(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[("argocd.example.test", "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_ui(_settings(), "argocd", "127.0.0.1")
    assert check.verdict == probe.FAIL
    assert "answered 200" in check.evidence


def test_admin_ui_skips_a_route_the_deployment_opted_in(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[("argocd.example.test", "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    settings = _settings(
        admin_uis_external=True,
        admin_uis_public={**dict.fromkeys(probe.ADMIN_UIS, False), "argocd": True},
    )
    check = probe.check_admin_ui(settings, "argocd", "127.0.0.1")
    assert check.verdict == probe.SKIP
    assert "is opted in" in check.evidence


def test_admin_ui_still_expects_absence_when_the_kill_switch_beats_the_flag(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """external false is absolute for the class, so a public flag changes nothing."""
    gateway.routes[("argocd.example.test", "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    settings = _settings(
        admin_uis_external=False,
        admin_uis_public={**dict.fromkeys(probe.ADMIN_UIS, False), "argocd": True},
    )
    check = probe.check_admin_ui(settings, "argocd", "127.0.0.1")
    assert check.verdict == probe.FAIL
    assert "edge.admin_uis.external is false" in check.evidence


def test_admin_ui_forgejo_is_probed_on_the_git_label(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The label is the canonical hostnames map's, not the key's own name."""
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_ui(_settings(), "forgejo", "127.0.0.1")
    assert "git.example.test" in check.evidence


# ---------------------------------------------------------------------------
# 8 -- the product login
# ---------------------------------------------------------------------------


def test_login_passes_when_the_page_answers(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[(PRODUCT, probe.LOGIN_PATH)] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_login(_settings(), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.PASS
    assert "/login answered 200" in check.evidence


def test_login_fails_when_the_route_404s(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_login(_settings(), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.FAIL
    assert "answered 404" in check.evidence


def test_login_fails_when_nothing_answers_at_all(
    gateway: StubGateway, closed_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway, {probe.HTTPS_PORT: closed_port}))
    check = probe.check_login(_settings(), PRODUCT, "127.0.0.1")
    assert check.verdict == probe.FAIL


# ---------------------------------------------------------------------------
# 9 -- the engine API on the product's own hostname
# ---------------------------------------------------------------------------


def test_engine_expectation_has_the_browser_families_and_the_keys_answer() -> None:
    absent, why = probe.engine_path_expectation("engine browser family", _settings())
    assert absent is False
    assert "cannot use the product without it" in why
    assert probe.engine_path_expectation("engine jwks", _settings())[0] is False


def test_engine_expectation_keeps_the_cli_families_private_until_their_switch() -> None:
    absent, why = probe.engine_path_expectation("engine cli family", _settings())
    assert absent is True
    assert "edge.engine_api.cli_families_public is false" in why
    opened = _settings(engine_cli_families_public=True)
    assert probe.engine_path_expectation("engine cli family", opened)[0] is False


def test_engine_expectation_opens_the_spec_with_the_cli_families() -> None:
    """The CLI is generated from the live spec, so the two move together."""
    assert probe.engine_path_expectation("engine openapi", _settings())[0] is True
    opened = _settings(engine_cli_families_public=True)
    assert probe.engine_path_expectation("engine openapi", opened)[0] is False


def test_engine_expectation_gives_scim_its_own_switch() -> None:
    assert probe.engine_path_expectation("engine scim", _settings())[0] is True
    assert probe.engine_path_expectation(
        "engine scim", _settings(engine_cli_families_public=True)
    )[0] is True
    assert probe.engine_path_expectation(
        "engine scim", _settings(engine_scim_public=True)
    )[0] is False


def test_engine_expectation_keeps_the_swagger_surface_off_under_every_switch() -> None:
    for settings in (
        _settings(),
        _settings(engine_cli_families_public=True),
        _settings(engine_scim_public=True),
        _settings(engine_cli_families_public=True, engine_scim_public=True),
    ):
        absent, why = probe.engine_path_expectation("engine docs", settings)
        assert absent is True
        assert "never on the public route" in why


def test_engine_browser_family_passes_when_the_product_hostname_answers_it(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[(PRODUCT, probe.ENGINE_BROWSER_PATH)] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_engine_path(
        _settings(), "engine browser family", probe.ENGINE_BROWSER_PATH, PRODUCT, "127.0.0.1"
    )
    assert check.verdict == probe.PASS
    assert "answered 200" in check.evidence


def test_engine_browser_family_fails_on_a_404_because_that_is_the_whole_fault(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dfe-ui loads and every call it makes lands on dfe-ui, which is the shape
    of a public product with no engine route behind it."""
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_engine_path(
        _settings(), "engine browser family", probe.ENGINE_BROWSER_PATH, PRODUCT, "127.0.0.1"
    )
    assert check.verdict == probe.FAIL
    assert "carries no route for it" in check.evidence


def test_engine_private_family_passes_on_a_404(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_engine_path(
        _settings(), "engine cli family", probe.ENGINE_CLI_PATH, PRODUCT, "127.0.0.1"
    )
    assert check.verdict == probe.PASS
    assert "no route is programmed for it" in check.evidence


def test_engine_private_family_fails_when_it_answers_with_its_switch_off(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[(PRODUCT, probe.ENGINE_CLI_PATH)] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_engine_path(
        _settings(), "engine cli family", probe.ENGINE_CLI_PATH, PRODUCT, "127.0.0.1"
    )
    assert check.verdict == probe.FAIL
    assert "edge.engine_api.cli_families_public is false" in check.evidence


def test_engine_private_family_fails_on_a_404_once_its_switch_is_on(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expectation flips with the dial, so an opt-in that rendered nothing
    is a failure rather than a quiet pass."""
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_engine_path(
        _settings(engine_cli_families_public=True), "engine cli family",
        probe.ENGINE_CLI_PATH, PRODUCT, "127.0.0.1",
    )
    assert check.verdict == probe.FAIL


def test_engine_docs_and_spec_pass_on_a_404(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    for name, path in (("engine docs", probe.ENGINE_DOCS_PATH),
                       ("engine openapi", probe.ENGINE_SPEC_PATH)):
        check = probe.check_engine_path(_settings(), name, path, PRODUCT, "127.0.0.1")
        assert check.verdict == probe.PASS, check


def test_engine_docs_fails_even_with_both_switches_on(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[(PRODUCT, probe.ENGINE_DOCS_PATH)] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    settings = _settings(engine_cli_families_public=True, engine_scim_public=True)
    check = probe.check_engine_path(
        settings, "engine docs", probe.ENGINE_DOCS_PATH, PRODUCT, "127.0.0.1"
    )
    assert check.verdict == probe.FAIL


def test_engine_jwks_fails_when_the_keys_do_not_answer(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A peer that cannot fetch the keys cannot verify a DFE token."""
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_engine_path(
        _settings(), "engine jwks", probe.ENGINE_JWKS_PATH, PRODUCT, "127.0.0.1"
    )
    assert check.verdict == probe.FAIL


def test_every_engine_check_skips_when_the_engine_is_off_the_public_hostname(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "_reach", lambda request: pytest.fail("a dial was made"))
    settings = _settings(engine_with_product=False)
    for name, path in probe.ENGINE_PATHS:
        check = probe.check_engine_path(settings, name, path, PRODUCT, "127.0.0.1")
        assert check.verdict == probe.SKIP
        assert "edge.engine_api.with_product is false" in check.evidence


def test_parse_edge_reads_the_engine_api_switches() -> None:
    settings = probe.parse_edge(probe.parse_yaml_subset(
        DIAL_TEXT + "  engine_api:\n    cli_families_public: true\n    scim_public: true\n"
    ))
    assert settings.engine_with_product is True
    assert settings.engine_cli_families_public is True
    assert settings.engine_scim_public is True


# ---------------------------------------------------------------------------
# The whole run
# ---------------------------------------------------------------------------


def _healthy_gateway(stub: StubGateway) -> None:
    """The routes a deployment that has done everything right answers on."""
    stub.routes[(PRODUCT, "/")] = 200
    stub.routes[(PRODUCT, probe.LOGIN_PATH)] = 200
    stub.routes[(PRODUCT, probe.ENGINE_BROWSER_PATH)] = 200
    stub.routes[(PRODUCT, probe.ENGINE_JWKS_PATH)] = 200
    stub.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    stub.burst = 3


def test_a_healthy_deployment_exits_zero_with_no_failed_check(
    gateway: StubGateway, closed_port: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    _healthy_gateway(gateway)
    closed = dict.fromkeys(probe.RECEIVER_INGEST_PORTS, closed_port)
    monkeypatch.setattr(probe, "_reach", _at_stub_over_tls(gateway, closed))

    rc = probe.cmd_edge_probe(_args(_dial_file(tmp_path)))
    err = capsys.readouterr().err

    assert rc == 0
    assert f"[{probe.FAIL}]" not in err
    assert "0 failed" in err


def test_one_failed_check_exits_non_zero(
    gateway: StubGateway, closed_port: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """An admin UI answering on the public edge is enough on its own."""
    _healthy_gateway(gateway)
    gateway.routes[("kafbat.example.test", "/")] = 200
    closed = dict.fromkeys(probe.RECEIVER_INGEST_PORTS, closed_port)
    monkeypatch.setattr(probe, "_reach", _at_stub_over_tls(gateway, closed))

    rc = probe.cmd_edge_probe(_args(_dial_file(tmp_path)))
    err = capsys.readouterr().err

    assert rc == 1
    assert "[FAIL] admin ui kafbat" in err


def test_every_check_is_reported_on_its_own_line(
    gateway: StubGateway, closed_port: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    _healthy_gateway(gateway)
    closed = dict.fromkeys(probe.RECEIVER_INGEST_PORTS, closed_port)
    monkeypatch.setattr(probe, "_reach", _at_stub_over_tls(gateway, closed))

    probe.cmd_edge_probe(_args(_dial_file(tmp_path)))
    err = capsys.readouterr().err

    for name in probe.check_names():
        assert f"] {name}:" in err
    # And the converse, or a check added to run_checks and not to check_names
    # would go missing from the all-skip path with nothing failing.
    reported = {line.split("] ", 1)[1].split(":", 1)[0] for line in err.splitlines() if "] " in line}
    assert reported == set(probe.check_names())


def test_an_edge_that_is_switched_off_skips_everything_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(probe, "_reach", lambda request: pytest.fail("a dial was made"))
    dial = _dial_file(tmp_path, DIAL_TEXT.replace("enabled: true", "enabled: false", 1))

    rc = probe.cmd_edge_probe(_args(dial))
    err = capsys.readouterr().err

    assert rc == 0
    assert err.count(f"[{probe.SKIP}]") == len(probe.check_names())
    assert "edge.enabled is false" in err


LEGACY_DIAL_TEXT = """\
profile: scale
ui:
  public:
    dfe_ui: true
    kafbat: true
  public_domain: example.test
  allowed_cidrs: "203.0.113.0/24"
ingest:
  mode: public
"""


def test_a_dial_still_on_the_deprecated_spellings_is_read_rather_than_skipped() -> None:
    """render_dial.py reads ui: and ingest: for one release, so a deployment on
    them is real -- a probe that skipped every check against it would exit zero
    and be read as a pass on a door nobody looked at."""
    settings = probe.parse_edge(probe.parse_yaml_subset(LEGACY_DIAL_TEXT))
    assert settings.domain == "example.test"
    assert settings.product_public is True
    assert settings.admin_uis_public["kafbat"] is True
    assert settings.receiver_mode == "public"
    assert tuple(settings.allowed_cidrs) == ("203.0.113.0/24",)


def test_an_inline_allow_list_loses_its_brackets_before_any_address_is_weighed() -> None:
    settings = probe.parse_edge(probe.parse_yaml_subset(
        DIAL_TEXT.replace('allowed_cidrs: ""', "allowed_cidrs: [10.0.0.0/8, 192.168.0.0/16]")
    ))
    assert tuple(settings.allowed_cidrs) == ("10.0.0.0/8", "192.168.0.0/16")


def test_a_run_that_proved_nothing_says_so_rather_than_reading_as_a_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(probe, "_reach", lambda request: pytest.fail("a dial was made"))
    dial = _dial_file(tmp_path, DIAL_TEXT.replace("enabled: true", "enabled: false", 1))

    rc = probe.cmd_edge_probe(_args(dial))
    err = capsys.readouterr().err

    assert rc == 0
    assert "NOTHING WAS PROVEN" in err


def test_a_name_that_does_not_resolve_and_no_target_skips_rather_than_guesses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(probe, "_resolves", lambda host: False)
    monkeypatch.setattr(probe, "_reach", lambda request: pytest.fail("a dial was made"))

    rc = probe.cmd_edge_probe(_args(_dial_file(tmp_path), target=""))
    err = capsys.readouterr().err

    assert rc == 0
    assert "--target" in err


def test_the_verb_refuses_a_dial_it_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = probe.cmd_edge_probe(_args(tmp_path / "absent.yaml"))
    assert rc == 1
    assert "no deployment dial" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def test_the_subparser_registers_the_verb_as_edge_probe() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    probe.add_edge_probe_subparser(sub)

    args = parser.parse_args(["edge-probe", "--target", "198.51.100.7"])

    assert args.func is probe.cmd_edge_probe
    assert args.target == "198.51.100.7"
    assert args.dial.endswith("deployment.yaml")


def test_the_parser_takes_no_credential(capsys: pytest.CaptureFixture) -> None:
    """Every check is what an unauthenticated caller sees, so there is nothing
    to pass and nothing that could land in argv."""
    parser = argparse.ArgumentParser(prog="dfe-ops")
    sub = parser.add_subparsers(dest="command")
    probe.add_edge_probe_subparser(sub)

    with pytest.raises(SystemExit):
        parser.parse_args(["edge-probe", "--help"])

    assert "--dial" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# admin-probe -- every admin UI the gateway lists loads through it
# ---------------------------------------------------------------------------

ARGO_HOST = "argocd.example.test"
ARGO = probe.AdminLink(name="Argo CD", url=f"https://{ARGO_HOST}")
KAFBAT = probe.AdminLink(name="Kafbat", url="https://kafbat.example.test")


def _redirect(stub: StubGateway, host: str, path: str, location: str, status: int = 302) -> None:
    stub.routes[(host, path)] = status
    stub.locations[(host, path)] = location


def test_an_admin_ui_answering_200_loads(gateway: StubGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway.routes[(ARGO_HOST, "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_link(ARGO, "127.0.0.1")
    assert check.verdict == probe.PASS, check
    assert check.evidence == f"https://{ARGO_HOST}/ answered 200"


def test_a_redirect_back_to_the_same_url_is_a_loop(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TLS-serving argocd-server behind a gateway that forwards plain HTTP."""
    _redirect(gateway, ARGO_HOST, "/", f"https://{ARGO_HOST}/", status=307)
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_link(ARGO, "127.0.0.1")
    assert check.verdict == probe.FAIL, check
    assert check.evidence == f"redirect loop: https://{ARGO_HOST}/ -> https://{ARGO_HOST}/"
    assert gateway.seen == [(ARGO_HOST, "/")]


def test_a_loop_through_a_second_page_is_a_loop(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect(gateway, ARGO_HOST, "/", "/applications")
    _redirect(gateway, ARGO_HOST, "/applications", "/")
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_link(ARGO, "127.0.0.1")
    assert check.verdict == probe.FAIL, check
    assert "redirect loop" in check.evidence
    assert f"https://{ARGO_HOST}/applications" in check.evidence


def test_a_5xx_fails(gateway: StubGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway.routes[(ARGO_HOST, "/")] = 502
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_link(ARGO, "127.0.0.1")
    assert check.verdict == probe.FAIL, check
    assert check.evidence.endswith("answered 502")


def test_a_5xx_behind_a_redirect_fails(gateway: StubGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    _redirect(gateway, ARGO_HOST, "/", "/login")
    gateway.routes[(ARGO_HOST, "/login")] = 503
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_link(ARGO, "127.0.0.1")
    assert check.verdict == probe.FAIL, check
    assert check.evidence == f"https://{ARGO_HOST}/ -> https://{ARGO_HOST}/login answered 503"


def test_a_login_handed_to_an_idp_loads_and_the_idp_is_not_dialled(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect(gateway, ARGO_HOST, "/", "https://id.example.com/authorize?client_id=dfe")
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_link(ARGO, "127.0.0.1")
    assert check.verdict == probe.PASS, check
    assert "hands the login to id.example.com (302)" in check.evidence
    assert [host for host, _ in gateway.seen] == [ARGO_HOST]


def test_a_login_page_on_the_deployments_own_domain_is_followed(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect(gateway, "kafbat.example.test", "/", "/login")
    gateway.routes[("kafbat.example.test", "/login")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_link(KAFBAT, "127.0.0.1")
    assert check.verdict == probe.PASS, check
    assert check.evidence == (
        "https://kafbat.example.test/ -> https://kafbat.example.test/login answered 200"
    )


def test_an_idp_on_the_deployments_own_domain_is_dialled_through_the_gateway(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect(gateway, ARGO_HOST, "/", "https://dex.example.test/auth?state=x")
    gateway.routes[("dex.example.test", "/auth?state=x")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_link(ARGO, "127.0.0.1")
    assert check.verdict == probe.PASS, check
    assert ("dex.example.test", "/auth?state=x") in gateway.seen


def test_a_redirect_chain_past_the_limit_fails(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    for step in range(6):
        _redirect(gateway, ARGO_HOST, "/" if step == 0 else f"/{step}", f"/{step + 1}")
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_link(ARGO, "127.0.0.1", max_redirects=3)
    assert check.verdict == probe.FAIL, check
    assert check.evidence.startswith("more than 3 redirects")
    assert len(gateway.seen) == 4


def test_a_redirect_with_nowhere_to_go_fails(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[(ARGO_HOST, "/")] = 302
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_link(ARGO, "127.0.0.1")
    assert check.verdict == probe.FAIL, check
    assert "no Location" in check.evidence


def test_a_login_wall_is_the_ui_answering(gateway: StubGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway.routes[(ARGO_HOST, "/")] = 401
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    assert probe.check_admin_link(ARGO, "127.0.0.1").verdict == probe.PASS


def test_an_admin_ui_that_answers_nothing_fails(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway, {probe.HTTPS_PORT: closed_port}))
    check = probe.check_admin_link(ARGO, "127.0.0.1")
    assert check.verdict == probe.FAIL, check
    assert "answered nothing through 127.0.0.1" in check.evidence


def test_a_redirect_to_a_port_that_cannot_exist_fails(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect(gateway, ARGO_HOST, "/", f"https://{ARGO_HOST}:99999/")
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_link(ARGO, "127.0.0.1")
    assert check.verdict == probe.FAIL, check
    assert "names no usable port" in check.evidence


def test_a_single_label_domain_is_still_a_domain(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.routes[("argocd.corp", "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    check = probe.check_admin_link(probe.AdminLink("Argo CD", "https://argocd.corp"), "127.0.0.1")
    assert check.verdict == probe.PASS, check


def test_an_undomained_link_skips_without_dialling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "_reach", lambda request: pytest.fail("a dial was made"))
    for url in ("https://argocd.", "https://argocd"):
        check = probe.check_admin_link(probe.AdminLink("Argo CD", url), "127.0.0.1")
        assert check.verdict == probe.SKIP, check
        assert "no domain" in check.evidence


# --- the list, read off the cluster ------------------------------------------
def _configmap(*links: probe.AdminLink) -> dict:
    """dfe-admin-links as the gateway chart renders it."""
    entries = [
        {"name": link.name, "purpose": "p", "url": link.url, "probe_url": "http://svc.ns.svc:80"}
        for link in links
    ]
    return {"data": {probe.ADMIN_LINKS_KEY: json.dumps(entries)}}


def _cluster(
    monkeypatch: pytest.MonkeyPatch, configmap: dict | None, gateways: list[dict] | None = None
) -> None:
    """A cluster carrying `configmap` (None: absent) and Gateways holding `gateways`."""

    def kubectl_json(kube: list[str], *argv: str) -> tuple[dict, str]:
        if "configmap" in argv:
            if configmap is None:
                return {}, 'Error from server (NotFound): configmaps "dfe-admin-links" not found'
            return configmap, ""
        if "gateway" in argv:
            return {"items": gateways or []}, ""
        pytest.fail(f"unexpected kubectl {argv}")

    monkeypatch.setattr(probe, "_kubectl_json", kubectl_json)


def _gateway(name: str, address: str) -> dict:
    return {"metadata": {"name": name}, "status": {"addresses": [{"value": address}]}}


GATEWAYS = [_gateway("dfe-gateway", "127.0.0.1")]


def test_the_rendered_list_parses() -> None:
    text = _configmap(ARGO, KAFBAT)["data"][probe.ADMIN_LINKS_KEY]
    assert probe.parse_admin_links(text) == [ARGO, KAFBAT]


@pytest.mark.parametrize("text", ["{}", "not json", '[{"name": "Argo CD"}]'])
def test_a_list_it_cannot_read_is_refused_by_name(text: str) -> None:
    with pytest.raises(probe.EdgeProbeError, match=probe.ADMIN_LINKS_KEY):
        probe.parse_admin_links(text)


def test_no_configmap_is_one_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    _cluster(monkeypatch, None, GATEWAYS)
    (check,) = probe.admin_ui_checks([], "dfe-local")
    assert check.verdict == probe.SKIP
    assert "no dfe-admin-links ConfigMap in dfe-local" in check.evidence


def test_an_empty_list_is_one_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    _cluster(monkeypatch, _configmap(), GATEWAYS)
    (check,) = probe.admin_ui_checks([], "dfe-local")
    assert check.verdict == probe.SKIP


def test_a_configmap_it_cannot_read_is_an_error_not_a_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "_kubectl_json", lambda kube, *argv: ({}, "Forbidden"))
    with pytest.raises(probe.EdgeProbeError, match="cannot read dfe-local/dfe-admin-links"):
        probe.admin_ui_checks([], "dfe-local")


def test_no_gateway_address_skips_every_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    _cluster(monkeypatch, _configmap(ARGO, KAFBAT), [])
    monkeypatch.setattr(probe, "_reach", lambda request: pytest.fail("a dial was made"))
    checks = probe.admin_ui_checks([], "dfe-local")
    assert [c.verdict for c in checks] == [probe.SKIP, probe.SKIP]
    assert all("no Gateway holds an address" in c.evidence for c in checks)


def test_a_gateway_this_machine_cannot_reach_skips_every_ui(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    _cluster(monkeypatch, _configmap(ARGO, KAFBAT), GATEWAYS)
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway, {probe.HTTPS_PORT: closed_port}))
    checks = probe.admin_ui_checks([], "dfe-local")
    assert [c.verdict for c in checks] == [probe.SKIP, probe.SKIP]
    assert "does not answer from this machine" in checks[0].evidence


def test_each_listed_ui_gets_its_own_verdict(gateway: StubGateway, monkeypatch: pytest.MonkeyPatch) -> None:
    _cluster(monkeypatch, _configmap(ARGO, KAFBAT), GATEWAYS)
    _redirect(gateway, ARGO_HOST, "/", f"https://{ARGO_HOST}/", status=307)
    gateway.routes[("kafbat.example.test", "/")] = 200
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    checks = probe.admin_ui_checks([], "dfe-local")
    assert [(c.name, c.verdict) for c in checks] == [
        ("admin ui Argo CD", probe.FAIL),
        ("admin ui Kafbat", probe.PASS),
    ]
    assert probe.exit_code(checks) == 1


def test_the_deployments_own_gateway_is_dialled_before_any_other(monkeypatch: pytest.MonkeyPatch) -> None:
    _cluster(monkeypatch, None, [_gateway("someone-elses", "192.0.2.1"), _gateway("dfe-gateway", "192.0.2.2")])
    assert probe.gateway_address([]) == "192.0.2.2"
    _cluster(monkeypatch, None, [_gateway("someone-elses", "192.0.2.1")])
    assert probe.gateway_address([]) == "192.0.2.1"


# --- the verb ----------------------------------------------------------------
def _admin_args(**overrides: object) -> argparse.Namespace:
    values = {"namespace": "dfe-local", "kubeconfig": "", "context": "", "gateway": "dfe-gateway",
              "target": "", "wait": 0.0}
    return argparse.Namespace(**{**values, **overrides})


def test_a_failing_ui_fails_the_verb(
    gateway: StubGateway, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _cluster(monkeypatch, _configmap(ARGO), GATEWAYS)
    _redirect(gateway, ARGO_HOST, "/", f"https://{ARGO_HOST}/", status=307)
    monkeypatch.setattr(probe, "_reach", _at_stub(gateway))
    assert probe.cmd_admin_probe(_admin_args()) == 1
    err = capsys.readouterr().err
    assert "[FAIL] admin ui Argo CD: redirect loop" in err
    assert "=== admin-probe: 0 passed, 1 failed, 0 skipped ===" in err


def test_the_wait_re_probes_until_the_ui_loads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    passes = iter([
        [probe.Check("admin ui Argo CD", probe.FAIL, "redirect loop")],
        [probe.Check("admin ui Argo CD", probe.PASS, "answered 200")],
    ])
    monkeypatch.setattr(probe, "admin_ui_checks", lambda *args: next(passes))
    monkeypatch.setattr(probe.time, "sleep", lambda seconds: None)
    assert probe.cmd_admin_probe(_admin_args(wait=60.0)) == 0
    err = capsys.readouterr().err
    assert "admin ui Argo CD not loading yet" in err
    assert "[PASS] admin ui Argo CD" in err


def test_a_list_it_cannot_read_fails_the_verb(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(probe, "_kubectl_json", lambda kube, *argv: ({}, "Forbidden"))
    assert probe.cmd_admin_probe(_admin_args()) == 1
    assert "dfe-ops admin-probe: cannot read" in capsys.readouterr().err


def test_the_subparser_registers_the_verb_as_admin_probe() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    probe.add_admin_probe_subparser(sub)
    args = parser.parse_args(["admin-probe", "--namespace", "dfe-prod", "--wait", "120"])
    assert args.func is probe.cmd_admin_probe
    assert (args.namespace, args.wait, args.gateway) == ("dfe-prod", 120.0, "dfe-gateway")
