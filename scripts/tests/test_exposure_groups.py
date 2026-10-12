#  Project:      dfe-infra
#  File:         scripts/tests/test_exposure_groups.py
#  Purpose:      Hold the exposure dial's data to the gateway's routes: the admin
#                tool map is the one admin list and names exactly the class-infra
#                routes, the dial-side admin lists stay its copies, every route
#                class has a group, the engine's two routes share one backend and
#                split its families cleanly, and no tracked values file carries a
#                retired exposure key.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The exposure groups as data, checked without rendering a chart.

argocd/values/common.yaml carries exposure.groups, and helm/edge/gateway's
values.yaml carries the routes those groups fence. The render tests
(test_edge_web_fence.py, scripts/test-route-exposure.sh) prove what the chart
does with them; this file proves the two files agree before any render runs.

    python3 -m pytest scripts/tests/test_exposure_groups.py -q
"""

import sys
from pathlib import Path

import pytest
import yaml

from _charts import chart_dir

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
VALUES = REPO_ROOT / "argocd" / "values"
GATEWAY_VALUES = chart_dir("envoy-gateway-config") / "values.yaml"

sys.path.insert(0, str(SCRIPTS))
import dfe_ops_edge_probe as probe  # noqa: E402
import dfe_ops_init as wiz  # noqa: E402
import render_dial  # noqa: E402

GROUPS = ("ingest", "console", "api", "admin", "vpn")
GROUP_OF_CLASS = {"product": "console", "api": "api", "infra": "admin", "ingest": "ingest"}
# The dial still names admin UIs in snake case and lists HyperDX beside them,
# which the chart classes product (ui.public.hyperdx).
DIAL_TO_ROUTE = {"cruise_control": "cruiseControl"}
DIAL_PRODUCT = {"hyperdx"}


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def routes() -> dict[str, dict]:
    return load(GATEWAY_VALUES)["routes"]


def common_groups() -> dict[str, dict]:
    return load(VALUES / "common.yaml")["exposure"]["groups"]


def infra_route_keys() -> set[str]:
    return {key for key, r in routes().items() if r.get("class", "infra") == "infra"}


def dial_admin_tools(names: set[str]) -> set[str]:
    """A dial-side admin list as admin tool keys."""
    return {DIAL_TO_ROUTE.get(name, name) for name in names - DIAL_PRODUCT}


# --- the canonical admin list ---------------------------------------------------------


def test_the_admin_tool_map_names_exactly_the_class_infra_routes() -> None:
    tools = set(common_groups()["admin"]["tools"])
    assert tools == infra_route_keys(), (sorted(tools), sorted(infra_route_keys()))


def test_every_admin_tool_defaults_off() -> None:
    """A tool's public hostname is an opt-in on every flavour."""
    assert set(common_groups()["admin"]["tools"].values()) == {False}


def test_hyperdx_is_product_and_never_an_admin_tool() -> None:
    assert routes()["hyperdx"]["class"] == "product"
    assert "hyperdx" not in common_groups()["admin"]["tools"]


@pytest.mark.parametrize(("where", "names"), [
    ("render_dial.ADMIN_UIS", set(render_dial.ADMIN_UIS)),
    ("dfe_ops_edge_probe.ADMIN_UI_HOSTNAMES", set(probe.ADMIN_UI_HOSTNAMES)),
], ids=["render-dial", "edge-probe"])
def test_each_dial_side_admin_list_is_a_copy_of_the_tool_map(where: str, names: set[str]) -> None:
    """The dial still spells these lists itself, so drift from the map fails here."""
    assert dial_admin_tools(names) == set(common_groups()["admin"]["tools"]), where


def test_the_wizards_admin_block_is_a_copy_of_the_tool_map() -> None:
    dial = yaml.safe_load(wiz.build_dial_text(wiz.Answers()))
    names = set(dial["edge"]["admin_uis"]["public"])
    assert dial_admin_tools(names) == set(common_groups()["admin"]["tools"]), sorted(names)


# --- every route has a group, every group its fields ------------------------------------


def test_every_route_class_puts_it_in_a_group() -> None:
    for key, r in routes().items():
        assert r.get("class", "infra") in GROUP_OF_CLASS, (key, r.get("class"))


def test_common_carries_every_group_with_its_fields() -> None:
    groups = common_groups()
    assert set(groups) == set(GROUPS)
    for name, spec in groups.items():
        assert isinstance(spec["enabled"], bool), name
        assert isinstance(spec["allow"], list), name
    for name in ("ingest", "console", "api", "admin"):
        assert groups[name]["deny"] == [], name
    assert groups["api"]["scim"] is False
    assert groups["admin"]["allowPublic"] is False
    assert load(VALUES / "common.yaml")["exposure"]["selfRanges"] == []


@pytest.mark.parametrize("flavour", ["aws", "gcp", "azure"])
def test_every_cloud_flavour_turns_the_admin_group_off(flavour: str) -> None:
    edge = load(VALUES / f"edge-{flavour}.yaml")
    assert edge["exposure"]["groups"]["admin"]["enabled"] is False


def test_the_default_groups_match_the_design_on_prem() -> None:
    groups = common_groups()
    assert [groups[g]["enabled"] for g in GROUPS] == [True, True, True, True, False]
    assert load(VALUES / "local.yaml")["exposure"]["groups"]["ingest"]["door"] == "gateway"
    assert groups["ingest"]["door"] == "loadbalancer"
    assert groups["vpn"]["door"] == "culvert"


# --- the engine's two routes ----------------------------------------------------------


SHARED_ENGINE_FIELDS = ("hostnameKey", "backendService", "backendPort", "pathPrefix", "requestTimeout",
                        "publicListenerOf")


@pytest.mark.parametrize("field", SHARED_ENGINE_FIELDS)
def test_the_cli_route_reaches_the_same_engine_as_the_browser_route(field: str) -> None:
    r = routes()
    assert r["dfeEngineCli"][field] == r["dfeEngine"][field], field


def test_the_engine_routes_carry_their_own_groups() -> None:
    r = routes()
    assert (r["dfeEngine"]["class"], r["dfeEngineCli"]["class"]) == ("product", "api")


def test_expected_fail_no_family_answers_on_both_engine_routes() -> None:
    r = routes()
    browser = set(r["dfeEngine"]["browserFamilies"]) | set(r["dfeEngine"]["browserPaths"])
    cli = set(r["dfeEngineCli"]["cliFamilies"]) | set(r["dfeEngineCli"]["cliPaths"])
    assert not browser & cli, sorted(browser & cli)
    assert "scim/v2" not in r["dfeEngine"]["browserFamilies"]
    assert "openapi.json" not in str(r["dfeEngine"])


def test_the_browser_route_carries_what_dfe_ui_calls() -> None:
    """dfe-ui's Admin Tools page reads /api/v1/deployment/admin-links."""
    assert "deployment" in routes()["dfeEngine"]["browserFamilies"]


# --- no tracked values file carries a retired key --------------------------------------


def leaf_paths(node: object, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    if not isinstance(node, dict) or not node:
        return [prefix] if prefix else []
    out: list[tuple[str, ...]] = []
    for key, child in node.items():
        out += leaf_paths(child, (*prefix, str(key)))
    return out


RETIRED = (
    ("ui", "allowed_cidrs"),
    ("exposure", "infraUisExternal"),
    *(("ui", "public", name) for name in ("kafbat", "cruise_control", "argocd", "links", "forgejo")),
)


@pytest.mark.parametrize("path", [
    GATEWAY_VALUES,
    *sorted(VALUES.glob("*.yaml")),
], ids=lambda p: p.name)
def test_expected_fail_no_values_file_carries_a_retired_exposure_key(path: Path) -> None:
    found = [".".join(p) for p in leaf_paths(load(path)) if any(p[: len(r)] == r for r in RETIRED)]
    assert not found, f"{path.name} still sets {found}"
