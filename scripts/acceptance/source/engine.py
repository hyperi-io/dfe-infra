#  Project:      dfe-infra
#  File:         acceptance/source/engine.py
#  Purpose:      The engine API and the datastore, as the source test uses them:
#                a bearer login, JSON calls, and one-row SQL over HTTP.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Thin clients, standard library only, so the runner needs nothing installed
beyond Playwright."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


def _context(verify: bool) -> ssl.SSLContext | None:
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
    """The engine's API with a token kept fresh across a long run."""

    base: str
    user: str
    password: str
    verify: bool = True
    token: str = field(default="", repr=False)

    def login(self) -> None:
        reply = self._request("POST", "/auth/login", {"username": self.user, "password": self.password}, auth=False)
        if reply.status != 200:
            raise RuntimeError(f"engine login failed: {reply.status} {reply.body}")
        self.token = str(reply["access_token"])

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
            with urllib.request.urlopen(request, timeout=180, context=_context(self.verify)) as response:
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
