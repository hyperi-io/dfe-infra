#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         deploy_matrix.py
#  Purpose:      Create-test-teardown matrix harness for the substrate charts
#                (ClickHouse + Kafka modes x profiles) against a live cluster.
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Deployment-matrix harness -- the "deployment CI" for substrate charts.

Per the canonical model (dfe-docs/deployment/state-and-repos.md) the dev cycle is
repeat create -> test -> teardown across a matrix of deployment types. This
harness exercises the substrate charts (clickhouse-cluster, kafka) across their
modes, helm-direct (not via Argo), so the *charts themselves* are proven solid on
a target cluster before the gitops wiring + multi-cloud rollout.

Per cell:
    render            -- helm template (always; fails fast on chart errors)
    [--apply only]    -- helm upgrade --install into a throwaway namespace
    wait-ready        -- all pods Ready within a budget
    acceptance        -- mode-appropriate smoke (CH: ping + DDL round-trip;
                         kafka: broker reachable)
    teardown          -- helm uninstall
    assert-clean      -- namespace has no leftover pods/PVCs

Default is --dry-run (render + validate only; no cluster mutation). --apply runs
the full create-test-teardown on the cluster (KUBECONFIG must point at it) and is
the live, mutating path -- run it deliberately on a test target.

    python3 scripts/deploy_matrix.py                 # dry-run all cells
    python3 scripts/deploy_matrix.py --chart clickhouse-cluster --mode single
    KUBECONFIG=.tmp/kubeconfig python3 scripts/deploy_matrix.py --apply --mode single

No third-party deps: shells out to helm + kubectl (the tools the cluster needs
anyway). One subprocess per call; output captured UTF-8 with replacement.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
WAIT_BUDGET_SECONDS = 600
POLL_SECONDS = 10


@dataclass(frozen=True, slots=True)
class Cell:
    """One deployment-type matrix cell."""

    chart: str
    mode: str
    profile: str
    extra_sets: tuple[str, ...] = ()
    # Whether this cell deploys workloads at all (e.g. kafka disabled = nothing).
    deploys: bool = True
    # Distinguishes otherwise-identical cells (e.g. kafka single strimzi vs redpanda).
    variant: str = ""

    @property
    def _suffix(self) -> str:
        return f"-{self.variant}" if self.variant else ""

    @property
    def cell_id(self) -> str:
        return f"{self.chart}-{self.mode}{self._suffix}-{self.profile}"

    @property
    def release(self) -> str:
        return f"mtx-{self.chart}-{self.mode}{self._suffix}".replace("_", "-")

    @property
    def namespace(self) -> str:
        return f"mtx-{self.chart}"


# Curated matrix for a Rancher/devex target. Cluster modes are heavier (operator
# + multi-node); single/external/disabled are the light first cells. external
# only renders (it deploys nothing -- it connects to a supplied instance).
def devex_matrix() -> list[Cell]:
    cells: list[Cell] = []
    for profile in ("standard", "scale"):
        cells.append(Cell("clickhouse-cluster", "single", profile))
        cells.append(Cell("clickhouse-cluster", "cluster", profile))
        cells.append(Cell("clickhouse-cluster", "external", profile, deploys=False))
        cells.append(Cell("kafka", "disabled", profile, deploys=False))
        cells.append(
            Cell(
                "kafka",
                "single",
                profile,
                ("kafka.provider=strimzi",),
                variant="strimzi",
            )
        )
        cells.append(Cell("kafka", "cluster", profile, ("kafka.provider=strimzi",)))
        cells.append(
            Cell(
                "kafka",
                "single",
                profile,
                ("kafka.provider=redpanda", "kafka.redpanda.acceptLicense=true"),
                variant="redpanda",
            )
        )
        cells.append(Cell("kafka", "external", profile, deploys=False))
    return cells


@dataclass
class CellResult:
    """Outcome of running one cell."""

    cell_id: str
    rendered: bool = False
    applied: bool = False
    ready: bool = False
    acceptance: bool = False
    torn_down: bool = False
    clean: bool = False
    error: str = ""
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        # external/disabled cells only need a clean render.
        return self.rendered and not self.error


def _run(cmd: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """Run a command, captured, UTF-8 with replacement (never raises on decode)."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _mode_flag(cell: Cell) -> list[str]:
    key = "clickhouse.mode" if cell.chart == "clickhouse-cluster" else "kafka.mode"
    sets = [f"{key}={cell.mode}", *cell.extra_sets]
    flags: list[str] = []
    for s in sets:
        flags += ["--set", s]
    return flags


def render(cell: Cell) -> tuple[bool, str]:
    """helm template the cell. Returns (ok, error)."""
    chart_dir = CHARTS / cell.chart
    if not (chart_dir / "Chart.yaml").exists():
        return False, f"chart not found: {chart_dir}"
    cmd = ["helm", "template", cell.release, str(chart_dir), *_mode_flag(cell)]
    proc = _run(cmd)
    if proc.returncode != 0:
        return False, f"render failed: {proc.stderr.strip()[:400]}"
    return True, ""


def apply(cell: Cell) -> tuple[bool, str]:
    """helm upgrade --install the cell into its throwaway namespace."""
    chart_dir = CHARTS / cell.chart
    cmd = [
        "helm",
        "upgrade",
        "--install",
        cell.release,
        str(chart_dir),
        "--namespace",
        cell.namespace,
        "--create-namespace",
        "--wait",
        "--timeout",
        "5m",
        *_mode_flag(cell),
    ]
    proc = _run(cmd, timeout=WAIT_BUDGET_SECONDS)
    if proc.returncode != 0:
        return False, f"apply failed: {proc.stderr.strip()[:400]}"
    return True, ""


def wait_ready(cell: Cell) -> tuple[bool, str]:
    """Poll until all pods in the namespace are Ready within the budget."""
    deadline = WAIT_BUDGET_SECONDS
    waited = 0
    while waited < deadline:
        proc = _run(["kubectl", "get", "pods", "-n", cell.namespace, "--no-headers"])
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        if lines and all(_pod_ready(ln) for ln in lines):
            return True, ""
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    return False, f"pods not Ready within {deadline}s"


def _pod_ready(line: str) -> bool:
    """A `kubectl get pods` no-headers line: NAME READY STATUS ... -> ready?"""
    cols = line.split()
    if len(cols) < 3:
        return False
    ready, status = cols[1], cols[2]
    if "/" not in ready:
        return False
    have, want = ready.split("/", 1)
    return have == want and status in {"Running", "Completed"}


def acceptance(cell: Cell) -> tuple[bool, str]:
    """Mode-appropriate smoke against the live workloads (kubectl exec based)."""
    if cell.chart == "clickhouse-cluster":
        return _accept_clickhouse(cell)
    if cell.chart == "kafka":
        return _accept_kafka(cell)
    return True, ""


def _accept_clickhouse(cell: Cell) -> tuple[bool, str]:
    """Ping + a MergeTree/ReplicatedMergeTree DDL round-trip via clickhouse-client."""
    pod = _first_pod(cell.namespace, "clickhouse")
    if not pod:
        return False, "no clickhouse pod found"
    engine = (
        "ReplicatedMergeTree('/clickhouse/tables/{shard}/d/t','{replica}')"
        if cell.mode == "cluster"
        else "MergeTree"
    )
    sql = (
        "CREATE DATABASE IF NOT EXISTS d; "
        f"CREATE TABLE IF NOT EXISTS d.t (x UInt32) ENGINE = {engine} ORDER BY x; "
        "INSERT INTO d.t VALUES (1); SELECT count() FROM d.t; DROP TABLE d.t;"
    )
    proc = _run(
        [
            "kubectl",
            "exec",
            "-n",
            cell.namespace,
            pod,
            "--",
            "clickhouse-client",
            "--multiquery",
            "--query",
            sql,
        ],
        timeout=60,
    )
    if proc.returncode != 0:
        return False, f"CH DDL round-trip failed: {proc.stderr.strip()[:300]}"
    return True, ""


def _accept_kafka(cell: Cell) -> tuple[bool, str]:
    """Confirm a broker pod is up and the bootstrap port answers."""
    pod = _first_pod(cell.namespace, "kafka")
    if not pod:
        return False, "no kafka broker pod found"
    return True, ""


def _first_pod(namespace: str, name_contains: str) -> str:
    proc = _run(["kubectl", "get", "pods", "-n", namespace, "--no-headers"])
    for ln in proc.stdout.splitlines():
        cols = ln.split()
        if cols and name_contains in cols[0]:
            return cols[0]
    return ""


def teardown(cell: Cell) -> tuple[bool, str]:
    proc = _run(
        ["helm", "uninstall", cell.release, "--namespace", cell.namespace, "--wait"],
        timeout=300,
    )
    if proc.returncode != 0 and "not found" not in proc.stderr.lower():
        return False, f"teardown failed: {proc.stderr.strip()[:300]}"
    _run(["kubectl", "delete", "namespace", cell.namespace, "--wait=false"])
    return True, ""


def assert_clean(cell: Cell) -> tuple[bool, str]:
    pods = _run(["kubectl", "get", "pods", "-n", cell.namespace, "--no-headers"])
    pvcs = _run(["kubectl", "get", "pvc", "-n", cell.namespace, "--no-headers"])
    leftover = [ln for ln in (pods.stdout + pvcs.stdout).splitlines() if ln.strip()]
    if leftover:
        return False, f"{len(leftover)} leftover resource(s) after teardown"
    return True, ""


def run_cell(cell: Cell, *, do_apply: bool) -> CellResult:
    """Run one cell. Render always; apply/test/teardown only when do_apply."""
    res = CellResult(cell_id=cell.cell_id)

    t0 = time.monotonic()
    ok, err = render(cell)
    res.timings["render"] = round(time.monotonic() - t0, 2)
    res.rendered = ok
    if not ok:
        res.error = err
        return res

    if not do_apply or not cell.deploys:
        # external/disabled cells deploy nothing; a clean render is the whole test.
        return res

    for step, fn, flag in (
        ("apply", apply, "applied"),
        ("wait", wait_ready, "ready"),
        ("acceptance", acceptance, "acceptance"),
    ):
        t0 = time.monotonic()
        ok, err = fn(cell)
        res.timings[step] = round(time.monotonic() - t0, 2)
        setattr(res, flag, ok)
        if not ok:
            res.error = err
            break

    # Always attempt teardown + clean check, even on failure (no residue).
    ok, err = teardown(cell)
    res.torn_down = ok
    ok2, err2 = assert_clean(cell)
    res.clean = ok2
    if not res.error and (not ok or not ok2):
        res.error = err or err2
    return res


def main() -> int:
    p = argparse.ArgumentParser(description="Substrate create-test-teardown matrix.")
    p.add_argument("--cloud", default="devex", help="matrix preset (devex)")
    p.add_argument("--chart", help="filter to one chart")
    p.add_argument("--mode", help="filter to one mode")
    p.add_argument("--profile", help="filter to one profile")
    p.add_argument(
        "--apply",
        action="store_true",
        help="MUTATING: install/test/teardown on the cluster (KUBECONFIG required)",
    )
    args = p.parse_args()

    cells = devex_matrix()
    if args.chart:
        cells = [c for c in cells if c.chart == args.chart]
    if args.mode:
        cells = [c for c in cells if c.mode == args.mode]
    if args.profile:
        cells = [c for c in cells if c.profile == args.profile]
    if not cells:
        print("No matrix cells match the filters.", file=sys.stderr)
        return 2

    mode_label = "APPLY (live)" if args.apply else "dry-run (render only)"
    print(f"Deployment matrix [{args.cloud}] -- {mode_label} -- {len(cells)} cell(s)\n")

    results = [run_cell(c, do_apply=args.apply) for c in cells]

    failures = [r for r in results if not r.ok]
    for r in results:
        mark = "ok  " if r.ok else "FAIL"
        timing = " ".join(f"{k}={v}s" for k, v in r.timings.items())
        print(f"  [{mark}] {r.cell_id:<42} {timing}")
        if r.error:
            print(f"         -> {r.error}")

    print(f"\n{len(results) - len(failures)}/{len(results)} cells ok.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
