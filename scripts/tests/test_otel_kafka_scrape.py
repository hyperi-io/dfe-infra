#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_otel_kafka_scrape.py
#  Purpose:      Prove the collector's kafka_metrics receiver dials a managed
#                broker with TLS, its provider's mechanism and one entry per broker.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The gateway's kafka_metrics receiver against each kind of broker.

    python3 -m pytest scripts/tests/test_otel_kafka_scrape.py

Needs `helm` on PATH.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "helm" / "charts" / "otel-collector"
BROKERS = "b-1.x.example.invalid:9096,b-2.x.example.invalid:9096"


def kafka_metrics(*sets: str) -> dict:
    cmd = ["helm", "template", "oc", str(CHART), "--show-only", "templates/configmap.yaml"]
    cmd += ["--set", "clusterTelemetry.kafka.enabled=true"]
    for item in sets:
        cmd += ["--set", item]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    assert out.returncode == 0, f"helm template failed for {sets}:\n{out.stderr}"
    (configmap,) = [doc for doc in yaml.safe_load_all(out.stdout) if doc]
    return yaml.safe_load(configmap["data"]["gateway-config.yaml"])["receivers"]["kafka_metrics"]


def managed(provider: str, *extra: str) -> dict:
    return kafka_metrics(
        "kafka.mode=external", f"kafka.external.provider={provider}", f"kafka.bootstrapServers={BROKERS.replace(',', '\\,')}", *extra
    )


@pytest.mark.parametrize(
    ("provider", "mechanism"),
    [("msk", "SCRAM-SHA-512"), ("redpanda-cloud", "SCRAM-SHA-512"), ("confluent-cloud", "PLAIN")],
)
def test_a_managed_broker_gets_tls_its_mechanism_and_each_broker(provider: str, mechanism: str) -> None:
    receiver = managed(provider)
    assert receiver["brokers"] == BROKERS.split(",")
    assert receiver["auth"]["tls"] == {}
    assert receiver["auth"]["sasl"]["mechanism"] == mechanism


def test_the_shipped_broker_stays_plaintext_scram() -> None:
    receiver = kafka_metrics()
    assert receiver["brokers"] == ["dfe-kafka.strimzi.svc.cluster.local:9092"]
    assert "tls" not in receiver["auth"]
    assert receiver["auth"]["sasl"]["mechanism"] == "SCRAM-SHA-512"


def test_an_explicit_mechanism_beats_the_provider_table() -> None:
    assert managed("msk", "clusterTelemetry.kafka.sasl.mechanism=SCRAM-SHA-256")["auth"]["sasl"]["mechanism"] == "SCRAM-SHA-256"


def test_a_self_run_tls_listener_asks_for_tls() -> None:
    assert kafka_metrics("clusterTelemetry.kafka.tls.enabled=true")["auth"]["tls"] == {}


def test_tls_without_sasl_still_renders_the_auth_block() -> None:
    receiver = kafka_metrics("clusterTelemetry.kafka.tls.enabled=true", "clusterTelemetry.kafka.sasl.enabled=false")
    assert receiver["auth"] == {"tls": {}}


def test_an_unknown_provider_fails_the_render() -> None:
    cmd = ["helm", "template", "oc", str(CHART), "--set", "clusterTelemetry.kafka.enabled=true"]
    cmd += ["--set", "kafka.mode=external", "--set", "kafka.external.provider=nope"]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    assert out.returncode != 0
    assert "unknown provider" in out.stderr
