#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_appset_app_chart_split.py
#  Purpose:      Pin the Application NAME an appset generates apart from the
#                chart DIRECTORY it renders, and pin the edge module's gate, so
#                moving a chart adopts its live objects instead of deleting them.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""`app` is the Application name; `chart` is only where the chart lives.

Nothing under argocd/ sets preserveResourcesOnDeletion, so an ApplicationSet
that stops generating an Application DELETES that Application's resources.
Argo's tracking id embeds the Application NAME, so a chart directory that moves
is adoption-safe only while the name it generates stays put -- which is what
these pin. A rename shows up here as a changed NAMES entry, and a directory move
shows up as a changed path and nothing else.

The edge module's two Applications are gated on the cluster secret's own
dfe.hyperi.io/edge, and its culvert half was moved out of layer2-apps. Both
halves of that are pinned too: two appsets generating one name is a fight
between controllers, and zero generating it is a deletion.

    python3 scripts/tests/test_appset_app_chart_split.py

No test runner, matching the other checks here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from _charts import CHART_TREES, chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
APPSETS = REPO_ROOT / "argocd" / "appsets"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"

sys.path.insert(0, str(SCRIPTS))
import render_dial  # noqa: E402
from yaml_subset import parse as parse_dial  # noqa: E402

# The labels bootstrap.sh writes on every cluster secret, minus the module
# switch, which each render case below supplies.
BASE_LABELS = {
    "argocd.argoproj.io/secret-type": "cluster",
    "dfe.hyperi.io/managed": "true",
    "dfe.hyperi.io/profile": "scale",
    "dfe.hyperi.io/cloud": "aws",
    "dfe.hyperi.io/dns-provider": "route53",
    "dfe.hyperi.io/bundled-deploy-repo": "false",
}

# The appsets that deploy a first-party dfe-infra chart by path from a list
# element keyed on `app`. layer1-addons and layer-scale name an UPSTREAM chart
# instead, so their `chart` key is the chart's own published name and no split
# applies; layer2-apps and the edge module's culvert half are driven by a
# deploy-repo file rather than an element, and are pinned separately below.
BY_ELEMENT = ("layer2-platform.yaml", "layer2-edge.yaml")

# The Application names those appsets generate, before the cluster suffix. This
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
    "envoy-gateway-config": "helm/edge/gateway",
    "karpenter-pools": "helm/charts/karpenter-pools",
    "network-policies": "helm/charts/network-policies",
}

# The expression a path must use. A bare `.chart` field lookup would fail the
# render on an element that states none, because goTemplateOptions is
# missingkey=error; `index` answers nil for an absent key instead, which is the
# same read layer2-platform.yaml already does for the karpenter_pools annotation
# bootstrap.sh writes only sometimes.
CHART_EXPR = '{{ index . "chart" | default .app }}'

# The deploy-repo glob the tunnel is enabled by. The engine writes one of these
# files to turn culvert on, and exactly one appset may fan it out.
CULVERT_GLOB = "values/culvert-*-values.yaml"

EDGE_KEY = "dfe.hyperi.io/edge"


def docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def appsets(name: str) -> list[dict]:
    return [d for d in docs(APPSETS / name) if d.get("kind") == "ApplicationSet"]


def elements(appset: dict) -> list[dict]:
    found: list[dict] = []
    for generator in appset["spec"]["generators"]:
        for child in generator.get("matrix", {}).get("generators", []):
            found.extend(child.get("list", {}).get("elements", []))
    return found


def element_appsets() -> list[tuple[str, dict]]:
    """Every (file, ApplicationSet) that fans out from a list element."""
    return [
        (name, appset)
        for name in BY_ELEMENT
        for appset in appsets(name)
        if any(elements(appset))
    ]


def generated() -> dict[str, str]:
    """app name -> chart path, across every element-driven appset."""
    out: dict[str, str] = {}
    for _, appset in element_appsets():
        template = appset["spec"]["template"]
        prefix = template["spec"]["sources"][0]["path"].replace(CHART_EXPR, "")
        for element in elements(appset):
            out[element["app"]] = prefix + element.get("chart", element["app"])
    return out


def cluster_selectors(appset: dict) -> list[dict]:
    return [
        child["clusters"]["selector"]
        for generator in appset["spec"]["generators"]
        for child in generator.get("matrix", {}).get("generators", [])
        if "clusters" in child
    ]


def git_paths(appset: dict) -> list[dict]:
    return [
        entry
        for generator in appset["spec"]["generators"]
        for child in generator.get("matrix", {}).get("generators", [])
        for entry in child.get("git", {}).get("files", [])
    ]


def test_the_application_name_comes_from_app_alone() -> None:
    for name, appset in element_appsets():
        rendered = appset["spec"]["template"]["metadata"]["name"]
        expect(f"{name} names the Application from .app",
               rendered == "{{ .app }}-{{ .name }}", rendered)


def test_the_chart_path_is_read_with_index_not_a_field_lookup() -> None:
    for name, appset in element_appsets():
        source = appset["spec"]["template"]["spec"]["sources"][0]["path"]
        expect(f"{name} defaults the chart to the app name", CHART_EXPR in source, source)
        expect(f"{name} never looks the chart key up as a field",
               "{{ .chart }}" not in source, source)


def test_every_generated_application_name_is_pinned() -> None:
    expect("the generated names are the pinned set", set(generated()) == NAMES, sorted(generated()))


def test_every_generated_chart_path_is_pinned() -> None:
    expect("the generated paths are the pinned set", generated() == PATHS, generated())


def test_an_element_stating_no_chart_falls_back_to_its_app_name() -> None:
    """network-policies carries no `chart`, so its path proves the default."""
    platform = appsets("layer2-platform.yaml")[0]
    entry = next(e for e in elements(platform) if e["app"] == "network-policies")
    expect("the element states no chart", "chart" not in entry, entry)
    expect("and still renders under its own name",
           generated()["network-policies"] == "helm/charts/network-policies", generated())


def test_the_deployer_overlay_follows_the_name_not_the_directory() -> None:
    """A moved chart must not orphan the `infra/<app>.yaml` a deployer wrote."""
    for name, appset in element_appsets():
        files = appset["spec"]["template"]["spec"]["sources"][0]["helm"]["valueFiles"]
        expect(f"{name} keys the deployer overlay on the app name",
               "$values/infra/{{ .app }}.yaml" in files, files)


def test_every_edge_applicationset_is_gated_on_the_module_switch() -> None:
    """Absent renders nothing: In alone would match a secret without the key."""
    edge = appsets("layer2-edge.yaml")
    expect("the edge file carries both ApplicationSets", len(edge) == 2, len(edge))
    for appset in edge:
        selectors = cluster_selectors(appset)
        expect(f"{appset['metadata']['name']} selects a cluster", selectors != [], selectors)
        for selector in selectors:
            operators = {
                e["operator"] for e in selector.get("matchExpressions", []) if e["key"] == EDGE_KEY
            }
            expect(f"{appset['metadata']['name']} requires the key to exist",
                   "Exists" in operators, operators)
            expect(f"{appset['metadata']['name']} requires it to be true",
                   any(e["key"] == EDGE_KEY and e["operator"] == "In" and e["values"] == ["true"]
                       for e in selector.get("matchExpressions", [])),
                   selector.get("matchExpressions"))


def test_every_first_party_chart_is_reachable_through_the_shared_resolver() -> None:
    """A chart tree left out of _charts is a silent hole rather than a failure:
    the repo-wide sweeps that run over CHART_TREES stop covering it, and the
    name-keyed lookups resolve to a path that does not exist."""
    trees = {tree.name for tree in CHART_TREES}
    on_disk = {
        chart_file.parent
        for chart_file in (REPO_ROOT / "helm").glob("*/*/Chart.yaml")
        # helm/library holds the shared template library and helm/dfe-stack is
        # the umbrella, neither of which renders as a chart of its own here.
        if chart_file.parent.parent.name not in ("library", "dfe-stack")
    }
    expect("every first-party chart sits in a tree _charts sweeps",
           {d.parent.name for d in on_disk} <= trees,
           sorted({d.parent.name for d in on_disk} - trees))
    # The same map is restated in two other languages, and a chart that moves has
    # to move in all three: the drift checker resolves a pin's file from it, and
    # validate-charts.sh prints [SKIP] rather than failing when a path is wrong.
    drift = (SCRIPTS / "check_versions_drift.py").read_text(encoding="utf-8")
    validate = (SCRIPTS / "validate-charts.sh").read_text(encoding="utf-8")
    for directory in sorted(on_disk):
        declared = yaml.safe_load((directory / "Chart.yaml").read_text(encoding="utf-8"))["name"]
        expect(f"chart_dir({declared!r}) resolves to where it actually lives",
               chart_dir(declared) == directory, f"{chart_dir(declared)} != {directory}")
        if directory.parent.name == "charts":
            continue
        relative = str(directory.relative_to(REPO_ROOT))
        expect(f"check_versions_drift.py resolves {declared} to {relative}",
               f'"{declared}": "{relative}"' in drift, f"no {declared} entry naming {relative}")
        expect(f"validate-charts.sh resolves {declared} to {relative}",
               re.search(rf"^\s*{re.escape(declared)}\)\s+echo \"{re.escape(relative)}\"", validate, re.M)
               is not None,
               f"no {declared} case naming {relative}")


def test_the_gateway_keeps_the_wave_the_load_balancer_controller_needs() -> None:
    """The controller is a wave-1 addon and the gateway config is wave 2, so the
    door goes in after the thing that provisions its load balancer -- and before
    the workloads its routes name, which report NotFound until they exist."""
    gateway = next(
        element
        for appset in appsets("layer2-edge.yaml")
        for element in elements(appset)
        if element.get("app") == "envoy-gateway-config"
    )
    expect("the gateway element pins wave 2", gateway.get("wave") == "2", gateway)

    controller = next(
        element
        for appset in appsets("layer1-addons.yaml")
        for element in elements(appset)
        if element.get("chart") == "aws-load-balancer-controller"
    )
    expect("and the load balancer controller is on an earlier wave",
           int(controller["wave"]) < int(gateway["wave"]), (controller, gateway))


def test_the_tunnel_keeps_the_name_and_the_wave_layer2_apps_gave_it() -> None:
    culvert = next(
        a for a in appsets("layer2-edge.yaml") if any(git_paths(a))
    )
    template = culvert["spec"]["template"]
    expect("the tunnel's Application name is unchanged",
           template["metadata"]["name"] == "{{ .deploy.service }}-{{ .deploy.instance }}-{{ .name }}",
           template["metadata"]["name"])
    expect("and stays on wave 7",
           template["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "7",
           template["metadata"]["annotations"])
    expect("and renders from the moved directory",
           template["spec"]["sources"][0]["path"] == "helm/edge/culvert",
           template["spec"]["sources"][0]["path"])


def test_exactly_one_applicationset_fans_out_the_tunnels_values_file() -> None:
    """Two would have the controllers fight over one Application; zero deletes it."""
    including = []
    for name in ("layer2-apps.yaml", "layer2-edge.yaml"):
        for appset in appsets(name):
            for entry in git_paths(appset):
                if entry["path"] == CULVERT_GLOB and not entry.get("exclude"):
                    including.append(appset["metadata"]["name"])
    expect("one appset generates the tunnel", len(including) == 1, including)

    apps = appsets("layer2-apps.yaml")[0]
    expect("and layer2-apps excludes the glob its own wildcard would catch",
           any(e["path"] == CULVERT_GLOB and e.get("exclude") for e in git_paths(apps)),
           git_paths(apps))


def test_the_gateway_left_the_platform_appset() -> None:
    """Both appsets generating one name is what a split has to avoid."""
    platform = appsets("layer2-platform.yaml")[0]
    apps = {e["app"] for e in elements(platform)}
    expect("layer2-platform no longer generates the gateway",
           "envoy-gateway-config" not in apps, sorted(apps))


def selects(selector: dict, labels: dict[str, str]) -> bool:
    """Kubernetes label-selector semantics, for the keys the appsets use."""
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    for term in selector.get("matchExpressions") or []:
        key, operator = term["key"], term["operator"]
        present = key in labels
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
        if operator == "In" and (not present or labels[key] not in term["values"]):
            return False
        if operator == "NotIn" and present and labels[key] in term["values"]:
            return False
    return True


def generated_for(labels: dict[str, str]) -> set[str]:
    """Every Application name the appsets generate against a cluster with those labels."""
    out: set[str] = set()
    for path in sorted(APPSETS.glob("*.yaml")):
        for appset in (d for d in docs(path) if d.get("kind") == "ApplicationSet"):
            template = appset["spec"]["template"]["metadata"]["name"]
            for generator in appset["spec"]["generators"]:
                children = generator.get("matrix", {}).get("generators", [])
                if not all(
                    selects(child["clusters"]["selector"], labels)
                    for child in children
                    if "clusters" in child
                ):
                    continue
                for child in children:
                    for element in child.get("list", {}).get("elements", []):
                        out.add(
                            template.replace("{{ .app }}", element.get("app", ""))
                            .replace("{{ .chart }}", element.get("chart", ""))
                            .replace("{{ .name }}", "CLUSTER")
                        )
                    for entry in child.get("git", {}).get("files", []):
                        if entry["path"] == CULVERT_GLOB and not entry.get("exclude"):
                            out.add(
                                template.replace("{{ .deploy.service }}", "culvert")
                                .replace("{{ .deploy.instance }}", "default")
                                .replace("{{ .name }}", "CLUSTER")
                            )
    return out


EDGE_APPS = {"envoy-gateway-config-CLUSTER", "culvert-default-CLUSTER"}


def test_the_module_on_generates_both_doors() -> None:
    on = generated_for({**BASE_LABELS, EDGE_KEY: "true"})
    expect("both edge Applications generate", EDGE_APPS <= on, sorted(on))


def test_the_module_off_generates_no_door_at_all() -> None:
    """The whole-module switch: off renders no Gateway, no route and no tunnel."""
    off = generated_for({**BASE_LABELS, EDGE_KEY: "false"})
    expect("neither edge Application generates", EDGE_APPS & off == set(), sorted(off))
    expect("and the platform apps are untouched",
           {"network-policies-CLUSTER", "dfe-toolbox-CLUSTER"} <= off, sorted(off))


def test_a_cluster_secret_without_the_key_generates_no_door_either() -> None:
    """Exists is what stops a secret written before the module from matching."""
    absent = generated_for(BASE_LABELS)
    expect("neither edge Application generates", EDGE_APPS & absent == set(), sorted(absent))


def test_the_module_switch_changes_nothing_outside_the_edge() -> None:
    on = generated_for({**BASE_LABELS, EDGE_KEY: "true"})
    off = generated_for({**BASE_LABELS, EDGE_KEY: "false"})
    expect("the switch moves exactly the two edge Applications",
           on - off == EDGE_APPS and off - on == set(), sorted(on ^ off))


def test_the_dial_carries_the_switch_to_the_bootstrap_env_key() -> None:
    dial = parse_dial("edge:\n  enabled: false\n", source="test")
    updates = render_dial._env_updates(dial)
    expect("edge.enabled reaches DFE_EDGE_ENABLED",
           updates.get("DFE_EDGE_ENABLED") == "false", updates.get("DFE_EDGE_ENABLED"))


def test_the_cluster_secret_carries_the_switch_as_a_label_and_an_annotation() -> None:
    """A cluster selector matches labels only, so the label is the one that gates."""
    body = CLUSTER_SECRET.read_text(encoding="utf-8")
    expect("the switch is written twice", body.count(f"{EDGE_KEY}: ") == 2, body.count(EDGE_KEY))
    label_block, _, annotation_block = body.partition("  annotations:")
    expect("once as a label", f"{EDGE_KEY}: " in label_block, "missing from the labels")
    expect("once as an annotation", f"{EDGE_KEY}: " in annotation_block,
           "missing from the annotations")


def main() -> int:
    with standalone():
        test_the_application_name_comes_from_app_alone()
        test_the_chart_path_is_read_with_index_not_a_field_lookup()
        test_every_generated_application_name_is_pinned()
        test_every_generated_chart_path_is_pinned()
        test_an_element_stating_no_chart_falls_back_to_its_app_name()
        test_the_deployer_overlay_follows_the_name_not_the_directory()
        test_every_edge_applicationset_is_gated_on_the_module_switch()
        test_every_first_party_chart_is_reachable_through_the_shared_resolver()
        test_the_gateway_keeps_the_wave_the_load_balancer_controller_needs()
        test_the_tunnel_keeps_the_name_and_the_wave_layer2_apps_gave_it()
        test_exactly_one_applicationset_fans_out_the_tunnels_values_file()
        test_the_gateway_left_the_platform_appset()
        test_the_module_on_generates_both_doors()
        test_the_module_off_generates_no_door_at_all()
        test_a_cluster_secret_without_the_key_generates_no_door_either()
        test_the_module_switch_changes_nothing_outside_the_edge()
        test_the_dial_carries_the_switch_to_the_bootstrap_env_key()
        test_the_cluster_secret_carries_the_switch_as_a_label_and_an_annotation()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
