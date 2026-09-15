#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_otel_freshness_wait.py
#  Purpose:      Prove CORE 1 of the integration smoke test polls for a fresh
#                self-telemetry row instead of sampling once, so a deploy whose
#                collectors just became Ready is not reported as a dead pipeline.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Behaviour tests for CORE 1 in bootstrap/smoke-test-integration.sh.

The check used to run one `SELECT count()` over a 600s window and fail on zero,
so a stack whose collectors had been Ready for seconds reported stack-deploy
FAILED for a pipeline that was streaming (dfe-infra #270). A fake `kubectl`
first on PATH answers every query from a fixture and counts how many times the
freshness query was asked, so what is under test is the polling and no cluster
is involved.

    python3 scripts/tests/test_otel_freshness_wait.py

No third-party deps and no test runner, matching the script it tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _expect import expect, standalone, summary

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


def test_a_row_that_is_already_there_is_not_waited_for() -> None:
    """The pipeline streaming before the suite starts must cost no wait at all."""
    out, counts = run_smoke({"otel_zero_answers": 0}, DFE_OTEL_WAIT="10")

    expect(
        "CORE 1 passes on the first sample",
        "[PASS] infra OTel logs landing fresh" in out.stdout,
        out.stdout,
    )
    expect("and asks once to pass, once to check", counts.get("otel") == 2, str(counts))
    expect("with no wait reported", "waited 0s of 10s" in out.stdout, out.stdout)


def test_a_collector_that_exports_late_still_passes() -> None:
    """The #270 case: nothing has landed when the suite starts, and it lands shortly after."""
    out, counts = run_smoke({"otel_zero_answers": 2}, DFE_OTEL_WAIT="10")

    expect(
        "CORE 1 passes once the first row lands",
        "[PASS] infra OTel logs landing fresh" in out.stdout,
        out.stdout,
    )
    expect("having polled rather than sampled", (counts.get("otel") or 0) > 2, str(counts))
    expect("and saying how long it waited", "waited 2s of 10s" in out.stdout, out.stdout)


def test_a_dead_pipeline_still_fails_inside_the_bound() -> None:
    """The check exists to catch a pipeline that never streams, so it must still fail."""
    out, _ = run_smoke({"otel_zero_answers": 99}, DFE_OTEL_WAIT="2")

    expect(
        "CORE 1 fails when no row lands inside the wait",
        "[FAIL] infra OTel logs landing fresh" in out.stdout,
        out.stdout,
    )
    expect("and the suite fails with it", out.returncode != 0, f"rc={out.returncode}")


def test_the_wait_is_bounded_and_the_bound_is_a_knob() -> None:
    """An unbounded poll would hang a deploy; DFE_OTEL_WAIT=0 is one sample."""
    out, counts = run_smoke({"otel_zero_answers": 99}, DFE_OTEL_WAIT="0")

    expect("no poll at all when the bound is zero", counts.get("otel") == 2, str(counts))
    expect(
        "and the row still fails rather than hanging",
        "[FAIL] infra OTel logs landing fresh" in out.stdout,
        out.stdout,
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
