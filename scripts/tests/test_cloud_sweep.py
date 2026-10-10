#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_cloud_sweep.py
#  Purpose:      Guard the post-proof AWS sweep: each class parses the aws CLI
#                shape it actually returns, deletion runs in dependency order,
#                the excluded bucket and untagged resources are never touched
#                without --include-untagged, --expired selects only run-tagged
#                resources past their expiry, and the exit code tracks what
#                remains.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for `cloud_sweep.py`.

    python3 -m pytest scripts/tests/test_cloud_sweep.py -q

Every test replaces `aws_cli.run_aws` with a fake answering fixture JSON lifted
from the real aws CLI shapes (list-of-Key/Value tags for EC2, TagSet for ENIs,
a plain dict for EKS/MSK) -- no network call and no real account is touched.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import cloud_run  # noqa: E402
import cloud_sweep  # noqa: E402

REGION = "us-west-2"
TAG_FILTER = {"service-name": "dfe", "environment": "test"}
DFE_TAGS = [{"Key": "service-name", "Value": "dfe"}, {"Key": "environment", "Value": "test"}]
# 1791460800 is 2026-10-08T12:00:00Z; every expiry case below is judged against it.
NOW = 1791460800


def _ok(stdout_obj: object) -> subprocess.CompletedProcess:
    """A successful aws CLI call returning this JSON body."""
    return subprocess.CompletedProcess(args=["aws"], returncode=0, stdout=json.dumps(stdout_obj), stderr="")


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["aws"], returncode=1, stdout="", stderr=stderr)


def _mock_run(monkeypatch: pytest.MonkeyPatch, *responses: subprocess.CompletedProcess) -> None:
    """Replace aws_cli.run_aws with one that returns `responses` in call order."""
    queue = list(responses)

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        return queue.pop(0)

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)


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


def test_vpc_endpoints_skip_the_deleted_and_the_default_vpcs_in_either_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deleted endpoint is not a leftover, and the CLI spells its State capitalised."""
    _mock_run(
        monkeypatch,
        _ok({"Vpcs": [{"VpcId": "vpc-default", "IsDefault": True}]}),  # _default_vpc_ids lookup
        _ok(
            {
                "VpcEndpoints": [
                    {"VpcEndpointId": "vpce-deleted", "VpcId": "vpc-dfe", "State": "Deleted", "Tags": DFE_TAGS},
                    {"VpcEndpointId": "vpce-lower", "VpcId": "vpc-dfe", "State": "deleted", "Tags": DFE_TAGS},
                    {"VpcEndpointId": "vpce-deleting", "VpcId": "vpc-dfe", "State": "Deleting", "Tags": DFE_TAGS},
                    {"VpcEndpointId": "vpce-live", "VpcId": "vpc-dfe", "State": "Available", "Tags": DFE_TAGS},
                    {"VpcEndpointId": "vpce-default", "VpcId": "vpc-default", "State": "Available", "Tags": []},
                ]
            }
        ),
    )
    found = cloud_sweep.list_vpc_endpoints(REGION, TAG_FILTER)
    assert [r.id for r in found] == ["vpce-deleting", "vpce-live"]


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
        _ok({"Buckets": [{"Name": "example-tfstate-bucket", "CreationDate": "2026-08-01T00:00:00+00:00"}]}),
        _fail("An error occurred (NoSuchTagSet)"),
    )
    found = cloud_sweep.list_s3_buckets(REGION, TAG_FILTER, exclude_bucket="example-tfstate-bucket")
    assert found[0].tagged is False
    assert found[0].extra["excluded"] == "True"


def test_the_s3_listing_is_sent_to_the_swept_region_not_the_shells(monkeypatch: pytest.MonkeyPatch) -> None:
    """ListBuckets is a global call, so without --region the CLI sends it wherever the
    shell's region points -- which an account fenced to one region denies."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return _ok({"Buckets": []}) if args[:2] == ["s3api", "list-buckets"] else _ok({})

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    assert cloud_sweep.list_s3_buckets(REGION, TAG_FILTER, exclude_bucket=None) == []
    assert calls[0][:2] == ["s3api", "list-buckets"]
    assert calls[0][calls[0].index("--region") + 1] == REGION


@pytest.mark.parametrize("call", [cloud_sweep.run_aws, cloud_sweep.run_aws_text])
def test_expected_fail_a_call_with_no_region_is_refused_before_the_cli_runs(
    monkeypatch: pytest.MonkeyPatch, call: object
) -> None:
    def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        raise AssertionError("a call with no region must never reach the aws CLI")

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", boom)
    with pytest.raises(cloud_sweep.CloudSweepError, match="names no region"):
        call(["s3api", "list-buckets"], "")


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
    found = cloud_sweep.list_tagged(REGION, TAG_FILTER, TAG_FILTER)
    assert [r.name for r in found] == ["dfe-db", "dfe-db-2"]
    assert all(r.kind == "tagged:rds" for r in found)
    assert all(r.tagged for r in found)


def test_the_tagging_api_asks_for_a_bare_key_when_the_value_is_any(monkeypatch: pytest.MonkeyPatch) -> None:
    """--expired finds every run-tagged resource whatever its run id, so the
    filter names the key alone -- Values= would match only one run."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return _ok({"ResourceTagMappingList": []})

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    cloud_sweep.list_tagged(REGION, {"dfe-e2e": None}, TAG_FILTER)
    assert "Key=dfe-e2e" in calls[0]
    assert not any(arg.startswith("Key=dfe-e2e,Values") for arg in calls[0])


def test_the_tagging_api_carries_each_resources_own_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok(
            {
                "ResourceTagMappingList": [
                    {
                        "ResourceARN": "arn:aws:rds:us-west-2:0:db:run-db",
                        "Tags": [{"Key": "dfe-e2e", "Value": "run-1"}, {"Key": "expires-at", "Value": "1"}],
                    }
                ]
            }
        ),
    )
    found = cloud_sweep.list_tagged(REGION, {"dfe-e2e": None}, TAG_FILTER)
    assert found[0].tags == {"dfe-e2e": "run-1", "expires-at": "1"}
    assert found[0].tagged is False  # it carries the run tag, not the governance filter


# ---------------------------------------------------------------------------
# collect(): de-duplication between the tagging API and the dedicated listers
# ---------------------------------------------------------------------------


def test_collect_drops_a_tagging_api_hit_already_found_by_a_dedicated_lister(monkeypatch: pytest.MonkeyPatch) -> None:
    resource = cloud_sweep.Resource(kind="ec2-instance", id="i-dupe", name="i-dupe", created=None, tagged=True)
    monkeypatch.setattr(cloud_sweep, "PER_SERVICE_COLLECTORS", [lambda region, tf: [resource]])
    monkeypatch.setattr(cloud_sweep, "list_s3_buckets", lambda region, tf, bucket, **_: [])
    monkeypatch.setattr(
        cloud_sweep,
        "list_tagged",
        lambda region, sf, tf: [
            cloud_sweep.Resource(kind="tagged:ec2", id="arn:aws:ec2:us-west-2:0:instance/i-dupe", name="i-dupe", created=None, tagged=True)
        ],
    )
    found = cloud_sweep.collect(REGION, TAG_FILTER, None)
    assert [r.id for r in found] == ["i-dupe"]


def test_collect_keeps_a_tagging_api_hit_no_lister_covers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud_sweep, "PER_SERVICE_COLLECTORS", [])
    monkeypatch.setattr(cloud_sweep, "list_s3_buckets", lambda region, tf, bucket, **_: [])
    monkeypatch.setattr(
        cloud_sweep,
        "list_tagged",
        lambda region, sf, tf: [cloud_sweep.Resource(kind="tagged:rds", id="arn:aws:rds:us-west-2:0:db:dfe-db", name="dfe-db", created=None, tagged=True)],
    )
    found = cloud_sweep.collect(REGION, TAG_FILTER, None)
    assert [r.name for r in found] == ["dfe-db"]


def test_collect_asks_the_tagging_api_for_the_route1_filter_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(cloud_sweep, "PER_SERVICE_COLLECTORS", [])
    monkeypatch.setattr(cloud_sweep, "list_s3_buckets", lambda region, tf, bucket, **_: [])
    monkeypatch.setattr(cloud_sweep, "list_tagged", lambda region, sf, tf: seen.append(sf) or [])
    cloud_sweep.collect(REGION, TAG_FILTER, None)
    cloud_sweep.collect(REGION, TAG_FILTER, None, route1_filter={"dfe-e2e": None})
    assert seen == [TAG_FILTER, {"dfe-e2e": None}]


# ---------------------------------------------------------------------------
# Exclusions: the state bucket, and untagged resources without --include-untagged
# ---------------------------------------------------------------------------


def _resource(kind: str, rid: str, *, tagged: bool) -> cloud_sweep.Resource:
    return cloud_sweep.Resource(kind=kind, id=rid, name=rid, created=None, tagged=tagged)


def test_the_excluded_bucket_is_never_eligible_even_if_tagged() -> None:
    resources = [_resource("s3-bucket", "example-tfstate-bucket", tagged=True), _resource("ec2-instance", "i-1", tagged=True)]
    eligible = cloud_sweep.filter_for_delete(resources, include_untagged=False, exclude_bucket="example-tfstate-bucket")
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


def test_delete_order_removes_vpc_endpoints_before_the_security_groups_their_enis_hold() -> None:
    """An interface endpoint's network interfaces hold its security groups, so a
    group deleted first fails with DependencyViolation."""
    order = cloud_sweep.DELETE_ORDER
    assert order.index("vpc-endpoint") < order.index("security-group")
    assert order.index("vpc-endpoint") < order.index("eni")


def test_every_kind_in_delete_order_has_a_delete_and_every_delete_has_a_place() -> None:
    assert set(cloud_sweep.DELETE_ORDER) == set(cloud_sweep.DELETE_FNS)


def test_a_vpc_endpoint_delete_waits_until_the_endpoint_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    responses = [
        _ok({}),  # delete-vpc-endpoints
        _ok({"VpcEndpoints": [{"VpcEndpointId": "vpce-1", "State": "Deleting"}]}),
        _fail("An error occurred (InvalidVpcEndpointId.NotFound)"),
    ]

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return responses.pop(0)

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    monkeypatch.setattr(cloud_sweep.time, "sleep", lambda _seconds: None)
    cloud_sweep._delete_vpc_endpoint(_resource("vpc-endpoint", "vpce-1", tagged=True), REGION)
    assert [c[1] for c in calls] == ["delete-vpc-endpoints", "describe-vpc-endpoints", "describe-vpc-endpoints"]


def test_a_nat_gateway_delete_waits_until_the_gateway_is_deleting_or_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    responses = [
        _ok({}),  # delete-nat-gateway
        _ok({"NatGateways": [{"NatGatewayId": "nat-1", "State": "available"}]}),
        _ok({"NatGateways": [{"NatGatewayId": "nat-1", "State": "deleting"}]}),
    ]

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return responses.pop(0)

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    monkeypatch.setattr(cloud_sweep.time, "sleep", lambda _seconds: None)
    cloud_sweep._delete_nat_gateway(_resource("nat-gateway", "nat-1", tagged=True), REGION)
    assert [c[1] for c in calls] == ["delete-nat-gateway", "describe-nat-gateways", "describe-nat-gateways"]


def test_a_nat_gateway_delete_is_done_once_aws_no_longer_lists_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [_ok({}), _fail("An error occurred (NatGatewayNotFound) when calling the DescribeNatGateways operation")]
    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", lambda *_a, **_k: responses.pop(0))
    monkeypatch.setattr(cloud_sweep.time, "sleep", lambda _seconds: None)
    cloud_sweep._delete_nat_gateway(_resource("nat-gateway", "nat-1", tagged=True), REGION)
    assert responses == []


# ---------------------------------------------------------------------------
# A tagging-API hit the cloud has already deleted
# ---------------------------------------------------------------------------

ACCOUNT_ID = "000000000000"
NAT_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:natgateway/nat-1"
ENDPOINT_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:vpc-endpoint/vpce-1"
INSTANCE_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:instance/i-1"
NAT_NOT_FOUND = "An error occurred (NatGatewayNotFound) when calling the DescribeNatGateways operation: nat-1"
ENDPOINT_NOT_FOUND = "An error occurred (InvalidVpcEndpointId.NotFound) when calling the DescribeVpcEndpoints operation"
INSTANCE_NOT_FOUND = "An error occurred (InvalidInstanceID.NotFound) when calling the DescribeInstances operation"


def _tagging_hit(arn: str, kind: str = "tagged:ec2") -> cloud_sweep.Resource:
    return cloud_sweep.Resource(kind=kind, id=arn, name=arn.rsplit("/", 1)[-1], created=None, tagged=True)


def _nat(state: str) -> subprocess.CompletedProcess:
    return _ok({"NatGateways": [{"NatGatewayId": "nat-1", "State": state}]})


def _endpoint(state: str) -> subprocess.CompletedProcess:
    return _ok({"VpcEndpoints": [{"VpcEndpointId": "vpce-1", "State": state}]})


def _instance(state: str) -> subprocess.CompletedProcess:
    return _ok({"Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": state}}]}]})


@pytest.mark.parametrize(
    ("arn", "answer", "gone"),
    [
        pytest.param(NAT_ARN, _nat("deleted"), True, id="nat-gateway-deleted"),
        pytest.param(NAT_ARN, _fail(NAT_NOT_FOUND), True, id="nat-gateway-not-found"),
        pytest.param(NAT_ARN, _nat("deleting"), False, id="nat-gateway-deleting"),
        pytest.param(NAT_ARN, _nat("available"), False, id="nat-gateway-available"),
        pytest.param(NAT_ARN, _nat("pending"), False, id="nat-gateway-pending"),
        pytest.param(ENDPOINT_ARN, _endpoint("Deleted"), True, id="vpc-endpoint-deleted"),
        pytest.param(ENDPOINT_ARN, _fail(ENDPOINT_NOT_FOUND), True, id="vpc-endpoint-not-found"),
        pytest.param(ENDPOINT_ARN, _endpoint("Deleting"), False, id="vpc-endpoint-deleting"),
        pytest.param(ENDPOINT_ARN, _endpoint("Available"), False, id="vpc-endpoint-available"),
        pytest.param(INSTANCE_ARN, _instance("terminated"), True, id="instance-terminated"),
        pytest.param(INSTANCE_ARN, _fail(INSTANCE_NOT_FOUND), True, id="instance-not-found"),
        pytest.param(INSTANCE_ARN, _instance("shutting-down"), False, id="instance-shutting-down"),
        pytest.param(INSTANCE_ARN, _instance("stopped"), False, id="instance-stopped"),
        pytest.param(INSTANCE_ARN, _instance("running"), False, id="instance-running"),
        pytest.param(INSTANCE_ARN, _ok({"Reservations": []}), True, id="instance-no-longer-returned"),
    ],
)
def test_a_tagging_hit_is_gone_when_aws_reports_it_deleted_or_unknown(
    monkeypatch: pytest.MonkeyPatch, arn: str, answer: subprocess.CompletedProcess, gone: bool
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return answer

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    assert cloud_sweep.tagging_hit_gone(_tagging_hit(arn), REGION) is gone
    (call,) = calls
    assert arn.rsplit("/", 1)[-1] in call, "the describe call names the bare id"
    assert arn not in call, "the ARN is not an id the describe calls accept"


@pytest.mark.parametrize("arn", [NAT_ARN, ENDPOINT_ARN, INSTANCE_ARN])
def test_a_tagging_hit_whose_state_cannot_be_read_raises_rather_than_reading_as_gone(
    monkeypatch: pytest.MonkeyPatch, arn: str
) -> None:
    _mock_run(monkeypatch, _fail("An error occurred (UnauthorizedOperation) when calling the Describe operation"))
    with pytest.raises(cloud_sweep.CloudSweepError, match="UnauthorizedOperation"):
        cloud_sweep.tagging_hit_gone(_tagging_hit(arn), REGION)


@pytest.mark.parametrize(
    "hit",
    [
        pytest.param(_tagging_hit(f"arn:aws:rds:{REGION}:{ACCOUNT_ID}:db:run-db", "tagged:rds"), id="another-service"),
        pytest.param(_tagging_hit(f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:volume/vol-1"), id="another-ec2-class"),
        pytest.param(_tagging_hit("natgateway/nat-1"), id="not-an-arn"),
        pytest.param(_tagging_hit(NAT_ARN, "nat-gateway"), id="a-lister-resource"),
    ],
)
def test_a_hit_with_no_gone_check_is_never_judged_gone(
    monkeypatch: pytest.MonkeyPatch, hit: cloud_sweep.Resource
) -> None:
    def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        raise AssertionError("nothing is described for a class with no gone-check")

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", boom)
    assert cloud_sweep.tagging_hit_gone(hit, REGION) is False


def test_a_route53_zone_loses_every_record_but_its_apex_soa_and_ns_before_the_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DeleteHostedZone refuses a populated zone, and Route 53 refuses to delete
    the apex SOA and NS itself -- so everything else goes first, and only that."""
    records = [
        {"Name": "run.example.com.", "Type": "SOA", "TTL": 900, "ResourceRecords": [{"Value": "soa"}]},
        {"Name": "run.example.com.", "Type": "NS", "TTL": 172800, "ResourceRecords": [{"Value": "ns-1."}]},
        {"Name": "ui.run.example.com.", "Type": "A", "TTL": 60, "ResourceRecords": [{"Value": "192.0.2.10"}]},
        {"Name": "sub.run.example.com.", "Type": "NS", "TTL": 300, "ResourceRecords": [{"Value": "ns-9."}]},
    ]
    calls: list[list[str]] = []
    responses = [_ok({"ResourceRecordSets": records}), _ok({}), _ok({})]

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return responses.pop(0)

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    zone = cloud_sweep.Resource(kind="route53-zone", id="Z123", name="run.example.com.", created=None, tagged=True)
    cloud_sweep._delete_route53_zone(zone, REGION)

    assert [c[1] for c in calls] == [
        "list-resource-record-sets",
        "change-resource-record-sets",
        "delete-hosted-zone",
    ]
    batch = json.loads(calls[1][calls[1].index("--change-batch") + 1])
    deleted = [(c["ResourceRecordSet"]["Name"], c["ResourceRecordSet"]["Type"]) for c in batch["Changes"]]
    assert deleted == [("ui.run.example.com.", "A"), ("sub.run.example.com.", "NS")]
    assert all(c["Action"] == "DELETE" for c in batch["Changes"])


def test_an_empty_route53_zone_is_deleted_with_no_change_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    apex = [
        {"Name": "run.example.com.", "Type": "SOA", "ResourceRecords": []},
        {"Name": "RUN.example.com", "Type": "NS", "ResourceRecords": []},
    ]
    responses = [_ok({"ResourceRecordSets": apex}), _ok({})]

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return responses.pop(0)

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    zone = cloud_sweep.Resource(kind="route53-zone", id="Z123", name="run.example.com.", created=None, tagged=True)
    cloud_sweep._delete_route53_zone(zone, REGION)
    assert [c[1] for c in calls] == ["list-resource-record-sets", "delete-hosted-zone"]


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
        lambda region, tf, bucket, **_: [_resource("s3-bucket", "example-tfstate-bucket", tagged=False)],
    )
    code = cloud_sweep.main(["--region", REGION])
    assert code == 0
    assert "sweep clean" in capsys.readouterr().out


def test_main_exits_one_when_a_tagged_resource_remains(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(cloud_sweep, "collect", lambda region, tf, bucket, **_: [_resource("ec2-instance", "i-1", tagged=True)])
    code = cloud_sweep.main(["--region", REGION])
    assert code == 1
    assert "eligible for --delete" in capsys.readouterr().out


def test_main_with_delete_recollects_and_reports_what_survived(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    first = [_resource("ec2-instance", "i-1", tagged=True)]
    second: list[cloud_sweep.Resource] = []
    responses = iter([first, second])
    monkeypatch.setattr(cloud_sweep, "verify_account", lambda region, account: None)
    monkeypatch.setattr(cloud_sweep, "collect", lambda region, tf, bucket, **_: next(responses))
    monkeypatch.setattr(cloud_sweep, "delete_resources", lambda resources, region: [])
    code = cloud_sweep.main(["--region", REGION, "--delete", "--account", "000000000000", "--yes"])
    assert code == 0
    assert "sweep clean" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --delete's account guard and confirmation prompt
# ---------------------------------------------------------------------------


def test_verify_account_passes_when_the_identity_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _ok({"Account": "000000000000"}))
    cloud_sweep.verify_account(REGION, "000000000000")  # must not raise


def test_verify_account_raises_on_a_mismatched_account(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _ok({"Account": "111111111111"}))
    with pytest.raises(cloud_sweep.CloudSweepError, match="000000000000") as excinfo:
        cloud_sweep.verify_account(REGION, "000000000000")
    assert "111111111111" in str(excinfo.value)


def test_delete_without_account_is_refused_before_any_aws_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        raise AssertionError("no aws call should happen before --account is checked")

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", boom)
    code = cloud_sweep.main(["--region", REGION, "--delete"])
    assert code == 2
    assert "--account" in capsys.readouterr().err


def test_delete_refuses_when_the_authenticated_account_does_not_match(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _mock_run(monkeypatch, _ok({"Account": "111111111111"}))
    code = cloud_sweep.main(["--region", REGION, "--delete", "--account", "000000000000"])
    assert code == 2
    assert "does not match" in capsys.readouterr().err


def test_delete_without_yes_prompts_and_cancels_on_anything_but_yes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(cloud_sweep, "verify_account", lambda region, account: None)
    monkeypatch.setattr(
        cloud_sweep, "collect", lambda region, tf, bucket, **_: [_resource("ec2-instance", "i-1", tagged=True)]
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    code = cloud_sweep.main(["--region", REGION, "--delete", "--account", "000000000000"])
    assert code == 1
    assert "cancelled" in capsys.readouterr().err


def test_delete_with_yes_skips_the_prompt_and_deletes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(cloud_sweep, "verify_account", lambda region, account: None)
    first = [_resource("ec2-instance", "i-1", tagged=True)]
    second: list[cloud_sweep.Resource] = []
    responses = iter([first, second])
    monkeypatch.setattr(cloud_sweep, "collect", lambda region, tf, bucket, **_: next(responses))
    monkeypatch.setattr(cloud_sweep, "delete_resources", lambda resources, region: [])

    def refuse_to_prompt(prompt: str) -> str:
        raise AssertionError("--yes must skip the interactive confirmation entirely")

    monkeypatch.setattr("builtins.input", refuse_to_prompt)
    code = cloud_sweep.main(["--region", REGION, "--delete", "--account", "000000000000", "--yes"])
    assert code == 0
    assert "sweep clean" in capsys.readouterr().out


def test_parse_tag_filter_splits_comma_separated_pairs() -> None:
    assert cloud_sweep.parse_tag_filter("service-name=dfe,environment=test") == {
        "service-name": "dfe",
        "environment": "test",
    }


# ---------------------------------------------------------------------------
# --expired: only run-tagged resources past expiry plus grace
# ---------------------------------------------------------------------------


def _run_resource(rid: str, tags: dict[str, str], kind: str = "ec2-instance") -> cloud_sweep.Resource:
    return cloud_sweep.Resource(kind=kind, id=rid, name=rid, created=None, tagged=True, tags=tags)


def _expiry(grace: int = 0) -> cloud_sweep.ExpirySelection:
    return cloud_sweep.ExpirySelection(keys=cloud_run.RunTagKeys(), now=NOW, grace=grace)


def _expired_ids(resources: list[cloud_sweep.Resource], grace: int = 0) -> list[str]:
    return [r.id for r in cloud_sweep.filter_expired(resources, expiry=_expiry(grace), exclude_bucket=None)]


def test_expired_selects_a_run_resource_whose_expiry_is_in_the_past() -> None:
    past = _run_resource("i-past", {"dfe-e2e": "run-1", "expires-at": "2026-10-08T11:00:00Z"})
    assert _expired_ids([past]) == ["i-past"]


def test_expired_reads_an_epoch_expiry_as_well_as_iso8601() -> None:
    epoch = _run_resource("i-epoch", {"dfe-e2e": "run-1", "expires-at": str(NOW - 60)})
    assert _expired_ids([epoch]) == ["i-epoch"]


def test_expected_fail_a_future_expiry_is_never_selected() -> None:
    future = _run_resource("i-future", {"dfe-e2e": "run-1", "expires-at": "2026-10-08T13:00:00Z"})
    assert _expired_ids([future]) == []


def test_expected_fail_a_resource_without_the_run_tag_is_never_selected() -> None:
    """Untagged, governance-tagged only, or carrying just an expiry: none is a run's."""
    resources = [
        _run_resource("i-bare", {}),
        _run_resource("i-governance", {"service-name": "dfe", "environment": "test"}),
        _run_resource("i-expiry-only", {"expires-at": "2026-10-08T11:00:00Z"}),
        _run_resource("i-empty-run", {"dfe-e2e": "", "expires-at": "2026-10-08T11:00:00Z"}),
    ]
    assert _expired_ids(resources) == []


def test_expected_fail_a_malformed_or_missing_expiry_is_never_selected() -> None:
    resources = [
        _run_resource("i-no-expiry", {"dfe-e2e": "run-1"}),
        _run_resource("i-words", {"dfe-e2e": "run-1", "expires-at": "yesterday"}),
        _run_resource("i-naive", {"dfe-e2e": "run-1", "expires-at": "2026-10-08T11:00:00"}),
        _run_resource("i-negative", {"dfe-e2e": "run-1", "expires-at": "-5"}),
        _run_resource("i-fraction", {"dfe-e2e": "run-1", "expires-at": "1.5"}),
    ]
    assert _expired_ids(resources) == []


def test_the_grace_boundary_selects_only_strictly_after_expiry_plus_grace() -> None:
    grace = 3600
    at_boundary = _run_resource("i-at", {"dfe-e2e": "run-1", "expires-at": str(NOW - grace)})
    past_boundary = _run_resource("i-past", {"dfe-e2e": "run-1", "expires-at": str(NOW - grace - 1)})
    inside_grace = _run_resource("i-inside", {"dfe-e2e": "run-1", "expires-at": str(NOW - grace + 1)})
    assert _expired_ids([at_boundary, past_boundary, inside_grace], grace=grace) == ["i-past"]


def test_expired_never_selects_the_excluded_bucket() -> None:
    bucket = _run_resource("example-tfstate", {"dfe-e2e": "run-1", "expires-at": "1"}, kind="s3-bucket")
    found = cloud_sweep.filter_expired([bucket], expiry=_expiry(), exclude_bucket="example-tfstate")
    assert found == []


def test_renamed_run_keys_from_the_environment_select_by_the_new_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DFE_RUN_TAG_KEY", "ci-run")
    monkeypatch.setenv("DFE_RUN_EXPIRY_KEY", "ci-expiry")
    renamed = _run_resource("i-renamed", {"ci-run": "run-1", "ci-expiry": "1"})
    default_keys = _run_resource("i-default", {"dfe-e2e": "run-1", "expires-at": "1"})
    expiry = cloud_sweep.ExpirySelection(keys=cloud_run.RunTagKeys.from_env(), now=NOW, grace=0)
    found = cloud_sweep.filter_expired([renamed, default_keys], expiry=expiry, exclude_bucket=None)
    assert [r.id for r in found] == ["i-renamed"]


def test_main_expired_lists_only_expired_and_exits_one_while_any_remain(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    seen_run_keys: list[str | None] = []
    resources = [
        _run_resource("i-past", {"dfe-e2e": "run-1", "expires-at": str(NOW - 10)}),
        _run_resource("i-future", {"dfe-e2e": "run-2", "expires-at": str(NOW + 10)}),
        _run_resource("i-bare", {}),
    ]

    def fake_collect(region: str, tf: dict, bucket: str | None, *, route1_filter: dict | None = None) -> list:
        seen_run_keys.append(next(iter(route1_filter)) if route1_filter else None)
        return resources

    monkeypatch.setattr(cloud_sweep, "collect", fake_collect)
    code = cloud_sweep.main(["--region", REGION, "--expired", "--grace", "0", "--now", str(NOW)])
    out = capsys.readouterr().out
    assert code == 1
    assert seen_run_keys == ["dfe-e2e"]
    eligible = out.split("eligible for --delete:", 1)[1]
    assert "i-past" in eligible
    assert "i-future" not in eligible
    assert "i-bare" not in eligible


def test_main_expired_with_delete_removes_only_the_expired_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    deleted: list[str] = []
    past = _run_resource("i-past", {"dfe-e2e": "run-1", "expires-at": "1"})
    live = _run_resource("i-live", {"dfe-e2e": "run-2", "expires-at": "9999999999"})
    responses = iter([[past, live], [live]])
    monkeypatch.setattr(cloud_sweep, "verify_account", lambda region, account: None)
    monkeypatch.setattr(cloud_sweep, "collect", lambda region, tf, bucket, **_: next(responses))
    monkeypatch.setattr(
        cloud_sweep, "delete_resources", lambda resources, region: deleted.extend(r.id for r in resources) or []
    )
    code = cloud_sweep.main(
        ["--region", REGION, "--expired", "--grace", "0", "--delete", "--account", "000000000000", "--yes"]
    )
    assert code == 0
    assert deleted == ["i-past"]
    assert "sweep clean: no expired run resources left" in capsys.readouterr().out


def test_main_expired_refuses_include_untagged(capsys: pytest.CaptureFixture) -> None:
    code = cloud_sweep.main(["--region", REGION, "--expired", "--include-untagged"])
    assert code == 2
    assert "--include-untagged" in capsys.readouterr().err


def test_main_refuses_now_with_delete(capsys: pytest.CaptureFixture) -> None:
    code = cloud_sweep.main(
        ["--region", REGION, "--expired", "--now", str(NOW), "--delete", "--account", "000000000000"]
    )
    assert code == 2
    assert "--now" in capsys.readouterr().err


def test_main_refuses_grace_without_expired(capsys: pytest.CaptureFixture) -> None:
    code = cloud_sweep.main(["--region", REGION, "--grace", "0"])
    assert code == 2
    assert "--expired" in capsys.readouterr().err


def test_main_refuses_a_malformed_grace(capsys: pytest.CaptureFixture) -> None:
    code = cloud_sweep.main(["--region", REGION, "--expired", "--grace", "an hour"])
    assert code == 2
    assert "duration" in capsys.readouterr().err


def test_the_default_grace_for_a_hand_run_is_one_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    within_hour = _run_resource("i-recent", {"dfe-e2e": "run-1", "expires-at": str(NOW - 1800)})
    monkeypatch.setattr(cloud_sweep, "collect", lambda region, tf, bucket, **_: [within_hour])
    assert cloud_sweep.main(["--region", REGION, "--expired", "--now", str(NOW)]) == 0
    assert cloud_sweep.main(["--region", REGION, "--expired", "--grace", "0", "--now", str(NOW)]) == 1


# ---------------------------------------------------------------------------
# Providers: GCP and Azure have the interface and refuse rather than report clean
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["gcp", "azure"])
def test_an_unbuilt_provider_refuses_by_name_and_touches_no_aws(
    provider: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        raise AssertionError("an unbuilt provider must not reach the aws CLI")

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", boom)
    code = cloud_sweep.main(["--provider", provider, "--region", "example-region", "--expired"])
    err = capsys.readouterr().err
    assert code == 2
    assert f"no {provider} listers" in err


@pytest.mark.parametrize("provider_cls", [cloud_sweep.GcpProvider, cloud_sweep.AzureProvider])
def test_every_unbuilt_provider_method_raises_not_implemented(provider_cls: type) -> None:
    provider = provider_cls("example-region")
    with pytest.raises(NotImplementedError, match=provider.name):
        provider.verify_account("0")
    with pytest.raises(NotImplementedError):
        provider.collect({}, None, run_key="dfe-e2e")
    with pytest.raises(NotImplementedError):
        provider.delete([])


def test_the_aws_provider_widens_the_catch_all_to_the_run_key_only_in_expired_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict | None] = []
    monkeypatch.setattr(
        cloud_sweep, "collect", lambda region, tf, bucket, *, route1_filter=None: seen.append(route1_filter) or []
    )
    provider = cloud_sweep.AwsProvider(REGION)
    provider.collect(TAG_FILTER, None, run_key=None)
    provider.collect(TAG_FILTER, None, run_key="dfe-e2e")
    assert seen == [None, {"dfe-e2e": None}]
