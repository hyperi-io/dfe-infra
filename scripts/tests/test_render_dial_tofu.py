#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_render_dial_tofu.py
#  Purpose:      Guard the dial -> tfvars render: it emits exactly the variables
#                the aws root declares, turns the dial's comma-separated scalars
#                into lists, and refuses a dial the root would refuse later.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for `render_dial.py --tofu` -- the tofu half of the deployment dial.

The roots receive values and compute none, so a field the render drops is a
prompt on an unattended plan and a field it invents is an error there. The
variable-set comparison below is what keeps the two in step.

    python3 -m pytest scripts/tests/test_render_dial_tofu.py -q
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(1, str(REPO_ROOT / "bootstrap"))
import check_platform  # noqa: E402
import node_images  # noqa: E402
import render_dial  # noqa: E402
from yaml_subset import parse as parse_dial  # noqa: E402

AWS_VARIABLES = REPO_ROOT / "terraform" / "environments" / "aws" / "variables.tf"
_VARIABLE_RE = re.compile(r'^variable\s+"([a-z_]+)"', re.MULTILINE)

# Every field the render reads, and nothing that names a real deployment: RFC
# 5737 documentation addresses and the reserved example domain.
DIAL = """
substrate: k8s

metadata:
  name: dfe-example
  owner: ""

profile: scale
registry: ghcr.io/hyperi-io

target:
  provision:
    cloud: aws
    account: "000000000000"
    region: us-west-2
    cidr: 10.90.0.0/16

kubernetes_version: "1.36"

network:
  nat: single

endpoint:
  public: "true"
  allowed_cidrs: 198.51.100.10/32, 203.0.113.0/24

dns:
  private_zone: dfe-example.internal
  public_zone: dfe.example.com

telemetry:
  aws:
    sink: cloudwatch
    retention_days: 5

kafka:
  provider: msk
  msk:
    shape_ref: msk-broker
    broker_count: 3
    broker_version: 4.2.x.kraft
    num_partitions: 12
    log_retention_ms: 259200000
    message_max_bytes: 16777216
    scram_username: dfe-kafka-user
    bootstrap_job:
      namespace: strimzi
      service_account: dfe-kafka-bootstrap
  landing_topics:
    main_land:

state:
  bucket: example-tfstate
  key: dfe/test/aws.tfstate
  region: us-west-2

tags:
  service-name: dfe
  service-namespace: example
  environment: test
  owner: owner@example.com
  cost-center: experiments
  lifecycle: ephemeral

secrets:
  backend: aws-sm
  ref: dfe

k8s:
  env: test
  storage_class: gp3
  repo_url: https://github.com/example/dfe-infra.git
  target_revision: main

endpoints:
  clickhouse_host: dfe-clickhouse.clickhouse.svc.cluster.local
  kafka_bootstrap: ""
  otel_endpoint: otel-collector-gateway.otel.svc.cluster.local:4317
"""

ALLOWED_CIDRS_LINE = "allowed_cidrs: 198.51.100.10/32, 203.0.113.0/24"

GHCR_CREDENTIAL = "arn:aws:secretsmanager:us-west-2:000000000000:secret:ecr-pullthroughcache/ghcr-AbCdEf"


def dial(*, drop: str = "", replace: tuple[str, str] | None = None) -> dict[str, object]:
    """Parse the fixture dial, optionally with one line dropped or rewritten."""
    text = DIAL
    if replace:
        text = text.replace(*replace)
    if drop:
        text = "\n".join(line for line in text.splitlines() if drop not in line)
    return parse_dial(text, source="test-dial")


def render(**kwargs: object) -> dict[str, object]:
    """The variables one render of the fixture dial produces."""
    _, variables = render_dial._tofu_vars(dial(**kwargs))  # type: ignore[arg-type]
    return variables


def test_the_render_emits_exactly_what_the_root_declares() -> None:
    """A variable the root declares and the render omits prompts on an unattended plan --
    except node_pools and resolved_shapes, which resolve_sizing.py's build_tfvars is now
    the SINGLE writer of (see _tofu_vars's own docstring): the two tfvars producers used
    to collide on node_pools (the correctness review's P1-3), so this render emits neither
    variable at all rather than risk the same collision on resolved_shapes too."""
    declared = set(_VARIABLE_RE.findall(AWS_VARIABLES.read_text(encoding="utf-8")))
    # These default in the root and are set by a guarded test run's own overlay,
    # never by the dial.
    guarded_run_only = {
        "run",
        "permissions_boundary",
        "iam_path",
        "s3_bucket_prefix",
        "inspector_ec2_exclusion",
        "cloudtrail",
        "edge_self_ranges",
    }
    # A credential the root reads from TF_VAR_*, which the dial never carries.
    environment_only = {"clickhouse_external_password"}
    # resolve_sizing.py images writes these into node-images.auto.tfvars.json, never the dial render.
    images = node_images.tfvars_keys("aws")
    expected = (
        declared - {"node_pools", "resolved_shapes"} - guarded_run_only - environment_only - images
    )
    assert set(render()) == expected


def test_provision_carries_the_account_the_root_asserts() -> None:
    assert render()["provision"]["account"] == "000000000000"


def test_comma_separated_scalars_become_lists() -> None:
    """The dial's YAML subset carries no list, so a scalar is split instead."""
    variables = render()
    assert variables["endpoint"]["allowed_cidrs"] == ["198.51.100.10/32", "203.0.113.0/24"]


def test_the_public_endpoint_renders_as_a_bool() -> None:
    assert render()["endpoint"]["public"] is True
    closed = render(replace=('public: "true"', 'public: "false"'))
    assert closed["endpoint"]["public"] is False


def test_a_public_endpoint_with_no_allowlist_is_refused() -> None:
    with pytest.raises(render_dial.DialError, match="allowed_cidrs"):
        render(replace=(ALLOWED_CIDRS_LINE, 'allowed_cidrs: ""'))


def test_a_cloud_with_no_root_is_refused_by_name() -> None:
    with pytest.raises(render_dial.DialError, match="oracle"):
        render(replace=("cloud: aws", "cloud: oracle"))


def test_a_missing_field_is_named() -> None:
    with pytest.raises(render_dial.DialError, match=r"network\.nat"):
        render(drop="nat: single")


def test_az_count_defaults_to_three() -> None:
    assert render()["network"]["az_count"] == 3


def test_az_count_takes_the_dials_own_value() -> None:
    assert render(replace=("nat: single", "nat: single\n  az_count: 2"))["network"]["az_count"] == 2


def test_an_out_of_range_az_count_is_refused_by_name() -> None:
    with pytest.raises(render_dial.DialError, match=r"network\.az_count"):
        render(replace=("nat: single", "nat: single\n  az_count: 7"))


def test_subnet_newbits_defaults_to_sixteen_blocks() -> None:
    assert render()["network"]["subnet_newbits"] == render_dial.SUBNET_NEWBITS == 4


def test_subnet_newbits_takes_the_dials_own_value() -> None:
    network = render(replace=("nat: single", "nat: single\n  subnet_newbits: 6"))["network"]
    assert network == {"nat": "single", "az_count": 3, "subnet_newbits": 6}


def test_subnet_newbits_too_small_for_the_zone_count_is_refused_by_name() -> None:
    """Three bits make eight blocks, and six zones need six on each side of the midpoint."""
    with pytest.raises(render_dial.DialError, match=r"network\.subnet_newbits"):
        render(replace=("nat: single", "nat: single\n  az_count: 6\n  subnet_newbits: 3"))


def test_the_eks_add_ons_are_the_versions_yaml_pins() -> None:
    """The cluster module pins nothing of its own, so the render carries the stack's eks-addons."""
    import check_versions_drift as drift

    pins = {
        key.split(".", 1)[1]: value
        for key, value in drift.load_versions().items()
        if key.startswith("eks-addons.")
    }
    assert pins, "versions.yaml's current stack pins no eks-addons"
    assert render()["eks_addon_versions"] == pins


def test_a_versions_file_with_no_eks_add_ons_renders_an_empty_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cluster module's preconditions refuse the plan, rather than this render guessing pins."""
    versions = tmp_path / "versions.yaml"
    versions.write_text('current: "9.9.9"\nstacks:\n  9.9.9:\n    platform:\n      eks: ">= 1.34"\n', encoding="utf-8")
    monkeypatch.setattr(render_dial, "VERSIONS_FILE", versions)
    assert render()["eks_addon_versions"] == {}


def test_the_lambda_runtimes_are_the_versions_yaml_pins() -> None:
    """The msk module pins no runtime of its own, so the render carries the stack's lambda-runtimes."""
    import check_versions_drift as drift

    pins = {
        key.split(".", 1)[1]: value
        for key, value in drift.load_versions().items()
        if key.startswith("lambda-runtimes.")
    }
    assert pins, "versions.yaml's current stack pins no lambda-runtimes"
    assert render()["lambda_runtimes"] == pins


def test_a_versions_file_with_no_lambda_runtimes_renders_an_empty_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The msk module's validation refuses the plan, rather than this render guessing a runtime."""
    versions = tmp_path / "versions.yaml"
    versions.write_text('current: "9.9.9"\nstacks:\n  9.9.9:\n    platform:\n      eks: ">= 1.34"\n', encoding="utf-8")
    monkeypatch.setattr(render_dial, "VERSIONS_FILE", versions)
    assert render()["lambda_runtimes"] == {}


def test_the_seventh_tag_names_the_root_that_created_the_resource() -> None:
    tags = render()["tags"]
    assert tags["iac-source"] == "dfe-infra/terraform/environments/aws"
    assert tags["owner"] == "owner@example.com"


def test_an_unknown_lifecycle_is_refused_by_name() -> None:
    with pytest.raises(render_dial.DialError, match=r"tags\.lifecycle"):
        render(replace=("lifecycle: ephemeral", "lifecycle: throwaway"))


def test_an_absent_secrets_ref_renders_empty() -> None:
    assert render(drop="ref: dfe")["secrets"]["ref"] == ""


def test_the_managed_broker_renders_what_the_module_refuses_to_default() -> None:
    msk = render()["kafka"]["msk"]
    assert msk["broker_version"] == "4.2.x.kraft"
    assert msk["broker_count"] == 3
    assert msk["bootstrap_job"] == {
        "namespace": "strimzi",
        "service_account": "dfe-kafka-bootstrap",
    }


def test_an_in_cluster_broker_renders_no_msk_block() -> None:
    """strimzi and redpanda run in the cluster, so the root creates no broker."""
    assert render(replace=("provider: msk", "provider: strimzi"))["kafka"] == {
        "provider": "strimzi"
    }


# The line every autoscaling test inserts after -- unique in the fixture, and the
# sibling key (autoscaling:) has to sit at msk:'s own 4-space depth, not
# bootstrap_job's 6-space one.
_AFTER_BOOTSTRAP_JOB = "      service_account: dfe-kafka-bootstrap\n  landing_topics:"


def test_msk_autoscaling_sends_its_sizing_when_the_dial_sets_nothing() -> None:
    """The root declares no default for the ceiling, the capacity or the headroom,
    so the render sends its own documented values; step stays the module's."""
    assert render()["kafka"]["msk"]["autoscaling"] == render_dial.MSK_AUTOSCALING_DEFAULTS


def test_msk_autoscaling_fields_reach_the_msk_block() -> None:
    autoscaling = render(
        replace=(
            _AFTER_BOOTSTRAP_JOB,
            "      service_account: dfe-kafka-bootstrap\n"
            "    autoscaling:\n"
            '      enabled: "false"\n'
            "      max_brokers: 9\n"
            "      step: 3\n"
            "      per_broker_capacity_mb_s: 75\n"
            "      headroom: 1.5\n"
            "      period_seconds: 120\n"
            "      evaluation_periods: 5\n"
            "  landing_topics:",
        )
    )["kafka"]["msk"]["autoscaling"]
    assert autoscaling == {
        "enabled": False,
        "max_brokers": 9,
        "step": 3,
        "per_broker_capacity_mb_s": 75,
        "headroom": 1.5,
        "period_seconds": 120,
        "evaluation_periods": 5,
    }


def test_msk_autoscaling_rejects_a_non_numeric_max_brokers() -> None:
    with pytest.raises(render_dial.DialError, match=r"kafka\.msk\.autoscaling\.max_brokers"):
        render(
            replace=(
                _AFTER_BOOTSTRAP_JOB,
                "      service_account: dfe-kafka-bootstrap\n"
                "    autoscaling:\n"
                "      max_brokers: six\n"
                "  landing_topics:",
            )
        )


def test_msk_autoscaling_rejects_a_non_numeric_headroom() -> None:
    with pytest.raises(render_dial.DialError, match=r"kafka\.msk\.autoscaling\.headroom"):
        render(
            replace=(
                _AFTER_BOOTSTRAP_JOB,
                "      service_account: dfe-kafka-bootstrap\n"
                "    autoscaling:\n"
                "      headroom: lots\n"
                "  landing_topics:",
            )
        )


def test_msk_autoscaling_is_not_read_for_a_saas_broker() -> None:
    """confluent-cloud and redpanda-cloud take no msk: block at all, autoscaling
    included -- MSK is the only provider with a broker-count scaler of its own."""
    kafka = render(replace=("provider: msk", "provider: confluent-cloud"))["kafka"]
    assert "autoscaling" not in kafka


def test_node_pools_and_resolved_shapes_are_never_emitted() -> None:
    """resolve_sizing.py's build_tfvars is the single writer of both (P1-3) --
    this render must never re-introduce the collision by emitting either."""
    variables = render()
    assert "node_pools" not in variables
    assert "resolved_shapes" not in variables


def test_a_confluent_cloud_broker_renders_no_msk_block() -> None:
    """confluent-cloud sizes and tunes itself, so no msk: sub-block is rendered."""
    kafka = render(replace=("provider: msk", "provider: confluent-cloud"))["kafka"]
    assert "msk" not in kafka
    assert kafka["provider"] == "confluent-cloud"


def test_a_redpanda_cloud_broker_renders_no_msk_block() -> None:
    """redpanda-cloud sizes and tunes itself, so no msk: sub-block is rendered."""
    kafka = render(replace=("provider: msk", "provider: redpanda-cloud"))["kafka"]
    assert "msk" not in kafka
    assert kafka["provider"] == "redpanda-cloud"


def test_a_saas_broker_gets_the_same_tuning_and_landing_topics_msk_would() -> None:
    """confluent-cloud and redpanda-cloud take no msk: block, but do take these."""
    for provider in ("confluent-cloud", "redpanda-cloud"):
        kafka = render(replace=("provider: msk", f"provider: {provider}"))["kafka"]
        assert kafka["num_partitions"] == 12
        assert kafka["log_retention_ms"] == 259200000
        assert kafka["message_max_bytes"] == 16777216
        assert kafka["landing_topics"] == {"main_land": {}}


def test_msk_reads_no_top_level_tuning_or_landing_topics() -> None:
    """msk's tuning lives inside msk:, and its topics are its bootstrap Job's."""
    kafka = render()["kafka"]
    assert "num_partitions" not in kafka
    assert "landing_topics" not in kafka


def test_a_saas_broker_with_no_landing_topics_is_refused_by_name() -> None:
    """No bootstrap Job on this path means tofu is the only thing that can create one."""
    without_topics = "\n".join(
        line for line in DIAL.splitlines() if line.strip() not in ("landing_topics:", "main_land:")
    )
    with pytest.raises(render_dial.DialError, match=r"kafka\.landing_topics"):
        render_dial._tofu_vars(
            parse_dial(without_topics.replace("provider: msk", "provider: confluent-cloud"), source="test-dial")
        )


# Placeholder ids in each vendor's own shape: Confluent's env- prefix, Redpanda's UUID.
CONFLUENT_ENVIRONMENT = "env-000000"
REDPANDA_RESOURCE_GROUP = "00000000-0000-0000-0000-000000000000"


def saas_kafka(provider: str, *lines: str, size: int = 16777216) -> dict[str, object]:
    """The kafka variable for one SaaS provider, with extra kafka: keys and the dial's message size."""
    extra = "".join(f"\n  {line}" for line in lines)
    text = DIAL.replace("provider: msk", f"provider: {provider}{extra}")
    text = text.replace("message_max_bytes: 16777216", f"message_max_bytes: {size}")
    _, variables = render_dial._tofu_vars(parse_dial(text, source="test-dial"))
    return variables["kafka"]  # type: ignore[return-value]


def test_a_saas_broker_carries_its_connectivity_to_the_root() -> None:
    """A cloud cycle cannot write topics and ACLs from outside the VPC, so it asks for public."""
    for provider in ("confluent-cloud", "redpanda-cloud"):
        kafka = saas_kafka(provider, "connectivity: public", "confluent_tier: basic", size=8388608)
        assert kafka["connectivity"] == "public", provider


def test_an_unstated_connectivity_is_left_to_the_roots_private_default() -> None:
    for provider in ("confluent-cloud", "redpanda-cloud"):
        assert "connectivity" not in saas_kafka(provider), provider


def test_an_unknown_connectivity_is_refused_by_name() -> None:
    with pytest.raises(render_dial.DialError, match=r"kafka\.connectivity"):
        saas_kafka("redpanda-cloud", "connectivity: peered")


def test_the_confluent_tier_and_environment_reach_the_root() -> None:
    kafka = saas_kafka(
        "confluent-cloud",
        "connectivity: public",
        "confluent_tier: basic",
        f"confluent_environment_id: {CONFLUENT_ENVIRONMENT}",
        size=8388608,
    )
    assert kafka["confluent_tier"] == "basic"
    assert kafka["confluent_environment_id"] == CONFLUENT_ENVIRONMENT
    assert kafka["message_max_bytes"] == 8388608


def test_a_confluent_dial_with_no_tier_is_sent_freight() -> None:
    """The root declares the tier no default, so the render always names one."""
    assert saas_kafka("confluent-cloud")["confluent_tier"] == render_dial.CONFLUENT_DEFAULT_TIER == "freight"


def test_an_unknown_confluent_tier_is_refused_by_name() -> None:
    """Standard is a real Confluent tier, but the module offers freight, enterprise and basic only."""
    with pytest.raises(render_dial.DialError, match=r"kafka\.confluent_tier"):
        saas_kafka("confluent-cloud", "confluent_tier: standard", size=8388608)


def test_a_malformed_confluent_environment_is_refused_by_name() -> None:
    with pytest.raises(render_dial.DialError, match=r"kafka\.confluent_environment_id"):
        saas_kafka("confluent-cloud", f"confluent_environment_id: {REDPANDA_RESOURCE_GROUP}")


def test_a_confluent_dial_with_no_rest_wait_is_sent_the_documented_one() -> None:
    """Neither the root nor the body defaults the wait, so the render always sends all of it."""
    assert saas_kafka("confluent-cloud")["confluent_rest_ready"] == render_dial.CONFLUENT_REST_READY_DEFAULTS == {
        "successes": 12,
        "timeout_seconds": 900,
        "interval_seconds": 5,
    }


def test_the_dials_rest_wait_reaches_the_root_field_by_field() -> None:
    kafka = saas_kafka("confluent-cloud", "confluent_rest_ready:", "  timeout_seconds: 1200")
    assert kafka["confluent_rest_ready"] == {"successes": 12, "timeout_seconds": 1200, "interval_seconds": 5}


def test_a_rest_wait_that_is_not_a_whole_number_is_refused_by_name() -> None:
    with pytest.raises(render_dial.DialError, match=r"kafka\.confluent_rest_ready\.successes"):
        saas_kafka("confluent-cloud", "confluent_rest_ready:", "  successes: twelve")


def test_basic_refuses_a_message_size_above_its_ceiling() -> None:
    """Confluent caps a Basic topic at 8 MiB; the render refuses rather than let apply fail on it."""
    with pytest.raises(render_dial.DialError) as refused:
        saas_kafka("confluent-cloud", "connectivity: public", "confluent_tier: basic")
    message = str(refused.value)
    assert "kafka.msk.message_max_bytes" in message
    assert "8388608" in message
    assert "16777216" in message
    assert "kafka.confluent_tier" in message


def test_basic_accepts_a_message_size_at_its_ceiling() -> None:
    kafka = saas_kafka("confluent-cloud", "connectivity: public", "confluent_tier: basic", size=8388608)
    assert kafka["message_max_bytes"] == 8388608


def test_the_larger_confluent_tiers_keep_the_16_mib_chain() -> None:
    assert saas_kafka("confluent-cloud")["message_max_bytes"] == 16777216
    for tier in ("freight", "enterprise"):
        assert saas_kafka("confluent-cloud", f"confluent_tier: {tier}")["message_max_bytes"] == 16777216


def test_the_redpanda_resource_group_reaches_the_root() -> None:
    kafka = saas_kafka(
        "redpanda-cloud", "connectivity: public", f"redpanda_resource_group_id: {REDPANDA_RESOURCE_GROUP}"
    )
    assert kafka["redpanda_resource_group_id"] == REDPANDA_RESOURCE_GROUP


def test_a_malformed_redpanda_resource_group_is_refused_by_name() -> None:
    with pytest.raises(render_dial.DialError, match=r"kafka\.redpanda_resource_group_id"):
        saas_kafka("redpanda-cloud", f"redpanda_resource_group_id: {CONFLUENT_ENVIRONMENT}")


def test_a_redpanda_dial_with_no_tier_is_sent_serverless() -> None:
    """The body declares the tier no default, so the render always names one."""
    assert saas_kafka("redpanda-cloud")["redpanda_tier"] == render_dial.REDPANDA_DEFAULT_TIER == "serverless"


def test_the_redpanda_tier_reaches_the_root() -> None:
    """dedicated reaches the root, whose body then refuses it at plan."""
    assert saas_kafka("redpanda-cloud", "redpanda_tier: dedicated")["redpanda_tier"] == "dedicated"


def test_an_unknown_redpanda_tier_is_refused_by_name() -> None:
    with pytest.raises(render_dial.DialError, match=r"kafka\.redpanda_tier"):
        saas_kafka("redpanda-cloud", "redpanda_tier: freight")


def test_the_redpanda_tiers_are_the_ones_the_body_validates() -> None:
    """The render's list and the body's validation name the same three products."""
    body = (REPO_ROOT / "terraform" / "modules" / "managed-kafka" / "redpanda-cloud" / "variables.tf").read_text(
        encoding="utf-8"
    )
    assert f"contains({json.dumps(list(render_dial.REDPANDA_TIERS))}, var.tier)" in body


def test_each_vendors_keys_are_read_for_that_vendor_only() -> None:
    """A Confluent tier left on a Redpanda dial neither caps its size nor reaches the root."""
    redpanda = saas_kafka("redpanda-cloud", "confluent_tier: basic", f"confluent_environment_id: {CONFLUENT_ENVIRONMENT}")
    assert "confluent_tier" not in redpanda
    assert "confluent_environment_id" not in redpanda
    assert "confluent_rest_ready" not in redpanda
    assert redpanda["message_max_bytes"] == 16777216
    confluent = saas_kafka(
        "confluent-cloud",
        "redpanda_tier: serverless",
        f"redpanda_resource_group_id: {REDPANDA_RESOURCE_GROUP}",
    )
    assert "redpanda_resource_group_id" not in confluent
    assert "redpanda_tier" not in confluent


def test_the_basic_ceiling_is_the_one_the_root_and_the_module_refuse_above() -> None:
    """Three copies of one vendor limit: the render, the root's validation and the module's check."""
    ceiling = render_dial.CONFLUENT_BASIC_MAX_MESSAGE_BYTES
    root = AWS_VARIABLES.read_text(encoding="utf-8")
    module = (REPO_ROOT / "terraform" / "modules" / "managed-kafka" / "confluent-cloud" / "main.tf").read_text(
        encoding="utf-8"
    )
    assert re.findall(r"coalesce\(var\.kafka\.message_max_bytes, 0\) > (\d+)", root) == [str(ceiling)]
    assert re.findall(r"^\s*basic\s*=\s*(\d+)\s*$", module, re.MULTILINE) == [str(ceiling)]


def test_the_kafka_seed_is_keyed_by_the_provider() -> None:
    """Its last segment is the key the kafka chart reads the credential back by.

    MSK takes none: its own AmazonMSK_ secret is the one copy the apps read.
    """
    assert not any(name.startswith("kafka/") for name in render()["seeds"])
    assert "kafka/strimzi" in render(replace=("provider: msk", "provider: strimzi"))["seeds"]
    assert "kafka/confluent-cloud" in render(
        replace=("provider: msk", "provider: confluent-cloud")
    )["seeds"]
    assert "kafka/redpanda-cloud" in render(
        replace=("provider: msk", "provider: redpanda-cloud")
    )["seeds"]


def test_only_the_confluent_seed_carries_a_username_field() -> None:
    """Confluent's API key IS the username, so the store has to hold it beside the secret."""
    confluent = render(replace=("provider: msk", "provider: confluent-cloud"))["seeds"]
    assert confluent["kafka/confluent-cloud"] == {"username": "", "password": ""}
    for provider in ("redpanda-cloud", "strimzi"):
        seeds = render(replace=("provider: msk", f"provider: {provider}"))["seeds"]
        assert seeds[f"kafka/{provider}"] == {"password": ""}, provider


def test_no_seed_carries_a_value() -> None:
    """An empty value is generated where it lands and never travels through here."""
    for fields in render()["seeds"].values():
        assert all(value == "" for value in fields.values())


def test_a_missing_broker_field_is_named() -> None:
    with pytest.raises(render_dial.DialError, match=r"kafka\.msk\.broker_version"):
        render(drop="broker_version: 4.2.x.kraft")


def test_the_dial_supplies_the_telemetry_sink_and_retention() -> None:
    """The fixture sets cloudwatch explicitly, to prove non-default values thread through."""
    assert render()["telemetry"] == {"sink": "cloudwatch", "retention_days": 5, "forced_retention_days": 1}


def test_telemetry_defaults_to_otel_with_a_two_day_retention() -> None:
    """DFE's monitoring goes to its own OTel feed, never CloudWatch, by default."""
    without_telemetry = "\n".join(
        line
        for line in DIAL.splitlines()
        if line.strip() not in ("telemetry:", "aws:", "sink: cloudwatch", "retention_days: 5")
    )
    _, variables = render_dial._tofu_vars(parse_dial(without_telemetry, source="test-dial"))
    assert variables["telemetry"] == {"sink": "otel", "retention_days": 2, "forced_retention_days": 1}


def test_telemetry_retention_defaults_to_seven_days_under_cloudwatch() -> None:
    """cloudwatch is the opt-in AWS-native path, and it defaults short too."""
    cloudwatch_no_retention = render(drop="retention_days: 5")
    assert cloudwatch_no_retention["telemetry"] == {
        "sink": "cloudwatch",
        "retention_days": 7,
        "forced_retention_days": 1,
    }


def test_the_forced_retention_takes_the_dials_own_value() -> None:
    forced = render(replace=("retention_days: 5", "retention_days: 5\n    forced_retention_days: 3"))
    assert forced["telemetry"]["forced_retention_days"] == 3


def test_a_forced_retention_cloudwatch_refuses_is_refused_by_name() -> None:
    """The forced log groups are CloudWatch under either sink, so 2 is never a period they take."""
    with pytest.raises(render_dial.DialError, match=r"telemetry\.aws\.forced_retention_days"):
        render(replace=("retention_days: 5", "retention_days: 5\n    forced_retention_days: 2"))


def test_an_unknown_telemetry_sink_is_refused() -> None:
    with pytest.raises(render_dial.DialError, match=r"telemetry\.aws\.sink"):
        render(replace=("sink: cloudwatch", "sink: splunk"))


def test_no_pull_secret_input_reaches_tofu() -> None:
    """The pull secret's inputs go from the deployer's environment straight to
    bootstrap.sh, so a registry credential never lands in tofu state. The
    registry cache names its upstream credentials by Secrets Manager ARN only."""
    variables = render(
        replace=("profile: scale", f"profile: scale\nregistry_cache:\n  credentials:\n    ghcr: {GHCR_CREDENTIAL}\n")
    )
    assert [key for key in variables if key.startswith("registry") and key != "registry_cache"] == []
    arns = [upstream["credential_arn"] for upstream in variables["registry_cache"]["upstreams"].values()]
    assert all(arn == "" or arn.startswith("arn:") for arn in arns)


def test_the_dials_kubernetes_version_wins_when_it_clears_the_platform_floor() -> None:
    """The repo's real versions.yaml floors aws at eks >=1.34; the fixture dial names 1.36."""
    assert render_dial._platform_version("aws") == "1.34"
    assert render()["kubernetes_version"] == "1.36"


def test_a_dial_kubernetes_version_below_the_platform_floor_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    versions = tmp_path / "versions.yaml"
    versions.write_text(
        'current: "9.9.9"\nstacks:\n  9.9.9:\n    platform:\n      eks: ">= 1.38"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(render_dial, "VERSIONS_FILE", versions)
    with pytest.raises(render_dial.DialError, match=r"1\.36") as excinfo:
        render()
    assert "1.38" in str(excinfo.value)


def test_a_dial_with_no_kubernetes_version_takes_the_platform_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stack keys are written bare and the `current` pointer quoted, as versions.yaml does."""
    versions = tmp_path / "versions.yaml"
    versions.write_text(
        'current: "9.9.9"\nstacks:\n  9.9.9:\n    platform:\n      eks: ">= 1.37"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(render_dial, "VERSIONS_FILE", versions)
    assert render_dial._platform_version("aws") == "1.37"
    assert render(drop='kubernetes_version: "1.36"')["kubernetes_version"] == "1.37"


def test_a_platform_range_floors_at_its_low_side_and_defaults_to_its_top(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gcp and azure have no key of their own, so they read the shared range."""
    versions = tmp_path / "versions.yaml"
    versions.write_text(
        'current: "9.9.9"\nstacks:\n  9.9.9:\n    platform:\n      kubernetes: "1.34-1.36"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(render_dial, "VERSIONS_FILE", versions)
    for cloud in ("gcp", "azure"):
        assert render_dial._platform_version(cloud) == "1.34"
        assert render_dial._kubernetes_version({"kubernetes_version": "1.35"}, cloud) == "1.35"
        assert render_dial._kubernetes_version({}, cloud) == "1.36"
        with pytest.raises(render_dial.DialError, match=r"below the platform floor 1\.34"):
            render_dial._kubernetes_version({"kubernetes_version": "1.33"}, cloud)


def test_an_empty_version_takes_the_minor_the_eks_addon_pins_are_built_for() -> None:
    """The real versions.yaml floors aws at eks >=1.34 and tests 1.34-1.36: the add-on pins target 1.36."""
    tree = render_dial._parse_yaml_subset(
        render_dial.VERSIONS_FILE.read_text(encoding="utf-8"), source="versions.yaml"
    )
    kube_proxy = tree["stacks"][tree["current"]]["eks-addons"]["kube-proxy"]
    minor = re.match(r"^v(\d+\.\d+)\.", kube_proxy)[1]
    assert render_dial._platform_version("aws") == "1.34"
    assert render_dial._kubernetes_version({}, "aws") == minor
    assert render(drop='kubernetes_version: "1.36"')["kubernetes_version"] == minor


def test_a_ceiling_below_the_clouds_own_floor_never_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    versions = tmp_path / "versions.yaml"
    versions.write_text(
        'current: "9.9.9"\nstacks:\n  9.9.9:\n    platform:\n      eks: ">=1.37"\n'
        '      kubernetes: "1.34-1.36"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(render_dial, "VERSIONS_FILE", versions)
    assert render_dial._kubernetes_version({}, "aws") == "1.37"


@pytest.mark.parametrize(
    ("value", "ceiling"),
    [(">=1.34", None), ("1.34-1.36", "1.36"), ("v1.34-v1.36", "1.36"), ("1.35", None)],
)
def test_a_platform_ceiling_is_the_high_side_when_there_is_one(value: str, ceiling: str | None) -> None:
    assert render_dial._platform_ceiling(value) == ceiling


@pytest.mark.parametrize(
    ("value", "floor"),
    [(">=1.34", "1.34"), (">= 1.37", "1.37"), ("1.34-1.36", "1.34"), ("v1.34-v1.36", "1.34"), ("1.35", "1.35")],
)
def test_a_platform_floor_is_the_low_side_whatever_the_spelling(value: str, floor: str) -> None:
    assert render_dial._platform_floor(value) == floor


def test_every_platform_floor_agrees_with_the_bootstrap_check() -> None:
    """bootstrap/check_platform.py refuses a cluster below the same floor this renders a dial against."""
    tree = render_dial._parse_yaml_subset(
        render_dial.VERSIONS_FILE.read_text(encoding="utf-8"), source="versions.yaml"
    )
    values = [
        str(value)
        for stack in tree["stacks"].values()
        for key, value in (stack.get("platform") or {}).items()
        if key in ("kubernetes", *render_dial._PLATFORM_KEY.values())
    ]
    assert values, "versions.yaml carries no platform floor to compare"
    for value in values:
        assert render_dial._platform_floor(value) == check_platform.parse_requirement(value)[0], value


def test_the_written_file_is_json_the_root_can_read(tmp_path: Path) -> None:
    out = tmp_path / render_dial.TFVARS_NAME
    assert render_dial._render_tofu(dial(), out) == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["provision"]["cloud"] == "aws"
    assert written["name"] == "dfe-example"


def test_a_dial_the_render_refuses_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / render_dial.TFVARS_NAME
    assert render_dial._render_tofu(dial(drop="cloud: aws"), out) == 1
    assert not out.exists()


def test_the_edge_module_is_on_unless_the_dial_turns_it_off() -> None:
    """A deployment with no door reaches nothing from outside the cluster, so
    the switch defaults on."""
    assert render()["edge"]["enabled"] is True
    off = render(replace=("profile: scale", "profile: scale\nedge:\n  enabled: false"))
    assert off["edge"]["enabled"] is False


def test_a_nonsense_edge_switch_is_refused_by_name() -> None:
    bad = ("profile: scale", "profile: scale\nedge:\n  enabled: maybe")
    with pytest.raises(render_dial.DialError, match=r"edge\.enabled"):
        render(replace=bad)


def test_the_tunnel_brings_its_own_address_unless_a_forwarder_is_asked_for() -> None:
    """byo is the default: the deployer already holds an address, and tofu
    creates nothing for the tunnel at all."""
    tunnel = render()["edge"]["tunnel"]
    assert tunnel["address"]["mode"] == "byo"
    assert tunnel["address"]["zone"] == ""
    assert tunnel["openvpn"] is True
    assert tunnel["source_ranges"] == []


def test_the_forwarder_launches_on_the_resolved_toolbox_shape_by_default() -> None:
    """The shape resolver picks the newest small arm64 burstable for the toolbox
    use case, and the forwarder borrows it: never an instance type in the dial."""
    assert render_dial.TUNNEL_FORWARDER_SHAPE == "toolbox"
    assert render()["edge"]["tunnel"]["address"]["shape_ref"] == "toolbox"
    assert "instance_type" not in render()["edge"]["tunnel"]["address"]


def test_the_forwarder_dial_reaches_the_tofu_variables() -> None:
    asked = (
        "profile: scale",
        "profile: scale\n"
        "edge:\n"
        "  ingest:\n"
        "    tunnel:\n"
        "      openvpn: false\n"
        "      loadBalancerSourceRanges: 203.0.113.0/24, 198.51.100.0/24\n"
        "      address:\n"
        "        mode: forwarder\n"
        "        shape_ref: general\n"
        "        zone: us-west-2c\n",
    )
    tunnel = render(replace=asked)["edge"]["tunnel"]
    assert tunnel["address"] == {"mode": "forwarder", "shape_ref": "general", "zone": "us-west-2c"}
    assert tunnel["openvpn"] is False
    assert tunnel["source_ranges"] == ["203.0.113.0/24", "198.51.100.0/24"]


def test_expected_fail_a_forwarder_instance_type_in_the_dial_is_refused() -> None:
    """A literal type would be read by nothing, so it is refused with the override that pins one."""
    asked = (
        "profile: scale",
        "profile: scale\nedge:\n  ingest:\n    tunnel:\n      address:\n        instance_type: t4g.small\n",
    )
    with pytest.raises(render_dial.DialError, match=r"sizing\.overrides\.toolbox\.instance_type"):
        render(replace=asked)


def test_the_tunnel_client_range_defaults_to_culverts_own() -> None:
    assert render()["edge"]["tunnel"]["client_cidr"] == render_dial.TUNNEL_CLIENT_CIDR


def test_the_tunnel_client_range_takes_the_dials_own_value() -> None:
    asked = ("profile: scale", "profile: scale\nedge:\n  ingest:\n    tunnel:\n      client_cidr: 100.96.0.0/12\n")
    assert render(replace=asked)["edge"]["tunnel"]["client_cidr"] == "100.96.0.0/12"


def test_a_tunnel_client_range_that_is_not_a_cidr_is_refused_by_name() -> None:
    asked = ("profile: scale", "profile: scale\nedge:\n  ingest:\n    tunnel:\n      client_cidr: 100.64.0.1/10\n")
    with pytest.raises(render_dial.DialError, match=r"edge\.ingest\.tunnel\.client_cidr"):
        render(replace=asked)


def test_an_inline_allow_list_loses_its_brackets_like_every_other_dial_list() -> None:
    """Every other list the dial carries is written `[a, b]`, so an allow-list
    written that way must not reach a security group as `[10.0.0.0/8`."""
    asked = (
        "profile: scale",
        "profile: scale\n"
        "edge:\n"
        "  ingest:\n"
        "    tunnel:\n"
        "      loadBalancerSourceRanges: [10.0.0.0/8, 192.168.0.0/16]\n",
    )
    assert render(replace=asked)["edge"]["tunnel"]["source_ranges"] == ["10.0.0.0/8", "192.168.0.0/16"]


def test_an_empty_inline_allow_list_is_no_allow_list_rather_than_one_bogus_entry() -> None:
    """`[]` left unstripped turns "no allow-list" into an allow-list nothing
    matches, which closes the door instead of opening it."""
    asked = (
        "profile: scale",
        "profile: scale\nedge:\n  ingest:\n    tunnel:\n      loadBalancerSourceRanges: []\n",
    )
    assert render(replace=asked)["edge"]["tunnel"]["source_ranges"] == []


def test_a_range_that_is_not_a_cidr_is_refused_by_name() -> None:
    bad = (
        "profile: scale",
        "profile: scale\nedge:\n  ingest:\n    tunnel:\n      loadBalancerSourceRanges: 203.0.113.0\n",
    )
    with pytest.raises(render_dial.DialError, match=r"loadBalancerSourceRanges"):
        render(replace=bad)


def test_the_node_ports_default_to_what_the_culvert_chart_pins() -> None:
    ports = render()["edge"]["tunnel"]["node_ports"]
    assert ports == {"wireguard": 31820, "openvpn": 31194}


@pytest.mark.parametrize("asked", [
    ("profile: scale", "profile: scale\nedge:\n  product:\n    allowed_cidrs: 198.51.100.0/24, 203.0.113.0/24\n"),
    ("profile: scale", "profile: scale\nui:\n  allowed_cidrs: 198.51.100.0/24\n"),
], ids=["edge-spelling", "deprecated-ui-spelling"])
def test_expected_fail_the_gateway_allow_list_never_reaches_the_root(asked: tuple[str, str]) -> None:
    """An allow list on the cluster secret beats the deploy repo at every sync, so it goes there by hand."""
    variables = render(replace=asked)
    assert "edge_allowed_cidrs" not in variables
    assert "edge_self_ranges" not in variables


def test_the_dials_trusted_proxies_reach_the_root() -> None:
    """A CDN in front of the gateway forwards the client address, so its range is trusted."""
    asked = (
        "profile: scale",
        "profile: scale\nedge:\n  product:\n    allowed_cidrs: 198.51.100.0/24\n"
        "    trusted_proxy_cidrs: 192.0.2.0/24\n",
    )
    variables = render(replace=asked)
    assert variables["edge_trusted_proxy_cidrs"] == ["192.0.2.0/24"]
    assert render()["edge_trusted_proxy_cidrs"] == []


def test_a_trusted_proxy_entry_with_no_prefix_is_refused_by_name() -> None:
    bad = ("profile: scale", "profile: scale\nedge:\n  product:\n    trusted_proxy_cidrs: 192.0.2.1\n")
    with pytest.raises(render_dial.DialError, match=r"edge\.product\.trusted_proxy_cidrs"):
        render(replace=bad)


@pytest.mark.parametrize("trusted", ["0.0.0.0/0", "::/0", "192.0.2.0/25, ::/0", "192.0.2.0/25, 0.0.0.0/0"])
def test_a_trusted_proxy_range_of_every_address_is_refused(trusted: str) -> None:
    """Every peer is then trusted, so the fence reads a header the client writes."""
    bad = (
        "profile: scale",
        f"profile: scale\nedge:\n  product:\n    allowed_cidrs: 198.51.100.0/24\n    trusted_proxy_cidrs: {trusted}\n",
    )
    with pytest.raises(render_dial.DialError, match=r"trusts every address"):
        render(replace=bad)


def test_a_gateway_allow_list_entry_with_no_prefix_is_refused_by_name() -> None:
    """The gateway refuses a bare address, so the dial refuses it before it is pasted."""
    parsed = render_dial._parse_yaml_subset("edge:\n  product:\n    allowed_cidrs: 198.51.100.10\n")
    with pytest.raises(render_dial.DialError, match=r"edge\.product\.allowed_cidrs"):
        render_dial._edge_cidrs(parsed, ("edge", "product", "allowed_cidrs"))


def test_a_nonsense_address_mode_is_refused_by_name() -> None:
    bad = (
        "profile: scale",
        "profile: scale\nedge:\n  ingest:\n    tunnel:\n      address:\n        mode: elastic\n",
    )
    with pytest.raises(render_dial.DialError, match=r"address\.mode"):
        render(replace=bad)


def test_the_registry_cache_is_on_with_the_credential_free_upstreams_when_the_dial_says_nothing() -> None:
    cache = render()["registry_cache"]
    assert cache["enabled"] is True
    assert cache["namespace"] == ""
    assert cache["expiry_days"] == render_dial.REGISTRY_CACHE_EXPIRY_DAYS
    assert sorted(cache["upstreams"]) == ["ecr-public", "k8s", "quay"]
    assert not any(upstream["requires_credential"] for upstream in cache["upstreams"].values())
    assert all(upstream["credential_arn"] == "" for upstream in cache["upstreams"].values())


def test_docker_hub_and_ghcr_are_cached_only_where_the_dial_names_a_credential() -> None:
    """ECR refuses those two without one, so a dial that names none pulls them directly."""
    block = ("profile: scale", f"profile: scale\nregistry_cache:\n  credentials:\n    ghcr: {GHCR_CREDENTIAL}\n")
    upstreams = render(replace=block)["registry_cache"]["upstreams"]
    assert sorted(upstreams) == ["ecr-public", "ghcr", "k8s", "quay"]
    assert upstreams["ghcr"] == {
        "upstream_url": "ghcr.io",
        "host": "ghcr.io",
        "requires_credential": True,
        "credential_arn": GHCR_CREDENTIAL,
    }


def test_a_blank_credential_caches_nothing_more() -> None:
    block = ("profile: scale", "profile: scale\nregistry_cache:\n  credentials:\n    docker-hub: \"\"\n")
    assert sorted(render(replace=block)["registry_cache"]["upstreams"]) == ["ecr-public", "k8s", "quay"]


def test_the_dials_registry_cache_reaches_the_root() -> None:
    block = (
        "profile: scale",
        "profile: scale\nregistry_cache:\n  enabled: \"true\"\n  namespace: dfe-blue\n"
        "  expiry_days: 30\n",
    )
    cache = render(replace=block)["registry_cache"]
    assert cache["enabled"] is True
    assert cache["namespace"] == "dfe-blue"
    assert cache["expiry_days"] == 30


def test_a_dial_that_turns_the_registry_cache_off_renders_it_off_with_its_upstreams() -> None:
    block = ("profile: scale", "profile: scale\nregistry_cache:\n  enabled: \"false\"\n")
    cache = render(replace=block)["registry_cache"]
    assert cache["enabled"] is False
    assert sorted(cache["upstreams"]) == ["ecr-public", "k8s", "quay"]


def test_the_example_dial_leaves_the_registry_cache_on() -> None:
    text = (REPO_ROOT / "deployment.example.yaml").read_text(encoding="utf-8")
    example = parse_dial(text, source="deployment.example.yaml")
    assert example["registry_cache"]["enabled"] == "true"


def test_a_registry_cache_flag_that_is_not_a_boolean_is_refused_by_name() -> None:
    block = ("profile: scale", "profile: scale\nregistry_cache:\n  enabled: \"yes\"\n")
    with pytest.raises(render_dial.DialError, match=r"registry_cache\.enabled"):
        render(replace=block)


def test_a_credential_for_an_upstream_that_takes_none_is_refused_by_name() -> None:
    """quay.io needs no credential, and a name the cache does not know is a typo."""
    for name in ("quay", "gitlab"):
        block = ("profile: scale", f"profile: scale\nregistry_cache:\n  credentials:\n    {name}: arn:aws:example\n")
        with pytest.raises(render_dial.DialError, match=rf"registry_cache\.credentials names {name}"):
            render(replace=block)


def test_a_cloud_other_than_aws_renders_no_registry_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ECR is the aws body; another cloud's root declares no such variable."""
    (tmp_path / "gcp").mkdir()
    monkeypatch.setattr(render_dial, "TOFU_ROOTS", tmp_path)
    monkeypatch.setattr(render_dial, "_kubernetes_version", lambda _dial, _cloud: "1.36")
    _, variables = render_dial._tofu_vars(dial(replace=("cloud: aws", "cloud: gcp")))
    assert "registry_cache" not in variables
