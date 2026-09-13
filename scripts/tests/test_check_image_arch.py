#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/tests/test_check_image_arch.py
#  Purpose:      Unit tests for the manifest-platform parser behind the
#                multi-arch image gate; no network, no docker.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Run directly:

    python3 scripts/tests/test_check_image_arch.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_image_arch import platforms_of  # noqa: E402


def _index(*platforms: tuple[str, str]) -> str:
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {"digest": f"sha256:{i:064x}", "platform": {"os": os_name, "architecture": arch}}
                for i, (os_name, arch) in enumerate(platforms)
            ],
        }
    )


class PlatformsOfTests(unittest.TestCase):
    def test_multi_arch_index_lists_both(self) -> None:
        doc = _index(("linux", "amd64"), ("linux", "arm64"))
        self.assertEqual(platforms_of(doc), {"linux/amd64", "linux/arm64"})

    def test_attestation_entries_are_dropped(self) -> None:
        # buildx attestation manifests report unknown/unknown and must not
        # count as a platform, or a single-arch image would look like two.
        doc = _index(("linux", "amd64"), ("unknown", "unknown"))
        self.assertEqual(platforms_of(doc), {"linux/amd64"})

    def test_single_manifest_has_no_platforms_until_verbose(self) -> None:
        doc = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json"})
        self.assertEqual(platforms_of(doc), set())

    def test_verbose_single_manifest_reads_descriptor(self) -> None:
        doc = json.dumps([{"Ref": "x", "Descriptor": {"platform": {"os": "linux", "architecture": "arm64"}}}])
        self.assertEqual(platforms_of(doc), {"linux/arm64"})


if __name__ == "__main__":
    unittest.main()
