#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_otel_collector_dfe_services.py
#  Purpose:      Prove the dfe-services scrape drops the pods of charts that push
#                their metrics over OTLP with replica identity, and only those.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The collector's dfe-services scrape and dfeServices.pushedCharts.

A service that pushes its metrics over OTLP with the pod as service.instance.id
and is scraped as well writes two series per pod, and every per-series sum over
otel_metrics_* counts it twice. pushedCharts names those charts; the scrape drops
their pods by the helm.sh/chart label and keeps every other DFE pod.

    python3 -m pytest scripts/tests/test_otel_collector_dfe_services.py

Needs `helm` on PATH.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "helm" / "charts" / "otel-collector"
CHART_LABEL = "__meta_kubernetes_pod_label_helm_sh_chart"


def dfe_services_relabels(*sets: str) -> list[dict]:
    """The dfe-services job's relabel_configs from the gateway config, rendered with `sets`."""
    cmd = ["helm", "template", "oc", str(CHART), "--show-only", "templates/configmap.yaml"]
    for item in sets:
        cmd += ["--set", item]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    assert out.returncode == 0, f"helm template failed for {sets}:\n{out.stderr}"
    (configmap,) = [doc for doc in yaml.safe_load_all(out.stdout) if doc]
    config = yaml.safe_load(configmap["data"]["gateway-config.yaml"])
    jobs = config["receivers"]["prometheus"]["config"]["scrape_configs"]
    (job,) = [j for j in jobs if j["job_name"] == "dfe-services"]
    return job["relabel_configs"]


def chart_drops(relabels: list[dict]) -> list[dict]:
    return [r for r in relabels if r.get("source_labels") == [CHART_LABEL]]


def test_the_default_drops_no_pod() -> None:
    assert chart_drops(dfe_services_relabels()) == []


def test_pushed_charts_drop_their_pods_after_the_dfe_keep() -> None:
    relabels = dfe_services_relabels("clusterTelemetry.dfeServices.pushedCharts=dfe-engine-[0-9].*")
    (drop,) = chart_drops(relabels)
    assert drop == {"source_labels": [CHART_LABEL], "action": "drop", "regex": "dfe-engine-[0-9].*"}
    part_of = next(i for i, r in enumerate(relabels) if r.get("regex") == "dfe" and r["action"] == "keep")
    assert relabels.index(drop) == part_of + 1


@pytest.mark.parametrize(("label", "dropped"), [
    ("dfe-engine-1.22.27", True),
    ("dfe-ui-1.7.13", False),
    ("dfe-hyperdx-0.3.3", False),
    ("culvert-2.1.18", False),
])
def test_the_regex_is_anchored_on_the_whole_label(label: str, dropped: bool) -> None:
    """Prometheus anchors relabel regexes at both ends, so `re.fullmatch` is the same test."""
    (drop,) = chart_drops(dfe_services_relabels("clusterTelemetry.dfeServices.pushedCharts=dfe-engine-[0-9].*"))
    assert bool(re.fullmatch(drop["regex"], label)) is dropped
