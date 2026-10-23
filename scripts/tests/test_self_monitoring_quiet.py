#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_self_monitoring_quiet.py
#  Purpose:      Prove the ClickHouse operator and the DocumentDB store ship
#                without the debug and per-run chatter that filled otel_logs
#                on an idle stack.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for the operator's log level and the DocumentDB store's cron settings.

On an idle stack the ClickHouse operator logged at DEBUG (dfe-infra#462), and pg_cron
logged and recorded a DocumentDB job every 2 seconds (dfe-infra#463). Each is a value
this repo passes, and this file reads those values back out of the appsets and the
documentdb render.

    python3 scripts/tests/test_self_monitoring_quiet.py

Needs `helm` on PATH.
"""

import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LAYER_SCALE = REPO_ROOT / "argocd" / "appsets" / "layer-scale.yaml"
OPERATOR_CHART = "clickhouse-operator-helm"
OPERATOR_ARGS = ["--leader-elect", "--zap-log-level=info"]


def operator_elements() -> list[tuple[str, dict, str]]:
    """(appset name, operator element, the template's helm values string) per appset."""
    found = []
    for doc in yaml.safe_load_all(LAYER_SCALE.read_text(encoding="utf-8")):
        if not doc:
            continue
        spec = doc["spec"]
        values = spec["template"]["spec"]["source"].get("helm", {}).get("values", "")
        for generator in spec["generators"]:
            for inner in generator.get("matrix", {}).get("generators", []):
                for element in inner.get("list", {}).get("elements", []):
                    if element.get("chart") == OPERATOR_CHART:
                        found.append((doc["metadata"]["name"], element, values))
    return found


def documentdb_cr() -> dict:
    cmd = ["helm", "template", "documentdb", str(chart_dir("documentdb")), "--set", "appNamespace=x"]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for documentdb:\n{out.stderr}")
    (cr,) = [d for d in yaml.safe_load_all(out.stdout) if d and d.get("kind") == "DocumentDB"]
    return cr


def test_the_operator_logs_at_info_in_every_appset_that_installs_it() -> None:
    elements = operator_elements()
    expect("both operator appsets are found", len(elements) == 2, f"got {len(elements)}")
    for appset, element, values in elements:
        chart_values = yaml.safe_load(element.get("chartValuesYaml") or "") or {}
        args = (chart_values.get("manager") or {}).get("args")
        expect(
            f"{appset} passes {OPERATOR_ARGS} to the operator",
            args == OPERATOR_ARGS,
            f"got {args!r}; manager.args replaces the chart's list, so --leader-elect must stay",
        )
        expect(
            f"{appset} hands the element's chartValuesYaml to helm",
            ".chartValuesYaml" in values,
            "the template's helm values never read it",
        )


def test_documentdb_neither_logs_nor_records_each_cron_run() -> None:
    parameters = (documentdb_cr()["spec"].get("postgres") or {}).get("parameters") or {}
    for key in ("cron.log_run", "cron.log_statement"):
        expect(f"documentdb sets {key} off", parameters.get(key) == "off", f"got {parameters}")


def main() -> int:
    with standalone():
        test_the_operator_logs_at_info_in_every_appset_that_installs_it()
        test_documentdb_neither_logs_nor_records_each_cron_run()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
