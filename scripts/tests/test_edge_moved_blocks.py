#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_edge_moved_blocks.py
#  Purpose:      Guard the edge module's state move: every resource that came
#                out of kubernetes-cluster/aws carries a moved {} block in the
#                aws root, so an existing deployment re-addresses rather than
#                destroys and recreates.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every moved resource has a moved {} block, and every block is well aimed.

Without one, OpenTofu reads the old address as a destroy and the new one as a
create. On `aws_route53_zone.public` that is not churn: the recreated zone is
handed a NEW NS set, the parent zone still delegates to the old one, and every
public name stops resolving. The IAM addresses fail the same way in miniature --
a recreated role breaks the Pod Identity association naming it, while the
controller keeps reporting Ready.

    python3 -m pytest scripts/tests/test_edge_moved_blocks.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EDGE_MODULE = REPO_ROOT / "terraform" / "modules" / "edge" / "aws"
MOVED_FILE = REPO_ROOT / "terraform" / "environments" / "aws" / "moved.tf"

# The two bodies that came out of kubernetes-cluster/aws wholesale. A file added
# to the edge module later holds resources that were never anywhere else, so it
# is not listed here and needs no moved block.
MOVED_BODIES = ("lbc.tf", "dns.tf")

OLD_MODULE = "module.cluster."
NEW_MODULE = "module.edge[0]."

RESOURCE = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"', re.M)
MOVED = re.compile(r"moved\s*\{\s*from\s*=\s*([^\s]+)\s*to\s*=\s*([^\s]+)\s*\}")


def _moved_pairs() -> list[tuple[str, str]]:
    return MOVED.findall(MOVED_FILE.read_text(encoding="utf-8"))


def _declared_addresses() -> list[str]:
    """Every resource address the moved bodies declare, module path excluded."""
    found: list[str] = []
    for name in MOVED_BODIES:
        text = (EDGE_MODULE / name).read_text(encoding="utf-8")
        found += [f"{kind}.{label}" for kind, label in RESOURCE.findall(text)]
    return found


def test_the_moved_file_exists_and_carries_blocks() -> None:
    assert MOVED_FILE.is_file(), f"{MOVED_FILE} is the only place both addresses can be named"
    assert _moved_pairs(), "moved.tf declares no moved block at all"


def test_every_moved_resource_has_a_block_naming_it() -> None:
    targets = {to for _from, to in _moved_pairs()}
    missing = [
        address
        for address in _declared_addresses()
        if f"{NEW_MODULE}{address}" not in targets
    ]
    assert not missing, (
        "resources moved out of kubernetes-cluster/aws with no moved block -- OpenTofu "
        "would destroy and recreate each one:\n" + "\n".join(sorted(missing))
    )


def test_every_block_moves_from_the_cluster_module_to_the_edge_module() -> None:
    wrong = [
        f"{old} -> {new}"
        for old, new in _moved_pairs()
        if not old.startswith(OLD_MODULE) or not new.startswith(NEW_MODULE)
    ]
    assert not wrong, (
        "a moved block must run from the cluster module to the edge module:\n" + "\n".join(wrong)
    )


def test_every_block_keeps_the_resource_address_unchanged() -> None:
    """Only the module path moves -- a renamed resource is a second change
    hiding inside a state move, and an index shape that changes with it
    re-keys the instance rather than carrying it."""
    renamed = [
        f"{old} -> {new}"
        for old, new in _moved_pairs()
        if old[len(OLD_MODULE) :] != new[len(NEW_MODULE) :]
    ]
    assert not renamed, "a moved block renames a resource as well as moving it:\n" + "\n".join(renamed)


def test_no_address_is_claimed_twice() -> None:
    olds = [old for old, _new in _moved_pairs()]
    news = [new for _old, new in _moved_pairs()]
    assert len(set(olds)) == len(olds), "two moved blocks read the same old address"
    assert len(set(news)) == len(news), "two moved blocks write the same new address"
