#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_propagate_stack_pin.py
#  Purpose:      Cover the dial rewrite, which is the one place a propagation
#                bug would land as a wrong pin in another repo.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/propagate_stack_pin.py.

The network half is a handful of `gh api` calls and is exercised for real by
the workflow. What is worth pinning here is the text surgery: the rewrite has
to move one number and leave every other byte of a file in another repo alone,
including the commented-out track-latest dial sitting three lines below it.

    python3 scripts/tests/test_propagate_stack_pin.py

No third-party deps and no test runner, matching the tool it tests.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "propagate_stack_pin.py"

spec = importlib.util.spec_from_file_location("propagate_stack_pin", SCRIPT)
propagate = importlib.util.module_from_spec(spec)
sys.modules["propagate_stack_pin"] = propagate
spec.loader.exec_module(propagate)

# The shape of the consumer's dial, commented track-latest block included.
DIAL = """\
registry: ghcr.io/hyperi-io

## Version dial: pin XOR track.
version:
  ## Pinned release -- written to DFE_STACK_VERSION.
  pin: 2.2.0-rc.11
  ## Track-latest instead: comment out `pin` above and use these.
  # track: latest
  # allow_prerelease: "false"

secrets:
  pin: never-touched
"""


def test_the_pinned_version_is_read_off_the_dial() -> None:
    expect("the dial's pin is found", propagate.pinned_in(DIAL) == "2.2.0-rc.11",
           f"{propagate.pinned_in(DIAL)!r}")


def test_a_track_latest_dial_has_nothing_to_move() -> None:
    """Commenting `pin` out is how a deployment opts into the update daemon;
    writing one back would take it off track-latest without being asked."""
    tracking = DIAL.replace("  pin: 2.2.0-rc.11\n", "  # pin: 2.2.0-rc.11\n")
    expect("no pin is reported", propagate.pinned_in(tracking) is None,
           f"{propagate.pinned_in(tracking)!r}")


def test_the_rewrite_moves_one_number_and_nothing_else() -> None:
    moved = propagate.rewrite(DIAL, "2.2.0-rc.13")
    expect("the pin moved", "  pin: 2.2.0-rc.13\n" in moved, moved)
    expect("the commented track dial is untouched", "  # track: latest\n" in moved, moved)
    expect(
        "and a `pin:` under another block is not the one that moved",
        "  pin: never-touched\n" in moved,
        moved,
    )
    expect(
        "only one line differs",
        sum(1 for a, b in zip(DIAL.splitlines(), moved.splitlines(), strict=True) if a != b) == 1,
        moved,
    )


def test_a_dial_already_on_current_rewrites_to_itself() -> None:
    expect(
        "an idempotent move is a no-op",
        propagate.rewrite(DIAL, "2.2.0-rc.11") == DIAL,
    )


def test_it_reads_the_committed_current_pointer() -> None:
    """Guards the failure where the pointer regex stops matching and the tool
    proposes moving a consumer onto an empty string."""
    version = propagate.current_stack(propagate.VERSIONS_FILE.read_text(encoding="utf-8"))
    expect("current resolves to a stack version", version.startswith("2."), f"{version!r}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
