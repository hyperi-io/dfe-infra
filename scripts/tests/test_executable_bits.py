#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_executable_bits.py
#  Purpose:      Prove every committed script that declares an interpreter is
#                executable in git, and that a sourced library is not.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A shebang is a promise the file can be run.

bootstrap.sh invokes access-summary.sh by path, so the 0644 mode it was
committed with turned the whole access summary into a `Permission denied` line
at the end of an otherwise successful deploy. The mode is what git stores, not
what the checkout happens to have, so this reads `git ls-files -s`.

A file with no shebang (bootstrap/scripts/profiles.sh is sourced, never run) is
expected to be 0644 and is checked for that.

    python3 scripts/tests/test_executable_bits.py

No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# Trees whose files are run rather than imported. scripts/ is deliberately out:
# its CLIs are invoked as `python3 scripts/<name>.py` from make targets and CI.
TREES = ("bootstrap",)
EXECUTABLE = "100755"
PLAIN = "100644"
# The bats runner takes its files as arguments, so a .bats shebang documents the
# runner rather than promising the file can be executed by path.
NOT_RUN_BY_PATH = (".bats",)


def tracked_modes() -> dict[str, str]:
    """path -> git file mode, for every tracked file in the checked trees."""
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-s", *TREES],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise SystemExit(f"git ls-files failed:\n{out.stderr}")
    modes = {}
    for line in out.stdout.splitlines():
        meta, _, path = line.partition("\t")
        modes[path] = meta.split()[0]
    return modes


def has_shebang(path: str) -> bool:
    full = REPO_ROOT / path
    if not full.is_file():
        return False
    with full.open("rb") as handle:
        return handle.read(2) == b"#!"


def test_a_shebang_means_executable() -> None:
    offenders = [
        f"{path} is {mode}"
        for path, mode in sorted(tracked_modes().items())
        if has_shebang(path) and mode != EXECUTABLE and not path.endswith(NOT_RUN_BY_PATH)
    ]
    expect("every script with a shebang is +x in git", offenders == [], f"got {offenders}")


def test_no_shebang_means_not_executable() -> None:
    offenders = [
        f"{path} is {mode}"
        for path, mode in sorted(tracked_modes().items())
        if (not has_shebang(path) or path.endswith(NOT_RUN_BY_PATH)) and mode != PLAIN
    ]
    expect("nothing else carries the bit", offenders == [], f"got {offenders}")


def main() -> int:
    with standalone():
        test_a_shebang_means_executable()
        test_no_shebang_means_not_executable()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
