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
"""The published thin charts, per component, profile, cloud and scenario.

    env DFE_WEAVE_PUBLISHED=1 python3 -m pytest scripts/tests/test_weave_published.py -q

The goldens and every live render assemble a thin chart from
fixtures/contracts/<service>.json through the scalo-service release _weave pins.
Argo deploys the chart the registry serves, pulled by the digest versions.yaml
chart-digests pins. Each cell here renders that chart through the same appset,
value files, cluster facts and scenario (_gate.published_render) and holds it
equal to the cell's golden, object for object and leaf for leaf, so every check
over the goldens holds for the published chart too. The committed contract must
also be the chart's files/contract.json, byte for byte.

A component whose chart-digests entry is not a digest yet is skipped. The pulls need
the registry, so the registry cases run only when DFE_WEAVE_PUBLISHED is set: Helm
Lint's published-charts job sets it whenever a change touches what they read. No pull
carries a credential.
"""

import json
import os
from pathlib import Path

import pytest

from _gate import CELLS, COMPONENTS, golden, object_id, published_render, render_differences
from _weave import _anonymous, contract, published_chart

NEEDS_REGISTRY = pytest.mark.skipif(
    not os.environ.get("DFE_WEAVE_PUBLISHED"),
    reason="pulls the published thin charts; set DFE_WEAVE_PUBLISHED=1 to run",
)


@NEEDS_REGISTRY
@pytest.mark.parametrize("service", COMPONENTS)
def test_the_committed_contract_is_the_published_one(service: str) -> None:
    published = published_chart(service) / "files" / "contract.json"
    assert contract(service).read_bytes() == published.read_bytes()


@NEEDS_REGISTRY
@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), CELLS)
def test_the_published_chart_renders_as_its_golden(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    published = published_render(service, profile, cloud, scenario)
    assert render_differences(golden(service, profile, cloud, scenario), published) == []


def test_a_registry_pull_is_handed_no_credential() -> None:
    """helm falls back to Docker's config when its own is empty, so both are empty."""
    env = _anonymous()[1]
    assert json.loads(Path(env["HELM_REGISTRY_CONFIG"]).read_text(encoding="utf-8")) == {}
    assert not (Path(env["DOCKER_CONFIG"]) / "config.json").exists()


# ---------------------------------------------------------------- expected fails


def test_a_moved_leaf_and_an_extra_object_are_caught() -> None:
    """The shape of a published chart running another image beside an object no golden holds."""
    moved = golden("dfe-ui", "scale", "aws")
    main = next(d for d in moved if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    before = main["containers"][0]["image"]
    main["containers"][0]["image"] = "registry.example.com/elsewhere/dfe-ui:v0"
    stray = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "stray"}}
    assert render_differences(golden("dfe-ui", "scale", "aws"), [*moved, stray]) == [
        "only right: ConfigMap/stray",
        "Deployment/dfe-ui spec.template.spec.containers[main].image: "
        f'"{before}" -> "registry.example.com/elsewhere/dfe-ui:v0"',
    ]


def test_an_object_the_published_chart_drops_is_caught() -> None:
    docs = golden("dfe-ui", "slim", "local")
    dropped = [d for d in docs if object_id(d) != "ServiceAccount/dfe-ui"]
    assert render_differences(docs, dropped) == ["only left: ServiceAccount/dfe-ui"]
