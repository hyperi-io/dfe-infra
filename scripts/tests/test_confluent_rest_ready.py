#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_confluent_rest_ready.py
#  Purpose:      Prove the confluent-cloud body holds its keys, ACLs and topics
#                behind a probe of the cluster's REST route, and that the probe
#                counts only handshakes in a row as ready.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The REST route probe in front of a public Confluent cluster.

Confluent's shared ingress resets TLS for a cluster it holds no route for, and
every write over the REST endpoint fails the apply on its first reset, so
main.tf runs scripts/wait-rest-ready.sh before any of them.

contract-confluent-cloud.tftest.hcl proves the probe is planned on public
connectivity only, and what it targets. Neither a depends_on edge nor a
provisioner is a resource attribute, so the wiring is read here as HCL. The
script itself runs against stand-in endpoints on 127.0.0.1 -- one that resets
connections, one that answers 401 over TLS, and a closed port -- so nothing
here needs the network.

    python3 -m pytest scripts/tests/test_confluent_rest_ready.py -q
"""

import contextlib
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BODY = REPO_ROOT / "terraform" / "modules" / "managed-kafka" / "confluent-cloud"
SCRIPT = BODY / "scripts" / "wait-rest-ready.sh"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_tofu_literals  # noqa: E402

# Every resource that writes over the REST endpoint, or mints a credential for it.
REST_WRITERS = (
    ("confluent_api_key", "manager"),
    ("confluent_api_key", "dfe"),
    ("confluent_kafka_acl", "dfe"),
    ("confluent_kafka_topic", "landing"),
)

# What the endpoint answers a request carrying no credential.
UNAUTHORISED = b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


def _blocks() -> list[check_tofu_literals.Block]:
    """Every top-level block of the body's own .tf files."""
    return [
        block
        for path in sorted(BODY.glob("*.tf"))
        for block in check_tofu_literals.parse(path.read_text(encoding="utf-8"))
    ]


def _resource(kind: str, name: str) -> check_tofu_literals.Block:
    """The body's resource block of this type and name."""
    for block in _blocks():
        if block.kind == "resource" and block.labels == [kind, name]:
            return block
    raise LookupError(f"no resource {kind}.{name} in {BODY}")


def _expression(block: check_tofu_literals.Block, name: str) -> str:
    """An attribute's expression with every space and line break removed."""
    for attribute in block.attributes:
        if attribute.name == name:
            return "".join(token.text for token in attribute.tokens if token.kind != "newline")
    raise LookupError(f"{block.labels} sets no {name}")


def _depends_on(kind: str, name: str) -> list[str]:
    """The addresses a resource's depends_on names."""
    return _expression(_resource(kind, name), "depends_on").strip("[]").split(",")


def _local_number(name: str) -> int:
    """The literal number a named local of the body holds."""
    for block in _blocks():
        if block.kind != "locals":
            continue
        for attribute in block.attributes:
            if attribute.name == name:
                value = [token for token in attribute.tokens if token.kind != "newline"]
                if len(value) != 1 or value[0].kind != "number":
                    raise ValueError(f"local.{name} is not a literal number")
                return int(value[0].text)
    raise LookupError(f"local.{name} is set in no locals block of {BODY}")


def _provisioner() -> check_tofu_literals.Block:
    """The probe's local-exec provisioner."""
    gate = _resource("terraform_data", "rest_ready")
    found = [b for b in gate.blocks if b.kind == "provisioner" and b.labels == ["local-exec"]]
    if len(found) != 1:
        raise LookupError(f"terraform_data.rest_ready carries {len(found)} local-exec provisioners")
    return found[0]


def test_the_probe_is_planned_for_a_public_cluster_only() -> None:
    assert _expression(_resource("terraform_data", "rest_ready"), "count") == "local.private?0:1"


def test_every_write_over_the_rest_endpoint_waits_for_the_probe() -> None:
    missing = [
        f"{kind}.{name}"
        for kind, name in REST_WRITERS
        if "terraform_data.rest_ready" not in _depends_on(kind, name)
    ]
    assert missing == [], f"{missing} can reach the REST endpoint before its route exists"


def test_the_manager_key_still_waits_for_its_role_binding() -> None:
    assert "confluent_role_binding.manager" in _depends_on("confluent_api_key", "manager")


def test_the_probe_runs_once_at_create_and_fails_the_apply_when_it_fails() -> None:
    settings = {attribute.name for attribute in _provisioner().attributes}
    assert "when" not in settings, "a destroy-time probe would never run before the writes"
    assert "on_failure" not in settings, "a probe that may fail and continue holds nothing back"


def test_the_probe_hands_the_script_its_arguments_in_order() -> None:
    command = _expression(_provisioner(), "command")
    assert "scripts/wait-rest-ready.sh" in command
    arguments = (
        '"${self.output.url}"'
        "${local.rest_ready_handshakes}"
        "${local.rest_ready_timeout_seconds}"
        "${local.rest_ready_interval_seconds}"
    )
    # Compared without spaces: the order is what the script reads, not the spacing.
    assert arguments in command.replace(" ", ""), command


def test_the_probe_constants_can_succeed_inside_their_own_bound() -> None:
    handshakes = _local_number("rest_ready_handshakes")
    timeout = _local_number("rest_ready_timeout_seconds")
    interval = _local_number("rest_ready_interval_seconds")
    assert handshakes > 1, "one handshake proves one load balancer has the route, not all of them"
    assert interval > 0, "back-to-back attempts can land on the same load balancer"
    assert handshakes * interval < timeout


@pytest.fixture(scope="module")
def tls(tmp_path_factory: pytest.TempPathFactory) -> tuple[ssl.SSLContext, Path]:
    """A server context for 127.0.0.1, and the certificate a client trusts to reach it."""
    openssl = shutil.which("openssl")
    assert openssl, "openssl mints the stand-in endpoint's certificate"
    work = tmp_path_factory.mktemp("rest-ready-tls")
    cert = work / "cert.pem"
    key = work / "key.pem"
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context, cert


def _reset(conn: socket.socket) -> None:
    """Take the ClientHello, then drop the connection with a TCP reset, as the ingress does."""
    with contextlib.suppress(OSError):
        conn.recv(4096)
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))


def _answer(conn: socket.socket, context: ssl.SSLContext) -> None:
    """Complete the handshake and answer 401, as the endpoint answers a request with no credential."""
    # A client that refuses the certificate ends the handshake, and that is its whole answer.
    with contextlib.suppress(OSError), context.wrap_socket(conn, server_side=True) as stream:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = stream.recv(4096)
            if not chunk:
                return
            request += chunk
        stream.sendall(UNAUTHORISED)


@contextlib.contextmanager
def endpoint(context: ssl.SSLContext, plan: list[str]) -> Iterator[tuple[int, list[str]]]:
    """A stand-in REST endpoint that resets or answers each connection in `plan` order.

    Every connection past the end of `plan` is answered. Yields the port and the
    list of what each connection got, in arrival order.
    """
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.1)
    served: list[str] = []
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            action = plan[len(served)] if len(served) < len(plan) else "answer"
            served.append(action)
            with conn:
                conn.settimeout(5)
                if action == "reset":
                    _reset(conn)
                else:
                    _answer(conn, context)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], served
    finally:
        stop.set()
        thread.join(timeout=5)
        listener.close()


def _env(ca: Path | None = None, curl_home: Path | None = None) -> dict[str, str]:
    """This process's environment, with curl's trust store and rc file set by the test alone."""
    env = dict(os.environ)
    for name in ("CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_HOME"):
        env.pop(name, None)
    # The stand-ins sit on 127.0.0.1, which no proxy the runner sets may intercept.
    env["NO_PROXY"] = env["no_proxy"] = "*"
    if ca is not None:
        env["CURL_CA_BUNDLE"] = str(ca)
    if curl_home is not None:
        env["CURL_HOME"] = str(curl_home)
    return env


def run_probe(
    *args: str, ca: Path | None = None, curl_home: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """wait-rest-ready.sh with these arguments, bounded so a hang fails the test."""
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(ca, curl_home),
        timeout=60,
        check=False,
    )


def _url(port: int) -> str:
    return f"https://127.0.0.1:{port}/kafka/v3/clusters/lkc-000000"


def test_a_401_over_tls_is_a_routed_endpoint(tls: tuple[ssl.SSLContext, Path]) -> None:
    context, ca = tls
    with endpoint(context, []) as (port, served):
        out = run_probe(_url(port), "3", "30", "0", ca=ca)
    assert out.returncode == 0, out.stdout + out.stderr
    assert served == ["answer"] * 3, "it stops at the third handshake in a row"
    assert "HTTP 401 from 127.0.0.1 (3/3 in a row)" in out.stdout, out.stdout


def test_a_reset_starts_the_count_again(tls: tuple[ssl.SSLContext, Path]) -> None:
    """Two load balancers with the route and one without must not read as routed."""
    context, ca = tls
    with endpoint(context, ["answer", "answer", "reset"]) as (port, served):
        out = run_probe(_url(port), "3", "30", "0", ca=ca)
    assert out.returncode == 0, out.stdout + out.stderr
    assert served == ["answer", "answer", "reset", "answer", "answer", "answer"], served
    third = [line for line in out.stdout.splitlines() if " attempt 3: " in line]
    assert third, out.stdout
    assert third[0].endswith("(0/3 in a row)"), out.stdout


def test_an_endpoint_that_keeps_resetting_fails_at_the_bound_and_names_the_host(
    tls: tuple[ssl.SSLContext, Path],
) -> None:
    context, ca = tls
    started = time.monotonic()
    with endpoint(context, ["reset"] * 50) as (port, served):
        out = run_probe(_url(port), "3", "3", "1", ca=ca)
    elapsed = time.monotonic() - started
    assert out.returncode == 1, out.stdout + out.stderr
    assert len(served) >= 2, "it retries a reset rather than failing on the first one"
    assert "Connection reset by peer" in out.stdout, out.stdout
    assert "127.0.0.1 did not answer 3 TLS handshakes in a row within 3s" in out.stderr, out.stderr
    assert elapsed < 20, f"a 3s bound took {elapsed:.0f}s"


def test_a_closed_port_fails_at_the_bound() -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]
    out = run_probe(_url(port), "3", "2", "1")
    assert out.returncode == 1, out.stdout + out.stderr
    assert "attempt 2: " in out.stdout, out.stdout
    assert "127.0.0.1 did not answer 3 TLS handshakes in a row within 2s" in out.stderr, out.stderr


def test_an_untrusted_certificate_is_not_a_handshake(tls: tuple[ssl.SSLContext, Path]) -> None:
    """The probe verifies the endpoint, so a certificate curl cannot trust is a failure."""
    context, _ = tls
    with endpoint(context, []) as (port, served):
        out = run_probe(_url(port), "1", "2", "1")
    assert out.returncode == 1, out.stdout + out.stderr
    assert served, "the probe reached the endpoint"
    assert "certificate" in out.stdout, out.stdout


def test_a_curlrc_cannot_turn_the_401_into_a_failure(
    tls: tuple[ssl.SSLContext, Path], tmp_path: Path
) -> None:
    context, ca = tls
    (tmp_path / ".curlrc").write_text("--fail\n", encoding="utf-8", newline="\n")
    with endpoint(context, []) as (port, _served):
        bare = subprocess.run(
            ["curl", "--silent", "--output", "/dev/null", _url(port)],
            env=_env(ca, tmp_path),
            timeout=30,
            check=False,
        )
        out = run_probe(_url(port), "1", "3", "1", ca=ca, curl_home=tmp_path)
    assert bare.returncode == 22, "the rc file is read, so it fails a curl that does not skip it"
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("https://127.0.0.1:1/",),
        ("https://127.0.0.1:1/", "3", "900"),
        ("https://127.0.0.1:1/", "three", "900", "5"),
        ("https://127.0.0.1:1/", "0", "900", "5"),
        ("https://127.0.0.1:1/", "3", "-1", "5"),
    ],
)
def test_bad_arguments_are_refused_before_any_attempt(args: tuple[str, ...]) -> None:
    out = run_probe(*args)
    assert out.returncode == 1, out.stdout + out.stderr
    assert out.stdout == "", "no attempt is made on arguments it cannot use"
    assert out.stderr.startswith("wait-rest-ready: "), out.stderr
