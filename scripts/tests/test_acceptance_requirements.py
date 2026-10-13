#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_acceptance_requirements.py
#  Purpose:      Hold scripts/acceptance/requirements.txt to what the suites
#                import, pinned and hashed, so it installs on its own.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/acceptance/requirements.txt.

An operator, and dfe-docker's unattended proof, install that file and nothing
else before running the suites. So every third-party module a suite loads must
be pinned in it, and every pin hashed, because it is installed with
--require-hashes.

    python3 -m pytest scripts/tests/test_acceptance_requirements.py
    python3 scripts/tests/test_acceptance_requirements.py
"""

import re
import sys
from pathlib import Path

import pytest

REQUIREMENTS = Path(__file__).resolve().parent.parent / "acceptance" / "requirements.txt"

# The third-party imports of dfe-engine's tests/e2e/filebeat_corpus.py and the
# tests/support/producer_contract.py it imports, which source/steps.py loads to
# read the corpus for every pushed case.
CORPUS_WRAPPER_IMPORTS = ("httpx", "pytest")

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)$")
_HASH = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")


def requirements(text: str) -> dict[str, tuple[str, list[str]]]:
    """Each requirement's normalised name, mapped to its version and its hash options."""
    found: dict[str, tuple[str, list[str]]] = {}
    logical = re.sub(r"\\\n", " ", text)
    for line in logical.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        spec, *options = line.split()
        pin = _PIN.match(spec)
        if pin is None:
            raise AssertionError(f"not an exact pin: {spec}")
        name = re.sub(r"[-_.]+", "-", pin.group(1)).lower()
        found[name] = (pin.group(2), options)
    return found


def test_every_requirement_is_an_exact_pin_carrying_a_sha256() -> None:
    pinned = requirements(REQUIREMENTS.read_text(encoding="utf-8"))

    assert pinned
    unhashed = [
        name
        for name, (_, options) in pinned.items()
        if not options or not all(_HASH.match(option) for option in options)
    ]
    assert unhashed == []


@pytest.mark.parametrize("module", CORPUS_WRAPPER_IMPORTS)
def test_what_the_engine_corpus_wrapper_imports_is_pinned_here(module: str) -> None:
    pinned = requirements(REQUIREMENTS.read_text(encoding="utf-8"))

    assert module in pinned, f"{module} is imported by the corpus wrapper and not pinned"


def test_an_unhashed_or_loose_line_is_refused() -> None:
    with pytest.raises(AssertionError):
        requirements("pytest>=9\n")
    assert requirements("pytest==9.1.1\n") == {"pytest": ("9.1.1", [])}


def main() -> int:
    return pytest.main([__file__, "-q"])


if __name__ == "__main__":
    sys.exit(main())
