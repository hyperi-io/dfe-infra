#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_cni_eni_teardown.py
#  Purpose:      Prove the EKS module sweeps the VPC CNI's leaked network
#                interfaces after the nodes and vpc-cni are gone and before the
#                cluster is deleted, using only the CNI's own leak filter.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A pod network interface the VPC CNI leaves `available` holds the EKS cluster
security group, so EKS cannot delete that group with the cluster and the VPC
delete fails on it. eks.tf sweeps them with a destroy-time provisioner.

contract.tftest.hcl (aws_cni_eni_teardown_captures_this_vpc) proves what the
sweep captures into its own state. Neither a provisioner block nor a depends_on
edge is a resource attribute, so the command and the destroy order are read
here as text.

    python3 -m pytest scripts/tests/test_cni_eni_teardown.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EKS_TF = REPO_ROOT / "terraform" / "modules" / "kubernetes-cluster" / "aws" / "eks.tf"


def _block(header: str) -> str:
    """The top-level block opening with `header`, up to its closing brace."""
    body = EKS_TF.read_text(encoding="utf-8")
    start = body.index(header)
    return body[start : body.index("\n}\n", start)]


def _depends_on(block: str) -> str:
    found = re.search(r"(?m)^  depends_on\s*=\s*\[([^\]]*)\]", block)
    assert found, f"no depends_on in:\n{block}"
    return found.group(1)


TEARDOWN = 'resource "terraform_data" "cni_eni_teardown"'
VPC_CNI = 'resource "aws_eks_addon" "vpc_cni"'
ADDONS = 'resource "aws_eks_addon" "this"'
NODE_GROUPS = 'resource "aws_eks_node_group" "this"'


def test_the_sweep_is_a_destroy_time_provisioner_fenced_to_this_vpc() -> None:
    block = _block(TEARDOWN)
    assert re.search(r"(?m)^\s*when\s*=\s*destroy\s*$", block), block
    assert "vpc_id = aws_vpc.this.id" in block, block
    # A destroy-time command may reference only self, count.index or each.key.
    assert "${self.output.vpc_id}" in block, block
    assert "${self.output.region}" in block, block


def test_the_sweep_deletes_only_what_the_cni_own_leak_filter_matches() -> None:
    """amazon-vpc-cni-k8s pkg/awsutils/awsutils.go getLeakedENIs sends exactly
    these three filters; anything wider could take an interface the CNI does not
    own."""
    block = _block(TEARDOWN)
    assert '"Name=vpc-id,Values=$vpc"' in block, block
    assert '"Name=tag-key,Values=node.k8s.amazonaws.com/instance_id"' in block, block
    assert "Name=status,Values=available" in block, block
    assert "ec2 delete-network-interface" in block, block
    # Every delete goes through the filtered list, never an id from anywhere else.
    describes = block.count("aws ec2 describe-network-interfaces")
    assert describes == 1, f"one filtered describe, reused for every query; found {describes}"


def test_the_sweep_fails_loudly_instead_of_continuing() -> None:
    """A swallowed failure here comes back as the VPC's DependencyViolation after
    the cluster is already gone."""
    block = _block(TEARDOWN)
    assert "on_failure" not in block, block
    assert "set -eu" in block, block
    assert re.search(r"(?m)^\s*exit 1\s*$", block), block


def test_destroy_order_is_nodes_then_vpc_cni_then_the_sweep_then_the_cluster() -> None:
    """A dependent is destroyed before what it depends on, so each edge below puts
    its own resource's DESTROY ahead of the one it names."""
    assert "aws_eks_addon.vpc_cni" in _depends_on(_block(NODE_GROUPS))
    assert "terraform_data.cni_eni_teardown" in _depends_on(_block(VPC_CNI))
    assert "aws_eks_cluster.this" in _depends_on(_block(TEARDOWN))


def test_the_other_addons_still_wait_for_the_nodes_and_exclude_vpc_cni() -> None:
    block = _block(ADDONS)
    assert "aws_eks_node_group.this" in _depends_on(block)
    assert re.search(r'for_each\s*=\s*setsubtract\(local\.addons,\s*\["vpc-cni"\]\)', block), block


def test_an_existing_vpc_cni_addon_is_moved_not_recreated() -> None:
    body = EKS_TF.read_text(encoding="utf-8")
    assert re.search(
        r'moved\s*\{\s*from\s*=\s*aws_eks_addon\.this\["vpc-cni"\]\s*to\s*=\s*aws_eks_addon\.vpc_cni\s*\}',
        body,
    ), 'no moved block from aws_eks_addon.this["vpc-cni"] to aws_eks_addon.vpc_cni'
