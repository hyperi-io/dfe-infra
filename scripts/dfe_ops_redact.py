#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/dfe_ops_redact.py
#  Purpose:      `dfe-ops redact` -- stdin to stdout through the failure-evidence redactor,
#                a line at a time, so a long-running command's output can be piped through
#                it. Registered into dfe-ops the way dfe_ops_failure_evidence.py is.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-ops redact -- a command's output with every credential masked, as it prints.

    set -o pipefail
    tofu apply ... 2>&1 | scripts/dfe-ops redact

Runs the failure-evidence redactor: values under credential-named keys, URL userinfo,
bearer tokens, private key blocks and common provider token shapes become `***REDACTED***`.
Each line is written and flushed as it is read, and memory stays at one line however much
passes through. A private key block is masked from its BEGIN line to its END line, and an
END with no BEGIN masks only its own line. A line longer than MAX_LINE bytes is cut there
and ends in `***TRUNCATED***`, so one unbroken blob cannot fill memory.

A pipeline reports its last command's status, so without `pipefail` the line above exits 0
whatever tofu exited with. Set it in the step itself rather than rely on the runner's default.

The redactor ignores SIGINT, SIGTERM and SIGHUP and ends only at end of input. A producer that
traps them to finish a teardown, as `cloud-cycle` does, still has a pipe to write its destroy
output to.
"""

import argparse
import os
import signal
import sys
from typing import IO

from dfe_ops_failure_evidence import MASK, PEM_BEGIN, PEM_END, redact

MAX_LINE = 1024 * 1024  # bytes of one line kept; the rest of a longer line is dropped
CHUNK = 64 * 1024  # bytes read at a time while dropping the rest of a long line
TRUNCATED = "***TRUNCATED***"
# Signals a producer may be sent for a graceful stop; the redactor outlives it to the last byte.
IGNORED_SIGNALS = ("SIGINT", "SIGTERM", "SIGHUP")


def _read_line(src: IO[bytes]) -> tuple[bytes, bool]:
    """The next line, newline included, and whether a longer one was cut at MAX_LINE bytes."""
    line = src.readline(MAX_LINE)
    if len(line) < MAX_LINE or line.endswith(b"\n"):
        return line, False
    cut, newline = False, b""
    while rest := src.readline(CHUNK):
        if rest.endswith(b"\n"):
            newline, rest = b"\n", rest[:-1]
        cut = cut or bool(rest)
        if newline:
            break
    return (line, True) if cut else (line + newline, False)


def redact_stream(src: IO[bytes], dst: IO[bytes]) -> None:
    """Copy `src` to `dst` through `redact`, a line at a time, until `src` ends.

    Only a private key block spans lines, so it is the one state carried between them.
    """
    in_block = False
    while True:
        line, cut = _read_line(src)
        if not line and not cut:
            return
        text = line.decode("utf-8", errors="replace")
        if in_block:
            end = PEM_END.search(text)
            if end is None:
                continue
            in_block, text = False, text[end.end() :]
        begin = PEM_BEGIN.search(text)
        if begin and not PEM_END.search(text, begin.end()):
            in_block, text = True, text[: begin.start()] + MASK
        out = redact(text)
        dst.write((out + TRUNCATED + "\n" if cut else out).encode("utf-8"))
        dst.flush()


def cmd_redact(args: argparse.Namespace) -> int:
    """`dfe-ops redact`: stdin to stdout, redacted, until stdin closes."""
    for name in IGNORED_SIGNALS:
        if (number := getattr(signal, name, None)) is not None:
            signal.signal(number, signal.SIG_IGN)
    try:
        redact_stream(sys.stdin.buffer, sys.stdout.buffer)
    except BrokenPipeError:
        # A closed stdout would raise again when the interpreter flushes it on exit.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1
    return 0


def add_redact_subparser(sub: argparse._SubParsersAction) -> None:
    """Register `dfe-ops redact`."""
    rd = sub.add_parser(
        "redact",
        help="stdin to stdout with credentials masked, a line at a time: pipe a command's "
        "output through it",
        description="Reads stdin and writes it to stdout a line at a time with every credential "
        "masked: values under credential-named keys, URL userinfo, bearer tokens, private key "
        "blocks and common token shapes become ***REDACTED***. Ignores SIGINT, SIGTERM and "
        "SIGHUP and ends at end of input, so the producer's last output still arrives. "
        "Pipe with `set -o pipefail` to keep the producer's exit status: "
        "set -o pipefail; tofu apply ... 2>&1 | dfe-ops redact",
    )
    # A filter prints nothing of its own: the ephemeral banner would land in the log it cleans.
    rd.set_defaults(func=cmd_redact, banner=False)
