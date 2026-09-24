#  Project:      dfe-infra
#  File:         scripts/dfe_suite/landing.py
#  Purpose:      Land a change on protected main through a PR, then follow the run.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The git operations a suite release needs, done the way protection allows.

Merging to main and cutting a release are the two operations an agent is
normally refused. They are safe HERE because the shape is fixed: a change only
ever reaches main as a branch, a PR, and a squash merge whose message this
module authors, and every wait early-fails the instant a job or a check goes
red rather than blocking opaquely to a deadline.

The squash message is the publish trigger. hyperi-ci reads
``git log -1 --format=%B`` on ``refs/heads/main`` and looks for a
``Publish: true`` trailer, so the message authored at merge time -- not the
branch commit -- is what turns the merge event into the publish run. A release
that needs workflow inputs a push event cannot carry lands with no trailer and
is fired by :func:`dispatch_release` instead.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import NamedTuple

from dfe_suite.artefacts import Artefact, await_artefact
from dfe_suite.proc import (
    CHECKS_TIMEOUT,
    RUN_APPEAR_TIMEOUT,
    RUN_POLL_SECONDS,
    FleetError,
    gh_json,
    run,
    say,
)
from dfe_suite.repos import git, head_sha

# The trailer hyperi-ci greps for on main. Must sit on its own line in the
# squash commit body: .github/actions/predict-version/action.yml matches
# `^[[:space:]]*Publish:[[:space:]]*true[[:space:]]*$`.
PUBLISH_TRAILER = "Publish: true"

_RED_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "startup_failure",
    "action_required",
}

# The release workflow's FILE name: every hyperi-ci consumer carries the same
# thin caller at .github/workflows/ci.yml. Other workflows land runs against
# the same head sha -- a "Graph Update: uv" dependency-graph run registered two
# seconds after the release run and was followed instead of it (issue #83) --
# so the run to watch is pinned by workflow, never by recency. The file name is
# what `gh run list --workflow` filters on, server-side, and unlike the `name:`
# a repo declares inside the file it cannot drift when someone re-titles it.
CI_WORKFLOW_FILE = "ci.yml"


# ---------------------------------------------------------------------------
# Watching a workflow run
# ---------------------------------------------------------------------------


class RunRow(NamedTuple):
    """A ``gh run list`` row, reduced to what the fleet uses."""

    id: int
    workflow: str


def _select_run(
    payload: object, sha: str, *, after_run_id: int | None = None
) -> RunRow | None:
    """The newest run for ``sha`` in an already workflow-filtered list, or None.

    ``gh run list --workflow ci.yml`` has done the workflow half server-side,
    so every row here is a ci.yml run and the job left is picking the right one
    for this sha. Run ids ascend, so the newest is the largest id -- and
    ``after_run_id`` is how the workflow_dispatch escape hatch refuses the run
    that was ALREADY sitting green on this sha before the dispatch fired.

    Args:
        payload: The parsed ``gh run list --json`` rows.
        sha: The head commit the run has to belong to.
        after_run_id: Reject any run at or below this id.

    Returns:
        The run, or None when no matching run has registered yet.
    """
    if not isinstance(payload, list):
        return None
    best: RunRow | None = None
    for row in payload:
        if not isinstance(row, dict):
            continue
        if row.get("headSha") != sha:
            continue
        raw = row.get("databaseId")
        # A row without a usable id is a row we cannot follow, not a crash.
        if not isinstance(raw, (int, str)):
            continue
        try:
            run_id = int(raw)
        except ValueError:
            continue
        if after_run_id is not None and run_id <= after_run_id:
            continue
        if best is None or run_id > best.id:
            best = RunRow(run_id, str(row.get("workflowName") or CI_WORKFLOW_FILE))
    return best


def find_run_for_sha(
    slug: str,
    sha: str,
    *,
    branch: str = "main",
    workflow: str = CI_WORKFLOW_FILE,
    after_run_id: int | None = None,
) -> int | None:
    """The newest ``workflow`` run whose head commit is ``sha``, or None.

    Args:
        slug: ``owner/name`` on GitHub.
        sha: The head commit the run has to belong to.
        branch: The branch the run has to sit on.
        workflow: The workflow FILE, filtered server-side by gh.
        after_run_id: Reject any run at or below this id, so a re-release of a
            sha does not resolve to the run that was already on it.
    """
    payload = gh_json(
        [
            "run",
            "list",
            "-R",
            slug,
            "--workflow",
            workflow,
            "--branch",
            branch,
            "--limit",
            "30",
            "--json",
            "databaseId,headSha,workflowName",
        ]
    )
    found = _select_run(payload, sha, after_run_id=after_run_id)
    if found is None:
        return None
    say(f"{workflow} run {found.id} ('{found.workflow}') matches {sha[:12]}")
    return found.id


def await_run(
    slug: str,
    sha: str,
    *,
    branch: str = "main",
    workflow: str = CI_WORKFLOW_FILE,
    after_run_id: int | None = None,
) -> int:
    """Wait for the ``workflow`` run to register for ``sha`` and return its id.

    Never resolve "the latest run on this branch" instead: immediately after a
    push the new run has not registered, so that returns the PREVIOUS already
    green run and reports success instantly -- a false green that hides a
    failing publish. ``after_run_id`` closes the same hole for a dispatch that
    re-releases a sha which already has a green run of its own.

    Raises:
        FleetError: If no run appears before the deadline.
    """
    say(f"finding the {workflow} run for {sha[:12]}")
    skipping = f" (ignoring runs up to {after_run_id})" if after_run_id else ""
    deadline = time.monotonic() + RUN_APPEAR_TIMEOUT
    while time.monotonic() < deadline:
        run_id = find_run_for_sha(
            slug, sha, branch=branch, workflow=workflow, after_run_id=after_run_id
        )
        if run_id is not None:
            return run_id
        time.sleep(5)
    raise FleetError(
        f"no {workflow} run appeared for {sha} on {slug}@{branch} after "
        f"{RUN_APPEAR_TIMEOUT // 60}min{skipping}. Is .github/workflows/{workflow} "
        f"present in the repo? Check `gh run list -R {slug} --workflow {workflow}`; "
        f"if the push genuinely triggered nothing, release it with "
        f"`hyperi-ci publish` from a checkout sitting on that commit."
    )


def poll_run(slug: str, run_id: int, *, timeout: int) -> None:
    """Follow a run to completion, failing the moment any job goes red.

    Deliberately not ``gh run watch``: that blocks opaquely for the whole run
    and reports a failure only at the end, stalling an unattended fleet for an
    hour on something that died ten minutes in.

    Raises:
        FleetError: On a red job, a non-success conclusion, or the deadline.
    """
    say(
        f"polling run {run_id} ({RUN_POLL_SECONDS}s cadence; early-fail on any red "
        f"job; {timeout // 60}min cap)"
    )
    deadline = time.monotonic() + timeout
    while True:
        proc = run(
            [
                "gh",
                "run",
                "view",
                str(run_id),
                "-R",
                slug,
                "--json",
                "status,conclusion,jobs",
            ],
            check=False,
        )
        if proc.returncode == 0:
            try:
                data = json.loads(proc.stdout or "{}")
            except json.JSONDecodeError:
                data = {}
            jobs = data.get("jobs") or []
            red = [
                j.get("name", "?")
                for j in jobs
                if isinstance(j, dict)
                and (j.get("conclusion") or "").lower() in _RED_CONCLUSIONS
            ]
            if red:
                raise FleetError(
                    f"job(s) went RED: {', '.join(red)} -- "
                    f"gh run view {run_id} -R {slug} --log-failed"
                )
            if data.get("status") == "completed":
                conclusion = (data.get("conclusion") or "unknown").lower()
                if conclusion == "success":
                    say(f"run {run_id} GREEN")
                    return
                raise FleetError(f"run {run_id} concluded '{conclusion}'")
        # A transient gh/API error just means poll again -- it is not a verdict.
        if time.monotonic() >= deadline:
            raise FleetError(
                f"run {run_id} still going after {timeout // 60}min -- bailing. "
                f"Check it by hand: gh run view {run_id} -R {slug}"
            )
        time.sleep(RUN_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Landing a change on protected main, via a PR
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """A short, git-safe branch fragment."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return cleaned[:40] or "change"


def commit_body(*, note: str, publish: bool) -> str:
    """The commit body, with the publish trailer last where one is wanted.

    Blank line before the trailer so git treats it as a trailer, and so the
    hyperi-ci gate's line-anchored match sees it on a line of its own.
    """
    return f"{note}\n\n{PUBLISH_TRAILER}\n" if publish else f"{note}\n"


def _pr_number_from_url(text: str) -> int | None:
    """The PR number in a ``.../pull/<n>`` URL, if there is one."""
    match = re.search(r"/pull/(\d+)", text or "")
    return int(match.group(1)) if match else None


def existing_pr(slug: str, branch: str) -> int | None:
    """An open PR already raised from ``branch``, if there is one."""
    payload = gh_json(
        [
            "pr",
            "list",
            "-R",
            slug,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number",
        ]
    )
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return int(payload[0]["number"])
    return None


def await_pr_mergeable(slug: str, pr: int) -> None:
    """Wait until the PR's required checks pass, failing early on any red one.

    ``mergeStateStatus`` is the authority on "required checks satisfied", which
    matters because the check rollup is EMPTY for the first few seconds after a
    PR opens -- treating that as "nothing pending" would merge the change
    unvalidated.

    Raises:
        FleetError: On a red check, a conflicted PR, or the deadline.
    """
    say(f"waiting on PR #{pr} checks (early-fail on any red check)")
    deadline = time.monotonic() + CHECKS_TIMEOUT
    while True:
        payload = gh_json(
            [
                "pr",
                "view",
                str(pr),
                "-R",
                slug,
                "--json",
                "mergeStateStatus,mergeable,statusCheckRollup",
            ]
        )
        data = payload if isinstance(payload, dict) else {}

        red = _red_checks(data.get("statusCheckRollup") or [])
        if red:
            raise FleetError(
                f"PR #{pr} check(s) went RED: {', '.join(red)} -- "
                f"gh pr checks {pr} -R {slug}"
            )

        state = data.get("mergeStateStatus")
        if state == "CLEAN":
            say(f"PR #{pr} is CLEAN -- required checks satisfied")
            return
        # These never resolve on their own, so waiting out the deadline would
        # be an hour of silence before the same answer. BLOCKED is deliberately
        # absent: it is the normal state while required checks are still going.
        stuck = {
            "DIRTY": "has merge conflicts against main",
            "BEHIND": "is behind main and the base requires a strict update",
            "DRAFT": "is a draft",
        }
        if state in stuck:
            raise FleetError(f"PR #{pr} {stuck[state]} -- resolve by hand")
        if data.get("mergeable") == "CONFLICTING":
            raise FleetError(f"PR #{pr} conflicts with main -- resolve by hand")
        # BLOCKED / UNSTABLE / UNKNOWN all mean "checks still settling".

        if time.monotonic() >= deadline:
            raise FleetError(
                f"PR #{pr} still '{state}' after {CHECKS_TIMEOUT // 60}min -- bailing. "
                f"Check it by hand: gh pr view {pr} -R {slug}"
            )
        time.sleep(RUN_POLL_SECONDS)


def _red_checks(rollup: object) -> list[str]:
    """Names of checks in the rollup that have definitively failed."""
    red: list[str] = []
    if not isinstance(rollup, list):
        return red
    for entry in rollup:
        if not isinstance(entry, dict):
            continue
        # CheckRun entries carry status+conclusion; StatusContext carries state.
        conclusion = (entry.get("conclusion") or "").lower()
        state = (entry.get("state") or "").lower()
        if conclusion in _RED_CONCLUSIONS or state in {"failure", "error"}:
            red.append(str(entry.get("name") or entry.get("context") or "?"))
    return red


def land_via_pr(
    *,
    repo: Path,
    slug: str,
    branch: str,
    subject: str,
    note: str,
    publish: bool,
    dry_run: bool,
) -> str | None:
    """Commit the staged change, land it on main through a PR, return main's sha.

    The whole point of the tool. Main is protected, so: branch, push, PR, wait
    for the required checks, squash-merge with an authored message, and come
    back to a fast-forwarded local main. The squash message is what lands on
    main, so it -- not the branch commit -- is what carries the publish
    trailer and what ``check-commits`` validates.

    Args:
        repo: The checkout.
        slug: ``owner/name`` on GitHub.
        branch: Deterministic branch name, so a re-run reuses its own PR.
        subject: Conventional-commit subject line.
        note: One-line body explaining the change.
        publish: Put the ``Publish: true`` trailer on the squash commit.
        dry_run: Describe the steps, touch nothing.

    Returns:
        The sha of the squash commit now on main, or None on a dry run.

    Raises:
        FleetError: If any step fails.
    """
    body = commit_body(note=note, publish=publish)

    if dry_run:
        say(
            f"[dry-run] would branch {branch}, commit '{subject}', open a PR on {slug},"
        )
        say(f"[dry-run] wait for its checks, then squash-merge with trailer={publish}")
        return None

    say(f"branching {branch}")
    # -C so a leftover branch from an interrupted run is reset, not fatal.
    git("switch", "-C", branch, cwd=repo)

    say(f"committing: {subject}")
    git("commit", "-m", subject, "-m", body, cwd=repo)

    # Rebase after the commit, so the branch is never stale against main and
    # the caller's staged set is already safely captured in a commit.
    say("rebasing the branch onto origin/main")
    git("fetch", "origin", "main", "--tags", "--quiet", cwd=repo)
    git("rebase", "--autostash", "origin/main", cwd=repo)
    branch_sha = head_sha(repo)

    say(f"pushing {branch}")
    pushed = run(
        ["git", "push", "--set-upstream", "origin", branch], cwd=repo, check=False
    )
    if pushed.returncode != 0:
        # A leftover branch from an interrupted run is the only expected
        # rejection here, and it is ours to replace. --force-with-lease still
        # refuses if someone else moved it.
        say(f"{branch} already exists on the remote -- replacing our own branch")
        git("fetch", "origin", branch, "--quiet", cwd=repo, check=False)
        git("push", "--force-with-lease", "--set-upstream", "origin", branch, cwd=repo)

    pr = existing_pr(slug, branch)
    if pr is None:
        say("opening the PR")
        created = run(
            [
                "gh",
                "pr",
                "create",
                "-R",
                slug,
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                subject,
                "--body",
                note,
            ],
            cwd=repo,
        )
        # gh prints the new PR's URL, which beats re-listing: the list endpoint
        # is eventually consistent and can miss a PR opened a second ago.
        pr = _pr_number_from_url(created.stdout) or existing_pr(slug, branch)
        if pr is None:
            raise FleetError(f"opened a PR from {branch} but cannot find it on {slug}")
        say(f"opened PR #{pr}")
    else:
        say(f"reusing the open PR #{pr} for {branch}")

    await_pr_mergeable(slug, pr)

    trailer = "carries the release trailer" if publish else "carries no release trailer"
    say(f"squash-merging PR #{pr} (the squash message {trailer})")
    run(
        [
            "gh",
            "pr",
            "merge",
            str(pr),
            "-R",
            slug,
            "--squash",
            "--delete-branch",
            # Refuse to merge anything other than the commit we just validated.
            "--match-head-commit",
            branch_sha,
            "--subject",
            subject,
            "--body",
            body,
        ],
        cwd=repo,
        capture=False,
    )

    # gh may already have switched away and deleted the local branch; either
    # way, end up on a main that matches the remote.
    git("switch", "main", cwd=repo, check=False)
    git("fetch", "origin", "main", "--tags", "--quiet", cwd=repo)
    git("merge", "--ff-only", "origin/main", cwd=repo)
    merged = head_sha(repo)
    say(f"merged -- main is now {merged[:12]}")
    return merged


# ---------------------------------------------------------------------------
# Shared release tail
# ---------------------------------------------------------------------------


def follow_release(
    *,
    slug: str,
    sha: str,
    artefact: Artefact,
    before: str,
    timeout: int,
    after_run_id: int | None = None,
) -> None:
    """Follow the publish run for ``sha`` and prove the artefact moved.

    Args:
        slug: ``owner/name`` on GitHub.
        sha: The commit the release rides on.
        artefact: Where the release lands.
        before: The version that destination served before the release.
        timeout: The run's own ceiling.
        after_run_id: Ignore runs at or below this id -- the caller snapshotted
            the run already sitting on ``sha`` before it dispatched a new one.
    """
    run_id = await_run(slug, sha, after_run_id=after_run_id)
    poll_run(slug, run_id, timeout=timeout)
    after = await_artefact(artefact, before, slug=slug, run_id=run_id)
    was = before or "nothing"
    say(f"SHIPPED: {artefact.name} {was} -> {after} live on {artefact.describe()}")


def dispatch_release(
    *,
    repo: Path,
    slug: str,
    sha: str,
    dispatch: list[str],
    artefact: Artefact,
    before: str,
    timeout: int,
    just_merged: bool = False,
    watch: bool = True,
) -> None:
    """Release main's HEAD through a workflow_dispatch and follow the run it creates.

    A dispatch is fire-and-forget: it returns no run id, and ``sha`` already
    carries the push run that landed it. That run is snapshotted FIRST, so the
    wait refuses it and follows the run the dispatch creates -- otherwise the
    tool watches a run that predates the release and judges the artefact
    against it.

    Args:
        repo: The checkout the dispatch runs from.
        slug: ``owner/name`` on GitHub.
        sha: main's HEAD, the commit the dispatched run releases.
        dispatch: The argv that fires the dispatch.
        artefact: Where the release lands.
        before: The version that destination served before the release.
        timeout: The run's own ceiling.
        just_merged: ``sha`` landed moments ago, so wait for its push run to
            register before taking the snapshot. hyperi-ci's rust-ci.yml
            cancels in-progress runs in a per-ref concurrency group, so a push
            run that registers after the dispatch cancels the release.
        watch: Follow the release. False returns once the dispatch is sent.

    Raises:
        FleetError: If the push run never registers, the dispatch fails, or the
            release does not complete.
    """
    if just_merged:
        previous: int | None = await_run(slug, sha)
    else:
        previous = find_run_for_sha(slug, sha)
    if previous is not None:
        say(f"run {previous} already sits on this sha -- the dispatch must beat it")
    say(f"dispatching: {' '.join(dispatch)}")
    run(dispatch, cwd=repo, capture=False)
    if not watch:
        say(f"dispatched -- not watching (--no-watch). Check: gh run list -R {slug}")
        return
    follow_release(
        slug=slug,
        sha=sha,
        artefact=artefact,
        before=before,
        timeout=timeout,
        after_run_id=previous,
    )


def already_released(repo: Path, live: str) -> str | None:
    """The tag on HEAD when HEAD is already published, else None.

    Re-running a ship must be idempotent. Dispatching anyway fires a duplicate
    release that dies at plan time, which burns a run and leaves a red mark in
    the history for nothing.
    """
    proc = run(
        ["git", "describe", "--tags", "--exact-match", "HEAD"], cwd=repo, check=False
    )
    tag = proc.stdout.strip()
    if tag and live and tag.lstrip("v") == live:
        return tag
    return None
