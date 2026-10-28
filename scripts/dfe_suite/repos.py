#  Project:      dfe-infra
#  File:         scripts/dfe_suite/repos.py
#  Purpose:      Locate a suite member's checkout and drive git inside it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Finding a checkout, and the small git reads the suite helpers depend on.

No disk layout is hardcoded: macOS and Linux hosts park their projects in
different places, so a repo is resolved through an explicit env var first
and a short list of roots after that.
"""

from __future__ import annotations

import os
from pathlib import Path

from dfe_suite.proc import FleetError, run, say


def _candidate_roots() -> list[Path]:
    """Projects roots to search, most specific first."""
    roots: list[Path] = []
    explicit = os.environ.get("HYPERI_PROJECTS_ROOT")
    if explicit:
        roots.append(Path(explicit))
    roots.extend(
        [
            Path("/Volumes/projects"),  # macOS
            Path.home() / "projects",
            Path("/projects"),  # the Linux hosts
        ]
    )
    return roots


def find_repo(name: str) -> Path:
    """Resolve a repo checkout by name across macOS and Linux layouts.

    Order: an explicit ``<NAME>_DIR`` env var (``SCALO_RS_DIR``,
    ``SCALO_PY_DIR``, ...), then ``$HYPERI_PROJECTS_ROOT``, then the usual
    roots. A candidate only counts if it actually holds a git checkout, so a
    stale empty directory does not shadow the real one.

    Args:
        name: Repo directory name, e.g. ``scalo-rs``.

    Returns:
        The checkout path.

    Raises:
        FleetError: If nothing matched, listing what was tried.
    """
    env_var = f"{name.upper().replace('-', '_')}_DIR"
    override = os.environ.get(env_var)
    if override:
        path = Path(override).expanduser()
        if not (path / ".git").exists():
            raise FleetError(f"{env_var}={override} is not a git checkout")
        return path

    tried = []
    for root in _candidate_roots():
        candidate = root / name
        tried.append(str(candidate))
        if (candidate / ".git").exists():
            return candidate
    raise FleetError(
        f"cannot find the {name} checkout. Set {env_var}=<path>, or "
        f"HYPERI_PROJECTS_ROOT=<root>. Tried: {', '.join(tried)}"
    )


def git(*args: str, cwd: Path, check: bool = True) -> str:
    """Run a git command in ``cwd`` and return its stripped stdout."""
    return run(["git", *args], cwd=cwd, check=check).stdout.strip()


def head_sha(repo: Path) -> str:
    """The current HEAD commit sha."""
    return git("rev-parse", "HEAD", cwd=repo)


def current_branch(repo: Path) -> str:
    """The checked-out branch name."""
    return git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)


def has_staged_changes(repo: Path) -> bool:
    """True when the index differs from HEAD."""
    return (
        run(["git", "diff", "--cached", "--quiet"], cwd=repo, check=False).returncode
        != 0
    )


def _origin_name(repo: Path) -> str | None:
    """The repo name in the origin remote's URL, which every worktree of a repo shares."""
    proc = run(["git", "remote", "get-url", "origin"], cwd=repo, check=False)
    tail = proc.stdout.strip().rstrip("/").removesuffix(".git").replace(":", "/").rsplit("/", 1)[-1]
    return tail if proc.returncode == 0 and tail else None


def repo_slug(repo: Path, org: str) -> str:
    """The ``owner/name`` slug for a checkout.

    Read from the origin remote where possible so a fork, a renamed directory or
    a worktree called anything at all is not silently addressed as the wrong repo.
    """
    proc = run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=repo,
        check=False,
    )
    slug = proc.stdout.strip()
    if proc.returncode == 0 and "/" in slug:
        return slug
    return f"{org}/{_origin_name(repo) or repo.name}"


def is_linked_worktree(repo: Path) -> bool:
    """True when ``repo`` is a linked worktree rather than the repo's main checkout."""
    git_dir = Path(git("rev-parse", "--absolute-git-dir", cwd=repo)).resolve()
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=repo)).resolve()
    return git_dir != common


def worktree_holding(repo: Path, branch: str) -> Path | None:
    """Another worktree of this repo that has ``branch`` checked out, or None.

    Git refuses to check a branch out twice, so ``git switch`` from any other
    worktree fails with its own one-line error that names neither the fix nor
    which checkout to use.

    Args:
        repo: A checkout of the repo.
        branch: The short branch name, e.g. ``main``.

    Returns:
        The holding worktree's path, or None when no other worktree holds it.
    """
    here = Path(git("rev-parse", "--show-toplevel", cwd=repo)).resolve()
    listing = git("worktree", "list", "--porcelain", cwd=repo)
    path: Path | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line == f"branch refs/heads/{branch}" and path is not None:
            if path.resolve() != here:
                return path
    return None


def sync_main(repo: Path, *, dry_run: bool) -> None:
    """Get onto an up-to-date ``main``, preserving any working-tree WIP.

    The WIP is deliberately kept: the fleet folds existing uncommitted work
    into the release commit rather than stranding it (no SEP fields).

    A linked worktree that is not already on ``main`` is left on a detached HEAD
    at ``origin/main`` instead, so the repo's main checkout keeps ``main`` and
    the worktree needs no branch of that name.

    Raises:
        FleetError: If the repo's main checkout has to switch to ``main`` but
            another worktree holds it, naming that worktree. Checked on a dry
            run too, so the dry run cannot pass a checkout the real run would
            fail on.
    """
    detach = is_linked_worktree(repo) and current_branch(repo) != "main"
    if not detach:
        holder = worktree_holding(repo, "main")
        if holder is not None:
            raise FleetError(
                f"main is checked out in the worktree at {holder}, so {repo} cannot "
                f"switch to it. Run the tool against {holder} instead."
            )
    if dry_run:
        say(
            "[dry-run] would fetch origin and detach at origin/main"
            if detach
            else "[dry-run] would fetch origin and fast-forward main"
        )
        return
    git("fetch", "origin", "main", "--tags", "--quiet", cwd=repo)
    if detach:
        say("linked worktree -- detaching at origin/main (WIP carries across)")
        git("switch", "--detach", "origin/main", cwd=repo)
        return
    branch = current_branch(repo)
    if branch != "main":
        say(f"on '{branch}' -- switching to main (WIP carries across)")
        git("switch", "main", cwd=repo)
    # --autostash so uncommitted work survives the rebase and comes back.
    git("pull", "--rebase", "--autostash", "origin", "main", cwd=repo)


def return_to_main(repo: Path) -> None:
    """After a merge, leave ``repo`` on the remote's ``main``: the branch in a main checkout, a detached HEAD in a linked worktree."""
    git("fetch", "origin", "main", "--tags", "--quiet", cwd=repo)
    if is_linked_worktree(repo):
        git("switch", "--detach", "origin/main", cwd=repo)
        return
    git("switch", "main", cwd=repo, check=False)
    git("merge", "--ff-only", "origin/main", cwd=repo)
