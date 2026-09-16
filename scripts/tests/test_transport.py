#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_transport.py
#  Purpose:      Prove every chart derives bus-or-direct from the one
#                deployment-wide kafka.mode through the shared helper, and that
#                the direct form renders the Push listener the stages dial.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One transport derivation, read by every chart.

`kafka.mode` in the profile values is the deployment's one transport fact:
`disabled` means direct point-to-point gRPC, anything else means a broker holds
records between stages. dfe-common.transport turns it into the bus/direct
vocabulary the source model and the docs use, so a chart never compares that
string itself -- the duplication this replaced was the same `if eq .Values
.kafka.mode "disabled"` branch in three charts' templates.

    python3 scripts/tests/test_transport.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _charts import CHART_TREES
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
LIBRARY_HARNESS = REPO_ROOT / "helm" / "library" / "dfe-common" / "tests" / "lint-test"
VALUES = REPO_ROOT / "argocd" / "values"

# The brokerless profiles run direct; the rest run the bus. This is the whole
# mapping, and it lives in the profile files rather than in any chart.
PROFILE_TRANSPORT = {
    "slim": "direct",
    "mesh": "direct",
    "single": "bus",
    "scale": "bus",
}

# The stages that receive records over gRPC on the direct transport, so their
# charts render the scalo Push Service. dfe-loader is not here: its Service
# predates the helper, is named `grpc`, and renders on both transports.
PUSH_STAGES = [
    "dfe-archiver",
    "dfe-transform-vrl",
    "dfe-transform-vector",
    "dfe-transform-elastic",
]

# A grep for the branch the helper replaced. A chart comparing the mode itself
# is a second copy of the derivation, whatever it happens to conclude.
BRANCH = 'Values.kafka.mode) "disabled"'


def render(chart_dir: Path, name: str, *args: str) -> str:
    cmd = ["helm", "template", name, str(chart_dir), *args]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {name} {args}:\n{out.stderr}")
    return out.stdout


def docs(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if d]


def build_harness_deps() -> None:
    # The harness vendors dfe-common under charts/, which is not committed.
    if (LIBRARY_HARNESS / "charts").is_dir():
        return
    out = subprocess.run(
        ["helm", "dependency", "build", str(LIBRARY_HARNESS)],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise SystemExit(f"helm dependency build failed for the harness:\n{out.stderr}")


def harness(*args: str) -> dict:
    build_harness_deps()
    text = render(LIBRARY_HARNESS, "lt", *args)
    for doc in docs(text):
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"].endswith("-transport"):
            return doc["data"]
    raise SystemExit("the library harness rendered no transport ConfigMap")


def test_the_helper_reads_the_deployment_wide_mode() -> None:
    expect("an unset mode is the bus", harness()["transport"] == "bus", harness()["transport"])
    direct = harness("--set", "kafka.mode=disabled")
    expect("disabled is direct", direct["transport"] == "direct", direct["transport"])
    for mode in ("single", "cluster", "external"):
        got = harness("--set", f"kafka.mode={mode}")
        expect(f"{mode} is the bus", got["transport"] == "bus", got["transport"])


def test_is_bus_reads_as_a_boolean() -> None:
    expect("bus is truthy", harness()["isBus"] == "true", harness()["isBus"])
    direct = harness("--set", "kafka.mode=disabled")
    expect("direct is falsey", direct["isBus"] == "", repr(direct["isBus"]))


def test_a_chart_with_no_kafka_values_still_renders() -> None:
    """dfe-engine has no broker settings of its own; a missing map is the bus."""
    deployment = next(
        d
        for d in docs(render(CHARTS / "dfe-engine", "dfe-engine"))
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == "dfe-engine"
    )
    env = {e["name"]: e.get("value") for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    expect("the engine is told the transport", env.get("DFE_TRANSPORT_DEFAULT") == "bus",
           f"got {env.get('DFE_TRANSPORT_DEFAULT')!r}")
    expect("and whether a bus is present", env.get("DFE_TRANSPORT_BUS_PRESENT") == "true",
           f"got {env.get('DFE_TRANSPORT_BUS_PRESENT')!r}")


def test_the_engine_follows_the_profile() -> None:
    for profile, want in sorted(PROFILE_TRANSPORT.items()):
        text = render(
            CHARTS / "dfe-engine",
            "dfe-engine",
            "-f", str(VALUES / "common.yaml"),
            "-f", str(VALUES / f"profile-{profile}.yaml"),
        )
        deployment = next(
            d for d in docs(text)
            if d.get("kind") == "Deployment" and d["metadata"]["name"] == "dfe-engine"
        )
        env = {e["name"]: e.get("value") for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        expect(f"{profile} tells the engine {want}", env.get("DFE_TRANSPORT_DEFAULT") == want,
               f"got {env.get('DFE_TRANSPORT_DEFAULT')!r}")
        expect(f"{profile} bus_present agrees", env.get("DFE_TRANSPORT_BUS_PRESENT") == str(want == "bus").lower(),
               f"got {env.get('DFE_TRANSPORT_BUS_PRESENT')!r}")


def test_the_push_service_renders_on_direct_only() -> None:
    for chart in PUSH_STAGES:
        bus = [
            d for d in docs(render(CHARTS / chart, chart))
            if d.get("kind") == "Service"
        ]
        expect(f"{chart} has no Service on the bus", bus == [], f"got {len(bus)}")
        direct = [
            d for d in docs(render(CHARTS / chart, chart, "--set", "kafka.mode=disabled"))
            if d.get("kind") == "Service"
        ]
        expect(f"{chart} has one Service on direct", len(direct) == 1, f"got {len(direct)}")
        if direct:
            ports = direct[0]["spec"]["ports"]
            expect(f"{chart} serves push on 6000",
                   [(p["name"], p["port"]) for p in ports] == [("push", 6000)], f"got {ports!r}")


def test_the_transform_config_names_the_transport() -> None:
    """The Service is not enough: the app has to be TOLD which transport it is on.

    Both transform apps default to the bus, so a brokerless deploy whose
    ConfigMap said nothing would start a Kafka consumer against localhost and
    report healthy while carrying no records.

    Asserting on both is the point of the assertion: the engine has ONE
    `transform` routing compiler, so the four keys have to mean the same thing
    in both charts.
    """

    def config(chart: str, *args: str) -> dict:
        doc = next(
            d for d in docs(render(CHARTS / chart, chart, *args))
            if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-config")
        )
        return yaml.safe_load(doc["data"]["config.yaml"]) or {}

    for chart in ("dfe-transform-vrl", "dfe-transform-vector"):
        bus = config(chart)
        expect(f"{chart} on the bus says bus", bus["source"]["transport"] == "bus",
               f"got {bus['source']!r}")
        expect(f"{chart} sink on the bus says bus", bus["sink"]["transport"] == "bus",
               f"got {bus['sink']!r}")

        direct = config(chart, "--set", "kafka.mode=disabled")
        expect(f"{chart} on direct says direct", direct["source"]["transport"] == "direct",
               f"got {direct['source']!r}")
        expect(f"{chart} binds the push port", direct["source"]["listen"] == "0.0.0.0:6000",
               f"got {direct['source']!r}")
        expect(f"{chart} dials the loader", direct["sink"]["endpoint"] == "http://dfe-loader:6000",
               f"got {direct['sink']!r}")


def test_the_fetcher_dlq_follows_the_instance_output() -> None:
    """The engine compiles a direct source's fetcher to output.type grpc on a bus
    deploy too, and the fetcher refuses a kafka-only DLQ with no kafka output."""

    def env(*args: str) -> dict:
        deployment = next(
            d for d in docs(render(CHARTS / "dfe-fetcher", "dfe-fetcher", *args))
            if d.get("kind") == "Deployment"
        )
        return {e["name"]: e.get("value") for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}

    bus = env()
    expect("a bus instance dead-letters to kafka", bus.get("DFE_FETCHER_DLQ_MODE") == "kafka_only",
           f"got {bus.get('DFE_FETCHER_DLQ_MODE')!r}")
    expect("and is not switched off", "DFE_FETCHER_DLQ_ENABLED" not in bus, repr(bus.get("DFE_FETCHER_DLQ_ENABLED")))

    direct_on_bus = env("--set", "config.output.type=grpc")
    expect("a direct instance on a bus deploy switches the DLQ off",
           direct_on_bus.get("DFE_FETCHER_DLQ_ENABLED") == "false",
           f"got {direct_on_bus.get('DFE_FETCHER_DLQ_ENABLED')!r}")
    expect("and names no kafka mode", "DFE_FETCHER_DLQ_MODE" not in direct_on_bus,
           repr(direct_on_bus.get("DFE_FETCHER_DLQ_MODE")))

    direct = env("--set", "kafka.mode=disabled")
    expect("a direct deploy switches the DLQ off", direct.get("DFE_FETCHER_DLQ_ENABLED") == "false",
           f"got {direct.get('DFE_FETCHER_DLQ_ENABLED')!r}")


def test_no_chart_derives_the_transport_itself() -> None:
    offenders = [
        str(t.relative_to(REPO_ROOT))
        for tree in CHART_TREES
        for t in tree.glob("*/templates/**/*.yaml")
        if BRANCH in t.read_text(encoding="utf-8", errors="replace")
    ]
    expect("the derivation lives in one helper", offenders == [], f"got {offenders}")


def main() -> int:
    with standalone():
        test_the_helper_reads_the_deployment_wide_mode()
        test_is_bus_reads_as_a_boolean()
        test_a_chart_with_no_kafka_values_still_renders()
        test_the_engine_follows_the_profile()
        test_the_push_service_renders_on_direct_only()
        test_the_transform_config_names_the_transport()
        test_the_fetcher_dlq_follows_the_instance_output()
        test_no_chart_derives_the_transport_itself()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
