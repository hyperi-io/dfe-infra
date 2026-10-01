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

  ref_digest(ref)              -> (index digest, error) for a full image ref
  ref_raw(ref)                 -> (raw index or manifest JSON, error) for a full image ref
  ref_platforms(ref)           -> (os/arch set, error) for a full image ref
  is_absent(error)             -> whether a read error means the tag does not exist
  tag_digest(org, app, tag)    -> the digest a tag resolves to, or None if absent
  package_versions(org, app)   -> the raw GHCR version records (paginated)
  package_tags(org, app)       -> {tag: digest} for every tagged version
  resolve(org, app, tag)       -> a Resolved(digest, published) or None
  resolve_digest(org, app, tag)-> just the digest string, or None
  version_key(tag)             -> numeric sort key for vX.Y.Z tags
  head_commit(org, repo, ref)  -> the commit sha a branch or tag points at

Two ways to reach a registry, because they need different credentials.
`docker buildx imagetools` authenticates from the docker credential store and
reads a public package with no credential at all; the GH packages API needs a
token carrying `read:packages`, which a developer's own `gh login` usually lacks.
So tag_digest tries imagetools first and the API second, and a checker that only
needs a digest keeps working on a plain `gh auth login`.

The API is still the only way to LIST a package's tags or to read a version's
publish time, so package_versions/package_tags/resolve stay on it.

A content repo that ships no container still has to be pinned immutably, so
head_commit is the same job as resolve_digest for a repo rather than a package:
a movable name in, an immutable id out.

dfe-deploy usage (an overlay pins image.tag; resolve its digest for a pull-by-
digest ref) is a two-liner:

    from registry_pins import resolve_digest
    digest = resolve_digest("hyperi-io", "dfe-loader", "v1.18.21")
    # -> "sha256:..."; write f"{tag}@{digest}" into the overlay's image field.

No third-party deps -- docker + gh + stdlib only.
"""

from __future__ import annotations

import datetime
import json
import random
import re
import subprocess
import time
from dataclasses import dataclass
from functools import cache

GHCR = "ghcr.io"

# The only two failures that mean the tag is GONE rather than unreadable.
_ABSENT = re.compile(r"not found|manifest unknown", re.IGNORECASE)

# Registry/network failures worth a retry -- never an absence, never an auth error.
_TRANSIENT = re.compile(
    r"\b(?:500|502|503|504|429)\b|toomanyrequests|i/o timeout|tls handshake timeout"
    r"|connection reset|\beof\b",
    re.IGNORECASE,
)

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = (1.0, 2.0)

# Module attributes so tests replace them with instant, deterministic stand-ins.
_sleep = time.sleep
_random = random.random


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


def _imagetools(ref: str, *flags: str) -> tuple[str | None, str]:
    """(stdout, error) of `docker buildx imagetools inspect <ref> <flags>`.

    Reads the registry through the docker credential store, so it needs no
    read:packages scope, and reads a public package unauthenticated. A
    transient failure (_TRANSIENT) is retried up to _MAX_ATTEMPTS times with
    jittered backoff; an absent tag or a missing docker binary returns on the
    first try.
    """
    error = ""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            proc = subprocess.run(
                ["docker", "buildx", "imagetools", "inspect", ref, *flags],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            # Worded so it cannot match _ABSENT: no docker is a missing tool, not a
            # missing image.
            return None, f"cannot run docker buildx: {exc}"
        if proc.returncode == 0:
            return proc.stdout, ""
        stderr = proc.stderr.strip()
        error = stderr.splitlines()[-1] if stderr else "docker buildx imagetools failed"
        if is_absent(error) or not _TRANSIENT.search(stderr):
            return None, error
        if attempt < _MAX_ATTEMPTS - 1:
            _sleep(_BACKOFF_BASE_SECONDS[attempt] * (0.5 + _random()))
    return None, f"{error} (after {_MAX_ATTEMPTS} attempts)"


def ref_raw(ref: str) -> tuple[str | None, str]:
    """(raw index or manifest JSON, error) for a full image ref, read with `docker buildx imagetools`."""
    return _imagetools(ref, "--raw")


def is_absent(err: str) -> bool:
    """Whether a registry read error means the tag does not exist, not that the read failed."""
    return bool(_ABSENT.search(err))


def ref_digest(ref: str) -> tuple[str | None, str]:
    """(digest, error) for a full image ref, read with `docker buildx imagetools`.

    The multi-arch INDEX digest -- what a pin records -- never one platform's
    manifest.
    """
    out, err = _imagetools(ref, "--format", "{{.Manifest.Digest}}")
    if out is None:
        return None, err
    digest = out.strip()
    if not digest.startswith("sha256:"):
        return None, f"unexpected digest {digest!r}"
    return digest, ""


def index_platforms(doc: object) -> set[str] | None:
    """The `os/arch` set an image index lists, or None when doc is not an index.

    buildx attestation manifests report `unknown/unknown` and are not platforms,
    or a single-arch image would read as two.
    """
    entries = doc.get("manifests") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        return None
    found: set[str] = set()
    for entry in entries:
        plat = entry.get("platform") if isinstance(entry, dict) else None
        if not isinstance(plat, dict):
            continue
        os_name, arch = plat.get("os"), plat.get("architecture")
        if os_name and arch and "unknown" not in (os_name, arch):
            found.add(f"{os_name}/{arch}")
    return found


def config_platform(doc: object) -> str | None:
    """`os/arch` from an image config, or None when it names neither."""
    if not isinstance(doc, dict):
        return None
    os_name, arch = doc.get("os"), doc.get("architecture")
    return f"{os_name}/{arch}" if os_name and arch else None


def ref_platforms(ref: str) -> tuple[set[str] | None, str]:
    """(os/arch set, error) for a full image ref, read with `docker buildx imagetools`.

    The same registry path and credentials as ref_digest. An index lists its
    platforms; a single-manifest image lists none, so its config is read for the
    one platform it was built for.
    """
    raw, err = ref_raw(ref)
    if raw is None:
        return None, err
    try:
        found = index_platforms(json.loads(raw))
    except json.JSONDecodeError as exc:
        return None, f"unreadable manifest: {exc}"
    if found is not None:
        return found, ""
    config, err = _imagetools(ref, "--format", "{{json .Image}}")
    if config is None:
        return None, err
    try:
        single = config_platform(json.loads(config))
    except json.JSONDecodeError as exc:
        return None, f"unreadable image config: {exc}"
    if single is None:
        return None, "image config names no os/architecture"
    return {single}, ""


def tag_digest(org: str, app: str, tag: str, registry: str = GHCR) -> str | None:
    """The digest (org, app, tag) resolves to right now, or None if the tag is absent.

    imagetools first, the packages API second: between them one works on a plain
    `gh auth login` and the other works where docker is absent. Raises
    RegistryError when NEITHER could answer, so a registry nobody reached never
    reads as a verified pin.
    """
    digest, err = ref_digest(f"{registry}/{org}/{app}:{tag}")
    if digest:
        return digest
    try:
        return resolve_digest(org, app, tag)
    except RegistryError as api_exc:
        if is_absent(err):
            return None
        raise RegistryError(
            f"{registry}/{org}/{app}:{tag} did not resolve -- "
            f"imagetools: {err}; gh api: {api_exc}"
        ) from api_exc


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
