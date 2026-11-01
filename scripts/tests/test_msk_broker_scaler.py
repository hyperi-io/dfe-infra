#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_msk_broker_scaler.py
#  Purpose:      Prove the MSK Express broker scaler reads the brokers the
#                cluster has now, scales again while they stay over their
#                throughput, and stops at the ceiling.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The Lambda behind terraform/modules/managed-kafka/msk/autoscaler.tf, run against fakes.

A one-shot alarm over the brokers the cluster started with never saw an added
broker and never fired twice while it stayed in ALARM, so the scaler now runs on
a schedule and reads its own metrics. These fakes stand in for the kafka and
cloudwatch clients, so nothing here reaches AWS.

    python3 -m pytest scripts/tests/test_msk_broker_scaler.py -q
"""

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HANDLER = REPO_ROOT / "terraform" / "modules" / "managed-kafka" / "msk" / "broker_scaler.py"

_spec = importlib.util.spec_from_file_location("msk_broker_scaler", HANDLER)
assert _spec is not None
assert _spec.loader is not None
scaler = importlib.util.module_from_spec(_spec)
sys.modules["msk_broker_scaler"] = scaler
_spec.loader.exec_module(scaler)

ARN = "arn:aws:kafka:us-west-2:000000000000:cluster/dfe-example/00000000-0000-0000-0000-000000000000-3"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
PER_BROKER = 65_000_000.0  # bytes per second a broker may carry before the scaler adds more
SETTINGS = scaler.Settings(
    cluster_arn=ARN,
    cluster_name="dfe-example",
    max_brokers=9,
    step=3,
    threshold_bytes_per_sec=PER_BROKER,
    period_seconds=300,
    evaluation_periods=3,
)


class Kafka:
    """The kafka client calls the scaler makes, against one in-memory cluster.

    boto3 takes keyword arguments in the API's own spelling, so each fake reads them from kwargs.
    """

    def __init__(self, brokers: int, state: str = "ACTIVE", operations: list[dict] | None = None) -> None:
        self.brokers = brokers
        self.state = state
        self.operations = operations or []
        self.updates: list[int] = []

    def describe_cluster_v2(self, **kwargs: object) -> dict:
        assert kwargs["ClusterArn"] == ARN
        return {"ClusterInfo": {"State": self.state, "CurrentVersion": "K1",
                                "Provisioned": {"NumberOfBrokerNodes": self.brokers}}}

    def list_cluster_operations_v2(self, **kwargs: object) -> dict:
        assert kwargs["ClusterArn"] == ARN
        return {"ClusterOperationInfoList": self.operations}

    def update_broker_count(self, **kwargs: object) -> dict:
        assert kwargs["ClusterArn"] == ARN
        assert kwargs["CurrentVersion"] == "K1"
        target = int(kwargs["TargetNumberOfBrokerNodes"])
        self.updates.append(target)
        self.brokers = target
        return {"ClusterOperationArn": "arn:aws:kafka:op"}


class CloudWatch:
    """GetMetricData over per-broker BytesInPerSec: each broker carries `total / brokers`."""

    def __init__(self, total_bytes_per_sec: float, periods: int = 3) -> None:
        self.total = total_bytes_per_sec
        self.periods = periods
        self.asked_brokers: list[list[str]] = []

    def get_metric_data(self, **kwargs: object) -> dict:
        queries: list[dict] = kwargs["MetricDataQueries"]
        start: datetime = kwargs["StartTime"]
        end: datetime = kwargs["EndTime"]
        assert end - start == timedelta(seconds=SETTINGS.period_seconds * SETTINGS.evaluation_periods)
        dimensions = [q["MetricStat"]["Metric"]["Dimensions"] for q in queries if "MetricStat" in q]
        self.asked_brokers.append(sorted(d["Value"] for dims in dimensions for d in dims if d["Name"] == "Broker ID"))
        returned = [q for q in queries if q.get("ReturnData", True)]
        assert len(returned) == 1
        assert "Expression" in returned[0]
        per_broker = self.total / len(dimensions)
        return {"MetricDataResults": [{"Id": returned[0]["Id"], "Values": [per_broker] * self.periods}]}


@pytest.mark.parametrize(
    ("brokers", "total", "updates"),
    [
        pytest.param(3, 3 * PER_BROKER * 1.2, [6], id="over-on-three-adds-a-step"),
        pytest.param(3, 3 * PER_BROKER * 0.8, [], id="under-on-three-adds-nothing"),
        pytest.param(9, 9 * PER_BROKER * 1.5, [], id="at-the-ceiling-adds-nothing"),
    ],
)
def test_the_scaler_reads_the_load_on_the_brokers_the_cluster_has(
    brokers: int, total: float, updates: list[int]
) -> None:
    kafka, cloudwatch = Kafka(brokers), CloudWatch(total)
    scaler.scale(kafka, cloudwatch, SETTINGS, NOW)
    assert kafka.updates == updates
    assert cloudwatch.asked_brokers == [[str(b) for b in range(1, brokers + 1)]]


def test_sustained_pressure_scales_again_on_the_next_run() -> None:
    """The alarm this replaced fired once and stayed in ALARM, so a second step never came."""
    kafka, cloudwatch = Kafka(3), CloudWatch(9 * PER_BROKER * 1.2)
    scaler.scale(kafka, cloudwatch, SETTINGS, NOW)
    scaler.scale(kafka, cloudwatch, SETTINGS, NOW + timedelta(hours=1))
    assert kafka.updates == [6, 9]
    assert cloudwatch.asked_brokers[1] == [str(b) for b in range(1, 7)]


def test_a_window_with_missing_periods_is_no_pressure() -> None:
    kafka, cloudwatch = Kafka(3), CloudWatch(3 * PER_BROKER * 2, periods=2)
    scaler.scale(kafka, cloudwatch, SETTINGS, NOW)
    assert kafka.updates == []


def test_settings_come_from_the_lambda_environment() -> None:
    env = {
        "CLUSTER_ARN": ARN, "CLUSTER_NAME": "dfe-example", "MAX_BROKERS": "9", "STEP": "3",
        "THRESHOLD_BYTES_PER_SEC_PER_BROKER": "65000000", "PERIOD_SECONDS": "300", "EVALUATION_PERIODS": "3",
    }
    assert scaler.Settings.from_env(env) == SETTINGS
