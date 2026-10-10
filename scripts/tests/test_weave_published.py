#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_weave_published.py
#  Purpose:      Prove each thin chart the registry serves renders as the chart
#                the gate assembles from its committed contract, so the gate's
#                verdicts hold for the chart Argo pulls.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The chart-switch gate on the published charts, per component, profile, cloud and scenario.

    env DFE_WEAVE_PUBLISHED=1 python3 -m pytest scripts/tests/test_weave_published.py -q

The gate renders a thin chart it assembles from fixtures/contracts/<service>.json
through the scalo-service release _weave pins. Argo deploys the chart the registry
serves, pulled by the digest versions.yaml chart-digests pins. Each cell here renders
that chart through the same appset, value files, cluster facts and scenario
(_gate.published_render) and holds it equal to the assembled render, object for
object and leaf for leaf, so every check the gate makes holds for the published
chart too. The committed contract must also be the chart's files/contract.json,
byte for byte.

A component whose chart-digests entry is not a digest yet is skipped. The pulls need
the registry, and a private chart needs the credentials helm already holds, so the
registry cases run only when DFE_WEAVE_PUBLISHED is set.
"""

import os

import pytest

from _gate import (
    CLOUDS,
    COMPONENTS,
    DEFAULT,
    PROFILES,
    SCENARIOS,
    cell,
    object_id,
    published_render,
    render_differences,
)
from _weave import contract, published_chart

NEEDS_REGISTRY = pytest.mark.skipif(
    not os.environ.get("DFE_WEAVE_PUBLISHED"),
    reason="pulls the published thin charts; set DFE_WEAVE_PUBLISHED=1 to run",
)

CELLS = [(s, p, k, DEFAULT) for s in COMPONENTS for p in PROFILES for k in CLOUDS] + [
    (s, "slim", "local", scenario) for s in COMPONENTS for scenario in SCENARIOS
]


@NEEDS_REGISTRY
@pytest.mark.parametrize("service", COMPONENTS)
def test_the_committed_contract_is_the_published_one(service: str) -> None:
    published = published_chart(service) / "files" / "contract.json"
    assert contract(service).read_bytes() == published.read_bytes()


@NEEDS_REGISTRY
@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), CELLS)
def test_the_published_chart_renders_as_the_gate_assembles_it(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    assembled = cell(service, profile, cloud, scenario).new
    published = published_render(service, profile, cloud, scenario)
    assert render_differences(assembled, published) == []


# ---------------------------------------------------------------- expected fails


def test_a_moved_leaf_and_an_extra_object_are_caught() -> None:
    """The shape of a published chart running another image beside an object the gate never saw."""
    c = cell("dfe-ui", "scale", "aws").mutable()
    main = next(d for d in c.new if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    before = main["containers"][0]["image"]
    main["containers"][0]["image"] = "registry.example.com/elsewhere/dfe-ui:v0"
    stray = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "stray"}}
    assert render_differences(cell("dfe-ui", "scale", "aws").new, [*c.new, stray]) == [
        "only right: ConfigMap/stray",
        "Deployment/dfe-ui spec.template.spec.containers[main].image: "
        f'"{before}" -> "registry.example.com/elsewhere/dfe-ui:v0"',
    ]


def test_an_object_the_published_chart_drops_is_caught() -> None:
    new = cell("dfe-ui", "slim", "local").new
    dropped = [d for d in new if object_id(d) != "ServiceAccount/dfe-ui"]
    assert render_differences(new, dropped) == ["only left: ServiceAccount/dfe-ui"]
