#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_pool_listener.py
#  Purpose:      Prove every stage pool gets the same listener from the same
#                helper on mesh, gets none on the other profiles, and that
#                the receiver's buffer figures reach the config the app reads.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One pool balancer, applied to every pool.

A Kubernetes Service balances per connection and gRPC holds one open, so a
sender pins itself to one pod of the pool it dials and the replicas KEDA adds
take nothing. dfe-common.poolListener puts a Gateway API listener in front of
each pool instead -- an alias Service naming the pool, a GRPCRoute from that
name to the pool's own Service, and a policy on that route -- and the profile is
what turns it on.

What is checked here is that it is ONE helper and one profile switch: the same
three objects for every pool, no pool growing its own wiring, nothing rendered
on a profile that did not ask, and the backend declared as cleartext HTTP/2 so
the proxy does not dial it as HTTP/1.1. The route policy is checked for the two
things that decide whether a held request survives the proxy: a timeout no
shorter than the sender's deadline, and retries that never re-send a request a
pod already took.

The listener objects are dfe-extras', rendered with each pool's chartName. The
pool's own Service and the receiver's buffer are still read off its 2.2.0 chart.

    python3 scripts/tests/test_pool_listener.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
EXTRAS = CHARTS / "dfe-extras"
VALUES = REPO_ROOT / "argocd" / "values"

# Every stage a record can be sent TO on the direct transport, and the Service
# port its listener answers on. The listener is the same for all of them, which
# is the point: a new pool is a chart shipping templates/pool-listener.yaml.
POOLS = {
    "dfe-receiver": 6000,
    "dfe-loader": 6000,
    "dfe-archiver": 6000,
    "dfe-transform-vrl": 6000,
    "dfe-transform-vector": 6000,
    "dfe-transform-elastic": 6000,
}

# The one profile that balances between pools. The other three either run a
# broker (which balances its own consumers) or one pod per stage.
MESH_PROFILE = "mesh"
QUIET_PROFILES = ("slim", "single", "scale")

# Where the mesh Gateway and the alias Services live -- argocd/values/common.yaml
# `mesh.gateway.namespace`, restated here so a silent move fails the test.
GATEWAY_NAMESPACE = "envoy-gateway-system"
MESH_GATEWAY = "dfe-mesh"

# The sender's send deadline. A route timing out sooner cuts a request the next
# stage is still holding until its own ack is released.
SENDER_DEADLINE_SECONDS = 30

# The only retry triggers that mean no pod took the request.
SAFE_RETRY_TRIGGERS = ["connect-failure", "resource-exhausted"]

DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def render(chart: str, profile: str, *extra: str, path: Path | None = None) -> list[dict]:
    """One chart under one profile, with the same values cascade Argo layers."""
    cmd = [
        "helm", "template", chart, str(path or chart_dir(chart)),
        "--namespace", "dfe",
        "--set", "appNamespace=dfe",
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


def listener(chart: str, profile: str, *extra: str) -> list[dict]:
    """The DFE-only objects dfe-extras renders beside one pool's chart."""
    return render(chart, profile, "--set", f"chartName={chart}", *extra, path=EXTRAS)


def of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def seconds(duration: str) -> float:
    """A Gateway API duration ("30s", "500ms", "1m") in seconds."""
    for unit in sorted(DURATION_UNITS, key=len, reverse=True):
        number = duration.removesuffix(unit)
        if number != duration and number.replace(".", "", 1).isdigit():
            return float(number) * DURATION_UNITS[unit]
    raise ValueError(f"not a duration: {duration!r}")


def test_every_pool_gets_a_listener_on_the_mesh_profile() -> None:
    for chart, port in sorted(POOLS.items()):
        docs = listener(chart, MESH_PROFILE)
        alias = f"{chart}-mesh"

        routes = of_kind(docs, "GRPCRoute")
        expect(f"{chart} has one GRPCRoute", len(routes) == 1, f"got {len(routes)}")
        if not routes:
            continue
        route = routes[0]
        expect(f"{chart} route is named for the pool", route["metadata"]["name"] == alias,
               f"got {route['metadata']['name']!r}")

        parents = route["spec"]["parentRefs"]
        expect(f"{chart} attaches to the one mesh Gateway",
               [(p["name"], p["namespace"]) for p in parents]
               == [(MESH_GATEWAY, GATEWAY_NAMESPACE)], f"got {parents!r}")

        expect(f"{chart} matches its own alias as the authority",
               route["spec"]["hostnames"]
               == [f"{alias}.{GATEWAY_NAMESPACE}.svc.cluster.local", f"{alias}.{GATEWAY_NAMESPACE}"],
               f"got {route['spec']['hostnames']!r}")

        backends = [b for rule in route["spec"]["rules"] for b in rule["backendRefs"]]
        expect(f"{chart} routes to its own pool Service",
               [(b["name"], b["port"]) for b in backends] == [(chart, port)],
               f"got {backends!r}")

        aliases = [s for s in of_kind(docs, "Service") if s["metadata"]["name"] == alias]
        expect(f"{chart} has one alias Service", len(aliases) == 1, f"got {len(aliases)}")
        if not aliases:
            continue
        svc = aliases[0]
        expect(f"{chart} alias sits with the proxy pods",
               svc["metadata"]["namespace"] == GATEWAY_NAMESPACE,
               f"got {svc['metadata'].get('namespace')!r}")
        expect(f"{chart} alias selects the mesh Gateway's proxy",
               svc["spec"]["selector"].get("gateway.envoyproxy.io/owning-gateway-name")
               == MESH_GATEWAY, f"got {svc['spec']['selector']!r}")
        expect(f"{chart} alias answers on the pool's port",
               [p["port"] for p in svc["spec"]["ports"]] == [port],
               f"got {svc['spec']['ports']!r}")


def test_the_backend_is_declared_cleartext_http2() -> None:
    """Without appProtocol the proxy dials the pool as HTTP/1.1 and gRPC resets."""
    for chart, port in sorted(POOLS.items()):
        docs = render(chart, MESH_PROFILE)
        pool = [s for s in of_kind(docs, "Service") if s["metadata"]["name"] == chart]
        expect(f"{chart} has its own pool Service", len(pool) == 1, f"got {len(pool)}")
        if not pool:
            continue
        ports = [p for p in pool[0]["spec"]["ports"] if p["port"] == port]
        expect(f"{chart} declares h2c on {port}",
               [p.get("appProtocol") for p in ports] == ["kubernetes.io/h2c"],
               f"got {ports!r}")


def test_every_route_carries_its_own_policy() -> None:
    for chart in sorted(POOLS):
        alias = f"{chart}-mesh"
        policies = of_kind(listener(chart, MESH_PROFILE), "BackendTrafficPolicy")
        expect(f"{chart} has one route policy", len(policies) == 1, f"got {len(policies)}")
        if not policies:
            continue
        spec = policies[0]["spec"]
        expect(
            f"{chart} policy targets its own route, not the Gateway",
            spec["targetRefs"]
            == [{"group": "gateway.networking.k8s.io", "kind": "GRPCRoute", "name": alias}],
            f"got {spec['targetRefs']!r}",
        )
        expect(
            f"{chart} policy merges into the Gateway's instead of replacing it",
            spec.get("mergeType") == "StrategicMerge",
            f"got {spec.get('mergeType')!r}",
        )
        timeout = spec["timeout"]["http"]["requestTimeout"]
        expect(
            f"{chart} request timeout is at or above the sender's {SENDER_DEADLINE_SECONDS}s",
            seconds(timeout) >= SENDER_DEADLINE_SECONDS,
            f"got {timeout!r}",
        )
        triggers = spec["retry"]["retryOn"]["triggers"]
        expect(
            f"{chart} retries only where no pod took the request",
            sorted(triggers) == SAFE_RETRY_TRIGGERS,
            f"got {triggers!r}",
        )
        passive = spec["healthCheck"]["passive"]
        expect(
            f"{chart} ejects a pod that keeps answering unavailable",
            passive.get("consecutive5XxErrors", 0) > 0,
            f"got {passive!r}",
        )


def test_the_duration_reader_reads_what_the_policy_writes() -> None:
    """The timeout check is only as good as this parse, so its edges are pinned."""
    expect("30s is 30", seconds("30s") == 30, str(seconds("30s")))
    expect("500ms is half a second", seconds("500ms") == 0.5, str(seconds("500ms")))
    expect("1m is 60", seconds("1m") == 60, str(seconds("1m")))
    for bad in ("", "30", "s", "thirty s"):
        try:
            seconds(bad)
        except ValueError:
            continue
        expect(f"{bad!r} is refused", False, "parsed as a duration")


def test_another_gateway_can_leave_the_route_policy_out() -> None:
    docs = listener("dfe-loader", MESH_PROFILE, "--set", "mesh.routePolicy.enabled=false")
    expect(
        "mesh.routePolicy.enabled=false renders no policy",
        of_kind(docs, "BackendTrafficPolicy") == [],
        f"got {of_kind(docs, 'BackendTrafficPolicy')!r}",
    )
    expect(
        "and the route still renders",
        len(of_kind(docs, "GRPCRoute")) == 1,
        f"got {len(of_kind(docs, 'GRPCRoute'))}",
    )


def test_no_listener_on_a_profile_that_did_not_ask() -> None:
    for profile in QUIET_PROFILES:
        for chart in sorted(POOLS):
            docs = listener(chart, profile)
            routes = of_kind(docs, "GRPCRoute")
            expect(f"{chart} has no route on {profile}", routes == [], f"got {len(routes)}")
            policies = of_kind(docs, "BackendTrafficPolicy")
            expect(
                f"{chart} has no route policy on {profile}", policies == [], f"got {len(policies)}"
            )
            aliases = [
                s for s in of_kind(docs, "Service")
                if s["metadata"]["name"].endswith("-mesh")
            ]
            expect(f"{chart} has no alias on {profile}", aliases == [], f"got {aliases!r}")


def test_the_gateway_the_listeners_attach_to() -> None:
    docs = render("envoy-gateway-config", MESH_PROFILE)
    mesh = [g for g in of_kind(docs, "Gateway") if g["metadata"]["name"] == MESH_GATEWAY]
    expect("the mesh Gateway renders", len(mesh) == 1, f"got {len(mesh)}")
    if mesh:
        listeners = mesh[0]["spec"]["listeners"]
        expect("one listener carries every pool, told apart by route hostname",
               [(listener["name"], listener["port"]) for listener in listeners] == [("mesh", 6000)],
               f"got {listeners!r}")
        expect("its proxy parameters are its own, not the edge's",
               mesh[0]["spec"]["infrastructure"]["parametersRef"]["name"] == f"{MESH_GATEWAY}-proxy",
               f"got {mesh[0]['spec'].get('infrastructure')!r}")

    proxy = [p for p in of_kind(docs, "EnvoyProxy") if p["metadata"]["name"] == f"{MESH_GATEWAY}-proxy"]
    expect("the mesh proxy is not a LoadBalancer", bool(proxy) and
           proxy[0]["spec"]["provider"]["kubernetes"]["envoyService"]["type"] == "ClusterIP",
           f"got {proxy!r}")

    balancing = [
        p for p in of_kind(docs, "BackendTrafficPolicy")
        if p["metadata"]["name"] == f"{MESH_GATEWAY}-pools"
    ]
    expect("the pools are balanced per request", bool(balancing) and
           balancing[0]["spec"]["loadBalancer"]["type"] == "RoundRobin", f"got {balancing!r}")
    expect("and the connection is kept alive", bool(balancing) and
           "connectionKeepalive" in balancing[0]["spec"]["http2"], f"got {balancing!r}")

    senders = [
        p for p in of_kind(docs, "ClientTrafficPolicy")
        if p["metadata"]["name"] == f"{MESH_GATEWAY}-senders"
    ]
    expect("the sender side is kept alive too", bool(senders) and
           "connectionKeepalive" in senders[0]["spec"]["http2"], f"got {senders!r}")

    for profile in QUIET_PROFILES:
        quiet = render("envoy-gateway-config", profile)
        named = [d for d in quiet if d["metadata"].get("name", "").startswith(MESH_GATEWAY)]
        expect(f"nothing mesh renders on {profile}", named == [],
               f"got {[d['metadata']['name'] for d in named]}")


def test_the_senders_are_allowed_to_reach_the_listener() -> None:
    """A route nothing may connect to is a balancer that drops the data plane.

    The app namespaces' baseline egress denies what it does not name, and the
    gateway rule names the edge's two container ports only -- so the mesh port
    has to join it or every stage times out dialling the next one.
    """
    policies = [
        p for p in of_kind(render("network-policies", MESH_PROFILE), "NetworkPolicy")
        if p["metadata"]["name"] == "allow-gateway-egress"
    ]
    expect("the gateway egress policy renders", bool(policies), f"got {policies!r}")
    for policy in policies:
        ports = [p["port"] for rule in policy["spec"]["egress"] for p in rule["ports"]]
        expect(f"{policy['metadata']['namespace']} may reach the mesh listener",
               6000 in ports, f"got {ports!r}")

    quiet = [
        p for p in of_kind(render("network-policies", "scale"), "NetworkPolicy")
        if p["metadata"]["name"] == "allow-gateway-egress"
    ]
    for policy in quiet:
        ports = [p["port"] for rule in policy["spec"]["egress"] for p in rule["ports"]]
        expect("no mesh port opened where there is no mesh", 6000 not in ports, f"got {ports!r}")


def test_the_receiver_buffers_what_the_profile_says() -> None:
    """The brokerless profile's slack, in the file the app actually reads."""
    docs = render("dfe-receiver", MESH_PROFILE)
    cfg = next(
        d for d in of_kind(docs, "ConfigMap") if d["metadata"]["name"] == "dfe-receiver-config"
    )
    buffer = yaml.safe_load(cfg["data"]["config.yaml"])["buffer"]
    expect("the memory ceiling is stated, not auto-detected",
           buffer["memory_limit"] == 3221225472, f"got {buffer.get('memory_limit')!r}")
    expect("spillover is on", buffer["spillover"]["enabled"] is True, f"got {buffer!r}")

    deployment = next(d for d in of_kind(docs, "Deployment"))
    pod = deployment["spec"]["template"]["spec"]
    mounts = {m["name"]: m["mountPath"] for m in pod["containers"][0]["volumeMounts"]}
    expect("the spool path is mounted",
           mounts.get("spool") == buffer["spillover"]["path"], f"got {mounts!r}")
    volumes = {v["name"]: v for v in pod["volumes"]}
    expect("and the volume is capped",
           volumes.get("spool", {}).get("emptyDir", {}).get("sizeLimit") == "20Gi",
           f"got {volumes.get('spool')!r}")

    quiet = render("dfe-receiver", "scale")
    pod = next(d for d in of_kind(quiet, "Deployment"))["spec"]["template"]["spec"]
    expect("no spool volume where a broker holds the records",
           [v for v in pod["volumes"] if v["name"] == "spool"] == [], f"got {pod['volumes']!r}")


def main() -> int:
    with standalone():
        test_every_pool_gets_a_listener_on_the_mesh_profile()
        test_the_backend_is_declared_cleartext_http2()
        test_every_route_carries_its_own_policy()
        test_the_duration_reader_reads_what_the_policy_writes()
        test_another_gateway_can_leave_the_route_policy_out()
        test_no_listener_on_a_profile_that_did_not_ask()
        test_the_gateway_the_listeners_attach_to()
        test_the_senders_are_allowed_to_reach_the_listener()
        test_the_receiver_buffers_what_the_profile_says()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
