#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_smoke_kafka_client_image.py
#  Purpose:      Prove the smoke suite's managed-broker client runs the apache/kafka
#                pin the kafka chart carries, read from the chart rather than copied.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The image CORE 3's client pod runs against a managed broker.

    python3 -m pytest scripts/tests/test_smoke_kafka_client_image.py -q

bootstrap/smoke-test-integration.sh reads kafka.version and kafka.imageDigest out
of helm/charts/kafka/values.yaml with a line reader, so a reordered or reformatted
chart could hand the client an image nobody pinned. The reader is run here on its
own, and its answer held to the chart as a YAML parser reads it.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SMOKE = REPO_ROOT / "bootstrap" / "smoke-test-integration.sh"
KAFKA_VALUES = REPO_ROOT / "helm" / "charts" / "kafka" / "values.yaml"


def _reader() -> str:
    """kafka_chart_image() as the smoke suite defines it, run on its own rather than copied."""
    lines = SMOKE.read_text(encoding="utf-8").splitlines()
    start = lines.index("kafka_chart_image() {")
    end = lines.index("}", start)
    return "\n".join(lines[start : end + 1])


def _image(values: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-uc", _reader() + '\nkafka_chart_image "$1"', "reader", str(values)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def test_the_client_runs_the_apache_kafka_pin_the_kafka_chart_carries() -> None:
    kafka = yaml.safe_load(KAFKA_VALUES.read_text(encoding="utf-8"))["kafka"]
    done = _image(KAFKA_VALUES)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == f"apache/kafka:{kafka['version']}@{kafka['imageDigest']}"
    assert kafka["imageDigest"].startswith("sha256:")


def test_a_key_of_the_same_name_in_another_block_is_not_read(tmp_path: Path) -> None:
    values = tmp_path / "values.yaml"
    values.write_text(
        "# kafka:\n"
        "redpanda:\n"
        '  version: "9.9.9"\n'
        '  imageDigest: "sha256:redpanda"\n'
        "kafka:\n"
        "  mode: disabled\n"
        "  # version: commented out\n"
        "  nested:\n"
        '    version: "0.0.1"\n'
        '  version: "4.3.1"\n'
        '  imageDigest: "sha256:kafka"\n'
        "user:\n"
        '  version: "1.0.0"\n',
        encoding="utf-8",
    )
    done = _image(values)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "apache/kafka:4.3.1@sha256:kafka"


@pytest.mark.parametrize(
    "body",
    [
        'kafka:\n  version: "4.3.1"\nuser:\n  imageDigest: "sha256:user"\n',
        'kafka:\n  imageDigest: "sha256:kafka"\n',
        'redpanda:\n  version: "1"\n  imageDigest: "sha256:x"\n',
    ],
    ids=["digest-only-in-another-block", "no-version", "no-kafka-block"],
)
def test_expected_fail_a_chart_without_both_pins_names_no_image(tmp_path: Path, body: str) -> None:
    """An image half-pinned is an image nobody reviewed, so the reader names none."""
    values = tmp_path / "values.yaml"
    values.write_text(body, encoding="utf-8")
    done = _image(values)
    assert done.returncode != 0
    assert done.stdout == ""
