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


def _no_terraform(tmp_path: Path, **over: object) -> argparse.Namespace:
    args = _args(tmp_path)
    args.from_terraform = None
    for key, value in over.items():
        setattr(args, key, value)
    return args


def test_the_access_summary_defaults_into_this_repos_run_directory(
    tmp_path: Path, monkeypatch
) -> None:
    """A cycle launched from another repo wrote dfe-access.md into THAT repo's tree."""
    elsewhere = tmp_path / "another-repo"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    env = dfeops._assemble_env(_no_terraform(tmp_path, access_out="", stack="2.2.0-rc.99"))

    out = Path(env["DFE_ACCESS_OUT"])
    assert out == REPO_ROOT / ".tmp" / "2.2.0-rc.99-scale" / dfeops.ACCESS_OUT_FILENAME
    assert out.is_absolute()
    assert elsewhere not in out.parents


def test_the_access_summary_sits_beside_the_launchers_login_file(tmp_path: Path) -> None:
    env = dfeops._assemble_env(_no_terraform(tmp_path, access_out=""))

    assert Path(env["DFE_ACCESS_OUT"]).parent == Path(env["DFE_ACCESS_SUMMARY_OUT"]).parent


def test_an_explicit_access_out_is_used_as_given(tmp_path: Path) -> None:
    chosen = tmp_path / "mine.md"

    env = dfeops._assemble_env(_no_terraform(tmp_path, access_out=str(chosen)))

    assert env["DFE_ACCESS_OUT"] == str(chosen)


def test_the_summary_directory_exists_before_bootstrap_writes_into_it(
    tmp_path: Path, monkeypatch
) -> None:
    """access-summary.sh writes through tee, which fails on a missing directory."""
    out = tmp_path / "run" / "nested" / "summary.md"
    seen: dict[str, bool] = {}

    def fake_bootstrap(cmd: list[str], *, env: dict | None = None) -> int:
        seen["parent_existed"] = out.parent.is_dir()
        seen["pointed_at_it"] = env is not None and env["DFE_ACCESS_OUT"] == str(out)
        return 0

    monkeypatch.setattr(dfeops, "_resolve_stack", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_offline_preflight", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_bootstrap_required", lambda _env: ())
    monkeypatch.setattr(dfeops, "_require_script", lambda _name: tmp_path / "bootstrap.sh")
    monkeypatch.setattr(dfeops, "_run_streaming", fake_bootstrap)
    monkeypatch.delenv("DFE_VAULT_ADDR", raising=False)
    args = _no_terraform(tmp_path, access_out=str(out), check_only=False, mode="single")

    assert dfeops.cmd_stack_deploy(args) == 0
    assert seen == {"parent_existed": True, "pointed_at_it": True}


def test_the_flag_defaults_empty_so_the_run_directory_decides() -> None:
    args = dfeops.build_parser().parse_args(["stack-deploy"])

    assert args.access_out == ""


def test_a_hand_run_bootstrap_also_writes_under_the_repos_tmp() -> None:
    """bootstrap.sh run without dfe-ops falls back to its own default, not the cwd."""
    body = (REPO_ROOT / "bootstrap" / "bootstrap.sh").read_text(encoding="utf-8")

    assert '"${DFE_ACCESS_OUT:-${REPO_ROOT}/.tmp/dfe-access.md}"' in body
    assert '"${DFE_ACCESS_SUMMARY_OUT:-${REPO_ROOT}/.tmp/access-summary.md}"' in body


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


def test_only_the_cluster_broker_reads_its_credential_from_the_store() -> None:
    """The single broker's credential comes from an in-cluster generator, so the
    store holds nothing it needs."""
    assert dfeops._broker_credential_from_store("scale") is True
    assert dfeops._broker_credential_from_store("single") is False
    assert dfeops._broker_credential_from_store("slim") is False
    assert dfeops._broker_credential_from_store("mesh") is False


def _deploy_without_a_secret_id(tmp_path: Path, monkeypatch, mode: str) -> tuple[int, bool]:
    """(stack-deploy's exit code, whether bootstrap ran) with a store address and no SecretID."""
    ran: dict[str, bool] = {"bootstrap": False}

    def fake_bootstrap(cmd: list[str], *, env: dict | None = None) -> int:
        ran["bootstrap"] = True
        return 0

    monkeypatch.setattr(dfeops, "_resolve_stack", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_offline_preflight", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_bootstrap_required", lambda _env: ())
    monkeypatch.setattr(dfeops, "_require_script", lambda _name: tmp_path / "bootstrap.sh")
    monkeypatch.setattr(dfeops, "_run_streaming", fake_bootstrap)
    monkeypatch.setenv("DFE_VAULT_ADDR", "https://store.example.invalid:8200")
    monkeypatch.delenv("DFE_VAULT_SECRET_ID", raising=False)
    args = _no_terraform(
        tmp_path, access_out=str(tmp_path / "access.md"), check_only=False, mode=mode
    )
    return dfeops.cmd_stack_deploy(args), ran["bootstrap"]


def test_a_single_broker_deploy_needs_no_secret_id(tmp_path: Path, monkeypatch) -> None:
    """Refusing it sent a store-less kind or on-prem deploy of the single tier away
    for a credential nothing in that tier reads."""
    assert _deploy_without_a_secret_id(tmp_path, monkeypatch, "single") == (0, True)


def test_a_cluster_broker_deploy_still_needs_the_secret_id(tmp_path: Path, monkeypatch) -> None:
    assert _deploy_without_a_secret_id(tmp_path, monkeypatch, "scale") == (1, False)


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
