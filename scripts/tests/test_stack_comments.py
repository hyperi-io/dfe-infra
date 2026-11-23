#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_stack_comments.py
#  Purpose:      A cut writes a pin's reason once, in the block that moved it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""stack_comments drops comments on held pins and keeps every other one."""

import sys
from pathlib import Path

import pytest

from _expect import expect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stack_comments

PREVIOUS = """  1.0.0:
    constraints: "constraints/1.0.0.yaml"   # locks

    # Section note.
    services:
      # renovate: datasource=docker depName=a/held
      held: "1"                # reason for held
      # reason above steady
      steady: "1"
      # reason for moved
      moved: "1"               # old moved note
      # reason for dropped
      dropped: "1"
"""

CURRENT = """  1.0.1:
    constraints: "constraints/1.0.1.yaml"   # locks

    # Section note.
    services:
      # renovate: datasource=docker depName=a/held
      held: "1"                # reason for held
      # reason above steady
      steady: "1"
      # reason for moved
      moved: "2"               # old moved note
      # reason for added
      added: "1"               # new note
"""


@pytest.fixture(scope="module")
def stripped() -> str:
    cut = stack_comments.strip_held_comments(
        CURRENT.splitlines(keepends=True),
        "1.0.1",
        PREVIOUS.splitlines(keepends=True),
        "1.0.0",
    )
    return "".join(cut)


@pytest.mark.parametrize(
    ("what", "text", "kept"),
    [
        ("held pin's trailing reason", "reason for held", False),
        ("section note over a held map", "Section note", True),
        ("own version in a held line", "# locks", False),
        ("reason above a held pin", "reason above steady", False),
        ("annotation above a held pin", "# renovate: datasource=docker", True),
        ("moved pin's comment above", "# reason for moved", True),
        ("moved pin's trailing comment", "old moved note", True),
        ("added pin's comments", "reason for added", True),
        ("added pin's trailing comment", "new note", True),
    ],
)
def test_comment_kept_only_where_the_pin_is_new_or_moved(
    stripped: str, what: str, text: str, kept: bool
) -> None:
    expect(what, (text in stripped) is kept, stripped)


def test_the_first_block_has_no_predecessor_to_strip_against() -> None:
    text = "stacks:\n" + PREVIOUS + CURRENT
    out = stack_comments.strip_file(text)
    expect("the first block is untouched", out.startswith("stacks:\n" + PREVIOUS), out)
    expect(
        "the second block is stripped",
        "reason for held" not in out.split("  1.0.1:")[1],
        out,
    )
