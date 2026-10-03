#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         _expect.py
#  Purpose:      The one check helper every file in scripts/tests shares, so a
#                failed check RAISES under pytest and COUNTS under the standalone
#                runner CI invokes. Stdlib only.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""expect -- one honest check helper for both ways these tests run.

Each file here runs two ways: `python3 -m pytest scripts/tests`, and in CI
`python3 scripts/tests/<file>.py`. The runner wants every failure in one pass,
which means counting rather than raising -- but a counter nobody reads is a
green pytest run over a broken assertion. So expect() raises unless the file's
own main() is driving, and main() gets the count.

The leading underscore keeps pytest from collecting this module as a test file.

    from _expect import expect, standalone, summary

    def main() -> int:
        with standalone():
            for name, fn in sorted(globals().items()):
                if name.startswith("test_") and callable(fn):
                    fn()
            return summary()
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

_failures = 0
_standalone = False


def expect(name: str, condition: bool, detail: str = "") -> None:
    """One check: printed and counted under a runner, raised under pytest."""
    global _failures
    if condition:
        if _standalone:
            print(f"PASS  {name}")
        return
    _failures += 1
    if not _standalone:
        raise AssertionError(f"{name}: {detail}" if detail else name)
    # Detail is test text about charts rendered from example values, never a live
    # credential, which is why code-scanning alert 14 on this print is dismissed.
    print(f"FAIL  {name}  {detail}")


@contextmanager
def standalone() -> Iterator[None]:
    """Run a file's own main() in counting mode, from a zeroed count."""
    global _failures, _standalone
    _failures, _standalone = 0, True
    try:
        yield
    finally:
        _standalone = False


def summary() -> int:
    """Print the runner's closing line and return its exit code."""
    print(f"\n{'FAILED' if _failures else 'ALL PASSED'} -- {_failures} failure(s)")
    return 1 if _failures else 0
