#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/check_submodule_drift.py
#  Purpose:      Drift guard: assert every consumer of a shared submodule pins
#                the same commit, so a repo cannot sit on a stale schema while
#                the others move on.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Validate that shared-submodule consumers agree on one commit.

Renovate raises the bump PR (the org preset enables the git-submodules manager),
but it cannot stop one repo sitting on an unmerged bump for a month. That gap is
what let dfe-engine, dfe-loader and dfe-fetcher hold three different dfe-schemas
commits -- 0, 26 and 27 behind main -- while two of them built against a schema
that predated the `_org_id` rename.

A bot to move it, a gate to prove it moved. This is the gate.

Pins are read over the GitHub API rather than from checkouts, so dfe-infra needs
none of the consumer repos on disk. A submodule entry comes back from the
contents API with `type: submodule` and its pinned commit in `sha`.

Usage:
    python3 scripts/check_submodule_drift.py
    python3 scripts/check_submodule_drift.py --json

Needs `gh` authenticated. Without it the check reports a LOUD skip rather than a
silent pass -- an unverified pin set is not an agreeing one.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

# Shared submodule -> the repos that vendor it, and the path it sits at.
# A consumer joins by being listed here; nothing discovers them, because a
# repo we forgot to list is exactly the drift this guard exists to catch.
#
# EMPTY: no suite repo vendors a shared submodule -- dfe-schemas ships as a
# pinned wheel, and no DFE repo carries a .gitmodules (verified 2026-09-16
# across the org). The table stays so the next one is an entry, not a tool.
SHARED_SUBMODULES: dict[str, dict] = {}

ORG = "hyperi-io"


def _gh_json(endpoint: str) -> object | None:
    """GET an API endpoint, returning None when it does not resolve."""
    result = subprocess.run(
        ["gh", "api", endpoint],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def _readable(repo: str) -> bool:
    """Whether the token can read *repo* at all.

    CI's default GITHUB_TOKEN is scoped to the repo it runs in, so every other
    private repo 404s exactly as a deleted one would. Asking this first is what
    separates "the token cannot see it" from "the submodule is gone".
    """
    return isinstance(_gh_json(f"repos/{ORG}/{repo}"), dict)


def _pinned_sha(repo: str, path: str) -> str | None:
    payload = _gh_json(f"repos/{ORG}/{repo}/contents/{path}")
    if not isinstance(payload, dict) or payload.get("type") != "submodule":
        return None
    sha = payload.get("sha")
    return sha if isinstance(sha, str) else None


def _head_sha(repo: str) -> str | None:
    payload = _gh_json(f"repos/{ORG}/{repo}/commits/HEAD")
    if not isinstance(payload, dict):
        return None
    sha = payload.get("sha")
    return sha if isinstance(sha, str) else None


def _behind(repo: str, base: str, head: str) -> int | None:
    """Commits `base` is behind `head`, or None when it cannot be compared."""
    if base == head:
        return 0
    payload = _gh_json(f"repos/{ORG}/{repo}/compare/{base}...{head}")
    if not isinstance(payload, dict):
        return None
    ahead = payload.get("ahead_by")
    return ahead if isinstance(ahead, int) else None


def audit() -> tuple[list[str], list[dict], list[str]]:
    """Return (failure messages, per-consumer rows, skip messages)."""
    failures: list[str] = []
    rows: list[dict] = []
    skips: list[str] = []

    for module, spec in sorted(SHARED_SUBMODULES.items()):
        head = _head_sha(module)
        if head is None:
            if not _readable(module):
                skips.append(f"{module}: not readable by this token")
                continue
            failures.append(f"{module}: cannot read its default-branch HEAD")
            continue

        looked: list[str] = []
        missing: list[str] = []
        for consumer in spec["consumers"]:
            if not _readable(consumer):
                skips.append(f"{consumer}: not readable by this token")
                continue
            looked.append(consumer)
            pinned = _pinned_sha(consumer, spec["path"])
            if pinned is None:
                missing.append(consumer)
                continue
            behind = _behind(module, pinned, head)
            rows.append(
                {
                    "module": module,
                    "consumer": consumer,
                    "pinned": pinned[:7],
                    "behind": behind,
                }
            )
            if pinned != head:
                gap = "an unknown number of" if behind is None else str(behind)
                failures.append(
                    f"{consumer}: pins {module} at {pinned[:7]}, {gap} commit(s) behind {head[:7]}"
                )

        # No consumer this token could read still vendors it: the class is
        # retired, so the entry is stale rather than the repos disagreeing.
        if looked and len(missing) == len(looked):
            failures.append(
                f"{module}: none of {', '.join(looked)} vendors it at `{spec['path']}` "
                f"any more -- delete the entry from SHARED_SUBMODULES"
            )
        else:
            failures += [
                f"{consumer}: no submodule at `{spec['path']}` -- it was removed or moved"
                for consumer in missing
            ]

    return failures, rows, skips


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the pin table as JSON")
    args = parser.parse_args()

    # An empty table is a real state, not an unverified one: nothing in the
    # suite vendors a shared submodule, so there is no pin to disagree about.
    if not SHARED_SUBMODULES:
        print("OK -- no shared submodule is declared; nothing vendors one.")
        return 0

    if shutil.which("gh") is None:
        print(
            "SKIPPED: gh is not on PATH -- submodule pins NOT checked. This is a skip, not a pass.",
            file=sys.stderr,
        )
        return 0

    failures, rows, skips = audit()

    if args.json:
        print(json.dumps(rows, indent=2))

    # A repo-scoped token 404s on a sibling private repo exactly as a deleted
    # one does, so report it as unverified rather than calling it drift.
    for skip in skips:
        print(
            f"SKIPPED: {skip} -- pin NOT checked. This is a skip, not a pass.",
            file=sys.stderr,
        )

    if failures:
        print("FAIL: shared-submodule drift", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(
            "  Renovate raises the bump PR; merging it is what closes this.",
            file=sys.stderr,
        )
        return 1

    checked = len(rows)
    if not checked:
        print(
            "SKIPPED: no submodule pin was readable -- nothing was verified.",
            file=sys.stderr,
        )
        return 0

    print(
        f"PASS: {checked} consumer pin(s) across {len(SHARED_SUBMODULES)} shared "
        "submodule(s) match the current commit"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
