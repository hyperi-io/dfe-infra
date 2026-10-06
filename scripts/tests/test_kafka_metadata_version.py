#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_kafka_metadata_version.py
#  Purpose:      Prove kafka.metadataVersion reaches the Strimzi Kafka CR as a
#                quoted string when set, and renders nothing when empty.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The Kafka metadata version hold, as the chart renders it.

Unset, Strimzi raises spec.kafka.metadataVersion to the new Kafka version's
default the moment a version roll finishes, so `dfe-ops upgrade apply` holds it
through the deploy repo's infra/kafka.yaml. The hold only works if the chart
renders it, renders it as a string (Strimzi reads an unquoted 4.2 as a float),
and renders nothing at all when no hold is set.

    python3 -m pytest scripts/tests/test_kafka_metadata_version.py -q

Needs `helm` on PATH.
"""

import subprocess
from functools import cache
from pathlib import Path

import yaml

from _charts import chart_dir

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"


@cache
def kafka_cr(sets: tuple[str, ...] = ()) -> dict:
    """The Kafka CR under the scale profile's value cascade, the one that runs Strimzi."""
    cmd = ["helm", "template", "kafka", str(chart_dir("kafka")), "--show-only", "templates/kafka.yaml"]
    for name in ("common.yaml", "local.yaml", "profile-scale.yaml"):
        cmd += ["-f", str(VALUES / name)]
    cmd += ["--set", "appNamespace=dfe-local"]
    for s in sets:
        cmd += ["--set-string", s]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    assert out.returncode == 0, out.stderr
    (doc,) = [d for d in yaml.safe_load_all(out.stdout) if d]
    return doc


def test_no_hold_renders_no_metadata_version() -> None:
    assert "metadataVersion" not in kafka_cr()["spec"]["kafka"]


def test_a_hold_renders_as_a_string() -> None:
    spec = kafka_cr(("kafka.metadataVersion=4.2-IV1",))["spec"]["kafka"]
    assert spec["metadataVersion"] == "4.2-IV1"
    assert spec["version"] == kafka_cr()["spec"]["kafka"]["version"]


def test_a_short_hold_stays_a_string() -> None:
    assert kafka_cr(("kafka.metadataVersion=4.2",))["spec"]["kafka"]["metadataVersion"] == "4.2"
