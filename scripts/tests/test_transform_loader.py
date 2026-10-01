#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_transform_loader.py
#  Purpose:      Prove a transform on the direct transport dials the loader at
#                the address the receiver does, and keeps an overlay endpoint.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Where a transform pushes its output on the direct transport.

The engine compiles `sink.endpoint` into each transform instance's overlay: the
loader's Service off the mesh, the loader pool's alias on it. A chart that
overwrites it pins every transform to whichever loader pod its one gRPC
connection lands on. The chart's own value is the default for the window before
the engine's first routing sync, built by the same rule as the receiver's.

    python3 scripts/tests/test_transform_loader.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
NAMESPACE = "dfe"

TRANSFORMS = ("dfe-transform-vrl", "dfe-transform-vector")
DIRECT_PROFILES = ("slim", "mesh")
BUS_PROFILES = ("single", "scale")

# argocd/values/common.yaml `mesh.gateway.namespace`, restated so a move fails here.
GATEWAY_NAMESPACE = "envoy-gateway-system"

MESH_ALIAS = f"http://dfe-loader-mesh.{GATEWAY_NAMESPACE}.svc.cluster.local:6000"

# What the engine writes to values/dfe-transform-*-<instance>-values.yaml on
# direct after a routing sync (dfe-engine appmgmt/routing.py _transform), per profile.
ENGINE_SINK = {
    "slim": {"transport": "direct", "endpoint": "http://dfe-loader:6000", "topic": "auth_load"},
    "mesh": {"transport": "direct", "endpoint": MESH_ALIAS, "topic": "auth_load"},
}


def render(chart: str, profile: str, *extra: str) -> list[dict]:
    """One chart under one profile, with the values cascade Argo layers."""
    cmd = [
        "helm", "template", chart, str(chart_dir(chart)),
        "--namespace", NAMESPACE,
        "-f", str(VALUES / "common.yaml"),
        "-f", str(VALUES / f"profile-{profile}.yaml"),
        *extra,
    ]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart} on {profile}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def with_config(config: dict) -> tuple[str, str]:
    return ("--set-json", f"config={json.dumps(config)}")


def app_config(chart: str, profile: str, *extra: str) -> dict:
    cm = next(
        d for d in of_kind(render(chart, profile, *extra), "ConfigMap")
        if d["metadata"]["name"] == f"{chart}-config"
    )
    return yaml.safe_load(cm["data"]["config.yaml"]) or {}


def sink_endpoint(chart: str, profile: str, *extra: str) -> str:
    return (app_config(chart, profile, *extra).get("sink") or {}).get("endpoint") or ""


def test_slim_dials_the_loader_service() -> None:
    for chart in TRANSFORMS:
        endpoint = sink_endpoint(chart, "slim")
        expect(f"{chart} on slim dials the loader's Service in this namespace",
               endpoint == f"http://dfe-loader.{NAMESPACE}.svc.cluster.local:6000",
               f"got {endpoint!r}")


def test_mesh_dials_the_loader_pool_listener() -> None:
    """A Service balances per connection, so on mesh the transform dials the pool's alias."""
    for chart in TRANSFORMS:
        endpoint = sink_endpoint(chart, "mesh")
        expect(f"{chart} on mesh dials the loader's alias in the gateway namespace",
               endpoint == MESH_ALIAS, f"got {endpoint!r}")


def test_the_engine_endpoint_wins() -> None:
    """The overlay's compiled endpoint reaches the app. The chart fills only a gap."""
    for chart in TRANSFORMS:
        for profile in DIRECT_PROFILES:
            want = ENGINE_SINK[profile]
            sink = app_config(chart, profile, *with_config({"sink": want})).get("sink") or {}
            expect(f"{chart} on {profile} keeps the engine's endpoint",
                   sink.get("endpoint") == want["endpoint"], f"got {sink!r}")
            expect(f"{chart} on {profile} keeps the engine's routing key",
                   sink.get("topic") == want["topic"], f"got {sink!r}")


def test_an_overlay_cannot_move_the_transport() -> None:
    """The endpoint is a default, but the transport is still the deployment's."""
    for chart in TRANSFORMS:
        overlay = {"sink": {"transport": "bus", "endpoint": "http://loader.elsewhere:7000"}}
        sink = app_config(chart, "slim", *with_config(overlay)).get("sink") or {}
        expect(f"{chart}: an overlay endpoint wins",
               sink.get("endpoint") == "http://loader.elsewhere:7000", f"got {sink!r}")
        expect(f"{chart}: an overlay transport does not",
               sink.get("transport") == "direct", f"got {sink!r}")


def test_the_transforms_dial_what_the_receiver_dials() -> None:
    """Three charts build the loader address by one rule; this catches one drifting."""
    for profile in DIRECT_PROFILES:
        receiver = (app_config("dfe-receiver", profile).get("loader") or {}).get("grpc_endpoint")
        for chart in TRANSFORMS:
            endpoint = sink_endpoint(chart, profile)
            expect(f"{chart} on {profile} dials the receiver's loader address",
                   endpoint == receiver, f"got {endpoint!r}, receiver {receiver!r}")


def test_the_endpoint_is_a_service_the_loader_renders() -> None:
    for chart in TRANSFORMS:
        for profile in DIRECT_PROFILES:
            endpoint = sink_endpoint(chart, profile)
            parts = urlsplit(endpoint)
            labels = (parts.hostname or "").split(".")
            name, namespace = [*labels, "", ""][:2]
            services = [
                s for s in of_kind(render("dfe-loader", profile), "Service")
                if s["metadata"]["name"] == name
                and s["metadata"].get("namespace", NAMESPACE) == namespace
            ]
            expect(f"{chart} on {profile}: {name}.{namespace} is a Service the loader renders",
                   len(services) == 1, f"got {len(services)} for {endpoint!r}")
            ports = [p["port"] for s in services for p in s["spec"]["ports"]]
            expect(f"{chart} on {profile}: that Service answers on {parts.port}",
                   parts.port in ports, f"got {ports!r}")


def test_the_port_is_the_manifest_push_port() -> None:
    """apps.yaml is what the engine compiles the overlay's endpoint from."""
    apps = yaml.safe_load((REPO_ROOT / "apps.yaml").read_text(encoding="utf-8"))
    push = apps["apps"]["dfe-loader"]["endpoints"]["push"]["port"]
    for chart in TRANSFORMS:
        port = urlsplit(sink_endpoint(chart, "slim")).port
        expect(f"{chart} dials apps.yaml dfe-loader.endpoints.push", port == push,
               f"got {port!r}, manifest {push!r}")


def test_the_bus_profiles_name_no_endpoint() -> None:
    for chart in TRANSFORMS:
        for profile in BUS_PROFILES:
            sink = app_config(chart, profile).get("sink") or {}
            expect(f"{chart} on {profile} renders no sink endpoint", "endpoint" not in sink,
                   f"got {sink!r}")


def main() -> int:
    with standalone():
        test_slim_dials_the_loader_service()
        test_mesh_dials_the_loader_pool_listener()
        test_the_engine_endpoint_wins()
        test_an_overlay_cannot_move_the_transport()
        test_the_transforms_dial_what_the_receiver_dials()
        test_the_endpoint_is_a_service_the_loader_renders()
        test_the_port_is_the_manifest_push_port()
        test_the_bus_profiles_name_no_endpoint()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
