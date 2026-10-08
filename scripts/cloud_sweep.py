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

--expired switches the selection to the run convention in cloud_run.py: a
resource is eligible ONLY when it carries the run tag (default `dfe-e2e`) and
its `expires-at` plus --grace is in the past. A resource without the run tag,
with a future expiry, or with an expiry that does not parse is never selected,
whatever else it carries; --tag-filter still decides the tagged/untagged
column but not eligibility. --grace defaults to 1h, the margin a last-resort
hand run gives a run that is still tearing itself down; the scheduled reaper
passes 0. --now previews a later moment and is refused with --delete.

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
cleanup. It is refused beside --expired.

--provider picks the cloud. aws is the one with listers; gcp and azure are
the same interface with nothing behind it yet, and say so rather than
reporting an empty sweep.

Stdlib only: every AWS call shells out to the `aws` CLI with --output json and
--region, so a global call such as ListBuckets lands in the swept region and
never in whatever region the shell defaults to.

    python3 scripts/cloud_sweep.py --region us-west-2
    python3 scripts/cloud_sweep.py --region us-west-2 --delete \\
        --account 000000000000 --exclude-bucket example-tfstate-bucket
    python3 scripts/cloud_sweep.py --region us-west-2 --expired --grace 0 --delete \\
        --account 000000000000 --yes
"""

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import NoReturn, Protocol

import aws_cli
import cloud_run

AWS_TIMEOUT = 60  # seconds allowed for one aws CLI call before it is a hang, not a slow API
POLL_TIMEOUT = 300  # seconds to wait for an async delete (NAT gateway, EKS, MSK) to finish
POLL_INTERVAL = 10
ROUTE53_BATCH = 100  # changes per ChangeResourceRecordSets call, well inside the API's 1000

DEFAULT_TAG_FILTER = "service-name=dfe,environment=test"
DEFAULT_EXPIRED_GRACE = "1h"


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
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExpirySelection:
    """What --expired judges a resource against: the run convention, a clock and a grace."""

    keys: cloud_run.RunTagKeys
    now: float
    grace: int

    def state(self, resource: Resource) -> cloud_run.ExpiryState:
        """Say whether this resource's run is over."""
        return cloud_run.classify(resource.tags, now=self.now, grace=self.grace, keys=self.keys)


# ---------------------------------------------------------------------------
# aws CLI plumbing
# ---------------------------------------------------------------------------


def _region_args(args: list[str], region: str) -> list[str]:
    """`--region <region>`, never omitted: a call without it goes wherever the shell's region points."""
    if not region:
        raise CloudSweepError(f"aws {' '.join(args)} names no region: refusing to let the CLI pick one")
    return ["--region", region]


def run_aws(args: list[str], region: str) -> dict:
    """Run one `aws ... --output json` call in `region` and return its parsed body."""
    result = aws_cli.run_aws([*args, *_region_args(args, region), "--output", "json"], timeout=AWS_TIMEOUT)
    if result.returncode != 0:
        raise CloudSweepError(f"aws {' '.join(args)} failed: {result.stderr.strip()}")
    text = result.stdout.strip()
    return json.loads(text) if text else {}


def run_aws_text(args: list[str], region: str) -> None:
    """Run one `aws` call whose output is not JSON (e.g. `s3 rm`), for its side effect."""
    result = aws_cli.run_aws([*args, *_region_args(args, region)], timeout=AWS_TIMEOUT)
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


def list_tagged(
    region: str, server_filter: dict[str, str | None], tag_filter: dict[str, str]
) -> list[Resource]:
    """Everything the tagging API returns for `server_filter`, kind-labelled by ARN service.

    A None value in `server_filter` asks for the key with any value, which is how
    --expired finds every run-tagged resource whatever its run id. `tagged` is
    judged against `tag_filter` from the tags the API returns.
    """
    filters: list[str] = []
    for key, value in server_filter.items():
        filters += ["--tag-filters", f"Key={key}" if value is None else f"Key={key},Values={value}"]
    resources: list[Resource] = []
    token: str | None = None
    while True:
        args = ["resourcegroupstaggingapi", "get-resources", *filters]
        if token:
            args += ["--starting-token", token]
        data = run_aws(args, region)
        for mapping in data.get("ResourceTagMappingList", []):
            arn = mapping["ResourceARN"]
            tags = _tags_from_list(mapping.get("Tags"))
            resources.append(
                Resource(
                    kind=_kind_from_arn(arn),
                    id=arn,
                    name=_name_from_arn(arn),
                    created=None,
                    tagged=_matches(tags, tag_filter),
                    tags=tags,
                )
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
        out.append(
            Resource(kind=kind, id=rid, name=name, created=created, tagged=_matches(tags, tag_filter), tags=tags)
        )
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
                    tags=tags,
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
                tags=tags,
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
                tags=tags,
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
    out: list[Resource] = []
    for lb in lbs:
        tags = tags_by_arn.get(lb["LoadBalancerArn"], {})
        out.append(
            Resource(
                kind="load-balancer",
                id=lb["LoadBalancerArn"],
                name=lb["LoadBalancerName"],
                created=lb.get("CreatedTime"),
                tagged=_matches(tags, tag_filter),
                tags=tags,
            )
        )
    return out


def list_target_groups(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    tgs = run_aws(["elbv2", "describe-target-groups"], region).get("TargetGroups", [])
    tags_by_arn = _elbv2_tags([tg["TargetGroupArn"] for tg in tgs], region)
    out: list[Resource] = []
    for tg in tgs:
        tags = tags_by_arn.get(tg["TargetGroupArn"], {})
        out.append(
            Resource(
                kind="target-group",
                id=tg["TargetGroupArn"],
                name=tg["TargetGroupName"],
                created=None,  # the API exposes no creation time for a target group
                tagged=_matches(tags, tag_filter),
                tags=tags,
            )
        )
    return out


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
            Resource(
                kind="route53-zone",
                id=zone_id,
                name=zone["Name"],
                created=None,
                tagged=_matches(tags, tag_filter),
                tags=tags,
            )
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
        out.append(
            Resource(
                kind="log-group", id=name, name=name, created=created, tagged=_matches(tags, tag_filter), tags=tags
            )
        )
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
                tags=tags,
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
        tags: dict[str, str] = {}
        key_id = alias.get("TargetKeyId")
        if key_id:
            try:
                key_tags = run_aws(["kms", "list-resource-tags", "--key-id", key_id], region).get("Tags", [])
                tags = {t["TagKey"]: t["TagValue"] for t in key_tags}
            except CloudSweepError:
                pass
        out.append(
            Resource(
                kind="kms-alias",
                id=name,
                name=name,
                created=alias.get("CreationDate"),
                tagged=_matches(tags, tag_filter),
                tags=tags,
            )
        )
    return out


def list_eks_clusters(region: str, tag_filter: dict[str, str]) -> list[Resource]:
    names = run_aws(["eks", "list-clusters"], region).get("clusters", [])
    out = []
    for name in names:
        detail = run_aws(["eks", "describe-cluster", "--name", name], region).get("cluster", {})
        tags = detail.get("tags", {})
        out.append(
            Resource(
                kind="eks-cluster",
                id=name,
                name=name,
                created=detail.get("createdAt"),
                tagged=_matches(tags, tag_filter),
                tags=tags,
            )
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
                tags=tags,
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
                tags=tags,
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


def collect(
    region: str,
    tag_filter: dict[str, str],
    exclude_bucket: str | None,
    *,
    route1_filter: dict[str, str | None] | None = None,
) -> list[Resource]:
    """Run every dedicated lister, then fold in anything the tagging API alone caught.

    A tagging-API hit is skipped when its bare id already matches a resource the
    dedicated listers found -- the two routes overlap by design, and de-duplicating
    on id (rather than a full ARN<->kind mapping) is enough to avoid double-counting
    the common case without needing a parser for every AWS ARN shape.

    `route1_filter` is what the tagging API is asked for; it defaults to
    `tag_filter`, and --expired passes the run key with any value.
    """
    resources: list[Resource] = []
    for collector in PER_SERVICE_COLLECTORS:
        resources.extend(collector(region, tag_filter))
    resources.extend(list_s3_buckets(region, tag_filter, exclude_bucket))
    known_ids = {r.id for r in resources}
    server_filter: dict[str, str | None] = dict(tag_filter) if route1_filter is None else route1_filter
    for tagged in list_tagged(region, server_filter, tag_filter):
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


def filter_expired(
    resources: list[Resource], *, expiry: ExpirySelection, exclude_bucket: str | None
) -> list[Resource]:
    """The subset --expired --delete would remove: run-tagged and past expiry plus grace, only."""
    return [
        r
        for r in resources
        if not (r.kind == "s3-bucket" and r.id == exclude_bucket)
        and expiry.state(r) is cloud_run.ExpiryState.EXPIRED
    ]


def print_report(resources: list[Resource], expiry: ExpirySelection | None = None) -> None:
    if not resources:
        print("no resources found")
        return
    for r in resources:
        tag_state = "dfe-tagged" if r.tagged else "untagged"
        line = f"{r.kind:16s} {r.name:44s} age={_age(r.created):8s} {tag_state}"
        if expiry is not None:
            run_id = r.tags.get(expiry.keys.run) or "-"
            line += f" run={run_id} {expiry.state(r)}"
        print(line)


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


def _vpc_endpoint_gone(r: Resource, region: str) -> bool:
    """True once an interface endpoint has released the network interfaces it holds."""
    try:
        endpoints = run_aws(["ec2", "describe-vpc-endpoints", "--vpc-endpoint-ids", r.id], region).get(
            "VpcEndpoints", []
        )
    except CloudSweepError as exc:
        if "NotFound" in str(exc):
            return True
        raise
    return all(e.get("State", "").lower() == "deleted" for e in endpoints)


def _delete_vpc_endpoint(r: Resource, region: str) -> None:
    """An interface endpoint's ENIs hold its security groups, so the security group tier waits on this."""
    run_aws(["ec2", "delete-vpc-endpoints", "--vpc-endpoint-ids", r.id], region)
    _wait_until(lambda: _vpc_endpoint_gone(r, region))


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


def _zone_apex_record(record: dict, zone_name: str) -> bool:
    """The zone's own SOA and NS sets, which Route 53 refuses to delete and removes with the zone."""
    name = record.get("Name", "").rstrip(".").lower()
    return record.get("Type") in ("SOA", "NS") and name == zone_name.rstrip(".").lower()


def _delete_route53_zone(r: Resource, region: str) -> None:
    """DeleteHostedZone refuses a zone holding any record but its apex SOA and NS, so those go first."""
    records = run_aws(["route53", "list-resource-record-sets", "--hosted-zone-id", r.id], region).get(
        "ResourceRecordSets", []
    )
    changes = [
        {"Action": "DELETE", "ResourceRecordSet": record}
        for record in records
        if not _zone_apex_record(record, r.name)
    ]
    for start in range(0, len(changes), ROUTE53_BATCH):
        batch = {"Changes": changes[start : start + ROUTE53_BATCH]}
        run_aws(
            ["route53", "change-resource-record-sets", "--hosted-zone-id", r.id, "--change-batch", json.dumps(batch)],
            region,
        )
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
    # Tier 3: network. An endpoint's ENIs hold security groups, so endpoints go first.
    "nat-gateway",
    "eip",
    "vpc-endpoint",
    "eni",
    "security-group",
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


# ---------------------------------------------------------------------------
# Providers: one interface, so GCP and Azure listers slot in beside AWS's
# ---------------------------------------------------------------------------


class SweepProvider(Protocol):
    """What main() needs from a cloud: prove the account, list, delete in order."""

    name: str

    def verify_account(self, expected: str) -> None:
        """Raise CloudSweepError unless the live session is in `expected`."""
        ...

    def collect(self, tag_filter: dict[str, str], exclude_bucket: str | None, *, run_key: str | None) -> list[Resource]:
        """Every resource found; `run_key` widens the catch-all to every run-tagged one."""
        ...

    def delete(self, resources: list[Resource]) -> list[str]:
        """Delete in dependency order, returning one message per failure."""
        ...


@dataclass(frozen=True, slots=True)
class AwsProvider:
    """The AWS listers and deletes above, in one region."""

    region: str
    name: str = "aws"

    def verify_account(self, expected: str) -> None:
        verify_account(self.region, expected)

    def collect(self, tag_filter: dict[str, str], exclude_bucket: str | None, *, run_key: str | None) -> list[Resource]:
        route1: dict[str, str | None] | None = {run_key: None} if run_key else None
        return collect(self.region, tag_filter, exclude_bucket, route1_filter=route1)

    def delete(self, resources: list[Resource]) -> list[str]:
        return delete_resources(resources, self.region)


@dataclass(frozen=True, slots=True)
class _UnbuiltProvider:
    """A cloud with the interface and no listers, which refuses rather than reporting clean."""

    region: str
    name: str = "unbuilt"

    def _refuse(self) -> NoReturn:
        raise NotImplementedError(
            f"cloud_sweep has no {self.name} listers yet: an empty {self.name} sweep would read as "
            "clean when nothing was looked at. Remove leftovers through the console or CLI until "
            f"{self.name} listers and deletes are added beside AwsProvider."
        )

    def verify_account(self, expected: str) -> None:
        self._refuse()

    def collect(self, tag_filter: dict[str, str], exclude_bucket: str | None, *, run_key: str | None) -> list[Resource]:
        self._refuse()

    def delete(self, resources: list[Resource]) -> list[str]:
        self._refuse()


@dataclass(frozen=True, slots=True)
class GcpProvider(_UnbuiltProvider):
    """GCP: run labels follow the same convention, with the epoch-seconds expiry format."""

    name: str = "gcp"


@dataclass(frozen=True, slots=True)
class AzureProvider(_UnbuiltProvider):
    """Azure: run tags follow the same convention."""

    name: str = "azure"


PROVIDERS: dict[str, Callable[[str], SweepProvider]] = {
    "aws": AwsProvider,
    "gcp": GcpProvider,
    "azure": AzureProvider,
}


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
        description="List, and on --delete remove, leftover cloud resources from a DFE cloud proof."
    )
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        default="aws",
        help="cloud to sweep; gcp and azure have the interface and no listers yet, and refuse by name",
    )
    parser.add_argument("--region", required=True, help="region to sweep, e.g. us-west-2")
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
    parser.add_argument(
        "--expired",
        action="store_true",
        help="select ONLY resources carrying the run tag (DFE_RUN_TAG_KEY, default dfe-e2e) whose "
        "expires-at plus --grace is in the past; nothing without the run tag is ever selected",
    )
    parser.add_argument(
        "--grace",
        default=None,
        help=f"with --expired: how long past expires-at before a resource counts (default "
        f"{DEFAULT_EXPIRED_GRACE}; the scheduled reaper passes 0). N, Ns, Nm, Nh or Nd",
    )
    parser.add_argument(
        "--now",
        type=int,
        default=None,
        help="with --expired: judge expiry at this epoch second instead of the current time, to preview "
        "what a later sweep would select. Refused with --delete",
    )
    return parser


def _refuse(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tag_filter = parse_tag_filter(args.tag_filter)

    expiry: ExpirySelection | None = None
    if args.expired:
        if args.include_untagged:
            return _refuse("--expired selects run-tagged resources only; --include-untagged contradicts it.")
        if args.now is not None and args.delete:
            return _refuse("--now previews a later moment and cannot be combined with --delete.")
        try:
            keys = cloud_run.RunTagKeys.from_env()
            grace = cloud_run.parse_duration(args.grace or DEFAULT_EXPIRED_GRACE)
        except cloud_run.RunTagError as exc:
            return _refuse(f"--expired: {exc}")
        now = float(args.now) if args.now is not None else time.time()
        expiry = ExpirySelection(keys=keys, now=now, grace=grace)
    elif args.grace is not None or args.now is not None:
        return _refuse("--grace and --now only apply with --expired.")

    provider = PROVIDERS[args.provider](args.region)
    try:
        return _sweep(args, provider, tag_filter, expiry)
    except NotImplementedError as exc:
        return _refuse(str(exc))


def _eligible(
    resources: list[Resource], args: argparse.Namespace, expiry: ExpirySelection | None
) -> list[Resource]:
    if expiry is not None:
        return filter_expired(resources, expiry=expiry, exclude_bucket=args.exclude_bucket)
    return filter_for_delete(resources, include_untagged=args.include_untagged, exclude_bucket=args.exclude_bucket)


def _sweep(
    args: argparse.Namespace,
    provider: SweepProvider,
    tag_filter: dict[str, str],
    expiry: ExpirySelection | None,
) -> int:
    if args.delete:
        if not args.account:
            return _refuse(
                "--delete needs --account <12-digit-id>, checked against the live session -- "
                "refusing to delete against whatever account happens to be authenticated."
            )
        try:
            provider.verify_account(args.account)
        except CloudSweepError as exc:
            return _refuse(str(exc))

    run_key = expiry.keys.run if expiry else None
    resources = provider.collect(tag_filter, args.exclude_bucket, run_key=run_key)
    print_report(resources, expiry)
    eligible = _eligible(resources, args, expiry)

    if args.delete and eligible:
        if not confirm_delete(eligible, skip_prompt=args.yes):
            print("delete cancelled: no 'yes' received", file=sys.stderr)
            return 1

        failures = provider.delete(eligible)
        resources = provider.collect(tag_filter, args.exclude_bucket, run_key=run_key)
        eligible = _eligible(resources, args, expiry)
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

    if expiry is not None:
        print("sweep clean: no expired run resources left")
    else:
        print("sweep clean: nothing left to delete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
