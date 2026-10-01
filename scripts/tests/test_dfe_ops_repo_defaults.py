#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_repo_defaults.py
#  Purpose:      Prove the acceptance and ui subcommands find their companion
#                checkout from the environment or the sibling directory, and
#                demand the flag when there is neither -- never from a path
#                baked into a product repo. kubeconfig's secret-store path is
#                always the operator's flag.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Where `dfe-ops acceptance --repo` and `dfe-ops ui --ui-repo` default from.

A hardcoded absolute path works on exactly one machine and is wrong for every
other org deploying the suite, so the default is resolved: the env var, else the
checkout beside this repo, else nothing and the flag is required. The same holds
for `dfe-ops kubeconfig --vault-path`, which has nothing to resolve from.

    python3 -m pytest scripts/tests/test_dfe_ops_repo_defaults.py -q
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_repo_defaults", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_repo_defaults", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_repo_defaults"] = dfeops
_loader.exec_module(dfeops)


def test_the_environment_variable_wins(tmp_path: Path) -> None:
    previous = os.environ.get("DFE_TEST_REPO_OVERRIDE")
    os.environ["DFE_TEST_REPO_OVERRIDE"] = str(tmp_path)
    try:
        assert dfeops._sibling_repo("dfe-engine", "DFE_TEST_REPO_OVERRIDE") == str(tmp_path)
    finally:
        if previous is None:
            os.environ.pop("DFE_TEST_REPO_OVERRIDE", None)
        else:
            os.environ["DFE_TEST_REPO_OVERRIDE"] = previous


def test_a_sibling_checkout_is_found() -> None:
    """The repo's own directory is a sibling of itself, so it always resolves."""
    found = dfeops._sibling_repo(REPO_ROOT.name, "DFE_TEST_REPO_UNSET")
    assert found == str(REPO_ROOT)


def test_no_env_and_no_sibling_resolves_to_nothing() -> None:
    assert dfeops._sibling_repo("not-a-checkout", "DFE_TEST_REPO_UNSET") is None


def test_parsing_follows_whatever_resolved() -> None:
    """Nothing resolved -> argparse refuses, naming the flag; otherwise it fills in."""
    parser = dfeops.build_parser()
    expected = dfeops._sibling_repo("dfe-engine", "DFE_ENGINE_REPO")
    if expected is None:
        with pytest.raises(SystemExit):
            parser.parse_args(["acceptance"])
    else:
        assert parser.parse_args(["acceptance"]).repo == expected


def test_no_absolute_developer_path_is_baked_into_a_default() -> None:
    """The defect this replaced: a /projects/... default in a product repo."""
    parser = dfeops.build_parser()
    for name in ("acceptance", "ui"):
        sub = parser._subparsers._group_actions[0].choices[name]
        for action in sub._actions:
            if not isinstance(action.default, str) or not action.default.startswith("/"):
                continue
            # A resolved sibling or env override is fine; a committed literal is not.
            assert action.default in (
                str(REPO_ROOT.parent / "dfe-engine"),
                str(REPO_ROOT.parent / "dfe-ui"),
                os.environ.get("DFE_ENGINE_REPO", ""),
                os.environ.get("DFE_UI_REPO", ""),
                os.environ.get("KUBECONFIG", ""),
            ), f"{name} {action.dest} defaults to {action.default}"


def test_the_ssh_key_path_is_the_operator_s_to_name() -> None:
    """Where a deployer keeps a node's SSH key is estate context, so kubeconfig has no default."""
    parser = dfeops.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["kubeconfig", "--node", "192.0.2.10"])
    args = parser.parse_args(
        ["kubeconfig", "--node", "192.0.2.10", "--vault-path", "kv/example/ssh"]
    )
    assert args.vault_path == "kv/example/ssh"
