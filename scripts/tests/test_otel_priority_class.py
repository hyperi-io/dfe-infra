#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_otel_priority_class.py
#  Purpose:      Prove the collector's node agent outranks the app pods on a
#                full node, and that the class it names exists before it does.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The collector daemonset preempts an app pod rather than pending on a full node.

A DaemonSet pod is bound to one node, so when every CPU on that node is
requested it waits until something there leaves. With no class it ranks 0,
level with every DFE app, and the scheduler reports "No preemption victims found
for incoming pod". The chart names a class for the daemonset, creates that class,
and under Argo creates it in an earlier wave: Argo applies a kind it does not
order after the DaemonSet, and a pod naming an absent class is refused.

    python3 scripts/tests/test_otel_priority_class.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "otel-collector"

# A user-defined class may not exceed this; the system-* classes sit above it.
USER_CLASS_CEILING = 1_000_000_000
WAVE = "argocd.argoproj.io/sync-wave"


def render(*sets: str) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "otel", str(CHART)]
    for s in sets:
        cmd += ["--set", s]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def rendered(*sets: str) -> list[dict]:
    out = render(*sets)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for otel-collector:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def test_the_daemonset_names_the_node_agent_class() -> None:
    pod = of_kind(rendered(), "DaemonSet")[0]["spec"]["template"]["spec"]
    expect(
        "the daemonset pod carries priorityClassName dfe-node-critical",
        pod.get("priorityClassName") == "dfe-node-critical",
        repr(pod.get("priorityClassName")),
    )


def test_the_chart_creates_the_class_it_names() -> None:
    classes = of_kind(rendered(), "PriorityClass")
    expect("exactly one PriorityClass renders", len(classes) == 1, f"got {len(classes)}")
    if not classes:
        return
    pc = classes[0]
    expect("it is scheduling.k8s.io/v1", pc.get("apiVersion") == "scheduling.k8s.io/v1", pc.get("apiVersion"))
    expect("it is the class the daemonset names", pc["metadata"]["name"] == "dfe-node-critical", pc["metadata"])
    value = pc.get("value", 0)
    expect("it outranks a pod that names no class (0)", isinstance(value, int) and value > 0, repr(value))
    expect(
        "it stays under the user-defined ceiling, so system-* classes still win",
        isinstance(value, int) and value <= USER_CLASS_CEILING,
        repr(value),
    )
    expect(
        "it preempts lower-priority pods",
        pc.get("preemptionPolicy") == "PreemptLowerPriority",
        repr(pc.get("preemptionPolicy")),
    )
    expect("it is never the cluster's default class", pc.get("globalDefault") is not True, repr(pc))


def test_argo_creates_the_class_before_the_daemonset() -> None:
    docs = rendered()
    classes = of_kind(docs, "PriorityClass")
    daemonset = of_kind(docs, "DaemonSet")[0]
    pc_wave = int((classes[0]["metadata"].get("annotations") or {}).get(WAVE, "0")) if classes else 0
    ds_wave = int((daemonset["metadata"].get("annotations") or {}).get(WAVE, "0"))
    expect("the class syncs in an earlier wave than the daemonset", pc_wave < ds_wave, f"{pc_wave} vs {ds_wave}")


def test_a_cluster_with_its_own_class_turns_creation_off() -> None:
    docs = rendered("daemonset.priorityClass.create=false", "daemonset.priorityClass.name=org-node-agents")
    expect("no PriorityClass renders", not of_kind(docs, "PriorityClass"), "")
    pod = of_kind(docs, "DaemonSet")[0]["spec"]["template"]["spec"]
    expect(
        "the daemonset names the cluster's own class",
        pod.get("priorityClassName") == "org-node-agents",
        repr(pod.get("priorityClassName")),
    )


def test_a_system_prefixed_name_is_refused_when_the_chart_creates_it() -> None:
    out = render("daemonset.priorityClass.name=system-node-critical")
    expect("the render fails", out.returncode != 0, out.stdout[:200])
    expect("it names the field", "daemonset.priorityClass.name" in out.stderr, out.stderr[-400:])


def main() -> int:
    with standalone():
        test_the_daemonset_names_the_node_agent_class()
        test_the_chart_creates_the_class_it_names()
        test_argo_creates_the_class_before_the_daemonset()
        test_a_cluster_with_its_own_class_turns_creation_off()
        test_a_system_prefixed_name_is_refused_when_the_chart_creates_it()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
