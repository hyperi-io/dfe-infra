#  Project:      dfe-infra
#  File:         scripts/suite/artefacts.py
#  Purpose:      Read the live published version and prove a release actually
#                moved -- a green CI run is never the evidence.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Where a repo's release lands, and the assertion that it got there.

A green CI run is not proof of a release. The publish stage can be gated out
and the run still ends green, so the only evidence that counts is the version
the registry serves changing from the baseline read before the release started.
Losing the baseline turns that assertion into a no-op, which is why an
unreadable registry refuses to start rather than assuming anything.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from suite.proc import (
    ARTEFACT_TIMEOUT,
    REGISTRY_POLL_SECONDS,
    REGISTRY_TIMEOUT,
    FleetError,
    http_json,
    run,
    say,
    warn,
)

# "Nothing published yet" is a legitimate baseline (a repo's first release), so
# it needs a value distinct from "the registry could not be read" -- which must
# never be silently treated as a version, or the shipped-it assertion turns into
# a no-op that reports success.
NOTHING_PUBLISHED = ""


def crates_version(crate: str) -> str:
    """The newest stable version crates.io serves for a crate.

    ``max_stable_version`` rather than ``max_version``: the latter counts
    pre-releases, so a stray ``-dev`` would read as a successful GA publish.

    Raises:
        FleetError: If crates.io cannot be read or answers with a shape this
            does not understand.
    """
    payload = http_json(f"https://crates.io/api/v1/crates/{crate}")
    body = payload.get("crate") if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        raise FleetError(f"crates.io returned no crate body for {crate}")
    version = body.get("max_stable_version") or body.get("max_version")
    return str(version) if version else NOTHING_PUBLISHED


def pypi_version(package: str) -> str:
    """The version PyPI currently serves for a package.

    Read ``info.version``: the per-file entries carry their own ``version``
    keys, so anything that scans the raw body picks whichever sorts first
    rather than the current release.

    Raises:
        FleetError: If PyPI cannot be read or answers with an unexpected shape.
    """
    payload = http_json(f"https://pypi.org/pypi/{package}/json")
    info = payload.get("info") if isinstance(payload, dict) else None
    if not isinstance(info, dict):
        raise FleetError(f"PyPI returned no info block for {package}")
    version = info.get("version")
    return str(version) if version else NOTHING_PUBLISHED


def latest_release_tag(slug: str) -> str:
    """The most recent GitHub release tag for a repo.

    Returns ``NOTHING_PUBLISHED`` when the repo genuinely has no releases yet.

    Raises:
        FleetError: If gh cannot answer, which is a different thing entirely.
    """
    proc = run(
        ["gh", "release", "list", "-R", slug, "--limit", "1", "--json", "tagName"],
        check=False,
    )
    if proc.returncode != 0:
        raise FleetError(f"gh release list -R {slug} failed: {proc.stderr.strip()}")
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise FleetError(f"gh release list -R {slug} did not return JSON") from exc
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return str(rows[0].get("tagName") or NOTHING_PUBLISHED)
    return NOTHING_PUBLISHED


@dataclass(frozen=True, slots=True)
class Artefact:
    """Where a repo's release lands, so the tool can prove it actually moved."""

    kind: str  # "crates" | "pypi" | "ghrelease"
    name: str  # crate name, PyPI package name, or the repo slug

    def live(self) -> str:
        """Read the currently published version.

        Raises:
            FleetError: If the destination cannot be read.
        """
        if self.kind == "crates":
            return crates_version(self.name)
        if self.kind == "pypi":
            return pypi_version(self.name)
        return latest_release_tag(self.name)

    def baseline(self) -> str:
        """Read the pre-release version, refusing to start if it is unreadable.

        Without a trustworthy baseline the post-release comparison cannot
        distinguish "published" from "unchanged", so a run that cannot take one
        would report SHIPPED on the strength of nothing.

        Raises:
            FleetError: If the destination cannot be read.
        """
        try:
            return self.live()
        except FleetError as exc:
            raise FleetError(
                f"cannot read the current version from {self.describe()} "
                f"({exc}). Refusing to start: without a baseline there is no way "
                f"to prove the release actually shipped."
            ) from exc

    def describe(self) -> str:
        """Human name of the destination, for progress lines."""
        return {"crates": "crates.io", "pypi": "PyPI"}.get(self.kind, "GitHub Releases")


def await_artefact(
    artefact: Artefact,
    before: str,
    *,
    slug: str,
    run_id: int,
    registry_timeout: int = REGISTRY_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Block until the published version differs from ``before``.

    A green CI run is NOT proof of a release: the publish stage can be skipped
    by a gate and the run still ends green. This is the assertion that catches
    that, and it is the last thing every command does.

    The caller has already followed the run to a green conclusion -- that is
    what early-fails a red job and NAMES it -- so what is left here is index
    lag: crates.io and PyPI trail the publish by a CDN hop. A flat five-minute
    window was the wrong deadline for that, because a rebuild called a release
    dead while it was still catching up. So the registry is polled in
    ``registry_timeout`` windows, each closing with a progress line, until it
    moves or the ``ARTEFACT_TIMEOUT`` ceiling is spent.

    Args:
        artefact: Where this repo's release lands.
        before: The version read before the release started.
        slug: ``owner/name`` of the repo the run belongs to, for the failure.
        run_id: The run this release rode on, for the failure.
        registry_timeout: How long one window runs before a progress line.
        sleep: How the wait between polls is spent. Injected, so a test can
            drive the ceiling without sleeping for real.
        monotonic: The clock the ceiling is measured on. Injected the same way.

    Returns:
        The newly published version.

    Raises:
        FleetError: If the version has still not moved by the ceiling.
    """
    where = artefact.describe()
    say(
        f"confirming the artefact reached {where} "
        f"({ARTEFACT_TIMEOUT // 60}min ceiling, {registry_timeout}s windows)"
    )
    started = monotonic()
    cap = started + ARTEFACT_TIMEOUT
    window_end = started + registry_timeout
    while True:
        try:
            after = artefact.live()
        except FleetError as exc:
            # A read failure mid-poll is a blip, not a verdict. Keep asking.
            warn(str(exc))
            after = before
        if after != before:
            return after
        now = monotonic()
        if now >= window_end:
            say(
                f"{where} still serves '{before or 'nothing'}' after "
                f"{int(now - started)}s -- the index lags the publish, waiting"
            )
            window_end = now + registry_timeout
        if now >= cap:
            break
        sleep(REGISTRY_POLL_SECONDS)
    shown = before or "nothing"
    raise FleetError(
        f"{where} still serves '{shown}' {ARTEFACT_TIMEOUT // 60}min after run "
        f"{run_id} was already green. The CI passed but published NOTHING -- do "
        f"not treat this as shipped. Usual cause: the release gate decided there "
        f"was nothing to release. Check: gh run view {run_id} -R {slug}"
    )
