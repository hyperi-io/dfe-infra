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

Opening a PR, squash-merging it with the release trailer and waiting on the run
are the same operations `scripts/dfe-suite` needs, so both drive them from
`scripts/suite/landing.py`. This file owns the GHCR half: the repo allowlist,
the release tag, the digest lookup, and the `ship` chain that ties them
together.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

# Sibling modules, imported by path so this runs from any cwd.
sys.path.insert(0, str(SCRIPTS))
import envfile  # noqa: E402
import registry_pins  # noqa: E402

from suite.landing import (  # noqa: E402
    DEFAULT_INTERVAL,
    DEFAULT_TIMEOUT,
    DEFAULT_WORKFLOW,
    PUBLISH_TRAILER,
    active_main_run,
    latest_run,
    merge_pr,
    open_pr,
    open_pr_number,
    wait_for_run,
)
from suite.proc import gh  # noqa: E402

ORG = "hyperi-io"

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


def cmd_open(a: argparse.Namespace) -> int:
    rc, out, err = open_pr(
        _repo(a.repo), a.head, a.base, a.title, body_file=a.body_file
    )
    if rc:
        print(err, file=sys.stderr)
        return rc
    print(out)
    return 0


def cmd_merge(a: argparse.Namespace) -> int:
    rc, out, err = merge_pr(
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
    proc = gh(
        ["workflow", "run", a.workflow, "-R", repo, "-f", "from-head=true", "--ref", a.ref]
    )
    print((proc.stdout + proc.stderr).strip() or f"dispatched {a.workflow} on {repo}@{a.ref}")
    return proc.returncode


def cmd_wait(a: argparse.Namespace) -> int:
    rc, _ = wait_for_run(_repo(a.repo), a.workflow, a.interval, a.timeout)
    return rc


def _latest_tag(repo: str) -> tuple[int, str, str]:
    """The newest published release tag. Returns (exit code, tag, error)."""
    proc = gh(["api", f"/repos/{repo}/releases/latest", "--jq", ".tag_name"])
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

    pr = open_pr_number(repo, a.branch)
    if pr is None:
        if not a.body_file:
            print(
                f"no open PR for {a.branch} and no --body-file to open one",
                file=sys.stderr,
            )
            return 2
        rc, out, err = open_pr(repo, a.branch, "main", a.title, body_file=a.body_file)
        if rc:
            print(err, file=sys.stderr)
            return rc
        print(f"opened {out}", file=sys.stderr)
        pr = open_pr_number(repo, a.branch)
        if pr is None:
            print(f"no open PR for {a.branch} after opening one", file=sys.stderr)
            return 1
    else:
        print(f"reusing open PR #{pr} for {a.branch}", file=sys.stderr)

    active, err = active_main_run(repo, a.workflow)
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

    before = latest_run(repo, a.workflow)
    before_id = before.get("databaseId") if before else None

    rc, out, err = merge_pr(repo, pr, publish=a.publish, subject=a.title)
    if rc:
        print(err or out, file=sys.stderr)
        return rc
    print(out, file=sys.stderr)

    rc, _ = wait_for_run(
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
