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
"""Registry pin resolution, read anonymously from the OCI registry.

The pain this kills: pinning a version means looking up its sha by hand and
pasting it in, in two repos. This is the shared core both the checker and the
writer sit on, so there is exactly one place that knows how to turn a tag into a
digest.

Everything here is decoupled from any pin FILE. The public surface is:

  ref_digest(ref)              -> (index digest, error) for a full image ref
  ref_raw(ref)                 -> (raw index or manifest JSON, error) for a full image ref
  ref_platforms(ref)           -> (os/arch set, error) for a full image ref
  run_with_retry(cmd, registry)-> (result, gave-up message) for any registry-reading command
  RetryBudget(seconds)         -> backoff a whole run may spend, shared across its reads
  registry_host(ref)           -> the registry host an image ref names
  is_absent(error)             -> whether a read error means the tag does not exist
  tag_digest(org, app, tag)    -> the digest a tag resolves to, or None if absent
  package_tags(org, app)       -> every tag the package carries
  resolve(org, app, tag)       -> a Resolved(digest, published) or None
  resolve_digest(org, app, tag)-> just the digest string, or None
  version_key(tag)             -> numeric sort key for vX.Y.Z tags
  head_commit(org, repo, ref)  -> the commit sha a branch or tag points at
  commit_summary(org, repo, sha) -> (subject line, commit date) of that commit

Every DFE image and chart is public, so no read here takes a credential. Tags,
digests and build times come from the registry's own OCI API through
oci_registry, anonymously. tag_digest tries `docker buildx imagetools` first, which
also reads a private mirror the docker credential store is logged in to, and the
OCI API where docker is absent.

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

import datetime
import json
import random
import re
import subprocess
import time
from dataclasses import dataclass, field

import oci_registry

GHCR = "ghcr.io"

# The only two failures that mean the tag is GONE rather than unreadable.
_ABSENT = re.compile(r"not found|manifest unknown", re.IGNORECASE)

# Registry/network failures worth a retry -- never an absence, never an auth error.
_TRANSIENT = re.compile(
    r"\b(?:500|502|503|504|429)\b|toomanyrequests|i/o timeout|tls handshake timeout"
    r"|connection reset|\beof\b",
    re.IGNORECASE,
)

# The subset of _TRANSIENT where the registry is refusing the caller's rate, not failing.
_RATE_LIMITED = re.compile(r"\b429\b|toomanyrequests", re.IGNORECASE)

# Seconds waited before each retry of one read, before jitter. A read with no
# RetryBudget keeps the quick schedule, so a caller that caps nothing cannot stall
# on a registry that stays down. PATIENT_WAITS is for a run that passes a budget.
_QUICK_WAITS = (1.0, 2.0)
PATIENT_WAITS = (4.0, 8.0, 16.0, 32.0)

# Module attributes so tests replace them with instant, deterministic stand-ins.
_sleep = time.sleep
_random = random.random


@dataclass(slots=True)
class RetryBudget:
    """How patiently a run's registry reads retry, and the backoff it has left.

    Without a shared cap, a registry that stays rate limited costs every read its
    full backoff in turn, so a gate reading forty images waits forty times over.

    Attributes:
        seconds: Backoff left to spend across every read of the run.
        waits: Seconds waited before each retry of one read, before jitter. One
            more attempt is made than there are waits.
        reads: Registry commands the run has started, by registry host, retries
            included, so a run can say what it asked of each registry.
    """

    seconds: float
    waits: tuple[float, ...] = PATIENT_WAITS
    reads: dict[str, int] = field(default_factory=dict)

    def spend(self, wait: float) -> bool:
        """Take ``wait`` seconds from the budget, or report that it holds fewer.

        Args:
            wait: Seconds the caller is about to sleep.

        Returns:
            True when the wait was taken, False when the budget cannot cover it.
        """
        if wait > self.seconds:
            return False
        self.seconds -= wait
        return True


@dataclass(frozen=True)
class Resolved:
    """A tag's registry state: the immutable digest and when it was published.

    `published` is when the image was built (UTC), from its
    org.opencontainers.image.created annotation or its config's `created`. It is
    what the supply-chain cooldown reads -- a digest whose image is younger than the
    cooldown window is not adopted without an explicit override.
    """

    digest: str
    published: datetime.datetime | None


class RegistryError(RuntimeError):
    """A registry or gh read that did not answer -- carries the underlying error."""


def package_tags(org: str, app: str, registry: str = GHCR) -> list[str]:
    """Every tag the org's package carries, empty when it is not published.

    A nested package such as charts/dfe-engine is the repository path as written.

    Raises:
        RegistryError: The registry did not answer, so an empty list always means
            nothing is published rather than nothing could be read.
    """
    try:
        return oci_registry.Repository(registry, f"{org}/{app}").tags()
    except oci_registry.RegistryReadError as exc:
        raise RegistryError(str(exc)) from exc


def resolve(org: str, app: str, tag: str, registry: str = GHCR) -> Resolved | None:
    """Resolve one (org, app, tag) to its digest + build time, or None.

    None means the tag is not present on the package -- a release tag whose
    container job never pushed (hyperi-ci#102), which the caller reports rather
    than silently pinning a phantom image.
    """
    repo = oci_registry.Repository(registry, f"{org}/{app}")
    try:
        found = repo.manifest(tag)
        if found is None:
            return None
        digest, document = found
        return Resolved(digest=digest, published=repo.created(document))
    except oci_registry.RegistryReadError as exc:
        raise RegistryError(str(exc)) from exc


def resolve_digest(org: str, app: str, tag: str, registry: str = GHCR) -> str | None:
    """Just the digest for (org, app, tag), or None if the tag is absent.

    The one-line entry point for a consumer that only wants the sha (dfe-deploy
    resolving an overlay's image.tag). Raises RegistryError when the registry does
    not answer, so a network problem is never mistaken for "tag absent".
    """
    try:
        found = oci_registry.Repository(registry, f"{org}/{app}").manifest(tag)
    except oci_registry.RegistryReadError as exc:
        raise RegistryError(str(exc)) from exc
    return found[0] if found else None


def registry_host(ref: str) -> str:
    """The registry host an image reference names, docker.io when it names none.

    Args:
        ref: An image reference such as ``public.ecr.aws/eks/app:v1`` or ``valkey/valkey:8``.

    Returns:
        The host. A first path segment is a host only when it holds a dot or a
        port colon, or is ``localhost``, which is how docker reads a reference.
    """
    first, slash, _rest = ref.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        return first
    return "docker.io"


def _last_line(stderr: str, default: str) -> str:
    lines = stderr.strip().splitlines()
    return lines[-1] if lines else default


def _gave_up(registry: str, stderr: str, attempts: int, budget_spent: bool) -> str:
    """The message for a read that stayed transient: which registry, and whether it rate limited."""
    verdict = "rate limited this read" if _RATE_LIMITED.search(stderr) else "kept failing"
    spent = " (the run's retry budget is spent)" if budget_spent else ""
    noun = "attempt" if attempts == 1 else "attempts"
    last = _last_line(stderr, "no output")
    return f"{registry} {verdict} and gave up after {attempts} {noun}{spent}: {last}"


def run_with_retry(
    cmd: list[str], registry: str, budget: RetryBudget | None = None
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run a registry-reading command, retrying a transient failure with backoff.

    A failure matching _TRANSIENT (a 429, a 5xx, a timeout) is retried with jittered
    exponential backoff, three attempts by default and one more than the budget's
    waits when a budget is given. Success, an absence, an auth error and any other
    failure return on the first attempt.

    Args:
        cmd: The command and its arguments.
        registry: The host the command reads, named in the message when the retries run out.
        budget: The run's shared RetryBudget, which sets the schedule and caps the
            total backoff, or None for the quick schedule alone. A wait the budget
            cannot cover ends the retries early.

    Returns:
        The last attempt's result, and a message naming the registry and whether it
        rate limited when the retries ran out ("" otherwise).

    Raises:
        OSError: The command could not be started. It is never retried.
    """
    waits = _QUICK_WAITS if budget is None else budget.waits
    attempts = 0
    while True:
        if budget is not None:
            budget.reads[registry] = budget.reads.get(registry, 0) + 1
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        attempts += 1
        absent = is_absent(_last_line(proc.stderr, ""))
        if proc.returncode == 0 or absent or not _TRANSIENT.search(proc.stderr):
            return proc, ""
        if attempts > len(waits):
            return proc, _gave_up(registry, proc.stderr, attempts, budget_spent=False)
        wait = waits[attempts - 1] * (0.5 + _random())
        if budget is not None and not budget.spend(wait):
            return proc, _gave_up(registry, proc.stderr, attempts, budget_spent=True)
        _sleep(wait)


def _imagetools(
    ref: str, *flags: str, budget: RetryBudget | None = None
) -> tuple[str | None, str]:
    """(stdout, error) of `docker buildx imagetools inspect <ref> <flags>`.

    Reads the registry through the docker credential store, so it needs no
    read:packages scope, and reads a public package unauthenticated. A transient
    failure is retried by run_with_retry; an absent tag or a missing docker binary
    returns on the first try.
    """
    cmd = ["docker", "buildx", "imagetools", "inspect", ref, *flags]
    try:
        proc, gave_up = run_with_retry(cmd, registry_host(ref), budget)
    except OSError as exc:
        # Worded so it cannot match _ABSENT: no docker is a missing tool, not a
        # missing image.
        return None, f"cannot run docker buildx: {exc}"
    if proc.returncode == 0:
        return proc.stdout, ""
    return None, gave_up or _last_line(proc.stderr, "docker buildx imagetools failed")


def ref_raw(ref: str, budget: RetryBudget | None = None) -> tuple[str | None, str]:
    """(raw index or manifest JSON, error) for a full image ref, read with `docker buildx imagetools`."""
    return _imagetools(ref, "--raw", budget=budget)


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


def ref_platforms(ref: str, budget: RetryBudget | None = None) -> tuple[set[str] | None, str]:
    """(os/arch set, error) for a full image ref, read with `docker buildx imagetools`.

    The same registry path and credentials as ref_digest. An index lists its
    platforms; a single-manifest image lists none, so its config is read for the
    one platform it was built for. ``budget`` caps the backoff a whole run spends.
    """
    raw, err = ref_raw(ref, budget)
    if raw is None:
        return None, err
    try:
        found = index_platforms(json.loads(raw))
    except json.JSONDecodeError as exc:
        return None, f"unreadable manifest: {exc}"
    if found is not None:
        return found, ""
    config, err = _imagetools(ref, "--format", "{{json .Image}}", budget=budget)
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

    imagetools first, the registry's OCI API second, so a machine without docker
    still resolves. Raises RegistryError when NEITHER could answer, so a registry
    nobody reached never reads as a verified pin.
    """
    digest, err = ref_digest(f"{registry}/{org}/{app}:{tag}")
    if digest:
        return digest
    try:
        return resolve_digest(org, app, tag, registry)
    except RegistryError as api_exc:
        if is_absent(err):
            return None
        raise RegistryError(
            f"{registry}/{org}/{app}:{tag} did not resolve -- "
            f"imagetools: {err}; OCI API: {api_exc}"
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


def commit_summary(org: str, repo: str, sha: str) -> tuple[str, str]:
    """The subject line and committer date (YYYY-MM-DD) of one commit.

    What a pin recorded by commit cites as its evidence. Raises RegistryError
    when the commit cannot be read or has no subject, so a pin is never
    justified by an empty string.
    """
    proc = subprocess.run(
        [
            "gh", "api", f"/repos/{org}/{repo}/commits/{sha}",
            "--jq", "{message: .commit.message, date: .commit.committer.date}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RegistryError(proc.stderr.strip() or f"gh api could not read {org}/{repo}@{sha}")
    record = json.loads(proc.stdout)
    lines = (record.get("message") or "").strip().splitlines()
    date = (record.get("date") or "")[:10]
    if not lines or not date:
        raise RegistryError(f"{org}/{repo}@{sha} returned no subject or date: {record!r}")
    return lines[0].strip(), date
