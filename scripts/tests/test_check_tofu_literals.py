#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_tofu_literals.py
#  Purpose:      Prove the OpenTofu tree carries no sizing, type, retention,
#                placement or version literal outside the allowed contract
#                constants, and that the check refuses one reintroduced.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The check against the real tree, and against every shape a literal hides in.

    python3 -m pytest scripts/tests/test_check_tofu_literals.py -q
"""

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_tofu_literals as check  # noqa: E402

TOOLBOX_MAIN = Path("terraform") / "modules" / "toolbox" / "aws" / "main.tf"


def literals(text: str) -> list[tuple[str, str, str]]:
    """Each hit in one snippet, as (address, attribute, literal)."""
    return [(hit.address, hit.attribute, hit.literal) for hit in check.scan_text(text, "probe.tf")]


def test_the_tree_carries_no_literal_outside_the_allowed_list() -> None:
    found = [
        f"{hit.path}:{hit.line} {hit.attribute} = {hit.literal}"
        for hit in check.scan_tree()
        if hit.key not in check.ALLOWED
    ]
    assert found == []


def test_every_allowance_still_matches_a_literal() -> None:
    keys = {hit.key for hit in check.scan_tree()}
    assert sorted(set(check.ALLOWED) - keys) == []


def test_every_allowance_states_its_reason() -> None:
    for key, reason in check.ALLOWED.items():
        assert reason.strip().endswith("."), key


def test_the_check_passes_on_the_tree(capsys: pytest.CaptureFixture[str]) -> None:
    assert check.main() == 0
    assert "check_tofu_literals: OK" in capsys.readouterr().out


def _tree_copy(tmp_path: Path) -> Path:
    """The repository's terraform tree, copied without provider caches or lock files."""
    shutil.copytree(
        REPO_ROOT / "terraform",
        tmp_path / "terraform",
        ignore=shutil.ignore_patterns(".terraform", "*.lock.hcl", "*.tfstate*"),
    )
    return tmp_path


def test_a_copy_of_the_tree_passes_unchanged(tmp_path: Path) -> None:
    assert check.main(_tree_copy(tmp_path)) == 0


def test_expected_fail_an_instance_type_put_back_in_a_real_module_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree_copy(tmp_path)
    module = root / TOOLBOX_MAIN
    text = module.read_text(encoding="utf-8")
    assert "instance_type = var.instance_type" in text
    literal = text.replace("instance_type = var.instance_type", 'instance_type = "t4g.small"')
    module.write_text(literal, encoding="utf-8", newline="\n")
    assert check.main(root) == 1
    out = capsys.readouterr().out
    assert f"{TOOLBOX_MAIN.as_posix()}:" in out
    assert "resource.aws_instance.this instance_type = 't4g.small'" in out
    assert "ALLOWED entry matches no literal" not in out


# Every shape a sizing literal takes in this tree, each one refused.
REFUSED = {
    "a resource attribute": (
        'resource "aws_instance" "x" {\n  instance_type = "t4g.small"\n}\n',
        ("resource.aws_instance.x", "instance_type", "t4g.small"),
    ),
    "a nested block": (
        'resource "aws_launch_template" "x" {\n  block_device_mappings {\n    ebs {\n'
        "      volume_size = 20\n    }\n  }\n}\n",
        ("resource.aws_launch_template.x", "block_device_mappings.ebs.volume_size", "20"),
    ),
    "a dynamic block's content": (
        'resource "aws_instance" "x" {\n  dynamic "ebs_block_device" {\n'
        "    for_each = var.disks\n    content {\n      iops = 3000\n    }\n  }\n}\n",
        ("resource.aws_instance.x", "ebs_block_device.content.iops", "3000"),
    ),
    "a variable default": (
        'variable "broker_count" {\n  type    = number\n  default = 3\n}\n',
        ("variable.broker_count", "default", "3"),
    ),
    "an object default": (
        'variable "provision" {\n  default = {\n    region = "us-east-1"\n  }\n}\n',
        ("variable.provision", "default.region", "us-east-1"),
    ),
    "an optional() default": (
        'variable "telemetry" {\n  type = object({\n    retention_days = optional(number, 7)\n  })\n}\n',
        ("variable.telemetry", "type.retention_days", "7"),
    ),
    "a local": (
        "locals {\n  node_count = 3\n}\n",
        ("locals", "node_count", "3"),
    ),
    "a conditional branch": (
        "locals {\n  retention_days = var.ephemeral ? var.short : 30\n}\n",
        ("locals", "retention_days", "30"),
    ),
    "a try() fallback": (
        'resource "aws_instance" "x" {\n  instance_type = try(var.shape, "m8g.large")\n}\n',
        ("resource.aws_instance.x", "instance_type", "m8g.large"),
    ),
    "a lookup() default": (
        'resource "aws_instance" "x" {\n  instance_type = lookup(var.shapes, "toolbox", "t4g.small")\n}\n',
        ("resource.aws_instance.x", "instance_type", "t4g.small"),
    ),
    "a module's object argument": (
        'module "edge" {\n  source = "./edge"\n  tunnel = {\n    instance_type = "t4g.small"\n  }\n}\n',
        ("module.edge", "tunnel.instance_type", "t4g.small"),
    ),
    "a CIDR list": (
        'resource "aws_security_group_rule" "x" {\n  cidr_blocks = ["10.0.0.0/8"]\n}\n',
        ("resource.aws_security_group_rule.x", "cidr_blocks", "10.0.0.0/8"),
    ),
    "an engine version": (
        'resource "aws_msk_cluster" "x" {\n  kafka_version = "4.2.x.kraft"\n}\n',
        ("resource.aws_msk_cluster.x", "kafka_version", "4.2.x.kraft"),
    ),
}


@pytest.mark.parametrize("snippet", REFUSED.values(), ids=REFUSED.keys())
def test_expected_fail_each_hiding_place_is_found(snippet: tuple[str, tuple[str, str, str]]) -> None:
    text, hit = snippet
    assert hit in literals(text)


# Literals that are not a chosen value, each one passed.
PASSED = {
    "a reference": 'resource "aws_instance" "x" {\n  instance_type = var.instance_type\n}\n',
    "an index": "locals {\n  instance_type = var.instance_types[0]\n}\n",
    "a comparison operand": "locals {\n  node_count = length(var.azs) > 3 ? var.many : var.few\n}\n",
    "an on-off switch": "locals {\n  node_count = var.enabled ? 1 : 0\n}\n",
    "a lookup key": 'locals {\n  instance_type = lookup(var.shapes, "toolbox")\n}\n',
    "an empty default": 'variable "kafka_version" {\n  default = ""\n}\n',
    "the any-address range": 'resource "aws_vpc_security_group_egress_rule" "x" {\n  cidr_ipv4 = "0.0.0.0/0"\n}\n',
    "an interpolated string": 'locals {\n  instance_type = "${var.family}.large"\n}\n',
    "a name out of scope": 'resource "aws_instance" "x" {\n  description = "three brokers"\n}\n',
    "a meta-argument": 'resource "aws_instance" "x" {\n  count = 2\n}\n',
}


@pytest.mark.parametrize("text", PASSED.values(), ids=PASSED.keys())
def test_a_value_that_is_not_chosen_is_passed(text: str) -> None:
    assert literals(text) == []


def test_expected_fail_a_construct_the_scanner_cannot_read_stops_it() -> None:
    with pytest.raises(check.ScanError):
        check.scan_text('resource "aws_instance" "x" {\n  instance_type = "t4g.small\n}\n', "probe.tf")
