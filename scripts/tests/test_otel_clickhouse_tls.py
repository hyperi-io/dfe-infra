#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_otel_clickhouse_tls.py
#  Purpose:      Prove the OTel collector dials ClickHouse's native TLS port and
#                verifies the server, and that the CA reaches its namespace
#                without the admin credential.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The collector's ClickHouse exporter and sql_query receiver over TLS.

    python3 -m pytest scripts/tests/test_otel_clickhouse_tls.py

Needs `helm` on PATH.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
COLLECTOR = REPO / "helm" / "charts" / "otel-collector"
SERVER = REPO / "helm" / "charts" / "clickhouse-cluster"
CA_DIR = "/etc/dfe-clickhouse-ca"
TLS_ON = (
    "clickhouse.tls.enabled=true",
    "clickhouse.tls.internalCA.present=true",
    "clickhouse.tls.ca.secretName=dfe-clickhouse-ca",
)


def helm(chart: Path, *sets: str, files: tuple[Path, ...] = ()) -> list[dict]:
    cmd = ["helm", "template", "x", str(chart)]
    for f in files:
        cmd += ["-f", str(f)]
    for item in sets:
        cmd += ["--set", item]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    assert out.returncode == 0, f"helm template failed for {sets}:\n{out.stderr}"
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def gateway(*sets: str) -> tuple[dict, dict]:
    """(collector config, gateway Deployment) with the ClickHouse exporter and receiver on."""
    docs = helm(COLLECTOR, "clusterTelemetry.clickhouse.enabled=true", *sets)
    (configmap,) = [d for d in docs if d["kind"] == "ConfigMap" and "gateway-config.yaml" in (d.get("data") or {})]
    (deployment,) = [d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-gateway")]
    return yaml.safe_load(configmap["data"]["gateway-config.yaml"]), deployment


def pod(deployment: dict) -> dict:
    return deployment["spec"]["template"]["spec"]


def test_plaintext_stays_plaintext_without_a_ca() -> None:
    config, deployment = gateway()
    exporter = config["exporters"]["clickhouse"]
    assert exporter["endpoint"].endswith(":9000")
    assert "tls" not in exporter
    assert ":9000/" in config["receivers"]["sql_query/clickhouse"]["datasource"]
    assert "clickhouse-ca" not in {v["name"] for v in pod(deployment).get("volumes", [])}


def test_tls_on_dials_the_secure_port_and_verifies_with_the_mounted_ca() -> None:
    config, deployment = gateway(*TLS_ON)
    exporter = config["exporters"]["clickhouse"]
    assert exporter["endpoint"].endswith(":9440?secure=true")
    assert exporter["tls"] == {"ca_file": f"{CA_DIR}/ca.crt"}
    datasource = config["receivers"]["sql_query/clickhouse"]["datasource"]
    assert ":9440/dfe?secure=true" in datasource
    assert "skip_verify" not in datasource
    container = pod(deployment)["containers"][0]
    assert {"name": "SSL_CERT_DIR", "value": CA_DIR} in container["env"]
    assert {"name": "clickhouse-ca", "mountPath": CA_DIR, "readOnly": True} in container["volumeMounts"]
    (volume,) = [v for v in pod(deployment)["volumes"] if v["name"] == "clickhouse-ca"]
    assert volume["secret"]["secretName"] == "dfe-clickhouse-ca"


def test_a_cluster_with_no_ca_to_sign_with_stays_plaintext() -> None:
    config, _ = gateway("clickhouse.tls.enabled=true", "clickhouse.tls.ca.secretName=dfe-clickhouse-ca")
    assert config["exporters"]["clickhouse"]["endpoint"].endswith(":9000")


def test_an_explicit_issuer_is_a_ca_to_sign_with() -> None:
    config, _ = gateway("clickhouse.tls.enabled=true", "clickhouse.tls.issuerRef.name=corp", "clickhouse.tls.ca.secretName=c")
    assert config["exporters"]["clickhouse"]["endpoint"].endswith(":9440?secure=true")


def test_verification_off_skips_it_in_both_clients_and_mounts_no_ca() -> None:
    config, deployment = gateway(*TLS_ON, "clickhouse.tls.verify=false")
    assert config["exporters"]["clickhouse"]["tls"] == {"insecure_skip_verify": True}
    assert "skip_verify=true" in config["receivers"]["sql_query/clickhouse"]["datasource"]
    assert "clickhouse-ca" not in {v["name"] for v in pod(deployment).get("volumes", [])}


def test_an_external_server_keeps_its_port_and_takes_a_named_bundle() -> None:
    external = ("clickhouse.mode=external", "clickhouse.nativePort=9440", "clickhouse.tls.enabled=true")
    config, deployment = gateway(*external)
    assert config["exporters"]["clickhouse"]["endpoint"].endswith(":9440?secure=true")
    assert "tls" not in config["exporters"]["clickhouse"]
    config, deployment = gateway(*external, "clickhouse.tls.ca.configMapName=corp-bundle")
    assert config["exporters"]["clickhouse"]["tls"] == {"ca_file": f"{CA_DIR}/ca.crt"}
    (volume,) = [v for v in pod(deployment)["volumes"] if v["name"] == "clickhouse-ca"]
    assert volume["configMap"]["name"] == "corp-bundle"


# --- the server side ---------------------------------------------------------


def server(*sets: str) -> list[dict]:
    values = REPO / "argocd" / "values"
    return helm(SERVER, "appNamespace=dfe-local", *sets, files=(values / "common.yaml", values / "profile-single.yaml"))


def kinds(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d["kind"] == kind]


def test_single_mode_serves_the_native_protocol_over_tls_beside_https() -> None:
    docs = server()
    (statefulset,) = kinds(docs, "StatefulSet")
    ports = {p["name"]: p["containerPort"] for p in statefulset["spec"]["template"]["spec"]["containers"][0]["ports"]}
    assert ports["native-tls"] == 9440
    (service,) = [d for d in kinds(docs, "Service") if d["spec"].get("clusterIP") == "None"]
    assert {"name": "native-tls", "port": 9440, "targetPort": "native-tls"} in service["spec"]["ports"]
    config = next(d for d in kinds(docs, "ConfigMap") if "dfe-tls.xml" in d["data"])["data"]["dfe-tls.xml"]
    assert "<tcp_secure>" in config
    assert "<port>9440</port>" in config


def test_the_collector_namespace_gets_the_ca_through_a_store_that_cannot_read_the_admin_secret() -> None:
    docs = server()
    (store,) = [d for d in kinds(docs, "ClusterSecretStore") if d["metadata"]["name"] == "dfe-clickhouse-ca-source-ns"]
    assert store["spec"]["conditions"] == [{"namespaces": ["otel"]}]
    (role,) = [d for d in kinds(docs, "Role") if d["metadata"]["name"] == "dfe-clickhouse-ca-reader"]
    (rule,) = role["rules"]
    assert rule["resourceNames"] == ["dfe-clickhouse-tls-ca"]
    assert rule["verbs"] == ["get"]
    (copy,) = [d for d in kinds(docs, "ClusterExternalSecret") if d["metadata"]["name"] == "dfe-clickhouse-ca-extra"]
    assert copy["spec"]["externalSecretSpec"]["secretStoreRef"]["name"] == "dfe-clickhouse-ca-source-ns"
    assert copy["spec"]["namespaceSelectors"] == [{"matchLabels": {"kubernetes.io/metadata.name": "otel"}}]


@pytest.mark.parametrize("sets", [("clickhouse.tls.ca.extraNamespaces=null",), ("clickhouse.tls.enabled=false",)])
def test_no_extra_namespace_or_no_tls_renders_no_extra_copy(sets: tuple[str, ...]) -> None:
    names = {d["metadata"]["name"] for d in server(*sets)}
    assert "dfe-clickhouse-ca-extra" not in names
    assert "dfe-clickhouse-ca-source-ns" not in names
