#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_cloud_sweep.py
#  Purpose:      Guard the post-proof AWS sweep: each class parses the aws CLI
#                shape it actually returns, deletion runs in dependency order,
#                the excluded bucket and untagged resources are never touched
#                without --include-untagged, and the exit code tracks what
#                remains.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for `cloud_sweep.py`.

    python3 -m pytest scripts/tests/test_cloud_sweep.py -q

Every test mocks `subprocess.run` with fixture JSON lifted from the real aws
CLI shapes (list-of-Key/Value tags for EC2, TagSet for ENIs, a plain dict for
EKS/MSK) -- no network call and no real account is touched.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import cloud_sweep  # noqa: E402

REGION = "us-west-2"
TAG_FILTER = {"service-name": "dfe", "environment": "test"}
DFE_TAGS = [{"Key": "service-name", "Value": "dfe"}, {"Key": "environment", "Value": "test"}]


def _ok(stdout_obj: object) -> subprocess.CompletedProcess:
    """A successful aws CLI call returning this JSON body."""
    return subprocess.CompletedProcess(args=["aws"], returncode=0, stdout=json.dumps(stdout_obj), stderr="")


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["aws"], returncode=1, stdout="", stderr=stderr)


def _mock_run(monkeypatch: pytest.MonkeyPatch, *responses: subprocess.CompletedProcess) -> None:
    """Replace subprocess.run with one that returns `responses` in call order."""
    queue = list(responses)

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        return queue.pop(0)

    monkeypatch.setattr(cloud_sweep.subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# Per-class listing parsers
# ---------------------------------------------------------------------------


def test_ec2_instances_are_parsed_and_terminated_ones_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok(
            {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-live",
                                "State": {"Name": "running"},
                                "LaunchTime": "2026-09-01T00:00:00+00:00",
                                "Tags": DFE_TAGS,
                            },
                            {
                                "InstanceId": "i-dead",
                                "State": {"Name": "terminated"},
                                "LaunchTime": "2026-09-01T00:00:00+00:00",
                                "Tags": [],
                            },
                        ]
                    }
                ]
            }
        ),
    )
    found = cloud_sweep.list_ec2_instances(REGION, TAG_FILTER)
    assert [r.id for r in found] == ["i-live"]
    assert found[0].tagged is True


def test_ebs_volumes_carry_state_for_the_delete_step(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok({"Volumes": [{"VolumeId": "vol-1", "CreateTime": "2026-09-01T00:00:00+00:00", "State": "in-use", "Tags": []}]}),
    )
    found = cloud_sweep.list_ebs_volumes(REGION, TAG_FILTER)
    assert found[0].extra["state"] == "in-use"
    assert found[0].tagged is False


def test_security_groups_skip_the_default_group(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok(
            {
                "SecurityGroups": [
                    {"GroupId": "sg-default", "GroupName": "default", "Tags": []},
                    {"GroupId": "sg-dfe", "GroupName": "dfe-nodes", "Tags": DFE_TAGS},
                ]
            }
        ),
    )
    found = cloud_sweep.list_security_groups(REGION, TAG_FILTER)
    assert [r.id for r in found] == ["sg-dfe"]
    assert found[0].tagged is True


def test_enis_read_tags_from_tag_set_not_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """ENIs are the one EC2 class the describe call answers under TagSet."""
    _mock_run(
        monkeypatch,
        _ok({"NetworkInterfaces": [{"NetworkInterfaceId": "eni-1", "TagSet": DFE_TAGS}]}),
    )
    found = cloud_sweep.list_enis(REGION, TAG_FILTER)
    assert found[0].tagged is True


def test_vpcs_skip_the_default_vpc(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok(
            {
                "Vpcs": [
                    {"VpcId": "vpc-default", "IsDefault": True, "Tags": []},
                    {"VpcId": "vpc-dfe", "IsDefault": False, "Tags": DFE_TAGS},
                ]
            }
        ),
    )
    found = cloud_sweep.list_vpcs(REGION, TAG_FILTER)
    assert [r.id for r in found] == ["vpc-dfe"]


def test_subnets_skip_default_vpc_subnets(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok({"Vpcs": [{"VpcId": "vpc-default", "IsDefault": True}]}),  # _default_vpc_ids lookup
        _ok(
            {
                "Subnets": [
                    {"SubnetId": "subnet-def", "VpcId": "vpc-default", "DefaultForAz": True, "Tags": []},
                    {"SubnetId": "subnet-dfe", "VpcId": "vpc-dfe", "DefaultForAz": False, "Tags": DFE_TAGS},
                ]
            }
        ),
    )
    found = cloud_sweep.list_subnets(REGION, TAG_FILTER)
    assert [r.id for r in found] == ["subnet-dfe"]


def test_route_tables_skip_the_main_table(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok({"Vpcs": []}),
        _ok(
            {
                "RouteTables": [
                    {"RouteTableId": "rtb-main", "VpcId": "vpc-dfe", "Associations": [{"Main": True}], "Tags": []},
                    {"RouteTableId": "rtb-dfe", "VpcId": "vpc-dfe", "Associations": [{"Main": False}], "Tags": DFE_TAGS},
                ]
            }
        ),
    )
    found = cloud_sweep.list_route_tables(REGION, TAG_FILTER)
    assert [r.id for r in found] == ["rtb-dfe"]


def test_internet_gateways_carry_the_attached_vpc_for_detach(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok({"Vpcs": [{"VpcId": "vpc-default", "IsDefault": True}]}),
        _ok(
            {
                "InternetGateways": [
                    {"InternetGatewayId": "igw-default", "Attachments": [{"VpcId": "vpc-default"}], "Tags": []},
                    {"InternetGatewayId": "igw-dfe", "Attachments": [{"VpcId": "vpc-dfe"}], "Tags": DFE_TAGS},
                ]
            }
        ),
    )
    found = cloud_sweep.list_internet_gateways(REGION, TAG_FILTER)
    assert [r.id for r in found] == ["igw-dfe"]
    assert found[0].extra["vpc_id"] == "vpc-dfe"


def test_load_balancers_and_target_groups_read_tags_from_a_separate_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok({"LoadBalancers": [{"LoadBalancerArn": "arn:lb:1", "LoadBalancerName": "dfe-lb", "CreatedTime": "2026-09-01T00:00:00+00:00"}]}),
        _ok({"TagDescriptions": [{"ResourceArn": "arn:lb:1", "Tags": DFE_TAGS}]}),
    )
    found = cloud_sweep.list_load_balancers(REGION, TAG_FILTER)
    assert found[0].tagged is True
    assert found[0].id == "arn:lb:1"


def test_eks_clusters_read_tags_as_a_plain_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """EKS (and MSK) return tags as {key: value}, unlike EC2's list of Key/Value pairs."""
    _mock_run(
        monkeypatch,
        _ok({"clusters": ["dfe-cluster"]}),
        _ok({"cluster": {"name": "dfe-cluster", "createdAt": "2026-09-01T00:00:00+00:00", "tags": {"service-name": "dfe", "environment": "test"}}}),
    )
    found = cloud_sweep.list_eks_clusters(REGION, TAG_FILTER)
    assert found[0].tagged is True


def test_msk_clusters_skip_ones_already_deleting(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok(
            {
                "ClusterInfoList": [
                    {"ClusterArn": "arn:msk:1", "ClusterName": "gone", "State": "DELETING", "Tags": {}},
                    {"ClusterArn": "arn:msk:2", "ClusterName": "live", "State": "ACTIVE", "Tags": {"service-name": "dfe", "environment": "test"}},
                ]
            }
        ),
    )
    found = cloud_sweep.list_msk_clusters(REGION, TAG_FILTER)
    assert [r.name for r in found] == ["live"]


def test_secrets_flag_ones_already_scheduled_for_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok(
            {
                "SecretList": [
                    {"ARN": "arn:secret:1", "Name": "dfe/db", "CreatedDate": "2026-09-01T00:00:00+00:00", "DeletedDate": "2026-09-10T00:00:00+00:00", "Tags": DFE_TAGS}
                ]
            }
        ),
    )
    found = cloud_sweep.list_secrets(REGION, TAG_FILTER)
    assert found[0].extra["scheduled_deletion"] == "True"
    assert found[0].tagged is True


def test_kms_aliases_skip_the_aws_managed_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok(
            {
                "Aliases": [
                    {"AliasName": "alias/aws/s3", "TargetKeyId": "k-aws"},
                    {"AliasName": "alias/dfe-key", "TargetKeyId": "k-dfe"},
                ]
            }
        ),
        _ok({"Tags": [{"TagKey": "service-name", "TagValue": "dfe"}, {"TagKey": "environment", "TagValue": "test"}]}),
    )
    found = cloud_sweep.list_kms_aliases(REGION, TAG_FILTER)
    assert [r.id for r in found] == ["alias/dfe-key"]
    assert found[0].tagged is True


def test_s3_buckets_treat_a_tagging_error_as_untagged(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok({"Buckets": [{"Name": "hyperi-s3-dfe-test-tfstate", "CreationDate": "2026-08-01T00:00:00+00:00"}]}),
        _fail("An error occurred (NoSuchTagSet)"),
    )
    found = cloud_sweep.list_s3_buckets(REGION, TAG_FILTER, exclude_bucket="hyperi-s3-dfe-test-tfstate")
    assert found[0].tagged is False
    assert found[0].extra["excluded"] == "True"


def test_the_tagging_api_paginates_and_labels_kind_by_arn_service(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok(
            {
                "ResourceTagMappingList": [{"ResourceARN": "arn:aws:rds:us-west-2:0:db:dfe-db", "Tags": DFE_TAGS}],
                "PaginationToken": "next",
            }
        ),
        _ok({"ResourceTagMappingList": [{"ResourceARN": "arn:aws:rds:us-west-2:0:db:dfe-db-2", "Tags": DFE_TAGS}]}),
    )
    found = cloud_sweep.list_tagged(REGION, TAG_FILTER)
    assert [r.name for r in found] == ["dfe-db", "dfe-db-2"]
    assert all(r.kind == "tagged:rds" for r in found)


# ---------------------------------------------------------------------------
# collect(): de-duplication between the tagging API and the dedicated listers
# ---------------------------------------------------------------------------


def test_collect_drops_a_tagging_api_hit_already_found_by_a_dedicated_lister(monkeypatch: pytest.MonkeyPatch) -> None:
    resource = cloud_sweep.Resource(kind="ec2-instance", id="i-dupe", name="i-dupe", created=None, tagged=True)
    monkeypatch.setattr(cloud_sweep, "PER_SERVICE_COLLECTORS", [lambda region, tf: [resource]])
    monkeypatch.setattr(cloud_sweep, "list_s3_buckets", lambda region, tf, bucket: [])
    monkeypatch.setattr(
        cloud_sweep,
        "list_tagged",
        lambda region, tf: [
            cloud_sweep.Resource(kind="tagged:ec2", id="arn:aws:ec2:us-west-2:0:instance/i-dupe", name="i-dupe", created=None, tagged=True)
        ],
    )
    found = cloud_sweep.collect(REGION, TAG_FILTER, None)
    assert [r.id for r in found] == ["i-dupe"]


def test_collect_keeps_a_tagging_api_hit_no_lister_covers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud_sweep, "PER_SERVICE_COLLECTORS", [])
    monkeypatch.setattr(cloud_sweep, "list_s3_buckets", lambda region, tf, bucket: [])
    monkeypatch.setattr(
        cloud_sweep,
        "list_tagged",
        lambda region, tf: [cloud_sweep.Resource(kind="tagged:rds", id="arn:aws:rds:us-west-2:0:db:dfe-db", name="dfe-db", created=None, tagged=True)],
    )
    found = cloud_sweep.collect(REGION, TAG_FILTER, None)
    assert [r.name for r in found] == ["dfe-db"]


# ---------------------------------------------------------------------------
# Exclusions: the state bucket, and untagged resources without --include-untagged
# ---------------------------------------------------------------------------


def _resource(kind: str, rid: str, *, tagged: bool) -> cloud_sweep.Resource:
    return cloud_sweep.Resource(kind=kind, id=rid, name=rid, created=None, tagged=tagged)


def test_the_excluded_bucket_is_never_eligible_even_if_tagged() -> None:
    resources = [_resource("s3-bucket", "hyperi-s3-dfe-test-tfstate", tagged=True), _resource("ec2-instance", "i-1", tagged=True)]
    eligible = cloud_sweep.filter_for_delete(resources, include_untagged=False, exclude_bucket="hyperi-s3-dfe-test-tfstate")
    assert [r.id for r in eligible] == ["i-1"]


def test_untagged_resources_are_excluded_unless_include_untagged() -> None:
    resources = [_resource("ec2-instance", "i-tagged", tagged=True), _resource("ec2-instance", "i-bare", tagged=False)]
    assert [r.id for r in cloud_sweep.filter_for_delete(resources, include_untagged=False, exclude_bucket=None)] == ["i-tagged"]
    both = cloud_sweep.filter_for_delete(resources, include_untagged=True, exclude_bucket=None)
    assert {r.id for r in both} == {"i-tagged", "i-bare"}


# ---------------------------------------------------------------------------
# Dependency order
# ---------------------------------------------------------------------------


def test_delete_order_runs_workloads_and_lbs_before_clusters_before_network_before_the_rest() -> None:
    order = cloud_sweep.DELETE_ORDER
    assert order.index("ec2-instance") < order.index("eks-cluster")
    assert order.index("load-balancer") < order.index("eks-cluster")
    assert order.index("eks-cluster") < order.index("nat-gateway")
    assert order.index("msk-cluster") < order.index("vpc")
    assert order.index("nat-gateway") < order.index("vpc")
    assert order.index("vpc") < order.index("s3-bucket")


def test_delete_resources_calls_each_kind_in_delete_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cloud_sweep, "DELETE_FNS", {
        "vpc": lambda r, region: calls.append("vpc"),
        "ec2-instance": lambda r, region: calls.append("ec2-instance"),
        "eks-cluster": lambda r, region: calls.append("eks-cluster"),
    })
    resources = [_resource("vpc", "vpc-1", tagged=True), _resource("ec2-instance", "i-1", tagged=True), _resource("eks-cluster", "c-1", tagged=True)]
    failures = cloud_sweep.delete_resources(resources, REGION)
    assert calls == ["ec2-instance", "eks-cluster", "vpc"]
    assert failures == []


def test_delete_resources_reports_a_failure_and_keeps_going(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(r: cloud_sweep.Resource, region: str) -> None:
        raise cloud_sweep.CloudSweepError("still attached")

    calls: list[str] = []
    monkeypatch.setattr(cloud_sweep, "DELETE_FNS", {
        "ebs-volume": boom,
        "vpc": lambda r, region: calls.append("vpc"),
    })
    resources = [_resource("ebs-volume", "vol-1", tagged=True), _resource("vpc", "vpc-1", tagged=True)]
    failures = cloud_sweep.delete_resources(resources, REGION)
    assert calls == ["vpc"]
    assert failures == ["ebs-volume vol-1: still attached"]


def test_delete_resources_names_a_kind_with_no_scripted_delete() -> None:
    resources = [_resource("tagged:rds", "arn:aws:rds:us-west-2:0:db:dfe-db", tagged=True)]
    failures = cloud_sweep.delete_resources(resources, REGION)
    assert "no scripted delete" in failures[0]


# ---------------------------------------------------------------------------
# Exit code
# ---------------------------------------------------------------------------


def test_main_exits_zero_when_nothing_is_eligible(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(
        cloud_sweep,
        "collect",
        lambda region, tf, bucket: [_resource("s3-bucket", "hyperi-s3-dfe-test-tfstate", tagged=False)],
    )
    code = cloud_sweep.main(["--region", REGION])
    assert code == 0
    assert "sweep clean" in capsys.readouterr().out


def test_main_exits_one_when_a_tagged_resource_remains(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(cloud_sweep, "collect", lambda region, tf, bucket: [_resource("ec2-instance", "i-1", tagged=True)])
    code = cloud_sweep.main(["--region", REGION])
    assert code == 1
    assert "eligible for --delete" in capsys.readouterr().out


def test_main_with_delete_recollects_and_reports_what_survived(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    first = [_resource("ec2-instance", "i-1", tagged=True)]
    second: list[cloud_sweep.Resource] = []
    responses = iter([first, second])
    monkeypatch.setattr(cloud_sweep, "collect", lambda region, tf, bucket: next(responses))
    monkeypatch.setattr(cloud_sweep, "delete_resources", lambda resources, region: [])
    code = cloud_sweep.main(["--region", REGION, "--delete"])
    assert code == 0
    assert "sweep clean" in capsys.readouterr().out


def test_parse_tag_filter_splits_comma_separated_pairs() -> None:
    assert cloud_sweep.parse_tag_filter("service-name=dfe,environment=test") == {
        "service-name": "dfe",
        "environment": "test",
    }
