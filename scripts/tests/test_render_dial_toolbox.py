#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_render_dial_toolbox.py
#  Purpose:      Guard the dial's toolbox: block -- render_dial.py never invents
#                a tool version, defaults match terraform/modules/toolbox/aws's
#                own, and a versions.yaml with no toolbox: stage degrades to an
#                empty map rather than crashing the render.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for render_dial.py's toolbox.* tfvars block.

    python3 -m pytest scripts/tests/test_render_dial_toolbox.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import render_dial  # noqa: E402
from yaml_subset import parse as parse_dial  # noqa: E402

# The same shape test_render_dial_tofu.py's fixture uses, trimmed to what
# _tofu_vars needs to reach _toolbox without a DialError elsewhere. RFC 5737
# documentation addresses, never a real deployment.
BASE_DIAL = """
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
  public: "false"
  allowed_cidrs: ""

dns:
  private_zone: dfe-example.internal
  public_zone: ""

kafka:
  provider: strimzi

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
  repo_url: https://github.com/example/dfe-deploy.git
  target_revision: main

endpoints:
  clickhouse_host: ""
  kafka_bootstrap: ""
  otel_endpoint: ""
"""

_TOOLBOX_BLOCK = """
toolbox:
  enabled: "true"
  aws:
    instance_type: t4g.small
    operator_role_arn: arn:aws:iam::000000000000:role/dfe-toolbox-operator
  ttl_minutes: 45
  session:
    idle_timeout_minutes: 10
    max_duration_minutes: 120
  session_log_retention_days: 30
"""

# A complete toolbox: stage plus the services: keys _toolbox_tool_versions
# also reads -- exercises the full assembly independent of whatever the real
# versions.yaml happens to carry right now.
_VERSIONS_WITH_TOOLBOX = """
current: "9.9.9"
stacks:
  9.9.9:
    toolbox:
      dfe-toolbox: "v1.0.0"
      kubectl: "v1.36.0"
      helm: "v4.2.4"
      argocd-cli: "v3.5.2"
      tofu: "1.11.0"
      yq: "v4.44.3"
      aws-cli: "2.19.0"
      aws-session-manager-plugin: "1.2.707.0"
      gcloud: "583.0.0"
      az-cli: "2.90.0"
    services:
      clickhouse-version: "26.3.17.56"
      postgresql: "17"
"""


def dial(text: str = BASE_DIAL) -> dict[str, object]:
    return parse_dial(text, source="test-dial")


def render(text: str = BASE_DIAL) -> dict[str, object]:
    _, variables = render_dial._tofu_vars(dial(text))  # type: ignore[arg-type]
    return variables


def test_toolbox_defaults_to_disabled_with_no_toolbox_block() -> None:
    toolbox = render()["toolbox"]
    assert toolbox["enabled"] is False
    assert toolbox["aws"]["instance_type"] == "t4g.small"
    assert toolbox["aws"]["operator_role_arn"] == ""
    assert toolbox["ttl_minutes"] == 60
    assert toolbox["session"] == {"idle_timeout_minutes": 15, "max_duration_minutes": 240}
    assert toolbox["session_log_retention_days"] == 90


def test_toolbox_block_values_thread_through() -> None:
    toolbox = render(BASE_DIAL + _TOOLBOX_BLOCK)["toolbox"]
    assert toolbox["enabled"] is True
    assert toolbox["aws"]["instance_type"] == "t4g.small"
    assert toolbox["aws"]["operator_role_arn"] == "arn:aws:iam::000000000000:role/dfe-toolbox-operator"
    assert toolbox["ttl_minutes"] == 45
    assert toolbox["session"] == {"idle_timeout_minutes": 10, "max_duration_minutes": 120}
    assert toolbox["session_log_retention_days"] == 30


def test_tool_versions_is_never_read_from_the_dial() -> None:
    """Even a dial that tried to name one could not -- there is no such field."""
    with_a_bogus_pin = BASE_DIAL + _TOOLBOX_BLOCK + "  tool_versions:\n    kubectl: 1.0.0\n"
    # yaml_subset has no list/inline-map syntax, so this parses as an ordinary
    # nested map like any other -- proving _toolbox() simply never looks at it.
    toolbox = render(with_a_bogus_pin)["toolbox"]
    assert "1.0.0" not in toolbox["tool_versions"].values()


def test_tool_versions_assembles_against_the_real_versions_yaml() -> None:
    """versions.yaml now carries a real toolbox: stage (docker/dfe-toolbox's own
    pins) -- prove every key this module needs is present and non-empty,
    without pinning to today's exact values (which move under their own
    7-day-cooldown discipline)."""
    tool_versions = render()["toolbox"]["tool_versions"]
    for tool in (
        "kubectl", "helm", "argocd-cli", "tofu", "yq", "aws-cli",
        "aws-session-manager-plugin", "clickhouse-client", "psql",
    ):
        assert tool in tool_versions, f"{tool} missing from tool_versions"
        assert tool_versions[tool], f"{tool} is present but empty"
    # jq, kcat and openssl carry no versions.yaml pin at all (deliberately --
    # see versions.yaml's own toolbox: stage comment).
    assert "jq" not in tool_versions
    assert "kcat" not in tool_versions
    assert "openssl" not in tool_versions


def test_clickhouse_client_and_psql_track_the_services_pins_not_their_own() -> None:
    """Reading services.clickhouse-version / services.postgresql directly is
    what keeps the debugging client from ever skewing off the server it
    debugs -- proven against the real versions.yaml rather than a fixture, so
    a future edit to either pin cannot silently break this coupling."""
    real = render()["toolbox"]["tool_versions"]
    versions_yaml = (REPO_ROOT / "versions.yaml").read_text(encoding="utf-8")
    current = render_dial._scalar(
        render_dial._parse_yaml_subset(versions_yaml, source="versions.yaml"), ("current",)
    )
    services = render_dial._parse_yaml_subset(versions_yaml, source="versions.yaml")["stacks"][current]["services"]
    assert real["clickhouse-client"] == render_dial._scalar(services, ("clickhouse-version",))
    assert real["psql"] == render_dial._scalar(services, ("postgresql",))


def test_tool_versions_assembles_fully_from_a_fixture_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    versions = tmp_path / "versions.yaml"
    versions.write_text(_VERSIONS_WITH_TOOLBOX, encoding="utf-8")
    monkeypatch.setattr(render_dial, "VERSIONS_FILE", versions)

    tool_versions = render()["toolbox"]["tool_versions"]
    assert tool_versions == {
        "kubectl": "v1.36.0",
        "helm": "v4.2.4",
        "argocd-cli": "v3.5.2",
        "tofu": "1.11.0",
        "yq": "v4.44.3",
        "aws-cli": "2.19.0",
        "aws-session-manager-plugin": "1.2.707.0",
        "clickhouse-client": "26.3.17.56",
        "psql": "17",
    }


def test_tool_versions_is_empty_with_no_toolbox_stage_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    versions = tmp_path / "versions.yaml"
    versions.write_text('current: "9.9.9"\nstacks:\n  9.9.9:\n    schema: 1\n', encoding="utf-8")
    monkeypatch.setattr(render_dial, "VERSIONS_FILE", versions)
    assert render()["toolbox"]["tool_versions"] == {}


def test_tool_versions_is_empty_with_no_versions_file_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(render_dial, "VERSIONS_FILE", tmp_path / "does-not-exist.yaml")
    assert render()["toolbox"]["tool_versions"] == {}


def test_toolbox_key_is_declared_by_the_aws_root() -> None:
    """The root's variables.tf must declare toolbox, or this render's own key
    would be a prompt on an unattended plan (variables.tf carries no default
    for it)."""
    variables_tf = (REPO_ROOT / "terraform" / "environments" / "aws" / "variables.tf").read_text(
        encoding="utf-8"
    )
    assert 'variable "toolbox"' in variables_tf
