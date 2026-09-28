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
which answers 0 for the first `otel_zero_answers` asks. Each ask of that query is
counted, so a test can tell polling from sampling.

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


def bump(key):
    counts[key] = counts.get(key, 0) + 1
    with open(counts_file, "w", encoding="utf-8") as fh:
        json.dump(counts, fh)
    return counts[key]


if "exec" in args:
    if "-i" in args:
        sys.stdin.read()
        sys.exit(0)
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
