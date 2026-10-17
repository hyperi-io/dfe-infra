#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_diagnostics.py
#  Purpose:      Guard `dfe-ops diagnostics` and the two places that run it: the
#                captures it makes, that a capture which raises or hangs never
#                stops the rest, the cycle's destroy or the source runner's
#                teardown, that the bundle keeps to its time box, that every file
#                is redacted, that no Secret is read, and that nothing is
#                captured without the flag.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe_ops_diagnostics.py, `dfe-ops cycle --diagnostics-dir` and the runner hook.

    python3 -m pytest scripts/tests/test_dfe_ops_diagnostics.py -q

Most tests swap the failure-evidence bounded runner for a fake that answers each
kubectl call from a fixture and records what was asked. The CLI tests put a fake
`kubectl` first on PATH, and the time-box tests run real child processes. No
cluster is involved.
"""

import argparse
import importlib.machinery
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import dfe_ops_cloud_guard as guard  # noqa: E402
import dfe_ops_diagnostics as dg  # noqa: E402
import dfe_ops_failure_evidence as fe  # noqa: E402
from acceptance.onboarding import wizard  # noqa: E402
from acceptance.source import run as source_run  # noqa: E402

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_diagnostics", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_diagnostics", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_diagnostics"] = dfeops
_loader.exec_module(dfeops)

# Credential values planted in every answer. None may reach a file.
PLANTED = (
    "hunter2-not-evidence",
    "abc.def.ghi123456",
    "AKIAABCDEFGHIJKLMNOP",
    "tok-not-evidence",
    "pass-not-evidence",
    "jaas-not-evidence",
)
IMAGE = "apache/kafka:4.3.1@sha256:" + "0" * 64


def _pod(
    namespace: str,
    workload: str,
    suffix: str,
    containers: tuple[str, ...],
    *,
    restarts: int = 0,
    init: tuple[str, ...] = (),
    owner_kind: str = "ReplicaSet",
) -> dict:
    """A pod as the API returns it, owned by `<workload>-<hash>` the way a Deployment's are."""
    labels = {"app.kubernetes.io/name": workload}
    owner = workload
    if owner_kind == "ReplicaSet":
        labels["pod-template-hash"] = "5d8f9c7b6"
        owner = f"{workload}-5d8f9c7b6"
    statuses = [{"name": name, "ready": True, "restartCount": restarts} for name in containers]
    return {
        "metadata": {
            "name": f"{workload}-{suffix}",
            "namespace": namespace,
            "labels": labels,
            "ownerReferences": [{"kind": owner_kind, "name": owner, "controller": True}],
        },
        "spec": {
            "nodeName": "node-a",
            "initContainers": [{"name": name} for name in init],
            "containers": [
                {"name": name, "env": [{"name": "DFE_SECRET", "value": PLANTED[3]}]}
                for name in containers
            ],
        },
        "status": {"phase": "Running", "containerStatuses": statuses},
    }


SOURCE = "fb0123abcd"
PER_SOURCE = f"dfe-transform-vrl-{SOURCE}"
PODS = [
    _pod("dfe", "dfe-receiver", "a1", ("dfe-receiver",)),
    _pod("dfe", "dfe-loader", "b2", ("dfe-loader",), restarts=2),
    _pod("dfe", "dfe-engine", "c3", ("dfe-engine", "git-sync"), init=("git-sync-init",)),
    _pod("dfe", "dfe-hunt-runner", "d4", ("hunt-runner",)),
    _pod("dfe", "dfe-transform-vrl", "e5", ("dfe-transform-vrl",)),
    _pod("dfe", PER_SOURCE, "f6", ("dfe-transform-vrl",), restarts=5),
    _pod("dfe", "dfe-hyperdx", "g7", ("hyperdx",)),
    _pod("dfe", "dfe-keda-shim", "h8", ("shim",)),
    _pod("strimzi", "dfe-kafka-broker", "0", ("kafka",), owner_kind="StatefulSet"),
]
LOGGED = [
    ("dfe", "dfe-engine-c3", "git-sync-init"),
    ("dfe", "dfe-engine-c3", "dfe-engine"),
    ("dfe", "dfe-engine-c3", "git-sync"),
    ("dfe", "dfe-hunt-runner-d4", "hunt-runner"),
    ("dfe", "dfe-loader-b2", "dfe-loader"),
    ("dfe", "dfe-receiver-a1", "dfe-receiver"),
    ("dfe", "dfe-transform-vrl-e5", "dfe-transform-vrl"),
    ("dfe", f"{PER_SOURCE}-f6", "dfe-transform-vrl"),
]


def _application(name: str, appset: str, destination: str, *, healthy: bool = True) -> dict:
    status: dict = {
        "sync": {"status": "Synced" if healthy else "OutOfSync", "revision": "abc1234"},
        "health": {"status": "Healthy"} if healthy else {
            "status": "Degraded",
            "message": f"https://user:{PLANTED[4]}@git.example/deploy is unreachable",
        },
        "operationState": {
            "phase": "Succeeded" if healthy else "Failed",
            "message": "successfully synced" if healthy else "one or more objects failed to apply",
            "startedAt": "2026-10-17T01:00:00Z",
            "finishedAt": "2026-10-17T01:00:09Z",
        },
        "resources": [
            {"kind": "Deployment", "namespace": destination, "name": name, "status": "Synced",
             "health": {"status": "Healthy" if healthy else "Degraded",
                        "message": "" if healthy else "Deployment exceeded its progress deadline"}},
        ],
    }
    if not healthy:
        status["conditions"] = [{"type": "SyncError", "message": "the apply failed",
                                 "lastTransitionTime": "2026-10-17T01:00:09Z"}]
    return {
        "metadata": {
            "name": name,
            "namespace": "argocd",
            "ownerReferences": [{"kind": "ApplicationSet", "name": appset}],
        },
        "spec": {"project": "dfe-apps", "destination": {"namespace": destination}},
        "status": status,
    }


APPS = [
    _application("dfe-receiver-default-values", "dfe-layer2-apps", "dfe"),
    _application(f"{PER_SOURCE}-values", "dfe-layer2-apps", "dfe", healthy=False),
    _application("clickhouse", "dfe-layer2-data", "clickhouse"),
]
# As the chart renders it: the SASL Secret in env, the bus address in the ConfigMap envFrom loads.
ENGINE_FACTS = "bootstrap=\nprotocol=\nsecret=dfe-kafka-user\nconfigmaps=dfe-engine-env\n"
CONFIGMAP_FACTS = "bootstrap=pkc-example.confluent.cloud:9092\nprotocol=SASL_SSL\n"
GROUPS = f"""
GROUP       TOPIC      PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG  CONSUMER-ID  HOST  CLIENT-ID
dfe-loader  main_land  0          40              42              2    rdkafka-1    /h1   dfe-loader
dfe-loader  main_land  1          38              38              0    rdkafka-1    /h1   dfe-loader

Consumer group '{PER_SOURCE}' has no active members.

GROUP  TOPIC  PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG  CONSUMER-ID  HOST  CLIENT-ID
{PER_SOURCE}  {SOURCE}_land  0  -  45  -  -  -  -
sasl.jaas.config=ScramLoginModule required username="dfe" password="{PLANTED[5]}";
"""
OFFSETS = f"""{SOURCE}_land:0:45
{SOURCE}_load:0:0
main_land:0:42
main_land:1:38
dfe_loader_dlq:0:0
dfe_receiver_dlq:0:3
dfe_transform_dlq:0:0
"""
LOG_TEXT = (
    "2026-10-17T01:00:00Z INFO consumer started\n"
    f"2026-10-17T01:00:01Z ERROR SASL authentication failed password={PLANTED[0]}\n"
    f"2026-10-17T01:00:02Z DEBUG Authorization: Bearer {PLANTED[1]}\n"
    f"2026-10-17T01:00:03Z DEBUG key {PLANTED[2]}\n"
)
EVENTS_TEXT = (
    f"LAST SEEN  TYPE  REASON  OBJECT  MESSAGE\n1m  Warning  Failed  pod/x  token={PLANTED[3]}\n"
)


def _items(items: list[dict]) -> fe.Outcome:
    return fe.Outcome(output=json.dumps({"items": items}).encode(), returncode=0)


def _text(text: str) -> fe.Outcome:
    return fe.Outcome(output=text.encode(), returncode=0)


def _answer(args: list[str]) -> fe.Outcome:
    joined = " ".join(args)
    if joined == "get pods -A -o json":
        return _items(PODS)
    if joined == "get applications.argoproj.io -A -o json":
        return _items(APPS)
    if joined.startswith(f"-n dfe get deploy {dg.ENGINE} -o jsonpath="):
        return _text(ENGINE_FACTS)
    if joined.startswith("-n dfe get configmap dfe-engine-env -o jsonpath="):
        return _text(CONFIGMAP_FACTS)
    if "kafka-consumer-groups.sh" in joined:
        return _text(GROUPS)
    if "kafka-get-offsets.sh" in joined:
        return _text(OFFSETS)
    if " logs " in f" {joined} ":
        return _text(LOG_TEXT)
    if " events " in f" {joined} ":
        return _text(EVENTS_TEXT)
    return _text(f"output of {joined}\n")


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    answer=_answer,
    budget: float = 60.0,
    minutes: int = dg.LOG_MINUTES,
    image: str = IMAGE,
) -> tuple[list[list[str]], Path]:
    """collect() against a fake runner: every kubectl argv after `kubectl`, and the bundle."""
    calls: list[list[str]] = []

    def fake(argv: list[str], *, timeout: float, limit: int, tail: bool = False) -> fe.Outcome:
        assert argv[0] == "kubectl"
        assert 0 < timeout <= budget
        calls.append(argv[1:])
        return answer(argv[1:])

    monkeypatch.setattr(fe, "run_bounded", fake)
    out = tmp_path / "bundle"
    out.mkdir()
    (out / fe.INDEX).write_text("", encoding="utf-8")
    start = time.monotonic()
    bundle = dg.Bundle(out, [], budget, start + budget - 5.0, start + budget)
    dg.collect(bundle, minutes=minutes, image=image)
    return calls, out


def _index(out: Path) -> str:
    return (out / fe.INDEX).read_text(encoding="utf-8")


# --- what it chooses to read -----------------------------------------------------


def test_a_pods_deployment_is_its_replica_set_less_the_template_hash() -> None:
    assert dg.owning_workload(PODS[5]) == PER_SOURCE
    assert dg.owning_workload(PODS[8]) == "dfe-kafka-broker"
    assert dg.owning_workload({"metadata": {"name": "bare"}}) == ""


def test_the_logged_apps_are_receiver_loader_engine_hunt_runner_and_every_transform() -> None:
    chosen = [
        (pod["metadata"]["namespace"], pod["metadata"]["name"]) for pod in dg.log_targets(PODS)
    ]
    assert ("dfe", f"{PER_SOURCE}-f6") in chosen
    assert {name for _, name in chosen} == {name for _, name, _ in LOGGED}
    assert not any("hyperdx" in name or "keda" in name or "kafka" in name for _, name in chosen)


def test_the_engine_namespace_is_where_its_pod_runs() -> None:
    assert dg.engine_namespace(PODS) == "dfe"
    assert dg.engine_namespace(PODS[:2]) == ""


def test_dfe_namespaces_are_argo_destinations_logged_namespaces_and_argocd() -> None:
    assert dg.dfe_namespaces(APPS, dg.log_targets(PODS)) == ["argocd", "clickhouse", "dfe"]
    assert dg.dfe_namespaces([], []) == ["argocd"]


def test_the_application_lines_name_the_appset_sync_health_and_what_is_degraded() -> None:
    lines = dg.application_lines(APPS)
    degraded = next(line for line in lines if line.startswith(f"application {PER_SOURCE}-values"))
    assert "appset dfe-layer2-apps" in degraded
    assert "sync OutOfSync" in degraded
    assert "health Degraded" in degraded
    assert "  last operation Failed" in "\n".join(lines)
    assert any("resource Deployment dfe/" in line and "progress deadline" in line for line in lines)
    assert any(line.startswith("  condition SyncError") for line in lines)
    # A healthy object adds nothing past its own line and its last operation.
    assert not [line for line in lines if "resource Deployment dfe/dfe-receiver" in line]


# --- the captures it makes -------------------------------------------------------


def test_every_capture_the_brief_names_is_issued_and_lands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, out = _run(monkeypatch, tmp_path)
    joined = [" ".join(call) for call in calls]
    assert "get pods,deploy,rs -A -o wide" in joined
    for namespace in ("argocd", "clickhouse", "dfe"):
        assert f"-n {namespace} get events {fe.OLDEST_FIRST} -o wide" in joined
    for namespace, pod, container in LOGGED:
        assert (
            f"-n {namespace} logs {pod} -c {container} --since={dg.LOG_MINUTES}m --timestamps"
        ) in joined
        assert (out / "logs" / namespace / f"{pod}.{container}.log").is_file()
    previous = [call for call in joined if "--previous" in call]
    assert len(previous) == 2
    assert any("dfe-loader-b2 -c dfe-loader --previous" in call for call in previous)
    assert any(f"{PER_SOURCE}-f6 -c dfe-transform-vrl --previous" in call for call in previous)
    for name in (
        "workloads.txt",
        "argo-applications.txt",
        "events/dfe.txt",
        "kafka/engine.txt",
        "kafka/configmap-dfe-engine-env.txt",
        dg.CLIENT_MANIFEST,
        "kafka/consumer-groups.txt",
        "kafka/end-offsets.txt",
        "kafka/summary.txt",
    ):
        assert (out / name).is_file(), name


def test_the_log_window_is_the_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls, _out = _run(monkeypatch, tmp_path, minutes=7)
    logs = [call for call in calls if "logs" in call and "--previous" not in call]
    assert logs
    assert all("--since=7m" in call for call in logs)


def test_the_kafka_reads_run_in_order_and_the_pod_is_deleted_last(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, _out = _run(monkeypatch, tmp_path)
    verbs = [
        next((word for word in ("apply", "wait", "exec", "delete") if word in call), "")
        for call in calls
    ]
    assert [verb for verb in verbs if verb] == ["apply", "wait", "exec", "exec", "delete"]
    assert verbs[-1] == "delete"
    delete = calls[-1]
    assert delete[:3] == ["-n", "dfe", "delete"]
    assert "--wait=false" in delete


# --- fail soft --------------------------------------------------------------------


def test_a_capture_that_raises_is_recorded_and_the_rest_still_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def answer(args: list[str]) -> fe.Outcome:
        if "applications.argoproj.io" in args:
            raise RuntimeError("the runner broke")
        return _answer(args)

    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert "raised RuntimeError: the runner broke" in _index(out)
    assert (out / "logs" / "dfe" / "dfe-receiver-a1.dfe-receiver.log").is_file()
    assert "delete" in calls[-1]


def test_a_capture_that_times_out_is_recorded_and_the_rest_still_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def answer(args: list[str]) -> fe.Outcome:
        if "events" in args:
            return fe.Outcome(returncode=-9, timed_out=True)
        return _answer(args)

    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert "events/dfe.txt: timed out" in _index(out)
    assert (out / "kafka" / "summary.txt").is_file()
    assert "delete" in calls[-1]


def test_the_client_pod_is_deleted_even_when_its_exec_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def answer(args: list[str]) -> fe.Outcome:
        if "exec" in args:
            raise OSError("exec broke")
        return _answer(args)

    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert "delete" in calls[-1]
    assert "raised OSError: exec broke" in _index(out)


def test_a_client_pod_that_never_becomes_ready_is_named_and_still_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def answer(args: list[str]) -> fe.Outcome:
        if "wait" in args:
            return fe.Outcome(returncode=1, error="timed out waiting for the condition")
        return _answer(args)

    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert not any("exec" in call for call in calls)
    assert "delete" in calls[-1]
    assert "the client pod never became Ready" in _index(out)


def test_a_cluster_that_answers_nothing_stops_after_two_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, out = _run(
        monkeypatch, tmp_path, answer=lambda _args: fe.Outcome(returncode=1, error="refused")
    )
    assert len(calls) == 2
    assert "not run: the API server answered neither read above" in _index(out)


def test_the_command_exits_zero_when_the_whole_collect_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    def broken(_bundle: dg.Bundle, *, minutes: int, image: str) -> None:
        raise ValueError("collect broke")

    monkeypatch.setattr(dg, "collect", broken)
    assert dg.cmd_diagnostics(_diagnostics_args(tmp_path / "diag", "smoke")) == 0
    (bundle,) = (tmp_path / "diag").iterdir()
    assert bundle.name.endswith("-smoke")
    assert "raised ValueError: collect broke" in _index(bundle)
    assert "file(s) under" in capsys.readouterr().out


def test_an_unwritable_dir_is_a_named_skip(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    assert dg.cmd_diagnostics(_diagnostics_args(blocker / "sub", "smoke")) == 0
    assert "cannot be written" in capsys.readouterr().err


def _diagnostics_args(root: Path, label: str, *, budget: float = 30.0) -> argparse.Namespace:
    return argparse.Namespace(
        dir=str(root), label=label, kubeconfig="", context="", server="",
        log_minutes=dg.LOG_MINUTES, budget=budget,
    )


# --- the time box -----------------------------------------------------------------


def test_the_bundle_stops_at_its_budget_and_the_pod_delete_still_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    budget = 3.0
    calls: list[list[str]] = []

    def slow(argv: list[str], *, timeout: float, limit: int, tail: bool = False) -> fe.Outcome:
        calls.append(argv[1:])
        time.sleep(min(timeout, 0.4))
        return _answer(argv[1:])

    monkeypatch.setattr(fe, "run_bounded", slow)
    monkeypatch.setenv(dg.KAFKA_CLI_IMAGE_ENV, IMAGE)
    started = time.monotonic()
    assert dg.cmd_diagnostics(_diagnostics_args(tmp_path / "diag", "smoke", budget=budget)) == 0
    elapsed = time.monotonic() - started
    assert elapsed < budget + 1.0
    (bundle,) = (tmp_path / "diag").iterdir()
    assert f"not run: the {budget:.0f}s budget was spent" in _index(bundle)
    assert "delete" in calls[-1]


def test_no_call_is_given_more_than_the_time_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    budget = 10.0
    start = time.monotonic()
    seen: list[tuple[float, float]] = []

    def fake(argv: list[str], *, timeout: float, limit: int, tail: bool = False) -> fe.Outcome:
        seen.append((timeout, start + budget - time.monotonic()))
        return _answer(argv[1:])

    monkeypatch.setattr(fe, "run_bounded", fake)
    out = tmp_path / "bundle"
    out.mkdir()
    bundle = dg.Bundle(out, [], budget, start + budget - 2.5, start + budget)
    dg.collect(bundle, minutes=30, image=IMAGE)
    assert seen
    assert all(timeout <= left + 0.05 for timeout, left in seen)


def test_the_box_stops_a_capture_that_hangs() -> None:
    started = time.monotonic()
    hang = [sys.executable, "-c", "import time; time.sleep(30)"]
    assert dfeops._run_boxed(hang, env=None, timeout=0.5) is None
    assert time.monotonic() - started < 5.0


def test_the_box_names_a_capture_that_cannot_start(capsys: pytest.CaptureFixture) -> None:
    assert dfeops._run_boxed(["/nonexistent/dfe-ops"], env=None, timeout=5.0) is None
    assert "could not start" in capsys.readouterr().err


# --- redaction and what is never read ---------------------------------------------


def test_every_file_in_the_bundle_is_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _calls, out = _run(monkeypatch, tmp_path)
    files = [path for path in out.rglob("*") if path.is_file()]
    assert len(files) > 10
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for planted in PLANTED:
            assert planted not in text, (path, planted)


def test_no_call_reads_a_secret_or_a_workloads_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, _out = _run(monkeypatch, tmp_path)
    for call in calls:
        assert "describe" not in call, call
        assert not {"secret", "secrets"} & set(call), call
        if {"deploy", "configmap"} & set(call):
            # The engine and its ConfigMaps are read through jsonpaths naming entries, never whole.
            assert not ({"json", "yaml"} & set(call)), call
    facts = next(call for call in calls if dg.ENGINE in call and "deploy" in call)
    template = facts[-1]
    assert template.count('@.name=="') == 3
    assert "DFE_KAFKA_SASL_PASSWORD" not in template
    configmap = next(call for call in calls if "configmap" in call)
    assert configmap[-1].count(".data.") == 2


# --- the Kafka reads ---------------------------------------------------------------


def test_the_engine_facts_are_bootstrap_protocol_and_secret_name() -> None:
    facts = dg.kafka_facts("dfe", ENGINE_FACTS, [CONFIGMAP_FACTS])
    assert facts == dg.KafkaFacts(
        "dfe", "pkc-example.confluent.cloud:9092", "SASL_SSL", "dfe-kafka-user"
    )
    assert dg.kafka_facts("dfe", "bootstrap=\nprotocol=\nsecret=\n") is None
    assert dg.kafka_facts("dfe", ENGINE_FACTS) is None
    plain = dg.kafka_facts("dfe", "bootstrap=dfe-kafka.strimzi.svc:9092\nprotocol=\nsecret=\n")
    assert plain is not None
    assert (plain.protocol, plain.secret) == ("PLAINTEXT", "")
    sasl = dg.kafka_facts("dfe", "bootstrap=b:9092\nprotocol=\nsecret=dfe-kafka-user\n")
    assert sasl is not None
    assert sasl.protocol == "SASL_PLAINTEXT"


def test_inline_env_outranks_the_configmaps_and_a_later_configmap_an_earlier_one() -> None:
    """Kubernetes' own order for env and envFrom."""
    early, late = "bootstrap=early:9092\nprotocol=SASL_SSL\n", "bootstrap=late:9092\n"
    facts = dg.kafka_facts("dfe", "secret=s\nconfigmaps=a b\n", [early, late])
    assert facts is not None
    assert (facts.bootstrap, facts.protocol) == ("late:9092", "SASL_SSL")
    inline = dg.kafka_facts("dfe", "bootstrap=inline:9092\nsecret=s\n", [early, late])
    assert inline is not None
    assert inline.bootstrap == "inline:9092"


def test_the_bootstrap_ports_are_each_distinct_port_once() -> None:
    assert dg.bootstrap_ports("a:9092,b:9092,c:9096") == [9092, 9096]
    assert dg.bootstrap_ports("[::1]:9093") == [9093]
    assert dg.bootstrap_ports("no-port,also:none") == []


def test_the_client_pod_mounts_the_engines_secret_and_carries_no_credential() -> None:
    facts = dg.KafkaFacts("dfe", "pkc-example.confluent.cloud:9092", "SASL_SSL", "dfe-kafka-user")
    manifest = dg.client_manifest("dfe-diagnostics-kafka-1", "1", facts, IMAGE, [9092])
    pod, policy = manifest["items"]
    spec = pod["spec"]
    assert spec["activeDeadlineSeconds"] == dg.CLIENT_LIFETIME
    assert spec["automountServiceAccountToken"] is False
    (container,) = spec["containers"]
    assert container["image"] == IMAGE
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    credential = next(v for v in spec["volumes"] if v["name"] == "credential")
    assert credential["secret"]["secretName"] == "dfe-kafka-user"
    mount = next(m for m in container["volumeMounts"] if m["name"] == "credential")
    assert (mount["mountPath"], mount["readOnly"]) == (dg.CREDENTIAL_MOUNT, True)
    assert policy["spec"]["egress"] == [{"ports": [{"port": 9092, "protocol": "TCP"}]}]
    assert policy["spec"]["podSelector"]["matchLabels"] == {dg.RUN_LABEL: "1"}
    assert all(item["metadata"]["labels"][dg.RUN_LABEL] == "1" for item in manifest["items"])
    # The Secret is named, never read, so no credential field can be in the manifest.
    assert "password" not in json.dumps(manifest)


def test_the_client_script_reads_the_credential_from_the_mounted_files() -> None:
    facts = dg.KafkaFacts("dfe", "pkc-example.confluent.cloud:9092", "SASL_SSL", "dfe-kafka-user")
    script = dg.client_script(facts, dg.GROUPS_COMMAND)
    assert f"{dg.CREDENTIAL_MOUNT}/password" in script
    assert "security.protocol=SASL_SSL" in script
    assert "--bootstrap-server pkc-example.confluent.cloud:9092" in script
    assert "--describe --all-groups" in script
    assert 'rm -f "$P"' in script


def test_a_plaintext_bus_writes_no_sasl_lines_and_mounts_nothing() -> None:
    facts = dg.KafkaFacts("dfe", "dfe-kafka.strimzi.svc:9092", "PLAINTEXT", "")
    script = dg.client_script(facts, dg.OFFSETS_COMMAND)
    assert "sasl" not in script
    assert dg.CREDENTIAL_MOUNT not in script
    manifest = dg.client_manifest("p", "1", facts, IMAGE, [9092])
    assert [v["name"] for v in manifest["items"][0]["spec"]["volumes"]] == ["tmp"]


def test_a_value_from_the_cluster_is_quoted_into_the_script() -> None:
    facts = dg.KafkaFacts("dfe", "b:9092; touch /tmp/x", "SASL_SSL", "s")
    script = dg.client_script(facts, dg.GROUPS_COMMAND)
    assert "'b:9092; touch /tmp/x'" in script


def test_a_brokerless_deployment_skips_the_kafka_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def answer(args: list[str]) -> fe.Outcome:
        if "configmap" in args:
            return _text("bootstrap=\nprotocol=\n")
        return _answer(args)

    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert not any({"apply", "exec", "delete"} & set(call) for call in calls)
    assert "dfe-engine dials no Kafka bus" in _index(out)


def test_no_engine_pod_skips_the_kafka_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def answer(args: list[str]) -> fe.Outcome:
        if " ".join(args) == "get pods -A -o json":
            return _items([pod for pod in PODS if dg.owning_workload(pod) != dg.ENGINE])
        return _answer(args)

    calls, out = _run(monkeypatch, tmp_path, answer=answer)
    assert not any("apply" in call for call in calls)
    assert "no dfe-engine pod" in _index(out)


def test_no_client_image_skips_the_kafka_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls, out = _run(monkeypatch, tmp_path, image="")
    assert not any("apply" in call for call in calls)
    assert dg.KAFKA_CLI_IMAGE_ENV in _index(out)


def _plaintext(args: list[str]) -> fe.Outcome:
    """A bus with no SASL: no Secret on the engine, PLAINTEXT on its ConfigMap."""
    if dg.ENGINE in args and "deploy" in args:
        return _text("bootstrap=\nprotocol=\nsecret=\nconfigmaps=dfe-engine-env\n")
    if "configmap" in args:
        return _text("bootstrap=dfe-kafka.strimzi.svc:9092\nprotocol=PLAINTEXT\n")
    return _answer(args)


def _applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answer=_answer
) -> tuple[list[tuple[Path, bytes]], Path]:
    """collect() with the real redactor, recording each file `apply -f` was handed as it ran."""
    handed: list[tuple[Path, bytes]] = []

    def reading(args: list[str]) -> fe.Outcome:
        if "apply" in args:
            path = Path(args[args.index("-f") + 1])
            handed.append((path, path.read_bytes()))
        return answer(args)

    _calls, out = _run(monkeypatch, tmp_path, answer=reading)
    return handed, out


@pytest.mark.parametrize("answer", [_answer, _plaintext], ids=["sasl", "plaintext"])
def test_the_manifest_kubectl_applies_is_the_unredacted_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answer
) -> None:
    """Redaction masks `automountServiceAccountToken` and `"secret": {`, so it never sees this."""
    handed, out = _applied(monkeypatch, tmp_path, answer)
    ((path, sent),) = handed
    assert not path.is_relative_to(out)
    pod, _policy = json.loads(sent)["items"]
    assert pod["spec"]["automountServiceAccountToken"] is False
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    if answer is _answer:
        assert volumes["credential"]["secret"]["secretName"] == "dfe-kafka-user"
    else:
        assert "credential" not in volumes


def test_the_bundle_keeps_a_redacted_copy_of_the_applied_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handed, out = _applied(monkeypatch, tmp_path)
    ((_path, sent),) = handed
    kept = (out / dg.CLIENT_MANIFEST).read_bytes()
    assert kept == fe.redact_bytes(sent)
    assert fe.MASK.encode() in kept


@pytest.mark.parametrize("failing", ["", "exec"])
def test_the_applied_manifest_is_removed_after_the_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failing: str
) -> None:
    def answer(args: list[str]) -> fe.Outcome:
        if failing and failing in args:
            raise OSError(f"{failing} broke")
        return _answer(args)

    handed, _out = _applied(monkeypatch, tmp_path, answer)
    ((path, _sent),) = handed
    assert not path.exists()


def test_the_summary_names_each_topics_end_offset_and_each_groups_commit() -> None:
    lines = dg.kafka_summary_lines(GROUPS, OFFSETS)
    text = "\n".join(lines)
    assert f"{SOURCE}_land end offset 45" in text
    assert f"  group {PER_SOURCE}: no offset committed" in text
    assert "main_land end offset 80" in text
    assert "  group dfe-loader: committed 78, lag 2" in text
    receiver = lines.index("dfe_receiver_dlq end offset 3")
    assert lines[receiver + 1] == "  no consumer group has an offset on it"
    assert f"{SOURCE}_load end offset 0" in text


def test_the_summary_of_unreadable_answers_says_so() -> None:
    assert dg.kafka_summary_lines("", "") == ["neither Kafka read answered with a row"]


def test_the_image_is_the_kafka_charts_pin_unless_overridden() -> None:
    values = 'kafka:\n  mode: single\n  version: "4.3.1"\n  imageDigest: "sha256:abc"\n'
    assert dg.kafka_image({}, values) == "apache/kafka:4.3.1@sha256:abc"
    override = {dg.KAFKA_CLI_IMAGE_ENV: "mirror/kafka@sha256:d"}
    assert dg.kafka_image(override, values) == "mirror/kafka@sha256:d"
    assert dg.kafka_image({}, "kafka:\n  mode: single\n") == ""
    real = (REPO_ROOT / "helm" / "charts" / "kafka" / "values.yaml").read_text(encoding="utf-8")
    assert dg.kafka_image({}, real).startswith("apache/kafka:")


# --- the CLI, end to end through a fake kubectl ------------------------------------

FAKE_KUBECTL = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
fixture = json.load(open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8"))
joined = " ".join(args)
for key, items in fixture["json"].items():
    if joined.endswith(key):
        print(json.dumps({"items": items}))
        sys.exit(0)
if any(arg.startswith("jsonpath=") for arg in args):
    sys.stdout.write(fixture["configmap" if "configmap" in args else "engine"])
    sys.exit(0)
if "logs" in args:
    sys.stdout.write(fixture["log"])
    sys.exit(0)
print("captured: " + joined)
"""


def _run_cli(
    tmp_path: Path, *, kubectl: bool = True
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if kubectl:
        fake = bindir / "kubectl"
        fake.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
        fake.chmod(0o755)
    fixture = {
        "json": {"get pods -A -o json": PODS, "get applications.argoproj.io -A -o json": APPS},
        "engine": ENGINE_FACTS,
        "configmap": CONFIGMAP_FACTS,
        "log": LOG_TEXT,
    }
    fixture_file = tmp_path / "fixture.json"
    fixture_file.write_text(json.dumps(fixture), encoding="utf-8", newline="\n")
    log = tmp_path / "calls.log"
    env = {
        "PATH": f"{bindir}{os.pathsep}{Path(sys.executable).parent}" if kubectl else str(bindir),
        "FAKE_KUBECTL_FIXTURE": str(fixture_file),
        "FAKE_KUBECTL_LOG": str(log),
        dg.KAFKA_CLI_IMAGE_ENV: IMAGE,
    }
    root = tmp_path / "diag"
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "diagnostics", "--dir", str(root), "--label", "smoke",
         "--kubeconfig", "/k/config", "--budget", "60"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, check=False,
        timeout=120,
    )
    return done, root, log


def test_the_cli_writes_a_bundle_and_exits_zero(tmp_path: Path) -> None:
    done, root, log = _run_cli(tmp_path)
    assert done.returncode == 0, done.stderr
    (bundle,) = root.iterdir()
    assert bundle.name.endswith("-smoke")
    assert (bundle / "logs" / "dfe" / f"{PER_SOURCE}-f6.dfe-transform-vrl.log").is_file()
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert all(call[:2] == ["--kubeconfig", "/k/config"] for call in calls)
    assert any("delete" in call for call in calls)
    for path in bundle.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert not [planted for planted in PLANTED if planted in text], path


def test_the_cli_with_no_kubectl_exits_zero_and_says_why(tmp_path: Path) -> None:
    done, root, _log = _run_cli(tmp_path, kubectl=False)
    assert done.returncode == 0, done.stderr
    (bundle,) = root.iterdir()
    assert "could not start kubectl" in _index(bundle)


def test_the_diagnostics_parser_needs_a_dir(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as refused:
        dfeops.build_parser().parse_args(["diagnostics"])
    assert refused.value.code == 2
    assert "--dir" in capsys.readouterr().err


# --- the cycle runs it on a failed stage from deploy+e2e on, before the destroy ----

SKIP_PRE_DEPLOY = ("--skip-capacity-check", "--skip-preflight")
# The flags that add the acceptance and ui stages, so the cycle runs every stage it has.
UI = ("--e2e", "--ui-repo", "/nonexistent/dfe-ui", "--acceptance-suite", "source")


def _cycle(
    monkeypatch: pytest.MonkeyPatch,
    *argv: str,
    fail: str = "",
    boxed=None,
    skip: tuple[str, ...] = SKIP_PRE_DEPLOY,
) -> tuple[int, list[list[str]]]:
    ran: list[list[str]] = []

    def run(cmd: list[str], env: dict | None = None) -> int:
        ran.append(cmd)
        return 1 if cmd[2] == fail else 0

    def box(cmd: list[str], *, env: dict | None, timeout: float) -> int | None:
        ran.append(cmd)
        assert timeout == dfeops.DIAGNOSTICS_BOX
        return 0 if boxed is None else boxed(cmd)

    monkeypatch.setattr(dfeops, "_resolve_stack", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_run_streaming", run)
    monkeypatch.setattr(dfeops, "_run_boxed", box)
    monkeypatch.setenv("DFE_ENGINE_REPO", "/nonexistent/dfe-engine")
    args = dfeops.build_parser().parse_args(
        ["cycle", *skip, "--stack", "2.2.0-rc.99", *argv]
    )
    return dfeops.cmd_cycle(args), ran


def _words(ran: list[list[str]]) -> list[str]:
    return [cmd[2] for cmd in ran]


@pytest.mark.parametrize(
    "stage", ["capacity-check", "preflight", "stack-deploy", "verify", "acceptance", "ui"]
)
def test_without_the_flag_no_failed_stage_captures_diagnostics(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    _rc, ran = _cycle(monkeypatch, *UI, fail=stage, skip=())
    assert stage in _words(ran)
    assert "diagnostics" not in _words(ran)
    acceptance = [cmd for cmd in ran if cmd[2] == "acceptance"]
    assert all("--diagnostics-dir" not in cmd for cmd in acceptance)


def test_a_failed_smoke_captures_diagnostics_before_the_destroy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("", encoding="utf-8")
    root = tmp_path / "diag"
    rc, ran = _cycle(
        monkeypatch, "--kubeconfig", str(kubeconfig), "--diagnostics-dir", str(root), fail="verify"
    )
    assert rc == 1
    assert _words(ran) == ["stack-deploy", "verify", "diagnostics", "failure-evidence", "teardown"]
    capture = ran[2]
    assert capture[capture.index("--dir") + 1] == str(root)
    assert capture[capture.index("--label") + 1] == "smoke"
    assert capture[capture.index("--kubeconfig") + 1] == str(kubeconfig)
    # The subcommand's own parser has to accept every flag the cycle hands it.
    parsed = dfeops.build_parser().parse_args(capture[2:])
    assert (parsed.dir, parsed.label) == (str(root), "smoke")


def test_a_failed_acceptance_captures_diagnostics_and_the_stage_is_handed_the_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "diag"
    rc, ran = _cycle(
        monkeypatch, "--acceptance-suite", "source", "--diagnostics-dir", str(root),
        fail="acceptance",
    )
    assert rc == 1
    assert _words(ran) == [
        "stack-deploy", "verify", "acceptance", "diagnostics", "failure-evidence", "teardown",
    ]
    acceptance = ran[2]
    assert acceptance[acceptance.index("--diagnostics-dir") + 1] == str(root)
    parsed = dfeops.build_parser().parse_args(acceptance[2:])
    assert parsed.diagnostics_dir == str(root)
    assert ran[3][ran[3].index("--label") + 1] == "acceptance"


def test_a_failed_deploy_captures_diagnostics_before_the_destroy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rc, ran = _cycle(monkeypatch, "--diagnostics-dir", str(tmp_path), fail="stack-deploy")
    assert rc == 1
    assert _words(ran) == ["stack-deploy", "diagnostics", "failure-evidence", "teardown"]
    assert ran[1][ran[1].index("--label") + 1] == "deploy+e2e"


def test_a_failed_ui_stage_captures_diagnostics_before_the_destroy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rc, ran = _cycle(monkeypatch, *UI, "--diagnostics-dir", str(tmp_path), fail="ui")
    assert rc == 1
    assert _words(ran) == [
        "stack-deploy", "verify", "acceptance", "ui", "diagnostics", "failure-evidence",
        "teardown",
    ]
    assert ran[4][ran[4].index("--label") + 1] == "ui"


@pytest.mark.parametrize("stage", ["capacity-check", "preflight"])
def test_a_stage_before_the_deploy_captures_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: str
) -> None:
    """Nothing is deployed yet, so there are no app logs to read."""
    rc, ran = _cycle(monkeypatch, "--diagnostics-dir", str(tmp_path), fail=stage, skip=())
    assert rc == 1
    assert _words(ran)[-3:] == [stage, "failure-evidence", "teardown"]
    assert "diagnostics" not in _words(ran)


def test_a_passing_cycle_captures_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rc, ran = _cycle(monkeypatch, "--diagnostics-dir", str(tmp_path))
    assert rc == 0
    assert "diagnostics" not in _words(ran)


@pytest.mark.parametrize("outcome", ["raises", "timed out", "exit 3"])
def test_diagnostics_that_fail_change_neither_the_verdict_nor_the_destroy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str
) -> None:
    def boxed(_cmd: list[str]) -> int | None:
        if outcome == "raises":
            raise RuntimeError("the capture broke")
        return None if outcome == "timed out" else 3

    rc, ran = _cycle(monkeypatch, "--diagnostics-dir", str(tmp_path), fail="verify", boxed=boxed)
    assert rc == 1
    assert _words(ran)[-1] == "teardown"


def test_the_destroy_runs_when_a_stage_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def run(cmd: list[str], env: dict | None = None) -> int:
        ran.append(cmd[2])
        if cmd[2] == "verify":
            raise RuntimeError("the smoke stage broke")
        return 0

    monkeypatch.setattr(dfeops, "_resolve_stack", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_run_streaming", run)
    args = dfeops.build_parser().parse_args(
        ["cycle", "--skip-capacity-check", "--skip-preflight", "--stack", "2.2.0-rc.99"]
    )
    with pytest.raises(RuntimeError, match="the smoke stage broke"):
        dfeops.cmd_cycle(args)
    assert ran == ["stack-deploy", "verify", "teardown"]


def test_cloud_cycle_accepts_the_flag_among_the_cycle_arguments() -> None:
    """A workflow passes it after `--`, where the cycle's own parser judges it."""
    command = guard.cycle_command(
        ["--", "--diagnostics-dir", "/runner/diagnostics"], "single", Path("/tf"), Path("/k")
    )
    assert command[command.index("--diagnostics-dir") + 1] == "/runner/diagnostics"


# --- the acceptance stage hands the source runner its capture --------------------


def test_acceptance_without_the_flag_hands_the_runner_nothing() -> None:
    assert dfeops.diagnostics_runner_args(None, ["kubectl", "--kubeconfig", "/k"]) == []


def test_acceptance_with_the_flag_hands_the_runner_a_capture_that_parses() -> None:
    kube = ["kubectl", "--kubeconfig", "/k", "--context", "c"]
    kube += ["--server", "https://192.0.2.10:6443"]
    extra = dfeops.diagnostics_runner_args("/diag", kube)
    assert extra[0] == "--diagnostics-exec"
    assert extra[2:] == ["--diagnostics-timeout", str(dfeops.DIAGNOSTICS_BOX)]
    prefix = shlex.split(extra[1])
    assert prefix[1] == str(SCRIPT.resolve())
    parsed = dfeops.build_parser().parse_args([*prefix[2:], "--label", "source-fb1"])
    assert (parsed.dir, parsed.kubeconfig, parsed.context, parsed.server) == (
        "/diag", "/k", "c", "https://192.0.2.10:6443",
    )
    runner = source_run.build_parser().parse_args(
        ["--ui-url", "u", "--engine-url", "e", "--engine-repo", "/r", *extra]
    )
    assert runner.diagnostics_timeout == dfeops.DIAGNOSTICS_BOX


def test_the_acceptance_parser_takes_the_dir() -> None:
    base = ["acceptance", "--repo", "/nowhere"]
    args = dfeops.build_parser().parse_args([*base, "--diagnostics-dir", "/diag"])
    assert args.diagnostics_dir == "/diag"
    assert dfeops.build_parser().parse_args(base).diagnostics_dir is None


# --- the source runner captures before it removes its source ---------------------


def _close_out(
    monkeypatch: pytest.MonkeyPatch, results: list[wizard.StepResult], *extra: str,
) -> tuple[list[str], list[wizard.StepResult]]:
    order: list[str] = []
    driver = types.SimpleNamespace(results=list(results))
    args = source_run.build_parser().parse_args(
        ["--ui-url", "u", "--engine-url", "e", "--engine-repo", "/r", *extra]
    )
    current = types.SimpleNamespace(driver=driver, args=args)
    case = types.SimpleNamespace(name="fb0123abcd")
    real = source_run.capture_diagnostics

    def capture(prefix: list[str], label: str, timeout: float) -> wizard.StepResult:
        order.append(f"capture {label}")
        return real(prefix, label, timeout)

    monkeypatch.setattr(source_run, "teardown", lambda *_a: order.append("teardown"))
    monkeypatch.setattr(source_run, "capture_diagnostics", capture)
    source_run.close_out(current, case, "https://engine.example")
    return order, driver.results


FAILED = [wizard.StepResult("routed", "failed", "NOT routed into dfe.fb0123abcd")]


def test_a_failed_run_captures_before_the_teardown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    write_label = f"import pathlib, sys; pathlib.Path({str(marker)!r}).write_text(sys.argv[-1])"
    prefix = shlex.join([sys.executable, "-c", write_label])
    order, results = _close_out(monkeypatch, FAILED, "--diagnostics-exec", prefix)
    assert order == ["capture source-fb0123abcd", "teardown"]
    assert marker.read_text(encoding="utf-8") == "source-fb0123abcd"
    row = results[-1]
    assert (row.slug, row.status) == ("diagnostics", "done")


def test_a_clean_run_captures_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    passed = [wizard.StepResult("landed", "done", "60 of 60 rows")]
    order, _results = _close_out(monkeypatch, passed, "--diagnostics-exec", "false")
    assert order == ["teardown"]


def test_without_the_flag_the_runner_captures_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    order, results = _close_out(monkeypatch, FAILED)
    assert order == ["teardown"]
    assert [row.slug for row in results] == ["routed"]


def test_a_capture_that_hangs_is_stopped_and_the_teardown_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = shlex.join([sys.executable, "-c", "import time; time.sleep(30)"])
    started = time.monotonic()
    order, results = _close_out(
        monkeypatch, FAILED, "--diagnostics-exec", prefix, "--diagnostics-timeout", "0.5"
    )
    assert time.monotonic() - started < 5.0
    assert order[-1] == "teardown"
    assert (results[-1].slug, results[-1].status) == ("diagnostics", "skipped")
    assert "stopped" in results[-1].detail


def test_a_capture_that_raises_still_lets_the_teardown_run(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    driver = types.SimpleNamespace(results=list(FAILED))
    args = source_run.build_parser().parse_args(
        ["--ui-url", "u", "--engine-url", "e", "--engine-repo", "/r", "--diagnostics-exec", "x"]
    )

    def broken(*_a: object) -> wizard.StepResult:
        raise RuntimeError("the capture broke")

    monkeypatch.setattr(source_run, "teardown", lambda *_a: order.append("teardown"))
    monkeypatch.setattr(source_run, "capture_diagnostics", broken)
    current = types.SimpleNamespace(driver=driver, args=args)
    with pytest.raises(RuntimeError):
        source_run.close_out(current, types.SimpleNamespace(name="fb1"), "u")
    assert order == ["teardown"]


@pytest.mark.parametrize(
    ("prefix", "detail"),
    [
        (["/nonexistent/dfe-ops"], "could not start"),
        ([sys.executable, "-c", "import sys; sys.exit(3)"], "exited 3"),
    ],
)
def test_a_capture_that_fails_is_a_skipped_row(prefix: list[str], detail: str) -> None:
    row = source_run.capture_diagnostics(prefix, "source-fb1", 30.0)
    assert (row.slug, row.status) == ("diagnostics", "skipped")
    assert detail in row.detail
    assert wizard.exit_code([row]) == 0
