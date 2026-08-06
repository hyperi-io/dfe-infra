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

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "bootstrap" / "bridge.py"

spec = importlib.util.spec_from_file_location("bridge", SCRIPT)
bridge = importlib.util.module_from_spec(spec)
sys.modules["bridge"] = bridge
spec.loader.exec_module(bridge)

_failures = 0


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


def test_state_fallback_reads_outputs() -> None:
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
    expect("a plain output is read", out.get("DFE_ENV") == "local", f"{out}")
    expect(
        "a SENSITIVE output is read too -- the state file holds the real value",
        out.get("DFE_VAULT_ROLE_ID") == "abc-123",
        f"{out}",
    )
    expect("a null-valued output is dropped", "DFE_UNSET" not in out, f"{out}")


def test_missing_state_is_fatal_and_says_why() -> None:
    """Silently returning {} would surface later as a confusing 'missing var'."""
    with tempfile.TemporaryDirectory() as tmp:
        raised = False
        try:
            bridge._outputs_from_state(tmp)
        except SystemExit:
            raised = True
    expect("no state file exits rather than returning empty", raised)


def test_malformed_state_is_fatal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "terraform.tfstate").write_text(
            "{not json", encoding="utf-8", newline="\n"
        )
        raised = False
        try:
            bridge._outputs_from_state(tmp)
        except SystemExit:
            raised = True
    expect("unparseable state exits rather than yielding nothing", raised)


def test_binary_finder_returns_none_rather_than_exiting() -> None:
    """It must be non-fatal now, or the fallback below it is unreachable."""
    result = bridge._find_tf_binary()
    expect(
        "the finder returns a name or None, never exits",
        result is None or isinstance(result, str),
        f"{result!r}",
    )


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'FAILED' if _failures else 'ALL PASSED'} -- {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
