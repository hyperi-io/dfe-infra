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
        -- --mode single --env-file bootstrap/.env

cloud-preflight reads and never writes. It REFUSES (exit 2) when:

  (a) the account guardrails it is told to expect cannot be read: the guardrail
      role, the in-cloud sweeper and the budget action (and the permissions
      boundary, when one is configured), each a configured identifier;
  (b) the active credential expires before now + run length + teardown margin,
      so the run could lose the identity its own teardown needs;
  (c) run-tagged resources whose expiry has already passed exist in the region
      -- a previous run left them, and it prints the cloud_sweep command that
      removes them.

It also refuses a dial whose tags.lifecycle is not ephemeral, because a
persistent deployment's deletion protection would stop the teardown short.

cloud-cycle runs the same preflight, then mints a run id and an expiry
(now + run length + teardown margin) and writes `zz-dfe-run.auto.tfvars.json`
into the root. That overlay gives the run its own state key
(`<prefix>/<run id>/terraform.tfstate`), the run's id and expiry as tags on
everything it creates, the guardrail inputs (boundary, IAM path, bucket
prefix) and CloudTrail off. A run record goes beside the state BEFORE the
apply, so the scheduled reaper can destroy the run if this process dies.
From then on every way out -- success, failure, an exception, SIGINT, SIGTERM,
SIGHUP -- tears the run down: the in-flight child is stopped, the cluster's
workloads go if the cycle did not reach its own destroy, then `tofu destroy`.
Only a destroy that succeeds removes the overlay and the run record; a failed
one leaves both, and the run's tags, for the reaper. SIGKILL skips all of
this, which is what the reaper is for.

Every value is a flag or an environment variable: DFE_GUARD_ROLE,
DFE_GUARD_SWEEPER, DFE_GUARD_BUDGET_ACTION (`<budget-name>:<action-id>`),
DFE_GUARD_PERMISSIONS_BOUNDARY, DFE_GUARD_IAM_PATH, DFE_GUARD_S3_BUCKET_PREFIX,
DFE_GUARD_INSPECTOR_EXCLUSION, DFE_RUN_STATE_PREFIX. The account, region and
state bucket come from the root's own JSON tfvars.
"""

import argparse
import contextlib
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
from typing import NoReturn

import aws_cli
import cloud_run
import cloud_sweep
import private_file

REPO_ROOT = Path(__file__).resolve().parent.parent
DFE_OPS = REPO_ROOT / "scripts" / "dfe-ops"
RUNS_DIR = REPO_ROOT / ".tmp" / "cloud-runs"

# Lexically last among the root's auto tfvars, so every value in it wins.
OVERLAY_NAME = "zz-dfe-run.auto.tfvars.json"
DEFAULT_TEARDOWN_MARGIN = "45m"
CHILD_STOP_TIMEOUT = 60  # seconds a stopped child gets to exit before it is killed
TEARDOWN_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)
)
# A cycle argument cloud-cycle sets itself, or one that would skip the destroy.
RESERVED_CYCLE_ARGS = ("--keep", "--from-terraform", "--kubeconfig")


class GuardError(RuntimeError):
    """The run cannot be configured or checked; the message is what dfe-ops prints."""


class _Interrupted(BaseException):
    """A termination signal, raised where the main thread is so `finally` tears down."""


# --- configuration -----------------------------------------------------------


def load_tfvars(tf_dir: Path) -> dict[str, object]:
    """The variables OpenTofu would load from this root, in its own order, later wins.

    Only JSON is read: an HCL tfvars file cannot be parsed with the stdlib, so
    its presence is refused by name rather than silently skipped. The run's own
    overlay is never read back, so a stale one cannot feed the next run.

    Raises:
        GuardError: An HCL tfvars file is present, or a JSON one does not parse.
    """
    candidates = [tf_dir / "terraform.tfvars", tf_dir / "terraform.tfvars.json"]
    candidates += sorted(
        (*tf_dir.glob("*.auto.tfvars"), *tf_dir.glob("*.auto.tfvars.json")), key=lambda p: p.name
    )
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
    return flag if flag is not None else env.get(name, default)


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
        answers; its output carries the secret, so only Expiration is kept and
        nothing it printed reaches an error message. None means a credential
        with no expiry.

        Raises:
            GuardError: The expiry cannot be read, so the check cannot pass.
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
            expiration = json.loads(result.stdout).get("Expiration")
        except (json.JSONDecodeError, AttributeError) as exc:
            raise GuardError("aws configure export-credentials returned no JSON object") from exc
        if not expiration:
            return None, "a credential with no expiry"
        try:
            return _parse_instant(str(expiration)), "aws configure export-credentials"
        except ValueError as exc:
            raise GuardError(f"the credential's Expiration is not an ISO-8601 instant: {exc}") from exc

    def expired_resources(self, keys: cloud_run.RunTagKeys, now: float, exclude_bucket: str) -> list:
        provider = cloud_sweep.AwsProvider(self.region)
        found = provider.collect({}, exclude_bucket, run_key=keys.run)
        expiry = cloud_sweep.ExpirySelection(keys=keys, now=now, grace=0)
        return cloud_sweep.filter_expired(found, expiry=expiry, exclude_bucket=exclude_bucket)

    def put_record(self, bucket: str, region: str, key: str, body: str) -> None:
        with tempfile.TemporaryDirectory(prefix="dfe-run-record-") as scratch:
            path = private_file.write_private(Path(scratch) / "run.json", body)
            self._read(["s3api", "put-object", "--bucket", bucket, "--key", key, "--body", str(path)], region)

    def delete_record(self, bucket: str, region: str, key: str) -> None:
        self._read(["s3api", "delete-object", "--bucket", bucket, "--key", key], region)

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
        if expires is None:
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

    # (c) nothing a previous run left behind is still there past its expiry.
    try:
        leftovers = guard.expired_resources(config.keys, now, config.state_bucket)
        if leftovers:
            names = ", ".join(f"{r.kind} {r.name}" for r in leftovers[:10])
            more = f" and {len(leftovers) - 10} more" if len(leftovers) > 10 else ""
            command = (
                f"python3 scripts/cloud_sweep.py --provider {config.provider} --region {config.region} "
                f"--expired --grace 0 --delete --account {config.account} --exclude-bucket {config.state_bucket}"
            )
            report.add("(c) expired run resources", False, f"{names}{more}; remove them with: {command}")
        else:
            report.add("(c) expired run resources", True, "none in this region")
    except cloud_sweep.CloudSweepError as exc:
        report.add("(c) expired run resources", False, f"could not list: {exc}")
    return report


# --- the run ---------------------------------------------------------------------


def build_overlay(config: GuardConfig, run_id: str, expires_at: int) -> dict[str, object]:
    """The run's tfvars overlay: its own state key, its tags, the guardrail inputs, CloudTrail off."""
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
    return overlay


class Runner:
    """Runs one child at a time with live output, and can stop the one in flight."""

    def __init__(self) -> None:
        self.current: subprocess.Popen | None = None

    def run(self, cmd: list[str], *, env: Mapping[str, str] | None = None, new_session: bool = False) -> int:
        print(f"==> {' '.join(cmd)}", file=sys.stderr)
        proc = subprocess.Popen(cmd, env=None if env is None else dict(env), start_new_session=new_session)
        self.current = proc
        returncode = proc.wait()
        # Cleared only after a normal wait, so an interrupted wait leaves it for stop_current.
        self.current = None
        return returncode

    def stop_current(self, timeout: float = CHILD_STOP_TIMEOUT) -> None:
        proc, self.current = self.current, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


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
    cycle_finished: bool = False
    result: int | None = None

    def run(self) -> int:
        self.runner.stop_current()
        tofu = shutil.which("tofu") or "tofu"
        if not self.cycle_finished and self.kubeconfig.is_file():
            # Controllers own load balancers and volumes tofu cannot see, so the workloads go first.
            env = {**os.environ, "KUBECONFIG": str(self.kubeconfig)}
            self.runner.run([sys.executable, str(DFE_OPS), "teardown", "--force"], env=env, new_session=True)
        destroyed = self.runner.run(
            [tofu, f"-chdir={self.config.tf_dir}", "destroy", "-auto-approve", "-input=false"], new_session=True
        )
        if destroyed == 0:
            (self.config.tf_dir / OVERLAY_NAME).unlink(missing_ok=True)
            try:
                self.guard.delete_record(self.config.state_bucket, self.config.state_region, self.record_key)
            except cloud_sweep.CloudSweepError as exc:
                print(f"run {self.run_id} destroyed; its run record stays: {exc}", file=sys.stderr)
        else:
            print(
                f"TEARDOWN FAILED for run {self.run_id}: its run record and expiry tags stay, so the "
                "reaper destroys it once it expires. Re-run `tofu destroy` in "
                f"{self.config.tf_dir} to finish it now.",
                file=sys.stderr,
            )
        self.result = destroyed
        return destroyed


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


def _cycle_args(raw: list[str]) -> list[str]:
    args = raw[1:] if raw[:1] == ["--"] else raw
    reserved = [a for a in args if a.split("=", 1)[0] in RESERVED_CYCLE_ARGS]
    if reserved:
        raise GuardError(f"cloud-cycle sets or forbids {', '.join(reserved)} itself; drop them from the cycle arguments")
    return args


def _make_guard(config: GuardConfig):
    return GUARDS[config.provider](config.region)


def cmd_cloud_preflight(args: argparse.Namespace) -> int:
    """`dfe-ops cloud-preflight`: read-only, exit 2 on any refusal."""
    try:
        config = resolve_config(args, os.environ)
        report = run_preflight(config, _make_guard(config), now=time.time(), env=os.environ)
    except (GuardError, NotImplementedError) as exc:
        print(f"cloud-preflight refused: {exc}", file=sys.stderr)
        return 2
    report.print()
    if not report.ok:
        print("cloud-preflight REFUSED: fix every FAIL above before an unattended run.", file=sys.stderr)
        return 2
    print("cloud-preflight passed.", file=sys.stderr)
    return 0


def cmd_cloud_cycle(args: argparse.Namespace) -> int:
    """`dfe-ops cloud-cycle`: preflight, then apply -> cycle -> destroy, torn down on every exit."""
    try:
        config = resolve_config(args, os.environ)
        cycle_args = _cycle_args(args.cycle_args)
        guard = _make_guard(config)
        now = time.time()
        report = run_preflight(config, guard, now=now, env=os.environ)
    except (GuardError, NotImplementedError) as exc:
        print(f"cloud-cycle refused: {exc}", file=sys.stderr)
        return 2
    report.print()
    if not report.ok:
        print("cloud-cycle REFUSED: nothing was created.", file=sys.stderr)
        return 2

    try:
        run_id = cloud_run.validate_run_id(args.run_id) if args.run_id else cloud_run.new_run_id(now)
    except cloud_run.RunTagError as exc:
        print(f"cloud-cycle refused: {exc}", file=sys.stderr)
        return 2
    expires_at = int(now) + config.window
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
    kubeconfig = RUNS_DIR / run_id / "kubeconfig"
    runner = Runner()
    teardown = Teardown(config, guard, runner, run_id, kubeconfig, record_key)
    tofu = shutil.which("tofu") or "tofu"
    print(f"run {run_id}: expires at {cloud_run.format_expiry(expires_at)}", file=sys.stderr)

    returncode = 1
    try:
        with teardown_on_exit(teardown):
            private_file.write_private(config.tf_dir / OVERLAY_NAME, json.dumps(overlay, indent=2) + "\n")
            guard.put_record(config.state_bucket, config.state_region, record_key, json.dumps(record))
            returncode = runner.run([tofu, f"-chdir={config.tf_dir}", "init", "-input=false", "-reconfigure"])
            if returncode == 0:
                returncode = runner.run([tofu, f"-chdir={config.tf_dir}", "apply", "-auto-approve", "-input=false"])
            if returncode == 0:
                guard.write_kubeconfig(_tofu_output(config.tf_dir, "cluster_name"), kubeconfig)
                returncode = runner.run(
                    [sys.executable, str(DFE_OPS), "cycle", "--from-terraform", str(config.tf_dir),
                     "--kubeconfig", str(kubeconfig), *cycle_args]
                )
                teardown.cycle_finished = True
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
    parser.add_argument("--run-length", required=True, help="how long the run may take: 90m, 3h")
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
                    help="after --: arguments for `dfe-ops cycle` (not --keep, --from-terraform or --kubeconfig)")
    cc.set_defaults(func=cmd_cloud_cycle)
