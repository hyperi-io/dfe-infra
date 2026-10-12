#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_receiver_eip_facts.py
#  Purpose:      Prove the receiver NLB's Elastic IPs and their subnets travel
#                from the AWS root through bootstrap and the cluster secret,
#                paired by position, and that the dial's switch reaches tofu.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The receiver's Elastic IPs, from the AWS root to the cluster secret.

The edge module allocates one Elastic IP per public subnet
(terraform/modules/edge/aws/tests asserts the pairing), the root emits both
lists, bootstrap.sh writes them onto the cluster secret, and layer2-apps.yaml
hands them to the receiver (test_appset_oci_sources.py renders that half). The
load balancer controller pairs allocation and subnet by position, so two lists
of different lengths would fail the NLB at creation, after every render passed.

    python3 -m pytest scripts/tests/test_receiver_eip_facts.py -q
"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import render_dial  # noqa: E402
from yaml_subset import parse as parse_dial  # noqa: E402

BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
AWS_ROOT_OUTPUTS = REPO_ROOT / "terraform" / "environments" / "aws" / "outputs.tf"
AWS_ROOT_MAIN = REPO_ROOT / "terraform" / "environments" / "aws" / "main.tf"

EIPS = "eipalloc-0aaaaaaaaaaaaaaa1,eipalloc-0aaaaaaaaaaaaaaa2"
SUBNETS = "subnet-0bbbbbbbbbbbbbbb1,subnet-0bbbbbbbbbbbbbbb2"
EIP_KEY = "dfe.hyperi.io/receiver_eip_allocations"
SUBNETS_KEY = "dfe.hyperi.io/receiver_subnets"


def _bootstrap_block() -> str:
    """bootstrap.sh's own block, run on its own rather than copied here."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = lines.index('DFE_RECEIVER_EIP_ALLOCATIONS="${DFE_RECEIVER_EIP_ALLOCATIONS:-}"')
    end = next(i for i, line in enumerate(lines) if line.startswith("export DFE_RECEIVER_SUBNETS_ANNOTATION="))
    return "\n".join(lines[start : end + 1])


def _run_block(**env: str) -> subprocess.CompletedProcess:
    script = (
        _bootstrap_block()
        + '\nprintf "%s\\n%s\\n" "$DFE_RECEIVER_EIP_ALLOCATIONS_ANNOTATION" "$DFE_RECEIVER_SUBNETS_ANNOTATION"'
    )
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def test_the_aws_root_emits_both_lists_from_the_edge_module() -> None:
    outputs = AWS_ROOT_OUTPUTS.read_text(encoding="utf-8")
    assert 'output "DFE_RECEIVER_EIP_ALLOCATIONS"' in outputs
    assert 'output "DFE_RECEIVER_SUBNETS"' in outputs
    assert "module.edge[*].receiver_eip_allocations" in outputs
    assert "module.edge[*].receiver_subnets" in outputs
    assert "receiver = var.edge.receiver" in AWS_ROOT_MAIN.read_text(encoding="utf-8")


def test_both_lists_write_both_annotations() -> None:
    done = _run_block(DFE_RECEIVER_EIP_ALLOCATIONS=EIPS, DFE_RECEIVER_SUBNETS=SUBNETS)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [f'{EIP_KEY}: "{EIPS}"', f'{SUBNETS_KEY}: "{SUBNETS}"']


def test_neither_writes_neither() -> None:
    done = _run_block()
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["", ""]


def test_spaces_come_out_of_both_lists() -> None:
    """layer2-apps.yaml splits on commas alone, so a space would become part of an id."""
    done = _run_block(DFE_RECEIVER_EIP_ALLOCATIONS=EIPS.replace(",", ", "),
                      DFE_RECEIVER_SUBNETS=SUBNETS.replace(",", ", "))
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [f'{EIP_KEY}: "{EIPS}"', f'{SUBNETS_KEY}: "{SUBNETS}"']


@pytest.mark.parametrize(("eips", "subnets"), [(EIPS, "subnet-0bbbbbbbbbbbbbbb1"), (EIPS, ""), ("", SUBNETS)])
def test_expected_fail_lists_of_different_lengths_are_refused(eips: str, subnets: str) -> None:
    done = _run_block(DFE_RECEIVER_EIP_ALLOCATIONS=eips, DFE_RECEIVER_SUBNETS=subnets)
    assert done.returncode == 1, done.stdout
    assert "one allocation per subnet, in the same order" in done.stderr, done.stderr
    assert done.stdout == ""


def test_the_cluster_secret_carries_both_annotations() -> None:
    rendered = subprocess.run(
        ["envsubst"],
        input=CLUSTER_SECRET.read_text(encoding="utf-8"),
        env={
            "PATH": "/usr/bin:/bin",
            "DFE_RECEIVER_EIP_ALLOCATIONS_ANNOTATION": f'{EIP_KEY}: "{EIPS}"',
            "DFE_RECEIVER_SUBNETS_ANNOTATION": f'{SUBNETS_KEY}: "{SUBNETS}"',
        },
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    annotations = yaml.safe_load(rendered)["metadata"]["annotations"]
    assert annotations[EIP_KEY] == EIPS
    assert annotations[SUBNETS_KEY] == SUBNETS


@pytest.mark.parametrize(("body", "eips"), [("", True), ("edge:\n  aws:\n    receiver_eips: false\n", False)])
def test_the_dial_switch_reaches_the_root(body: str, eips: bool) -> None:
    dial = parse_dial("substrate: k8s\n" + body, source="test-dial")
    assert render_dial._edge(dial)["receiver"] == {"eips": eips}
