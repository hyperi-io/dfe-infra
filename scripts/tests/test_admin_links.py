#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_admin_links.py
#  Purpose:      Prove the admin-links list the engine serves is exactly the
#                infra HTTPRoutes the gateway renders, in the shape the engine
#                accepts, and that the engine reads it without depending on it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for the admin-links ConfigMap.

The gateway chart decides which admin UIs a deployment serves, and the engine
lists them from DFE_ADMIN_LINKS. The list is built from routeEnabled, the same
call each HTTPRoute makes, so the proof that the two cannot disagree is that the
list equals the rendered infra HTTPRoutes on every cloud, profile, edge login and
deploy-repo shape layer2-edge.yaml can produce.

    python3 -m pytest scripts/tests/test_admin_links.py -q

Needs `helm` on PATH.
"""

import functools
import itertools
import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from _charts import chart_dir

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATEWAY = chart_dir("envoy-gateway-config")
VALUES = REPO_ROOT / "argocd" / "values"
APPSETS = REPO_ROOT / "argocd" / "appsets"

APP_NAMESPACE = "dfe"
DOMAIN = "dfe.example.com"
CONFIGMAP = "dfe-admin-links"
DFE_NAMESPACE = '{{ index .metadata.annotations "dfe.hyperi.io/dfe_namespace" }}'

# Cloud fact -> the edge flavour file layer2-edge.yaml layers for it.
CLOUDS = {
    "local": "onprem",
    "local-dfe": "onprem",
    "aws": "aws",
    "gcp": "gcp",
    "azure": "azure",
}
PROFILES = ("slim", "single", "scale", "mesh")
MATRIX = list(itertools.product(sorted(CLOUDS), PROFILES, (False, True), (False, True)))

PROVIDER = [{"name": "acme", "issuerUrl": "https://id.example.com", "clientId": "dfe"}]
OIDC = ("--set", "oidc.enabled=true", "--set-json", f"oidc.providers={json.dumps(PROVIDER)}")

# The engine's AdminLink model refuses any other key.
ENTRY_KEYS = {"name", "purpose", "url", "probe_url"}


def helm(release: str, chart: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", release, str(chart), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


@functools.cache
def render(release: str, chart: Path, *args: str) -> tuple[dict, ...]:
    out = helm(release, chart, *args)
    assert out.returncode == 0, out.stderr
    return tuple(d for d in yaml.safe_load_all(out.stdout) if d)


def gateway(*args: str) -> tuple[dict, ...]:
    return render("envoy-gateway-config", GATEWAY, *args)


def cascade(cloud: str, profile: str, oidc: bool, bundled: bool) -> tuple[str, ...]:
    """The value files and parameters layer2-edge.yaml hands the gateway."""
    args = (
        "--namespace", "envoy-gateway-system",
        "-f", str(VALUES / "common.yaml"),
        "-f", str(VALUES / f"{cloud}.yaml"),
        "-f", str(VALUES / f"edge-{CLOUDS[cloud]}.yaml"),
        "-f", str(VALUES / f"profile-{profile}.yaml"),
        "--set", f"appNamespace={APP_NAMESPACE}",
        "--set", f"domain={DOMAIN}",
        "--set", f"deployRepo.bundled={str(bundled).lower()}",
    )
    return (*args, *OIDC) if oidc else args


def admin_links(docs: tuple[dict, ...]) -> tuple[dict, list[dict]]:
    """The ConfigMap, and the entries its one data key carries."""
    found = [
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == CONFIGMAP
    ]
    assert len(found) == 1, f"expected one {CONFIGMAP}, got {len(found)}"
    configmap = found[0]
    assert len(configmap["data"]) == 1, sorted(configmap["data"])
    (text,) = configmap["data"].values()
    return configmap, json.loads(text)


def infra_routes() -> dict[str, dict]:
    """routeName -> its values entry, for every route the engine lists: infra class or an adminLink."""
    values = yaml.safe_load((GATEWAY / "values.yaml").read_text(encoding="utf-8"))
    return {
        route["routeName"]: route
        for route in values["routes"].values()
        if route.get("class", "infra") == "infra" or route.get("adminLink")
    }


def served(docs: tuple[dict, ...]) -> list[tuple[str, str, str]]:
    """(name, url, probe_url) for every infra HTTPRoute the render carries, read off the route."""
    infra = infra_routes()
    rows = []
    for doc in docs:
        if doc.get("kind") != "HTTPRoute" or doc["metadata"]["name"] not in infra:
            continue
        (host,) = doc["spec"]["hostnames"]
        (backend,) = doc["spec"]["rules"][0]["backendRefs"]
        namespace = doc["metadata"]["namespace"]
        rows.append((
            infra[doc["metadata"]["name"]]["adminLink"]["name"],
            f"https://{host}",
            f"http://{backend['name']}.{namespace}.svc:{backend['port']}",
        ))
    return sorted(rows)


def listed(entries: list[dict]) -> list[tuple[str, str, str]]:
    return sorted((e["name"], e["url"], e["probe_url"]) for e in entries)


def assert_engine_shape(entry: dict) -> None:
    """What dfe_engine.admin_links.AdminLink accepts, plus https for the browser URL."""
    assert set(entry) == ENTRY_KEYS, entry
    assert entry["name"].strip(), entry
    assert entry["purpose"].strip(), entry
    url = urlsplit(entry["url"])
    assert url.scheme == "https", entry
    assert url.hostname, entry
    assert url.username is None, entry
    assert url.password is None, entry
    probe = urlsplit(entry["probe_url"])
    assert probe.scheme in ("http", "https"), entry
    assert probe.hostname, entry
    assert probe.port, entry
    assert probe.username is None, entry
    assert probe.password is None, entry


# --- the list is the rendered infra routes -----------------------------------
@pytest.mark.parametrize(("cloud", "profile", "oidc", "bundled"), MATRIX)
def test_the_list_is_exactly_the_infra_routes_the_gateway_renders(
    cloud: str, profile: str, oidc: bool, bundled: bool
) -> None:
    docs = gateway(*cascade(cloud, profile, oidc, bundled))
    configmap, entries = admin_links(docs)
    assert configmap["metadata"]["namespace"] == APP_NAMESPACE
    assert len(listed(entries)) == len(set(listed(entries))), entries
    assert listed(entries) == served(docs)
    for entry in entries:
        assert_engine_shape(entry)


def test_the_matrix_lists_every_infra_route_somewhere() -> None:
    """Two empty lists are equal and prove nothing, so every route has to turn up."""
    seen: set[str] = set()
    for combo in MATRIX:
        _, entries = admin_links(gateway(*cascade(*combo)))
        seen |= {entry["name"] for entry in entries}
    assert seen == {route["adminLink"]["name"] for route in infra_routes().values()}


def test_the_kill_switch_leaves_only_the_product_links_and_keeps_the_configmap() -> None:
    """An engine restarted after a lock-down must read no infra link, not a stale list."""
    docs = gateway(*cascade("local", "scale", True, True), "--set", "exposure.infraUisExternal=false")
    _, entries = admin_links(docs)
    assert [entry["name"] for entry in entries] == ["Search"]
    assert listed(entries) == served(docs)


def test_a_renamed_host_reaches_the_link() -> None:
    """The link is the route's own hostname, so an overlay rename moves both."""
    docs = gateway(*cascade("local", "single", False, False), "--set", "routes.argocd.hostname=gitops")
    urls = {entry["url"] for entry in admin_links(docs)[1]}
    assert f"https://gitops.{DOMAIN}" in urls, urls
    assert f"https://argocd.{DOMAIN}" not in urls, urls


def test_an_infra_route_with_no_purpose_is_refused_by_name() -> None:
    """The engine drops an entry with an empty purpose, so the render says so first."""
    out = helm(
        "envoy-gateway-config", GATEWAY,
        *cascade("local", "single", False, False), "--set", "routes.kafbat.adminLink.purpose=",
    )
    assert out.returncode != 0, "the render was expected to refuse and succeeded"
    assert "routes.kafbat renders and carries no adminLink" in out.stderr, out.stderr


def test_a_withheld_route_needs_no_label() -> None:
    """Only a route that renders is listed, so only a route that renders is checked."""
    args = (
        *cascade("local", "single", False, False),
        "--set", "routes.links.adminLink.name=", "--set", "routes.links.adminLink.purpose=",
    )
    docs = gateway(*args)
    assert "links" not in {d["metadata"]["name"] for d in docs if d.get("kind") == "HTTPRoute"}
    assert listed(admin_links(docs)[1]) == served(docs)


# --- the engine reads it, and only where it runs -----------------------------
def test_the_engine_reads_the_list_optionally_from_that_configmap() -> None:
    """Optional, so an engine with no gateway, or started before it, still starts."""
    configmap, _ = admin_links(gateway(*cascade("local", "single", False, False)))
    (key,) = configmap["data"]
    docs = render("dfe-engine", chart_dir("dfe-engine"))
    deployment = next(
        d for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == "dfe-engine"
    )
    env = {e["name"]: e for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["DFE_ADMIN_LINKS"]["valueFrom"] == {
        "configMapKeyRef": {"name": configmap["metadata"]["name"], "key": key, "optional": True},
    }


def appsets(filename: str) -> list[dict]:
    text = (APPSETS / filename).read_text(encoding="utf-8")
    return [d for d in yaml.safe_load_all(text) if d and d.get("kind") == "ApplicationSet"]


def generated_apps(appset: dict) -> set[str]:
    """Every `app` a list generator inside one of the appset's matrices names."""
    apps: set[str] = set()
    for generator in appset["spec"]["generators"]:
        for child in generator.get("matrix", {}).get("generators", []):
            apps |= {element.get("app") for element in child.get("list", {}).get("elements", [])}
    return apps


def test_the_configmap_lands_in_the_namespace_the_engine_runs_in() -> None:
    """A configMapKeyRef reads only the pod's own namespace, so both ends take one fact."""
    (apps,) = appsets("layer2-apps.yaml")
    assert apps["spec"]["template"]["spec"]["destination"]["namespace"] == DFE_NAMESPACE

    gateway_appsets = []
    for filename in ("layer2-edge.yaml", "layer2-platform.yaml"):
        for appset in appsets(filename):
            if "envoy-gateway-config" in generated_apps(appset):
                gateway_appsets.append((filename, appset))
    assert [f for f, _ in gateway_appsets] == ["layer2-edge.yaml", "layer2-platform.yaml"]
    for filename, appset in gateway_appsets:
        params = {
            entry["name"]: entry["value"]
            for entry in appset["spec"]["template"]["spec"]["sources"][0]["helm"]["parameters"]
        }
        assert params.get("appNamespace") == DFE_NAMESPACE, (filename, params.get("appNamespace"))


# --- the links page carries no second list -----------------------------------
def test_the_links_page_carries_no_list_for_a_reader_that_is_not_a_browser() -> None:
    docs = render(
        "links", chart_dir("links"),
        "-f", str(VALUES / "common.yaml"), "--set", f"domain={DOMAIN}",
    )
    pages = [d for d in docs if d.get("kind") == "ConfigMap"]
    assert len(pages) == 1, [d["metadata"]["name"] for d in pages]
    assert set(pages[0]["data"]) == {"index.html"}, sorted(pages[0]["data"])
