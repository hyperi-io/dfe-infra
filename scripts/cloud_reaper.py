#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/cloud_reaper.py
#  Purpose:      The scheduled reaper's logic, driven by environment variables
#                so .github/workflows/cloud-reaper.yml carries no value of its
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

`reap` works in two passes, both with a grace of 0. First every run record
under the prefix: one that fails `cloud_run.record_problems` is reported and
left alone, an unexpired one is left alone, and an expired one gets `tofu
destroy` against its own per-run state, from a copy of this checkout's
terraform/ tree so no run sees another's overlay. A successful destroy removes
the record. Then `cloud_sweep.py --expired --grace 0 --delete` in every region,
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
# Providers a root configures whether or not the dial selects them; a placeholder satisfies their check.
PLACEHOLDER_PROVIDER_ENV = ("REDPANDA_CLIENT_ID", "REDPANDA_CLIENT_SECRET")


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


def list_record_keys(config: ReaperConfig) -> list[str]:
    """Every `<prefix>/<run id>/run.json` object in the state bucket."""
    keys: list[str] = []
    token: str | None = None
    while True:
        args = ["s3api", "list-objects-v2", "--bucket", config.state_bucket, "--prefix", f"{config.state_prefix}/"]
        if token:
            args += ["--starting-token", token]
        body = cloud_sweep.run_aws(args, config.state_region)
        for item in body.get("Contents", []):
            key = item.get("Key", "")
            parts = key.split("/")
            if key.startswith(f"{config.state_prefix}/") and parts[-1] == cloud_run.RECORD_OBJECT:
                keys.append(key)
        token = body.get("NextToken") or None
        if not token:
            return keys


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


def destroy_run(record: Mapping[str, object]) -> int:
    """`tofu destroy` one run against its own state, from a private copy of terraform/."""
    tf_root = str(record["tf_root"])
    env = dict(os.environ)
    for name in PLACEHOLDER_PROVIDER_ENV:
        env.setdefault(name, "unused-placeholder")
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
            returncode = run_tofu(["destroy", "-auto-approve", "-input=false"], root, env)
        return returncode


def reap_runs(config: ReaperConfig, now: float) -> list[str]:
    """Destroy every expired run with state; one message per run left behind."""
    problems: list[str] = []
    for key in list_record_keys(config):
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
        if state_exists(config, state) and destroy_run(record) != 0:
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
