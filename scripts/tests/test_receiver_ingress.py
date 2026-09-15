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
pushes terabytes a day through it -- docs/deployment/aws.md#receiver-ingress
carries the rates. argocd/values/aws.yaml now defaults exposure.mode to vpn and
points culvert at a NodePort instead of a LoadBalancer; this proves both hold
under the REAL cascade an AWS deploy gets (common.yaml + aws.yaml +
profile-scale.yaml), not just the chart's own defaults, and that the costed
public opt-in still renders correctly for a deployer who wants it.

The cascade also sets oidc.enabled: true for the gateway's edge OIDC in the
SAME pass, which used to break culvert's render outright (it read the same
top-level oidc.enabled as its own tunnel-login switch). That collision is
fixed by moving culvert's OIDC fields to vpn.oidc.* -- this module renders
culvert under the unmodified cascade with no isolating override, matching
what a real AWS deploy actually gets.

    python3 scripts/tests/test_receiver_ingress.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
VALUES = REPO_ROOT / "argocd" / "values"

# The valueFiles order an AWS deploy actually gets (layer2-data /
# layer2-platform's own cascade), matching test_storage_model.py's
# test_the_aws_cascade_is_what_turns_pod_identity_on.
AWS_CASCADE = [VALUES / "common.yaml", VALUES / "aws.yaml", VALUES / "profile-scale.yaml"]

# The annotations docs/deployment/aws.md documents for the costed public
# opt-in -- proven here to reach the rendered Service verbatim.
NLB_ANNOTATIONS = {
    "service.beta.kubernetes.io/aws-load-balancer-type": "external",
    "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
    "service.beta.kubernetes.io/aws-load-balancer-scheme": "internal",
}


def render(chart: str, *args: str, values: list[Path] | None = None) -> list[dict]:
    cmd = ["helm", "template", chart, str(chart_dir(chart))]
    for v in values or AWS_CASCADE:
        cmd += ["-f", str(v)]
    cmd += list(args)
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart} {args}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def render_error(chart: str, *args: str, values: list[Path] | None = None) -> str:
    """The stderr of a render that MUST fail. Empty string means it did not."""
    cmd = ["helm", "template", chart, str(chart_dir(chart))]
    for v in values or AWS_CASCADE:
        cmd += ["-f", str(v)]
    cmd += list(args)
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return "" if out.returncode == 0 else out.stderr


def one(docs: list[dict], kind: str, name: str) -> dict:
    for doc in docs:
        if doc.get("kind") == kind and doc["metadata"]["name"] == name:
            return doc
    raise SystemExit(f"no {kind}/{name} in the render")


def env_of(deployment: dict) -> dict[str, str]:
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value", "") for e in container.get("env", [])}


def test_the_aws_cascade_renders_the_receiver_as_one_clusterip_service() -> None:
    docs = render("dfe-receiver")
    services = [d for d in docs if d.get("kind") == "Service"]
    expect("exactly one Service renders", len(services) == 1, f"got {len(services)}")
    expect("and it is ClusterIP", services[0]["spec"]["type"] == "ClusterIP",
           f"got {services[0]['spec']}")
    labelled_public = [
        d for d in docs
        if d.get("metadata", {}).get("labels", {}).get("dfe.hyperi.io/exposure") == "public"
    ]
    expect("no object carries dfe.hyperi.io/exposure: public", labelled_public == [],
           f"got {[d.get('metadata', {}).get('name') for d in labelled_public]}")


def test_the_aws_cascade_renders_culvert_with_no_load_balancer() -> None:
    """The real AWS cascade, unmodified: aws.yaml sets oidc.enabled: true for
    the gateway's edge OIDC AND exposure.serviceType: NodePort for culvert in
    the same pass -- the two used to collide (culvert read the same
    oidc.enabled as its own tunnel-login switch), which is fixed by moving
    culvert's OIDC fields to vpn.oidc.*."""
    docs = render("culvert")
    types = {d["spec"]["type"] for d in docs if d.get("kind") == "Service"}
    expect("no Service is a LoadBalancer", "LoadBalancer" not in types, f"got {types}")
    expect("the public door is a NodePort instead", "NodePort" in types, f"got {types}")


def test_the_tunnels_allow_list_is_refused_where_it_would_not_filter() -> None:
    """Kubernetes only filters on loadBalancerSourceRanges for a LoadBalancer,
    and the AWS cascade makes culvert a NodePort -- so an allow-list set there
    is refused rather than rendered into a Service that ignores it."""
    err = render_error("culvert", "--set", "exposure.loadBalancerSourceRanges={203.0.113.0/24}")
    expect("an allow-list under NodePort is refused by name",
           "Kubernetes only filters on that field for a LoadBalancer" in err, err.strip()[-300:])
    docs = render("culvert", "--set", "exposure.serviceType=LoadBalancer",
                  "--set", "exposure.externalTrafficPolicy=Local",
                  "--set", "exposure.loadBalancerSourceRanges={203.0.113.0/24}")
    ranges = {
        tuple(d["spec"].get("loadBalancerSourceRanges", []))
        for d in docs if d.get("kind") == "Service" and d["spec"]["type"] == "LoadBalancer"
    }
    expect("and it still reaches a real LoadBalancer", ranges == {("203.0.113.0/24",)}, f"got {ranges}")


def test_the_aws_cascades_edge_oidc_switch_renders_culvert_with_no_tunnel_login() -> None:
    """aws.yaml's oidc.enabled: true is the UIs' edge OIDC, not the tunnel's --
    proves the cascade renders no CULVERT_OAUTH2_* var, the positive half of
    the fix (the render succeeding is only half the proof)."""
    env = env_of(one(render("culvert"), "Deployment", "dfe-culvert"))
    expect("no CULVERT_OAUTH2_* var is rendered under the real AWS cascade",
           not any(k.startswith("CULVERT_OAUTH2_") for k in env), f"got {env}")


def test_the_old_oidc_path_still_fails_under_the_aws_cascade() -> None:
    """A deploy-repo overlay layered after aws.yaml that still carries the old
    top-level oidc.issuer/oidc.clientId is refused, not silently ignored, even
    though aws.yaml's own oidc.enabled: true is already in the cascade."""
    err = render_error("culvert", "--set", "oidc.issuer=https://idp.example.com")
    expect("the old path is refused under the real cascade too",
           "move the tunnel's own OIDC client-auth fields to vpn.oidc" in err, err.strip()[-300:])


def test_an_explicit_public_opt_in_renders_the_annotated_load_balancer() -> None:
    docs = render(
        "dfe-receiver",
        "--set", "exposure.mode=public",
        "--set-json", f"exposure.public.annotations={json.dumps(NLB_ANNOTATIONS)}",
        "--set-json", 'exposure.public.loadBalancerSourceRanges=["203.0.113.0/24"]',
    )
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
    docs = render("dfe-receiver")
    policy = one(docs, "NetworkPolicy", "dfe-receiver-ingest")
    rule = policy["spec"]["ingress"][0]
    expect("it admits the culvert pods by label",
           rule.get("from") == [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "dfe-culvert"}}}],
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
