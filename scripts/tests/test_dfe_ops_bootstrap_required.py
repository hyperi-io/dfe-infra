#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_bootstrap_required.py
#  Purpose:      Prove stack-deploy's required-env check names the OpenBao
#                AppRole outputs only on the backend that actually emits them.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions on dfe-ops' bootstrap_required() (dfe-infra#291).

The aws-sm terraform root emits DFE_SECRETS_BACKEND=aws-sm and never emits
DFE_VAULT_ADDR / DFE_VAULT_ROLE_ID, so a stack-deploy on that root failed the
required-env check before touching the cluster. These two vars are AppRole
outputs the OpenBao secrets backend produces; on aws-sm the store's own
identity is the credential.

    python3 -m pytest scripts/tests/test_dfe_ops_bootstrap_required.py -q
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_bootstrap_required", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_bootstrap_required", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_bootstrap_required"] = dfeops
_loader.exec_module(dfeops)


def _full_env(**overrides: str) -> dict[str, str]:
    """A deploy env with every backend-independent var set, plus overrides."""
    env = {name: "x" for name in dfeops.BOOTSTRAP_REQUIRED}
    env.update(overrides)
    return env


def test_aws_sm_backend_passes_with_no_vault_vars_at_all() -> None:
    """The aws-sm root never emits the vault two, and must not need to."""
    env = _full_env(DFE_SECRETS_BACKEND="aws-sm")
    required = dfeops.bootstrap_required(env)
    assert "DFE_VAULT_ADDR" not in required
    assert "DFE_VAULT_ROLE_ID" not in required
    missing = [v for v in required if not env.get(v)]
    assert missing == []


def test_openbao_backend_still_requires_the_vault_two() -> None:
    """openbao is the default when DFE_SECRETS_BACKEND is unset, either way needs both outputs."""
    explicit = _full_env(DFE_SECRETS_BACKEND="openbao")
    unset = _full_env()
    for env in (explicit, unset):
        required = dfeops.bootstrap_required(env)
        assert "DFE_VAULT_ADDR" in required
        assert "DFE_VAULT_ROLE_ID" in required
        missing = [v for v in required if not env.get(v)]
        assert missing == ["DFE_VAULT_ADDR", "DFE_VAULT_ROLE_ID"]
