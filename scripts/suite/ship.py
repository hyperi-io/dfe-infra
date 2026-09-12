#  Project:      dfe-infra
#  File:         scripts/suite/ship.py
#  Purpose:      Release a library node itself, to crates.io or PyPI.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Shipping the producer, which is where a suite pass starts.

Two paths, chosen by whether the caller has staged a fix. With a staged fix
there is a new commit to land, so the squash message carries the publish
trailer and the merge event is the release. With nothing staged there is no
message to hang a trailer on, so the workflow_dispatch escape hatch releases
main's HEAD as it stands -- and that path first snapshots the run already
sitting on the sha, so the wait cannot mistake it for the release.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from suite.artefacts import Artefact
from suite.landing import (
    already_released,
    find_run_for_sha,
    follow_release,
    land_via_pr,
    slugify,
)
from suite.proc import (
    DEFAULT_ORG,
    RUN_TIMEOUT_PYTHON,
    RUN_TIMEOUT_RUST,
    FleetError,
    require_tools,
    run,
    say,
    warn,
)
from suite.repos import find_repo, has_staged_changes, head_sha, repo_slug, sync_main


@dataclass(frozen=True, slots=True)
class ShipSpec:
    """The per-language differences between shipping scalo-rs and scalo-py."""

    repo_name: str
    artefact: Artefact
    run_timeout: int


SHIP_RS = ShipSpec(
    repo_name="scalo-rs",
    artefact=Artefact(kind="crates", name="scalo"),
    run_timeout=RUN_TIMEOUT_RUST,
)
SHIP_PY = ShipSpec(
    repo_name="scalo-py",
    artefact=Artefact(kind="pypi", name="scalo"),
    run_timeout=RUN_TIMEOUT_PYTHON,
)


def ship_library(
    spec: ShipSpec,
    *,
    repo_dir: str | Path | None = None,
    org: str = DEFAULT_ORG,
    subject: str = "",
    dry_run: bool = False,
) -> int:
    """Release the library itself, landing any staged fix through a PR first.

    Args:
        spec: Which library, and where its release lands.
        repo_dir: The checkout, when it is not where ``find_repo`` looks.
        org: GitHub org used when the remote cannot be read.
        subject: Conventional-commit subject, required when a fix is staged.
        dry_run: Say what would happen, touch nothing.

    Returns:
        The process exit code -- non-zero means STOP.
    """
    require_tools("git", "gh")
    repo = Path(repo_dir).expanduser() if repo_dir else find_repo(spec.repo_name)
    slug = repo_slug(repo, org)
    before = spec.artefact.baseline()
    say(
        f"{slug}: {spec.artefact.describe()} currently serves "
        f"{spec.artefact.name} {before or 'nothing'}"
    )

    staged = has_staged_changes(repo)
    if staged and not subject:
        raise FleetError("staged changes present but no commit subject (arg 1) -- aborting")

    if staged:
        # Commit the staged set before syncing, not after: the caller's index
        # is captured verbatim, and a subsequent conflict with origin/main
        # surfaces as a branch-level rebase conflict we can name rather than as
        # a mangled index. land_via_pr does the rebase.
        say("a fix is staged -- committing it to a branch before syncing")
        # A new commit to land, so use the proven trailer path: the squash
        # message reaches main and hyperi-ci's gate sees the trailer.
        merged = land_via_pr(
            repo=repo,
            slug=slug,
            branch=f"release/{slugify(subject)}",
            subject=subject,
            note=f"Release {spec.artefact.name}.",
            publish=True,
            dry_run=dry_run,
        )
        if dry_run:
            return 0
        assert merged is not None
        follow_release(
            slug=slug,
            sha=merged,
            artefact=spec.artefact,
            before=before,
            timeout=spec.run_timeout,
        )
        return 0

    # Nothing staged: re-release main's HEAD as it stands. There is no new
    # commit, so there is no squash message to hang a trailer on -- this is
    # exactly the case the workflow_dispatch escape hatch exists for, and it
    # is branch-protection-safe by design (the runner cuts the tag).
    if subject:
        warn(
            f"nothing is staged, so no commit is made and the subject "
            f"'{subject}' is unused. `git add` the fix first if you meant "
            f"to land one."
        )
    if dry_run:
        say("[dry-run] nothing staged -- would dispatch `hyperi-ci publish` for main HEAD")
        return 0

    sync_main(repo, dry_run=False)
    shipped = already_released(repo, before)
    if shipped:
        say(
            f"HEAD is already released as {shipped} and {spec.artefact.describe()} "
            f"serves {before} -- nothing to do"
        )
        return 0

    require_tools("hyperi-ci")
    sha = head_sha(repo)
    # `hyperi-ci publish` is a fire-and-forget `gh workflow run`: it returns no
    # run id, and this sha already carries the green push run that landed it.
    # Snapshot that run FIRST, so the wait below refuses it and follows the run
    # the dispatch creates -- otherwise the tool watches a run that predates
    # the release and judges the registry against it.
    previous = find_run_for_sha(slug, sha)
    if previous is not None:
        say(f"run {previous} already sits on this sha -- the dispatch must beat it")
    say("nothing staged -- dispatching a from-head release for main HEAD")
    run(["hyperi-ci", "publish"], cwd=repo, capture=False)
    follow_release(
        slug=slug,
        sha=sha,
        artefact=spec.artefact,
        before=before,
        timeout=spec.run_timeout,
        after_run_id=previous,
    )
    return 0
