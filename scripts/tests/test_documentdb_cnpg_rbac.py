#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_documentdb_cnpg_rbac.py
#  Purpose:      Prove the documentdb chart grants CloudNativePG the EndpointSlice
#                read its bundled chart lacks, in the namespace the operator
#                addon installs it into.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for helm/charts/documentdb/templates/cnpg-rbac.yaml.

CloudNativePG 1.29.2+ watches EndpointSlices in its own namespace. The operator
addon runs 1.29.3 under the cloudnative-pg chart 0.28.3, whose RBAC predates that
watch, so without this grant the operator never syncs its caches and exits.

    python3 scripts/tests/test_documentdb_cnpg_rbac.py

Needs `helm` on PATH.
"""

import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LAYER1 = REPO_ROOT / "argocd" / "appsets" / "layer1-addons.yaml"
VERBS = {"get", "list", "watch"}


def render() -> list[dict]:
    cmd = ["helm", "template", "documentdb", str(chart_dir("documentdb")), "--namespace", "cnpg"]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for documentdb:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def operator_namespace_override() -> str | None:
    """The cloudnative-pg namespaceOverride the documentdb-operator element passes."""
    for doc in yaml.safe_load_all(LAYER1.read_text(encoding="utf-8")):
        for generator in (doc or {}).get("spec", {}).get("generators", []):
            for inner in generator.get("matrix", {}).get("generators", []):
                for element in inner.get("list", {}).get("elements", []):
                    if element.get("chart") == "documentdb-operator":
                        values = yaml.safe_load(element.get("chartValuesYaml") or "") or {}
                        return (values.get("cloudnative-pg") or {}).get("namespaceOverride")
    return None


def test_the_role_lands_where_the_operator_addon_installs_cloudnative_pg() -> None:
    override = operator_namespace_override()
    roles = [d for d in render() if d["kind"] == "Role" and "endpointslices" in str(d.get("rules"))]
    expect("the documentdb-operator element names a namespaceOverride", bool(override), f"got {override!r}")
    expect("one EndpointSlice Role renders", len(roles) == 1, f"got {len(roles)}")
    for role in roles:
        expect(
            "the Role is in the operator's namespace",
            role["metadata"]["namespace"] == override,
            f"Role in {role['metadata']['namespace']!r}, operator in {override!r}",
        )


def test_the_role_reads_endpointslices_and_nothing_else() -> None:
    (role,) = [d for d in render() if d["kind"] == "Role" and "endpointslices" in str(d.get("rules"))]
    rules = role["rules"]
    expect("the Role carries one rule", len(rules) == 1, f"got {rules}")
    rule = rules[0]
    expect("the rule is discovery.k8s.io", rule["apiGroups"] == ["discovery.k8s.io"], f"got {rule}")
    expect("the rule names endpointslices only", rule["resources"] == ["endpointslices"], f"got {rule}")
    expect("the rule only reads", set(rule["verbs"]) == VERBS, f"got {rule['verbs']}")


def test_the_binding_reaches_the_operator_whatever_argo_names_its_release() -> None:
    docs = render()
    (role,) = [d for d in docs if d["kind"] == "Role" and "endpointslices" in str(d.get("rules"))]
    namespace = role["metadata"]["namespace"]
    bindings = [
        d
        for d in docs
        if d["kind"] == "RoleBinding" and d["roleRef"]["name"] == role["metadata"]["name"]
    ]
    expect("one RoleBinding names the Role", len(bindings) == 1, f"got {len(bindings)}")
    for binding in bindings:
        expect("the binding sits beside the Role", binding["metadata"]["namespace"] == namespace)
        expect(
            "the binding's subject is the namespace's service accounts",
            binding["subjects"]
            == [
                {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "Group",
                    "name": f"system:serviceaccounts:{namespace}",
                }
            ],
            f"got {binding['subjects']}",
        )


def main() -> int:
    with standalone():
        test_the_role_lands_where_the_operator_addon_installs_cloudnative_pg()
        test_the_role_reads_endpointslices_and_nothing_else()
        test_the_binding_reaches_the_operator_whatever_argo_names_its_release()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
