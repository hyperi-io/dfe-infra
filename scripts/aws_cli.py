#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/aws_cli.py
#  Purpose:      The one subprocess.run call every aws CLI wrapper in this repo
#                shares, so cloud_sweep.py and resolve_sizing.py stop each
#                carrying their own copy of it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""aws_cli -- the shared plumbing under every `aws` CLI call in this repo.

Building the argv (``--region``, ``--output json``, or neither), choosing the
timeout, and turning a non-zero exit or a missing binary into the caller's own
error type all stay with the caller -- they differ deliberately (cloud_sweep.py
raises `CloudSweepError` with the FULL stderr at a 60s timeout,
resolve_sizing.py raises `ResolveError` with just the LAST stderr line at a
600s timeout) -- this is only the part that never varied between them: run
`aws <args>`, capture both streams as text, never raise on a non-zero exit.

    from aws_cli import run_aws

    done = run_aws(["ec2", "describe-instance-types", "--region", region], timeout=600)
"""

from __future__ import annotations

import subprocess


def run_aws(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run ``aws <args>``, capturing stdout/stderr as text.

    Args:
        args: The full argv after ``aws`` -- the caller assembles ``--region``
            and ``--output json`` itself, since not every call wants both.
        timeout: Seconds before the call counts as a hang rather than a slow
            API, passed straight to `subprocess.run`.

    Returns:
        The completed process, whatever its exit code -- `check=False`
        always, so a non-zero exit is the caller's to name.

    Raises:
        FileNotFoundError: The `aws` binary is not on PATH. Uncaught here:
            whether that is a fatal error and what it should say is a
            per-caller decision.
    """
    return subprocess.run(
        ["aws", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )
