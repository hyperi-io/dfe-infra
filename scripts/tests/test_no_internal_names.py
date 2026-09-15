#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_no_internal_names.py
#  Purpose:      Guard the public-at-GA rule: no internal estate hostname, host
#                nickname or private address survives anywhere in the tree.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Refuse an internal estate name anywhere a reader of the public repo can see it.

dfe-infra becomes public at GA, so every committed byte ships to the world. An
internal hostname, a host nickname or an RFC 1918 address written down here
tells an outsider how the development estate is laid out, and it cannot be
taken back once the visibility flips -- rewriting a repo's history to remove it
is painful and easy to get wrong. Documentation names exist for this: RFC 2606
`example.com` / `example.internal` for hostnames, and the RFC 5737 ranges
192.0.2.0/24, 198.51.100.0/24 and 203.0.113.0/24 for addresses.

The sweep reads every tracked file and names the file and line of each hit.

    python3 -m pytest scripts/tests/test_no_internal_names.py -q
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The zone the estate is served from, the hosts addressed by nickname, and the
# two private ranges it numbers. Each is matched case-insensitively, so a
# capitalised product or heading is caught alongside a hostname.
INTERNAL = re.compile(
    r"devex\.hyperi\.io"
    r"|tyrell"
    r"|hypersec"
    r"|10\.66\."
    r"|10\.1\.2\."
    r"|dragonfly"
    r"|desktop-derek"
    r"|proxmox",
    re.IGNORECASE,
)

# versions.yaml and constraints/ are the version SSoT and are compared
# byte-for-byte against main by the drift guards, so they are never rewritten
# here. This file carries the pattern itself and would match on every line.
EXCLUDED = {
    "versions.yaml",
    "scripts/tests/test_no_internal_names.py",
}
EXCLUDED_PREFIXES = ("constraints/",)


def _tracked_files() -> list[str]:
    """Every file git tracks -- exactly the set that ships when the repo goes public."""
    listed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [name for name in listed.split("\0") if name]


def _in_scope(name: str) -> bool:
    return name not in EXCLUDED and not name.startswith(EXCLUDED_PREFIXES)


def _hits(name: str) -> list[str]:
    try:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError):
        # A binary blob carries no prose to leak, and a listed-but-absent path is
        # a submodule pointer rather than a file.
        return []
    return [
        f"{name}:{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if INTERNAL.search(line)
    ]


def test_no_tracked_file_names_the_internal_estate() -> None:
    found: list[str] = []
    for name in _tracked_files():
        if _in_scope(name):
            found += _hits(name)
    assert not found, (
        "internal estate names found -- this repo goes public, so use a "
        "documentation name (example.com, example.internal) or an RFC 5737 "
        "address instead:\n" + "\n".join(found)
    )


def test_the_sweep_would_catch_a_leak() -> None:
    """The guard is worth nothing if the pattern never matches, so prove it does."""
    for sample in ("k8s-1.devex.hyperi.io", "10.66.0.200", "Proxmox VE", "dragonfly"):
        assert INTERNAL.search(sample), sample
