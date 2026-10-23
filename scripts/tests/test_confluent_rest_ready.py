#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_confluent_rest_ready.py
#  Purpose:      Prove the confluent-cloud body holds its ACLs behind a probe
#                that lists the topics with the manager key, and that
#                the probe counts only HTTP 200s in a row as ready.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The REST endpoint probe in front of a public Confluent cluster's writes.

Confluent's shared ingress resets TLS for a cluster it holds no route for, a key
the endpoint has not synced gets a 401, and an ACL write fails the apply on
either, so main.tf runs scripts/wait-rest-ready.sh with the manager key before
any of them.

contract-confluent-cloud.tftest.hcl proves the probe is planned on public
connectivity only, and what it targets. Neither a depends_on edge nor a
provisioner is a resource attribute, so the wiring is read here as HCL. The
script itself runs against stand-in endpoints on 127.0.0.1 -- one that resets
connections, one that answers over TLS, and a closed port -- so nothing here
needs the network.

    python3 -m pytest scripts/tests/test_confluent_rest_ready.py -q
"""

import base64
import contextlib
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BODY = REPO_ROOT / "terraform" / "modules" / "managed-kafka" / "confluent-cloud"
SCRIPT = BODY / "scripts" / "wait-rest-ready.sh"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_tofu_literals  # noqa: E402
import render_dial  # noqa: E402

# Every resource that writes over the REST endpoint.
REST_WRITERS = (("confluent_kafka_acl", "dfe"),)

# A stand-in credential, in the shape Confluent issues one.
KEY = "TESTKEY0000000AB"
SECRET = "stand-in+secret/for+the+probe/only00000000000000000000000000000"
WRONG_SECRET = "not-the-stand-in-secret"

OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
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
    """The addresses a resource's depends_on names, or none when it sets no depends_on."""
    block = _resource(kind, name)
    if not any(attribute.name == "depends_on" for attribute in block.attributes):
        return []
    return _expression(block, "depends_on").strip("[]").split(",")


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
    assert missing == [], f"{missing} can reach the REST endpoint before it answers the manager key"


def test_no_key_waits_for_the_probe() -> None:
    """The probe signs in with the manager key, so the keys come first."""
    waiting = [
        name
        for name in ("manager", "dfe")
        if "terraform_data.rest_ready" in _depends_on("confluent_api_key", name)
    ]
    assert waiting == [], f"confluent_api_key {waiting} waits on a probe that needs a key to run"


def test_neither_key_takes_the_providers_wait() -> None:
    """The provider's wait fails the apply on one reset its retries do not outlast."""
    for name in ("manager", "dfe"):
        assert _expression(_resource("confluent_api_key", name), "disable_wait_for_ready") == "true"


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
        "${var.rest_ready.successes}"
        "${var.rest_ready.timeout_seconds}"
        "${var.rest_ready.interval_seconds}"
    )
    # Compared without spaces: the order is what the script reads, not the spacing.
    assert arguments in command.replace(" ", ""), command


def test_the_manager_key_reaches_the_probe_through_the_environment_alone() -> None:
    command = _expression(_provisioner(), "command")
    assert "confluent_api_key" not in command, "a credential on the command line shows in a process listing"
    environment = _expression(_provisioner(), "environment")
    assert "CONFLUENT_REST_KEY=confluent_api_key.manager.id" in environment, environment
    assert "CONFLUENT_REST_SECRET=confluent_api_key.manager.secret" in environment, environment


def test_the_rendered_wait_can_succeed_inside_its_own_bound() -> None:
    wait = render_dial.CONFLUENT_REST_READY_DEFAULTS
    assert wait["successes"] > 1, "one answer proves one load balancer has the route, not all of them"
    assert wait["interval_seconds"] > 0, "back-to-back attempts can land on the same load balancer"
    assert wait["successes"] * wait["interval_seconds"] < wait["timeout_seconds"]


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


def _authorised(request: bytes) -> bool:
    """Whether the request carries the stand-in key as HTTP basic auth."""
    expected = base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
    for line in request.decode("latin-1").split("\r\n")[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "authorization":
            return value.strip() == f"Basic {expected}"
    return False


def _answer(conn: socket.socket, context: ssl.SSLContext, synced: bool) -> None:
    """Complete the handshake and answer 200 to the stand-in key, or 401 while it is not synced."""
    # A client that refuses the certificate ends the handshake, and that is its whole answer.
    with contextlib.suppress(OSError), context.wrap_socket(conn, server_side=True) as stream:
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = stream.recv(4096)
            if not chunk:
                return
            request += chunk
        stream.sendall(OK if synced and _authorised(request) else UNAUTHORISED)


@contextlib.contextmanager
def endpoint(context: ssl.SSLContext, plan: list[str]) -> Iterator[tuple[int, list[str]]]:
    """A stand-in REST endpoint that resets, refuses or answers each connection in `plan` order.

    "reset" drops the connection, "unsynced" answers 401 whatever the request
    carries, and "answer" checks the credential. Every connection past the end
    of `plan` is answered. Yields the port and what each connection got.
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
                    _answer(conn, context, synced=action == "answer")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], served
    finally:
        stop.set()
        thread.join(timeout=5)
        listener.close()


def _env(
    ca: Path | None = None,
    curl_home: Path | None = None,
    key: str | None = KEY,
    secret: str | None = SECRET,
    path: Path | None = None,
) -> dict[str, str]:
    """This process's environment, with curl's trust store, rc file and the credential set by the test alone."""
    env = dict(os.environ)
    for name in ("CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_HOME",
                 "CONFLUENT_REST_KEY", "CONFLUENT_REST_SECRET"):
        env.pop(name, None)
    # The stand-ins sit on 127.0.0.1, which no proxy the runner sets may intercept.
    env["NO_PROXY"] = env["no_proxy"] = "*"
    if ca is not None:
        env["CURL_CA_BUNDLE"] = str(ca)
    if curl_home is not None:
        env["CURL_HOME"] = str(curl_home)
    if key is not None:
        env["CONFLUENT_REST_KEY"] = key
    if secret is not None:
        env["CONFLUENT_REST_SECRET"] = secret
    if path is not None:
        env["PATH"] = f"{path}{os.pathsep}{env.get('PATH', '')}"
    return env


def run_probe(
    *args: str,
    ca: Path | None = None,
    curl_home: Path | None = None,
    key: str | None = KEY,
    secret: str | None = SECRET,
    path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """wait-rest-ready.sh with these arguments, bounded so a hang fails the test."""
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(ca, curl_home, key, secret, path),
        timeout=60,
        check=False,
    )


def _url(port: int) -> str:
    return f"https://127.0.0.1:{port}/kafka/v3/clusters/lkc-000000/topics"


@pytest.fixture
def clock(tmp_path: Path) -> Path:
    """A `date +%s` and a `sleep` on PATH that share one counter, so a bound is counted, not raced.

    The probe reads its clock through `date` and waits through `sleep`, so with
    these first on PATH every interval it waits moves the counter by exactly
    that much and no real time passes: the attempts a bound allows are a fixed
    number, whatever the host's load.
    """
    real_date = shutil.which("date")
    assert real_date, "date is on PATH"
    counter = tmp_path / "clock"
    counter.write_text("1000000\n", encoding="utf-8")
    bin_dir = tmp_path / "clock-bin"
    bin_dir.mkdir()
    (bin_dir / "date").write_text(
        "#!/usr/bin/env bash\n"
        f'if [[ $# -eq 1 && "$1" == "+%s" ]]; then cat "{counter}"; else exec "{real_date}" "$@"; fi\n',
        encoding="utf-8",
        newline="\n",
    )
    (bin_dir / "sleep").write_text(
        "#!/usr/bin/env bash\n"
        f'now="$(cat "{counter}")"\n'
        f'printf "%s\\n" "$(( now + $1 ))" > "{counter}"\n',
        encoding="utf-8",
        newline="\n",
    )
    for shim in ("date", "sleep"):
        (bin_dir / shim).chmod(0o755)
    return bin_dir


def _attempts(stdout: str) -> int:
    """How many attempts the probe printed."""
    return sum(1 for line in stdout.splitlines() if " attempt " in line)


def test_a_200_for_the_key_is_a_ready_endpoint(tls: tuple[ssl.SSLContext, Path]) -> None:
    context, ca = tls
    with endpoint(context, []) as (port, served):
        out = run_probe(_url(port), "3", "30", "0", ca=ca)
    assert out.returncode == 0, out.stdout + out.stderr
    assert served == ["answer"] * 3, "it stops at the third 200 in a row"
    assert "HTTP 200 from 127.0.0.1 (3/3 in a row)" in out.stdout, out.stdout


def test_a_401_is_not_ready_and_starts_the_count_again(tls: tuple[ssl.SSLContext, Path]) -> None:
    """A routed endpoint that has not synced the key answers 401, which no write survives."""
    context, ca = tls
    with endpoint(context, ["answer", "unsynced", "answer"]) as (port, served):
        out = run_probe(_url(port), "2", "30", "0", ca=ca)
    assert out.returncode == 0, out.stdout + out.stderr
    assert served == ["answer", "unsynced", "answer", "answer"], served
    second = [line for line in out.stdout.splitlines() if " attempt 2: " in line]
    assert second, out.stdout
    assert second[0].endswith("HTTP 401 from 127.0.0.1 (0/2 in a row)"), out.stdout


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
    tls: tuple[ssl.SSLContext, Path], clock: Path
) -> None:
    """A 3s bound at a 1s interval is attempts at 0s, 1s and 2s, and a fourth would land past it."""
    context, ca = tls
    with endpoint(context, ["reset"] * 50) as (port, served):
        out = run_probe(_url(port), "3", "3", "1", ca=ca, path=clock)
    assert out.returncode == 1, out.stdout + out.stderr
    assert _attempts(out.stdout) == 3, out.stdout
    assert served == ["reset"] * 3, "it retries a reset rather than failing on the first one"
    assert "Connection reset by peer" in out.stdout, out.stdout
    assert "127.0.0.1 did not list its topics with HTTP 200 3 times in a row within 3s (3 attempts" in out.stderr, out.stderr


def test_a_key_that_never_syncs_fails_at_the_bound(tls: tuple[ssl.SSLContext, Path], clock: Path) -> None:
    context, ca = tls
    with endpoint(context, ["unsynced"] * 50) as (port, served):
        out = run_probe(_url(port), "2", "3", "1", ca=ca, path=clock)
    assert out.returncode == 1, out.stdout + out.stderr
    assert served == ["unsynced"] * 3, served
    assert "last: HTTP 401 from 127.0.0.1" in out.stderr, out.stderr


def test_a_wrong_key_is_not_ready(tls: tuple[ssl.SSLContext, Path]) -> None:
    """The stand-in answers 200 to the stand-in key alone, so a pass proves the key was sent."""
    context, ca = tls
    with endpoint(context, []) as (port, _served):
        out = run_probe(_url(port), "1", "2", "1", ca=ca, secret=WRONG_SECRET)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "HTTP 401" in out.stdout, out.stdout


def test_a_closed_port_fails_at_the_bound(clock: Path) -> None:
    """A 2s bound at a 1s interval is exactly two attempts, counted on the stand-in clock."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]
    out = run_probe(_url(port), "3", "2", "1", path=clock)
    assert out.returncode == 1, out.stdout + out.stderr
    assert _attempts(out.stdout) == 2, out.stdout
    assert "127.0.0.1 did not list its topics with HTTP 200 3 times in a row within 2s (2 attempts" in out.stderr, out.stderr


def test_the_probe_waits_on_the_clock_it_is_handed(clock: Path) -> None:
    """Every wait goes through the stand-in, so the counter moves and no real interval passes."""
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]
    out = run_probe(_url(port), "3", "60", "30", path=clock)
    assert out.returncode == 1, out.stdout + out.stderr
    assert _attempts(out.stdout) == 2, out.stdout
    assert (clock.parent / "clock").read_text(encoding="utf-8").strip() == "1000030"


def test_an_untrusted_certificate_is_not_an_answer(tls: tuple[ssl.SSLContext, Path]) -> None:
    """The probe verifies the endpoint, so a certificate curl cannot trust is a failure."""
    context, _ = tls
    with endpoint(context, []) as (port, served):
        out = run_probe(_url(port), "1", "2", "1")
    assert out.returncode == 1, out.stdout + out.stderr
    assert served, "the probe reached the endpoint"
    assert "certificate" in out.stdout, out.stdout


def test_a_curlrc_cannot_reach_the_probe(tls: tuple[ssl.SSLContext, Path], tmp_path: Path) -> None:
    context, ca = tls
    # Every connection sent to a closed port, which the probe must never read.
    (tmp_path / ".curlrc").write_text(
        '--connect-to "::127.0.0.1:1"\n', encoding="utf-8", newline="\n"
    )
    with endpoint(context, []) as (port, _served):
        bare = subprocess.run(
            ["curl", "--silent", "--output", "/dev/null", _url(port)],
            env=_env(ca, tmp_path),
            timeout=30,
            check=False,
        )
        out = run_probe(_url(port), "1", "3", "1", ca=ca, curl_home=tmp_path)
    assert bare.returncode == 7, "the rc file is read, so it misroutes a curl that does not skip it"
    assert out.returncode == 0, out.stdout + out.stderr


def test_the_secret_is_never_an_argument_and_never_printed(
    tls: tuple[ssl.SSLContext, Path], tmp_path: Path
) -> None:
    """A curl on PATH ahead of the real one records every argument it is handed."""
    context, ca = tls
    real = shutil.which("curl")
    assert real, "curl is on PATH"
    argv = tmp_path / "argv"
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "curl").write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "{argv}"\nexec "{real}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    (shim / "curl").chmod(0o755)
    with endpoint(context, ["unsynced", "reset"]) as (port, _served):
        out = run_probe(_url(port), "2", "30", "0", ca=ca, path=shim)
    assert out.returncode == 0, out.stdout + out.stderr
    recorded = argv.read_text(encoding="utf-8")
    assert "--config" in recorded, "the shim saw the probe's curl"
    assert SECRET not in recorded, "the secret sat in curl's argument list"
    assert SECRET not in out.stdout + out.stderr, "the secret reached the probe's output"


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


@pytest.mark.parametrize(
    ("key", "secret"),
    [
        (None, SECRET),
        (KEY, None),
        (KEY, ""),
        (KEY, 'with"quote'),
        (KEY, "with\\backslash"),
        (KEY, "with\nnewline"),
    ],
)
def test_a_missing_or_unsafe_credential_is_refused_before_any_attempt(
    key: str | None, secret: str | None
) -> None:
    out = run_probe("https://127.0.0.1:1/", "3", "900", "5", key=key, secret=secret)
    assert out.returncode == 1, out.stdout + out.stderr
    assert out.stdout == "", "no attempt is made with a credential it cannot send"
    assert out.stderr.startswith("wait-rest-ready: "), out.stderr
    if secret:
        assert secret not in out.stderr, "the refusal names the problem, never the secret"
