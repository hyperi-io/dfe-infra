#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_weave_exposure.py
#  Purpose:      Prove no switched component's thin chart opens a door to
#                outside the cluster that the exposure table does not list.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The exposure gate of the chart switch, per component, profile and cloud.

    python3 -m pytest scripts/tests/test_weave_exposure.py -q

A Service of type LoadBalancer or NodePort is reachable from outside the cluster.
Only dfe-receiver takes untrusted input from there (docs/THREAT-MODEL.md), so its
row of EXPOSURE is the only one that may hold any. A contract can mark a port
public (dfe-hyperdx marks 8080), and scalo-service renders its load balancer only
where `publicService.enabled` is set, so this holds the integration values to it.

Render (b) needs the scalo-service library (_weave.library).
"""

import tempfile
from pathlib import Path

import pytest
import yaml

from _gate import COMPONENTS, MATRIX, cell, exposed
from _weave import render_app

# Each component's Services reachable from outside, per cloud: name -> type and ports.
EXPOSURE: dict[str, dict[str, dict]] = {
    "dfe-ui": {"local": {}, "aws": {}},
    "hyperdx": {"local": {}, "aws": {}},
    "dfe-archiver": {"local": {}, "aws": {}},
    "dfe-transform-vrl": {"local": {}, "aws": {}},
    "dfe-transform-vector": {"local": {}, "aws": {}},
    "dfe-transform-elastic": {"local": {}, "aws": {}},
}
INTERNET_FACING = {"dfe-receiver"}


@pytest.mark.parametrize(("service", "profile", "cloud"), MATRIX)
def test_the_thin_chart_exposes_only_what_the_table_lists(
    service: str, profile: str, cloud: str
) -> None:
    c = cell(service, profile, cloud)
    assert exposed(c.new) == EXPOSURE[service][cloud]


def test_every_gated_component_has_an_exposure_row() -> None:
    assert set(COMPONENTS) <= set(EXPOSURE)


@pytest.mark.parametrize("service", sorted(set(EXPOSURE) - INTERNET_FACING))
def test_only_the_receiver_is_exposed(service: str) -> None:
    assert all(rows == {} for rows in EXPOSURE[service].values())


# ---------------------------------------------------------------- expected fails


def test_a_public_service_switched_on_is_caught() -> None:
    """dfe-hyperdx's contract marks 8080 public; one overlay line would put it on a load balancer."""
    with tempfile.TemporaryDirectory(prefix="dfe-exposure-") as tmp:
        overlay = Path(tmp) / "values" / "hyperdx-default-values.yaml"
        overlay.parent.mkdir()
        body = {
            "deploy": {"service": "hyperdx", "instance": "default"},
            "publicService": {"enabled": True},
        }
        overlay.write_text(yaml.safe_dump(body), encoding="utf-8", newline="\n")
        docs = render_app("hyperdx", "scale", "aws", "new", deploy_repo=Path(tmp))
    assert exposed(docs) == {
        "Service/dfe-hyperdx-public": {"type": "LoadBalancer", "ports": ["8080/TCP"]}
    }
    assert exposed(docs) != EXPOSURE["hyperdx"]["aws"]
