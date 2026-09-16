#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         registry_pins.py
#  Purpose:      The ONE tag -> digest resolver for GHCR container packages.
#                Factored out of check_image_pins.py so the freshness check and
#                the resolve_pins writer share a single definition of "what
#                digest does this org/app/tag point at right now, and when was
#                it published". GENERIC: (org, app, tag) in, digest out -- no
#                versions.yaml layout knowledge, so dfe-deploy can call it the
#                same way dfe-infra does.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Registry pin resolution over the GitHub Packages (GHCR) container API.

The pain this kills: pinning a version means looking up its sha by hand and
pasting it in, in two repos. This is the shared core both the checker and the
writer sit on, so there is exactly one place that knows how to turn a tag into a
digest.

Everything here is decoupled from any pin FILE. The public surface is:

  package_versions(org, app)   -> the raw GHCR version records (paginated)
  package_tags(org, app)       -> {tag: digest} for every tagged version
  resolve(org, app, tag)       -> a Resolved(digest, published) or None
  resolve_digest(org, app, tag)-> just the digest string, or None
  version_key(tag)             -> numeric sort key for vX.Y.Z tags
  head_commit(org, repo, ref)  -> the commit sha a branch or tag points at

A content repo that ships no container still has to be pinned immutably, so
head_commit is the same job as resolve_digest for a repo rather than a package:
a movable name in, an immutable id out.

dfe-deploy usage (an overlay pins image.tag; resolve its digest for a pull-by-
digest ref) is a two-liner:

    from registry_pins import resolve_digest
    digest = resolve_digest("hyperi-io", "dfe-loader", "v1.18.21")
    # -> "sha256:..."; write f"{tag}@{digest}" into the overlay's image field.

Needs an authenticated `gh` (the packages API wants read:packages). No third-
party deps -- gh + stdlib only.
"""

from __future__ import annotations

import datetime
import json
import re
import subprocess
from dataclasses import dataclass
from functools import cache


@dataclass(frozen=True)
class Resolved:
    """A tag's registry state: the immutable digest and when it was published.

    `published` is the package version's created_at (UTC). It is what the
    supply-chain cooldown reads -- a digest whose image is younger than the
    cooldown window is not adopted without an explicit override.
    """

    digest: str
    published: datetime.datetime | None


class RegistryError(RuntimeError):
    """A gh/API failure resolving a package -- carries the underlying stderr."""


def _gh_api(path: str) -> list[dict]:
    """Call `gh api --paginate <path>` and return the concatenated records.

    --paginate concatenates one JSON array per page with no separator, so the
    pages are decoded one raw array at a time rather than json.loads on the whole
    (which only ever sees the first page).
    """
    proc = subprocess.run(
        ["gh", "api", "--paginate", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RegistryError(proc.stderr.strip() or "gh api failed")

    records: list[dict] = []
    decoder = json.JSONDecoder()
    text = proc.stdout.strip()
    idx = 0
    while idx < len(text):
        chunk, end = decoder.raw_decode(text, idx)
        records.extend(chunk)
        idx = end
        while idx < len(text) and text[idx].isspace():
            idx += 1
    return records


@cache
def package_versions(org: str, app: str) -> tuple[dict, ...]:
    """Every version record for an org's container package (cached per process).

    Cached because a run resolves many apps but often re-reads the same one
    across the check and the write path -- one API sweep per package, not per
    lookup. Returns a tuple so the lru_cache value stays immutable.
    """
    return tuple(_gh_api(f"/orgs/{org}/packages/container/{app}/versions?per_page=100"))


def _parse_ts(value: str) -> datetime.datetime | None:
    """Parse a GH ISO-8601 timestamp ('...Z') to an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def package_tags(org: str, app: str) -> dict[str, str]:
    """Map tag -> digest for every tagged version of the container package.

    The check path's workhorse: a stable name -> what image the tag points at.
    """
    tags: dict[str, str] = {}
    for version in package_versions(org, app):
        digest = version.get("name", "")
        for tag in version.get("metadata", {}).get("container", {}).get("tags", []):
            tags[tag] = digest
    return tags


def resolve(org: str, app: str, tag: str) -> Resolved | None:
    """Resolve one (org, app, tag) to its digest + publish time, or None.

    None means the tag is not present on the package -- a release tag whose
    container job never pushed (hyperi-ci#102), which the caller reports rather
    than silently pinning a phantom image.
    """
    for version in package_versions(org, app):
        container_tags = version.get("metadata", {}).get("container", {}).get("tags", [])
        if tag in container_tags:
            return Resolved(
                digest=version.get("name", ""),
                published=_parse_ts(version.get("created_at", "")),
            )
    return None


def resolve_digest(org: str, app: str, tag: str) -> str | None:
    """Just the digest for (org, app, tag), or None if the tag is absent.

    The one-line entry point for a consumer that only wants the sha (dfe-deploy
    resolving an overlay's image.tag). Raises RegistryError on an API failure so
    a network/auth problem is never mistaken for "tag absent".
    """
    found = resolve(org, app, tag)
    return found.digest if found else None


def version_key(tag: str) -> tuple[int, ...]:
    """Numeric sort key so v1.18.19 ranks above v1.18.9, unlike a string sort."""
    return tuple(int(part) if part.isdigit() else 0 for part in tag.lstrip("v").split("."))


def head_commit(org: str, repo: str, ref: str = "main") -> str:
    """The commit sha a branch or tag currently points at.

    The repo half of resolve_digest: a content repo that ships no container has
    no package to read a digest from, so the immutable id for it is the commit
    the ref resolves to right now. Raises RegistryError rather than returning
    None, because a ref that does not resolve is a wrong name, not an absence.
    """
    proc = subprocess.run(
        ["gh", "api", f"/repos/{org}/{repo}/commits/{ref}", "--jq", ".sha"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RegistryError(proc.stderr.strip() or f"gh api could not read {org}/{repo}@{ref}")
    sha = proc.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RegistryError(f"{org}/{repo}@{ref} did not resolve to a commit sha: {sha!r}")
    return sha
