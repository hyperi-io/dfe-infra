#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_infra_route_login.py
#  Purpose:      Prove no infra UI without a login of its own is served bare,
#                an internet-facing infra class needs a real OIDC provider, the
#                git route exists only where Forgejo does, and the Cruise
#                Control label comes from the canonical hostnames map.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for the infra routes whose backend has no login.

The links page and the Cruise Control UI ask nobody for credentials, and the
gateway defaults to exposing the infra class with edge OIDC off. On-prem the
internet-facing guard never fires, so both were served to anyone on the LAN.

    python3 -m pytest scripts/tests/test_infra_route_login.py -q

Needs `helm` on PATH.
"""

import json
import subprocess
from pathlib import Path

import yaml

from _charts import chart_dir

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATEWAY = chart_dir("envoy-gateway-config")
VALUES = REPO_ROOT / "argocd" / "values"
APPSETS = REPO_ROOT / "argocd" / "appsets"

# The cascade layer2-edge.yaml runs for an on-prem cluster on the one profile
# whose Kafka tier deploys the Cruise Control UI.
ON_PREM = (
    "--namespace", "envoy-gateway-system",
    "-f", str(VALUES / "common.yaml"),
    "-f", str(VALUES / "local.yaml"),
    "-f", str(VALUES / "edge-onprem.yaml"),
    "-f", str(VALUES / "profile-scale.yaml"),
    "--set", "appNamespace=dfe",
    "--set", "domain=dfe.example.com",
)

# The same cascade on AWS, where the Service is internet-facing: aws.yaml turns
# oidc.enabled on with no provider, and edge-aws.yaml turns the infra class off.
AWS = (
    "--namespace", "envoy-gateway-system",
    "-f", str(VALUES / "common.yaml"),
    "-f", str(VALUES / "aws.yaml"),
    "-f", str(VALUES / "edge-aws.yaml"),
    "-f", str(VALUES / "profile-scale.yaml"),
    "--set", "appNamespace=dfe",
    "--set", "domain=dfe.example.com",
)

PROVIDER = [{"name": "acme", "issuerUrl": "https://id.example.com", "clientId": "dfe"}]
OIDC = ("--set", "oidc.enabled=true", "--set-json", f"oidc.providers={json.dumps(PROVIDER)}")

LOGIN_LESS = {"links", "cruise-control"}
OWN_LOGIN = {"argocd", "hyperdx", "kafbat"}
INFRA = {"argocd", "hyperdx", "kafbat", "forgejo", "links", "cruise-control"}
BUNDLED_LABEL = "dfe.hyperi.io/bundled-deploy-repo"


def helm(*args: str, cascade: tuple[str, ...] = ON_PREM) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "envoy-gateway-config", str(GATEWAY), *cascade, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def render(*args: str, cascade: tuple[str, ...] = ON_PREM) -> list[dict]:
    out = helm(*args, cascade=cascade)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def refusal(*args: str, cascade: tuple[str, ...] = ON_PREM) -> str:
    out = helm(*args, cascade=cascade)
    assert out.returncode != 0, "the render was expected to refuse and succeeded"
    return out.stderr


def names(docs: list[dict], kind: str) -> set[str]:
    return {d["metadata"]["name"] for d in docs if d.get("kind") == kind}


def routes(*args: str, cascade: tuple[str, ...] = ON_PREM) -> set[str]:
    return names(render(*args, cascade=cascade), "HTTPRoute")


def chart_values() -> dict:
    return yaml.safe_load((GATEWAY / "values.yaml").read_text(encoding="utf-8"))


def gateway_appsets() -> list[tuple[str, dict]]:
    """Every (file, ApplicationSet) that generates envoy-gateway-config."""
    found = []
    for filename in ("layer2-edge.yaml", "layer2-platform.yaml"):
        text = (APPSETS / filename).read_text(encoding="utf-8")
        for doc in yaml.safe_load_all(text):
            if not doc or doc.get("kind") != "ApplicationSet":
                continue
            apps = {
                element.get("app")
                for generator in doc["spec"]["generators"]
                for child in generator.get("matrix", {}).get("generators", [])
                for element in child.get("list", {}).get("elements", [])
            }
            if "envoy-gateway-config" in apps:
                found.append((filename, doc))
    return found


# --- a backend with no login is never served bare ----------------------------
def test_a_default_on_prem_deploy_serves_no_login_less_ui() -> None:
    served = routes()
    assert not LOGIN_LESS & served, sorted(served)


def test_the_edge_login_puts_them_back_behind_their_policies() -> None:
    docs = render(*OIDC)
    assert LOGIN_LESS <= names(docs, "HTTPRoute"), sorted(names(docs, "HTTPRoute"))
    policies = names(docs, "SecurityPolicy")
    assert {"dfe-oidc-links-admin", "dfe-oidc-cruise-control-admin"} <= policies, sorted(policies)


def test_oidc_switched_on_with_no_provider_is_not_a_login() -> None:
    """The infra policy renders only with a provider, so the switch alone guards nothing."""
    served = routes("--set", "oidc.enabled=true")
    assert not LOGIN_LESS & served, sorted(served)


def test_a_login_less_route_that_drops_its_edge_policy_stays_off() -> None:
    served = routes(*OIDC, "--set", "routes.links.edgePolicy=false")
    assert "links" not in served, sorted(served)


def test_routes_with_their_own_login_keep_rendering_without_the_edge_login() -> None:
    served = routes()
    assert OWN_LOGIN <= served, sorted(served)


def test_product_and_ingest_routes_are_untouched() -> None:
    served = routes()
    assert {"dfe-ui", "dfe-engine", "otel", "receiver"} <= served, sorted(served)


def test_own_login_is_read_from_the_route_values() -> None:
    """Flipping the data withdraws the route, so no template names it."""
    served = routes("--set", "routes.kafbat.ownLogin=false")
    assert "kafbat" not in served, sorted(served)


def test_every_infra_route_declares_own_login() -> None:
    declared = {
        key: route.get("ownLogin")
        for key, route in chart_values()["routes"].items()
        if route.get("class", "infra") == "infra"
    }
    assert declared == {
        "argocd": True,
        "hyperdx": True,
        "forgejo": True,
        "kafbat": True,
        "cruiseControl": False,
        "links": False,
    }


def test_a_quoted_own_login_is_refused_by_name() -> None:
    """A quoted "true" or "false" is truthy either way, and would publish the page bare."""
    assert "routes.links.ownLogin" in refusal("--set-string", "routes.links.ownLogin=false")


def test_a_public_flag_on_a_withheld_route_is_refused_by_name() -> None:
    err = refusal("--set", "ui.public_domain=example.com", "--set", "ui.public.links=true")
    assert "routes.links has no login of its own" in err, err


# --- an internet-facing infra class needs a real provider --------------------
def test_the_aws_defaults_are_the_switch_without_a_provider() -> None:
    """Pins the shape the next two cases rely on, so a changed overlay fails here."""
    aws = yaml.safe_load((VALUES / "aws.yaml").read_text(encoding="utf-8"))
    edge = yaml.safe_load((VALUES / "edge-aws.yaml").read_text(encoding="utf-8"))
    assert aws["oidc"]["enabled"] is True
    assert "providers" not in aws["oidc"], aws["oidc"]
    assert edge["envoyGateway"]["service"]["internetFacing"] is True
    assert edge["exposure"]["infraUisExternal"] is False


def test_the_aws_defaults_render_with_no_infra_route() -> None:
    served = routes(cascade=AWS)
    assert not INFRA & served, sorted(served)
    assert {"dfe-ui", "dfe-engine"} <= served, sorted(served)


def test_oidc_switched_on_with_no_provider_does_not_satisfy_the_guard() -> None:
    """The switch alone renders no edge policy, so the admin UIs would sit bare."""
    err = refusal("--set", "exposure.infraUisExternal=true", cascade=AWS)
    assert "envoyGateway.service.internetFacing is true" in err, err
    assert "no edge OIDC provider" in err, err


def test_a_provider_satisfies_the_guard_and_fronts_every_policy_route() -> None:
    docs = render("--set", "exposure.infraUisExternal=true", *OIDC, cascade=AWS)
    assert {"argocd", "links", "cruise-control"} <= names(docs, "HTTPRoute")
    policies = names(docs, "SecurityPolicy")
    assert {"dfe-oidc-argocd-admin", "dfe-oidc-links-admin",
            "dfe-oidc-cruise-control-admin"} <= policies, sorted(policies)


# --- the git route exists only where Forgejo does ----------------------------
def test_no_git_route_without_the_bundled_deploy_repo() -> None:
    assert "forgejo" not in routes(), "git.<domain> published with no Forgejo behind it"
    assert "forgejo" not in routes(*OIDC)


def test_the_git_route_renders_with_the_bundled_deploy_repo() -> None:
    assert "forgejo" in routes("--set", "deployRepo.bundled=true")


def test_a_quoted_bundled_flag_is_refused_by_name() -> None:
    assert "deployRepo.bundled" in refusal("--set-string", "deployRepo.bundled=false")


def test_both_gateway_appsets_read_the_label_forgejo_is_deployed_on() -> None:
    """The deploy-repo appset and the gateway must key on the same label and value."""
    deploy_repo = yaml.safe_load((APPSETS / "layer2-deploy-repo.yaml").read_text(encoding="utf-8"))
    selector = deploy_repo["spec"]["generators"][0]["clusters"]["selector"]["matchLabels"]
    assert selector.get(BUNDLED_LABEL) == "true", selector

    found = gateway_appsets()
    assert [f for f, _ in found] == ["layer2-edge.yaml", "layer2-platform.yaml"], found
    for filename, appset in found:
        params = {
            entry["name"]: entry["value"]
            for source in appset["spec"]["template"]["spec"]["sources"]
            for entry in source.get("helm", {}).get("parameters", [])
        }
        value = params.get("deployRepo.bundled", "")
        assert f'.metadata.labels "{BUNDLED_LABEL}"' in value, (filename, value)
        assert value.endswith('"true" }}'), (filename, value)


# --- the Cruise Control label is the canonical map's -------------------------
def test_the_cruise_control_label_lives_in_the_canonical_map() -> None:
    common = yaml.safe_load((VALUES / "common.yaml").read_text(encoding="utf-8"))
    assert common["hostnames"].get("cruiseControl") == "cruise-control", common["hostnames"]
    assert chart_values()["routes"]["cruiseControl"]["hostname"] == ""


def test_the_cruise_control_route_takes_its_host_from_the_map() -> None:
    docs = render(*OIDC, "--set", "hostnames.cruiseControl=rebalancer")
    route = [d for d in docs if d.get("kind") == "HTTPRoute"
             and d["metadata"]["name"] == "cruise-control"]
    assert len(route) == 1, sorted(names(docs, "HTTPRoute"))
    assert route[0]["spec"]["hostnames"] == ["rebalancer.dfe.example.com"]
