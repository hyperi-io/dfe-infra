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
    dfe-ops upgrade apply --deploy <dir> [--to <stack>] [--from <stack>] [--yes] [--push]
                           [--dry-run] [--finalise] [--stop-before <stage-key>]
                           [--target-revision <ref>]
    dfe-ops upgrade rollback --deploy <dir> --to <stack> [--dry-run] [--push]
                           [--skip-cluster-check] [--target-revision <ref>]

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
               no long merge, every Strimzi CRD the 1.x operator needs stores
               v1 only (only checked when the plan crosses the conversion), the
               on-prem node capacity holds the new sizing, and a backup marker
               exists when the plan carries a one-way step. Each check prints
               PASS/FAIL with the evidence line that decided it.

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
               new operator version, under the same bound.

               The first stage that moves an Argo-managed component also moves
               the cluster secret's `dfe.hyperi.io/target_revision` from the
               FROM stack's tag to the TO stack's, after that stage's before
               hooks pass and its commit is pushed -- the ref every chart and
               operator Application renders from. A secret tracking a branch is
               left alone, and one pinned to a commit refuses unless
               --target-revision names the ref. A retarget needs --push.

               When the plan moves services.kafka-version on a Strimzi
               cluster, that same stage first writes two holds into the
               deploy's infra/kafka.yaml: kafka.metadataVersion at the live
               status.kafkaMetadataVersion, and kafka.version at the FROM
               version so the operator moves before the brokers do. The stage
               that carries services.kafka-version drops the version hold and
               waits for every Kafka CR to report the new version. The
               metadata hold stays until finalise.

               Confirms before each stage unless --yes. Stops at the first
               failure and prints that step's rollback note. --stop-before
               <stage-key> stops the walk before that upgrade-order.yaml stage,
               touching nothing in it or after. A reached step carrying a
               `finalise` note is printed and left pending unless --finalise is
               given, in which case apply asks whether the soak is over and, on
               yes, runs the step's finalise program (services.kafka-version
               drops the metadata hold) and records it in the marker `rollback`
               reads. --from <stack> names the FROM stack when pins.yaml already
               names the target, which is how a finalise after the soak, or a
               resumed apply, is run. --dry-run prints every command it would
               run, including a reached finalise and a --stop-before halt, and
               touches nothing.

    rollback   The reverse plan for TO -> the deploy's current FROM, refusing
               by name a step with `rollback: none` and no `finalise` note (an
               unconditional one-way step), or a `finalise`-bearing step whose
               finalise has ALREADY run -- read from the marker `apply
               --finalise` writes (`upgrades/<from>-to-<to>.finalised`), not
               from the pin diff alone. A `finalise`-bearing step with no
               marker yet is reversed like any other step, with a note that
               the soak can be abandoned safely. A rollback that moves
               services.kafka-version also reads every live Kafka CR and
               refuses when its status.kafkaMetadataVersion is above what the
               rollback target's Kafka version runs, marker or not;
               --skip-cluster-check rolls the pin back without that read and
               without the retarget. Like apply, it moves the cluster secret's
               target_revision back to the rollback target's tag.

Nothing here executes a `before` or `finalise` note as a shell command -- they
are runbook prose, not argv. `apply` checks the one `before` note this repo
already has a program for (the Strimzi stored-version conversion) and
otherwise prints the note and asks for confirmation that an operator ran it by
hand. That one check refuses with the exact conversion commands, from the
tarball of the version the cluster is RUNNING rather than the one it is moving
to; it never runs them, because crd-upgrade is one-way and needs a JVM and
cluster-admin rights on the CRDs. A `finalise` note is printed and left pending
unless --finalise is passed, in which case apply asks for confirmation, runs
the step's FINALISE_HOOKS entry if it has one, and only writes the marker once
both succeed.
"""

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
DEFAULT_TIER = "scale"
DEFAULT_BACKUP_MARKER = "upgrades/.backup-ok"
DEFAULT_TIMEOUT = 900
_SYNC_POLL_INTERVAL = 10.0

# Every CRD the 1.x operator requires stored as v1: CRD_NAMES in the conversion tool's crd-upgrade.
STRIMZI_CRDS = (
    "kafkas.kafka.strimzi.io",
    "kafkanodepools.kafka.strimzi.io",
    "kafkatopics.kafka.strimzi.io",
    "kafkausers.kafka.strimzi.io",
    "kafkaconnects.kafka.strimzi.io",
    "kafkaconnectors.kafka.strimzi.io",
    "kafkabridges.kafka.strimzi.io",
    "kafkamirrormaker2s.kafka.strimzi.io",
    "kafkarebalances.kafka.strimzi.io",
    "strimzipodsets.core.strimzi.io",
)

# The upgrade-order.yaml step whose pin move IS the Strimzi operator upgrade.
STRIMZI_OPERATOR_KEY = "operators.strimzi-kafka-operator"

# The upgrade-order.yaml step whose pin move rolls the Kafka brokers.
KAFKA_VERSION_KEY = "services.kafka-version"

# The Argo cluster secret bootstrap writes (bootstrap/templates/cluster-secret.yaml.tpl).
CLUSTER_SECRET = "dfe-cluster"
TARGET_REVISION_ANNOTATION = "dfe.hyperi.io/target_revision"
STACK_VERSION_ANNOTATION = "dfe.hyperi.io/stack_version"

# The deploy repo's kafka chart overlay, layered last by appsets/layer2-data.yaml.
KAFKA_OVERLAY = Path("infra") / "kafka.yaml"
# Marks a line apply wrote, so only those are ever rewritten or dropped.
HOLD_MARK = "# dfe-ops upgrade hold"


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


def _absent(err: str) -> bool:
    """Whether a kubectl error says the object or its type does not exist, as
    opposed to a cluster that could not answer."""
    return "NotFound" in err or "the server doesn't have a resource type" in err


def _git(deploy: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(deploy), *args])


# The deploy paths an upgrade commit carries. `git add` with one missing
# pathspec stages nothing at all, so only the ones that exist are named.
STAGED_PATHS = ("pins.yaml", "sizing", "upgrades", str(KAFKA_OVERLAY))


def _stage(deploy: Path) -> tuple[bool, str]:
    present = [p for p in STAGED_PATHS if (deploy / p).exists()]
    result = _git(deploy, "add", *present)
    if result.returncode != 0:
        return False, _last_line(result.stderr) or "git add failed"
    return True, " ".join(present)


def _wait_until(
    check: Callable[[], tuple[bool, str]], *, timeout: float, stuck: str, sleep=time.sleep, now=time.monotonic
) -> tuple[bool, str]:
    """Poll `check` until it passes or `timeout` seconds pass; `stuck` heads the
    timeout detail. `sleep`/`now` are injected so a test drives it without a clock."""
    deadline = now() + timeout
    while True:
        ok, detail = check()
        if ok:
            return True, detail
        remaining = deadline - now()
        if remaining <= 0:
            return False, f"{stuck} after {timeout:.0f}s -- {detail}"
        sleep(min(_SYNC_POLL_INTERVAL, remaining))


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


def _source_revisions(app: dict) -> list[str]:
    spec = app.get("spec") or {}
    sources = [spec["source"]] if isinstance(spec.get("source"), dict) else list(spec.get("sources") or [])
    return [str(src.get("targetRevision") or "") for src in sources if isinstance(src, dict)]


def check_argo_apps(
    kubeconfig: str | None, namespace: str = DEFAULT_ARGOCD_NAMESPACE, *, stale_revision: str = ""
) -> tuple[bool, str]:
    """Every Argo Application in `namespace` is Synced and Healthy.

    Reads kubectl directly (`applications.argoproj.io`), the same path
    dfe-ops refresh/cycle already use -- no second CLI (argocd) dependency.
    With `stale_revision`, an Application still rendering from that ref also
    fails: right after a retarget every Application is still Synced to the old
    one until the ApplicationSet controller regenerates it.
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
        if stale_revision and stale_revision in _source_revisions(app):
            bad.append(f"{name} (still renders from {stale_revision})")
        elif sync != "Synced" or health != "Healthy":
            bad.append(f"{name} (sync {sync}, health {health})")
    if bad:
        extra = f", +{len(bad) - 6} more" if len(bad) > 6 else ""
        return False, f"{len(bad)} app(s) not Synced/Healthy: {', '.join(bad[:6])}{extra}"
    return True, f"{len(items)} Application(s) Synced and Healthy"


def check_no_kafka_rebalance(kubeconfig: str | None) -> tuple[bool, str]:
    rc, doc, err = _kubectl_json(kubeconfig, "get", "kafkarebalances.kafka.strimzi.io", "-A")
    if rc != 0:
        if _absent(err):
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
    """Every Strimzi CRD the 1.x operator needs stores v1 only -- the sign
    `bin/v1-api-conversion.sh convert-resource` (then `crd-upgrade`) already
    ran, which 1.x requires before the operator upgrade lands.

    A CRD that is not installed has nothing stored to convert; a CRD kubectl
    could not read fails, because an unanswered read proves nothing.
    """
    stale = []
    unread = []
    checked = 0
    for crd in crds:
        rc, doc, err = _kubectl_json(kubeconfig, "get", "crd", crd)
        if rc != 0:
            if not _absent(err):
                unread.append(f"{crd} ({err or 'kubectl failed'})")
            continue
        checked += 1
        stored = (doc.get("status") or {}).get("storedVersions") or []
        non_v1 = [v for v in stored if v != "v1"]
        if non_v1:
            stale.append(f"{crd} (stored: {', '.join(stored)})")
    if unread:
        return False, f"cannot read {len(unread)} Strimzi CRD(s): {', '.join(unread)}"
    if checked == 0:
        return True, "no Strimzi CRDs installed -- nothing to convert"
    if stale:
        return False, f"{len(stale)} CRD(s) still store a pre-v1 version: {', '.join(stale)}"
    return True, f"{checked} Strimzi CRD(s) store v1 only"


def strimzi_conversion_tool(operator_version: str) -> str:
    """The conversion tool's release artefact for one operator version."""
    return f"strimzi-v1-api-conversion-{operator_version}.tar.gz"


# The two conversion runs, in order, from the Strimzi 1.0.0 API conversion guide.
CONVERSION_COMMANDS = (
    "bin/v1-api-conversion.sh convert-resource --all-namespaces",
    "bin/v1-api-conversion.sh crd-upgrade",
)


def conversion_tool_line(move: Move) -> str:
    """Which release's conversion tarball to fetch for this operator move, and
    the two commands to run from it.

    The tool rewrites the CRs the RUNNING operator wrote, so it comes from that
    release rather than the one the pin is moving to.
    """
    return (
        f"fetch {strimzi_conversion_tool(move.old)} from the RUNNING operator release "
        f"{move.old}, never the target {move.new}, then run "
        f"`{CONVERSION_COMMANDS[0]}` and `{CONVERSION_COMMANDS[1]}`"
    )


def check_strimzi_conversion_before(kubeconfig: str | None, move: Move) -> tuple[bool, str]:
    """The strimzi-kafka-operator step's before-hook, naming its own tool.

    A stale detail line would leave an operator reaching for the target
    version's tarball, which is the wrong artefact for the CRs on disk.
    """
    ok, detail = check_strimzi_conversion(kubeconfig)
    return ok, f"{detail}; {conversion_tool_line(move)}"


def _list_kafka_crs(kubeconfig: str | None, namespace: str | None = None) -> tuple[list[dict] | None, str]:
    """Every Strimzi Kafka CR: an empty list when the CRD is absent, None plus
    the error when kubectl could not answer. Reads every namespace unless the
    caller names one, because "every Kafka CR" is the claim callers make."""
    args = ["get", "kafkas.kafka.strimzi.io", *(["-n", namespace] if namespace else ["-A"])]
    rc, doc, err = _kubectl_json(kubeconfig, *args)
    if rc != 0:
        return ([], "no Kafka CRD on this cluster") if _absent(err) else (None, err)
    return list(doc.get("items") or []), ""


def _cr_name(item: dict) -> str:
    meta = item.get("metadata") or {}
    return f"{meta.get('namespace', '')}/{meta.get('name', '<unnamed>')}"


def _major_minor(version: str) -> tuple[int, int] | None:
    """(major, minor) of a Kafka version (`4.3.1`) or a metadata version
    (`4.3-IV0`, `4.2`); None when the text carries neither."""
    match = re.match(r"\s*(\d+)\.(\d+)", version or "")
    return (int(match[1]), int(match[2])) if match else None


def check_kafka_status(
    kubeconfig: str | None,
    field: str,
    want: str,
    *,
    accept: Callable[[str], bool] | None = None,
    namespace: str | None = None,
) -> tuple[bool, str]:
    """Every Kafka CR's `status.<field>` is `want` (or passes `accept`).

    No Kafka CRD, or no Kafka CR, passes: a cluster with no Strimzi broker has
    nothing to report. A CR that has not written the field is behind.
    """
    accept = accept or (lambda seen: seen == want)
    items, err = _list_kafka_crs(kubeconfig, namespace)
    if items is None:
        return False, f"cannot list Kafka CRs: {err}"
    if not items:
        return True, f"{err or 'no Kafka CR on this cluster'} -- no {field} to wait on"
    behind = []
    for item in items:
        seen = str((item.get("status") or {}).get(field) or "")
        if not seen or not accept(seen):
            behind.append(f"{_cr_name(item)} ({field} {seen or 'unset'})")
    if behind:
        extra = f", +{len(behind) - 6} more" if len(behind) > 6 else ""
        return False, f"{len(behind)} Kafka CR(s) not yet at {field} {want}: {', '.join(behind[:6])}{extra}"
    return True, f"{len(items)} Kafka CR(s) report {field} {want}"


def check_kafka_operator_version(
    kubeconfig: str | None, version: str, *, namespace: str | None = None
) -> tuple[bool, str]:
    """Every Kafka CR reports `status.operatorLastSuccessfulVersion` at `version`.

    The CR's own `Ready` condition stays True and stale across an operator
    upgrade, so a wait on it returns at once and proves nothing; this field is
    the one the new operator writes only after it has reconciled the cluster.
    """
    return check_kafka_status(kubeconfig, "operatorLastSuccessfulVersion", version, namespace=namespace)


def check_kafka_version(kubeconfig: str | None, version: str) -> tuple[bool, str]:
    """Every Kafka CR reports `status.kafkaVersion` at `version` -- the roll is done."""
    return check_kafka_status(kubeconfig, "kafkaVersion", version)


def check_kafka_metadata_moved(kubeconfig: str | None, kafka_version: str) -> tuple[bool, str]:
    """Every Kafka CR's `status.kafkaMetadataVersion` is on `kafka_version`'s
    major.minor -- the finalise has landed."""
    target = _major_minor(kafka_version)
    return check_kafka_status(
        kubeconfig, "kafkaMetadataVersion", f"{kafka_version}'s line",
        accept=lambda seen: _major_minor(seen) == target,
    )


@dataclass(frozen=True, slots=True)
class KafkaState:
    """What the live Strimzi Kafka CRs agree on: the broker version and the
    metadata version. `crs` is 0 on a cluster with no Strimzi broker."""

    crs: int
    version: str = ""
    metadata: str = ""


def read_kafka_state(kubeconfig: str | None) -> KafkaState:
    """The live Kafka version and metadata version every Strimzi Kafka CR reports.

    Raises UpgradeError when kubectl cannot answer, or when the CRs disagree:
    infra/kafka.yaml carries one hold for the whole chart, so there is no
    single value to pin.
    """
    items, err = _list_kafka_crs(kubeconfig)
    if items is None:
        raise UpgradeError(f"cannot list Kafka CRs: {err}")
    if not items:
        return KafkaState(crs=0)
    seen: dict[tuple[str, str], list[str]] = {}
    for item in items:
        status = item.get("status") or {}
        pair = (str(status.get("kafkaVersion") or ""), str(status.get("kafkaMetadataVersion") or ""))
        seen.setdefault(pair, []).append(_cr_name(item))
    if len(seen) > 1:
        detail = "; ".join(f"{', '.join(names)}: kafka {v or 'unset'}, metadata {m or 'unset'}" for (v, m), names in seen.items())
        raise UpgradeError(f"the Kafka CRs disagree, so no one hold fits them all: {detail}")
    ((version, metadata),) = seen
    return KafkaState(crs=len(items), version=version, metadata=metadata)


def check_rollback_metadata(kubeconfig: str | None, target_kafka: str) -> tuple[bool, str]:
    """Whether the brokers can go back to Kafka `target_kafka` at all.

    Kafka cannot run a metadata version newer than its own release line, so a
    rollback to 4.2.0 under a live 4.3-IV0 strands the brokers. That happens
    with no finalise marker whenever the metadata version moved without
    `apply --finalise`: unpinned, Strimzi bumps it as soon as a version roll
    finishes. Every Kafka CR's live `status.kafkaMetadataVersion` is compared
    by major.minor against the target. No Strimzi broker passes; a cluster
    kubectl cannot read, or a CR with no metadata version, fails.
    """
    target = _major_minor(target_kafka)
    if target is None:
        return False, f"cannot read a major.minor from the rollback target's Kafka version {target_kafka!r}"
    items, err = _list_kafka_crs(kubeconfig)
    if items is None:
        return False, f"cannot list Kafka CRs: {err}"
    if not items:
        return True, f"{err or 'no Kafka CR on this cluster'} -- no metadata version to strand"
    above = []
    for item in items:
        live = str((item.get("status") or {}).get("kafkaMetadataVersion") or "")
        live_mm = _major_minor(live)
        if live_mm is None or live_mm > target:
            above.append(f"{_cr_name(item)} (kafkaMetadataVersion {live or 'unset'})")
    if above:
        return False, f"Kafka {target_kafka} cannot run the live metadata version: {', '.join(above)}"
    return True, f"{len(items)} Kafka CR(s) carry a metadata version Kafka {target_kafka} runs"


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
    conversion = next((move for move in moves if "stored-version conversion" in move.step.before), None)
    if conversion is not None:
        checks.append(("strimzi stored-version conversion", *check_strimzi_conversion_before(kubeconfig, conversion)))
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


# --- the kafka overlay holds (infra/kafka.yaml) ---------------------------------
# Only lines ending in HOLD_MARK are rewritten or dropped, so a deployer's own value is never touched.

_KAFKA_BLOCK = re.compile(r"^kafka:\s*(#.*)?$")


def _kafka_block(lines: list[str]) -> tuple[int, int, str] | None:
    """(index of the top-level `kafka:` line, end of its block, child indent),
    or None when the file has no `kafka:` key."""
    start = next((i for i, line in enumerate(lines) if _KAFKA_BLOCK.match(line)), None)
    if start is None:
        if any(line.startswith("kafka:") for line in lines):
            raise UpgradeError(f"{KAFKA_OVERLAY}: `kafka:` is not a block mapping -- edit the hold by hand")
        return None
    end = len(lines)
    indent = ""
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not lines[i][0].isspace():
            end = i
            break
        if not indent:
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    return start, end, indent or "  "


def _child_line(lines: list[str], block: tuple[int, int, str], key: str) -> int | None:
    start, end, indent = block
    pattern = re.compile(rf"^{re.escape(indent)}{re.escape(key)}:(\s|$)")
    return next((i for i in range(start + 1, end) if pattern.match(lines[i])), None)


def overlay_entry(text: str, key: str) -> tuple[str, bool] | None:
    """(value, written by apply) of `kafka.<key>` in an overlay's text, or None."""
    lines = text.splitlines()
    block = _kafka_block(lines)
    index = _child_line(lines, block, key) if block else None
    if index is None:
        return None
    line = lines[index].rstrip()
    value = line.split(":", 1)[1].split(" #", 1)[0].strip()
    return _unquote(value), line.endswith(HOLD_MARK)


def set_overlay_hold(text: str, key: str, value: str) -> str:
    """Write `kafka.<key>: "<value>"` as a hold, replacing an earlier hold.

    Raises UpgradeError when the deployer set the key themselves.
    """
    lines = text.splitlines()
    entry = f'{key}: "{value}"  {HOLD_MARK}'
    block = _kafka_block(lines)
    if block is None:
        gap = [""] if lines and lines[-1].strip() else []
        lines += [*gap, "kafka:", f"  {entry}"]
        return "\n".join(lines) + "\n"
    index = _child_line(lines, block, key)
    if index is None:
        lines.insert(block[0] + 1, f"{block[2]}{entry}")
    elif lines[index].rstrip().endswith(HOLD_MARK):
        lines[index] = f"{block[2]}{entry}"
    else:
        raise UpgradeError(f"{KAFKA_OVERLAY} sets kafka.{key} itself, so apply will not overwrite it")
    return "\n".join(lines) + "\n"


def drop_overlay_hold(text: str, key: str) -> str:
    """Remove a `kafka.<key>` hold, and the `kafka:` line with it when nothing
    else is left under it -- an empty `kafka:` is a YAML null, which Helm reads
    as deleting every chart default under kafka."""
    lines = text.splitlines()
    block = _kafka_block(lines)
    index = _child_line(lines, block, key) if block else None
    if block is None or index is None or not lines[index].rstrip().endswith(HOLD_MARK):
        return text
    del lines[index]
    start, end, _indent = block
    children = [ln for ln in lines[start + 1 : end - 1] if ln.strip() and not ln.strip().startswith("#")]
    if not children:
        del lines[start]
    return "\n".join(lines) + "\n" if lines else ""


def _read_overlay(deploy: Path) -> str:
    path = deploy / KAFKA_OVERLAY
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _write_overlay(deploy: Path, text: str) -> None:
    path = deploy / KAFKA_OVERLAY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@dataclass(frozen=True, slots=True)
class KafkaHold:
    """The infra/kafka.yaml holds apply writes for a Strimzi Kafka version move.
    An empty field writes nothing for that key."""

    metadata: str = ""
    version: str = ""


def plan_kafka_hold(
    kubeconfig: str | None, deploy: Path, kafka_move: Move | None, finalised: dict[str, str]
) -> tuple[KafkaHold | None, str]:
    """Which holds the move needs, decided from the live Kafka CRs before any
    stage runs, so a cluster in an unexpected state refuses before anything moves.

    The metadata hold pins the CURRENT metadata version: unset, Strimzi moves
    it to the new version's default the moment the roll finishes, which ends
    the soak before it starts. The version hold keeps the brokers on the FROM
    version until the stage that carries services.kafka-version, so the
    operator moves first; it is skipped once the brokers already run TO, so a
    resumed apply never rolls them back.
    """
    if kafka_move is None:
        return None, "the plan does not move services.kafka-version"
    if KAFKA_VERSION_KEY in finalised:
        return None, f"{KAFKA_VERSION_KEY} finalise already ran -- nothing to hold"
    state = read_kafka_state(kubeconfig)
    if state.crs == 0:
        return None, "no Strimzi Kafka CR on this cluster -- nothing to hold"
    if state.version not in (kafka_move.old, kafka_move.new):
        raise UpgradeError(
            f"the Kafka CRs run {state.version or 'no reported version'}, neither "
            f"{kafka_move.old} nor {kafka_move.new} -- settle the brokers before upgrading"
        )
    text = _read_overlay(deploy)
    pinned = overlay_entry(text, "metadataVersion")
    if pinned is None and not state.metadata:
        raise UpgradeError("the Kafka CR reports no status.kafkaMetadataVersion, so there is no value to hold")
    deployer_version = overlay_entry(text, "version")
    version_held_by_hand = deployer_version is not None and not deployer_version[1]
    hold = KafkaHold(
        metadata=state.metadata if pinned is None else "",
        version=kafka_move.old if state.version == kafka_move.old and not version_held_by_hand else "",
    )
    detail = f"metadata held at {pinned[0] if pinned else state.metadata}"
    if hold.version:
        detail += f", brokers held at {hold.version} until the {kafka_move.step.stage} stage"
    return hold, detail


def release_metadata_hold(deploy: Path, move: Move) -> tuple[bool, str]:
    """services.kafka-version's finalise: drop the kafka.metadataVersion hold,
    so Strimzi moves the metadata version to `move.new`'s default once Argo
    syncs. One way: Kafka cannot run a metadata version above its own line."""
    text = _read_overlay(deploy)
    entry = overlay_entry(text, "metadataVersion")
    if entry is None:
        return True, f"no kafka.metadataVersion in {KAFKA_OVERLAY} -- the metadata version is not held"
    value, held = entry
    if not held:
        return False, (
            f"{KAFKA_OVERLAY} sets kafka.metadataVersion {value} itself -- raise it to the "
            f"{move.new} line by hand"
        )
    _write_overlay(deploy, drop_overlay_hold(text, "metadataVersion"))
    return True, f"dropped the kafka.metadataVersion {value} hold; Strimzi moves it to {move.new}'s default"


# --- the cluster secret's target revision ----------------------------------------
# Every chart and operator Application renders from this ref; pins.yaml reaches only `dfe-stack resolve`.


def _norm_ref(ref: str) -> str:
    return _norm_stack(ref) if ref else ""


def read_target_revision(kubeconfig: str | None, namespace: str) -> str:
    """The cluster secret's target_revision. Raises UpgradeError when unreadable."""
    jsonpath = "jsonpath={.metadata.annotations." + TARGET_REVISION_ANNOTATION.replace(".", "\\.") + "}"
    result = _kubectl(
        kubeconfig, "-n", namespace, f"--request-timeout={DEFAULT_KUBECTL_REQUEST_TIMEOUT}",
        "get", "secret", CLUSTER_SECRET, "-o", jsonpath,
    )
    if result.returncode != 0:
        raise UpgradeError(
            f"cannot read {TARGET_REVISION_ANNOTATION} on secret/{CLUSTER_SECRET} in {namespace}: "
            f"{_last_line(result.stderr) or 'kubectl failed'}"
        )
    return (result.stdout or "").strip()


def decide_retarget(current: str, from_name: str, to_name: str, explicit: str | None) -> tuple[str | None, str]:
    """(the ref to write, why) -- None leaves the secret as it is.

    A tag-pinned secret names the FROM stack and moves to the TO tag (the stack
    name is the git tag). A branch is tracked on purpose and left alone. A
    commit SHA could be any stack, so it refuses without an explicit ref.
    """
    if explicit:
        if explicit == current:
            return None, f"already targets {current}"
        return explicit, f"{current or '(unset)'} -> {explicit} (--target-revision)"
    if not current:
        raise UpgradeError(f"secret/{CLUSTER_SECRET} carries no {TARGET_REVISION_ANNOTATION} -- pass --target-revision")
    if _norm_ref(current) == _norm_ref(to_name):
        return None, f"already targets {current}"
    if _norm_ref(current) == _norm_ref(from_name):
        return to_name, f"{current} -> {to_name}"
    if re.fullmatch(r"[0-9a-f]{40}", current):
        raise UpgradeError(
            f"secret/{CLUSTER_SECRET} pins commit {current}, which names no stack -- pass "
            f"--target-revision <ref> to say where the charts go"
        )
    return None, f"tracks {current}, a branch, so the charts already follow it -- left as it is"


def write_target_revision(kubeconfig: str | None, namespace: str, ref: str, stack: str) -> tuple[bool, str]:
    """Move the cluster secret's target_revision, and its stack_version with it."""
    result = _kubectl(
        kubeconfig, "-n", namespace, "annotate", "--overwrite", f"secret/{CLUSTER_SECRET}",
        f"{TARGET_REVISION_ANNOTATION}={ref}", f"{STACK_VERSION_ANNOTATION}={stack}",
    )
    if result.returncode != 0:
        return False, _last_line(result.stderr) or "kubectl annotate failed"
    return True, f"{TARGET_REVISION_ANNOTATION}={ref}, {STACK_VERSION_ANNOTATION}={stack}"


def retarget_command(namespace: str, ref: str, stack: str) -> str:
    return (
        f"kubectl -n {namespace} annotate --overwrite secret/{CLUSTER_SECRET} "
        f"{TARGET_REVISION_ANNOTATION}={ref} {STACK_VERSION_ANNOTATION}={stack}"
    )


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
    kubeconfig: str | None,
    *,
    argocd_namespace: str,
    timeout: float,
    stale_revision: str = "",
    sleep=time.sleep,
    now=time.monotonic,
) -> tuple[bool, str]:
    """Block until check_argo_apps reports every Application Synced and
    Healthy (and none on `stale_revision`), or `timeout` seconds pass."""
    return _wait_until(
        lambda: check_argo_apps(kubeconfig, argocd_namespace, stale_revision=stale_revision),
        timeout=timeout, stuck="still not converged", sleep=sleep, now=now,
    )


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
    watches.
    """
    return _wait_until(
        lambda: check_kafka_operator_version(kubeconfig, version, namespace=namespace),
        timeout=timeout, stuck="still not reconciled", sleep=sleep, now=now,
    )


# --- plan ----------------------------------------------------------------------


def _load_from_to(
    deploy: Path, to_arg: str | None, from_arg: str | None = None
) -> tuple[dict, str, dict, str, dict]:
    """(root, from_name, from_pins, to_name, to_pins) -- raises UpgradeError.

    FROM is pins.yaml's pin unless `from_arg` names it, which is how an apply
    whose first stage already moved the pin is resumed or finalised.
    """
    root = load_versions_root()
    to_name, to_pins = resolve_stack(root, to_arg or current_stack(root))
    pinned = read_deploy_pin(deploy)
    from_name, from_pins = resolve_stack(root, from_arg or pinned)
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

# The program a finalise note runs once the operator confirms the soak is over,
# keyed by the step's versions.yaml `key`. A note with no entry is confirmed by
# the operator alone, who ran it by hand.
FINALISE_HOOKS: dict[str, Callable[[Path, Move], tuple[bool, str]]] = {
    KAFKA_VERSION_KEY: release_metadata_hold,
}


def _confirm(prompt: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def run_finalise_hook(
    deploy: Path, move: Move, *, from_name: str, to_name: str, assume_yes: bool
) -> tuple[bool, str]:
    """Confirm the soak is over, run the step's FINALISE_HOOKS program if it has
    one, and mark the step finalised. The marker is written only when both
    succeed, so a decline or a failed hook leaves the step exactly as
    reversible as it was before this call."""
    if not _confirm(f"soak complete -- run finalise now: {move.step.finalise}", assume_yes=assume_yes):
        return False, "declined by operator"
    detail = "confirmed by operator"
    hook = FINALISE_HOOKS.get(move.step.key)
    if hook is not None:
        ok, hook_detail = hook(deploy, move)
        detail = f"{detail}; {hook_detail}"
        if not ok:
            return False, detail
    marker = write_finalise_marker(deploy, from_name, to_name, move.step.key)
    return True, f"{detail} -- marker written: {marker}"


def _stage_failed(stage_index: int, reason: str, stage_moves: list[Move]) -> int:
    print(f"dfe-ops upgrade apply FAILED at stage {stage_index}: {reason}", file=sys.stderr)
    _print_rollback(stage_moves)
    return EXIT_BLOCKED


def _chart_stage(grouped: list[tuple[str, list[Move]]]) -> str | None:
    """The first stage moving a component Argo renders -- everything outside
    the bootstrap section, which bootstrap.sh installs before Argo exists."""
    return next(
        (stage for stage, stage_moves in grouped if any(not m.step.key.startswith("bootstrap.") for m in stage_moves)),
        None,
    )


def _write_holds(deploy: Path, hold: KafkaHold, *, hold_version: bool) -> list[str]:
    """Write the planned holds into infra/kafka.yaml; the lines written."""
    text = _read_overlay(deploy)
    written = []
    if hold.metadata:
        text = set_overlay_hold(text, "metadataVersion", hold.metadata)
        written.append(f"kafka.metadataVersion {hold.metadata}")
    if hold_version and hold.version:
        text = set_overlay_hold(text, "version", hold.version)
        written.append(f"kafka.version {hold.version}")
    if written:
        _write_overlay(deploy, text)
    return written


def cmd_upgrade_apply(args: argparse.Namespace) -> int:
    deploy = Path(args.deploy)
    try:
        _root, from_name, from_pins, to_name, to_pins = _load_from_to(deploy, args.to, args.from_stack)
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

    stage_names = [stage for stage, _ in grouped]
    reached = stage_names[: stage_names.index(args.stop_before)] if args.stop_before else stage_names
    chart_stage = _chart_stage(grouped)
    kafka_move = next((m for m in moves if m.step.key == KAFKA_VERSION_KEY), None)
    kafka_stage = kafka_move.step.stage if kafka_move else None

    # Read before any stage moves, so a cluster in a state apply cannot move
    # refuses with nothing half done.
    current_ref = ""
    retarget: str | None = None
    hold: KafkaHold | None = None
    if not args.dry_run and chart_stage in reached:
        try:
            current_ref = read_target_revision(args.kubeconfig, args.argocd_namespace)
            retarget, retarget_why = decide_retarget(current_ref, from_name, to_name, args.target_revision)
            hold, hold_why = plan_kafka_hold(args.kubeconfig, deploy, kafka_move, read_finalised_keys(deploy))
        except UpgradeError as err:
            print(f"dfe-ops upgrade apply: REFUSED -- {err}", file=sys.stderr)
            return EXIT_BLOCKED
        print(f"  target_revision: {retarget_why}", file=sys.stderr)
        print(f"  kafka holds: {hold_why}", file=sys.stderr)
        if retarget and not args.push:
            print(
                f"dfe-ops upgrade apply: REFUSED -- moving {TARGET_REVISION_ANNOTATION} to {retarget} needs "
                "--push: Argo reads the deploy repo's remote, so an unpushed stage would move the charts "
                "with none of its holds in place",
                file=sys.stderr,
            )
            return EXIT_BLOCKED

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
                    return _stage_failed(stage_index, "resolver did not accept the migration", stage_moves)

        # The holds land in the stage commit, so Argo has them before the retarget moves any chart.
        hold_version = kafka_stage is not None and kafka_stage != stage
        if stage == chart_stage and kafka_move is not None:
            emit(f"hold kafka.metadataVersion at the live status.kafkaMetadataVersion in {KAFKA_OVERLAY} (Strimzi only)")
            if hold_version:
                emit(f"hold kafka.version at {kafka_move.old} in {KAFKA_OVERLAY} until stage {kafka_stage}")
            if not args.dry_run and hold is not None:
                try:
                    written = _write_holds(deploy, hold, hold_version=hold_version)
                except UpgradeError as err:
                    return _stage_failed(stage_index, str(err), stage_moves)
                print(f"  kafka holds: {', '.join(written) or 'already in place'}", file=sys.stderr)
        if stage == kafka_stage:
            emit(f"drop the kafka.version hold from {KAFKA_OVERLAY}, so the brokers roll to {kafka_move.new}")
            if not args.dry_run:
                text = _read_overlay(deploy)
                released = drop_overlay_hold(text, "version")
                if released != text:
                    _write_overlay(deploy, released)
                    print(f"  kafka holds: dropped kafka.version {kafka_move.old}", file=sys.stderr)

        metadata_released = False
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
            was_held = (overlay_entry(_read_overlay(deploy), "metadataVersion") or ("", False))[1]
            ok, detail = run_finalise_hook(deploy, move, from_name=from_name, to_name=to_name, assume_yes=args.yes)
            print(f"  [{'DONE' if ok else 'PENDING'}] finalise {move.step.key}: {detail}", file=sys.stderr)
            metadata_released = metadata_released or (ok and was_held and move.step.key == KAFKA_VERSION_KEY)

        keys = ", ".join(move.step.key for move in stage_moves)
        message = f"chore(upgrade): {to_name} stage {stage_index} -- {keys}"
        emit(f"git -C {deploy} add {' '.join(STAGED_PATHS)} (those present)")
        emit(f"git -C {deploy} commit -m {message!r}")
        if not args.dry_run:
            added, detail = _stage(deploy)
            if not added:
                return _stage_failed(stage_index, f"git add failed: {detail}", stage_moves)
            # A later stage's pin already matches an earlier stage's bump, so
            # this can stage nothing -- commit only when something is staged.
            staged = _git(deploy, "diff", "--cached", "--quiet")
            if staged.returncode == 0:
                print(f"  stage {stage_index} ({stage}) changed nothing -- pin and sizing already matched", file=sys.stderr)
            else:
                commit = _git(deploy, "commit", "-m", message)
                if commit.returncode != 0:
                    failure = _last_line(commit.stderr) or _last_line(commit.stdout)
                    return _stage_failed(stage_index, f"git commit failed: {failure}", stage_moves)

        if args.push:
            emit(f"git -C {deploy} push")
            if not args.dry_run:
                push = _git(deploy, "push")
                if push.returncode != 0:
                    return _stage_failed(stage_index, "git push failed", stage_moves)

        emit(f"wait for Argo Applications in {args.argocd_namespace} (timeout {args.timeout}s)")
        if not args.dry_run:
            ok, detail = wait_for_argo(
                args.kubeconfig, argocd_namespace=args.argocd_namespace, timeout=args.timeout
            )
            print(f"  argo: {detail}", file=sys.stderr)
            if not ok:
                return _stage_failed(stage_index, "Argo did not converge", stage_moves)

        if stage == chart_stage:
            ref = args.target_revision or to_name
            emit(f"if secret/{CLUSTER_SECRET} targets {from_name}: {retarget_command(args.argocd_namespace, ref, to_name)}")
            emit(f"wait for Argo Applications to leave {from_name} (timeout {args.timeout}s)")
            if not args.dry_run and retarget:
                ok, detail = write_target_revision(args.kubeconfig, args.argocd_namespace, retarget, to_name)
                print(f"  [{'DONE' if ok else 'FAIL'}] retarget: {detail}", file=sys.stderr)
                if not ok:
                    return _stage_failed(stage_index, "the cluster secret did not take the new target_revision", stage_moves)
                ok, detail = wait_for_argo(
                    args.kubeconfig, argocd_namespace=args.argocd_namespace, timeout=args.timeout,
                    stale_revision=current_ref,
                )
                print(f"  argo: {detail}", file=sys.stderr)
                if not ok:
                    return _stage_failed(stage_index, f"Argo did not converge on {retarget}", stage_moves)

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
                    return _stage_failed(stage_index, "the Strimzi operator did not reconcile every Kafka CR", stage_moves)

        # Unpushed, Argo has nothing new to roll, so these waits run only with --push.
        if stage == kafka_stage:
            emit(f"wait for every Kafka CR to report kafkaVersion {kafka_move.new} (timeout {args.timeout}s)")
            if not args.dry_run and hold is not None and args.push:
                ok, detail = _wait_until(
                    lambda: check_kafka_version(args.kubeconfig, kafka_move.new),
                    timeout=args.timeout, stuck="still rolling",
                )
                print(f"  kafka: {detail}", file=sys.stderr)
                if not ok:
                    return _stage_failed(stage_index, f"the brokers did not roll to {kafka_move.new}", stage_moves)
        if metadata_released:
            emit(f"wait for every Kafka CR's kafkaMetadataVersion to reach the {kafka_move.new} line")
            if args.push:
                ok, detail = _wait_until(
                    lambda: check_kafka_metadata_moved(args.kubeconfig, kafka_move.new),
                    timeout=args.timeout, stuck="metadata version still held",
                )
                print(f"  kafka: {detail}", file=sys.stderr)
                if not ok:
                    return _stage_failed(stage_index, "the metadata version did not move", stage_moves)

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

    # The reverse move's `old` is the rollback target's value.
    kafka_move = next((m for m in moves if m.step.key == KAFKA_VERSION_KEY), None)
    retarget: str | None = None
    if args.skip_cluster_check:
        print(
            "[SKIP] cluster reads: --skip-cluster-check -- neither the live metadata version nor "
            f"{TARGET_REVISION_ANNOTATION} was read, so only the pin moves",
            file=sys.stderr,
        )
    else:
        if kafka_move is not None:
            ok, detail = check_rollback_metadata(args.kubeconfig, kafka_move.old)
            print(f"[{'PASS' if ok else 'FAIL'}] live metadata version: {detail}", file=sys.stderr)
            if not ok:
                print(
                    f"dfe-ops upgrade rollback: REFUSED -- the brokers cannot be shown able to go back to "
                    f"Kafka {kafka_move.old}. --skip-cluster-check moves the pin without this read; it does "
                    "not make a downgrade below the live metadata version possible",
                    file=sys.stderr,
                )
                return EXIT_BLOCKED
        try:
            current_ref = read_target_revision(args.kubeconfig, args.argocd_namespace)
            retarget, why = decide_retarget(current_ref, from_name, to_name, args.target_revision)
        except UpgradeError as err:
            print(f"dfe-ops upgrade rollback: REFUSED -- {err}", file=sys.stderr)
            return EXIT_BLOCKED
        print(f"  target_revision: {why}", file=sys.stderr)
        if retarget and not args.push and not args.dry_run:
            print(
                f"dfe-ops upgrade rollback: REFUSED -- moving {TARGET_REVISION_ANNOTATION} back to {retarget} "
                "needs --push, so Argo reads the rollback commit with it",
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
        if retarget:
            print(f"[dry-run] {retarget_command(args.argocd_namespace, retarget, to_name)}")
        return EXIT_OK

    bump_pin_file(deploy, to_name)
    keys = ", ".join(move.step.key for move in moves) or "no pinned key moved"
    message = f"chore(upgrade): rollback to {to_name} -- {keys}"
    added, detail = _stage(deploy)
    if not added:
        print(f"dfe-ops upgrade rollback: git add failed: {detail}", file=sys.stderr)
        return EXIT_BLOCKED
    commit = _git(deploy, "commit", "-m", message)
    if commit.returncode != 0:
        print(f"dfe-ops upgrade rollback: git commit failed: {_last_line(commit.stderr)}", file=sys.stderr)
        return EXIT_BLOCKED
    if args.push:
        push = _git(deploy, "push")
        if push.returncode != 0:
            print(f"dfe-ops upgrade rollback: git push failed: {_last_line(push.stderr)}", file=sys.stderr)
            return EXIT_BLOCKED
    if retarget:
        ok, detail = write_target_revision(args.kubeconfig, args.argocd_namespace, retarget, to_name)
        print(f"  [{'DONE' if ok else 'FAIL'}] retarget: {detail}", file=sys.stderr)
        if not ok:
            print("dfe-ops upgrade rollback: the cluster secret did not take the rollback ref", file=sys.stderr)
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
    apply_.add_argument(
        "--from",
        dest="from_stack",
        default=None,
        metavar="<stack>",
        help="the stack the deployment is moving FROM, when pins.yaml already names the target "
        "(finalise after the soak, or resume an apply) (default: pins.yaml's base.dfe-infra)",
    )
    _add_target_revision_arg(apply_)
    apply_.set_defaults(func=cmd_upgrade_apply)

    rollback = verbs.add_parser(
        "rollback",
        help="the reverse plan, refusing by name any step whose finalise has actually run",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_deploy_target_args(rollback, to_required=True)
    rollback.add_argument("--kubeconfig", default=None, help="kubeconfig for the target cluster")
    rollback.add_argument("--argocd-namespace", default=DEFAULT_ARGOCD_NAMESPACE, help="namespace holding the cluster secret")
    rollback.add_argument("--push", action="store_true", help="git push after the rollback commit")
    rollback.add_argument("--dry-run", action="store_true", help="print what would happen without running it")
    rollback.add_argument(
        "--skip-cluster-check",
        action="store_true",
        help="move only the pin: read neither the live Kafka metadata version nor the cluster secret, "
        "and leave target_revision where it is",
    )
    _add_target_revision_arg(rollback)
    rollback.set_defaults(func=cmd_upgrade_rollback)


def _add_target_revision_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--target-revision",
        default=None,
        metavar="<ref>",
        help=f"the dfe-infra ref to write as the cluster secret's {TARGET_REVISION_ANNOTATION} "
        "(default: the target stack's tag, when the secret is pinned to the FROM stack's)",
    )
