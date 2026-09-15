#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_pod_identity_accounts.py
#  Purpose:      Prove every EKS Pod Identity association names the namespace
#                and service account the chart that consumes it actually
#                creates.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Pod Identity resolves a credential by namespace AND service account, so an
association naming an account nothing runs as hands its controller nothing --
and the controller keeps reporting Ready while every call it owes fails.

The two halves live in different files and different languages, so a `tofu test`
can only ever compare the tofu literal to itself. These checks read both sides.

    python3 -m pytest scripts/tests/test_pod_identity_accounts.py -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CLUSTER_AWS = REPO_ROOT / "terraform" / "modules" / "kubernetes-cluster" / "aws"
APPSETS = REPO_ROOT / "argocd" / "appsets"
CHARTS = REPO_ROOT / "helm" / "charts"


def association(tf_file: Path, name: str) -> dict[str, str]:
    """The namespace and service account one aws_eks_pod_identity_association pins.

    A field set from a variable answers that variable's own default, which is
    the value a caller passing nothing gets.
    """
    body = tf_file.read_text(encoding="utf-8")
    start = body.index(f'resource "aws_eks_pod_identity_association" "{name}"')
    block = body[start : body.index("\n}\n", start)]
    out: dict[str, str] = {}
    for field in ("namespace", "service_account"):
        literal = re.search(rf'^\s*{field}\s*=\s*"([^"]+)"', block, re.MULTILINE)
        if literal:
            out[field] = literal.group(1)
            continue
        from_var = re.search(rf"^\s*{field}\s*=\s*var\.(\S+)", block, re.MULTILINE)
        if from_var:
            out[field] = variable_default(tf_file.parent / "variables.tf", from_var.group(1))
    return out


def variable_default(variables_tf: Path, name: str) -> str:
    match = re.search(
        rf'variable "{re.escape(name)}".*?default\s*=\s*"([^"]+)"',
        variables_tf.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"no default for var.{name} in {variables_tf}")
    return match.group(1)


def test_external_dns_binds_the_account_the_appset_names() -> None:
    """The chart derives its account name from the release, which is
    external-dns-<cluster>; the appset names it instead, and that name is what
    the association has to match."""
    pinned = association(CLUSTER_AWS / "dns.tf", "external_dns")
    expect("the association names both halves",
           set(pinned) == {"namespace", "service_account"}, pinned)

    addons = (APPSETS / "layer1-addons.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(addons)
    elements = [
        element
        for generator in doc["spec"]["generators"]
        for child in generator.get("matrix", {}).get("generators", [])
        for element in child.get("list", {}).get("elements", [])
    ]
    entry = next(e for e in elements if e["chart"] == "external-dns")
    expect("the appset deploys external-dns into the namespace the association names",
           entry["namespace"] == pinned["namespace"],
           f"appset {entry['namespace']!r} vs tofu {pinned['namespace']!r}")

    # The serviceAccount.name sits inside the appset's Helm values template, so
    # it is read as text rather than through the parsed document.
    named = re.search(
        r'eq \.chart "external-dns".*?serviceAccount:\s*\n\s*create: true\s*\n\s*name: (\S+)',
        addons,
        re.DOTALL,
    )
    expect("the appset names the service account explicitly", named is not None, "no name: found")
    expect("and it is the account the association binds",
           named.group(1) == pinned["service_account"],
           f"appset {named.group(1)!r} vs tofu {pinned['service_account']!r}")


def test_clickhouse_object_store_binds_the_account_the_chart_creates() -> None:
    """The S3 role is narrowed to ClickHouse alone by naming its own account, so
    a chart default that drifts from the module default puts the association
    back on an account nothing runs as."""
    pinned = association(CLUSTER_AWS / "object-store.tf", "clickhouse_object_store")
    values = yaml.safe_load((CHARTS / "clickhouse-cluster" / "values.yaml").read_text(encoding="utf-8"))
    chart_account = values["clickhouse"]["serviceAccount"]["name"]
    expect("the chart creates the account the association binds",
           chart_account == pinned["service_account"],
           f"chart {chart_account!r} vs tofu {pinned['service_account']!r}")
    expect("and the chart creates it at all",
           values["clickhouse"]["serviceAccount"]["create"] is True, values["clickhouse"]["serviceAccount"])
    expect("the account is not the release namespace's default, which dfe-schema also runs as",
           chart_account != "default", chart_account)


def test_keeper_gets_its_own_account_and_no_association() -> None:
    """Keeper never touches S3, so it carries no association -- but it must not
    sit on the namespace default either, or a grant aimed at `default` reaches
    it."""
    values = yaml.safe_load((CHARTS / "clickhouse-cluster" / "values.yaml").read_text(encoding="utf-8"))
    keeper = values["clickhouse"]["keeper"]["serviceAccount"]
    expect("keeper creates its own account", keeper["create"] is True, keeper)
    expect("and it is not the namespace default", keeper["name"] != "default", keeper)

    for tf_file in sorted(CLUSTER_AWS.glob("*.tf")):
        body = tf_file.read_text(encoding="utf-8")
        expect(f"{tf_file.name} names no association on the keeper account",
               f'service_account = "{keeper["name"]}"' not in body, tf_file.name)
        expect(f"{tf_file.name} names no association on a default account",
               'service_account = "default"' not in body, tf_file.name)


def main() -> int:
    with standalone():
        test_external_dns_binds_the_account_the_appset_names()
        test_clickhouse_object_store_binds_the_account_the_chart_creates()
        test_keeper_gets_its_own_account_and_no_association()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
