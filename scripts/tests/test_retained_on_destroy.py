#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_retained_on_destroy.py
#  Purpose:      Prove a persistent deployment's KMS key and buckets outlive
#                tofu destroy, and an ephemeral deployment's do not.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What a persistent deployment's tofu destroy forgets rather than deletes.

The contract tests (kubernetes-cluster, s3-bucket, the aws root) prove which
variant each lifecycle gets. A lifecycle block is not a resource attribute, so
whether destroy forgets a resource or deletes it is read here as text.

    python3 -m pytest scripts/tests/test_retained_on_destroy.py -q
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TERRAFORM = REPO_ROOT / "terraform"
KMS_TF = TERRAFORM / "modules" / "kubernetes-cluster" / "aws" / "kms.tf"
BUCKET_TF = TERRAFORM / "modules" / "s3-bucket" / "main.tf"
TOOLBOX = TERRAFORM / "modules" / "toolbox" / "aws"

_DESTROY_FALSE = re.compile(r"(?m)^    destroy\s*=\s*false\s*$")
_RESOURCE = re.compile(r'(?m)^resource "([^"]+)" "([^"]+)" \{')


def _block(path: Path, kind: str, name: str) -> str:
    """The top-level resource `kind.name` in `path`, up to its closing brace."""
    body = path.read_text(encoding="utf-8")
    start = body.index(f'resource "{kind}" "{name}"')
    return body[start : body.index("\n}\n", start)]


@pytest.mark.parametrize(
    ("name", "forgotten"),
    [
        pytest.param("retained", True, id="persistent-key-is-forgotten"),
        pytest.param("ephemeral", False, id="ephemeral-key-is-deleted"),
    ],
)
def test_destroy_forgets_only_the_persistent_key(name: str, forgotten: bool) -> None:
    assert bool(_DESTROY_FALSE.search(_block(KMS_TF, "aws_kms_key", name))) is forgotten


def test_no_other_key_resource_escapes_the_split() -> None:
    names = re.findall(r'(?m)^resource "aws_kms_key" "([^"]+)"', KMS_TF.read_text(encoding="utf-8"))
    assert sorted(names) == ["ephemeral", "retained"]


def test_every_bucket_resource_comes_as_a_forgotten_and_a_deleted_pair() -> None:
    """A retained bucket that lost its public-access block or encryption on destroy is not kept."""
    found = _RESOURCE.findall(BUCKET_TF.read_text(encoding="utf-8"))
    kinds = sorted({kind for kind, _name in found})
    assert sorted(found) == sorted((kind, name) for kind in kinds for name in ("ephemeral", "retained"))
    for kind in kinds:
        assert _DESTROY_FALSE.search(_block(BUCKET_TF, kind, "retained")), kind
        assert not _DESTROY_FALSE.search(_block(BUCKET_TF, kind, "ephemeral")), kind


def test_the_bucket_module_reports_the_address_it_forgets() -> None:
    outputs = (TERRAFORM / "modules" / "s3-bucket" / "outputs.tf").read_text(encoding="utf-8")
    assert '"aws_s3_bucket.retained[0]"' in outputs


def test_the_cluster_reports_the_key_it_forgets() -> None:
    outputs = (TERRAFORM / "modules" / "kubernetes-cluster" / "aws" / "outputs.tf").read_text(encoding="utf-8")
    assert '"aws_kms_key.retained[0]"' in outputs


def test_bastion_down_cannot_reach_the_session_log_bucket() -> None:
    """`bastion down` is a destroy targeted at module.toolbox, so no bucket may live in it."""
    found = [m for path in sorted(TOOLBOX.glob("*.tf")) for m in _RESOURCE.findall(path.read_text(encoding="utf-8"))]
    assert not [kind for kind, _name in found if kind.startswith("aws_s3_bucket")]
