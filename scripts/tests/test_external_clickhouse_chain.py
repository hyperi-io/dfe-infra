#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_external_clickhouse_chain.py
#  Purpose:      Prove an external ClickHouse travels from the aws root's outputs
#                through bootstrap and the cluster secret to every chart that
#                dials it, and that an in-cluster server moves nothing.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""An external ClickHouse, from the cluster secret to the charts.

    python3 -m pytest scripts/tests/test_external_clickhouse_chain.py -q

Wave 4 renders before the engine writes the deploy repo, so the mode has to be a
cluster fact: bootstrap.sh validates the aws root's DFE_CLICKHOUSE_* outputs and
writes them onto the cluster secret, and layer2-data, layer2-platform and
layer2-apps read them back. Each appset's `values` block is executed here with
Helm's own text/template, the engine renders the ApplicationSet controller uses,
then the chart is rendered with what it produced.

Needs helm on PATH; the loader and hyperdx renders also need the scalo-service
library (_weave.library). Every host is under the reserved example domain.
"""

import functools
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from _gate import effective_env, golden, object_id, references, render_differences
from _weave import render_app, render_extras

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
APPSETS = REPO_ROOT / "argocd" / "appsets"
VALUES = REPO_ROOT / "argocd" / "values"
CHARTS = REPO_ROOT / "helm" / "charts"

HOST = "ch.example.com"
APP_NAMESPACE = "dfe-aws"
CELL = ("scale", "aws")
BASE_CASCADE = [VALUES / "common.yaml", VALUES / "aws.yaml", VALUES / "profile-scale.yaml"]

# Each DFE_CLICKHOUSE_* fact and the annotation bootstrap writes it under.
FACTS = {
    "DFE_CLICKHOUSE_MODE": "dfe.hyperi.io/clickhouse_mode",
    "DFE_CLICKHOUSE_HOST": "dfe.hyperi.io/clickhouse_host",
    "DFE_CLICKHOUSE_PORT": "dfe.hyperi.io/clickhouse_port",
    "DFE_CLICKHOUSE_NATIVE_PORT": "dfe.hyperi.io/clickhouse_native_port",
    "DFE_CLICKHOUSE_SECURE": "dfe.hyperi.io/clickhouse_secure",
    "DFE_CLICKHOUSE_USER": "dfe.hyperi.io/clickhouse_user",
}

# The cluster secret bootstrap.sh writes for ClickHouse Cloud.
EXTERNAL = {
    "dfe.hyperi.io/clickhouse_mode": "external",
    "dfe.hyperi.io/clickhouse_host": HOST,
    "dfe.hyperi.io/clickhouse_port": "8443",
    "dfe.hyperi.io/clickhouse_native_port": "9440",
    "dfe.hyperi.io/clickhouse_secure": "true",
    "dfe.hyperi.io/clickhouse_user": "default",
}

# And for the in-cluster server: every external fact empty.
IN_CLUSTER = {
    "dfe.hyperi.io/clickhouse_mode": "in_cluster",
    "dfe.hyperi.io/clickhouse_host": "dfe-clickhouse.clickhouse.svc.cluster.local",
    "dfe.hyperi.io/clickhouse_port": "",
    "dfe.hyperi.io/clickhouse_native_port": "",
    "dfe.hyperi.io/clickhouse_secure": "",
    "dfe.hyperi.io/clickhouse_user": "",
}

# A BYO server dialled in plaintext on its own ports, which the dial allows.
PLAINTEXT = {
    **EXTERNAL,
    "dfe.hyperi.io/clickhouse_port": "8123",
    "dfe.hyperi.io/clickhouse_native_port": "9000",
    "dfe.hyperi.io/clickhouse_secure": "false",
    "dfe.hyperi.io/clickhouse_user": "dfe_writer",
}


# ---------------------------------------------------------------------- bootstrap


def _bootstrap_block() -> str:
    """bootstrap.sh's own ClickHouse block, run on its own rather than copied here."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = lines.index('export DFE_CLICKHOUSE_MODE="${DFE_CLICKHOUSE_MODE:-}"')
    end = lines.index("export DFE_CLICKHOUSE_USER")
    return "\n".join(lines[start : end + 1])


def _run_block(**env: str) -> subprocess.CompletedProcess:
    printed = " ".join(f'"${{{name}}}"' for name in FACTS)
    script = _bootstrap_block() + f"\nprintf '%s|' {printed}"
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def test_bootstrap_carries_every_external_fact() -> None:
    env = {name: EXTERNAL[annotation] for name, annotation in FACTS.items()}
    done = _run_block(**env)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split("|")[:-1] == list(env.values())


def test_bootstrap_defaults_an_older_roots_missing_facts_empty() -> None:
    """The local root and a root predating these outputs set none of them."""
    done = _run_block(DFE_CLICKHOUSE_HOST="dfe-clickhouse.clickhouse.svc.cluster.local")
    assert done.returncode == 0, done.stderr
    assert done.stdout.split("|")[:-1] == ["", "dfe-clickhouse.clickhouse.svc.cluster.local", "", "", "", ""]


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"DFE_CLICKHOUSE_MODE": "cloud"}, "DFE_CLICKHOUSE_MODE"),
        ({"DFE_CLICKHOUSE_MODE": "external", "DFE_CLICKHOUSE_PORT": "",
          "DFE_CLICKHOUSE_NATIVE_PORT": "9440", "DFE_CLICKHOUSE_SECURE": "true"}, "DFE_CLICKHOUSE_PORT"),
        ({"DFE_CLICKHOUSE_MODE": "external", "DFE_CLICKHOUSE_PORT": "8443",
          "DFE_CLICKHOUSE_NATIVE_PORT": "native", "DFE_CLICKHOUSE_SECURE": "true"}, "DFE_CLICKHOUSE_NATIVE_PORT"),
        ({"DFE_CLICKHOUSE_MODE": "external", "DFE_CLICKHOUSE_PORT": "8443",
          "DFE_CLICKHOUSE_NATIVE_PORT": "9440", "DFE_CLICKHOUSE_SECURE": "yes"}, "DFE_CLICKHOUSE_SECURE"),
    ],
)
def test_expected_fail_bootstrap_refuses_a_fact_no_chart_could_dial(env: dict[str, str], message: str) -> None:
    done = _run_block(DFE_CLICKHOUSE_HOST=HOST, DFE_CLICKHOUSE_USER="default", **env)
    assert done.returncode == 1, done.stdout
    assert message in done.stderr


def test_the_cluster_secret_carries_every_fact() -> None:
    template = CLUSTER_SECRET.read_text(encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", **{name: EXTERNAL[annotation] for name, annotation in FACTS.items()}}
    for name, annotation in FACTS.items():
        assert f'{annotation}: "${{{name}}}"' in template, annotation
    rendered = subprocess.run(
        ["envsubst"], input=template, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    annotations = yaml.safe_load(rendered)["metadata"]["annotations"]
    assert {annotation: annotations[annotation] for annotation in FACTS.values()} == EXTERNAL


# ---------------------------------------------------------------------- the appsets


@functools.cache
def appset_values(appset: str, index: int, facts: tuple[tuple[str, str], ...], app: str = "") -> dict:
    """One appset source's `values` block, executed for the cluster secret's facts."""
    doc = yaml.safe_load((APPSETS / appset).read_text(encoding="utf-8"))
    block = doc["spec"]["template"]["spec"]["sources"][index]["helm"]["values"]
    params = {
        "metadata": {"annotations": dict(facts), "labels": {}},
        "app": app,
        "deploy": {"service": app or "dfe-engine", "instance": "default"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        chart = Path(tmp) / "appset"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: appset\nversion: 0.0.0\n", encoding="utf-8")
        (chart / "templates" / "_block.tpl").write_text(
            '{{- define "block" -}}{{- with .Values }}\n' + block + "\n{{- end }}{{- end -}}\n",
            encoding="utf-8",
        )
        (chart / "templates" / "out.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: appset\ndata:\n"
            '  values.yaml: |\n{{ include "block" . | trim | indent 4 }}\n',
            encoding="utf-8",
        )
        params_file = Path(tmp) / "params.yaml"
        params_file.write_text(yaml.safe_dump(params), encoding="utf-8")
        out = subprocess.run(
            ["helm", "template", "appset", str(chart), "-f", str(params_file)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    assert out.returncode == 0, out.stderr
    configmap = next(d for d in yaml.safe_load_all(out.stdout) if d)
    return yaml.safe_load(configmap["data"]["values.yaml"] or "") or {}


def render_chart(chart: str, overlay: dict, namespace: str) -> list[dict]:
    """The chart over the scale/aws cascade, with an appset's block layered last."""
    with tempfile.TemporaryDirectory() as tmp:
        overlay_file = Path(tmp) / "overlay.yaml"
        overlay_file.write_text(yaml.safe_dump(overlay), encoding="utf-8")
        cmd = ["helm", "template", chart, str(CHARTS / chart), "--namespace", namespace]
        for path in [*BASE_CASCADE, overlay_file]:
            cmd += ["-f", str(path)]
        cmd += [
            "--set", f"appNamespace={APP_NAMESPACE}", "--set", "env=test", "--set", "cloud=aws",
            "--set", "global.registry=registry.example.com/dfe", "--set", f"dfeNamespaces[0]={APP_NAMESPACE}",
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def data_overlay(facts: dict[str, str]) -> dict:
    return appset_values("layer2-data.yaml", 0, tuple(sorted(facts.items())))


def test_an_in_cluster_server_moves_no_data_chart() -> None:
    assert "clickhouse" not in data_overlay(IN_CLUSTER)
    assert "clickhouse" not in data_overlay({})


def test_external_deploys_no_clickhouse_of_its_own() -> None:
    docs = render_chart("clickhouse-cluster", data_overlay(EXTERNAL), "clickhouse")
    kinds = {d["kind"] for d in docs}
    assert not kinds & {"ClickHouseCluster", "KeeperCluster", "StatefulSet", "Service", "Certificate"}, kinds
    in_cluster = {d["kind"] for d in render_chart("clickhouse-cluster", data_overlay(IN_CLUSTER), "clickhouse")}
    assert "ClickHouseCluster" in in_cluster


def test_external_projects_its_password_where_the_apps_and_the_collector_read_it() -> None:
    docs = render_chart("clickhouse-cluster", data_overlay(EXTERNAL), "clickhouse")
    copies = [
        d for d in docs
        if d["kind"] == "ClusterExternalSecret"
        and d["spec"]["externalSecretSpec"]["target"]["name"] == "clickhouse-admin-password"
    ]
    assert len(copies) == 1, [object_id(d) for d in docs]
    spec = copies[0]["spec"]
    namespaces = {s["matchLabels"]["kubernetes.io/metadata.name"] for s in spec["namespaceSelectors"]}
    assert namespaces == {APP_NAMESPACE, "otel"}
    assert spec["externalSecretSpec"]["secretStoreRef"] == {"name": "dfe-secret-store", "kind": "ClusterSecretStore"}
    assert spec["externalSecretSpec"]["data"] == [
        {"secretKey": "password", "remoteRef": {"key": "dfe/test/clickhouse/external", "property": "password"}}
    ]


def test_external_drops_the_in_cluster_credential_and_ca_copies() -> None:
    docs = render_chart("clickhouse-cluster", data_overlay(EXTERNAL), "clickhouse")
    kinds = {object_id(d) for d in docs}
    assert not any(k.startswith(("Password/", "ClusterSecretStore/", "Role/")) for k in kinds), kinds


def collector_config(overlay: dict) -> dict:
    docs = render_chart("otel-collector", overlay, "otel")
    configmap = next(d for d in docs if d["kind"] == "ConfigMap" and "gateway-config.yaml" in d.get("data", {}))
    return yaml.safe_load(configmap["data"]["gateway-config.yaml"])


def test_the_collector_writes_to_the_external_server_over_tls() -> None:
    config = collector_config(data_overlay(EXTERNAL))
    exporter = config["exporters"]["clickhouse"]
    assert exporter["endpoint"] == f"tcp://{HOST}:9440?secure=true"
    assert exporter["username"] == "default"
    datasource = config["receivers"]["sqlquery/clickhouse"]["datasource"]
    assert f"@{HOST}:9440/dfe?secure=true" in datasource


def test_a_plaintext_external_server_is_dialled_without_tls() -> None:
    config = collector_config(data_overlay(PLAINTEXT))
    assert config["exporters"]["clickhouse"]["endpoint"] == f"tcp://{HOST}:9000"
    assert config["exporters"]["clickhouse"]["username"] == "dfe_writer"


def test_the_in_cluster_collector_keeps_its_plaintext_native_port() -> None:
    config = collector_config(data_overlay(IN_CLUSTER))
    profile = yaml.safe_load((VALUES / "profile-scale.yaml").read_text(encoding="utf-8"))
    assert config["exporters"]["clickhouse"]["endpoint"] == f"tcp://{profile['clickhouse']['host']}:9000"


def test_external_opens_the_apps_egress_on_the_servers_ports() -> None:
    overlay = appset_values("layer2-platform.yaml", 0, tuple(sorted(EXTERNAL.items())), app="network-policies")
    docs = render_chart("network-policies", overlay, APP_NAMESPACE)
    policies = [d for d in docs if d["kind"] == "NetworkPolicy" and "clickhouse" in d["metadata"]["name"]]
    assert policies, sorted(object_id(d) for d in docs)
    ports = {p["port"] for d in policies for rule in d["spec"]["egress"] for p in rule.get("ports", [])}
    assert {8443, 9440} <= ports
    in_cluster = appset_values("layer2-platform.yaml", 0, tuple(sorted(IN_CLUSTER.items())), app="network-policies")
    assert "clickhouse" not in in_cluster


def test_a_byo_servers_own_ports_are_the_ones_opened() -> None:
    overlay = appset_values("layer2-platform.yaml", 0, tuple(sorted(PLAINTEXT.items())), app="network-policies")
    docs = render_chart("network-policies", overlay, APP_NAMESPACE)
    policies = [d for d in docs if d["kind"] == "NetworkPolicy" and "clickhouse" in d["metadata"]["name"]]
    ports = {p["port"] for d in policies for rule in d["spec"]["egress"] for p in rule.get("ports", [])}
    assert {8123, 9000} <= ports


# ---------------------------------------------------------------------- the apps


def _engine_env(facts: dict[str, str]) -> dict[str, str]:
    docs = render_extras("dfe-engine", *CELL, annotations=facts)
    return next(d for d in docs if object_id(d) == "ConfigMap/dfe-engine-env")["data"]


def test_both_app_sources_read_the_same_facts() -> None:
    """The thin chart and dfe-extras each take the block, so neither dials a different server."""
    facts = tuple(sorted(EXTERNAL.items()))
    assert appset_values("layer2-apps.yaml", 0, facts)["clickhouse"] == appset_values(
        "layer2-apps.yaml", 1, facts
    )["clickhouse"]


def test_the_engine_dials_the_external_server_over_verified_tls() -> None:
    env = _engine_env(EXTERNAL)
    assert env["DFE_CLICKHOUSE_HOST"] == HOST
    assert env["DFE_CLICKHOUSE_PORT"] == "8443"
    assert env["DFE_CLICKHOUSE_SECURE"] == "true"
    assert env["DFE_CLICKHOUSE_VERIFY"] == "true"
    assert env["DFE_CLICKHOUSE_USERNAME"] == "default"
    # ClickHouse Cloud's certificate chains to a public root, so no CA is mounted.
    assert "DFE_CLICKHOUSE_CA_CERT" not in env


def test_the_engine_dials_a_plaintext_server_in_plaintext() -> None:
    env = _engine_env(PLAINTEXT)
    assert (env["DFE_CLICKHOUSE_PORT"], env["DFE_CLICKHOUSE_SECURE"]) == ("8123", "false")
    assert env["DFE_CLICKHOUSE_USERNAME"] == "dfe_writer"


@functools.cache
def _app(service: str, facts: tuple[tuple[str, str], ...]) -> tuple[dict, ...]:
    return tuple(render_app(service, *CELL, annotations=dict(facts)))


def test_the_loader_inserts_into_the_external_server_with_the_system_roots() -> None:
    docs = list(_app("dfe-loader", tuple(sorted(EXTERNAL.items()))))
    env = effective_env(docs, "Deployment/dfe-loader")
    assert env["DFE_LOADER_CLICKHOUSE_HOSTS"] == f"{HOST}:8443"
    assert env["DFE_LOADER_CLICKHOUSE_USERNAME"] == "default"
    assert "SSL_CERT_FILE" not in env


def test_hyperdx_refuses_webhooks_at_the_external_server() -> None:
    docs = list(_app("hyperdx", tuple(sorted(EXTERNAL.items()))))
    env = effective_env(docs, "Deployment/dfe-hyperdx")
    assert env["CLICKHOUSE_HOST"] == f"http://{HOST}:8443"
    assert "NODE_EXTRA_CA_CERTS" not in env


@pytest.mark.parametrize("service", ["dfe-engine", "dfe-loader", "hyperdx"])
def test_no_app_reads_the_in_cluster_ca_an_external_server_never_writes(service: str) -> None:
    """The volume falls back to an optional ConfigMap, so only a Secret named for
    the in-cluster CA would point a client at a certificate that is not there."""
    docs = list(_app(service, tuple(sorted(EXTERNAL.items()))))
    named = {(kind, name) for kind, name, _optional, _where in references(docs)}
    assert ("Secret", "dfe-clickhouse-ca") not in named
    assert ("Secret", "clickhouse-admin-password") in named


@pytest.mark.parametrize("service", ["dfe-engine", "dfe-loader", "hyperdx"])
def test_an_in_cluster_server_moves_no_app(service: str) -> None:
    docs = list(_app(service, tuple(sorted(IN_CLUSTER.items()))))
    assert render_differences(golden(service, *CELL), docs) == []
