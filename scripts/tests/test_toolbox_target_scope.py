#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_toolbox_target_scope.py
#  Purpose:      Prove every toolbox forward target the aws root builds says
#                which side of the VPC boundary its address sits on, so an
#                off-VPC endpoint cannot inherit a VPC-scoped egress rule.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The root half of the toolbox `targets` contract.

The module's own `tofu test` hands itself a targets map, so it can only ever
prove the module is self-consistent with whatever that fixture says. What
decides a real deployment is the map the aws root computes, and a target added
there with no `scope` takes the module default -- correct for an address inside
the VPC, and a rule pointing at the wrong network for one outside it.

    python3 -m pytest scripts/tests/test_toolbox_target_scope.py -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AWS_ROOT = REPO_ROOT / "terraform" / "environments" / "aws" / "main.tf"
TOOLBOX = REPO_ROOT / "terraform" / "modules" / "toolbox" / "aws" / "variables.tf"

ASSIGNMENT = r"^\s*{}\s*="


def target_locals() -> str:
    """The slice of the root that builds the toolbox targets map."""
    body = AWS_ROOT.read_text(encoding="utf-8")
    start = body.index("toolbox_eks_api_target")
    return body[start : body.index("toolbox_targets = merge(", start)]


def test_every_target_the_root_builds_names_its_scope() -> None:
    slice_ = target_locals()
    ports = re.findall(ASSIGNMENT.format("port"), slice_, re.MULTILINE)
    scopes = re.findall(ASSIGNMENT.format("scope"), slice_, re.MULTILINE)
    expect("the root builds at least the api, broker and ClickHouse targets",
           len(ports) >= 3, f"{len(ports)} port assignments")
    expect("every target that names a port names a scope beside it",
           len(ports) == len(scopes), f"{len(ports)} ports vs {len(scopes)} scopes")


def test_an_unstated_scope_means_the_vpc() -> None:
    """The count check above is only worth anything while the default it
    guards against is the narrow one."""
    body = TOOLBOX.read_text(encoding="utf-8")
    declared = re.search(r'scope\s*=\s*optional\(string,\s*"([a-z]+)"\)', body)
    expect("the module declares a default scope", declared is not None, "no optional() default found")
    expect("and it is vpc", declared.group(1) == "vpc", declared.group(1))


def main() -> int:
    with standalone():
        test_every_target_the_root_builds_names_its_scope()
        test_an_unstated_scope_means_the_vpc()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
