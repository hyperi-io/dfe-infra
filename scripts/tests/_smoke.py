#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         _smoke.py
#  Purpose:      Run bootstrap/smoke-test-integration.sh against a fake kubectl
#                that answers from a fixture, so its tests need no cluster.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A fake `kubectl` first on PATH, and the smoke suite run under it.

The fake answers every ClickHouse query with 1, except the OTel freshness query,
which answers 0 for the first `otel_zero_answers` asks, and the queries over the
landing table, which answer from the POSTs the fake received. Each ask of the OTel
query is counted, so a test can tell polling from sampling.

A `kafka` fixture adds a broker namespace: its `pods` (name, labels, phase and a
role of broker, redpanda or other), the `topics` it holds and the `group_output`
a broker prints for `kafka-consumer-groups.sh`. A pod with the role `other` ships
the Kafka tools and runs no broker, as Strimzi's cruise-control pod does. Every
exec into one of its pods is counted as `kafka_exec:<pod>`.

`hyperdx_ready` adds a dfe-hyperdx deployment whose node runtime answers /readyz
with that verdict (the image has node and no wget or curl).
`loader_copies_payload_to_raw` makes the landing table hold the whole payload in
_raw on every row.

The leading underscore keeps pytest from collecting this module as a test file.

    from _smoke import run_smoke

    out, counts = run_smoke({"otel_zero_answers": 0}, DFE_OTEL_WAIT="10")
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SMOKE = REPO_ROOT / "bootstrap" / "smoke-test-integration.sh"

# One ClickHouse pod that answers every query. `otel_zero_answers` is how many
# times the freshness query returns 0 before it returns 1, which is the collector
# that has not exported yet; the call count lands in FAKE_KUBECTL_CALLS.
FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8") as fh:
    fixture = json.load(fh)
counts_file = os.environ["FAKE_KUBECTL_CALLS"]
try:
    with open(counts_file, encoding="utf-8") as fh:
        counts = json.load(fh)
except (OSError, ValueError):
    counts = {}
kafka = fixture.get("kafka")


def bump(key):
    counts[key] = counts.get(key, 0) + 1
    with open(counts_file, "w", encoding="utf-8") as fh:
        json.dump(counts, fh)
    return counts[key]


def namespace():
    return args[args.index("-n") + 1] if "-n" in args else ""


def exec_target():
    return next(a for a in args[args.index("exec") + 1:] if not a.startswith("-"))


def selected(labels, selector):
    for term in filter(None, selector.split(",")):
        if term.startswith("!"):
            if term[1:] in labels:
                return False
        elif "=" in term:
            key, value = term.split("=", 1)
            if labels.get(key) != value:
                return False
        elif term not in labels:
            return False
    return True


def kafka_pod(name):
    return next((p for p in kafka["pods"] if p["name"] == name), None)


def kafka_tool(pod, script):
    broker = pod["role"] == "broker"
    if "kafka-consumer-groups.sh" in script:
        if broker:
            print(kafka.get("group_output", ""))
        else:
            print("Error: Executing consumer group command failed due to "
                  "Failed to create new KafkaAdminClient")
    elif broker and "kafka-topics.sh" in script and "--list" in script:
        print("\\n".join(kafka["topics"]))
    elif broker and "kafka-get-offsets.sh" in script:
        print("main_land:0:5")
    sys.exit(0)


def kafka_rpk(rest):
    if rest[:2] == ["topic", "list"]:
        print("\\n".join(kafka["topics"]))
    elif rest[:2] == ["topic", "describe"]:
        print("PARTITION LOG-START-OFFSET HIGH-WATERMARK")
        print("0 0 0 5")
    elif rest[:2] == ["group", "list"]:
        print(kafka.get("rpk_groups", ""))
    sys.exit(0)


if kafka and namespace() == kafka["namespace"]:
    if "exec" in args:
        pod = kafka_pod(exec_target())
        if pod is None:
            sys.exit(1)
        if "command -v rpk" in args:
            sys.exit(0 if pod["role"] == "redpanda" else 1)
        bump("kafka_exec:" + pod["name"])
        if "rpk" in args:
            kafka_rpk(args[args.index("rpk") + 1:])
        kafka_tool(pod, sys.stdin.read())
    if "pods" in args:
        selector = args[args.index("-l") + 1] if "-l" in args else ""
        columns = args[args.index("-o") + 1] if "-o" in args else ""
        for pod in kafka["pods"]:
            if selected(pod["labels"], selector):
                print(*([pod["name"], pod["phase"]] if "status.phase" in columns else [pod["name"]]))
        sys.exit(0)
    if "secret" in args:
        print("cHc=")
        sys.exit(0)
if kafka and args[:2] == ["get", "ns"]:
    sys.exit(0 if args[2] == kafka["namespace"] else 1)
if "hyperdx_ready" in fixture and args[-3:] == ["get", "deploy", "dfe-hyperdx"]:
    sys.exit(0)

if "exec" in args:
    if "-i" in args:
        posted = sys.stdin.read()
        bump("posted")
        if '"_raw"' in posted:
            bump("posted_raw")
        sys.exit(0)
    if "node" in args:
        sys.exit(0 if fixture.get("hyperdx_ready") else 1)
    if "mongosh" in args or "wget" in args:
        sys.exit(1)
    query = args[args.index("--query") + 1] if "--query" in args else ""
    if "otel_logs" in query:
        asked = bump("otel")
        print(0 if asked <= fixture.get("otel_zero_answers", 0) else 1)
        sys.exit(0)
    if "SHOW DATABASES" in query:
        print("dfe")
        sys.exit(0)
    # The landing table holds one row per POST, and a POST that carries a _raw
    # field of its own lands with it set. A loader that copies the payload into
    # _raw leaves no row with it NULL and none holding a line that is not JSON.
    copies_payload = fixture.get("loader_copies_payload_to_raw")
    if "_raw IS NULL" in query:
        print(0 if copies_payload else counts.get("posted", 0) - counts.get("posted_raw", 0))
    elif "length(_raw)" in query:
        print(0 if copies_payload else counts.get("posted_raw", 0))
    elif "LIKE" in query:
        print(counts.get("posted", 0))
    else:
        print(1)
    sys.exit(0)
if "pods" in args:
    print("dfe-clickhouse-0")
    sys.exit(0)
if "secret" in args or "get" in args and "ns" in args:
    sys.exit(1)
sys.exit(1)
"""


def run_smoke(fixture: dict, **env_overrides: str) -> tuple[subprocess.CompletedProcess, dict]:
    """The smoke suite against a fixed ClickHouse reading, and the call counts."""
    with tempfile.TemporaryDirectory() as tmp:
        bindir = Path(tmp)
        kubectl = bindir / "kubectl"
        kubectl.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
        kubectl.chmod(0o755)
        fixture_file = bindir / "fixture.json"
        fixture_file.write_text(json.dumps(fixture), encoding="utf-8", newline="\n")
        counts_file = bindir / "counts.json"

        env = dict(os.environ)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["FAKE_KUBECTL_FIXTURE"] = str(fixture_file)
        env["FAKE_KUBECTL_CALLS"] = str(counts_file)
        env["DFE_OTEL_WAIT_INTERVAL"] = "1"
        env.update(env_overrides)
        out = subprocess.run(
            ["bash", str(SMOKE)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
        counts = json.loads(counts_file.read_text(encoding="utf-8")) if counts_file.exists() else {}
        return out, counts
