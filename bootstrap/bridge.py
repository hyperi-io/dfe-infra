#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         bridge.py
#  Purpose:      Read Terraform outputs and invoke bootstrap.sh with correct env vars
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Bridge from Terraform outputs to bootstrap.sh environment variables.

Usage:
    # From repo root after `terraform apply` in environments/local:
    python3 bootstrap/bridge.py --tf-dir terraform/environments/local

    # Dry-run (shows env vars, runs bootstrap in DFE_DRY_RUN=true mode):
    python3 bootstrap/bridge.py --tf-dir terraform/environments/local --dry-run
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from shutil import which


def _find_tf_binary() -> str | None:
    """The IaC binary, or None -- a missing one has a fallback, so it is not fatal."""
    for cmd in ("tofu", "terraform"):
        if which(cmd):
            return cmd
    return None


def _outputs_from_state(tf_dir: str) -> dict[str, str]:
    """Read outputs straight out of terraform.tfstate.

    The binary is the right reader when it is present -- it honours remote state
    and workspaces. This is for the machine that has the state file but no tofu
    installed, where the alternative is being unable to deploy at all. Local
    state only, and it says so rather than silently reading a stale file.
    """
    state = Path(tf_dir) / "terraform.tfstate"
    if not state.is_file():
        print(
            f"ERROR: neither terraform nor opentofu is on PATH, and there is no "
            f"local state to fall back on at {state}",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        raw = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not read {state}: {exc}", file=sys.stderr)
        sys.exit(1)

    outputs = {
        k: str(v.get("value", ""))
        for k, v in (raw.get("outputs") or {}).items()
        if v.get("value") is not None
    }
    print(
        f"NOTE: no tofu/terraform on PATH -- read {len(outputs)} output(s) from "
        f"the LOCAL state file {state}. If this environment uses remote state, "
        f"that file may be stale; install tofu to read authoritatively.",
        file=sys.stderr,
    )
    return outputs


def get_tf_outputs(tf_dir: str) -> dict[str, str]:
    """Run `terraform output -json` and return a flat dict of name->value.

    Handles sensitive outputs: terraform output -json redacts them.
    For any sensitive output, falls back to `terraform output -raw <key>`.

    With no IaC binary installed, falls back to reading terraform.tfstate.
    """
    tf_bin = _find_tf_binary()
    if tf_bin is None:
        return _outputs_from_state(tf_dir)
    result = subprocess.run(
        [tf_bin, "output", "-json"],
        cwd=tf_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: {tf_bin} output failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    raw = json.loads(result.stdout)
    outputs = {}
    for k, v in raw.items():
        if v.get("sensitive", False):
            # Sensitive outputs are redacted in -json mode; fetch individually
            raw_result = subprocess.run(
                [tf_bin, "output", "-raw", k],
                cwd=tf_dir,
                capture_output=True,
                text=True,
            )
            if raw_result.returncode != 0:
                print(
                    f"WARNING: could not read sensitive output '{k}': {raw_result.stderr}",
                    file=sys.stderr,
                )
                outputs[k] = ""
            else:
                outputs[k] = raw_result.stdout.strip()
        else:
            outputs[k] = str(v["value"])
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge Terraform outputs to bootstrap.sh")
    parser.add_argument(
        "--tf-dir",
        default="terraform/environments/local",
        help="Path to Terraform environment directory (default: terraform/environments/local)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run bootstrap.sh with DFE_DRY_RUN=true (shows commands without executing)",
    )
    args = parser.parse_args()

    # Resolve paths
    repo_root = Path(__file__).resolve().parent.parent
    tf_dir = (repo_root / args.tf_dir).resolve()
    bootstrap_sh = repo_root / "bootstrap" / "bootstrap.sh"

    if not tf_dir.is_dir():
        print(f"ERROR: Terraform directory not found: {tf_dir}", file=sys.stderr)
        sys.exit(1)

    if not bootstrap_sh.is_file():
        print(f"ERROR: bootstrap.sh not found: {bootstrap_sh}", file=sys.stderr)
        sys.exit(1)

    # Read Terraform outputs
    print(f"Reading Terraform outputs from {tf_dir}...")
    outputs = get_tf_outputs(str(tf_dir))

    # Filter to DFE_* keys only
    env_vars = {k: v for k, v in outputs.items() if k.startswith("DFE_")}

    if not env_vars:
        print("ERROR: No DFE_* outputs found in Terraform state.", file=sys.stderr)
        print("Did you run `terraform apply` first?", file=sys.stderr)
        sys.exit(1)

    # Validate required vars
    required = {
        "DFE_ENV",
        "DFE_CLOUD",
        "DFE_REGION",
        "DFE_DOMAIN",
        "DFE_PROFILE",
        "DFE_REPO_URL",
        "DFE_TARGET_REVISION",
        "DFE_STORAGE_CLASS",
        "DFE_NAMESPACE",
        "DFE_CLICKHOUSE_HOST",
        "DFE_KAFKA_BOOTSTRAP",
        "DFE_OTEL_ENDPOINT",
        "DFE_VAULT_ADDR",
        "DFE_VAULT_ROLE_ID",
        "DFE_WORKLOAD_IDENTITY_ANNOTATIONS",
    }
    missing = required - set(env_vars.keys())
    if missing:
        print(f"ERROR: Missing required outputs: {', '.join(sorted(missing))}", file=sys.stderr)
        sys.exit(1)

    # Show env vars summary
    print(f"\n  {len(env_vars)} DFE_* variables loaded from Terraform")
    for k in sorted(env_vars.keys()):
        v = env_vars[k]
        if "TOKEN" in k or "SECRET" in k or "ROLE_ID" in k:
            display = v[:4] + "***" if len(v) > 4 else "***"
        else:
            display = v
        print(f"    {k}={display}")

    # Merge with current env
    full_env = {**os.environ, **env_vars}

    if args.dry_run:
        full_env["DFE_DRY_RUN"] = "true"
        print("\n  [DRY-RUN mode: bootstrap.sh will print commands without executing]")

    # Execute bootstrap.sh
    print(f"\nExecuting {bootstrap_sh}...\n")
    result = subprocess.run(
        ["bash", str(bootstrap_sh)],
        env=full_env,
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
