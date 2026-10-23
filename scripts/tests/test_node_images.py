#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_node_images.py
#  Purpose:      Prove the node-image cascade's lookups and release gates
#                against recorded provider responses, and that every failure
#                falls through.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/node_images.py and ``resolve_sizing.py images``.

``eks-ami-releases.json`` and ``eks-ami-open-issues.jsonl`` are the verbatim
answers of node_images.py's own two ``gh api`` reads of awslabs/amazon-eks-ami,
captured 2026-10-24, so every gate below runs against a real historical
release. The ``recommended`` SSM fixtures and ``aws-us-west-2-al2023-ami-arm64.txt``
are the verbatim us-west-2 public parameters, captured 2026-10-18. The
per-release ``image_id`` fixture for v20261012 carries the AMI id the arm64
``recommended`` answer named. The v20261019 ``image_id`` and every AMI in
``aws-us-west-2-al2023-images-arm64.json`` except ami-0957e38de3db270cc are
illustrative, in DescribeImages' documented shape. The GKE and AKS fixtures
are the documented response shapes with illustrative values, each saying so in
its own ``_provenance`` field.

    python3 -m pytest scripts/tests/test_node_images.py -q
"""

import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import node_images  # noqa: E402
import resolve_sizing  # noqa: E402
from yaml_subset import at  # noqa: E402
from yaml_subset import parse as parse_yaml  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "node-images"
SIZING_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sizing"
RECORDED = node_images.fixture_reader(FIXTURES)
AWS_ROOT_VARIABLES = REPO_ROOT / "terraform" / "environments" / "aws" / "variables.tf"
VERSIONS = REPO_ROOT / "versions.yaml"
NODE_POOLS_VALUES = REPO_ROOT / "helm" / "charts" / "node-pools" / "values.yaml"

AWS = node_images.Target("aws", "us-west-2", "000000000000", "1.36", "arm64")
GCP = node_images.Target("gcp", "us-central1", "example-project", "1.36", "arm64")
AZURE = node_images.Target(
    "azure", "westus2", "00000000-0000-0000-0000-000000000000", "1.36", "arm64"
)

# The day the releases were captured: v20261019 was two days old, v20261012 ten.
NOW = datetime.fromisoformat("2026-10-24T00:00:00+00:00")
POLICY = node_images.read_policy(VERSIONS)
# The real cooldown with no skip-list and a floor below every recorded candidate,
# so each gate below is the only one that can refuse its release.
OPEN = node_images.GatePolicy(POLICY.cooldown_days, "v20240101", {}, "20240101", {})

RELEASES = node_images.parse_releases(
    (FIXTURES / "eks-ami-releases.json").read_text(encoding="utf-8")
)
TITLES = node_images.open_issue_titles(
    (FIXTURES / "eks-ami-open-issues.jsonl").read_text(encoding="utf-8")
)


def _recorded(tmp_path: Path, name: str, body: str) -> node_images.Reader:
    """A reader over one recorded response, rewritten for the case under test."""
    (tmp_path / name).write_text(body, encoding="utf-8")
    return node_images.fixture_reader(tmp_path)


def _release(tag: str) -> node_images.Release:
    return next(r for r in RELEASES if r.tag == tag)


def _refusals(tag: str, policy: node_images.GatePolicy = OPEN, now: datetime = NOW) -> list[str]:
    return node_images.eks_refusals(
        _release(tag), releases=RELEASES, policy=policy, open_titles=TITLES, now=now
    )


# --- the policy ----------------------------------------------------------------


def test_versions_yaml_carries_the_gates() -> None:
    assert POLICY.cooldown_days == 7
    assert POLICY.eks_floor == "v20261012"
    assert set(POLICY.eks_skip) == {"v20260625", "v20260930", "v20261003"}
    assert POLICY.al2023_floor == "20261012"
    assert POLICY.al2023_skip == {}


def test_the_chart_default_alias_clears_the_floor() -> None:
    """The alias a deployment falls back to with none recorded must itself pass gate g."""
    tree = parse_yaml(VERSIONS.read_text(encoding="utf-8"), source="versions.yaml")
    pin = str(
        at(tree, ("stacks", str(at(tree, ("current",))), "operators", "karpenter-al2023-ami"))
    )
    assert pin >= POLICY.eks_floor
    assert f"amiAlias: al2023@{pin}\n" in NODE_POOLS_VALUES.read_text(encoding="utf-8")


def test_the_gates_sit_outside_every_stack() -> None:
    """A released stack's block is frozen, and a skip-list entry cannot wait for a stack cut."""
    tree = parse_yaml(VERSIONS.read_text(encoding="utf-8"), source="versions.yaml")
    assert isinstance(tree["node-images"], dict)
    assert all("node-images" not in stack for stack in tree["stacks"].values())


@pytest.mark.parametrize(
    ("block", "match"),
    [
        ("", "carries no top-level node-images block"),
        ('node-images:\n  cooldown-days: "a week"\n', "whole number of days"),
        (
            'node-images:\n  cooldown-days: "7"\n  eks-ami-floor: ">=latest"\n'
            '  al2023-ami-floor: ">=20261012"\n',
            r"eks-ami-floor must be >=",
        ),
        (
            'node-images:\n  cooldown-days: "7"\n  eks-ami-floor: ">=v20261012"\n'
            '  al2023-ami-floor: ">=20261012"\n  eks-ami-skip: "v20260930"\n',
            "must map each release to its reason",
        ),
    ],
    ids=["absent", "cooldown", "undated-floor", "skip-not-a-map"],
)
def test_expected_fail_a_malformed_policy_is_refused(
    tmp_path: Path, block: str, match: str
) -> None:
    versions = tmp_path / "versions.yaml"
    versions.write_text(f'current: "9.9.9"\n{block}stacks:\n  9.9.9: {{}}\n', encoding="utf-8")
    with pytest.raises(node_images.GatePolicyError, match=match):
        node_images.read_policy(versions)


# --- the EKS gates, each on a real release ----------------------------------------


def test_gate_a_refuses_a_release_inside_the_cooldown() -> None:
    """v20261019 was two days old on the capture day, and clears every gate five days later."""
    assert _refusals("v20261019", POLICY) == [
        "released 2026-10-21, 2 day(s) ago, inside the 7-day cooldown"
    ]
    assert _refusals("v20261019", POLICY, datetime.fromisoformat("2026-10-29T00:00:00+00:00")) == []


def test_gate_a_measures_from_published_at_never_the_tag() -> None:
    """v20260311 was published at 2026-04-27T23:35Z, 47 days after its tag date."""
    tag_age_50_days = datetime.fromisoformat("2026-04-30T12:00:00+00:00")
    assert _refusals("v20260311", now=tag_age_50_days) == [
        "released 2026-04-27, 2 day(s) ago, inside the 7-day cooldown"
    ]


def test_gate_a_alone_would_adopt_the_known_bad_releases() -> None:
    """Fifteen and 22 days old on the capture day, both clear a cooldown and nothing else."""
    for tag in ("v20260930", "v20261003"):
        assert node_images._age_refusal(_release(tag).published, NOW, 7) is None
        assert _refusals(tag, POLICY) != []


@pytest.mark.parametrize("tag", ["v20260707", "v20260523", "v20260128"])
def test_gate_b_refuses_a_prerelease(tag: str) -> None:
    assert _refusals(tag) == ["a draft or prerelease"]


@pytest.mark.parametrize(
    ("tag", "reason"),
    [
        ("v20260625", "ENA driver 2.17.0g"),
        ("v20260930", "kernel 6.12.110"),
        ("v20261003", "kernel 6.12.110"),
    ],
)
def test_gate_c_refuses_a_skip_listed_release(tag: str, reason: str) -> None:
    refusals = _refusals(tag, POLICY)
    assert any(r.startswith("skip-listed in versions.yaml:") and reason in r for r in refusals)


@pytest.mark.parametrize("tag", ["v20260930", "v20261003", "v20260625"])
def test_gate_d_refuses_a_release_whose_own_notes_warn(tag: str) -> None:
    warned = [r for r in _refusals(tag) if r.startswith("its own release notes warn:")]
    assert len(warned) == 1
    assert "might cause kernel panic" in warned[0]


def test_gate_d_reads_a_fix_note_as_no_warning() -> None:
    """v20261012's admonition says it FIXES the kernel panic, and v20261012 is the floor."""
    admonition = _release("v20261012").admonitions[0]
    assert "fixes a kernel panic" in admonition
    assert node_images.warning_sentences(admonition) == []
    assert _refusals("v20261012") == []


def test_gate_d_counts_only_a_hedge_about_the_release_itself() -> None:
    assert (
        node_images.warning_sentences("> [!IMPORTANT]\n> This release fixes a known issue in runc.")
        == []
    )
    assert node_images.warning_sentences("> [!WARNING]\n> This release may cause failures.") == [
        "This release may cause failures."
    ]


@pytest.mark.parametrize("tag", ["v20260930", "v20261003"])
def test_gate_e_refuses_a_release_a_newer_ones_notes_name(tag: str) -> None:
    assert "v20261012's release notes name it as affected" in _refusals(tag)


def test_gate_e_never_reads_an_older_release_naming_a_newer_one() -> None:
    """The abandoned v20260128 prerelease names v20260129, which is the release that replaced it."""
    assert "v20260129" in _release("v20260128").admonitions[0]
    assert _refusals("v20260129") == []


def test_gate_f_refuses_a_release_an_open_issue_names() -> None:
    refusals = _refusals("v20260409")
    assert len(refusals) == 1
    assert refusals[0].startswith("open issue #2681 bug(al2023): v20260409")


def test_gate_g_refuses_a_clean_release_below_the_floor() -> None:
    assert _refusals("v20260923") == []
    assert _refusals("v20260923", POLICY) == ["below the floor v20261012"]


def test_the_release_version_is_read_per_minor() -> None:
    release = _release("v20261012")
    assert node_images.release_version_for(release, "1.36") == "1.36.4-20261012"
    assert node_images.release_version_for(release, "1.34") == "1.34.11-20261012"
    assert node_images.release_version_for(release, "1.40") is None


def test_the_newest_eligible_release_wins_and_names_what_it_passed_over() -> None:
    release, version, skipped = node_images.eks_release(AWS, RECORDED, POLICY, NOW)
    assert (release.tag, version) == ("v20261012", "1.36.4-20261012")
    assert skipped == ("v20261019: released 2026-10-21, 2 day(s) ago, inside the 7-day cooldown",)


def test_the_eks_lookup_reads_the_releases_the_issues_and_the_regions_parameter() -> None:
    calls: list[tuple[str, list[str]]] = []

    def record(fixture: str, tool: str, args: list[str]) -> str:
        calls.append((tool, args))
        return RECORDED(fixture, tool, args)

    node_images.eks_release(AWS, record, POLICY, NOW)
    assert [tool for tool, _args in calls] == ["gh", "gh", "aws"]
    assert calls[0][1][1] == "repos/awslabs/amazon-eks-ami/releases?per_page=100"
    assert "--paginate" in calls[1][1]
    args = calls[2][1]
    assert args[args.index("--name") + 1] == (
        "/aws/service/eks/optimized-ami/1.36/amazon-linux-2023/arm64/standard/"
        "amazon-eks-node-al2023-arm64-standard-1.36-v20261012/image_id"
    )
    assert args[args.index("--region") + 1] == "us-west-2"


def test_expected_fail_no_eligible_release_names_each_refusal() -> None:
    """A day after v20261012 shipped, everything at or above the floor was too young."""
    day_after = datetime.fromisoformat("2026-10-15T12:00:00+00:00")
    with pytest.raises(node_images.NoEligibleImageError) as caught:
        node_images.eks_release(AWS, RECORDED, POLICY, day_after)
    assert caught.value.refusals == [
        "v20261019: released 2026-10-21, after 2026-10-15",
        "v20261012: released 2026-10-14, 0 day(s) ago, inside the 7-day cooldown",
    ]


def test_expected_fail_a_release_the_region_does_not_publish_is_passed_over() -> None:
    def missing(fixture: str, tool: str, args: list[str]) -> str:
        if tool == "aws":
            raise node_images.ImageLookupError(
                "aws ssm get-parameter failed: An error occurred (ParameterNotFound) when calling "
                "the GetParameter operation"
            )
        return RECORDED(fixture, tool, args)

    with pytest.raises(node_images.NoEligibleImageError) as caught:
        node_images.eks_release(AWS, missing, POLICY, NOW)
    assert "v20261012: not published in us-west-2" in caught.value.refusals


def test_expected_fail_an_ssm_failure_is_a_lookup_failure_not_a_gate() -> None:
    def denied(fixture: str, tool: str, args: list[str]) -> str:
        if tool == "aws":
            raise node_images.ImageLookupError(
                "aws ssm get-parameter failed: UnrecognizedClientException"
            )
        return RECORDED(fixture, tool, args)

    with pytest.raises(node_images.ImageLookupError) as caught:
        node_images.eks_release(AWS, denied, POLICY, NOW)
    assert not isinstance(caught.value, node_images.NoEligibleImageError)


def test_expected_fail_releases_that_are_not_the_trimmed_list_are_refused() -> None:
    with pytest.raises(node_images.ImageLookupError, match="not a JSON array"):
        node_images.parse_releases('{"message": "Not Found"}')
    with pytest.raises(node_images.ImageLookupError, match="no tag_name"):
        node_images.parse_releases('[{"published_at": "2026-10-14T17:28:15Z"}]')
    with pytest.raises(node_images.ImageLookupError, match="not JSON lines"):
        node_images.open_issue_titles("[{")


def test_expected_fail_a_version_that_is_not_a_minor_is_refused_before_any_read() -> None:
    target = node_images.Target("aws", "us-west-2", "000000000000", "1.34-1.36", "arm64")

    def refuse(*_args: object) -> str:
        raise AssertionError("a target with no minor must read nothing")

    with pytest.raises(node_images.ImageLookupError, match="not a Kubernetes minor"):
        node_images.eks_release(target, refuse, POLICY, NOW)


# --- the recommended alias check_karpenter_ami.py reports against ----------------


def test_the_recommended_alias_is_the_dated_release_never_latest() -> None:
    assert node_images.recommended_ami_alias(AWS, RECORDED) == "al2023@v20261012"
    target = node_images.Target("aws", "us-west-2", "000000000000", "1.36", "amd64")
    assert node_images.recommended_ami_alias(target, RECORDED) == "al2023@v20261012"


@pytest.mark.parametrize(
    ("release", "image_name", "match"),
    [
        ("1.35.9-20261012", "amazon-eks-node-al2023-arm64-standard-1.35-v20261012", "not a 1.36"),
        ("1.36.4", "amazon-eks-node-al2023-arm64-standard-1.36-v20261012", "not <patch>"),
        ("", "amazon-eks-node-al2023-arm64-standard-1.36-v20261012", "not <patch>"),
        (
            "1.36.4-20261012",
            "amazon-eks-node-al2023-arm64-standard-1.36-v20261003",
            "does not carry",
        ),
    ],
    ids=["another-minor", "undated", "missing", "name-disagrees"],
)
def test_expected_fail_a_recommended_answer_that_does_not_hold_together_is_refused(
    tmp_path: Path, release: str, image_name: str, match: str
) -> None:
    body = json.dumps({"release_version": release, "image_name": image_name})
    read = _recorded(tmp_path, "aws-us-west-2-eks-recommended-1.36-arm64.json", body)
    with pytest.raises(node_images.ImageLookupError, match=match):
        node_images.recommended_ami_alias(AWS, read)


def test_expected_fail_a_recommended_answer_that_is_not_json_is_refused(tmp_path: Path) -> None:
    read = _recorded(tmp_path, "aws-us-west-2-eks-recommended-1.36-arm64.json", "ami-0123")
    with pytest.raises(node_images.ImageLookupError, match="not JSON"):
        node_images.recommended_ami_alias(AWS, read)


# --- the AL2023 gates ----------------------------------------------------------


def test_the_newest_al2023_ami_past_the_cooldown_wins() -> None:
    pick = node_images.al2023_image(AWS, RECORDED, POLICY, NOW)
    assert (pick.value, pick.release) == (
        "ami-0957e38de3db270cc",
        "al2023-ami-2023.12.20261016.0-kernel-6.12-arm64",
    )


def test_al2023_gate_a_measures_from_creation_date_and_keeps_to_the_default_family() -> None:
    """The kernel-6.1 AMI is as old as the 6.12 one but sits in another family."""
    pick = node_images.al2023_image(
        AWS, RECORDED, POLICY, datetime.fromisoformat("2026-10-20T00:00:00+00:00")
    )
    assert pick.value == "ami-02026101200000000"
    assert pick.skipped == (
        "al2023-ami-2023.12.20261016.0-kernel-6.12-arm64: released 2026-10-16, 3 day(s) ago, inside "
        "the 7-day cooldown",
    )


def test_al2023_gate_c_refuses_a_skip_listed_release() -> None:
    policy = node_images.GatePolicy(
        7, "v20261012", {}, "20261012", {"2023.12.20261016.0": "bad kernel"}
    )
    assert node_images.al2023_image(AWS, RECORDED, policy, NOW).value == "ami-02026101200000000"


def test_expected_fail_al2023_gate_g_leaves_nothing_eligible() -> None:
    """Two days after 2023.12.20261012.0, the only AMI past the cooldown is below the floor."""
    with pytest.raises(node_images.NoEligibleImageError) as caught:
        node_images.al2023_image(
            AWS, RECORDED, POLICY, datetime.fromisoformat("2026-10-14T23:00:00+00:00")
        )
    assert caught.value.refusals == [
        "al2023-ami-2023.12.20261016.0-kernel-6.12-arm64: released 2026-10-16, after 2026-10-14",
        "al2023-ami-2023.12.20261012.0-kernel-6.12-arm64: released 2026-10-12, 2 day(s) ago, inside "
        "the 7-day cooldown",
    ]


def test_expected_fail_a_default_ami_ec2_does_not_list_is_refused(tmp_path: Path) -> None:
    (tmp_path / "aws-us-west-2-al2023-ami-arm64.txt").write_text(
        "ami-0957e38de3db270cc\n", encoding="utf-8"
    )
    read = _recorded(tmp_path, "aws-us-west-2-al2023-images-arm64.json", "[]")
    with pytest.raises(node_images.ImageLookupError, match="is not among the AL2023 images"):
        node_images.al2023_image(AWS, read, POLICY, NOW)


def test_expected_fail_an_arch_aws_publishes_no_image_for_is_refused() -> None:
    target = node_images.Target("aws", "us-west-2", "000000000000", "1.36", "riscv64")
    with pytest.raises(node_images.ImageLookupError, match="no image for arch"):
        node_images.al2023_image(target, RECORDED, POLICY, NOW)


def test_expected_fail_an_ssm_answer_that_is_not_an_ami_is_refused(tmp_path: Path) -> None:
    read = _recorded(tmp_path, "aws-us-west-2-al2023-ami-arm64.txt", "None\n")
    with pytest.raises(node_images.ImageLookupError, match="not an AMI id"):
        node_images.al2023_image(AWS, read, POLICY, NOW)


# --- gcp -----------------------------------------------------------------------


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


# --- azure ---------------------------------------------------------------------


def test_aks_takes_the_version_tracking_ubuntu_sku_on_a_ga_minor() -> None:
    assert node_images.aks_node_image(AZURE, RECORDED) == {"family": "Ubuntu"}


@pytest.mark.parametrize(
    ("minor", "match"),
    [("1.37", "preview only"), ("1.33", "lists no 1.33")],
    ids=["preview", "absent"],
)
def test_expected_fail_a_minor_aks_does_not_offer_generally_is_refused(
    minor: str, match: str
) -> None:
    target = node_images.Target("azure", "westus2", AZURE.account, minor, "arm64")
    with pytest.raises(node_images.ImageLookupError, match=match):
        node_images.aks_node_image(target, RECORDED)


def test_expected_fail_an_aks_answer_with_no_values_is_refused(tmp_path: Path) -> None:
    read = _recorded(tmp_path, "azure-westus2-aks-versions.json", '{"orchestrators": []}')
    with pytest.raises(node_images.ImageLookupError, match="no values list"):
        node_images.aks_node_image(AZURE, read)


# --- the cascade -------------------------------------------------------------


def _resolve(
    read: node_images.Reader = RECORDED,
    *,
    now: datetime = NOW,
    recorded: dict[str, dict[str, object]] | None = None,
    warnings: list[str] | None = None,
) -> node_images.Resolution:
    sink = warnings if warnings is not None else []
    return node_images.resolve(
        AWS, pinned=False, read=read, warn=sink.append, policy=POLICY, now=now, recorded=recorded
    )


def test_aws_resolves_every_key_and_records_where_each_came_from() -> None:
    warnings: list[str] = []
    resolution = _resolve(warnings=warnings)
    assert resolution.values == {
        "node_image": {"family": "AL2023_ARM_64_STANDARD", "version": "1.36.4-20261012"},
        "karpenter_ami_alias": "al2023@v20261012",
        "instance_ami_id": "ami-0957e38de3db270cc",
        "forwarder_ami_id": "ami-0957e38de3db270cc",
    }
    assert resolution.record["node_image"]["release"] == "v20261012"
    assert resolution.record["karpenter_ami_alias"]["release"] == "v20261012"
    assert (
        resolution.record["instance_ami_id"]["release"]
        == "al2023-ami-2023.12.20261016.0-kernel-6.12-arm64"
    )
    assert warnings == []


def test_one_github_read_answers_both_eks_keys() -> None:
    reads: list[str] = []

    def count(fixture: str, tool: str, args: list[str]) -> str:
        reads.append(fixture)
        return RECORDED(fixture, tool, args)

    _resolve(count)
    assert reads.count(node_images.EKS_RELEASES_FIXTURE) == 1
    assert reads.count("aws-us-west-2-al2023-images-arm64.json") == 1


def test_every_tfvars_key_is_one_the_aws_root_declares() -> None:
    text = AWS_ROOT_VARIABLES.read_text(encoding="utf-8")
    declared = set(re.findall(r'^variable "([^"]+)"', text, re.MULTILINE))
    assert node_images.tfvars_keys("aws") <= declared
    assert "karpenter_ami_alias" not in node_images.tfvars_keys("aws")


def test_the_roots_node_image_has_no_default_to_fall_back_to() -> None:
    text = AWS_ROOT_VARIABLES.read_text(encoding="utf-8")
    block = text[text.index('variable "node_image"') :]
    block = block[: block.index("\n}\n")]
    assert "default = null" in block
    assert "Run scripts/resolve_sizing.py images" in block


def test_a_pin_skips_every_lookup() -> None:
    def refuse(*_args: object) -> str:
        raise AssertionError("a pinned resolve must read nothing")

    resolution = node_images.resolve(
        AWS, pinned=True, read=refuse, warn=print, policy=POLICY, now=NOW
    )
    assert resolution.values == {}


def test_a_cloud_with_no_provider_lookup_resolves_nothing() -> None:
    target = node_images.Target("rancher", "", "", "1.36", "amd64")
    resolution = node_images.resolve(
        target, pinned=False, read=RECORDED, warn=print, policy=POLICY, now=NOW
    )
    assert resolution.values == {}


def test_expected_fail_a_failed_github_read_leaves_only_the_eks_keys_out(tmp_path: Path) -> None:
    for name in FIXTURES.glob("aws-*al2023*"):
        shutil.copy(name, tmp_path / name.name)
    warnings: list[str] = []
    resolution = _resolve(node_images.fixture_reader(tmp_path), warnings=warnings)
    assert set(resolution.values) == {"instance_ami_id", "forwarder_ami_id"}
    assert len(warnings) == 2
    assert all("no recorded response" in w and "the plan refuses by name" in w for w in warnings)


def test_a_key_no_candidate_clears_holds_its_recorded_value() -> None:
    day_after = datetime.fromisoformat("2026-10-15T12:00:00+00:00")
    recorded = {
        "node_image": {
            "value": {"family": "AL2023_ARM_64_STANDARD", "version": "1.36.4-20261012"},
            "release": "v20261012",
        },
    }
    warnings: list[str] = []
    resolution = _resolve(now=day_after, recorded=recorded, warnings=warnings)
    assert resolution.values["node_image"] == recorded["node_image"]["value"]
    assert "karpenter_ami_alias" not in resolution.values
    assert any(w.startswith("node_image holds its recorded v20261012") for w in warnings)
    assert any(
        "karpenter_ami_alias not resolved" in w and "nothing was recorded" in w for w in warnings
    )


@pytest.mark.parametrize(
    ("key", "entry", "why"),
    [
        (
            "node_image",
            {
                "value": {"family": "AL2023_ARM_64_STANDARD", "version": "1.36.4-20261003"},
                "release": "v20261003",
            },
            "v20261003 is skip-listed",
        ),
        (
            "karpenter_ami_alias",
            {"value": "al2023@v20260923", "release": "v20260923"},
            "below the floor",
        ),
        (
            "instance_ami_id",
            {
                "value": "ami-02026093000000000",
                "release": "al2023-ami-2023.12.20260930.0-kernel-6.12-arm64",
            },
            "below the floor 20261012",
        ),
    ],
    ids=["skip-listed", "below-eks-floor", "below-al2023-floor"],
)
def test_expected_fail_a_recorded_value_the_gates_now_refuse_is_never_held(
    key: str, entry: dict[str, object], why: str
) -> None:
    before_any = datetime.fromisoformat("2026-10-14T23:00:00+00:00")
    warnings: list[str] = []
    resolution = _resolve(now=before_any, recorded={key: entry}, warnings=warnings)
    assert key not in resolution.values
    assert key in resolution.refused
    assert any(f"{key} not resolved" in w and why in w for w in warnings)


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

NOW_ARG = ("--now", "2026-10-24T00:00:00Z")


def _dial(
    tmp_path: Path, *, cloud: str = "aws", region: str = "us-west-2", pin: str = "false"
) -> Path:
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
    return resolve_sizing.main(["images", "--dial", str(dial), "--out", str(out), *NOW_ARG, *extra])


def _root(tmp_path: Path) -> Path:
    out = tmp_path / "root"
    out.mkdir()
    return out


def test_the_images_subcommand_writes_tfvars_the_record_and_the_chart_overlay(
    tmp_path: Path,
) -> None:
    out = _root(tmp_path)
    assert _images(_dial(tmp_path), out, "--fixtures", str(FIXTURES)) == 0
    written = json.loads((out / "node-images.auto.tfvars.json").read_text(encoding="utf-8"))
    assert written == {
        "forwarder_ami_id": "ami-0957e38de3db270cc",
        "instance_ami_id": "ami-0957e38de3db270cc",
        "node_image": {"family": "AL2023_ARM_64_STANDARD", "version": "1.36.4-20261012"},
    }
    record = node_images.read_record(out / "sizing" / "node-images.json")
    assert record["karpenter_ami_alias"] == {"value": "al2023@v20261012", "release": "v20261012"}
    overlay = (out / "sizing" / "node-pools.values.yaml").read_text(encoding="utf-8")
    assert "pools:\n  aws:\n    nodeClass:\n      amiAlias: al2023@v20261012\n" in overlay


def test_the_chart_overlay_sets_the_key_the_chart_reads() -> None:
    """The chart's own default sits at pools.aws.nodeClass.amiAlias, the path the overlay writes."""
    values = NODE_POOLS_VALUES.read_text(encoding="utf-8")
    section = values[values.index("\n  aws:\n") : values.index("amiAlias:")]
    assert "\n    nodeClass:\n" in section


def test_a_pinned_dial_writes_no_key_and_leaves_the_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _root(tmp_path)
    assert _images(_dial(tmp_path, pin="true"), out, "--fixtures", str(FIXTURES)) == 0
    assert json.loads((out / "node-images.auto.tfvars.json").read_text(encoding="utf-8")) == {}
    assert not (out / "sizing").exists()
    assert "node_image.pin is true" in capsys.readouterr().err


def test_every_failed_lookup_still_exits_zero_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _root(tmp_path)
    empty = tmp_path / "no-answers"
    empty.mkdir()
    assert _images(_dial(tmp_path), out, "--fixtures", str(empty)) == 0
    assert json.loads((out / "node-images.auto.tfvars.json").read_text(encoding="utf-8")) == {}
    assert not (out / "sizing" / "node-pools.values.yaml").exists()
    assert capsys.readouterr().err.count("WARNING node image") == len(node_images.LOOKUPS["aws"])


def test_previous_holds_the_deploy_repos_value_when_nothing_clears_the_gates(
    tmp_path: Path,
) -> None:
    deploy = tmp_path / "deploy" / "sizing"
    deploy.mkdir(parents=True)
    held = {
        "karpenter_ami_alias": {"value": "al2023@v20261012", "release": "v20261012"},
        "node_image": {
            "value": {"family": "AL2023_ARM_64_STANDARD", "version": "1.36.4-20261012"},
            "release": "v20261012",
        },
    }
    (deploy / "node-images.json").write_text(json.dumps(held), encoding="utf-8")
    out = _root(tmp_path)
    argv = [
        "images",
        "--dial",
        str(_dial(tmp_path)),
        "--out",
        str(out),
        "--fixtures",
        str(FIXTURES),
        "--now",
        "2026-10-15T12:00:00Z",
        "--previous",
        str(deploy / "node-images.json"),
    ]
    assert resolve_sizing.main(argv) == 0
    tfvars = json.loads((out / "node-images.auto.tfvars.json").read_text(encoding="utf-8"))
    assert tfvars["node_image"] == held["node_image"]["value"]
    assert (
        node_images.read_record(out / "sizing" / "node-images.json")["node_image"]
        == held["node_image"]
    )
    assert "al2023@v20261012" in (out / "sizing" / "node-pools.values.yaml").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("cloud", "region", "family"),
    [("gcp", "us-central1", "COS_CONTAINERD"), ("azure", "westus2", "Ubuntu")],
)
def test_a_gcp_or_azure_dial_resolves_against_the_shared_platform_range(
    tmp_path: Path, cloud: str, region: str, family: str
) -> None:
    """Neither has a root yet, so --out names where the answer goes, and versions.yaml floors both by range."""
    out = _root(tmp_path)
    dial = _dial(tmp_path, cloud=cloud, region=region)
    assert _images(dial, out, "--fixtures", str(FIXTURES)) == 0
    written = json.loads((out / "node-images.auto.tfvars.json").read_text(encoding="utf-8"))
    assert written == {"node_image": {"family": family}}
    assert not (out / "sizing" / "node-pools.values.yaml").exists()


def _existing(out: Path) -> Path:
    """A node-images file an earlier resolve left: a node release and AMIs old enough to replace."""
    path = out / "node-images.auto.tfvars.json"
    path.write_text(
        json.dumps(
            {
                "forwarder_ami_id": "ami-11111111111111111",
                "instance_ami_id": "ami-00000000000000000",
                "node_image": {"family": "AL2023_ARM_64_STANDARD", "version": "1.36.1-20260901"},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_only_one_key_is_resolved_and_the_rest_of_the_file_kept(tmp_path: Path) -> None:
    """bastion up refreshes the toolbox AMI without moving the node release or replacing the forwarder."""
    out = _root(tmp_path)
    path = _existing(out)
    assert (
        _images(_dial(tmp_path), out, "--fixtures", str(FIXTURES), "--only", "instance_ami_id") == 0
    )
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "forwarder_ami_id": "ami-11111111111111111",
        "instance_ami_id": "ami-0957e38de3db270cc",
        "node_image": {"family": "AL2023_ARM_64_STANDARD", "version": "1.36.1-20260901"},
    }
    assert set(node_images.read_record(out / "sizing" / "node-images.json")) == {"instance_ami_id"}
    assert not (out / "sizing" / "node-pools.values.yaml").exists()


def test_a_pinned_only_run_leaves_nothing_an_earlier_resolve_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Under a pin terraform.tfvars must hold, and any key left in the auto file would beat it."""
    out = _root(tmp_path)
    path = _existing(out)
    dial = _dial(tmp_path, pin="true")
    assert _images(dial, out, "--fixtures", str(FIXTURES), "--only", "instance_ami_id") == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {}
    assert "node_image.pin is true" in capsys.readouterr().err


def test_expected_fail_a_failed_only_lookup_drops_the_stale_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An AMI an earlier resolve wrote must not survive a lookup that could not confirm it."""
    out = _root(tmp_path)
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
    out = _root(tmp_path)
    path = _existing(out)
    assert _images(_dial(tmp_path), out, "--fixtures", str(FIXTURES), "--only", "node_pools") == 1
    assert (
        json.loads(path.read_text(encoding="utf-8"))["instance_ami_id"] == "ami-00000000000000000"
    )


def test_expected_fail_a_versions_file_with_no_gates_is_refused(tmp_path: Path) -> None:
    out = _root(tmp_path)
    versions = tmp_path / "versions.yaml"
    versions.write_text(
        'current: "9.9.9"\nstacks:\n  9.9.9:\n    platform:\n      eks: ">=1.34"\n',
        encoding="utf-8",
    )
    argv = ("--fixtures", str(FIXTURES), "--versions", str(versions))
    assert _images(_dial(tmp_path), out, *argv) == 1
    assert not (out / "node-images.auto.tfvars.json").exists()


def test_a_dial_with_no_provisioned_cloud_resolves_nothing(tmp_path: Path) -> None:
    dial = tmp_path / "deployment.yaml"
    dial.write_text("target:\n  existing:\n    namespace: dfe\n", encoding="utf-8")
    out = _root(tmp_path)
    assert _images(dial, out) == 0
    assert not (out / "node-images.auto.tfvars.json").exists()


def test_an_on_prem_dial_resolves_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _root(tmp_path)
    assert _images(_dial(tmp_path, cloud="rancher"), out) == 0
    assert not (out / "node-images.auto.tfvars.json").exists()
    assert "no provider publishes a node image for rancher" in capsys.readouterr().err


def test_expected_fail_a_pin_that_is_not_a_boolean_is_refused(tmp_path: Path) -> None:
    out = _root(tmp_path)
    assert _images(_dial(tmp_path, pin="sometimes"), out, "--fixtures", str(FIXTURES)) == 1
    assert not (out / "node-images.auto.tfvars.json").exists()


def test_expected_fail_a_missing_tofu_root_is_refused(tmp_path: Path) -> None:
    assert _images(_dial(tmp_path), tmp_path / "no-such-root", "--fixtures", str(FIXTURES)) == 1


# --- the sizing lock never sees a node image ---------------------------------


def test_the_sizing_lock_names_no_node_image_key() -> None:
    sizing = resolve_sizing._load(resolve_sizing.SIZING_FILE)
    locked = set(resolve_sizing._at(sizing, "locked"))
    assert locked
    assert locked.isdisjoint({key for key, _lookup in node_images.LOOKUPS["aws"]})


def test_a_moved_node_image_never_trips_the_sizing_lock(tmp_path: Path) -> None:
    """The images record sits beside resolved.yaml, never in it, so an AMI moving is not a LOCKED change."""
    dial = tmp_path / "deployment.yaml"
    dial.write_text(
        "target:\n  provision:\n    cloud: aws\n    region: us-west-2\n"
        '    account: "000000000000"\n'
        'kubernetes_version: "1.36"\nnode_image:\n  pin: "false"\n'
        "profile: scale\nkafka:\n  provider: strimzi\n"
        "sizing:\n  focus: economy\n  ingest_gb_per_day: 1000\n  spend_warn_usd_month: 5000\n"
        "retention:\n  default_ttl_days: 90\nk8s:\n  env: test\n  cloud: aws\n",
        encoding="utf-8",
    )
    out = _root(tmp_path)
    sizing_argv = ["--dial", str(dial), "--fixtures", str(SIZING_FIXTURES), "--out", str(out)]
    assert resolve_sizing.main(sizing_argv) == 0
    resolved = out / "sizing" / "resolved.yaml"
    before = resolved.read_bytes()

    assert _images(dial, out, "--fixtures", str(FIXTURES)) == 0
    first = node_images.read_record(out / "sizing" / "node-images.json")["node_image"]["release"]
    later = ("--fixtures", str(FIXTURES), "--now", "2026-10-29T00:00:00Z")
    assert resolve_sizing.main(["images", "--dial", str(dial), "--out", str(out), *later]) == 0
    second = node_images.read_record(out / "sizing" / "node-images.json")["node_image"]["release"]
    assert (first, second) == ("v20261012", "v20261019")
    assert resolved.read_bytes() == before

    assert resolve_sizing.main([*sizing_argv, "--previous", str(resolved)]) == 0
