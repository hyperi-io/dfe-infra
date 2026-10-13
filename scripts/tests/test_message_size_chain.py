#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_message_size_chain.py
#  Purpose:      Prove a managed broker's message size reaches every app-side
#                record limit, and an in-cluster broker moves none of them.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The record limits each Kafka-producing app takes from kafka_message_max_bytes.

    python3 -m pytest scripts/tests/test_message_size_chain.py -q

bootstrap.sh writes the size a managed broker was built with onto the cluster
secret, and layer2-apps.yaml hands it on. A producer left at the stack's 16 MiB
sends a record a smaller topic refuses, and a receiver door above the topic
admits a body none of its records can land. Each app reads its limit through
its own key, at the release versions.yaml pins:

- dfe-receiver v1.15.45: DFE_RECEIVER_MAX_BODY_SIZE (src/config/mod.rs:738), and
  kafka.librdkafka_overrides (src/config/mod.rs:2493), which its sink and its
  DLQ producer both take (src/config/mod.rs:2232)
- dfe-loader v1.18.50: kafka.librdkafka_overrides (src/config/kafka.rs:41), which
  its DLQ producer is built from (src/kafka/transport.rs:136)
- dfe-transform-vrl v1.1.33: sink.librdkafka_options (src/kafka/mod.rs:85)
- dfe-transform-vector v1.0.48: sink.librdkafka_options, which also sets the
  size filter in front of Vector's sink (src/config/generate.rs:252)
- dfe-transform-elastic v1.1.12: DFE_TRANSFORM_ELASTIC_SINK_MAX_MESSAGE_BYTES
  (src/config/loader.rs:200 and :281)

dfe-fetcher and dfe-archiver read the key from their first release after the
v1.4.36 and v1.7.32 versions.yaml pins, and lay the map over their own settings
key by key rather than replacing a default map:

- dfe-fetcher: kafka.librdkafka_overrides (crates/fetcher/src/config/mod.rs:4549),
  over the producer section its output and DLQ producers are built from
  (crates/fetcher/src/output.rs:579)
- dfe-archiver: kafka.librdkafka_overrides (crates/core/src/config.rs:308), for
  its consumer and DLQ producer, with statistics.interval.ms kept at 5000 unless
  the map names it (crates/io/src/kafka.rs:256)

An image before those releases ignores the key: the thin chart writes
configOverrides into the config file unvalidated, and neither app refuses an
unknown key there.

The renders need helm and the scalo-service library (_weave.library).
"""

import functools

import pytest
import yaml

from _gate import effective_env, golden, object_id, render_differences
from _weave import render_app

SIZE_ANNOTATION = "dfe.hyperi.io/kafka_message_max_bytes"

# Confluent Basic and Standard cap a topic's max.message.bytes here.
BASIC = 8388608

# The stack's own record ceiling, scalo-rs MESSAGE_MAX_BYTES.
STACK = 16777216

# A managed broker's cluster secret as bootstrap.sh writes it. The endpoint is an
# RFC 2606 placeholder: this repo ships publicly.
MANAGED = {
    "dfe.hyperi.io/kafka_mode": "external",
    "dfe.hyperi.io/kafka_bootstrap": "b-1.example.invalid:9096",
    "dfe.hyperi.io/kafka_security_protocol": "SASL_SSL",
}

# A bus tier, where every producer here sends to Kafka.
CELL = ("scale", "aws")

# Each env var a producer reads a record limit from.
ENV_LIMITS: dict[str, tuple[str, ...]] = {
    "dfe-receiver": ("DFE_RECEIVER_MAX_BODY_SIZE",),
    "dfe-transform-elastic": ("DFE_TRANSFORM_ELASTIC_SINK_MAX_MESSAGE_BYTES",),
}

# Each config-file path a producer reads librdkafka's message.max.bytes from.
# Tuples, because the last key carries dots of its own.
CONFIG_LIMITS: dict[str, tuple[tuple[str, ...], ...]] = {
    "dfe-receiver": (("kafka", "librdkafka_overrides", "message.max.bytes"),),
    "dfe-loader": (("kafka", "librdkafka_overrides", "message.max.bytes"),),
    "dfe-fetcher": (("kafka", "librdkafka_overrides", "message.max.bytes"),),
    "dfe-archiver": (("kafka", "librdkafka_overrides", "message.max.bytes"),),
    "dfe-transform-vrl": (("sink", "librdkafka_options", "message.max.bytes"),),
    "dfe-transform-vector": (("sink", "librdkafka_options", "message.max.bytes"),),
}

PRODUCERS = tuple(sorted({*ENV_LIMITS, *CONFIG_LIMITS}))


@functools.cache
def _render(app: str, size: str) -> tuple[dict, ...]:
    return tuple(render_app(app, *CELL, annotations={**MANAGED, SIZE_ANNOTATION: size}))


def config_file(docs: list[dict] | tuple[dict, ...], app: str) -> dict:
    """The app's config file as the thin chart writes it into <app>-config."""
    configmap = next(d for d in docs if object_id(d) == f"ConfigMap/{app}-config")
    (text,) = configmap["data"].values()
    return yaml.safe_load(text) or {}


def _dig(doc: object, path: tuple[str, ...]) -> object:
    for key in path:
        if not isinstance(doc, dict) or key not in doc:
            return None
        doc = doc[key]
    return doc


def limits(app: str, size: str) -> dict[str, str]:
    """Every record limit the render hands ``app``, keyed by where it reads it."""
    docs = _render(app, size)
    env = effective_env(list(docs), f"Deployment/{app}")
    config = config_file(docs, app)
    found = {name: env[name] for name in ENV_LIMITS.get(app, ()) if env.get(name)}
    for path in CONFIG_LIMITS.get(app, ()):
        value = _dig(config, path)
        if value is not None:
            found[".".join(path)] = value
    return found


@pytest.mark.parametrize("app", PRODUCERS)
def test_a_managed_size_bounds_every_limit_the_app_reads(app: str) -> None:
    found = limits(app, str(BASIC))
    wanted = {*ENV_LIMITS.get(app, ()), *(".".join(p) for p in CONFIG_LIMITS.get(app, ()))}
    assert set(found) == wanted, f"{app} is handed {sorted(found)}, not {sorted(wanted)}"
    over = {where: value for where, value in found.items() if int(value) > BASIC}
    assert over == {}, f"{app} is handed limits above the {BASIC}-byte topic ceiling"


@pytest.mark.parametrize("app", sorted(CONFIG_LIMITS))
def test_the_producer_ceiling_is_the_topic_ceiling(app: str) -> None:
    """Broker, topic and producer agree on one number, so a record the producer
    takes the topic takes too, and an oversize one is screened before the send."""
    for path in CONFIG_LIMITS[app]:
        assert _dig(config_file(_render(app, str(BASIC)), app), path) == str(BASIC)


@pytest.mark.parametrize(
    ("app", "name", "wanted"),
    [
        # The 10 MiB door under the 16 MiB chain: room for a transform to grow a record.
        ("dfe-receiver", "DFE_RECEIVER_MAX_BODY_SIZE", BASIC * 5 // 8),
        # The 1 MiB in 16 the app keeps for framing, headers and zstd growth.
        ("dfe-transform-elastic", "DFE_TRANSFORM_ELASTIC_SINK_MAX_MESSAGE_BYTES", BASIC * 15 // 16),
    ],
)
def test_a_payload_limit_leaves_room_under_the_ceiling(app: str, name: str, wanted: int) -> None:
    got = limits(app, str(BASIC)).get(name)
    assert got == str(wanted), f"{name} is {got!r}"
    assert int(got) < BASIC


@pytest.mark.parametrize(
    ("app", "name", "default"),
    [
        # dfe-receiver v1.15.45 src/config/mod.rs:1007.
        ("dfe-receiver", "DFE_RECEIVER_MAX_BODY_SIZE", 10 * 1024 * 1024),
        # dfe-transform-elastic v1.1.12 src/config.rs:291.
        ("dfe-transform-elastic", "DFE_TRANSFORM_ELASTIC_SINK_MAX_MESSAGE_BYTES", 15 * 1024 * 1024),
    ],
)
def test_at_the_stacks_own_size_a_payload_limit_is_the_apps_default(
    app: str, name: str, default: int
) -> None:
    """A managed broker built at 16 MiB moves no app off what it runs in-cluster."""
    assert limits(app, str(STACK)).get(name) == str(default)


@pytest.mark.parametrize(
    ("app", "interval"),
    [
        # dfe-receiver v1.15.45 src/config/mod.rs:2500: no stats handler reads them.
        ("dfe-receiver", "0"),
        # dfe-loader v1.18.50 src/config/kafka.rs:63: its Kafka-lag scaling input.
        ("dfe-loader", "5000"),
    ],
)
def test_a_replaced_overrides_map_keeps_the_apps_statistics_interval(
    app: str, interval: str
) -> None:
    """A config file's librdkafka_overrides replaces the app's default map whole."""
    config = config_file(_render(app, str(BASIC)), app)
    got = _dig(config, ("kafka", "librdkafka_overrides", "statistics.interval.ms"))
    assert got == interval, f"{app} statistics.interval.ms is {got!r}"


@pytest.mark.parametrize("app", ["dfe-archiver", "dfe-fetcher"])
def test_a_merged_overrides_map_carries_the_size_alone(app: str) -> None:
    """These apps lay the map over their own settings key by key, so any key named
    here replaces the app's own, and a "0" interval would stop the archiver's lag signal."""
    config = config_file(_render(app, str(BASIC)), app)
    got = _dig(config, ("kafka", "librdkafka_overrides"))
    assert got == {"message.max.bytes": str(BASIC)}, f"{app} librdkafka_overrides is {got!r}"


@pytest.mark.parametrize("app", PRODUCERS)
def test_an_in_cluster_broker_moves_nothing(app: str) -> None:
    """bootstrap.sh writes the annotation empty for strimzi and redpanda."""
    assert limits(app, "") == {}
    in_cluster = render_app(app, *CELL, annotations={SIZE_ANNOTATION: ""})
    assert render_differences(golden(app, *CELL), in_cluster) == []
