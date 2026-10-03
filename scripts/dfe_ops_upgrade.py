#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/dfe_ops_upgrade.py
#  Purpose:      `dfe-ops upgrade` -- plan/preflight/apply/rollback a stack
#                upgrade against a deployment repo, walking upgrade-order.yaml
#                stage by stage. Split into its own module the way
#                dfe_ops_bastion.py and dfe_ops_init.py already are, and
#                imported into dfe-ops's build_parser() the same way.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-ops upgrade -- move a deployment repo from one stack pin to another.

    dfe-ops upgrade plan --deploy <dir> [--to <stack>]
    dfe-ops upgrade preflight --deploy <dir> [--to <stack>]
    dfe-ops upgrade apply --deploy <dir> [--to <stack>] [--yes] [--push] [--dry-run]
                           [--finalise] [--stop-before <stage-key>]
    dfe-ops upgrade rollback --deploy <dir> --to <stack> [--dry-run] [--check-cluster]

Every verb takes `--deploy`, a dfe-deploy checkout (its `pins.yaml` names the
FROM stack in `base.dfe-infra`), and `--to`, a versions.yaml stack version
(default: the `current` pointer). The move is computed by diffing the FROM and
TO stacks' pins, keyed by upgrade-order.yaml's declared order -- the same file
`docs/deployment/upgrades.md` says is applied by hand today.

    plan       Diff FROM -> TO, ordered by stage, with each step's `before`/
               `finalise`/`pair`/`rollback` note attached. Runs
               `dfe-stack compat-check --strict` for TO, and -- only when
               `--dial` is given -- resolve_sizing.py's locked-change
               classifier against the deploy's committed sizing/resolved.yaml.
               Writes the plan to <deploy>/upgrades/<from>-to-<to>.md.
               Exit 0 nothing moves (or the plan is clean), 1 the plan is
               BLOCKED (a compat-check failure, or a locked sizing change
               with no --migrate evidence), 2 a pre-flight failure (the
               deploy repo or the stack name do not resolve).

    preflight  The checks `apply` refuses to run without: the deploy repo is
               clean, the cluster answers, every Argo Application is Synced
               and Healthy, no KafkaRebalance is mid-flight, ClickHouse carries
               no long merge, the Strimzi stored-version conversion already
               ran (only checked when the plan crosses it), the on-prem node
               capacity holds the new sizing, and a backup marker exists when
               the plan carries a one-way step. Each check prints PASS/FAIL
               with the evidence line that decided it.

    apply      Runs preflight, then walks the plan stage by stage: bump
               `pins.yaml` (a surgical field edit, never a hand rewrite) and,
               with --dial, re-run the resolver with --migrate and copy its
               sizing/ output over <deploy>/sizing/, so the committed sizing
               moves the same way a re-size would; commit the stage
               in the deploy repo (`chore(upgrade): <stack> stage <n> -- <keys>`);
               push only with --push; wait for Argo to report every
               Application Synced and Healthy, bounded by --timeout, and after
               a stage that bumps the Strimzi operator wait again on every
               Kafka CR's `status.operatorLastSuccessfulVersion` reaching the
               new operator version, under the same bound. Confirms
               before each stage unless --yes. Stops at the first failure and
               prints that step's rollback note. --stop-before <stage-key>
               stops the walk before that upgrade-order.yaml stage, touching
               nothing in it or after. A reached step carrying a `finalise`
               note is printed and left pending unless --finalise is given, in
               which case apply asks whether the soak is over and, on yes,
               records it in the marker `rollback` reads (see below).
               --dry-run prints every command it would run, including a
               reached finalise and a --stop-before halt, and touches nothing.

    rollback   The reverse plan for TO -> the deploy's current FROM, refusing
               by name a step with `rollback: none` and no `finalise` note (an
               unconditional one-way step), or a `finalise`-bearing step whose
               finalise has ALREADY run -- read from the marker `apply
               --finalise` writes (`upgrades/<from>-to-<to>.finalised`), not
               from the pin diff alone. A `finalise`-bearing step with no
               marker yet is reversed like any other step, with a note that
               the soak can be abandoned safely. --check-cluster additionally
               reads the live Kafka CR's `status.kafkaMetadataVersion` and
               refuses when it already shows the bumped value, even with no
               marker (a finalise run by hand, outside this tool).

Nothing here executes a `before` or `finalise` note as a shell command -- they
are runbook prose, not argv. `apply` checks the one `before` note this repo
already has a program for (the Strimzi stored-version conversion) and
otherwise prints the note and asks for confirmation that an operator ran it by
hand. That one check also names the conversion tarball to fetch, at the version
the cluster is RUNNING rather than the one it is moving to. A `finalise` note works the same way: printed and left pending unless
--finalise is passed, in which case apply asks for confirmation instead of
running anything itself, and only writes the marker once the operator (or a
future automated hook) confirms it ran.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent

sys.path.insert(0, str(SCRIPTS))
import yaml_subset  # noqa: E402

UPGRADE_ORDER = REPO_ROOT / "upgrade-order.yaml"
VERSIONS_FILE = REPO_ROOT / "versions.yaml"
DFE_STACK = SCRIPTS / "dfe-stack"
RESOLVE_SIZING = SCRIPTS / "resolve_sizing.py"
CHECK_NODE_CAPACITY = SCRIPTS / "check_node_capacity.py"

# The versions.yaml sections upgrade-order.yaml's `key:` fields point into --
# the deploy-relevant pin set. digests/services-digests/content/providers/stack
# are versions.yaml sections too, but no upgrade-order.yaml step names one, so
# a move there is invisible to this diff by design (dfe-stack refresh-digests
# owns that half).
UPGRADE_SECTIONS = ("bootstrap", "operators", "services", "apps")

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_PREFLIGHT_FAILED = 2

DEFAULT_ARGOCD_NAMESPACE = "argocd"
DEFAULT_CLICKHOUSE_NAMESPACE = "clickhouse"
DEFAULT_CLICKHOUSE_SELECTOR = "app.kubernetes.io/name=clickhouse"
DEFAULT_CLICKHOUSE_MERGE_THRESHOLD = 300.0  # seconds
DEFAULT_KAFKA_NAMESPACE = "kafka"
DEFAULT_KAFKA_NAME = "dfe-kafka"  # helm/charts/kafka/values.yaml's kafka.name default
DEFAULT_TIER = "scale"
DEFAULT_BACKUP_MARKER = "upgrades/.backup-ok"
DEFAULT_TIMEOUT = 900
_SYNC_POLL_INTERVAL = 10.0

# The Strimzi-owned CRDs a 0.x -> 1.x stored-version conversion touches.
STRIMZI_CRDS = (
    "kafkas.kafka.strimzi.io",
    "kafkanodepools.kafka.strimzi.io",
    "kafkatopics.kafka.strimzi.io",
    "kafkausers.kafka.strimzi.io",
)

# The upgrade-order.yaml step whose pin move IS the Strimzi operator upgrade.
STRIMZI_OPERATOR_KEY = "operators.strimzi-kafka-operator"


class UpgradeError(RuntimeError):
    """An upgrade verb cannot proceed -- the message is what dfe-ops prints."""


# --- the shared subprocess boundary ------------------------------------------
# Every kubectl/git/dfe-stack/resolve_sizing/check_node_capacity call in this
# module goes through this one function, so a test mocks exactly one thing --
# the same shape dfe_ops_bastion.py's `_run` is for tofu/render_dial.py.


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return subprocess.run(cmd, **kwargs)  # type: ignore[call-overload]


def _last_line(text: str) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _kubectl(kubeconfig: str | None, *args: str) -> subprocess.CompletedProcess:
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += list(args)
    return _run(cmd)


# Bounds every read-only preflight kubectl call, so an unreachable cluster
# fails fast instead of each check paying client-go's own retry/backoff.
DEFAULT_KUBECTL_REQUEST_TIMEOUT = "10s"


def _kubectl_json(kubeconfig: str | None, *args: str) -> tuple[int, dict, str]:
    result = _kubectl(kubeconfig, *args, f"--request-timeout={DEFAULT_KUBECTL_REQUEST_TIMEOUT}", "-o", "json")
    if result.returncode != 0:
        return result.returncode, {}, (result.stderr or "").strip()
    try:
        return 0, json.loads(result.stdout or "{}"), ""
    except json.JSONDecodeError as exc:
        return 1, {}, f"unparseable kubectl output: {exc}"


def _git(deploy: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(deploy), *args])


# --- upgrade-order.yaml -------------------------------------------------------


def _unquote(text: str) -> str:
    """Strip one layer of matching quotes -- yaml_subset keeps a quoted block
    key's quotes verbatim (it only unquotes scalar VALUES), and every stage and
    step key in upgrade-order.yaml is written quoted (`"10-bootstrap":`)."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


@dataclass(frozen=True, slots=True)
class Step:
    """One upgrade-order.yaml step -- a versions.yaml key and its notes."""

    stage: str
    order: str
    key: str
    before: str = ""
    finalise: str = ""
    pair: str = ""
    rollback: str = ""
    scope: str = ""

    @property
    def one_way(self) -> bool:
        """A step with no reverse path -- explicit `rollback: none`, or a
        `finalise` (a one-way step by definition, once it has run)."""
        return bool(self.finalise) or self.rollback.strip().lower() == "none"


def load_steps(path: Path | None = None) -> list[Step]:
    """Every step in upgrade-order.yaml, in stage then order-key order.

    `path` defaults to the module-level UPGRADE_ORDER, looked up at CALL time
    (not bound as a default-argument value) so a test can monkeypatch
    `dfe_ops_upgrade.UPGRADE_ORDER` and have every caller that omits `path`
    pick it up -- a bound default would freeze the original path forever.
    """
    path = path if path is not None else UPGRADE_ORDER
    tree = yaml_subset.parse(path.read_text(encoding="utf-8"), source=str(path))
    stages = tree.get("stages")
    if not isinstance(stages, dict):
        raise UpgradeError(f"{path}: carries no stages: map")
    steps: list[Step] = []
    for stage_key in sorted(stages, key=_unquote):
        stage_name = _unquote(stage_key)
        body = stages[stage_key]
        if not isinstance(body, dict):
            continue
        for order_key in sorted(body, key=_unquote):
            entry = body[order_key]
            if not isinstance(entry, dict) or "key" not in entry:
                continue
            steps.append(
                Step(
                    stage=stage_name,
                    order=_unquote(order_key),
                    key=str(entry["key"]),
                    before=str(entry.get("before", "")),
                    finalise=str(entry.get("finalise", "")),
                    pair=str(entry.get("pair", "")),
                    rollback=str(entry.get("rollback", "")),
                    scope=str(entry.get("scope", "")),
                )
            )
    return steps


# --- versions.yaml -------------------------------------------------------


def load_versions_root(path: Path | None = None) -> dict:
    """versions.yaml, parsed. See load_steps() for why `path` is looked up at
    call time rather than bound as a default-argument value."""
    path = path if path is not None else VERSIONS_FILE
    return yaml_subset.parse(path.read_text(encoding="utf-8"), source=str(path))


def _norm_stack(name: str) -> str:
    name = name.strip()
    return name[1:] if name.startswith("v") else name


def resolve_stack(root: dict, name: str) -> tuple[str, dict]:
    """(canonical name, pin set) for a stack version, tolerant of a v-prefix
    mismatch -- the same rule dfe-stack's own stack_pins() applies."""
    stacks = root.get("stacks")
    if not isinstance(stacks, dict):
        raise UpgradeError("versions.yaml carries no stacks: map")
    if name in stacks:
        return name, stacks[name]
    match = next((n for n in stacks if _norm_stack(n) == _norm_stack(name)), None)
    if match is None:
        have = ", ".join(sorted(stacks)) or "none"
        raise UpgradeError(f"stack {name!r} not in versions.yaml stacks: (have: {have})")
    return match, stacks[match]


def current_stack(root: dict) -> str:
    current = root.get("current")
    if not current:
        raise UpgradeError("versions.yaml carries no `current` pointer")
    return str(current)


def flatten_stack(stack: dict) -> dict[str, str]:
    """A stack's pins as dotted keys (bootstrap.X, operators.X, ...), limited
    to the sections upgrade-order.yaml's `key:` fields can name."""
    flat: dict[str, str] = {}
    for section in UPGRADE_SECTIONS:
        body = stack.get(section)
        if isinstance(body, dict):
            for name, value in body.items():
                if isinstance(value, str):
                    flat[f"{section}.{name}"] = value
    return flat


def read_deploy_pin(deploy: Path) -> str:
    """The deploy repo's currently pinned stack, from pins.yaml's `base.dfe-infra`."""
    pins_path = deploy / "pins.yaml"
    if not pins_path.is_file():
        raise UpgradeError(f"no pins.yaml in {deploy} -- not a dfe-deploy checkout")
    tree = yaml_subset.parse(pins_path.read_text(encoding="utf-8"), source=str(pins_path))
    pinned = yaml_subset.at(tree, ("base", "dfe-infra"))
    if not pinned:
        raise UpgradeError(f"{pins_path}: no base.dfe-infra pin")
    return str(pinned)


# --- the diff (plan_moves) -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Move:
    """One upgrade-order.yaml step whose pin differs between FROM and TO."""

    step: Step
    old: str
    new: str


def plan_moves(steps: list[Step], from_pins: dict[str, str], to_pins: dict[str, str]) -> list[Move]:
    """Every step whose key's value differs between the two flattened pin sets,
    in upgrade-order.yaml order."""
    moves: list[Move] = []
    for step in steps:
        old = from_pins.get(step.key)
        new = to_pins.get(step.key)
        if old == new:
            continue
        moves.append(Move(step=step, old=old or "(absent)", new=new or "(absent)"))
    return moves


def render_plan(moves: list[Move], *, from_stack: str, to_stack: str) -> str:
    """The numbered plan, grouped by stage, each move carrying its notes."""
    lines = [f"# Upgrade plan: {from_stack} -> {to_stack}", ""]
    if not moves:
        lines.append("No pinned key moves between these two stacks.")
        return "\n".join(lines) + "\n"
    stage = None
    n = 0
    for move in moves:
        if move.step.stage != stage:
            stage = move.step.stage
            lines.append(f"## stage {stage}")
            lines.append("")
        n += 1
        scoped_key = f"{move.step.key} ({move.step.scope})" if move.step.scope else move.step.key
        lines.append(f"{n}. {scoped_key}: {move.old} -> {move.new}")
        if move.step.before:
            lines.append(f"   before:   {move.step.before}")
        if move.step.finalise:
            lines.append(f"   finalise: {move.step.finalise}")
        if move.step.pair:
            lines.append(f"   pair:     {move.step.pair}")
        if move.step.rollback:
            lines.append(f"   rollback: {move.step.rollback}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def moves_by_stage(moves: list[Move]) -> list[tuple[str, list[Move]]]:
    """Moves grouped by stage, stages in first-seen (upgrade-order.yaml) order."""
    order: list[str] = []
    groups: dict[str, list[Move]] = {}
    for move in moves:
        if move.step.stage not in groups:
            groups[move.step.stage] = []
            order.append(move.step.stage)
        groups[move.step.stage].append(move)
    return [(stage, groups[stage]) for stage in order]


# --- compat-check --------------------------------------------------------------


def run_compat_check(stack: str) -> tuple[bool, str]:
    """`dfe-stack compat-check --stack <stack> --strict`. ok, combined output."""
    result = _run([sys.executable, str(DFE_STACK), "compat-check", "--stack", stack, "--strict"])
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    return result.returncode == 0, output


# --- the sizing locked-change classifier --------------------------------------


def check_locked_sizing(
    dial: Path,
    previous: Path,
    *,
    fixtures: Path | None,
    live: bool,
    migrate: bool = False,
    refresh: Path | None = None,
) -> tuple[bool, str, bool]:
    """Run resolve_sizing.py's locked-change classifier against a deploy's
    committed sizing/resolved.yaml.

    Returns (ok, report text, blocked). `ok` is False only on an actual
    resolver failure; a clean skip (no previous file, no --fixtures/--live)
    is `ok=True, blocked=False` -- there being nothing to check is not a
    failure. `blocked` is True exactly when the resolver exited 3 (a LOCKED
    field moved and --migrate was not accepted).

    The resolver always writes into a temporary --out. With `refresh` (a
    deploy checkout) a clean resolve's sizing/ is copied over
    `<refresh>/sizing/`; without it the output is discarded, which is what
    `plan` needs. Only sizing/ is copied: the OpenTofu inputs and the shape
    answer the resolver writes at the root of --out belong beside the
    OpenTofu root module, which a deploy repo does not hold.
    """
    if not previous.is_file():
        return True, f"skipped: no {previous}", False
    if not fixtures and not live:
        return True, "skipped: neither --fixtures nor --live given", False

    cmd = [sys.executable, str(RESOLVE_SIZING), "resolve", "--dial", str(dial), "--previous", str(previous)]
    if fixtures:
        cmd += ["--fixtures", str(fixtures)]
    else:
        cmd += ["--live"]
    if migrate:
        cmd.append("--migrate")

    refreshed = ""
    with tempfile.TemporaryDirectory(prefix="dfe-ops-upgrade-sizing-") as tmp:
        result = _run([*cmd, "--out", tmp])
        if refresh is not None and result.returncode == 0:
            target = refresh / "sizing"
            shutil.copytree(Path(tmp) / "sizing", target, dirs_exist_ok=True)
            refreshed = f"\nrefreshed {target}"

    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode == 3:
        locked_lines = [ln for ln in output.splitlines() if "LOCKED" in ln]
        detail = "LOCKED change(s) detected -- --migrate required:\n" + "\n".join(locked_lines or [output])
        return True, detail, True
    if result.returncode != 0:
        return False, f"resolver failed (rc {result.returncode}): {_last_line(output)}", True
    return True, (output or "no locked-field change") + refreshed, False


# --- preflight checks ----------------------------------------------------------


def check_deploy_clean(deploy: Path) -> tuple[bool, str]:
    result = _git(deploy, "status", "--porcelain")
    if result.returncode != 0:
        return False, _last_line(result.stderr) or "git status failed"
    dirty = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
    if dirty:
        extra = f" (+{len(dirty) - 1} more)" if len(dirty) > 1 else ""
        return False, f"{len(dirty)} uncommitted change(s): {dirty[0]}{extra}"
    return True, "working tree clean"


def check_cluster_reachable(kubeconfig: str | None, timeout: str = "10s") -> tuple[bool, str]:
    result = _kubectl(kubeconfig, "version", f"--request-timeout={timeout}")
    if result.returncode != 0:
        return False, _last_line(result.stderr) or "kubectl version failed"
    return True, _last_line(result.stdout) or "cluster reachable"


def check_argo_apps(kubeconfig: str | None, namespace: str = DEFAULT_ARGOCD_NAMESPACE) -> tuple[bool, str]:
    """Every Argo Application in `namespace` is Synced and Healthy.

    Reads kubectl directly (`applications.argoproj.io`), the same path
    dfe-ops refresh/cycle already use -- no second CLI (argocd) dependency.
    """
    rc, doc, err = _kubectl_json(kubeconfig, "-n", namespace, "get", "applications.argoproj.io")
    if rc != 0:
        return False, f"cannot list Argo Applications in {namespace}: {err}"
    items = doc.get("items") or []
    if not items:
        return False, f"no Argo Applications found in {namespace}"
    bad = []
    for app in items:
        name = (app.get("metadata") or {}).get("name", "<unnamed>")
        status = app.get("status") or {}
        sync = (status.get("sync") or {}).get("status") or "Unknown"
        health = (status.get("health") or {}).get("status") or "Unknown"
        if sync != "Synced" or health != "Healthy":
            bad.append(f"{name} (sync {sync}, health {health})")
    if bad:
        extra = f", +{len(bad) - 6} more" if len(bad) > 6 else ""
        return False, f"{len(bad)} app(s) not Synced/Healthy: {', '.join(bad[:6])}{extra}"
    return True, f"{len(items)} Application(s) Synced and Healthy"


def check_no_kafka_rebalance(kubeconfig: str | None) -> tuple[bool, str]:
    rc, doc, err = _kubectl_json(kubeconfig, "get", "kafkarebalances.kafka.strimzi.io", "-A")
    if rc != 0:
        if "NotFound" in err or "the server doesn't have a resource type" in err:
            return True, "no KafkaRebalance CRD on this cluster"
        return False, f"cannot list KafkaRebalance: {err}"
    items = doc.get("items") or []
    in_progress = []
    for item in items:
        name = (item.get("metadata") or {}).get("name", "<unnamed>")
        conditions = ((item.get("status") or {}).get("conditions")) or []
        if any(c.get("type") == "Rebalancing" for c in conditions):
            in_progress.append(name)
    if in_progress:
        return False, f"{len(in_progress)} KafkaRebalance in progress: {', '.join(in_progress)}"
    return True, f"no KafkaRebalance in progress ({len(items)} total)"


def check_clickhouse_merges(
    kubeconfig: str | None,
    namespace: str = DEFAULT_CLICKHOUSE_NAMESPACE,
    selector: str = DEFAULT_CLICKHOUSE_SELECTOR,
    threshold_seconds: float = DEFAULT_CLICKHOUSE_MERGE_THRESHOLD,
) -> tuple[bool, str]:
    rc, doc, err = _kubectl_json(kubeconfig, "-n", namespace, "get", "pods", "-l", selector)
    if rc != 0:
        return False, f"cannot find a ClickHouse pod: {err}"
    items = doc.get("items") or []
    if not items:
        return False, f"no pod matching {selector!r} in {namespace}"
    pod_name = (items[0].get("metadata") or {}).get("name", "")
    query = f"SELECT count() FROM system.merges WHERE elapsed > {threshold_seconds}"
    result = _kubectl(
        kubeconfig, "-n", namespace, f"--request-timeout={DEFAULT_KUBECTL_REQUEST_TIMEOUT}",
        "exec", pod_name, "--", "clickhouse-client", "-q", query,
    )
    if result.returncode != 0:
        return False, _last_line(result.stderr) or "clickhouse-client query failed"
    raw = (result.stdout or "0").strip()
    try:
        count = int(raw)
    except ValueError:
        return False, f"unparseable clickhouse-client output: {raw!r}"
    if count > 0:
        return False, f"{count} merge(s) running longer than {threshold_seconds:.0f}s"
    return True, f"no merge running longer than {threshold_seconds:.0f}s"


def check_strimzi_conversion(kubeconfig: str | None, crds: tuple[str, ...] = STRIMZI_CRDS) -> tuple[bool, str]:
    """Every Strimzi CRD this stack owns stores v1 only -- the sign
    `bin/v1-api-conversion.sh convert-resource` (then `crd-upgrade`) already
    ran, which 1.x requires before the operator upgrade lands."""
    stale = []
    checked = 0
    for crd in crds:
        rc, doc, _err = _kubectl_json(kubeconfig, "get", "crd", crd)
        if rc != 0:
            continue  # not installed yet -- nothing to convert
        checked += 1
        stored = (doc.get("status") or {}).get("storedVersions") or []
        non_v1 = [v for v in stored if v != "v1"]
        if non_v1:
            stale.append(f"{crd} (stored: {', '.join(stored)})")
    if checked == 0:
        return True, "no Strimzi CRDs installed -- nothing to convert"
    if stale:
        return False, f"{len(stale)} CRD(s) still store a pre-v1 version: {', '.join(stale)}"
    return True, f"{checked} Strimzi CRD(s) store v1 only"


def strimzi_conversion_tool(operator_version: str) -> str:
    """The conversion tool's release artefact for one operator version."""
    return f"strimzi-v1-api-conversion-{operator_version}.tar.gz"


def conversion_tool_line(move: Move) -> str:
    """Which release's conversion tarball to fetch for this operator move.

    The tool rewrites the CRs the RUNNING operator wrote, so it comes from that
    release rather than the one the pin is moving to.
    """
    return (
        f"fetch {strimzi_conversion_tool(move.old)} from the RUNNING operator release "
        f"{move.old}, never the target {move.new}"
    )


def check_strimzi_conversion_before(kubeconfig: str | None, move: Move) -> tuple[bool, str]:
    """The strimzi-kafka-operator step's before-hook, naming its own tool.

    A stale detail line would leave an operator reaching for the target
    version's tarball, which is the wrong artefact for the CRs on disk.
    """
    ok, detail = check_strimzi_conversion(kubeconfig)
    return ok, f"{detail}; {conversion_tool_line(move)}"


def check_kafka_operator_version(
    kubeconfig: str | None, version: str, *, namespace: str | None = None
) -> tuple[bool, str]:
    """Every Kafka CR reports `status.operatorLastSuccessfulVersion` at `version`.

    The CR's own `Ready` condition stays True and stale across an operator
    upgrade, so a wait on it returns at once and proves nothing; this field is
    the one the new operator writes only after it has reconciled the cluster.
    Reads every namespace unless the caller names one, because "every Kafka CR"
    is the claim being made.
    """
    args = ["get", "kafkas.kafka.strimzi.io"]
    args += ["-n", namespace] if namespace else ["-A"]
    rc, doc, err = _kubectl_json(kubeconfig, *args)
    if rc != 0:
        if "NotFound" in err or "the server doesn't have a resource type" in err:
            return True, "no Kafka CRD on this cluster -- no operator version to reconcile"
        return False, f"cannot list Kafka CRs: {err}"
    items = doc.get("items") or []
    if not items:
        return True, "no Kafka CR on this cluster -- no operator version to reconcile"
    behind = []
    for item in items:
        meta = item.get("metadata") or {}
        name = f"{meta.get('namespace', '')}/{meta.get('name', '<unnamed>')}"
        seen = str((item.get("status") or {}).get("operatorLastSuccessfulVersion") or "")
        if seen != version:
            behind.append(f"{name} (operatorLastSuccessfulVersion {seen or 'unset'})")
    if behind:
        extra = f", +{len(behind) - 6} more" if len(behind) > 6 else ""
        return False, f"{len(behind)} Kafka CR(s) not yet reconciled by {version}: {', '.join(behind[:6])}{extra}"
    return True, f"{len(items)} Kafka CR(s) report operatorLastSuccessfulVersion {version}"


def check_cluster_metadata_version(
    kubeconfig: str | None,
    moves: list[Move],
    *,
    kafka_name: str = DEFAULT_KAFKA_NAME,
    kafka_namespace: str = DEFAULT_KAFKA_NAMESPACE,
) -> tuple[bool, str]:
    """Whether the LIVE cluster already carries a finalised metadata.version
    for a step this rollback would reverse -- the signal a finalise run by
    hand (outside `apply --finalise`, so no marker was written) leaves behind.

    Reads the Strimzi Kafka CR's `status.kafkaMetadataVersion` -- the field
    upgrade-order.yaml's kafka-brokers `finalise` note names -- and refuses
    when it already equals (or is prefixed by) a finalise-bearing step's NEW
    pin, the value that step's finalise would have moved it to. Unreachable,
    absent, or not yet at that value all pass: this is a live-cluster
    corroboration on top of the marker, never a replacement for it.
    """
    finalise_moves = [move for move in moves if move.step.finalise]
    if not finalise_moves:
        return True, "no finalise-bearing step in this rollback -- nothing to check"
    rc, doc, err = _kubectl_json(kubeconfig, "-n", kafka_namespace, "get", "kafka", kafka_name)
    if rc != 0:
        return True, f"cannot read Kafka CR {kafka_name} in {kafka_namespace} -- skipped: {err}"
    live = str((doc.get("status") or {}).get("kafkaMetadataVersion") or "")
    if not live:
        return True, f"Kafka CR {kafka_name} carries no status.kafkaMetadataVersion -- skipped"
    bumped = [move for move in finalise_moves if live == move.new or live.startswith(move.new)]
    if bumped:
        names = ", ".join(move.step.key for move in bumped)
        return False, f"live status.kafkaMetadataVersion is {live!r} -- {names} already reflects the bumped value"
    return True, f"live status.kafkaMetadataVersion is {live!r} -- does not match a bumped step"


def check_node_capacity(kubeconfig: str | None, nodes_file: Path) -> tuple[bool, str]:
    if not nodes_file.is_file():
        return True, f"no {nodes_file} -- nothing to check"
    cmd = [sys.executable, str(CHECK_NODE_CAPACITY), "--nodes-file", str(nodes_file)]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    result = _run(cmd)
    ok = result.returncode == 0
    detail = _last_line(result.stdout) or _last_line(result.stderr)
    return ok, detail or ("capacity ok" if ok else "capacity check failed")


def check_backup_marker(deploy: Path, moves: list[Move], marker: str = DEFAULT_BACKUP_MARKER) -> tuple[bool, str]:
    irreversible = [move for move in moves if move.step.one_way]
    if not irreversible:
        return True, "no one-way step in this plan -- no backup marker required"
    marker_path = deploy / marker
    if marker_path.is_file():
        return True, f"backup marker present: {marker_path}"
    names = ", ".join(move.step.key for move in irreversible)
    return False, f"one-way step(s) in this plan ({names}) need a backup marker at {marker_path} before apply"


def run_preflight(
    deploy: Path,
    moves: list[Move],
    *,
    kubeconfig: str | None,
    argocd_namespace: str = DEFAULT_ARGOCD_NAMESPACE,
    clickhouse_namespace: str = DEFAULT_CLICKHOUSE_NAMESPACE,
    clickhouse_selector: str = DEFAULT_CLICKHOUSE_SELECTOR,
    clickhouse_merge_threshold: float = DEFAULT_CLICKHOUSE_MERGE_THRESHOLD,
    nodes_file: Path | None = None,
    backup_marker: str = DEFAULT_BACKUP_MARKER,
) -> list[tuple[str, bool, str]]:
    """Every preflight check, in report order. Does not print -- the caller
    (cmd_upgrade_preflight, or apply before it acts) owns presentation."""
    checks: list[tuple[str, bool, str]] = []
    checks.append(("deploy repo clean", *check_deploy_clean(deploy)))
    checks.append(("cluster reachable", *check_cluster_reachable(kubeconfig)))
    checks.append(("argo apps synced and healthy", *check_argo_apps(kubeconfig, argocd_namespace)))
    checks.append(("no KafkaRebalance in progress", *check_no_kafka_rebalance(kubeconfig)))
    checks.append((
        "clickhouse merges under threshold",
        *check_clickhouse_merges(kubeconfig, clickhouse_namespace, clickhouse_selector, clickhouse_merge_threshold),
    ))
    if any("stored-version conversion" in move.step.before for move in moves):
        checks.append(("strimzi stored-version conversion", *check_strimzi_conversion(kubeconfig)))
    resolved_nodes_file = nodes_file or (deploy / "sizing" / f"{DEFAULT_TIER}.nodes.json")
    checks.append(("on-prem node capacity", *check_node_capacity(kubeconfig, resolved_nodes_file)))
    checks.append(("backup marker (one-way steps)", *check_backup_marker(deploy, moves, backup_marker)))
    return checks


def _print_checks(checks: list[tuple[str, bool, str]]) -> int:
    failed = 0
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            failed += 1
    return failed


# --- the pin-file editor (surgical, never a hand rewrite) ---------------------


class PinFileError(UpgradeError):
    """pins.yaml carries no editable base.dfe-infra field."""


def set_pin_stack(text: str, stack: str) -> str:
    """Set `base.dfe-infra` inside pins.yaml's `base:` block, in place.

    Walks from the `base:` line (column 0) to the next column-0 non-blank
    line and replaces the first `dfe-infra:` scalar it finds -- every other
    line (comments, `dfe-schemas`, the whole `overrides:`/`channel:` rest of
    the file) survives verbatim. The same focused-editor shape as
    dfe_ops_bastion.py's `_set_toolbox_field`, so a bump is never a full
    rewrite that could silently drop an operator's override block.
    """
    field_re = re.compile(r'^(\s*)dfe-infra:\s*(#.*)?$|^(\s*)dfe-infra:\s+\S.*$')
    lines = text.splitlines()
    out: list[str] = []
    in_block = False
    found = False
    for line in lines:
        if not in_block:
            if re.match(r"^base:\s*(#.*)?$", line):
                in_block = True
            out.append(line)
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if stripped and indent == 0:
            in_block = False
            out.append(line)
            continue
        if not found and field_re.match(line):
            leading = line[: len(line) - len(line.lstrip(" "))]
            out.append(f'{leading}dfe-infra: "{stack}"')
            found = True
            continue
        out.append(line)
    if not found:
        raise PinFileError("pins.yaml carries no base.dfe-infra field to bump")
    rendered = "\n".join(out)
    return rendered + "\n" if text.endswith("\n") else rendered


def bump_pin_file(deploy: Path, stack: str) -> bool:
    """Set the deploy's pins.yaml to `stack`, idempotently. Returns whether it
    changed anything (False when it already named this stack)."""
    pins_path = deploy / "pins.yaml"
    text = pins_path.read_text(encoding="utf-8")
    updated = set_pin_stack(text, stack)
    if updated == text:
        return False
    pins_path.write_text(updated, encoding="utf-8")
    return True


# --- finalise markers ----------------------------------------------------------
# The record that a one-way step's `finalise` note actually ran -- the fact
# `rollback` keys its refusal on, rather than the pin diff alone. A pin move
# that touches a one-way step is reversible right up until its finalise runs.


def _finalise_marker_path(deploy: Path, from_stack: str, to_stack: str) -> Path:
    """Same `<from>-to-<to>` naming as the plan file
    (`upgrades/<from>-to-<to>.md`) -- `apply` writes this name in the forward
    direction it moved, and a rollback checking for it computes the SAME name
    because a rollback's own FROM/TO are that forward move's TO/FROM, swapped."""
    return deploy / "upgrades" / f"{from_stack}-to-{to_stack}.finalised"


def read_finalised_keys(deploy: Path) -> dict[str, str]:
    """Every versions.yaml key any `*.finalised` marker under <deploy>/upgrades
    records, mapped to the timestamp of its last write.

    Scans every marker file, not just the one name this rollback's own
    from/to would compute -- a rollback spanning more than one past `apply`
    (a multi-hop rollback) still has to see a finalise that ran during an
    intermediate hop.
    """
    upgrades_dir = deploy / "upgrades"
    finalised: dict[str, str] = {}
    if not upgrades_dir.is_dir():
        return finalised
    for marker in sorted(upgrades_dir.glob("*.finalised")):
        for line in marker.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            key, _, timestamp = line.partition(" ")
            if key:
                finalised[key] = timestamp
    return finalised


def write_finalise_marker(
    deploy: Path, from_stack: str, to_stack: str, key: str, *, timestamp: str | None = None
) -> Path:
    """Record that `key`'s finalise hook ran, in
    <deploy>/upgrades/<from_stack>-to-<to_stack>.finalised. Idempotent per
    key -- a repeat write updates the timestamp rather than duplicating the
    line, and every other key already in the marker survives."""
    path = _finalise_marker_path(deploy, from_stack, to_stack)
    path.parent.mkdir(parents=True, exist_ok=True)
    entries: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            existing_key, _, existing_ts = line.partition(" ")
            if existing_key:
                entries[existing_key] = existing_ts
    entries[key] = timestamp or datetime.now(UTC).isoformat()
    path.write_text("".join(f"{k} {v}\n" for k, v in sorted(entries.items())), encoding="utf-8")
    return path


# --- Argo sync wait (apply) ----------------------------------------------------


def wait_for_argo(
    kubeconfig: str | None, *, argocd_namespace: str, timeout: float, sleep=time.sleep, now=time.monotonic
) -> tuple[bool, str]:
    """Block until check_argo_apps reports every Application Synced and
    Healthy, or `timeout` seconds pass. `sleep`/`now` are injected so a test
    drives this without a real clock."""
    deadline = now() + timeout
    while True:
        ok, detail = check_argo_apps(kubeconfig, argocd_namespace)
        if ok:
            return True, detail
        remaining = deadline - now()
        if remaining <= 0:
            return False, f"still not converged after {timeout:.0f}s -- {detail}"
        sleep(min(_SYNC_POLL_INTERVAL, remaining))


def wait_for_kafka_operator_version(
    kubeconfig: str | None,
    version: str,
    *,
    namespace: str | None = None,
    timeout: float,
    sleep=time.sleep,
    now=time.monotonic,
) -> tuple[bool, str]:
    """Block until every Kafka CR reports the new operator version, or time out.

    Argo reporting the operator Application Healthy only says the new
    Deployment is up, which is minutes before it has reconciled the clusters it
    watches. `sleep`/`now` are injected the same way wait_for_argo's are.
    """
    deadline = now() + timeout
    while True:
        ok, detail = check_kafka_operator_version(kubeconfig, version, namespace=namespace)
        if ok:
            return True, detail
        remaining = deadline - now()
        if remaining <= 0:
            return False, f"still not reconciled after {timeout:.0f}s -- {detail}"
        sleep(min(_SYNC_POLL_INTERVAL, remaining))


# --- plan ----------------------------------------------------------------------


def _load_from_to(deploy: Path, to_arg: str | None) -> tuple[dict, str, dict, str, dict]:
    """(root, from_name, from_pins, to_name, to_pins) -- raises UpgradeError."""
    root = load_versions_root()
    to_name, to_pins = resolve_stack(root, to_arg or current_stack(root))
    from_name = read_deploy_pin(deploy)
    _, from_pins = resolve_stack(root, from_name)
    return root, from_name, from_pins, to_name, to_pins


def cmd_upgrade_plan(args: argparse.Namespace) -> int:
    deploy = Path(args.deploy)
    try:
        _root, from_name, from_pins, to_name, to_pins = _load_from_to(deploy, args.to)
    except UpgradeError as err:
        print(f"dfe-ops upgrade plan: {err}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    steps = load_steps()
    moves = plan_moves(steps, flatten_stack(from_pins), flatten_stack(to_pins))
    blocked = False

    sections = [render_plan(moves, from_stack=from_name, to_stack=to_name)]

    compat_ok, compat_out = run_compat_check(to_name)
    sections.append(f"## compat-check ({to_name}, --strict)\n\n```\n{compat_out}\n```\n")
    if not compat_ok:
        blocked = True

    if args.dial:
        previous = deploy / "sizing" / "resolved.yaml"
        sizing_ok, sizing_detail, sizing_blocked = check_locked_sizing(
            Path(args.dial), previous, fixtures=Path(args.fixtures) if args.fixtures else None, live=args.live
        )
        sections.append(f"## sizing locked-change check\n\n{sizing_detail}\n")
        if not sizing_ok or sizing_blocked:
            blocked = True
    else:
        sections.append("## sizing locked-change check\n\nskipped: no --dial given\n")

    text = "\n".join(sections)
    out_dir = deploy / "upgrades"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{from_name}-to-{to_name}.md"
    out_path.write_text(text, encoding="utf-8")

    print(text)
    print(f"plan written to {out_path}", file=sys.stderr)
    if blocked:
        print("dfe-ops upgrade plan: BLOCKED -- see compat-check / sizing sections above", file=sys.stderr)
        return EXIT_BLOCKED
    return EXIT_OK


# --- preflight -------------------------------------------------------------


def cmd_upgrade_preflight(args: argparse.Namespace) -> int:
    deploy = Path(args.deploy)
    try:
        _root, _from_name, from_pins, _to_name, to_pins = _load_from_to(deploy, args.to)
    except UpgradeError as err:
        print(f"dfe-ops upgrade preflight: {err}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    steps = load_steps()
    moves = plan_moves(steps, flatten_stack(from_pins), flatten_stack(to_pins))
    checks = run_preflight(
        deploy,
        moves,
        kubeconfig=args.kubeconfig,
        argocd_namespace=args.argocd_namespace,
        clickhouse_namespace=args.clickhouse_namespace,
        clickhouse_selector=args.clickhouse_selector,
        clickhouse_merge_threshold=args.clickhouse_merge_threshold,
        nodes_file=Path(args.nodes_file) if args.nodes_file else None,
        backup_marker=args.backup_marker,
    )
    failed = _print_checks(checks)
    print()
    if failed:
        print(f"preflight FAILED: {failed}/{len(checks)} check(s) failed", file=sys.stderr)
        return EXIT_BLOCKED
    print(f"preflight PASSED: {len(checks)} check(s)")
    return EXIT_OK


# --- apply -----------------------------------------------------------------

# A step whose `before` note this repo already has a program to verify,
# keyed by the step's versions.yaml `key`. A note with no entry here is a
# manual gate: apply prints it and asks for confirmation instead.
BEFORE_CHECKS: dict[str, Callable[[str | None, Move], tuple[bool, str]]] = {
    STRIMZI_OPERATOR_KEY: check_strimzi_conversion_before,
}

# The extra line a step's before-hook prints in a --dry-run, where no check
# runs and the detail line that would have carried it never appears.
BEFORE_NOTES: dict[str, Callable[[Move], str]] = {
    STRIMZI_OPERATOR_KEY: conversion_tool_line,
}

# A finalise note this repo already has a program for, same shape as
# BEFORE_CHECKS, keyed by the step's versions.yaml `key`. Empty today -- no
# finalise note has an automated hook yet, so every one falls back to the
# manual-confirm path a `before` note with no BEFORE_CHECKS entry already uses.
FINALISE_HOOKS: dict[str, Callable[[str | None], tuple[bool, str]]] = {}


def _confirm(prompt: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def run_finalise_hook(
    deploy: Path, move: Move, *, from_name: str, to_name: str, kubeconfig: str | None, assume_yes: bool
) -> tuple[bool, str]:
    """Run (or confirm) one finalise-bearing move's hook, and mark it when it
    runs. `ran` is False only when an automated hook fails or the operator
    declines -- the marker is written only when `ran` is True, so a decline
    leaves the step exactly as reversible as it was before this call."""
    check = FINALISE_HOOKS.get(move.step.key)
    if check is not None:
        ok, detail = check(kubeconfig)
    else:
        confirmed = _confirm(f"soak complete -- run finalise now: {move.step.finalise}", assume_yes=assume_yes)
        ok, detail = confirmed, ("confirmed by operator" if confirmed else "declined by operator")
    if ok:
        marker = write_finalise_marker(deploy, from_name, to_name, move.step.key)
        detail = f"{detail} -- marker written: {marker}"
    return ok, detail


def cmd_upgrade_apply(args: argparse.Namespace) -> int:
    deploy = Path(args.deploy)
    try:
        _root, from_name, from_pins, to_name, to_pins = _load_from_to(deploy, args.to)
    except UpgradeError as err:
        print(f"dfe-ops upgrade apply: {err}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    steps = load_steps()
    moves = plan_moves(steps, flatten_stack(from_pins), flatten_stack(to_pins))
    if not moves:
        print(f"dfe-ops upgrade apply: {from_name} already matches {to_name} -- nothing to apply")
        return EXIT_OK

    compat_ok, compat_out = run_compat_check(to_name)
    if not compat_ok:
        print(f"dfe-ops upgrade apply: REFUSED -- compat-check --strict failed for {to_name}:", file=sys.stderr)
        print(compat_out, file=sys.stderr)
        return EXIT_BLOCKED

    if not args.dry_run:
        checks = run_preflight(
            deploy,
            moves,
            kubeconfig=args.kubeconfig,
            argocd_namespace=args.argocd_namespace,
            clickhouse_namespace=args.clickhouse_namespace,
            clickhouse_selector=args.clickhouse_selector,
            clickhouse_merge_threshold=args.clickhouse_merge_threshold,
            nodes_file=Path(args.nodes_file) if args.nodes_file else None,
            backup_marker=args.backup_marker,
        )
        failed = _print_checks(checks)
        if failed:
            print(f"dfe-ops upgrade apply: REFUSED -- {failed} preflight check(s) failed", file=sys.stderr)
            return EXIT_BLOCKED

    grouped = moves_by_stage(moves)
    if args.stop_before and args.stop_before not in [stage for stage, _ in grouped]:
        have = ", ".join(stage for stage, _ in grouped) or "none"
        print(
            f"dfe-ops upgrade apply: --stop-before {args.stop_before!r} does not match any stage this "
            f"plan reaches (have: {have})",
            file=sys.stderr,
        )
        return EXIT_BLOCKED
    commands: list[str] = []

    def emit(cmd: str) -> None:
        commands.append(cmd)
        if args.dry_run:
            print(f"[dry-run] {cmd}", file=sys.stderr)

    for stage_index, (stage, stage_moves) in enumerate(grouped, start=1):
        if args.stop_before and stage == args.stop_before:
            print(
                f"\ndfe-ops upgrade apply: stopping before stage {stage_index}/{len(grouped)} ({stage}) "
                "-- --stop-before",
                file=sys.stderr,
            )
            return EXIT_OK
        print(f"\n=== stage {stage_index}/{len(grouped)}: {stage} ===", file=sys.stderr)
        for move in stage_moves:
            print(f"  {move.step.key}: {move.old} -> {move.new}", file=sys.stderr)

        if not args.dry_run and not _confirm(f"apply stage {stage_index} ({stage})?", assume_yes=args.yes):
            print("dfe-ops upgrade apply: aborted by operator", file=sys.stderr)
            return EXIT_BLOCKED

        for move in stage_moves:
            if not move.step.before:
                continue
            check = BEFORE_CHECKS.get(move.step.key)
            if check is not None:
                emit(f"# verify before-hook: {move.step.before}")
                note = BEFORE_NOTES.get(move.step.key)
                if note is not None:
                    emit(f"# {note(move)}")
                if not args.dry_run:
                    ok, detail = check(args.kubeconfig, move)
                    print(f"  [{'PASS' if ok else 'FAIL'}] before-hook {move.step.key}: {detail}", file=sys.stderr)
                    if not ok:
                        print(
                            f"dfe-ops upgrade apply FAILED at stage {stage_index} ({move.step.key}): "
                            f"before-hook not satisfied -- {move.step.before}",
                            file=sys.stderr,
                        )
                        _print_rollback(stage_moves)
                        return EXIT_BLOCKED
            else:
                emit(f"# manual before-hook: {move.step.before}")
                if not args.dry_run and not _confirm(
                    f"confirm this has been run by hand: {move.step.before}", assume_yes=args.yes
                ):
                    print(
                        f"dfe-ops upgrade apply FAILED at stage {stage_index} ({move.step.key}): "
                        "before-hook not confirmed",
                        file=sys.stderr,
                    )
                    _print_rollback(stage_moves)
                    return EXIT_BLOCKED

        emit(f'set pins.yaml base.dfe-infra = "{to_name}"')
        if not args.dry_run:
            bump_pin_file(deploy, to_name)

        if args.dial:
            previous = deploy / "sizing" / "resolved.yaml"
            emit(
                f"resolve_sizing.py resolve --dial {args.dial} --previous {previous} --migrate "
                f"--out <tmp>, then copy <tmp>/sizing/ over {deploy / 'sizing'}"
            )
            if not args.dry_run:
                ok, detail, _blocked = check_locked_sizing(
                    Path(args.dial),
                    previous,
                    fixtures=Path(args.fixtures) if args.fixtures else None,
                    live=args.live,
                    migrate=True,
                    refresh=deploy,
                )
                print(f"  sizing: {detail}", file=sys.stderr)
                if not ok:
                    print(
                        f"dfe-ops upgrade apply FAILED at stage {stage_index}: resolver did not accept the migration",
                        file=sys.stderr,
                    )
                    _print_rollback(stage_moves)
                    return EXIT_BLOCKED

        for move in stage_moves:
            if not move.step.finalise:
                continue
            if not args.finalise:
                print(
                    f"  finalise pending (manual, after a soak): {move.step.key}: {move.step.finalise}",
                    file=sys.stderr,
                )
                continue
            emit(f"finalise {move.step.key}: {move.step.finalise}")
            if args.dry_run:
                continue
            ok, detail = run_finalise_hook(
                deploy, move, from_name=from_name, to_name=to_name, kubeconfig=args.kubeconfig, assume_yes=args.yes
            )
            print(f"  [{'DONE' if ok else 'PENDING'}] finalise {move.step.key}: {detail}", file=sys.stderr)

        keys = ", ".join(move.step.key for move in stage_moves)
        message = f"chore(upgrade): {to_name} stage {stage_index} -- {keys}"
        emit(f"git -C {deploy} add pins.yaml sizing upgrades")
        emit(f"git -C {deploy} commit -m {message!r}")
        if not args.dry_run:
            _git(deploy, "add", "pins.yaml", "sizing", "upgrades")
            # A later stage's pin already matches an earlier stage's bump, so
            # this can stage nothing -- commit only when something is staged.
            staged = _git(deploy, "diff", "--cached", "--quiet")
            if staged.returncode == 0:
                print(f"  stage {stage_index} ({stage}) changed nothing -- pin and sizing already matched", file=sys.stderr)
            else:
                commit = _git(deploy, "commit", "-m", message)
                if commit.returncode != 0:
                    print(f"dfe-ops upgrade apply FAILED at stage {stage_index}: git commit failed", file=sys.stderr)
                    print(_last_line(commit.stderr) or _last_line(commit.stdout), file=sys.stderr)
                    _print_rollback(stage_moves)
                    return EXIT_BLOCKED

        if args.push:
            emit(f"git -C {deploy} push")
            if not args.dry_run:
                push = _git(deploy, "push")
                if push.returncode != 0:
                    print(f"dfe-ops upgrade apply FAILED at stage {stage_index}: git push failed", file=sys.stderr)
                    _print_rollback(stage_moves)
                    return EXIT_BLOCKED

        emit(f"wait for Argo Applications in {args.argocd_namespace} (timeout {args.timeout}s)")
        if not args.dry_run:
            ok, detail = wait_for_argo(
                args.kubeconfig, argocd_namespace=args.argocd_namespace, timeout=args.timeout
            )
            print(f"  argo: {detail}", file=sys.stderr)
            if not ok:
                print(f"dfe-ops upgrade apply FAILED at stage {stage_index}: Argo did not converge", file=sys.stderr)
                _print_rollback(stage_moves)
                return EXIT_BLOCKED

        # Argo calls the operator Application Healthy as soon as its Deployment
        # is up, which is well before the new operator has reconciled anything.
        operator_move = next((m for m in stage_moves if m.step.key == STRIMZI_OPERATOR_KEY), None)
        if operator_move is not None:
            emit(
                f"wait for every Kafka CR to report operatorLastSuccessfulVersion "
                f"{operator_move.new} (timeout {args.timeout}s)"
            )
            if not args.dry_run:
                ok, detail = wait_for_kafka_operator_version(
                    args.kubeconfig, operator_move.new, timeout=args.timeout
                )
                print(f"  strimzi: {detail}", file=sys.stderr)
                if not ok:
                    print(
                        f"dfe-ops upgrade apply FAILED at stage {stage_index}: the Strimzi operator "
                        "did not reconcile every Kafka CR",
                        file=sys.stderr,
                    )
                    _print_rollback(stage_moves)
                    return EXIT_BLOCKED

    if args.dry_run:
        print(f"\n[dry-run] {len(commands)} command(s) would run; nothing was executed", file=sys.stderr)
    else:
        print(f"\ndfe-ops upgrade apply OK: {from_name} -> {to_name} ({len(grouped)} stage(s))", file=sys.stderr)
    return EXIT_OK


def _print_rollback(stage_moves: list[Move]) -> None:
    for move in stage_moves:
        note = move.step.rollback or ("none (finalise: " + move.step.finalise + ")" if move.step.finalise else "")
        print(f"  rollback ({move.step.key}): {note or 'not declared -- restore from the backup marker'}", file=sys.stderr)


# --- rollback ----------------------------------------------------------------


def cmd_upgrade_rollback(args: argparse.Namespace) -> int:
    deploy = Path(args.deploy)
    try:
        root = load_versions_root()
        to_name, to_pins = resolve_stack(root, args.to)
        from_name = read_deploy_pin(deploy)
        _, from_pins = resolve_stack(root, from_name)
    except UpgradeError as err:
        print(f"dfe-ops upgrade rollback: {err}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    steps = load_steps()
    # A rollback is the reverse move: from the deploy's CURRENT stack back to
    # --to. Diffing FROM -> TO the normal way and reading the notes off the
    # step names the FORWARD direction, so a step's one-way-ness is the same
    # fact regardless of which way the pin is about to move.
    moves = plan_moves(steps, flatten_stack(to_pins), flatten_stack(from_pins))

    # A step is refused for one of two reasons: `rollback: none` with no
    # `finalise` at all has no soak to wait out -- it is unconditionally
    # one-way. A `finalise`-bearing step is refused only once that finalise
    # has ACTUALLY run, read from the marker `apply --finalise` writes, not
    # from the pin diff alone -- see read_finalised_keys().
    finalised = read_finalised_keys(deploy)
    blocked: list[tuple[Move, str]] = []
    pending_finalise: list[Move] = []
    for move in moves:
        step = move.step
        if step.finalise:
            if step.key in finalised:
                blocked.append((move, f"finalise already ran ({finalised[step.key]}): {step.finalise}"))
            else:
                pending_finalise.append(move)
            continue
        if step.rollback.strip().lower() == "none":
            blocked.append((move, f"no rollback path: {step.rollback}"))

    if blocked:
        names = ", ".join(move.step.key for move, _ in blocked)
        reasons = "; ".join(f"{move.step.key}: {reason}" for move, reason in blocked)
        print(
            f"dfe-ops upgrade rollback: REFUSED -- {names} carries no rollback path ({reasons})",
            file=sys.stderr,
        )
        return EXIT_BLOCKED

    if args.check_cluster:
        ok, detail = check_cluster_metadata_version(
            args.kubeconfig, moves, kafka_name=args.kafka_name, kafka_namespace=args.kafka_namespace
        )
        print(f"[{'PASS' if ok else 'FAIL'}] cluster metadata.version check: {detail}", file=sys.stderr)
        if not ok:
            print(
                "dfe-ops upgrade rollback: REFUSED -- --check-cluster found the live cluster already past "
                "this step (no marker was written for it)",
                file=sys.stderr,
            )
            return EXIT_BLOCKED

    for move in pending_finalise:
        print(
            f"dfe-ops upgrade rollback: {move.step.key} finalise has not run yet -- reversing the pin; "
            f"the soak can be abandoned safely ({move.step.finalise})",
            file=sys.stderr,
        )

    print(render_plan(moves, from_stack=from_name, to_stack=to_name))
    if args.dry_run:
        print(f"[dry-run] set pins.yaml base.dfe-infra = \"{to_name}\"")
        print(f"[dry-run] git -C {deploy} commit -m 'chore(upgrade): rollback to {to_name}'")
        if args.push:
            print(f"[dry-run] git -C {deploy} push")
        return EXIT_OK

    bump_pin_file(deploy, to_name)
    keys = ", ".join(move.step.key for move in moves) or "no pinned key moved"
    message = f"chore(upgrade): rollback to {to_name} -- {keys}"
    _git(deploy, "add", "pins.yaml", "sizing", "upgrades")
    commit = _git(deploy, "commit", "-m", message)
    if commit.returncode != 0:
        print(f"dfe-ops upgrade rollback: git commit failed: {_last_line(commit.stderr)}", file=sys.stderr)
        return EXIT_BLOCKED
    if args.push:
        push = _git(deploy, "push")
        if push.returncode != 0:
            print(f"dfe-ops upgrade rollback: git push failed: {_last_line(push.stderr)}", file=sys.stderr)
            return EXIT_BLOCKED
    print(f"dfe-ops upgrade rollback OK: {from_name} -> {to_name}", file=sys.stderr)
    return EXIT_OK


# --- parser --------------------------------------------------------------------


def _add_deploy_target_args(parser: argparse.ArgumentParser, *, to_required: bool = False) -> None:
    parser.add_argument("--deploy", required=True, help="the dfe-deploy checkout (pins.yaml's directory)")
    parser.add_argument(
        "--to",
        required=to_required,
        default=None,
        help="target versions.yaml stack (default: the `current` pointer)",
    )


def _add_sizing_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dial", default=None, help="deployment.yaml dial to re-resolve sizing against (default: skip the check)"
    )
    parser.add_argument("--fixtures", default=None, help="resolve_sizing.py --fixtures (offline sizing check)")
    parser.add_argument("--live", action="store_true", help="resolve_sizing.py --live (calls the real cloud API)")


def _add_preflight_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kubeconfig", default=None, help="kubeconfig for the target cluster")
    parser.add_argument("--argocd-namespace", default=DEFAULT_ARGOCD_NAMESPACE, help="namespace holding Argo Applications")
    parser.add_argument("--clickhouse-namespace", default=DEFAULT_CLICKHOUSE_NAMESPACE, help="namespace ClickHouse runs in")
    parser.add_argument("--clickhouse-selector", default=DEFAULT_CLICKHOUSE_SELECTOR, help="label selector for a ClickHouse pod")
    parser.add_argument(
        "--clickhouse-merge-threshold", type=float, default=DEFAULT_CLICKHOUSE_MERGE_THRESHOLD,
        help="refuse when a merge has run longer than this many seconds",
    )
    parser.add_argument("--nodes-file", default=None, help="sizing/<tier>.nodes.json (default: <deploy>/sizing/scale.nodes.json)")
    parser.add_argument("--backup-marker", default=DEFAULT_BACKUP_MARKER, help="path (relative to --deploy) proving a backup was taken")


def add_upgrade_subparser(sub: argparse._SubParsersAction) -> None:
    """Register `dfe-ops upgrade` and its verbs."""
    upgrade = sub.add_parser(
        "upgrade",
        help="plan/preflight/apply/rollback a stack upgrade against a deployment repo",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    verbs = upgrade.add_subparsers(dest="upgrade_verb", required=True, metavar="<verb>")

    plan = verbs.add_parser(
        "plan",
        help="diff the deployment's pinned stack against the target, ordered by upgrade-order.yaml",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_deploy_target_args(plan)
    _add_sizing_args(plan)
    plan.set_defaults(func=cmd_upgrade_plan)

    preflight = verbs.add_parser(
        "preflight",
        help="the checks apply refuses to run without",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_deploy_target_args(preflight)
    _add_preflight_args(preflight)
    preflight.set_defaults(func=cmd_upgrade_preflight)

    apply_ = verbs.add_parser(
        "apply",
        help="execute the plan stage by stage",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_deploy_target_args(apply_)
    _add_sizing_args(apply_)
    _add_preflight_args(apply_)
    apply_.add_argument("--yes", action="store_true", help="do not ask for confirmation between stages")
    apply_.add_argument("--push", action="store_true", help="git push after each stage's commit")
    apply_.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="bounded wait (seconds) for Argo to converge, per stage")
    apply_.add_argument("--dry-run", action="store_true", help="print every command without running it")
    apply_.add_argument(
        "--finalise",
        action="store_true",
        help="run a reached stage's finalise hook after confirming the soak is over, writing the marker rollback reads",
    )
    apply_.add_argument(
        "--stop-before",
        default=None,
        metavar="<stage-key>",
        help="stop the run before this upgrade-order.yaml stage (e.g. 30-services), touching nothing in it or after",
    )
    apply_.set_defaults(func=cmd_upgrade_apply)

    rollback = verbs.add_parser(
        "rollback",
        help="the reverse plan, refusing by name any step whose finalise has actually run",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_deploy_target_args(rollback, to_required=True)
    rollback.add_argument("--kubeconfig", default=None, help="kubeconfig for the target cluster")
    rollback.add_argument("--push", action="store_true", help="git push after the rollback commit")
    rollback.add_argument("--dry-run", action="store_true", help="print what would happen without running it")
    rollback.add_argument(
        "--check-cluster",
        action="store_true",
        help="when kubectl is reachable, also refuse if the live Kafka CR already shows a bumped "
        "status.kafkaMetadataVersion, even with no marker",
    )
    rollback.add_argument("--kafka-name", default=DEFAULT_KAFKA_NAME, help="Kafka CR name --check-cluster reads")
    rollback.add_argument("--kafka-namespace", default=DEFAULT_KAFKA_NAMESPACE, help="namespace the Kafka CR runs in")
    rollback.set_defaults(func=cmd_upgrade_rollback)
