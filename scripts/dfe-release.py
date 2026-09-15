#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/dfe-release.py
#  Purpose:      Scoped GHCR release driver -- open/merge a PR, dispatch the CI
#                release, wait for it, and resolve the published image digest.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Drive a dfe-* app release through the GHCR CI path, wrapping the `gh` calls in
one process so an unattended run does not stop on a per-command permission prompt.

Deliberately NOT open slather: `--repo` is checked against a fixed allowlist of
hyperi-io DFE app repos, and the only actions are PR open/merge, the CI release
dispatch, and read-only status/digest lookups. Nothing here can touch another org,
delete a repo, or change settings.

Subcommands (each is one `gh` orchestration):
  open     Create the PR for a pushed branch (prints the PR number).
  merge    Merge a PR to main (--merge, preserving the typed commits for
           semantic-release; --publish squash-merges with the release trailer so the
           merge itself ships; --admin to bypass required checks on an authorised run).
  dispatch Trigger the CI workflow with from-head=true (runs semantic-release +
           the GHCR image build/publish).
  wait     Poll the latest CI run to completion; prints its conclusion. Long-running
           -- run it backgrounded.
  latest   Print the newest published release tag.
  digest   Resolve the sha256 digest GHCR published for a version tag.
  ship     open -> merge -> wait -> latest -> digest as one verb, printing a single
           `SHIPPED <repo> <tag> <digest>` line. Long-running -- run it backgrounded.

The GHCR reads need a token with `read:packages`, which a developer's own
`gh login` usually lacks. Pass `--env-file PATH` and the GHCR_TOKEN (or GH_TOKEN)
in that file authenticates gh for this process:

    python3 scripts/dfe-release.py --env-file bootstrap/.env digest \\
        --repo dfe-loader --version v1.18.21
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# Sibling modules, imported by path so this runs from any cwd.
sys.path.insert(0, str(SCRIPTS))
import envfile  # noqa: E402
import registry_pins  # noqa: E402

ORG = "hyperi-io"

# Shared across wait/ship so the two never drift apart.
DEFAULT_WORKFLOW = "CI"
DEFAULT_INTERVAL = 20
DEFAULT_TIMEOUT = 1800

# The release workflow greps a main-branch push message for this exact trailer.
PUBLISH_TRAILER = "Publish: true"

# Fixed allowlist -- the DFE app repos this tool may release. Not open slather.
ALLOWED_REPOS = {
    "dfe-engine",
    "dfe-ui",
    "dfe-hyperdx",
    "dfe-receiver",
    "dfe-loader",
    "dfe-archiver",
    "dfe-fetcher",
    "dfe-transform-vrl",
    "dfe-transform-vector",
}


def _repo(name: str) -> str:
    if name not in ALLOWED_REPOS:
        print(f"repo not in the DFE allowlist: {name}", file=sys.stderr)
        raise SystemExit(2)
    return f"{ORG}/{name}"


def _gh(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def _open_pr(
    repo: str, head: str, base: str, title: str, body_file: str
) -> tuple[int, str, str]:
    """Create the PR for a branch, falling back to the one already open for it.

    Returns:
        (exit code, stdout text, stderr text).
    """
    cmd = [
        "pr",
        "create",
        "-R",
        repo,
        "--head",
        head,
        "--base",
        base,
        "--title",
        title,
        "--body-file",
        body_file,
    ]
    proc = _gh(cmd, check=False)
    if proc.returncode != 0:
        # Already open? report the existing PR url/number.
        existing = _gh(["pr", "view", head, "-R", repo, "--json", "number,url"], check=False)
        if existing.returncode == 0:
            return 0, existing.stdout.strip(), ""
        return 1, "", proc.stderr.strip()
    return 0, proc.stdout.strip(), ""


def cmd_open(a: argparse.Namespace) -> int:
    rc, out, err = _open_pr(_repo(a.repo), a.head, a.base, a.title, a.body_file)
    if rc:
        print(err, file=sys.stderr)
        return rc
    print(out)
    return 0


def _open_pr_number(repo: str, head: str) -> int | None:
    """The number of the OPEN PR for a branch, or None.

    State is checked because `gh pr view <branch>` also resolves a merged or
    closed PR for that branch, and merging one of those again is not a no-op.
    """
    proc = _gh(["pr", "view", head, "-R", repo, "--json", "number,state"], check=False)
    if proc.returncode != 0:
        return None
    data = json.loads(proc.stdout or "{}")
    if data.get("state") != "OPEN":
        return None
    return data.get("number")


def _merge_pr(
    repo: str,
    pr: int,
    *,
    publish: bool,
    admin: bool = False,
    delete_branch: bool = False,
    subject: str | None = None,
    note: str | None = None,
) -> tuple[int, str, str]:
    """Merge a PR to main; publish squash-merges so the merge itself ships.

    The publish workflow only fires on a `refs/heads/main` push whose message
    carries `Publish: true`, and a squash merge replaces the branch commits with
    the message given here -- so the trailer belongs on the squash message, not
    on any commit in the branch. One landing instead of merge-then-dispatch.

    Returns:
        (exit code, stdout text, stderr text).
    """
    cmd = ["pr", "merge", str(pr), "-R", repo]
    if publish:
        view = _gh(["pr", "view", str(pr), "-R", repo, "--json", "title,body"], check=False)
        if view.returncode != 0:
            return view.returncode, "", view.stderr.strip()
        info = json.loads(view.stdout or "{}")
        squash_subject = subject or info.get("title") or f"fix: land #{pr}"
        pr_lines = (info.get("body") or "").strip().splitlines()
        squash_note = note or (pr_lines[0] if pr_lines else "")
        body = f"{squash_note}\n\n{PUBLISH_TRAILER}" if squash_note else PUBLISH_TRAILER
        cmd += ["--squash", "--subject", squash_subject, "--body", body]
    else:
        cmd.append("--merge")
    if admin:
        cmd.append("--admin")
    if delete_branch:
        cmd.append("--delete-branch")

    proc = _gh(cmd, check=False)
    out = (proc.stdout + proc.stderr).strip()
    if publish and proc.returncode == 0:
        out = f"{out}\nsquashed with the release trailer -- {repo} will publish from main"
    return proc.returncode, out, ""


def cmd_merge(a: argparse.Namespace) -> int:
    rc, out, err = _merge_pr(
        _repo(a.repo),
        a.pr,
        publish=a.publish,
        admin=a.admin,
        delete_branch=a.delete_branch,
        subject=a.subject,
        note=a.note,
    )
    if err:
        print(err, file=sys.stderr)
        return rc
    print(out)
    return rc


def cmd_dispatch(a: argparse.Namespace) -> int:
    repo = _repo(a.repo)
    proc = _gh(
        ["workflow", "run", a.workflow, "-R", repo, "-f", "from-head=true", "--ref", a.ref],
        check=False,
    )
    print((proc.stdout + proc.stderr).strip() or f"dispatched {a.workflow} on {repo}@{a.ref}")
    return proc.returncode


def _latest_run(repo: str, workflow: str) -> dict | None:
    proc = _gh(
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
        ],
        check=False,
    )
    if proc.returncode != 0:
        return None
    rows = json.loads(proc.stdout or "[]")
    return rows[0] if rows else None


def _active_main_run(repo: str, workflow: str) -> tuple[dict | None, str]:
    """A queued or in-progress run of the workflow on main, if there is one.

    Returns:
        (the run record or None, an error message if the state is unknown).
    """
    for status in ("in_progress", "queued"):
        proc = _gh(
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
            ],
            check=False,
        )
        if proc.returncode != 0:
            return None, proc.stderr.strip() or f"gh run list failed for {repo}"
        rows = json.loads(proc.stdout or "[]")
        if rows:
            return rows[0], ""
    return None, ""


def _wait_for_run(
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
        run = _latest_run(repo, workflow)
        if run is None:
            print("no runs found yet", file=err)
        elif after_id is not None and run.get("databaseId") == after_id:
            print(f"waiting for the run {workflow} will start ({waited}s)", file=err)
        else:
            status = run.get("status")
            if status == "completed":
                concl = run.get("conclusion")
                print(f"{workflow} {concl} ({run.get('url')})", file=out)
                return (0 if concl == "success" else 1), run
            print(f"{workflow} {status}... ({waited}s) {run.get('url')}", file=out)
        if waited >= timeout:
            print(f"timed out after {timeout}s waiting for {workflow}", file=err)
            return 2, None
        time.sleep(interval)
        waited += interval


def cmd_wait(a: argparse.Namespace) -> int:
    rc, _ = _wait_for_run(_repo(a.repo), a.workflow, a.interval, a.timeout)
    return rc


def _latest_tag(repo: str) -> tuple[int, str, str]:
    """The newest published release tag. Returns (exit code, tag, error)."""
    proc = _gh(
        ["api", f"/repos/{repo}/releases/latest", "--jq", ".tag_name"],
        check=False,
    )
    if proc.returncode != 0:
        return 1, "", proc.stderr.strip()
    return 0, proc.stdout.strip(), ""


def cmd_latest(a: argparse.Namespace) -> int:
    rc, tag, err = _latest_tag(_repo(a.repo))
    if rc:
        print(err, file=sys.stderr)
        return 1
    print(tag)
    return 0


def _digest_for_tag(app: str, version: str) -> tuple[int, str, str]:
    """The sha256 digest GHCR published for a version tag.

    Resolved through registry_pins so this and the pin tooling read the packages
    API one way: `gh api --paginate` concatenates one JSON array per page with no
    separator, which a single json.loads cannot decode past the first page.

    Returns:
        (exit code, digest, error).
    """
    try:
        tags = registry_pins.package_tags(ORG, app)
    except registry_pins.RegistryError as exc:
        return 1, "", str(exc)
    digest = tags.get(version)
    if not digest:
        return 1, "", f"version tag not found in GHCR: {version}"
    return 0, digest, ""


def cmd_digest(a: argparse.Namespace) -> int:
    if a.repo not in ALLOWED_REPOS:
        print(f"repo not in the DFE allowlist: {a.repo}", file=sys.stderr)
        return 2
    rc, digest, err = _digest_for_tag(a.repo, a.version)
    if rc:
        print(err, file=sys.stderr)
        return 1
    print(digest)  # the sha256:... digest
    return 0


def _title_problem(title: str) -> str | None:
    """Why this squash subject would not pass commit lint, or None if it would."""
    if len(title) >= 100:
        return f"title must be under 100 characters (got {len(title)})"
    if not title[:1].islower():
        return f"title must start with a lowercase character: {title!r}"
    return None


def cmd_ship(a: argparse.Namespace) -> int:
    """Release one app end to end: open -> merge -> wait -> latest -> digest.

    Progress goes to stderr so stdout carries exactly one line on success:
    `SHIPPED <repo> <tag> <digest>`.

    STOPS with exit 3 when a run is already queued or in progress on main for
    the repo. The publish workflow's concurrency group is keyed on the branch, so
    merging now would cancel that run and its release would never land.

    A failed run is never rerun: `gh run rerun` re-executes the tagger, which can
    force-rewrite tags that already point at a published image.
    """
    repo = _repo(a.repo)
    problem = _title_problem(a.title)
    if problem:
        print(problem, file=sys.stderr)
        return 2

    pr = _open_pr_number(repo, a.branch)
    if pr is None:
        if not a.body_file:
            print(
                f"no open PR for {a.branch} and no --body-file to open one",
                file=sys.stderr,
            )
            return 2
        rc, out, err = _open_pr(repo, a.branch, "main", a.title, a.body_file)
        if rc:
            print(err, file=sys.stderr)
            return rc
        print(f"opened {out}", file=sys.stderr)
        pr = _open_pr_number(repo, a.branch)
        if pr is None:
            print(f"no open PR for {a.branch} after opening one", file=sys.stderr)
            return 1
    else:
        print(f"reusing open PR #{pr} for {a.branch}", file=sys.stderr)

    active, err = _active_main_run(repo, a.workflow)
    if err:
        print(err, file=sys.stderr)
        return 1
    if active:
        print(
            f"a publish is in flight on main for {a.repo}; merging now would "
            "cancel it (concurrency group) -- wait for it, then re-run",
            file=sys.stderr,
        )
        return 3

    before = _latest_run(repo, a.workflow)
    before_id = before.get("databaseId") if before else None

    rc, out, err = _merge_pr(repo, pr, publish=a.publish, subject=a.title)
    if rc:
        print(err or out, file=sys.stderr)
        return rc
    print(out, file=sys.stderr)

    rc, _ = _wait_for_run(
        repo, a.workflow, DEFAULT_INTERVAL, a.timeout,
        after_id=before_id, out=sys.stderr, err=sys.stderr,
    )
    if rc:
        return rc

    rc, tag, err = _latest_tag(repo)
    if rc:
        print(err, file=sys.stderr)
        return rc

    rc, digest, err = _digest_for_tag(a.repo, tag)
    if rc:
        print(err, file=sys.stderr)
        return rc

    print(f"SHIPPED {a.repo} {tag} {digest}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Scoped GHCR release driver for DFE app repos.")
    p.add_argument(
        "--env-file",
        action="append",
        metavar="PATH",
        help=(
            "KEY=VALUE file; GHCR_TOKEN or GH_TOKEN in it authenticates gh for "
            "the registry reads; repeatable, later wins"
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    po = sub.add_parser("open", help="create the PR for a pushed branch")
    po.add_argument("--repo", required=True)
    po.add_argument("--head", required=True)
    po.add_argument("--base", default="main")
    po.add_argument("--title", required=True)
    po.add_argument("--body-file", required=True)
    po.set_defaults(func=cmd_open)

    pm = sub.add_parser("merge", help="merge a PR to main")
    pm.add_argument("--repo", required=True)
    pm.add_argument("--pr", required=True, type=int)
    pm.add_argument("--admin", action="store_true")
    pm.add_argument("--delete-branch", action="store_true")
    pm.add_argument(
        "--publish",
        action="store_true",
        help="squash-merge with the Publish: true trailer -- the merge itself releases",
    )
    pm.add_argument("--subject", help="squash subject (default: the PR title)")
    pm.add_argument("--note", help="squash body line (default: the PR body's first line)")
    pm.set_defaults(func=cmd_merge)

    pd = sub.add_parser("dispatch", help="trigger the CI release workflow")
    pd.add_argument("--repo", required=True)
    pd.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    pd.add_argument("--ref", default="main")
    pd.set_defaults(func=cmd_dispatch)

    pw = sub.add_parser("wait", help="poll the latest CI run to completion")
    pw.add_argument("--repo", required=True)
    pw.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    pw.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    pw.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    pw.set_defaults(func=cmd_wait)

    pl = sub.add_parser("latest", help="print the newest published release tag")
    pl.add_argument("--repo", required=True)
    pl.set_defaults(func=cmd_latest)

    pg = sub.add_parser("digest", help="resolve the GHCR digest for a version tag")
    pg.add_argument("--repo", required=True)
    pg.add_argument("--version", required=True)
    pg.set_defaults(func=cmd_digest)

    ps = sub.add_parser(
        "ship",
        help="open -> merge -> wait -> latest -> digest, printing one SHIPPED line",
    )
    ps.add_argument("--repo", required=True, help="app repo name, e.g. dfe-loader")
    ps.add_argument("--branch", required=True, help="the pushed branch to land")
    ps.add_argument(
        "--title",
        required=True,
        help="squash subject; lowercase first character, under 100 characters",
    )
    ps.add_argument(
        "--body-file",
        help="PR body, used only when the branch has no open PR yet",
    )
    ps.add_argument(
        "--publish",
        action="store_true",
        help=f"squash with the `{PUBLISH_TRAILER}` trailer -- the merge itself releases",
    )
    ps.add_argument(
        "--workflow",
        default=DEFAULT_WORKFLOW,
        help=f"CI workflow to guard on and wait for (default {DEFAULT_WORKFLOW})",
    )
    ps.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"seconds to wait for the CI run (default {DEFAULT_TIMEOUT})",
    )
    ps.set_defaults(func=cmd_ship)

    a = p.parse_args()
    # Every gh call below goes through _gh/registry_pins, which inherit os.environ.
    envfile.apply_gh_token(a.env_file)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
