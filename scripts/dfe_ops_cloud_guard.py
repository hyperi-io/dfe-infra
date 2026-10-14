#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/dfe_ops_cloud_guard.py
#  Purpose:      `dfe-ops cloud-preflight` and `dfe-ops cloud-cycle` -- the
#                read-only checks that refuse an unattended cloud test run, and
#                the run itself: per-run state, run tags, and a teardown that
#                fires on every way out. Registered into dfe-ops the way
#                dfe_ops_bastion.py is.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-ops cloud-preflight / cloud-cycle -- a cloud test run that cannot strand spend.

    dfe-ops cloud-preflight --tf-dir terraform/environments/aws --run-length 3h
    dfe-ops cloud-cycle --tf-dir terraform/environments/aws --run-length 3h \\
        -- --env-file bootstrap/.env

The cycle's mode is the dial's profile, read from the root's rendered tfvars
(render_dial.py --tofu writes it), and cloud-cycle adds `--mode <profile>` to
the cycle arguments. `dfe-ops cycle`'s own parser reads the arguments before
anything is created, and cloud-cycle refuses any it rejects, --keep, and a
`--mode` naming any other profile: one dial, one profile.

cloud-preflight reads and never writes. It REFUSES (exit 2) when:

  (a) the account guardrails it is told to expect cannot be read: the guardrail
      role, the in-cloud sweeper and the budget action (and the permissions
      boundary, when one is configured), each a configured identifier;
  (b) the active credential expires before now + run length + teardown margin,
      so the run could lose the identity its own teardown needs, or it is a
      session credential (AWS_SESSION_TOKEN) whose expiry cannot be read;
  (c) run-tagged resources whose expiry has already passed exist in the region
      -- a previous run left them, and it prints the cloud_sweep command that
      removes them;
  (d) a run is unfinished: a run record exists under the state prefix, or a
      run-tagged resource in the region has not yet expired. A run's names come
      from its dial, so applying the dial again stops on AlreadyExists after
      paying for part of a deployment;
  (e) a command bootstrap.sh or destroy.sh runs is not on the PATH the run's
      children get (bootstrap/check_tools.py names them). The deploy would
      otherwise stop at it after `tofu apply` had built the cluster.

(c) and (d) leave out a hit AWS reports deleted, terminated or not found, which
the tagging API goes on listing for a while: an EC2 object, a KMS key scheduled
for deletion, an EKS pod identity association whose cluster is gone, or a secret
Secrets Manager no longer lists. One whose state cannot be read counts as
present, and so does a secret scheduled for deletion, which keeps its name.

It also refuses a dial whose tags.lifecycle is not ephemeral, because a
persistent deployment's deletion protection would stop the teardown short.

cloud-cycle runs the same preflight, then mints a run id and an expiry
(now + run length + teardown margin) and writes `zz-dfe-run.auto.tfvars.json`
into the root. That overlay gives the run its own state key
(`<prefix>/<run id>/terraform.tfstate`), the run's id and expiry as tags on
everything it creates, the guardrail inputs (boundary, IAM path, bucket
prefix) and CloudTrail off. Given an endpoint CIDR, it also opens the
Kubernetes API's public endpoint to that one address, in place of whatever the
dial sets, so a runner outside the VPC can reach the cluster it created. The
private endpoint stays on either way. The same address becomes the run's
edge_self_ranges, which the gateway's web exposure groups admit beside the
cluster's own NAT addresses, so the runner reaches the console and nothing off
those addresses does until the deploy repo names its own lists. Every child gets
it as DFE_PROBE_SOURCE, so the readiness gate's admin probe fails a gateway that
does not answer the runner rather than skipping it. A run record
goes beside the state BEFORE the apply, so the scheduled reaper can destroy the
run if this process dies.
From then on every way out -- success, failure, an exception, SIGINT, SIGTERM,
SIGHUP, the run length running out -- tears the run down: the in-flight child
is stopped, the cluster's workloads go if the cycle did not reach its own
destroy, then `tofu destroy`.
When `tofu destroy` fails, cloud_sweep.py deletes the network interfaces that
carry the run's tag, and the destroy runs once more. The VPC CNI allocates
those outside tofu, and one whose node terminated mid-destroy stays behind
holding its subnet. Nothing else is swept while state remains.
Only a destroy that succeeds removes the overlay and the run record; a failed
one leaves both, and the run's tags, for the reaper. After a successful destroy
cloud_sweep.py deletes what still carries the run tag with this run's id --
what controllers created outside tofu, such as the CA secrets ESO pushes -- and
lists it again. Anything left fails the run, since the next preflight's (d)
would refuse on it. SIGKILL skips all of
this, which is what the reaper is for. Every child runs with AWS_REGION and
AWS_DEFAULT_REGION set to the dial's region, so a call that names no region
lands in the run's region rather than the shell's.

The run length is a deadline, not a hint. Apply and cycle share it, and a
cycle still running when it passes is stopped, its whole process tree with
it, so the teardown starts inside the credential with the margin still ahead
of it. The workload teardown gets half the margin and is stopped there, so
`tofu destroy` always runs. A tofu apply is never cut short, since a killed
apply strands its state lock and resources its state never recorded: an
apply that finishes past the run length skips the cycle instead.

cloud-preflight and cloud-cycle both refuse a dial whose clickhouse.mode is external
unless DFE_CYCLE_CH_SERVICE_ID, DFE_CYCLE_CH_API_KEY_ID, DFE_CYCLE_CH_API_KEY_SECRET
and TF_VAR_clickhouse_external_password are set. After its apply the run adds its NAT
egress (DFE_EGRESS_CIDRS) and the endpoint CIDR to the ClickHouse Cloud service's
IP access list, and the teardown removes them first (scripts/clickhouse_cloud_access.py).

cloud-cycle also refuses, before the preflight and the apply, a cycle that runs the edge
suite (--acceptance-suite edge) when DFE_RUN_INGEST_ALLOW_CIDR is unset or admits every
address. It is read from the environment and the cycle's --env-file files, as dfe-ops
cycle reads it, which would otherwise refuse only after the apply had built the cluster.

Every value is a flag or an environment variable: DFE_GUARD_ROLE,
DFE_GUARD_SWEEPER, DFE_GUARD_BUDGET_ACTION (`<budget-name>:<action-id>`),
DFE_GUARD_PERMISSIONS_BOUNDARY, DFE_GUARD_IAM_PATH, DFE_GUARD_S3_BUCKET_PREFIX,
DFE_GUARD_INSPECTOR_EXCLUSION, DFE_RUN_STATE_PREFIX, DFE_RUN_ENDPOINT_CIDR (one
public IPv4 address, written `<address>/32`). The account, region and state
bucket come from the root's own JSON tfvars.
"""

import argparse
import contextlib
import functools
import importlib.machinery
import importlib.util
import ipaddress
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import NoReturn

import aws_cli
import cloud_run
import cloud_sweep
import private_file
from dfe_ops_edge_probe import PROBE_SOURCE_ENV

REPO_ROOT = Path(__file__).resolve().parent.parent
DFE_OPS = REPO_ROOT / "scripts" / "dfe-ops"
RUNS_DIR = REPO_ROOT / ".tmp" / "cloud-runs"

# Lexically last among the root's auto tfvars, which load_tfvars enforces, so every value in it wins.
OVERLAY_NAME = "zz-dfe-run.auto.tfvars.json"
DEFAULT_TEARDOWN_MARGIN = "45m"
CHILD_STOP_TIMEOUT = 60  # seconds a stopped child gets to exit before it is killed
# A tofu inside the stopped cycle finishes its in-flight calls and saves state on SIGTERM;
# killed sooner, it leaves the state lock held and the guard's own destroy refused.
DEADLINE_STOP_TIMEOUT = 300
WORKLOAD_TEARDOWN_SHARE = 0.5  # of the margin; tofu destroy keeps the rest
DEADLINE_EXIT = 124  # what a run stopped at its run length returns, as timeout(1) does
TEARDOWN_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)
)
ENDPOINT_CIDR_ENV = "DFE_RUN_ENDPOINT_CIDR"
# The ClickHouse Cloud access list a run against an external server opens and closes.
CLICKHOUSE_ACCESS = REPO_ROOT / "scripts" / "clickhouse_cloud_access.py"
CLICKHOUSE_ACCESS_CIDRS_ENV = "DFE_CH_ACCESS_CIDRS"
CLICKHOUSE_ACCESS_ENV = (
    "DFE_CYCLE_CH_SERVICE_ID",
    "DFE_CYCLE_CH_API_KEY_ID",
    "DFE_CYCLE_CH_API_KEY_SECRET",
    "TF_VAR_clickhouse_external_password",
)
CLICKHOUSE_CLOSE_TIMEOUT = 120  # seconds the close gets, out of the teardown margin
# The one list of what bootstrap.sh and destroy.sh run, checked before the apply.
CHECK_TOOLS = REPO_ROOT / "bootstrap" / "check_tools.py"
CHECK_TOOLS_TIMEOUT = 60  # seconds; it only searches PATH


class GuardError(RuntimeError):
    """The run cannot be configured or checked; the message is what dfe-ops prints."""


class _Interrupted(BaseException):
    """A termination signal, raised where the main thread is so `finally` tears down."""


class DeadlineError(RuntimeError):
    """A child was still running at its deadline and has been stopped; the message names it."""


# --- configuration -----------------------------------------------------------


def load_tfvars(tf_dir: Path) -> dict[str, object]:
    """The variables OpenTofu would load from this root, in its own order, later wins.

    Only JSON is read: an HCL tfvars file cannot be parsed with the stdlib, so
    its presence is refused by name rather than silently skipped. The run's own
    overlay is never read back, so a stale one cannot feed the next run.

    Raises:
        GuardError: An HCL tfvars file is present, a JSON one does not parse, or
            an auto tfvars file sorts after the run's overlay and would override it.
    """
    auto = sorted((*tf_dir.glob("*.auto.tfvars"), *tf_dir.glob("*.auto.tfvars.json")), key=lambda p: p.name)
    after_overlay = [p.name for p in auto if p.name > OVERLAY_NAME]
    if after_overlay:
        raise GuardError(
            f"{', '.join(after_overlay)} sorts after {OVERLAY_NAME}, so OpenTofu would let it override "
            "the run's own state key, tags and endpoint: rename it to sort first"
        )
    candidates = [tf_dir / "terraform.tfvars", tf_dir / "terraform.tfvars.json", *auto]
    merged: dict[str, object] = {}
    for path in candidates:
        if not path.is_file() or path.name == OVERLAY_NAME:
            continue
        if not path.name.endswith(".json"):
            raise GuardError(f"{path} is HCL; the guard reads JSON tfvars only (render_dial.py --tofu writes JSON)")
        try:
            merged.update(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, AttributeError, TypeError) as exc:
            raise GuardError(f"{path} is not a JSON object: {exc}") from exc
    return merged


def _setting(flag: str | None, env: Mapping[str, str], name: str, default: str = "") -> str:
    # A workflow passes an unset variable as an empty string, which means unset here too.
    return flag if flag is not None else (env.get(name, "").strip() or default)


def endpoint_cidr(raw: str) -> str:
    """The one address a run opens the Kubernetes API's public endpoint to, as `<address>/32`.

    A bare address is read as its /32. Empty means the run leaves the dial's
    endpoint as it is.

    Raises:
        GuardError: The value is not one public IPv4 address.
    """
    if not raw:
        return ""
    try:
        network = ipaddress.ip_network(raw, strict=True)
    except ValueError as exc:
        raise GuardError(f"endpoint CIDR {raw!r} is not an address: {exc}") from exc
    if network.version != 4 or network.prefixlen != 32 or not network.is_global:
        raise GuardError(
            f"endpoint CIDR {raw!r} is not one public IPv4 address: a run opens the Kubernetes "
            "API to the machine running it and to nothing wider"
        )
    return str(network)


@dataclass(frozen=True, slots=True)
class GuardConfig:
    """Everything a guarded run needs, resolved once from flags, env and the root's tfvars."""

    provider: str
    tf_dir: Path
    tf_root: str
    tfvars: dict[str, object]
    account: str
    region: str
    state_bucket: str
    state_region: str
    state_prefix: str
    role: str
    sweeper: str
    budget_action: str
    run_length: int
    teardown_margin: int
    keys: cloud_run.RunTagKeys
    permissions_boundary: str = ""
    iam_path: str = ""
    s3_bucket_prefix: str = ""
    inspector_exclusion: bool = False
    endpoint_cidr: str = ""

    @property
    def window(self) -> int:
        """Seconds from start until the run must be gone: run length plus teardown margin."""
        return self.run_length + self.teardown_margin


def resolve_config(args: argparse.Namespace, env: Mapping[str, str]) -> GuardConfig:
    """Resolve flags over env over the root's tfvars, refusing what a run cannot use.

    Raises:
        GuardError: A value is missing, malformed, or names something the reaper could not follow.
    """
    tf_dir = Path(args.tf_dir).resolve()
    if not tf_dir.is_dir():
        raise GuardError(f"--tf-dir {args.tf_dir} is not a directory")
    try:
        tf_root = tf_dir.relative_to(REPO_ROOT).as_posix()
    except ValueError as exc:
        raise GuardError(f"--tf-dir must be a root inside this repository, not {tf_dir}") from exc
    tfvars = load_tfvars(tf_dir)
    provision = tfvars.get("provision") if isinstance(tfvars.get("provision"), dict) else {}
    state = tfvars.get("state") if isinstance(tfvars.get("state"), dict) else {}
    tags = tfvars.get("tags") if isinstance(tfvars.get("tags"), dict) else {}
    if tags.get("lifecycle") != "ephemeral":
        raise GuardError(
            f"tags.lifecycle is {tags.get('lifecycle')!r}, not ephemeral: a persistent deployment keeps "
            "deletion protection and recovery windows that stop an unattended teardown short"
        )
    try:
        keys = cloud_run.RunTagKeys.from_env(env)
        run_length = cloud_run.parse_duration(args.run_length)
        teardown_margin = cloud_run.parse_duration(args.teardown_margin)
        state_prefix = cloud_run.validate_state_prefix(
            _setting(args.state_prefix, env, cloud_run.STATE_PREFIX_ENV, cloud_run.DEFAULT_STATE_PREFIX)
        )
    except cloud_run.RunTagError as exc:
        raise GuardError(str(exc)) from exc
    if run_length <= 0:
        raise GuardError("--run-length must be more than zero")
    config = GuardConfig(
        provider=args.provider,
        tf_dir=tf_dir,
        tf_root=tf_root,
        tfvars=tfvars,
        account=args.account or str(provision.get("account", "")),
        region=args.region or str(provision.get("region", "")),
        state_bucket=str(state.get("bucket", "")),
        state_region=str(state.get("region", "")),
        state_prefix=state_prefix,
        role=_setting(args.guard_role, env, "DFE_GUARD_ROLE"),
        sweeper=_setting(args.guard_sweeper, env, "DFE_GUARD_SWEEPER"),
        budget_action=_setting(args.guard_budget_action, env, "DFE_GUARD_BUDGET_ACTION"),
        run_length=run_length,
        teardown_margin=teardown_margin,
        keys=keys,
        permissions_boundary=_setting(args.permissions_boundary, env, "DFE_GUARD_PERMISSIONS_BOUNDARY"),
        iam_path=_setting(args.iam_path, env, "DFE_GUARD_IAM_PATH"),
        s3_bucket_prefix=_setting(args.s3_bucket_prefix, env, "DFE_GUARD_S3_BUCKET_PREFIX"),
        inspector_exclusion=bool(args.inspector_exclusion)
        or env.get("DFE_GUARD_INSPECTOR_EXCLUSION", "").lower() in ("1", "true", "yes"),
        endpoint_cidr=endpoint_cidr(_setting(args.endpoint_cidr, env, ENDPOINT_CIDR_ENV)),
    )
    missing = [
        name
        for name, value in (
            ("account (provision.account or --account)", config.account),
            ("region (provision.region or --region)", config.region),
            ("state.bucket", config.state_bucket),
            ("state.region", config.state_region),
        )
        if not value
    ]
    if missing:
        raise GuardError(f"the root's tfvars do not name: {', '.join(missing)}")
    return config


# --- the cloud the guard reads ------------------------------------------------


def _parse_instant(raw: str) -> float:
    """Epoch seconds from an ISO-8601 instant; a naive one is refused, since it names no instant."""
    stamp = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError(f"{raw!r} carries no zone")
    return stamp.timestamp()


@dataclass
class AwsGuard:
    """The AWS reads the preflight makes and the writes the run makes, in one region."""

    region: str
    name: str = "aws"

    def _read(self, args: list[str], region: str | None = None) -> dict:
        return cloud_sweep.run_aws(args, region or self.region)

    def caller_account(self) -> str:
        return str(self._read(["sts", "get-caller-identity"]).get("Account", ""))

    def role(self, identifier: str) -> str:
        """The role's ARN; a name or an ARN (path included) is accepted."""
        name = identifier.rsplit("/", 1)[-1]
        return str(self._read(["iam", "get-role", "--role-name", name])["Role"]["Arn"])

    def sweeper(self, identifier: str) -> str:
        """The sweeper function's state; Failed is refused by the caller."""
        body = self._read(["lambda", "get-function-configuration", "--function-name", identifier])
        return str(body.get("State") or "Active")

    def budget_action(self, account: str, identifier: str) -> str:
        """The budget action's status, from `<budget-name>:<action-id>` (a budget name cannot hold ':')."""
        budget, sep, action_id = identifier.rpartition(":")
        if not sep or not budget or not action_id:
            raise GuardError(f"budget action {identifier!r} is not <budget-name>:<action-id>")
        body = self._read(
            ["budgets", "describe-budget-action", "--account-id", account, "--budget-name", budget,
             "--action-id", action_id]
        )
        return str(body.get("Action", {}).get("Status", "unknown"))

    def policy(self, arn: str) -> str:
        return str(self._read(["iam", "get-policy", "--policy-arn", arn])["Policy"]["Arn"])

    def credential_expiry(self, env: Mapping[str, str]) -> tuple[float | None, str]:
        """When the active credential expires, and where that reading came from.

        AWS_CREDENTIAL_EXPIRATION wins when set. Otherwise the CLI's own resolver
        answers. Its output carries the secret, so only Expiration and whether a
        SessionToken is present are kept, and nothing it printed reaches an
        error message. None means a long-term credential with no expiry.

        Raises:
            GuardError: The expiry cannot be read, or the credential is a session
                credential that names none, so the check cannot pass.
        """
        raw = env.get("AWS_CREDENTIAL_EXPIRATION")
        if raw:
            try:
                return _parse_instant(raw), "AWS_CREDENTIAL_EXPIRATION"
            except ValueError as exc:
                raise GuardError(f"AWS_CREDENTIAL_EXPIRATION is not an ISO-8601 instant: {exc}") from exc
        result = aws_cli.run_aws(["configure", "export-credentials", "--format", "process"], timeout=60)
        if result.returncode != 0:
            raise GuardError(f"could not resolve the active credential: {result.stderr.strip()}")
        try:
            body = json.loads(result.stdout)
            expiration = body.get("Expiration")
            session = bool(body.get("SessionToken"))
        except (json.JSONDecodeError, AttributeError) as exc:
            raise GuardError("aws configure export-credentials returned no JSON object") from exc
        if not expiration:
            if session:
                raise GuardError(
                    "the active credential is a session credential with no expiry the CLI can read: set "
                    "AWS_CREDENTIAL_EXPIRATION to its expiry"
                )
            return None, "a credential with no expiry"
        try:
            return _parse_instant(str(expiration)), "aws configure export-credentials"
        except ValueError as exc:
            raise GuardError(f"the credential's Expiration is not an ISO-8601 instant: {exc}") from exc

    def run_resources(self, keys: cloud_run.RunTagKeys, exclude_bucket: str) -> list:
        """Every run-tagged resource in the region, whatever its run or expiry, the state bucket aside.

        A hit the tagging API still lists after AWS deleted it is not one: collect
        drops it (cloud_sweep.tagging_hit_gone names the classes it can judge).
        """
        found = cloud_sweep.AwsProvider(self.region).collect({}, exclude_bucket, run_key=keys.run)
        return [
            r for r in found
            if r.tags.get(keys.run)
            and not (r.kind == "s3-bucket" and r.id == exclude_bucket)
        ]

    def run_records(self, bucket: str, region: str, prefix: str) -> list[str]:
        """Every run record under the prefix; a record goes only when its run's destroy succeeds."""
        return cloud_sweep.list_run_record_keys(bucket, region, prefix)

    def put_record(self, bucket: str, region: str, key: str, body: str) -> None:
        with tempfile.TemporaryDirectory(prefix="dfe-run-record-") as scratch:
            path = private_file.write_private(Path(scratch) / "run.json", body)
            self._read(["s3api", "put-object", "--bucket", bucket, "--key", key, "--body", str(path)], region)

    def delete_record(self, bucket: str, region: str, key: str) -> None:
        self._read(["s3api", "delete-object", "--bucket", bucket, "--key", key], region)

    def sweep(self, args: list[str]) -> int:
        """cloud_sweep.py's exit code for `args`, run in this process; 0 when nothing is left.

        Raises:
            CloudSweepError: A listing failed, so what is left cannot be known.
        """
        try:
            return cloud_sweep.main(args)
        finally:
            # A pipe holds the sweep's stdout until exit, behind every stderr line written after it.
            sys.stdout.flush()

    def write_kubeconfig(self, cluster: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        cloud_sweep.run_aws_text(
            ["eks", "update-kubeconfig", "--name", cluster, "--kubeconfig", str(path)], self.region
        )


@dataclass
class _UnbuiltGuard:
    """A cloud with the guard's interface and nothing behind it, which refuses by name."""

    region: str
    name: str = "unbuilt"

    def __getattr__(self, attribute: str) -> NoReturn:
        raise NotImplementedError(
            f"the cloud guard has no {self.name} checks yet: refusing rather than starting an "
            f"unguarded {self.name} run. Add them beside AwsGuard."
        )


@dataclass
class GcpGuard(_UnbuiltGuard):
    name: str = "gcp"


@dataclass
class AzureGuard(_UnbuiltGuard):
    name: str = "azure"


GUARDS = {"aws": AwsGuard, "gcp": GcpGuard, "azure": AzureGuard}


# --- preflight -----------------------------------------------------------------


@dataclass
class Check:
    """One preflight verdict, printed as PASS or FAIL with its evidence."""

    name: str
    ok: bool
    detail: str


@dataclass
class PreflightReport:
    """Every check the preflight ran; the run starts only when all passed."""

    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, ok, detail))

    def print(self) -> None:
        for check in self.checks:
            print(f"{'PASS' if check.ok else 'FAIL'}  {check.name}: {check.detail}", file=sys.stderr)


def _identifier_check(report: PreflightReport, name: str, identifier: str, env_name: str, read) -> None:
    if not identifier:
        report.add(name, False, f"not configured: pass the flag or set {env_name}")
        return
    try:
        report.add(name, True, f"{identifier} -> {read(identifier)}")
    except (cloud_sweep.CloudSweepError, GuardError, KeyError) as exc:
        report.add(name, False, f"{identifier} could not be read: {exc}")


def run_preflight(config: GuardConfig, guard, *, now: float, env: Mapping[str, str]) -> PreflightReport:
    """Run every read-only check; a check that cannot run is a FAIL, never a skip."""
    report = PreflightReport()

    try:
        actual = guard.caller_account()
        report.add("account", actual == config.account, f"authenticated {actual or 'unknown'}, dial {config.account}")
    except cloud_sweep.CloudSweepError as exc:
        report.add("account", False, f"could not read the caller identity: {exc}")

    # (a) the guardrails this account is meant to carry, each read by its identifier.
    _identifier_check(report, "(a) guardrail role", config.role, "DFE_GUARD_ROLE", guard.role)
    _identifier_check(report, "(a) sweeper", config.sweeper, "DFE_GUARD_SWEEPER", guard.sweeper)
    if report.checks[-1].ok and report.checks[-1].detail.endswith("-> Failed"):
        report.checks[-1] = Check("(a) sweeper", False, f"{config.sweeper} is in state Failed")
    _identifier_check(
        report,
        "(a) budget action",
        config.budget_action,
        "DFE_GUARD_BUDGET_ACTION",
        lambda ident: guard.budget_action(config.account, ident),
    )
    if config.permissions_boundary:
        _identifier_check(
            report, "(a) permissions boundary", config.permissions_boundary, "DFE_GUARD_PERMISSIONS_BOUNDARY",
            guard.policy,
        )

    # (b) the credential has to outlive the run and its teardown.
    needed_until = now + config.window
    try:
        expires, source = guard.credential_expiry(env)
        if expires is None and env.get("AWS_SESSION_TOKEN"):
            # A session credential always expires, so one whose expiry is unread would end the run unannounced.
            report.add(
                "(b) credential",
                False,
                "AWS_SESSION_TOKEN is set and no expiry is known: set AWS_CREDENTIAL_EXPIRATION to the "
                "credential's expiry",
            )
        elif expires is None:
            report.add("(b) credential", True, f"{source}")
        else:
            left = int(expires - now)
            report.add(
                "(b) credential",
                expires >= needed_until,
                f"expires in {left}s ({source}); the run and its teardown need {config.window}s",
            )
    except GuardError as exc:
        report.add("(b) credential", False, str(exc))

    _run_resource_checks(report, config, guard, now)

    # (e) the run's children find every tool the deploy and its teardown run.
    report.add("(e) deploy tools", *deploy_tools(config.provider, run_env(config)))
    return report


def deploy_tools(provider: str, env: Mapping[str, str]) -> tuple[bool, str]:
    """Whether bootstrap/check_tools.py finds every tool on `env`'s PATH, and what it said.

    It runs as a child with the environment the run's own children get, so it
    searches the PATH bootstrap.sh and destroy.sh will.

    Args:
        provider: The run's cloud, which adds its own CLI to the list.
        env: The environment the deploy runs in.

    Returns:
        (True, the tools found) when every one is there, else (False, each
        missing tool with what needs it).
    """
    try:
        done = subprocess.run(
            [sys.executable, str(CHECK_TOOLS), "--cloud", provider],
            env=dict(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=CHECK_TOOLS_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{CHECK_TOOLS.name} could not run: {exc}"
    if done.returncode == 0:
        return True, done.stdout.strip()
    # check_tools.py exits 1 with one indented line per missing tool under a heading.
    missing = [line.strip() for line in done.stderr.splitlines() if line.startswith("  ")]
    if done.returncode != 1 or not missing:
        return False, f"{CHECK_TOOLS.name} exited {done.returncode}: {done.stderr.strip()}"
    return False, f"not on PATH: {'; '.join(missing)}. Install them on this machine before a run"


def _run_resource_checks(report: PreflightReport, config: GuardConfig, guard, now: float) -> None:
    """(c) and (d): read the region's run-tagged resources once and split them by expiry."""
    try:
        found = guard.run_resources(config.keys, config.state_bucket)
    except cloud_sweep.CloudSweepError as exc:
        report.add("(c) expired run resources", False, f"could not list: {exc}")
        report.add("(d) unfinished runs", False, f"could not list the region's run resources: {exc}")
        return
    expiry = cloud_sweep.ExpirySelection(keys=config.keys, now=now, grace=0)
    classified = [(r, expiry.state(r)) for r in found]

    # (c) nothing a previous run left behind is still there past its expiry.
    expired = [r for r, state in classified if state is cloud_run.ExpiryState.EXPIRED]
    if expired:
        command = (
            f"python3 scripts/cloud_sweep.py --provider {config.provider} --region {config.region} "
            f"--expired --grace 0 --delete --account {config.account} --exclude-bucket {config.state_bucket}"
        )
        detail = f"{_resource_names(expired)}; remove them with: {command}"
        report.add("(c) expired run resources", False, detail)
    else:
        report.add("(c) expired run resources", True, "none in this region")

    # (d) no other run is still up, since an apply sharing its names would stop on AlreadyExists after
    # paying for part of a deployment. A run tag with no readable expiry is never swept, so it counts here.
    live = [
        (r, f"until {r.tags.get(config.keys.expiry)}" if state is cloud_run.ExpiryState.LIVE else str(state))
        for r, state in classified
        if state not in (cloud_run.ExpiryState.EXPIRED, cloud_run.ExpiryState.UNTAGGED)
    ]
    try:
        records = guard.run_records(config.state_bucket, config.state_region, config.state_prefix)
    except cloud_sweep.CloudSweepError as exc:
        report.add("(d) unfinished runs", False, f"could not list the run records: {exc}")
        return
    held = [f"run record {key}" for key in records]
    if live:
        held.append(_resource_names([r for r, _ in live], [note for _, note in live]))
    if held:
        report.add(
            "(d) unfinished runs",
            False,
            f"{'; '.join(held)}. A run whose destroy has not succeeded still holds them: wait for it, run "
            "`tofu destroy` against its state, or let the reaper remove it once it expires",
        )
    else:
        clear = f"no run record under {config.state_prefix}/, no live run resources"
        report.add("(d) unfinished runs", True, clear)


def _resource_names(resources: list[cloud_sweep.Resource], notes: list[str] | None = None) -> str:
    """The first ten resources by kind and name, each with its note when given, and how many more."""
    shown = [
        f"{r.kind} {r.name}" + (f" ({notes[i]})" if notes else "") for i, r in enumerate(resources[:10])
    ]
    return ", ".join(shown) + (f" and {len(resources) - 10} more" if len(resources) > 10 else "")


# --- the run ---------------------------------------------------------------------


def build_overlay(config: GuardConfig, run_id: str, expires_at: int) -> dict[str, object]:
    """The run's tfvars overlay: its own state key, its tags, the guardrail inputs, CloudTrail off.

    Given an endpoint CIDR it also replaces the dial's endpoint and makes the
    runner one of the gateway's self ranges, for this run alone.
    """
    overlay: dict[str, object] = {
        "run": cloud_run.run_tfvar(run_id, expires_at, config.keys),
        "state": {
            "bucket": config.state_bucket,
            "region": config.state_region,
            "key": cloud_run.state_key(config.state_prefix, run_id),
        },
        "cloudtrail": {"enabled": False},
    }
    if config.permissions_boundary:
        overlay["permissions_boundary"] = config.permissions_boundary
    if config.iam_path:
        overlay["iam_path"] = config.iam_path
    if config.s3_bucket_prefix:
        overlay["s3_bucket_prefix"] = config.s3_bucket_prefix
    if config.inspector_exclusion:
        overlay["inspector_ec2_exclusion"] = True
    if config.endpoint_cidr:
        overlay["endpoint"] = {"public": True, "allowed_cidrs": [config.endpoint_cidr]}
        overlay["edge_self_ranges"] = [config.endpoint_cidr]
    return overlay


def endpoint_summary(config: GuardConfig) -> str:
    """Who can reach the run's Kubernetes API, in one line for the run's log."""
    if config.endpoint_cidr:
        return f"Kubernetes API: private, and public to {config.endpoint_cidr} alone for this run"
    endpoint = config.tfvars.get("endpoint")
    if isinstance(endpoint, dict) and endpoint.get("public"):
        allowed = ", ".join(str(c) for c in endpoint.get("allowed_cidrs") or [])
        return f"Kubernetes API: private, and public to {allowed} as the dial sets"
    return "Kubernetes API: private only, so only a machine inside the VPC can reach it"


def run_env(config: GuardConfig, **extra: str) -> dict[str, str]:
    """The environment every child of a run gets: the shell's, with the run's region pinned.

    A call that names no region goes wherever AWS_REGION or AWS_DEFAULT_REGION
    points, which in an operator's shell can be outside the run's region.
    """
    return {**os.environ, "AWS_REGION": config.region, "AWS_DEFAULT_REGION": config.region, **extra}


def clickhouse_external(tfvars: Mapping[str, object]) -> bool:
    """Whether the dial points the run at an external ClickHouse, whose access list it opens."""
    clickhouse = tfvars.get("clickhouse")
    return isinstance(clickhouse, Mapping) and clickhouse.get("mode") == "external"


def clickhouse_access_missing(tfvars: Mapping[str, object], env: Mapping[str, str]) -> list[str]:
    """The settings an external-ClickHouse run needs that `env` leaves unset or blank; empty otherwise."""
    if not clickhouse_external(tfvars):
        return []
    return [name for name in CLICKHOUSE_ACCESS_ENV if not env.get(name, "").strip()]


def require_clickhouse_access(tfvars: Mapping[str, object], env: Mapping[str, str]) -> None:
    """Refuse an external-ClickHouse dial whose run settings `env` leaves unset.

    Raises:
        GuardError: The dial's clickhouse.mode is external and `env` leaves any of
            CLICKHOUSE_ACCESS_ENV unset or blank.
    """
    missing = clickhouse_access_missing(tfvars, env)
    if missing:
        raise GuardError(
            f"clickhouse.mode is external, and the environment does not set {', '.join(missing)}: the run "
            "seeds the server's password and opens its access list with them"
        )


def clickhouse_access_command(verb: str, run_id: str, *extra: str) -> list[str]:
    """clickhouse_cloud_access.py for one run; its addresses travel in the environment, never here."""
    return [sys.executable, str(CLICKHOUSE_ACCESS), verb, "--run-id", run_id, *extra]


def run_sweep_args(config: GuardConfig, run_id: str) -> list[str]:
    """cloud_sweep.py arguments that delete what carries this run's tag, in its region and account.

    The filter is the run key AND this run's id, so another run's resources and
    anything without the run tag are never selected, and the state bucket is
    excluded by name. --account makes the sweep check the live session first.
    """
    selector = f"{config.keys.run}={cloud_run.validate_run_id(run_id)}"
    return [
        "--provider", config.provider, "--region", config.region, "--tag-filter", selector,
        "--delete", "--account", config.account, "--exclude-bucket", config.state_bucket,
    ]


def _group_alive(pgid: int) -> bool:
    """Whether any process is left in the group; one that cannot be signalled still counts."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Runner:
    """Runs one child at a time with live output, and can stop the one in flight."""

    def __init__(self) -> None:
        self.current: subprocess.Popen | None = None
        # The process group `current` leads, when it was started in a session of its own.
        self.group: int | None = None

    def run(
        self,
        cmd: list[str],
        *,
        env: Mapping[str, str] | None = None,
        new_session: bool = False,
        deadline: float | None = None,
        stop_timeout: float = CHILD_STOP_TIMEOUT,
    ) -> int:
        """Run `cmd` to its end, or stop it at `deadline`, a `time.monotonic()` reading.

        Args:
            cmd: The child's argv.
            env: Its environment, or None for this process's own.
            new_session: Start it in a session of its own, so a terminal's Ctrl-C
                never reaches it and a stop signals its whole tree.
            deadline: When to stop it; None waits for as long as it runs.
            stop_timeout: Seconds a child stopped at the deadline gets before it is killed.

        Returns:
            The child's exit code.

        Raises:
            DeadlineError: The child was still running at `deadline` and has been stopped.
        """
        print(f"==> {' '.join(cmd)}", file=sys.stderr)
        proc = subprocess.Popen(cmd, env=None if env is None else dict(env), start_new_session=new_session)
        self.current = proc
        self.group = proc.pid if new_session and hasattr(os, "killpg") else None
        wait_for = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            returncode = proc.wait(timeout=wait_for)
        except subprocess.TimeoutExpired:
            print(f"==> deadline reached, stopping: {' '.join(cmd)}", file=sys.stderr)
            self.stop_current(stop_timeout)
            raise DeadlineError(f"{' '.join(cmd[:3])} ran past its deadline and was stopped") from None
        # Cleared only after a normal wait, so an interrupted wait leaves it for stop_current.
        self.current = None
        self.group = None
        return returncode

    def stop_current(self, timeout: float = CHILD_STOP_TIMEOUT) -> None:
        """Stop the child in flight, and every process in its group when it leads one.

        A child in a session of its own is waited on until its whole group is
        gone, so a grandchild still shutting down cannot outlive the stop and
        race whatever runs next. The child stays `current` until then, so a stop
        cut short by a signal is finished by the teardown's own stop.
        """
        proc, pgid = self.current, self.group
        if proc is None:
            return
        if pgid is None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        elif proc.poll() is None or _group_alive(pgid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGTERM)
            give_up = time.monotonic() + timeout
            while time.monotonic() < give_up and (proc.poll() is None or _group_alive(pgid)):
                time.sleep(0.2)
            if proc.poll() is None or _group_alive(pgid):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)
            proc.wait()
        self.current = None
        self.group = None


def _raise_interrupted(signum: int, _frame: object) -> NoReturn:
    raise _Interrupted(signal.Signals(signum).name)


@dataclass
class Teardown:
    """What every exit from a guarded run does once the run may have created anything."""

    config: GuardConfig
    guard: object
    runner: Runner
    run_id: str
    kubeconfig: Path
    record_key: str
    provisioning_started: bool = False
    cycle_finished: bool = False
    clickhouse_opened: bool = False
    result: int | None = None

    def run(self) -> int:
        self.runner.stop_current()
        overlay = self.config.tf_dir / OVERLAY_NAME
        if not self.provisioning_started:
            # Nothing reached tofu, so there is no state to destroy, only the overlay and record to remove.
            overlay.unlink(missing_ok=True)
            with contextlib.suppress(cloud_sweep.CloudSweepError):
                self.guard.delete_record(self.config.state_bucket, self.config.state_region, self.record_key)
            self.result = 0
            return 0
        access_left = self.close_clickhouse_access()
        result = self.destroy()
        if access_left and result == 0:
            result = 1
        self.result = result
        return result

    def close_clickhouse_access(self) -> bool:
        """Remove the run's access-list entries; True when they may still stand."""
        if not self.clickhouse_opened:
            return False
        try:
            closed = self.runner.run(
                clickhouse_access_command("close", self.run_id),
                env=run_env(self.config),
                new_session=True,
                deadline=time.monotonic() + CLICKHOUSE_CLOSE_TIMEOUT,
            )
        except DeadlineError:
            closed = 1
        if closed == 0:
            return False
        print(
            f"ACCESS LIST NOT CLOSED for run {self.run_id}: its entries on the ClickHouse Cloud service stay "
            "until the reaper's sweep removes them once the run expires. Remove them now with: "
            f"python3 scripts/clickhouse_cloud_access.py close --run-id {self.run_id}",
            file=sys.stderr,
        )
        return True

    def destroy(self) -> int:
        """The workloads, then `tofu destroy`, then the run's own leftovers; the run's exit code.

        A failed destroy is run once more after the run's leaked network interfaces go.
        """
        overlay = self.config.tf_dir / OVERLAY_NAME
        if not self.cycle_finished and self.kubeconfig.is_file():
            # Controllers own load balancers and volumes tofu cannot see, so the workloads go first.
            env = run_env(self.config, KUBECONFIG=str(self.kubeconfig))
            budget = self.config.teardown_margin * WORKLOAD_TEARDOWN_SHARE
            try:
                self.runner.run(
                    [sys.executable, str(DFE_OPS), "teardown", "--force",
                     "--from-terraform", str(self.config.tf_dir)],
                    env=env,
                    new_session=True,
                    deadline=time.monotonic() + budget,
                )
            except DeadlineError:
                print(
                    f"run {self.run_id}: the workload teardown passed its {int(budget)}s share of the "
                    "teardown margin and was stopped, so tofu destroy runs now",
                    file=sys.stderr,
                )
        destroyed = self.tofu_destroy()
        if destroyed != 0:
            self.sweep_leaked_interfaces()
            destroyed = self.tofu_destroy()
        if destroyed != 0:
            print(
                f"TEARDOWN FAILED for run {self.run_id}: its run record and expiry tags stay, so the "
                "reaper destroys it once it expires. Re-run `tofu destroy` in "
                f"{self.config.tf_dir} to finish it now.",
                file=sys.stderr,
            )
            return destroyed
        overlay.unlink(missing_ok=True)
        try:
            self.guard.delete_record(self.config.state_bucket, self.config.state_region, self.record_key)
        except cloud_sweep.CloudSweepError as exc:
            print(f"run {self.run_id} destroyed; its run record stays: {exc}", file=sys.stderr)
        return self.sweep_leftovers()

    def tofu_destroy(self) -> int:
        """`tofu destroy` of the run's state, in a session of its own; its exit code."""
        return self.runner.run(
            [shutil.which("tofu") or "tofu", f"-chdir={self.config.tf_dir}", "destroy", "-auto-approve",
             "-input=false"],
            env=run_env(self.config),
            new_session=True,
        )

    def sweep_leaked_interfaces(self) -> None:
        """Delete the network interfaces that carry this run's tag, before the destroy runs again.

        The VPC CNI allocates them outside tofu, and one whose node terminated
        during the destroy stays behind holding its subnet. Interfaces are the one
        kind swept while state remains, and a refresh drops any tofu itself owned
        from that state. One EC2 will not delete, an attached one say, is named
        in the sweep's output and left, and the destroy runs again regardless.
        """
        args = [*run_sweep_args(self.config, self.run_id), "--kind", "eni", "--yes"]
        selector = args[args.index("--tag-filter") + 1]
        print(
            f"==> run {self.run_id}: tofu destroy failed; deleting the network interfaces that carry "
            f"{selector}",
            file=sys.stderr,
        )
        try:
            left = self.guard.sweep(args)
        except cloud_sweep.CloudSweepError as exc:
            print(f"run {self.run_id}: the network interface sweep could not finish: {exc}", file=sys.stderr)
        else:
            if left:
                print(
                    f"run {self.run_id}: network interfaces carrying {selector} are still there, named above",
                    file=sys.stderr,
                )
        print(f"==> run {self.run_id}: tofu destroy, once more", file=sys.stderr)

    def sweep_leftovers(self) -> int:
        """Delete what still carries this run's tag once its state is destroyed; 1 when any is left.

        Controllers create resources tofu never recorded, and they carry the run's
        tags, so the next run's preflight refuses on them until they go.
        """
        args = run_sweep_args(self.config, self.run_id)
        selector = args[args.index("--tag-filter") + 1]
        print(f"==> run {self.run_id}: deleting what still carries {selector}", file=sys.stderr)
        try:
            left = self.guard.sweep([*args, "--yes"])
            why = "the sweep's output above names it"
        except cloud_sweep.CloudSweepError as exc:
            left, why = 1, f"the sweep could not finish: {exc}"
        if left == 0:
            return 0
        print(
            f"TEARDOWN INCOMPLETE for run {self.run_id}: tofu destroy succeeded, but not everything "
            f"carrying {selector} is gone ({why}). The next run's preflight refuses while it stands, "
            "and the reaper removes it once the run expires. Remove it now with: "
            f"python3 scripts/cloud_sweep.py {' '.join(args)}",
            file=sys.stderr,
        )
        return 1


@contextlib.contextmanager
def teardown_on_exit(teardown: Teardown) -> Iterator[None]:
    """Run `teardown` however the block exits; a termination signal becomes an exception first.

    SIGINT and the termination signals are ignored while the teardown itself
    runs, and its children start in a session of their own, so a second Ctrl-C
    cannot cut a destroy short.
    """
    previous = {sig: signal.signal(sig, _raise_interrupted) for sig in TEARDOWN_SIGNALS}
    try:
        yield
    finally:
        for sig in (signal.SIGINT, *TEARDOWN_SIGNALS):
            signal.signal(sig, signal.SIG_IGN)
        try:
            teardown.run()
        finally:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def _tofu_output(tf_dir: Path, name: str) -> str:
    result = subprocess.run(
        [shutil.which("tofu") or "tofu", f"-chdir={tf_dir}", "output", "-raw", name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise GuardError(f"tofu output {name} failed: {result.stderr.strip()}")
    return result.stdout.strip()


@functools.cache
def _dfe_ops() -> ModuleType:
    """The dfe-ops CLI as a module, which has no .py name to import it by."""
    loader = importlib.machinery.SourceFileLoader("dfe_ops_cli", str(DFE_OPS))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def _parse_cycle(argv: list[str], **defaults: object) -> argparse.Namespace:
    """Parse with `dfe-ops cycle`'s own parser, so the guard refuses what the cycle would.

    Raises:
        GuardError: The cycle's parser rejects the arguments.
    """
    parser = _dfe_ops().build_parser()
    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    cycle = subparsers.choices["cycle"]
    cycle.exit_on_error = False
    cycle.set_defaults(**defaults)
    try:
        return cycle.parse_args(argv)
    except argparse.ArgumentError as exc:
        raise GuardError(f"dfe-ops cycle would refuse the cycle arguments: {exc}") from exc
    except SystemExit as exc:
        raise GuardError("dfe-ops cycle would exit on the cycle arguments without running (-h?)") from exc


def cycle_command(raw: list[str], profile: str, tf_dir: Path, kubeconfig: Path) -> list[str]:
    """The arguments `dfe-ops cycle` runs with, checked by its own parser before anything is created.

    The guard supplies --from-terraform, --kubeconfig and, when the caller names
    none, --mode from the dial's profile.

    Raises:
        GuardError: The cycle's parser rejects them, they set --from-terraform,
            --kubeconfig or --keep, the dial names no profile, or the mode is not
            the dial's profile.
    """
    args = raw[1:] if raw[:1] == ["--"] else raw
    if not profile:
        raise GuardError("the root's tfvars name no profile, so the cycle has no mode: render the dial with "
                         "render_dial.py --tofu")
    base = ["--from-terraform", str(tf_dir), "--kubeconfig", str(kubeconfig)]
    # No defaults, so what comes back set is what the caller set.
    given = _parse_cycle(args, mode=None, kubeconfig=None, from_terraform=None)
    clashing = [flag for flag, value in (("--from-terraform", given.from_terraform),
                                         ("--kubeconfig", given.kubeconfig)) if value is not None]
    if clashing:
        raise GuardError(f"cloud-cycle sets {' and '.join(clashing)} itself; drop them from the cycle arguments")
    command = [*base, *args, *([] if given.mode is not None else ["--mode", profile])]
    parsed = _parse_cycle(command)
    if parsed.keep:
        raise GuardError("--keep skips the cycle's destroy, which an unattended cloud run cannot do without")
    if parsed.mode != profile:
        raise GuardError(
            f"--mode {parsed.mode!r} is not the dial's profile {profile!r}: one dial, one profile. Drop --mode "
            "from the cycle arguments, or change the dial"
        )
    return command


def require_edge_inputs(cycle_argv: list[str]) -> None:
    """Refuse a cycle running the edge suite without the address it sends from.

    The cycle refuses the same, but only after the apply has built the cluster.

    Raises:
        GuardError: The cycle asks for the edge suite and dfe-ops finds its ingest
            allow address unset, not one network or admitting every address,
            in the environment or the env files the cycle passes.
    """
    dfe_ops = _dfe_ops()
    parsed = _parse_cycle(cycle_argv)
    if parsed.acceptance_suite != dfe_ops.EDGE_SUITE:
        return
    problem = dfe_ops.edge_inputs_problem(parsed.env_file, os.environ)
    if problem:
        raise GuardError(f"the edge acceptance stage cannot run: {problem}")


def _make_guard(config: GuardConfig):
    return GUARDS[config.provider](config.region)


def cmd_cloud_preflight(args: argparse.Namespace) -> int:
    """`dfe-ops cloud-preflight`: read-only, exit 2 on any refusal."""
    try:
        config = resolve_config(args, os.environ)
        require_clickhouse_access(config.tfvars, os.environ)
        report = run_preflight(config, _make_guard(config), now=time.time(), env=os.environ)
    except (GuardError, NotImplementedError) as exc:
        print(f"cloud-preflight refused: {exc}", file=sys.stderr)
        return 2
    report.print()
    print(endpoint_summary(config), file=sys.stderr)
    if not report.ok:
        print("cloud-preflight REFUSED: fix every FAIL above before an unattended run.", file=sys.stderr)
        return 2
    print("cloud-preflight passed.", file=sys.stderr)
    return 0


def cmd_cloud_cycle(args: argparse.Namespace) -> int:
    """`dfe-ops cloud-cycle`: preflight, then apply -> cycle -> destroy, torn down on every exit."""
    try:
        config = resolve_config(args, os.environ)
        now = time.time()
        run_id = cloud_run.validate_run_id(args.run_id) if args.run_id else cloud_run.new_run_id(now)
        kubeconfig = RUNS_DIR / run_id / "kubeconfig"
        profile = str(config.tfvars.get("profile") or "")
        cycle_argv = cycle_command(args.cycle_args, profile, config.tf_dir, kubeconfig)
        require_clickhouse_access(config.tfvars, os.environ)
        require_edge_inputs(cycle_argv)
        guard = _make_guard(config)
        report = run_preflight(config, guard, now=now, env=os.environ)
    except (GuardError, NotImplementedError, cloud_run.RunTagError) as exc:
        print(f"cloud-cycle refused: {exc}", file=sys.stderr)
        return 2
    report.print()
    print(endpoint_summary(config), file=sys.stderr)
    if not report.ok:
        print("cloud-cycle REFUSED: nothing was created.", file=sys.stderr)
        return 2

    expires_at = int(now) + config.window
    # Monotonic, so a clock step cannot move it, and net of the preflight already spent.
    deadline = time.monotonic() + config.run_length - (time.time() - now)
    overlay = build_overlay(config, run_id, expires_at)
    record = cloud_run.build_record(
        run_id=run_id,
        expires_at=expires_at,
        keys=config.keys,
        tf_root=config.tf_root,
        state=overlay["state"],  # type: ignore[arg-type]
        tfvars={**config.tfvars, **overlay},
    )
    record_key = cloud_run.record_key(config.state_prefix, run_id)
    runner = Runner()
    teardown = Teardown(config, guard, runner, run_id, kubeconfig, record_key)
    tofu = shutil.which("tofu") or "tofu"
    print(
        f"run {run_id}: the cycle stops by {cloud_run.format_expiry(int(now) + config.run_length)}, "
        f"and the run expires at {cloud_run.format_expiry(expires_at)}",
        file=sys.stderr,
    )

    # The runner is one of the gateway's self ranges, so its admin probe can tell a
    # dead door from a fenced one.
    probe_source = (
        {PROBE_SOURCE_ENV: config.endpoint_cidr.split("/")[0]} if config.endpoint_cidr else {}
    )
    child_env = run_env(config, **probe_source)
    returncode = 1
    try:
        with teardown_on_exit(teardown):
            private_file.write_private(config.tf_dir / OVERLAY_NAME, json.dumps(overlay, indent=2) + "\n")
            guard.put_record(config.state_bucket, config.state_region, record_key, json.dumps(record))
            teardown.provisioning_started = True
            returncode = runner.run(
                [tofu, f"-chdir={config.tf_dir}", "init", "-input=false", "-reconfigure"], env=child_env
            )
            if returncode == 0:
                returncode = runner.run(
                    [tofu, f"-chdir={config.tf_dir}", "apply", "-auto-approve", "-input=false"], env=child_env
                )
            if returncode == 0:
                guard.write_kubeconfig(_tofu_output(config.tf_dir, "cluster_name"), kubeconfig)
                if time.monotonic() >= deadline:
                    raise DeadlineError("tofu apply finished past it, so the cycle never started")
                if clickhouse_external(config.tfvars):
                    # Marked first, so an open cut short is still closed.
                    teardown.clickhouse_opened = True
                    addresses = [_tofu_output(config.tf_dir, "DFE_EGRESS_CIDRS"), config.endpoint_cidr]
                    returncode = runner.run(
                        clickhouse_access_command("open", run_id, "--expires-at", str(expires_at)),
                        env={**child_env, CLICKHOUSE_ACCESS_CIDRS_ENV: ",".join(a for a in addresses if a)},
                    )
                if returncode == 0:
                    returncode = runner.run(
                        [sys.executable, str(DFE_OPS), "cycle", *cycle_argv],
                        env=child_env,
                        new_session=True,
                        deadline=deadline,
                        stop_timeout=DEADLINE_STOP_TIMEOUT,
                    )
                    teardown.cycle_finished = True
    except DeadlineError as exc:
        print(f"run {run_id} reached its {config.run_length}s run length: {exc}; torn down above",
              file=sys.stderr)
        returncode = DEADLINE_EXIT
    except _Interrupted as exc:
        print(f"run {run_id} interrupted by {exc}; torn down above", file=sys.stderr)
        returncode = 143
    except KeyboardInterrupt:
        print(f"run {run_id} interrupted; torn down above", file=sys.stderr)
        returncode = 130
    except (GuardError, cloud_sweep.CloudSweepError) as exc:
        print(f"run {run_id} failed: {exc}; torn down above", file=sys.stderr)
        returncode = 1
    if teardown.result:
        return teardown.result
    return returncode


# --- parser ------------------------------------------------------------------


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tf-dir", required=True, help="the OpenTofu root the run applies, e.g. terraform/environments/aws")
    parser.add_argument("--provider", choices=sorted(GUARDS), default="aws",
                        help="cloud; gcp and azure have the interface and no checks yet, and refuse by name")
    parser.add_argument("--run-length", required=True,
                        help="how long apply and the cycle may take before the cycle is stopped and "
                             "the teardown starts: 90m, 3h")
    parser.add_argument("--teardown-margin", default=DEFAULT_TEARDOWN_MARGIN,
                        help=f"time allowed for the teardown after the run length (default {DEFAULT_TEARDOWN_MARGIN})")
    parser.add_argument("--account", default=None, help="account id the run must land in (default: provision.account)")
    parser.add_argument("--region", default=None, help="region of the run (default: provision.region)")
    parser.add_argument("--guard-role", default=None, help="the guardrail role's name or ARN (env DFE_GUARD_ROLE)")
    parser.add_argument("--guard-sweeper", default=None,
                        help="the in-cloud sweeper function's name or ARN (env DFE_GUARD_SWEEPER)")
    parser.add_argument("--guard-budget-action", default=None,
                        help="<budget-name>:<action-id> of the deny-create budget action (env DFE_GUARD_BUDGET_ACTION)")
    parser.add_argument("--permissions-boundary", default=None,
                        help="policy ARN set as every run role's boundary (env DFE_GUARD_PERMISSIONS_BOUNDARY)")
    parser.add_argument("--iam-path", default=None, help="IAM path for every run role (env DFE_GUARD_IAM_PATH)")
    parser.add_argument("--s3-bucket-prefix", default=None,
                        help="prefix on every run bucket name (env DFE_GUARD_S3_BUCKET_PREFIX)")
    parser.add_argument("--inspector-exclusion", action="store_true",
                        help="tag run instances InspectorEc2Exclusion (env DFE_GUARD_INSPECTOR_EXCLUSION=true)")
    parser.add_argument("--state-prefix", default=None,
                        help=f"key prefix for per-run state (env {cloud_run.STATE_PREFIX_ENV}, "
                             f"default {cloud_run.DEFAULT_STATE_PREFIX})")
    parser.add_argument("--endpoint-cidr", default=None,
                        help="open the Kubernetes API's public endpoint to this one public IPv4 "
                             "address, <address>/32, in place of the dial's. The private endpoint "
                             f"stays on (env {ENDPOINT_CIDR_ENV})")


def add_cloud_guard_subparsers(sub: argparse._SubParsersAction) -> None:
    """Register `dfe-ops cloud-preflight` and `dfe-ops cloud-cycle`."""
    pf = sub.add_parser(
        "cloud-preflight",
        help="READ-ONLY: refuse an unattended cloud run without its guardrails, credential time or a clean region",
    )
    _common(pf)
    pf.set_defaults(func=cmd_cloud_preflight)

    cc = sub.add_parser(
        "cloud-cycle",
        help="the guarded cloud cycle: preflight, per-run state and tags, apply, cycle, destroy on every exit",
    )
    _common(cc)
    cc.add_argument("--run-id", default=None, help="use this run id instead of minting one")
    cc.add_argument("cycle_args", nargs=argparse.REMAINDER,
                    help="after --: arguments for `dfe-ops cycle` (not --keep, --from-terraform or --kubeconfig). "
                         "--mode is the dial's profile, added when absent and refused when it names another")
    cc.set_defaults(func=cmd_cloud_cycle)
