#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_node_images.py
#  Purpose:      Prove the node-image cascade's lookups against recorded
#                provider responses, and that every failure falls through.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/node_images.py and ``resolve_sizing.py images``.

The AWS fixtures are the verbatim ``--query Parameter.Value --output text``
answers of us-west-2's public SSM parameters, captured 2026-10-18. The GKE and
AKS fixtures are the documented response shapes with illustrative values, each
saying so in its own ``_provenance`` field.

    python3 -m pytest scripts/tests/test_node_images.py -q
"""

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import node_images  # noqa: E402
import resolve_sizing  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "node-images"
RECORDED = node_images.fixture_reader(FIXTURES)
AWS_ROOT_VARIABLES = REPO_ROOT / "terraform" / "environments" / "aws" / "variables.tf"

AWS = node_images.Target("aws", "us-west-2", "000000000000", "1.36", "arm64")
GCP = node_images.Target("gcp", "us-central1", "example-project", "1.36", "arm64")
AZURE = node_images.Target(
    "azure", "westus2", "00000000-0000-0000-0000-000000000000", "1.36", "arm64"
)


def _recorded(tmp_path: Path, name: str, body: str) -> node_images.Reader:
    """A reader over one recorded response, rewritten for the case under test."""
    (tmp_path / name).write_text(body, encoding="utf-8")
    return node_images.fixture_reader(tmp_path)


# --- aws ---------------------------------------------------------------------


def test_eks_takes_the_recommended_al2023_type_and_its_dated_release() -> None:
    assert node_images.eks_node_image(AWS, RECORDED) == {
        "family": "AL2023_ARM_64_STANDARD",
        "version": "1.36.4-20261012",
    }


def test_eks_on_amd64_shapes_takes_the_x86_type() -> None:
    target = node_images.Target("aws", "us-west-2", "000000000000", "1.36", "amd64")
    assert node_images.eks_node_image(target, RECORDED)["family"] == "AL2023_x86_64_STANDARD"


def test_the_eks_lookup_reads_the_minors_own_recommended_parameter() -> None:
    calls: list[tuple[str, str, list[str]]] = []

    def record(fixture: str, tool: str, args: list[str]) -> str:
        calls.append((fixture, tool, args))
        return RECORDED(fixture, tool, args)

    node_images.eks_node_image(AWS, record)
    _fixture, tool, args = calls[0]
    assert tool == "aws"
    assert args[args.index("--name") + 1] == (
        "/aws/service/eks/optimized-ami/1.36/amazon-linux-2023/arm64/standard/recommended"
    )
    assert args[args.index("--region") + 1] == "us-west-2"


def test_karpenter_alias_is_the_dated_release_never_latest() -> None:
    assert node_images.karpenter_ami_alias(AWS, RECORDED) == "al2023@v20261012"


def test_an_instance_takes_the_newest_al2023_ami_in_the_region() -> None:
    assert node_images.instance_ami(AWS, RECORDED) == "ami-0957e38de3db270cc"


@pytest.mark.parametrize(
    ("release", "image_name", "match"),
    [
        ("1.35.9-20261012", "amazon-eks-node-al2023-arm64-standard-1.35-v20261012", "not a 1.36"),
        ("1.36.4", "amazon-eks-node-al2023-arm64-standard-1.36-v20261012", "not <patch>"),
        ("", "amazon-eks-node-al2023-arm64-standard-1.36-v20261012", "not <patch>"),
        ("1.36.4-20261012", "amazon-eks-node-al2023-arm64-standard-1.36-v20261003", "does not carry"),
    ],
    ids=["another-minor", "undated", "missing", "name-disagrees"],
)
def test_expected_fail_an_eks_answer_that_does_not_hold_together_is_refused(
    tmp_path: Path, release: str, image_name: str, match: str
) -> None:
    body = json.dumps({"release_version": release, "image_name": image_name})
    read = _recorded(tmp_path, "aws-us-west-2-eks-recommended-1.36-arm64.json", body)
    with pytest.raises(node_images.ImageLookupError, match=match):
        node_images.eks_node_image(AWS, read)


def test_expected_fail_an_eks_answer_that_is_not_json_is_refused(tmp_path: Path) -> None:
    read = _recorded(tmp_path, "aws-us-west-2-eks-recommended-1.36-arm64.json", "ami-0123")
    with pytest.raises(node_images.ImageLookupError, match="not JSON"):
        node_images.eks_node_image(AWS, read)


def test_expected_fail_a_minor_with_no_recommended_image_is_a_lookup_failure() -> None:
    target = node_images.Target("aws", "us-west-2", "000000000000", "1.99", "arm64")
    with pytest.raises(node_images.ImageLookupError, match="no recorded response"):
        node_images.eks_node_image(target, RECORDED)


def test_expected_fail_a_version_that_is_not_a_minor_is_refused_before_any_read() -> None:
    target = node_images.Target("aws", "us-west-2", "000000000000", "1.34-1.36", "arm64")
    with pytest.raises(node_images.ImageLookupError, match="not a Kubernetes minor"):
        node_images.eks_node_image(target, RECORDED)


def test_expected_fail_an_arch_aws_publishes_no_image_for_is_refused() -> None:
    target = node_images.Target("aws", "us-west-2", "000000000000", "1.36", "riscv64")
    with pytest.raises(node_images.ImageLookupError, match="no image for arch"):
        node_images.instance_ami(target, RECORDED)


def test_expected_fail_an_instance_answer_that_is_not_an_ami_is_refused(tmp_path: Path) -> None:
    read = _recorded(tmp_path, "aws-us-west-2-al2023-ami-arm64.txt", "None\n")
    with pytest.raises(node_images.ImageLookupError, match="not an AMI id"):
        node_images.instance_ami(AWS, read)


# --- gcp ---------------------------------------------------------------------


def test_gke_takes_the_server_configs_default_image_type() -> None:
    assert node_images.gke_node_image(GCP, RECORDED) == {"family": "COS_CONTAINERD"}


def test_the_gke_lookup_asks_for_the_location_and_the_project() -> None:
    calls: list[list[str]] = []

    def record(fixture: str, tool: str, args: list[str]) -> str:
        calls.append([tool, *args])
        return RECORDED(fixture, tool, args)

    node_images.gke_node_image(GCP, record)
    assert calls[0][:3] == ["gcloud", "container", "get-server-config"]
    assert calls[0][calls[0].index("--location") + 1] == "us-central1"
    assert calls[0][calls[0].index("--project") + 1] == "example-project"


def test_expected_fail_a_gke_default_outside_the_valid_list_is_refused(tmp_path: Path) -> None:
    doc = json.loads(
        (FIXTURES / "gcp-us-central1-gke-server-config.json").read_text(encoding="utf-8")
    )
    doc["validImageTypes"] = ["UBUNTU_CONTAINERD"]
    read = _recorded(tmp_path, "gcp-us-central1-gke-server-config.json", json.dumps(doc))
    with pytest.raises(node_images.ImageLookupError, match="outside validImageTypes"):
        node_images.gke_node_image(GCP, read)


def test_expected_fail_a_minor_gke_does_not_offer_is_refused() -> None:
    target = node_images.Target("gcp", "us-central1", "example-project", "1.37", "arm64")
    with pytest.raises(node_images.ImageLookupError, match=r"no 1\.37 node version"):
        node_images.gke_node_image(target, RECORDED)


# --- azure -------------------------------------------------------------------


def test_aks_takes_the_version_tracking_ubuntu_sku_on_a_ga_minor() -> None:
    assert node_images.aks_node_image(AZURE, RECORDED) == {"family": "Ubuntu"}


@pytest.mark.parametrize(
    ("minor", "match"),
    [("1.37", "preview only"), ("1.33", "lists no 1.33")],
    ids=["preview", "absent"],
)
def test_expected_fail_a_minor_aks_does_not_offer_generally_is_refused(minor: str, match: str) -> None:
    target = node_images.Target("azure", "westus2", AZURE.account, minor, "arm64")
    with pytest.raises(node_images.ImageLookupError, match=match):
        node_images.aks_node_image(target, RECORDED)


def test_expected_fail_an_aks_answer_with_no_values_is_refused(tmp_path: Path) -> None:
    read = _recorded(tmp_path, "azure-westus2-aks-versions.json", '{"orchestrators": []}')
    with pytest.raises(node_images.ImageLookupError, match="no values list"):
        node_images.aks_node_image(AZURE, read)


# --- the cascade -------------------------------------------------------------


def test_aws_resolves_every_key_its_root_declares() -> None:
    warnings: list[str] = []
    values = node_images.resolve(AWS, pinned=False, read=RECORDED, warn=warnings.append)
    assert values == {
        "node_image": {"family": "AL2023_ARM_64_STANDARD", "version": "1.36.4-20261012"},
        "instance_ami_id": "ami-0957e38de3db270cc",
        "forwarder_ami_id": "ami-0957e38de3db270cc",
    }
    assert warnings == []


def test_every_key_a_lookup_writes_is_one_the_aws_root_declares() -> None:
    text = AWS_ROOT_VARIABLES.read_text(encoding="utf-8")
    declared = set(re.findall(r'^variable "([^"]+)"', text, re.MULTILINE))
    assert {key for key, _lookup in node_images.LOOKUPS["aws"]} <= declared


def test_the_roots_last_resort_default_is_the_type_the_lookup_writes_for_arm64() -> None:
    text = AWS_ROOT_VARIABLES.read_text(encoding="utf-8")
    block = text[text.index('variable "node_image"') :]
    block = block[: block.index("\n}\n")]
    assert f'family = "{node_images.EKS_AMI_TYPES["arm64"]}"' in block


def test_a_pin_skips_every_lookup() -> None:
    def refuse(*_args: object) -> str:
        raise AssertionError("a pinned resolve must read nothing")

    assert node_images.resolve(AWS, pinned=True, read=refuse, warn=print) == {}


def test_a_cloud_with_no_provider_lookup_resolves_nothing() -> None:
    target = node_images.Target("rancher", "", "", "1.36", "amd64")
    assert node_images.resolve(target, pinned=False, read=RECORDED, warn=print) == {}


def test_expected_fail_one_failed_lookup_warns_once_and_leaves_only_its_key_out(
    tmp_path: Path,
) -> None:
    ami = (FIXTURES / "aws-us-west-2-al2023-ami-arm64.txt").read_text(encoding="utf-8")
    read = _recorded(tmp_path, "aws-us-west-2-al2023-ami-arm64.txt", ami)
    warnings: list[str] = []
    values = node_images.resolve(AWS, pinned=False, read=read, warn=warnings.append)
    assert values == {
        "instance_ami_id": "ami-0957e38de3db270cc",
        "forwarder_ami_id": "ami-0957e38de3db270cc",
    }
    assert len(warnings) == 1
    assert warnings[0].startswith("node_image not resolved (no recorded response")
    assert "the tfvars value or the variable default applies" in warnings[0]


def test_an_empty_answer_still_replaces_an_earlier_resolves_file(tmp_path: Path) -> None:
    node_images.write_tfvars(tmp_path, {"instance_ami_id": "ami-0957e38de3db270cc"})
    path = node_images.write_tfvars(tmp_path, {})
    assert path.name == "node-images.auto.tfvars.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {}


# --- the live reader, against real binaries that never reach a cloud ---------


def test_expected_fail_a_missing_cli_is_a_lookup_failure() -> None:
    read = node_images.live_reader()
    with pytest.raises(node_images.ImageLookupError, match="not on PATH"):
        read("unused", "dfe-no-such-cli", ["ssm", "get-parameter"])


def test_expected_fail_a_cli_that_exits_non_zero_is_a_lookup_failure() -> None:
    read = node_images.live_reader()
    with pytest.raises(node_images.ImageLookupError, match="failed: no stderr"):
        read("unused", "false", ["ssm", "get-parameter"])


def test_expected_fail_a_cli_that_hangs_is_a_lookup_failure() -> None:
    read = node_images.live_reader(timeout=0.2)
    with pytest.raises(node_images.ImageLookupError, match=r"took over 0\.2s"):
        read("unused", "sleep", ["5", "5"])


# --- resolve_sizing.py images ------------------------------------------------


def _dial(tmp_path: Path, *, cloud: str = "aws", region: str = "us-west-2", pin: str = "false") -> Path:
    path = tmp_path / "deployment.yaml"
    path.write_text(
        "target:\n"
        "  provision:\n"
        f"    cloud: {cloud}\n"
        '    account: "000000000000"\n'
        f"    region: {region}\n"
        'kubernetes_version: "1.36"\n'
        "node_image:\n"
        f'  pin: "{pin}"\n',
        encoding="utf-8",
    )
    return path


def _images(dial: Path, out: Path, *extra: str) -> int:
    return resolve_sizing.main(["images", "--dial", str(dial), "--out", str(out), *extra])


def test_the_images_subcommand_writes_every_aws_key(tmp_path: Path) -> None:
    out = tmp_path / "root"
    out.mkdir()
    assert _images(_dial(tmp_path), out, "--fixtures", str(FIXTURES)) == 0
    written = json.loads((out / "node-images.auto.tfvars.json").read_text(encoding="utf-8"))
    assert written == {
        "forwarder_ami_id": "ami-0957e38de3db270cc",
        "instance_ami_id": "ami-0957e38de3db270cc",
        "node_image": {"family": "AL2023_ARM_64_STANDARD", "version": "1.36.4-20261012"},
    }


def test_a_pinned_dial_writes_no_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "root"
    out.mkdir()
    assert _images(_dial(tmp_path, pin="true"), out, "--fixtures", str(FIXTURES)) == 0
    assert json.loads((out / "node-images.auto.tfvars.json").read_text(encoding="utf-8")) == {}
    assert "node_image.pin is true" in capsys.readouterr().err


def test_every_failed_lookup_still_exits_zero_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "root"
    out.mkdir()
    empty = tmp_path / "no-answers"
    empty.mkdir()
    assert _images(_dial(tmp_path), out, "--fixtures", str(empty)) == 0
    assert json.loads((out / "node-images.auto.tfvars.json").read_text(encoding="utf-8")) == {}
    assert capsys.readouterr().err.count("WARNING node image") == len(node_images.LOOKUPS["aws"])


@pytest.mark.parametrize(
    ("cloud", "region", "family"),
    [("gcp", "us-central1", "COS_CONTAINERD"), ("azure", "westus2", "Ubuntu")],
)
def test_a_gcp_or_azure_dial_resolves_against_the_shared_platform_range(
    tmp_path: Path, cloud: str, region: str, family: str
) -> None:
    """Neither has a root yet, so --out names where the answer goes, and versions.yaml floors both by range."""
    out = tmp_path / "root"
    out.mkdir()
    dial = _dial(tmp_path, cloud=cloud, region=region)
    assert _images(dial, out, "--fixtures", str(FIXTURES)) == 0
    written = json.loads((out / "node-images.auto.tfvars.json").read_text(encoding="utf-8"))
    assert written == {"node_image": {"family": family}}


def _existing(out: Path) -> Path:
    """A node-images file an earlier resolve left: a node release and AMIs old enough to replace."""
    path = out / "node-images.auto.tfvars.json"
    path.write_text(
        json.dumps({
            "forwarder_ami_id": "ami-11111111111111111",
            "instance_ami_id": "ami-00000000000000000",
            "node_image": {"family": "AL2023_ARM_64_STANDARD", "version": "1.36.1-20260901"},
        }),
        encoding="utf-8",
    )
    return path


def test_only_one_key_is_resolved_and_the_rest_of_the_file_kept(tmp_path: Path) -> None:
    """bastion up refreshes the toolbox AMI without moving the node release or replacing the forwarder."""
    out = tmp_path / "root"
    out.mkdir()
    path = _existing(out)
    assert _images(_dial(tmp_path), out, "--fixtures", str(FIXTURES), "--only", "instance_ami_id") == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "forwarder_ami_id": "ami-11111111111111111",
        "instance_ami_id": "ami-0957e38de3db270cc",
        "node_image": {"family": "AL2023_ARM_64_STANDARD", "version": "1.36.1-20260901"},
    }


def test_a_pinned_only_run_leaves_nothing_an_earlier_resolve_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under a pin terraform.tfvars must hold, and any key left in the auto file would beat it."""
    out = tmp_path / "root"
    out.mkdir()
    path = _existing(out)
    dial = _dial(tmp_path, pin="true")
    assert _images(dial, out, "--fixtures", str(FIXTURES), "--only", "instance_ami_id") == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {}
    assert "node_image.pin is true" in capsys.readouterr().err


def test_expected_fail_a_failed_only_lookup_drops_the_stale_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An AMI an earlier resolve wrote must not survive a lookup that could not confirm it."""
    out = tmp_path / "root"
    out.mkdir()
    path = _existing(out)
    empty = tmp_path / "no-answers"
    empty.mkdir()
    assert _images(_dial(tmp_path), out, "--fixtures", str(empty), "--only", "instance_ami_id") == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "forwarder_ami_id": "ami-11111111111111111",
        "node_image": {"family": "AL2023_ARM_64_STANDARD", "version": "1.36.1-20260901"},
    }
    assert capsys.readouterr().err.count("WARNING node image") == 1


def test_expected_fail_only_a_key_the_cloud_does_not_resolve_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "root"
    out.mkdir()
    path = _existing(out)
    assert _images(_dial(tmp_path), out, "--fixtures", str(FIXTURES), "--only", "node_pools") == 1
    assert json.loads(path.read_text(encoding="utf-8"))["instance_ami_id"] == "ami-00000000000000000"


def test_a_dial_with_no_provisioned_cloud_resolves_nothing(tmp_path: Path) -> None:
    dial = tmp_path / "deployment.yaml"
    dial.write_text("target:\n  existing:\n    namespace: dfe\n", encoding="utf-8")
    out = tmp_path / "root"
    out.mkdir()
    assert _images(dial, out) == 0
    assert not (out / "node-images.auto.tfvars.json").exists()


def test_an_on_prem_dial_resolves_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "root"
    out.mkdir()
    assert _images(_dial(tmp_path, cloud="rancher"), out) == 0
    assert not (out / "node-images.auto.tfvars.json").exists()
    assert "no provider publishes a node image for rancher" in capsys.readouterr().err


def test_expected_fail_a_pin_that_is_not_a_boolean_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "root"
    out.mkdir()
    assert _images(_dial(tmp_path, pin="sometimes"), out, "--fixtures", str(FIXTURES)) == 1
    assert not (out / "node-images.auto.tfvars.json").exists()


def test_expected_fail_a_missing_tofu_root_is_refused(tmp_path: Path) -> None:
    assert _images(_dial(tmp_path), tmp_path / "no-such-root", "--fixtures", str(FIXTURES)) == 1
