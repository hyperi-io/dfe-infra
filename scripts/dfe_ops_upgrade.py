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
                           [--target-revision <ref>] [--namespace <ns>]
    dfe-ops upgrade rollback --deploy <dir> --to <stack> [--dry-run] [--push]
                           [--skip-cluster-check] [--target-revision <ref>]
    dfe-ops upgrade migrate-values --deploy <dir> [--check]

Every verb takes `--deploy`, a dfe-deploy checkout (its `pins.yaml` names the
FROM stack in `base.dfe-infra`), and `--to`, a versions.yaml stack version
(default: the `current` pointer). A deploy repo with no `pins.yaml`, such as the
bundled one, takes FROM from the cluster secret's `dfe.hyperi.io/stack_version`,
says so, and gets a `pins.yaml` in dfe-deploy's shape at apply's first stage.
The move is computed by diffing the FROM and
TO stacks' pins, keyed by upgrade-order.yaml's declared order -- the same file
`docs/deployment/upgrades.md` says is applied by hand today. A step moves when
its `key` moves, or the image or thin-chart digest its `image`/`chart` names
does, so a component whose tag stays put while its digest moves still moves.

    plan       Diff FROM -> TO, ordered by stage, with each step's `before`/
               `finalise`/`pair`/`rollback` note attached. Runs
               `dfe-stack compat-check --strict` for TO, preflight's two
               exposure checks (the ingest door and the web plane), and --
               only when `--dial` is given -- resolve_sizing.py's
               locked-change classifier against the deploy's committed
               sizing/resolved.yaml. A move across the first thin-chart stack
               lists every 2.2.0 value key values_migration.py finds.
               Writes the plan to --out, by default
               .tmp/upgrades/<from>-to-<to>.md in this checkout -- never the
               deploy repo, whose untracked files preflight refuses.
               Exit 0 nothing moves (or the plan is clean), 1 the plan is
               BLOCKED (a compat-check failure, a failed exposure check, a
               2.2.0 value key needing a hand edit, or a locked sizing change
               with no --migrate evidence), 2 a
               pre-flight failure (the deploy repo or the stack name do not
               resolve).

    preflight  The checks `apply` refuses to run without: the deploy repo is
               clean, its infra/common.yaml sets exposure.groups.ingest when
               the plan crosses INGEST_GROUP_STACK (where the receiver's door
               stopped being exposure.mode), it states the console and api
               groups' allow lists when the plan crosses WEB_FENCE_STACK on an
               internet-facing gateway (where an empty list started refusing
               every address), no 2.2.0 value key needs a hand edit when the
               plan crosses the first thin-chart stack, the cluster answers, every Argo
               Application is Synced and Healthy, no KafkaRebalance is
               mid-flight, ClickHouse carries
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
               Application Synced and Healthy and then for every Deployment
               and StatefulSet in the DFE namespace to finish rolling out,
               both bounded by --timeout. After a push that carried a commit,
               and after the retarget below, each Application must also show
               a `status.reconciledAt` from after that change, since until
               Argo compares it again its Synced and Healthy are the state
               from before. An Application of pinned Helm charts that Argo
               last compared against its current spec is exempt, since
               neither change can move it. After a stage that bumps the
               Strimzi operator, wait again on every Kafka CR's
               `status.operatorLastSuccessfulVersion` reaching the new
               operator version, under the same bound. A rollout has finished
               when its controller has observed the current generation, every
               replica runs the new template, no old replica is left, and
               every replica is available -- what `kubectl rollout status`
               waits on. The DFE namespace is --namespace, else the cluster
               secret's `dfe.hyperi.io/dfe_namespace`, read before anything
               moves; a cluster secret naming none refuses the run.

               The first stage that moves an Argo-managed component also moves
               the cluster secret's `dfe.hyperi.io/target_revision` from the
               FROM stack's tag to the TO stack's, after that stage's before
               hooks pass and its commit is pushed -- the ref every chart and
               operator Application renders from. A secret tracking a branch is
               left alone, and one pinned to a commit refuses unless
               --target-revision names the ref. A retarget needs --push.

               A move across the first thin-chart stack writes each thin-chart
               key beside its 2.2.0 key in that stage's commit, and again from
               the pulled remote just before the retarget, since until then the
               2.2.0 engine writes only the old keys. --finalise drops the old
               keys once the cluster is on the TO charts, and writes the
               marker rollback refuses on.

               bootstrap.sh installs the bootstrap section (external-secrets,
               cert-manager, Argo CD) and Argo never renders it, so moving its
               pin changes nothing on the cluster. After such a stage apply
               reads the chart version each release runs, prints [DONE] where
               it matches the pin, and otherwise [PENDING] with the helm upgrade
               that moves it: --reset-values and every value bootstrap.sh's
               install sets, from the bootstrap/helm_releases.py table
               bootstrap.sh reads. It never runs that upgrade. After the walk it
               reads every other bootstrap pin of the target stack the same
               way, so one an earlier upgrade left uninstalled still keeps the
               run from reporting OK. An Argo CD that
               bootstrap/argocd_release.py does not recognise as bootstrap.sh's
               own reads [ADOPTED] and is left to its owner. A run with anything
               still pending ends NOT complete with exit 1, and a re-run with
               --from confirms it.

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
               target_revision back to the rollback target's tag. It also
               refuses a rollback below the first thin-chart stack once apply
               --finalise has dropped the 2.2.0 value keys.

    migrate-values  Write each thin-chart key beside its 2.2.0 key in the
               deploy checkout's values/*-values.yaml and infra/common.yaml,
               by helm/charts/dfe-extras/files/values-migrations.yaml, and
               commit nothing. --check writes nothing and exits 1 while a key
               is left to carry or needs a hand edit.

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
import base64
import functools
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(1, str(REPO_ROOT / "bootstrap"))
import argocd_release  # noqa: E402
import helm_releases  # noqa: E402
import values_migration  # noqa: E402
import yaml_subset  # noqa: E402

UPGRADE_ORDER = REPO_ROOT / "upgrade-order.yaml"
VERSIONS_FILE = REPO_ROOT / "versions.yaml"
DFE_STACK = SCRIPTS / "dfe-stack"
RESOLVE_SIZING = SCRIPTS / "resolve_sizing.py"
CHECK_NODE_CAPACITY = SCRIPTS / "check_node_capacity.py"
ARGOCD_LOGIN = REPO_ROOT / "bootstrap" / "argocd_login.py"

# Where a step's `key:` (the first five), `image:` (digests) and `chart:` (chart-digests) point; a pin no step names moves unreported.
UPGRADE_SECTIONS = ("bootstrap", "operators", "services", "apps", "content", "digests", "chart-digests")

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_PREFLIGHT_FAILED = 2

DEFAULT_ARGOCD_NAMESPACE = "argocd"
DEFAULT_CLICKHOUSE_NAMESPACE = "clickhouse"
# The server pod in either layout: the chart's single-mode StatefulSet, then the operator's cluster pods.
DEFAULT_CLICKHOUSE_SELECTOR = "app.kubernetes.io/name in (dfe-clickhouse,clickhouse-server)"
DEFAULT_CLICKHOUSE_MERGE_THRESHOLD = 300.0  # seconds
# The chart's clickhouse.users.admin.secretName, whose `password` key is the deploy layer's credential.
DEFAULT_CLICKHOUSE_CREDENTIALS = "clickhouse-admin-password"
# The names that credential has carried across releases, tried in order.
CLICKHOUSE_USERS = ("default", "admin")
DEFAULT_TIER = "scale"
DEFAULT_BACKUP_MARKER = "upgrades/.backup-ok"
# Outside every deploy repo: preflight refuses a deploy tree carrying an untracked file.
DEFAULT_PLAN_DIR = REPO_ROOT / ".tmp" / "upgrades"
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
ARGO_CLUSTER = "dfe-cluster"
TARGET_REVISION_ANNOTATION = "dfe.hyperi.io/target_revision"
STACK_VERSION_ANNOTATION = "dfe.hyperi.io/stack_version"
DFE_NAMESPACE_ANNOTATION = "dfe.hyperi.io/dfe_namespace"
DOMAIN_ANNOTATION = "dfe.hyperi.io/domain"

# The deploy repo's kafka chart overlay, layered last by appsets/layer2-data.yaml.
KAFKA_OVERLAY = Path("infra") / "kafka.yaml"
# Marks a line apply wrote, so only those are ever rewritten or dropped.
HOLD_MARK = "# dfe-ops upgrade hold"

# The deploy repo's shared values overlay, layered by every layer 2 appset.
COMMON_OVERLAY = Path("infra") / "common.yaml"
# The first stack whose receiver door is exposure.groups.ingest; below it the door is exposure.mode.
INGEST_GROUP_STACK = "2.2.1-rc.7"
INGEST_GROUP_PATH = ("exposure", "groups", "ingest")
FROM_220_DOC = "docs/deployment/upgrades.md#from-220"

# The deploy repo's gateway overlay, layered after infra/common.yaml by argocd/appsets/layer2-edge.yaml.
GATEWAY_OVERLAY = Path("infra") / "envoy-gateway-config.yaml"
# The first stack whose internet-facing gateway refuses every address on a web route with an empty allow list.
WEB_FENCE_STACK = "2.2.1-rc.6"
# The first stack whose web allow lists are exposure.groups.<group>.allow; at WEB_FENCE_STACK it is ui.allowed_cidrs.
WEB_GROUP_STACK = "2.2.1-rc.7"
WEB_FENCE_KEY = ("ui", "allowed_cidrs")
# The groups an empty allow list closes, and what each serves. A closed admin group fails the render instead (validateUi).
WEB_GROUPS = {
    "console": "dfe-ui, HyperDX and the engine's browser routes",
    "api": "the engine's CLI routes and /openapi.json",
}
# The keys the gateway chart's groupFence reads a group's state from.
WEB_GROUP_KEYS = ("enabled", "allow", "allowPublic")
INTERNET_FACING_PATH = ("envoyGateway", "service", "internetFacing")
CLOUD_ANNOTATION = "dfe.hyperi.io/cloud"
# The cluster's own egress, comma-separated, which layer2-edge.yaml hands the gateway as exposure.selfRanges.
SELF_RANGES_ANNOTATION = "dfe.hyperi.io/edge_self_ranges"
# The gateway's values below the deploy repo's, layered in layer2-edge.yaml's order.
GATEWAY_CHART_VALUES = REPO_ROOT / "helm" / "edge" / "gateway" / "values.yaml"
ARGO_VALUES = REPO_ROOT / "argocd" / "values"
# The clouds layer2-edge.yaml gives argocd/values/edge-onprem.yaml.
ONPREM_CLOUDS = ("local", "local-dfe", "rancher")
_CLOUD_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# The finalise marker key recording that apply --finalise dropped the 2.2.0 value keys.
VALUES_CONTRACT_KEY = "values.2.2.0-keys"
VALUES_CONTRACT_SUBJECT = "drop the 2.2.0 value keys"


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


def _kubectl(kubeconfig: str | None, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += list(args)
    if stdin is None:
        return _run(cmd)
    return _run(cmd, input=stdin)


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


def _unpushed(deploy: Path) -> bool:
    """Whether the deploy repo holds a commit its upstream lacks; True when git cannot say."""
    result = _git(deploy, "rev-list", "--count", "@{upstream}..HEAD")
    return result.returncode != 0 or result.stdout.strip() != "0"


def _push_moment(deploy: Path) -> datetime | None:
    """change_moment() when a push has a commit to carry; None when Argo has nothing new to read."""
    return change_moment() if _unpushed(deploy) else None


# The deploy paths an upgrade commit carries. `git add` with one missing
# pathspec stages nothing at all, so only the ones that exist are named.
STAGED_PATHS = (
    "pins.yaml", "sizing", "upgrades", str(KAFKA_OVERLAY),
    values_migration.OVERLAY_DIR.as_posix(), COMMON_OVERLAY.as_posix(),
)


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
    """One upgrade-order.yaml step -- a versions.yaml key and its notes.

    `image` and `chart` name the versions.yaml keys of the component's image
    digest and thin-chart digest, which move with `key` as one component.
    """

    stage: str
    order: str
    key: str
    before: str = ""
    finalise: str = ""
    pair: str = ""
    rollback: str = ""
    scope: str = ""
    image: str = ""
    chart: str = ""

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
                    image=str(entry.get("image", "")),
                    chart=str(entry.get("chart", "")),
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
    mismatch -- the same rule dfe-stack's own stack_pins() applies.

    The name returned is versions.yaml's own key, never `name`, so a stack read
    off the cluster secret goes on as a label this repo certifies.
    """
    stacks = root.get("stacks")
    if not isinstance(stacks, dict):
        raise UpgradeError("versions.yaml carries no stacks: map")
    match = next((n for n in stacks if n == name), None)
    if match is None:
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
    to the sections an upgrade-order.yaml step can name."""
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
    """One upgrade-order.yaml step whose pins differ between FROM and TO.

    `old`/`new` are the step key's values, and `pins` each digest the step's
    `image`/`chart` names that moved, as (label, key, old, new).
    """

    step: Step
    old: str
    new: str
    pins: tuple[tuple[str, str, str, str], ...] = ()


def plan_moves(steps: list[Step], from_pins: dict[str, str], to_pins: dict[str, str]) -> list[Move]:
    """Every step whose key, image digest or chart digest differs between the two
    flattened pin sets, in upgrade-order.yaml order."""
    moves: list[Move] = []
    for step in steps:
        old = from_pins.get(step.key)
        new = to_pins.get(step.key)
        pins = tuple(
            (label, key, from_pins.get(key) or "(absent)", to_pins.get(key) or "(absent)")
            for label, key in (("image", step.image), ("chart", step.chart))
            if key and from_pins.get(key) != to_pins.get(key)
        )
        if old == new and not pins:
            continue
        moves.append(Move(step=step, old=old or "(absent)", new=new or "(absent)", pins=pins))
    return moves


def render_plan(moves: list[Move], *, from_stack: str, to_stack: str, note: str = "") -> str:
    """The numbered plan, grouped by stage, each move carrying its notes, with `note`
    under the heading."""
    lines = [f"# Upgrade plan: {from_stack} -> {to_stack}", ""]
    if note:
        lines += [note, ""]
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
        for label, key, old, new in move.pins:
            lines.append(f"   {label + ':':<9} {key}: {old} -> {new}")
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


def _sources(holder: dict) -> list[dict]:
    """The sources an Application spec or its status.sync.comparedTo names.

    comparedTo carries both keys on a multi-source Application, so a non-empty
    `sources` list wins over `source`.
    """
    listed = [src for src in holder.get("sources") or [] if isinstance(src, dict)]
    if listed:
        return listed
    return [holder["source"]] if isinstance(holder.get("source"), dict) else []


def _source_revisions(app: dict) -> list[str]:
    return [str(src.get("targetRevision") or "") for src in _sources(app.get("spec") or {})]


def _pinned_chart_as_compared(app: dict) -> bool:
    """Whether every source is a Helm chart and Argo last compared exactly these sources.

    A chart source is fixed by the version its spec names, so neither a deploy
    repo push nor a target_revision move changes it without changing the spec,
    and a changed spec leaves comparedTo behind until Argo compares it again.
    """
    sources = _sources(app.get("spec") or {})
    compared = _sources(((app.get("status") or {}).get("sync") or {}).get("comparedTo") or {})
    return bool(sources) and all(src.get("chart") for src in sources) and sources == compared


def _argo_time(value: object) -> datetime | None:
    """An Argo status timestamp (RFC 3339 in UTC, whole seconds), or None when absent or unreadable."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def change_moment() -> datetime:
    """The instant, to the whole second Argo stamps, taken before a push or retarget lands.

    Taken before, so a reconcile the change itself triggers is never mistaken for an older one.
    """
    return datetime.now(UTC).replace(microsecond=0)


def check_argo_apps(
    kubeconfig: str | None,
    namespace: str = DEFAULT_ARGOCD_NAMESPACE,
    *,
    stale_revision: str = "",
    reconciled_after: datetime | None = None,
) -> tuple[bool, str]:
    """Every Argo Application in `namespace` is Synced and Healthy.

    Reads kubectl directly (`applications.argoproj.io`), the same path
    dfe-ops refresh/cycle already use -- no second CLI (argocd) dependency.
    With `stale_revision`, an Application still rendering from that ref also
    fails: right after a retarget every Application is still Synced to the old
    one until the ApplicationSet controller regenerates it. With
    `reconciled_after`, an Application whose `status.reconciledAt` is earlier,
    or absent, also fails: until Argo compares an Application against the
    change again, its Synced and Healthy are the state from before it. The
    exception is an Application made only of Helm chart sources that Argo last
    compared against its current spec, which neither change can move, so its
    Synced and Healthy stand without a fresh reconcile.
    """
    rc, doc, err = _kubectl_json(kubeconfig, "-n", namespace, "get", "applications.argoproj.io")
    if rc != 0:
        return False, f"cannot list Argo Applications in {namespace}: {err}"
    items = doc.get("items") or []
    if not items:
        return False, f"no Argo Applications found in {namespace}"
    bad = []
    unmoved = 0
    for app in items:
        name = (app.get("metadata") or {}).get("name", "<unnamed>")
        status = app.get("status") or {}
        sync = (status.get("sync") or {}).get("status") or "Unknown"
        health = (status.get("health") or {}).get("status") or "Unknown"
        reconciled = _argo_time(status.get("reconciledAt"))
        before_change = reconciled_after is not None and (
            reconciled is None or reconciled < reconciled_after
        )
        if before_change and _pinned_chart_as_compared(app):
            before_change = False
            unmoved += 1
        if stale_revision and stale_revision in _source_revisions(app):
            bad.append(f"{name} (still renders from the previous {TARGET_REVISION_ANNOTATION})")
        elif before_change:
            seen = status.get("reconciledAt") or "never"
            bad.append(f"{name} (last reconciled {seen}, before this change)")
        elif sync != "Synced" or health != "Healthy":
            bad.append(f"{name} (sync {sync}, health {health})")
    if bad:
        extra = f", +{len(bad) - 6} more" if len(bad) > 6 else ""
        return False, f"{len(bad)} app(s) not Synced/Healthy: {', '.join(bad[:6])}{extra}"
    since = ""
    if reconciled_after and unmoved:
        since = (
            f", each reconciled since this change but {unmoved} pinned Helm chart(s) it cannot move"
        )
    elif reconciled_after:
        since = ", each reconciled since this change"
    return True, f"{len(items)} Application(s) Synced and Healthy{since}"


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


# Run by the ClickHouse pod's shell: a password line on stdin becomes clickhouse-client's
# CLICKHOUSE_PASSWORD, so neither the kubectl command line nor the pod's carries it.
CLICKHOUSE_CLIENT_SH = 'if IFS= read -r pw; then export CLICKHOUSE_PASSWORD="$pw"; fi; exec clickhouse-client "$@" </dev/null'


@dataclass(frozen=True)
class ClickHouseLogin:
    """A clickhouse-client login: no user is the server's `default` with no password."""

    user: str | None = None
    password: str = field(default="", repr=False)


def _clickhouse_client(
    kubeconfig: str | None, namespace: str, pod: str, query: str, login: ClickHouseLogin
) -> subprocess.CompletedProcess:
    user = ["--user", login.user] if login.user else []
    return _kubectl(
        kubeconfig, "-n", namespace, f"--request-timeout={DEFAULT_KUBECTL_REQUEST_TIMEOUT}",
        "exec", "-i", pod, "--", "sh", "-c", CLICKHOUSE_CLIENT_SH, "sh", *user, "-q", query,
        # Always given, even empty: exec -i with no stdin would read the caller's terminal.
        stdin=f"{login.password}\n" if login.password else "",
    )


def _clickhouse_login(kubeconfig: str | None, namespace: str, store: str, pod: str) -> ClickHouseLogin:
    """The clickhouse-client login that answers on `pod` -- raises UpgradeError.

    The `password` key of the `store` Secret as each of CLICKHOUSE_USERS, then
    `default` with no password, the order bootstrap/smoke-test-integration.sh
    resolves it in. The password only ever travels on stdin.
    """
    read = _kubectl(
        kubeconfig, "-n", namespace, f"--request-timeout={DEFAULT_KUBECTL_REQUEST_TIMEOUT}",
        "get", "secret", store, "-o", "jsonpath={.data.password}",
    )
    users: tuple[str, ...] = ()
    unread = ""
    if read.returncode == 0:
        try:
            pw = base64.b64decode((read.stdout or "").strip(), validate=True).decode("utf-8", errors="replace")
        except ValueError:
            pw = ""
        if pw:
            users = CLICKHOUSE_USERS
            for user in users:
                login = ClickHouseLogin(user=user, password=pw)
                if _clickhouse_client(kubeconfig, namespace, pod, "SELECT 1", login).returncode == 0:
                    return login
        else:
            unread = f"secret/{store} holds no password, "
    else:
        unread = f"secret/{store} unreadable ({_last_line(read.stderr) or 'kubectl failed'}), "
    if _clickhouse_client(kubeconfig, namespace, pod, "SELECT 1", ClickHouseLogin()).returncode == 0:
        return ClickHouseLogin()
    tried = [f"secret/{store} as {user}" for user in users] + ["default with no password"]
    raise UpgradeError(f"no ClickHouse credential answers on {pod}: {unread}tried {', '.join(tried)}")


def check_clickhouse_merges(
    kubeconfig: str | None,
    namespace: str = DEFAULT_CLICKHOUSE_NAMESPACE,
    selector: str = DEFAULT_CLICKHOUSE_SELECTOR,
    threshold_seconds: float = DEFAULT_CLICKHOUSE_MERGE_THRESHOLD,
    credentials: str = DEFAULT_CLICKHOUSE_CREDENTIALS,
) -> tuple[bool, str]:
    """No ClickHouse server pod the selector matches runs a merge past the
    threshold. system.merges is per server, so every pod is asked."""
    rc, doc, err = _kubectl_json(kubeconfig, "-n", namespace, "get", "pods", "-l", selector)
    if rc != 0:
        return False, f"cannot find a ClickHouse pod: {err}"
    pods = [str((item.get("metadata") or {}).get("name", "")) for item in doc.get("items") or []]
    if not pods:
        return False, f"no pod matching {selector!r} in {namespace}"
    try:
        login = _clickhouse_login(kubeconfig, namespace, credentials, pods[0])
    except UpgradeError as exc:
        return False, str(exc)
    query = f"SELECT count() FROM system.merges WHERE elapsed > {threshold_seconds}"
    merging: list[str] = []
    for pod in pods:
        result = _clickhouse_client(kubeconfig, namespace, pod, query, login)
        if result.returncode != 0:
            return False, f"{pod}: {_last_line(result.stderr) or 'clickhouse-client query failed'}"
        raw = (result.stdout or "0").strip()
        try:
            count = int(raw)
        except ValueError:
            return False, f"{pod}: unparseable clickhouse-client output: {raw!r}"
        if count > 0:
            merging.append(f"{pod} ({count})")
    if merging:
        return False, (
            f"{len(merging)} of {len(pods)} ClickHouse pod(s) carry a merge running longer than "
            f"{threshold_seconds:.0f}s: {', '.join(merging)}"
        )
    return True, f"no merge running longer than {threshold_seconds:.0f}s on {len(pods)} ClickHouse pod(s)"


def stale_strimzi_crds(installed: dict[str, dict]) -> dict[str, str]:
    """Each CRD in `installed` (name -> CRD object) whose status.storedVersions
    holds anything but v1, mapped to `<name> (stored: <versions>)`, in input order.

    The 1.x operator serves v1 only, so its chart refuses to apply over such a
    CRD. Shared by check_strimzi_conversion and `dfe-ops preflight`.
    """
    stale = {}
    for name, doc in installed.items():
        stored = (doc.get("status") or {}).get("storedVersions") or []
        if any(v != "v1" for v in stored):
            stale[name] = f"{name} (stored: {', '.join(stored)})"
    return stale


def check_strimzi_conversion(kubeconfig: str | None, crds: tuple[str, ...] = STRIMZI_CRDS) -> tuple[bool, str]:
    """Every Strimzi CRD the 1.x operator needs stores v1 only -- the sign
    `bin/v1-api-conversion.sh convert-resource` (then `crd-upgrade`) already
    ran, which 1.x requires before the operator upgrade lands.

    A CRD that is not installed has nothing stored to convert; a CRD kubectl
    could not read fails, because an unanswered read proves nothing.
    """
    installed: dict[str, dict] = {}
    unread = []
    for crd in crds:
        rc, doc, err = _kubectl_json(kubeconfig, "get", "crd", crd)
        if rc != 0:
            if not _absent(err):
                unread.append(f"{crd} ({err or 'kubectl failed'})")
            continue
        installed[crd] = doc
    if unread:
        return False, f"cannot read {len(unread)} Strimzi CRD(s): {', '.join(unread)}"
    if not installed:
        return True, "no Strimzi CRDs installed -- nothing to convert"
    stale = stale_strimzi_crds(installed)
    if stale:
        return False, f"{len(stale)} CRD(s) still store a pre-v1 version: {', '.join(stale.values())}"
    return True, f"{len(installed)} Strimzi CRD(s) store v1 only"


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


@functools.cache
def _dfe_stack() -> ModuleType:
    """scripts/dfe-stack, imported for its semver_key so stack order has one definition."""
    loader = importlib.machinery.SourceFileLoader("dfe_stack", str(DFE_STACK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise UpgradeError(f"cannot load {DFE_STACK}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def check_ingest_door(deploy: Path, from_stack: str, to_stack: str) -> tuple[bool, str]:
    """The deploy repo states its ingest door when the upgrade crosses INGEST_GROUP_STACK.

    Below that stack the receiver's door was exposure.mode, whose chart default
    was a LoadBalancer open to every address. From it the door is
    exposure.groups.ingest, whose cloud default renders no load balancer or
    carries HTTP alone, so a deploy repo that set neither key moves its door on
    upgrade without an error. An explicit block in infra/common.yaml, even
    `allow: []`, is the operator's decision and passes. The block is read in
    block style, or as a flow mapping on the `ingest:` line; any other spelling
    reads as unset, which refuses. A stack name dfe-stack cannot order counts as
    crossing the cutover, so it refuses rather than passes.

    Args:
        deploy: The deploy repo checkout.
        from_stack: The stack the deployment runs.
        to_stack: The stack it moves to.

    Returns:
        (ok, the evidence line).
    """
    semver_key = _dfe_stack().semver_key
    cutover = semver_key(INGEST_GROUP_STACK)
    before, after = semver_key(from_stack), semver_key(to_stack)
    if before is not None and before >= cutover:
        return True, (
            f"{from_stack} is at or after {INGEST_GROUP_STACK}: "
            "the receiver's door is already exposure.groups.ingest"
        )
    if after is not None and after < cutover:
        return True, f"{to_stack} predates {INGEST_GROUP_STACK}, so the receiver's door does not move"
    overlay = deploy / COMMON_OVERLAY
    keys = yaml_subset.keys_under(overlay.read_text(encoding="utf-8"), INGEST_GROUP_PATH) if overlay.is_file() else []
    if keys:
        return True, f"{COMMON_OVERLAY.as_posix()} sets exposure.groups.ingest: {', '.join(keys)}"
    return False, (
        f"{from_stack} predates {INGEST_GROUP_STACK}, which moved the receiver's door from exposure.mode to "
        f"exposure.groups.ingest, and {COMMON_OVERLAY.as_posix()} does not set exposure.groups.ingest. "
        f"Under {from_stack} the door was exposure.mode: by default a LoadBalancer open to every address on the "
        "receiver's public ports, the tunnel under vpn, the gateway route under internal. "
        f"On local-dfe and rancher {from_stack} served two doors at once, that LoadBalancer and HTTP /ingest on "
        f"the gateway at receiver.{{domain}}. {to_stack} serves one: door loadbalancer drops the "
        "receiver.{domain} route, so senders posting there repoint to the load balancer's address, and door "
        "gateway drops the load balancer, so its senders repoint to receiver.{domain}. "
        f"Under {to_stack} the cloud's own door applies, and door loadbalancer with an empty allow renders no "
        "load balancer at all. Set exposure.groups.ingest.door (loadbalancer, gateway or tunnel) and "
        f"exposure.groups.ingest.allow (the senders' CIDRs) in {COMMON_OVERLAY.as_posix()}. "
        f"An explicit block, even allow: [], passes. See {FROM_220_DOC}."
    )


def read_cloud(kubeconfig: str | None, namespace: str) -> str:
    """The cluster secret's cloud, which picks the gateway's values files. Raises UpgradeError when unreadable."""
    return _cluster_secret_annotation(kubeconfig, namespace, CLOUD_ANNOTATION)


def _read_layers(root: Path, paths: tuple[Path, ...]) -> list[tuple[str, str]]:
    """(path under `root`, body) of each values file present, in layering order.

    A missing file is skipped, as the appsets' ignoreMissingValueFiles skips it.
    """
    layers: list[tuple[str, str]] = []
    for path in paths:
        try:
            layers.append((path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")))
        except OSError:
            continue
    return layers


def gateway_values(cloud: str) -> list[tuple[str, str]]:
    """(path, body) of this checkout's gateway values files for `cloud`, in layer2-edge.yaml's order.

    The profile file is left out because no profile file sets a key the web
    check reads, which scripts/tests/test_dfe_ops_upgrade.py holds.
    """
    flavour = "onprem" if cloud in ONPREM_CLOUDS else cloud
    paths = (
        GATEWAY_CHART_VALUES,
        ARGO_VALUES / "common.yaml",
        ARGO_VALUES / f"{cloud}.yaml",
        ARGO_VALUES / f"edge-{flavour}.yaml",
    )
    return _read_layers(REPO_ROOT, paths)


def _layered(layers: list[tuple[str, str]], path: tuple[str, ...]) -> tuple[str, str]:
    """(the scalar the last layer setting `path` gives it, that layer's path); empty strings when none does."""
    found = ("", "")
    for name, text in layers:
        value = yaml_subset.value_at(text, path)
        if value is not None:
            found = (value, name)
    return found


def _keys_set(overlays: list[tuple[str, str]], parent: tuple[str, ...], keys: tuple[str, ...]) -> list[str]:
    """Which of `keys` any overlay sets under `parent`, each once, in file order."""
    found: list[str] = []
    for _name, text in overlays:
        for key in yaml_subset.keys_under(text, parent):
            if key in keys and key not in found:
                found.append(key)
    return found


@dataclass(frozen=True, slots=True)
class WebList:
    """One allow list a deploy repo states for the web routes.

    Attributes:
        name: The key an operator sets, such as exposure.groups.console.
        parent: The mapping the stating keys sit under.
        keys: The keys whose presence states the list.
        serves: What answers 403 while the list is empty.
        group: The exposure group; empty for ui.allowed_cidrs, which covers every web route.
    """

    name: str
    parent: tuple[str, ...]
    keys: tuple[str, ...]
    serves: str
    group: str = ""


def web_lists(*, grouped: bool) -> list[WebList]:
    """The web allow lists a target stack reads: one per web group from WEB_GROUP_STACK, else ui.allowed_cidrs."""
    if not grouped:
        serves = "dfe-ui, HyperDX and the engine API"
        return [WebList(".".join(WEB_FENCE_KEY), WEB_FENCE_KEY[:-1], WEB_FENCE_KEY[-1:], serves)]
    return [
        WebList(f"exposure.groups.{group}", ("exposure", "groups", group), WEB_GROUP_KEYS, serves, group)
        for group, serves in WEB_GROUPS.items()
    ]


def _cloud_from_secret(kubeconfig: str | None, namespace: str) -> tuple[str, str]:
    """(the cluster secret's cloud, why there is none when it is empty)."""
    try:
        cloud = read_cloud(kubeconfig, namespace)
    except UpgradeError as err:
        return "", str(err)
    return cloud, f"secret/{ARGO_CLUSTER}'s {CLOUD_ANNOTATION} is empty"


def read_self_ranges(kubeconfig: str | None, namespace: str) -> str:
    """The cluster secret's self ranges, comma-separated. Raises UpgradeError when unreadable."""
    return _cluster_secret_annotation(kubeconfig, namespace, SELF_RANGES_ANNOTATION)


def _lists_an_entry(text: str, path: tuple[str, ...]) -> bool:
    """Whether the list a values file sets at `path` holds an entry.

    A spelling this cannot read counts as holding one, so the refusal still names its overlay.
    """
    dotted = ".".join(path)
    inline = values_migration.value_of(text, dotted)
    if inline is not None:
        # A flow list opened here and closed on a later line reads back as a bare "[".
        if inline.strip().startswith("[") and not inline.strip().endswith("]"):
            return True
        return inline.strip("[] \t") not in ("", "~", "null", "Null", "NULL")
    items = values_migration.list_items(text, dotted)
    return items is None or bool(items)


def _self_range_reach(
    overlays: list[tuple[str, str]], kubeconfig: str | None, namespace: str, *, read_secret: bool = True
) -> str:
    """Whom a closed web group answers 403, worded for the refusal.

    Every web group admits exposure.selfRanges. layer2-edge.yaml writes the
    cluster secret's annotation as inline values, which Argo CD layers over
    every values file, so the deploy repo's own exposure.selfRanges counts only
    while that annotation is empty, and then the last overlay setting it decides.
    With `read_secret` False the annotation is unknown, as when it is unreadable.
    """
    try:
        ranges = read_self_ranges(kubeconfig, namespace) if read_secret else None
    except UpgradeError:
        ranges = None
    if ranges is None:
        return "every address outside exposure.selfRanges"
    own = [cidr.strip() for cidr in ranges.split(",") if cidr.strip()]
    if own:
        return f"every address outside exposure.selfRanges ({', '.join(own)})"
    stated = [
        (name, text) for name, text in overlays if "selfRanges" in yaml_subset.keys_under(text, ("exposure",))
    ]
    if stated and _lists_an_entry(stated[-1][1], ("exposure", "selfRanges")):
        return f"every address outside the exposure.selfRanges {stated[-1][0]} sets"
    return "every address"


def _group_closes(layers: list[tuple[str, str]], parent: tuple[str, ...]) -> bool:
    """Whether the layered values leave a group enabled without allowPublic, which an empty allow list closes."""
    enabled = _layered(layers, (*parent, "enabled"))[0].lower() == "true"
    open_to_all = _layered(layers, (*parent, "allowPublic"))[0].lower() == "true"
    return enabled and not open_to_all


def check_web_plane(
    deploy: Path,
    from_stack: str,
    to_stack: str,
    *,
    cloud: str | None,
    kubeconfig: str | None,
    argocd_namespace: str = DEFAULT_ARGOCD_NAMESPACE,
    cluster: bool = True,
) -> tuple[bool, str]:
    """The deploy repo states who reaches the web routes when the upgrade crosses WEB_FENCE_STACK.

    Below that stack the gateway fenced no web route by address. From it an
    internet-facing gateway refuses every address outside exposure.selfRanges on
    a web route whose allow list is empty, so a deploy repo that set nothing loses dfe-ui, HyperDX and
    the engine API to 403 on upgrade without an error. From WEB_GROUP_STACK the
    list is per exposure group, and a console or api group whose enabled, allow
    or allowPublic the deploy repo's gateway overlays set is the operator's
    decision and passes, `allow: []` included. Below it the list is
    ui.allowed_cidrs. A gateway that is not internet-facing for the
    deployment's cloud fences nothing and passes. The cloud is `cloud`, else the
    cluster secret's, read only once a list is unstated, and a refusal names the
    self ranges off the same secret while a cluster is in play. A stack name dfe-stack
    cannot order counts as crossing the cutover, so it refuses rather than
    passes.

    Args:
        deploy: The deploy repo checkout.
        from_stack: The stack the deployment runs.
        to_stack: The stack it moves to.
        cloud: The deployment's cloud, as the cluster secret names it; None reads the secret.
        kubeconfig: The kubeconfig that secret is read through.
        argocd_namespace: The namespace holding that secret.
        cluster: Whether a cluster is in play. False skips the self-range read unless the
            cloud came off the secret.

    Returns:
        (ok, the evidence line).
    """
    semver_key = _dfe_stack().semver_key
    fence = semver_key(WEB_FENCE_STACK)
    before, after = semver_key(from_stack), semver_key(to_stack)
    if before is not None and before >= fence:
        return True, f"{from_stack} is at or after {WEB_FENCE_STACK}: the web routes are already fenced"
    if after is not None and after < fence:
        return True, f"{to_stack} predates {WEB_FENCE_STACK}, so the web routes stay unfenced"

    overlays = _read_layers(deploy, (deploy / COMMON_OVERLAY, deploy / GATEWAY_OVERLAY))
    stated: list[str] = []
    unstated: list[WebList] = []
    for web in web_lists(grouped=after is None or after >= semver_key(WEB_GROUP_STACK)):
        found = _keys_set(overlays, web.parent, web.keys)
        if found:
            stated.append(f"{web.name}: {', '.join(found)}" if web.group else web.name)
        else:
            unstated.append(web)
    if not unstated:
        return True, f"the deploy repo sets {'; '.join(stated)}"

    crossing = (
        f"{from_stack} predates {WEB_FENCE_STACK}, from which an internet-facing gateway answers 403 on every "
        "web route whose allow list is empty"
    )
    unset = f"neither {COMMON_OVERLAY.as_posix()} nor {GATEWAY_OVERLAY.as_posix()} sets"
    read_secret = cluster or not cloud
    if not cloud:
        cloud, why = _cloud_from_secret(kubeconfig, argocd_namespace)
        if not cloud:
            listed = [web.name for web in unstated]
            return False, (
                f"{crossing}, and {unset} {' or '.join(listed)}. Whether this gateway is internet-facing depends on "
                f"the deployment's cloud, and {why}. Pass --cloud, or set {' and '.join(listed)} in "
                f"{COMMON_OVERLAY.as_posix()}. See {FROM_220_DOC}."
            )
    if not _CLOUD_NAME.match(cloud):
        return False, f"the cloud {cloud!r} is not a cloud name, so no argocd/values file can be read for it"

    layers = gateway_values(cloud) + overlays
    facing, facing_from = _layered(layers, INTERNET_FACING_PATH)
    if facing.lower() != "true":
        return True, f"the gateway is not internet-facing on {cloud}, so an empty web allow list fences nothing"
    closed = [web for web in unstated if not web.group or _group_closes(layers, web.parent)]
    if not closed:
        names = " and ".join(web.name for web in unstated)
        return True, f"the gateway is internet-facing on {cloud}, and its values leave {names} off or open"

    served = "; ".join(f"{web.serves} ({web.name})" for web in closed)
    reach = _self_range_reach(overlays, kubeconfig, argocd_namespace, read_secret=read_secret)
    if closed[0].group:
        remedy = (
            "Set exposure.groups.<group>.allow to the CIDRs that may reach them, for "
            f"{' and '.join(web.group for web in closed)}, in {COMMON_OVERLAY.as_posix()}. Any explicit "
            "enabled, allow or allowPublic passes, allow: [] included."
        )
    else:
        remedy = (
            f"Set ui.allowed_cidrs to the CIDRs that may reach them in {COMMON_OVERLAY.as_posix()}; from "
            f"{WEB_GROUP_STACK} the list is exposure.groups.<group>.allow. Any explicit value passes, \"\" included."
        )
    return False, (
        f"{crossing}. The gateway is internet-facing on {cloud} ({facing_from} sets "
        f"{'.'.join(INTERNET_FACING_PATH)}: true), and {unset} {' or '.join(web.name for web in closed)}. "
        f"Under {from_stack} the gateway fenced no web route by address. Under {to_stack} these answer 403 to "
        f"{reach} from the first sync: {served}. {remedy} "
        f"A /0 admits every address, as {from_stack} did. See {FROM_220_DOC}."
    )


def exposure_checks(
    deploy: Path,
    from_stack: str,
    to_stack: str,
    *,
    cloud: str | None,
    kubeconfig: str | None,
    argocd_namespace: str = DEFAULT_ARGOCD_NAMESPACE,
    cluster: bool = True,
) -> list[tuple[str, bool, str]]:
    """The preflight checks a plan runs too: the doors an upgrade can move or close without an error."""
    return [
        ("ingest door (exposure.groups.ingest)", *check_ingest_door(deploy, from_stack, to_stack)),
        (
            "web plane (exposure.groups console and api)",
            *check_web_plane(
                deploy, from_stack, to_stack, cloud=cloud, kubeconfig=kubeconfig, argocd_namespace=argocd_namespace,
                cluster=cluster,
            ),
        ),
    ]


# --- the 2.2.0 values migration (scripts/values_migration.py) ------------------


def values_cutover(from_stack: str, to_stack: str, since: str) -> tuple[bool, str]:
    """(whether the move crosses `since`, the line saying so).

    From `since` the thin charts read keys the 2.2.0 charts did not, so a move
    across it needs the deploy repo's values carried. A stack dfe-stack cannot
    order counts as crossing, as the exposure checks count it.
    """
    semver_key = _dfe_stack().semver_key
    cutover = semver_key(since)
    before, after = semver_key(from_stack), semver_key(to_stack)
    if before is not None and cutover is not None and before >= cutover:
        return False, f"{from_stack} is at or after {since}: the thin charts already read this deploy repo"
    if after is not None and cutover is not None and after < cutover:
        return False, f"{to_stack} predates {since}, so the 2.2.0 charts keep reading the 2.2.0 keys"
    return True, f"the move crosses {since}, from which the thin charts read their own keys"


def check_values_migration(deploy: Path, from_stack: str, to_stack: str) -> tuple[bool, str]:
    """Every 2.2.0 value key in the deploy repo can be carried onto its thin-chart key.

    Fails on a refuse rule, an image pin the thin chart would keep running, and
    on a key the line surgery cannot reach. Both need a person before anything
    moves. apply carries every other key itself, before the retarget.
    """
    migration = values_migration.load_map()
    crosses, why = values_cutover(from_stack, to_stack, migration.since)
    if not crosses:
        return True, why
    hits = values_migration.scan(deploy, migration)
    refused = values_migration.refused(hits)
    if refused:
        listed = "; ".join(f"{hit.file}: {hit.detail}" for hit in refused[:4])
        extra = f" (+{len(refused) - 4} more)" if len(refused) > 4 else ""
        return False, (
            f"{len(refused)} 2.2.0 value key(s) need a hand edit before the move across {migration.since}: "
            f"{listed}{extra}. `dfe-ops upgrade migrate-values --deploy <dir> --check` lists every key. "
            f"See {FROM_220_DOC}."
        )
    carried = [hit for hit in hits if hit.state == values_migration.MIGRATE]
    files = len({hit.file for hit in carried})
    if not carried:
        return True, "no 2.2.0 value key left to carry onto the thin-chart keys"
    return True, (
        f"{len(carried)} 2.2.0 value key(s) in {files} file(s) for apply to carry onto the thin-chart keys "
        "before the retarget"
    )


def _expand_values(deploy: Path, migration: values_migration.MigrationMap, *, resync: bool) -> tuple[bool, str]:
    """Run expand, printing each key it wrote; (False, why) when one needs a person."""
    hits = values_migration.expand(deploy, migration, resync=resync)
    written = [hit for hit in hits if hit.state in (values_migration.MIGRATE, values_migration.RESYNC)]
    for hit in written:
        print(f"  values: {hit.line()}", file=sys.stderr)
    refused = values_migration.refused(hits)
    if refused:
        return False, "; ".join(f"{hit.file}: {hit.detail}" for hit in refused)
    return True, f"{len(written)} key(s) written" if written else "every 2.2.0 key already carried"


def _commit_and_push(deploy: Path, message: str, *, push: bool) -> tuple[str, str]:
    """Stage the upgrade paths, commit them and push. ("", what happened), or (why it failed, "")."""
    added, detail = _stage(deploy)
    if not added:
        return f"git add failed: {detail}", ""
    if _git(deploy, "diff", "--cached", "--quiet").returncode == 0:
        return "", "nothing to commit"
    commit = _git(deploy, "commit", "-m", message)
    if commit.returncode != 0:
        return f"git commit failed: {_last_line(commit.stderr) or _last_line(commit.stdout)}", ""
    if push and _git(deploy, "push").returncode != 0:
        return "git push failed", ""
    return "", "committed and pushed" if push else "committed"


def _recarry_values(deploy: Path, migration: values_migration.MigrationMap, to_name: str) -> tuple[bool, str]:
    """Carry the 2.2.0 value keys again from the deploy repo's remote head, just before the retarget.

    Until the retarget the 2.2.0 engine writes the old keys alone, so a console
    edit since the stage push would reach the thin charts stale. The old key
    wins here, since it is the one the running charts read.
    """
    pull = _git(deploy, "pull", "--ff-only", "-q")
    if pull.returncode != 0:
        return False, f"git pull --ff-only failed before the retarget: {_last_line(pull.stderr) or 'no detail'}"
    ok, detail = _expand_values(deploy, migration, resync=True)
    if not ok:
        return False, f"a 2.2.0 value key needs a hand edit: {detail}"
    failure, done = _commit_and_push(deploy, f"chore(upgrade): {to_name} carry the 2.2.0 value keys again", push=True)
    if failure:
        return False, failure
    return True, f"carried again before the retarget: {detail}, {done}"


def _finalise_values(
    args: argparse.Namespace,
    deploy: Path,
    migration: values_migration.MigrationMap,
    emit: Callable[[str], None],
    *,
    names: tuple[str, str],
    on_target: bool,
    rollout_namespace: str,
) -> str:
    """Drop the 2.2.0 value keys at --finalise; why that failed, or empty.

    The thin charts read none of them, so the render does not move. A stack
    before `since` cannot render the overlays without them, so this is the
    values migration's one-way step and writes the marker rollback refuses on.
    It runs only once the cluster is shown on the TO stack's charts.
    """
    from_name, to_name = names
    dropped_at = read_finalised_keys(deploy).get(VALUES_CONTRACT_KEY)
    if dropped_at:
        print(f"  [DONE] values: apply --finalise dropped the 2.2.0 value keys ({dropped_at})", file=sys.stderr)
        return ""
    if not args.finalise:
        print(
            f"  finalise pending (manual, after a soak): the 2.2.0 value keys -- apply --finalise drops them, "
            f"after which nothing rolls back below {migration.since}",
            file=sys.stderr,
        )
        return ""
    emit(
        "finalise the values migration: drop each 2.2.0 value key whose thin-chart key is set, "
        f"commit{', push' if args.push else ''} and wait for Argo"
    )
    if args.dry_run:
        return ""
    if not on_target:
        print(
            f"  [PENDING] values: {TARGET_REVISION_ANNOTATION} does not name {to_name}, so nothing shows the thin "
            "charts reading the new keys -- the 2.2.0 keys stay",
            file=sys.stderr,
        )
        return ""
    if not _confirm(
        f"soak complete -- drop the 2.2.0 value keys now? No rollback below {migration.since} renders without them",
        assume_yes=args.yes,
    ):
        print("  [PENDING] values: declined by operator", file=sys.stderr)
        return ""
    hits = values_migration.contract(deploy, migration)
    for hit in hits:
        print(f"  values: {hit.line()}", file=sys.stderr)
    if not any(hit.state == values_migration.CONTRACT for hit in hits):
        print("  [DONE] values: no 2.2.0 value key left to drop", file=sys.stderr)
        return ""
    write_finalise_marker(deploy, from_name, to_name, VALUES_CONTRACT_KEY)
    moment = change_moment()
    failure, done = _commit_and_push(deploy, f"chore(upgrade): {to_name} {VALUES_CONTRACT_SUBJECT}", push=args.push)
    if failure:
        return failure
    pushed_at = moment if args.push else None
    ok, detail = wait_for_argo(
        args.kubeconfig, argocd_namespace=args.argocd_namespace, timeout=args.timeout,
        rollout_namespace=rollout_namespace, reconciled_after=pushed_at,
    )
    print(f"  [DONE] values: the 2.2.0 keys dropped, {done}; argo: {detail}", file=sys.stderr)
    return "" if ok else "Argo and the rollouts did not settle after the 2.2.0 value keys were dropped"


def run_preflight(
    deploy: Path,
    moves: list[Move],
    *,
    from_stack: str,
    to_stack: str,
    kubeconfig: str | None,
    cloud: str | None = None,
    argocd_namespace: str = DEFAULT_ARGOCD_NAMESPACE,
    clickhouse_namespace: str = DEFAULT_CLICKHOUSE_NAMESPACE,
    clickhouse_selector: str = DEFAULT_CLICKHOUSE_SELECTOR,
    clickhouse_merge_threshold: float = DEFAULT_CLICKHOUSE_MERGE_THRESHOLD,
    clickhouse_credentials: str = DEFAULT_CLICKHOUSE_CREDENTIALS,
    nodes_file: Path | None = None,
    backup_marker: str = DEFAULT_BACKUP_MARKER,
) -> list[tuple[str, bool, str]]:
    """Every preflight check, in report order. Does not print -- the caller
    (cmd_upgrade_preflight, or apply before it acts) owns presentation."""
    checks: list[tuple[str, bool, str]] = []
    checks.append(("deploy repo clean", *check_deploy_clean(deploy)))
    checks += exposure_checks(
        deploy, from_stack, to_stack, cloud=cloud, kubeconfig=kubeconfig, argocd_namespace=argocd_namespace
    )
    checks.append(("2.2.0 value keys (values-migrations.yaml)", *check_values_migration(deploy, from_stack, to_stack)))
    checks.append(("cluster reachable", *check_cluster_reachable(kubeconfig)))
    checks.append(("argo apps synced and healthy", *check_argo_apps(kubeconfig, argocd_namespace)))
    checks.append(("no KafkaRebalance in progress", *check_no_kafka_rebalance(kubeconfig)))
    checks.append((
        "clickhouse merges under threshold",
        *check_clickhouse_merges(
            kubeconfig, clickhouse_namespace, clickhouse_selector, clickhouse_merge_threshold, clickhouse_credentials
        ),
    ))
    conversion = next((move for move in moves if "stored-version conversion" in move.step.before), None)
    if conversion is not None:
        checks.append(("strimzi stored-version conversion", *check_strimzi_conversion_before(kubeconfig, conversion)))
    resolved_nodes_file = nodes_file or (deploy / "sizing" / f"{DEFAULT_TIER}.nodes.json")
    checks.append(("on-prem node capacity", *check_node_capacity(kubeconfig, resolved_nodes_file)))
    checks.append(("backup marker (one-way steps)", *check_backup_marker(deploy, moves, backup_marker)))
    return checks


def _check_line(name: str, ok: bool, detail: str) -> str:
    return f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}"


def _print_checks(checks: list[tuple[str, bool, str]]) -> int:
    failed = 0
    for name, ok, detail in checks:
        print(_check_line(name, ok, detail))
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


# The dfe-deploy template's pins.yaml, less its prose, for a deploy repo seeded without one.
_NEW_PIN_FILE = """\
# pins.yaml -- the certified DFE stack this deployment runs, written by
# `dfe-ops upgrade apply` into a deploy repo that carried none.
#
# ONE pin selects the WHOLE stack: dfe-infra's versions.yaml at this version
# names every dfe-* app image, operator and data service. `dfe-ops upgrade
# apply` moves it, and leaves your values/ and config/ untouched.

base:
  dfe-infra: "{stack}"
{channel}
# Per-component overrides -- move ONE component off the certified set, at your
# own documented risk. Use tag@sha256 digests, never floating tags.
# overrides:
#   apps:
#     dfe-receiver: "v1.16.0@sha256:..."
"""

_NEW_PIN_CHANNEL = """
# Release channel this environment tracks: alpha | beta | rc | release.
channel: "{channel}"
"""


def bump_pin_file(deploy: Path, stack: str, *, channel: str = "") -> bool:
    """Set the deploy's pins.yaml to `stack`, idempotently. Returns whether it
    changed anything (False when it already named this stack).

    A deploy repo with no pins.yaml gets dfe-deploy's shape, naming `channel`
    when one is given.
    """
    pins_path = deploy / "pins.yaml"
    if not pins_path.is_file():
        named = _NEW_PIN_CHANNEL.format(channel=channel) if channel else ""
        pins_path.write_text(_NEW_PIN_FILE.format(stack=stack, channel=named), encoding="utf-8")
        return True
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


def _cluster_secret_annotation(kubeconfig: str | None, namespace: str, annotation: str) -> str:
    """One annotation on the cluster secret, empty when unset. Raises UpgradeError when unreadable."""
    jsonpath = "jsonpath={.metadata.annotations." + annotation.replace(".", "\\.") + "}"
    result = _kubectl(
        kubeconfig, "-n", namespace, f"--request-timeout={DEFAULT_KUBECTL_REQUEST_TIMEOUT}",
        "get", "secret", ARGO_CLUSTER, "-o", jsonpath,
    )
    if result.returncode != 0:
        raise UpgradeError(
            f"cannot read {annotation} on secret/{ARGO_CLUSTER} in {namespace}: "
            f"{_last_line(result.stderr) or 'kubectl failed'}"
        )
    return (result.stdout or "").strip()


def read_target_revision(kubeconfig: str | None, namespace: str) -> str:
    """The cluster secret's target_revision. Raises UpgradeError when unreadable."""
    return _cluster_secret_annotation(kubeconfig, namespace, TARGET_REVISION_ANNOTATION)


def read_stack_version(kubeconfig: str | None, namespace: str) -> str:
    """The cluster secret's stack_version: the stack bootstrap deployed, moved by every
    retarget since. Raises UpgradeError when unreadable."""
    return _cluster_secret_annotation(kubeconfig, namespace, STACK_VERSION_ANNOTATION)


def read_dfe_namespace(kubeconfig: str | None, namespace: str) -> str:
    """The cluster secret's dfe_namespace, the namespace bootstrap deployed the apps into.
    It came out of a Secret, so nothing prints it. Raises UpgradeError when unreadable."""
    return _cluster_secret_annotation(kubeconfig, namespace, DFE_NAMESPACE_ANNOTATION)


def decide_retarget(current: str, from_name: str, to_name: str, explicit: str | None) -> tuple[str | None, str]:
    """(the ref to write, why) -- None leaves the secret as it is.

    A tag-pinned secret names the FROM stack and moves to the TO tag (the stack
    name is the git tag). A branch is tracked on purpose and left alone. A
    commit SHA could be any stack, so it refuses without an explicit ref.
    `current` came out of a Secret, so no message here repeats it.
    """
    key = f"secret/{ARGO_CLUSTER} {TARGET_REVISION_ANNOTATION}"
    if explicit:
        if explicit == current:
            return None, f"{key} already names the --target-revision ref"
        return explicit, f"{key} -> {explicit} (--target-revision)"
    if not current:
        raise UpgradeError(f"{key} is unset -- pass --target-revision")
    if _norm_ref(current) == _norm_ref(to_name):
        return None, f"{key} already names the {to_name} tag"
    if _norm_ref(current) == _norm_ref(from_name):
        return to_name, f"{key}: the {from_name} tag -> {to_name}"
    if re.fullmatch(r"[0-9a-f]{40}", current):
        raise UpgradeError(
            f"{key} pins a commit, which names no stack -- pass --target-revision <ref> "
            f"to say where the charts go"
        )
    return None, (
        f"{key} names neither the {from_name} nor the {to_name} tag, so it tracks a branch "
        f"the charts already follow -- left as it is"
    )


def write_target_revision(kubeconfig: str | None, namespace: str, ref: str, stack: str) -> tuple[bool, str]:
    """Move the cluster secret's target_revision, and its stack_version with it."""
    result = _kubectl(
        kubeconfig, "-n", namespace, "annotate", "--overwrite", f"secret/{ARGO_CLUSTER}",
        f"{TARGET_REVISION_ANNOTATION}={ref}", f"{STACK_VERSION_ANNOTATION}={stack}",
    )
    if result.returncode != 0:
        return False, _last_line(result.stderr) or "kubectl annotate failed"
    return True, f"{TARGET_REVISION_ANNOTATION}={ref}, {STACK_VERSION_ANNOTATION}={stack}"


def retarget_command(namespace: str, ref: str, stack: str) -> str:
    return (
        f"kubectl -n {namespace} annotate --overwrite secret/{ARGO_CLUSTER} "
        f"{TARGET_REVISION_ANNOTATION}={ref} {STACK_VERSION_ANNOTATION}={stack}"
    )


# --- bootstrap-installed components ------------------------------------------
# Argo never renders the bootstrap section, so a moved pin changes nothing on the
# cluster until an operator upgrades the release bootstrap.sh installed.

BOOTSTRAP_PREFIX = "bootstrap."
ARGOCD_KEY = "bootstrap.argocd"
# check_bootstrap_move's verdicts; only PENDING keeps an apply from reporting OK.
BOOTSTRAP_DONE, BOOTSTRAP_PENDING, BOOTSTRAP_ADOPTED = "DONE", "PENDING", "ADOPTED"
# The shell variable a printed upgrade reads the domain into, the name bootstrap.sh gives it.
DOMAIN_VAR = "DFE_DOMAIN"
# Marks where the domain goes while the command is quoted; no value it stands for contains it.
_DOMAIN_MARK = "DFE-DOMAIN-FROM-THE-CLUSTER-SECRET"

# The releases bootstrap.sh's steps [2/7], [3/7] and [6/7] install, from the table they read.
BOOTSTRAP_RELEASES: dict[str, helm_releases.HelmRelease] = {
    "bootstrap.external-secrets": helm_releases.EXTERNAL_SECRETS,
    "bootstrap.cert-manager": helm_releases.CERT_MANAGER,
    ARGOCD_KEY: helm_releases.ARGOCD,
}


def _cache_service() -> str:
    """The Valkey Service bootstrap.sh wires Argo to, read from DFE_VALKEY_SERVICE as bootstrap.sh reads it."""
    return os.environ.get("DFE_VALKEY_SERVICE") or argocd_release.DEFAULT_CACHE_SERVICE


def _helm_json(kubeconfig: str | None, *argv: str) -> tuple[object, str]:
    """(parsed output, error) for one read-only helm call."""
    cmd = ["helm", *argv, "-o", "json"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    try:
        result = _run(cmd)
    except FileNotFoundError:
        return None, "helm is not on PATH"
    if result.returncode != 0:
        return None, _last_line(result.stderr) or f"helm {argv[0]} exited {result.returncode}"
    try:
        return json.loads(result.stdout or "null"), ""
    except json.JSONDecodeError as exc:
        return None, f"helm {argv[0]} printed no JSON: {exc}"


def argocd_installed_by_bootstrap(kubeconfig: str | None) -> tuple[bool | None, str]:
    """(bootstrap.sh installed this cluster's Argo CD, why); None when helm cannot say.

    argocd_release.py's own verdict, the one bootstrap.sh's step [6/7] reaches
    before it upgrades Argo, so an adopted Argo is never handed an upgrade.
    """
    scope = ["--namespace", argocd_release.NAMESPACE]
    releases, error = _helm_json(kubeconfig, "list", *scope, "--filter", f"^{argocd_release.RELEASE}$")
    if error:
        return None, f"cannot list helm releases in {argocd_release.NAMESPACE}: {error}"
    listed = [r for r in releases if isinstance(r, dict)] if isinstance(releases, list) else []
    cache = _cache_service()
    if not any(r.get("name") == argocd_release.RELEASE for r in listed):
        return argocd_release.verdict([], None, cache)
    values, error = _helm_json(kubeconfig, "get", "values", argocd_release.RELEASE, *scope)
    if error:
        return None, f"cannot read the values of helm release {argocd_release.RELEASE}: {error}"
    return argocd_release.verdict(listed, values if isinstance(values, dict) else None, cache)


def read_bootstrap_chart(kubeconfig: str | None, release: helm_releases.HelmRelease) -> tuple[str, str]:
    """(the chart version the cluster runs, why it could not be read).

    Read off the Deployment's `helm.sh/chart` label, which the chart stamps with
    its own version. A Deployment without one for this chart is an operator the
    deploy adopted rather than installed, which is the host's to upgrade.
    """
    rc, doc, err = _kubectl_json(kubeconfig, "-n", release.namespace, "get", "deployment", release.deployment)
    if rc != 0:
        return "", f"cannot read deployment/{release.deployment} in {release.namespace}: {err}"
    label = ((doc.get("metadata") or {}).get("labels") or {}).get("helm.sh/chart", "")
    prefix = f"{release.chart}-"
    if not label.startswith(prefix):
        return "", (
            f"deployment/{release.deployment} in {release.namespace} carries no helm.sh/chart label for "
            f"{release.chart} -- an operator this deploy adopted is upgraded by its owner"
        )
    return label[len(prefix):], ""


def bootstrap_upgrade_command(
    release: helm_releases.HelmRelease,
    version: str,
    kubeconfig: str | None,
    argocd_namespace: str = DEFAULT_ARGOCD_NAMESPACE,
) -> str:
    """The helm upgrade an operator runs to move a bootstrap release to `version`.

    --reset-values starts from the new chart's defaults, and every value after it
    is one bootstrap.sh's install sets, read from the same helm_releases.py table,
    so the release ends up as a fresh bootstrap would install it. Argo's login
    values are piped in from argocd_login.py, as bootstrap.sh feeds them on stdin.
    A command that needs the domain reads it off the cluster secret in
    `argocd_namespace` as it runs, and stops when the secret names none, so a
    value read from a Secret is never printed. `upgrade` without --install
    refuses where no such release exists.
    """
    kube = ["--kubeconfig", kubeconfig] if kubeconfig else []
    values = helm_releases.install_args(release, domain=_DOMAIN_MARK, cache_service=_cache_service())
    command = shlex.join([
        "helm", *kube, "-n", release.namespace, "upgrade", release.release, release.chart,
        "--repo", release.repo, "--version", version, "--reset-values", *values,
        "--wait", "--timeout", release.timeout,
    ])
    if release.login_values:
        login = shlex.join(["python3", str(ARGOCD_LOGIN), *kube, "values", "--domain", _DOMAIN_MARK])
        command = f"{login} | {command}"
    if _DOMAIN_MARK not in command:
        return command
    jsonpath = "jsonpath={.metadata.annotations." + DOMAIN_ANNOTATION.replace(".", "\\.") + "}"
    lookup = shlex.join(["kubectl", *kube, "-n", argocd_namespace, "get", "secret", ARGO_CLUSTER, "-o", jsonpath])
    domain = f'"${DOMAIN_VAR}"'
    return f'{DOMAIN_VAR}="$({lookup})" && test -n {domain} && {command.replace(_DOMAIN_MARK, domain)}'


def check_bootstrap_move(
    kubeconfig: str | None, move: Move, argocd_namespace: str = DEFAULT_ARGOCD_NAMESPACE
) -> tuple[str, str]:
    """(BOOTSTRAP_DONE, _PENDING or _ADOPTED, the line saying why).

    A PENDING line ends with the command that moves the release. Argo's owner is
    decided first, so a host's Argo reads ADOPTED rather than PENDING. An Argo
    command reads its domain off the cluster secret in `argocd_namespace`.
    """
    moved = (
        f"{move.step.key} {move.old} -> {move.new}" if move.old != move.new
        else f"{move.step.key} {move.new} (unchanged by this upgrade)"
    )
    release = BOOTSTRAP_RELEASES.get(move.step.key)
    if release is None:
        return BOOTSTRAP_PENDING, (
            f"{moved}: bootstrap.sh installs it, not Argo -- re-run bootstrap/bootstrap.sh with "
            "DFE_STACK_VERSION set to the target stack"
        )
    where = ", where this deploy installed it"
    if move.step.key == ARGOCD_KEY:
        ours, why = argocd_installed_by_bootstrap(kubeconfig)
        if ours is False:
            return BOOTSTRAP_ADOPTED, f"{moved}: {why}, so its owner upgrades it"
        where = f" ({why}), where this deploy installed it" if ours is None else ""
    running, why = read_bootstrap_chart(kubeconfig, release)
    if running == move.new:
        return BOOTSTRAP_DONE, f"{move.step.key} runs {running} (deployment/{release.deployment} helm.sh/chart)"
    seen = f"the cluster runs {running}" if running else why
    return BOOTSTRAP_PENDING, (
        f"{moved}: bootstrap.sh installs it, not Argo, and {seen}. "
        f"Run{where}: {bootstrap_upgrade_command(release, move.new, kubeconfig, argocd_namespace)}"
    )


# --- finalise markers ----------------------------------------------------------
# The record that a one-way step's `finalise` note actually ran -- the fact
# `rollback` keys its refusal on, rather than the pin diff alone. A pin move
# that touches a one-way step is reversible right up until its finalise runs.


def _finalise_marker_path(deploy: Path, from_stack: str, to_stack: str) -> Path:
    """Same `<from>-to-<to>` naming as the plan file -- `apply` writes this name
    in the forward direction it moved, and a rollback checking for it computes
    the SAME name because a rollback's own FROM/TO are that forward move's
    TO/FROM, swapped."""
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


# --- Argo sync and rollout wait (apply) ----------------------------------------
# Argo can report every Application Synced and Healthy while a Deployment it
# synced is still rolling, so a healthy wait also reads each rollout itself.


def rollout_pending(item: dict) -> str:
    """Why one Deployment or StatefulSet has not finished rolling out; empty once it has.

    The conditions `kubectl rollout status` waits on: the controller has observed
    the current generation, every replica runs the new template (up to a
    StatefulSet's partition), no old replica is left, and the replicas are
    available. A StatefulSet on the OnDelete strategy has no rollout to finish.
    """
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    generation = int((item.get("metadata") or {}).get("generation") or 0)
    observed = int(status.get("observedGeneration") or 0)
    if observed < generation:
        return f"generation {generation} not yet observed, at {observed}"
    replicas = int(spec.get("replicas", 1))
    updated = int(status.get("updatedReplicas") or 0)
    available = int(status.get("availableReplicas") or 0)
    if item.get("kind") == "StatefulSet":
        strategy = spec.get("updateStrategy") or {}
        if strategy.get("type", "RollingUpdate") != "RollingUpdate":
            return ""
        want = replicas - int((strategy.get("rollingUpdate") or {}).get("partition") or 0)
        if updated < want:
            return f"{updated} of {want} replicas updated"
        if available < replicas:
            return f"{available} of {replicas} replicas available"
        return ""
    conditions = status.get("conditions") or []
    if any(c.get("type") == "Progressing" and c.get("reason") == "ProgressDeadlineExceeded" for c in conditions):
        return "past its progress deadline"
    if updated < replicas:
        return f"{updated} of {replicas} replicas updated"
    total = int(status.get("replicas") or 0)
    if total > updated:
        return f"{total - updated} old replica(s) still running"
    if available < updated:
        return f"{available} of {updated} updated replicas available"
    return ""


def check_rollouts(kubeconfig: str | None, namespace: str) -> tuple[bool, str]:
    """Every Deployment and StatefulSet in `namespace` has finished rolling out.

    A namespace holding neither fails: it is the wrong namespace or an empty
    stack, and neither is a settled one. The detail never names `namespace`,
    which apply can have read off the cluster secret.
    """
    rc, doc, err = _kubectl_json(kubeconfig, "-n", namespace, "get", "deployments.apps,statefulsets.apps")
    if rc != 0:
        return False, f"cannot list Deployments and StatefulSets in the DFE namespace: {err}"
    items = doc.get("items") or []
    if not items:
        return False, "no Deployment or StatefulSet in the DFE namespace"
    pending = []
    for item in items:
        why = rollout_pending(item)
        if why:
            name = (item.get("metadata") or {}).get("name", "<unnamed>")
            pending.append(f"{str(item.get('kind') or 'workload').lower()}/{name} ({why})")
    if pending:
        extra = f", +{len(pending) - 6} more" if len(pending) > 6 else ""
        return False, f"{len(pending)} of {len(items)} rollout(s) not finished: {', '.join(pending[:6])}{extra}"
    return True, f"{len(items)} rollout(s) finished"


def wait_for_argo(
    kubeconfig: str | None,
    *,
    argocd_namespace: str,
    timeout: float,
    stale_revision: str = "",
    rollout_namespace: str = "",
    reconciled_after: datetime | None = None,
    sleep=time.sleep,
    now=time.monotonic,
) -> tuple[bool, str]:
    """Block until check_argo_apps reports every Application Synced and
    Healthy (none on `stale_revision`, and each the change can move
    reconciled at or after `reconciled_after`), or `timeout` seconds pass.

    With `rollout_namespace`, every rollout there must also have finished,
    inside the same `timeout`; the timeout detail names each Application not
    yet reconciled and each rollout that had not finished.
    """

    def settled() -> tuple[bool, str]:
        ok, detail = check_argo_apps(
            kubeconfig, argocd_namespace, stale_revision=stale_revision,
            reconciled_after=reconciled_after,
        )
        if not ok or not rollout_namespace:
            return ok, detail
        rolled, rollouts = check_rollouts(kubeconfig, rollout_namespace)
        return rolled, f"{detail}; {rollouts}"

    return _wait_until(settled, timeout=timeout, stuck="still not converged", sleep=sleep, now=now)


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


def read_from_stack(
    deploy: Path, from_arg: str | None, *, kubeconfig: str | None, argocd_namespace: str
) -> tuple[str, str]:
    """(the FROM stack, a note saying so when it came from the cluster) -- raises UpgradeError.

    pins.yaml's pin, unless `from_arg` names it. A pins.yaml present is read
    either way, so one apply could not bump refuses before anything moves. A
    deploy repo with no pins.yaml (the bundled one is seeded without it) falls
    back to the cluster secret's stack_version, and apply writes the file at its
    first stage.
    """
    if (deploy / "pins.yaml").is_file():
        pinned = read_deploy_pin(deploy)
        return from_arg or pinned, ""
    if from_arg:
        return from_arg, ""
    stack = read_stack_version(kubeconfig, argocd_namespace)
    if not stack:
        raise UpgradeError(
            f"no pins.yaml in {deploy}, and secret/{ARGO_CLUSTER} carries no {STACK_VERSION_ANNOTATION} -- "
            "commit a pins.yaml whose base.dfe-infra names the stack this deployment runs"
        )
    return stack, (
        f"FROM is secret/{ARGO_CLUSTER}'s {STACK_VERSION_ANNOTATION}: the deploy repo carries "
        "no pins.yaml, so apply writes one at its first stage"
    )


def _load_from_to(
    deploy: Path,
    to_arg: str | None,
    from_arg: str | None = None,
    *,
    kubeconfig: str | None,
    argocd_namespace: str,
) -> tuple[dict, str, dict, str, dict, str]:
    """(root, from_name, from_pins, to_name, to_pins, from_note) -- raises UpgradeError.

    FROM is read_from_stack's; `from_arg` is how an apply whose first stage
    already moved the pin is resumed or finalised.
    """
    root = load_versions_root()
    to_name, to_pins = resolve_stack(root, to_arg or current_stack(root))
    from_stack, from_note = read_from_stack(
        deploy, from_arg, kubeconfig=kubeconfig, argocd_namespace=argocd_namespace
    )
    from_name, from_pins = resolve_stack(root, from_stack)
    return root, from_name, from_pins, to_name, to_pins, from_note


def cmd_upgrade_plan(args: argparse.Namespace) -> int:
    deploy = Path(args.deploy)
    try:
        _root, from_name, from_pins, to_name, to_pins, from_note = _load_from_to(
            deploy, args.to, kubeconfig=args.kubeconfig, argocd_namespace=args.argocd_namespace
        )
    except UpgradeError as err:
        print(f"dfe-ops upgrade plan: {err}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED

    steps = load_steps()
    moves = plan_moves(steps, flatten_stack(from_pins), flatten_stack(to_pins))
    blocked = False

    sections = [render_plan(moves, from_stack=from_name, to_stack=to_name, note=from_note)]

    compat_ok, compat_out = run_compat_check(to_name)
    sections.append(f"## compat-check ({to_name}, --strict)\n\n```\n{compat_out}\n```\n")
    if not compat_ok:
        blocked = True

    # A cluster is in play once one is named, or FROM came off its secret.
    exposure = exposure_checks(
        deploy, from_name, to_name, cloud=args.cloud, kubeconfig=args.kubeconfig, argocd_namespace=args.argocd_namespace,
        cluster=args.kubeconfig is not None or bool(from_note),
    )
    lines = "\n".join(_check_line(*check) for check in exposure)
    sections.append(f"## exposure checks\n\n{lines}\n")
    if not all(ok for _name, ok, _detail in exposure):
        blocked = True

    values_section, values_blocked = render_values_section(deploy, from_name, to_name)
    sections.append(values_section)
    blocked = blocked or values_blocked

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
    out_path = Path(args.out) if args.out else DEFAULT_PLAN_DIR / f"{from_name}-to-{to_name}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8", newline="\n")

    print(text)
    print(f"plan written to {out_path}", file=sys.stderr)
    if blocked:
        print(
            "dfe-ops upgrade plan: BLOCKED -- see compat-check / exposure / values migration / sizing sections above",
            file=sys.stderr,
        )
        return EXIT_BLOCKED
    return EXIT_OK


def render_values_section(deploy: Path, from_stack: str, to_stack: str) -> tuple[str, bool]:
    """(the plan's values-migration section, whether it blocks the plan).

    Lists every hit but the keys already carried: MIGRATE is a key apply writes
    beside its old one before the retarget, REFUSED needs a person and blocks
    the plan, and DROPPED is a key the thin charts read nothing in place of.
    """
    migration = values_migration.load_map()
    crosses, why = values_cutover(from_stack, to_stack, migration.since)
    heading = "## values migration (2.2.0 keys)"
    if not crosses:
        return f"{heading}\n\n{why}\n", False
    hits = [hit for hit in values_migration.scan(deploy, migration) if hit.state != values_migration.DONE]
    if not hits:
        return f"{heading}\n\nno 2.2.0 key in values/ or {COMMON_OVERLAY.as_posix()}\n", False
    lines = "\n".join(hit.line() for hit in hits)
    note = (
        f"apply writes each MIGRATE key beside its old one before it moves {TARGET_REVISION_ANNOTATION}, and "
        "apply --finalise drops the old keys once the soak is over."
    )
    return f"{heading}\n\n{note}\n\n{lines}\n", bool(values_migration.refused(hits))


# --- preflight -------------------------------------------------------------


def cmd_upgrade_preflight(args: argparse.Namespace) -> int:
    deploy = Path(args.deploy)
    try:
        _root, from_name, from_pins, to_name, to_pins, from_note = _load_from_to(
            deploy, args.to, kubeconfig=args.kubeconfig, argocd_namespace=args.argocd_namespace
        )
    except UpgradeError as err:
        print(f"dfe-ops upgrade preflight: {err}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    if from_note:
        print(from_note)

    steps = load_steps()
    moves = plan_moves(steps, flatten_stack(from_pins), flatten_stack(to_pins))
    checks = run_preflight(
        deploy,
        moves,
        from_stack=from_name,
        to_stack=to_name,
        kubeconfig=args.kubeconfig,
        cloud=args.cloud,
        argocd_namespace=args.argocd_namespace,
        clickhouse_namespace=args.clickhouse_namespace,
        clickhouse_selector=args.clickhouse_selector,
        clickhouse_merge_threshold=args.clickhouse_merge_threshold,
        clickhouse_credentials=args.clickhouse_credentials,
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


def _rollout_scope(args: argparse.Namespace) -> str:
    """The namespace the rollout waits read, named by where it comes from rather than by a value read off a Secret."""
    return args.namespace or f"the namespace secret/{ARGO_CLUSTER}'s {DFE_NAMESPACE_ANNOTATION} names"


def _wait_line(args: argparse.Namespace, *, leaving: str = "", reconciled: str = "") -> str:
    """What a healthy wait waits on, as a --dry-run prints it; `leaving` names the ref a retarget leaves.

    `reconciled` names the change each Application must have been reconciled after.
    """
    argo = f"Argo Applications to leave {leaving}" if leaving else f"Argo Applications in {args.argocd_namespace}"
    if reconciled:
        argo += f", each reconciled after {reconciled}"
    return (
        f"wait for {argo}, then every Deployment and StatefulSet in {_rollout_scope(args)} to finish rolling out "
        f"(timeout {args.timeout}s)"
    )


def cmd_upgrade_apply(args: argparse.Namespace) -> int:
    deploy = Path(args.deploy)
    try:
        _root, from_name, from_pins, to_name, to_pins, from_note = _load_from_to(
            deploy, args.to, args.from_stack, kubeconfig=args.kubeconfig, argocd_namespace=args.argocd_namespace
        )
    except UpgradeError as err:
        print(f"dfe-ops upgrade apply: {err}", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    if from_note:
        print(from_note, file=sys.stderr)

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
            from_stack=from_name,
            to_stack=to_name,
            kubeconfig=args.kubeconfig,
            cloud=args.cloud,
            argocd_namespace=args.argocd_namespace,
            clickhouse_namespace=args.clickhouse_namespace,
            clickhouse_selector=args.clickhouse_selector,
            clickhouse_merge_threshold=args.clickhouse_merge_threshold,
            clickhouse_credentials=args.clickhouse_credentials,
            nodes_file=Path(args.nodes_file) if args.nodes_file else None,
            backup_marker=args.backup_marker,
        )
        failed = _print_checks(checks)
        if failed:
            print(f"dfe-ops upgrade apply: REFUSED -- {failed} preflight check(s) failed", file=sys.stderr)
            return EXIT_BLOCKED

    walk = moves_by_stage(moves)
    chart_stage = _chart_stage(walk)
    stage_names = [stage for stage, _ in walk]
    if args.stop_before and args.stop_before not in stage_names:
        have = ", ".join(stage_names) or "none"
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

    reached = stage_names[: stage_names.index(args.stop_before)] if args.stop_before else stage_names
    pins_present = (deploy / "pins.yaml").is_file()
    kafka_move = next((m for m in moves if m.step.key == KAFKA_VERSION_KEY), None)
    kafka_stage = kafka_move.step.stage if kafka_move else None
    migration = values_migration.load_map()
    values_move = values_cutover(from_name, to_name, migration.since)[0]

    # Read before any stage moves, so a cluster in a state apply cannot move
    # refuses with nothing half done.
    rollout_namespace = args.namespace or ""
    if not args.dry_run and not rollout_namespace:
        try:
            rollout_namespace = read_dfe_namespace(args.kubeconfig, args.argocd_namespace)
        except UpgradeError as err:
            print(f"dfe-ops upgrade apply: REFUSED -- {err}", file=sys.stderr)
            return EXIT_BLOCKED
        if not rollout_namespace:
            print(
                f"dfe-ops upgrade apply: REFUSED -- secret/{ARGO_CLUSTER} carries no {DFE_NAMESPACE_ANNOTATION}, "
                "so no rollout can be waited on -- pass --namespace",
                file=sys.stderr,
            )
            return EXIT_BLOCKED
    if not args.dry_run:
        print(f"  rollouts: every Deployment and StatefulSet in {_rollout_scope(args)}", file=sys.stderr)
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

    bootstrap_pending: list[str] = []
    for stage_index, (stage, stage_moves) in enumerate(walk, start=1):
        if args.stop_before and stage == args.stop_before:
            print(
                f"\ndfe-ops upgrade apply: stopping before stage {stage_index}/{len(walk)} ({stage}) "
                "-- --stop-before",
                file=sys.stderr,
            )
            return _report_bootstrap_pending(bootstrap_pending, from_name, to_name)
        print(f"\n=== stage {stage_index}/{len(walk)}: {stage} ===", file=sys.stderr)
        for move in stage_moves:
            print(f"  {move.step.key}: {move.old} -> {move.new}", file=sys.stderr)
            for label, key, old, new in move.pins:
                print(f"    {label} {key}: {old} -> {new}", file=sys.stderr)

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

        verb = "set pins.yaml" if pins_present else "write pins.yaml with"
        emit(f'{verb} base.dfe-infra = "{to_name}"')
        pins_present = True
        if not args.dry_run:
            bump_pin_file(deploy, to_name, channel=str(to_pins.get("maturity") or ""))

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

        # The 2.2.0 charts read none of the keys this adds, so they ride the stage
        # commit, ahead of the retarget that hands the overlays to the thin charts.
        if stage == chart_stage and values_move:
            emit(
                "carry the 2.2.0 value keys onto the thin-chart keys beside them in values/ and "
                f"{COMMON_OVERLAY.as_posix()}, as {values_migration.MAP_FILE.relative_to(REPO_ROOT).as_posix()} maps them"
            )
            if not args.dry_run:
                ok, detail = _expand_values(deploy, migration, resync=retarget is not None)
                print(f"  values: {detail}", file=sys.stderr)
                if not ok:
                    return _stage_failed(stage_index, f"a 2.2.0 value key needs a hand edit: {detail}", stage_moves)

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

        pushed_at: datetime | None = None
        if args.push:
            emit(f"git -C {deploy} push")
            if not args.dry_run:
                pushed_at = _push_moment(deploy)
                push = _git(deploy, "push")
                if push.returncode != 0:
                    return _stage_failed(stage_index, "git push failed", stage_moves)

        emit(_wait_line(args, reconciled="any change it pushes" if args.push else ""))
        if not args.dry_run:
            ok, detail = wait_for_argo(
                args.kubeconfig, argocd_namespace=args.argocd_namespace, timeout=args.timeout,
                rollout_namespace=rollout_namespace, reconciled_after=pushed_at,
            )
            print(f"  argo: {detail}", file=sys.stderr)
            if not ok:
                return _stage_failed(stage_index, "Argo and the rollouts did not settle", stage_moves)

        for move in stage_moves:
            if move.step.key.startswith(BOOTSTRAP_PREFIX) and _bootstrap_pending(args, move, emit):
                bootstrap_pending.append(move.step.key)

        if stage == chart_stage:
            ref = args.target_revision or to_name
            if values_move:
                emit(
                    f"git -C {deploy} pull --ff-only, carry the 2.2.0 value keys again from what the 2.2.0 engine "
                    "wrote since the push, then commit and push what changed"
                )
            emit(f"if secret/{ARGO_CLUSTER} targets {from_name}: {retarget_command(args.argocd_namespace, ref, to_name)}")
            emit(_wait_line(args, leaving=from_name, reconciled="the retarget"))
            if not args.dry_run and retarget:
                if values_move:
                    ok, detail = _recarry_values(deploy, migration, to_name)
                    print(f"  values: {detail}", file=sys.stderr)
                    if not ok:
                        return _stage_failed(stage_index, detail, stage_moves)
                retargeted_at = change_moment()
                ok, detail = write_target_revision(args.kubeconfig, args.argocd_namespace, retarget, to_name)
                print(f"  [{'DONE' if ok else 'FAIL'}] retarget: {detail}", file=sys.stderr)
                if not ok:
                    return _stage_failed(stage_index, "the cluster secret did not take the new target_revision", stage_moves)
                ok, detail = wait_for_argo(
                    args.kubeconfig, argocd_namespace=args.argocd_namespace, timeout=args.timeout,
                    stale_revision=current_ref, rollout_namespace=rollout_namespace,
                    reconciled_after=retargeted_at,
                )
                print(f"  argo: {detail}", file=sys.stderr)
                if not ok:
                    return _stage_failed(stage_index, f"Argo and the rollouts did not settle on {retarget}", stage_moves)
            if values_move:
                on_target = bool(retarget) or _norm_ref(current_ref) in (_norm_ref(to_name), _norm_ref(ref))
                failure = _finalise_values(
                    args, deploy, migration, emit,
                    names=(from_name, to_name), on_target=on_target, rollout_namespace=rollout_namespace,
                )
                if failure:
                    return _stage_failed(stage_index, failure, stage_moves)

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

    # A pin this plan does not move can still be one an earlier upgrade left uninstalled.
    walked = {move.step.key for _stage, stage_moves in walk for move in stage_moves}
    from_flat, to_flat = flatten_stack(from_pins), flatten_stack(to_pins)
    held = [
        Move(step=step, old=from_flat.get(step.key) or "(absent)", new=to_flat[step.key])
        for step in steps
        if step.key.startswith(BOOTSTRAP_PREFIX) and step.key not in walked and to_flat.get(step.key)
    ]
    if held:
        print("\n=== bootstrap releases this plan does not move ===", file=sys.stderr)
    for move in held:
        if _bootstrap_pending(args, move, emit):
            bootstrap_pending.append(move.step.key)

    if args.dry_run:
        print(f"\n[dry-run] {len(commands)} command(s) would run; nothing was executed", file=sys.stderr)
        return EXIT_OK
    if bootstrap_pending:
        return _report_bootstrap_pending(bootstrap_pending, from_name, to_name)
    print(f"\ndfe-ops upgrade apply OK: {from_name} -> {to_name} ({len(walk)} stage(s))", file=sys.stderr)
    return EXIT_OK


def _bootstrap_pending(args: argparse.Namespace, move: Move, emit: Callable[[str], None]) -> bool:
    """Whether the cluster is not shown running a bootstrap pin; a dry run only names the read and the command."""
    if args.dry_run:
        release = BOOTSTRAP_RELEASES.get(move.step.key)
        command = (
            bootstrap_upgrade_command(release, move.new, args.kubeconfig, args.argocd_namespace)
            if release else "re-run bootstrap/bootstrap.sh"
        )
        emit(f"# {move.step.key} is installed by bootstrap.sh, not Argo; unless it runs {move.new}: {command}")
        return False
    state, detail = check_bootstrap_move(args.kubeconfig, move, args.argocd_namespace)
    print(f"  [{state}] {detail}", file=sys.stderr)
    return state == BOOTSTRAP_PENDING


def _report_bootstrap_pending(pending: list[str], from_name: str, to_name: str) -> int:
    """EXIT_OK when every bootstrap pin reached runs on the cluster; else say which do not and refuse OK."""
    if not pending:
        return EXIT_OK
    print(
        f"\ndfe-ops upgrade apply: {from_name} -> {to_name} NOT complete -- {', '.join(pending)} not "
        f"shown running the pinned chart. Run the PENDING command(s) above, then re-run this apply with "
        f"--from {from_name} to confirm.",
        file=sys.stderr,
    )
    return EXIT_BLOCKED


# The reverse path of a step that declares none: the backup marker guards only one-way steps.
_ROLLBACK_DEFAULT = "not declared -- dfe-ops upgrade rollback --to <the FROM stack> moves the pin and target_revision back"


def _print_rollback(stage_moves: list[Move]) -> None:
    for move in stage_moves:
        note = move.step.rollback or ("none (finalise: " + move.step.finalise + ")" if move.step.finalise else "")
        print(f"  rollback ({move.step.key}): {note or _ROLLBACK_DEFAULT}", file=sys.stderr)


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

    # The charts before `since` cannot render the overlays once the 2.2.0 keys are gone.
    since = values_migration.load_map().since
    if VALUES_CONTRACT_KEY in finalised and values_cutover(to_name, from_name, since)[0]:
        print(
            f"dfe-ops upgrade rollback: REFUSED -- apply --finalise dropped the 2.2.0 value keys "
            f"({finalised[VALUES_CONTRACT_KEY]}), and a stack before {since} renders the overlays without them: "
            "every per-config workload renamed, the fetcher's cursor claim pruned. Revert the deploy repo's "
            f"'{VALUES_CONTRACT_SUBJECT}' commit first, which also removes its marker line",
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


# --- migrate-values ------------------------------------------------------------


def cmd_upgrade_migrate_values(args: argparse.Namespace) -> int:
    """Carry a deploy checkout's 2.2.0 value keys onto the thin-chart keys, or with --check list them.

    Writes each new key beside its old one and never overwrites a key already
    set, so a second run changes nothing. It commits nothing. --check writes
    nothing and exits 1 while a key is left to carry or needs a hand edit.
    """
    deploy = Path(args.deploy)
    if not deploy.is_dir():
        print(f"dfe-ops upgrade migrate-values: {deploy} is not a directory", file=sys.stderr)
        return EXIT_PREFLIGHT_FAILED
    migration = values_migration.load_map()
    hits = (values_migration.scan if args.check else values_migration.expand)(deploy, migration)
    shown = [hit for hit in hits if hit.state != values_migration.DONE]
    for hit in shown:
        print(hit.line())
    left = values_migration.pending(hits) if args.check else values_migration.refused(hits)
    carried = [hit for hit in hits if hit.state == values_migration.MIGRATE]
    if args.check:
        verdict = f"{len(left)} 2.2.0 value key(s) to carry or edit by hand" if left else "nothing to carry"
        print(f"dfe-ops upgrade migrate-values --check: {verdict} (map: since {migration.since})", file=sys.stderr)
    elif left:
        print(
            f"dfe-ops upgrade migrate-values: {len(carried)} key(s) written, {len(left)} need a hand edit (REFUSED above)",
            file=sys.stderr,
        )
    else:
        print(
            f"dfe-ops upgrade migrate-values: {len(carried)} key(s) written beside their 2.2.0 keys"
            + (" -- commit and push them" if carried else ""),
            file=sys.stderr,
        )
    return EXIT_BLOCKED if left else EXIT_OK


# --- parser --------------------------------------------------------------------


def _add_deploy_target_args(parser: argparse.ArgumentParser, *, to_required: bool = False) -> None:
    parser.add_argument("--deploy", required=True, help="the dfe-deploy checkout (pins.yaml's directory)")
    parser.add_argument(
        "--to",
        required=to_required,
        default=None,
        help="target versions.yaml stack (default: the `current` pointer)",
    )


def _add_cloud_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cloud", default=None, metavar="<cloud>",
        help=f"the deployment's cloud (aws, gcp, azure, local, ...), which decides whether its gateway is "
        f"internet-facing (default: the cluster secret's {CLOUD_ANNOTATION}, read only when the web check needs it)",
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
    parser.add_argument(
        "--clickhouse-credentials", default=DEFAULT_CLICKHOUSE_CREDENTIALS,
        help="Secret in --clickhouse-namespace whose `password` key logs clickhouse-client in",
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
    _add_cloud_arg(plan)
    plan.add_argument(
        "--kubeconfig", default=None,
        help="kubeconfig for the target cluster, read only when the deploy repo carries no pins.yaml, "
        "for the cloud when --cloud is not given, or for the self ranges a web plane refusal names",
    )
    plan.add_argument("--argocd-namespace", default=DEFAULT_ARGOCD_NAMESPACE, help="namespace holding the cluster secret")
    plan.add_argument(
        "--out", default=None, metavar="<file>",
        help="where the plan is written (default: .tmp/upgrades/<from>-to-<to>.md in this dfe-infra "
        "checkout, outside the deploy repo, whose untracked files preflight refuses)",
    )
    plan.set_defaults(func=cmd_upgrade_plan)

    preflight = verbs.add_parser(
        "preflight",
        help="the checks apply refuses to run without",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_deploy_target_args(preflight)
    _add_preflight_args(preflight)
    _add_cloud_arg(preflight)
    preflight.set_defaults(func=cmd_upgrade_preflight)

    apply_ = verbs.add_parser(
        "apply",
        help="execute the plan stage by stage",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_deploy_target_args(apply_)
    _add_sizing_args(apply_)
    _add_preflight_args(apply_)
    _add_cloud_arg(apply_)
    apply_.add_argument("--yes", action="store_true", help="do not ask for confirmation between stages")
    apply_.add_argument("--push", action="store_true", help="git push after each stage's commit")
    apply_.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help="bounded wait (seconds) for Argo to converge and the rollouts to finish, per stage",
    )
    apply_.add_argument(
        "--namespace", default=None, metavar="<ns>",
        help=f"the DFE namespace whose Deployments and StatefulSets every wait waits on to finish rolling "
        f"out (default: the cluster secret's {DFE_NAMESPACE_ANNOTATION})",
    )
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
        help="stop the run before this upgrade-order.yaml stage (e.g. 30-services), "
        "touching nothing in it or after",
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

    migrate = verbs.add_parser(
        "migrate-values",
        help="write the thin-chart keys beside a deploy checkout's 2.2.0 value keys (--check: list them)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    migrate.add_argument("--deploy", required=True, help="the dfe-deploy checkout holding values/ and infra/")
    migrate.add_argument(
        "--check", action="store_true",
        help="write nothing; exit 1 while a 2.2.0 key is left to carry or needs a hand edit",
    )
    migrate.set_defaults(func=cmd_upgrade_migrate_values)


def _add_target_revision_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--target-revision",
        default=None,
        metavar="<ref>",
        help=f"the dfe-infra ref to write as the cluster secret's {TARGET_REVISION_ANNOTATION} "
        "(default: the target stack's tag, when the secret is pinned to the FROM stack's)",
    )
