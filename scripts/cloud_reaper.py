#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/cloud_reaper.py
#  Purpose:      The scheduled reaper's logic, driven by environment variables
#                so the workflow that schedules it carries no value of its
#                own: destroy every expired run's per-run state, then sweep
#                every expired run-tagged resource left behind.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""cloud_reaper -- remove what an expired cloud test run left behind.

    python3 scripts/cloud_reaper.py plan    # validate, write account= and region= to $GITHUB_OUTPUT
    python3 scripts/cloud_reaper.py reap    # destroy expired runs, then sweep expired resources

Configuration is environment only:

    DFE_REAPER_AWS_ROLE       role ARN the workflow assumes by OIDC. Unset is a
                              notice and exit 0, so a fork or another
                              organisation's copy of this repository does nothing.
    DFE_REAPER_AWS_REGIONS    comma-separated regions to sweep; required with a role.
    DFE_REAPER_AWS_ACCOUNT    the account --delete must land in; defaults to the
                              account in the role ARN.
    DFE_REAPER_STATE_BUCKET   the bucket holding per-run state; unset skips the
                              tofu destroys and sweeps only.
    DFE_REAPER_STATE_REGION   that bucket's region; defaults to the first region.
    DFE_RUN_STATE_PREFIX      per-run state prefix (default dfe-e2e-runs).
    DFE_RUN_TAG_KEY / DFE_RUN_EXPIRY_KEY   the run convention's keys (cloud_run.py).
    CONFLUENT_CLOUD_API_KEY / CONFLUENT_CLOUD_API_SECRET   Confluent Cloud's
                              organisation credential, read by the confluent
                              provider; required to destroy a run whose
                              kafka.provider is confluent-cloud.
    REDPANDA_CLIENT_ID / REDPANDA_CLIENT_SECRET   Redpanda Cloud's client
                              credential, read by the redpanda provider;
                              required to destroy a run whose kafka.provider is
                              redpanda-cloud. Any other run gets a placeholder,
                              because the root configures that provider on
                              every destroy.

`reap` works in two passes, both with a grace of 0. First every run record
under the prefix: one that fails `cloud_run.record_problems` is reported and
left alone, an unexpired one is left alone, an expired one holding a SaaS broker
whose vendor credential is unset is reported and kept for the next schedule, and
any other expired one gets `tofu destroy` against its own per-run state, from a copy of this checkout's
terraform/ tree so no run sees another's overlay. A secret Secrets Manager no
longer holds is dropped from that state first, since the reaper's tag-scoped
read cannot refresh it. A successful destroy removes the record, and so does a
failed one after which a refresh finds nothing of the run left, since the sweep
can win the race for the last resource. Then `cloud_sweep.py --expired --grace 0 --delete` in every region,
with the state bucket excluded. Exit 0 means nothing expired remains; exit 1
names what does, so the scheduled run goes red; exit 2 is a configuration error.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import cloud_run
import cloud_sweep

REPO_ROOT = Path(__file__).resolve().parent.parent
TERRAFORM = REPO_ROOT / "terraform"
OVERLAY_NAME = "zz-dfe-run.auto.tfvars.json"
NOTICE_UNCONFIGURED = (
    "::notice::cloud reaper not configured for this repository (DFE_REAPER_AWS_ROLE is unset); nothing to do."
)
# The credential each SaaS broker's provider reads from the environment; the values come from the runner's secrets.
VENDOR_PROVIDER_ENV: dict[str, tuple[str, ...]] = {
    "confluent-cloud": ("CONFLUENT_CLOUD_API_KEY", "CONFLUENT_CLOUD_API_SECRET"),
    "redpanda-cloud": ("REDPANDA_CLIENT_ID", "REDPANDA_CLIENT_SECRET"),
}
# Providers a root configures whether or not the dial selects them; a placeholder satisfies their check.
PLACEHOLDER_PROVIDER_ENV = VENDOR_PROVIDER_ENV["redpanda-cloud"]
PLACEHOLDER_VALUE = "unused-placeholder"


class ReaperConfigError(ValueError):
    """The reaper is configured, but not well enough to act."""


@dataclass(frozen=True, slots=True)
class ReaperConfig:
    """What the reaper acts on, from the environment."""

    role: str
    account: str
    regions: tuple[str, ...]
    state_bucket: str
    state_region: str
    state_prefix: str
    keys: cloud_run.RunTagKeys


def load_config(env: Mapping[str, str]) -> ReaperConfig | None:
    """The reaper's configuration, or None when no role is set (the no-op case).

    Raises:
        ReaperConfigError: A role is set but the rest cannot be used.
    """
    role = env.get("DFE_REAPER_AWS_ROLE", "").strip()
    if not role:
        return None
    parts = role.split(":")
    if len(parts) < 6 or parts[2] != "iam" or not parts[5].startswith("role/"):
        raise ReaperConfigError(f"DFE_REAPER_AWS_ROLE {role!r} is not an IAM role ARN")
    account = env.get("DFE_REAPER_AWS_ACCOUNT", "").strip() or parts[4]
    if not (account.isdigit() and len(account) == 12):
        raise ReaperConfigError(f"account {account!r} is not a 12-digit AWS account id")
    regions = tuple(r.strip() for r in env.get("DFE_REAPER_AWS_REGIONS", "").split(",") if r.strip())
    if not regions:
        raise ReaperConfigError("DFE_REAPER_AWS_REGIONS names no region; set it beside DFE_REAPER_AWS_ROLE")
    try:
        keys = cloud_run.RunTagKeys.from_env(env)
        prefix = cloud_run.validate_state_prefix(
            env.get(cloud_run.STATE_PREFIX_ENV, "").strip() or cloud_run.DEFAULT_STATE_PREFIX
        )
    except cloud_run.RunTagError as exc:
        raise ReaperConfigError(str(exc)) from exc
    return ReaperConfig(
        role=role,
        account=account,
        regions=regions,
        state_bucket=env.get("DFE_REAPER_STATE_BUCKET", "").strip(),
        state_region=env.get("DFE_REAPER_STATE_REGION", "").strip() or regions[0],
        state_prefix=prefix,
        keys=keys,
    )


# --- per-run state ---------------------------------------------------------------


def read_record(config: ReaperConfig, key: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="dfe-reaper-") as scratch:
        out = Path(scratch) / "run.json"
        cloud_sweep.run_aws(
            ["s3api", "get-object", "--bucket", config.state_bucket, "--key", key, str(out)], config.state_region
        )
        return json.loads(out.read_text(encoding="utf-8"))


def state_exists(config: ReaperConfig, key: str) -> bool:
    try:
        cloud_sweep.run_aws(["s3api", "head-object", "--bucket", config.state_bucket, "--key", key], config.state_region)
    except cloud_sweep.CloudSweepError as exc:
        if "Not Found" in str(exc) or "404" in str(exc) or "NoSuchKey" in str(exc):
            return False
        raise
    return True


def delete_record(config: ReaperConfig, key: str) -> None:
    cloud_sweep.run_aws(["s3api", "delete-object", "--bucket", config.state_bucket, "--key", key], config.state_region)


def run_tofu(args: list[str], cwd: Path, env: Mapping[str, str]) -> int:
    """One tofu command, live output, in `cwd`."""
    print(f"==> tofu {' '.join(args)}  (in {cwd})", file=sys.stderr)
    return subprocess.run([shutil.which("tofu") or "tofu", *args], cwd=cwd, env=dict(env), check=False).returncode


def state_json(cwd: Path, env: Mapping[str, str]) -> dict:
    """`tofu show -json` of the initialised root, held in memory only: it carries secret values.

    Empty when tofu cannot answer, which leaves the destroy to report its own failure.
    """
    try:
        done = subprocess.run(
            [shutil.which("tofu") or "tofu", "show", "-json"], cwd=cwd, env=dict(env),
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    except OSError:
        return {}
    if done.returncode != 0:
        return {}
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        return {}


# The state's secret resources, and the attribute naming the secret each belongs to.
_SECRET_ARN_ATTRIBUTE = {"aws_secretsmanager_secret": "arn", "aws_secretsmanager_secret_version": "secret_id"}


def _state_resources(module: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Every resource in one `tofu show -json` module and the modules below it."""
    found = list(module.get("resources", []))  # type: ignore[arg-type]
    for child in module.get("child_modules", []):  # type: ignore[union-attr]
        found += _state_resources(child)
    return found


def _secret_gone(arn: str) -> bool:
    """True when Secrets Manager no longer has the secret, or has it scheduled for deletion."""
    region = arn.split(":")[3] if arn.count(":") >= 6 else ""
    if not region:
        return False
    try:
        body = cloud_sweep.run_aws(["secretsmanager", "describe-secret", "--secret-id", arn], region)
    except cloud_sweep.CloudSweepError as exc:
        if "ResourceNotFoundException" in str(exc):
            return True
        raise
    return bool(body.get("DeletedDate"))


def stale_state_addresses(state: Mapping[str, object]) -> list[str]:
    """State addresses of secrets, and their versions, that the account no longer holds.

    The reaper may read a secret's value only while the secret carries the run tag, so a
    secret removed outside tofu answers its refresh with AccessDenied rather than NotFound,
    and the provider keeps it in state: every destroy then fails on the same two reads.
    DescribeSecret is not tag-conditioned, so it tells the two cases apart.
    """
    root = (state.get("values") or {}).get("root_module") or {}  # type: ignore[union-attr]
    held = [r for r in _state_resources(root) if str(r.get("type")) in _SECRET_ARN_ATTRIBUTE]
    gone = {arn for arn in {_owning_arn(r) for r in held} if arn and _secret_gone(arn)}
    # The address is read off the resource itself, never off its values, which hold the secret.
    return [str(r["address"]) for r in held if _owning_arn(r) in gone]


def _owning_arn(resource: Mapping[str, object]) -> str:
    """The ARN of the secret a secret or secret-version resource belongs to."""
    attribute = _SECRET_ARN_ATTRIBUTE[str(resource.get("type"))]
    return str((resource.get("values") or {}).get(attribute, ""))  # type: ignore[union-attr]


def kafka_provider(record: Mapping[str, object]) -> str:
    """The kafka.provider a run applied with, or "" for a record whose tfvars name none."""
    tfvars = record.get("tfvars")
    kafka = tfvars.get("kafka") if isinstance(tfvars, Mapping) else None
    provider = kafka.get("provider") if isinstance(kafka, Mapping) else None
    return provider if isinstance(provider, str) else ""


def missing_vendor_credentials(record: Mapping[str, object], env: Mapping[str, str]) -> list[str]:
    """The vendor credential names a run's destroy needs that `env` leaves unset or blank.

    A runner passes an undefined secret through as an empty string, so blank counts as unset.
    """
    return [name for name in VENDOR_PROVIDER_ENV.get(kafka_provider(record), ()) if not env.get(name, "").strip()]


def destroy_env(record: Mapping[str, object]) -> dict[str, str]:
    """The environment one run's tofu commands get: this process's, plus a placeholder where nothing reads it."""
    env = dict(os.environ)
    if kafka_provider(record) != "redpanda-cloud":
        for name in PLACEHOLDER_PROVIDER_ENV:
            if not env.get(name, "").strip():
                env[name] = PLACEHOLDER_VALUE
    return env


def destroy_run(record: Mapping[str, object]) -> int:
    """`tofu destroy` one run against its own state, from a private copy of terraform/."""
    tf_root = str(record["tf_root"])
    env = destroy_env(record)
    with tempfile.TemporaryDirectory(prefix="dfe-reaper-tf-") as scratch:
        copy = Path(scratch) / "terraform"
        shutil.copytree(
            TERRAFORM,
            copy,
            ignore=shutil.ignore_patterns(".terraform", "*.tfstate", "*.tfstate.*", "*.tfvars", "*.tfvars.json"),
        )
        root = Path(scratch) / tf_root
        if not root.is_dir():
            print(f"run {record['run_id']}: {tf_root} is not a root in this checkout", file=sys.stderr)
            return 1
        (root / OVERLAY_NAME).write_text(json.dumps(record["tfvars"]), encoding="utf-8", newline="\n")
        returncode = run_tofu(["init", "-input=false", "-reconfigure"], root, env)
        if returncode == 0:
            try:
                gone = stale_state_addresses(state_json(root, env))
            except cloud_sweep.CloudSweepError as exc:
                print(f"run {record['run_id']}: could not check its secrets, destroying as is: {exc}", file=sys.stderr)
                gone = []
            for address in gone:
                returncode = run_tofu(["state", "rm", address], root, env)
                if returncode != 0:
                    return returncode
            returncode = run_tofu(["destroy", "-auto-approve", "-input=false"], root, env)
            if returncode != 0 and nothing_left(root, env):
                print(f"run {record['run_id']}: destroy failed, but a refresh finds nothing of the run left", file=sys.stderr)
                returncode = 0
        return returncode


def nothing_left(root: Path, env: Mapping[str, str]) -> bool:
    """True when a refresh-only apply leaves no managed resource in the run's state.

    The sweep can delete a resource between the destroy's refresh and its delete call. EC2 then
    answers the delete of the vanished object with UnauthorizedOperation, because a tag-fenced
    allow cannot match a resource that no longer exists, and the state still lists it. A refresh
    drops whatever the account no longer holds. A refresh or a state read that fails counts as
    something left, so a real failure is never reported as reaped.
    """
    if run_tofu(["apply", "-refresh-only", "-auto-approve", "-input=false"], root, env) != 0:
        return False
    state = state_json(root, env)
    if not state:
        return False
    root_module = (state.get("values") or {}).get("root_module") or {}  # type: ignore[union-attr]
    return not [r for r in _state_resources(root_module) if r.get("mode", "managed") == "managed"]


def reap_runs(config: ReaperConfig, now: float) -> list[str]:
    """Destroy every expired run with state; one message per run left behind."""
    problems: list[str] = []
    for key in cloud_sweep.list_run_record_keys(config.state_bucket, config.state_region, config.state_prefix):
        try:
            record = read_record(config, key)
        except (cloud_sweep.CloudSweepError, json.JSONDecodeError) as exc:
            problems.append(f"{key}: unreadable run record: {exc}")
            continue
        refused = cloud_run.record_problems(record, bucket=config.state_bucket, prefix=config.state_prefix)
        if refused or key != cloud_run.record_key(config.state_prefix, str(record["run_id"])):
            problems.append(f"{key}: refused, not acted on: {'; '.join(refused) or 'key does not match run_id'}")
            continue
        run_id = str(record["run_id"])
        tags = cloud_run.run_tags(run_id, int(record["expires_at"]), config.keys)
        if cloud_run.classify(tags, now=now, grace=0, keys=config.keys) is not cloud_run.ExpiryState.EXPIRED:
            print(f"run {run_id}: not expired, left alone", file=sys.stderr)
            continue
        state = cloud_run.state_key(config.state_prefix, run_id)
        if state_exists(config, state):
            # Without its vendor credential tofu cannot reach the SaaS broker, so name what is missing first.
            missing = missing_vendor_credentials(record, os.environ)
            if missing:
                problems.append(
                    f"run {run_id}: holds {kafka_provider(record)} resources and its destroy needs "
                    f"{', '.join(missing)}, which the environment leaves unset; retried on the next schedule"
                )
                continue
            if destroy_run(record) != 0:
                problems.append(f"run {run_id}: tofu destroy failed; retried on the next schedule")
                continue
        delete_record(config, key)
        print(f"run {run_id}: destroyed and its record removed", file=sys.stderr)
    return problems


# --- the sweep -----------------------------------------------------------------


def sweep_region(config: ReaperConfig, region: str) -> int:
    argv = ["--provider", "aws", "--region", region, "--expired", "--grace", "0",
            "--delete", "--yes", "--account", config.account]
    if config.state_bucket:
        argv += ["--exclude-bucket", config.state_bucket]
    return cloud_sweep.main(argv)


# --- CLI -----------------------------------------------------------------------


def _write_outputs(values: Mapping[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def cmd_plan(env: Mapping[str, str]) -> int:
    """Validate the configuration and publish what the credential step needs."""
    try:
        config = load_config(env)
    except ReaperConfigError as exc:
        print(f"::error::cloud reaper misconfigured: {exc}")
        return 2
    if config is None:
        print(NOTICE_UNCONFIGURED)
        _write_outputs({"enabled": "false"})
        return 0
    _write_outputs({"enabled": "true", "account": config.account, "region": config.regions[0]})
    print(f"cloud reaper: account {config.account}, regions {', '.join(config.regions)}")
    return 0


def cmd_reap(env: Mapping[str, str]) -> int:
    """Destroy expired runs, then sweep expired resources, in every region."""
    try:
        config = load_config(env)
    except ReaperConfigError as exc:
        print(f"::error::cloud reaper misconfigured: {exc}")
        return 2
    if config is None:
        print(NOTICE_UNCONFIGURED)
        return 0
    problems: list[str] = []
    if config.state_bucket:
        try:
            problems += reap_runs(config, time.time())
        except cloud_sweep.CloudSweepError as exc:
            problems.append(f"run records could not be listed: {exc}")
    for region in config.regions:
        if sweep_region(config, region) != 0:
            problems.append(f"{region}: expired run resources remain (see the sweep above)")
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    commands = {"plan": cmd_plan, "reap": cmd_reap}
    if len(args) != 1 or args[0] not in commands:
        print("usage: cloud_reaper.py plan|reap  (configured by environment; see the module docstring)", file=sys.stderr)
        return 2
    return commands[args[0]](os.environ)


if __name__ == "__main__":
    sys.exit(main())
