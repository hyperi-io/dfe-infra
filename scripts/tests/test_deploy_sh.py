#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_deploy_sh.py
#  Purpose:      Prove bootstrap/deploy.sh hands the deployment's store choice to
#                the OpenTofu root, so a store-less on-prem tier never needs OpenBao.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What deploy.sh passes OpenTofu about the secrets store.

DFE_SECRETS_BACKEND, from the environment or the operator's .env in the
OpenTofu environment dir, reaches the root as TF_VAR_secrets_backend. A stub
`tofu` records what it was given, and its `output` fails so the run stops
before bootstrap.sh.

    python3 -m pytest scripts/tests/test_deploy_sh.py -q
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEPLOY = REPO_ROOT / "bootstrap" / "deploy.sh"

_STUB = """#!/bin/sh
if [ "$1" = "output" ]; then
  echo "stub: no state" >&2
  exit 1
fi
echo "$1 ${TF_VAR_secrets_backend-<unset>}" >> "$STUB_LOG"
"""


def _deploy(tmp_path: Path, env_file: str | None, **env: str) -> tuple[int, list[str]]:
    """(deploy.sh's exit code, one "<tofu subcommand> <TF_VAR_secrets_backend>" line per call)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "tofu"
    stub.write_text(_STUB, encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    tf_dir = tmp_path / "root"
    tf_dir.mkdir()
    if env_file is not None:
        (tf_dir / ".env").write_text(env_file, encoding="utf-8", newline="\n")
    log = tmp_path / "tofu.log"
    base = {k: v for k, v in os.environ.items() if not k.startswith(("DFE_", "TF_VAR_"))}
    done = subprocess.run(
        ["bash", str(DEPLOY), "--tf-dir", str(tf_dir), "--dry-run"],
        env={**base, "PATH": f"{bin_dir}:{base.get('PATH', '')}", "STUB_LOG": str(log), **env},
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return done.returncode, lines


@pytest.mark.parametrize(
    ("env_file", "env", "expected"),
    [
        # The operator's .env is read before OpenTofu, not only before bootstrap.
        ('DFE_SECRETS_BACKEND="none"\n', {}, "none"),
        (None, {"DFE_SECRETS_BACKEND": "openbao"}, "openbao"),
        # The shipped env example leaves it empty, which leaves the root to resolve it.
        ('DFE_SECRETS_BACKEND=""\n', {}, "<unset>"),
        (None, {}, "<unset>"),
    ],
)
def test_the_store_choice_reaches_the_root(
    tmp_path: Path, env_file: str | None, env: dict[str, str], expected: str
) -> None:
    rc, calls = _deploy(tmp_path, env_file, **env)
    assert calls == [f"init {expected}", f"plan {expected}"], calls
    # The stub's failed `output` stops bridge.py, so deploy.sh exits non-zero.
    assert rc == 1, calls
