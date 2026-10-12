#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_receiver_ingress.py
#  Purpose:      Prove the receiver's door follows exposure.groups.ingest: no
#                load balancer until the senders are named, the AWS NLB on its
#                Elastic IPs once they are, Local traffic on GCP and Azure,
#                no door at all on tunnel or with the group off, and an ingest
#                policy admitting every port its load balancers serve.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The receiver's ingress door, as each cloud's real cascade renders it.

The door is the ingest exposure group (argocd/values/common.yaml), which the
deploy repo's infra/common.yaml sets. Door loadbalancer renders the receiver's
own load balancer only while its allow list names someone, so a fresh cloud
deploy has no public ingest at all. Each cloud's file under
argocd/values/apps/dfe-receiver/ shapes that load balancer, and on AWS the
cluster secret carries the Elastic IPs the root allocated.

culvert stays off a load balancer on AWS under the SAME cascade, and its
render under the unmodified cascade is what a real AWS deploy gets.

The default renders are the goldens (_gate.golden). The dial cases render live,
which needs the scalo-service library (_weave.library).

    python3 scripts/tests/test_receiver_ingress.py
"""

import sys

from _expect import expect, standalone, summary
from _gate import APPSETS, INSTANCE, SCENARIOS, effective_env, golden, rendered_twice
from _weave import render_app, weave

# The controller annotations argocd/values/apps/dfe-receiver/aws.yaml sets.
NLB_ANNOTATIONS = {
    "service.beta.kubernetes.io/aws-load-balancer-type": "external",
    "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
    "service.beta.kubernetes.io/aws-load-balancer-scheme": "internet-facing",
    "service.beta.kubernetes.io/aws-load-balancer-target-group-attributes": "preserve_client_ip.enabled=true",
    "service.beta.kubernetes.io/aws-load-balancer-manage-backend-security-group-rules": "true",
}
EIP_KEY = "service.beta.kubernetes.io/aws-load-balancer-eip-allocations"
SUBNETS_KEY = "service.beta.kubernetes.io/aws-load-balancer-subnets"
FENCE_KEY = "dfe.hyperi.io/ingest-fence"

# RFC 5737 ranges standing in for a deployment's senders.
SENDERS = ["198.51.100.0/24", "203.0.113.0/24"]
EIPS = "eipalloc-0aaaaaaaaaaaaaaa1,eipalloc-0aaaaaaaaaaaaaaa2"
SUBNETS = "subnet-0bbbbbbbbbbbbbbb1,subnet-0bbbbbbbbbbbbbbb2"
CLUSTER_FACTS = {
    "dfe.hyperi.io/receiver_eip_allocations": EIPS,
    "dfe.hyperi.io/receiver_subnets": SUBNETS,
}

# Contract ports the receiver serves only once its config turns them on, each marked
# public: OTLP over gRPC and HTTP, and syslog over TCP, UDP and TLS.
LISTENERS_ON = {"config": {"otlp": {"enabled": True}, "syslog": {"enabled": True}}}
TURNED_ON = {(4317, "TCP"), (4318, "TCP"), (514, "TCP"), (514, "UDP"), (6514, "TCP")}


def ingest(**fields: object) -> dict:
    """The deploy repo's infra/common.yaml setting the ingest group's fields."""
    return {"exposure": {"groups": {"ingest": fields}}}


def culvert(overlay: dict) -> list[dict]:
    """culvert on scale/aws through the edge appset, with ``overlay`` over its instance values."""
    body = {**INSTANCE["culvert"]["aws"], **overlay}
    return render_app("culvert", "scale", "aws", appset=APPSETS["culvert"], overlay=body)


def refusal(fn, *args: object) -> str:
    """The message of a render that MUST fail. Empty string means it did not."""
    try:
        fn(*args)
    except weave().WeaveError as refused:
        return str(refused)
    return ""


def one(docs: list[dict], kind: str, name: str) -> dict:
    for doc in docs:
        if doc.get("kind") == kind and doc["metadata"]["name"] == name:
            return doc
    raise SystemExit(f"no {kind}/{name} in the render")


def public_services(docs: list[dict]) -> list[dict]:
    return [
        d for d in docs
        if d.get("kind") == "Service"
        and d.get("metadata", {}).get("labels", {}).get("dfe.hyperi.io/exposure") == "public"
    ]


def test_expected_fail_the_aws_cascade_renders_no_load_balancer_until_senders_are_named() -> None:
    docs = golden("dfe-receiver", "scale", "aws")
    services = [d for d in docs if d.get("kind") == "Service"]
    expect("exactly one Service renders", len(services) == 1, f"got {len(services)}")
    expect("and it is ClusterIP", services[0]["spec"].get("type", "ClusterIP") == "ClusterIP",
           f"got {services[0]['spec']}")
    labelled = [d["metadata"]["name"] for d in docs
                if d.get("metadata", {}).get("labels", {}).get("dfe.hyperi.io/exposure")]
    expect("no object carries an exposure label", labelled == [], f"got {labelled}")


def test_the_aws_cascade_renders_culvert_with_no_load_balancer() -> None:
    """The real edge cascade, unmodified: edge-aws.yaml sets exposure.serviceType:
    NodePort for culvert beside aws.yaml's oidc.enabled: true."""
    docs = golden("culvert", "scale", "aws")
    types = {d["spec"].get("type", "ClusterIP") for d in docs if d.get("kind") == "Service"}
    expect("no Service is a LoadBalancer", "LoadBalancer" not in types, f"got {types}")
    expect("the public door is a NodePort instead", "NodePort" in types, f"got {types}")


def test_the_tunnels_allow_list_is_refused_where_it_would_not_filter() -> None:
    """Kubernetes only filters on loadBalancerSourceRanges for a LoadBalancer,
    and the AWS cascade makes culvert a NodePort -- so an allow-list set there
    is refused rather than rendered into a Service that ignores it."""
    ranges = {"exposure": {"loadBalancerSourceRanges": ["203.0.113.0/24"]}}
    err = refusal(culvert, ranges)
    expect("an allow-list under NodePort is refused by name",
           "Kubernetes only filters on that field for a LoadBalancer" in err, err.strip()[-300:])
    docs = culvert(
        {
            "exposure": {
                "serviceType": "LoadBalancer",
                "externalTrafficPolicy": "Local",
                "loadBalancerSourceRanges": ["203.0.113.0/24"],
            }
        }
    )
    found = {
        tuple(d["spec"].get("loadBalancerSourceRanges", []))
        for d in docs if d.get("kind") == "Service" and d["spec"].get("type") == "LoadBalancer"
    }
    expect("and it still reaches a real LoadBalancer", found == {("203.0.113.0/24",)},
           f"got {found}")


def test_the_aws_cascades_edge_oidc_switch_renders_culvert_with_no_tunnel_login() -> None:
    """aws.yaml's oidc.enabled: true is the UIs' edge OIDC, not the tunnel's --
    the cascade sets no CULVERT_OAUTH2_* var, wherever culvert's env comes from.

    The contract's optional oauth2 secret group still names its Secret keys, which
    the tunnel reads only once a login is switched on, so an optional reference is
    no setting.
    """
    env = effective_env(golden("culvert", "scale", "aws"), "Deployment/dfe-culvert")
    set_here = {
        name: value
        for name, value in env.items()
        if name.startswith("CULVERT_OAUTH2_")
        and not (value.get("valueFrom", {}).get("secretKeyRef", {}).get("optional") is True
                 if isinstance(value, dict) else False)
    }
    expect("no CULVERT_OAUTH2_* var is set under the real AWS cascade", set_here == {},
           f"got {set_here}")


def test_the_old_oidc_path_still_fails_under_the_aws_cascade() -> None:
    """A deploy-repo overlay layered after aws.yaml that still carries the old
    top-level oidc.issuer/oidc.clientId is refused, not silently ignored, even
    though aws.yaml's own oidc.enabled: true is already in the cascade."""
    err = refusal(culvert, {"oidc": {"issuer": "https://idp.example.com"}})
    expect("the old path is refused under the real cascade too",
           "move the tunnel's own OIDC client-auth fields to vpn.oidc" in err, err.strip()[-300:])


def test_named_senders_on_aws_render_the_nlb_on_its_elastic_ips() -> None:
    docs = render_app("dfe-receiver", "scale", "aws", infra=ingest(allow=SENDERS),
                      annotations=CLUSTER_FACTS)
    public = one(docs, "Service", "dfe-receiver-public")
    annotations = public["metadata"].get("annotations") or {}
    expect("the senders render a LoadBalancer", public["spec"]["type"] == "LoadBalancer",
           f"got {public['spec']}")
    expect("carrying every controller annotation aws.yaml sets",
           all(annotations.get(k) == v for k, v in NLB_ANNOTATIONS.items()), f"got {annotations}")
    expect("on the Elastic IPs the cluster secret names", annotations.get(EIP_KEY) == EIPS,
           f"got {annotations.get(EIP_KEY)}")
    expect("one per subnet, in the same order", annotations.get(SUBNETS_KEY) == SUBNETS,
           f"got {annotations.get(SUBNETS_KEY)}")
    expect("its source ranges are exactly the allow list",
           public["spec"].get("loadBalancerSourceRanges") == SENDERS,
           f"got {public['spec'].get('loadBalancerSourceRanges')}")
    expect("and it says the list is a list", annotations.get(FENCE_KEY) == "listed",
           f"got {annotations.get(FENCE_KEY)}")
    expect("IP targets need no externalTrafficPolicy", "externalTrafficPolicy" not in public["spec"],
           f"got {public['spec']}")


def test_expected_fail_the_udp_load_balancer_takes_no_elastic_ip() -> None:
    """One Elastic IP attaches to one NLB, and the TCP door holds them."""
    docs = render_app("dfe-receiver", "scale", "aws", overlay=LISTENERS_ON,
                      infra=ingest(allow=SENDERS), annotations=CLUSTER_FACTS)
    udp = one(docs, "Service", "dfe-receiver-public-udp")
    annotations = udp["metadata"].get("annotations") or {}
    expect("the UDP door names no Elastic IP", EIP_KEY not in annotations and SUBNETS_KEY not in annotations,
           f"got {annotations}")
    expect("and still carries the allow list", udp["spec"].get("loadBalancerSourceRanges") == SENDERS,
           f"got {udp['spec']}")


def test_expected_fail_gcp_and_azure_keep_the_senders_address_and_carry_no_aws_annotation() -> None:
    for cloud in ("gcp", "azure"):
        public = one(render_app("dfe-receiver", "scale", cloud, infra=ingest(allow=SENDERS)),
                     "Service", "dfe-receiver-public")
        annotations = public["metadata"].get("annotations") or {}
        expect(f"{cloud}: externalTrafficPolicy is Local",
               public["spec"].get("externalTrafficPolicy") == "Local", f"got {public['spec']}")
        expect(f"{cloud}: the source ranges are the allow list",
               public["spec"].get("loadBalancerSourceRanges") == SENDERS, f"got {public['spec']}")
        expect(f"{cloud}: no AWS controller annotation",
               not any(k.startswith("service.beta.kubernetes.io/aws-") for k in annotations),
               f"got {annotations}")


def test_expected_fail_every_address_renders_and_is_marked() -> None:
    public = one(render_app("dfe-receiver", "scale", "gcp", infra=ingest(allow=["0.0.0.0/0"])),
                 "Service", "dfe-receiver-public")
    expect("a /0 marks the Service allow-all",
           (public["metadata"].get("annotations") or {}).get(FENCE_KEY) == "allow-all",
           f"got {public['metadata'].get('annotations')}")


def test_expected_fail_no_door_renders_on_tunnel_or_with_the_group_off() -> None:
    cells = {
        "door tunnel": ingest(allow=SENDERS, door="tunnel"),
        "the group off": ingest(allow=SENDERS, enabled=False),
    }
    for label, infra in cells.items():
        docs = render_app("dfe-receiver", "scale", "aws", infra=infra, annotations=CLUSTER_FACTS)
        expect(f"{label}: no public Service", public_services(docs) == [],
               f"got {[d['metadata']['name'] for d in public_services(docs)]}")


def test_the_tunnel_door_policy_admits_culvert_not_the_internet() -> None:
    docs = render_app("dfe-receiver", "scale", "aws", infra=ingest(door="tunnel"))
    policy = one(docs, "NetworkPolicy", "dfe-receiver-ingest")
    rule = policy["spec"]["ingress"][0]
    expect("it admits the culvert pods by label",
           rule.get("from")
           == [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "dfe-culvert"}}}],
           f"got {rule.get('from')!r}")
    expect("no ipBlock names the whole internet",
           not any("ipBlock" in peer for peer in rule.get("from") or []),
           f"got {rule.get('from')!r}")


def load_balancer_ports(docs: list[dict]) -> set[tuple[int, str]]:
    """Each port and protocol a Service labelled public serves on its load balancer."""
    return {
        (port["port"], port.get("protocol", "TCP"))
        for doc in public_services(docs)
        for port in doc["spec"].get("ports", [])
    }


def policy_ports(docs: list[dict]) -> set[tuple[int, str]]:
    """Each port and protocol the ingest NetworkPolicy admits."""
    policy = one(docs, "NetworkPolicy", "dfe-receiver-ingest")
    return {
        (port["port"], port.get("protocol", "TCP"))
        for rule in policy["spec"]["ingress"]
        for port in rule.get("ports", [])
    }


def test_the_ingest_policy_admits_every_port_the_public_load_balancer_serves() -> None:
    """A port on the load balancer that the policy does not admit is dropped at the pod."""
    infra = SCENARIOS["public-ingest"]["infra"]
    cells = {
        "the public-ingest golden": golden("dfe-receiver", "slim", "local", "public-ingest"),
        "public ingest with OTLP and syslog on": render_app(
            "dfe-receiver", "slim", "local", overlay=LISTENERS_ON, infra=infra
        ),
    }
    for label, docs in cells.items():
        served = load_balancer_ports(docs)
        admitted = policy_ports(docs)
        expect(f"{label}: the load balancer serves a port", bool(served), f"got {served}")
        expect(f"{label}: the policy admits exactly the load balancer's ports",
               admitted == served, f"policy {sorted(admitted)}, load balancer {sorted(served)}")
        expect(f"{label}: each object renders once", rendered_twice(docs) == [],
               f"got {rendered_twice(docs)}")
    turned_on = load_balancer_ports(cells["public ingest with OTLP and syslog on"])
    expect("the listeners turned on reach the load balancer", TURNED_ON <= turned_on,
           f"missing {sorted(TURNED_ON - turned_on)}")


def main() -> int:
    with standalone():
        test_expected_fail_the_aws_cascade_renders_no_load_balancer_until_senders_are_named()
        test_the_aws_cascade_renders_culvert_with_no_load_balancer()
        test_the_tunnels_allow_list_is_refused_where_it_would_not_filter()
        test_the_aws_cascades_edge_oidc_switch_renders_culvert_with_no_tunnel_login()
        test_the_old_oidc_path_still_fails_under_the_aws_cascade()
        test_named_senders_on_aws_render_the_nlb_on_its_elastic_ips()
        test_expected_fail_the_udp_load_balancer_takes_no_elastic_ip()
        test_expected_fail_gcp_and_azure_keep_the_senders_address_and_carry_no_aws_annotation()
        test_expected_fail_every_address_renders_and_is_marked()
        test_expected_fail_no_door_renders_on_tunnel_or_with_the_group_off()
        test_the_tunnel_door_policy_admits_culvert_not_the_internet()
        test_the_ingest_policy_admits_every_port_the_public_load_balancer_serves()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
