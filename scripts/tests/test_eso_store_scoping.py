#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_eso_store_scoping.py
#  Purpose:      Prove every kubernetes-provider ClusterSecretStore in the repo
#                is fenced to the namespaces its own copies land in, so an
#                ExternalSecret anywhere else cannot read what its Role names.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A ClusterSecretStore with no `conditions` serves every namespace in the
cluster, not just the one its own ClusterExternalSecret copies into. The kafka
single-user store and the ferretdb source store both carried this gap; the
clickhouse admin store carried it before dfe-infra#477.

This file does two things:

1. Sweeps every chart under helm/charts and helm/edge for a template that
   DEFINES `kind: ClusterSecretStore` at document root, and pins that set to
   the three known definitions -- so a fourth one added later fails this test
   until it is covered here, the same closed-set discipline `_charts.py`
   documents for its own chart-tree sweep.
2. For each of the three, renders the chart and asserts the store's
   `conditions.namespaces` equals exactly the namespaces its
   ClusterExternalSecret(s) copy into -- derived from the same chart values,
   so the two cannot disagree.

    python3 scripts/tests/test_eso_store_scoping.py

Needs `helm` on PATH. Runs under pytest too, which is how CI reaches it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from _charts import CHART_TREES
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The only templates in the repo that define `kind: ClusterSecretStore` at
# document root, as of this file's writing. dfe-secret-store (bootstrap's own
# ClusterSecretStore, applied outside helm) already carries conditions and is
# out of scope here -- it is not a chart template.
KNOWN_STORE_TEMPLATES = {
    REPO_ROOT / "helm/charts/clickhouse-cluster/templates/clickhouse-admin-appns.yaml",
    REPO_ROOT / "helm/charts/kafka/templates/kafka-single-user-appns.yaml",
    REPO_ROOT / "helm/charts/ferretdb/templates/auth.yaml",
}


def defines_cluster_secret_store(path: Path) -> bool:
    """True if `path` declares `kind: ClusterSecretStore` at document root.

    Column-zero only: a reference such as `secretStoreRef: kind:
    ClusterSecretStore` is indented and must not count.
    """
    return any(
        line == "kind: ClusterSecretStore"
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def test_every_cluster_secret_store_definition_is_covered_here() -> None:
    found = {
        path
        for tree in CHART_TREES
        for path in tree.rglob("templates/*.yaml")
        if defines_cluster_secret_store(path)
    }
    expect(
        "the known set is exactly every ClusterSecretStore-defining template",
        found == KNOWN_STORE_TEMPLATES,
        f"missing from the known set: {found - KNOWN_STORE_TEMPLATES}, "
        f"stale in the known set: {KNOWN_STORE_TEMPLATES - found}",
    )


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )


def render(chart: str, *sets: str, namespace: str = "default") -> tuple[dict, ...]:
    cmd = ["helm", "template", chart, str(REPO_ROOT / "helm" / "charts" / chart),
           "--namespace", namespace]
    for s in sets:
        cmd += ["--set", s]
    out = _run(cmd)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart} {sets}:\n{out.stderr}")
    return tuple(d for d in yaml.safe_load_all(out.stdout) if d)


def one(docs: tuple[dict, ...], kind: str, name: str | None = None) -> dict | None:
    return next(
        (d for d in docs if d.get("kind") == kind and (name is None or d["metadata"]["name"] == name)),
        None,
    )


def every(docs: tuple[dict, ...], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def selected(copy: dict) -> set[str]:
    return {s["matchLabels"]["kubernetes.io/metadata.name"] for s in copy["spec"]["namespaceSelectors"]}


def role_secrets(docs: tuple[dict, ...], role: str) -> set[str]:
    found = one(docs, "Role", role)
    return {n for rule in (found or {}).get("rules", []) for n in rule.get("resourceNames", [])}


def store_role(docs: tuple[dict, ...], store: dict) -> str | None:
    """The Role bound to the ServiceAccount a store authenticates as."""
    account = store["spec"]["provider"]["kubernetes"]["auth"]["serviceAccount"]["name"]
    for binding in every(docs, "RoleBinding"):
        if any(s["kind"] == "ServiceAccount" and s["name"] == account for s in binding["subjects"]):
            return binding["roleRef"]["name"]
    return None


def assert_store_matches_its_copies(docs: tuple[dict, ...], label: str) -> None:
    stores = every(docs, "ClusterSecretStore")
    expect(f"{label}: renders its ClusterSecretStore", bool(stores), "none rendered")
    for store in stores:
        name = store["metadata"]["name"]
        conditions = store["spec"].get("conditions") or []
        allowed = {n for c in conditions for n in c.get("namespaces", [])}
        copies = [
            c for c in every(docs, "ClusterExternalSecret")
            if c["spec"]["externalSecretSpec"]["secretStoreRef"]["name"] == name
        ]
        targets = set().union(*(selected(c) for c in copies)) if copies else set()
        expect(f"{label}: {name} carries namespace conditions", bool(conditions), "none")
        expect(
            f"{label}: {name} admits exactly the namespaces its copies land in",
            allowed == targets and bool(targets),
            f"conditions {allowed}, copies {targets}",
        )


# --- kafka --------------------------------------------------------------


def test_kafka_single_store_serves_only_its_app_namespace() -> None:
    docs = render("kafka", "kafka.mode=single", "appNamespace=dfe", namespace="kafka")
    assert_store_matches_its_copies(docs, "kafka default")
    role = role_secrets(docs, store_role(docs, one(docs, "ClusterSecretStore")) or "")
    expect(
        "kafka: the Role names only the user's own Secret",
        role == {"dfe-kafka-user"},
        f"got {sorted(role)}",
    )


def test_kafka_single_store_grows_with_extra_app_namespaces() -> None:
    docs = render(
        "kafka", "kafka.mode=single", "appNamespace=dfe",
        "user.password.extraAppNamespaces={otel,audit}", namespace="kafka",
    )
    assert_store_matches_its_copies(docs, "kafka extra namespaces")
    conditions = one(docs, "ClusterSecretStore")["spec"]["conditions"]
    allowed = {n for c in conditions for n in c["namespaces"]}
    expect(
        "kafka: conditions include every extra namespace",
        allowed == {"dfe", "otel", "audit"},
        f"got {sorted(allowed)}",
    )


def test_kafka_cluster_mode_renders_no_single_tier_store() -> None:
    """cluster mode has its own store (clusterexternalsecret-appns.yaml); this
    file's `if` must not also fire and double up the fence."""
    docs = render("kafka", "kafka.mode=cluster", "kafka.provider=strimzi",
                  "appNamespace=dfe", namespace="kafka")
    stores = [
        s for s in every(docs, "ClusterSecretStore")
        if s["metadata"]["name"].endswith("-broker-ns")
    ]
    expect("kafka: cluster mode renders no single-tier store", not stores, f"got {stores}")


# --- ferretdb -------------------------------------------------------------


def test_ferretdb_store_serves_only_its_app_namespace() -> None:
    docs = render("ferretdb", "ferretdb.mode=deploy", "appNamespace=dfe-ui", namespace="ferretdb")
    assert_store_matches_its_copies(docs, "ferretdb default")
    role = role_secrets(docs, store_role(docs, one(docs, "ClusterSecretStore")) or "")
    expect(
        "ferretdb: the Role names only the backend password Secret",
        role == {"dfe-ferretdb-password"},
        f"got {sorted(role)}",
    )


def test_ferretdb_renders_no_store_with_no_app_namespace() -> None:
    """No cross-namespace copy needed -> no store, no unfenced surface at all."""
    docs = render("ferretdb", "ferretdb.mode=deploy", namespace="ferretdb")
    expect(
        "ferretdb: no appNamespace renders no ClusterSecretStore",
        not every(docs, "ClusterSecretStore"),
        f"got {every(docs, 'ClusterSecretStore')}",
    )


# --- clickhouse-cluster (dfe-infra#477, pinned so it cannot regress) --------


def test_clickhouse_admin_store_still_serves_only_its_copies() -> None:
    docs = render("clickhouse-cluster", "appNamespace=dfe", namespace="clickhouse")
    assert_store_matches_its_copies(docs, "clickhouse-cluster")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    raise SystemExit(main())
