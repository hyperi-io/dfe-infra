#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_pod_spec_invariants.py
#  Purpose:      Assert two pod-spec invariants across every chart, so the classes
#                stop being enumerated by hand and re-broken one pod at a time.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Two invariants that have each been broken more than once by enumeration.

1. **Every pod running a DFE image sets `enableServiceLinks: false`.**
   Kubernetes injects a legacy Docker-link variable per Service in the
   namespace, so `svc/dfe-keda-shim` becomes
   `DFE_KEDA_SHIM_PORT="tcp://<ip>:8080"`. The engine reads that name as its own
   setting and parses it as an int (`settings.py`), so the pod dies at startup.
   The dfe-engine chart's comment calls it live-proven across v1.10.4/5/6, and
   the hunt-runner's says the first fix "claimed to kill the whole CLASS while
   being applied to one Deployment in a chart that ships three". A sixth pod --
   the dfe-schema Job -- was still missing it in 2026-09. The class was right
   every time; its membership was written out by hand every time.

2. **No Job's pod carries labels that match a Service's selector.**
   A Service selects a pod when the pod's labels are a superset of the
   selector, so a Job that reuses `dfe-common.selectorLabels` joins the
   endpoints of the Service that selects on them. The Kafka bootstrap Job did
   exactly that to the headless broker Service: no readiness probe, so Ready
   from start, in the endpoint set for the whole broker wait plus every topic
   create, listening on nothing -- while receiver and loader were resolving that
   same address for the first time.

Both are structural and cheap to assert against rendered output, which is the
point: a check the renderer runs cannot be forgotten the way a list can.

    python3 scripts/tests/test_pod_spec_invariants.py

Needs `helm` on PATH. No test runner, matching the other checks here.

A chart that cannot render with the standard cascade is REPORTED and counted,
never silently passed -- the run prints what it actually covered.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
VALUES = REPO_ROOT / "argocd" / "values"

BASE_CASCADE = [VALUES / "common.yaml", VALUES / "local.yaml"]

# Workload kinds whose pod template we assert on.
POD_PARENTS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "ReplicaSet"}

_failures = 0
_skipped: list[str] = []


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


def render(chart: Path) -> list[dict] | None:
    """Rendered docs, or None when the chart needs values this cascade lacks."""
    cmd = ["helm", "template", chart.name, str(chart)]
    for v in BASE_CASCADE:
        if v.exists():
            cmd += ["-f", str(v)]
    # The appsets pass these as helm parameters rather than values files; some
    # charts refuse to render without them.
    cmd += ["--set", "appNamespace=dfe-local", "--set", "kafka.mode=single"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        _skipped.append(chart.name)
        return None
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def pod_templates(docs: list[dict]) -> list[tuple[str, str, dict]]:
    """(kind, name, podSpec) for every workload that owns a pod template."""
    found = []
    for d in docs:
        if d.get("kind") not in POD_PARENTS:
            continue
        tmpl = (d.get("spec") or {}).get("template") or {}
        spec = tmpl.get("spec")
        if isinstance(spec, dict):
            found.append((d["kind"], (d.get("metadata") or {}).get("name", "?"), spec))
    return found


def pod_labels(docs: list[dict], kind: str) -> list[tuple[str, dict]]:
    out = []
    for d in docs:
        if d.get("kind") != kind:
            continue
        tmpl = (d.get("spec") or {}).get("template") or {}
        labels = (tmpl.get("metadata") or {}).get("labels") or {}
        out.append(((d.get("metadata") or {}).get("name", "?"), labels))
    return out


def is_dfe_image(spec: dict) -> bool:
    """A pod running one of our own images, which read DFE_* config."""
    containers = (spec.get("containers") or []) + (spec.get("initContainers") or [])
    return any("dfe-" in (c.get("image") or "") for c in containers)


def test_dfe_pods_disable_service_links(chart: str, docs: list[dict]) -> None:
    for kind, name, spec in pod_templates(docs):
        if not is_dfe_image(spec):
            continue
        expect(
            f"{chart}: {kind}/{name} sets enableServiceLinks: false",
            spec.get("enableServiceLinks") is False,
            "a Service whose name uppercases onto a DFE_* setting will shadow it",
        )


def test_no_job_pod_matches_a_service(chart: str, docs: list[dict]) -> None:
    selectors = [
        ((d.get("metadata") or {}).get("name", "?"), (d.get("spec") or {}).get("selector") or {})
        for d in docs
        if d.get("kind") == "Service"
    ]
    for job_name, labels in pod_labels(docs, "Job"):
        for svc_name, sel in selectors:
            if not sel:
                continue
            matched = all(labels.get(k) == v for k, v in sel.items())
            expect(
                f"{chart}: Job/{job_name} is not selected by svc/{svc_name}",
                not matched,
                "the Job's pod joins that Service's endpoints while it runs",
            )


def main() -> int:
    charts = sorted(p for p in CHARTS.iterdir() if (p / "Chart.yaml").exists())
    checked = 0
    for chart in charts:
        docs = render(chart)
        if docs is None:
            continue
        checked += 1
        test_dfe_pods_disable_service_links(chart.name, docs)
        test_no_job_pod_matches_a_service(chart.name, docs)

    print(f"\nchecked {checked} chart(s) of {len(charts)}")
    if _skipped:
        # Loud on purpose. A skip that reads like a pass is how a gate stops
        # being one.
        print(f"SKIPPED (would not render with the standard cascade): {', '.join(_skipped)}")
    print("FAILURES" if _failures else "OK", _failures or "")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
