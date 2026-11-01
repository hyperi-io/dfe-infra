#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_kms_key_retention.py
#  Purpose:      Prove a persistent deployment's KMS key outlives tofu destroy,
#                and an ephemeral deployment's key does not.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The deployment key encrypts buckets a persistent deployment keeps through a destroy.

contract.tftest.hcl (aws_kms_key_is_retained_for_a_persistent_deployment,
aws_kms_key_goes_with_an_ephemeral_deployment) proves which key each lifecycle
gets. A lifecycle block is not a resource attribute, so whether destroy forgets
the key or schedules its deletion is read here as text.

    python3 -m pytest scripts/tests/test_kms_key_retention.py -q
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
KMS_TF = REPO_ROOT / "terraform" / "modules" / "kubernetes-cluster" / "aws" / "kms.tf"

_DESTROY_FALSE = re.compile(r"(?m)^    destroy\s*=\s*false\s*$")


def _block(name: str) -> str:
    """The top-level `aws_kms_key` resource named `name`, up to its closing brace."""
    body = KMS_TF.read_text(encoding="utf-8")
    start = body.index(f'resource "aws_kms_key" "{name}"')
    return body[start : body.index("\n}\n", start)]


@pytest.mark.parametrize(
    ("name", "forgotten"),
    [
        pytest.param("retained", True, id="persistent-key-is-forgotten"),
        pytest.param("ephemeral", False, id="ephemeral-key-is-deleted"),
    ],
)
def test_destroy_forgets_only_the_persistent_key(name: str, forgotten: bool) -> None:
    assert bool(_DESTROY_FALSE.search(_block(name))) is forgotten


def test_the_roots_retained_report_names_the_forgotten_key() -> None:
    """dfe-ops teardown reports what a destroy kept from this output, so it names the real address."""
    outputs = (REPO_ROOT / "terraform" / "environments" / "aws" / "outputs.tf").read_text(encoding="utf-8")
    assert '"module.cluster.aws_kms_key.retained[0]"' in outputs


def test_no_other_key_resource_escapes_the_split() -> None:
    names = re.findall(r'(?m)^resource "aws_kms_key" "([^"]+)"', KMS_TF.read_text(encoding="utf-8"))
    assert sorted(names) == ["ephemeral", "retained"]
