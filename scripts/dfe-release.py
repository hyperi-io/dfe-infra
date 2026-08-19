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
           semantic-release; --admin to bypass required checks on an authorised run).
  dispatch Trigger the CI workflow with from-head=true (runs semantic-release +
           the GHCR image build/publish).
  wait     Poll the latest CI run to completion; prints its conclusion. Long-running
           -- run it backgrounded.
  digest   Resolve the sha256 digest GHCR published for a version tag.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

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


def _gh(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def cmd_open(a: argparse.Namespace) -> int:
    repo = _repo(a.repo)
    cmd = [
        "pr", "create", "-R", repo, "--head", a.head, "--base", a.base,
        "--title", a.title, "--body-file", a.body_file,
    ]
    proc = _gh(cmd, check=False)
    if proc.returncode != 0:
        # Already open? print the existing PR url/number.
        existing = _gh(["pr", "view", a.head, "-R", repo, "--json", "number,url"], check=False)
        if existing.returncode == 0:
            print(existing.stdout.strip())
            return 0
        print(proc.stderr.strip(), file=sys.stderr)
        return 1
    print(proc.stdout.strip())
    return 0


def cmd_merge(a: argparse.Namespace) -> int:
    repo = _repo(a.repo)
    cmd = ["pr", "merge", str(a.pr), "-R", repo, "--merge"]
    if a.admin:
        cmd.append("--admin")
    if a.delete_branch:
        cmd.append("--delete-branch")
    proc = _gh(cmd, check=False)
    print((proc.stdout + proc.stderr).strip())
    return proc.returncode


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
        ["run", "list", "-R", repo, "-w", workflow, "--limit", "1",
         "--json", "databaseId,status,conclusion,headBranch,createdAt,url"],
        check=False,
    )
    if proc.returncode != 0:
        return None
    rows = json.loads(proc.stdout or "[]")
    return rows[0] if rows else None


def cmd_wait(a: argparse.Namespace) -> int:
    repo = _repo(a.repo)
    deadline = a.timeout
    waited = 0
    while True:
        run = _latest_run(repo, a.workflow)
        if run is None:
            print("no runs found yet", file=sys.stderr)
        else:
            status = run.get("status")
            if status == "completed":
                concl = run.get("conclusion")
                print(f"{a.workflow} {concl} ({run.get('url')})")
                return 0 if concl == "success" else 1
            print(f"{a.workflow} {status}... ({waited}s) {run.get('url')}")
        if waited >= deadline:
            print(f"timed out after {deadline}s waiting for {a.workflow}", file=sys.stderr)
            return 2
        time.sleep(a.interval)
        waited += a.interval


def cmd_latest(a: argparse.Namespace) -> int:
    repo = _repo(a.repo)
    proc = _gh(
        ["api", f"/repos/{repo}/releases/latest", "--jq", ".tag_name"],
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        return 1
    print(proc.stdout.strip())
    return 0


def cmd_digest(a: argparse.Namespace) -> int:
    if a.repo not in ALLOWED_REPOS:
        print(f"repo not in the DFE allowlist: {a.repo}", file=sys.stderr)
        return 2
    proc = _gh(
        ["api", f"/orgs/{ORG}/packages/container/{a.repo}/versions", "--paginate"],
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        return 1
    versions = json.loads(proc.stdout or "[]")
    for v in versions:
        tags = v.get("metadata", {}).get("container", {}).get("tags", [])
        if a.version in tags:
            print(v["name"])  # the sha256:... digest
            return 0
    print(f"version tag not found in GHCR: {a.version}", file=sys.stderr)
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description="Scoped GHCR release driver for DFE app repos.")
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
    pm.set_defaults(func=cmd_merge)

    pd = sub.add_parser("dispatch", help="trigger the CI release workflow")
    pd.add_argument("--repo", required=True)
    pd.add_argument("--workflow", default="CI")
    pd.add_argument("--ref", default="main")
    pd.set_defaults(func=cmd_dispatch)

    pw = sub.add_parser("wait", help="poll the latest CI run to completion")
    pw.add_argument("--repo", required=True)
    pw.add_argument("--workflow", default="CI")
    pw.add_argument("--interval", type=int, default=20)
    pw.add_argument("--timeout", type=int, default=1800)
    pw.set_defaults(func=cmd_wait)

    pl = sub.add_parser("latest", help="print the newest published release tag")
    pl.add_argument("--repo", required=True)
    pl.set_defaults(func=cmd_latest)

    pg = sub.add_parser("digest", help="resolve the GHCR digest for a version tag")
    pg.add_argument("--repo", required=True)
    pg.add_argument("--version", required=True)
    pg.set_defaults(func=cmd_digest)

    a = p.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
