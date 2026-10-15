#  Project:      dfe-infra
#  File:         stack_freeze.py
#  Purpose:      The one definition of "this stack is released" (its git tag
#                exists), for the writers that edit a versions.yaml stack block.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A stack whose git tag exists is a released record and takes no further edits.

Stack Release tags the stack `current` names and publishes the signed manifest at
that tag, and check_pin_evidence.py compares the next stack against the block in
versions.yaml. A block edited after its tag no longer matches what shipped, and
the edit never reaches the evidence check. The writers that rewrite a block call
refuse_if_tagged() first, with the same tag test release.yml cuts on.
"""

import subprocess
from pathlib import Path


def is_tagged(stack: str, repo_root: Path) -> bool:
    """Whether the repository at `repo_root` holds a git tag named `stack`.

    A checkout with no tags answers False for every stack, so a CI job that
    calls this must fetch tags (actions/checkout fetch-depth: 0).

    Args:
        stack: The stack version, which is also its tag name.
        repo_root: The checkout holding versions.yaml.

    Returns:
        True when `refs/tags/<stack>` resolves, the test release.yml cuts on.
    """
    done = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "-q", "--verify", f"refs/tags/{stack}"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    return done.returncode == 0


def refuse_if_tagged(stack: str, repo_root: Path, action: str) -> None:
    """Exit with an error naming the next step when `stack` is released.

    Args:
        stack: The stack version the caller is about to write to.
        repo_root: The checkout holding versions.yaml.
        action: What the caller would do, for the message ("bump-app").

    Raises:
        SystemExit: If the stack's git tag exists.
    """
    if is_tagged(stack, repo_root):
        raise SystemExit(
            f"error: stack {stack} is released (git tag {stack} exists), so {action} would edit "
            f"a record that shipped. Put the change in the next stack: dfe-stack cut <version>."
        )
