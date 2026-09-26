#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_stack_deploy_env.py
#  Purpose:      Prove stack-deploy's env assembly unwraps the terraform bridge's
#                (value, sensitive) pairs, demands only the secrets vars the
#                declared backend actually uses, and runs its preflight with no
#                --registry given.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What `dfe-ops stack-deploy --from-terraform` builds its environment from.

bridge.get_tf_outputs answers name -> (value, sensitive) by design, documented
in its own docstring. Handing those pairs straight to subprocess.run refuses the
whole call with a TypeError, so every --from-terraform deploy failed before
bootstrap.sh ran at all.

The required-variable gate is the other half: DFE_VAULT_ADDR and
DFE_VAULT_ROLE_ID are openbao-only, and demanding them on an aws-sm deployment
refuses a deploy that has everything it needs.

    python3 -m pytest scripts/tests/test_dfe_ops_stack_deploy_env.py -q
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_stack_deploy_env", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_stack_deploy_env", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_stack_deploy_env"] = dfeops
_loader.exec_module(dfeops)

# The shape bridge.get_tf_outputs really answers with.
TF_OUTPUTS = {
    "DFE_DOMAIN": ("dfe-test.internal", False),
    "DFE_NAMESPACE": ("dfe-test", False),
    "DFE_VAULT_SECRET_ID": ("s3cr3t", True),
    "cluster_name": ("dfe-aws-test", False),
}


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        from_terraform=str(tmp_path),
        env_file=[],
        mode="scale",
        target_revision="",
        kubeconfig="",
        readiness_timeout=900,
        access_out=str(tmp_path / "access.txt"),
        stack="current",
        registry="registry.example.com/dfe",
        base_domain=None,
        domain=None,
        skip_e2e=False,
        dry_run=False,
        e2e=False,
    )


def test_terraform_outputs_land_as_strings(tmp_path: Path, monkeypatch) -> None:
    """A raw 2-tuple in the env dict is what crashed subprocess.run outright."""
    bridge = type(sys)("bridge")
    bridge.get_tf_outputs = lambda _dir: dict(TF_OUTPUTS)
    monkeypatch.setitem(sys.modules, "bridge", bridge)
    env = dfeops._assemble_env(_args(tmp_path))
    assert env["DFE_DOMAIN"] == "dfe-test.internal"
    assert env["DFE_NAMESPACE"] == "dfe-test"
    assert env["DFE_VAULT_SECRET_ID"] == "s3cr3t"
    assert all(isinstance(value, str) for value in env.values())
    # A non-DFE_ output is still filtered out.
    assert "cluster_name" not in env


def test_the_registry_flag_reaches_bootstrap(tmp_path: Path, monkeypatch) -> None:
    """--registry otherwise stops at the version-set render, and every dfe-*
    image is left resolving against Docker Hub."""
    bridge = type(sys)("bridge")
    bridge.get_tf_outputs = lambda _dir: dict(TF_OUTPUTS)
    monkeypatch.setitem(sys.modules, "bridge", bridge)
    env = dfeops._assemble_env(_args(tmp_path))
    assert env["DFE_REGISTRY"] == "registry.example.com/dfe"


def _preflight_render_argv(monkeypatch, registry: str | None) -> list[str]:
    """The argv the offline preflight's first step runs, stopping it right there."""
    seen: list[list[str]] = []

    def fake_run_text(cmd: list[str], env: dict | None = None) -> tuple[int, str, str]:
        seen.append(cmd)
        return 1, "", "stopped by the test"

    monkeypatch.setattr(dfeops, "_run_text", fake_run_text)
    args = argparse.Namespace(registry=registry, stack="2.2.0-rc.99", strict_compat=False, mode="single")
    assert dfeops._offline_preflight(args) == 1
    return seen[0]


def test_preflight_runs_with_no_registry_flag(monkeypatch) -> None:
    """--registry defaults to None, and a None in argv refused the whole run with
    a TypeError before any check had run."""
    argv = _preflight_render_argv(monkeypatch, None)
    assert None not in argv
    assert "--registry" not in argv
    assert argv[-3:] == ["render", "--stack", "2.2.0-rc.99"]


def test_preflight_passes_a_given_registry_on(monkeypatch) -> None:
    argv = _preflight_render_argv(monkeypatch, "registry.example.com/dfe")
    assert argv[argv.index("--registry") + 1] == "registry.example.com/dfe"


def test_openbao_is_required_only_when_the_backend_is_openbao() -> None:
    aws_sm = dfeops._bootstrap_required({"DFE_SECRETS_BACKEND": "aws-sm"})
    assert "DFE_VAULT_ADDR" not in aws_sm
    assert "DFE_VAULT_ROLE_ID" not in aws_sm
    assert "DFE_SECRETS_REGION" in aws_sm

    openbao = dfeops._bootstrap_required({"DFE_SECRETS_BACKEND": "openbao"})
    assert "DFE_VAULT_ADDR" in openbao
    assert "DFE_VAULT_ROLE_ID" in openbao
    assert "DFE_SECRETS_REGION" not in openbao


def test_an_unset_or_empty_backend_reads_as_openbao() -> None:
    """bootstrap.sh's own ${DFE_SECRETS_BACKEND:-openbao} -- a present-but-empty
    value must not be read as aws-sm and drop both checks."""
    for env in ({}, {"DFE_SECRETS_BACKEND": ""}):
        required = dfeops._bootstrap_required(env)
        assert "DFE_VAULT_ADDR" in required, env
        assert "DFE_SECRETS_REGION" not in required, env


def test_the_backend_split_matches_the_bridge_it_drives() -> None:
    """dfe-ops and bridge.py gate the same deploy, so a disagreement is either a
    false refusal or a miss that surfaces later and worse."""
    sys.path.insert(0, str(REPO_ROOT / "bootstrap"))
    import bridge

    for backend in ("openbao", "aws-sm"):
        ours = set(dfeops._bootstrap_required({"DFE_SECRETS_BACKEND": backend}))
        theirs = bridge._required_vars({"DFE_SECRETS_BACKEND": backend})
        assert ours & {"DFE_VAULT_ADDR", "DFE_VAULT_ROLE_ID", "DFE_SECRETS_REGION"} == theirs & {
            "DFE_VAULT_ADDR",
            "DFE_VAULT_ROLE_ID",
            "DFE_SECRETS_REGION",
        }, backend
