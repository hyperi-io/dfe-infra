#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_receiver_loader.py
#  Purpose:      Prove the receiver on the direct transport is told to push to
#                the loader over gRPC, at an address the loader chart renders.
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

    python3 scripts/tests/test_receiver_loader.py

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

DIRECT_PROFILES = ("slim", "mesh")
BUS_PROFILES = ("single", "scale")

# argocd/values/common.yaml `mesh.gateway.namespace`, restated so a move fails here.
GATEWAY_NAMESPACE = "envoy-gateway-system"

# What the engine writes to values/dfe-receiver-<instance>-values.yaml on direct
# after a routing sync: the default destination by name, and no loader address.
ENGINE_OVERLAY = {"destinations": {"default": "loader", "rules": []}}


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


def receiver_config(profile: str, *extra: str) -> dict:
    cm = next(
        d for d in of_kind(render("dfe-receiver", profile, *extra), "ConfigMap")
        if d["metadata"]["name"] == "dfe-receiver-config"
    )
    return yaml.safe_load(cm["data"]["config.yaml"]) or {}


def container_env(docs: list[dict]) -> dict:
    pod = of_kind(docs, "Deployment")[0]["spec"]["template"]["spec"]
    return {e["name"]: e.get("value") for e in pod["containers"][0].get("env", [])}


def on_the_bus(cfg: dict) -> list[str]:
    """The destinations the receiver resolves to the bus, by its own rule.

    Mirrors dfe-receiver src/config/mod.rs at v1.15.41: `resolved_destinations`
    turns an undeclared `loader` into a bus destination when `loader.transport` is
    kafka (its default), and `validate` refuses any bus destination when
    `kafka.brokers` is empty.
    """
    destinations = cfg.get("destinations") or {}
    default = destinations.get("default", "kafka")
    names = [default] if isinstance(default, str) else list(default)
    for rule in destinations.get("rules") or []:
        target = rule.get("destination")
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
        env = container_env(render("dfe-receiver", profile))
        expect(f"{profile} hands the receiver no brokers", "DFE_RECEIVER_KAFKA_BROKERS" not in env,
               f"got {env.get('DFE_RECEIVER_KAFKA_BROKERS')!r}")
        seed = receiver_config(profile)
        expect(f"{profile} seed config has no bus destination", on_the_bus(seed) == [],
               f"got {on_the_bus(seed)!r} from {seed!r}")
        synced = receiver_config(profile, *with_config(ENGINE_OVERLAY))
        expect(f"{profile} synced config has no bus destination", on_the_bus(synced) == [],
               f"got {on_the_bus(synced)!r} from {synced!r}")


def test_slim_pushes_to_the_loader_over_grpc() -> None:
    cfg = receiver_config("slim", *with_config(ENGINE_OVERLAY))
    loader = cfg.get("loader") or {}
    expect("slim sets loader.transport grpc", loader.get("transport") == "grpc", f"got {loader!r}")
    expect("slim dials the loader's Service in this namespace",
           loader.get("grpc_endpoint") == f"http://dfe-loader.{NAMESPACE}.svc.cluster.local:6000",
           f"got {loader!r}")
    expect("an unmatched record goes to the loader",
           (cfg.get("destinations") or {}).get("default") == "loader", f"got {cfg!r}")


def test_mesh_dials_the_loader_pool_listener() -> None:
    """A Service balances per connection, so on mesh the receiver dials the pool's alias."""
    endpoint = (receiver_config("mesh").get("loader") or {}).get("grpc_endpoint")
    expect("mesh dials the loader's alias in the gateway namespace",
           endpoint == f"http://dfe-loader-mesh.{GATEWAY_NAMESPACE}.svc.cluster.local:6000",
           f"got {endpoint!r}")


def test_the_endpoint_is_a_service_the_loader_renders() -> None:
    for profile in DIRECT_PROFILES:
        endpoint = (receiver_config(profile).get("loader") or {}).get("grpc_endpoint") or ""
        parts = urlsplit(endpoint)
        labels = (parts.hostname or "").split(".")
        name, namespace = [*labels, "", ""][:2]
        services = [
            s for s in of_kind(render("dfe-loader", profile), "Service")
            if s["metadata"]["name"] == name
            and s["metadata"].get("namespace", NAMESPACE) == namespace
        ]
        expect(f"{profile}: {name}.{namespace} is a Service the loader chart renders",
               len(services) == 1, f"got {len(services)} for {endpoint!r}")
        ports = [p["port"] for s in services for p in s["spec"]["ports"]]
        expect(f"{profile}: that Service answers on {parts.port}", parts.port in ports,
               f"got {ports!r}")


def test_the_loader_listens_where_the_receiver_dials() -> None:
    """The Service is not enough: the loader has to serve gRPC on that port on slim."""
    port = urlsplit((receiver_config("slim").get("loader") or {}).get("grpc_endpoint") or "").port
    docs = render("dfe-loader", "slim")
    env = container_env(docs)
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
    port = urlsplit((receiver_config("slim").get("loader") or {}).get("grpc_endpoint") or "").port
    expect("the receiver dials apps.yaml dfe-loader.endpoints.push", port == push,
           f"got {port!r}, manifest {push!r}")


def test_the_overlay_keeps_what_it_set() -> None:
    """The engine writes destinations whole; the chart fills only what it left out."""
    overlay = {
        "destinations": {
            "default": "dfe-transform-vrl-auth",
            "rules": [],
            "dfe-transform-vrl-auth": {"grpc": {"endpoint": "http://dfe-transform-vrl-auth:6000"}},
        },
        "loader": {"grpc_endpoint": "http://loader.elsewhere:7000", "transport": "kafka"},
    }
    cfg = receiver_config("slim", *with_config(overlay))
    destinations = cfg.get("destinations") or {}
    expect("the overlay's default destination wins",
           destinations.get("default") == "dfe-transform-vrl-auth", f"got {destinations!r}")
    expect("its rules survive", destinations.get("rules") == [], f"got {destinations!r}")
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
        cfg = receiver_config(profile)
        expect(f"{profile} renders no loader block", "loader" not in cfg, f"got {cfg.get('loader')!r}")
        expect(f"{profile} renders no destinations", "destinations" not in cfg,
               f"got {cfg.get('destinations')!r}")
        env = container_env(render("dfe-receiver", profile))
        expect(f"{profile} still hands the receiver its brokers",
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
