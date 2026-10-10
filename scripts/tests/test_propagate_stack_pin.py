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
import subprocess
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

def _fake_gh(files_changed: int, calls: list[list[str]]):
    """A stand-in for `gh` answering each call the --open-pr path makes."""

    def fake(args: list[str]) -> str:
        calls.append(args)
        url = next(a for a in args if a.startswith("/repos/"))
        if "/contents/" in url and "--method" not in args:
            if f"ref={propagate.BRANCH}" in url:
                return '{"sha": "blob"}'
            return DIAL
        if "/commits/" in url:
            return "base-sha\n"
        if "/compare/" in url:
            return f"{files_changed}\n"
        if url.endswith("/pulls") and "POST" in args:
            return '{"html_url": "https://example.test/pull/1"}'
        if "/pulls?" in url:
            return "[]"
        return "{}"

    return fake


def _run_open_pr(tag_cut: bool, files_changed: int) -> tuple[int, list[list[str]]]:
    calls: list[list[str]] = []
    real_gh, real_git = propagate._gh, propagate._git
    propagate._gh = _fake_gh(files_changed, calls)
    # git ls-remote --exit-code: 0 when the tag exists, 2 when nothing matched.
    propagate._git = lambda args: subprocess.CompletedProcess(args, 0 if tag_cut else 2, "", "")
    try:
        rc = propagate.main(["--open-pr"])
    finally:
        propagate._gh, propagate._git = real_gh, real_git
    return rc, calls


def test_a_tag_lookup_error_is_not_read_as_uncut() -> None:
    real_git = propagate._git
    propagate._git = lambda args: subprocess.CompletedProcess(args, 128, "", "fatal: auth failed")
    try:
        propagate.tag_exists("2.2.1-rc.1")
    except propagate.PropagateError as exc:
        expect("an auth failure raises", "auth failed" in str(exc), str(exc))
    else:
        expect("an auth failure raises", False, "tag_exists returned instead of raising")
    finally:
        propagate._git = real_git


def _opened_pr(calls: list[list[str]]) -> bool:
    return any("POST" in c and any(a.endswith("/pulls") for a in c) for c in calls)


def test_an_uncut_stack_opens_no_pr_and_writes_nothing() -> None:
    rc, calls = _run_open_pr(tag_cut=False, files_changed=1)
    expect("exits 0", rc == 0, f"rc={rc}")
    expect("makes no write call", not any("--method" in c for c in calls), str(calls))


def test_a_bump_that_changes_nothing_opens_no_pr() -> None:
    rc, calls = _run_open_pr(tag_cut=True, files_changed=0)
    expect("exits 0", rc == 0, f"rc={rc}")
    expect("opens no PR", not _opened_pr(calls), str(calls))


def test_a_real_bump_on_a_cut_stack_opens_the_pr() -> None:
    rc, calls = _run_open_pr(tag_cut=True, files_changed=1)
    expect("exits 0", rc == 0, f"rc={rc}")
    expect("opens the PR", _opened_pr(calls), str(calls))


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
