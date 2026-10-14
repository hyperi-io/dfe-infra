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
loader's Service off the mesh, the loader pool's alias on it. Integration values
that overwrite it pin every transform to whichever loader pod its one gRPC
connection lands on. The integration values' own endpoint is the default for the
window before the engine's first routing sync, built by the same rule as the
receiver's.

The default renders are the goldens (_gate.golden). The engine's overlay renders
live, which needs the scalo-service library (_weave.library).

    python3 scripts/tests/test_transform_loader.py
"""

import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from _expect import expect, standalone, summary
from _gate import CLOUDS, configmap_files, golden
from _weave import render_app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
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


def of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def app_config(docs: list[dict], service: str) -> dict:
    cm = next(d for d in of_kind(docs, "ConfigMap") if d["metadata"]["name"] == f"{service}-config")
    (text,) = configmap_files(cm).values()
    return yaml.safe_load(text) or {}


def sink(service: str, profile: str, cloud: str = "local", overlay: dict | None = None) -> dict:
    """A transform's sink block: its golden's, or a live render's with ``overlay`` as its instance."""
    if overlay is None:
        docs = golden(service, profile, cloud)
    else:
        docs = render_app(service, profile, cloud, overlay=overlay)
    return app_config(docs, service).get("sink") or {}


def service_address(endpoint: str) -> tuple[str, str, int | None]:
    """The Service name, namespace and port a gRPC endpoint dials; a bare name is this namespace."""
    parts = urlsplit(endpoint)
    labels = (parts.hostname or "").split(".")
    namespace = labels[1] if len(labels) > 1 else NAMESPACE
    return labels[0], namespace, parts.port


def test_slim_dials_the_loader_service() -> None:
    for service in TRANSFORMS:
        for cloud in CLOUDS:
            endpoint = sink(service, "slim", cloud).get("endpoint") or ""
            expect(f"{service} on slim/{cloud} dials the loader's Service in this namespace",
                   service_address(endpoint) == ("dfe-loader", NAMESPACE, 6000),
                   f"got {endpoint!r}")


def test_mesh_dials_the_loader_pool_listener() -> None:
    """A Service balances per connection, so on mesh the transform dials the pool's alias."""
    for service in TRANSFORMS:
        for cloud in CLOUDS:
            endpoint = sink(service, "mesh", cloud).get("endpoint")
            expect(f"{service} on mesh/{cloud} dials the loader's alias in the gateway namespace",
                   endpoint == MESH_ALIAS, f"got {endpoint!r}")


def test_the_engine_endpoint_wins() -> None:
    """The overlay's compiled endpoint reaches the app. The integration values fill only a gap."""
    for service in TRANSFORMS:
        for profile in DIRECT_PROFILES:
            want = ENGINE_SINK[profile]
            got = sink(service, profile, overlay={"config": {"sink": want}})
            expect(f"{service} on {profile} keeps the engine's endpoint",
                   got.get("endpoint") == want["endpoint"], f"got {got!r}")
            expect(f"{service} on {profile} keeps the engine's routing key",
                   got.get("topic") == want["topic"], f"got {got!r}")


def test_an_overlay_cannot_move_the_transport() -> None:
    """The endpoint is a default, but the transport is still the deployment's."""
    for service in TRANSFORMS:
        moved = {"transport": "bus", "endpoint": "http://loader.elsewhere:7000"}
        overlay = {"config": {"sink": moved}}
        got = sink(service, "slim", overlay=overlay)
        expect(f"{service}: an overlay endpoint wins",
               got.get("endpoint") == "http://loader.elsewhere:7000", f"got {got!r}")
        expect(f"{service}: an overlay transport does not",
               got.get("transport") == "direct", f"got {got!r}")


def test_the_transforms_dial_what_the_receiver_dials() -> None:
    """Three integration files build the loader address by one rule; this catches one drifting."""
    for profile in DIRECT_PROFILES:
        for cloud in CLOUDS:
            receiver_docs = golden("dfe-receiver", profile, cloud)
            receiver = (app_config(receiver_docs, "dfe-receiver").get("loader") or {}).get(
                "grpc_endpoint"
            )
            for service in TRANSFORMS:
                endpoint = sink(service, profile, cloud).get("endpoint")
                expect(f"{service} on {profile}/{cloud} dials the receiver's loader address",
                       endpoint == receiver, f"got {endpoint!r}, receiver {receiver!r}")


def test_the_endpoint_is_a_service_the_loader_renders() -> None:
    for service in TRANSFORMS:
        for profile in DIRECT_PROFILES:
            for cloud in CLOUDS:
                endpoint = sink(service, profile, cloud).get("endpoint") or ""
                name, namespace, port = service_address(endpoint)
                services = [
                    s for s in of_kind(golden("dfe-loader", profile, cloud), "Service")
                    if s["metadata"]["name"] == name
                    and s["metadata"].get("namespace", NAMESPACE) == namespace
                ]
                where = f"{service} on {profile}/{cloud}"
                expect(f"{where}: {name}.{namespace} is a Service the loader's render holds",
                       len(services) == 1, f"got {len(services)} for {endpoint!r}")
                ports = [p["port"] for s in services for p in s["spec"]["ports"]]
                expect(f"{where}: that Service answers on {port}", port in ports, f"got {ports!r}")


def test_the_port_is_the_manifest_push_port() -> None:
    """apps.yaml is what the engine compiles the overlay's endpoint from."""
    apps = yaml.safe_load((REPO_ROOT / "apps.yaml").read_text(encoding="utf-8"))
    push = apps["apps"]["dfe-loader"]["endpoints"]["push"]["port"]
    for service in TRANSFORMS:
        _, _, port = service_address(sink(service, "slim").get("endpoint") or "")
        expect(f"{service} dials apps.yaml dfe-loader.endpoints.push", port == push,
               f"got {port!r}, manifest {push!r}")


def test_the_bus_profiles_name_no_endpoint() -> None:
    for service in TRANSFORMS:
        for profile in BUS_PROFILES:
            for cloud in CLOUDS:
                got = sink(service, profile, cloud)
                expect(f"{service} on {profile}/{cloud} renders no sink endpoint",
                       "endpoint" not in got, f"got {got!r}")


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
