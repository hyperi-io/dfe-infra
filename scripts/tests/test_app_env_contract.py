#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_app_env_contract.py
#  Purpose:      Prove each dfe-* app deployment renders the env var NAMES its
#                app actually reads, and none that nothing reads.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered env names against the settings the apps actually read.

An env var no app reads renders green, lints green and passes a server-side
dry-run: the API server has no opinion about a name no process reads. The app
keeps its built-in default and reports nothing, so the defect surfaces as an app
not doing its job, never as a config error.

CONTRACT is the apps' side of that, one row per name: when the deployment renders
it, and the file:line in the app's own repo that reads it. A `scalo-rs` path is
in the scalo release that app's Cargo.lock pins -- dfe-transform-elastic reads
bare KAFKA_* through scalo's KafkaConfig::from_env, and every app reads OTEL_*
through scalo. CI has no app checkout, so the rows are kept by hand: a rename
upstream fails HERE, which makes it a decision rather than a silent drift.

A name counts as rendered only with a value: every reader here takes an empty
value as unset. The env asserted is the effective env -- the thin chart's own
entries over the `<fullname>-env` ConfigMap dfe-extras renders for dfe-receiver
and dfe-loader -- read from the goldens under fixtures/thin-renders on the bus
(single, scale) and direct (slim, mesh) profiles, which test_thin_renders.py holds
equal to what Argo deploys. The dials, an overlay's own TLS, and the managed and
in-cluster cluster secrets render live through scripts/dfe-weave, which executes
layer2-apps' values block for the cluster secret's annotations.

Rows cover only what the deployment is responsible for. The archiver's S3 names
belong to test_config_authority.py and an unnamed secretKeyRef to
test_inert_chart_env.py. dfe-ui runs no scalo, so its OTLP endpoint and exporter
are asserted on their own, with the egress that endpoint needs.

The Kafka wire protocol follows one dial, `kafka.securityProtocol`. A TLS
listener turns TLS on in every app; a plaintext one renders nothing to the apps
whose reader would switch TLS off with it, so an overlay's own TLS stands.

    python3 scripts/tests/test_app_env_contract.py

Runs standalone or under pytest. Needs `helm` on PATH and the scalo-service
library (_weave.library) for the live renders.
"""

import functools
import json
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary
from _gate import CLOUDS, configmap_files, effective_env, golden, object_id
from _weave import CONTRACTS, render_app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMMON_VALUES = REPO_ROOT / "argocd" / "values" / "common.yaml"
SCALE_VALUES = REPO_ROOT / "argocd" / "values" / "profile-scale.yaml"
APP_VALUES = REPO_ROOT / "argocd" / "values" / "apps"
LAYER2_APPS = REPO_ROOT / "argocd" / "appsets" / "layer2-apps.yaml"

APPS = (
    "dfe-archiver",
    "dfe-fetcher",
    "dfe-loader",
    "dfe-receiver",
    "dfe-transform-elastic",
    "dfe-transform-vector",
    "dfe-transform-vrl",
)

# When a name renders: on the bus, on the direct transport, on both, or only once
# a deployment turns the dial that feeds it.
BUS = "bus"
DIRECT = "direct"
ALWAYS = "always"
DIALLED = "dialled"

# The profiles each transport runs on, read from their goldens.
BUS_PROFILES = ("single", "scale")
DIRECT_PROFILES = ("slim", "mesh")

# An overlay that turned its own TLS on beneath common.yaml's plaintext listener.
OVERLAY_TLS = "overlay-tls"

# Each rendered through layer2-apps' values block: a managed broker's cluster
# secret, and an in-cluster one.
MANAGED = "managed"
IN_CLUSTER = "in-cluster"

# Every annotation layer2-apps' values block reads, as bootstrap.sh writes it --
# always present, empty where the fact does not apply. The endpoint is an RFC
# 2606 placeholder: this repo ships publicly.
CLUSTER_ANNOTATIONS = {
    MANAGED: {
        "dfe.hyperi.io/kafka_mode": "external",
        "dfe.hyperi.io/kafka_bootstrap": "b-1.example.invalid:9096,b-2.example.invalid:9096",
        "dfe.hyperi.io/kafka_security_protocol": "SASL_SSL",
        "dfe.hyperi.io/kafka_message_max_bytes": "16777216",
        "dfe.hyperi.io/image_pull_secret": "",
    },
    IN_CLUSTER: {
        "dfe.hyperi.io/kafka_mode": "",
        "dfe.hyperi.io/kafka_bootstrap": "dfe-kafka-kafka-bootstrap.strimzi.svc.cluster.local:9092",
        "dfe.hyperi.io/kafka_security_protocol": "",
        "dfe.hyperi.io/kafka_message_max_bytes": "",
        "dfe.hyperi.io/image_pull_secret": "",
    },
}

# The deployer's instance values behind every DIALLED row, set together on the bus.
DIALS = {
    "versionCheck": {
        "enabled": False,
        "sendInstanceId": False,
        "apiUrl": "https://releases.example.com/check",
        "instanceId": "example-install",
    },
    "kafka": {
        "sourceTopic": "example_land",
        "destTopic": "example_load",
        "securityProtocol": "SASL_SSL",
    },
}
# The apps whose integration values mount a transforms file set.
TRANSFORM_FILES = {"fileSets": {"transforms": {"files": [{"name": "example.vrl", "content": "."}]}}}
WITH_TRANSFORM_FILES = ("dfe-transform-vector", "dfe-transform-vrl")

OVERLAY_TLS_VALUES = {
    "config": {
        "kafka": {"tls": {"enabled": True}},
        "source": {"tls": {"enabled": True}},
        "sink": {"tls": {"enabled": True}},
    }
}

# Where each live scenario renders.
DIAL_CELL = ("single", "local")
MANAGED_CELL = ("scale", "aws")

# The two transforms read their TLS switch from DFE_TRANSFORM_KAFKA_SECURITY_PROTOCOL
# one way only -- an SSL value turns source and sink TLS on, any other leaves the
# file's setting -- so a plaintext value reaching them is safe.
ONE_WAY_TLS = ("dfe-transform-vector", "dfe-transform-vrl")

# The apps whose reader sets TLS from an env value in BOTH directions, so a
# plaintext value rendered to either switches TLS off.
TLS_BOTH_WAYS = ("dfe-fetcher", "dfe-receiver")

# The scalo cascade keys under version_check (scalo-rs 2.13 src/version_check/mod.rs:186).
VERSION_CHECK_KEYS = ("ENABLED", "SEND_INSTANCE_ID", "API_URL", "INSTANCE_ID")


def _version_check(app: str, prefix: str, where: str) -> tuple[tuple[str, str, str, str], ...]:
    """The four VERSION_CHECK__ rows for an app whose cascade reads `prefix`."""
    names = [f"{prefix}VERSION_CHECK__{key}" for key in VERSION_CHECK_KEYS]
    return tuple((app, DIALLED, name, where) for name in names)


# The four apps known to set up scalo's cascade under --config.
VERSION_CHECK = (
    *_version_check("dfe-archiver", "ARCHIVER_", "crates/archiver/src/config/loader.rs:40"),
    *_version_check("dfe-loader", "DFE_LOADER_", "src/main.rs:79"),
    *_version_check("dfe-transform-vrl", "DFE_TRANSFORM_", "src/cli.rs:93"),
    *_version_check("dfe-transform-elastic", "DFE_TRANSFORM_ELASTIC_", "src/config/loader.rs:151"),
)


def _in_every_app(name: str, where: str) -> tuple[tuple[str, str, str, str], ...]:
    """One ALWAYS row per app, for a name scalo reads the same way in each."""
    return tuple((app, ALWAYS, name, where) for app in APPS)


# Every app runs scalo's CLI and exporters, which read these bare.
SCALO_OTEL = (
    *_in_every_app("OTEL_EXPORTER_OTLP_ENDPOINT", "scalo-rs 2.13 src/cli/args.rs:126"),
    *_in_every_app("OTEL_SERVICE_NAME", "scalo-rs 2.13 src/metrics/otel.rs:70"),
)

# (app, when, env name, where the app reads it), for the names each app reads itself.
APP_ROWS = (
    # Bare KAFKA_*/DLQ_*/ARCHIVER_*, applied after the file at
    # crates/archiver/src/config/loader.rs:89.
    ("dfe-archiver", BUS, "KAFKA_BROKERS", "crates/core/src/config.rs:587"),
    ("dfe-archiver", BUS, "KAFKA_SECURITY_PROTOCOL", "crates/core/src/config.rs:608"),
    ("dfe-archiver", BUS, "KAFKA_SASL_USER", "crates/core/src/config.rs:611"),
    ("dfe-archiver", BUS, "KAFKA_SASL_PASSWORD", "crates/core/src/config.rs:614"),
    ("dfe-archiver", BUS, "KAFKA_SASL_MECHANISM", "crates/core/src/config.rs:605"),
    ("dfe-archiver", BUS, "DLQ_TOPIC", "crates/core/src/config.rs:632"),
    ("dfe-archiver", BUS, "DLQ_MODE", "crates/core/src/config.rs:636"),
    ("dfe-archiver", DIRECT, "ARCHIVER_TRANSPORT", "crates/core/src/config.rs:577"),
    ("dfe-archiver", DIRECT, "ARCHIVER_GRPC_LISTEN", "crates/core/src/config.rs:580"),
    ("dfe-archiver", DIRECT, "DLQ_ENABLED", "crates/core/src/config.rs:629"),
    ("dfe-archiver", ALWAYS, "ARCHIVER_SPOOL_DIR", "crates/core/src/config.rs:676"),
    # DFE_FETCHER_*, applied after the file at crates/fetcher/src/config/mod.rs:354.
    ("dfe-fetcher", BUS, "DFE_FETCHER_KAFKA_BROKERS", "crates/fetcher/src/config/mod.rs:989"),
    ("dfe-fetcher", BUS, "DFE_FETCHER_KAFKA_SASL_USER", "crates/fetcher/src/config/mod.rs:935"),
    ("dfe-fetcher", BUS, "DFE_FETCHER_KAFKA_SASL_PASSWORD", "crates/fetcher/src/config/mod.rs:939"),
    ("dfe-fetcher", BUS, "DFE_FETCHER_KAFKA_SASL_MECHANISM",
     "crates/fetcher/src/config/mod.rs:926"),
    ("dfe-fetcher", BUS, "DFE_FETCHER_DLQ_TOPIC", "crates/fetcher/src/config/mod.rs:981"),
    ("dfe-fetcher", BUS, "DFE_FETCHER_DLQ_MODE", "crates/fetcher/src/config/mod.rs:985"),
    ("dfe-fetcher", DIRECT, "DFE_FETCHER_DLQ_ENABLED", "crates/fetcher/src/config/mod.rs:975"),
    ("dfe-fetcher", DIALLED, "DFE_FETCHER_KAFKA_SECURITY_PROTOCOL",
     "crates/fetcher/src/config/mod.rs:932"),
    # DFE_LOADER_*: flat names applied at src/config/loader.rs:423; TRANSPORT and the
    # `__` names reach their fields through the figment layer, which trims the
    # separator a DFE_LOADER__ name leaves behind (src/config/loader.rs:392).
    ("dfe-loader", BUS, "DFE_LOADER_KAFKA_BROKERS", "src/config/loader.rs:223"),
    ("dfe-loader", BUS, "DFE_LOADER__KAFKA__SASL__USERNAME", "src/config/loader.rs:392"),
    ("dfe-loader", BUS, "DFE_LOADER__KAFKA__SASL__PASSWORD", "src/config/loader.rs:392"),
    ("dfe-loader", BUS, "DFE_LOADER_KAFKA_SASL_MECHANISM", "src/config/loader.rs:275"),
    ("dfe-loader", BUS, "DFE_LOADER_DLQ_TOPIC", "src/config/loader.rs:273"),
    ("dfe-loader", DIALLED, "DFE_LOADER_KAFKA_SECURITY_PROTOCOL", "src/config/loader.rs:247"),
    ("dfe-loader", DIRECT, "DFE_LOADER_TRANSPORT", "src/config/loader.rs:61"),
    ("dfe-loader", DIRECT, "DFE_LOADER_GRPC__LISTEN", "src/config/kafka.rs:96"),
    ("dfe-loader", ALWAYS, "DFE_LOADER_DLQ_MODE", "src/config/loader.rs:276"),
    ("dfe-loader", ALWAYS, "DFE_LOADER_CLICKHOUSE_HOSTS", "src/config/loader.rs:256"),
    ("dfe-loader", ALWAYS, "DFE_LOADER_CLICKHOUSE_DATABASE", "src/config/loader.rs:259"),
    ("dfe-loader", ALWAYS, "DFE_LOADER_CLICKHOUSE_USERNAME", "src/config/loader.rs:262"),
    ("dfe-loader", ALWAYS, "DFE_LOADER__CLICKHOUSE__PASSWORD", "src/config/loader.rs:392"),
    ("dfe-loader", ALWAYS, "DFE_LOADER_CLICKHOUSE__PROTOCOL", "src/config/loader.rs:148"),
    # common.yaml's clickhouse.tls mounts the ClickHouse CA; the loader's ClickHouse and
    # release-check clients load their roots through rustls-native-certs (Cargo.lock 0.8.4).
    ("dfe-loader", ALWAYS, "SSL_CERT_FILE", "rustls-native-certs 0.8.4 src/lib.rs:200"),
    # DFE_RECEIVER_*, applied after the file at src/config/mod.rs:210. The bind
    # address is the app's own default; test_the_receiver_listens_where_its_service_sends.
    ("dfe-receiver", BUS, "DFE_RECEIVER_KAFKA_BROKERS", "src/config/mod.rs:613"),
    ("dfe-receiver", BUS, "DFE_RECEIVER_KAFKA_SASL_USER", "src/config/mod.rs:626"),
    ("dfe-receiver", BUS, "DFE_RECEIVER_KAFKA_SASL_PASSWORD", "src/config/mod.rs:630"),
    ("dfe-receiver", BUS, "DFE_RECEIVER_KAFKA_SASL_MECHANISM", "src/config/mod.rs:619"),
    ("dfe-receiver", BUS, "DFE_RECEIVER_DLQ_TOPIC", "src/config/mod.rs:647"),
    ("dfe-receiver", BUS, "DFE_RECEIVER_DLQ_MODE", "src/config/mod.rs:650"),
    ("dfe-receiver", DIRECT, "DFE_RECEIVER_DLQ_ENABLED", "src/config/mod.rs:644"),
    ("dfe-receiver", DIALLED, "DFE_RECEIVER_KAFKA_SECURITY_PROTOCOL", "src/config/mod.rs:623"),
    # Bare KAFKA_*, the fallback scalo's from_env builds at config.rs:1880, called
    # from src/service.rs:894.
    ("dfe-transform-elastic", BUS, "KAFKA_BOOTSTRAP_SERVERS",
     "scalo-rs 2.13.0 src/transport/kafka/config.rs:1893"),
    ("dfe-transform-elastic", BUS, "KAFKA_CONSUMER_PROTOCOL",
     "scalo-rs 2.13.0 src/transport/kafka/config.rs:1924"),
    ("dfe-transform-elastic", BUS, "KAFKA_SECURITY_PROTOCOL",
     "scalo-rs 2.13.0 src/transport/kafka/config.rs:1941"),
    ("dfe-transform-elastic", BUS, "KAFKA_SASL_MECHANISM",
     "scalo-rs 2.13.0 src/transport/kafka/config.rs:1945"),
    ("dfe-transform-elastic", BUS, "KAFKA_SASL_USERNAME",
     "scalo-rs 2.13.0 src/transport/kafka/config.rs:1949"),
    ("dfe-transform-elastic", BUS, "KAFKA_SASL_PASSWORD",
     "scalo-rs 2.13.0 src/transport/kafka/config.rs:1953"),
    # The source's broker list, applied after the file at src/config/loader.rs:182.
    ("dfe-transform-elastic", BUS, "DFE_TRANSFORM_ELASTIC_SOURCE_BROKERS",
     "src/config/loader.rs:258"),
    # DFE_TRANSFORM_* -- the shared transform prefix, not a per-app one -- applied
    # after the file at src/config/loader.rs:864.
    ("dfe-transform-vector", BUS, "DFE_TRANSFORM_SOURCE_BROKERS", "src/config/loader.rs:697"),
    ("dfe-transform-vector", BUS, "DFE_TRANSFORM_SINK_BROKERS", "src/config/loader.rs:723"),
    ("dfe-transform-vector", BUS, "DFE_TRANSFORM_SOURCE_GROUP_ID", "src/config/loader.rs:703"),
    ("dfe-transform-vector", BUS, "DFE_TRANSFORM_SOURCE_SASL_USERNAME",
     "src/config/loader.rs:706"),
    ("dfe-transform-vector", BUS, "DFE_TRANSFORM_SOURCE_SASL_PASSWORD",
     "src/config/loader.rs:709"),
    ("dfe-transform-vector", BUS, "DFE_TRANSFORM_SINK_SASL_USERNAME", "src/config/loader.rs:738"),
    ("dfe-transform-vector", BUS, "DFE_TRANSFORM_SINK_SASL_PASSWORD", "src/config/loader.rs:741"),
    ("dfe-transform-vector", BUS, "DFE_TRANSFORM_KAFKA_SECURITY_PROTOCOL",
     "src/config/loader.rs:715"),
    ("dfe-transform-vector", ALWAYS, "DFE_TRANSFORM_VECTOR_DATA_DIR", "src/config/loader.rs:768"),
    ("dfe-transform-vector", DIALLED, "DFE_TRANSFORM_SOURCE_TOPICS", "src/config/loader.rs:700"),
    ("dfe-transform-vector", DIALLED, "DFE_TRANSFORM_SINK_TOPIC", "src/config/loader.rs:726"),
    ("dfe-transform-vector", DIALLED, "DFE_TRANSFORM_TRANSFORMS_DIR", "src/config/loader.rs:760"),
    # The same prefix, applied after the file at src/config/loader.rs:703.
    ("dfe-transform-vrl", BUS, "DFE_TRANSFORM_SOURCE_BROKERS", "src/config/loader.rs:563"),
    ("dfe-transform-vrl", BUS, "DFE_TRANSFORM_SINK_BROKERS", "src/config/loader.rs:585"),
    ("dfe-transform-vrl", BUS, "DFE_TRANSFORM_SOURCE_GROUP_ID", "src/config/loader.rs:569"),
    ("dfe-transform-vrl", BUS, "DFE_TRANSFORM_SOURCE_SASL_USERNAME", "src/config/loader.rs:572"),
    ("dfe-transform-vrl", BUS, "DFE_TRANSFORM_SOURCE_SASL_PASSWORD", "src/config/loader.rs:575"),
    ("dfe-transform-vrl", BUS, "DFE_TRANSFORM_SINK_SASL_USERNAME", "src/config/loader.rs:597"),
    ("dfe-transform-vrl", BUS, "DFE_TRANSFORM_SINK_SASL_PASSWORD", "src/config/loader.rs:600"),
    ("dfe-transform-vrl", BUS, "DFE_TRANSFORM_KAFKA_SECURITY_PROTOCOL",
     "src/config/loader.rs:576"),
    ("dfe-transform-vrl", DIALLED, "DFE_TRANSFORM_SOURCE_TOPICS", "src/config/loader.rs:566"),
    ("dfe-transform-vrl", DIALLED, "DFE_TRANSFORM_SINK_TOPIC", "src/config/loader.rs:588"),
    ("dfe-transform-vrl", DIALLED, "DFE_TRANSFORM_TRANSFORMS_DIR", "src/config/loader.rs:604"),
)

CONTRACT = (*APP_ROWS, *VERSION_CHECK, *SCALO_OTEL)

# Each env-reading app's own name for the wire protocol, from the rows above.
PROTOCOL_ENV = {
    app: name for app, _, name, _ in CONTRACT if name.endswith("KAFKA_SECURITY_PROTOCOL")
}

# dfe-engine carries no data, but on the bus it dials the same broker to create
# each source's topics (dfe-engine src/dfe_engine/settings.py:1945).
ENGINE_PROTOCOL = {"dfe-engine": "DFE_KAFKA_SECURITY_PROTOCOL"}

# Names a deployment rendered once and no app reads. A regression here is invisible
# in a cluster, so it is asserted rather than left to review. dfe-transform-elastic
# is absent from the first two sets because from_env reads both names there.
_NOT_FROM_ENV = tuple(app for app in APPS if app != "dfe-transform-elastic")
RETIRED = (
    *((app, "KAFKA_BOOTSTRAP_SERVERS") for app in _NOT_FROM_ENV),
    *((app, "KAFKA_PROVIDER") for app in _NOT_FROM_ENV),
    ("dfe-fetcher", "DFE_FETCHER_API_KEY"),
    ("dfe-fetcher", "DFE_FETCHER_API_SECRET"),
)

LIVE_SCENARIOS = (DIALLED, OVERLAY_TLS, MANAGED, IN_CLUSTER)


@functools.cache
def appset_values(scenario: str) -> str:
    """layer2-apps' `values` block, executed for the scenario's cluster secret.

    Helm runs the same text/template and sprig library the ApplicationSet
    controller renders goTemplate with, so the block is executed, not read.
    """
    doc = yaml.safe_load(LAYER2_APPS.read_text(encoding="utf-8"))
    block = doc["spec"]["template"]["spec"]["sources"][0]["helm"]["values"]
    # The git generator's deploy block, which every Application carries; dfe-engine
    # takes no per-service key from the block.
    deploy = {"service": "dfe-engine", "instance": "default"}
    annotations = yaml.safe_dump(
        {"metadata": {"annotations": CLUSTER_ANNOTATIONS[scenario]}, "deploy": deploy}
    )
    with tempfile.TemporaryDirectory() as tmp:
        chart = Path(tmp) / "appset"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text(
            "apiVersion: v2\nname: appset\nversion: 0.0.0\n", encoding="utf-8", newline="\n"
        )
        # The block reads .metadata.annotations, so .Values is its dot.
        (chart / "templates" / "_block.tpl").write_text(
            '{{- define "block" -}}{{- with .Values }}\n' + block + "\n{{- end }}{{- end -}}\n",
            encoding="utf-8",
            newline="\n",
        )
        (chart / "templates" / "out.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: appset\ndata:\n"
            '  values.yaml: |\n{{ include "block" . | trim | indent 4 }}\n',
            encoding="utf-8",
            newline="\n",
        )
        (Path(tmp) / "annotations.yaml").write_text(annotations, encoding="utf-8", newline="\n")
        out = subprocess.run(
            ["helm", "template", "appset", str(chart), "-f", str(Path(tmp) / "annotations.yaml")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    if out.returncode != 0:
        raise SystemExit(f"layer2-apps' values block failed [{scenario}]:\n{out.stderr}")
    configmap = next(d for d in yaml.safe_load_all(out.stdout) if d)
    return configmap["data"]["values.yaml"] or ""


def dials(app: str) -> dict:
    """The DIALLED instance values for one app."""
    overlay = json.loads(json.dumps(DIALS))
    if app in WITH_TRANSFORM_FILES:
        overlay.update(json.loads(json.dumps(TRANSFORM_FILES)))
    return overlay


@functools.cache
def _live(app: str, scenario: str) -> tuple[dict, ...]:
    if scenario == DIALLED:
        docs = render_app(app, *DIAL_CELL, overlay=dials(app))
    elif scenario == OVERLAY_TLS:
        docs = render_app(app, *DIAL_CELL, overlay=OVERLAY_TLS_VALUES)
    else:
        docs = render_app(app, *MANAGED_CELL, annotations=CLUSTER_ANNOTATIONS[scenario])
    return tuple(docs)


def live(app: str, scenario: str) -> list[dict]:
    """One app rendered live for a scenario, once per run."""
    return list(_live(app, scenario))


def renders(app: str, scenario: str) -> list[tuple[str, list[dict]]]:
    """Every render a scenario covers for one app, each labelled with its cell."""
    if scenario == BUS:
        return [(f"{p}/{c}", golden(app, p, c)) for p in BUS_PROFILES for c in CLOUDS]
    if scenario == DIRECT:
        return [(f"{p}/{c}", golden(app, p, c)) for p in DIRECT_PROFILES for c in CLOUDS]
    return [(scenario, live(app, scenario))]


def env_of(docs: list[dict], app: str) -> dict[str, object]:
    """The app container's effective env: the thin chart's entries over its envFrom."""
    return effective_env(docs, f"Deployment/{app}")


def has_value(value: object) -> bool:
    """A literal that is not empty, or a valueFrom reference."""
    return value not in (None, "")


def config_file(docs: list[dict], app: str) -> dict:
    """The app's config file as the thin chart writes it into <app>-config."""
    configmap = next(d for d in docs if object_id(d) == f"ConfigMap/{app}-config")
    (text,) = configmap_files(configmap).values()
    return yaml.safe_load(text) or {}


def names_read(*when: str) -> dict[str, dict[str, str]]:
    """CONTRACT rows for these moments, as app -> {name: where it is read}."""
    wanted: dict[str, dict[str, str]] = {}
    for app, row_when, name, where in CONTRACT:
        if row_when in when:
            wanted.setdefault(app, {})[name] = where
    return wanted


def expect_rendered(scenario: str, *when: str) -> None:
    for app, wanted in sorted(names_read(*when).items()):
        for at, docs in renders(app, scenario):
            env = env_of(docs, app)
            absent = [name for name in sorted(wanted) if not has_value(env.get(name))]
            missing = [f"{name} ({wanted[name]})" for name in absent]
            expect(
                f"{app} renders every name its app reads [{scenario} {at}]",
                not missing,
                f"missing {missing}",
            )


def test_every_app_renders_the_names_it_reads_on_the_bus() -> None:
    expect_rendered(BUS, BUS, ALWAYS)


def test_every_app_renders_the_names_it_reads_on_the_direct_transport() -> None:
    expect_rendered(DIRECT, DIRECT, ALWAYS)


def test_a_dial_that_is_set_renders_the_name_its_app_reads() -> None:
    expect_rendered(DIALLED, DIALLED)


def test_the_contract_covers_every_app() -> None:
    """An app dropped from the table would pass every check above by having none."""
    covered = {app for app, *_ in APP_ROWS}
    expect("every app has contract rows", covered == set(APPS), f"got {sorted(covered)}")


def test_no_app_renders_a_name_nothing_reads() -> None:
    for scenario in (BUS, DIRECT, *LIVE_SCENARIOS):
        for app in APPS:
            retired = {name for owner, name in RETIRED if owner == app}
            for at, docs in renders(app, scenario):
                back = sorted(retired & set(env_of(docs, app)))
                expect(f"{app} renders no retired name [{scenario} {at}]", not back, f"got {back}")


def test_the_receiver_listens_where_its_service_sends() -> None:
    """The contract binds the http port from server.bind_address, which defaults to
    0.0.0.0:8080 (dfe-receiver src/config/mod.rs:1006), so no deployment value moves it."""
    contract = json.loads((CONTRACTS / "dfe-receiver.json").read_text(encoding="utf-8"))
    http = next(p for p in contract["extra_ports"] if p["name"] == "http")
    expect(
        "the contract binds http on 8080 from server.bind_address",
        (http["port"], http.get("bound_from")) == (8080, "server.bind_address"),
        f"got {http!r}",
    )
    for scenario in (BUS, DIRECT):
        for at, docs in renders("dfe-receiver", scenario):
            dep = next(d for d in docs if object_id(d) == "Deployment/dfe-receiver")
            ports = {
                p["name"]: p["containerPort"]
                for p in dep["spec"]["template"]["spec"]["containers"][0]["ports"]
            }
            expect(f"the receiver pod opens http on 8080 [{at}]", ports.get("http") == 8080,
                   f"got {ports!r}")
            server = config_file(docs, "dfe-receiver").get("server") or {}
            bind = env_of(docs, "dfe-receiver").get("DFE_RECEIVER_BIND_ADDRESS")
            expect(
                f"and nothing moves the app off its default bind address [{at}]",
                "bind_address" not in server and bind in (None, "", "0.0.0.0:8080"),
                f"got file {server!r}, env {bind!r}",
            )


def test_a_tls_listener_turns_tls_on_in_every_app() -> None:
    """One dial names the listener, so an app it does not reach dials TLS in the clear."""
    expect("every app takes its protocol from the dial", set(PROTOCOL_ENV) == set(APPS),
           f"got {sorted(PROTOCOL_ENV)}")
    for app, name in sorted(PROTOCOL_ENV.items()):
        got = env_of(live(app, DIALLED), app).get(name)
        expect(f"{app} renders {name}=SASL_SSL", got == "SASL_SSL", f"got {got!r}")


def test_a_plaintext_listener_switches_no_tls_off() -> None:
    """common.yaml names a plaintext listener, and an overlay's own TLS outlives it.

    The two-way readers take a plaintext value as tls.enabled false, so the dial
    reaches them only when it names TLS. The transforms read it one way, and the
    overlay's TLS in their config file stands beside it.
    """
    for app in TLS_BOTH_WAYS:
        name = PROTOCOL_ENV[app]
        got = env_of(live(app, OVERLAY_TLS), app).get(name)
        expect(f"{app} renders no {name} value for a plaintext listener", not has_value(got),
               f"got {got!r}, which would switch TLS off")
    for app in ONE_WAY_TLS:
        docs = live(app, OVERLAY_TLS)
        got = str(env_of(docs, app).get(PROTOCOL_ENV[app]) or "")
        expect(f"{app} is handed no SSL protocol for a plaintext listener",
               "SSL" not in got.upper(), f"got {got!r}")
        written = config_file(docs, app)
        for side in ("source", "sink"):
            enabled = ((written.get(side) or {}).get("tls") or {}).get("enabled")
            expect(f"{app} keeps the overlay's {side}.tls", enabled is True, f"got {enabled!r}")


def test_a_managed_broker_turns_tls_on_in_every_app() -> None:
    """Every managed provider serves TLS only, and nothing else on a managed
    deploy's path moves common.yaml's plain listener off the apps."""
    block = yaml.safe_load(appset_values(MANAGED)) or {}
    got = (block.get("kafka") or {}).get("securityProtocol")
    expect(
        "layer2-apps lands the cluster secret's protocol on kafka.securityProtocol",
        got == "SASL_SSL",
        f"got {got!r}",
    )
    for app, name in sorted({**PROTOCOL_ENV, **ENGINE_PROTOCOL}.items()):
        got = env_of(live(app, MANAGED), app).get(name)
        expect(f"{app} renders {name}=SASL_SSL [{MANAGED}]", got == "SASL_SSL", f"got {got!r}")


def test_an_in_cluster_broker_keeps_the_plain_listener() -> None:
    """bootstrap.sh leaves the managed annotations empty for strimzi and redpanda,
    so the block writes no kafka key and common.yaml's dial stands."""
    block = yaml.safe_load(appset_values(IN_CLUSTER)) or {}
    expect(
        "layer2-apps writes no kafka key for an in-cluster broker",
        "kafka" not in block,
        f"got {block.get('kafka')!r}",
    )
    dial = yaml.safe_load(COMMON_VALUES.read_text(encoding="utf-8"))["kafka"]["securityProtocol"]
    for app, name in sorted({**PROTOCOL_ENV, **ENGINE_PROTOCOL}.items()):
        got = env_of(live(app, IN_CLUSTER), app).get(name)
        expect(
            f"{app} renders {name} as {dial} or not at all [{IN_CLUSTER}]",
            got in (None, "", dial),
            f"got {got!r}",
        )
    for app in ONE_WAY_TLS:
        written = config_file(live(app, IN_CLUSTER), app)
        for side in ("source", "sink"):
            expect(
                f"{app} writes no {side}.tls [{IN_CLUSTER}]",
                "tls" not in (written.get(side) or {}),
                f"got {(written.get(side) or {}).get('tls')!r}",
            )


def test_no_provider_key_overrides_the_protocol_dial() -> None:
    """The scale tier names provider strimzi, which scalo maps to SASL_SSL.

    dfe-transform-elastic applies a provider over its protocol when it builds the
    transport (scalo-rs 2.13.0 src/transport/kafka/mod.rs:696), and that tier's
    broker serves 9092 without TLS (helm/charts/kafka/templates/kafka.yaml).
    """
    scale = yaml.safe_load(SCALE_VALUES.read_text(encoding="utf-8"))
    expect("the scale tier still names a provider", bool(scale["kafka"].get("provider")),
           "no provider, so this render proves nothing")
    dial = yaml.safe_load(COMMON_VALUES.read_text(encoding="utf-8"))["kafka"]["securityProtocol"]
    for cloud in CLOUDS:
        env = env_of(golden("dfe-transform-elastic", "scale", cloud), "dfe-transform-elastic")
        expect(f"elastic gets no KAFKA_PROVIDER on the scale tier [{cloud}]",
               "KAFKA_PROVIDER" not in env, f"got {env.get('KAFKA_PROVIDER')!r}")
        expect(f"and takes the dial's protocol [{cloud}]",
               env.get("KAFKA_SECURITY_PROTOCOL") == dial,
               f"got {env.get('KAFKA_SECURITY_PROTOCOL')!r}")


def integration_values(app: str) -> list[tuple[str, dict]]:
    """Each argocd/values/apps/<app> file, the values that wire the app's thin chart."""
    return [
        (path.name, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        for path in sorted((APP_VALUES / app).glob("*.yaml"))
    ]


def test_no_app_chart_offers_a_provider_dial() -> None:
    """A dial left in an app's values is still an offer, even with nothing reading it."""
    for app in APPS:
        files = integration_values(app)
        expect(f"{app} has integration values", bool(files), f"none under {APP_VALUES / app}")
        for name, values in files:
            kafka = values.get("kafka") or {}
            expect(f"{app}/{name} offers no kafka.provider", "provider" not in kafka,
                   f"got {kafka.get('provider')!r}")


def test_every_kafka_credential_comes_from_the_declared_secret() -> None:
    """A template naming another Secret mounts nothing the deployment provisioned.

    The failure names the env entries only: what they reference stays out of the
    output, so no Secret coordinate reaches a CI log.
    """
    for app in APPS:
        declared = dict(integration_values(app))["values.yaml"]["kafka"]["saslSecretName"]
        for at, docs in renders(app, BUS):
            mounted = []
            strays = []
            for name, value in env_of(docs, app).items():
                ref = (value.get("valueFrom") or {}).get("secretKeyRef") if isinstance(
                    value, dict) else None
                if "SASL" not in name or not ref:
                    continue
                mounted.append(name)
                if ref.get("name") != declared:
                    strays.append(name)
            expect(f"{app} mounts its Kafka credential from a Secret [{at}]", bool(mounted),
                   "none rendered")
            expect(
                f"{app} reads it from kafka.saslSecretName [{at}]",
                not strays,
                f"{sorted(strays)} name a different Secret",
            )


def test_the_scalo_apps_push_otlp_to_the_grpc_port() -> None:
    """scalo exports over gRPC unless told otherwise, and no app deployment tells it.

    scalo-rs 2.13 src/metrics/otel_types.rs:95 and src/otel_tracing/mod.rs:120 default
    both exporters to gRPC, which the collector serves on 4317 and not on 4318.
    """
    for app in APPS:
        for at, docs in renders(app, BUS):
            env = env_of(docs, app)
            endpoint = str(env.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""))
            expect(
                f"{app} pushes OTLP to the gRPC port [{at}]",
                endpoint.endswith(":4317"),
                f"got {endpoint!r}",
            )
            expect(
                f"{app} leaves scalo on its gRPC exporter [{at}]",
                "OTEL_EXPORTER_OTLP_PROTOCOL" not in env,
                f"got {env.get('OTEL_EXPORTER_OTLP_PROTOCOL')!r}",
            )


def ui_env(overlay: dict | None = None) -> dict[str, object]:
    """dfe-ui's effective env on the bus: the golden, or a live render under an overlay."""
    if overlay is None:
        docs = golden("dfe-ui", *DIAL_CELL)
    else:
        docs = render_app("dfe-ui", *DIAL_CELL, overlay=overlay)
    return env_of(docs, "dfe-ui")


def test_the_ui_pushes_otlp_to_the_http_port() -> None:
    """dfe-ui exports over HTTP only, which the collector serves on 4318 and not on 4317.

    dfe-ui pins @vercel/otel 2.1.3 (apps/dfe-core-ui/package.json), whose only trace
    exporters are http/protobuf and http/json, posted to {endpoint}/v1/traces. A
    deployer endpoint on the gRPC port moves to the HTTP port; any other is kept as given.
    """
    derived = "http://dfe-otel-collector-gateway.otel.svc.cluster.local:4318"
    cases = (
        ("the derived collector", None, derived),
        (
            "a deployer's gRPC port",
            {"telemetry": {"hyperdxEndpoint": "otlp.example.com:4317"}},
            "http://otlp.example.com:4318",
        ),
        (
            "a deployer's own port",
            {"otel": {"endpoint": "https://otlp.example.com"}},
            "https://otlp.example.com",
        ),
    )
    for case, overlay, wanted in cases:
        env = ui_env(overlay)
        endpoint = env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        expect(
            f"dfe-ui pushes OTLP to the HTTP port [{case}]",
            endpoint == wanted,
            f"got {endpoint!r}",
        )
        expect(
            f"dfe-ui selects the http/protobuf exporter [{case}]",
            env.get("OTEL_EXPORTER_OTLP_PROTOCOL") == "http/protobuf",
            f"got {env.get('OTEL_EXPORTER_OTLP_PROTOCOL')!r}",
        )


def network_policies() -> list[dict]:
    """The network-policies chart under common.yaml, the file every appset reads first."""
    chart = chart_dir("network-policies")
    cmd = ["helm", "template", chart.name, str(chart), "-f", str(COMMON_VALUES)]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart.name}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def test_the_ui_can_reach_the_otlp_port_it_is_handed() -> None:
    """The baseline egress is default-deny, so a collector port it leaves shut loses
    every export at connect."""
    endpoint = urllib.parse.urlsplit(str(ui_env().get("OTEL_EXPORTER_OTLP_ENDPOINT", "")))
    labels = (endpoint.hostname or "").split(".")
    in_cluster = len(labels) > 2 and labels[2] == "svc"
    expect("dfe-ui is handed an in-cluster collector", in_cluster, f"got {endpoint.geturl()!r}")
    if not in_cluster:
        return
    namespace = labels[1]
    values = chart_dir("network-policies") / "values.yaml"
    app_namespaces = set(yaml.safe_load(values.read_text(encoding="utf-8"))["dfeNamespaces"])
    policies = [
        d
        for d in network_policies()
        if d.get("kind") == "NetworkPolicy"
        and d["metadata"]["name"] == "allow-baseline-egress"
        and d["metadata"]["namespace"] in app_namespaces
    ]
    expect("the app namespaces' baseline egress renders", bool(policies), "none rendered")
    for policy in policies:
        opened = set()
        for rule in policy["spec"]["egress"]:
            peers = [
                (to.get("namespaceSelector") or {}).get("matchLabels") or {}
                for to in rule.get("to") or []
            ]
            if any(p.get("kubernetes.io/metadata.name") == namespace for p in peers):
                opened |= {p["port"] for p in rule.get("ports") or []}
        expect(
            f"{policy['metadata']['namespace']} egress opens the collector port dfe-ui is handed",
            endpoint.port in opened,
            f"{namespace} opens {sorted(opened)}, dfe-ui is handed {endpoint.port}",
        )


def test_the_fetcher_reads_the_deployment_brokers() -> None:
    """DFE_FETCHER_KAFKA_BROKERS sets kafka.brokers after the file is read
    (dfe-fetcher crates/fetcher/src/config/mod.rs:989), unless the instance's own
    config names a broker list, which then stands alone."""
    common = yaml.safe_load(COMMON_VALUES.read_text(encoding="utf-8"))["kafka"]
    for at, docs in renders("dfe-fetcher", BUS):
        profile = at.split("/")[0]
        profile_values = REPO_ROOT / "argocd" / "values" / f"profile-{profile}.yaml"
        kafka = yaml.safe_load(profile_values.read_text(encoding="utf-8")).get("kafka") or {}
        wanted = kafka.get("bootstrapServers", common["bootstrapServers"])
        got = env_of(docs, "dfe-fetcher").get("DFE_FETCHER_KAFKA_BROKERS")
        expect(f"the fetcher is handed the deployment's broker list [{at}]", got == wanted,
               f"got {got!r}")
    own = {"config": {"kafka": {"brokers": ["chosen.example.test:9092"]}}}
    docs = render_app("dfe-fetcher", *DIAL_CELL, overlay=own)
    got = env_of(docs, "dfe-fetcher").get("DFE_FETCHER_KAFKA_BROKERS")
    expect("an instance naming its own brokers gets no broker env", not has_value(got),
           f"got {got!r}")
    brokers = (config_file(docs, "dfe-fetcher").get("kafka") or {}).get("brokers")
    expect("and its list reaches fetcher.yaml", brokers == ["chosen.example.test:9092"],
           f"got {brokers!r}")


def test_no_values_file_offers_a_postgresql_dial() -> None:
    """No template reads .Values.postgresql, so a key by that name is a dial that moves nothing.

    The engine keeps its state in ClickHouse and its config in the gitops repo, and
    the ferretdb chart configures its DocumentDB backend from keys of its own.
    """
    files = [
        *sorted((REPO_ROOT / "helm").rglob("values.yaml")),
        *sorted((REPO_ROOT / "argocd" / "values").rglob("*.yaml")),
    ]
    offenders = []
    for path in files:
        docs = yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if any(isinstance(doc, dict) and "postgresql" in doc for doc in docs):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    expect("the values files were found", len(files) > 1, f"got {len(files)}")
    expect("no values file declares a postgresql dial", not offenders, f"got {offenders}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
