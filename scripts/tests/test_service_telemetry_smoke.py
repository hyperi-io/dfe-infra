#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_service_telemetry_smoke.py
#  Purpose:      Prove CORE 1b of the integration smoke test fails when a deployed
#                DFE service stops pushing metrics, and only then.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""CORE 1b in bootstrap/smoke-test-integration.sh, against a fake kubectl.

CORE 1 passes on otel_logs, which the daemonset fills from container stdout, so an
app that stopped pushing its own telemetry did not fail it. CORE 1b lists the ready
scalo workloads that export to the collector gateway and asks the metrics tables for
each one's ServiceName.

    python3 -m pytest scripts/tests/test_service_telemetry_smoke.py
"""

import pytest
from _smoke import run_smoke

GATEWAY = "http://dfe-otel-collector-gateway.otel.svc.cluster.local:4317"
APPS = ("dfe-receiver", "dfe-loader", "dfe-archiver", "dfe-engine")
PASS = "[PASS] every deployed DFE service has a metric row newer than"
FAIL = "[FAIL] every deployed DFE service has a metric row newer than"


def workloads(*names: str, **over: object) -> list[dict]:
    return [{"name": n, "ready": 1, "endpoint": GATEWAY, **over} for n in names]


def smoke(fixture: dict, **env: str):
    return run_smoke({"otel_zero_answers": 0, **fixture}, DFE_OTEL_WAIT="2", **env)


def test_every_pushing_service_with_a_fresh_row_passes() -> None:
    out, counts = smoke({"workloads": workloads(*APPS), "metric_services": [*APPS, "k8s-node", "otelcol-contrib"]})

    assert PASS in out.stdout, out.stdout
    assert "waited 0s of 2s" in out.stdout
    assert counts["metrics"] == 1


def test_a_service_with_no_metric_row_fails_and_is_named() -> None:
    out, _ = smoke({"workloads": workloads(*APPS), "metric_services": ["dfe-receiver", "dfe-archiver", "dfe-engine"]})

    assert FAIL in out.stdout, out.stdout
    assert "no fresh metrics from: dfe-loader" in out.stdout
    assert out.returncode != 0


def test_a_service_that_pushes_late_still_passes() -> None:
    out, counts = smoke(
        {"workloads": workloads(*APPS), "metric_services": list(APPS), "metric_zero_answers": 2},
    )

    assert PASS in out.stdout, out.stdout
    assert counts["metrics"] > 2
    assert "waited 2s of 2s" in out.stdout or "waited 1s of 2s" in out.stdout


@pytest.mark.parametrize("series", ["dfe-transform-vrl-flowvrl", "dfe-transform-vrl"])
def test_a_per_config_deployment_is_matched_by_its_family(series: str) -> None:
    out, _ = smoke({"workloads": workloads("dfe-transform-vrl-flowvrl"), "metric_services": [series]})

    assert PASS in out.stdout, out.stdout


def test_a_lookalike_name_does_not_stand_in_for_the_service() -> None:
    out, _ = smoke({"workloads": workloads("dfe-loader"), "metric_services": ["dfe-loaderx", "dfe-loading"]})

    assert FAIL in out.stdout, out.stdout


@pytest.mark.parametrize(
    "workload",
    [
        {"endpoint": ""},
        {"endpoint": "http://elsewhere.example.invalid:4317"},
        {"ready": 0},
    ],
    ids=["prometheus-mode", "external-endpoint", "not-ready"],
)
def test_a_workload_that_is_not_expected_to_push_is_not_held_to_it(workload: dict) -> None:
    out, _ = smoke({"workloads": workloads("dfe-loader", **workload), "metric_services": []})

    assert "[SKIP] per-service telemetry" in out.stdout, out.stdout
    assert FAIL not in out.stdout


def test_a_workload_that_is_not_a_scalo_service_is_not_held_to_it() -> None:
    out, _ = smoke({"workloads": workloads("dfe-ui", "dfe-hyperdx"), "metric_services": []})

    assert "[SKIP] per-service telemetry" in out.stdout


def test_the_section_can_be_switched_off() -> None:
    out, counts = smoke({"workloads": workloads(*APPS), "metric_services": []}, DFE_SERVICE_TELEMETRY="skip")

    assert "[SKIP] per-service telemetry -- DFE_SERVICE_TELEMETRY=skip" in out.stdout
    assert "metrics" not in counts
