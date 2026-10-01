#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_pin_evidence.py
#  Purpose:      Prove the evidence check fires on both writers that detach a
#                comment from its pin, and stays quiet on the moves that carry
#                their justification with them.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Failure-path tests for scripts/check_pin_evidence.py.

A check that only ever prints OK proves nothing, so these drive the two shapes
dfe-infra#184 names -- Renovate rewriting a value in place, and a cut cloning a
block's comments onto moved pins -- and assert the check notices both.

Everything runs in memory over literal versions.yaml fragments; no tracked file
is read or written.

    python3 scripts/tests/test_pin_evidence.py

No third-party deps and no test runner, matching the check it tests.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_pin_evidence.py"

spec = importlib.util.spec_from_file_location("pin_evidence", SCRIPT)
evidence = importlib.util.module_from_spec(spec)
sys.modules["pin_evidence"] = evidence
spec.loader.exec_module(evidence)


def _fragment(older: str, newer: str) -> dict:
    """A two-stack versions.yaml carrying one services pin per stack."""
    return evidence.read_pins(
        'schema: 2\n'
        'current: "9.9.0-rc.2"\n'
        '\n'
        'stacks:\n'
        '\n'
        '  9.9.0-rc.1:\n'
        '    services:\n'
        f'{older}'
        '    stack:\n'
        '      previous: ""\n'
        '\n'
        '  9.9.0-rc.2:\n'
        '    services:\n'
        f'{newer}'
        '    stack:\n'
        '      previous: "9.9.0-rc.1"\n'
    )


def _detached(older: str, newer: str) -> list[str]:
    stacks = _fragment(older, newer)
    return [name for name, _, _ in evidence.detached(stacks["9.9.0-rc.1"], stacks["9.9.0-rc.2"])]


def test_a_value_rewritten_under_its_old_comment_is_caught() -> None:
    """Renovate's shape: the number moves in place and the comment never moves."""
    line = '      a-thing: "{}"    # 2026-07-22 (clears cooldown); validated against this image.\n'
    expect(
        "the moved pin is reported",
        _detached(line.format("1.0.0"), line.format("2.0.0")) == ["services.a-thing"],
        f"{_detached(line.format('1.0.0'), line.format('2.0.0'))}",
    )


def test_a_move_that_rewrites_its_evidence_is_left_alone() -> None:
    """The inverse -- a check that flagged these would train people to ignore it."""
    expect(
        "a pin carrying new evidence raises nothing",
        _detached(
            '      a-thing: "1.0.0"    # 2026-07-22 (clears cooldown).\n',
            '      a-thing: "2.0.0"    # 2026-08-18 (clears cooldown); newest on the LTS line.\n',
        ) == [],
    )


def test_an_unmoved_pin_is_never_reported() -> None:
    """Most pins carry over untouched, and carrying a comment over with them is
    the correct thing to do."""
    line = '      a-thing: "1.0.0"    # 2026-07-22 (clears cooldown).\n'
    expect("an unchanged pin raises nothing", _detached(line, line) == [])


def test_a_pin_the_previous_stack_never_had_is_not_a_detachment() -> None:
    """A stack that adds a component has no earlier evidence to have detached."""
    expect(
        "a new pin raises nothing",
        _detached("", '      a-thing: "1.0.0"    # 2026-07-22 (clears cooldown).\n') == [],
    )


def test_the_evidence_above_a_pin_counts_as_its_evidence() -> None:
    """A justification long enough to wrap lives above the key, not beside it."""
    older = (
        "      # 2026-07-22 (clears cooldown). Stays on the 26.3 LTS line\n"
        "      # deliberately: 26.4 is stable-line, not LTS.\n"
        '      a-thing: "1.0.0"\n'
    )
    moved_only = older.replace('"1.0.0"', '"2.0.0"')
    expect(
        "moving the value under an unchanged block comment is caught",
        _detached(older, moved_only) == ["services.a-thing"],
        f"{_detached(older, moved_only)}",
    )
    rewritten = (
        "      # 2026-08-18 (clears cooldown). 26.4 opened an LTS line of its\n"
        "      # own, so this moves off 26.3 with it.\n"
        '      a-thing: "2.0.0"\n'
    )
    expect(
        "and rewriting that block clears it",
        _detached(older, rewritten) == [],
        f"{_detached(older, rewritten)}",
    )


def test_rewrapping_a_comment_is_not_rewriting_it() -> None:
    """Otherwise a formatter pass would silence the check on every pin at once."""
    older = '      a-thing: "1.0.0"    # 2026-07-22 (clears cooldown). Stays on LTS.\n'
    rewrapped = (
        "      # 2026-07-22 (clears cooldown).\n"
        "      # Stays on LTS.\n"
        '      a-thing: "2.0.0"\n'
    )
    expect(
        "the same words in a different shape still read as unchanged",
        _detached(older, rewrapped) == ["services.a-thing"],
        f"{_detached(older, rewrapped)}",
    )


def test_a_blank_line_breaks_the_comment_to_pin_binding() -> None:
    """A section header two blank lines up is not evidence for the pin below it."""
    stacks = _fragment(
        '      a-thing: "1.0.0"\n',
        "      # A heading about the section, not about the pin.\n"
        "\n"
        '      a-thing: "2.0.0"\n',
    )
    expect(
        "the pin reads as having no evidence at all",
        stacks["9.9.0-rc.2"]["services.a-thing"].evidence == "",
        f"{stacks['9.9.0-rc.2']['services.a-thing'].evidence!r}",
    )


def test_digests_are_not_treated_as_evidence_bearing_pins() -> None:
    """A digest is the immutable half of a tag pinned elsewhere; it moves WITH
    that tag and its comment is the image ref, not a justification."""
    stacks = evidence.read_pins(
        'stacks:\n'
        '\n'
        '  9.9.0-rc.1:\n'
        '    digests:\n'
        '      an-app: "sha256:aaa"   # ghcr.io/org/an-app:v1.0.0\n'
    )
    expect(
        "no digests entry is collected",
        stacks["9.9.0-rc.1"] == {},
        f"{stacks['9.9.0-rc.1']}",
    )


def test_it_reads_the_committed_file_and_finds_real_pins() -> None:
    """Guards the failure where the reader stops matching and audits nothing,
    which prints the same OK as a clean file."""
    stacks = evidence.read_pins(evidence.VERSIONS_FILE.read_text(encoding="utf-8"))
    expect("every stack block is seen", len(stacks) >= 13, f"{sorted(stacks)}")
    current = stacks.get("2.2.0-rc.13", {})
    expect("and the current stack yields a real pin set", len(current) > 40, f"{len(current)}")
    expect(
        "including a pin whose evidence is the wrapped block above it",
        "1 (clears cooldown)" not in current["services.redpanda-version"].evidence
        and "tested pairing" in current["services.redpanda-version"].evidence,
        current["services.redpanda-version"].evidence[:120],
    )


def test_the_committed_current_stack_passes_the_gate() -> None:
    expect("check_pin_evidence is green as committed", evidence.main([]) == 0)


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
