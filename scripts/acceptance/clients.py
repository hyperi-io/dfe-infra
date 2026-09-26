#  Project:      dfe-infra
#  File:         acceptance/clients.py
#  Purpose:      The engine API, the datastore and the tidy-up calls every
#                acceptance suite makes, in one place: a bearer login, JSON
#                calls, one-row SQL over HTTP, and removing what a run created.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Thin clients, standard library only, so a suite needs nothing installed
beyond Playwright.

Both suites talk to the same engine and delete the same way, so the login, the
delete-and-confirm and the setup contract live here rather than once per suite.
"""

from __future__ import annotations

import json
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


def tls_context(verify: bool) -> ssl.SSLContext | None:
    """The TLS posture for a run's own API calls."""
    if verify:
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


@dataclass
class Reply:
    status: int
    body: object

    def __getitem__(self, key: str):
        return self.body[key]  # type: ignore[index]


@dataclass
class Engine:
    """The engine's API with a token kept fresh across a long run.

    A fresh deployment's admin must replace its issued password before the engine
    serves it anything else, and a run that has made that change holds the admin
    on ``new_password`` after it; login handles both.
    """

    base: str
    user: str
    password: str = field(repr=False)
    verify: bool = True
    token: str = field(default="", repr=False)
    new_password: str = field(default="", repr=False)

    def login(self) -> None:
        reply = self._request("POST", "/auth/login", {"username": self.user, "password": self.password}, auth=False)
        if reply.status == 401 and self.new_password and self.new_password != self.password:
            # An earlier run already replaced the issued password.
            self.password = self.new_password
            reply = self._request("POST", "/auth/login", {"username": self.user, "password": self.password}, auth=False)
        if reply.status != 200:
            raise RuntimeError(f"engine login failed: {reply.status} {reply.body}")
        self.token = str(reply["access_token"])
        if isinstance(reply.body, dict) and reply.body.get("password_change_required"):
            self._complete_forced_change()

    def _complete_forced_change(self) -> None:
        """Replace the issued password, which the engine demands before anything else."""
        if not self.new_password:
            raise RuntimeError(
                f"'{self.user}' must change its issued password before the engine serves it; "
                "set DFE_E2E_ADMIN_NEW_PASSWORD to the password to change it to"
            )
        changed = self._request("POST", "/auth/accounts/reset-password", {"new_password": self.new_password})
        if changed.status != 200:
            raise RuntimeError(f"forced password change failed: {changed.status} {changed.body}")
        self.password = self.new_password

    def call(self, method: str, path: str, body: object = None) -> Reply:
        if not self.token:
            self.login()
        reply = self._request(method, path, body)
        if reply.status == 401:
            self.login()
            reply = self._request(method, path, body)
        return reply

    def _request(self, method: str, path: str, body: object = None, *, auth: bool = True) -> Reply:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.base.rstrip('/')}/api/v1{path}", data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=180, context=tls_context(self.verify)) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        try:
            decoded = json.loads(raw) if raw else None
        except ValueError:
            decoded = raw.decode(errors="replace")
        return Reply(status, decoded)


@dataclass
class Datastore:
    """ClickHouse over its HTTP interface."""

    host: str
    port: int
    user: str
    password: str = field(default="", repr=False)
    database: str = "dfe"

    def query(self, sql: str) -> list[list]:
        params = urllib.parse.urlencode({"database": self.database, "default_format": "JSONCompact"})
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/?{params}",
            data=sql.encode(),
            method="POST",
            headers={"X-ClickHouse-User": self.user, "X-ClickHouse-Key": self.password},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read())["data"]
        except urllib.error.HTTPError as exc:
            text = exc.read().decode(errors="replace")
            if "UNKNOWN_TABLE" in text or "doesn't exist" in text or "does not exist" in text:
                return []
            raise RuntimeError(f"datastore refused the query: {exc.code} {text[:300]}") from exc

    def scalar(self, sql: str) -> int:
        rows = self.query(sql)
        return int(rows[0][0]) if rows else 0

    def table_exists(self, name: str) -> bool:
        return self.scalar(
            f"SELECT count() FROM system.tables WHERE database = '{self.database}' AND name = '{name}'"
        ) > 0


def source_names(engine_url: str, verify: bool, token: str) -> tuple[str, ...]:
    """Every source the deployment currently carries."""
    request = urllib.request.Request(
        f"{engine_url.rstrip('/')}/api/v1/sources",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=60, context=tls_context(verify)) as response:
        return tuple(str(item["name"]) for item in json.loads(response.read())["items"])


def remove_source(engine_url: str, verify: bool, token: str, name: str, deadline: float) -> str:
    """Delete a source the run created, and confirm it went.

    Through the API rather than the console: this is the run tidying up after
    itself, not part of what it claims to prove.

    The confirmation is a read, not the DELETE's status. A delete commits to the
    deploy repo and reconciles the apps, which outlasts a gateway's own timeout,
    so a 504 on a delete that landed would otherwise read as a source left behind.

    Args:
        engine_url: Engine API base.
        verify: Whether to verify TLS.
        token: A bearer token for the engine.
        name: The source to remove.
        deadline: Seconds to keep checking that it went.

    Returns:
        One line saying whether it went.
    """
    request = urllib.request.Request(
        f"{engine_url.rstrip('/')}/api/v1/sources/{name}",
        method="DELETE",
        headers={"Authorization": f"Bearer {token}"},
    )
    status = ""
    try:
        with urllib.request.urlopen(request, timeout=120, context=tls_context(verify)) as response:
            status = str(response.status)
    except urllib.error.HTTPError as exc:
        status = str(exc.code)
    except (urllib.error.URLError, OSError) as exc:
        status = type(exc).__name__

    until = time.monotonic() + deadline
    while True:
        try:
            if name not in source_names(engine_url, verify, token):
                return f"removed {name} (delete answered {status})"
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            pass
        if time.monotonic() >= until:
            return f"could NOT remove {name}: still there {deadline:.0f}s after a {status}"
        time.sleep(5)


def engine_token(engine_url: str, verify: bool, user: str, password: str) -> str:
    """A bearer token for a run's own tidy-up, or empty when login fails."""
    request = urllib.request.Request(
        f"{engine_url.rstrip('/')}/api/v1/auth/login",
        data=json.dumps({"username": user, "password": password}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60, context=tls_context(verify)) as response:
            return str(json.loads(response.read())["access_token"])
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return ""


def setup_status(engine_url: str, verify: bool) -> dict:
    """The deployment's setup contract.

    Args:
        engine_url: Engine API base.
        verify: Whether to verify TLS.

    Returns:
        The setup-status document.

    Raises:
        OnboardingError: The engine would not answer.
    """
    from acceptance.onboarding import wizard

    url = f"{engine_url.rstrip('/')}/api/v1/auth/setup-status"
    try:
        with urllib.request.urlopen(url, timeout=30, context=tls_context(verify)) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise wizard.OnboardingError(f"the engine did not serve {url}: {exc}") from exc


def companion(repo: Path, name: str) -> Path:
    """The checkout beside *repo*'s main clone, so a worktree resolves the same as a clone."""
    common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    home = (repo / common).resolve().parent if common else repo
    return home.parent / name
