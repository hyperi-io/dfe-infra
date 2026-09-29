#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_otel_ingress.py
#  Purpose:      Prove OTLP ingest from outside the cluster is off by default,
#                that turning it on always brings a bearer-token receiver with
#                it, and that neither chart renders it without a token.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for the otel.ingress switch (dfe-infra#236).

An otel route on the gateway with nothing checking the sender takes telemetry
from anyone who can reach otel.<domain>, and the threat model grants that to
dfe-receiver and dfe-ui alone. One key, otel.ingress.enabled in
argocd/values/common.yaml, decides it for the two charts that take part:

- the gateway renders the otel HTTPRoute only while it is on;
- the collector then adds a second OTLP/HTTP receiver behind that route which
  admits only a bearer token, read from the deployment's secret store;
- the in-cluster receiver on 4317/4318 is the same with the switch on or off,
  so the stack's own self-monitoring does not move;
- on, with no token path, both charts refuse to render.

    python3 scripts/tests/test_otel_ingress.py

Runs standalone or under pytest. Needs `helm` on PATH.
"""

import copy
import functools
import importlib.machinery
import importlib.util
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
APPSETS = REPO_ROOT / "argocd" / "appsets"
GATEWAY = "envoy-gateway-config"
COLLECTOR = "otel-collector"

# Every cloud overlay, and the edge flavour layer2-edge.yaml layers after it.
FLAVOURS = {"local": "onprem", "local-dfe": "onprem", "aws": "aws", "gcp": "gcp", "azure": "azure"}
PROFILES = ("slim", "single", "scale", "mesh")

STORE_PATH = "dfe/local/otel/ingress"
ON = ("--set", "otel.ingress.enabled=true", "--set", f"otel.ingress.auth.remoteKey={STORE_PATH}")
ON_NO_TOKEN = ("--set", "otel.ingress.enabled=true")
NO_TOKEN = "otel.ingress.auth.remoteKey is empty"
NOT_A_BOOL = "otel.ingress.enabled is"

ROUTE = "otel"
EXTENSION = "bearertokenauth/ingress"
RECEIVER = "otlp/ingress"
PORT_NAME = "otlp-ingress"


def cascade(chart: str, cloud: str = "local", profile: str = "slim") -> tuple[str, ...]:
    """The values files the appsets layer onto this chart, in their order."""
    files = [VALUES / "common.yaml", VALUES / f"{cloud}.yaml"]
    if chart == GATEWAY:
        files.append(VALUES / f"edge-{FLAVOURS[cloud]}.yaml")
    files.append(VALUES / f"profile-{profile}.yaml")
    args: list[str] = []
    for path in files:
        args += ["-f", str(path)]
    return (
        *args,
        "--set", "appNamespace=dfe",
        "--set", "domain=dfe.example.com",
        "--set", "global.registry=ghcr.io/hyperi-io",
    )


@functools.cache
def helm(chart: str, *args: str) -> subprocess.CompletedProcess:
    """helm template of one chart with these args, rendered or not."""
    namespace = "envoy-gateway-system" if chart == GATEWAY else "otel"
    return subprocess.run(
        ["helm", "template", chart, str(chart_dir(chart)), "--namespace", namespace, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def render(chart: str, *args: str) -> tuple[dict, ...]:
    """The chart's documents under the default cascade plus these args."""
    out = helm(chart, *cascade(chart), *args)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart}:\n{out.stderr}")
    return tuple(d for d in yaml.safe_load_all(out.stdout) if d)


def refusal(chart: str, *args: str) -> str:
    """helm's error when the chart refuses these values; empty when it renders."""
    out = helm(chart, *cascade(chart), *args)
    return out.stderr.strip() if out.returncode != 0 else ""


def of_kind(docs: Iterable[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def named(docs: Iterable[dict], kind: str, name: str) -> dict | None:
    found = [d for d in of_kind(docs, kind) if d["metadata"]["name"] == name]
    return found[0] if len(found) == 1 else None


def collector_config(docs: Iterable[dict]) -> dict:
    """The gateway collector's own config, as the chart writes it into its ConfigMap."""
    maps = [d for d in of_kind(docs, "ConfigMap") if "gateway-config.yaml" in (d.get("data") or {})]
    if len(maps) != 1:
        raise SystemExit(f"the collector wrote {len(maps)} ConfigMaps carrying gateway-config.yaml")
    return yaml.safe_load(maps[0]["data"]["gateway-config.yaml"])


def gateway_pod(docs: Iterable[dict]) -> dict:
    """The collector gateway Deployment's pod spec."""
    deployments = [d for d in of_kind(docs, "Deployment") if d["metadata"]["name"].endswith("-gateway")]
    if len(deployments) != 1:
        raise SystemExit(f"the collector rendered {len(deployments)} gateway Deployments")
    return deployments[0]["spec"]["template"]["spec"]


def gateway_service(docs: Iterable[dict]) -> dict:
    services = [d for d in of_kind(docs, "Service") if d["metadata"]["name"].endswith("-gateway")]
    if len(services) != 1:
        raise SystemExit(f"the collector rendered {len(services)} gateway Services")
    return services[0]


def service_ports(docs: Iterable[dict]) -> dict[str, dict]:
    return {p["name"]: p for p in gateway_service(docs)["spec"]["ports"]}


def container_ports(docs: Iterable[dict]) -> dict[str, int]:
    container = gateway_pod(docs)["containers"][0]
    return {p["name"]: p["containerPort"] for p in container.get("ports") or []}


def routes(docs: Iterable[dict]) -> set[str]:
    return {d["metadata"]["name"] for d in of_kind(docs, "HTTPRoute")}


# --- off by default --------------------------------------------------------------
def test_no_tracked_cascade_publishes_the_otel_route() -> None:
    """Every cloud overlay on every profile, the way layer2-edge.yaml layers them."""
    for cloud in FLAVOURS:
        for profile in PROFILES:
            out = helm(GATEWAY, *cascade(GATEWAY, cloud, profile))
            if out.returncode != 0:
                expect(f"the gateway renders [{cloud} {profile}]", False, out.stderr.strip())
                continue
            served = routes(d for d in yaml.safe_load_all(out.stdout) if d)
            expect(f"no otel route [{cloud} {profile}]", ROUTE not in served, f"got {sorted(served)}")


def test_off_the_collector_carries_no_ingress_plumbing() -> None:
    for profile in PROFILES:
        docs = tuple(
            d for d in yaml.safe_load_all(helm(COLLECTOR, *cascade(COLLECTOR, "local", profile)).stdout)
            if d
        )
        config = collector_config(docs)
        expect(f"no token extension [{profile}]", EXTENSION not in (config.get("extensions") or {}),
               f"got {sorted(config.get('extensions') or {})}")
        expect(f"no ingress receiver [{profile}]", RECEIVER not in config["receivers"],
               f"got {sorted(config['receivers'])}")
        expect(f"no ExternalSecret [{profile}]", not of_kind(docs, "ExternalSecret"),
               f"got {[d['metadata']['name'] for d in of_kind(docs, 'ExternalSecret')]}")
        expect(f"no ingress port on the Service [{profile}]", PORT_NAME not in service_ports(docs),
               f"got {sorted(service_ports(docs))}")
        expect(f"no ingress port on the pod [{profile}]", PORT_NAME not in container_ports(docs),
               f"got {sorted(container_ports(docs))}")


# --- the in-cluster receiver does not move --------------------------------------
def test_the_in_cluster_receiver_is_the_same_on_or_off() -> None:
    """Every DFE app, KEDA and HyperDX push to 4317/4318 with no token, on or off."""
    off, on = render(COLLECTOR), render(COLLECTOR, *ON)
    otlp_off, otlp_on = collector_config(off)["receivers"]["otlp"], collector_config(on)["receivers"]["otlp"]
    expect("the otlp receiver is identical with the switch on", otlp_off == otlp_on,
           f"off {otlp_off} / on {otlp_on}")
    protocols = otlp_on.get("protocols") or {}
    expect("it still serves gRPC on 4317 and HTTP on 4318",
           (protocols.get("grpc") or {}).get("endpoint") == "0.0.0.0:4317"
           and (protocols.get("http") or {}).get("endpoint") == "0.0.0.0:4318",
           f"got {protocols}")
    expect("and neither protocol asks a sender for a token",
           all("auth" not in (p or {}) for p in protocols.values()), f"got {protocols}")
    for name in ("otlp-grpc", "otlp-http"):
        expect(f"the Service's {name} port is the same with the switch on",
               service_ports(off).get(name) == service_ports(on).get(name),
               f"off {service_ports(off).get(name)} / on {service_ports(on).get(name)}")
    pipelines = collector_config(on)["service"]["pipelines"]
    for signal in ("traces", "metrics", "logs"):
        expect(f"the {signal} pipeline still takes the otlp receiver",
               "otlp" in pipelines[signal]["receivers"], f"got {pipelines[signal]['receivers']}")


# --- on: the route, and the receiver behind it -----------------------------------
def test_the_switch_publishes_the_route_onto_the_authenticated_port() -> None:
    route = named(render(GATEWAY, *ON), "HTTPRoute", ROUTE)
    expect("the otel route renders with the switch on", route is not None, "absent")
    if route is None:
        return
    expect("it lives in the collector's namespace", route["metadata"]["namespace"] == "otel",
           f"got {route['metadata']['namespace']}")
    expect("on otel.<domain>", route["spec"]["hostnames"] == ["otel.dfe.example.com"],
           f"got {route['spec']['hostnames']}")
    expect("pinned to the https listener",
           [p.get("sectionName") for p in route["spec"]["parentRefs"]] == ["https"],
           f"got {route['spec']['parentRefs']}")
    backends = [b for rule in route["spec"]["rules"] for b in rule["backendRefs"]]
    ingress = service_ports(render(COLLECTOR, *ON)).get(PORT_NAME) or {}
    expect("its one backend is the collector Service's authenticated port",
           [(b["name"], b["port"]) for b in backends]
           == [(gateway_service(render(COLLECTOR, *ON))["metadata"]["name"], ingress.get("port"))],
           f"route {backends} / collector {ingress}")
    expect("which is not the in-cluster OTLP/HTTP port",
           all(b["port"] != 4318 for b in backends), f"got {backends}")


def test_the_route_and_the_receiver_share_one_port() -> None:
    """One key, otel.ingress.port, so the route cannot aim at a port nobody serves."""
    moved = (*ON, "--set", "otel.ingress.port=4400")
    route = named(render(GATEWAY, *moved), "HTTPRoute", ROUTE) or {"spec": {"rules": []}}
    ports = [b["port"] for rule in route["spec"]["rules"] for b in rule["backendRefs"]]
    docs = render(COLLECTOR, *moved)
    receiver = collector_config(docs)["receivers"].get(RECEIVER) or {}
    endpoint = ((receiver.get("protocols") or {}).get("http") or {}).get("endpoint")
    expect("the route follows the port", ports == [4400], f"got {ports}")
    expect("the receiver listens on it", endpoint == "0.0.0.0:4400", f"got {endpoint}")
    expect("the pod exposes it", container_ports(docs).get(PORT_NAME) == 4400,
           f"got {container_ports(docs)}")
    expect("the Service carries it", (service_ports(docs).get(PORT_NAME) or {}).get("port") == 4400,
           f"got {service_ports(docs)}")


def test_on_the_ingress_receiver_admits_only_a_bearer_token() -> None:
    config = collector_config(render(COLLECTOR, *ON))
    extension = (config.get("extensions") or {}).get(EXTENSION) or {}
    expect("the bearer-token extension renders, reading a file",
           set(extension) == {"filename"} and str(extension.get("filename", "")).startswith("/"),
           f"got {extension}")
    receiver = config["receivers"].get(RECEIVER) or {}
    expect(
        "the ingress receiver is OTLP/HTTP alone, and every request goes through the extension",
        receiver == {"protocols": {"http": {
            "endpoint": "0.0.0.0:4319", "auth": {"authenticator": EXTENSION},
        }}},
        f"got {receiver}",
    )
    expect("the extension is started", EXTENSION in config["service"]["extensions"],
           f"got {config['service']['extensions']}")
    pipelines = config["service"]["pipelines"]
    for signal in ("traces", "metrics", "logs"):
        expect(f"the {signal} pipeline takes the ingress receiver",
               RECEIVER in pipelines[signal]["receivers"], f"got {pipelines[signal]['receivers']}")


def test_on_the_token_comes_from_the_secret_store_into_the_file_the_receiver_reads() -> None:
    docs = render(COLLECTOR, *ON)
    secrets = of_kind(docs, "ExternalSecret")
    expect("one ExternalSecret renders", len(secrets) == 1, f"got {len(secrets)}")
    if len(secrets) != 1:
        return
    spec = secrets[0]["spec"]
    expect("it reads the deployment's ClusterSecretStore",
           spec["secretStoreRef"] == {"name": "dfe-secret-store", "kind": "ClusterSecretStore"},
           f"got {spec['secretStoreRef']}")
    expect("at the configured path, into the key the file is named after",
           spec["data"] == [{"secretKey": "token",
                             "remoteRef": {"key": STORE_PATH, "property": "token"}}],
           f"got {spec['data']}")
    target = spec["target"]["name"]
    pod = gateway_pod(docs)
    volumes = [v for v in pod.get("volumes") or [] if (v.get("secret") or {}).get("secretName") == target]
    expect("the gateway pod mounts that Secret", len(volumes) == 1, f"got {pod.get('volumes')}")
    if len(volumes) != 1:
        return
    mounts = [m for m in pod["containers"][0].get("volumeMounts") or []
              if m["name"] == volumes[0]["name"]]
    filename = PurePosixPath(collector_config(docs)["extensions"][EXTENSION]["filename"])
    expect("read-only, at the directory the extension reads its token file from",
           len(mounts) == 1 and mounts[0].get("readOnly") is True
           and PurePosixPath(mounts[0]["mountPath"]) == filename.parent
           and filename.name == "token",
           f"mounts {mounts}, filename {filename}")


# --- on with no token: refused ---------------------------------------------------
def test_on_with_no_token_path_refuses_in_both_charts() -> None:
    for chart in (GATEWAY, COLLECTOR):
        for shape, args in (
            ("unset", ON_NO_TOKEN),
            ("empty", (*ON_NO_TOKEN, "--set", "otel.ingress.auth.remoteKey=")),
        ):
            error = refusal(chart, *args)
            expect(f"{chart} refuses [{shape}]", NO_TOKEN in error, f"got {error or 'a render'}")


def test_a_quoted_switch_is_refused_in_both_charts() -> None:
    """A quoted "false" is a non-empty string, and truthy, so it would publish the route."""
    for chart in (GATEWAY, COLLECTOR):
        for value in ("false", "true"):
            error = refusal(chart, "--set-string", f"otel.ingress.enabled={value}")
            expect(f"{chart} refuses a quoted {value}", NOT_A_BOOL in error and "not a bool" in error,
                   f"got {error or 'a render'}")


# --- one key, both charts --------------------------------------------------------
def test_one_switch_reaches_both_charts() -> None:
    common = yaml.safe_load((VALUES / "common.yaml").read_text(encoding="utf-8"))
    ingress = (common.get("otel") or {}).get("ingress") or {}
    expect("common.yaml declares the switch, off", ingress.get("enabled") is False, f"got {ingress}")
    expect("and the shared port and token path",
           isinstance(ingress.get("port"), int) and (ingress.get("auth") or {}).get("remoteKey") == "",
           f"got {ingress}")
    for chart in (GATEWAY, COLLECTOR):
        values = yaml.safe_load((chart_dir(chart) / "values.yaml").read_text(encoding="utf-8"))
        own = (values.get("otel") or {}).get("ingress") or {}
        expect(f"{chart} defaults the switch off", own.get("enabled") is False, f"got {own}")
        expect(f"{chart} defaults the same port", own.get("port") == ingress.get("port"),
               f"got {own.get('port')}")


def test_every_appset_rendering_either_chart_layers_the_deployers_common_yaml() -> None:
    """The deploy repo turns it on once, in infra/common.yaml, and both charts see it."""
    wanted = {GATEWAY: ("layer2-edge.yaml", "layer2-platform.yaml"), COLLECTOR: ("layer2-data.yaml",)}
    for app, files in wanted.items():
        for filename in files:
            found = False
            for doc in yaml.safe_load_all((APPSETS / filename).read_text(encoding="utf-8")):
                if not doc or doc.get("kind") != "ApplicationSet":
                    continue
                apps = {
                    element.get("app")
                    for generator in doc["spec"]["generators"]
                    for child in generator.get("matrix", {}).get("generators", [])
                    for element in child.get("list", {}).get("elements", [])
                }
                if app not in apps:
                    continue
                found = True
                value_files = [
                    f
                    for source in doc["spec"]["template"]["spec"]["sources"]
                    for f in (source.get("helm") or {}).get("valueFiles") or []
                ]
                expect(f"{filename} layers infra/common.yaml onto {app}",
                       "$values/infra/common.yaml" in value_files, f"got {value_files}")
            expect(f"{filename} generates {app}", found, "no ApplicationSet lists it")


def _load_dfe_ops():
    """Import dfe-ops as a module -- it has no .py extension, so no import finds it."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    spec = importlib.util.spec_from_loader(
        "dfe_ops", importlib.machinery.SourceFileLoader("dfe_ops", str(REPO_ROOT / "scripts" / "dfe-ops"))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_token_counts_among_what_reads_the_secret_store() -> None:
    """stack-deploy creates no store for a deployment it believes reads nothing from one."""
    ops = _load_dfe_ops()
    tracked = ops._chart_values

    def switched_on(chart: Path, mode: str, cloud: str = "") -> dict:
        values = copy.deepcopy(tracked(chart, mode, cloud))
        if chart.name == COLLECTOR:
            values["otel"]["ingress"]["enabled"] = True
        return values

    expect("off, the token is not a store consumer",
           not [u for u in ops._store_consumers("slim", {}) if "OTLP" in u],
           f"got {ops._store_consumers('slim', {})}")
    ops._chart_values = switched_on
    try:
        uses = ops._store_consumers("slim", {})
    finally:
        ops._chart_values = tracked
    expect("on, it is", any("OTLP ingress bearer token" in u for u in uses), f"got {uses}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
