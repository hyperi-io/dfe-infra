#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/deploy_repo_session.py
#  Purpose:      Give a git checkout of the bundled in-cluster Forgejo deploy
#                repo the admin credential and a route to it, without the
#                operator ever handling either.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Clone, pull and push the bundled Forgejo deploy repo with its own admin credential.

The bundled repo is private and its only credential is the Secret bootstrap made,
so a plain `git push` from an operator's clone cannot work. A session reads that
Secret in this process, port-forwards the Forgejo Service, points the checkout's
origin at the forward, and hands git the credential through GIT_CONFIG_* in its
environment: never argv, never the URL, never the checkout's .git/config.

    with BundledRepo(kubeconfig=None).open(Path("deploy")) as session:
        subprocess.run(["git", "-C", "deploy", "push"], env={**os.environ, **session.git_env})
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

FORGEJO_REMOTE_PORT = 3000


class SessionError(RuntimeError):
    """The deploy repo could not be reached or authenticated."""


def _kubectl(kubeconfig: str | None, *args: str) -> list[str]:
    return ["kubectl", *(["--kubeconfig", kubeconfig] if kubeconfig else []), *args]


def read_admin(kubeconfig: str | None, namespace: str, secret: str) -> tuple[str, str]:
    """The (username, password) of the Forgejo admin Secret, read in this process."""
    out = subprocess.run(
        _kubectl(kubeconfig, "-n", namespace, "get", "secret", secret, "-o", "json"),
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if out.returncode != 0:
        raise SessionError(f"cannot read secret {namespace}/{secret}: {out.stderr.strip().splitlines()[-1:] or ['no detail']}")
    try:
        data = json.loads(out.stdout)["data"]
        return base64.b64decode(data["username"]).decode(), base64.b64decode(data["password"]).decode()
    except (KeyError, ValueError) as exc:
        raise SessionError(f"secret {namespace}/{secret} lacks a username and password: {exc!r}") from exc


def git_auth_env(user: str, password: str) -> dict[str, str]:
    """Environment that makes git send HTTP basic auth, with nothing on argv."""
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token}",
        "GIT_TERMINAL_PROMPT": "0",
    }


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Session:
    """An open route to the deploy repo: the remote URL and the environment git needs."""

    url: str
    git_env: dict[str, str] = field(repr=False)


@dataclass
class BundledRepo:
    """Where the bundled deploy repo lives and how to reach it."""

    kubeconfig: str | None = None
    namespace: str = "forgejo"
    service: str = "svc/dfe-forgejo"
    secret: str = "dfe-forgejo-admin"
    repo: str = "deploy"
    owner: str | None = None
    # Set to skip the port-forward and the Secret read: a test's local bare repo.
    url_override: str | None = None
    ready_timeout: float = 30.0

    @contextlib.contextmanager
    def open(self, checkout: Path) -> Iterator[Session]:
        """Forward Forgejo, point `checkout`'s origin at it (cloning it when absent), then clean up."""
        if self.url_override is not None:
            session = Session(self.url_override, {})
            self._attach(checkout, session)
            yield session
            return
        user, password = read_admin(self.kubeconfig, self.namespace, self.secret)
        port = free_port()
        forward = subprocess.Popen(
            _kubectl(self.kubeconfig, "-n", self.namespace, "port-forward", self.service, f"{port}:{FORGEJO_REMOTE_PORT}"),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
        try:
            self._wait_ready(port, forward)
            session = Session(
                f"http://127.0.0.1:{port}/{self.owner or user}/{self.repo}.git", git_auth_env(user, password)
            )
            self._attach(checkout, session)
            yield session
        finally:
            forward.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                forward.wait(timeout=5)

    def _wait_ready(self, port: int, forward: subprocess.Popen) -> None:
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if forward.poll() is not None:
                reason = (forward.stderr.read() if forward.stderr else "").strip().splitlines()[-1:]
                raise SessionError(f"port-forward to {self.namespace}/{self.service} exited: {reason or 'no detail'}")
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
                return
            time.sleep(0.5)
        raise SessionError(f"port-forward to {self.namespace}/{self.service} not ready after {self.ready_timeout:.0f}s")

    def _attach(self, checkout: Path, session: Session) -> None:
        """Clone into `checkout` when it holds no repo, else retarget its origin at this session."""
        env = {**os.environ, **session.git_env}
        if (checkout / ".git").exists():
            run = subprocess.run(
                ["git", "-C", str(checkout), "remote", "set-url", "origin", session.url],
                capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, check=False,
            )
        else:
            checkout.parent.mkdir(parents=True, exist_ok=True)
            run = subprocess.run(
                ["git", "clone", "--quiet", session.url, str(checkout)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, check=False,
            )
        if run.returncode != 0:
            detail = run.stderr.strip().splitlines()[-1:] or ["no detail"]
            raise SessionError(f"cannot attach {checkout} to the deploy repo: {detail[0]}")
