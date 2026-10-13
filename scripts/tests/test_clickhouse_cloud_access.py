#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_clickhouse_cloud_access.py
#  Purpose:      Prove a cloud run opens, closes and sweeps only its own entries
#                on a ClickHouse Cloud service's IP access list.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/clickhouse_cloud_access.py.

    python3 -m pytest scripts/tests/test_clickhouse_cloud_access.py -q

The ClickHouse Cloud API is a local HTTP server speaking the three calls the
script makes, so no request leaves this machine. Every id, key and address
below is a placeholder: RFC 5737 documentation addresses only.
"""

import base64
import http.server
import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "clickhouse_cloud_access.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import clickhouse_cloud_access as access  # noqa: E402

ORG = "00000000-0000-0000-0000-00000000000a"
SERVICE = "00000000-0000-0000-0000-00000000000b"
KEY_ID = "placeholder-key-id"
KEY_SECRET = "placeholder-key-secret"
NOW = 1791460800
OPERATOR = {"source": "203.0.113.0/24", "description": "office"}


class FakeCloud:
    """The service's access list, and every request the script sent."""

    def __init__(self) -> None:
        self.entries: list[dict[str, str]] = [dict(OPERATOR)]
        self.requests: list[tuple[str, str, dict | None, str]] = []
        self.organisations = [{"id": ORG, "name": "example"}]
        self.fail_status = 0
        self.redirect_to = ""


def _handler(cloud: FakeCloud) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return None

        def _reply(self, status: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            cloud.requests.append((method, self.path, body, self.headers.get("Authorization", "")))
            if cloud.redirect_to:
                self.send_response(302)
                self.send_header("Location", cloud.redirect_to)
                self.end_headers()
                return
            if cloud.fail_status:
                self._reply(cloud.fail_status, {"status": cloud.fail_status, "error": f"bad {SERVICE}"})
                return
            service = f"/v1/organizations/{ORG}/services/{SERVICE}"
            if method == "GET" and self.path == "/v1/organizations":
                self._reply(200, {"status": 200, "result": cloud.organisations})
            elif method == "GET" and self.path == service:
                self._reply(200, {"status": 200, "result": {"id": SERVICE, "ipAccessList": cloud.entries}})
            elif method == "PATCH" and self.path == service:
                patch = (body or {}).get("ipAccessList", {})
                gone = {e["source"] for e in patch.get("remove", [])}
                cloud.entries = [e for e in cloud.entries if e["source"] not in gone]
                cloud.entries += patch.get("add", [])
                self._reply(200, {"status": 200, "result": {"id": SERVICE, "ipAccessList": cloud.entries}})
            else:
                self._reply(404, {"status": 404, "error": "not found"})

        def do_GET(self) -> None:            self._serve("GET")

        def do_PATCH(self) -> None:            self._serve("PATCH")

    return Handler


@pytest.fixture
def cloud() -> Iterator[tuple[FakeCloud, str]]:
    state = FakeCloud()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _env(url: str, **extra: str) -> dict[str, str]:
    return {
        "DFE_CYCLE_CH_SERVICE_ID": SERVICE,
        "DFE_CYCLE_CH_API_KEY_ID": KEY_ID,
        "DFE_CYCLE_CH_API_KEY_SECRET": KEY_SECRET,
        "DFE_CYCLE_CH_API_URL": url,
        **extra,
    }


def _api(url: str) -> access.Api:
    return access.api_from_env(_env(url))


def _run_entries(cloud: FakeCloud) -> list[dict[str, str]]:
    return [e for e in cloud.entries if e.get("description", "").startswith("dfe-run:")]


def test_open_adds_each_address_described_with_the_run_and_its_expiry(cloud: tuple[FakeCloud, str]) -> None:
    state, url = cloud
    added = access.open_access(_api(url), "run-1", NOW + 3600, ["198.51.100.1/32", "198.51.100.2/32"])
    assert added == 2
    assert _run_entries(state) == [
        {"source": "198.51.100.1/32", "description": f"dfe-run:run-1:{NOW + 3600}"},
        {"source": "198.51.100.2/32", "description": f"dfe-run:run-1:{NOW + 3600}"},
    ]
    assert OPERATOR in state.entries
    patches = [r for r in state.requests if r[0] == "PATCH"]
    assert len(patches) == 1
    assert "remove" not in patches[0][2]["ipAccessList"] or patches[0][2]["ipAccessList"]["remove"] == []


def test_every_request_authenticates_with_the_key_pair(cloud: tuple[FakeCloud, str]) -> None:
    state, url = cloud
    access.open_access(_api(url), "run-1", NOW + 3600, ["198.51.100.1/32"])
    wanted = "Basic " + base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode()
    assert {r[3] for r in state.requests} == {wanted}


def test_the_organisation_is_the_one_the_key_belongs_to(cloud: tuple[FakeCloud, str]) -> None:
    state, url = cloud
    access.open_access(_api(url), "run-1", NOW + 3600, ["198.51.100.1/32"])
    assert state.requests[0][:2] == ("GET", "/v1/organizations")
    assert all(f"/organizations/{ORG}/services/{SERVICE}" in r[1] for r in state.requests[1:])


@pytest.mark.parametrize("organisations", [[], [{"id": ORG}, {"id": "00000000-0000-0000-0000-00000000000c"}]])
def test_expected_fail_a_key_reaching_no_single_organisation_is_refused(
    cloud: tuple[FakeCloud, str], organisations: list[dict]
) -> None:
    state, url = cloud
    state.organisations = organisations
    with pytest.raises(access.AccessError, match="organisation"):
        access.open_access(_api(url), "run-1", NOW + 3600, ["198.51.100.1/32"])
    assert [r for r in state.requests if r[0] == "PATCH"] == []


@pytest.mark.parametrize("cidr", ["0.0.0.0/0", "198.51.100.0/24", "198.51.100.1", "2001:db8::1/128", "nonsense"])
def test_expected_fail_open_refuses_anything_but_one_ipv4_address(cloud: tuple[FakeCloud, str], cidr: str) -> None:
    state, url = cloud
    with pytest.raises(access.AccessError, match="/32"):
        access.open_access(_api(url), "run-1", NOW + 3600, ["198.51.100.1/32", cidr])
    assert state.requests == []


def test_expected_fail_open_with_no_address_sends_nothing(cloud: tuple[FakeCloud, str]) -> None:
    state, url = cloud
    with pytest.raises(access.AccessError, match="no address"):
        access.open_access(_api(url), "run-1", NOW + 3600, [])
    assert state.requests == []


def test_an_address_already_allowed_is_not_added_twice(cloud: tuple[FakeCloud, str]) -> None:
    state, url = cloud
    state.entries.append({"source": "198.51.100.1/32", "description": "runner"})
    added = access.open_access(_api(url), "run-1", NOW + 3600, ["198.51.100.1/32", "198.51.100.2/32"])
    assert added == 1
    assert [e["source"] for e in state.entries].count("198.51.100.1/32") == 1
    assert {"source": "198.51.100.1/32", "description": "runner"} in state.entries


def test_close_removes_this_runs_entries_and_nothing_else(cloud: tuple[FakeCloud, str]) -> None:
    state, url = cloud
    access.open_access(_api(url), "run-1", NOW + 3600, ["198.51.100.1/32", "198.51.100.2/32"])
    access.open_access(_api(url), "run-2", NOW + 3600, ["198.51.100.3/32"])
    removed = access.close_access(_api(url), "run-1")
    assert removed == 2
    assert state.entries == [OPERATOR, {"source": "198.51.100.3/32", "description": f"dfe-run:run-2:{NOW + 3600}"}]


def test_close_of_a_run_with_no_entries_sends_no_patch(cloud: tuple[FakeCloud, str]) -> None:
    state, url = cloud
    assert access.close_access(_api(url), "run-1") == 0
    assert [r for r in state.requests if r[0] == "PATCH"] == []


def test_a_run_id_that_prefixes_another_closes_only_its_own(cloud: tuple[FakeCloud, str]) -> None:
    state, url = cloud
    access.open_access(_api(url), "run-1", NOW + 3600, ["198.51.100.1/32"])
    access.open_access(_api(url), "run-10", NOW + 3600, ["198.51.100.2/32"])
    access.close_access(_api(url), "run-1")
    assert [e["source"] for e in _run_entries(state)] == ["198.51.100.2/32"]


def test_sweep_removes_expired_run_entries_only(cloud: tuple[FakeCloud, str]) -> None:
    state, url = cloud
    state.entries += [
        {"source": "198.51.100.1/32", "description": f"dfe-run:run-old:{NOW - 1}"},
        {"source": "198.51.100.2/32", "description": f"dfe-run:run-now:{NOW}"},
        {"source": "198.51.100.3/32", "description": f"dfe-run:run-live:{NOW + 60}"},
        {"source": "198.51.100.4/32", "description": "dfe-run:hand-written"},
    ]
    removed = access.sweep(_api(url), NOW)
    assert removed == 2
    assert [e["source"] for e in state.entries] == ["203.0.113.0/24", "198.51.100.3/32", "198.51.100.4/32"]


def test_expected_fail_an_api_error_names_the_call_and_never_the_service(cloud: tuple[FakeCloud, str]) -> None:
    state, url = cloud
    state.fail_status = 403
    with pytest.raises(access.AccessError) as raised:
        access.close_access(_api(url), "run-1")
    assert "403" in str(raised.value)
    assert SERVICE not in str(raised.value)
    assert ORG not in str(raised.value)


def test_expected_fail_a_redirect_is_refused_and_the_key_goes_nowhere_else(cloud: tuple[FakeCloud, str]) -> None:
    state, url = cloud
    elsewhere = FakeCloud()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(elsewhere))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        state.redirect_to = f"http://127.0.0.1:{server.server_address[1]}/v1/organizations"
        with pytest.raises(access.AccessError, match="redirect"):
            access.sweep(_api(url), NOW)
    finally:
        server.shutdown()
        server.server_close()
    assert elsewhere.requests == []


def test_expected_fail_missing_settings_are_named_and_no_value_is_echoed() -> None:
    with pytest.raises(access.AccessError) as raised:
        access.api_from_env({"DFE_CYCLE_CH_API_KEY_SECRET": KEY_SECRET, "DFE_CYCLE_CH_SERVICE_ID": " "})
    message = str(raised.value)
    assert "DFE_CYCLE_CH_SERVICE_ID" in message
    assert "DFE_CYCLE_CH_API_KEY_ID" in message
    assert "DFE_CYCLE_CH_API_KEY_SECRET" not in message
    assert KEY_SECRET not in message


def test_the_api_defaults_to_clickhouse_cloud_over_https() -> None:
    api = access.api_from_env({k: v for k, v in _env("").items() if k != "DFE_CYCLE_CH_API_URL"})
    assert api.base_url == "https://api.clickhouse.cloud"


@pytest.mark.parametrize("url", ["http://api.example.com", "ftp://127.0.0.1", "file:///etc/passwd"])
def test_expected_fail_a_key_is_never_sent_in_clear_off_this_machine(url: str) -> None:
    with pytest.raises(access.AccessError, match="https"):
        access.api_from_env(_env(url))


def test_the_cli_prints_counts_and_never_an_address_id_or_key(cloud: tuple[FakeCloud, str]) -> None:
    _state, url = cloud
    env = {"PATH": "/usr/bin:/bin", **_env(url, DFE_CH_ACCESS_CIDRS="198.51.100.1/32, 198.51.100.2/32")}
    runs = [
        ["open", "--run-id", "run-1", "--expires-at", str(NOW + 3600)],
        ["close", "--run-id", "run-1"],
        ["sweep"],
    ]
    for argv in runs:
        done = subprocess.run(
            [sys.executable, str(SCRIPT), *argv], env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        assert done.returncode == 0, done.stderr
        printed = done.stdout + done.stderr
        for secret in (SERVICE, ORG, KEY_ID, KEY_SECRET, "198.51.100.1", "198.51.100.2"):
            assert secret not in printed, argv
    assert "opened 2" in subprocess.run(
        [sys.executable, str(SCRIPT), "open", "--run-id", "run-3", "--expires-at", str(NOW + 3600)],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    ).stderr


def test_expected_fail_the_cli_exits_2_on_a_missing_setting() -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "sweep"], env={"PATH": "/usr/bin:/bin"}, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert done.returncode == 2
    assert "DFE_CYCLE_CH_SERVICE_ID" in done.stderr


def test_expected_fail_the_cli_exits_1_on_an_api_failure(cloud: tuple[FakeCloud, str]) -> None:
    state, url = cloud
    state.fail_status = 500
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "close", "--run-id", "run-1"], env={"PATH": "/usr/bin:/bin", **_env(url)},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert done.returncode == 1
    assert SERVICE not in done.stderr
