#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_argocd_behind_gateway.py
#  Purpose:      Prove the Argo CD bootstrap installs answers plain HTTP, and
#                that the gateway route, the admin-link probe and the Forgejo
#                push hook all reach argocd-server on its plain HTTP port.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Where the pieces that call argocd-server meet it.

Envoy terminates TLS at the gateway and forwards plain HTTP. A TLS-serving
argocd-server answers plain HTTP with a 307 to https on the same host, which
through the gateway is a redirect back to the URL the browser asked for. So the
Argo bootstrap installs runs insecure, and every in-cluster caller uses the
Service's plain HTTP port.

    python3 -m pytest scripts/tests/test_argocd_behind_gateway.py -q

Needs `helm` on PATH.
"""

import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from _charts import chart_dir

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
VALUES = REPO_ROOT / "argocd" / "values"
GATEWAY = chart_dir("envoy-gateway-config")

ARGO_INSTALL = "helm upgrade --install argocd argo/argo-cd"
INSECURE_FLAG = "--set-string 'configs.params.server\\.insecure=true'"
ADOPT_BRANCH = "Using existing ArgoCD"

# The argo-cd chart's server.service.servicePortHttp: the Service port whose
# target is the server's one listener, named `http`.
ARGO_SERVER_HTTP_PORT = 80
ARGO_SERVER = "argocd-server"
ARGO_NAMESPACE = "argocd"

ON_PREM = (
    "--namespace", "envoy-gateway-system",
    "-f", str(VALUES / "common.yaml"),
    "-f", str(VALUES / "local-dfe.yaml"),
    "--set", "appNamespace=dfe",
    "--set", "domain=dfe.example.com",
)


def helm_template(release: str, chart: Path, *args: str) -> list[dict]:
    out = subprocess.run(
        ["helm", "template", release, str(chart), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def install_command() -> str:
    """bootstrap.sh's Argo CD install, from the helm call to its --wait."""
    body = BOOTSTRAP.read_text(encoding="utf-8")
    start = body.find(ARGO_INSTALL)
    assert start >= 0, f"bootstrap.sh no longer carries {ARGO_INSTALL!r}"
    end = body.find("--wait", start)
    assert end > start, "the Argo CD install no longer ends in --wait"
    return body[start:end]


# --- bootstrap installs Argo CD insecure -------------------------------------
def test_the_argo_install_runs_the_server_insecure() -> None:
    assert INSECURE_FLAG in install_command()


def test_an_adopted_argo_is_not_reconfigured() -> None:
    """The flag lives on the install alone, never on the adopt branch."""
    body = BOOTSTRAP.read_text(encoding="utf-8")
    assert body.count("server\\.insecure") == 1
    assert body.find(INSECURE_FLAG) < body.find(ADOPT_BRANCH)


# --- the route and the probe reach the plain HTTP port -----------------------
def test_the_argocd_route_targets_the_plain_http_port() -> None:
    docs = helm_template("envoy-gateway-config", GATEWAY, *ON_PREM)
    (route,) = [
        d for d in docs if d.get("kind") == "HTTPRoute" and d["metadata"]["name"] == "argocd"
    ]
    (backend,) = route["spec"]["rules"][0]["backendRefs"]
    assert backend["name"] == ARGO_SERVER
    assert backend["port"] == ARGO_SERVER_HTTP_PORT
    assert route["metadata"]["namespace"] == ARGO_NAMESPACE


def test_the_admin_link_probe_speaks_plain_http_to_the_same_port() -> None:
    docs = helm_template("envoy-gateway-config", GATEWAY, *ON_PREM)
    (configmap,) = [
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "dfe-admin-links"
    ]
    (text,) = configmap["data"].values()
    probes = [
        urlsplit(entry["probe_url"])
        for entry in yaml.safe_load(text)
        if entry["name"] == "Argo CD"
    ]
    assert len(probes) == 1, text
    assert probes[0].scheme == "http"
    assert probes[0].hostname == f"{ARGO_SERVER}.{ARGO_NAMESPACE}.svc"
    assert probes[0].port == ARGO_SERVER_HTTP_PORT


# --- the Forgejo push hook reaches the same port -----------------------------
def forgejo_setup_env() -> dict[str, str]:
    docs = helm_template(
        "t", chart_dir("forgejo"), "--show-only", "templates/setup-job.yaml",
    )
    (job,) = docs
    container = job["spec"]["template"]["spec"]["containers"][0]
    return {item["name"]: item["value"] for item in container["env"] if "value" in item}


def test_the_push_hook_speaks_plain_http_to_the_argo_service() -> None:
    url = urlsplit(forgejo_setup_env()["WEBHOOK_URL"])
    assert url.scheme == "http"
    assert url.hostname == f"{ARGO_SERVER}.{ARGO_NAMESPACE}.svc.cluster.local"
    assert (url.port or ARGO_SERVER_HTTP_PORT) == ARGO_SERVER_HTTP_PORT
    assert url.path == "/api/webhook"


def test_the_hook_it_replaces_is_the_same_endpoint_over_https() -> None:
    env = forgejo_setup_env()
    current, legacy = urlsplit(env["WEBHOOK_URL"]), urlsplit(env["LEGACY_WEBHOOK_URL"])
    assert legacy.scheme == "https"
    assert (legacy.hostname, legacy.path) == (current.hostname, current.path)


def test_a_disabled_hook_removes_nothing() -> None:
    docs = helm_template(
        "t", chart_dir("forgejo"), "--show-only", "templates/setup-job.yaml",
        "--set", "webhook.enabled=false",
    )
    container = docs[0]["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["WEBHOOK_URL"] == ""
    assert env["LEGACY_WEBHOOK_URL"] == ""
