#  Project:      dfe-infra
#  File:         scripts/suite/landing.py
#  Purpose:      The ONE PR-landing implementation: open, wait, squash-merge
#                with the release trailer, then follow the run it triggered.
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
branch commit -- is what turns the merge event into the publish run.

Two callers share this: :mod:`suite.ship` and :mod:`suite.rebuild` land a
change they have just staged, and ``scripts/dfe-release.py`` lands a branch
somebody else pushed and then resolves the GHCR digest. Both open the PR, wait
and squash-merge through the functions below, so there is one definition of
what landing on main means.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import NamedTuple

from suite.artefacts import Artefact, await_artefact
from suite.proc import (
    CHECKS_TIMEOUT,
    RUN_APPEAR_TIMEOUT,
    RUN_POLL_SECONDS,
    FleetError,
    gh,
    gh_json,
    run,
    say,
)
from suite.repos import git, head_sha

# The trailer hyperi-ci greps for on main. Must sit on its own line in the
# squash commit body: .github/actions/predict-version/action.yml matches
# `^[[:space:]]*Publish:[[:space:]]*true[[:space:]]*$`.
PUBLISH_TRAILER = "Publish: true"

RED_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "startup_failure",
    "action_required",
}

# The release workflow's FILE name: every hyperi-ci consumer carries the same
# thin caller at .github/workflows/ci.yml. Other workflows land runs against
# the same head sha -- a dependency-graph run registered two seconds after the
# release run and was followed instead of it -- so the run to watch is pinned by
# workflow, never by recency. The file name is what `gh run list --workflow`
# filters on, server-side, and unlike the `name:` a repo declares inside the
# file it cannot drift when someone re-titles it.
CI_WORKFLOW_FILE = "ci.yml"

# The workflow DISPLAY name, which is what `gh run list -w` matches for the
# GHCR verbs. Shared across wait/ship so the two never drift apart.
DEFAULT_WORKFLOW = "CI"
DEFAULT_INTERVAL = 20
DEFAULT_TIMEOUT = 1800


# ---------------------------------------------------------------------------
# Watching a workflow run
# ---------------------------------------------------------------------------


class RunRow(NamedTuple):
    """A ``gh run list`` row, reduced to what the suite tooling uses."""

    id: int
    workflow: str


def select_run(payload: object, sha: str, *, after_run_id: int | None = None) -> RunRow | None:
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
    found = select_run(payload, sha, after_run_id=after_run_id)
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
        proc = gh(
            ["run", "view", str(run_id), "-R", slug, "--json", "status,conclusion,jobs"]
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
                if isinstance(j, dict) and (j.get("conclusion") or "").lower() in RED_CONCLUSIONS
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


def latest_run(repo: str, workflow: str) -> dict | None:
    """The newest run of a workflow on any branch, or None when it cannot be read."""
    proc = gh(
        [
            "run",
            "list",
            "-R",
            repo,
            "-w",
            workflow,
            "--limit",
            "1",
            "--json",
            "databaseId,status,conclusion,headBranch,createdAt,url",
        ]
    )
    if proc.returncode != 0:
        return None
    rows = json.loads(proc.stdout or "[]")
    return rows[0] if rows else None


def active_main_run(repo: str, workflow: str) -> tuple[dict | None, str]:
    """A queued or in-progress run of the workflow on main, if there is one.

    The publish workflow's concurrency group is keyed on the branch, so merging
    while one of these is in flight cancels it and its release never lands.

    Returns:
        (the run record or None, an error message if the state is unknown).
    """
    for status in ("in_progress", "queued"):
        proc = gh(
            [
                "run",
                "list",
                "-R",
                repo,
                "-w",
                workflow,
                "-b",
                "main",
                "--status",
                status,
                "--limit",
                "1",
                "--json",
                "databaseId,status,url",
            ]
        )
        if proc.returncode != 0:
            return None, proc.stderr.strip() or f"gh run list failed for {repo}"
        rows = json.loads(proc.stdout or "[]")
        if rows:
            return rows[0], ""
    return None, ""


def wait_for_run(
    repo: str,
    workflow: str,
    interval: int,
    timeout: int,
    *,
    after_id: int | None = None,
    out=None,
    err=None,
) -> tuple[int, dict | None]:
    """Poll the workflow's latest run to completion.

    after_id names the run that was already the latest before the trigger, so a
    just-merged publish waits for ITS run instead of reporting the previous
    one's conclusion.

    Returns:
        (exit code -- 0 success, 1 failed, 2 timed out; the completed run or None).
    """
    # Resolved here, not as a default: a default binds sys.stdout at import.
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    waited = 0
    while True:
        found = latest_run(repo, workflow)
        if found is None:
            print("no runs found yet", file=err)
        elif after_id is not None and found.get("databaseId") == after_id:
            print(f"waiting for the run {workflow} will start ({waited}s)", file=err)
        else:
            status = found.get("status")
            if status == "completed":
                concl = found.get("conclusion")
                print(f"{workflow} {concl} ({found.get('url')})", file=out)
                return (0 if concl == "success" else 1), found
            print(f"{workflow} {status}... ({waited}s) {found.get('url')}", file=out)
        if waited >= timeout:
            print(f"timed out after {timeout}s waiting for {workflow}", file=err)
            return 2, None
        time.sleep(interval)
        waited += interval


# ---------------------------------------------------------------------------
# Landing a change on protected main, via a PR
# ---------------------------------------------------------------------------


def slugify(text: str) -> str:
    """A short, git-safe branch fragment."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return cleaned[:40] or "change"


def commit_body(*, note: str, publish: bool) -> str:
    """The commit body, with the publish trailer last where one is wanted.

    Blank line before the trailer so git treats it as a trailer, and so the
    hyperi-ci gate's line-anchored match sees it on a line of its own.
    """
    return f"{note}\n\n{PUBLISH_TRAILER}\n" if publish else f"{note}\n"


def pr_number_from_url(text: str) -> int | None:
    """The PR number in a ``.../pull/<n>`` URL, if there is one."""
    match = re.search(r"/pull/(\d+)", text or "")
    return int(match.group(1)) if match else None


def open_pr_number(repo: str, head: str) -> int | None:
    """The number of the OPEN PR for a branch, or None.

    State is checked because `gh pr view <branch>` also resolves a merged or
    closed PR for that branch, and merging one of those again is not a no-op.
    """
    proc = gh(["pr", "view", head, "-R", repo, "--json", "number,state"])
    if proc.returncode != 0:
        return None
    data = json.loads(proc.stdout or "{}")
    if data.get("state") != "OPEN":
        return None
    return data.get("number")


def open_pr(
    repo: str,
    head: str,
    base: str,
    title: str,
    *,
    body: str | None = None,
    body_file: str | None = None,
) -> tuple[int, str, str]:
    """Create the PR for a branch, falling back to the one already open for it.

    Args:
        repo: ``owner/name`` on GitHub.
        head: The pushed branch.
        base: The branch to merge into.
        title: The PR title.
        body: PR body text, when the caller authored one in memory.
        body_file: PR body file, when the caller has one on disk.

    Returns:
        (exit code, stdout text, stderr text). stdout carries the PR URL on a
        fresh create, or the existing PR's JSON when one was already open.
    """
    cmd = ["pr", "create", "-R", repo, "--head", head, "--base", base, "--title", title]
    if body_file:
        cmd += ["--body-file", body_file]
    else:
        cmd += ["--body", body or ""]
    proc = gh(cmd)
    if proc.returncode != 0:
        # Already open? report the existing PR url/number.
        existing = gh(["pr", "view", head, "-R", repo, "--json", "number,url"])
        if existing.returncode == 0:
            return 0, existing.stdout.strip(), ""
        return 1, "", proc.stderr.strip()
    return 0, proc.stdout.strip(), ""


def merge_pr(
    repo: str,
    pr: int,
    *,
    publish: bool,
    admin: bool = False,
    delete_branch: bool = False,
    subject: str | None = None,
    note: str | None = None,
    body: str | None = None,
    match_head_commit: str | None = None,
) -> tuple[int, str, str]:
    """Merge a PR to main; publish squash-merges so the merge itself ships.

    The publish workflow only fires on a `refs/heads/main` push whose message
    carries `Publish: true`, and a squash merge replaces the branch commits with
    the message given here -- so the trailer belongs on the squash message, not
    on any commit in the branch. One landing instead of merge-then-dispatch.

    Args:
        repo: ``owner/name`` on GitHub.
        pr: The PR number.
        publish: Squash-merge with the release trailer.
        admin: Bypass required checks, on an authorised run.
        delete_branch: Delete the head branch on merge.
        subject: Squash subject; defaults to the PR title.
        note: Squash body line; defaults to the PR body's first line. Ignored
            when ``body`` is given.
        body: The whole squash body verbatim, for a caller that authored it.
        match_head_commit: Refuse to merge anything but this commit.

    Returns:
        (exit code, stdout text, stderr text).
    """
    cmd = ["pr", "merge", str(pr), "-R", repo]
    if publish:
        squash_subject = subject
        squash_body = body
        if squash_subject is None or squash_body is None:
            view = gh(["pr", "view", str(pr), "-R", repo, "--json", "title,body"])
            if view.returncode != 0:
                return view.returncode, "", view.stderr.strip()
            info = json.loads(view.stdout or "{}")
            squash_subject = squash_subject or info.get("title") or f"fix: land #{pr}"
            if squash_body is None:
                pr_lines = (info.get("body") or "").strip().splitlines()
                squash_note = note or (pr_lines[0] if pr_lines else "")
                squash_body = (
                    f"{squash_note}\n\n{PUBLISH_TRAILER}" if squash_note else PUBLISH_TRAILER
                )
        cmd += ["--squash", "--subject", squash_subject, "--body", squash_body]
    else:
        cmd.append("--merge")
    if admin:
        cmd.append("--admin")
    if delete_branch:
        cmd.append("--delete-branch")
    if match_head_commit:
        cmd += ["--match-head-commit", match_head_commit]

    proc = gh(cmd)
    text = (proc.stdout + proc.stderr).strip()
    if publish and proc.returncode == 0:
        text = f"{text}\nsquashed with the release trailer -- {repo} will publish from main"
    return proc.returncode, text, ""


def red_checks(rollup: object) -> list[str]:
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
        if conclusion in RED_CONCLUSIONS or state in {"failure", "error"}:
            red.append(str(entry.get("name") or entry.get("context") or "?"))
    return red


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

        red = red_checks(data.get("statusCheckRollup") or [])
        if red:
            raise FleetError(
                f"PR #{pr} check(s) went RED: {', '.join(red)} -- gh pr checks {pr} -R {slug}"
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
        say(f"[dry-run] would branch {branch}, commit '{subject}', open a PR on {slug},")
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
    pushed = run(["git", "push", "--set-upstream", "origin", branch], cwd=repo, check=False)
    if pushed.returncode != 0:
        # A leftover branch from an interrupted run is the only expected
        # rejection here, and it is ours to replace. --force-with-lease still
        # refuses if someone else moved it.
        say(f"{branch} already exists on the remote -- replacing our own branch")
        git("fetch", "origin", branch, "--quiet", cwd=repo, check=False)
        git("push", "--force-with-lease", "--set-upstream", "origin", branch, cwd=repo)

    pr = open_pr_number(slug, branch)
    if pr is None:
        say("opening the PR")
        rc, created, err = open_pr(slug, branch, "main", subject, body=note)
        if rc:
            raise FleetError(f"cannot open a PR from {branch} on {slug}: {err}")
        # gh prints the new PR's URL, which beats re-listing: the list endpoint
        # is eventually consistent and can miss a PR opened a second ago.
        pr = pr_number_from_url(created) or open_pr_number(slug, branch)
        if pr is None:
            raise FleetError(f"opened a PR from {branch} but cannot find it on {slug}")
        say(f"opened PR #{pr}")
    else:
        say(f"reusing the open PR #{pr} for {branch}")

    await_pr_mergeable(slug, pr)

    say(f"squash-merging PR #{pr} (the squash message carries the release trailer)")
    rc, merged_out, err = merge_pr(
        slug,
        pr,
        publish=True,
        delete_branch=True,
        subject=subject,
        body=body,
        # Refuse to merge anything other than the commit we just validated.
        match_head_commit=branch_sha,
    )
    if rc:
        raise FleetError(f"cannot squash-merge PR #{pr} on {slug}: {err or merged_out}")
    say(merged_out)

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


def already_released(repo: Path, live: str) -> str | None:
    """The tag on HEAD when HEAD is already published, else None.

    Re-running a ship must be idempotent. Dispatching anyway fires a duplicate
    release that dies at plan time, which burns a run and leaves a red mark in
    the history for nothing.
    """
    proc = run(["git", "describe", "--tags", "--exact-match", "HEAD"], cwd=repo, check=False)
    found = proc.stdout.strip()
    if found and live and found.lstrip("v") == live:
        return found
    return None
