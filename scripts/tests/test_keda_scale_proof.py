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
was broken or whether the HPA read the pressure and did nothing. A fake
`kubectl` first on PATH answers every read from a fixture, so the bound, the
apps phase 2 finds and the verdict are what is under test and no cluster is
involved. It records every exec with what it was sent on stdin, so a test can
see where the pressure rows went.

A fixture's `rolling` list, and each Deployment's `ready` list, are consumed one
entry per call and the last entry repeats, which is how a cluster that changes
between polls is described. `ready` as a list answers for every Deployment, and
as a mapping for the one it names, with "*" for the throwaway target, whose name
is minted per run.

    python3 scripts/tests/test_keda_scale_proof.py

No third-party deps and no test runner, matching the script it tests.
"""

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


def take(key, values, default):
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


def after(word):
    if word in args and args.index(word) + 1 < len(args):
        return args[args.index(word) + 1]
    return ""


def scaledobjects():
    return fixture.get("scaledobjects") or []


def pressure_index(so_name):
    for so in scaledobjects():
        if so["name"] == so_name:
            for index, trigger in enumerate(so["triggers"]):
                if trigger[0] == "metrics-api":
                    return index
    return 0


if "apply" in args:
    sys.stdin.read()
    sys.exit(0)
if "delete" in args:
    sys.exit(0)
if "secret" in args:
    # The dfe-cluster facts: only clickhouse_mode is ever asked for.
    print(fixture.get("clickhouse_mode", "") if "clickhouse_mode" in jsonpath() else "", end="")
    sys.exit(0)
if "exec" in args:
    sent = sys.stdin.read() if "-i" in args else ""
    with open(os.environ["FAKE_KUBECTL_EXECS"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"args": args, "stdin": sent}) + "\\n")
    query = sent + " ".join(args)
    print(fixture.get("own_rows", 1) if "NOT mapContains" in query else 1)
    sys.exit(0)
if any("custom-columns" in arg for arg in args):
    for line in take("rolling", fixture.get("rolling"), ""):
        print(line)
    sys.exit(0)
if "hpa" in args:
    name = after("hpa")
    hpa = fixture.get("hpa") or {}
    path = jsonpath()
    if "range .status.currentMetrics" in path:
        metric = f"s{pressure_index(name[len('keda-hpa-'):])}-metric-api-value"
        for key, kind, metric_name in (
            ("value", "current", metric), ("average_value", "current", metric),
            ("cpu", "current", "cpu"), ("target", "target", metric),
            ("cpu_target", "target", "cpu"),
        ):
            if key in hpa:
                print(f"{kind}|{metric_name}|{hpa[key]}")
    elif "averageValue" in path:
        print(hpa.get("average_value", ""), end="")
    elif "currentMetrics" in path:
        print(hpa.get("value", ""), end="")
    elif "desiredReplicas" in path:
        print(hpa.get("desired", ""), end="")
    elif "message" in path:
        print(hpa.get("why", ""), end="")
    elif "ScalingActive" in path:
        print(hpa.get("active", ""), end="")
    sys.exit(0)
if "scaledobject" in args:
    path = jsonpath()
    if "range .items" in path:
        for so in scaledobjects():
            fields = [so["name"], so["target"], so.get("min", ""), so.get("max", ""), so.get("poll", "")]
            print("|".join(str(field) for field in fields))
    elif "range .spec.triggers" in path:
        for so in scaledobjects():
            if so["name"] == after("scaledobject"):
                for trigger in so["triggers"]:
                    print("|".join(trigger))
    sys.exit(0)
if "deploy" in args:
    name = after("deploy")
    known = fixture.get("deployments")
    if known is not None and name not in known and not name.startswith("keda-scale-test-"):
        sys.exit(1)
    if "name" in args:
        print(f"deployment.apps/{name}")
        sys.exit(0)
    ready = fixture.get("ready")
    if isinstance(ready, dict):
        print(take(f"ready:{name}", ready.get(name) or ready.get("*"), 1), end="")
    else:
        print(take("ready", ready, 1), end="")
    sys.exit(0)
if "pod" in args or "pods" in args:
    if fixture.get("clickhouse_pods", True):
        print("dfe-clickhouse-0", end="")
        sys.exit(0)
    # What kubectl answers a jsonpath of items[0] on an empty list.
    sys.stderr.write("error: array index out of bounds: index 0, length 0\\n")
    sys.exit(1)
sys.exit(1)
"""

# A cluster where nothing is mid-roll and the throwaway scaled out and back in.
SETTLED: list[str] = []
SCALED = ["2", "1"]
# What an HPA that the pressure drove reads at scale-out.
DRIVEN = {"value": "100", "cpu": "4", "cpu_target": "70"}
SHIM = "http://dfe-keda-shim.dfe.svc.cluster.local:8080/keda/pressure?service="


def pressure_app(name: str, **fields: object) -> dict:
    """A ScaledObject as the charts render it: floor 2, ceiling 12, cpu then the shim's pressure."""
    triggers = [["cpu", "", ""], ["metrics-api", f"{SHIM}{name}", "70"]]
    return {"name": f"{name}-scaler", "target": name, "min": 2, "max": 12, "poll": 15,
            "triggers": triggers, **fields}


def phase_two(apps: list[dict], ready: dict, **fixture: object) -> dict:
    """A cluster whose throwaway scales cleanly, carrying *apps* for phase 2."""
    return {"rolling": [SETTLED], "ready": {"*": SCALED, **ready}, "hpa": DRIVEN,
            "scaledobjects": apps, "deployments": [app["target"] for app in apps], **fixture}


def run_proof_logged(
    fixture: dict, **env_overrides: str
) -> tuple[subprocess.CompletedProcess, list[dict]]:
    """The proof against a fixed cluster reading, and every exec it made."""
    with tempfile.TemporaryDirectory() as tmp:
        bindir = Path(tmp)
        kubectl = bindir / "kubectl"
        kubectl.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
        kubectl.chmod(0o755)
        fixture_file = bindir / "fixture.json"
        fixture_file.write_text(json.dumps(fixture), encoding="utf-8", newline="\n")
        execs_file = bindir / "execs.jsonl"

        dropped = ("DFE_CLICKHOUSE_MODE", "DFE_KEDA_REAL_DEPLOYMENT")
        env = {k: v for k, v in os.environ.items() if k not in dropped}
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["FAKE_KUBECTL_FIXTURE"] = str(fixture_file)
        env["FAKE_KUBECTL_CALLS"] = str(bindir / "state.json")
        env["FAKE_KUBECTL_EXECS"] = str(execs_file)
        env["DFE_WAIT_INTERVAL"] = "1"
        env["DFE_SETTLE_INTERVAL"] = "1"
        env["DFE_SCALE_IN_TIMEOUT"] = "2"
        env["DFE_KEDA_PRESSURE_INTERVAL"] = "1"
        env["DFE_KEDA_ZERO_HOLD"] = "1"
        env.update(env_overrides)
        out = subprocess.run(
            ["bash", str(PROOF)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
        lines = execs_file.read_text(encoding="utf-8").splitlines() if execs_file.exists() else []
        return out, [json.loads(line) for line in lines]


def run_proof(fixture: dict, **env_overrides: str) -> subprocess.CompletedProcess:
    """The proof against a fixed cluster reading, with the waits shortened."""
    return run_proof_logged(fixture, **env_overrides)[0]


def run_stalled(**hpa: str) -> subprocess.CompletedProcess:
    """A run where the replicas never move, under the shortest bound the terms allow."""
    return run_proof(
        {"rolling": [SETTLED], "ready": ["1"], "hpa": hpa},
        DFE_HPA_SYNC_PERIOD="0",
        DFE_POD_READY_ALLOWANCE="1",
    )


def inserts(execs: list[dict]) -> list[str]:
    """Every INSERT the proof sent, whichever way it reached ClickHouse."""
    sent = [e["stdin"] or " ".join(e["args"]) for e in execs]
    return [s for s in sent if "INSERT INTO dfe.otel_metrics_gauge" in s]


def test_the_scale_out_bound_is_the_hpa_sync_twice_plus_a_pod_start() -> None:
    """Above a floor of zero the HPA reads the metric itself, so KEDA's poll is no term of it."""
    out = run_proof({"rolling": [SETTLED], "ready": SCALED, "hpa": DRIVEN})

    # HPA sync 15, twice over, plus 120s for a pod that may wait on a node.
    expect("the bound is the formula", "bounded 150s" in out.stdout, out.stdout)
    expect("and it shows its working", "HPA sync 15s x2 + 120s pod start" in out.stdout, out.stdout)
    expect("a scaling target passes", out.returncode == 0, f"rc={out.returncode} {out.stdout}")


def test_every_term_of_the_bound_is_a_dial() -> None:
    """A cluster with a different sync period must be able to say so."""
    out = run_proof(
        {"rolling": [SETTLED], "ready": SCALED, "hpa": DRIVEN},
        DFE_HPA_SYNC_PERIOD="30",
        DFE_POD_READY_ALLOWANCE="10",
    )

    expect("30 x2 + 10", "bounded 70s" in out.stdout, out.stdout)


def test_the_throwaway_scaledobject_sets_no_field_keda_ignores_above_zero() -> None:
    """KEDA warns that pollingInterval and cooldownPeriod do nothing at a floor of 1."""
    text = PROOF.read_text(encoding="utf-8")
    manifest = text[text.index("kind: ScaledObject"):text.index("EOF\n\nreset_set")]

    expect("no pollingInterval", "pollingInterval" not in manifest, manifest)
    expect("no cooldownPeriod", "cooldownPeriod" not in manifest, manifest)


def test_the_proof_waits_for_the_stack_to_stop_rolling() -> None:
    """A run started inside a roll is what missed the bound on a live deploy (#275)."""
    rolling = ["dfe-loader 2 1 1 4 3"]
    out = run_proof({"rolling": [rolling, rolling, SETTLED], "ready": SCALED, "hpa": DRIVEN})

    expect("it waited for the roll", "settled after 2s" in out.stdout, out.stdout)
    expect("and then proved the scaler", out.returncode == 0, f"rc={out.returncode} {out.stdout}")


def test_a_stack_that_never_settles_is_reported_and_not_blocked_on() -> None:
    """The settle wait is a courtesy, so it must never become a second timeout."""
    out = run_proof(
        {"rolling": [["dfe-loader 2 1 1 4 3"]], "ready": SCALED, "hpa": DRIVEN},
        DFE_SETTLE_TIMEOUT="2",
    )

    expect("the roll is named", "still rolling after 2s: dfe-loader" in out.stdout, out.stdout)
    expect("and the proof still ran", out.returncode == 0, f"rc={out.returncode} {out.stdout}")


def test_a_deployment_with_no_status_yet_does_not_read_as_rolling() -> None:
    """custom-columns prints <none> for an absent field, which is zero, not a mismatch."""
    out = run_proof(
        {"rolling": [["dfe-hunt-runner 0 <none> <none> 1 1"]], "ready": SCALED, "hpa": DRIVEN}
    )

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
    """The HPA read the pressure over target and left the replicas: neither verdict is honest."""
    out = run_stalled(active="True", value="100", desired="1")

    expect("its own exit code", out.returncode == UNPROVEN, f"rc={out.returncode} {out.stdout}")
    expect("said plainly", "UNPROVEN" in out.stdout, out.stdout)
    expect("citing no closed issue", "#134" not in out.stdout, out.stdout)
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


def test_scale_out_logs_each_hpa_reading_and_what_the_hpa_scaled_on() -> None:
    """The log must show the pressure moved it, not cpu."""
    why = ("the HPA was able to successfully calculate a replica count from external metric "
           "s0-metric-api-value(&LabelSelector{})")
    hpa = {**DRIVEN, "target": "50", "why": why}
    out = run_proof({"rolling": [SETTLED], "ready": SCALED, "hpa": hpa})

    expect("it passes", out.returncode == 0, f"rc={out.returncode} {out.stdout}")
    expect(
        "the pressure reading and its target",
        "pressure 100 against target 50, cpu 4% against 70%" in out.stdout,
        out.stdout,
    )
    expect(
        "and the HPA's own word on which metric it used, without the selector",
        "from external metric s0-metric-api-value\n" in out.stdout,
        out.stdout,
    )


def test_a_scale_out_with_the_pressure_under_target_is_a_failure() -> None:
    """Replicas that rose while the pressure read under target were raised by something else."""
    hpa = {"value": "20", "cpu": "95", "cpu_target": "70"}
    out = run_proof({"rolling": [SETTLED], "ready": SCALED, "hpa": hpa})

    expect("it fails", out.returncode == 1, f"rc={out.returncode} {out.stdout}")
    expect(
        "naming the reading",
        "scaled out with its pressure reading 20 under target 50" in out.stdout,
        out.stdout,
    )


def test_a_scale_out_with_no_pressure_reading_is_a_failure() -> None:
    """An HPA that carries no reading for the pressure trigger cannot have scaled on it."""
    hpa = {"cpu": "95", "cpu_target": "70"}
    out = run_proof({"rolling": [SETTLED], "ready": SCALED, "hpa": hpa})

    expect("it fails", out.returncode == 1, f"rc={out.returncode} {out.stdout}")
    expect("saying the reading is absent", "pressure reading absent" in out.stdout, out.stdout)


def test_cpu_over_its_own_target_at_scale_out_is_said() -> None:
    """Both triggers over target is still a pass, but the log must not read as pressure alone."""
    hpa = {"value": "100", "cpu": "90", "cpu_target": "70"}
    out = run_proof({"rolling": [SETTLED], "ready": SCALED, "hpa": hpa})

    expect("it passes", out.returncode == 0, f"rc={out.returncode} {out.stdout}")
    expect("the cpu is named", "cpu was at or over its target too" in out.stdout, out.stdout)


def ran_in_engine(args: list[str]) -> bool:
    """Whether a kubectl exec ran in the engine Deployment's own dfe-engine container."""
    head = args[: args.index("--")]
    return "deploy/dfe-engine" in head and head[head.index("-c") + 1] == "dfe-engine"


def test_an_external_clickhouse_takes_the_pressure_through_the_engine() -> None:
    """ClickHouse Cloud runs no pod here, and the shim reads the engine's own server."""
    out, execs = run_proof_logged(
        {
            "rolling": [SETTLED],
            "ready": SCALED,
            "hpa": DRIVEN,
            "clickhouse_mode": "external",
            "clickhouse_pods": False,
        }
    )

    expect("the proof passes", out.returncode == 0, f"rc={out.returncode} {out.stdout}{out.stderr}")
    in_engine = [e for e in execs if ran_in_engine(e["args"])]
    expect(
        "every query ran in the engine's own container",
        bool(execs) and in_engine == execs,
        f"{execs}",
    )
    sent = [e["stdin"] for e in in_engine]
    expect(
        "the pressure row went in on stdin",
        any(
            s.startswith("INSERT INTO dfe.otel_metrics_gauge")
            and "'dfe_scaling_pressure', 100" in s
            for s in sent
        ),
        f"{sent}",
    )
    expect(
        "and the cleanup tombstoned it the same way",
        any(s.startswith("ALTER TABLE dfe.otel_metrics_gauge DELETE") for s in sent),
        f"{sent}",
    )


def test_an_in_cluster_clickhouse_takes_the_pressure_through_its_pod() -> None:
    """No clickhouse_mode fact keeps the injection on clickhouse-client in a ClickHouse pod."""
    out, execs = run_proof_logged({"rolling": [SETTLED], "ready": SCALED, "hpa": DRIVEN})

    expect("the proof passes", out.returncode == 0, f"rc={out.returncode} {out.stdout}{out.stderr}")
    expect(
        "every query ran clickhouse-client in the ClickHouse pod",
        bool(execs)
        and all(
            "dfe-clickhouse-0" in e["args"] and "clickhouse-client" in e["args"] for e in execs
        ),
        f"{execs}",
    )


def test_phase_two_finds_every_pressure_app_and_proves_them_together() -> None:
    """The ScaledObjects decide the set, not a list in the script."""
    apps = [pressure_app("dfe-receiver"), pressure_app("dfe-loader")]
    ready = {"dfe-receiver": ["2", "3", "2"], "dfe-loader": ["2", "3", "2"]}
    out, execs = run_proof_logged(phase_two(apps, ready))

    expect("it passes", out.returncode == 0, f"rc={out.returncode} {out.stdout}")
    expect(
        "both real apps went floor, +1, floor",
        "KEDA scaled the real dfe-receiver 2->3->2, dfe-loader 2->3->2" in out.stdout,
        out.stdout,
    )
    together = [s for s in inserts(execs) if "'dfe-receiver'" in s and "'dfe-loader'" in s]
    expect("one INSERT carries both services' pressure", bool(together), f"{inserts(execs)}")
    expect(
        "each app's pressure is read off its own trigger's HPA metric",
        "HPA metric s1-*" in out.stdout,
        out.stdout,
    )


def test_an_app_scaled_on_cpu_alone_is_a_reported_skip() -> None:
    """dfe-ui and a vector transform carry no pressure trigger, so pressure is not theirs to prove."""
    cpu_only = {"name": "dfe-ui-scaler", "target": "dfe-ui", "min": 2, "max": 12,
                "triggers": [["cpu", "", ""]]}
    apps = [pressure_app("dfe-receiver"), cpu_only]
    out = run_proof(phase_two(apps, {"dfe-receiver": ["2", "3", "2"], "dfe-ui": ["2"]}))

    expect("it passes", out.returncode == 0, f"rc={out.returncode} {out.stdout}")
    expect(
        "the skip names the app and why",
        "SKIP dfe-ui: dfe-ui-scaler carries no shim pressure trigger" in out.stdout,
        out.stdout,
    )


def test_an_app_this_profile_does_not_run_is_a_reported_skip() -> None:
    """A ScaledObject whose Deployment is absent is skipped with its reason, never failed."""
    apps = [pressure_app("dfe-receiver"), pressure_app("dfe-loader")]
    fixture = phase_two(apps, {"dfe-receiver": ["2", "3", "2"]})
    fixture["deployments"] = ["dfe-receiver"]
    out = run_proof(fixture)

    expect("it passes", out.returncode == 0, f"rc={out.returncode} {out.stdout}")
    expect(
        "the skip names the app",
        "SKIP dfe-loader: dfe-loader-scaler targets a Deployment" in out.stdout,
        out.stdout,
    )
    expect(
        "and the rest is proven",
        "KEDA scaled the real dfe-receiver 2->3->2 on" in out.stdout,
        out.stdout,
    )


def test_no_pressure_app_at_all_skips_phase_two_cleanly() -> None:
    """slim clears the pressure trigger, so phase 2 has nothing to drive and says so."""
    cpu_only = {"name": "dfe-receiver-scaler", "target": "dfe-receiver", "min": 1, "max": 2,
                "triggers": [["cpu", "", ""]]}
    out = run_proof(phase_two([cpu_only], {"dfe-receiver": ["1"]}))

    expect("it passes", out.returncode == 0, f"rc={out.returncode} {out.stdout}")
    expect(
        "said as a skip",
        "SKIP: no app ns=dfe runs is scaled by the shim's pressure" in out.stdout,
        out.stdout,
    )


def test_a_pinned_replica_count_is_a_reported_skip() -> None:
    """A ScaledObject with its ceiling at its floor has no scale to prove."""
    pinned = pressure_app("dfe-loader", max=2)
    apps = [pressure_app("dfe-receiver"), pinned]
    out = run_proof(phase_two(apps, {"dfe-receiver": ["2", "3", "2"]}))

    expect("it passes", out.returncode == 0, f"rc={out.returncode} {out.stdout}")
    expect(
        "the pin is named",
        "SKIP dfe-loader: dfe-loader-scaler pins it at 2 replicas" in out.stdout,
        out.stdout,
    )


def test_an_app_off_its_floor_is_unproven_and_the_rest_still_proven() -> None:
    """A loader already at its ceiling on real load cannot show a step from its floor."""
    apps = [pressure_app("dfe-receiver"), pressure_app("dfe-loader")]
    out = run_proof(phase_two(apps, {"dfe-receiver": ["2", "3", "2"], "dfe-loader": ["12"]}))

    expect("UNPROVEN", out.returncode == UNPROVEN, f"rc={out.returncode} {out.stdout}")
    expect(
        "naming the app and its count",
        "dfe-loader has 12 Ready replica(s), not its floor of 2" in out.stdout,
        out.stdout,
    )
    expect("the receiver was still proven", "PASS: dfe-receiver back to 2" in out.stdout, out.stdout)


def test_a_service_name_that_is_not_plain_never_reaches_sql() -> None:
    """The ServiceName comes off the cluster and goes into an INSERT."""
    hostile = pressure_app("dfe-receiver")
    hostile["triggers"] = [["cpu", "", ""], ["metrics-api", f"{SHIM}x');DROP", "70"]]
    out, execs = run_proof_logged(phase_two([hostile], {"dfe-receiver": ["2"]}))

    expect("UNPROVEN", out.returncode == UNPROVEN, f"rc={out.returncode} {out.stdout}")
    expect("no INSERT carries it", not [s for s in inserts(execs) if "DROP" in s], f"{inserts(execs)}")


def test_a_real_app_that_does_not_scale_back_in_fails_by_name() -> None:
    """Scale-in is half the proof."""
    apps = [pressure_app("dfe-receiver"), pressure_app("dfe-loader")]
    out = run_proof(phase_two(apps, {"dfe-receiver": ["2", "3", "2"], "dfe-loader": ["2", "3"]}))

    expect("it fails", out.returncode == 1, f"rc={out.returncode} {out.stdout}")
    expect(
        "naming the loader",
        "FAIL: dfe-loader did not scale back in to 2" in out.stdout,
        out.stdout,
    )


def test_an_app_that_writes_no_pressure_of_its_own_is_said() -> None:
    """Injected pressure scales an app whose own gauge is missing, so the log has to say so."""
    fixture = phase_two([pressure_app("dfe-loader")], {"dfe-loader": ["2", "3", "2"]}, own_rows=0)
    out = run_proof(fixture)

    expect("it still passes", out.returncode == 0, f"rc={out.returncode} {out.stdout}")
    expect(
        "the gap is named",
        "WARN: dfe-loader wrote no pressure row of its own" in out.stdout,
        out.stdout,
    )


def test_a_floor_of_zero_adds_keda_activation_to_the_bound() -> None:
    """From zero KEDA's own poll has to activate the target before the HPA takes over."""
    zero = pressure_app("dfe-transform-vrl-a", min=0, poll=5)
    out = run_proof(phase_two([zero], {"dfe-transform-vrl-a": ["0", "1", "0"]}))

    # HPA sync 15 x2 + 120, plus 5 x2 for the zero-floor app.
    expect("the bound carries the poll", "bounded 160s" in out.stdout, out.stdout)
    expect("and it passes", out.returncode == 0, f"rc={out.returncode} {out.stdout}")


def test_real_deployment_narrows_phase_two_to_one_app() -> None:
    """An operator chasing one app proves it alone."""
    apps = [pressure_app("dfe-receiver"), pressure_app("dfe-loader")]
    out, execs = run_proof_logged(
        phase_two(apps, {"dfe-receiver": ["2", "3", "2"], "dfe-loader": ["2", "3", "2"]}),
        DFE_KEDA_REAL_DEPLOYMENT="dfe-loader",
    )

    expect("it passes", out.returncode == 0, f"rc={out.returncode} {out.stdout}")
    expect(
        "the receiver was left alone",
        not [s for s in inserts(execs) if "'dfe-receiver'" in s],
        out.stdout,
    )


def run_runner(codes: dict[str, int], **env: str) -> subprocess.CompletedProcess:
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
            env={**os.environ, **env},
        )


def test_the_runner_prints_unproven_as_its_own_word_and_fails() -> None:
    """A suite exiting 3 is named UNPROVEN, counted apart, and fails the run."""
    out = run_runner({"keda-scale-test.sh": UNPROVEN})

    expect("the suite reads UNPROVEN", "UNPROVEN -- it ran" in out.stdout, out.stdout)
    expect("counted apart", "0 failed, 1 unproven" in out.stdout, out.stdout)
    expect("and it fails the deploy", out.returncode == 1, f"rc={out.returncode}")


def test_a_failing_suite_still_fails_the_run() -> None:
    """The UNPROVEN verdict must not have changed what a FAIL does."""
    out = run_runner({"keda-scale-test.sh": 1})

    expect("it reads FAILED", "FAILED" in out.stdout, out.stdout)
    expect("counted as a failure", "1 failed, 0 unproven" in out.stdout, out.stdout)
    expect("and the run fails", out.returncode == 1, f"rc={out.returncode}")


def test_a_disabled_scale_proof_is_not_run() -> None:
    """DFE_KEDA_SCALE_TEST=0 is the fast smoke run, so the proof's verdict cannot reach it."""
    out = run_runner({"keda-scale-test.sh": UNPROVEN}, DFE_KEDA_SCALE_TEST="0")

    expect("the proof did not run", "KEDA scale proof" not in out.stdout, out.stdout)
    expect("and the run passes", out.returncode == 0, f"rc={out.returncode}")


def test_every_suite_passing_passes_the_run() -> None:
    """The control: nothing failed, nothing unproven."""
    out = run_runner({}, DFE_KEDA_SCALE_TEST="1")

    expect("0 failed, 0 unproven", "0 failed, 0 unproven" in out.stdout, out.stdout)
    expect("and the run passes", out.returncode == 0, f"rc={out.returncode}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
