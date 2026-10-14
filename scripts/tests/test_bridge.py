#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_bridge.py
#  Purpose:      Cover the terraform-output reader's state-file fallback, which
#                is what lets a box with no tofu installed still deploy.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for bootstrap/bridge.py's terraform-output reader.

`--from-terraform` used to require tofu or terraform on PATH and exited 1
without them, which blocks deploying from any machine that holds the state but
not the toolchain. The fallback reads terraform.tfstate directly.

Uses tempfile for its fixtures, so nothing is written outside the process.

    python3 scripts/tests/test_bridge.py

No third-party deps and no test runner, matching the rest of scripts/tests.
"""

import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "bootstrap" / "bridge.py"

spec = importlib.util.spec_from_file_location("bridge", SCRIPT)
bridge = importlib.util.module_from_spec(spec)
sys.modules["bridge"] = bridge
spec.loader.exec_module(bridge)


def test_state_fallback_reads_outputs() -> None:
    """Each output comes back as (value, sensitive) -- see get_tf_outputs's
    docstring for why the flag has to survive alongside the value rather than
    being resolved and dropped here."""
    state = {
        "outputs": {
            "DFE_ENV": {"value": "local"},
            "DFE_VAULT_ROLE_ID": {"value": "abc-123", "sensitive": True},
            "DFE_UNSET": {"value": None},
        }
    }
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "terraform.tfstate").write_text(
            json.dumps(state), encoding="utf-8", newline="\n"
        )
        out = bridge._outputs_from_state(tmp)
    expect("a plain output is read", out.get("DFE_ENV") == ("local", False), f"{out}")
    expect(
        "a SENSITIVE output is read too -- the state file holds the real value",
        out.get("DFE_VAULT_ROLE_ID") == ("abc-123", True),
        f"{out}",
    )
    expect("a null-valued output is dropped", "DFE_UNSET" not in out, f"{out}")


def _raised(call) -> str:
    """The TerraformOutputError message `call` raises, or "" when it raises none."""
    try:
        call()
    except bridge.TerraformOutputError as exc:
        return str(exc)
    return ""


def test_missing_state_is_fatal_and_says_why() -> None:
    """Silently returning {} would surface later as a confusing 'missing var'."""
    with tempfile.TemporaryDirectory() as tmp:
        message = _raised(lambda: bridge._outputs_from_state(tmp))
    expect("no state file raises rather than returning empty",
           "no local state to fall back on" in message, message)


def test_malformed_state_is_fatal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "terraform.tfstate").write_text("{not json", encoding="utf-8", newline="\n")
        message = _raised(lambda: bridge._outputs_from_state(tmp))
    expect("unparseable state raises rather than yielding nothing",
           message.startswith("could not read"), message)


@contextlib.contextmanager
def _path_holding(tools: dict[str, str]) -> Iterator[None]:
    """PATH set to one directory holding a shell script per tool name, for the block."""
    saved = os.environ.get("PATH", "")
    with tempfile.TemporaryDirectory() as tmp:
        for name, body in tools.items():
            script = Path(tmp) / name
            script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
            script.chmod(0o755)
        os.environ["PATH"] = tmp
        try:
            yield
        finally:
            os.environ["PATH"] = saved


def test_a_failed_tofu_output_raises_naming_what_tofu_said() -> None:
    """A caller that can go on without the outputs catches this; one that cannot reports it."""
    with _path_holding({"tofu": "echo 'Error: no outputs found' >&2\nexit 1"}):
        message = _raised(lambda: bridge.get_tf_outputs("/"))
    expect("a non-zero tofu output raises with tofu's own stderr",
           message.startswith("tofu output failed") and "no outputs found" in message, message)


def test_tofu_output_that_is_not_json_raises() -> None:
    with _path_holding({"tofu": "echo 'Warning: no state'"}):
        message = _raised(lambda: bridge.get_tf_outputs("/"))
    expect("output that is not JSON raises rather than a JSONDecodeError",
           "printed no JSON" in message, message)


def test_the_cli_reports_an_unreadable_root_and_exits_1() -> None:
    """bridge.py's own command line keeps its ERROR line and exit code."""
    with tempfile.TemporaryDirectory() as tmp, _path_holding({}):
        done = subprocess.run(
            [sys.executable, str(SCRIPT), "--tf-dir", tmp],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
    expect("exit 1", done.returncode == 1, f"exit {done.returncode}\n{done.stderr}")
    expect("ERROR names the missing state", "ERROR: neither terraform nor opentofu" in done.stderr,
           done.stderr)


def test_json_output_reads_sensitive_flag_with_no_second_call() -> None:
    """`-json` carries the real value AND the sensitive flag together -- a
    second `-raw` fetch per sensitive key would only re-fetch what this
    already has, so get_tf_outputs makes exactly one subprocess call."""
    calls = []

    class FakeResult:
        def __init__(self, stdout: str) -> None:
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    payload = json.dumps(
        {
            "DFE_ENV": {"value": "local", "sensitive": False},
            "DFE_VAULT_ROLE_ID": {"value": "abc-123", "sensitive": True},
        }
    )

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult(payload)

    real_run, real_finder = bridge.subprocess.run, bridge._find_tf_binary
    bridge.subprocess.run = fake_run
    bridge._find_tf_binary = lambda: "tofu"
    try:
        out = bridge.get_tf_outputs("/unused")
    finally:
        bridge.subprocess.run = real_run
        bridge._find_tf_binary = real_finder

    expect("exactly one subprocess call", len(calls) == 1, f"{calls}")
    expect("a plain output keeps its flag", out.get("DFE_ENV") == ("local", False), f"{out}")
    expect(
        "a sensitive output's real value comes from the one -json call",
        out.get("DFE_VAULT_ROLE_ID") == ("abc-123", True),
        f"{out}",
    )


_BASE_ENV = {
    "DFE_ENV": "prod",
    "DFE_CLOUD": "aws",
    "DFE_REGION": "us-east-1",
    "DFE_DOMAIN": "dfe.example.com",
    "DFE_PROFILE": "scale",
    "DFE_REPO_URL": "https://example.com/repo.git",
    "DFE_TARGET_REVISION": "main",
    "DFE_STORAGE_CLASS": "gp3",
    "DFE_NAMESPACE": "dfe-prod",
    "DFE_CLICKHOUSE_HOST": "clickhouse.example.com",
    "DFE_KAFKA_BOOTSTRAP": "broker:9092",
    "DFE_OTEL_ENDPOINT": "otel:4317",
    "DFE_WORKLOAD_IDENTITY_ANNOTATIONS": "{}",
}


def test_aws_sm_backend_with_no_vault_vars_passes_validation() -> None:
    """The aws root never emits DFE_VAULT_ADDR/DFE_VAULT_ROLE_ID -- they are
    openbao-only, and an aws-sm deployment must not be refused for lacking
    them."""
    env_vars = {**_BASE_ENV, "DFE_SECRETS_BACKEND": "aws-sm", "DFE_SECRETS_REGION": "us-east-1"}
    missing = bridge._required_vars(env_vars) - set(env_vars.keys())
    expect("an aws-sm deployment has nothing missing", not missing, f"{missing}")


def test_aws_sm_backend_demands_the_store_region_bootstrap_demands() -> None:
    """bootstrap.sh adds DFE_SECRETS_REGION on every non-openbao backend; the
    two lists disagreeing makes this gate miss what bootstrap.sh then refuses."""
    env_vars = {**_BASE_ENV, "DFE_SECRETS_BACKEND": "aws-sm"}
    missing = bridge._required_vars(env_vars) - set(env_vars.keys())
    expect("DFE_SECRETS_REGION is named as missing",
           missing == {"DFE_SECRETS_REGION"}, f"{missing}")


def test_an_empty_backend_output_defaults_to_openbao() -> None:
    """A tofu root that emits DFE_SECRETS_BACKEND as an empty string means the
    default, exactly as bootstrap.sh's ${DFE_SECRETS_BACKEND:-openbao} reads it
    -- reading it as aws-sm would drop the AppRole checks AND the region one."""
    env_vars = {**_BASE_ENV, "DFE_SECRETS_BACKEND": ""}
    missing = bridge._required_vars(env_vars) - set(env_vars.keys())
    expect("the AppRole pair is still demanded",
           missing == {"DFE_VAULT_ADDR", "DFE_VAULT_ROLE_ID"}, f"{missing}")


def test_openbao_backend_with_no_vault_vars_still_fails_naming_both_keys() -> None:
    """openbao is the default backend, and without the AppRole address/role_id
    ESO can never authenticate."""
    env_vars = dict(_BASE_ENV)
    missing = bridge._required_vars(env_vars) - set(env_vars.keys())
    expect("DFE_VAULT_ADDR is named as missing", "DFE_VAULT_ADDR" in missing, f"{missing}")
    expect("DFE_VAULT_ROLE_ID is named as missing", "DFE_VAULT_ROLE_ID" in missing, f"{missing}")


def test_binary_finder_returns_none_rather_than_exiting() -> None:
    """It must be non-fatal now, or the fallback below it is unreachable."""
    result = bridge._find_tf_binary()
    expect(
        "the finder returns a name or None, never exits",
        result is None or isinstance(result, str),
        f"{result!r}",
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
