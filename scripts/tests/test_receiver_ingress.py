#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_receiver_ingress.py
#  Purpose:      Prove the AWS cascade never renders a per-GB load balancer for
#                the receiver's ingest ports by default, that culvert stays off
#                one too under the SAME unmodified cascade, and that the
#                explicit public opt-in still works.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The receiver's ingress door must not be priced by volume on AWS, because DFE
pushes terabytes a day through it -- docs/deployment/aws.md carries the rates.
argocd/values/aws.yaml defaults exposure.mode to vpn and
argocd/values/edge-aws.yaml points culvert at a NodePort instead of a
LoadBalancer; this proves both hold under the REAL cascade each Application
gets, and that the costed public opt-in still renders correctly for a deployer
who wants it. The two cascades differ: the receiver is fanned out by layer2-apps
and reads no flavour file, while culvert is one of the edge module's two
Applications and reads edge-aws.yaml.

The cascade also sets oidc.enabled: true for the gateway's edge OIDC in the SAME
pass, which culvert must not read as its own tunnel-login switch: culvert's OIDC
fields live under vpn.oidc.*, and its render under the unmodified cascade is
what a real AWS deploy gets.

The default renders are the goldens (_gate.golden). The dial cases render live,
which needs the scalo-service library (_weave.library).

    python3 scripts/tests/test_receiver_ingress.py
"""

import sys

from _expect import expect, standalone, summary
from _gate import APPSETS, INSTANCE, effective_env, golden
from _weave import render_app, weave

# The annotations docs/deployment/aws.md documents for the costed public
# opt-in -- proven here to reach the rendered Service verbatim.
NLB_ANNOTATIONS = {
    "service.beta.kubernetes.io/aws-load-balancer-type": "external",
    "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
    "service.beta.kubernetes.io/aws-load-balancer-scheme": "internal",
}


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


def test_the_aws_cascade_renders_the_receiver_as_one_clusterip_service() -> None:
    docs = golden("dfe-receiver", "scale", "aws")
    services = [d for d in docs if d.get("kind") == "Service"]
    expect("exactly one Service renders", len(services) == 1, f"got {len(services)}")
    expect("and it is ClusterIP", services[0]["spec"].get("type", "ClusterIP") == "ClusterIP",
           f"got {services[0]['spec']}")
    labelled_public = [
        d for d in docs
        if d.get("metadata", {}).get("labels", {}).get("dfe.hyperi.io/exposure") == "public"
    ]
    expect("no object carries dfe.hyperi.io/exposure: public", labelled_public == [],
           f"got {[d.get('metadata', {}).get('name') for d in labelled_public]}")


def test_the_aws_cascade_renders_culvert_with_no_load_balancer() -> None:
    """The real edge cascade, unmodified: aws.yaml sets oidc.enabled: true for
    the gateway's edge OIDC AND edge-aws.yaml sets exposure.serviceType:
    NodePort for culvert in the same pass."""
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


def test_an_explicit_public_opt_in_renders_the_annotated_load_balancer() -> None:
    """The keys docs/INGEST-EDGE.md and deployment.example.yaml document."""
    overlay = {
        "exposure": {
            "mode": "public",
            "public": {
                "annotations": NLB_ANNOTATIONS,
                "loadBalancerSourceRanges": ["203.0.113.0/24"],
            },
        },
    }
    docs = render_app("dfe-receiver", "scale", "aws", overlay=overlay)
    public = one(docs, "Service", "dfe-receiver-public")
    expect("the opt-in renders a LoadBalancer", public["spec"]["type"] == "LoadBalancer",
           f"got {public['spec']}")
    expect("carrying the documented annotations",
           public["metadata"].get("annotations") == NLB_ANNOTATIONS,
           f"got {public['metadata'].get('annotations')}")
    expect("and the source-range allow-list, never empty",
           public["spec"].get("loadBalancerSourceRanges") == ["203.0.113.0/24"],
           f"got {public['spec'].get('loadBalancerSourceRanges')}")


def test_the_ingest_networkpolicy_admits_culvert_not_the_internet() -> None:
    policy = one(golden("dfe-receiver", "scale", "aws"), "NetworkPolicy", "dfe-receiver-ingest")
    rule = policy["spec"]["ingress"][0]
    expect("it admits the culvert pods by label",
           rule.get("from")
           == [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "dfe-culvert"}}}],
           f"got {rule.get('from')!r}")
    expect("no ipBlock names the whole internet",
           not any("ipBlock" in peer for peer in rule.get("from") or []),
           f"got {rule.get('from')!r}")


def main() -> int:
    with standalone():
        test_the_aws_cascade_renders_the_receiver_as_one_clusterip_service()
        test_the_aws_cascade_renders_culvert_with_no_load_balancer()
        test_the_tunnels_allow_list_is_refused_where_it_would_not_filter()
        test_the_aws_cascades_edge_oidc_switch_renders_culvert_with_no_tunnel_login()
        test_the_old_oidc_path_still_fails_under_the_aws_cascade()
        test_an_explicit_public_opt_in_renders_the_annotated_load_balancer()
        test_the_ingest_networkpolicy_admits_culvert_not_the_internet()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
