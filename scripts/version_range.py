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
(``26.3.13.31`` is four parts, ``v26.1.8`` is three).

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


def version_tuple(value: str) -> tuple[int, ...]:
    """Tolerant version -> tuple of ints: 26.3.13.31 -> (26,3,13,31); v26.1.8 -> (26,1,8)."""
    value = value.strip()
    if value.startswith("v"):
        value = value[1:]
    parts: list[int] = []
    for segment in value.split("."):
        m = re.match(r"\d+", segment)
        if not m:
            break
        parts.append(int(m.group(0)))
    return tuple(parts)


def satisfies(version: str, spec: str) -> bool:
    """ALL whitespace-separated comparators (>= <= > < ==) must hold.

    A spec with a token this cannot parse is False, not an exception: a
    malformed range must read as unsatisfied so the rule carrying it is
    reported, never silently treated as met.
    """
    actual = version_tuple(version)
    for token in spec.split():
        m = _COMPARATOR.match(token)
        if not m:
            return False
        if not _OPS[m.group(1)](actual, version_tuple(m.group(2))):
            return False
    return True
