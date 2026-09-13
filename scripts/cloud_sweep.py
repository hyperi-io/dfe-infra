#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/cloud_sweep.py
#  Purpose:      List, and on --delete remove, every AWS resource a DFE cloud
#                proof can leave behind once its own teardown has run.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""cloud_sweep.py -- the scripted sweep for leftover AWS resources.

A cloud proof tears itself down as soon as it has its evidence, but a
controller-created resource (an ENI a load balancer left behind, a security
group a cluster provisioned for itself) is invisible to the deploy's own
teardown and to tofu state alike. This script is the separate check that
catches that class of leftover, and removes it on request.

Route 1 is the tagging API (`resourcegroupstaggingapi get-resources`), which
catches anything a proof tagged but this script does not otherwise know
about. Route 2 is a dedicated lister per resource class the tagging API
reaches unreliably or not at all -- because a controller created it without
copying the stack's tags, or because the service does not support tags at
all (target groups, security groups, ENIs, subnets, route tables): EKS and
MSK clusters, EC2 instances, EBS volumes and snapshots, NAT gateways and
EIPs, load balancers (v2) and target groups, non-default security groups,
ENIs, non-default VPCs and their subnets/route tables/internet gateways/
endpoints, Route 53 hosted zones, CloudWatch log groups, Secrets Manager
secrets (including ones already scheduled for deletion), KMS aliases, and S3
buckets. AWS account defaults (the default VPC and everything that comes
with it, AWS-managed KMS aliases) are never listed or touched -- a proof did
not create them and this sweep has no business reporting on them.

A resource counts as "dfe's" when it carries every tag in --tag-filter
(default service-name=dfe,environment=test). Everything found is printed
either way, with its age and whether it is tagged; only the tagged set (or
everything, with --include-untagged) is deleted or counted against the exit
code, and the named --exclude-bucket (the tofu state bucket) is never
touched. Exit 0 means the tagged set is empty; exit 1 means resources
remain, printed for cleanup.

--delete refuses to run without --account, checked against
`aws sts get-caller-identity` before anything is touched -- a 12-digit id
matched against the live session, never a name, because a name describes
intent and an id is what the delete calls actually land against. It then
prints the full eligible list and asks for an explicit "yes" before
deleting anything, unless --yes is given to skip that prompt (for CI).

--include-untagged is DANGEROUS with --delete. It drops the one filter that
keeps this script's own deletions to what the tag-filter names, so --delete
--include-untagged removes EVERY resource the 21 listers found in --region,
tagged or not, in whatever account --account named -- EC2 instances, EKS and
MSK clusters, NAT gateways, security groups, non-default VPCs and their
subnets, Route 53 zones, log groups, Secrets Manager secrets (with
--force-delete-without-recovery), KMS aliases and S3 buckets (with
--exclude-bucket the only exemption). Reach for it only when the sweep is
meant to clear a region entirely, never as a way to speed up an ordinary
cleanup.

Stdlib only: every AWS call shells out to the `aws` CLI with --output json.

    python3 scripts/cloud_sweep.py --region us-west-2
    python3 scripts/cloud_sweep.py --region us-west-2 --delete \\
        --account 000000000000 --exclude-bucket example-tfstate-bucket
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aws_cli

AWS_TIMEOUT = 60  # seconds allowed for one aws CLI call before it is a hang, not a slow API
POLL_TIMEOUT = 300  # seconds to wait for an async delete (NAT gateway, EKS, MSK) to finish
POLL_INTERVAL = 10

DEFAULT_TAG_FILTER = "service-name=dfe,environment=test"


class CloudSweepError(RuntimeError):
    """One `aws` call failed outright -- not just "nothing found"."""


@dataclass(frozen=True)
class Resource:
    """One thing the sweep found, before or after a --delete pass."""

    kind: str
    id: str
    name: str
    created: str | None  # ISO-8601 or an AWS epoch-millis timestamp; None when the API omits it
    tagged: bool
    extra: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# aws CLI plumbing
# ---------------------------------------------------------------------------


def run_aws(args: list[str], region: str) -> dict:
    """Run one `aws ... --output json` call and return its parsed body."""
    result = aws_cli.run_aws([*args, "--region", region, "--output", "json"], timeout=AWS_TIMEOUT)
    if result.returncode != 0:
        raise CloudSweepError(f"aws {' '.join(args)} failed: {result.stderr.strip()}")
    text = result.stdout.strip()
    return json.loads(text) if text else {}


def run_aws_text(args: list[str], region: str) -> None:
    """Run one `aws` call whose output is not JSON (e.g. `s3 rm`), for its side effect."""
    result = aws_cli.run_aws([*args, "--region", region], timeout=AWS_TIMEOUT)
    if result.returncode != 0:
        raise CloudSweepError(f"aws {' '.join(args)} failed: {result.stderr.strip()}")


def _tags_from_list(tags: list[dict] | None, key_field: str = "Key", value_field: str = "Value") -> dict:
    return {t[key_field]: t[value_field] for t in (tags or [])}


def _matches(tags: dict, tag_filter: dict[str, str]) -> bool:
    return all(tags.get(key) == value for key, value in tag_filter.items())


def _age(created: str | None) -> str:
    """A short human age, or "unknown" when the API exposes no creation time."""
    if not created:
        return "unknown"
    try:
        stamp = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    delta = datetime.now(UTC) - stamp
    seconds = delta.total_seconds()
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{delta.days}d"


# ---------------------------------------------------------------------------
# Route 1: the tagging API, as a catch-all for anything the listers below miss
# ---------------------------------------------------------------------------


def _kind_from_arn(arn: str) -> str:
    parts = arn.split(":", 5)
    service = parts[2] if len(parts) > 2 else "unknown"
    return f"tagged:{service}"


def _name_from_arn(arn: str) -> str:
    tail = arn.rsplit("/", 1)[-1]
    return tail.rsplit(":", 1)[-1]


def list_tagged(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    """Everything the tagging API returns for `tag_filter`, kind-labelled by ARN service."""
    filters: list[str] = []
    for key, value in tag_filter.items():
        filters += ["--tag-filters", f"Key={key},Values={value}"]
    resources: list[Resource] = []
    token: str | None = None
    while True:
        args = ["resourcegroupstaggingapi", "get-resources", *filters]
        if token:
            args += ["--starting-token", token]
        data = run_aws(args, region)
        for mapping in data.get("ResourceTagMappingList", []):
            arn = mapping["ResourceARN"]
            resources.append(
                Resource(kind=_kind_from_arn(arn), id=arn, name=_name_from_arn(arn), created=None, tagged=True)
            )
        token = data.get("PaginationToken") or None
        if not token:
            break
    return resources


# ---------------------------------------------------------------------------
# Route 2: dedicated listers, one per class the tagging API reaches unreliably
# ---------------------------------------------------------------------------


def _default_vpc_ids(region: str) -> set[str]:
    data = run_aws(["ec2", "describe-vpcs"], region)
    return {v["VpcId"] for v in data.get("Vpcs", []) if v.get("IsDefault")}


def _list_ec2(
    list_cmd: list[str],
    list_key: str,
    id_field: str,
    region: str,
    tag_filter: dict[str, str],
    *,
    kind: str,
    name_field: str | None = None,
    created_field: str | None = None,
    tag_field: str = "Tags",
    skip: Callable[[dict], bool] | None = None,
) -> list[Resource]:
    """Share the describe-list-parse shape every plain EC2 class below follows."""
    data = run_aws(["ec2", *list_cmd], region)
    out: list[Resource] = []
    for item in data.get(list_key, []):
        if skip and skip(item):
            continue
        tags = _tags_from_list(item.get(tag_field))
        rid = item[id_field]
        name = item.get(name_field, rid) if name_field else rid
        created = item.get(created_field) if created_field else None
        out.append(Resource(kind=kind, id=rid, name=name, created=created, tagged=_matches(tags, tag_filter)))
    return out


def list_ec2_instances(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    data = run_aws(["ec2", "describe-instances"], region)
    out: list[Resource] = []
    for reservation in data.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            if inst.get("State", {}).get("Name") == "terminated":
                continue
            tags = _tags_from_list(inst.get("Tags"))
            out.append(
                Resource(
                    kind="ec2-instance",
                    id=inst["InstanceId"],
                    name=inst["InstanceId"],
                    created=inst.get("LaunchTime"),
                    tagged=_matches(tags, tag_filter),
                )
            )
    return out


def list_ebs_volumes(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    data = run_aws(["ec2", "describe-volumes"], region)
    out: list[Resource] = []
    for vol in data.get("Volumes", []):
        tags = _tags_from_list(vol.get("Tags"))
        out.append(
            Resource(
                kind="ebs-volume",
                id=vol["VolumeId"],
                name=vol["VolumeId"],
                created=vol.get("CreateTime"),
                tagged=_matches(tags, tag_filter),
                extra={"state": vol.get("State", "")},
            )
        )
    return out


def list_ebs_snapshots(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    return _list_ec2(
        ["describe-snapshots", "--owner-ids", "self"],
        "Snapshots",
        "SnapshotId",
        region,
        tag_filter,
        created_field="StartTime",
        kind="ebs-snapshot",
    )


def list_nat_gateways(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    return _list_ec2(
        ["describe-nat-gateways"],
        "NatGateways",
        "NatGatewayId",
        region,
        tag_filter,
        created_field="CreateTime",
        kind="nat-gateway",
        skip=lambda i: i.get("State") == "deleted",
    )


def list_eips(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    return _list_ec2(
        ["describe-addresses"],
        "Addresses",
        "AllocationId",
        region,
        tag_filter,
        name_field="PublicIp",
        kind="eip",
    )


def list_security_groups(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    return _list_ec2(
        ["describe-security-groups"],
        "SecurityGroups",
        "GroupId",
        region,
        tag_filter,
        name_field="GroupName",
        kind="security-group",
        skip=lambda i: i.get("GroupName") == "default",
    )


def list_enis(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    return _list_ec2(
        ["describe-network-interfaces"],
        "NetworkInterfaces",
        "NetworkInterfaceId",
        region,
        tag_filter,
        tag_field="TagSet",
        kind="eni",
    )


def list_vpcs(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    return _list_ec2(
        ["describe-vpcs"], "Vpcs", "VpcId", region, tag_filter, kind="vpc", skip=lambda i: i.get("IsDefault")
    )


def list_subnets(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    default_vpcs = _default_vpc_ids(region)
    return _list_ec2(
        ["describe-subnets"],
        "Subnets",
        "SubnetId",
        region,
        tag_filter,
        kind="subnet",
        skip=lambda i: i.get("DefaultForAz") or i.get("VpcId") in default_vpcs,
    )


def list_route_tables(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    default_vpcs = _default_vpc_ids(region)
    return _list_ec2(
        ["describe-route-tables"],
        "RouteTables",
        "RouteTableId",
        region,
        tag_filter,
        kind="route-table",
        skip=lambda i: i.get("VpcId") in default_vpcs
        or any(a.get("Main") for a in i.get("Associations", [])),
    )


def list_internet_gateways(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    default_vpcs = _default_vpc_ids(region)
    data = run_aws(["ec2", "describe-internet-gateways"], region)
    out: list[Resource] = []
    for igw in data.get("InternetGateways", []):
        attachments = igw.get("Attachments", [])
        if any(a.get("VpcId") in default_vpcs for a in attachments):
            continue
        tags = _tags_from_list(igw.get("Tags"))
        vpc_id = attachments[0]["VpcId"] if attachments else ""
        out.append(
            Resource(
                kind="internet-gateway",
                id=igw["InternetGatewayId"],
                name=igw["InternetGatewayId"],
                created=None,
                tagged=_matches(tags, tag_filter),
                extra={"vpc_id": vpc_id},
            )
        )
    return out


def list_vpc_endpoints(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    default_vpcs = _default_vpc_ids(region)
    return _list_ec2(
        ["describe-vpc-endpoints"],
        "VpcEndpoints",
        "VpcEndpointId",
        region,
        tag_filter,
        kind="vpc-endpoint",
        skip=lambda i: i.get("VpcId") in default_vpcs,
    )


def _elbv2_tags(arns: list[str], region: str) -> dict[str, dict]:
    if not arns:
        return {}
    data = run_aws(["elbv2", "describe-tags", "--resource-arns", *arns], region)
    return {d["ResourceArn"]: _tags_from_list(d.get("Tags")) for d in data.get("TagDescriptions", [])}


def list_load_balancers(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    lbs = run_aws(["elbv2", "describe-load-balancers"], region).get("LoadBalancers", [])
    tags_by_arn = _elbv2_tags([lb["LoadBalancerArn"] for lb in lbs], region)
    return [
        Resource(
            kind="load-balancer",
            id=lb["LoadBalancerArn"],
            name=lb["LoadBalancerName"],
            created=lb.get("CreatedTime"),
            tagged=_matches(tags_by_arn.get(lb["LoadBalancerArn"], {}), tag_filter),
        )
        for lb in lbs
    ]


def list_target_groups(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    tgs = run_aws(["elbv2", "describe-target-groups"], region).get("TargetGroups", [])
    tags_by_arn = _elbv2_tags([tg["TargetGroupArn"] for tg in tgs], region)
    return [
        Resource(
            kind="target-group",
            id=tg["TargetGroupArn"],
            name=tg["TargetGroupName"],
            created=None,  # the API exposes no creation time for a target group
            tagged=_matches(tags_by_arn.get(tg["TargetGroupArn"], {}), tag_filter),
        )
        for tg in tgs
    ]


def list_route53_zones(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    zones = run_aws(["route53", "list-hosted-zones"], region).get("HostedZones", [])
    out: list[Resource] = []
    for zone in zones:
        zone_id = zone["Id"].rsplit("/", 1)[-1]
        tags = {}
        try:
            tag_data = run_aws(
                ["route53", "list-tags-for-resource", "--resource-type", "hostedzone", "--resource-id", zone_id],
                region,
            )
            tags = _tags_from_list(tag_data.get("ResourceTagSet", {}).get("Tags"))
        except CloudSweepError:
            pass  # a zone with no tags is still a finding, just an untagged one
        out.append(
            Resource(kind="route53-zone", id=zone_id, name=zone["Name"], created=None, tagged=_matches(tags, tag_filter))
        )
    return out


def list_log_groups(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    groups = run_aws(["logs", "describe-log-groups"], region).get("logGroups", [])
    out: list[Resource] = []
    for group in groups:
        name = group["logGroupName"]
        tags: dict = {}
        try:
            tags = run_aws(["logs", "list-tags-log-group", "--log-group-name", name], region).get("tags", {})
        except CloudSweepError:
            pass
        created = None
        millis = group.get("creationTime")
        if millis:
            created = datetime.fromtimestamp(millis / 1000, tz=UTC).isoformat()
        out.append(Resource(kind="log-group", id=name, name=name, created=created, tagged=_matches(tags, tag_filter)))
    return out


def list_secrets(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    secrets = run_aws(["secretsmanager", "list-secrets", "--include-planned-deletion"], region).get("SecretList", [])
    out = []
    for secret in secrets:
        tags = _tags_from_list(secret.get("Tags"))
        out.append(
            Resource(
                kind="secret",
                id=secret["ARN"],
                name=secret["Name"],
                created=secret.get("CreatedDate"),
                tagged=_matches(tags, tag_filter),
                extra={"scheduled_deletion": str("DeletedDate" in secret)},
            )
        )
    return out


def list_kms_aliases(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    aliases = run_aws(["kms", "list-aliases"], region).get("Aliases", [])
    out = []
    for alias in aliases:
        name = alias["AliasName"]
        if name.startswith("alias/aws/"):
            continue  # AWS-managed, present in every account, never a proof's own
        tagged = False
        key_id = alias.get("TargetKeyId")
        if key_id:
            try:
                key_tags = run_aws(["kms", "list-resource-tags", "--key-id", key_id], region).get("Tags", [])
                tagged = _matches({t["TagKey"]: t["TagValue"] for t in key_tags}, tag_filter)
            except CloudSweepError:
                pass
        out.append(
            Resource(kind="kms-alias", id=name, name=name, created=alias.get("CreationDate"), tagged=tagged)
        )
    return out


def list_eks_clusters(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    names = run_aws(["eks", "list-clusters"], region).get("clusters", [])
    out = []
    for name in names:
        detail = run_aws(["eks", "describe-cluster", "--name", name], region).get("cluster", {})
        tags = detail.get("tags", {})
        out.append(
            Resource(kind="eks-cluster", id=name, name=name, created=detail.get("createdAt"), tagged=_matches(tags, tag_filter))
        )
    return out


def list_msk_clusters(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    clusters = run_aws(["kafka", "list-clusters-v2"], region).get("ClusterInfoList", [])
    out = []
    for cluster in clusters:
        if cluster.get("State") == "DELETING":
            continue
        tags = cluster.get("Tags", {})
        out.append(
            Resource(
                kind="msk-cluster",
                id=cluster["ClusterArn"],
                name=cluster["ClusterName"],
                created=cluster.get("CreationTime"),
                tagged=_matches(tags, tag_filter),
            )
        )
    return out


def list_s3_buckets(region: str, tag_filter: dict[str, str], exclude_bucket: str | None) -> list[Resource]:
    buckets = run_aws(["s3api", "list-buckets"], region).get("Buckets", [])
    out = []
    for bucket in buckets:
        name = bucket["Name"]
        tags = {}
        try:
            tag_data = run_aws(["s3api", "get-bucket-tagging", "--bucket", name], region)
            tags = _tags_from_list(tag_data.get("TagSet"))
        except CloudSweepError:
            pass  # a bucket with no tag set returns an error instead of an empty one
        out.append(
            Resource(
                kind="s3-bucket",
                id=name,
                name=name,
                created=bucket.get("CreationDate"),
                tagged=_matches(tags, tag_filter),
                extra={"excluded": str(name == exclude_bucket)},
            )
        )
    return out


PER_SERVICE_COLLECTORS: list[Callable[[str, dict], list[Resource]]] = [
    list_ec2_instances,
    list_ebs_volumes,
    list_ebs_snapshots,
    list_nat_gateways,
    list_eips,
    list_load_balancers,
    list_target_groups,
    list_security_groups,
    list_enis,
    list_vpcs,
    list_subnets,
    list_route_tables,
    list_internet_gateways,
    list_vpc_endpoints,
    list_route53_zones,
    list_log_groups,
    list_secrets,
    list_kms_aliases,
    list_eks_clusters,
    list_msk_clusters,
]


def collect(region: str, tag_filter: dict[str, str], exclude_bucket: str | None) -> list[Resource]:
    """Run every dedicated lister, then fold in anything the tagging API alone caught.

    A tagging-API hit is skipped when its bare id already matches a resource the
    dedicated listers found -- the two routes overlap by design, and de-duplicating
    on id (rather than a full ARN<->kind mapping) is enough to avoid double-counting
    the common case without needing a parser for every AWS ARN shape.
    """
    resources: list[Resource] = []
    for collector in PER_SERVICE_COLLECTORS:
        resources.extend(collector(region, tag_filter))
    resources.extend(list_s3_buckets(region, tag_filter, exclude_bucket))
    known_ids = {r.id for r in resources}
    for tagged in list_tagged(region, tag_filter):
        bare = tagged.id.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        if tagged.id in known_ids or bare in known_ids:
            continue
        resources.append(tagged)
        known_ids.add(tagged.id)
    return sorted(resources, key=lambda r: (r.kind, r.name))


# ---------------------------------------------------------------------------
# Filtering, reporting, exit code
# ---------------------------------------------------------------------------


def filter_for_delete(
    resources: list[Resource], *, include_untagged: bool, exclude_bucket: str | None
) -> list[Resource]:
    """The subset --delete would remove and the exit code is judged against."""
    eligible = []
    for r in resources:
        if r.kind == "s3-bucket" and r.id == exclude_bucket:
            continue
        if not r.tagged and not include_untagged:
            continue
        eligible.append(r)
    return eligible


def print_report(resources: list[Resource]) -> None:
    if not resources:
        print("no resources found")
        return
    for r in resources:
        tag_state = "dfe-tagged" if r.tagged else "untagged"
        print(f"{r.kind:16s} {r.name:44s} age={_age(r.created):8s} {tag_state}")


# ---------------------------------------------------------------------------
# Deletion, in dependency order: workloads and load balancers, then clusters,
# then network, then the rest.
# ---------------------------------------------------------------------------


def _wait_until(check: Callable[[], bool], timeout: int = POLL_TIMEOUT, interval: int = POLL_INTERVAL) -> bool:
    """Poll `check` (True once the resource is gone) up to `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(interval)
    return False


def _delete_ec2_instance(r: Resource, region: str) -> None:
    run_aws(["ec2", "terminate-instances", "--instance-ids", r.id], region)


def _delete_load_balancer(r: Resource, region: str) -> None:
    run_aws(["elbv2", "delete-load-balancer", "--load-balancer-arn", r.id], region)


def _delete_target_group(r: Resource, region: str) -> None:
    run_aws(["elbv2", "delete-target-group", "--target-group-arn", r.id], region)


def _delete_eks_cluster(r: Resource, region: str) -> None:
    """A nodegroup blocks cluster deletion, so every one goes first."""
    nodegroups = run_aws(["eks", "list-nodegroups", "--cluster-name", r.id], region).get("nodegroups", [])
    for ng in nodegroups:
        run_aws(["eks", "delete-nodegroup", "--cluster-name", r.id, "--nodegroup-name", ng], region)
        _wait_until(
            lambda ng=ng: ng
            not in run_aws(["eks", "list-nodegroups", "--cluster-name", r.id], region).get("nodegroups", [])
        )
    run_aws(["eks", "delete-cluster", "--name", r.id], region)


def _delete_msk_cluster(r: Resource, region: str) -> None:
    run_aws(["kafka", "delete-cluster", "--cluster-arn", r.id], region)


def _delete_nat_gateway(r: Resource, region: str) -> None:
    run_aws(["ec2", "delete-nat-gateway", "--nat-gateway-id", r.id], region)
    _wait_until(
        lambda: run_aws(["ec2", "describe-nat-gateways", "--nat-gateway-ids", r.id], region)["NatGateways"][0][
            "State"
        ]
        in ("deleted", "deleting")
    )


def _delete_eip(r: Resource, region: str) -> None:
    run_aws(["ec2", "release-address", "--allocation-id", r.id], region)


def _delete_eni(r: Resource, region: str) -> None:
    run_aws(["ec2", "delete-network-interface", "--network-interface-id", r.id], region)


def _delete_security_group(r: Resource, region: str) -> None:
    run_aws(["ec2", "delete-security-group", "--group-id", r.id], region)


def _delete_vpc_endpoint(r: Resource, region: str) -> None:
    run_aws(["ec2", "delete-vpc-endpoints", "--vpc-endpoint-ids", r.id], region)


def _delete_route_table(r: Resource, region: str) -> None:
    run_aws(["ec2", "delete-route-table", "--route-table-id", r.id], region)


def _delete_internet_gateway(r: Resource, region: str) -> None:
    vpc_id = r.extra.get("vpc_id")
    if vpc_id:
        run_aws(["ec2", "detach-internet-gateway", "--internet-gateway-id", r.id, "--vpc-id", vpc_id], region)
    run_aws(["ec2", "delete-internet-gateway", "--internet-gateway-id", r.id], region)


def _delete_subnet(r: Resource, region: str) -> None:
    run_aws(["ec2", "delete-subnet", "--subnet-id", r.id], region)


def _delete_vpc(r: Resource, region: str) -> None:
    run_aws(["ec2", "delete-vpc", "--vpc-id", r.id], region)


def _delete_ebs_volume(r: Resource, region: str) -> None:
    if r.extra.get("state") == "in-use":
        raise CloudSweepError(f"volume {r.id} is still attached")
    run_aws(["ec2", "delete-volume", "--volume-id", r.id], region)


def _delete_ebs_snapshot(r: Resource, region: str) -> None:
    run_aws(["ec2", "delete-snapshot", "--snapshot-id", r.id], region)


def _delete_route53_zone(r: Resource, region: str) -> None:
    run_aws(["route53", "delete-hosted-zone", "--id", r.id], region)


def _delete_log_group(r: Resource, region: str) -> None:
    run_aws(["logs", "delete-log-group", "--log-group-name", r.id], region)


def _delete_secret(r: Resource, region: str) -> None:
    run_aws(["secretsmanager", "delete-secret", "--secret-id", r.id, "--force-delete-without-recovery"], region)


def _delete_kms_alias(r: Resource, region: str) -> None:
    run_aws(["kms", "delete-alias", "--alias-name", r.id], region)


def _delete_s3_bucket(r: Resource, region: str) -> None:
    run_aws_text(["s3", "rm", f"s3://{r.id}", "--recursive"], region)
    run_aws(["s3api", "delete-bucket", "--bucket", r.id], region)


DELETE_ORDER: list[str] = [
    # Tier 1: workloads and load balancers.
    "ec2-instance",
    "load-balancer",
    "target-group",
    # Tier 2: clusters -- they own ENIs and security groups the network tier needs gone.
    "eks-cluster",
    "msk-cluster",
    # Tier 3: network.
    "nat-gateway",
    "eip",
    "eni",
    "security-group",
    "vpc-endpoint",
    "route-table",
    "internet-gateway",
    "subnet",
    "vpc",
    # Tier 4: the rest.
    "ebs-volume",
    "ebs-snapshot",
    "route53-zone",
    "log-group",
    "secret",
    "kms-alias",
    "s3-bucket",
]

DELETE_FNS: dict[str, Callable[[Resource, str], None]] = {
    "ec2-instance": _delete_ec2_instance,
    "load-balancer": _delete_load_balancer,
    "target-group": _delete_target_group,
    "eks-cluster": _delete_eks_cluster,
    "msk-cluster": _delete_msk_cluster,
    "nat-gateway": _delete_nat_gateway,
    "eip": _delete_eip,
    "eni": _delete_eni,
    "security-group": _delete_security_group,
    "vpc-endpoint": _delete_vpc_endpoint,
    "route-table": _delete_route_table,
    "internet-gateway": _delete_internet_gateway,
    "subnet": _delete_subnet,
    "vpc": _delete_vpc,
    "ebs-volume": _delete_ebs_volume,
    "ebs-snapshot": _delete_ebs_snapshot,
    "route53-zone": _delete_route53_zone,
    "log-group": _delete_log_group,
    "secret": _delete_secret,
    "kms-alias": _delete_kms_alias,
    "s3-bucket": _delete_s3_bucket,
}


def delete_resources(resources: list[Resource], region: str) -> list[str]:
    """Delete `resources` in DELETE_ORDER, returning one message per failure."""
    by_kind: dict[str, list[Resource]] = {}
    for r in resources:
        by_kind.setdefault(r.kind, []).append(r)
    failures: list[str] = []
    for kind in DELETE_ORDER:
        for r in by_kind.get(kind, []):
            try:
                DELETE_FNS[kind](r, region)
                print(f"deleted {kind} {r.name}")
            except CloudSweepError as exc:
                failures.append(f"{kind} {r.name}: {exc}")
                print(f"FAILED to delete {kind} {r.name}: {exc}", file=sys.stderr)
    unhandled = {r.kind for r in resources} - set(DELETE_FNS)
    for kind in unhandled:
        for r in by_kind.get(kind, []):
            failures.append(f"{kind} {r.name}: no scripted delete for this class, remove by hand")
    return failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_tag_filter(raw: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        key, _, value = item.partition("=")
        pairs[key.strip()] = value.strip()
    return pairs


def verify_account(region: str, expected_account: str) -> None:
    """Refuse to delete unless the live session's account matches --account.

    A name describes intent; only the id `aws sts get-caller-identity` returns
    describes which account the delete calls actually land against.
    """
    identity = run_aws(["sts", "get-caller-identity"], region)
    actual = identity.get("Account", "")
    if actual != expected_account:
        raise CloudSweepError(
            f"--account {expected_account} does not match the authenticated account "
            f"{actual or 'unknown'} -- re-authenticate, or correct --account. Refusing to delete."
        )


def confirm_delete(eligible: list[Resource], *, skip_prompt: bool) -> bool:
    """Print the full eligible list and get an explicit "yes", unless --yes skips the prompt."""
    print(f"About to delete {len(eligible)} resource(s):")
    for r in eligible:
        print(f"  {r.kind} {r.name}")
    if skip_prompt:
        return True
    reply = input(f"Type 'yes' to delete these {len(eligible)} resource(s): ")
    return reply.strip() == "yes"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List, and on --delete remove, leftover AWS resources from a DFE cloud proof."
    )
    parser.add_argument("--region", required=True, help="AWS region to sweep, e.g. us-west-2")
    parser.add_argument("--delete", action="store_true", help="delete what is found instead of only listing it")
    parser.add_argument(
        "--account",
        default=None,
        help="12-digit AWS account id --delete must run against. Required with --delete, and checked "
        "against `aws sts get-caller-identity` before anything is touched -- refused on a mismatch.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive confirmation --delete otherwise prints the eligible list and asks for "
        "(for CI; a human runs without it)",
    )
    parser.add_argument(
        "--exclude-bucket", default=None, help="S3 bucket name never to touch, e.g. the tofu state bucket"
    )
    parser.add_argument(
        "--tag-filter",
        default=DEFAULT_TAG_FILTER,
        help="comma-separated key=value pairs a resource must carry to count as dfe's",
    )
    parser.add_argument(
        "--include-untagged",
        action="store_true",
        help="DANGEROUS with --delete: also delete and count resources that carry none of the tag-filter "
        "tags -- turns --delete into a region-wide wipe of everything the listers found in --account, "
        "tagged or not (--exclude-bucket is the one exemption). See the module docstring.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tag_filter = parse_tag_filter(args.tag_filter)

    if args.delete:
        if not args.account:
            print(
                "--delete needs --account <12-digit-id>, checked against the live session -- "
                "refusing to delete against whatever account happens to be authenticated.",
                file=sys.stderr,
            )
            return 2
        try:
            verify_account(args.region, args.account)
        except CloudSweepError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    resources = collect(args.region, tag_filter, args.exclude_bucket)
    print_report(resources)
    eligible = filter_for_delete(resources, include_untagged=args.include_untagged, exclude_bucket=args.exclude_bucket)

    if args.delete and eligible:
        if not confirm_delete(eligible, skip_prompt=args.yes):
            print("delete cancelled: no 'yes' received", file=sys.stderr)
            return 1

        failures = delete_resources(eligible, args.region)
        resources = collect(args.region, tag_filter, args.exclude_bucket)
        eligible = filter_for_delete(
            resources, include_untagged=args.include_untagged, exclude_bucket=args.exclude_bucket
        )
        if failures:
            print("failed to delete:", file=sys.stderr)
            for failure in failures:
                print(f"  {failure}", file=sys.stderr)

    if eligible:
        label = "remaining after sweep" if args.delete else "eligible for --delete"
        print(f"{label}:")
        for r in eligible:
            print(f"  {r.kind} {r.name}")
        return 1

    print("sweep clean: nothing left to delete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
