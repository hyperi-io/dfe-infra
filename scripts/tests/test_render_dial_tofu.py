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
    }
    assert set(render()) == declared - {"node_pools", "resolved_shapes"} - guarded_run_only


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


def test_msk_autoscaling_is_omitted_when_the_dial_sets_nothing() -> None:
    """Every field already has a root-level default (variables.tf); a dial that
    names no autoscaling block should not restate them -- the root's own
    `optional(object({...}), {})` takes an empty object exactly this way."""
    assert render()["kafka"]["msk"]["autoscaling"] == {}


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
            "  landing_topics:",
        )
    )["kafka"]["msk"]["autoscaling"]
    assert autoscaling == {
        "enabled": False,
        "max_brokers": 9,
        "step": 3,
        "per_broker_capacity_mb_s": 75,
        "headroom": 1.5,
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


def test_the_kafka_seed_is_keyed_by_the_provider() -> None:
    """Its last segment is the key the kafka chart reads the credential back by."""
    assert "kafka/msk" in render()["seeds"]
    assert "kafka/strimzi" in render(replace=("provider: msk", "provider: strimzi"))["seeds"]
    assert "kafka/confluent-cloud" in render(
        replace=("provider: msk", "provider: confluent-cloud")
    )["seeds"]
    assert "kafka/redpanda-cloud" in render(
        replace=("provider: msk", "provider: redpanda-cloud")
    )["seeds"]


def test_no_seed_carries_a_value() -> None:
    """An empty value is generated where it lands and never travels through here."""
    for fields in render()["seeds"].values():
        assert all(value == "" for value in fields.values())


def test_a_missing_broker_field_is_named() -> None:
    with pytest.raises(render_dial.DialError, match=r"kafka\.msk\.broker_version"):
        render(drop="broker_version: 4.2.x.kraft")


def test_the_dial_supplies_the_telemetry_sink_and_retention() -> None:
    """The fixture sets cloudwatch explicitly, to prove non-default values thread through."""
    assert render()["telemetry"] == {"sink": "cloudwatch", "retention_days": 5}


def test_telemetry_defaults_to_otel_with_a_two_day_retention() -> None:
    """DFE's monitoring goes to its own OTel feed, never CloudWatch, by default."""
    without_telemetry = "\n".join(
        line
        for line in DIAL.splitlines()
        if line.strip() not in ("telemetry:", "aws:", "sink: cloudwatch", "retention_days: 5")
    )
    _, variables = render_dial._tofu_vars(parse_dial(without_telemetry, source="test-dial"))
    assert variables["telemetry"] == {"sink": "otel", "retention_days": 2}


def test_telemetry_retention_defaults_to_seven_days_under_cloudwatch() -> None:
    """cloudwatch is the opt-in AWS-native path, and it defaults short too."""
    cloudwatch_no_retention = render(drop="retention_days: 5")
    assert cloudwatch_no_retention["telemetry"] == {"sink": "cloudwatch", "retention_days": 7}


def test_an_unknown_telemetry_sink_is_refused() -> None:
    with pytest.raises(render_dial.DialError, match=r"telemetry\.aws\.sink"):
        render(replace=("sink: cloudwatch", "sink: splunk"))


def test_no_pull_secret_input_reaches_tofu() -> None:
    """The pull secret's inputs go from the deployer's environment straight to
    bootstrap.sh, so a registry credential never lands in tofu state."""
    assert [key for key in render() if key.startswith("registry")] == []


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


def test_the_forwarders_default_type_is_the_catalogues_small_arm_shape() -> None:
    """Sized by bandwidth, never by anything else -- the forwarder moves every
    tunnel byte and does nothing else."""
    assert render_dial.TUNNEL_FORWARDER_TYPE == "t4g.small"
    assert render()["edge"]["tunnel"]["address"]["instance_type"] == "t4g.small"


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
        "        instance_type: m8g.large\n"
        "        zone: us-west-2c\n",
    )
    tunnel = render(replace=asked)["edge"]["tunnel"]
    assert tunnel["address"] == {"mode": "forwarder", "instance_type": "m8g.large", "zone": "us-west-2c"}
    assert tunnel["openvpn"] is False
    assert tunnel["source_ranges"] == ["203.0.113.0/24", "198.51.100.0/24"]


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


def test_a_nonsense_address_mode_is_refused_by_name() -> None:
    bad = (
        "profile: scale",
        "profile: scale\nedge:\n  ingest:\n    tunnel:\n      address:\n        mode: elastic\n",
    )
    with pytest.raises(render_dial.DialError, match=r"address\.mode"):
        render(replace=bad)
