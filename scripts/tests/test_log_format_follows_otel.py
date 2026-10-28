#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_log_format_follows_otel.py
#  Purpose:      Hold the deploy layer to the one knob the scalo chassis reads for
#                its log format: the OTLP endpoint. Nothing here sets a format.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The scalo chassis logs JSON when an OTLP endpoint is configured and lines when not.

The deploy layer owns the endpoint (argocd/values/apps/_common.yaml, through the
dfe-common seam) and nothing else. A chart or values file that also named a format
would override the chassis rule per component and bring back the sprawl the rule
removed. Two checks hold that:

- no chart, template, values file or appset names a log format, and none of the
  committed renders carries an env var or ConfigMap key that does
- every scalo component's workload carries OTEL_EXPORTER_OTLP_ENDPOINT, set and
  empty alike, in every cell of the render matrix

dfe-ui, hyperdx and culvert are not on the chassis, so they are not held to the second.

    python3 -m pytest scripts/tests/test_log_format_follows_otel.py
"""

import re
from pathlib import Path

import pytest

from _gate import CELLS, effective_env, golden, object_id

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
# logfmt alone is a log syntax the collector parses, not a setting.
FORMAT_NAME = re.compile(r"log[_.\-]?format|log[_.\-]fmt", re.IGNORECASE)
ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
SCALO = (
    "dfe-receiver", "dfe-loader", "dfe-archiver", "dfe-fetcher",
    "dfe-transform-vrl", "dfe-transform-vector", "dfe-transform-elastic", "dfe-engine",
)
SOURCES = ("helm/charts", "helm/library", "helm/edge", "argocd", "bootstrap")


def deploy_files() -> list[Path]:
    return [
        path
        for root in SOURCES
        for path in (REPO_ROOT / root).rglob("*")
        if path.is_file() and path.suffix in {".yaml", ".yml", ".tpl", ".sh"}
    ]


def test_no_deploy_file_names_a_log_format() -> None:
    hits = [
        f"{path.relative_to(REPO_ROOT)}:{n}"
        for path in deploy_files()
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if FORMAT_NAME.search(line)
    ]
    assert hits == []


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), CELLS)
def test_no_render_carries_a_log_format(service: str, profile: str, cloud: str, scenario: str) -> None:
    names: list[str] = []
    for doc in golden(service, profile, cloud, scenario):
        if doc.get("kind") == "ConfigMap":
            names += [k for k in (doc.get("data") or {}) if FORMAT_NAME.search(k)]
        pod = (doc.get("spec", {}).get("template", {}) or {}).get("spec", {})
        for container in (pod.get("containers") or []) + (pod.get("initContainers") or []):
            names += [e["name"] for e in container.get("env") or [] if FORMAT_NAME.search(e["name"])]
    assert names == []


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), [c for c in CELLS if c[0] in SCALO])
def test_every_scalo_workload_carries_the_endpoint_variable(service: str, profile: str, cloud: str, scenario: str) -> None:
    docs = golden(service, profile, cloud, scenario)
    workloads = [object_id(d) for d in docs if d.get("kind") in ("Deployment", "StatefulSet") and d["metadata"]["name"] == service]
    assert workloads, f"{service} {profile}/{cloud}/{scenario} renders no workload of its own name"
    for workload in workloads:
        assert ENDPOINT in effective_env(list(docs), workload), workload
