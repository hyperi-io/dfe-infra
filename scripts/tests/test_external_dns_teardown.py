#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_external_dns_teardown.py
#  Purpose:      Prove external-dns is told to delete what it published, and
#                that the private zone is emptied before `tofu destroy`
#                reaches it even when the cluster running external-dns is
#                already gone.
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
import sys
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APPSETS = REPO_ROOT / "argocd" / "appsets"
DNS_TF = REPO_ROOT / "terraform" / "modules" / "kubernetes-cluster" / "aws" / "dns.tf"


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
    expect("the filter excludes NS and SOA -- Route 53 manages those itself and rejects deleting them",
           "Type!='NS'" in block and "Type!='SOA'" in block, block)


def main() -> int:
    with standalone():
        test_external_dns_runs_sync_with_a_deployment_unique_owner_id()
        test_private_zone_teardown_runs_before_the_zone_is_destroyed()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
