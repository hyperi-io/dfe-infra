#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_receiver_loader.py
#  Purpose:      Prove the receiver on the direct transport is told to push to
#                the loader over gRPC, at an address the loader's render serves.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The receiver's built-in `loader` destination, on the direct transport.

dfe-receiver's `loader.transport` defaults to kafka and has no flat-env key, and
the engine compiles `destinations` with `default: loader` but no loader address,
which it leaves to the deployment. A ConfigMap that names neither leaves a
brokerless receiver refusing to start: "kafka.brokers is required when a
destination is on the bus (destinations, or loader.transport: kafka)".

The default renders are the goldens (_gate.golden). The engine's routing sync and
a deployer's own overlay render live, with the instance file the engine writes,
which needs the scalo-service library (_weave.library).

    python3 scripts/tests/test_receiver_loader.py
"""

import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from _expect import expect, standalone, summary
from _gate import CLOUDS, configmap_files, effective_env, golden
from _weave import render_app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
NAMESPACE = "dfe"

DIRECT_PROFILES = ("slim", "mesh")
BUS_PROFILES = ("single", "scale")

# argocd/values/common.yaml `mesh.gateway.namespace`, restated so a move fails here.
GATEWAY_NAMESPACE = "envoy-gateway-system"

# What the engine writes to values/dfe-receiver-<instance>-values.yaml on direct
# after a routing sync: the default destination by name, and no loader address.
ENGINE_OVERLAY = {"config": {"destinations": {"default": "loader", "sources": {}}}}


def of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def receiver_docs(profile: str, cloud: str = "local", overlay: dict | None = None) -> list[dict]:
    """The receiver's render: its golden, or a live render with ``overlay`` as its instance file."""
    if overlay is None:
        return golden("dfe-receiver", profile, cloud)
    return render_app("dfe-receiver", profile, cloud, overlay=overlay)


def receiver_config(docs: list[dict]) -> dict:
    cm = next(
        d for d in of_kind(docs, "ConfigMap") if d["metadata"]["name"] == "dfe-receiver-config"
    )
    return yaml.safe_load(configmap_files(cm)["config.yaml"]) or {}


def endpoint_of(docs: list[dict]) -> str:
    return (receiver_config(docs).get("loader") or {}).get("grpc_endpoint") or ""


def service_address(endpoint: str) -> tuple[str, str, int | None]:
    """The Service name, namespace and port a gRPC endpoint dials; a bare name is this namespace."""
    parts = urlsplit(endpoint)
    labels = (parts.hostname or "").split(".")
    namespace = labels[1] if len(labels) > 1 else NAMESPACE
    return labels[0], namespace, parts.port


def on_the_bus(cfg: dict) -> list[str]:
    """The destinations the receiver resolves to the bus, by its own rule.

    Mirrors dfe-receiver src/config/mod.rs at v1.15.54: `referenced_names` is the
    default and every `sources` entry, `resolved_destinations` turns an undeclared
    `loader` into a bus destination when `loader.transport` is kafka (its
    default), and `validate` refuses any bus destination when `kafka.brokers` is
    empty.
    """
    destinations = cfg.get("destinations") or {}
    default = destinations.get("default", "kafka")
    names = [default] if isinstance(default, str) else list(default)
    for target in (destinations.get("sources") or {}).values():
        names.extend([target] if isinstance(target, str) else target or [])
    loader_transport = (cfg.get("loader") or {}).get("transport", "kafka")
    bus = []
    for name in names:
        declared = destinations.get(name)
        if isinstance(declared, dict):
            if "kafka" in declared:
                bus.append(name)
        elif name == "kafka" or (name == "loader" and loader_transport == "kafka"):
            bus.append(name)
    return bus


def test_direct_starts_without_brokers() -> None:
    """The receiver's own startup rule, before and after the engine's routing sync."""
    for profile in DIRECT_PROFILES:
        for cloud in CLOUDS:
            docs = receiver_docs(profile, cloud)
            env = effective_env(docs, "Deployment/dfe-receiver")
            expect(f"{profile}/{cloud} hands the receiver no brokers",
                   "DFE_RECEIVER_KAFKA_BROKERS" not in env,
                   f"got {env.get('DFE_RECEIVER_KAFKA_BROKERS')!r}")
            seed = receiver_config(docs)
            expect(f"{profile}/{cloud} seed config has no bus destination", on_the_bus(seed) == [],
                   f"got {on_the_bus(seed)!r} from {seed!r}")
        synced = receiver_config(receiver_docs(profile, overlay=ENGINE_OVERLAY))
        expect(f"{profile} synced config has no bus destination", on_the_bus(synced) == [],
               f"got {on_the_bus(synced)!r} from {synced!r}")


def test_slim_pushes_to_the_loader_over_grpc() -> None:
    cfg = receiver_config(receiver_docs("slim", overlay=ENGINE_OVERLAY))
    loader = cfg.get("loader") or {}
    expect("slim sets loader.transport grpc", loader.get("transport") == "grpc", f"got {loader!r}")
    expect("slim dials the loader's Service in this namespace",
           service_address(loader.get("grpc_endpoint") or "") == ("dfe-loader", NAMESPACE, 6000),
           f"got {loader!r}")
    expect("an unmatched record goes to the loader",
           (cfg.get("destinations") or {}).get("default") == "loader", f"got {cfg!r}")


def test_mesh_dials_the_loader_pool_listener() -> None:
    """A Service balances per connection, so on mesh the receiver dials the pool's alias."""
    for cloud in CLOUDS:
        endpoint = endpoint_of(receiver_docs("mesh", cloud))
        expect(f"mesh/{cloud} dials the loader's alias in the gateway namespace",
               endpoint == f"http://dfe-loader-mesh.{GATEWAY_NAMESPACE}.svc.cluster.local:6000",
               f"got {endpoint!r}")


def test_the_endpoint_is_a_service_the_loader_renders() -> None:
    for profile in DIRECT_PROFILES:
        for cloud in CLOUDS:
            endpoint = endpoint_of(receiver_docs(profile, cloud))
            name, namespace, port = service_address(endpoint)
            services = [
                s for s in of_kind(golden("dfe-loader", profile, cloud), "Service")
                if s["metadata"]["name"] == name
                and s["metadata"].get("namespace", NAMESPACE) == namespace
            ]
            expect(f"{profile}/{cloud}: {name}.{namespace} is a Service the loader's render holds",
                   len(services) == 1, f"got {len(services)} for {endpoint!r}")
            ports = [p["port"] for s in services for p in s["spec"]["ports"]]
            expect(f"{profile}/{cloud}: that Service answers on {port}", port in ports,
                   f"got {ports!r}")


def test_the_loader_listens_where_the_receiver_dials() -> None:
    """The Service is not enough: the loader has to serve gRPC on that port on slim."""
    _, _, port = service_address(endpoint_of(receiver_docs("slim")))
    docs = golden("dfe-loader", "slim", "local")
    env = effective_env(docs, "Deployment/dfe-loader")
    expect("the loader runs the grpc transport on slim", env.get("DFE_LOADER_TRANSPORT") == "grpc",
           f"got {env.get('DFE_LOADER_TRANSPORT')!r}")
    expect("it binds the port the receiver dials",
           env.get("DFE_LOADER_GRPC__LISTEN") == f"0.0.0.0:{port}",
           f"got {env.get('DFE_LOADER_GRPC__LISTEN')!r}")
    service = next(s for s in of_kind(docs, "Service") if s["metadata"]["name"] == "dfe-loader")
    target = next((p["targetPort"] for p in service["spec"]["ports"] if p["port"] == port), None)
    pod = of_kind(docs, "Deployment")[0]["spec"]["template"]["spec"]
    container_ports = {p["name"]: p["containerPort"] for p in pod["containers"][0]["ports"]}
    expect(f"the Service's target {target!r} is the container's {port}",
           target is not None and container_ports.get(target) == port, f"got {container_ports!r}")


def test_the_port_is_the_manifest_push_port() -> None:
    """apps.yaml is what the engine and the receiver's own default read the port from."""
    apps = yaml.safe_load((REPO_ROOT / "apps.yaml").read_text(encoding="utf-8"))
    push = apps["apps"]["dfe-loader"]["endpoints"]["push"]["port"]
    _, _, port = service_address(endpoint_of(receiver_docs("slim")))
    expect("the receiver dials apps.yaml dfe-loader.endpoints.push", port == push,
           f"got {port!r}, manifest {push!r}")


def test_the_overlay_keeps_what_it_set() -> None:
    """The engine writes destinations whole; the integration values fill only what it left out."""
    auth = {"grpc": {"endpoint": "http://dfe-transform-vrl-auth:6000"}}
    overlay = {
        "config": {
            "destinations": {
                "default": "dfe-transform-vrl-auth",
                "sources": {},
                "dfe-transform-vrl-auth": auth,
            },
            "loader": {"grpc_endpoint": "http://loader.elsewhere:7000", "transport": "kafka"},
        }
    }
    cfg = receiver_config(receiver_docs("slim", overlay=overlay))
    destinations = cfg.get("destinations") or {}
    expect("the overlay's default destination wins",
           destinations.get("default") == "dfe-transform-vrl-auth", f"got {destinations!r}")
    expect("its sources survive", destinations.get("sources") == {}, f"got {destinations!r}")
    expect("its declared destination survives",
           destinations.get("dfe-transform-vrl-auth")
           == {"grpc": {"endpoint": "http://dfe-transform-vrl-auth:6000"}},
           f"got {destinations!r}")
    loader = cfg.get("loader") or {}
    expect("an overlay loader address wins",
           loader.get("grpc_endpoint") == "http://loader.elsewhere:7000", f"got {loader!r}")
    expect("the transport does not: there is no broker on direct",
           loader.get("transport") == "grpc", f"got {loader!r}")


def test_the_bus_profiles_leave_the_loader_alone() -> None:
    for profile in BUS_PROFILES:
        for cloud in CLOUDS:
            docs = receiver_docs(profile, cloud)
            cfg = receiver_config(docs)
            expect(f"{profile}/{cloud} renders no loader block", "loader" not in cfg,
                   f"got {cfg.get('loader')!r}")
            expect(f"{profile}/{cloud} renders no destinations", "destinations" not in cfg,
                   f"got {cfg.get('destinations')!r}")
            env = effective_env(docs, "Deployment/dfe-receiver")
            expect(f"{profile}/{cloud} still hands the receiver its brokers",
                   "DFE_RECEIVER_KAFKA_BROKERS" in env, f"got {sorted(env)!r}")


def main() -> int:
    with standalone():
        test_direct_starts_without_brokers()
        test_slim_pushes_to_the_loader_over_grpc()
        test_mesh_dials_the_loader_pool_listener()
        test_the_endpoint_is_a_service_the_loader_renders()
        test_the_loader_listens_where_the_receiver_dials()
        test_the_port_is_the_manifest_push_port()
        test_the_overlay_keeps_what_it_set()
        test_the_bus_profiles_leave_the_loader_alone()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
