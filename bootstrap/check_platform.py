#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         check_platform.py
#  Purpose:      Refuse a target cluster below the stack's platform floor
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Check the target cluster against versions.yaml's `platform` stage.

The platform entries are REQUIREMENTS on a cluster this repo does not build, so
nothing else in the tree can drift against them -- which would leave them as a
declaration nobody reads. This is what reads them.

Run it before installing anything:

    python3 check_platform.py                  # against the current kubecontext
    python3 check_platform.py --actual 1.33    # against a version you name

Exits 3 when the cluster is below the floor, 0 when it meets it.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from read_versions import load_versions


def parse_requirement(raw: str) -> tuple[str, str | None]:
    """Split a requirement string into (floor, ceiling).

    `>=1.34` is a floor with no ceiling, `1.34-1.36` is both, and a bare `2.15`
    pins exactly. Written as strings because check_versions_drift.py flattens
    only str leaves, so a {min:, max:} map would be dropped in silence.
    """
    value = raw.strip()
    if value.startswith(">="):
        return value[2:].strip(), None
    if "-" in value and not value.startswith("v"):
        low, _, high = value.partition("-")
        return low.strip(), high.strip()
    return value, value


def as_tuple(version: str) -> tuple[int, ...]:
    """Numeric parts of a version, so 1.34 and v1.34.11+rke2r1 compare.

    Distributions append build metadata (`+rke2r1`) and EKS reports a minor of
    `34+`, so anything that is not a digit run is dropped rather than parsed.
    """
    return tuple(int(p) for p in re.findall(r"\d+", version))


def cluster_version() -> str:
    """The target cluster's server version, via kubectl."""
    out = subprocess.run(
        ["kubectl", "version", "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    server = json.loads(out)["serverVersion"]
    return f"{server['major']}.{server['minor']}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default=None, help="Path to versions.yaml")
    parser.add_argument("--stack", default=None, help="Stack to read (default: `current`)")
    parser.add_argument(
        "--actual",
        default=None,
        help="Cluster version to check instead of asking kubectl",
    )
    args = parser.parse_args()

    versions_file = Path(args.file) if args.file else Path(__file__).resolve().parent.parent / "versions.yaml"
    root = load_versions(versions_file)
    stack_name = args.stack or root.get("current")
    stack = root.get("stacks", {}).get(stack_name)
    if stack is None:
        print(f"ERROR: stack '{stack_name}' not found in {versions_file}", file=sys.stderr)
        return 1

    platform = stack.get("platform")
    if not platform:
        print(f"ERROR: stack '{stack_name}' declares no platform stage", file=sys.stderr)
        return 1

    required = platform.get("kubernetes")
    if not required:
        print(f"ERROR: platform.kubernetes missing from stack '{stack_name}'", file=sys.stderr)
        return 1

    try:
        actual = args.actual or cluster_version()
    except (subprocess.CalledProcessError, FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not read the cluster version: {exc}", file=sys.stderr)
        return 1

    floor, ceiling = parse_requirement(required)
    actual_t, floor_t = as_tuple(actual), as_tuple(floor)

    if actual_t < floor_t:
        print(
            f"REFUSED: cluster is Kubernetes {actual}, and stack {stack_name} "
            f"requires {required}.\n"
            f"         Upgrade the cluster, or deploy a stack whose platform "
            f"floor it meets.",
            file=sys.stderr,
        )
        return 3

    if ceiling and actual_t > as_tuple(ceiling):
        print(
            f"WARNING: cluster is Kubernetes {actual}, above the {ceiling} this "
            f"stack was tested against. Proceeding.",
            file=sys.stderr,
        )

    print(f"platform: Kubernetes {actual} meets {required} (stack {stack_name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
