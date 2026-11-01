#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_kafka_disk_alert.py
#  Purpose:      Prove the kafka chart renders a broker disk-pressure
#                PrometheusRule at its warning and critical fill levels, and
#                only where the cluster serves the PrometheusRule API.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The broker disk alert, rendered.

Each topic gets the full per-partition allowance, so a disk sized too small fills
rather than dropping unread data. The alert is how an operator hears before it does.

    python3 -m pytest scripts/tests/test_kafka_disk_alert.py -q

Needs `helm` on PATH.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "kafka"
VALUES = REPO_ROOT / "argocd" / "values"
SCALE_CASCADE = [VALUES / "common.yaml", VALUES / "profile-scale.yaml"]
RULE_API = "monitoring.coreos.com/v1/PrometheusRule"


def _render(*overlays: str, api: bool = True) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "kafka", str(CHART), "--namespace", "strimzi", "--set", "appNamespace=dfe"]
    if api:
        cmd += ["--api-versions", RULE_API]
    files = []
    for body in overlays:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(body)
            files.append(Path(fh.name))
    for v in [*SCALE_CASCADE, *files]:
        cmd += ["-f", str(v)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    for f in files:
        f.unlink()
    return out


def _rules(out: subprocess.CompletedProcess) -> dict[str, dict]:
    assert out.returncode == 0, out.stderr
    docs = [d for d in yaml.safe_load_all(out.stdout) if d and d.get("kind") == "PrometheusRule"]
    return {r["alert"]: r for d in docs for g in d["spec"]["groups"] for r in g["rules"]}


def test_both_levels_render_on_every_broker_volume() -> None:
    rules = _rules(_render())
    assert sorted(rules) == ["KafkaBrokerDiskCritical", "KafkaBrokerDiskWarning"]
    warning, critical = rules["KafkaBrokerDiskWarning"], rules["KafkaBrokerDiskCritical"]
    assert warning["labels"]["severity"] == "warning"
    assert critical["labels"]["severity"] == "critical"
    assert warning["expr"].rstrip().endswith("> 70")
    assert critical["expr"].rstrip().endswith("> 85")
    # Strimzi names a JBOD volume's claim data-<volume>-<cluster>-<pool>-<pod>.
    for rule in rules.values():
        assert 'persistentvolumeclaim=~"data-0-dfe-kafka-dfe-kafka-pool-[0-9]+"' in rule["expr"]
        assert 'namespace="strimzi"' in rule["expr"]
        assert "kubelet_volume_stats_used_bytes" in rule["expr"]
        assert "kubelet_volume_stats_capacity_bytes" in rule["expr"]


def test_the_levels_and_the_wait_are_chart_values() -> None:
    rules = _rules(_render("kafka:\n  diskAlert:\n    warningPercent: 60\n    criticalPercent: 80\n    for: 30m\n"))
    assert rules["KafkaBrokerDiskWarning"]["expr"].rstrip().endswith("> 60")
    assert rules["KafkaBrokerDiskCritical"]["expr"].rstrip().endswith("> 80")
    assert {r["for"] for r in rules.values()} == {"30m"}


@pytest.mark.parametrize(
    ("overlay", "api"),
    [
        pytest.param("", False, id="no-prometheus-operator"),
        pytest.param("kafka:\n  diskAlert:\n    enabled: false\n", True, id="switched-off"),
        pytest.param("kafka:\n  mode: external\n  external:\n    bootstrap: b:9092\n", True, id="not-our-broker"),
    ],
)
def test_no_rule_renders_where_none_can_fire(overlay: str, api: bool) -> None:
    out = _render(overlay, api=api) if overlay else _render(api=api)
    assert _rules(out) == {}


def test_expected_fail_a_warning_at_or_above_critical_is_refused() -> None:
    out = _render("kafka:\n  diskAlert:\n    warningPercent: 90\n    criticalPercent: 85\n")
    assert out.returncode != 0
    assert "kafka.diskAlert.warningPercent" in out.stderr
