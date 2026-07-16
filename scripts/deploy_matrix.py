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
import base64
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
WAIT_BUDGET_SECONDS = 600
POLL_SECONDS = 10
# Optional label for CONCURRENT matrix runs. Each run is its own process with its
# own INSTANCE, so `--instance a` and `--instance b` get disjoint ns/release
# prefixes (mtx-a-* vs mtx-b-*) and never collide -- the isolation test for
# "multiple concurrent deploys". Empty = the default single-run prefix (mtx-*).
INSTANCE = ""


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
        pre = f"-{INSTANCE}" if INSTANCE else ""
        return f"mtx{pre}-{self.chart}-{self.mode}{self._suffix}".replace("_", "-")

    @property
    def namespace(self) -> str:
        pre = f"-{INSTANCE}" if INSTANCE else ""
        return f"mtx{pre}-{self.chart}"


# Curated matrix for a Rancher/devex target. Cluster modes are heavier (operator
# + multi-node); single/external/disabled are the light first cells. external
# only renders (it deploys nothing -- it connects to a supplied instance).
def devex_matrix() -> list[Cell]:
    cells: list[Cell] = []
    for profile in ("slim", "scale"):
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
        cells.append(
            Cell(
                "kafka",
                "cluster",
                profile,
                ("kafka.provider=strimzi",),
                variant="strimzi",
            )
        )
        cells.append(
            Cell(
                "kafka",
                "single",
                profile,
                ("kafka.provider=redpanda", "kafka.redpanda.acceptLicense=true"),
                variant="redpanda",
            )
        )
        cells.append(
            Cell(
                "kafka",
                "cluster",
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
    """Run a command, captured, UTF-8 with replacement.

    Never raises: a decode issue is replaced, and a timeout is converted to a
    non-zero CompletedProcess so a hung step (e.g. an auth-blocked kubectl exec)
    becomes a normal cell failure -- it must NOT crash the run and skip teardown.
    """
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        return subprocess.CompletedProcess(
            cmd, returncode=124, stdout=out, stderr=f"timed out after {timeout}s"
        )


def _mode_flag(cell: Cell) -> list[str]:
    """Helm value args: the profile valueFile (so profiles differ) + mode --sets."""
    key = "clickhouse.mode" if cell.chart == "clickhouse-cluster" else "kafka.mode"
    flags: list[str] = []
    profile_file = REPO_ROOT / "argocd" / "values" / f"profile-{cell.profile}.yaml"
    if profile_file.exists():
        flags += ["-f", str(profile_file)]
    # --set wins over the profile file, so the cell's mode is authoritative.
    for s in [f"{key}={cell.mode}", *cell.extra_sets]:
        flags += ["--set", s]
    return flags


def provision_test_secrets(cell: Cell) -> tuple[bool, str]:
    """Create throwaway namespace + secrets the chart expects, so pods can start.

    Real deployments get these from ESO/Vault; a matrix test stands up dummies so
    the create-test-teardown can exercise the chart without the secrets backend.
    Idempotent (ignores already-exists). Cleaned up by namespace delete at teardown.
    """
    _run(["kubectl", "create", "namespace", cell.namespace], timeout=30)  # ok if exists
    if cell.chart == "clickhouse-cluster":
        proc = _run(
            [
                "kubectl",
                "create",
                "secret",
                "generic",
                "clickhouse-admin-password",
                "--from-literal=password=matrixtest",
                "-n",
                cell.namespace,
            ],
            timeout=30,
        )
        if proc.returncode != 0 and "already exists" not in proc.stderr.lower():
            return False, f"test secret failed: {proc.stderr.strip()[:300]}"
    return True, ""


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


def _expected_workloads(cell: Cell) -> list[str]:
    """Pod-name substrings that MUST be present-and-ready for this cell.

    Defends against operator wave-reconcile false positives: in cluster mode the
    ClickHouse operator creates Keeper pods before ClickHouse pods, so a naive
    'all existing pods ready' check passes during the keeper-only window before
    ClickHouse even exists. Requiring each expected workload by name closes that.
    external/disabled deploy nothing -> empty list -> nothing to wait for.
    """
    if cell.chart == "clickhouse-cluster":
        if cell.mode == "cluster":
            return ["clickhouse", "keeper"]
        if cell.mode == "single":
            return ["clickhouse"]
        return []
    if cell.chart == "kafka":
        if cell.mode not in {"single", "cluster"}:
            return []
        # single = a plain StatefulSet of the chosen provider (mirrors CH-single,
        # per kafka-single.yaml) -- NO operator, so no nodepool/entity-operator
        # pods ever appear. Only cluster+strimzi is operator-managed with a broker
        # nodepool (*-pool-*) + the entity-operator (User Operator that mints the
        # SCRAM secret). cluster+redpanda is one StatefulSet named after the CR.
        if cell.mode == "single" or _kafka_provider(cell) == "redpanda":
            return ["kafka"]  # StatefulSet broker pod (named after kafka.name)
        return ["pool", "entity-operator"]
    return []


def _kafka_provider(cell: Cell) -> str:
    """strimzi (default) or redpanda, read from the cell's --set overrides."""
    return "redpanda" if any("redpanda" in s for s in cell.extra_sets) else "strimzi"


def wait_ready(cell: Cell) -> tuple[bool, str]:
    """Wait until the cell's workloads are ready in budget.

    Redpanda is operator-managed, so gate on the Redpanda CR's own Ready condition
    rather than counting pods: with a 3-broker cluster a pod-presence check races
    the operator creating brokers one by one. `kubectl wait` returns the instant
    the operator reports the whole cluster ready (deterministic, no race); the
    300s ceiling is a backstop for a genuinely stuck cluster. Everything else uses
    the topology-aware pod check below.
    """
    # Only cluster+redpanda is operator-managed (a Redpanda CR to wait on). single
    # redpanda is a plain StatefulSet (no CR) -> fall through to the pod check.
    if cell.chart == "kafka" and _kafka_provider(cell) == "redpanda" and cell.mode == "cluster":
        waited = _run(
            [
                "kubectl",
                "wait",
                "--for=condition=Ready",
                "redpanda.cluster.redpanda.com/dfe-kafka",
                "-n",
                cell.namespace,
                "--timeout=300s",
            ],
            timeout=320,
        )
        if waited.returncode != 0:
            return False, f"Redpanda CR not Ready: {waited.stderr.strip()[:200]}"
        return True, ""

    required = _expected_workloads(cell)
    if not required:
        return True, ""  # external/disabled: no workloads to deploy
    deadline = WAIT_BUDGET_SECONDS
    waited = 0
    while waited < deadline:
        proc = _run(["kubectl", "get", "pods", "-n", cell.namespace, "--no-headers"])
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        names = " ".join(ln.split()[0] for ln in lines)
        present = all(w in names for w in required)
        if lines and present and all(_pod_ready(ln) for ln in lines):
            return True, ""
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    return False, f"expected workloads not Ready within {deadline}s (need {required})"


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
    # Function, not health: the inserted row must read back as count()==1.
    if "1" not in proc.stdout.split():
        return (
            False,
            f"CH insert not read back (count!=1): stdout={proc.stdout.strip()[:200]!r}",
        )
    return True, ""


def _accept_kafka(cell: Cell) -> tuple[bool, str]:
    """Functional kafka produce->consume round-trip, dispatched by provider."""
    pod = _first_pod(cell.namespace, "kafka")
    if not pod:
        return False, "no kafka broker pod found"
    if _kafka_provider(cell) == "redpanda":
        return _accept_kafka_redpanda(cell, pod)
    return _accept_kafka_strimzi(cell, pod)


def _wait_kafka_user_password(cell: Cell) -> str:
    """Wait on the operator's OWN ready condition for the user, then read the secret.

    A 'timing flake' is a missing dependency wait, not bad luck. Rather than poll
    the Secret against a guessed timeout, block on the User CR's status condition:
    the operator only reports ready once it has created the SCRAM user AND written
    its Secret, so the read below is then immediate and deterministic. Both
    operators mint a `dfe-kafka-user` Secret with a `password` key (same shape), so
    clients read SCRAM creds identically regardless of provider.

    The 240s ceiling is a backstop, not the gate -- `kubectl wait` returns the
    instant the condition flips, so a fast reconcile is fast; only a genuinely
    stuck operator hits the limit (a real failure, surfaced as such).
    """
    user = "dfe-kafka-user"
    # single mode has no User Operator/CR: the SCRAM secret is generated IN-CLUSTER
    # by the chart's ESO Password generator (kafka-single-user.yaml). There is no
    # condition to wait on, so block on the artifact itself -- poll the Secret until
    # ESO populates it (a bounded dependency wait, not a raced timeout).
    if cell.mode == "single":
        for _ in range(30):  # ~60s backstop for ESO to mint the secret
            sec = _run(
                [
                    "kubectl",
                    "get",
                    "secret",
                    user,
                    "-n",
                    cell.namespace,
                    "-o",
                    "jsonpath={.data.password}",
                ],
            )
            if sec.returncode == 0 and sec.stdout.strip():
                return base64.b64decode(sec.stdout.strip()).decode("utf-8", "replace")
            time.sleep(2)
        return ""
    if _kafka_provider(cell) == "redpanda":
        target, condition = f"user.cluster.redpanda.com/{user}", "Synced"
    else:
        target, condition = f"kafkauser/{user}", "Ready"
    waited = _run(
        [
            "kubectl",
            "wait",
            f"--for=condition={condition}",
            target,
            "-n",
            cell.namespace,
            "--timeout=240s",
        ],
        timeout=260,
    )
    if waited.returncode != 0:
        return ""  # operator never reported ready -> real failure, not a race
    # Condition is true => the Secret exists; read it (one brief retry for the
    # vanishingly small window between condition flip and Secret visibility).
    for _ in range(5):
        sec = _run(
            [
                "kubectl",
                "get",
                "secret",
                user,
                "-n",
                cell.namespace,
                "-o",
                "jsonpath={.data.password}",
            ],
        )
        if sec.returncode == 0 and sec.stdout.strip():
            return base64.b64decode(sec.stdout.strip()).decode("utf-8", "replace")
        time.sleep(2)
    return ""


def _accept_kafka_strimzi(cell: Cell, pod: str) -> tuple[bool, str]:
    """SCRAM produce->consume via the Strimzi KafkaUser (kafka-*.sh tools).

    Creates a topic, produces a message and consumes it back (consumer group
    'dfe-*' per the user's ACL). A real data round-trip, not pod presence.
    """
    password = _wait_kafka_user_password(cell)
    if not password:
        return (
            False,
            "KafkaUser secret 'dfe-kafka-user' not populated (no SCRAM password)",
        )
    jaas = (
        "org.apache.kafka.common.security.scram.ScramLoginModule required "
        f'username="dfe-kafka-user" password="{password}";'
    )
    topic = "dfe-acceptance"
    script = (
        "set -e; P=/tmp/mtx.props; "
        "{ echo 'security.protocol=SASL_PLAINTEXT'; "
        "echo 'sasl.mechanism=SCRAM-SHA-512'; "
        f"echo 'sasl.jaas.config={jaas}'; }} > $P; "
        # Absolute /opt/kafka/bin path works for BOTH the single-tier apache/kafka
        # image (cwd is not /opt/kafka -> a relative bin/ fails) AND the Strimzi
        # cluster broker (also /opt/kafka). Was `bin/...` which only worked in the
        # Strimzi pod (F5).
        "/opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --command-config $P "
        f"--create --if-not-exists --topic {topic}; "
        "echo mtx-msg | /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server localhost:9092 "
        f"--producer.config $P --topic {topic}; "
        "/opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 "
        f"--consumer.config $P --topic {topic} --group dfe-mtx --from-beginning "
        "--max-messages 1 --timeout-ms 30000"
    )
    proc = _run(
        ["kubectl", "exec", "-n", cell.namespace, pod, "--", "sh", "-c", script],
        timeout=120,
    )
    if proc.returncode != 0:
        return False, f"kafka produce/consume failed: {proc.stderr.strip()[:300]}"
    if "mtx-msg" not in proc.stdout:
        return (
            False,
            f"kafka message not read back: stdout={proc.stdout.strip()[:200]!r}",
        )
    return True, ""


def _accept_kafka_redpanda(cell: Cell, pod: str) -> tuple[bool, str]:
    """SCRAM produce->consume via the Redpanda operator's User (rpk + SASL).

    Redpanda is now operator-managed with SASL/SCRAM-512 on (the DFE standard),
    so this authenticates as dfe-kafka-user -- same creds shape as Strimzi, just
    rpk instead of kafka-*.sh.
    """
    password = _wait_kafka_user_password(cell)
    if not password:
        return False, "User secret 'dfe-kafka-user' not populated (no SCRAM password)"
    topic = "dfe-acceptance"
    # Broker port is mode-dependent (F6): the single-tier standalone StatefulSet
    # serves SASL on 9092 (kafka-single.yaml --kafka-addr ...:9092); the operator
    # (cluster) redpanda exposes its TLS-off kafka listener on 9093.
    port = "9092" if cell.mode == "single" else "9093"
    creds = (
        f"-X user=dfe-kafka-user -X pass='{password}' "
        f"-X sasl.mechanism=SCRAM-SHA-512 -X brokers=localhost:{port}"
    )
    script = (
        "set -e; "
        f"rpk topic create {topic} {creds} || true; "
        f"echo mtx-msg | rpk topic produce {topic} {creds}; "
        f"rpk topic consume {topic} --num 1 --offset start {creds}"
    )
    proc = _run(
        ["kubectl", "exec", "-n", cell.namespace, pod, "--", "sh", "-c", script],
        timeout=90,
    )
    if proc.returncode != 0:
        return False, f"redpanda produce/consume failed: {proc.stderr.strip()[:300]}"
    if "mtx-msg" not in proc.stdout:
        return (
            False,
            f"redpanda message not read back: stdout={proc.stdout.strip()[:200]!r}",
        )
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
    # Delete the namespace -- this reaps StatefulSet PVCs (which helm uninstall
    # leaves behind) and everything else. Async; assert_clean waits for it.
    _run(["kubectl", "delete", "namespace", cell.namespace, "--ignore-not-found"])
    return True, ""


def assert_clean(cell: Cell) -> tuple[bool, str]:
    """Clean = the namespace is fully gone (reaps pods + StatefulSet PVCs)."""
    waited = 0
    while waited < 180:
        proc = _run(["kubectl", "get", "namespace", cell.namespace])
        if proc.returncode != 0 and "notfound" in proc.stderr.lower().replace(" ", ""):
            return True, ""
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    return False, f"namespace {cell.namespace} not deleted within 180s"


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
        ("secrets", provision_test_secrets, "applied"),
        ("apply", apply, "applied"),
        ("wait", wait_ready, "ready"),
        ("acceptance", acceptance, "acceptance"),
    ):
        t0 = time.monotonic()
        try:
            ok, err = fn(cell)
        except Exception as exc:  # noqa: BLE001 -- any phase failure must still teardown
            ok, err = False, f"{step} raised: {exc!r}"
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
    p.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run the whole matrix N times (repeat-deploy solidity / flake hunt)",
    )
    p.add_argument(
        "--instance",
        default="",
        help="label for a CONCURRENT run -> ns/release prefix mtx-<instance>-* "
        "(run two with different --instance to prove concurrent deploys don't collide)",
    )
    args = p.parse_args()
    global INSTANCE
    INSTANCE = args.instance

    # Allow a bare invocation: fall back to the gitignored local kubeconfig so the
    # command matches the python3 allow-list (no KUBECONFIG= env prefix needed).
    if not os.environ.get("KUBECONFIG"):
        local_kubeconfig = REPO_ROOT / ".tmp" / "kubeconfig"
        if local_kubeconfig.is_file():
            os.environ["KUBECONFIG"] = str(local_kubeconfig)

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
    rounds = max(1, args.repeat)
    overall_failures = 0
    for i in range(rounds):
        tag = f" round {i + 1}/{rounds}" if rounds > 1 else ""
        print(f"Deployment matrix [{args.cloud}] -- {mode_label}{tag} -- {len(cells)} cell(s)\n")
        results = [run_cell(c, do_apply=args.apply) for c in cells]
        failures = [r for r in results if not r.ok]
        overall_failures += len(failures)
        for r in results:
            mark = "ok  " if r.ok else "FAIL"
            timing = " ".join(f"{k}={v}s" for k, v in r.timings.items())
            print(f"  [{mark}] {r.cell_id:<42} {timing}")
            if r.error:
                print(f"         -> {r.error}")
        print(f"\n{len(results) - len(failures)}/{len(results)} cells ok{tag}.\n")

    return 1 if overall_failures else 0


if __name__ == "__main__":
    sys.exit(main())
