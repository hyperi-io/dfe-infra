#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_smoke_kafka_broker_pod.py
#  Purpose:      Prove CORE 3 of the integration smoke test runs its Kafka checks
#                in a broker pod, found by label, and that its consumer-group
#                check needs a real committed offset from the loader's own group,
#                read by OffsetFetch from dfe-engine when the Kafka tool errors.
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
# What kafka-consumer-groups.sh 4.3.1 printed against MSK Express in a cloud cycle, verbatim.
MSK_GROUPS_ERROR = (
    "\nError: Executing consumer group command failed due to java.util.concurrent."
    "ExecutionException: org.apache.kafka.common.errors.UnknownServerException: The server "
    "experienced an unexpected error when processing the request."
)
GROUP_CHECK = "consumer group dfe-loader is committed on main_land (loader CONSUMED)"
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


def smoke(
    pods: list,
    profile: str = "scale",
    *,
    engine_offsets: dict | None = None,
    engine_env: dict | None = None,
    engine_ready: bool = False,
    **kafka: object,
) -> tuple[str, dict]:
    fixture: dict = {
        "otel_zero_answers": 0,
        "kafka": {
            "namespace": NS,
            "pods": pods,
            "topics": TOPICS,
            "group_output": LOADER_GROUP,
            **kafka,
        },
    }
    # A dfe-engine whose schema converged, so CORE 0 passes and only CORE 3 can fail the run.
    if engine_ready:
        fixture["engine_schema"] = True
    if engine_offsets is not None:
        fixture["engine_offsets"] = engine_offsets
    if engine_env is not None:
        fixture["engine_env"] = engine_env
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
        f"[PASS] {GROUP_CHECK}: offset 5, read by kafka-consumer-groups.sh" in stdout,
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
    stdout, counts = smoke(SCALE_NAMESPACE, group_output=MSK_GROUPS_ERROR)

    expect("the group check fails", f"[FAIL] {GROUP_CHECK}\n" in stdout, stdout)
    expect(
        "it prints the tool's first line",
        f"    kafka-consumer-groups.sh: {MSK_GROUPS_ERROR.strip()}\n" in stdout,
        stdout,
    )
    expect("the fallback was asked", counts.get("engine_offsets") == 1, str(counts))
    expect(
        "and its failure is printed too",
        "    dfe-engine OffsetFetch: Error: KafkaException: " in stdout,
        stdout,
    )


def test_the_check_asks_for_the_loaders_group_by_name() -> None:
    stdout, counts = smoke(SCALE_NAMESPACE)

    expect("the check names the group", GROUP_CHECK in stdout, stdout)
    asked = counts.get("groups_asked:dfe-loader")
    expect("the tool is asked for dfe-loader", asked == 1, str(counts))
    expect("never for every group", "groups_asked:--all-groups" not in counts, str(counts))


def test_a_group_with_no_offset_on_the_topic_does_not_count() -> None:
    other_topic = LOADER_GROUP.replace("main_land", "other_land")
    nothing_committed = LOADER_GROUP.replace("5               5", "-               5")
    # The archiver reads the _land topics too, and its commit is not the loader's.
    other_group = LOADER_GROUP.replace("dfe-loader  main_land", "dfe-archiver  main_land")
    for name, output in (
        ("another topic", other_topic),
        ("no committed offset", nothing_committed),
        ("another group's commit", other_group),
    ):
        stdout, counts = smoke(SCALE_NAMESPACE, group_output=output)
        expect(f"{name} fails the check", f"[FAIL] {GROUP_CHECK}\n" in stdout, stdout)
        expect(
            f"{name}: the tool answered, so no fallback is asked",
            "engine_offsets" not in counts,
            str(counts),
        )


# The engine's own bus settings and credential, as dfe-engine-env and dfe-kafka-user set them.
ENGINE_ENV = {
    "DFE_KAFKA_BOOTSTRAP_SERVERS": "b-1.kafka.example.com:9096",
    "DFE_KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
    "DFE_KAFKA_SASL_MECHANISM": "SCRAM-SHA-512",
    "DFE_KAFKA_SASL_USERNAME": "dfe-kafka-user",
    "DFE_KAFKA_SASL_PASSWORD": "engine-held-pw",
}


def test_a_tool_error_falls_back_to_offset_fetch_which_passes_on_a_commit() -> None:
    offsets = {
        "dfe-loader": [
            ["main_land", 0, 2],
            ["main_land", 1, 1],
            ["main_land", 2, -1001],
            ["other_land", 0, 9],
        ],
        "dfe-archiver": [["main_land", 0, 40]],
    }
    stdout, counts = smoke(
        SCALE_NAMESPACE, group_output=MSK_GROUPS_ERROR, engine_offsets=offsets,
        engine_env=ENGINE_ENV,
    )

    expect(
        "the fallback proves the loader's commit, and the PASS names it",
        f"[PASS] {GROUP_CHECK}: offset 3, read by librdkafka OffsetFetch from dfe-engine\n"
        f"    kafka-consumer-groups.sh failed first: {MSK_GROUPS_ERROR.strip()}\n" in stdout,
        stdout,
    )
    expect(
        "it dials with the engine's own settings and credential",
        counts.get("engine_conf") == {
            "bootstrap.servers": "b-1.kafka.example.com:9096",
            "security.protocol": "SASL_SSL",
            "sasl.mechanisms": "SCRAM-SHA-512",
            "sasl.username": "dfe-kafka-user",
            "sasl.password": "engine-held-pw",
        },
        str(counts.get("engine_conf")),
    )
    expect("no credential reaches the output", "engine-held-pw" not in stdout, stdout)


def test_a_fallback_with_no_commit_on_the_topic_stays_a_fail() -> None:
    for name, offsets in (
        ("only another topic", {"dfe-loader": [["other_land", 0, 9]]}),
        ("no committed offset", {"dfe-loader": [["main_land", 0, -1001]]}),
        ("only another group", {"dfe-archiver": [["main_land", 0, 40]]}),
    ):
        stdout, _ = smoke(SCALE_NAMESPACE, group_output=MSK_GROUPS_ERROR, engine_offsets=offsets)
        expect(f"{name} fails the check", f"[FAIL] {GROUP_CHECK}\n" in stdout, stdout)
        expect(
            f"{name}: what the fallback found is printed",
            "    dfe-engine OffsetFetch: committed 0 over 0 partition(s) of main_land" in stdout,
            stdout,
        )


def test_a_fallback_that_errors_stays_a_fail() -> None:
    refused = {"error": "KafkaError{code=GROUP_AUTHORIZATION_FAILED}"}
    stdout, _ = smoke(SCALE_NAMESPACE, group_output=MSK_GROUPS_ERROR, engine_offsets=refused)

    expect("the group check fails", f"[FAIL] {GROUP_CHECK}\n" in stdout, stdout)
    expect(
        "the fallback's error is printed",
        "    dfe-engine OffsetFetch: Error: KafkaException: "
        "KafkaError{code=GROUP_AUTHORIZATION_FAILED}" in stdout,
        stdout,
    )


def test_a_plaintext_engine_dials_with_no_sasl() -> None:
    plaintext = {
        "DFE_KAFKA_BOOTSTRAP_SERVERS": "dfe-kafka:9092",
        "DFE_KAFKA_SECURITY_PROTOCOL": "PLAINTEXT",
    }
    _, counts = smoke(
        SCALE_NAMESPACE, group_output=MSK_GROUPS_ERROR,
        engine_offsets={"dfe-loader": [["main_land", 0, 1]]}, engine_env=plaintext,
    )

    expect(
        "only the bootstrap list is set",
        counts.get("engine_conf") == {"bootstrap.servers": "dfe-kafka:9092"},
        str(counts.get("engine_conf")),
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
    stdout, counts = smoke(
        [sidecar, broker], rpk_groups=RPK_GROUPS, rpk_describe=RPK_LOADER, engine_ready=True
    )

    expect("the broker is used", execs(counts, "redpanda-0") > 0, str(counts))
    expect("the other pod is not", execs(counts, "redpanda-console-7d9f") == 0, str(counts))
    expect("the topic is found", "[PASS] topic main_land exists (created)" in stdout, stdout)
    expect("its messages are counted", f"[PASS] {PRODUCED_CHECK}" in stdout, stdout)
    expect(
        "the loader's group is found",
        f"[PASS] {GROUP_CHECK}: offset 5, read by rpk group describe" in stdout,
        stdout,
    )
    expect("it is described by name", counts.get("rpk_described:dfe-loader") == 1, str(counts))
    expect("the whole run passes", "Vertical integration verified." in stdout, stdout)


PRODUCED_CHECK = "topic main_land has messages (receiver PRODUCED)"


def rpk_partitions(replicas: str, log_start: int, high_watermark: int) -> str:
    """`rpk topic describe main_land -p` for one partition, REPLICAS printed as rpk does."""
    header = "PARTITION  LEADER  EPOCH  REPLICAS  LOG-START-OFFSET  HIGH-WATERMARK"
    return f"{header}\n0          0       1      {replicas}   {log_start}   {high_watermark}"


def test_on_redpanda_produced_reads_the_high_watermark_whatever_the_replica_count() -> None:
    broker = pod("redpanda-0", {"cluster.redpanda.com/broker": "true"}, "redpanda")
    for name, partitions, verdict in (
        ("RF 3, high watermark 0", rpk_partitions("[0 1 2]", 0, 0), "FAIL"),
        ("RF 1, log start 0 and high watermark 5", rpk_partitions("[0]", 0, 5), "PASS"),
        ("RF 3, high watermark 7", rpk_partitions("[0 1 2]", 2, 7), "PASS"),
    ):
        stdout, _ = smoke(
            [broker], rpk_groups=RPK_GROUPS, rpk_describe=RPK_LOADER, rpk_partitions=partitions,
            engine_ready=True,
        )
        shown = f"[{verdict}] {PRODUCED_CHECK}" in stdout
        expect(f"{name}: PRODUCED is a {verdict}", shown, stdout)
        passed = "Vertical integration verified." in stdout
        expect(f"{name}: the run's verdict follows it", passed == (verdict == "PASS"), stdout)


# What `rpk group list` prints with only the archiver's group on the broker.
RPK_GROUPS = "BROKER  GROUP         STATE\n0       dfe-archiver  Stable"
# What `rpk group describe dfe-loader` prints for a group with a commit of 5 on main_land.
RPK_LOADER = (
    "GROUP        dfe-loader\nCOORDINATOR  0\nSTATE        Stable\nMEMBERS      1\n"
    "TOTAL-LAG    0\n\n"
    "TOPIC      PARTITION  CURRENT-OFFSET  LOG-START-OFFSET  LOG-END-OFFSET  LAG  MEMBER-ID\n"
    "main_land  0          5               0                 5               0    dfe-loader-1-a"
)


def test_on_redpanda_another_groups_presence_is_not_the_loaders_commit() -> None:
    broker = pod("redpanda-0", {"cluster.redpanda.com/broker": "true"}, "redpanda")
    never_committed = "GROUP        dfe-loader\nCOORDINATOR  0\nSTATE        Dead\nMEMBERS      0"
    for name, describe in (
        ("a group that never committed", never_committed),
        ("another topic's commit", RPK_LOADER.replace("main_land", "other_land")),
        ("no committed offset", RPK_LOADER.replace("5               0", "-               0")),
    ):
        stdout, counts = smoke([broker], rpk_groups=RPK_GROUPS, rpk_describe=describe)
        expect(f"{name} fails the check", f"[FAIL] {GROUP_CHECK}\n" in stdout, stdout)
        expect(f"{name}: no fallback is asked", "engine_offsets" not in counts, str(counts))


DLQ_RETENTION = "DLQ retention is longer than the data-topic default"


def test_the_dlq_retention_is_asserted_on_a_strimzi_broker() -> None:
    stdout, _ = smoke(SCALE_NAMESPACE)

    expect("the seven-day DLQ retention passes", f"[PASS] {DLQ_RETENTION}" in stdout, stdout)
    expect("it is never skipped", "[SKIP] DLQ retention" not in stdout, stdout)


def test_a_wrong_dlq_retention_fails() -> None:
    stdout, _ = smoke(SCALE_NAMESPACE, dlq_retention_ms="259200000")

    expect("the data-topic default on a DLQ fails", f"[FAIL] {DLQ_RETENTION}" in stdout, stdout)


def test_a_withheld_describeconfigs_fails_rather_than_skips() -> None:
    # The DFE user holds DescribeConfigs on every broker, so a refusal is a broken grant.
    stdout, _ = smoke(SCALE_NAMESPACE, dlq_retention_ms=None)

    expect("a refused describe fails", f"[FAIL] {DLQ_RETENTION}" in stdout, stdout)
    expect("it is not reported as a skip", "[SKIP] DLQ retention" not in stdout, stdout)


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
