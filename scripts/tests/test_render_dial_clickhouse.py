#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_render_dial_clickhouse.py
#  Purpose:      Guard the dial's clickhouse block: what it renders for the aws
#                root, and the shapes it refuses before a plan runs.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the dial's `clickhouse:` block under `render_dial.py --tofu`.

    python3 -m pytest scripts/tests/test_render_dial_clickhouse.py -q

The external host travels on endpoints.clickhouse_host, the one key the root
already reads for it, and the password never enters the dial: the root takes it
from TF_VAR_clickhouse_external_password. Every host here is under the reserved
example domain.
"""

import sys
from pathlib import Path

import pytest
from test_render_dial_tofu import DIAL

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import render_dial  # noqa: E402
from yaml_subset import parse as parse_dial  # noqa: E402

EXAMPLE = REPO_ROOT / "deployment.example.yaml"
HOST = "ch.example.com"
IN_CLUSTER_HOST = "dfe-clickhouse.clickhouse.svc.cluster.local"


def render(block: str = "", *, endpoint_host: str | None = None) -> dict[str, object]:
    """The aws root's variables for the fixture dial with `block` appended."""
    text = DIAL
    if endpoint_host is not None:
        text = text.replace(f"clickhouse_host: {IN_CLUSTER_HOST}", f'clickhouse_host: "{endpoint_host}"')
    _, variables = render_dial._tofu_vars(parse_dial(text + block, source="test-dial"))
    return variables


def external(**fields: str) -> str:
    """A `clickhouse:` block in external mode, every field the dial writes as a string."""
    body = {"mode": "external", "host": HOST, **fields}
    return "\nclickhouse:\n" + "".join(f'  {key}: "{value}"\n' for key, value in body.items())


def test_a_dial_with_no_block_keeps_clickhouse_in_the_cluster() -> None:
    variables = render()
    assert variables["clickhouse"] == {"mode": "in_cluster"}
    assert variables["endpoints"]["clickhouse_host"] == IN_CLUSTER_HOST
    assert "clickhouse/external" not in variables["seeds"]


def test_external_takes_clickhouse_clouds_ports_secure_and_the_default_user() -> None:
    variables = render(external(), endpoint_host="")
    assert variables["clickhouse"] == {
        "mode": "external",
        "port": 8443,
        "native_port": 9440,
        "secure": True,
        "user": "default",
    }


def test_the_external_host_travels_on_the_endpoint_the_root_already_reads() -> None:
    assert render(external(), endpoint_host="")["endpoints"]["clickhouse_host"] == HOST


def test_the_same_host_on_both_keys_is_one_fact() -> None:
    assert render(external(), endpoint_host=HOST)["endpoints"]["clickhouse_host"] == HOST


def test_expected_fail_two_different_hosts_are_refused() -> None:
    with pytest.raises(render_dial.DialError, match=r"endpoints\.clickhouse_host"):
        render(external())


def test_every_field_the_dial_sets_reaches_the_root() -> None:
    block = external(port="9443", native_port="9441", secure="false", user="dfe_writer")
    assert render(block, endpoint_host="")["clickhouse"] == {
        "mode": "external",
        "port": 9443,
        "native_port": 9441,
        "secure": False,
        "user": "dfe_writer",
    }


def test_external_seeds_an_empty_password_the_root_fills_from_its_own_variable() -> None:
    seeds = render(external(), endpoint_host="")["seeds"]
    assert seeds["clickhouse/external"] == {"password": ""}
    assert "kafka/msk" in seeds


def test_expected_fail_external_with_no_host_is_refused() -> None:
    with pytest.raises(render_dial.DialError, match=r"clickhouse\.host"):
        render('\nclickhouse:\n  mode: "external"\n', endpoint_host="")


@pytest.mark.parametrize("host", ["https://ch.example.com", "ch.example.com:8443", "ch.example.com/db", "ch example.com"])
def test_expected_fail_a_host_that_is_not_a_bare_name_is_refused(host: str) -> None:
    with pytest.raises(render_dial.DialError, match=r"clickhouse\.host"):
        render(external(host=host), endpoint_host="")


def test_expected_fail_an_unknown_mode_is_refused() -> None:
    with pytest.raises(render_dial.DialError, match=r"clickhouse\.mode"):
        render('\nclickhouse:\n  mode: "cloud"\n')


@pytest.mark.parametrize(("field", "value"), [("port", "https"), ("port", "0"), ("native_port", "70000")])
def test_expected_fail_a_port_outside_1_to_65535_is_refused(field: str, value: str) -> None:
    with pytest.raises(render_dial.DialError, match=rf"clickhouse\.{field}"):
        render(external(**{field: value}), endpoint_host="")


def test_expected_fail_secure_must_be_true_or_false() -> None:
    with pytest.raises(render_dial.DialError, match=r"clickhouse\.secure"):
        render(external(secure="yes"), endpoint_host="")


@pytest.mark.parametrize("field", ["host", "port", "native_port", "secure", "user"])
def test_expected_fail_an_external_field_on_an_in_cluster_server_is_refused(field: str) -> None:
    """Read only in external mode, so set beside in_cluster it would do nothing."""
    with pytest.raises(render_dial.DialError, match=rf"clickhouse\.{field}"):
        render(f'\nclickhouse:\n  mode: "in_cluster"\n  {field}: "9440"\n')


def test_expected_fail_a_password_in_the_dial_is_refused() -> None:
    with pytest.raises(render_dial.DialError, match="TF_VAR_clickhouse_external_password"):
        render(external() + '  password: "placeholder"\n', endpoint_host="")


def test_the_example_dial_documents_the_block_in_cluster() -> None:
    example = parse_dial(EXAMPLE.read_text(encoding="utf-8"), source=str(EXAMPLE))
    assert render_dial._clickhouse(example) == {"mode": "in_cluster"}
