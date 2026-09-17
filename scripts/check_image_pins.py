#!/usr/bin/env python3
"""Verify every DFE app pin in versions.yaml resolves to a real registry image.

check_versions_drift.py proves the pins agree with each OTHER; nothing proved
they name an image that EXISTS. A release can cut its tag while the container
job fails (hyperi-ci#102, live on dfe-loader v1.18.19: GH release v1.18.19 with
GHCR still at v1.18.18), so a sweep taken from the release page can pin an image
that was never pushed -- and the deploy discovers it as ImagePullBackOff.

Two assertions per app:
  1. the pinned version tag exists on the package
  2. its digest EQUALS the pinned digest (catches a moved tag, and a sweep that
     paired the right version with the wrong digest)

Network-dependent by design, so it is NOT part of the offline drift check --
run it when pins change, before the sweep lands.

Usage:
    python3 scripts/check_image_pins.py [--app NAME ...] [--org ORG] [--stack VER]

Digests resolve through registry_pins.tag_digest, so `docker buildx` alone is
enough; a `gh` carrying read:packages is the fallback, and also what fills in the
newest-tags hint when a tag turns out to be missing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The tag -> digest lookup lives in registry_pins so this checker and the
# resolve_pins writer share ONE definition (dfe-infra#116); stack selection
# comes from resolve_pins for the same reason. Imported by path so it works
# whether check_image_pins is run as a script or imported.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry_pins import RegistryError, package_tags, tag_digest, version_key
from resolve_pins import load_stack

DEFAULT_ORG = "hyperi-io"
# Pins carrying no digest are declared NOT YET PUBLISHED in versions.yaml, so a
# missing image is the documented state rather than a fault.
SKIP_WITHOUT_DIGEST = True


def load_pins(stack: str | None = None) -> tuple[str, dict[str, str], dict[str, str]]:
    """Return (stack name, apps, digests) for `stack`, else the `current` pointer.

    versions.yaml carries every stack's complete pin set, so the section must be
    taken from a NAMED stack -- a search of the whole file finds the oldest one.
    """
    _, _, name, body = load_stack(stack)
    return name, _section(body, "apps"), _section(body, "digests")


def _section(stack_map: object, name: str) -> dict[str, str]:
    """One stack's `name:` mapping, or {} when the stack does not carry it."""
    section = stack_map.get(name) if isinstance(stack_map, dict) else None
    if not isinstance(section, dict):
        return {}
    return {str(k): str(v) for k, v in section.items()}


def _newest_tags(org: str, app: str) -> str:
    """A ` (newest tags: ...)` aside for a missing-tag report, else empty.

    Listing a package's tags needs read:packages, which resolving a digest no
    longer does, so this hint is advisory -- its absence never changes a verdict.
    """
    try:
        tags = package_tags(org, app)
    except RegistryError:
        return ""
    released = sorted((t for t in tags if t.startswith("v")), key=version_key)
    return f" (newest tags: {', '.join(released[-3:])})" if released else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", action="append", help="check only these apps")
    parser.add_argument("--org", default=DEFAULT_ORG, help=f"GH org (default {DEFAULT_ORG})")
    parser.add_argument("--stack", help="stack version to check (default: the `current` pointer)")
    args = parser.parse_args()

    stack, apps, digests = load_pins(args.stack)
    if not apps:
        print(f"stack {stack} has no `apps:` section in versions.yaml", file=sys.stderr)
        return 1
    print(f"checking stack {stack}")

    wanted = args.app or sorted(apps)
    failures: list[str] = []
    checked = 0
    skipped: list[str] = []

    for app in wanted:
        version = apps.get(app)
        if version is None:
            failures.append(f"  [config]  {app}: no apps.{app} pin in versions.yaml")
            continue
        digest = digests.get(app)
        if digest is None and SKIP_WITHOUT_DIGEST:
            skipped.append(f"  [skip]    {app} {version} -- no digest pin (not yet published)")
            continue

        try:
            actual = tag_digest(args.org, app, version)
        except RegistryError as exc:
            failures.append(f"  [api]     {app}: {exc}")
            continue

        if actual is None:
            failures.append(
                f"  [MISSING] {app} {version}: NO image with that tag in "
                f"ghcr.io/{args.org}/{app}{_newest_tags(args.org, app)}. A release "
                f"tag without an image -- see hyperi-ci#102."
            )
            continue

        checked += 1
        if actual != digest:
            failures.append(
                f"  [DIGEST]  {app} {version}: registry has {actual}, "
                f"versions.yaml pins {digest}"
            )

    for line in skipped:
        print(line)
    if failures:
        print("\nImage pins do NOT resolve:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        print(f"\n{len(failures)} problem(s); {checked} pin(s) verified.", file=sys.stderr)
        return 1

    print(f"\nOK -- {checked} image pin(s) exist in the registry with the pinned digest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
