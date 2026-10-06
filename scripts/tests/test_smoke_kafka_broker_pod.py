#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_smoke_kafka_broker_pod.py
#  Purpose:      Prove CORE 3 of the integration smoke test runs its Kafka checks
#                in a broker pod, found by label, and that its consumer-group
#                check needs a real committed offset.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""CORE 3 picks its pod by the provider's broker label, never by name order.

On the scale tier the strimzi namespace lists Cruise Control ahead of the
brokers, and a name match on "kafka" took it. Its Kafka tools find no broker, so
the topic, offset and DLQ checks failed on a healthy stack, and the group check
passed on the tool's own error text. A fake `kubectl` first on PATH lists the
namespace the way Strimzi, the single-tier StatefulSet and the Redpanda operator
lay it out, and counts the execs each pod receives.

    python3 scripts/tests/test_smoke_kafka_broker_pod.py

No third-party deps and no test runner, matching the script it tests.
"""

import sys

from _expect import expect, standalone, summary
from _smoke import run_smoke

NS = "strimzi"
LOADER_GROUP = (
    "GROUP       TOPIC      PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG\n"
    "dfe-loader  main_land  0          5               5               0\n"
    "dfe-loader  main_land  1          -               0               -"
)
TOPICS = [
    "main_land",
    "dfe_receiver_dlq",
    "dfe_loader_dlq",
    "dfe_archiver_dlq",
    "dfe_fetcher_dlq",
    "dfe_transform_dlq",
]
STRIMZI = {"strimzi.io/cluster": "dfe-kafka", "strimzi.io/kind": "Kafka"}


def pod(name: str, labels: dict, role: str, phase: str = "Running") -> dict:
    return {"name": name, "labels": labels, "role": role, "phase": phase}


def strimzi_broker(index: int) -> dict:
    labels = {**STRIMZI, "strimzi.io/broker-role": "true", "strimzi.io/controller-role": "true"}
    return pod(f"dfe-kafka-dfe-kafka-pool-{index}", labels, "broker")


CRUISE_CONTROL = pod(
    "dfe-kafka-cruise-control-6848fdcd66-zl5ht",
    {**STRIMZI, "strimzi.io/component-type": "cruise-control"},
    "other",
)
CRUISE_CONTROL_UI = pod(
    "dfe-kafka-cruise-control-ui-c564c4844-kbg6h",
    {"app.kubernetes.io/name": "dfe-kafka", "app.kubernetes.io/component": "cruise-control-ui"},
    "other",
)
ENTITY_OPERATOR = pod(
    "dfe-kafka-entity-operator-567796dc85-dfx2p",
    {**STRIMZI, "strimzi.io/component-type": "entity-operator"},
    "other",
)
CLUSTER_OPERATOR = pod(
    "strimzi-cluster-operator-57b47c8c7f-nrl69",
    {"strimzi.io/kind": "cluster-operator"},
    "other",
)
# Name order, as `kubectl get pods` lists the strimzi namespace on the scale tier.
SCALE_NAMESPACE = [
    CRUISE_CONTROL,
    CRUISE_CONTROL_UI,
    strimzi_broker(0),
    strimzi_broker(1),
    strimzi_broker(2),
    ENTITY_OPERATOR,
    CLUSTER_OPERATOR,
]
NO_BROKER = [CRUISE_CONTROL, CRUISE_CONTROL_UI, ENTITY_OPERATOR, CLUSTER_OPERATOR]


def smoke(pods: list, profile: str = "scale", **kafka: object) -> tuple[str, dict]:
    fixture = {
        "otel_zero_answers": 0,
        "kafka": {
            "namespace": NS,
            "pods": pods,
            "topics": TOPICS,
            "group_output": LOADER_GROUP,
            **kafka,
        },
    }
    out, counts = run_smoke(fixture, DFE_OTEL_WAIT="0", DFE_PROFILE=profile)
    return out.stdout, counts


def execs(counts: dict, *names: str) -> int:
    return sum(n for key, n in counts.items() if key.removeprefix("kafka_exec:") in names)


def test_the_checks_run_in_a_broker_and_not_in_cruise_control() -> None:
    stdout, counts = smoke(SCALE_NAMESPACE)

    expect("the topic is found", "[PASS] topic main_land exists (created)" in stdout, stdout)
    expect(
        "its messages are counted",
        "[PASS] topic main_land has messages (receiver PRODUCED)" in stdout,
        stdout,
    )
    expect(
        "the loader's group is found",
        "[PASS] a consumer group is committed on main_land (loader CONSUMED)" in stdout,
        stdout,
    )
    expect(
        "every DLQ topic is found",
        all(f"[PASS] DLQ topic {t} pre-created" in stdout for t in TOPICS[1:]),
        stdout,
    )
    brokers = [p["name"] for p in SCALE_NAMESPACE if p["role"] == "broker"]
    expect("a broker received the execs", execs(counts, *brokers) > 0, str(counts))
    expect(
        "no other pod received one",
        execs(counts, *[p["name"] for p in SCALE_NAMESPACE if p["role"] != "broker"]) == 0,
        str(counts),
    )


def test_a_broker_is_found_when_the_first_kafka_named_pod_is_not_one() -> None:
    # Cruise Control sorts first and runs: the old name match took it, so only
    # the label can reach the broker behind it.
    stdout, counts = smoke([CRUISE_CONTROL, strimzi_broker(0)])

    expect("the broker is used", execs(counts, strimzi_broker(0)["name"]) > 0, str(counts))
    expect("Cruise Control is not", execs(counts, CRUISE_CONTROL["name"]) == 0, str(counts))
    core_3 = stdout.split("=== CORE 3")[1].split("=== Ancillary")[0]
    expect("so the seam passes", "[FAIL]" not in core_3, stdout)


def test_no_broker_fails_a_tier_that_runs_one() -> None:
    stdout, counts = smoke(NO_BROKER)

    expect(
        "the missing broker is a FAIL, naming the tier",
        "[FAIL] kafka broker pod present in ns/strimzi (required by the scale tier)" in stdout,
        stdout,
    )
    expect(
        "nothing is exec'd in a pod that is not one",
        execs(counts, *[p["name"] for p in NO_BROKER]) == 0,
        str(counts),
    )
    expect("no Kafka check reports", "topic main_land" not in stdout, stdout)


def test_no_broker_skips_a_brokerless_tier() -> None:
    stdout, _ = smoke(NO_BROKER, profile="slim")

    expect(
        "the seam is skipped", "[SKIP] kafka seam -- no broker pod in ns/strimzi" in stdout, stdout
    )


def test_a_tool_error_is_not_a_committed_group() -> None:
    error = "Error: Executing consumer group command failed due to Timed out waiting for a node"
    stdout, _ = smoke(SCALE_NAMESPACE, group_output=error)

    expect(
        "the group check fails",
        "[FAIL] a consumer group is committed on main_land (loader CONSUMED)" in stdout,
        stdout,
    )


def test_a_group_with_no_offset_on_the_topic_does_not_count() -> None:
    other_topic = LOADER_GROUP.replace("main_land", "other_land")
    nothing_committed = LOADER_GROUP.replace("5               5", "-               5")
    for name, output in (
        ("another topic", other_topic),
        ("no committed offset", nothing_committed),
    ):
        stdout, _ = smoke(SCALE_NAMESPACE, group_output=output)
        expect(
            f"a group with {name} fails the check",
            "[FAIL] a consumer group is committed on main_land (loader CONSUMED)" in stdout,
            stdout,
        )


def test_the_single_tier_broker_is_found_beside_a_pod_that_shares_its_name_label() -> None:
    broker = pod(
        "dfe-kafka-0",
        {"app.kubernetes.io/name": "dfe-kafka", "app.kubernetes.io/instance": "dfe-kafka"},
        "broker",
    )
    stdout, counts = smoke([CRUISE_CONTROL_UI, broker], profile="single")

    expect("the StatefulSet pod is used", execs(counts, "dfe-kafka-0") > 0, str(counts))
    expect("the component pod is not", execs(counts, CRUISE_CONTROL_UI["name"]) == 0, str(counts))
    expect("the topic is found", "[PASS] topic main_land exists (created)" in stdout, stdout)


def test_a_redpanda_broker_is_found_by_its_label() -> None:
    broker = pod(
        "redpanda-0",
        {"cluster.redpanda.com/broker": "true", "app.kubernetes.io/name": "redpanda"},
        "redpanda",
    )
    sidecar = pod("redpanda-console-7d9f", {"app.kubernetes.io/name": "console"}, "other")
    stdout, counts = smoke([sidecar, broker], rpk_groups="BROKER GROUP STATE\n0 dfe-loader Stable")

    expect("the broker is used", execs(counts, "redpanda-0") > 0, str(counts))
    expect("the other pod is not", execs(counts, "redpanda-console-7d9f") == 0, str(counts))
    expect("the topic is found", "[PASS] topic main_land exists (created)" in stdout, stdout)
    expect(
        "the group is found",
        "[PASS] a consumer group is committed on main_land (loader CONSUMED)" in stdout,
        stdout,
    )


def test_an_rpk_group_list_with_only_its_header_is_not_a_group() -> None:
    broker = pod("redpanda-0", {"cluster.redpanda.com/broker": "true"}, "redpanda")
    stdout, _ = smoke([broker], rpk_groups="BROKER GROUP STATE")

    expect(
        "the group check fails",
        "[FAIL] a consumer group is committed on main_land (loader CONSUMED)" in stdout,
        stdout,
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
