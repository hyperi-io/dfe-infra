#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_clickhouse_app_identity.py
#  Purpose:      Prove no app workload holds the ClickHouse admin credential: each
#                connects as its own user, on a password of its own.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The admin credential stays with the engine, the one workload that administers ClickHouse.

    python3 -m pytest scripts/tests/test_clickhouse_app_identity.py -q

dfe-engine creates every user and grant, so it alone connects as the deploy layer's
admin account (docs/CLICKHOUSE-IDENTITY.md). Every other workload connects as the
user the engine made for it. What has to hold, and what each check reads:

1. No workload but the engine's names the admin Secret, in any golden render.
2. No workload but the engine's dials ClickHouse as the admin user.
3. The collector's gateway reads neither, on any profile or cloud, nor on an
   external server.
4. The admin password is copied into the app namespace alone, so the collector's
   namespace never holds it.

Needs `helm` on PATH. The golden checks read fixtures/thin-renders.
"""

import functools
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from _gate import CELLS, CLOUDS, PROFILES, WORKLOAD_KINDS, golden, object_id, references

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
CHARTS = REPO_ROOT / "helm" / "charts"
COMMON = yaml.safe_load((VALUES / "common.yaml").read_text(encoding="utf-8"))
ADMIN_USER = COMMON["clickhouse"]["user"]
ADMIN_SECRET = COMMON["clickhouse"]["passwordSecretName"]
# The one workload that administers ClickHouse.
ADMINISTERS = {"Deployment/dfe-engine"}
# Every variable a DFE workload names its ClickHouse user in.
USER_VARIABLES = (
    "CLICKHOUSE_USER",
    "CLICKHOUSE_USERNAME",
    "DFE_CLICKHOUSE_USERNAME",
    "DFE_LOADER_CLICKHOUSE_USERNAME",
)
APP_NAMESPACE = "dfe-test"
# An external server's cluster secret, as bootstrap.sh writes it for ClickHouse Cloud.
EXTERNAL = {
    "clickhouse": {
        "mode": "external",
        "host": "ch.example.com",
        "port": 8443,
        "nativePort": 9440,
        "user": ADMIN_USER,
        "tls": {"enabled": True, "port": 8443, "ca": {"secretName": "", "configMapName": ""}},
        "external": {"host": "ch.example.com", "port": 8443, "user": ADMIN_USER},
    }
}


def _workloads(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d.get("kind") in WORKLOAD_KINDS]


def _containers(doc: dict) -> list[dict]:
    pod = doc["spec"]["template"]["spec"]
    return (pod.get("initContainers") or []) + (pod.get("containers") or [])


def admin_secret_readers(docs: list[dict]) -> list[str]:
    """Each workload outside ADMINISTERS that names the admin Secret. Empty is a pass."""
    return sorted(
        where
        for kind, name, _optional, where in references(docs)
        if kind == "Secret" and name == ADMIN_SECRET and where.split(" ")[0] not in ADMINISTERS
    )


def admin_user_dialers(docs: list[dict]) -> list[str]:
    """Each container outside ADMINISTERS whose env, ConfigMaps included, names the admin user."""
    configmaps = {d["metadata"]["name"]: d.get("data") or {} for d in docs if d["kind"] == "ConfigMap"}
    found = []
    for doc in _workloads(docs):
        if object_id(doc) in ADMINISTERS:
            continue
        for container in _containers(doc):
            env = {}
            for source in container.get("envFrom") or []:
                env.update(configmaps.get((source.get("configMapRef") or {}).get("name"), {}))
            env.update({e["name"]: e.get("value") for e in container.get("env") or []})
            found += [
                f"{object_id(doc)} {container['name']} {name}={env[name]}"
                for name in USER_VARIABLES
                if env.get(name) == ADMIN_USER
            ]
    return sorted(found)


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), CELLS)
def test_no_app_reads_the_admin_password(service: str, profile: str, cloud: str, scenario: str) -> None:
    assert admin_secret_readers(golden(service, profile, cloud, scenario)) == []


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), CELLS)
def test_no_app_dials_as_the_admin_user(service: str, profile: str, cloud: str, scenario: str) -> None:
    assert admin_user_dialers(golden(service, profile, cloud, scenario)) == []


def test_the_engine_keeps_the_admin_password() -> None:
    """The control: the engine reads it, so the scan above is looking in the right place."""
    docs = golden("dfe-engine", "scale", "aws")
    named = {(kind, name, where.split(" ")[0]) for kind, name, _o, where in references(docs)}
    assert ("Secret", ADMIN_SECRET, "Deployment/dfe-engine") in named


def test_an_app_reading_the_admin_password_is_caught() -> None:
    docs = golden("dfe-loader", "scale", "aws")
    loader = next(d for d in docs if object_id(d) == "Deployment/dfe-loader")
    container = loader["spec"]["template"]["spec"]["containers"][0]
    container.setdefault("env", []).append(
        {"name": "X", "valueFrom": {"secretKeyRef": {"name": ADMIN_SECRET, "key": "password"}}}
    )
    container["env"].append({"name": "CLICKHOUSE_USER", "value": ADMIN_USER})
    assert admin_secret_readers(docs) == ["Deployment/dfe-loader dfe-loader env X"]
    assert admin_user_dialers(docs) == [f"Deployment/dfe-loader dfe-loader CLICKHOUSE_USER={ADMIN_USER}"]


# ------------------------------------------------------------------ the collector


@functools.cache
def _chart(chart: str, cloud: str, profile: str, namespace: str, overlay: str = "") -> tuple[dict, ...]:
    """A data-layer chart over the layer2-data cascade, an overlay layered last."""
    files = [VALUES / "common.yaml", VALUES / f"{cloud}.yaml", VALUES / f"profile-{profile}.yaml"]
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["helm", "template", chart, str(CHARTS / chart), "--namespace", namespace]
        if overlay:
            path = Path(tmp) / "overlay.yaml"
            path.write_text(overlay, encoding="utf-8")
            files.append(path)
        for path in files:
            if path.is_file():
                cmd += ["-f", str(path)]
        cmd += ["--set", f"appNamespace={APP_NAMESPACE}", "--set", "global.registry=registry.example.com/dfe"]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    assert out.returncode == 0, out.stderr
    return tuple(d for d in yaml.safe_load_all(out.stdout) if d)


def _gateway_config(docs: tuple[dict, ...]) -> dict:
    configmap = next(d for d in docs if d["kind"] == "ConfigMap" and "gateway-config.yaml" in d.get("data", {}))
    return yaml.safe_load(configmap["data"]["gateway-config.yaml"])


def collector_admin_use(docs: tuple[dict, ...]) -> list[str]:
    """Where the collector's render names the admin Secret or dials as the admin user."""
    found = [f"{where} -> {name}" for kind, name, _o, where in references(list(docs)) if name == ADMIN_SECRET]
    config = _gateway_config(docs)
    if config["exporters"]["clickhouse"]["username"] == ADMIN_USER:
        found.append(f"exporters.clickhouse.username={ADMIN_USER}")
    datasource = config["receivers"]["sqlquery/clickhouse"]["datasource"]
    if datasource.startswith(f"clickhouse://{ADMIN_USER}:"):
        found.append(f"receivers.sqlquery/clickhouse dials as {ADMIN_USER}")
    return found


@pytest.mark.parametrize(("profile", "cloud"), [(p, k) for p in PROFILES for k in CLOUDS])
def test_the_collector_neither_reads_nor_dials_as_the_admin(profile: str, cloud: str) -> None:
    assert collector_admin_use(_chart("otel-collector", cloud, profile, "otel")) == []


def test_the_collector_on_an_external_server_keeps_its_own_user() -> None:
    docs = _chart("otel-collector", "aws", "scale", "otel", yaml.safe_dump(EXTERNAL))
    assert collector_admin_use(docs) == []


# ------------------------------------------------------------- where the admin lands


def _admin_copy_namespaces(docs: tuple[dict, ...]) -> set[str]:
    copies = [
        d
        for d in docs
        if d["kind"] == "ClusterExternalSecret"
        and d["spec"]["externalSecretSpec"]["target"]["name"] == ADMIN_SECRET
    ]
    assert len(copies) == 1, sorted(object_id(d) for d in docs)
    return {s["matchLabels"]["kubernetes.io/metadata.name"] for s in copies[0]["spec"]["namespaceSelectors"]}


@pytest.mark.parametrize("overlay", ["", yaml.safe_dump(EXTERNAL)], ids=["in-cluster", "external"])
def test_the_admin_password_is_copied_into_the_app_namespace_alone(overlay: str) -> None:
    docs = _chart("clickhouse-cluster", "aws", "scale", "clickhouse", overlay)
    assert _admin_copy_namespaces(docs) == {APP_NAMESPACE}
