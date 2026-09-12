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

`platform.kubernetes` is checked on every target. `platform.rke2` is checked as
well when the cluster reports `+rke2` in gitVersion, which is the on-prem side;
`platform.rancher` is declared rather than checked, because nothing in a cluster
reports the Rancher managing it.

Exits 3 when the cluster is below a floor, 0 when it meets it. A cluster above
the ceiling warns and proceeds: untested is not the same as unsupported.
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
    # Decide range-or-exact on the bare number: a leading `v` is a prefix, not a
    # separator, so `v1.34-v1.36` is a range and `v1.36.4+rke2r1` is not.
    bare = value[1:] if value.startswith("v") else value
    if "-" in bare:
        low, _, high = bare.partition("-")
        return low.strip(), high.strip()
    return value, value


def as_tuple(version: str) -> tuple[int, ...]:
    """Numeric parts of a version, so 1.34 and v1.34.11+rke2r1 compare.

    Build metadata (`+rke2r1`) carries digits that are not version parts, so it
    is cut before the parts are read; EKS reports a minor of `34+`, so anything
    that is not a digit run is dropped rather than parsed. That cut also puts
    v1.36.4+rke2r1 and +rke2r2 on the same tuple, so a platform.rke2 floor
    cannot name a build revision -- only a Kubernetes version.
    """
    return tuple(int(p) for p in re.findall(r"\d+", version.partition("+")[0]))


def above_ceiling(actual: tuple[int, ...], ceiling: tuple[int, ...]) -> bool:
    """Compare at the ceiling's own precision: v1.36.4 is not above a 1.36 ceiling."""
    return actual[: len(ceiling)] > ceiling


def cluster_version() -> tuple[str, str]:
    """The target cluster's (major.minor, gitVersion), via kubectl."""
    out = subprocess.run(
        ["kubectl", "version", "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    server = json.loads(out)["serverVersion"]
    return f"{server['major']}.{server['minor']}", server.get("gitVersion", "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default=None, help="Path to versions.yaml")
    parser.add_argument("--stack", default=None, help="Stack to read (default: `current`)")
    parser.add_argument(
        "--actual",
        default=None,
        help="Cluster version to check instead of asking kubectl (a gitVersion "
        "such as v1.36.4+rke2r1 drives the rke2 check too)",
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
        actual, git_version = (args.actual, args.actual) if args.actual else cluster_version()
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

    if ceiling and above_ceiling(actual_t, as_tuple(ceiling)):
        print(
            f"WARNING: cluster is Kubernetes {actual}, above the {ceiling} this "
            f"stack was tested against. Proceeding.",
            file=sys.stderr,
        )

    print(f"platform: Kubernetes {actual} meets {required} (stack {stack_name})")

    # The on-prem half. RKE2 names itself in gitVersion, so it can be checked;
    # the Rancher managing the cluster is invisible from inside it, so
    # platform.rancher stays a declaration.
    rke2_required = platform.get("rke2")
    if rke2_required and "+rke2" in git_version:
        rke2_floor = as_tuple(parse_requirement(rke2_required)[0])
        if as_tuple(git_version) < rke2_floor:
            print(
                f"REFUSED: cluster is RKE2 {git_version}, and stack {stack_name} "
                f"requires rke2 {rke2_required}.",
                file=sys.stderr,
            )
            return 3
        print(f"platform: RKE2 {git_version} meets {rke2_required} (stack {stack_name})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
