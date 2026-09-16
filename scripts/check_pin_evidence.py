#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         check_pin_evidence.py
#  Purpose:      Fail when a versions.yaml pin moves and the comment holding
#                the evidence for it does not, so a bump cannot silently leave
#                the justification describing the version it replaced.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every pin in versions.yaml carries its own evidence, and a move must move it.

    python3 scripts/check_pin_evidence.py
    python3 scripts/check_pin_evidence.py --stack 2.2.0-rc.13
    python3 scripts/check_pin_evidence.py --all

Each version in versions.yaml sits under a comment holding the reasoning for
it: the release date, that it cleared the 7-day cooldown, why this line rather
than the stable one, and sometimes what was validated against it. That prose is
the audit trail, and it is the only place the reasoning is written down.

Two writers move a pin without touching that comment. Renovate rewrites the
value in the current stack block and never reads the comment at all. `dfe-stack
cut` clones the previous block as the shape and rewrites the app pins inside
it, so the comments arrive describing the versions they replaced. Either way
the evidence detaches from the thing it is evidence for, and the reason it goes
unnoticed is that every other entry in the file is trustworthy (dfe-infra#184).

The check needs no base ref and no git: versions.yaml already holds the
previous state, because every stack carries a complete pin set and names its
predecessor. So for each pin in the audited stack, compare it to the same pin
in that stack's `stack.previous`. A value that moved with byte-identical
evidence is reported.

By default the AUDITED stack is `current` -- the block under development, the
one a PR actually edits. Historical blocks are frozen records of what shipped,
and rewriting their comments now would be inventing evidence, so `--all`
sweeps every adjacent pair as a REPORT and never as a gate.

No third-party deps: the same restricted YAML subset the rest of the pin
tooling reads, plus the comments a parser throws away.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_FILE = REPO_ROOT / "versions.yaml"

# `key: "value"` with an optional trailing comment. Only quoted scalars are
# pins; an unquoted value in this file is structure, not a version.
_PIN = re.compile(r'^(?P<indent>\s*)(?P<key>[A-Za-z0-9_.-]+):\s*"(?P<value>[^"]*)"\s*(?:#(?P<note>.*))?$')
_MAP = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_.-]+):\s*$")
_COMMENT = re.compile(r"^\s*#(?P<text>.*)$")

# Sections whose entries are pins with evidence. `digests` and `services-digests`
# are the immutable half of a pin somewhere else and carry the image ref rather
# than a justification, so a digest moving with its tag is not a detachment.
PIN_SECTIONS = ("bootstrap", "operators", "services", "apps", "content", "providers", "toolbox")


@dataclass(frozen=True)
class Pin:
    """One pin and the prose standing behind it."""

    value: str
    evidence: str
    line: int


def _normalise(text: str) -> str:
    """Comment prose, whitespace-flattened, so a rewrap is not a rewrite."""
    return " ".join(text.split())


def read_pins(text: str) -> dict[str, dict[str, Pin]]:
    """Every stack's pins, keyed stack -> 'section.key' -> Pin.

    A pin's evidence is its own trailing comment plus the contiguous comment
    lines immediately above it, which is where a wrapped justification lives.
    A blank line breaks that run, the same binding `dfe-stack refresh-digests`
    reads for its image annotations.
    """
    stacks: dict[str, dict[str, Pin]] = {}
    stack = section = None
    preamble: list[str] = []
    in_stacks = False

    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            preamble = []
            continue
        comment = _COMMENT.match(raw)
        if comment:
            preamble.append(comment.group("text"))
            continue

        pin = _PIN.match(raw)
        if pin:
            indent = len(pin.group("indent"))
            if stack and section and indent >= 6:
                note = pin.group("note") or ""
                stacks[stack][f"{section}.{pin.group('key')}"] = Pin(
                    value=pin.group("value"),
                    evidence=_normalise(" ".join([*preamble, note])),
                    line=number,
                )
            preamble = []
            continue

        opener = _MAP.match(raw)
        preamble = []
        if not opener:
            continue
        indent, key = len(opener.group("indent")), opener.group("key")
        if indent == 0:
            in_stacks = key == "stacks"
            stack = section = None
        elif indent == 2 and in_stacks:
            stack, section = key, None
            stacks.setdefault(stack, {})
        elif indent == 4 and stack:
            section = key if key in PIN_SECTIONS else None
    return stacks


def detached(previous: dict[str, Pin], current: dict[str, Pin]) -> list[tuple[str, Pin, str]]:
    """Pins whose value moved between two stacks while their evidence did not.

    A pin the previous stack does not carry is new: there is no evidence to
    have detached, and a stack that adds a component is the normal case.
    """
    found = []
    for name, pin in sorted(current.items()):
        was = previous.get(name)
        if was is None or was.value == pin.value:
            continue
        if was.evidence == pin.evidence:
            found.append((name, pin, was.value))
    return found


def _previous_of(text: str, stack: str) -> str | None:
    """The stack a block names as its predecessor, read off `stack.previous`.

    `previous` is a comma-separated list where a stack was reachable from
    several; the first entry is the immediate predecessor the block was cut
    from, which is the one whose comments this block inherited.
    """
    block = re.search(
        rf"^  {re.escape(stack)}:\n(.*?)(?=^  \S|\Z)", text, re.MULTILINE | re.DOTALL
    )
    if not block:
        return None
    found = re.search(r'^      previous:\s*"([^"]*)"', block.group(1), re.MULTILINE)
    if not found:
        return None
    first = found.group(1).split(",")[0].strip()
    return first or None


def report(pairs: list[tuple[str, str]], stacks: dict[str, dict[str, Pin]]) -> list[str]:
    """One line per detachment, naming the file line so it can be repaired."""
    problems = []
    for previous, current in pairs:
        for name, pin, was in detached(stacks[previous], stacks[current]):
            problems.append(
                f"  versions.yaml:{pin.line}  {current} {name}: moved {was!r} -> "
                f"{pin.value!r} and its comment is word-for-word the one that "
                f"justified {was!r} in {previous} -- rewrite the evidence"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--stack", help="audit this stack instead of `current`")
    parser.add_argument(
        "--all",
        action="store_true",
        help="report every adjacent pair rather than gating on one (exit 0 either way)",
    )
    args = parser.parse_args(argv)

    text = VERSIONS_FILE.read_text(encoding="utf-8")
    stacks = read_pins(text)
    if not stacks:
        print("FAIL -- versions.yaml yielded no pins; the reader and the file disagree")
        return 1

    current = args.stack or (re.search(r'^current:\s*"([^"]+)"', text, re.MULTILINE) or [None, None])[1]
    if current not in stacks:
        raise SystemExit(f"versions.yaml: stack {current!r} not found ({', '.join(stacks)})")

    if args.all:
        pairs = [
            (p, name)
            for name in stacks
            if (p := _previous_of(text, name)) and p in stacks
        ]
    else:
        previous = _previous_of(text, current)
        if not previous or previous not in stacks:
            print(f"OK -- {current} names no predecessor in this file; nothing to compare")
            return 0
        pairs = [(previous, current)]

    problems = report(pairs, stacks)
    for line in problems:
        print(f"{'REPORT' if args.all else 'FAIL  '}{line}")
    if args.all:
        print(f"\nreported {len(problems)} detached comment(s) across {len(pairs)} stack pair(s)")
        return 0
    if problems:
        print(
            f"\nFAIL -- {len(problems)} pin(s) in {current} moved without their evidence.\n"
            f"versions.yaml's comments are the audit trail and its most valuable\n"
            f"property is that they can be trusted. Write the new justification:\n"
            f"the release date, that it clears the cooldown, and why this line."
        )
        return 1
    audited = len(stacks[current])
    print(f"OK -- every pin {current} moved from {pairs[0][0]} carries its own evidence "
          f"({audited} pin(s) compared)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
