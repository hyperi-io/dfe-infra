"""Add STEP brokers to an MSK Express cluster while its brokers stay over their throughput, and never take any away.

EventBridge runs this every PERIOD_SECONDS. Each run reads the cluster's own
broker count and the BytesInPerSec of exactly those brokers, so the load it
judges is the cluster's as it is now, and pressure that outlasts one step brings
the next. The event body is not read.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


@dataclass(frozen=True)
class Settings:
    """What autoscaler.tf hands the function in its environment."""

    cluster_arn: str
    cluster_name: str
    max_brokers: int
    step: int
    threshold_bytes_per_sec: float
    period_seconds: int
    evaluation_periods: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        return cls(
            cluster_arn=env["CLUSTER_ARN"],
            cluster_name=env["CLUSTER_NAME"],
            max_brokers=int(env["MAX_BROKERS"]),
            step=int(env["STEP"]),
            threshold_bytes_per_sec=float(env["THRESHOLD_BYTES_PER_SEC_PER_BROKER"]),
            period_seconds=int(env["PERIOD_SECONDS"]),
            evaluation_periods=int(env["EVALUATION_PERIODS"]),
        )

    @property
    def window(self) -> timedelta:
        return timedelta(seconds=self.period_seconds * self.evaluation_periods)


def per_broker_load(cloudwatch: Any, settings: Settings, brokers: int, now: datetime) -> list[float]:
    """Mean BytesInPerSec per broker, one value per period of the last window, over brokers 1..brokers.

    Average, not Sum: BytesInPerSec is already a per-second rate published once a
    minute, so Sum would add the samples in a period and read several times hot.
    A broker with no datapoint counts as 0, which reads low rather than high.
    """
    ids = [f"broker_{n}" for n in range(1, brokers + 1)]
    queries: list[dict[str, Any]] = [
        {
            "Id": query_id,
            "ReturnData": False,
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/Kafka",
                    "MetricName": "BytesInPerSec",
                    "Dimensions": [
                        {"Name": "Cluster Name", "Value": settings.cluster_name},
                        {"Name": "Broker ID", "Value": str(n)},
                    ],
                },
                "Period": settings.period_seconds,
                "Stat": "Average",
            },
        }
        for n, query_id in enumerate(ids, start=1)
    ]
    queries.append({"Id": "per_broker", "Expression": f"SUM([{','.join(ids)}])/{brokers}", "ReturnData": True})
    result = cloudwatch.get_metric_data(MetricDataQueries=queries, StartTime=now - settings.window, EndTime=now)
    return [float(v) for r in result["MetricDataResults"] if r["Id"] == "per_broker" for v in r["Values"]]


def scale(kafka: Any, cloudwatch: Any, settings: Settings, now: datetime) -> dict[str, Any]:
    """One run: read the cluster, judge its current brokers' load, add a step if every period was over."""
    info = kafka.describe_cluster_v2(ClusterArn=settings.cluster_arn)["ClusterInfo"]
    current = int(info["Provisioned"]["NumberOfBrokerNodes"])

    load = per_broker_load(cloudwatch, settings, current, now)
    if len(load) < settings.evaluation_periods or min(load) <= settings.threshold_bytes_per_sec:
        return {"scaled": False, "reason": "no sustained pressure", "current_brokers": current}

    target = current + settings.step
    if target > settings.max_brokers:
        logger.info("refusing to scale %s: %d brokers would exceed max_brokers %d",
                    settings.cluster_arn, target, settings.max_brokers)
        return {"scaled": False, "reason": "at max_brokers ceiling", "current_brokers": current}

    logger.info("scaling %s from %d to %d brokers", settings.cluster_arn, current, target)
    response = kafka.update_broker_count(
        ClusterArn=settings.cluster_arn,
        CurrentVersion=info["CurrentVersion"],
        TargetNumberOfBrokerNodes=target,
    )
    logger.info("update_broker_count accepted: %s", response.get("ClusterOperationArn"))
    return {"scaled": True, "from": current, "to": target}


def handler(event: object, context: object) -> dict[str, Any]:
    import boto3  # in the Lambda runtime, never needed to import this module

    return scale(boto3.client("kafka"), boto3.client("cloudwatch"), Settings.from_env(os.environ), datetime.now(UTC))
