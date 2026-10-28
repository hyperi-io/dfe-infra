#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_smoke_hyperdx_seams.py
#  Purpose:      Prove bootstrap/smoke-test-hyperdx.sh probes from inside a pod that has
#                node and neither curl nor wget, and never passes on an unread answer.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The HyperDX seam smoke test against a pod shaped like the dfe-hyperdx image.

The image ships node and neither curl nor wget. A fake `kubectl` first on PATH
runs `kubectl exec` commands locally with a PATH holding only `env`, `sh` and
(optionally) `node`, and answers the deploy reads from a fixture. The settings sit
in an envFrom ConfigMap, as the thin chart puts them, so only a read of the running
container's environment finds them, and an exec naming any container but
`dfe-hyperdx` is refused. The JWKS,
ClickHouse and frontend are one local HTTP server, so the probes' JavaScript really
runs under node and the assertions are on what the server received.

The embed checks must only run on headers that were read: a probe that fails prints
nothing, and an error message from a missing tool used to count as headers, so
"X-Frame-Options NOT set" passed on a pod that had answered nothing.

    python3 scripts/tests/test_smoke_hyperdx_seams.py

Needs node on PATH, as the CI runner has. No third-party deps and no test runner.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SMOKE = REPO_ROOT / "bootstrap" / "smoke-test-hyperdx.sh"

# The query reader the platform team's connection reads as, and the Secret beside
# the engine its password is in. HyperDX's own pod holds no ClickHouse credential.
CH_USER = "dfe_query_reader"
CH_SECRET = "query-reader-clickhouse"
CH_FIXTURE_VALUE = "fixture-clickhouse-value"
WRONG_VALUE = "not-the-fixture-value"
TOKEN = "engine.issued.jwt"

JWKS = "HyperDX can fetch the engine JWKS"
ES384 = "engine JWKS advertises an ES384 key"
PING = "HyperDX pod reaches ClickHouse over HTTP"
TABLE = "otel table dfe.otel_logs is queryable from HyperDX"
CSP = "CSP frame-ancestors present"
XFO = "X-Frame-Options NOT set"
BEARER = "HyperDX accepts an engine-issued token"
REFUSES = "HyperDX refuses a token it did not issue"
EMBED_SKIPPED = "embed headers -- could not read response headers"
ENV_UNREAD = "read HyperDX's runtime env"

# `kubectl exec` runs the command with PATH set to the pod's bin dir, so a tool the
# image lacks fails exactly as it does in the pod.
FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, re, subprocess, sys

args = sys.argv[1:]
with open(os.environ["FAKE_POD"], encoding="utf-8") as fh:
    pod = json.load(fh)

if "get" in args:
    rest = args[args.index("get") + 1:]
    name = rest[1]
    if rest[0] == "secret":
        held = pod.get("secrets", {}).get(name)
        if held is None:
            sys.exit(1)
        import base64
        sys.stdout.write(base64.b64encode(held.encode()).decode())
        sys.exit(0)
    engine = "engine_token" in pod or "engine_env" in pod
    present = name == "dfe-hyperdx" or (name == "dfe-engine" and engine)
    if "-o" in rest:
        wanted = re.search(r'@\\.name=="([A-Za-z_]+)"', rest[rest.index("-o") + 1])
        sys.stdout.write(pod["env"].get(wanted.group(1), "") if wanted else "")
    sys.exit(0 if present else 1)

if "exec" in args:
    target = next(a for a in args[args.index("exec") + 1:] if not a.startswith("-"))
    if target == "deploy/dfe-engine":
        if "python3" in args and "-c" in args:
            # The sign-in helper: the account that signed in, then its token. Nothing on a refusal.
            if "engine_token" in pod:
                print(pod.get("engine_account", "admin"))
                print(pod["engine_token"])
            sys.exit(0)
        # Any other engine command runs in the engine's own shell, under its environment.
        engine_env = {"PATH": os.environ["FAKE_POD_BIN"], **pod.get("engine_env", {})}
        command = args[args.index("--") + 1:]
        sys.exit(subprocess.run(command, env=engine_env, check=False).returncode)
    if "-c" in args and args[args.index("-c") + 1] != "dfe-hyperdx":
        sys.stderr.write("error: container is not valid for pod\\n")
        sys.exit(1)
    # The Deployment's env and an envFrom ConfigMap both land in the container's environment.
    child_env = {"PATH": os.environ["FAKE_POD_BIN"], **pod["env"], **pod["configmap_env"], **pod["pod_env"]}
    try:
        sys.exit(subprocess.run(args[args.index("--") + 1:], env=child_env, check=False).returncode)
    except FileNotFoundError:
        sys.stderr.write("exec: executable file not found in $PATH\\n")
        sys.exit(126)

sys.exit(1)
"""


class Frontend(BaseHTTPRequestHandler):
    """JWKS, ClickHouse HTTP and the HyperDX frontend, answering from server.cfg."""

    def log_message(self, *_args: object) -> None:
        return

    def reply(self, status: int, body: str = "", headers: tuple = ()) -> None:
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body.encode())))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body.encode())

    def do_HEAD(self) -> None:
        self.route()

    def do_GET(self) -> None:
        self.route()

    def route(self) -> None:
        cfg, seen = self.server.cfg, self.server.seen
        parts = urlsplit(self.path)
        if parts.path == "/.well-known/jwks.json":
            keys = {"keys": [{"kty": "EC", "crv": "P-384", "alg": cfg["jwks_alg"]}]}
            self.reply(cfg["jwks_status"], json.dumps(keys))
        elif parts.path == "/ping":
            self.reply(200, "Ok.\n")
        elif parts.path == "/" and parts.query:
            seen["queries"].append(parse_qs(parts.query)["query"][0])
            sent = (self.headers.get("X-ClickHouse-User"), self.headers.get("X-ClickHouse-Key"))
            if sent == (CH_USER, CH_FIXTURE_VALUE):
                seen["ch_credentials_ok"] = True
                self.reply(200, "42\n")
            else:
                self.reply(401, "Code: 516. Authentication failed\n")
        elif parts.path == "/":
            headers = [("Content-Type", "text/html")]
            if cfg["csp"]:
                headers.append(("Content-Security-Policy", "frame-ancestors 'self'"))
            if cfg["x_frame_options"]:
                headers.append(("X-Frame-Options", "DENY"))
            self.reply(cfg["head_status"], headers=tuple(headers))
        elif parts.path == "/api/me":
            seen["bearer"] = self.headers.get("Authorization")
            seen["bearers"].append(seen["bearer"])
            ok = cfg["accept_any_bearer"] or (seen["bearer"] == f"Bearer {TOKEN}" and not cfg["reject_bearer"])
            self.reply(200 if ok else 401, '{"email":"smoke"}' if ok else "")
        else:
            self.reply(404)


def run_smoke(
    serve: dict | None = None,
    pod_env: dict | None = None,
    node: bool = True,
    engine_token: str | None = None,
    engine_account: str = "admin",
    in_deployment_env: bool = False,
    engine_ch: dict | None = None,
    reader_password: str | None = CH_FIXTURE_VALUE,
) -> tuple[subprocess.CompletedProcess, dict]:
    """The smoke script against the local server, and what the server saw.

    The settings live in an envFrom ConfigMap, the thin chart's shape, unless
    `in_deployment_env` puts them in the Deployment's own env. `engine_ch` deploys
    an engine whose ClickHouse settings name the local server, with these entries
    laid over them. An entry of None leaves that variable unset. `reader_password`
    is the query reader's Secret, None for a deployment that has none.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), Frontend)
    server.cfg = {
        "jwks_alg": "ES384",
        "jwks_status": 200,
        "csp": True,
        "x_frame_options": False,
        "head_status": 200,
        "accept_any_bearer": False,
        "reject_bearer": False,
        **(serve or {}),
    }
    server.seen = {"queries": [], "ch_credentials_ok": False, "bearer": None, "bearers": []}
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, podbin = Path(tmp) / "bin", Path(tmp) / "podbin"
            bindir.mkdir()
            podbin.mkdir()
            kubectl = bindir / "kubectl"
            kubectl.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
            kubectl.chmod(0o755)
            for tool in ("env", "sh", *(["node"] if node else [])):
                (podbin / tool).symlink_to(shutil.which(tool))

            settings = {
                "DFE_AUTH_MODE": "oidc-proxy",
                "DFE_ENGINE_JWKS_URL": f"{base}/.well-known/jwks.json",
                # The chart's form: the ClickHouse HTTP URL, which the script dials as is.
                "CLICKHOUSE_HOST": base,
            }
            pod = {
                "env": settings if in_deployment_env else {},
                "configmap_env": {} if in_deployment_env else settings,
                "pod_env": dict(pod_env or {}),
                "secrets": {} if reader_password is None else {CH_SECRET: reader_password},
            }
            if engine_token:
                pod["engine_token"] = engine_token
                pod["engine_account"] = engine_account
            if engine_ch is not None:
                engine_env = {
                    "DFE_CLICKHOUSE_HOST": "127.0.0.1",
                    "DFE_CLICKHOUSE_PORT": base.rsplit(":", 1)[1],
                    **engine_ch,
                }
                pod["engine_env"] = {k: v for k, v in engine_env.items() if v is not None}
            fixture = Path(tmp) / "pod.json"
            fixture.write_text(json.dumps(pod), encoding="utf-8", newline="\n")

            env = {k: v for k, v in os.environ.items() if not k.startswith("DFE_")}
            env.update(
                PATH=f"{bindir}{os.pathsep}{env['PATH']}",
                FAKE_POD=str(fixture),
                FAKE_POD_BIN=str(podbin),
                DFE_NS="dfe",
                DFE_HYPERDX_PORT=base.rsplit(":", 1)[1],
            )
            out = subprocess.run(
                ["bash", str(SMOKE)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=120,
                check=False,
            )
            return out, server.seen
    finally:
        server.shutdown()
        server.server_close()


def verdict(out: subprocess.CompletedProcess, tag: str, name: str) -> bool:
    """Whether a `[tag] name...` line was printed (name is a prefix of the check)."""
    return any(line.strip().startswith(f"[{tag}] {name}") for line in out.stdout.splitlines())


def test_a_node_only_pod_passes_all_three_seams() -> None:
    expect("node is on PATH for the probes", shutil.which("node") is not None)
    out, seen = run_smoke()

    for name in (JWKS, ES384, PING, TABLE, CSP, XFO):
        expect(f"{name} passes", verdict(out, "PASS", name), out.stdout)
    expect("no check failed", "0 failed" in out.stdout, out.stdout)
    expect(
        "the count query reached ClickHouse",
        seen["queries"] == ["SELECT count() FROM dfe.otel_logs"],
    )
    expect("the query reader's credentials were sent", seen["ch_credentials_ok"])
    expect("the password is never echoed", CH_FIXTURE_VALUE not in out.stdout + out.stderr)


def test_hyperdx_holding_a_credential_of_its_own_is_not_used() -> None:
    """The pod's own CLICKHOUSE_* env is not what the check reads as."""
    out, seen = run_smoke(
        pod_env={"CLICKHOUSE_USER": "default", "CLICKHOUSE_PASSWORD": CH_FIXTURE_VALUE},
        reader_password=None,
    )

    expect("the table check fails", verdict(out, "FAIL", TABLE), out.stdout)
    expect("no credentials were accepted", not seen["ch_credentials_ok"])


def test_unreadable_headers_skip_the_embed_checks() -> None:
    out, _ = run_smoke(serve={"head_status": 500})

    expect("the embed checks are skipped", verdict(out, "SKIP", EMBED_SKIPPED), out.stdout)
    expect("X-Frame-Options does not pass", not verdict(out, "PASS", XFO), out.stdout)
    expect("CSP does not pass", not verdict(out, "PASS", CSP), out.stdout)


def test_a_pod_that_answers_nothing_passes_no_embed_check() -> None:
    out, _ = run_smoke(node=False)

    expect("the embed checks are skipped", verdict(out, "SKIP", EMBED_SKIPPED), out.stdout)
    expect("X-Frame-Options does not pass", not verdict(out, "PASS", XFO), out.stdout)
    # An unreadable env must not read as the DFE middleware being off.
    expect("the env read fails", verdict(out, "FAIL", ENV_UNREAD), out.stdout)
    expect("auth is not skipped as unset", not verdict(out, "SKIP", "DFE_AUTH_MODE unset"), out.stdout)
    for name in (PING, TABLE):
        expect(f"{name} fails", verdict(out, "FAIL", name), out.stdout)


def test_settings_in_the_deployment_env_read_the_same() -> None:
    # The thin chart sets them in a ConfigMap, and the script reads the container's env either way.
    out, _ = run_smoke(in_deployment_env=True)

    for name in (JWKS, ES384, PING, TABLE, CSP, XFO):
        expect(f"{name} passes", verdict(out, "PASS", name), out.stdout)
    expect("no check failed", "0 failed" in out.stdout, out.stdout)


def test_x_frame_options_on_the_response_fails_the_check() -> None:
    out, _ = run_smoke(serve={"x_frame_options": True})

    expect("the header is caught", verdict(out, "FAIL", XFO), out.stdout)
    expect("CSP still passes", verdict(out, "PASS", CSP), out.stdout)


def test_a_response_without_csp_fails_the_check() -> None:
    out, _ = run_smoke(serve={"csp": False})

    expect("the missing CSP is caught", verdict(out, "FAIL", CSP), out.stdout)
    expect("X-Frame-Options still passes", verdict(out, "PASS", XFO), out.stdout)


def test_a_jwks_without_an_es384_key_fails_that_check() -> None:
    out, _ = run_smoke(serve={"jwks_alg": "ES256"})

    expect("the JWKS is fetched", verdict(out, "PASS", JWKS), out.stdout)
    expect("the missing ES384 key is caught", verdict(out, "FAIL", ES384), out.stdout)


def test_an_unreachable_jwks_fails_both_checks() -> None:
    out, _ = run_smoke(serve={"jwks_status": 503})

    expect("the fetch fails", verdict(out, "FAIL", JWKS), out.stdout)
    expect("the ES384 check fails", verdict(out, "FAIL", ES384), out.stdout)


def test_rejected_clickhouse_credentials_fail_the_table_check() -> None:
    out, seen = run_smoke(reader_password=WRONG_VALUE)

    expect("ping needs no credentials", verdict(out, "PASS", PING), out.stdout)
    expect("the table check fails", verdict(out, "FAIL", TABLE), out.stdout)
    expect("the query was attempted", len(seen["queries"]) == 1, out.stdout)


def test_the_engine_issued_token_is_presented_as_a_bearer() -> None:
    out, seen = run_smoke(engine_token=TOKEN, engine_ch={"DFE_CLICKHOUSE_SECURE": "false"})

    expect("the round trip passes", verdict(out, "PASS", BEARER), out.stdout)
    expect("the server saw the bearer", f"Bearer {TOKEN}" in seen["bearers"])
    expect("a token the engine never signed is refused", verdict(out, "PASS", REFUSES), out.stdout)
    expect("no check failed", "0 failed" in out.stdout, out.stdout)
    expect("the token is never echoed", TOKEN not in out.stdout + out.stderr)


def test_the_account_that_signed_in_is_named() -> None:
    out, _ = run_smoke(engine_token=TOKEN, engine_account="breakglass", engine_ch={"DFE_CLICKHOUSE_SECURE": "false"})

    expect("break-glass is named", "signed in to dfe-engine as breakglass" in out.stdout, out.stdout)


def test_a_hyperdx_that_rejects_the_engines_token_fails_the_round_trip() -> None:
    out, _ = run_smoke(
        serve={"reject_bearer": True}, engine_token=TOKEN, engine_ch={"DFE_CLICKHOUSE_SECURE": "false"}
    )

    expect("the round trip fails", verdict(out, "FAIL", BEARER), out.stdout)


def test_a_hyperdx_that_accepts_any_bearer_fails_the_refusal_check() -> None:
    out, _ = run_smoke(
        serve={"accept_any_bearer": True}, engine_token=TOKEN, engine_ch={"DFE_CLICKHOUSE_SECURE": "false"}
    )

    expect("the acceptance passes", verdict(out, "PASS", BEARER), out.stdout)
    expect("the refusal check fails", verdict(out, "FAIL", REFUSES), out.stdout)


def test_an_engine_that_gives_no_token_fails_rather_than_skips() -> None:
    out, seen = run_smoke(engine_ch={"DFE_CLICKHOUSE_SECURE": "false"}, engine_token=None)

    expect("the round trip is a failure", "[FAIL] engine-issued-token round trip" in out.stdout, out.stdout)
    expect("it is not a skip", "[SKIP] engine-issued-token" not in out.stdout, out.stdout)
    expect("no bearer was sent", seen["bearers"] == [], str(seen["bearers"]))


# Nothing listens on the discard port, so a probe that dials it fails.
DEAD_CLICKHOUSE_HOST = "http://127.0.0.1:9"


def test_the_probe_dials_the_url_the_engine_hands_hyperdx() -> None:
    """The engine serves HyperDX's proxy its ClickHouse URL. CLICKHOUSE_HOST only keys
    HyperDX's webhook refusal, and the chart writes it http:// whatever the server speaks."""
    out, seen = run_smoke(
        engine_ch={"DFE_CLICKHOUSE_SECURE": "false"},
        pod_env={"CLICKHOUSE_HOST": DEAD_CLICKHOUSE_HOST},
    )

    for name in (PING, TABLE):
        expect(f"{name} passes", verdict(out, "PASS", name), out.stdout)
    line = next((ln for ln in out.stdout.splitlines() if PING in ln), "")
    expect(
        "the probe names the engine's URL, not CLICKHOUSE_HOST",
        "(http://127.0.0.1:" in line and DEAD_CLICKHOUSE_HOST not in line,
        line,
    )
    expect(
        "the count query reached ClickHouse",
        seen["queries"] == ["SELECT count() FROM dfe.otel_logs"],
    )
    expect("with the query reader's credentials", seen["ch_credentials_ok"])


def test_an_engine_on_tls_is_dialled_over_https_on_its_port() -> None:
    """The engine's own default is secure, so an unset flag is https too."""
    for secure in ("true", None):
        out, _ = run_smoke(
            engine_ch={"DFE_CLICKHOUSE_SECURE": secure},
            pod_env={"CLICKHOUSE_HOST": DEAD_CLICKHOUSE_HOST},
        )
        line = next((ln for ln in out.stdout.splitlines() if PING in ln), "")
        expect(
            f"secure={secure!r} dials https on the engine's port",
            "(https://127.0.0.1:" in line,
            line,
        )


def test_an_engine_that_names_no_clickhouse_fails_the_probe_saying_so() -> None:
    """An empty URL must never fall back to a guessed host and pass."""
    out, _ = run_smoke(
        engine_ch={"DFE_CLICKHOUSE_HOST": None},
        pod_env={"CLICKHOUSE_HOST": DEAD_CLICKHOUSE_HOST},
    )

    expect("the probe fails", verdict(out, "FAIL", PING), out.stdout)
    expect(
        "naming the engine", "deploy/dfe-engine named no ClickHouse URL" in out.stdout, out.stdout
    )
    expect("and the table check fails", verdict(out, "FAIL", TABLE), out.stdout)


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
