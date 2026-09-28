#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_app_env_contract.py
#  Purpose:      Prove each dfe-* app chart renders the env var NAMES its app
#                actually reads, and none that nothing reads.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Chart env names against the settings the apps actually read.

An env var no app reads renders green, lints green and passes a server-side
dry-run: the API server has no opinion about a name no process reads. The app
keeps its built-in default and reports nothing, so the defect surfaces as an app
not doing its job, never as a config error.

CONTRACT is the apps' side of that, one row per name: when the chart renders it,
and the file:line in the app's own repo that reads it. A `scalo-rs` path is in
the scalo release that app's Cargo.lock pins -- dfe-transform-elastic reads bare
KAFKA_* through scalo's KafkaConfig::from_env, and every app reads OTEL_* through
scalo. CI has no app checkout, so the rows are kept by hand: a rename upstream
fails HERE, which makes it a decision rather than a silent drift.

Rows cover only what the chart is responsible for. dfe-fetcher takes its broker
list from the config file the chart writes, not from env, so that is asserted on
the file. The archiver's S3 names belong to test_config_authority.py and an
unnamed secretKeyRef to test_inert_chart_env.py.

Every render layers argocd/values/common.yaml, the file every app Application
reads first, so the names asserted are the ones a deployment actually gets.

    python3 scripts/tests/test_app_env_contract.py

Runs standalone or under pytest. Needs `helm` on PATH.
"""

import functools
import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMMON_VALUES = REPO_ROOT / "argocd" / "values" / "common.yaml"

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

# The dials behind every DIALLED row, set together on the bus.
DIALS = (
    "versionCheck.enabled=false",
    "versionCheck.sendInstanceId=false",
    "versionCheck.apiUrl=https://releases.example.com/check",
    "versionCheck.instanceId=example-install",
    "transformFiles[0].name=example.vrl",
    "transformFiles[0].content=.",
    "kafka.sourceTopic=example_land",
    "kafka.destTopic=example_load",
)

SCENARIOS = {
    BUS: (),
    DIRECT: ("kafka.mode=disabled",),
    DIALLED: DIALS,
}

# The scalo cascade keys under version_check (scalo-rs 2.13 src/version_check/mod.rs:186).
VERSION_CHECK_KEYS = ("ENABLED", "SEND_INSTANCE_ID", "API_URL", "INSTANCE_ID")


def _version_check(app: str, prefix: str, where: str) -> tuple[tuple[str, str, str, str], ...]:
    """The four VERSION_CHECK__ rows for an app whose cascade reads `prefix`."""
    names = [f"{prefix}VERSION_CHECK__{key}" for key in VERSION_CHECK_KEYS]
    return tuple((app, DIALLED, name, where) for name in names)


# Only these four set up scalo's cascade under the charts' --config, so no
# VERSION_CHECK__ name reaches dfe-fetcher, dfe-receiver or dfe-transform-vector.
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
    ("dfe-fetcher", BUS, "DFE_FETCHER_KAFKA_SASL_USER", "crates/fetcher/src/config/mod.rs:935"),
    ("dfe-fetcher", BUS, "DFE_FETCHER_KAFKA_SASL_PASSWORD", "crates/fetcher/src/config/mod.rs:939"),
    ("dfe-fetcher", BUS, "DFE_FETCHER_KAFKA_SASL_MECHANISM",
     "crates/fetcher/src/config/mod.rs:926"),
    ("dfe-fetcher", BUS, "DFE_FETCHER_DLQ_TOPIC", "crates/fetcher/src/config/mod.rs:981"),
    ("dfe-fetcher", BUS, "DFE_FETCHER_DLQ_MODE", "crates/fetcher/src/config/mod.rs:985"),
    ("dfe-fetcher", DIRECT, "DFE_FETCHER_DLQ_ENABLED", "crates/fetcher/src/config/mod.rs:975"),
    # DFE_LOADER_*: flat names applied at src/config/loader.rs:423; TRANSPORT and the
    # `__` names reach their fields through the figment layer at :365.
    ("dfe-loader", BUS, "DFE_LOADER_KAFKA_BROKERS", "src/config/loader.rs:223"),
    ("dfe-loader", BUS, "DFE_LOADER_KAFKA_SASL_USERNAME", "src/config/loader.rs:239"),
    ("dfe-loader", BUS, "DFE_LOADER_KAFKA_SASL_PASSWORD", "src/config/loader.rs:243"),
    ("dfe-loader", BUS, "DFE_LOADER_DLQ_TOPIC", "src/config/loader.rs:273"),
    ("dfe-loader", DIRECT, "DFE_LOADER_TRANSPORT", "src/config/loader.rs:61"),
    ("dfe-loader", DIRECT, "DFE_LOADER_GRPC__LISTEN", "src/config/kafka.rs:96"),
    ("dfe-loader", ALWAYS, "DFE_LOADER_DLQ_MODE", "src/config/loader.rs:276"),
    ("dfe-loader", ALWAYS, "DFE_LOADER_CLICKHOUSE_HOSTS", "src/config/loader.rs:256"),
    ("dfe-loader", ALWAYS, "DFE_LOADER_CLICKHOUSE_DATABASE", "src/config/loader.rs:259"),
    ("dfe-loader", ALWAYS, "DFE_LOADER_CLICKHOUSE_USERNAME", "src/config/loader.rs:262"),
    ("dfe-loader", ALWAYS, "DFE_LOADER_CLICKHOUSE_PASSWORD", "src/config/loader.rs:265"),
    ("dfe-loader", ALWAYS, "DFE_LOADER_CLICKHOUSE__PROTOCOL", "src/config/loader.rs:148"),
    # DFE_RECEIVER_*, applied after the file at src/config/mod.rs:210.
    ("dfe-receiver", BUS, "DFE_RECEIVER_KAFKA_BROKERS", "src/config/mod.rs:613"),
    ("dfe-receiver", BUS, "DFE_RECEIVER_KAFKA_SASL_USER", "src/config/mod.rs:626"),
    ("dfe-receiver", BUS, "DFE_RECEIVER_KAFKA_SASL_PASSWORD", "src/config/mod.rs:630"),
    ("dfe-receiver", BUS, "DFE_RECEIVER_KAFKA_SASL_MECHANISM", "src/config/mod.rs:619"),
    ("dfe-receiver", BUS, "DFE_RECEIVER_DLQ_TOPIC", "src/config/mod.rs:647"),
    ("dfe-receiver", BUS, "DFE_RECEIVER_DLQ_MODE", "src/config/mod.rs:650"),
    ("dfe-receiver", DIRECT, "DFE_RECEIVER_DLQ_ENABLED", "src/config/mod.rs:644"),
    ("dfe-receiver", ALWAYS, "DFE_RECEIVER_BIND_ADDRESS", "src/config/mod.rs:584"),
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
    ("dfe-transform-vrl", DIALLED, "DFE_TRANSFORM_SOURCE_TOPICS", "src/config/loader.rs:566"),
    ("dfe-transform-vrl", DIALLED, "DFE_TRANSFORM_SINK_TOPIC", "src/config/loader.rs:588"),
    ("dfe-transform-vrl", DIALLED, "DFE_TRANSFORM_TRANSFORMS_DIR", "src/config/loader.rs:604"),
)

CONTRACT = (*APP_ROWS, *VERSION_CHECK, *SCALO_OTEL)

# Names a chart rendered once and no app reads. A regression here is invisible in a
# cluster, so it is asserted rather than left to review. dfe-transform-elastic is
# absent from the first set because from_env reads KAFKA_BOOTSTRAP_SERVERS there.
RETIRED = (
    ("dfe-archiver", "KAFKA_BOOTSTRAP_SERVERS"),
    ("dfe-fetcher", "KAFKA_BOOTSTRAP_SERVERS"),
    ("dfe-loader", "KAFKA_BOOTSTRAP_SERVERS"),
    ("dfe-receiver", "KAFKA_BOOTSTRAP_SERVERS"),
    ("dfe-transform-vector", "KAFKA_BOOTSTRAP_SERVERS"),
    ("dfe-transform-vrl", "KAFKA_BOOTSTRAP_SERVERS"),
    ("dfe-fetcher", "DFE_FETCHER_API_KEY"),
    ("dfe-fetcher", "DFE_FETCHER_API_SECRET"),
)


@functools.cache
def render(app: str, scenario: str) -> tuple[dict, ...]:
    """The app's chart under common.yaml and one scenario's sets."""
    cmd = ["helm", "template", app, str(chart_dir(app)), "-f", str(COMMON_VALUES)]
    for s in SCENARIOS[scenario]:
        cmd += ["--set", s]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {app} [{scenario}]:\n{out.stderr}")
    return tuple(d for d in yaml.safe_load_all(out.stdout) if d)


def env_entries(app: str, scenario: str) -> list[dict]:
    """Every env entry on the app's containers; init containers reach no app."""
    deployments = [d for d in render(app, scenario) if d.get("kind") == "Deployment"]
    if not deployments:
        raise SystemExit(f"{app} [{scenario}] rendered no Deployment")
    pod = deployments[0]["spec"]["template"]["spec"]
    return [e for c in pod.get("containers", []) for e in c.get("env") or []]


def env_names(app: str, scenario: str) -> set[str]:
    return {e["name"] for e in env_entries(app, scenario)}


def names_read(*when: str) -> dict[str, dict[str, str]]:
    """CONTRACT rows for these moments, as app -> {name: where it is read}."""
    wanted: dict[str, dict[str, str]] = {}
    for app, row_when, name, where in CONTRACT:
        if row_when in when:
            wanted.setdefault(app, {})[name] = where
    return wanted


def expect_rendered(scenario: str, *when: str) -> None:
    for app, wanted in sorted(names_read(*when).items()):
        rendered = env_names(app, scenario)
        missing = [f"{name} ({wanted[name]})" for name in sorted(wanted) if name not in rendered]
        expect(
            f"{app} renders every name its app reads [{scenario}]",
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
    for scenario in SCENARIOS:
        for app in APPS:
            retired = {name for owner, name in RETIRED if owner == app}
            back = sorted(retired & env_names(app, scenario))
            expect(f"{app} renders no retired name [{scenario}]", not back, f"got {back}")


def test_every_kafka_credential_comes_from_the_declared_secret() -> None:
    """A template naming another Secret mounts nothing the deployment provisioned.

    The failure names the env entries only: what they reference stays out of the
    output, so no Secret coordinate reaches a CI log.
    """
    for app in APPS:
        values = yaml.safe_load((chart_dir(app) / "values.yaml").read_text(encoding="utf-8"))
        declared = values["kafka"]["saslSecretName"]
        mounted = []
        strays = []
        for entry in env_entries(app, BUS):
            ref = (entry.get("valueFrom") or {}).get("secretKeyRef")
            if "SASL" not in entry["name"] or not ref:
                continue
            mounted.append(entry["name"])
            if ref.get("name") != declared:
                strays.append(entry["name"])
        expect(f"{app} mounts its Kafka credential from a Secret", bool(mounted), "none rendered")
        expect(
            f"{app} reads it from kafka.saslSecretName",
            not strays,
            f"{sorted(strays)} name a different Secret",
        )


def test_the_scalo_apps_push_otlp_to_the_grpc_port() -> None:
    """scalo exports over gRPC unless told otherwise, and no app chart tells it.

    scalo-rs 2.13 src/metrics/otel_types.rs:95 and src/otel_tracing/mod.rs:120 default
    both exporters to gRPC, which the collector serves on 4317 and not on 4318.
    """
    for app in APPS:
        env = {e["name"]: e.get("value", "") for e in env_entries(app, BUS)}
        endpoint = env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        expect(
            f"{app} pushes OTLP to the gRPC port",
            endpoint.endswith(":4317"),
            f"got {endpoint!r}",
        )
        expect(
            f"{app} leaves scalo on its gRPC exporter",
            "OTEL_EXPORTER_OTLP_PROTOCOL" not in env,
            f"got {env.get('OTEL_EXPORTER_OTLP_PROTOCOL')!r}",
        )


def test_the_fetcher_reads_its_brokers_from_the_file_the_chart_writes() -> None:
    """build_scalo_kafka_config takes the brokers from kafka.brokers in the file
    (dfe-fetcher crates/fetcher/src/output.rs:478), so no broker env is rendered."""
    configmaps = [
        d for d in render("dfe-fetcher", BUS)
        if d.get("kind") == "ConfigMap" and "fetcher.yaml" in (d.get("data") or {})
    ]
    expect("the fetcher chart writes fetcher.yaml", len(configmaps) == 1, f"got {len(configmaps)}")
    if not configmaps:
        return
    written = yaml.safe_load(configmaps[0]["data"]["fetcher.yaml"]) or {}
    brokers = (written.get("kafka") or {}).get("brokers")
    common = yaml.safe_load(COMMON_VALUES.read_text(encoding="utf-8"))
    expect(
        "the file carries the deployment's broker list",
        brokers == [common["kafka"]["bootstrapServers"]],
        f"got {brokers!r}",
    )


def test_no_values_file_offers_a_postgresql_dial() -> None:
    """No template reads .Values.postgresql, so a key by that name is a dial that moves nothing.

    The engine keeps its state in ClickHouse and its config in the gitops repo, and
    cnpg-cluster writes its Cluster from keys of its own.
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
