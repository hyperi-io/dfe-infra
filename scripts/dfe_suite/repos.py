#  Project:      dfe-infra
#  File:         scripts/dfe_suite/repos.py
#  Purpose:      Locate a suite member's checkout and drive git inside it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Finding a checkout, and the small git reads the suite helpers depend on.

No disk layout is hardcoded: the Mac, dragonfly and desktop-derek all park
their projects somewhere different, so a repo is resolved through an explicit
env var first and a short list of roots after that.
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
            Path("/Volumes/projects"),  # the Mac
            Path.home() / "projects",
            Path("/projects"),  # dragonfly and the Linux boxes
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


def repo_slug(repo: Path, org: str) -> str:
    """The ``owner/name`` slug for a checkout.

    Read from the origin remote where possible so a fork or a renamed
    directory is not silently addressed as the wrong repo.
    """
    proc = run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=repo,
        check=False,
    )
    slug = proc.stdout.strip()
    if proc.returncode == 0 and "/" in slug:
        return slug
    return f"{org}/{repo.name}"


def sync_main(repo: Path, *, dry_run: bool) -> None:
    """Get onto an up-to-date ``main``, preserving any working-tree WIP.

    The WIP is deliberately kept: the fleet folds existing uncommitted work
    into the release commit rather than stranding it (no SEP fields).
    """
    if dry_run:
        say("[dry-run] would fetch origin and fast-forward main")
        return
    git("fetch", "origin", "main", "--tags", "--quiet", cwd=repo)
    branch = current_branch(repo)
    if branch != "main":
        say(f"on '{branch}' -- switching to main (WIP carries across)")
        git("switch", "main", cwd=repo)
    # --autostash so uncommitted work survives the rebase and comes back.
    git("pull", "--rebase", "--autostash", "origin", "main", cwd=repo)
