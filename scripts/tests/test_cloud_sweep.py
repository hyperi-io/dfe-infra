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
                    {"GroupId": "sg-default", "GroupName": "default", "VpcId": "vpc-run", "Tags": []},
                    {"GroupId": "sg-dfe", "GroupName": "dfe-nodes", "VpcId": "vpc-run", "Tags": DFE_TAGS},
                ]
            }
        ),
    )
    found = cloud_sweep.list_security_groups(REGION, TAG_FILTER)
    assert [r.id for r in found] == ["sg-dfe"]
    assert found[0].tagged is True
    assert found[0].extra["vpc_id"] == "vpc-run"


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


def _tagging_only(
    monkeypatch: pytest.MonkeyPatch,
    hits: list[cloud_sweep.Resource],
    described: dict[str, subprocess.CompletedProcess],
) -> None:
    """A region where only the tagging API finds anything; a describe call is answered by the id it names."""
    monkeypatch.setattr(cloud_sweep, "PER_SERVICE_COLLECTORS", [])
    monkeypatch.setattr(cloud_sweep, "list_s3_buckets", lambda region, tf, bucket, **_: [])
    monkeypatch.setattr(cloud_sweep, "list_tagged", lambda region, sf, tf: hits)

    def answer(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        for rid, reply in described.items():
            if rid in args:
                return reply
        raise AssertionError(f"unexpected aws call: {args}")

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", answer)


def test_collect_drops_a_tagging_api_hit_aws_reports_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tagging API lists a deleted endpoint for a while, and no scripted delete can remove it."""
    _tagging_only(
        monkeypatch,
        [_tagging_hit(ENDPOINT_ARN), _tagging_hit(INSTANCE_ARN)],
        {"vpce-1": _fail(ENDPOINT_NOT_FOUND), "i-1": _instance("running")},
    )
    found = cloud_sweep.collect(REGION, TAG_FILTER, None)
    assert [r.id for r in found] == [INSTANCE_ARN]


def test_collect_keeps_a_tagging_api_hit_whose_state_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A describe that fails for any reason but NotFound proves nothing, so the hit stays."""
    _tagging_only(
        monkeypatch,
        [_tagging_hit(ENDPOINT_ARN)],
        {"vpce-1": _fail("An error occurred (UnauthorizedOperation) when calling the DescribeVpcEndpoints operation")},
    )
    found = cloud_sweep.collect(REGION, TAG_FILTER, None)
    assert [r.id for r in found] == [ENDPOINT_ARN]
    assert "counting it live" in capsys.readouterr().err


def test_an_expired_sweep_is_clean_when_every_expired_hit_is_already_deleted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The reaper's sweep: a deleted endpoint, a key scheduled for deletion and an association gone with its cluster."""
    expired = {"dfe-e2e": "run-1", "expires-at": "2026-10-08T11:00:00Z"}
    hits = [
        cloud_sweep.Resource(kind=kind, id=arn, name=arn.rsplit("/", 1)[-1], created=None, tagged=True, tags=expired)
        for kind, arn in (
            ("tagged:ec2", ENDPOINT_ARN),
            ("tagged:kms", KMS_ARN),
            ("tagged:eks", POD_IDENTITY_ARN),
        )
    ]
    _tagging_only(
        monkeypatch,
        hits,
        {
            "vpce-1": _fail(ENDPOINT_NOT_FOUND),
            KMS_ARN.rsplit("/", 1)[-1]: _kms("PendingDeletion"),
            "a-0example": _fail(CLUSTER_NOT_FOUND),
        },
    )
    monkeypatch.setattr(cloud_sweep, "verify_account", lambda region, account: None)
    deleted: list[str] = []
    monkeypatch.setattr(
        cloud_sweep, "delete_resources", lambda resources, region: deleted.extend(r.id for r in resources) or []
    )
    code = cloud_sweep.main(
        ["--region", REGION, "--expired", "--grace", "0", "--delete", "--account", ACCOUNT_ID, "--yes"]
    )
    assert code == 0
    assert deleted == []
    assert "sweep clean: no expired run resources left" in capsys.readouterr().out


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
ENI_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:network-interface/eni-1"
ENI_NOT_FOUND = (
    "An error occurred (InvalidNetworkInterfaceID.NotFound) when calling the DescribeNetworkInterfaces operation"
)
VOLUME_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:volume/vol-1"
VOLUME_NOT_FOUND = "An error occurred (InvalidVolume.NotFound) when calling the DescribeVolumes operation"
KMS_ARN = f"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/7a67c3b8-0000-4000-8000-000000000001"
KMS_NOT_FOUND = "An error occurred (NotFoundException) when calling the DescribeKey operation"


def _kms(state: str) -> subprocess.CompletedProcess:
    return _ok({"KeyMetadata": {"KeyState": state}})


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
        pytest.param(ENI_ARN, _fail(ENI_NOT_FOUND), True, id="network-interface-not-found"),
        pytest.param(ENI_ARN, _ok({"NetworkInterfaces": []}), True, id="network-interface-no-longer-returned"),
        pytest.param(
            ENI_ARN, _ok({"NetworkInterfaces": [{"Status": "available"}]}), False, id="network-interface-available"
        ),
        pytest.param(ENI_ARN, _ok({"NetworkInterfaces": [{"Status": "in-use"}]}), False, id="network-interface-in-use"),
        pytest.param(VOLUME_ARN, _fail(VOLUME_NOT_FOUND), True, id="volume-not-found"),
        pytest.param(VOLUME_ARN, _ok({"Volumes": [{"State": "deleted"}]}), True, id="volume-deleted"),
        pytest.param(VOLUME_ARN, _ok({"Volumes": [{"State": "deleting"}]}), False, id="volume-deleting"),
        pytest.param(VOLUME_ARN, _ok({"Volumes": [{"State": "available"}]}), False, id="volume-available"),
        pytest.param(VOLUME_ARN, _ok({"Volumes": [{"State": "in-use"}]}), False, id="volume-in-use"),
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


# Each network class: its tagging-API ARN, the id it carries, the describe call's list key and NotFound code.
NETWORK_CLASSES = [
    ("subnet", "subnet-1", "Subnets", "InvalidSubnetID.NotFound"),
    ("security-group", "sg-1", "SecurityGroups", "InvalidGroup.NotFound"),
    ("route-table", "rtb-1", "RouteTables", "InvalidRouteTableID.NotFound"),
    ("internet-gateway", "igw-1", "InternetGateways", "InvalidInternetGatewayID.NotFound"),
    ("vpc", "vpc-1", "Vpcs", "InvalidVpcID.NotFound"),
    ("launch-template", "lt-1", "LaunchTemplates", "InvalidLaunchTemplateId.NotFound"),
    ("elastic-ip", "eipalloc-1", "Addresses", "InvalidAllocationID.NotFound"),
]


@pytest.mark.parametrize(
    ("resource_type", "object_id", "key", "not_found", "answer", "gone"),
    [
        pytest.param(t, i, k, n, outcome, gone, id=f"{t}-{label}")
        for t, i, k, n in NETWORK_CLASSES
        for label, outcome, gone in (
            ("not-found", "not-found", True),
            ("no-longer-returned", "empty", True),
            ("still-listed", "listed", False),
        )
    ],
)
def test_a_network_hit_is_gone_once_aws_no_longer_knows_it(
    monkeypatch: pytest.MonkeyPatch, resource_type: str, object_id: str, key: str, not_found: str, answer: str,
    gone: bool,
) -> None:
    """A teardown deletes the run's network, and the tagging API goes on listing each piece of it."""
    replies = {
        "not-found": _fail(f"An error occurred ({not_found}) when calling the Describe operation"),
        "empty": _ok({key: []}),
        "listed": _ok({key: [{"Id": object_id}]}),
    }
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return replies[answer]

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:{resource_type}/{object_id}"
    assert cloud_sweep.tagging_hit_gone(_tagging_hit(arn), REGION) is gone
    (call,) = calls
    assert object_id in call, "the describe call names the bare id"
    assert arn not in call, "the ARN is not an id the describe calls accept"


@pytest.mark.parametrize(("resource_type", "object_id"), [(t, i) for t, i, _k, _n in NETWORK_CLASSES])
def test_a_network_hit_whose_state_cannot_be_read_raises(
    monkeypatch: pytest.MonkeyPatch, resource_type: str, object_id: str
) -> None:
    _mock_run(monkeypatch, _fail("An error occurred (UnauthorizedOperation) when calling the Describe operation"))
    arn = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:{resource_type}/{object_id}"
    with pytest.raises(cloud_sweep.CloudSweepError, match="UnauthorizedOperation"):
        cloud_sweep.tagging_hit_gone(_tagging_hit(arn), REGION)


@pytest.mark.parametrize(
    ("answer", "gone"),
    [
        pytest.param(_kms("PendingDeletion"), True, id="kms-pending-deletion"),
        pytest.param(_kms("PendingReplicaDeletion"), True, id="kms-pending-replica-deletion"),
        pytest.param(_fail(KMS_NOT_FOUND), True, id="kms-not-found"),
        pytest.param(_kms("Enabled"), False, id="kms-enabled"),
        pytest.param(_kms("Disabled"), False, id="kms-disabled"),
    ],
)
def test_a_kms_key_scheduled_for_deletion_is_gone(
    monkeypatch: pytest.MonkeyPatch, answer: subprocess.CompletedProcess, gone: bool
) -> None:
    """A destroy can only schedule a key's deletion, so that is what finished looks like."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return answer

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    assert cloud_sweep.tagging_hit_gone(_tagging_hit(KMS_ARN, "tagged:kms"), REGION) is gone
    (call,) = calls
    assert call[call.index("--key-id") + 1] == KMS_ARN.rsplit("/", 1)[-1]


POD_IDENTITY_ARN = f"arn:aws:eks:{REGION}:{ACCOUNT_ID}:podidentityassociation/dfe-example/a-0example"
CLUSTER_NOT_FOUND = (
    "An error occurred (ResourceNotFoundException) when calling the DescribePodIdentityAssociation "
    "operation: No cluster found for name: dfe-example."
)


@pytest.mark.parametrize(
    ("answer", "gone"),
    [
        pytest.param(_fail(CLUSTER_NOT_FOUND), True, id="cluster-gone"),
        pytest.param(_ok({"association": {"associationId": "a-0example"}}), False, id="association-live"),
    ],
)
def test_a_pod_identity_association_is_gone_with_its_cluster(
    monkeypatch: pytest.MonkeyPatch, answer: subprocess.CompletedProcess, gone: bool
) -> None:
    """Deleting a cluster takes its associations, and the tagging API goes on listing them."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return answer

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    assert cloud_sweep.tagging_hit_gone(_tagging_hit(POD_IDENTITY_ARN, "tagged:eks"), REGION) is gone
    (call,) = calls
    assert call[call.index("--cluster-name") + 1] == "dfe-example"
    assert call[call.index("--association-id") + 1] == "a-0example"


# A secret's name may hold '/', and its ARN separates the type with ':'.
SECRET_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:dfe/test/pki/internal-ca-crt-AbCdEf"
OTHER_SECRET_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:dfe/test/pki/internal-ca-key-GhIjKl"


def _secret(arn: str = SECRET_ARN, *, scheduled: bool = False) -> dict:
    entry: dict[str, object] = {"ARN": arn, "Name": arn.split(":secret:", 1)[1][:-7], "Tags": DFE_TAGS}
    if scheduled:
        entry["DeletedDate"] = "2026-10-08T12:00:00+00:00"
    return entry


def _secrets(*entries: dict) -> subprocess.CompletedProcess:
    return _ok({"SecretList": list(entries)})


@pytest.mark.parametrize(
    ("listing", "gone"),
    [
        pytest.param(_secrets(), True, id="no-longer-listed"),
        pytest.param(_secrets(_secret(OTHER_SECRET_ARN)), True, id="only-another-secret-listed"),
        pytest.param(_secrets(_secret(scheduled=True)), False, id="scheduled-for-deletion-keeps-its-name"),
        pytest.param(_secrets(_secret()), False, id="live"),
    ],
)
def test_a_secret_tagging_hit_is_gone_once_secrets_manager_no_longer_lists_it(
    monkeypatch: pytest.MonkeyPatch, listing: subprocess.CompletedProcess, gone: bool
) -> None:
    """The run's teardown purges the secrets ESO pushed, and the tagging API goes on listing them."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return listing

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    assert cloud_sweep.tagging_hit_gone(_tagging_hit(SECRET_ARN, "tagged:secretsmanager"), REGION) is gone
    (call,) = calls
    assert call[:3] == ["secretsmanager", "list-secrets", "--include-planned-deletion"]


def test_a_secret_tagging_hit_whose_listing_fails_raises_rather_than_reading_as_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_run(monkeypatch, _fail("An error occurred (AccessDeniedException) when calling the ListSecrets operation"))
    with pytest.raises(cloud_sweep.CloudSweepError, match="AccessDeniedException"):
        cloud_sweep.tagging_hit_gone(_tagging_hit(SECRET_ARN, "tagged:secretsmanager"), REGION)


def test_another_secretsmanager_class_is_never_judged_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        raise AssertionError("nothing is listed for a class with no gone-check")

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", boom)
    hit = _tagging_hit(f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:other:thing", "tagged:secretsmanager")
    assert cloud_sweep.tagging_hit_gone(hit, REGION) is False


def test_a_forced_secret_delete_waits_until_secrets_manager_no_longer_lists_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A background process purges a forced delete, and until it does the name is still taken."""
    calls: list[list[str]] = []
    responses = [_ok({"ARN": SECRET_ARN}), _secrets(_secret(scheduled=True)), _secrets()]

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return responses.pop(0)

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    monkeypatch.setattr(cloud_sweep.time, "sleep", lambda _seconds: None)
    cloud_sweep._delete_secret(_resource("secret", SECRET_ARN, tagged=True), REGION)
    assert [c[1] for c in calls] == ["delete-secret", "list-secrets", "list-secrets"]
    assert calls[0][calls[0].index("--secret-id") + 1] == SECRET_ARN
    assert "--force-delete-without-recovery" in calls[0]


def test_expected_fail_a_secret_still_listed_when_the_wait_runs_out_fails_its_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The call answers success without saying it purged anything, so only the listing can say."""
    clock = [0.0]

    def tick() -> float:
        clock[0] += 100.0
        return clock[0]

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        return _ok({"ARN": SECRET_ARN}) if args[1] == "delete-secret" else _secrets(_secret(scheduled=True))

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    monkeypatch.setattr(cloud_sweep.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(cloud_sweep.time, "monotonic", tick)
    with pytest.raises(cloud_sweep.CloudSweepError, match="still listed 300s after its forced delete"):
        cloud_sweep._delete_secret(_resource("secret", SECRET_ARN, tagged=True), REGION)


def test_a_hung_aws_call_is_a_sweep_error_not_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every caller handles CloudSweepError; a TimeoutExpired would end a teardown with a traceback."""

    def hang(args: list[str], *, timeout: float) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(["aws", *args], timeout)

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", hang)
    with pytest.raises(cloud_sweep.CloudSweepError, match="did not answer within 60s"):
        cloud_sweep.run_aws(["secretsmanager", "list-secrets"], REGION)
    with pytest.raises(cloud_sweep.CloudSweepError, match="did not answer within 60s"):
        cloud_sweep.run_aws_text(["s3", "rm", "s3://example", "--recursive"], REGION)


FLEET_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:fleet/fleet-0example"
FLEET_NOT_FOUND = "An error occurred (InvalidFleetId.NotFound) when calling the DescribeFleets operation"


def _fleet(fleet_type: str, state: str = "active", instance_ids: tuple[str, ...] = ("i-1",)) -> subprocess.CompletedProcess:
    return _ok({"Fleets": [{"Type": fleet_type, "FleetState": state, "Instances": [{"InstanceIds": list(instance_ids)}]}]})


TERMINATED = _ok({"Reservations": [{"Instances": [{"State": {"Name": "terminated"}}]}]})
RUNNING = _ok({"Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]})


@pytest.mark.parametrize(
    ("replies", "gone"),
    [
        pytest.param([_fleet("instant"), TERMINATED], True, id="instant-every-instance-terminated"),
        pytest.param([_fleet("instant", instance_ids=())], True, id="instant-with-no-instances"),
        pytest.param([_fleet("instant", instance_ids=("i-1", "i-2")), TERMINATED, RUNNING], False, id="instant-one-instance-running"),
        pytest.param([_fleet("maintain")], False, id="maintain-fleet-is-never-an-inert-record"),
        pytest.param([_fleet("request", state="deleted")], True, id="deleted-fleet"),
        pytest.param([_fail(FLEET_NOT_FOUND)], True, id="fleet-not-found"),
        pytest.param([_ok({"Fleets": []})], True, id="fleet-no-longer-returned"),
    ],
)
def test_an_instant_fleet_record_is_gone_once_its_instances_are(
    monkeypatch: pytest.MonkeyPatch, replies: list[subprocess.CompletedProcess], gone: bool
) -> None:
    """Karpenter launches through instant fleets, and AWS keeps each record active after its instances go."""
    calls: list[list[str]] = []
    queue = list(replies)

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return queue.pop(0)

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    assert cloud_sweep.tagging_hit_gone(_tagging_hit(FLEET_ARN), REGION) is gone
    assert calls[0][calls[0].index("--fleet-ids") + 1] == "fleet-0example"


def test_a_fleet_whose_state_cannot_be_read_raises_rather_than_reading_as_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _fail("An error occurred (UnauthorizedOperation) when calling the DescribeFleets operation"))
    with pytest.raises(cloud_sweep.CloudSweepError, match="UnauthorizedOperation"):
        cloud_sweep.tagging_hit_gone(_tagging_hit(FLEET_ARN), REGION)


SPOT_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:spot-instances-request/sir-0example"
SPOT_NOT_FOUND = "An error occurred (InvalidSpotInstanceRequestID.NotFound) when calling the DescribeSpotInstanceRequests operation"


def _spot(state: str, instance_id: str | None = None) -> subprocess.CompletedProcess:
    request: dict[str, str] = {"State": state}
    if instance_id:
        request["InstanceId"] = instance_id
    return _ok({"SpotInstanceRequests": [request]})


@pytest.mark.parametrize(
    ("replies", "gone"),
    [
        pytest.param([_fail(SPOT_NOT_FOUND)], True, id="purged-request"),
        pytest.param([_ok({"SpotInstanceRequests": []})], True, id="no-longer-returned"),
        pytest.param([_spot("cancelled")], True, id="cancelled-without-instance"),
        pytest.param([_spot("closed", "i-1"), TERMINATED], True, id="closed-instance-terminated"),
        pytest.param([_spot("closed", "i-1"), RUNNING], False, id="closed-instance-running"),
        pytest.param([_spot("active", "i-1")], False, id="active-request"),
        pytest.param([_spot("open")], False, id="open-request"),
    ],
)
def test_a_spot_request_is_gone_once_aws_purges_or_closes_it(
    monkeypatch: pytest.MonkeyPatch, replies: list[subprocess.CompletedProcess], gone: bool
) -> None:
    """The tagging API goes on listing a spot request EC2 has already purged."""
    calls: list[list[str]] = []
    queue = list(replies)

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return queue.pop(0)

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    assert cloud_sweep.tagging_hit_gone(_tagging_hit(SPOT_ARN), REGION) is gone
    assert calls[0][calls[0].index("--spot-instance-request-ids") + 1] == "sir-0example"


def test_a_spot_request_whose_state_cannot_be_read_raises_rather_than_reading_as_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_run(monkeypatch, _fail("An error occurred (UnauthorizedOperation) when calling the DescribeSpotInstanceRequests operation"))
    with pytest.raises(cloud_sweep.CloudSweepError, match="UnauthorizedOperation"):
        cloud_sweep.tagging_hit_gone(_tagging_hit(SPOT_ARN), REGION)


LT_ID = "lt-0123456789abcdef0"
LT_NAME = "karpenter.k8s.aws/1234567890123456789"
LT_ARN = f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:launch-template/{LT_ID}"
LT_NOT_FOUND = "An error occurred (InvalidLaunchTemplateId.NotFound) when calling the DescribeLaunchTemplates operation"


def test_launch_templates_are_listed_by_id_with_their_name_age_and_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch,
        _ok(
            {
                "LaunchTemplates": [
                    {
                        "LaunchTemplateId": LT_ID,
                        "LaunchTemplateName": LT_NAME,
                        "CreateTime": "2026-10-13T01:14:30+00:00",
                        "Tags": DFE_TAGS,
                    }
                ]
            }
        ),
    )
    (found,) = cloud_sweep.list_launch_templates(REGION, TAG_FILTER)
    assert (found.kind, found.id, found.name, found.created, found.tagged) == (
        "launch-template", LT_ID, LT_NAME, "2026-10-13T01:14:30+00:00", True,
    )


def test_a_launch_template_delete_names_it_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return _ok({})

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    cloud_sweep.DELETE_FNS["launch-template"](_resource("launch-template", LT_ID, tagged=True), REGION)
    (call,) = calls
    assert call[:2] == ["ec2", "delete-launch-template"]
    assert call[call.index("--launch-template-id") + 1] == LT_ID


def test_a_runs_tag_sweep_deletes_the_launch_templates_karpenter_left(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Karpenter puts the run's tags on its launch templates, and the tagging API lists one after it goes."""
    run_tags = [{"Key": "dfe-e2e", "Value": "run-1"}, {"Key": "karpenter.k8s.aws/ec2nodeclass", "Value": "general"}]
    held = {LT_ID: {"LaunchTemplateId": LT_ID, "LaunchTemplateName": LT_NAME, "Tags": run_tags}}

    def answer(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        if args[:2] == ["sts", "get-caller-identity"]:
            return _ok({"Account": ACCOUNT_ID})
        if args[:2] == ["resourcegroupstaggingapi", "get-resources"]:
            return _ok({"ResourceTagMappingList": [{"ResourceARN": LT_ARN, "Tags": run_tags}]})
        if args[:2] == ["ec2", "describe-launch-templates"]:
            if "--launch-template-ids" not in args:
                return _ok({"LaunchTemplates": list(held.values())})
            named = args[args.index("--launch-template-ids") + 1]
            return _ok({"LaunchTemplates": [held[named]]}) if named in held else _fail(LT_NOT_FOUND)
        if args[:2] == ["ec2", "delete-launch-template"]:
            held.pop(args[args.index("--launch-template-id") + 1])
            return _ok({})
        return _ok({})

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", answer)
    code = cloud_sweep.main(
        ["--region", REGION, "--tag-filter", "dfe-e2e=run-1", "--delete", "--account", ACCOUNT_ID, "--yes"]
    )
    assert code == 0
    assert held == {}
    out = capsys.readouterr().out
    assert f"deleted launch-template {LT_NAME}" in out
    assert "sweep clean: nothing left to delete" in out


def test_another_eks_class_is_never_judged_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        raise AssertionError("nothing is described for a class with no gone-check")

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", boom)
    hit = _tagging_hit(f"arn:aws:eks:{REGION}:{ACCOUNT_ID}:cluster/dfe-example", "tagged:eks")
    assert cloud_sweep.tagging_hit_gone(hit, REGION) is False


@pytest.mark.parametrize("arn", [NAT_ARN, ENDPOINT_ARN, INSTANCE_ARN, ENI_ARN, VOLUME_ARN])
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
        pytest.param(_tagging_hit(f"arn:aws:ec2:{REGION}:{ACCOUNT_ID}:snapshot/snap-1"), id="another-ec2-class"),
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


@pytest.mark.parametrize("tag_filter", ["", ",", " , "])
def test_expected_fail_delete_with_an_empty_tag_filter_is_refused_before_any_aws_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tag_filter: str
) -> None:
    """An empty filter matches every resource, which is --include-untagged without saying so."""

    def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        raise AssertionError("no aws call should happen before the filter is checked")

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", boom)
    code = cloud_sweep.main(["--region", REGION, "--tag-filter", tag_filter, "--delete", "--account", "000000000000"])
    assert code == 2
    assert "--include-untagged" in capsys.readouterr().err


def test_an_empty_tag_filter_still_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud_sweep, "collect", lambda region, tf, bucket, **_: [])
    assert cloud_sweep.main(["--region", REGION, "--tag-filter", ""]) == 0


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


EXPIRED_RUN = {"dfe-e2e": "run-1", "expires-at": "2026-10-08T11:00:00Z"}
LIVE_RUN = {"dfe-e2e": "run-2", "expires-at": "2026-10-08T13:00:00Z"}
EKS_CLUSTER_SG_TAGS = {"Name": "eks-cluster-sg-example", "aws:eks:cluster-name": "example"}


def _vpc(rid: str, tags: dict[str, str]) -> cloud_sweep.Resource:
    return cloud_sweep.Resource(kind="vpc", id=rid, name=rid, created=None, tagged=False, tags=tags)


def _sg(rid: str, vpc_id: str, *, name: str | None = None, tags: dict[str, str] | None = None) -> cloud_sweep.Resource:
    return cloud_sweep.Resource(
        kind="security-group",
        id=rid,
        name=name or rid,
        created=None,
        tagged=False,
        extra={"vpc_id": vpc_id},
        tags=EKS_CLUSTER_SG_TAGS if tags is None else tags,
    )


def test_expired_selects_an_untagged_security_group_inside_an_expired_vpc() -> None:
    """EKS leaves its cluster security group with no run tag, and DeleteVpc fails while it stands."""
    resources = [_vpc("vpc-expired", EXPIRED_RUN), _sg("sg-eks-cluster", "vpc-expired")]
    assert sorted(_expired_ids(resources)) == ["sg-eks-cluster", "vpc-expired"]


def test_expected_fail_a_security_group_in_a_live_or_unowned_vpc_is_never_selected() -> None:
    resources = [
        _vpc("vpc-live", LIVE_RUN),
        _vpc("vpc-unowned", {"Name": "someone-elses"}),
        _vpc("vpc-no-expiry", {"dfe-e2e": "run-3"}),
        _sg("sg-in-live", "vpc-live"),
        _sg("sg-in-unowned", "vpc-unowned"),
        _sg("sg-in-no-expiry", "vpc-no-expiry"),
        _sg("sg-no-vpc", ""),
    ]
    assert _expired_ids(resources) == []


def test_expected_fail_the_default_group_of_an_expired_vpc_is_never_selected() -> None:
    """AWS refuses DeleteSecurityGroup on a VPC's default group and removes it with the VPC."""
    resources = [_vpc("vpc-expired", EXPIRED_RUN), _sg("sg-default", "vpc-expired", name="default", tags={})]
    assert _expired_ids(resources) == ["vpc-expired"]


def test_expected_fail_a_run_tagged_group_in_an_expired_vpc_is_judged_by_its_own_tags() -> None:
    resources = [
        _vpc("vpc-expired", EXPIRED_RUN),
        _sg("sg-live", "vpc-expired", tags=LIVE_RUN),
        _sg("sg-malformed", "vpc-expired", tags={"dfe-e2e": "run-1", "expires-at": "yesterday"}),
    ]
    assert _expired_ids(resources) == ["vpc-expired"]


def test_expected_fail_only_security_groups_inherit_the_vpcs_expiry() -> None:
    eni = cloud_sweep.Resource(
        kind="eni", id="eni-1", name="eni-1", created=None, tagged=False, extra={"vpc_id": "vpc-expired"}
    )
    assert _expired_ids([_vpc("vpc-expired", EXPIRED_RUN), eni]) == ["vpc-expired"]


def test_the_report_names_the_expired_vpc_an_untagged_group_is_selected_through(
    capsys: pytest.CaptureFixture,
) -> None:
    resources = [_vpc("vpc-expired", EXPIRED_RUN), _sg("sg-eks-cluster", "vpc-expired")]
    cloud_sweep.print_report(resources, _expiry())
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("security-group"))
    assert "in-expired-vpc=vpc-expired" in line


def test_main_expired_deletes_the_orphan_group_before_its_vpc(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return _ok({})

    resources = [_vpc("vpc-expired", EXPIRED_RUN), _sg("sg-eks-cluster", "vpc-expired"), _sg("sg-other", "vpc-live")]
    responses = iter([resources, [resources[2]]])
    monkeypatch.setattr(cloud_sweep, "verify_account", lambda region, account: None)
    monkeypatch.setattr(cloud_sweep, "collect", lambda region, tf, bucket, **_: next(responses))
    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", fake_run)
    code = cloud_sweep.main(
        ["--region", REGION, "--expired", "--grace", "0", "--delete", "--account", "000000000000", "--yes"]
    )
    deletes = [args[1] for args in calls]
    assert code == 0
    assert deletes == ["delete-security-group", "delete-vpc"]
    assert "sg-other" not in " ".join(" ".join(args) for args in calls)
    assert "sweep clean: no expired run resources left" in capsys.readouterr().out


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

    def fake_collect(
        region: str, tf: dict, bucket: str | None, *, route1_filter: dict | None = None, kinds: object = None
    ) -> list:
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
        cloud_sweep,
        "collect",
        lambda region, tf, bucket, *, route1_filter=None, kinds=None: seen.append(route1_filter) or [],
    )
    provider = cloud_sweep.AwsProvider(REGION)
    provider.collect(TAG_FILTER, None, run_key=None)
    provider.collect(TAG_FILTER, None, run_key="dfe-e2e")
    assert seen == [None, {"dfe-e2e": None}]


# ---------------------------------------------------------------------------
# --kind: a sweep limited to some kinds
# ---------------------------------------------------------------------------

RUN_ENI_TAGS = [
    {"Key": "dfe-e2e", "Value": "run-1"},
    {"Key": "eks:eni:owner", "Value": "amazon-vpc-cni"},
    {"Key": "node.k8s.amazonaws.com/instance_id", "Value": "i-0gone"},
]
IN_USE = (
    "An error occurred (InvalidNetworkInterface.InUse) when calling the DeleteNetworkInterface "
    "operation: Interface eni-attached is currently in use."
)


class InterfaceRegion:
    """The aws CLI for one region holding network interfaces and a security group, and nothing else.

    A delete of an interface named in `in_use` is refused the way EC2 refuses an attached one.
    """

    def __init__(self) -> None:
        self.interfaces = {
            "eni-leaked": {"NetworkInterfaceId": "eni-leaked", "Status": "available", "TagSet": RUN_ENI_TAGS},
            "eni-attached": {"NetworkInterfaceId": "eni-attached", "Status": "in-use", "TagSet": RUN_ENI_TAGS},
            "eni-other-run": {
                "NetworkInterfaceId": "eni-other-run",
                "Status": "available",
                "TagSet": [{"Key": "dfe-e2e", "Value": "run-2"}],
            },
        }
        self.groups = {"sg-run": {"GroupId": "sg-run", "GroupName": "run", "Tags": [{"Key": "dfe-e2e", "Value": "run-1"}]}}
        self.in_use = {"eni-attached"}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append(args)
        if args[:2] == ["sts", "get-caller-identity"]:
            return _ok({"Account": ACCOUNT_ID})
        if args[:2] == ["ec2", "describe-network-interfaces"]:
            return _ok({"NetworkInterfaces": list(self.interfaces.values())})
        if args[:2] == ["ec2", "delete-network-interface"]:
            eni = args[args.index("--network-interface-id") + 1]
            if eni in self.in_use:
                return _fail(IN_USE)
            del self.interfaces[eni]
            return _ok({})
        if args[:2] == ["ec2", "describe-security-groups"]:
            return _ok({"SecurityGroups": list(self.groups.values())})
        if args[:2] == ["ec2", "delete-security-group"]:
            del self.groups[args[args.index("--group-id") + 1]]
            return _ok({})
        raise AssertionError(f"a sweep limited by --kind made a call outside its kinds: {args}")


def _kind_sweep(monkeypatch: pytest.MonkeyPatch, region: InterfaceRegion, *kinds: str) -> int:
    """The teardown's run-tag sweep for run-1, limited to `kinds`, against `region`."""
    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", region)
    argv = ["--region", REGION, "--tag-filter", "dfe-e2e=run-1", "--delete", "--account", ACCOUNT_ID, "--yes"]
    for kind in kinds:
        argv += ["--kind", kind]
    return cloud_sweep.main(argv)


def test_a_sweep_limited_to_enis_deletes_the_runs_detached_interface_and_lists_nothing_else(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The VPC CNI's interface outlives its node, carries the run tag, and holds its subnet."""
    region = InterfaceRegion()
    del region.interfaces["eni-attached"]
    assert _kind_sweep(monkeypatch, region, "eni") == 0
    assert set(region.interfaces) == {"eni-other-run"}
    assert set(region.groups) == {"sg-run"}
    called = {tuple(call[:2]) for call in region.calls}
    assert called == {
        ("sts", "get-caller-identity"),
        ("ec2", "describe-network-interfaces"),
        ("ec2", "delete-network-interface"),
    }
    out = capsys.readouterr().out
    assert "deleted eni eni-leaked" in out
    assert "sweep clean: nothing left to delete" in out


def test_expected_fail_an_attached_interface_is_reported_left_and_the_sweep_finishes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """EC2 refuses to delete an interface still attached, and that refusal is a finding, not a crash."""
    region = InterfaceRegion()
    assert _kind_sweep(monkeypatch, region, "eni") == 1
    assert set(region.interfaces) == {"eni-attached", "eni-other-run"}
    captured = capsys.readouterr()
    assert "deleted eni eni-leaked" in captured.out
    assert "FAILED to delete eni eni-attached" in captured.err
    assert "InvalidNetworkInterface.InUse" in captured.err
    assert "remaining after sweep:\n  eni eni-attached" in captured.out


def test_repeated_kinds_run_each_lister_and_delete_in_dependency_order(monkeypatch: pytest.MonkeyPatch) -> None:
    region = InterfaceRegion()
    # Named group first, so the order below is DELETE_ORDER's rather than the command line's.
    assert _kind_sweep(monkeypatch, region, "security-group", "eni") == 1  # the attached interface stays
    deletes = [call[1] for call in region.calls if call[1].startswith("delete-")]
    assert deletes == ["delete-network-interface", "delete-network-interface", "delete-security-group"]
    assert region.groups == {}


def test_without_kind_every_lister_and_the_tagging_api_still_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def record(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return _ok({})

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", record)
    assert cloud_sweep.main(["--region", REGION]) == 0
    called = {tuple(call[:2]) for call in calls}
    assert ("resourcegroupstaggingapi", "get-resources") in called
    assert ("ec2", "describe-network-interfaces") in called
    assert ("s3api", "list-buckets") in called


def test_a_kind_limited_collect_skips_the_tagging_api_whose_hits_have_no_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tagging-API hit is labelled by service, so --kind could never select it, only list it as undeletable."""
    calls: list[list[str]] = []

    def record(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return _ok({})

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", record)
    assert cloud_sweep.collect(REGION, TAG_FILTER, None, kinds=["s3-bucket"]) == []
    assert [call[:2] for call in calls] == [["s3api", "list-buckets"]]


def test_expected_fail_an_unknown_kind_is_refused_before_any_aws_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    def boom(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        raise AssertionError("no aws call should happen before --kind is checked")

    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", boom)
    with pytest.raises(SystemExit) as exited:
        cloud_sweep.main(["--region", REGION, "--kind", "network-interface"])
    assert exited.value.code == 2
    assert "invalid choice: 'network-interface'" in capsys.readouterr().err


def test_every_kind_a_sweep_deletes_has_exactly_one_lister() -> None:
    """--kind picks listers by the kind each reports, so a kind with none could never be swept."""
    assert sorted([*cloud_sweep.LISTERS, "s3-bucket"]) == sorted(cloud_sweep.DELETE_ORDER)
    assert cloud_sweep.PER_SERVICE_COLLECTORS == list(cloud_sweep.LISTERS.values())


@pytest.mark.parametrize("kind", sorted(set(cloud_sweep.DELETE_ORDER) - {"s3-bucket"}))
def test_each_lister_reports_the_kind_it_is_listed_under(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """A lister filed under the wrong kind would be skipped by a --kind sweep that asked for it."""
    body = {
        "Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": "running"}}]}],
        "Volumes": [{"VolumeId": "vol-1"}],
        "Snapshots": [{"SnapshotId": "snap-1"}],
        "LaunchTemplates": [{"LaunchTemplateId": "lt-1", "LaunchTemplateName": "lt"}],
        "NatGateways": [{"NatGatewayId": "nat-1", "State": "available"}],
        "Addresses": [{"AllocationId": "eipalloc-1", "PublicIp": "192.0.2.1"}],
        "LoadBalancers": [{"LoadBalancerArn": "arn:lb", "LoadBalancerName": "lb"}],
        "TargetGroups": [{"TargetGroupArn": "arn:tg", "TargetGroupName": "tg"}],
        "TagDescriptions": [],
        "SecurityGroups": [{"GroupId": "sg-1", "GroupName": "run"}],
        "NetworkInterfaces": [{"NetworkInterfaceId": "eni-1"}],
        "Vpcs": [{"VpcId": "vpc-1"}],
        "Subnets": [{"SubnetId": "subnet-1", "VpcId": "vpc-1"}],
        "RouteTables": [{"RouteTableId": "rtb-1", "VpcId": "vpc-1"}],
        "InternetGateways": [{"InternetGatewayId": "igw-1"}],
        "VpcEndpoints": [{"VpcEndpointId": "vpce-1", "VpcId": "vpc-1", "State": "available"}],
        "HostedZones": [{"Id": "/hostedzone/Z1", "Name": "example.internal."}],
        "logGroups": [{"logGroupName": "/dfe/run"}],
        "SecretList": [{"ARN": "arn:secret", "Name": "dfe/run"}],
        "Aliases": [{"AliasName": "alias/dfe-run"}],
        "clusters": ["dfe-run"],
        "cluster": {"tags": {}},
        "ClusterInfoList": [{"ClusterArn": "arn:msk", "ClusterName": "dfe-run", "State": "ACTIVE"}],
    }
    monkeypatch.setattr(cloud_sweep.aws_cli, "run_aws", lambda args, **_kwargs: _ok(body))
    found = cloud_sweep.collect(REGION, TAG_FILTER, None, kinds=[kind])
    assert {r.kind for r in found} == {kind}
