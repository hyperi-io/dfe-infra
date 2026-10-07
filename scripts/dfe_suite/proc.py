#  Project:      dfe-infra
#  File:         scripts/dfe_suite/proc.py
#  Purpose:      Output, subprocess and HTTP primitives shared by every suite helper.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The bottom layer: printing, running a command, and reading a JSON body.

Everything else in ``hyperi_ai.suite`` sits on these. No shell is ever used --
every command is an argv list -- and every subprocess and HTTP read decodes as
UTF-8 with replacement, so a stray byte from a runner log cannot abort a
release mid-flight.

The poll cadences live here too, because the run wait, the PR wait and the
registry wait all measure themselves against the same set.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = "scalo-fleet/2.0 (+https://github.com/hyperi-io/hyperi-ai)"
DEFAULT_ORG = os.environ.get("SCALO_FLEET_ORG", "hyperi-io")

# Poll cadences. The registry lags the publish by a CDN hop, so it gets its
# own shorter, more patient loop.
RUN_POLL_SECONDS = 30
RUN_APPEAR_TIMEOUT = 180
CHECKS_TIMEOUT = 3600
REGISTRY_POLL_SECONDS = 15
REGISTRY_TIMEOUT = 300
# The ONE ceiling on the whole artefact confirmation, spent as REGISTRY_TIMEOUT
# windows. 15 minutes covers crates.io and PyPI index lag with room to spare --
# the lag is a CDN hop, not a build -- without stalling an unattended fleet for
# an hour on a release that published nothing.
ARTEFACT_TIMEOUT = 900

# A Rust publish that does PGO + BOLT on both targets legitimately takes 50-70
# minutes; the early-fail is the real safety, the cap is just a backstop.
RUN_TIMEOUT_RUST = 7200
RUN_TIMEOUT_PYTHON = 3600

_TAG = "scalo-fleet"


class FleetError(RuntimeError):
    """A failure the operator has to act on. Never retried automatically."""


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def set_tag(name: str) -> None:
    """Name the tool in every progress line; each entry point sets its own."""
    global _TAG
    _TAG = name


def tag() -> str:
    """The tool name currently in force.

    An accessor rather than the module global, because importing ``_TAG`` by
    value freezes it at import time and every later ``set_tag`` is lost.
    """
    return _TAG


def say(message: str) -> None:
    """Print a progress line for the operator."""
    print(f"[{_TAG}] {message}", flush=True)


def warn(message: str) -> None:
    """Print a non-fatal warning to stderr."""
    print(f"[{_TAG}] WARNING: {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Subprocess
# ---------------------------------------------------------------------------


class CommandTimeoutError(FleetError):
    """A command ``run`` stopped because it outlived its timeout."""

    def __init__(self, argv: list[str], seconds: float) -> None:
        """Record which command was stopped and after how long.

        Args:
            argv: The command that was stopped.
            seconds: The timeout it outlived.
        """
        super().__init__(f"{argv[0] if argv else 'command'} timed out after {seconds:g}s")
        self.seconds = seconds


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command with no shell, decoding output as UTF-8.

    Args:
        argv: Command and arguments. Never a string -- no shell, no injection.
        cwd: Working directory.
        check: Raise ``FleetError`` on a non-zero exit.
        capture: Capture stdout/stderr rather than letting them through.
        timeout: Seconds before the command is killed, or None to wait for it.

    Returns:
        The completed process.

    Raises:
        CommandTimeoutError: If the command outlives ``timeout``.
        FleetError: If ``check`` and the command exits non-zero.
    """
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandTimeoutError(argv, timeout or 0) from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise FleetError(f"{' '.join(argv)} failed ({proc.returncode}): {detail}")
    return proc


def out(argv: list[str], *, cwd: Path | None = None) -> str:
    """Run a command and return its stripped stdout."""
    return run(argv, cwd=cwd).stdout.strip()


def gh_json(argv: list[str], *, cwd: Path | None = None) -> object:
    """Run a ``gh`` command that emits JSON and parse the result.

    Raises:
        FleetError: If gh fails, or emits something that is not JSON.
    """
    text = out(["gh", *argv], cwd=cwd)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FleetError(
            f"gh {' '.join(argv)} did not return JSON: {text[:200]}"
        ) from exc


def require_tools(*names: str) -> None:
    """Fail before doing any work if a required binary is missing.

    Raises:
        FleetError: Naming every missing tool at once, not just the first.
    """
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        raise FleetError(f"not on PATH: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _http_json(url: str, *, timeout: int = 30) -> object:
    """GET a URL and parse the JSON body.

    Raises:
        FleetError: On any transport or decode failure.
    """
    # urlopen honours file:// and ftp://, so pin the scheme rather than trust
    # the caller. Every URL here is a module constant plus a package name, but
    # the check keeps that true if someone later threads one in from argv.
    if not url.startswith("https://"):
        raise FleetError(f"refusing a non-https registry URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise FleetError(f"GET {url} failed: {exc}") from exc
