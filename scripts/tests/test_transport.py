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
"""One transport derivation, read by every deployment.

`kafka.mode` in the profile values is the deployment's one transport fact:
`disabled` means direct point-to-point gRPC, anything else means a broker holds
records between stages. dfe-common.transport turns it into the bus/direct
vocabulary the source model and the docs use, so a chart never compares that
string itself -- the duplication this replaced was the same `if eq .Values
.kafka.mode "disabled"` branch in three charts' templates. dfe-extras reads it
through that helper for the engine's env, and refuses a mode its profile was not
built for.

The thin charts take what depends on the transport from each app's profile file
under argocd/values/apps; the renders are the goldens under fixtures/thin-renders,
which test_thin_renders.py holds equal to what Argo deploys. The dials render live
through scripts/dfe-weave and the scalo-service library (_weave.library).

    python3 scripts/tests/test_transport.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

import subprocess
import sys
from pathlib import Path

import yaml

from _charts import CHART_TREES
from _expect import expect, standalone, summary
from _gate import CLOUDS, effective_env, golden, object_id
from _weave import render_app, render_extras, weave

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
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
# Service opens the scalo Push port there and nowhere else.
PUSH_STAGES = [
    "dfe-archiver",
    "dfe-loader",
    "dfe-transform-vrl",
    "dfe-transform-vector",
]

# A stage apps.yaml runs on the bus alone, which binds no push listener on any tier.
BUS_ONLY = "dfe-transform-elastic"

# Every stage's Service carries its metrics listener, which dfe-engine reads the
# stage's metric manifest from.
METRICS = ("metrics", 9090)
PUSH = ("push", 6000)

ENGINE = "dfe-engine"

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


def engine_env(docs_: list[dict]) -> dict[str, object]:
    """The engine's effective env: its thin chart's entries over dfe-engine-env."""
    if any(object_id(d) == f"Deployment/{ENGINE}" for d in docs_):
        return effective_env(docs_, f"Deployment/{ENGINE}")
    return next(d for d in docs_ if object_id(d) == "ConfigMap/dfe-engine-env")["data"]


def test_a_chart_with_no_kafka_values_still_renders() -> None:
    """The engine names no broker mode of its own; a mode left unset is the bus.

    A brokerless profile is built for direct, so dfe-extras refuses an unset mode
    there rather than handing the engine the bus.
    """
    env = engine_env(render_extras(ENGINE, "single", "local", overlay={"kafka": {"mode": None}}))
    expect("the engine is told the transport", env.get("DFE_TRANSPORT_DEFAULT") == "bus",
           f"got {env.get('DFE_TRANSPORT_DEFAULT')!r}")
    expect("and whether a bus is present", env.get("DFE_TRANSPORT_BUS_PRESENT") == "true",
           f"got {env.get('DFE_TRANSPORT_BUS_PRESENT')!r}")
    try:
        render_extras(ENGINE, "slim", "local", overlay={"kafka": {"mode": None}})
    except weave().WeaveError as refused:
        expect("a brokerless profile refuses the bus", "built for direct" in str(refused),
               str(refused)[-300:])
    else:
        expect("a brokerless profile refuses the bus", False, "slim rendered with no mode")


def test_the_engine_follows_the_profile() -> None:
    for profile, want in sorted(PROFILE_TRANSPORT.items()):
        for cloud in CLOUDS:
            env = engine_env(golden(ENGINE, profile, cloud))
            at = f"{profile}/{cloud}"
            expect(f"{at} tells the engine {want}", env.get("DFE_TRANSPORT_DEFAULT") == want,
                   f"got {env.get('DFE_TRANSPORT_DEFAULT')!r}")
            expect(f"{at} bus_present agrees",
                   env.get("DFE_TRANSPORT_BUS_PRESENT") == str(want == "bus").lower(),
                   f"got {env.get('DFE_TRANSPORT_BUS_PRESENT')!r}")


def service_ports(docs_: list[dict], stage: str) -> list[tuple[str, int]]:
    """The stage's own Service, as its (name, port) pairs."""
    service = next(d for d in docs_ if object_id(d) == f"Service/{stage}")
    return [(p["name"], p["port"]) for p in service["spec"]["ports"]]


def push_anywhere(docs_: list[dict]) -> list[str]:
    """Every Service of the render that serves a port named push."""
    return [
        object_id(d)
        for d in docs_
        if d.get("kind") == "Service" and any(p["name"] == "push" for p in d["spec"]["ports"])
    ]


def test_the_push_service_renders_on_direct_only() -> None:
    for stage in PUSH_STAGES:
        for profile, transport in sorted(PROFILE_TRANSPORT.items()):
            for cloud in CLOUDS:
                docs_ = golden(stage, profile, cloud)
                at = f"{stage} on {profile}/{cloud}"
                if transport == "bus":
                    expect(f"{at} serves no push", push_anywhere(docs_) == [],
                           f"got {push_anywhere(docs_)}")
                    expect(f"{at} keeps its metrics Service", service_ports(docs_, stage)
                           == [METRICS], f"got {service_ports(docs_, stage)!r}")
                else:
                    expect(f"{at} serves push on 6000 beside its metrics",
                           service_ports(docs_, stage) == [METRICS, PUSH],
                           f"got {service_ports(docs_, stage)!r}")
    for profile in PROFILE_TRANSPORT:
        for cloud in CLOUDS:
            docs_ = golden(BUS_ONLY, profile, cloud)
            expect(f"{BUS_ONLY} on {profile}/{cloud} binds no push listener",
                   push_anywhere(docs_) == [], f"got {push_anywhere(docs_)}")


def transform_config(docs_: list[dict], stage: str) -> dict:
    doc = next(d for d in docs_ if object_id(d) == f"ConfigMap/{stage}-config")
    return yaml.safe_load(doc["data"]["config.yaml"]) or {}


def test_the_transform_config_names_the_transport() -> None:
    """The Service is not enough: the app has to be TOLD which transport it is on.

    Both transform apps default to the bus, so a brokerless deploy whose
    ConfigMap said nothing would start a Kafka consumer against localhost and
    report healthy while carrying no records.

    Asserting on both is the point of the assertion: the engine has ONE
    `transform` routing compiler, so the four keys have to mean the same thing
    in both deployments. On direct the sink dials the loader's push port in the
    release namespace, or the loader pool's mesh alias on the mesh tier.
    """
    common = yaml.safe_load((VALUES / "common.yaml").read_text(encoding="utf-8"))
    gateway = common["mesh"]["gateway"]["namespace"]
    loader = {
        "slim": "http://dfe-loader:6000",
        "mesh": f"http://dfe-loader-mesh.{gateway}.svc.cluster.local:6000",
    }
    for stage in ("dfe-transform-vrl", "dfe-transform-vector"):
        for profile, transport in sorted(PROFILE_TRANSPORT.items()):
            for cloud in CLOUDS:
                config = transform_config(golden(stage, profile, cloud), stage)
                at = f"{stage} on {profile}/{cloud}"
                source, sink = config.get("source") or {}, config.get("sink") or {}
                expect(f"{at} says {transport}", source.get("transport") == transport,
                       f"got {source!r}")
                expect(f"{at}: the sink says {transport}", sink.get("transport") == transport,
                       f"got {sink!r}")
                if transport == "bus":
                    continue
                expect(f"{at} binds the push port", source.get("listen") == "0.0.0.0:6000",
                       f"got {source!r}")
                expect(f"{at} dials the loader", sink.get("endpoint") == loader[profile],
                       f"got {sink!r}")


def test_the_fetcher_dlq_follows_the_instance_output() -> None:
    """The engine compiles a direct source's fetcher to output.type grpc on a bus
    deploy too, and the fetcher refuses a kafka-only DLQ with no kafka output.

    The fetcher reads an empty value as unset (scalo flat env), so a name the
    deployment leaves empty counts as not rendered.
    """

    def env(docs_: list[dict]) -> dict[str, object]:
        return effective_env(docs_, "Deployment/dfe-fetcher")

    for cloud in CLOUDS:
        bus = env(golden("dfe-fetcher", "single", cloud))
        expect(f"a bus instance dead-letters to kafka [{cloud}]",
               bus.get("DFE_FETCHER_DLQ_MODE") == "kafka_only",
               f"got {bus.get('DFE_FETCHER_DLQ_MODE')!r}")
        expect(f"and is not switched off [{cloud}]",
               bus.get("DFE_FETCHER_DLQ_ENABLED") in (None, ""),
               repr(bus.get("DFE_FETCHER_DLQ_ENABLED")))

    overlay = {"config": {"output": {"type": "grpc"}}}
    direct_on_bus = env(render_app("dfe-fetcher", "single", "local", overlay=overlay))
    expect("a direct instance on a bus deploy switches the DLQ off",
           direct_on_bus.get("DFE_FETCHER_DLQ_ENABLED") == "false",
           f"got {direct_on_bus.get('DFE_FETCHER_DLQ_ENABLED')!r}")
    expect("and names no kafka mode", direct_on_bus.get("DFE_FETCHER_DLQ_MODE") in (None, ""),
           repr(direct_on_bus.get("DFE_FETCHER_DLQ_MODE")))

    for profile in ("slim", "mesh"):
        for cloud in CLOUDS:
            direct = env(golden("dfe-fetcher", profile, cloud))
            expect(f"a direct deploy switches the DLQ off [{profile}/{cloud}]",
                   direct.get("DFE_FETCHER_DLQ_ENABLED") == "false",
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
