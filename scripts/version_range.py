#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/version_range.py
#  Purpose:      The one version comparator the constraints files are written
#                against, so the check that enforces a guard and the check that
#                reports it dead can never disagree about what the range means.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""version_range -- tolerant version comparison for the constraints DSL.

    from version_range import satisfies, version_tuple

    satisfies("v26.1.17", ">=26.1.0 <26.2.0")   # True

A range is whitespace-separated comparators (``>=`` ``<=`` ``>`` ``<`` ``==``)
and ALL of them must hold. Comparison is tolerant rather than strict semver: a
leading ``v`` is stripped and the leading-numeric dotted segments are compared
as integers, because the pins in versions.yaml are upstream tags, not semver
(``26.3.13.31`` is four parts, ``v26.1.8`` is three). A trailing alpha, beta or
rc suffix ranks below the same version without it, so ``1.22.81-rc.1`` is
``<1.22.81``.

Stdlib only: check_versions_drift.py runs on a bare CI image with no PyYAML and
no third-party anything, and this is on its path.

dfe-stack's compat-check DECIDES with these, and check_versions_drift.py's
dead-guard check REPORTS with them. Two copies would let a rule read as live to
one and dead to the other, which is the failure dfe-infra#295 is about.
"""

from __future__ import annotations

import re

_OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
}

_COMPARATOR = re.compile(r"(>=|<=|==|>|<)\s*(.+)")


_PRERELEASE = re.compile(r"^(?P<core>.+?)-(?P<pre>(?:alpha|beta|rc)(?:\.[0-9A-Za-z]+)*)$")


def _split(value: str) -> tuple[str, str | None]:
    """A version without its leading v, split from a trailing alpha/beta/rc suffix."""
    value = value.strip()
    if value.startswith("v"):
        value = value[1:]
    m = _PRERELEASE.match(value)
    return (m.group("core"), m.group("pre")) if m else (value, None)


def version_tuple(value: str) -> tuple[int, ...]:
    """Tolerant version -> tuple of ints: 26.3.13.31 -> (26,3,13,31); v26.1.8 -> (26,1,8)."""
    core, _ = _split(value)
    parts: list[int] = []
    for segment in core.split("."):
        m = re.match(r"\d+", segment)
        if not m:
            break
        parts.append(int(m.group(0)))
    return tuple(parts)


def version_key(value: str) -> tuple:
    """Sort key that puts 1.22.81-rc.1 below 1.22.81 and rc.10 above rc.9.

    Only a trailing alpha, beta or rc suffix is a prerelease. Any other dash, as in
    a dated upstream tag like 1.34.1-20251010, compares by version_tuple alone.
    """
    _, pre = _split(value)
    if pre is None:
        return (version_tuple(value), 1, ())
    ids = tuple((0, int(x), "") if x.isdigit() else (1, 0, x) for x in pre.split("."))
    return (version_tuple(value), 0, ids)


def is_prerelease(value: str) -> bool:
    """True for a tag ending in an alpha, beta or rc suffix."""
    return _split(value)[1] is not None


def satisfies(version: str, spec: str) -> bool:
    """ALL whitespace-separated comparators (>= <= > < ==) must hold.

    A spec with a token this cannot parse is False, not an exception: a
    malformed range must read as unsatisfied so the rule carrying it is
    reported, never silently treated as met.
    """
    actual = version_key(version)
    for token in spec.split():
        m = _COMPARATOR.match(token)
        if not m:
            return False
        if not _OPS[m.group(1)](actual, version_key(m.group(2))):
            return False
    return True
