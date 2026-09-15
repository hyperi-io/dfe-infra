#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_pod_security_context.py
#  Purpose:      Prove dfe-common.podSecurityContext renders the hardened
#                defaults, honours an explicit uid/gid of 0, and disappears
#                entirely when a chart manages its own securityContext.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One pod-level securityContext, expressible for every workload.

The helper reads runAsUser/runAsGroup/fsGroup with hasKey rather than `default`,
because 0 is falsy: `default 1000` silently claimed an explicit 0, so a chart
that had to run a workload as root could not say so through the helper and wrote
its own securityContext instead -- a second copy of the hardening, and one that
the container-level drop-ALL never reached.

    python3 scripts/tests/test_pod_security_context.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
LIBRARY_HARNESS = REPO_ROOT / "helm" / "library" / "dfe-common" / "tests" / "lint-test"

# Third-party images whose entrypoint owns the uid, so the chart states an
# fsGroup and leaves runAsUser unset -- a shape the helper cannot express.
FSGROUP_ONLY = {
    "helm/charts/clickhouse-cluster/templates/clickhouse-single.yaml",  # clickhouse uid 101
    "helm/charts/ferretdb/templates/documentdb.yaml",  # postgres uid 999, gosu drop
    "helm/charts/forgejo/templates/deployment.yaml",  # forgejo uid 1000, chown init
}


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


def render(chart_dir: Path, name: str, *args: str) -> str:
    cmd = ["helm", "template", name, str(chart_dir), *args]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {name} {args}:\n{out.stderr}")
    return out.stdout


def docs(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if d]


def pod_spec(*args: str) -> dict:
    """The harness Deployment's pod spec under the given --set overrides."""
    build_harness_deps()
    for doc in docs(render(LIBRARY_HARNESS, "lt", *args)):
        if doc.get("kind") == "Deployment":
            return doc["spec"]["template"]["spec"]
    raise SystemExit("the library harness rendered no Deployment")


def test_the_defaults_are_the_hardened_ones() -> None:
    sc = pod_spec().get("securityContext", {})
    expect("runAsNonRoot by default", sc.get("runAsNonRoot") is True, repr(sc.get("runAsNonRoot")))
    expect("uid 1000 by default", sc.get("runAsUser") == 1000, repr(sc.get("runAsUser")))
    expect("gid 1000 by default", sc.get("runAsGroup") == 1000, repr(sc.get("runAsGroup")))
    expect("fsGroup 1000 by default", sc.get("fsGroup") == 1000, repr(sc.get("fsGroup")))
    expect(
        "seccomp RuntimeDefault by default",
        sc.get("seccompProfile", {}).get("type") == "RuntimeDefault",
        repr(sc.get("seccompProfile")),
    )


def test_an_explicit_zero_renders() -> None:
    """0 is falsey in a Helm `default`, so it needs hasKey to survive."""
    sc = pod_spec(
        "--set", "podSecurityContext.runAsNonRoot=false",
        "--set", "podSecurityContext.runAsUser=0",
        "--set", "podSecurityContext.runAsGroup=0",
        "--set", "podSecurityContext.fsGroup=0",
    ).get("securityContext", {})
    expect("runAsUser 0 survives", sc.get("runAsUser") == 0, repr(sc.get("runAsUser")))
    expect("runAsGroup 0 survives", sc.get("runAsGroup") == 0, repr(sc.get("runAsGroup")))
    expect("fsGroup 0 survives", sc.get("fsGroup") == 0, repr(sc.get("fsGroup")))
    expect("runAsNonRoot false survives", sc.get("runAsNonRoot") is False, repr(sc.get("runAsNonRoot")))


def test_a_non_default_uid_still_renders() -> None:
    """The kafbat/otel-collector case: a truthy override was never the problem."""
    sc = pod_spec("--set", "podSecurityContext.runAsUser=100").get("securityContext", {})
    expect("uid 100 renders", sc.get("runAsUser") == 100, repr(sc.get("runAsUser")))


def test_disabled_renders_no_pod_security_context() -> None:
    """culvert manages its own; the helper must then emit nothing at all."""
    spec = pod_spec("--set", "podSecurityContext.enabled=false")
    expect("no pod securityContext", "securityContext" not in spec, repr(spec.get("securityContext")))


def test_the_container_context_drops_every_capability() -> None:
    container = pod_spec()["containers"][0]
    sc = container.get("securityContext", {})
    expect("ALL dropped", sc.get("capabilities", {}).get("drop") == ["ALL"], repr(sc.get("capabilities")))
    expect("no privilege escalation", sc.get("allowPrivilegeEscalation") is False, repr(sc))
    expect("read-only rootfs", sc.get("readOnlyRootFilesystem") is True, repr(sc))


def test_the_root_workloads_go_through_the_helper() -> None:
    """culvert and the log daemonset are the two pods that must run as uid 0."""
    for chart, template, name in (
        ("culvert", "templates/deployment.yaml", "culvert"),
        ("otel-collector", "templates/daemonset.yaml", "otel"),
    ):
        text = render(CHARTS / chart, name, "--show-only", template)
        sc = docs(text)[0]["spec"]["template"]["spec"]["securityContext"]
        expect(f"{chart} runs as root", sc.get("runAsUser") == 0, repr(sc))
        expect(f"{chart} says so", sc.get("runAsNonRoot") is False, repr(sc))
        expect(
            f"{chart} keeps the shared seccomp profile",
            sc.get("seccompProfile", {}).get("type") == "RuntimeDefault",
            repr(sc),
        )


def test_no_chart_writes_its_own_pod_security_context() -> None:
    """A hand-written pod securityContext is a second copy of the hardening.

    Pod level is the six-space `securityContext:` directly under a pod spec; a
    container's own block sits deeper and is a different concern.
    """
    offenders = [
        f"{t.relative_to(REPO_ROOT)}:{n}"
        for t in sorted(CHARTS.glob("*/templates/**/*.yaml"))
        if str(t.relative_to(REPO_ROOT)) not in FSGROUP_ONLY
        for n, line in enumerate(t.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if line == "      securityContext:"
    ]
    expect("the hardening lives in one helper", offenders == [], f"got {offenders}")


def main() -> int:
    with standalone():
        test_the_defaults_are_the_hardened_ones()
        test_an_explicit_zero_renders()
        test_a_non_default_uid_still_renders()
        test_disabled_renders_no_pod_security_context()
        test_the_container_context_drops_every_capability()
        test_the_root_workloads_go_through_the_helper()
        test_no_chart_writes_its_own_pod_security_context()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
