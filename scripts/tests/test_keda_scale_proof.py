#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_keda_scale_proof.py
#  Purpose:      Prove the artificial KEDA scale proof bounds itself on the
#                control loops it waits for, waits for the stack's own rollouts,
#                and reaches a named verdict rather than one FAIL for every cause.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Behaviour tests for bootstrap/keda-scale-test.sh.

The proof used to fail on a fixed 120s bound whenever the cluster was busy
reconciling a roll (dfe-infra #275), and to report one FAIL whether the scaler
was broken or whether it was the unreproduced stall in dfe-infra #134. A fake
`kubectl` first on PATH answers every read from a fixture, so the bound and the
verdict are what is under test and no cluster is involved.

    python3 scripts/tests/test_keda_scale_proof.py

No third-party deps and no test runner, matching the script it tests.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROOF = REPO_ROOT / "bootstrap" / "keda-scale-test.sh"
RUNNER = REPO_ROOT / "bootstrap" / "run-all-smoke-tests.sh"
UNPROVEN = 3

# Every read the proof makes, answered from the fixture. `ready` and `rolling`
# are consumed one entry per call and the last entry repeats, which is how a
# cluster that changes between polls is described.
FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8") as fh:
    fixture = json.load(fh)
state_file = os.environ["FAKE_KUBECTL_CALLS"]
try:
    with open(state_file, encoding="utf-8") as fh:
        state = json.load(fh)
except (OSError, ValueError):
    state = {}


def take(key, default):
    values = fixture.get(key)
    if not values:
        return default
    index = state.get(key, 0)
    state[key] = index + 1
    with open(state_file, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    return values[min(index, len(values) - 1)]


def jsonpath():
    for arg in args:
        if arg.startswith("jsonpath="):
            return arg[len("jsonpath="):]
    return ""


if "apply" in args:
    sys.stdin.read()
    sys.exit(0)
if "delete" in args:
    sys.exit(0)
if "exec" in args:
    print(1)
    sys.exit(0)
if any("custom-columns" in arg for arg in args):
    for line in take("rolling", ""):
        print(line)
    sys.exit(0)
if "hpa" in args:
    hpa = fixture.get("hpa") or {}
    path = jsonpath()
    if "averageValue" in path:
        print(hpa.get("average_value", ""), end="")
    elif "currentMetrics" in path:
        print(hpa.get("value", ""), end="")
    elif "desiredReplicas" in path:
        print(hpa.get("desired", ""), end="")
    elif "ScalingActive" in path:
        print(hpa.get("active", ""), end="")
    sys.exit(0)
if "scaledobject" in args:
    sys.exit(0 if fixture.get("real_scaledobject") else 1)
if "deploy" in args:
    print(take("ready", 1), end="")
    sys.exit(0)
if "pod" in args or "pods" in args:
    print("dfe-clickhouse-0", end="")
    sys.exit(0)
sys.exit(1)
"""

# A cluster where nothing is mid-roll and the target scaled out and back in.
SETTLED: list[str] = []
SCALED = ["2", "1"]


def run_proof(fixture: dict, **env_overrides: str) -> subprocess.CompletedProcess:
    """The proof against a fixed cluster reading, with the waits shortened."""
    with tempfile.TemporaryDirectory() as tmp:
        bindir = Path(tmp)
        kubectl = bindir / "kubectl"
        kubectl.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
        kubectl.chmod(0o755)
        fixture_file = bindir / "fixture.json"
        fixture_file.write_text(json.dumps(fixture), encoding="utf-8", newline="\n")

        env = dict(os.environ)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["FAKE_KUBECTL_FIXTURE"] = str(fixture_file)
        env["FAKE_KUBECTL_CALLS"] = str(bindir / "state.json")
        env["DFE_WAIT_INTERVAL"] = "1"
        env["DFE_SETTLE_INTERVAL"] = "1"
        env["DFE_SCALE_IN_TIMEOUT"] = "2"
        env.update(env_overrides)
        return subprocess.run(
            ["bash", str(PROOF)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )


def run_stalled(**hpa: str) -> subprocess.CompletedProcess:
    """A run where the replicas never move, under the shortest bound the terms allow."""
    return run_proof(
        {"rolling": [SETTLED], "ready": ["1"], "hpa": hpa},
        DFE_KEDA_TEST_POLL="1",
        DFE_HPA_SYNC_PERIOD="0",
        DFE_POD_READY_ALLOWANCE="1",
    )


def test_the_scale_out_bound_is_derived_from_the_loops_it_waits_for() -> None:
    """The #275 fix: 120s was a guess, and a busy cluster exceeded it."""
    out = run_proof({"rolling": [SETTLED], "ready": SCALED})

    # poll 10 + HPA sync 15, doubled because the two loops are unsynchronised,
    # plus 90s for a scheduled pod to report Ready.
    expect("the bound is the formula, not 120s", "bounded 140s" in out.stdout, out.stdout)
    expect("and it shows its working", "x2 + 90s pod start" in out.stdout, out.stdout)
    expect("a scaling target passes", out.returncode == 0, f"rc={out.returncode} {out.stdout}")


def test_every_term_of_the_bound_is_a_knob() -> None:
    """A cluster with a different sync period must be able to say so."""
    out = run_proof(
        {"rolling": [SETTLED], "ready": SCALED},
        DFE_KEDA_TEST_POLL="5",
        DFE_HPA_SYNC_PERIOD="30",
        DFE_POD_READY_ALLOWANCE="10",
    )

    expect("(5 + 30) x2 + 10", "bounded 80s" in out.stdout, out.stdout)


def test_the_proof_waits_for_the_stack_to_stop_rolling() -> None:
    """A run started inside a roll is what missed the bound on a live deploy (#275)."""
    rolling = ["dfe-loader 2 1 1 4 3"]
    out = run_proof({"rolling": [rolling, rolling, SETTLED], "ready": SCALED})

    expect("it waited for the roll", "settled after 2s" in out.stdout, out.stdout)
    expect("and then proved the scaler", out.returncode == 0, f"rc={out.returncode} {out.stdout}")


def test_a_stack_that_never_settles_is_reported_and_not_blocked_on() -> None:
    """The settle wait is a courtesy, so it must never become a second timeout."""
    out = run_proof(
        {"rolling": [["dfe-loader 2 1 1 4 3"]], "ready": SCALED},
        DFE_SETTLE_TIMEOUT="2",
    )

    expect("the roll is named", "still rolling after 2s: dfe-loader" in out.stdout, out.stdout)
    expect("and the proof still ran", out.returncode == 0, f"rc={out.returncode} {out.stdout}")


def test_a_deployment_with_no_status_yet_does_not_read_as_rolling() -> None:
    """custom-columns prints <none> for an absent field, which is zero, not a mismatch."""
    out = run_proof({"rolling": [["dfe-hunt-runner 0 <none> <none> 1 1"]], "ready": SCALED})

    expect("a scaled-to-zero deployment is settled", "settled after 0s" in out.stdout, out.stdout)


def test_a_pressure_the_hpa_never_saw_is_a_failure() -> None:
    """The metrics path is the thing being proven, so its absence is a real FAIL."""
    out = run_stalled(active="False", desired="1")

    expect("it fails", out.returncode == 1, f"rc={out.returncode} {out.stdout}")
    expect("naming the HPA it asked", "never reached the HPA" in out.stdout, out.stdout)


def test_a_scale_decision_whose_pods_never_arrived_is_a_failure() -> None:
    """KEDA and the HPA agreed; the cluster could not place the pod."""
    out = run_stalled(active="True", value="100", desired="2")

    expect("it fails", out.returncode == 1, f"rc={out.returncode} {out.stdout}")
    expect("naming the pods", "no Ready pod" in out.stdout, out.stdout)


def test_pressure_that_never_carried_the_average_is_a_failure() -> None:
    """A reading under target is the injection not landing, which the proof owns."""
    out = run_stalled(active="True", value="10", desired="1")

    expect("it fails", out.returncode == 1, f"rc={out.returncode} {out.stdout}")
    expect(
        "naming the average",
        "never carried the shim's average" in out.stdout,
        out.stdout,
    )


def test_an_hpa_that_read_the_pressure_and_did_nothing_is_unproven() -> None:
    """dfe-infra #134: proven once, never reproduced, so neither verdict is honest."""
    out = run_stalled(active="True", value="100", desired="1")

    expect("its own exit code", out.returncode == UNPROVEN, f"rc={out.returncode} {out.stdout}")
    expect("said plainly", "UNPROVEN" in out.stdout, out.stdout)
    expect("with the issue to read", "#134" in out.stdout, out.stdout)
    expect(
        "and no claim either way",
        "is NOT proven by this run and is not disproven" in out.stdout,
        out.stdout,
    )


def test_a_milli_unit_reading_is_read_as_its_own_value() -> None:
    """An HPA renders a Quantity, so 100 can arrive as 100000m."""
    out = run_stalled(active="True", value="100000m", desired="1")

    expect("100000m is 100, which is over target", out.returncode == UNPROVEN,
           f"rc={out.returncode} {out.stdout}")


def test_an_average_value_trigger_is_read_too() -> None:
    """metricType AverageValue puts the reading in a different field of the same status."""
    out = run_stalled(active="True", average_value="90", desired="1")

    expect("the averageValue field answers", out.returncode == UNPROVEN,
           f"rc={out.returncode} {out.stdout}")


def run_runner(codes: dict[str, int]) -> subprocess.CompletedProcess:
    """run-all-smoke-tests.sh over stub suites, each exiting the code it is given.

    The suite names come out of the runner itself, so a renamed script fails
    here rather than silently running nothing.
    """
    text = RUNNER.read_text(encoding="utf-8")
    scripts = re.findall(r'run_test\s+"[^"]+"\s+"([^"]+)"', text)
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / "run-all-smoke-tests.sh").write_text(text, encoding="utf-8", newline="\n")
        for script in scripts:
            stub = home / script
            stub.write_text(
                f"#!/usr/bin/env bash\nexit {codes.get(script, 0)}\n",
                encoding="utf-8",
                newline="\n",
            )
            stub.chmod(0o755)
        return subprocess.run(
            ["bash", str(home / "run-all-smoke-tests.sh")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )


def test_the_runner_prints_unproven_as_its_own_word() -> None:
    """A suite exiting 3 must not be counted as a pass or as a failure."""
    out = run_runner({"keda-scale-test.sh": UNPROVEN})

    expect("the suite reads UNPROVEN", "UNPROVEN -- it ran" in out.stdout, out.stdout)
    expect("counted apart", "0 failed, 1 unproven" in out.stdout, out.stdout)
    expect("and it does not fail the deploy", out.returncode == 0, f"rc={out.returncode}")


def test_a_failing_suite_still_fails_the_run() -> None:
    """The new verdict must not have widened what passes."""
    out = run_runner({"keda-scale-test.sh": 1})

    expect("it reads FAILED", "FAILED" in out.stdout, out.stdout)
    expect("counted as a failure", "1 failed, 0 unproven" in out.stdout, out.stdout)
    expect("and the run fails", out.returncode == 1, f"rc={out.returncode}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
