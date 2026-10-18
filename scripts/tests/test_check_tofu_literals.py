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
EKS = Path("terraform") / "modules" / "kubernetes-cluster" / "aws" / "eks.tf"
FORWARDER = Path("terraform") / "modules" / "edge" / "aws" / "forwarder.tf"


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


def test_expected_fail_an_eks_ami_type_put_back_in_the_cluster_module_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree_copy(tmp_path)
    module = root / EKS
    text = module.read_text(encoding="utf-8")
    assert "ami_type = var.node_image.family" in text
    literal = text.replace("ami_type = var.node_image.family", 'ami_type = "AL2023_ARM_64_STANDARD"')
    module.write_text(literal, encoding="utf-8", newline="\n")
    assert check.main(root) == 1
    out = capsys.readouterr().out
    assert "ami_type = 'AL2023_ARM_64_STANDARD' -- a node image resolves through" in out


def test_expected_fail_an_ssm_image_lookup_put_back_in_tofu_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The data source the resolver replaced, under a name the sizing scope never reads."""
    root = _tree_copy(tmp_path)
    module = root / FORWARDER
    text = module.read_text(encoding="utf-8")
    assert "ami           = var.tunnel.address.ami_id" in text
    lookup = (
        'data "aws_ssm_parameter" "al2023_arm64" {\n'
        '  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"\n'
        "}\n"
    )
    module.write_text(text + "\n" + lookup, encoding="utf-8", newline="\n")
    assert check.main(root) == 1
    out = capsys.readouterr().out
    assert "data.aws_ssm_parameter.al2023_arm64 name = '/aws/service/ami-amazon-linux-latest/" in out


def test_the_node_image_default_is_the_one_image_literal_in_the_tree() -> None:
    images = [hit.key for hit in check.scan_tree() if hit.kind == "image"]
    assert images == ["terraform/environments/aws/variables.tf:variable.node_image:default.family"]


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
    "an AMI id": (
        'resource "aws_instance" "x" {\n  ami = "ami-0123456789abcdef0"\n}\n',
        ("resource.aws_instance.x", "ami", "ami-0123456789abcdef0"),
    ),
    "an EKS AMI type in a local": (
        'locals {\n  node_ami_type = "AL2023_ARM_64_STANDARD"\n}\n',
        ("locals", "node_ami_type", "AL2023_ARM_64_STANDARD"),
    ),
    "a node image under a name out of scope": (
        'resource "aws_eks_node_group" "x" {\n  labels = { os = "BOTTLEROCKET_ARM_64" }\n}\n',
        ("resource.aws_eks_node_group.x", "labels.os", "BOTTLEROCKET_ARM_64"),
    ),
    "an image SSM parameter path": (
        'data "aws_ssm_parameter" "x" {\n'
        '  name = "/aws/service/eks/optimized-ami/1.36/amazon-linux-2023/arm64/standard/recommended"\n'
        "}\n",
        (
            "data.aws_ssm_parameter.x",
            "name",
            "/aws/service/eks/optimized-ami/1.36/amazon-linux-2023/arm64/standard/recommended",
        ),
    ),
    "an SSM image reference": (
        'resource "aws_launch_template" "x" {\n'
        '  image_id = "resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"\n'
        "}\n",
        (
            "resource.aws_launch_template.x",
            "image_id",
            "resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64",
        ),
    ),
    "a Karpenter alias": (
        'locals {\n  alias = "al2023@v20261012"\n}\n',
        ("locals", "alias", "al2023@v20261012"),
    ),
    "a GKE image type": (
        'resource "google_container_node_pool" "x" {\n  node_config {\n'
        '    image_type = "COS_CONTAINERD"\n  }\n}\n',
        ("resource.google_container_node_pool.x", "node_config.image_type", "COS_CONTAINERD"),
    ),
    "an AKS OS SKU": (
        'resource "azurerm_kubernetes_cluster_node_pool" "x" {\n  os_sku = "AzureLinux"\n}\n',
        ("resource.azurerm_kubernetes_cluster_node_pool.x", "os_sku", "AzureLinux"),
    ),
    "a node image variable default": (
        'variable "node_image" {\n  default = { family = "AL2023_ARM_64_STANDARD" }\n}\n',
        ("variable.node_image", "default.family", "AL2023_ARM_64_STANDARD"),
    ),
    "an AMI data source": (
        'data "aws_ami" "x" {\n  most_recent = true\n}\n',
        ("data.aws_ami.x", "data source", "aws_ami"),
    ),
    "an SSM parameter data source, whatever it names": (
        'data "aws_ssm_parameter" "x" {\n  name = var.parameter\n}\n',
        ("data.aws_ssm_parameter.x", "data source", "aws_ssm_parameter"),
    ),
    "an SSM path data source": (
        'data "aws_ssm_parameters_by_path" "x" {\n  path = var.path\n}\n',
        ("data.aws_ssm_parameters_by_path.x", "data source", "aws_ssm_parameters_by_path"),
    ),
    "an AMI id in upper-case hex, under a name out of scope": (
        'locals {\n  base = "ami-0123456789ABCDEF0"\n}\n',
        ("locals", "base", "ami-0123456789ABCDEF0"),
    ),
    "Canonical's Ubuntu SSM path": (
        'locals {\n  base = "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"\n}\n',
        ("locals", "base", "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"),
    ),
    "Debian's SSM path": (
        'locals {\n  base = "/aws/service/debian/release/12/latest/arm64"\n}\n',
        ("locals", "base", "/aws/service/debian/release/12/latest/arm64"),
    ),
    "SUSE's SSM path": (
        'locals {\n  base = "/aws/service/suse/sles/15-sp6/arm64/latest"\n}\n',
        ("locals", "base", "/aws/service/suse/sles/15-sp6/arm64/latest"),
    ),
    "the ECS-optimised SSM path": (
        'locals {\n  base = "/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended"\n}\n',
        ("locals", "base", "/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended"),
    ),
    "an AMI id in a heredoc script": (
        'resource "aws_instance" "x" {\n  user_data = <<-EOT\n    #!/bin/bash\n'
        "    aws ec2 run-instances --image-id=ami-0123456789abcdef0\n  EOT\n}\n",
        ("resource.aws_instance.x", "user_data", "ami-0123456789abcdef0"),
    ),
    "a Karpenter alias in a heredoc manifest": (
        'resource "kubectl_manifest" "x" {\n  yaml_body = <<-YAML\n    spec:\n'
        "      amiSelectorTerms:\n        - alias: al2023@v20261012\n  YAML\n}\n",
        ("resource.kubectl_manifest.x", "yaml_body", "al2023@v20261012"),
    ),
    "an AMI id in a heredoc JSON document": (
        'locals {\n  launch = <<EOT\n{"ImageId":"ami-0123456789abcdef0"}\nEOT\n}\n',
        ("locals", "launch", "ami-0123456789abcdef0"),
    ),
}


@pytest.mark.parametrize("snippet", REFUSED.values(), ids=REFUSED.keys())
def test_expected_fail_each_hiding_place_is_found(snippet: tuple[str, tuple[str, str, str]]) -> None:
    text, hit = snippet
    assert hit in literals(text)


def test_expected_fail_an_image_in_a_heredoc_is_reported_on_its_own_line() -> None:
    text = REFUSED["a Karpenter alias in a heredoc manifest"][0]
    (hit,) = check.scan_text(text, "probe.tf")
    assert hit.line == text.splitlines().index("        - alias: al2023@v20261012") + 1


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
    "an image from the cascade": 'resource "aws_instance" "x" {\n  ami = var.ami_id\n}\n',
    "an image family checked by its suffix": (
        'variable "node_image" {\n  validation {\n'
        '    condition = can(regex("_ARM_64(_|$)", var.node_image.family))\n  }\n}\n'
    ),
    "an image name inside prose": (
        'variable "node_image" {\n  description = "EKS AMI type, AL2023_ARM_64_STANDARD by default"\n}\n'
    ),
    "an image path in a heredoc description": (
        'variable "ami_id" {\n  description = <<-DESCRIPTION\n'
        "    Read from /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64, else tfvars.\n"
        "  DESCRIPTION\n}\n"
    ),
    "an AMI id in a heredoc error message": (
        'variable "ami_id" {\n  validation {\n    condition = var.ami_id != null\n'
        "    error_message = <<-EOT\n      Set one such as ami-0123456789abcdef0 in tfvars\n    EOT\n  }\n}\n"
    ),
    "a heredoc that names no image": (
        'resource "aws_instance" "x" {\n  user_data = <<-EOT\n    #!/bin/bash\n'
        "    echo cosine ubuntu-like ${var.ami_id}\n  EOT\n}\n"
    ),
}


@pytest.mark.parametrize("text", PASSED.values(), ids=PASSED.keys())
def test_a_value_that_is_not_chosen_is_passed(text: str) -> None:
    assert literals(text) == []


def test_expected_fail_a_construct_the_scanner_cannot_read_stops_it() -> None:
    with pytest.raises(check.ScanError):
        check.scan_text('resource "aws_instance" "x" {\n  instance_type = "t4g.small\n}\n', "probe.tf")
