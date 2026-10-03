#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         local_path_image.py
#  Purpose:      Pin local-path-provisioner's image by digest in upstream's manifest
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Print upstream's local-path-storage manifest with the provisioner pinned by digest.

Upstream's deploy/local-path-storage.yaml names the provisioner image by tag. This
fetches the manifest at the pinned git tag, rewrites that one reference to
tag@sha256, and prints the result for `kubectl apply -f -`, so the cluster never
sees the bare tag and no pod pulls by it. A manifest that names the image other
than exactly once is refused rather than applied unpinned.

Usage:
    python3 local_path_image.py --version v0.0.36 --digest sha256:<64 hex> \\
      | kubectl apply -f -
"""

import argparse
import re
import sys
import urllib.request

IMAGE = "docker.io/rancher/local-path-provisioner"
MANIFEST_URL = "https://raw.githubusercontent.com/rancher/local-path-provisioner/{version}/deploy/local-path-storage.yaml"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
FETCH_TIMEOUT_SECONDS = 30


def pin(manifest: str, version: str, digest: str) -> str:
    """Return the manifest with the provisioner image at version pinned to digest.

    Args:
        manifest: Upstream's local-path-storage.yaml text.
        version: The git tag the manifest was fetched at, which is also its image tag.
        digest: The image's index digest, `sha256:` and 64 hex characters.

    Returns:
        The manifest with `<IMAGE>:<version>` replaced by `<IMAGE>:<version>@<digest>`.

    Raises:
        ValueError: The digest is malformed, or the manifest names the image at
            that version other than exactly once.
    """
    if not DIGEST.fullmatch(digest):
        raise ValueError(f"digest {digest!r} is not sha256:<64 hex>")
    tagged = f"{IMAGE}:{version}"
    pattern = re.compile(rf"^([ \t]*image:[ \t]*){re.escape(tagged)}[ \t]*$", re.MULTILINE)
    hits = pattern.findall(manifest)
    if len(hits) != 1:
        raise ValueError(f"manifest names {tagged} {len(hits)} time(s), expected 1")
    return pattern.sub(lambda m: f"{m.group(1)}{tagged}@{digest}", manifest)


def fetch(version: str) -> str:
    """Upstream's manifest at the git tag `version`."""
    with urllib.request.urlopen(
        MANIFEST_URL.format(version=version), timeout=FETCH_TIMEOUT_SECONDS
    ) as reply:
        return reply.read().decode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--version", required=True, help="bootstrap.local-path-provisioner, e.g. v0.0.36"
    )
    parser.add_argument("--digest", required=True, help="services-digests.local-path-provisioner")
    args = parser.parse_args()
    try:
        sys.stdout.write(pin(fetch(args.version), args.version, args.digest))
    except (OSError, ValueError) as err:
        print(f"local_path_image: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
