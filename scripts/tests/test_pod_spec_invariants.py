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
   It was live-proven across engine v1.10.4/5/6, and the first fix "claimed to
   kill the whole CLASS while being applied to one Deployment in a chart that
   ships three". A sixth pod was
   still missing it in 2026-09. The class was right every time; its membership
   was written out by hand every time.

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

Needs `helm` on PATH. Runs under pytest too, which is how CI reaches it.

A chart this cascade cannot render is asserted against UNRENDERABLE rather than
counted and printed: under pytest a printed skip is invisible, and a skip that
reads like a pass is how a gate stops being one.
"""

import subprocess
import sys
from functools import cache
from pathlib import Path

import yaml

from _charts import CHART_TREES
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
EXTRAS = REPO_ROOT / "helm" / "charts" / "dfe-extras"

BASE_CASCADE = [VALUES / "common.yaml", VALUES / "local.yaml"]

# Workload kinds whose pod template we assert on.
POD_PARENTS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "ReplicaSet"}

# Charts this cascade cannot reach, and why. A chart that stops rendering without
# being named here fails the coverage check rather than dropping out quietly.
UNRENDERABLE = {
    # culvert's objects fail its own listeners guard: the cascade's listeners are
    # the receiver's, and a server accepting no tunnel is a deliberate render error.
    "dfe-extras/culvert",
}


def render(chart: Path, *sets: str, layers: tuple[Path, ...] = ()) -> list[dict] | None:
    """Rendered docs, or None when the chart needs values this cascade lacks."""
    cmd = ["helm", "template", chart.name, str(chart)]
    for v in (*BASE_CASCADE, *layers):
        if v.exists():
            cmd += ["-f", str(v)]
    # The appsets pass these as helm parameters rather than values files; some
    # charts refuse to render without them.
    cmd += ["--set", "appNamespace=dfe-local", "--set", "kafka.mode=single"]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if out.returncode != 0:
        return None
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def extras_components() -> list[str]:
    """The components dfe-extras renders objects for, each named by its chartName."""
    values = yaml.safe_load((EXTRAS / "values.yaml").read_text(encoding="utf-8"))
    return sorted(values["extras"]["appVersions"])


def integration_values(component: str) -> tuple[Path, ...]:
    """The per-app layers layer2-apps.yaml adds for a component on the local cloud."""
    service = "hyperdx" if component == "dfe-hyperdx" else component
    apps = VALUES / "apps"
    return (apps / "_common.yaml", apps / service / "values.yaml", apps / service / "local.yaml")


@cache
def rendered() -> tuple[tuple[tuple[str, tuple[dict, ...]], ...], tuple[str, ...]]:
    """Every deployed chart rendered once: (chart, docs) pairs, plus the names that would not.

    dfe-extras renders nothing without a chartName, so it is rendered once per
    component, over that component's integration values, each entry named
    dfe-extras/<component>.
    """
    covered: list[tuple[str, tuple[dict, ...]]] = []
    skipped: list[str] = []
    charts = [p for root in CHART_TREES for p in root.iterdir() if (p / "Chart.yaml").exists()]
    renders = [(c.name, c, (), ()) for c in charts if c != EXTRAS]
    renders += [
        (f"dfe-extras/{c}", EXTRAS, (f"chartName={c}",), integration_values(c))
        for c in extras_components()
    ]
    for name, chart, sets, layers in sorted(renders):
        docs = render(chart, *sets, layers=layers)
        if docs is None:
            skipped.append(name)
        else:
            covered.append((name, tuple(docs)))
    return tuple(covered), tuple(skipped)


def pod_templates(docs: tuple[dict, ...]) -> list[tuple[str, str, dict]]:
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


def pod_labels(docs: tuple[dict, ...], kind: str) -> list[tuple[str, dict]]:
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


def test_dfe_pods_disable_service_links() -> None:
    covered, _ = rendered()
    for chart, docs in covered:
        for kind, name, spec in pod_templates(docs):
            if not is_dfe_image(spec):
                continue
            expect(
                f"{chart}: {kind}/{name} sets enableServiceLinks: false",
                spec.get("enableServiceLinks") is False,
                "a Service whose name uppercases onto a DFE_* setting will shadow it",
            )


def test_no_job_pod_matches_a_service() -> None:
    covered, _ = rendered()
    for chart, docs in covered:
        selectors = [
            (
                (d.get("metadata") or {}).get("name", "?"),
                (d.get("spec") or {}).get("selector") or {},
            )
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


def test_every_chart_is_covered_or_declared() -> None:
    """A chart that drops out of the render is a hole in both invariants above."""
    covered, skipped = rendered()
    expect(
        "only the declared charts fail to render with the standard cascade",
        set(skipped) == UNRENDERABLE,
        f"skipped {sorted(skipped)}, declared {sorted(UNRENDERABLE)}",
    )
    expect("the render reached something to check", len(covered) > 1, f"got {len(covered)}")


def main() -> int:
    with standalone():
        test_dfe_pods_disable_service_links()
        test_no_job_pod_matches_a_service()
        test_every_chart_is_covered_or_declared()
        covered, skipped = rendered()
        print(f"\nchecked {len(covered)} chart(s), {len(skipped)} not rendered: {sorted(skipped)}")
        return summary()


if __name__ == "__main__":
    sys.exit(main())
