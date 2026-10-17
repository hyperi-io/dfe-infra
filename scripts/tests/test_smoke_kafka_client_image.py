#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_smoke_kafka_client_image.py
#  Purpose:      Prove the smoke suite's managed-broker client runs the kafka.cliImage
#                pin the kafka chart carries, read from the chart rather than copied.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The image CORE 3's client pod runs against a managed broker.

    python3 -m pytest scripts/tests/test_smoke_kafka_client_image.py -q

bootstrap/smoke-test-integration.sh reads kafka.cliImage out of
helm/charts/kafka/values.yaml with a line reader, so a reordered or reformatted
chart could hand the client an image nobody pinned. The reader is run here on its
own, and its answer held to the chart as a YAML parser reads it. The pin itself is
held to the brokers' Kafka version, so the CLI moves when the brokers do.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SMOKE = REPO_ROOT / "bootstrap" / "smoke-test-integration.sh"
KAFKA_VALUES = REPO_ROOT / "helm" / "charts" / "kafka" / "values.yaml"
DIGEST = "sha256:" + "a" * 64


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


def test_the_client_runs_the_cli_image_the_kafka_chart_carries() -> None:
    kafka = yaml.safe_load(KAFKA_VALUES.read_text(encoding="utf-8"))["kafka"]
    done = _image(KAFKA_VALUES)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == kafka["cliImage"]


def test_the_cli_image_is_digest_pinned_at_the_brokers_kafka_version() -> None:
    kafka = yaml.safe_load(KAFKA_VALUES.read_text(encoding="utf-8"))["kafka"]
    found = re.fullmatch(
        r"quay\.io/strimzi/kafka:\d+\.\d+\.\d+-kafka-(?P<kafka>[^@]+)@sha256:[0-9a-f]{64}",
        kafka["cliImage"],
    )
    assert found, f"kafka.cliImage {kafka['cliImage']!r} is not a digest-pinned Strimzi Kafka tag"
    assert found["kafka"] == kafka["version"], (
        f"kafka.cliImage carries Kafka {found['kafka']}, the brokers run {kafka['version']}: "
        "move cliImage to the Strimzi tag for the new version, and its digest with it"
    )


def test_a_key_of_the_same_name_in_another_block_is_not_read(tmp_path: Path) -> None:
    values = tmp_path / "values.yaml"
    values.write_text(
        "# kafka:\n"
        "redpanda:\n"
        f'  cliImage: "redpanda/cli:1@{DIGEST}"\n'
        "kafka:\n"
        "  mode: disabled\n"
        "  # cliImage: commented out\n"
        "  nested:\n"
        f'    cliImage: "nested/cli:1@{DIGEST}"\n'
        f'  cliImage: "quay.io/strimzi/kafka:1.3.0-kafka-4.3.1@{DIGEST}"   # trailing note\n'
        "user:\n"
        f'  cliImage: "user/cli:1@{DIGEST}"\n',
        encoding="utf-8",
    )
    done = _image(values)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == f"quay.io/strimzi/kafka:1.3.0-kafka-4.3.1@{DIGEST}"


@pytest.mark.parametrize(
    "body",
    [
        f'kafka:\n  mode: single\nuser:\n  cliImage: "user/cli:1@{DIGEST}"\n',
        'kafka:\n  cliImage: "quay.io/strimzi/kafka:1.3.0-kafka-4.3.1"\n',
        f'redpanda:\n  cliImage: "redpanda/cli:1@{DIGEST}"\n',
    ],
    ids=["pin-only-in-another-block", "tag-without-digest", "no-kafka-block"],
)
def test_expected_fail_a_chart_without_a_digest_pinned_cli_image_names_none(
    tmp_path: Path, body: str
) -> None:
    """A tag with no digest is an image nobody reviewed, so the reader names none."""
    values = tmp_path / "values.yaml"
    values.write_text(body, encoding="utf-8")
    done = _image(values)
    assert done.returncode != 0
    assert done.stdout == ""
