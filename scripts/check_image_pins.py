#!/usr/bin/env python3
"""Verify every DFE app pin in versions.yaml resolves to a real registry image.

check_versions_drift.py proves the pins agree with each OTHER; nothing proved
they name an image that EXISTS. A release can cut its tag while the container
job fails (hyperi-ci#102, live on dfe-loader v1.18.19: GH release v1.18.19 with
GHCR still at v1.18.18), so a sweep taken from the release page can pin an image
that was never pushed -- and the deploy discovers it as ImagePullBackOff.

Two assertions per app, against the GH packages API:
  1. the pinned version tag exists on the package
  2. its digest EQUALS the pinned digest (catches a moved tag, and a sweep that
     paired the right version with the wrong digest)

Network-dependent by design, so it is NOT part of the offline drift check --
run it when pins change, before the sweep lands.

Usage:
    python3 scripts/check_image_pins.py [--app NAME ...] [--org ORG]
Requires an authenticated `gh` (the packages API needs read:packages).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The tag -> digest lookup lives in registry_pins so this checker and the
# resolve_pins writer share ONE definition (dfe-infra#116). Imported by path so
# it works whether check_image_pins is run as a script or imported.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry_pins import RegistryError, package_tags, version_key  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS = REPO_ROOT / "versions.yaml"
DEFAULT_ORG = "hyperi-io"
# Pins carrying no digest are declared NOT YET PUBLISHED in versions.yaml, so a
# missing image is the documented state rather than a fault.
SKIP_WITHOUT_DIGEST = True


def load_pins() -> tuple[dict[str, str], dict[str, str]]:
    """Return (apps, digests) from versions.yaml."""
    import yaml

    data = yaml.safe_load(VERSIONS.read_text(encoding="utf-8", errors="replace"))
    apps = _find_section(data, "apps")
    digests = _find_section(data, "digests")
    return apps, digests


def _find_section(tree: object, name: str) -> dict[str, str]:
    """Depth-first search for the first mapping called `name`."""
    if isinstance(tree, dict):
        if name in tree and isinstance(tree[name], dict):
            return {k: str(v) for k, v in tree[name].items()}
        for value in tree.values():
            found = _find_section(value, name)
            if found:
                return found
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", action="append", help="check only these apps")
    parser.add_argument("--org", default=DEFAULT_ORG, help=f"GH org (default {DEFAULT_ORG})")
    args = parser.parse_args()

    apps, digests = load_pins()
    if not apps:
        print("could not find an `apps:` section in versions.yaml", file=sys.stderr)
        return 1

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
            tags = package_tags(args.org, app)
        except RegistryError as exc:
            failures.append(f"  [api]     {app}: {exc}")
            continue

        if version not in tags:
            released = sorted((t for t in tags if t.startswith("v")), key=version_key)
            newest = ", ".join(released[-3:]) or "none"
            failures.append(
                f"  [MISSING] {app} {version}: NO image with that tag in "
                f"ghcr.io/{args.org}/{app} (newest tags: {newest}). A release tag "
                f"without an image -- see hyperi-ci#102."
            )
            continue

        checked += 1
        if tags[version] != digest:
            failures.append(
                f"  [DIGEST]  {app} {version}: registry has {tags[version]}, "
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
