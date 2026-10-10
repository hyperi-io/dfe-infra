#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/propagate_stack_pin.py
#  Purpose:      Carry a versions.yaml `current` move into the consuming repo's
#                stack dial as a pull request, so the two pins cannot drift by
#                nobody noticing.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Open the pull request that moves a consumer's stack pin onto `current`.

    python3 scripts/propagate_stack_pin.py                 # report only
    python3 scripts/propagate_stack_pin.py --open-pr       # and open it

versions.yaml is the SSoT for BOTH deploy targets. dfe-infra self-updates
through GitOps, but dfe-docker holds one committed dial -- `version.pin`, which
`make dial` writes to DFE_STACK_VERSION -- and nothing moves it, so it sits on
whatever stack was current when somebody last edited it by hand (dfe-infra#116).

The pins that matter are resolved from the signed OCI manifest at that version,
so this moves ONE number and nothing else; the manifest is still the authority
for what that number means. Config differences between the docker and k8s
topologies are deliberate and are NOT synced here.

Without `--open-pr` it prints what it would do and touches nothing, which is
what a check run wants. With it, `gh` needs contents:write and pull-requests:
write on the target, and a caller with neither gets a refusal naming the scope
rather than a silent no-op.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_FILE = REPO_ROOT / "versions.yaml"

DEFAULT_ORG = "hyperi-io"
DEFAULT_REPO = "dfe-docker"
DEFAULT_PATH = "deployment.example.yaml"
BRANCH = "stack-pin/current"

# The `version:` block, then the `pin:` inside it: other blocks in the dial
# carry a `pin:` too, so an unanchored match moves the wrong key.
_VERSION_BLOCK = re.compile(r"(?m)^version:[^\S\n]*\n(?P<body>(?:[ \t#].*\n|\n)*)")
_PIN = re.compile(r"(?m)^(?P<lead>[ \t]+pin:[^\S\n]*)(?P<value>\S+)(?P<rest>[^\n]*)$")


class PropagateError(RuntimeError):
    """A gh/API failure -- carries the underlying stderr."""


def current_stack(text: str) -> str:
    found = re.search(r'^current:\s*"([^"]+)"', text, re.MULTILINE)
    if not found:
        raise PropagateError("versions.yaml has no `current:` pointer to propagate")
    return found.group(1)


def _version_pin(dial_text: str) -> re.Match | None:
    """The `pin:` line inside the dial's `version:` block, if it has one."""
    block = _VERSION_BLOCK.search(dial_text)
    if not block:
        return None
    found = _PIN.search(block.group("body"))
    if not found:
        return None
    # Re-match at the whole-file offset so a caller can splice by index.
    return _PIN.search(dial_text, block.start("body"), block.end("body"))


def pinned_in(dial_text: str) -> str | None:
    """The stack version the consumer's dial holds, or None when it tracks latest."""
    found = _version_pin(dial_text)
    return found.group("value") if found else None


def rewrite(dial_text: str, version: str) -> str:
    """The dial with its `pin:` moved, leaving every other byte alone."""
    found = _version_pin(dial_text)
    if not found:
        return dial_text
    return (
        dial_text[: found.start()]
        + f"{found.group('lead')}{version}{found.group('rest')}"
        + dial_text[found.end():]
    )


def _gh(args: list[str]) -> str:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    if result.returncode != 0:
        raise PropagateError(result.stderr.strip() or f"gh {' '.join(args)} failed")
    return result.stdout


def read_remote(org: str, repo: str, path: str, ref: str) -> str:
    return _gh(["api", f"/repos/{org}/{repo}/contents/{path}?ref={ref}",
                "--header", "Accept: application/vnd.github.raw"])


def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


def tag_exists(tag: str) -> bool:
    """Whether *tag* is cut in this repo -- the consumer pulls that tag's stack manifest.

    Asked through git rather than the API: the PR token is scoped to the
    consumer alone and cannot see this repo, while the checkout's credentials can.
    """
    result = _git(["ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{tag}"])
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    raise PropagateError(result.stderr.strip() or f"git ls-remote for tag {tag} failed")


def open_pr(org: str, repo: str, path: str, base: str, text: str, version: str) -> str | None:
    """Land the rewritten dial on a branch and open (or update) the PR.

    Returns None when the commit left the branch identical to *base*, since a
    PR with no diff only waits to be closed.
    """
    head = _gh(["api", f"/repos/{org}/{repo}/commits/{base}", "--jq", ".sha"]).strip()
    try:
        _gh(["api", "--method", "POST", f"/repos/{org}/{repo}/git/refs",
             "-f", f"ref=refs/heads/{BRANCH}", "-f", f"sha={head}"])
    except PropagateError as exc:
        # The branch survives a closed PR, so an existing one is reused rather
        # than opening a second PR for the same dial.
        if "already exists" not in str(exc).lower():
            raise
        _gh(["api", "--method", "PATCH", f"/repos/{org}/{repo}/git/refs/heads/{BRANCH}",
             "-f", f"sha={head}", "-F", "force=true"])

    import base64

    existing = json.loads(_gh(["api", f"/repos/{org}/{repo}/contents/{path}?ref={BRANCH}"]))
    _gh(["api", "--method", "PUT", f"/repos/{org}/{repo}/contents/{path}",
         "-f", f"branch={BRANCH}",
         "-f", f"message=fix(stack): pin the stack dial at {version}",
         "-f", f"sha={existing['sha']}",
         "-f", f"content={base64.b64encode(text.encode()).decode()}"])

    changed = int(_gh(["api", f"/repos/{org}/{repo}/compare/{base}...{BRANCH}",
                       "--jq", ".files | length"]).strip() or "0")
    if changed == 0:
        return None

    body = (
        f"dfe-infra moved `current` to {version}, so this moves the dial the "
        f"compose stack resolves its pins from.\n\n"
        f"Only the one number changes. Everything it selects still comes out of "
        f"the signed stack manifest at that version, so nothing here is a second "
        f"copy of a pin. Config that differs between the docker and k8s "
        f"topologies is deliberate and is not touched.\n\n"
        f"Opened by dfe-infra's `propagate_stack_pin.py` (dfe-infra#116)."
    )
    open_prs = json.loads(_gh(["api", f"/repos/{org}/{repo}/pulls?head={org}:{BRANCH}&state=open"]))
    if open_prs:
        return open_prs[0]["html_url"]
    created = json.loads(_gh([
        "api", "--method", "POST", f"/repos/{org}/{repo}/pulls",
        "-f", f"title=fix(stack): move the stack dial to {version}",
        "-f", f"head={BRANCH}", "-f", f"base={base}", "-f", f"body={body}",
    ]))
    return created["html_url"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--org", default=DEFAULT_ORG)
    parser.add_argument("--repo", default=DEFAULT_REPO, help="the consuming repo")
    parser.add_argument("--path", default=DEFAULT_PATH, help="the dial file inside it")
    parser.add_argument("--base", default="main", help="the branch to open against")
    parser.add_argument("--open-pr", action="store_true", help="actually open it")
    args = parser.parse_args(argv)

    version = current_stack(VERSIONS_FILE.read_text(encoding="utf-8"))
    target = f"{args.org}/{args.repo} {args.path}"
    try:
        dial = read_remote(args.org, args.repo, args.path, args.base)
    except PropagateError as exc:
        print(f"FAIL -- cannot read {target}: {exc}", file=sys.stderr)
        return 1

    pinned = pinned_in(dial)
    if pinned is None:
        print(f"OK -- {target} carries no `pin:` (it tracks latest); nothing to move")
        return 0
    if pinned == version:
        print(f"OK -- {target} already pins {version}")
        return 0

    print(f"{target}: pins {pinned}, dfe-infra current is {version}")
    if not args.open_pr:
        print("report only -- pass --open-pr to raise the bump")
        return 0

    try:
        if not tag_exists(version):
            print(f"SKIP -- {version} is not cut yet (no tag on origin), "
                  f"and the consumer pulls that tag's stack manifest; no PR opened")
            return 0
        url = open_pr(args.org, args.repo, args.path, args.base, rewrite(dial, version), version)
        if url is None:
            print(f"SKIP -- the bump left {BRANCH} identical to {args.base}; no PR opened")
            return 0
        print(f"PR: {url}")
    except PropagateError as exc:
        print(
            f"FAIL -- could not open the PR: {exc}\n"
            f"The token needs contents:write and pull-requests:write on "
            f"{args.org}/{args.repo}.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
