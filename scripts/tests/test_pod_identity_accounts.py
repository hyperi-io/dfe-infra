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
EDGE_AWS = REPO_ROOT / "terraform" / "modules" / "edge" / "aws"
SECRETS_AWS = REPO_ROOT / "terraform" / "modules" / "secrets" / "aws-sm"
APPSETS = REPO_ROOT / "argocd" / "appsets"
CHARTS = REPO_ROOT / "helm" / "charts"
GOLDENS = REPO_ROOT / "scripts" / "tests" / "fixtures" / "thin-renders"


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
            out[field] = variable_default(
                tf_file.parent / "variables.tf", from_var.group(1)
            )
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


def local_value(tf_file: Path, name: str) -> str:
    """The string a module's locals block gives `name`."""
    match = re.search(
        rf'^\s*{re.escape(name)}\s*=\s*"([^"]+)"',
        tf_file.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:
        raise AssertionError(f"no string local {name} in {tf_file}")
    return match.group(1)


def reader_accounts(tf_file: Path) -> dict[str, str]:
    """Reader key -> the service account aws-sm's readers map binds it to."""
    return dict(
        re.findall(
            r'^\s*([\w-]+)\s*=\s*\{\s*\n(?:\s*//.*\n)*\s*service_account\s*=\s*"([^"]+)"',
            tf_file.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )


def runs_as(golden: Path, deployment: str) -> set[str]:
    """The serviceAccountName each Deployment called `deployment` in a golden runs as."""
    return {
        doc["spec"]["template"]["spec"].get("serviceAccountName")
        for doc in yaml.safe_load_all(golden.read_text(encoding="utf-8"))
        if doc
        and doc.get("kind") == "Deployment"
        and doc["metadata"]["name"] == deployment
    }


def test_each_reader_role_binds_the_account_its_pods_run_as() -> None:
    """dfe-receiver reads its ingest bearer tokens and dfe-hunt-runner its alert
    destinations through these associations, so an account the pods do not run as
    leaves the receiver with no tokens and the runner with no destinations."""
    main_tf = SECRETS_AWS / "main.tf"
    body = main_tf.read_text(encoding="utf-8")
    start = body.index('resource "aws_eks_pod_identity_association" "reader"')
    block = body[start : body.index("\n}\n", start)]
    expect(
        "the association binds each reader's service account",
        re.search(
            r"^\s*service_account\s*=\s*each\.value\.service_account$",
            block,
            re.MULTILINE,
        )
        is not None,
        block,
    )
    accounts = reader_accounts(main_tf)
    expect(
        "the readers map names the receiver and the hunt runner",
        set(accounts) == {"receiver", "hunt-runner"},
        f"{accounts}",
    )

    # reader key -> (golden directory, Deployment name)
    pods = {
        "receiver": ("dfe-receiver", "dfe-receiver"),
        "hunt-runner": ("dfe-engine", "dfe-hunt-runner"),
    }
    for key, (component, deployment) in pods.items():
        renders = sorted((GOLDENS / component).glob("*-aws*.yaml"))
        expect(f"{component} has aws goldens to read", bool(renders), str(GOLDENS))
        for golden in renders:
            found = runs_as(golden, deployment)
            expect(
                f"{golden.name} runs {deployment} as the account the {key} association binds",
                found == {accounts.get(key)},
                f"golden {sorted(found)!r} vs tofu {accounts.get(key)!r}",
            )


def test_external_dns_binds_the_account_the_appset_names() -> None:
    """The chart derives its account name from the release, which is
    external-dns-<cluster>; the appset names it instead, and that name is what
    the association has to match."""
    pinned = association(EDGE_AWS / "dns.tf", "external_dns")
    expect(
        "the association names both halves",
        set(pinned) == {"namespace", "service_account"},
        pinned,
    )

    addons = (APPSETS / "layer1-addons.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(addons)
    elements = [
        element
        for generator in doc["spec"]["generators"]
        for child in generator.get("matrix", {}).get("generators", [])
        for element in child.get("list", {}).get("elements", [])
    ]
    entry = next(e for e in elements if e["chart"] == "external-dns")
    expect(
        "the appset deploys external-dns into the namespace the association names",
        entry["namespace"] == pinned["namespace"],
        f"appset {entry['namespace']!r} vs tofu {pinned['namespace']!r}",
    )

    # The serviceAccount.name sits inside the appset's Helm values template, so
    # it is read as text rather than through the parsed document.
    named = re.search(
        r'eq \.chart "external-dns".*?serviceAccount:\s*\n\s*create: true\s*\n\s*name: (\S+)',
        addons,
        re.DOTALL,
    )
    expect(
        "the appset names the service account explicitly",
        named is not None,
        "no name: found",
    )
    expect(
        "and it is the account the association binds",
        named.group(1) == pinned["service_account"],
        f"appset {named.group(1)!r} vs tofu {pinned['service_account']!r}",
    )


def test_clickhouse_object_store_binds_the_account_the_chart_creates() -> None:
    """The S3 role is narrowed to ClickHouse alone by naming its own account, so
    a chart default that drifts from the module default puts the association
    back on an account nothing runs as."""
    pinned = association(CLUSTER_AWS / "object-store.tf", "clickhouse_object_store")
    values = yaml.safe_load(
        (CHARTS / "clickhouse-cluster" / "values.yaml").read_text(encoding="utf-8")
    )
    chart_account = values["clickhouse"]["serviceAccount"]["name"]
    expect(
        "the chart creates the account the association binds",
        chart_account == pinned["service_account"],
        f"chart {chart_account!r} vs tofu {pinned['service_account']!r}",
    )
    expect(
        "and the chart creates it at all",
        values["clickhouse"]["serviceAccount"]["create"] is True,
        values["clickhouse"]["serviceAccount"],
    )
    expect(
        "the account is not the release namespace's default, which every other pod there shares",
        chart_account != "default",
        chart_account,
    )


def test_keeper_gets_its_own_account_and_no_association() -> None:
    """Keeper never touches S3, so it carries no association -- but it must not
    sit on the namespace default either, or a grant aimed at `default` reaches
    it."""
    values = yaml.safe_load(
        (CHARTS / "clickhouse-cluster" / "values.yaml").read_text(encoding="utf-8")
    )
    keeper = values["clickhouse"]["keeper"]["serviceAccount"]
    expect("keeper creates its own account", keeper["create"] is True, keeper)
    expect("and it is not the namespace default", keeper["name"] != "default", keeper)

    # Both modules mint associations against this cluster, so both are swept.
    for tf_file in sorted(CLUSTER_AWS.glob("*.tf")) + sorted(EDGE_AWS.glob("*.tf")):
        body = tf_file.read_text(encoding="utf-8")
        expect(
            f"{tf_file.name} names no association on the keeper account",
            f'service_account = "{keeper["name"]}"' not in body,
            tf_file.name,
        )
        expect(
            f"{tf_file.name} names no association on a default account",
            'service_account = "default"' not in body,
            tf_file.name,
        )


def main() -> int:
    with standalone():
        test_each_reader_role_binds_the_account_its_pods_run_as()
        test_external_dns_binds_the_account_the_appset_names()
        test_clickhouse_object_store_binds_the_account_the_chart_creates()
        test_keeper_gets_its_own_account_and_no_association()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
