#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_kafka_module_ordering.py
#  Purpose:      Hold the managed-broker module's ordering to explicit
#                references, so it builds beside the cluster and not after it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What orders the managed broker against the cluster.

A module-wide `depends_on` is the coarsest instrument OpenTofu has: it orders
the module behind EVERY resource in the target, not the one it needs. On the
aws root that put the deployment's longest-running resource -- the broker --
behind the control plane, three node groups and every addon, none of which a
broker reads, and every one of those minutes bills.

The ordering that IS needed is narrow: the key policy carries the grant a
broker's first log delivery to an encrypted sink uses, and the key's own
resource says nothing about its policy. That belongs on the kms_key_arn output,
where it orders a reader against the grants alone.

`aws_eks_pod_identity_association.bootstrap` is the one resource in the broker
module that needs a live EKS cluster, and its own reference to the cluster name
orders it correctly without help.

    python3 scripts/tests/test_kafka_module_ordering.py

No test runner, matching the other checks here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AWS_ROOT = REPO_ROOT / "terraform" / "environments" / "aws" / "main.tf"
CLUSTER_OUTPUTS = REPO_ROOT / "terraform" / "modules" / "kubernetes-cluster" / "aws" / "outputs.tf"
MSK = REPO_ROOT / "terraform" / "modules" / "managed-kafka" / "msk"

# Every input the broker module takes from the cluster module. Each one is an
# explicit reference, which orders the broker against that resource alone.
CLUSTER_INPUTS = (
    "network          = module.cluster.network",
    "eks_cluster_name = module.cluster.cluster_name",
    "kms_key_arn      = module.cluster.kms_key_arn",
    "pod_identity_trust_policy_json = module.cluster.pod_identity_trust_policy_json",
)


def module_block(body: str, name: str) -> str:
    """One `module "<name>" { ... }` block, to its closing brace at column 0."""
    match = re.search(rf'(?ms)^module "{re.escape(name)}" \{{\n(.*?)^\}}', body)
    if match is None:
        raise SystemExit(f"no module block named {name!r} in {AWS_ROOT}")
    return match.group(1)


def strip_comments(body: str) -> str:
    return "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("//"))


def test_the_broker_module_carries_no_module_wide_depends_on() -> None:
    block = strip_comments(module_block(AWS_ROOT.read_text(encoding="utf-8"), "kafka"))
    expect(
        "module.kafka declares no depends_on",
        "depends_on" not in block,
        "a module-wide depends_on holds the broker behind the whole cluster",
    )


def test_no_module_in_the_root_depends_on_the_whole_cluster() -> None:
    body = strip_comments(AWS_ROOT.read_text(encoding="utf-8"))
    offenders = [ln.strip() for ln in body.splitlines() if "depends_on" in ln and "module.cluster" in ln]
    expect("nothing in the aws root waits on all of module.cluster", offenders == [],
           f"got {offenders}")


def test_every_cluster_fact_the_broker_takes_is_an_explicit_reference() -> None:
    block = module_block(AWS_ROOT.read_text(encoding="utf-8"), "kafka")
    for line in CLUSTER_INPUTS:
        expect(f"module.kafka takes {line.split('=')[0].strip()} by reference",
               line in block, f"looked for: {line}")


def test_the_key_arn_output_orders_against_the_key_policy() -> None:
    """Without this the broker could apply before the grants exist, which is
    the one ordering the module-wide depends_on was standing in for."""
    body = CLUSTER_OUTPUTS.read_text(encoding="utf-8")
    match = re.search(r'(?ms)^output "kms_key_arn" \{\n(.*?)^\}', body)
    expect("the cluster module still exports kms_key_arn", match is not None, "output is gone")
    assert match is not None
    expect("kms_key_arn orders against aws_kms_key_policy.this",
           "depends_on = [aws_kms_key_policy.this]" in match.group(1),
           "the grant ordering has nothing carrying it")


def test_the_cluster_name_reaches_only_the_pod_identity_association() -> None:
    """The one resource in the broker module that needs a live EKS cluster --
    anything else reading it would re-serialise the broker behind the API."""
    readers = [
        path.name
        for path in sorted(MSK.glob("*.tf"))
        if "var.eks_cluster_name" in path.read_text(encoding="utf-8")
    ]
    expect("bootstrap.tf is the only consumer of eks_cluster_name",
           readers == ["bootstrap.tf"], f"got {readers}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
