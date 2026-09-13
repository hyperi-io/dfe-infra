#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         local_path_dir.py
#  Purpose:      Point local-path-provisioner's default node path at a chosen disk
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Rewrite the local-path-provisioner config.json so its volumes land on a chosen disk.

Upstream's manifest hands every node `/opt/local-path-provisioner`, which on a
node whose data disk is mounted elsewhere puts every PV on the root filesystem.
This reads the live `config.json` on stdin, replaces the path for the default
node entry, and prints the new document for `kubectl patch` to apply. Keys the
manifest carries besides `nodePathMap` are passed through untouched, and a
per-node entry a deployer added by hand is left where it is.

Usage:
    kubectl -n local-path-storage get configmap local-path-config \\
        -o jsonpath='{.data.config\\.json}' \\
      | python3 local_path_dir.py --dir /var/lib/rancher/local-path-provisioner
"""

from __future__ import annotations

import argparse
import json
import sys

DEFAULT_NODE = "DEFAULT_PATH_FOR_NON_LISTED_NODES"


def retarget(config: str, directory: str) -> str:
    """Return config.json with the default node entry pointing at directory."""
    document = json.loads(config)
    entries = document.get("nodePathMap") or []
    for entry in entries:
        if entry.get("node") == DEFAULT_NODE:
            entry["paths"] = [directory]
            break
    else:
        entries.append({"node": DEFAULT_NODE, "paths": [directory]})
    document["nodePathMap"] = entries
    return json.dumps(document, indent=4)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True, help="directory on each node the volumes are created under")
    args = parser.parse_args()
    if not args.dir.startswith("/"):
        print(f"local_path_dir: --dir must be an absolute path, got {args.dir}", file=sys.stderr)
        return 2
    sys.stdout.write(retarget(sys.stdin.read(), args.dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
