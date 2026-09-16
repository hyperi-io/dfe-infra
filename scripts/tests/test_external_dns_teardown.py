#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_external_dns_teardown.py
#  Purpose:      Prove external-dns publishes only what this repo's own charts
#                mark, is told to delete what it published, and that the private
#                zone is emptied before `tofu destroy` reaches it even when the
#                cluster running external-dns is already gone.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""external-dns's own default (upsert-only) never removes a record it
published, so a private zone accumulates every hostname a workload ever
carried and `tofu destroy` fails with HostedZoneNotEmpty once the EKS
cluster -- and external-dns along with it -- is already gone. Two halves fix
it: sync policy for the case the cluster is still alive to react, and a
destroy-time provisioner in OpenTofu for the case it is not.

A `tofu test` run block can prove what the provisioner captures into its own
state (contract.tftest.hcl, aws_private_zone_teardown_captures_the_zone_to_
empty), but not the provisioner block itself -- provisioner configuration is
not a resource attribute, so no `assert` condition can reach `when = destroy`
or the command it runs. These checks read the two files as text instead.

    python3 -m pytest scripts/tests/test_external_dns_teardown.py -q
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APPSETS = REPO_ROOT / "argocd" / "appsets"
DNS_TF = REPO_ROOT / "terraform" / "modules" / "kubernetes-cluster" / "aws" / "dns.tf"
GATEWAY = chart_dir("envoy-gateway-config")
VALUES = REPO_ROOT / "argocd" / "values"

# The one marker, stated here so a drift between the chart and the appset is a
# failure rather than a deployment that publishes nothing.
MARKER_KEY = "dfe.hyperi.io/publish-dns"
MARKER_VALUE = "true"


def render_gateway(*args: str) -> list[dict]:
    cmd = [
        "helm", "template", "envoy-gateway-config", str(GATEWAY),
        "--namespace", "envoy-gateway-system",
        "-f", str(VALUES / "common.yaml"),
        "--set", "domain=dfe.example.com",
        "--set", "appNamespace=dfe-local",
        *args,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for the gateway {args}:\n{out.stderr}")
    return [doc for doc in yaml.safe_load_all(out.stdout) if doc]


def external_dns_block(addons_text: str) -> str:
    """The Go-template fragment gated on .chart == "external-dns", as text --
    it lives inside a YAML block scalar, so it cannot be reached by parsing
    the document as YAML."""
    start = addons_text.index('{{- if eq .chart "external-dns" }}')
    end = addons_text.index("{{- end }}", start)
    return addons_text[start:end]


def test_external_dns_runs_sync_with_a_deployment_unique_owner_id() -> None:
    """upsert-only (the chart's own default) never deletes a record it
    published, so a Service or Ingress removed from the cluster leaves its
    name in the zone forever."""
    block = external_dns_block((APPSETS / "layer1-addons.yaml").read_text(encoding="utf-8"))

    expect("external-dns runs policy: sync, not the chart's upsert-only default",
           re.search(r"^\s*policy:\s*sync\s*$", block, re.MULTILINE) is not None, block)

    owner = re.search(r"^\s*txtOwnerId:\s*'([^']+)'\s*$", block, re.MULTILINE)
    expect("external-dns carries a txtOwnerId", owner is not None, block)
    if owner is not None:
        expect("and it is a template expression, not an empty or literal string",
               owner.group(1).startswith("{{") and owner.group(1).endswith("}}"),
               owner.group(1))
        # dfe.hyperi.io/cluster_name renders no key at all on any cloud but
        # AWS (bootstrap/templates/cluster-secret.yaml.tpl); the external-dns
        # matrix generator's own selector matches on dns-provider alone, not
        # on cloud, so reading that annotation here would fail goTemplate's
        # missingkey=error the day a second cloud declares a provider.
        expect("and it is not the AWS-only cluster_name annotation",
               "cluster_name" not in owner.group(1), owner.group(1))


def test_external_dns_publishes_only_what_our_charts_mark() -> None:
    """The Gateway admits routes from every namespace, so with the route source
    on and no filter, a namespace-scoped actor could publish a resolvable,
    publicly certificated name under the deployment's own domain."""
    block = external_dns_block((APPSETS / "layer1-addons.yaml").read_text(encoding="utf-8"))

    expect("the route source is on, which is what makes the filter necessary",
           re.search(r"^\s*-\s*gateway-httproute\s*$", block, re.MULTILINE) is not None, block)

    selector = re.search(r"^\s*annotationFilter:\s*(\S+)\s*$", block, re.MULTILINE)
    expect("external-dns carries an annotation filter", selector is not None, block)
    if selector is not None:
        expect("and it names the marker this repo's own charts write",
               selector.group(1) == f"{MARKER_KEY}={MARKER_VALUE}", selector.group(1))


def test_every_rendered_httproute_carries_the_marker() -> None:
    """A route the chart renders without the marker is a hostname external-dns
    never publishes, which reports healthy and resolves nowhere."""
    docs = render_gateway()
    routes = [doc for doc in docs if doc.get("kind") == "HTTPRoute"]
    expect("the chart renders routes to check at all", len(routes) > 0, f"got {len(routes)}")
    unmarked = [
        doc["metadata"]["name"]
        for doc in routes
        if (doc["metadata"].get("annotations") or {}).get(MARKER_KEY) != MARKER_VALUE
    ]
    expect("every rendered HTTPRoute carries the marker", not unmarked, f"unmarked: {unmarked}")


def test_the_front_door_service_is_marked_alongside_its_hostnames() -> None:
    """The filter covers Services as well as routes, so the hostname annotation
    on the managed proxy's Service stops being read without the marker beside
    it -- and that annotation is what publishes every public name."""
    docs = render_gateway(
        "-f", str(VALUES / "aws.yaml"),
        "-f", str(VALUES / "edge-aws.yaml"),
        "--set", "ui.public_domain=example.com",
        "--set", "ui.public.dfe_ui=true",
    )
    proxies = [doc for doc in docs if doc.get("kind") == "EnvoyProxy"]
    expect("the chart renders one EnvoyProxy", len(proxies) == 1, f"got {len(proxies)}")
    service = proxies[0]["spec"]["provider"]["kubernetes"]["envoyService"]
    annotations = service.get("annotations") or {}
    expect("the Service carries the hostnames external-dns publishes",
           "external-dns.alpha.kubernetes.io/hostname" in annotations, f"got {sorted(annotations)}")
    expect("and the marker beside them",
           annotations.get(MARKER_KEY) == MARKER_VALUE, f"got {sorted(annotations)}")

    routes = [doc for doc in docs if doc.get("kind") == "HTTPRoute"]
    unmarked = [
        doc["metadata"]["name"]
        for doc in routes
        if (doc["metadata"].get("annotations") or {}).get(MARKER_KEY) != MARKER_VALUE
    ]
    expect("and the public route rendered on this cascade is marked too",
           not unmarked, f"unmarked: {unmarked}")


def test_private_zone_teardown_runs_before_the_zone_is_destroyed() -> None:
    """The cleanup resource must be a destroy-time provisioner shaped so
    OpenTofu destroys it ahead of the zone -- a reference to the zone's own
    id, not an unrelated trigger that happens to fire around the same time."""
    body = DNS_TF.read_text(encoding="utf-8")
    start = body.index('resource "terraform_data" "private_zone_teardown"')
    block = body[start : body.index("\n}\n", start)]

    expect("the cleanup resource captures the zone id it must empty",
           "aws_route53_zone.private.zone_id" in block, block)
    expect("the provisioner is destroy-time, not create-time",
           re.search(r"^\s*when\s*=\s*destroy\s*$", block, re.MULTILINE) is not None, block)
    expect("the command reads the zone id back off self, per OpenTofu's own restriction "
           "that a destroy-time command may reference only self, count.index or each.key",
           "self.output.zone_id" in block, block)
    expect("aws route53 change-resource-record-sets is the call that actually empties the zone",
           "route53 change-resource-record-sets" in block, block)
    # Route 53 refuses to delete a hosted zone still carrying anything but its
    # own apex NS/SOA pair -- so the cleanup must delete everything else and
    # leave those two alone, never the reverse.
    expect("the filter spares NS and SOA at the APEX NAME, not every NS in the zone -- "
           "a sub-delegation's NS record is not Route 53's and blocks the delete if it stays",
           "Name=='$apex'" in block and "Type=='NS'" in block and "Type=='SOA'" in block, block)
    expect("the apex name travels through the resource's own input, since a destroy-time "
           "command may read nothing but self",
           "apex = aws_route53_zone.private.name" in block and "self.output.apex" in block, block)


def test_the_cluster_is_destroyed_before_the_zone_it_publishes_into_is_emptied() -> None:
    """external-dns runs until the cluster goes. A record it republishes between
    the delete and DeleteHostedZone fails the destroy with HostedZoneNotEmpty and
    succeeds on a re-run, which reads as a flake rather than a fault."""
    eks = (DNS_TF.parent / "eks.tf").read_text(encoding="utf-8")
    start = eks.index('resource "aws_eks_cluster" "this"')
    block = eks[start : eks.index("\n}\n", start)]

    expect("the cluster names the teardown in depends_on, which is what puts the cluster's "
           "DESTROY ahead of it -- a dependent is destroyed before what it depends on",
           "terraform_data.private_zone_teardown" in block, block)


def main() -> int:
    with standalone():
        test_external_dns_runs_sync_with_a_deployment_unique_owner_id()
        test_external_dns_publishes_only_what_our_charts_mark()
        test_every_rendered_httproute_carries_the_marker()
        test_the_front_door_service_is_marked_alongside_its_hostnames()
        test_private_zone_teardown_runs_before_the_zone_is_destroyed()
        test_the_cluster_is_destroyed_before_the_zone_it_publishes_into_is_emptied()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
