#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         check_tools.py
#  Purpose:      Refuse a deploy before it starts when a command bootstrap.sh
#                or destroy.sh runs is not on PATH
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Check that every command bootstrap.sh and destroy.sh run is on PATH.

A deploy that finds a tool missing halfway has already built what came before
it, and on a cloud that is a whole cluster. bootstrap.sh runs this before its
first cluster call, and `dfe-ops cloud-preflight` runs it before `tofu apply`,
so a machine without one is refused while nothing exists yet.

The list is the one place the tools are named. Coreutils, grep and bash itself
are taken as given.

Usage:
    python3 bootstrap/check_tools.py                # cloud from DFE_CLOUD
    python3 bootstrap/check_tools.py --cloud aws

Exit 0 when every tool is on PATH, 1 naming each missing one and what needs it.
"""

import argparse
import os
import shutil
import sys

# Every deployment, whatever its cloud.
TOOLS: tuple[tuple[str, str], ...] = (
    ("kubectl", "every cluster call bootstrap.sh and destroy.sh make"),
    ("helm", "every chart install in bootstrap.sh and uninstall in destroy.sh"),
    ("python3", "the helpers both scripts call, from reading versions.yaml to rendering templates"),
    ("openssl", "mints the bundled deploy repo's admin password and webhook secret"),
)

# Only where DFE_CLOUD names the cloud.
CLOUD_TOOLS: dict[str, tuple[tuple[str, str], ...]] = {
    "aws": (
        ("aws", "destroy.sh step 9 terminates a test run's leftover nodes and security groups"),
    ),
}


def required(cloud: str) -> tuple[tuple[str, str], ...]:
    """Every (tool, what needs it) pair a deploy on `cloud` runs."""
    return TOOLS + CLOUD_TOOLS.get(cloud, ())


def missing(cloud: str) -> list[tuple[str, str]]:
    """The (tool, what needs it) pairs not found on this process's PATH, in list order.

    Args:
        cloud: DFE_CLOUD's value; empty checks only the tools every cloud needs.

    Returns:
        Each missing tool with what needs it; empty when nothing is missing.
    """
    return [(tool, why) for tool, why in required(cloud) if shutil.which(tool) is None]


def main(argv: list[str] | None = None) -> int:
    """Print what is missing and exit 1, or name what was found and exit 0."""
    parser = argparse.ArgumentParser(
        description="Check that every command bootstrap.sh and destroy.sh run is on PATH."
    )
    parser.add_argument(
        "--cloud",
        default=os.environ.get("DFE_CLOUD", ""),
        help="the deployment's cloud, which adds its own CLI (default: DFE_CLOUD)",
    )
    args = parser.parse_args(argv)
    absent = missing(args.cloud)
    if absent:
        print("not on PATH, and a deploy cannot finish without them:", file=sys.stderr)
        for tool, why in absent:
            print(f"  {tool}: {why}", file=sys.stderr)
        return 1
    print(f"deploy tools on PATH: {' '.join(tool for tool, _ in required(args.cloud))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
