#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_acceptance_external_datastore.py
#  Purpose:      Prove `dfe-ops acceptance` reaches an external ClickHouse directly
#                from the cluster secret's facts, keeps its port-forward for an
#                in-cluster server, and that the datastore client speaks the scheme
#                it is told to.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The acceptance suite's datastore, in-cluster and external.

    python3 -m pytest scripts/tests/test_acceptance_external_datastore.py -q

An external ClickHouse has no Service to forward to: bootstrap.sh writes its host,
port, TLS and user onto the dfe-cluster secret, and clickhouse-cluster copies its
password into the app namespace as clickhouse-admin-password. Every host is under
the reserved example domain. Each run starts from an environment of two keys and
assertions compare only the DFE_E2E_CH_* keys, so a failure prints none of the
environment pytest itself runs in.
"""

import http.server
import importlib.machinery
import importlib.util
import json
import ssl
import sys
import threading
import urllib.error
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_external_datastore", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_external_datastore", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_external_datastore"] = dfeops
_loader.exec_module(dfeops)


@pytest.fixture(autouse=True)
def _engine_checkout_is_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about what the stage does after the engine-checkout gate, which has its own tests."""
    monkeypatch.setattr(dfeops, "_engine_checkout_gate", lambda *_args: 0)

from acceptance import clients  # noqa: E402

APP_NAMESPACE = "dfe-aws-test"
HOST = "ch.example.com"

EXTERNAL = {
    "dfe.hyperi.io/clickhouse_mode": "external",
    "dfe.hyperi.io/clickhouse_host": HOST,
    "dfe.hyperi.io/clickhouse_port": "8443",
    "dfe.hyperi.io/clickhouse_native_port": "9440",
    "dfe.hyperi.io/clickhouse_secure": "true",
    "dfe.hyperi.io/clickhouse_user": "default",
}
PLAINTEXT = {
    **EXTERNAL,
    "dfe.hyperi.io/clickhouse_port": "8123",
    "dfe.hyperi.io/clickhouse_secure": "false",
    "dfe.hyperi.io/clickhouse_user": "dfe_writer",
}
IN_CLUSTER = {
    "dfe.hyperi.io/clickhouse_mode": "in_cluster",
    "dfe.hyperi.io/clickhouse_host": "dfe-clickhouse.clickhouse.svc.cluster.local",
    "dfe.hyperi.io/clickhouse_port": "",
    "dfe.hyperi.io/clickhouse_secure": "",
    "dfe.hyperi.io/clickhouse_user": "",
}


# --- the datastore client ------------------------------------------------------------


class _ClickHouse(http.server.BaseHTTPRequestHandler):
    """ClickHouse's HTTP interface, as far as Datastore uses it: one JSONCompact row."""

    def do_POST(self) -> None:
        sql = self.rfile.read(int(self.headers["Content-Length"])).decode()
        self.server.seen.append((self.path, self.headers["X-ClickHouse-User"], sql))  # type: ignore[attr-defined]
        body = json.dumps({"data": [[7]]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def plain_server() -> Iterator[http.server.ThreadingHTTPServer]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ClickHouse)
    server.seen = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_a_datastore_speaks_plain_http_unless_told_otherwise(plain_server) -> None:
    store = clients.Datastore("127.0.0.1", plain_server.server_port, "default")
    assert store.secure is False
    assert store.scalar("SELECT 7") == 7
    path, user, sql = plain_server.seen[0]
    assert (user, sql) == ("default", "SELECT 7")
    assert "database=dfe" in path


def test_a_secure_datastore_opens_tls_before_it_sends_anything(plain_server) -> None:
    """A plain listener answers a TLS hello with HTTP, which no handshake accepts."""
    store = clients.Datastore("127.0.0.1", plain_server.server_port, "default", secure=True)
    with pytest.raises(urllib.error.URLError) as refused:
        store.query("SELECT 7")
    assert isinstance(refused.value.reason, ssl.SSLError)
    assert plain_server.seen == []


def test_the_suite_env_names_the_datastore_and_its_scheme() -> None:
    store = clients.Datastore.from_env({
        "DFE_E2E_CH_HOST": HOST, "DFE_E2E_CH_PORT": "8443", "DFE_E2E_CH_USER": "dfe_writer",
        "DFE_E2E_CH_PASSWORD": "from-env", "DFE_E2E_CH_DB": "dfe2", "DFE_E2E_CH_SECURE": "true",
    })
    assert (store.host, store.port, store.user, store.database, store.secure) == (
        HOST, 8443, "dfe_writer", "dfe2", True,
    )
    assert store.password == "from-env"
    assert "from-env" not in repr(store)


def test_a_suite_env_without_a_scheme_keeps_plain_http() -> None:
    """dfe-ops forwards an in-cluster server onto localhost, which serves plain HTTP."""
    store = clients.Datastore.from_env({"DFE_E2E_CH_HOST": "localhost", "DFE_E2E_CH_PORT": "18123"})
    assert (store.host, store.port, store.user, store.database, store.secure) == (
        "localhost", 18123, "default", "dfe", False,
    )


# --- dfe-ops acceptance -------------------------------------------------------------


def _acceptance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    annotations: dict[str, str] | None,
    *argv: str,
    answering: bool = True,
    expect: int = 0,
) -> dict:
    """Run cmd_acceptance --suite source against a dfe-cluster secret carrying *annotations*.

    None makes the secret unreadable. Returns what the run did: the forwards it
    started, the datastore URLs it probed directly, the secrets it read, the
    DFE_E2E_CH_* keys it handed the runner (empty when the runner never started),
    and the cluster-secret reads it made.
    """
    did: dict = {"forwards": [], "probed": [], "secrets": [], "ch": {}, "cluster_reads": []}

    class Done:
        returncode = 0

    def fake_run(cmd, *, env=None, **_kwargs):
        if "run.py" in str(cmd[1]):
            did["ch"] = {k: v for k, v in (env or {}).items() if k.startswith("DFE_E2E_CH_")}
        return Done()

    def kubectl_json(base: list[str], *argv: str, **_kwargs: object) -> tuple[int, dict, str]:
        did["cluster_reads"].append([*base, *argv])
        if annotations is None:
            return 1, {}, "Forbidden"
        return 0, {"metadata": {"annotations": annotations}, "data": {"config": "c2VjcmV0"}}, ""

    def secret_value(_kube: list[str], namespace: str, name: str, key: str) -> str:
        did["secrets"].append((namespace, name, key))
        return "pw-from-the-app-namespace"

    def datastore_ready(url: str, _timeout: float) -> bool:
        did["probed"].append(url)
        return answering

    monkeypatch.setattr(dfeops, "_forward", lambda *args, **_k: did["forwards"].append(args))
    monkeypatch.setattr(dfeops, "_forward_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(dfeops, "_datastore_ready", datastore_ready)
    monkeypatch.setattr(dfeops, "_kubectl_json", kubectl_json)
    monkeypatch.setattr(dfeops, "_taken_ports", lambda _ports: [])
    monkeypatch.setattr(dfeops, "_secret_value", secret_value)
    monkeypatch.setattr(dfeops, "_pods_of", lambda *_a, **_k: [])
    monkeypatch.setattr(dfeops, "_resolves", lambda _host: True)
    monkeypatch.setattr(dfeops, "admin_credential", lambda *_a, **_k: ("admin", "minted"))
    monkeypatch.setattr(dfeops.subprocess, "run", fake_run)
    # The run copies the whole environment into the runner's, so it starts from one holding nothing.
    monkeypatch.setattr(dfeops.os, "environ", {"DFE_NAMESPACE": APP_NAMESPACE, "PATH": "/usr/bin:/bin"})
    args = dfeops.build_parser().parse_args([
        "acceptance", "--repo", "/nonexistent/dfe-engine", "--suite", "source",
        "--ui-url", "https://dfe.example", "--shots-dir", str(tmp_path / "shots"), *argv,
    ])
    assert dfeops.cmd_acceptance(args) == expect
    return did


def test_an_external_datastore_is_dialled_directly_with_no_forward(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    did = _acceptance(monkeypatch, tmp_path, EXTERNAL)
    assert [args[0] for args in did["forwards"]] == ["receiver", "engine"]
    assert did["probed"] == [f"https://{HOST}:8443/ping"]
    assert did["ch"] == {
        "DFE_E2E_CH_HOST": HOST,
        "DFE_E2E_CH_PORT": "8443",
        "DFE_E2E_CH_SECURE": "true",
        "DFE_E2E_CH_USER": "default",
        "DFE_E2E_CH_PASSWORD": "pw-from-the-app-namespace",
        "DFE_E2E_CH_DB": "dfe",
    }
    # The copy clickhouse-cluster projects for the apps, never the server's own namespace.
    assert did["secrets"] == [(APP_NAMESPACE, "clickhouse-admin-password", "password")]
    assert did["cluster_reads"] == [["kubectl", "-n", "argocd", "get", "secret", "dfe-cluster"]]


def test_a_plaintext_external_datastore_is_dialled_in_plaintext(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    did = _acceptance(monkeypatch, tmp_path, PLAINTEXT)
    assert did["probed"] == [f"http://{HOST}:8123/ping"]
    assert (did["ch"]["DFE_E2E_CH_SECURE"], did["ch"]["DFE_E2E_CH_PORT"]) == ("false", "8123")
    assert did["ch"]["DFE_E2E_CH_USER"] == "dfe_writer"


def test_a_named_ch_user_wins_over_the_cluster_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    did = _acceptance(monkeypatch, tmp_path, EXTERNAL, "--ch-user", "dfe_reader")
    assert did["ch"]["DFE_E2E_CH_USER"] == "dfe_reader"


@pytest.mark.parametrize("annotations", [IN_CLUSTER, {}, None], ids=["in_cluster", "no-facts", "unreadable"])
def test_an_in_cluster_datastore_keeps_its_forward(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, annotations: dict[str, str] | None
) -> None:
    did = _acceptance(monkeypatch, tmp_path, annotations)
    forwards = {args[0]: args[2:6] for args in did["forwards"]}
    assert forwards["datastore"] == ("clickhouse", "svc/dfe-clickhouse", 18123, 8123)
    assert did["probed"] == []
    assert did["ch"] == {
        "DFE_E2E_CH_HOST": "localhost",
        "DFE_E2E_CH_PORT": "18123",
        "DFE_E2E_CH_SECURE": "false",
        "DFE_E2E_CH_USER": "default",
        "DFE_E2E_CH_PASSWORD": "pw-from-the-app-namespace",
        "DFE_E2E_CH_DB": "dfe",
    }


def test_expected_fail_an_external_datastore_that_never_answers_stops_the_run_without_naming_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A cloud cycle's log can be public, so the address stays out of it."""
    did = _acceptance(monkeypatch, tmp_path, EXTERNAL, answering=False, expect=1)
    assert did["ch"] == {}
    err = capsys.readouterr().err
    assert "datastore" in err
    assert HOST not in err


@pytest.mark.parametrize("missing", ["dfe.hyperi.io/clickhouse_host", "dfe.hyperi.io/clickhouse_port"])
def test_expected_fail_an_external_server_with_no_address_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture, missing: str
) -> None:
    did = _acceptance(monkeypatch, tmp_path, {**EXTERNAL, missing: ""}, expect=1)
    assert (did["forwards"], did["ch"]) == ([], {})
    assert missing.split("/")[1] in capsys.readouterr().err


def test_the_readiness_probe_names_no_address(capsys: pytest.CaptureFixture) -> None:
    """Nothing listens on the discard port, so the probe gives up and says so without the URL."""
    assert dfeops._datastore_ready("http://127.0.0.1:9/ping", 0.1) is False
    err = capsys.readouterr().err
    assert "127.0.0.1" not in err
    assert "datastore" in err
