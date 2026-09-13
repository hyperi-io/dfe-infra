#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_pinned_addresses.py
#  Purpose:      Prove a deployment can pin the addresses its DNS names, and that
#                a profile-tagged domain is derived from the base domain.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The front-door address pins and the profile-tagged domain.

Nothing pinned the LoadBalancer addresses, so a rebuild took whatever the pool
handed out and the published hostnames pointed at the address the previous build
held. And one estate domain shared by slim, single and scale means the three
profiles claim the same records, on a cluster that runs one at a time.

    python3 scripts/tests/test_pinned_addresses.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
GATEWAY = CHARTS / "envoy-gateway-config"
RECEIVER = CHARTS / "dfe-receiver"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
METALLB_POOL = REPO_ROOT / "bootstrap" / "templates" / "metallb-pool.yaml.tpl"
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
ENV_EXAMPLE = REPO_ROOT / "bootstrap" / "local.env.example"
APPSETS = REPO_ROOT / "argocd" / "appsets"

_loader = importlib.machinery.SourceFileLoader("dfeops", str(REPO_ROOT / "scripts" / "dfe-ops"))
_spec = importlib.util.spec_from_loader("dfeops", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops"] = dfeops
_loader.exec_module(dfeops)


def render(chart: Path, *sets: str) -> list[dict]:
    cmd = ["helm", "template", chart.name, str(chart)]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart.name} {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def one(docs: list[dict], kind: str, name: str) -> dict:
    hits = [d for d in docs if d.get("kind") == kind and d["metadata"]["name"] == name]
    if len(hits) != 1:
        raise SystemExit(f"expected exactly one {kind}/{name}, got {len(hits)}")
    return hits[0]


def envoy_service_patch(docs: list[dict]) -> dict:
    """The Service fields the EnvoyProxy sets, dedicated v1.8 fields and the legacy patch merged."""
    svc = one(docs, "EnvoyProxy", "dfe-envoy-proxy")["spec"]["provider"]["kubernetes"][
        "envoyService"
    ]
    return {**svc.get("patch", {}).get("value", {}).get("spec", {}), **svc}


# --- the gateway's address ---------------------------------------------------
def test_an_unset_gateway_address_leaves_the_pool_to_choose() -> None:
    patch = envoy_service_patch(render(GATEWAY))
    expect("the default EnvoyProxy pins no address", "loadBalancerIP" not in patch,
           f"got {patch}")


def test_a_set_gateway_address_reaches_the_generated_service() -> None:
    patch = envoy_service_patch(
        render(GATEWAY, "envoyGateway.service.loadBalancerIP=192.0.2.10")
    )
    expect("the configured address is patched onto the Service",
           patch.get("loadBalancerIP") == "192.0.2.10", f"got {patch}")


def test_the_address_is_not_pinned_on_a_service_that_has_none() -> None:
    """A ClusterIP front door has no address to hand out, so the field is wrong."""
    patch = envoy_service_patch(
        render(GATEWAY, "envoyGateway.service.type=ClusterIP",
               "envoyGateway.service.loadBalancerIP=192.0.2.10")
    )
    expect("a ClusterIP gateway pins nothing", "loadBalancerIP" not in patch, f"got {patch}")


def test_the_address_does_not_displace_the_other_lb_fields() -> None:
    patch = envoy_service_patch(
        render(GATEWAY,
               "envoyGateway.service.loadBalancerIP=192.0.2.10",
               "envoyGateway.service.loadBalancerClass=metallb.universe.tf/metallb",
               "envoyGateway.service.loadBalancerSourceRanges[0]=192.0.2.0/24")
    )
    expect("address, class and source ranges render together",
           (patch.get("loadBalancerIP"), patch.get("loadBalancerClass"),
            patch.get("loadBalancerSourceRanges")) ==
           ("192.0.2.10", "metallb.universe.tf/metallb", ["192.0.2.0/24"]),
           f"got {patch}")


# --- the receiver's address --------------------------------------------------
def test_an_unset_receiver_address_leaves_the_pool_to_choose() -> None:
    svc = one(render(RECEIVER), "Service", "dfe-receiver-public")
    expect("the default public Service pins no address",
           "loadBalancerIP" not in svc["spec"], f"got {svc['spec']}")


def test_a_set_receiver_address_reaches_the_public_service() -> None:
    docs = render(RECEIVER, "exposure.public.loadBalancerIP=192.0.2.11")
    svc = one(docs, "Service", "dfe-receiver-public")
    expect("the configured address is on the public TCP Service",
           svc["spec"].get("loadBalancerIP") == "192.0.2.11", f"got {svc['spec']}")


def test_the_udp_door_takes_its_own_address() -> None:
    """Two Services cannot hold one address, so only the TCP door is pinned."""
    docs = render(
        RECEIVER,
        "exposure.public.loadBalancerIP=192.0.2.11",
        "listeners[0].name=http", "listeners[0].port=8080",
        "listeners[0].protocol=TCP", "listeners[0].exposed=true",
        "listeners[1].name=netflow", "listeners[1].port=2055",
        "listeners[1].protocol=UDP", "listeners[1].exposed=true",
    )
    udp = one(docs, "Service", "dfe-receiver-public-udp")
    expect("the UDP LoadBalancer is not pinned to the TCP address",
           "loadBalancerIP" not in udp["spec"], f"got {udp['spec']}")


def test_an_internal_receiver_renders_no_public_service_at_all() -> None:
    docs = render(RECEIVER, "exposure.mode=internal",
                  "exposure.public.loadBalancerIP=192.0.2.11",
                  "listeners[0].name=http", "listeners[0].port=8080",
                  "listeners[0].protocol=TCP", "listeners[0].exposed=true")
    names = [d["metadata"]["name"] for d in docs if d.get("kind") == "Service"]
    expect("internal exposure renders the ClusterIP Service alone",
           names == ["dfe-receiver"], f"got {names}")


# --- the values reach the charts from the deploy's own env -------------------
def test_the_cluster_secret_carries_both_addresses() -> None:
    body = CLUSTER_SECRET.read_text(encoding="utf-8")
    for annotation, var in (
        ("dfe.hyperi.io/gateway_address", "${DFE_GATEWAY_IP}"),
        ("dfe.hyperi.io/receiver_address", "${DFE_RECEIVER_IP}"),
    ):
        expect(f"{annotation} is set from {var}", f'{annotation}: "{var}"' in body,
               "annotation missing from the template")


def test_the_appsets_pass_the_addresses_to_the_charts() -> None:
    wanted = {
        "layer2-platform.yaml": (
            "envoyGateway.service.loadBalancerIP", "dfe.hyperi.io/gateway_address"),
        "layer2-apps.yaml": (
            "exposure.public.loadBalancerIP", "dfe.hyperi.io/receiver_address"),
    }
    for filename, (param, annotation) in wanted.items():
        appset = yaml.safe_load((APPSETS / filename).read_text(encoding="utf-8"))
        params = {}
        for source in appset["spec"]["template"]["spec"]["sources"]:
            for entry in source.get("helm", {}).get("parameters", []):
                params[entry["name"]] = entry["value"]
        expect(f"{filename} sets {param}", param in params, f"got {sorted(params)}")
        expect(f"{filename} reads it from {annotation}",
               annotation in params.get(param, ""), f"got {params.get(param)}")


def test_the_env_contract_documents_the_new_keys() -> None:
    body = ENV_EXAMPLE.read_text(encoding="utf-8")
    for key in ("DFE_BASE_DOMAIN", "DFE_GATEWAY_IP", "DFE_RECEIVER_IP"):
        expect(f"local.env.example declares {key}", f"{key}=" in body, "key missing")


# --- something on-prem has to be able to hand the addresses out --------------
def test_the_metallb_pool_holds_the_two_pinned_addresses() -> None:
    """A pinned loadBalancerIP is unassignable unless a pool holds that address."""
    body = METALLB_POOL.read_text(encoding="utf-8")
    for var in ("${DFE_GATEWAY_IP}", "${DFE_RECEIVER_IP}"):
        expect(f"the pool holds {var} as a /32", f'"{var}/32"' in body, "address missing")
    expect("and hands neither out to a Service that did not ask for it",
           "autoAssign: false" in body, "the pool auto-assigns")
    expect("the pool is advertised on layer 2",
           "kind: L2Advertisement" in body, "nothing advertises the pool")


def test_bootstrap_installs_metallb_on_prem_only() -> None:
    """A cloud target already has a LoadBalancer controller; two would fight."""
    body = BOOTSTRAP.read_text(encoding="utf-8")
    # A deployment picks its own on-prem name, so the gate lists the clouds.
    expect("the clouds are declared in one place",
           'DFE_CLOUD_LB_PROVIDERS="aws gcp az azure"' in body, "the list is missing")
    expect("the MetalLB step asks that list",
           "if dfe_cloud_programs_loadbalancers; then" in body, "the gate is missing")
    expect("the chart version comes from versions.yaml, not the script",
           "bootstrap.metallb" in body and '--version "${METALLB_VERSION}"' in body,
           "the version is not read from the SSoT")


def test_preflight_previews_metallb_off_the_same_list() -> None:
    """Cluster B is local-dfe: a preview keyed on the literal 'local' warned where it installs."""
    for cloud in ("aws", "gcp", "az", "azure"):
        expect(f"{cloud} programs its own LoadBalancers", dfeops._cloud_programs_loadbalancers(cloud))
    for cloud in ("local", "local-dfe", "tyrell", ""):
        expect(f"{cloud!r} does not", not dfeops._cloud_programs_loadbalancers(cloud))


def test_bootstrap_applies_the_pool_only_with_both_addresses() -> None:
    """Half a pool is a bare /32, which MetalLB rejects and the deploy needs."""
    body = BOOTSTRAP.read_text(encoding="utf-8")
    expect("bootstrap.sh renders the pool template",
           "metallb-pool.yaml.tpl" in body, "the template is never applied")
    expect("and refuses to render it with either address unset",
           'if [[ -z "${DFE_GATEWAY_IP}" ]] || [[ -z "${DFE_RECEIVER_IP}" ]]; then' in body,
           "the guard is missing")


def test_destroy_leaves_the_loadbalancer_provider_alone() -> None:
    """destroy.sh cannot tell an adopted MetalLB from one it installed."""
    body = (REPO_ROOT / "bootstrap" / "destroy.sh").read_text(encoding="utf-8")
    deletes = [ln.strip() for ln in body.splitlines() if "delete" in ln and "metallb" in ln]
    expect("nothing in destroy.sh deletes MetalLB", deletes == [], f"got {deletes}")
    expect("and the teardown records the decision",
           "metallb-system" in body, "a reader cannot tell it was deliberate")


# --- the profile-tagged domain -----------------------------------------------
def derive(**env: str) -> str:
    mode = env.pop("mode", "slim")
    return dfeops._apply_domain(dict(env), mode).get("DFE_DOMAIN", "")


def test_a_base_domain_is_tagged_with_the_profile() -> None:
    expect("slim publishes under slim.<base>",
           derive(DFE_BASE_DOMAIN="dfe.example.com", mode="slim")
           == "slim.dfe.example.com", "wrong domain")
    expect("scale publishes under scale.<base>",
           derive(DFE_BASE_DOMAIN="dfe.example.com", mode="scale")
           == "scale.dfe.example.com", "wrong domain")


def test_an_explicit_domain_alone_wins() -> None:
    expect("DFE_DOMAIN is not overwritten",
           derive(DFE_DOMAIN="fixed.example.com") == "fixed.example.com",
           "the derivation overrode an explicit domain")


def test_an_explicit_domain_contradicting_the_base_is_refused() -> None:
    """A declared base domain names the convention; a stale override must not publish."""
    try:
        derive(DFE_DOMAIN="fixed.example.com", DFE_BASE_DOMAIN="dfe.example.com")
    except SystemExit as refused:
        expect("the refusal names the domain the convention wants",
               "slim.dfe.example.com" in str(refused), str(refused))
        return
    expect("a contradicting DFE_DOMAIN is refused", False, "the deploy would have published it")


def test_no_base_domain_derives_nothing() -> None:
    expect("neither key set leaves the domain empty", derive() == "", "a domain appeared")


def test_a_modeless_run_derives_nothing() -> None:
    """`verify` without --mode does not know which profile it is looking at."""
    expect("no mode leaves the domain alone",
           derive(DFE_BASE_DOMAIN="dfe.example.com", mode="") == "", "a domain appeared")


def test_bootstrap_derives_the_same_domain_without_the_driver() -> None:
    """bootstrap.sh is runnable on its own, so the rule cannot live only in dfe-ops."""
    body = BOOTSTRAP.read_text(encoding="utf-8")
    expect("bootstrap.sh derives DFE_DOMAIN from DFE_BASE_DOMAIN",
           'DFE_DOMAIN="${DFE_PROFILE}.${DFE_BASE_DOMAIN}"' in body,
           "the derivation is missing")
    expect("and defaults the two addresses so the annotations always render",
           'DFE_GATEWAY_IP="${DFE_GATEWAY_IP:-}"' in body
           and 'DFE_RECEIVER_IP="${DFE_RECEIVER_IP:-}"' in body,
           "an address var is not defaulted")


# --- the tester IdP follows the same base domain -----------------------------
def test_the_tester_idp_registers_every_profile_hostname() -> None:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import tester_idp

    uris = tester_idp.default_redirect_uris(
        hostname="dex.slim.dfe.example.com",
        host_label="dex",
        base_domain="dfe.example.com",
        profiles=[],
    )
    expect("one callback per profile",
           uris == [
               "https://dfe.slim.dfe.example.com/api/v1/auth/oidc/dex/callback",
               "https://dfe.single.dfe.example.com/api/v1/auth/oidc/dex/callback",
               "https://dfe.scale.dfe.example.com/api/v1/auth/oidc/dex/callback",
               "https://dfe.mesh.dfe.example.com/api/v1/auth/oidc/dex/callback",
           ], f"got {uris}")

    plain = tester_idp.default_redirect_uris(
        hostname="dex.dfe.example.com", host_label="dex"
    )
    expect("no base domain keeps the single derived callback",
           plain == ["https://dfe.dfe.example.com/api/v1/auth/oidc/dex/callback"],
           f"got {plain}")


def test_the_idp_cli_still_parses() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from tester_idp import add_idp_subparser

    add_idp_subparser(sub)
    args = parser.parse_args(
        ["idp", "deploy", "--domain", "slim.dfe.example.com",
         "--base-domain", "dfe.example.com", "--profile", "slim"]
    )
    expect("--base-domain and --profile parse",
           (args.base_domain, args.profile) == ("dfe.example.com", ["slim"]),
           f"got {args.base_domain} {args.profile}")


def main() -> int:
    with standalone():
        test_an_unset_gateway_address_leaves_the_pool_to_choose()
        test_a_set_gateway_address_reaches_the_generated_service()
        test_the_address_is_not_pinned_on_a_service_that_has_none()
        test_the_address_does_not_displace_the_other_lb_fields()
        test_an_unset_receiver_address_leaves_the_pool_to_choose()
        test_a_set_receiver_address_reaches_the_public_service()
        test_the_udp_door_takes_its_own_address()
        test_an_internal_receiver_renders_no_public_service_at_all()
        test_the_cluster_secret_carries_both_addresses()
        test_the_appsets_pass_the_addresses_to_the_charts()
        test_the_env_contract_documents_the_new_keys()
        test_the_metallb_pool_holds_the_two_pinned_addresses()
        test_bootstrap_installs_metallb_on_prem_only()
        test_bootstrap_applies_the_pool_only_with_both_addresses()
        test_destroy_leaves_the_loadbalancer_provider_alone()
        test_a_base_domain_is_tagged_with_the_profile()
        test_an_explicit_domain_alone_wins()
        test_an_explicit_domain_contradicting_the_base_is_refused()
        test_no_base_domain_derives_nothing()
        test_a_modeless_run_derives_nothing()
        test_bootstrap_derives_the_same_domain_without_the_driver()
        test_the_tester_idp_registers_every_profile_hostname()
        test_the_idp_cli_still_parses()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
