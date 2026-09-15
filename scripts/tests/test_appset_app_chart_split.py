#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_appset_app_chart_split.py
#  Purpose:      Pin the Application NAME an appset generates apart from the
#                chart DIRECTORY it renders, so moving a chart adopts its live
#                objects instead of deleting and recreating them.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""`app` is the Application name; `chart` is only where the chart lives.

Nothing under argocd/ sets preserveResourcesOnDeletion, so an ApplicationSet
that stops generating an Application DELETES that Application's resources. Argo's
tracking id embeds the Application NAME, so a chart directory that moves is
adoption-safe only while the name it generates stays put -- which is what these
pin. A rename that reaches the name shows up here as a changed NAMES entry, and
a directory move shows up as a changed path and nothing else.

    python3 scripts/tests/test_appset_app_chart_split.py

No test runner, matching the other checks here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APPSETS = REPO_ROOT / "argocd" / "appsets"

# The appsets that deploy a first-party dfe-infra chart by path, keyed on `app`.
# layer1-addons and layer-scale name an UPSTREAM chart instead, so their `chart`
# key is the chart's own published name and no split applies.
BY_PATH = ("layer2-platform.yaml", "layer2-edge.yaml")

# The Application names these appsets generate, before the cluster suffix. This
# list is the outage guard: an entry that changes deletes and recreates every
# object that Application owns, so a move must leave it untouched.
NAMES = {
    "dfe-toolbox",
    "envoy-gateway-config",
    "karpenter-pools",
    "network-policies",
}

# Where each of those names renders from. Unlike NAMES, a path here is expected
# to change when a chart moves.
PATHS = {
    "dfe-toolbox": "helm/charts/dfe-toolbox",
    "envoy-gateway-config": "helm/charts/envoy-gateway-config",
    "karpenter-pools": "helm/charts/karpenter-pools",
    "network-policies": "helm/charts/network-policies",
}

# The expression a path must use. A bare `.chart` field lookup would fail the
# render on an element that states none, because goTemplateOptions is
# missingkey=error; `index` answers nil for an absent key instead, which is the
# same read layer2-platform.yaml already does for the karpenter_pools annotation
# bootstrap.sh writes only sometimes.
CHART_EXPR = '{{ index . "chart" | default .app }}'


def present() -> list[Path]:
    return [APPSETS / name for name in BY_PATH if (APPSETS / name).is_file()]


def elements(appset: dict) -> list[dict]:
    found: list[dict] = []
    for generator in appset["spec"]["generators"]:
        for child in generator.get("matrix", {}).get("generators", []):
            found.extend(child.get("list", {}).get("elements", []))
    return found


def generated() -> dict[str, str]:
    """app name -> chart path, across every by-path appset that exists."""
    out: dict[str, str] = {}
    for path in present():
        appset = yaml.safe_load(path.read_text(encoding="utf-8"))
        template = appset["spec"]["template"]
        prefix = template["spec"]["sources"][0]["path"].replace(CHART_EXPR, "")
        for element in elements(appset):
            app = element["app"]
            out[app] = prefix + element.get("chart", app)
    return out


def test_the_application_name_comes_from_app_alone() -> None:
    for path in present():
        appset = yaml.safe_load(path.read_text(encoding="utf-8"))
        name = appset["spec"]["template"]["metadata"]["name"]
        expect(f"{path.name} names the Application from .app",
               name == "{{ .app }}-{{ .name }}", name)


def test_the_chart_path_is_read_with_index_not_a_field_lookup() -> None:
    for path in present():
        appset = yaml.safe_load(path.read_text(encoding="utf-8"))
        source = appset["spec"]["template"]["spec"]["sources"][0]["path"]
        expect(f"{path.name} defaults the chart to the app name",
               CHART_EXPR in source, source)
        expect(f"{path.name} never looks the chart key up as a field",
               "{{ .chart }}" not in source, source)


def test_every_generated_application_name_is_pinned() -> None:
    expect("the generated names are the pinned set", set(generated()) == NAMES, sorted(generated()))


def test_every_generated_chart_path_is_pinned() -> None:
    expect("the generated paths are the pinned set", generated() == PATHS, generated())


def test_an_element_stating_no_chart_falls_back_to_its_app_name() -> None:
    """network-policies carries no `chart`, so its path proves the default."""
    platform = yaml.safe_load((APPSETS / "layer2-platform.yaml").read_text(encoding="utf-8"))
    entry = next(e for e in elements(platform) if e["app"] == "network-policies")
    expect("the element states no chart", "chart" not in entry, entry)
    expect("and still renders under its own name",
           generated()["network-policies"] == "helm/charts/network-policies", generated())


def test_the_deployer_overlay_follows_the_name_not_the_directory() -> None:
    """A moved chart must not orphan the `infra/<app>.yaml` a deployer wrote."""
    for path in present():
        appset = yaml.safe_load(path.read_text(encoding="utf-8"))
        files = appset["spec"]["template"]["spec"]["sources"][0]["helm"]["valueFiles"]
        expect(f"{path.name} keys the deployer overlay on the app name",
               "$values/infra/{{ .app }}.yaml" in files, files)


def main() -> int:
    with standalone():
        test_the_application_name_comes_from_app_alone()
        test_the_chart_path_is_read_with_index_not_a_field_lookup()
        test_every_generated_application_name_is_pinned()
        test_every_generated_chart_path_is_pinned()
        test_an_element_stating_no_chart_falls_back_to_its_app_name()
        test_the_deployer_overlay_follows_the_name_not_the_directory()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
