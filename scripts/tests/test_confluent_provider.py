#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_confluent_provider.py
#  Purpose:      Hold the aws root's Confluent provider to a retry count that
#                rides out the cluster's REST endpoint resetting connections.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The retry count the aws root's provider "confluent" block sets.

    python3 -m pytest scripts/tests/test_confluent_provider.py -q

The manager API key's readiness wait lists topics with the new key, and one
transport error its HTTP client gives up on fails the apply. That client is
go-retryablehttp, and the provider's max_retries is its retry count.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AWS_ROOT = REPO_ROOT / "terraform" / "environments" / "aws"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_tofu_literals  # noqa: E402

# terraform-provider-confluent's rateLimitedAPIMinMaxRetries: its own IAM, API key and RBAC clients retry at least this often.
PROVIDER_CONTROL_PLANE_FLOOR = 12


def _root_blocks() -> list[check_tofu_literals.Block]:
    """Every top-level block of the aws root's own .tf files."""
    return [
        block
        for path in sorted(AWS_ROOT.glob("*.tf"))
        for block in check_tofu_literals.parse(path.read_text(encoding="utf-8"))
    ]


def _local_number(blocks: list[check_tofu_literals.Block], name: str) -> int:
    """The literal number a named local holds."""
    for block in blocks:
        if block.kind != "locals":
            continue
        for attribute in block.attributes:
            if attribute.name == name:
                value = [token for token in attribute.tokens if token.kind != "newline"]
                if len(value) != 1 or value[0].kind != "number":
                    raise ValueError(f"local.{name} is not a literal number")
                return int(value[0].text)
    raise LookupError(f"local.{name} is set in no locals block")


def _max_retries(blocks: list[check_tofu_literals.Block]) -> int | None:
    """The confluent provider's max_retries, read through one named local, or None when it is left unset."""
    provider = next((b for b in blocks if b.kind == "provider" and b.labels == ["confluent"]), None)
    setting = (
        None
        if provider is None
        else next((a for a in provider.attributes if a.name == "max_retries"), None)
    )
    if setting is None:
        return None
    texts = [token.text for token in setting.tokens if token.kind != "newline"]
    if len(texts) == 3 and texts[:2] == ["local", "."]:
        return _local_number(blocks, texts[2])
    raise ValueError(f"max_retries is {' '.join(texts)}, not a named local")


def test_the_confluent_provider_retries_at_least_its_own_control_plane_floor() -> None:
    retries = _max_retries(_root_blocks())
    assert retries is not None, (
        'provider "confluent" leaves max_retries at the provider\'s default of 4'
    )
    assert retries >= PROVIDER_CONTROL_PLANE_FLOOR


def test_a_provider_block_with_no_retry_count_reads_as_unset() -> None:
    assert _max_retries(check_tofu_literals.parse('provider "confluent" {}\n')) is None
