#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_teardown_facts.py
#  Purpose:      Prove `dfe-ops teardown --from-terraform` hands destroy.sh the
#                cloud root's facts its step 9 falls back on, and that the cycle
#                passes its root to the teardown
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What `dfe-ops teardown` hands destroy.sh when the cluster secret may be gone.

A bootstrap that stops before writing the Argo CD cluster secret, or a second
teardown after step 8 deleted it, leaves destroy.sh nothing to read the run's
cloud, region, cluster, VPC and run tags from. The cloud root's outputs carry
the same facts, so the teardown passes them through and step 9 can still sweep
a test run.

    python3 -m pytest scripts/tests/test_dfe_ops_teardown_facts.py -q
"""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_teardown_facts", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_teardown_facts", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_teardown_facts"] = dfeops
_loader.exec_module(dfeops)

RUN_TAGS = '{"dfe-e2e":"r20261013t210430z-73a3d0","expires-at":"2026-10-14T00:23:30Z"}'
# The shape bridge.get_tf_outputs really answers with: name -> (value, sensitive).
TF_OUTPUTS = {
    "DFE_CLOUD": ("aws", False),
    "DFE_REGION": ("us-west-2", False),
    "DFE_KUBE_CLUSTER_NAME": ("dfe-cycle", False),
    "DFE_VPC_ID": ("vpc-0run", False),
    "DFE_RUN_TAGS": (RUN_TAGS, False),
    "DFE_NAMESPACE": ("dfe-test", False),
    "cluster_name": ("dfe-cycle", False),
}


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict]]:
    """Every command the teardown runs, with its environment, and none of them run for real."""
    calls: list[tuple[list[str], dict]] = []

    def run(cmd: list[str], env: dict | None = None) -> int:
        calls.append((cmd, dict(env or {})))
        return 0

    monkeypatch.setattr(dfeops, "_run_streaming", run)
    for name in dfeops.TEARDOWN_FACTS:
        monkeypatch.delenv(name, raising=False)
    return calls


def _bridge(monkeypatch: pytest.MonkeyPatch, outputs: object) -> None:
    bridge = type(sys)("bridge")

    def get_tf_outputs(_tf_dir: str) -> dict:
        if isinstance(outputs, BaseException):
            raise outputs
        return dict(outputs)

    bridge.get_tf_outputs = get_tf_outputs
    monkeypatch.setitem(sys.modules, "bridge", bridge)


def _teardown(*argv: str) -> int:
    return dfeops.cmd_teardown(dfeops.build_parser().parse_args(["teardown", "--force", *argv]))


def test_the_roots_cluster_facts_reach_destroy_sh(
    monkeypatch: pytest.MonkeyPatch, ran: list[tuple[list[str], dict]]
) -> None:
    _bridge(monkeypatch, TF_OUTPUTS)
    assert _teardown("--from-terraform", "terraform/environments/aws") == 0
    ((cmd, env),) = ran
    assert cmd[-2:] == [str(dfeops.BOOTSTRAP / "destroy.sh"), "--force"]
    assert {name: env[name] for name in dfeops.TEARDOWN_FACTS} == {
        "DFE_CLOUD": "aws",
        "DFE_REGION": "us-west-2",
        "DFE_KUBE_CLUSTER_NAME": "dfe-cycle",
        "DFE_VPC_ID": "vpc-0run",
        "DFE_RUN_TAGS": RUN_TAGS,
    }


def test_only_the_cluster_facts_are_passed(
    monkeypatch: pytest.MonkeyPatch, ran: list[tuple[list[str], dict]]
) -> None:
    """destroy.sh reads these five and nothing else, so nothing else of the root's is handed on."""
    monkeypatch.delenv("DFE_NAMESPACE", raising=False)
    _bridge(monkeypatch, TF_OUTPUTS)
    _teardown("--from-terraform", "terraform/environments/aws")
    ((_cmd, env),) = ran
    assert "DFE_NAMESPACE" not in env
    assert "cluster_name" not in env


def test_an_empty_output_is_not_passed(
    monkeypatch: pytest.MonkeyPatch, ran: list[tuple[list[str], dict]]
) -> None:
    """A deployment that is not a test run has an empty DFE_RUN_TAGS output."""
    _bridge(monkeypatch, {**TF_OUTPUTS, "DFE_RUN_TAGS": ("", False)})
    _teardown("--from-terraform", "terraform/environments/aws")
    ((_cmd, env),) = ran
    assert "DFE_RUN_TAGS" not in env
    assert env["DFE_CLOUD"] == "aws"


def test_without_a_root_destroy_sh_gets_no_facts(
    monkeypatch: pytest.MonkeyPatch, ran: list[tuple[list[str], dict]]
) -> None:
    _bridge(monkeypatch, AssertionError("no root was named, so no output is read"))
    assert _teardown() == 0
    ((_cmd, env),) = ran
    assert [name for name in dfeops.TEARDOWN_FACTS if name in env] == []


def test_expected_fail_outputs_that_cannot_be_read_still_tear_down(
    monkeypatch: pytest.MonkeyPatch,
    ran: list[tuple[list[str], dict]],
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """A teardown that stopped on an unreadable root would leave the workloads running.

    The real bridge reads a root with no tofu on PATH and no state file, which
    raises its TerraformOutputError.
    """
    monkeypatch.delitem(sys.modules, "bridge", raising=False)
    (tmp_path / "bin").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    root = tmp_path / "root"
    root.mkdir()
    assert _teardown("--from-terraform", str(root)) == 0
    ((cmd, env),) = ran
    assert cmd[-1] == "--force"
    assert [name for name in dfeops.TEARDOWN_FACTS if name in env] == []
    err = capsys.readouterr().err
    assert f"could not read the outputs of {root}" in err
    assert "no local state to fall back on" in err


def test_an_error_that_is_not_an_unreadable_root_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, ran: list[tuple[list[str], dict]]
) -> None:
    """Only the bridge's own failure is a root that cannot be read; a bug elsewhere surfaces."""
    _bridge(monkeypatch, KeyError("value"))
    sys.modules["bridge"].TerraformOutputError = type("TerraformOutputError", (RuntimeError,), {})
    with pytest.raises(KeyError):
        _teardown("--from-terraform", "terraform/environments/aws")
    assert ran == []


def test_the_cycle_hands_its_root_to_the_teardown(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []

    def run(cmd: list[str], env: dict | None = None) -> int:
        ran.append(cmd)
        return 0

    monkeypatch.setattr(dfeops, "_resolve_stack", lambda _args: 0)
    monkeypatch.setattr(dfeops, "_run_streaming", run)
    args = dfeops.build_parser().parse_args(
        [
            "cycle",
            "--skip-capacity-check",
            "--skip-preflight",
            "--stack",
            "2.2.0-rc.99",
            "--from-terraform",
            "terraform/environments/aws",
        ]
    )
    dfeops.cmd_cycle(args)
    teardown = next(cmd for cmd in ran if cmd[2] == "teardown")
    assert teardown[teardown.index("--from-terraform") + 1] == "terraform/environments/aws"
    assert teardown[teardown.index("--tf-dir") + 1] == "terraform/environments/aws"
    assert "--with-terraform" in teardown
