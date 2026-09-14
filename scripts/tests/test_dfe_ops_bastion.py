#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_bastion.py
#  Purpose:      Guard `dfe-ops bastion` -- the dial edit is surgical, up/down
#                shell out to render_dial.py and tofu in the right order,
#                forward refuses an unknown target and never writes
#                insecure-skip-tls-verify, and down's teardown proof reads
#                every one of the four checks it claims to.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe_ops_bastion.py.

    python3 -m pytest scripts/tests/test_dfe_ops_bastion.py -q

Every AWS CLI call is mocked at aws_cli.run_aws (the same boundary
test_cloud_sweep.py mocks); every tofu/render_dial.py call is mocked at this
module's own `_run` -- no network call and no real account is touched.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import dfe_ops_bastion as bastion  # noqa: E402

# ---------------------------------------------------------------------------
# _set_toolbox_field -- the dial editor
# ---------------------------------------------------------------------------

DIAL_WITH_TOOLBOX = """substrate: k8s

toolbox:
  enabled: "false"
  aws:
    instance_type: t4g.small
    operator_role_arn: ""
  ttl_minutes: 60
  session:
    idle_timeout_minutes: 15
    max_duration_minutes: 240
  session_log_retention_days: 90

kafka:
  provider: strimzi
"""


def test_set_toolbox_field_flips_enabled_and_leaves_everything_else() -> None:
    updated = bastion._set_toolbox_field(DIAL_WITH_TOOLBOX, "enabled", "true")
    assert '  enabled: "true"' in updated
    # Every sibling field, and the block AFTER toolbox:, survive verbatim.
    assert "instance_type: t4g.small" in updated
    assert "ttl_minutes: 60" in updated
    assert "provider: strimzi" in updated


def test_set_toolbox_field_sets_ttl_minutes() -> None:
    updated = bastion._set_toolbox_field(DIAL_WITH_TOOLBOX, "ttl_minutes", "45")
    assert '  ttl_minutes: "45"' in updated


def test_set_toolbox_field_does_not_touch_a_same_named_field_outside_the_block() -> None:
    dial = DIAL_WITH_TOOLBOX + '\nother:\n  enabled: "false"\n'
    updated = bastion._set_toolbox_field(dial, "enabled", "true")
    lines = updated.splitlines()
    toolbox_at = lines.index("toolbox:")
    other_at = lines.index("other:")
    # toolbox.enabled flips; other.enabled (outside the toolbox: block) does not.
    assert lines[toolbox_at + 1] == '  enabled: "true"'
    assert lines[other_at + 1] == '  enabled: "false"'


def test_set_toolbox_field_refuses_a_dial_with_no_toolbox_block() -> None:
    with pytest.raises(bastion.BastionError, match=r"toolbox\.enabled"):
        bastion._set_toolbox_field("substrate: k8s\nkafka:\n  provider: strimzi\n", "enabled", "true")


def test_set_toolbox_field_refuses_an_unknown_field() -> None:
    with pytest.raises(bastion.BastionError, match=r"toolbox\.bogus_field"):
        bastion._set_toolbox_field(DIAL_WITH_TOOLBOX, "bogus_field", "true")


# ---------------------------------------------------------------------------
# Shared mocking helpers
# ---------------------------------------------------------------------------


def _ok(stdout_obj: object = None) -> subprocess.CompletedProcess:
    stdout = json.dumps(stdout_obj) if stdout_obj is not None else ""
    return subprocess.CompletedProcess(args=["aws"], returncode=0, stdout=stdout, stderr="")


def _fail() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["aws"], returncode=1, stdout="", stderr="boom")


def _mock_aws(monkeypatch: pytest.MonkeyPatch, *responses: subprocess.CompletedProcess) -> list[list[str]]:
    """Replace aws_cli.run_aws with one that returns `responses` in call order
    and records each call's argv."""
    queue = list(responses)
    calls: list[list[str]] = []

    def fake_run_aws(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(args)
        return queue.pop(0)

    monkeypatch.setattr(bastion.aws_cli, "run_aws", fake_run_aws)
    return calls


def _mock_run(monkeypatch: pytest.MonkeyPatch, *responses: subprocess.CompletedProcess) -> list[list[str]]:
    """Replace this module's own tofu/render_dial subprocess boundary."""
    queue = list(responses)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return queue.pop(0)

    monkeypatch.setattr(bastion, "_run", fake_run)
    return calls


TOOLBOX_OUTPUTS = {
    "toolbox_instance_id": {"value": "i-0123456789abcdef0"},
    "toolbox_ssm_session_document": {"value": "dfe-test-toolbox-shell"},
    "toolbox_targets": {
        "value": {
            "eks-api": {
                "host": "abc123.gr7.us-west-2.eks.amazonaws.com",
                "port": 443,
                "document_name": "dfe-test-toolbox-forward-eks-api",
            },
            "kafka": {
                "host": "b-1.mock.kafka.us-west-2.amazonaws.com",
                "port": 9096,
                "document_name": "dfe-test-toolbox-forward-kafka",
            },
        }
    },
    "cluster_name": {"value": "dfe-test"},
    "DFE_REGION": {"value": "us-west-2"},
}


def _mock_outputs(monkeypatch: pytest.MonkeyPatch, outputs: dict[str, object] = TOOLBOX_OUTPUTS) -> None:
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: outputs)


# ---------------------------------------------------------------------------
# up
# ---------------------------------------------------------------------------


def _dial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str = DIAL_WITH_TOOLBOX) -> Path:
    dial = tmp_path / "deployment.yaml"
    dial.write_text(text, encoding="utf-8")
    monkeypatch.setattr(bastion, "DIAL", dial)
    return dial


def _args(**overrides: object) -> bastion.argparse.Namespace:
    import argparse

    base: dict[str, object] = {"ttl": None, "wait_timeout": 1.0, "target": None, "local_port": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_up_sets_enabled_applies_and_waits_for_online(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dial = _dial_file(tmp_path, monkeypatch)
    tofu_calls = _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),  # render_dial.py --tofu
        subprocess.CompletedProcess(args=[], returncode=0),  # tofu apply
    )
    _mock_outputs(monkeypatch)
    aws_calls = _mock_aws(
        monkeypatch,
        _ok({"InstanceInformationList": [{"PingStatus": "Online"}]}),
    )

    rc = bastion.cmd_bastion_up(_args())

    assert rc == 0
    assert '  enabled: "true"' in dial.read_text(encoding="utf-8")
    assert any("render_dial.py" in str(part) for part in tofu_calls[0])
    assert "apply" in tofu_calls[1]
    assert "-target=module.toolbox" in tofu_calls[1]
    assert "-target=module.cluster.aws_eks_access_entry.toolbox_operator" in tofu_calls[1]
    assert "-target=module.cluster.aws_eks_access_policy_association.toolbox_operator_view" in tofu_calls[1]
    assert any("describe-instance-information" in call for call in aws_calls)


def test_up_with_ttl_overrides_ttl_minutes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dial = _dial_file(tmp_path, monkeypatch)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    _mock_outputs(monkeypatch)
    _mock_aws(monkeypatch, _ok({"InstanceInformationList": [{"PingStatus": "Online"}]}))

    rc = bastion.cmd_bastion_up(_args(ttl=30))

    assert rc == 0
    assert '  ttl_minutes: "30"' in dial.read_text(encoding="utf-8")


def test_up_fails_when_tofu_apply_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _dial_file(tmp_path, monkeypatch)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=1),
    )

    assert bastion.cmd_bastion_up(_args()) == 1


def test_up_fails_when_the_instance_never_goes_online(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dial_file(tmp_path, monkeypatch)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    _mock_outputs(monkeypatch)
    # A deterministic clock: three polls, then the deadline (10) is reached.
    # time.sleep is a no-op so the bounded wait loop runs instantly under test.
    clock = iter([0, 1, 2, 11])
    monkeypatch.setattr(bastion.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(bastion.time, "sleep", lambda _seconds: None)
    _mock_aws(monkeypatch, *([_ok({"InstanceInformationList": []})] * 3))

    assert bastion.cmd_bastion_up(_args(wait_timeout=10.0)) == 1


def test_up_refuses_a_dial_with_no_toolbox_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _dial_file(tmp_path, monkeypatch, text="substrate: k8s\nkafka:\n  provider: strimzi\n")
    assert bastion.cmd_bastion_up(_args()) == 1


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------


def test_shell_invokes_start_session_with_the_shell_document(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_outputs(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(bastion, "_run_interactive", lambda cmd: calls.append(cmd) or 0)

    rc = bastion.cmd_bastion_shell(_args())

    assert rc == 0
    assert calls[0] == [
        "aws", "ssm", "start-session",
        "--target", "i-0123456789abcdef0",
        "--document-name", "dfe-test-toolbox-shell",
    ]


def test_shell_refuses_when_no_instance_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_outputs(monkeypatch, {})
    assert bastion.cmd_bastion_shell(_args()) == 1


# ---------------------------------------------------------------------------
# forward
# ---------------------------------------------------------------------------


def test_forward_invokes_start_session_with_the_named_targets_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_outputs(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(bastion, "_run_interactive", lambda cmd: calls.append(cmd) or 0)

    rc = bastion.cmd_bastion_forward(_args(target="kafka", local_port=19096))

    assert rc == 0
    assert calls[0] == [
        "aws", "ssm", "start-session",
        "--target", "i-0123456789abcdef0",
        "--document-name", "dfe-test-toolbox-forward-kafka",
        "--parameters", "localPortNumber=19096",
    ]


def test_forward_refuses_an_unknown_target(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    _mock_outputs(monkeypatch)
    rc = bastion.cmd_bastion_forward(_args(target="not-a-real-target", local_port=8080))
    assert rc == 1
    assert "unknown target" in capsys.readouterr().err


def test_forward_prints_that_the_session_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _mock_outputs(monkeypatch)
    monkeypatch.setattr(bastion, "_run_interactive", lambda cmd: 0)
    bastion.cmd_bastion_forward(_args(target="kafka", local_port=19096))
    assert "NOT recorded" in capsys.readouterr().err


def test_forward_to_eks_api_writes_a_0600_kubeconfig_with_no_insecure_skip_tls_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_outputs(monkeypatch)
    monkeypatch.setattr(bastion, "_run_interactive", lambda cmd: 0)
    scratch = tmp_path / "toolbox-eks-api.kubeconfig"
    monkeypatch.setattr(bastion, "SCRATCH_KUBECONFIG", scratch)

    bastion.cmd_bastion_forward(_args(target="eks-api", local_port=18443))

    assert scratch.exists()
    mode = scratch.stat().st_mode & 0o777
    assert mode == 0o600
    content = scratch.read_text(encoding="utf-8")
    assert "server: https://127.0.0.1:18443" in content
    assert "tls-server-name: abc123.gr7.us-west-2.eks.amazonaws.com" in content
    assert "insecure-skip-tls-verify" not in content
    assert "--cluster-name" in content
    assert "dfe-test" in content


def test_forward_to_kafka_writes_no_kubeconfig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_outputs(monkeypatch)
    monkeypatch.setattr(bastion, "_run_interactive", lambda cmd: 0)
    scratch = tmp_path / "toolbox-eks-api.kubeconfig"
    monkeypatch.setattr(bastion, "SCRATCH_KUBECONFIG", scratch)

    bastion.cmd_bastion_forward(_args(target="kafka", local_port=19096))

    assert not scratch.exists()


# ---------------------------------------------------------------------------
# down -- the teardown proof
# ---------------------------------------------------------------------------


def test_down_flips_enabled_applies_and_proves_clean_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dial = _dial_file(
        tmp_path,
        monkeypatch,
        text=DIAL_WITH_TOOLBOX.replace('enabled: "false"', 'enabled: "true"'),
    )
    # Outputs BEFORE the apply (still carries the instance id) are read once,
    # then render_dial.py + tofu apply run.
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: TOOLBOX_OUTPUTS)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    aws_calls = _mock_aws(
        monkeypatch,
        _ok(["terminated"]),  # describe-instances
        _ok({"Sessions": []}),  # describe-sessions
        _ok([]),  # describe-volumes
        _ok([]),  # describe-network-interfaces
    )

    rc = bastion.cmd_bastion_down(_args())

    assert rc == 0
    assert '  enabled: "false"' in dial.read_text(encoding="utf-8")
    assert len(aws_calls) == 4


def test_down_reports_non_zero_when_a_volume_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dial_file(tmp_path, monkeypatch, text=DIAL_WITH_TOOLBOX.replace('enabled: "false"', 'enabled: "true"'))
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: TOOLBOX_OUTPUTS)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    _mock_aws(
        monkeypatch,
        _ok(["terminated"]),
        _ok({"Sessions": []}),
        _ok(["vol-0123456789abcdef0"]),  # a volume survived
        _ok([]),
    )

    assert bastion.cmd_bastion_down(_args()) == 1


def test_down_reports_non_zero_when_the_instance_has_not_terminated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dial_file(tmp_path, monkeypatch, text=DIAL_WITH_TOOLBOX.replace('enabled: "false"', 'enabled: "true"'))
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: TOOLBOX_OUTPUTS)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    _mock_aws(
        monkeypatch,
        _ok(["shutting-down"]),  # not yet terminated
        _ok({"Sessions": []}),
        _ok([]),
        _ok([]),
    )

    assert bastion.cmd_bastion_down(_args()) == 1


def test_down_reports_non_zero_on_an_active_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _dial_file(tmp_path, monkeypatch, text=DIAL_WITH_TOOLBOX.replace('enabled: "false"', 'enabled: "true"'))
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: TOOLBOX_OUTPUTS)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    _mock_aws(
        monkeypatch,
        _ok(["terminated"]),
        _ok({"Sessions": [{"SessionId": "s-1"}]}),
        _ok([]),
        _ok([]),
    )

    assert bastion.cmd_bastion_down(_args()) == 1


def test_down_removes_the_scratch_kubeconfig_if_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dial_file(tmp_path, monkeypatch, text=DIAL_WITH_TOOLBOX.replace('enabled: "false"', 'enabled: "true"'))
    monkeypatch.setattr(bastion, "_tofu_outputs", lambda: TOOLBOX_OUTPUTS)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    _mock_aws(monkeypatch, _ok(["terminated"]), _ok({"Sessions": []}), _ok([]), _ok([]))

    scratch = tmp_path / "toolbox-eks-api.kubeconfig"
    scratch.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(bastion, "SCRATCH_KUBECONFIG", scratch)

    bastion.cmd_bastion_down(_args())

    assert not scratch.exists()


def test_down_with_no_prior_instance_reports_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """bastion down on a toolbox that was never brought up: nothing to prove
    wrong, and the two instance-scoped checks are never even queried."""
    _dial_file(tmp_path, monkeypatch)
    monkeypatch.setattr(bastion, "_tofu_outputs", dict)
    _mock_run(
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=0),
        subprocess.CompletedProcess(args=[], returncode=0),
    )
    aws_calls = _mock_aws(monkeypatch, _ok([]), _ok([]))

    assert bastion.cmd_bastion_down(_args()) == 0
    assert len(aws_calls) == 2  # volumes + network interfaces only


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_reports_not_enabled_with_no_instance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _mock_outputs(monkeypatch, {})
    assert bastion.cmd_bastion_status(_args()) == 0
    assert "not enabled" in capsys.readouterr().err


def test_status_reports_ping_and_active_sessions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _mock_outputs(monkeypatch)
    _mock_aws(
        monkeypatch,
        _ok({"InstanceInformationList": [{"PingStatus": "Online"}]}),
        _ok({"Sessions": [{"SessionId": "s-1"}]}),
    )

    rc = bastion.cmd_bastion_status(_args())
    err = capsys.readouterr().err

    assert rc == 0
    assert "Online" in err
    assert "active sessions: 1" in err
    assert "eks-api" in err
    assert "kafka" in err
