#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/aws_cli.py
#  Purpose:      The one subprocess.run call every cloud CLI wrapper in this
#                repo shares, so cloud_sweep.py, resolve_sizing.py and
#                node_images.py stop each carrying their own copy of it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""aws_cli -- the shared plumbing under every cloud CLI call in this repo.

Building the argv (``--region``, ``--output json``, or neither), choosing the
timeout, and turning a non-zero exit or a missing binary into the caller's own
error type all stay with the caller -- they differ deliberately (cloud_sweep.py
raises `CloudSweepError` with the FULL stderr at a 60s timeout,
resolve_sizing.py raises `ResolveError` with just the LAST stderr line at a
600s timeout) -- this is only the part that never varied between them: run
the CLI, capture both streams as text, never raise on a non-zero exit.
`run_cli` takes the binary (``aws``, ``gcloud``, ``az``), and `run_aws` is the
same call with ``aws`` filled in.

    from aws_cli import run_aws

    done = run_aws(["ec2", "describe-instance-types", "--region", region], timeout=600)
"""

import subprocess


def run_cli(
    binary: str, args: list[str], *, timeout: float, new_session: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run ``<binary> <args>``, capturing stdout/stderr as text.

    Args:
        binary: The CLI to run, found on PATH.
        args: The full argv after the binary -- the caller assembles its own
            region and output flags, since not every call wants them.
        timeout: Seconds before the call counts as a hang rather than a slow
            API, passed straight to `subprocess.run`.
        new_session: Start the CLI in a session of its own, so a signal sent to
            the caller's process group never reaches it. The timeout and an
            exception in the caller still kill it.

    Returns:
        The completed process, whatever its exit code -- `check=False`
        always, so a non-zero exit is the caller's to name.

    Raises:
        FileNotFoundError: The binary is not on PATH. Uncaught here: whether
            that is a fatal error and what it should say is a per-caller
            decision.
    """
    return subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        start_new_session=new_session,
    )


def run_aws(args: list[str], *, timeout: float, new_session: bool = False) -> subprocess.CompletedProcess[str]:
    """Run ``aws <args>``, capturing stdout/stderr as text.

    Args:
        args: The full argv after ``aws``.
        timeout: Seconds before the call counts as a hang.
        new_session: Start it in a session of its own, as `run_cli` describes.

    Returns:
        The completed process, whatever its exit code.

    Raises:
        FileNotFoundError: The `aws` binary is not on PATH.
    """
    return run_cli("aws", args, timeout=timeout, new_session=new_session)
