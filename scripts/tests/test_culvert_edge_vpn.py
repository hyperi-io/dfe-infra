#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_culvert_edge_vpn.py
#  Purpose:      Prove the edge-fleet VPN reaches the receiver and nothing else,
#                that its tunnels sit in the reserved client range, and that the
#                receiver's exposure seam has a third rendering for it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What a tunnelled client can and cannot reach.

The VPN is the one workload whose whole point is carrying traffic from outside
the cluster to one stage inside it, so the two things worth asserting are the
ports it opens to the world and the ports it may open in the other direction.
Both are policy, and both are silent when wrong: a NetworkPolicy naming the
wrong label denies nothing and reports nothing.

The receiver-only rule only bites because the VPN pods opt OUT of the
namespace-wide egress baseline; that opt-out is asserted here too, since
without it the rule is decoration.

    python3 scripts/tests/test_culvert_edge_vpn.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import ipaddress
import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
VALUES = REPO_ROOT / "argocd" / "values"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import composition  # noqa: E402
import profiles  # noqa: E402

# RFC 6598. Every tunnel subnet has to sit inside it, so a client address can
# never collide with the cluster's pod or service ranges.
RESERVED = ipaddress.ip_network("100.64.0.0/10")

# The receiver's ingest listeners. The Push door on 6000 is east-west only and
# must NOT appear in the VPN's egress.
INGEST_PORTS = {8080, 8443}
PUSH_PORT = 6000


def render(chart: str, *args: str) -> str:
    cmd = [
        "helm", "template", chart, str(CHARTS / chart),
        "-f", str(VALUES / "common.yaml"),
        *args,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart} {args}:\n{out.stderr}")
    return out.stdout


def fails(chart: str, *args: str) -> str:
    cmd = ["helm", "template", chart, str(CHARTS / chart), "-f", str(VALUES / "common.yaml"), *args]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode == 0:
        raise SystemExit(f"helm template was expected to fail for {chart} {args}")
    return out.stderr


def docs(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if d]


def one(text: str, kind: str, name: str) -> dict:
    for doc in docs(text):
        if doc.get("kind") == kind and doc["metadata"]["name"] == name:
            return doc
    raise SystemExit(f"no {kind}/{name} in the render")


def env_of(deployment: dict) -> dict[str, str]:
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value", "") for e in container["env"]}


def test_the_tunnels_sit_in_the_reserved_client_range() -> None:
    env = env_of(one(render("culvert"), "Deployment", "dfe-culvert"))
    subnets = {
        "openvpn-udp": f"{env['CULVERT_UDP_NETWORK']}/24",
        "wireguard": env["CULVERT_WG_NETWORK"],
    }
    for name, cidr in sorted(subnets.items()):
        net = ipaddress.ip_network(cidr)
        expect(f"the {name} tunnel is inside the reserved range",
               net.subnet_of(RESERVED), f"{name} is {cidr}")
    expect("the two tunnels do not overlap",
           not ipaddress.ip_network(subnets["openvpn-udp"]).overlaps(
               ipaddress.ip_network(subnets["wireguard"])),
           f"{subnets}")


def test_the_client_range_is_declared_once() -> None:
    """The chart carves its subnets out of the deploy-config SSoT's value."""
    common = yaml.safe_load((VALUES / "common.yaml").read_text(encoding="utf-8"))
    expect("common.yaml declares the client range",
           common.get("vpn", {}).get("clientCIDR") == "100.64.0.0/10",
           f"got {common.get('vpn')!r}")
    env = env_of(one(render("culvert", "--set", "vpn.clientCIDR=10.99.0.0/16"),
                     "Deployment", "dfe-culvert"))
    expect("moving it moves every tunnel",
           env["CULVERT_UDP_NETWORK"] == "10.99.0.0" and env["CULVERT_WG_NETWORK"] == "10.99.2.0/24",
           f"{env['CULVERT_UDP_NETWORK']} / {env['CULVERT_WG_NETWORK']}")
    err = fails("culvert", "--set", "vpn.clientCIDR=10.99.0.0/24")
    expect("a range with no room for the carve is refused", "leaves no room" in err, err.strip()[-200:])


def test_both_protocols_are_public_and_the_metrics_port_is_not() -> None:
    rendered = render("culvert")
    public = one(rendered, "Service", "dfe-culvert-public-udp")
    ports = {(p["name"], p["port"], p["protocol"]) for p in public["spec"]["ports"]}
    expect("WireGuard and OpenVPN are both on the public LB",
           ports == {("wireguard", 51820, "UDP"), ("openvpn-udp", 1194, "UDP")}, f"got {ports}")
    expect("nothing else is exposed",
           not any(d["metadata"]["name"] == "dfe-culvert-public"
                   for d in docs(rendered) if d.get("kind") == "Service"),
           "a TCP LoadBalancer rendered with no exposed TCP listener")
    internal = one(rendered, "Service", "dfe-culvert")
    expect("the metrics port is on the internal Service only",
           any(p["name"] == "metrics" for p in internal["spec"]["ports"]),
           f"{internal['spec']['ports']}")


def test_the_source_range_dial_reaches_the_load_balancer() -> None:
    rendered = render("culvert", "--set", "exposure.loadBalancerSourceRanges={203.0.113.0/24}")
    public = one(rendered, "Service", "dfe-culvert-public-udp")
    expect("the allow-list lands on the Service",
           public["spec"].get("loadBalancerSourceRanges") == ["203.0.113.0/24"],
           f"got {public['spec'].get('loadBalancerSourceRanges')!r}")


def test_the_pods_reach_the_receiver_and_nothing_else() -> None:
    policy = one(render("culvert"), "NetworkPolicy", "dfe-culvert")
    egress = policy["spec"]["egress"]
    to_pods = [rule for rule in egress
               if any("podSelector" in peer for peer in rule.get("to") or [])]
    expect("exactly one rule names a pod", len(to_pods) == 1, f"got {len(to_pods)}")
    rule = to_pods[0]
    expect("and it names the receiver",
           rule["to"][0]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "dfe-receiver"},
           f"got {rule['to'][0]!r}")
    ports = {p["port"] for p in rule["ports"]}
    expect("on the ingest listeners", ports == INGEST_PORTS, f"got {ports}")
    expect("never the east-west Push door", PUSH_PORT not in ports, f"got {ports}")
    # The remaining rules are DNS and the collector. Anything else in this list
    # is a destination a tunnelled client can reach.
    open_ports = {p["port"] for rule in egress for p in rule.get("ports") or []}
    expect("nothing beyond the receiver, DNS and telemetry",
           open_ports == INGEST_PORTS | {53, 4317}, f"got {open_ports}")


def test_the_pods_opt_out_of_the_namespace_baseline() -> None:
    """Policies are additive: without the opt-out the rule above adds nothing."""
    labels = one(render("culvert"), "Deployment", "dfe-culvert")[
        "spec"]["template"]["metadata"]["labels"]
    expect("the pods carry the opt-out label",
           labels.get("dfe.hyperi.io/egress-scoped") == "true", f"got {labels!r}")
    baseline = next(
        d for d in docs(render("network-policies"))
        if d.get("kind") == "NetworkPolicy" and d["metadata"]["name"] == "allow-baseline-egress"
    )
    expect("and the baseline honours it",
           baseline["spec"]["podSelector"].get("matchExpressions") == [
               {"key": "dfe.hyperi.io/egress-scoped", "operator": "DoesNotExist"}],
           f"got {baseline['spec']['podSelector']!r}")


def test_it_holds_at_one_replica() -> None:
    err = fails("culvert", "--set", "replicas=2")
    expect("a second replica is refused", "WireGuard mints its server key" in err,
           err.strip()[-200:])


def test_it_rolls_surge_first_on_a_config_change() -> None:
    """The one deployment mechanism: a config change checksums into the pod template."""
    plain = one(render("culvert"), "Deployment", "dfe-culvert")
    expect("it replaces surge-first",
           plain["spec"]["strategy"] == {
               "type": "RollingUpdate",
               "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}},
           f"got {plain['spec']['strategy']!r}")
    before = plain["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    after = one(render("culvert", "--set", "tuning.tun_mtu=1400"),
                "Deployment", "dfe-culvert")["spec"]["template"]["metadata"]["annotations"]
    expect("a tuning change moves the checksum",
           after["checksum/config"] != before, f"{before} == {after['checksum/config']}")
    durable = one(render("culvert", "--set", "persistence.enabled=true"),
                  "Deployment", "dfe-culvert")
    expect("a durable PKI volume replaces in place instead",
           durable["spec"]["strategy"] == {"type": "Recreate"},
           f"got {durable['spec']['strategy']!r}")


def test_it_renders_on_every_profile_it_is_offered_in() -> None:
    """The scale profiles set replicaCount 2 for every app; this one cannot take it."""
    for profile in ("scale", "mesh"):
        text = render("culvert", "-f", str(VALUES / f"profile-{profile}.yaml"))
        deployment = one(text, "Deployment", "dfe-culvert")
        expect(f"{profile} still renders one replica",
               deployment["spec"]["replicas"] == 1, f"got {deployment['spec']['replicas']}")


def test_the_receiver_gains_a_third_exposure_rendering() -> None:
    vpn = render("dfe-receiver", "--set", "exposure.mode=vpn")
    services = [d["metadata"]["name"] for d in docs(vpn) if d.get("kind") == "Service"]
    expect("vpn mode publishes no LoadBalancer", services == ["dfe-receiver"], f"got {services}")
    policy = one(vpn, "NetworkPolicy", "dfe-receiver-ingest")
    rule = policy["spec"]["ingress"][0]
    expect("and admits the tunnel pods by label",
           rule["from"] == [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "dfe-culvert"}}}],
           f"got {rule.get('from')!r}")
    expect("on the exposed ingest ports",
           {p["port"] for p in rule["ports"]} == INGEST_PORTS,
           f"got {[p['port'] for p in rule['ports']]}")
    public = one(render("dfe-receiver"), "NetworkPolicy", "dfe-receiver-ingest")
    expect("public mode still names no source",
           "from" not in public["spec"]["ingress"][0], f"got {public['spec']['ingress'][0]!r}")
    # Internal mode still refuses a listener the cluster Gateway cannot route,
    # and vpn mode deliberately does not -- the tunnel reaches every listener.
    err = fails("dfe-receiver", "--set", "exposure.mode=internal")
    expect("internal mode still refuses an unroutable listener",
           'exposure.mode is "internal"' in err, err.strip()[-200:])
    http_only = '[{"name":"http","port":8080,"protocol":"TCP","exposed":true}]'
    internal = [d for d in docs(render("dfe-receiver", "--set", "exposure.mode=internal",
                                       "--set-json", f"listeners={http_only}"))
                if d.get("kind") == "NetworkPolicy"]
    expect("internal mode renders no ingest policy", internal == [], f"got {internal}")


def test_the_vpn_is_not_seeded_into_any_deployment() -> None:
    """Opt-in means a values file an operator adds, never a seeded default."""
    for profile in profiles.PROFILE_NAMES:
        seeded = composition.default_apps(profile)
        expect(
            f"culvert is not in {profile}'s default composition",
            "culvert" not in seeded,
            f"got {seeded}",
        )


def main() -> int:
    with standalone():
        test_the_tunnels_sit_in_the_reserved_client_range()
        test_the_client_range_is_declared_once()
        test_both_protocols_are_public_and_the_metrics_port_is_not()
        test_the_source_range_dial_reaches_the_load_balancer()
        test_the_pods_reach_the_receiver_and_nothing_else()
        test_the_pods_opt_out_of_the_namespace_baseline()
        test_it_holds_at_one_replica()
        test_it_rolls_surge_first_on_a_config_change()
        test_it_renders_on_every_profile_it_is_offered_in()
        test_the_receiver_gains_a_third_exposure_rendering()
        test_the_vpn_is_not_seeded_into_any_deployment()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
