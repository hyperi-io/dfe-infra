#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_metrics_manifest.py
#  Purpose:      Prove dfe-engine is told where each app serves its metric
#                manifest, and that the address is a port the app's own chart
#                publishes on a Service in the engine's namespace.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Where dfe-engine reads an app's live metric manifest.

A metrics refresh fetches DFE_SERVICES_METRICS_MANIFEST_URL with `{service}`
filled by the app's name. scalo serves `/metrics/manifest` on the listener that
serves `/metrics`, so the address works only where a Service named after the app
publishes that listener, in the namespace a bare name resolves in. An address
with nothing behind it fails quietly: the refresh answers `refreshed: false`.

    python3 scripts/tests/test_metrics_manifest.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-apps.yaml"

PROFILES = ("slim", "single", "scale", "mesh")

# The apps dfe-engine ships a surface for (services/surfaces/resources/<app>.yaml).
SURFACE_APPS = ("dfe-archiver", "dfe-loader", "dfe-receiver")

MANIFEST_ENV = "DFE_SERVICES_METRICS_MANIFEST_URL"
MANIFEST_PATH = "/metrics/manifest"
# scalo's metrics listener, the one that serves the manifest.
METRICS_PORT = 9090

# The one destination every Application in layer2-apps.yaml gets, the engine's
# included, so a bare Service name resolves from the engine to any app.
DFE_NAMESPACE = '{{ index .metadata.annotations "dfe.hyperi.io/dfe_namespace" }}'


def render(chart: str, profile: str) -> list[dict]:
    """One chart under one profile, with the values cascade Argo layers."""
    cmd = [
        "helm", "template", chart, str(chart_dir(chart)),
        "-f", str(VALUES / "common.yaml"),
        "-f", str(VALUES / f"profile-{profile}.yaml"),
    ]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart} on {profile}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def manifest_template(profile: str) -> str:
    deployment = next(
        d for d in of_kind(render("dfe-engine", profile), "Deployment")
        if d["metadata"]["name"] == "dfe-engine"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}
    return env.get(MANIFEST_ENV) or ""


def test_the_engine_is_told_where_the_manifests_are() -> None:
    for profile in PROFILES:
        template = manifest_template(profile)
        expect(f"{profile} hands the engine a manifest address", bool(template),
               f"no {MANIFEST_ENV} rendered")
        # The engine refuses to start on any placeholder other than {service}.
        expect(f"{profile} names the app with the one placeholder the engine fills",
               template.count("{") == 1 and "{service}" in template, f"got {template!r}")


def test_each_address_is_a_port_the_app_chart_publishes() -> None:
    for profile in PROFILES:
        template = manifest_template(profile)
        for app in SURFACE_APPS:
            url = urlsplit(template.format(service=app))
            expect(f"{profile}: {app} is asked for its manifest path",
                   url.path == MANIFEST_PATH, f"got {url.path!r}")
            docs = render(app, profile)
            services = [s for s in of_kind(docs, "Service") if s["metadata"]["name"] == url.hostname]
            expect(f"{profile}: {app} renders a Service named {url.hostname}",
                   len(services) == 1, f"got {len(services)}")
            if not services:
                continue
            port = next((p for p in services[0]["spec"]["ports"] if p["port"] == url.port), None)
            expect(f"{profile}: that Service publishes {url.port}", port is not None,
                   f"got {services[0]['spec']['ports']!r}")
            if port is None:
                continue
            pod = of_kind(docs, "Deployment")[0]["spec"]["template"]["spec"]
            container_ports = {p["name"]: p["containerPort"] for p in pod["containers"][0]["ports"]}
            expect(f"{profile}: and targets {app}'s metrics listener",
                   container_ports.get(port["targetPort"]) == METRICS_PORT,
                   f"target {port['targetPort']!r}, container ports {container_ports!r}")


def test_the_engine_and_the_apps_land_in_one_namespace() -> None:
    """The address carries no namespace, so it resolves only beside the engine."""
    appset = yaml.safe_load(APPSET.read_text(encoding="utf-8"))
    namespace = appset["spec"]["template"]["spec"]["destination"]["namespace"]
    expect("every Application goes to the one DFE namespace", namespace == DFE_NAMESPACE,
           f"got {namespace!r}")


def main() -> int:
    with standalone():
        test_the_engine_is_told_where_the_manifests_are()
        test_each_address_is_a_port_the_app_chart_publishes()
        test_the_engine_and_the_apps_land_in_one_namespace()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
