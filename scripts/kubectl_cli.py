#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/kubectl_cli.py
#  Purpose:      The one subprocess.run call every kubectl wrapper in this repo
#                shares, so check_platform.py and check_node_capacity.py stop
#                each carrying their own copy of it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""kubectl_cli -- the shared plumbing under every `kubectl` CLI call in this repo.

Building the argv (``--kubeconfig``, ``get nodes -o json``, ``version -o
json``), choosing `check` and the timeout, and turning a non-zero exit or a
missing binary into the caller's own error type all stay with the caller --
they differ deliberately (check_platform.py's `cluster_version` wants
`check=True` and no timeout, letting `CalledProcessError` propagate raw;
check_node_capacity.py's `kubectl_get_nodes` wants `check=False`, a 60s
timeout, and its own `CapacityError` naming the last stderr line) -- this is
only the part that never varied between them: run `kubectl <args>`, capture
both streams as text.

    from kubectl_cli import run_kubectl

    done = run_kubectl(["get", "nodes", "-o", "json"], timeout=60)
"""

from __future__ import annotations

import subprocess


def run_kubectl(
    args: list[str], *, timeout: float | None = None, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run ``kubectl <args>``, capturing stdout/stderr as UTF-8 text.

    Decoding is pinned rather than left to the locale, and an undecodable byte
    becomes U+FFFD instead of raising.

    Args:
        args: The full argv after ``kubectl`` -- the caller assembles
            ``--kubeconfig``, the verb and ``-o json`` itself.
        timeout: Seconds before the call counts as a hang, passed straight to
            `subprocess.run`. None means no timeout, matching the caller that
            never set one.
        check: Passed straight to `subprocess.run` -- True raises
            `CalledProcessError` on a non-zero exit instead of returning it,
            for the one caller that wants that.

    Returns:
        The completed process.

    Raises:
        FileNotFoundError: The `kubectl` binary is not on PATH. Uncaught
            here: whether that is fatal and what it should say is a
            per-caller decision.
        subprocess.CalledProcessError: Only when `check` is True and the
            call exits non-zero.
    """
    return subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
        timeout=timeout,
    )
