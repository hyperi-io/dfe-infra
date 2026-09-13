#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_init.py
#  Purpose:      Guard `dfe-ops init` -- the wizard writes a dial render_dial.py
#                actually accepts, every non-trivial answer is validated by
#                CALLING the renderer's own functions rather than a second
#                copy of their rules, the OIDC and overwrite refusals hold,
#                and a bad answer re-asks interactively but refuses outright
#                through --answers.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe_ops_init.py.

    python3 -m pytest scripts/tests/test_dfe_ops_init.py -q

Two levels: whitebox calls into dfe_ops_init's own functions (the fast,
focused majority), and a handful of real subprocess calls into
render_dial.py / resolve_sizing.py so "the dial render_dial.py accepts" is
proven against the actual renderer, not a mocked one.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import dfe_ops_init as wiz  # noqa: E402

FIXTURES = SCRIPTS / "tests" / "fixtures" / "sizing"

ONPREM_ANSWERS = {
    "name": "dfe-test",
    "target": "on-prem",
    "existing_cluster_ref": "rke2-devex",
    "existing_namespace": "dfe",
    "profile": "single",
    "ingest_gb_per_day": "",
    "focus": "economy",
    "kafka_provider": "strimzi",
    "ui_dfe_ui": "n",
    "ui_kafbat": "n",
    "ui_cruise_control": "n",
    "ui_hyperdx": "n",
    "ui_argocd": "n",
    "ui_links": "n",
    "lifecycle": "ephemeral",
    "az_count": "3",
    "toolbox_pod_enabled": "n",
}

AWS_MSK_ANSWERS = {
    "name": "dfe-test-aws",
    "target": "aws",
    "aws_account": "123456789012",
    "aws_region": "us-west-2",
    "aws_cidr": "10.42.0.0/16",
    "profile": "single",
    "ingest_gb_per_day": "500",
    "focus": "economy",
    "kafka_provider": "msk",
    "msk_broker_count": "3",
    "ui_dfe_ui": "y",
    "ui_kafbat": "n",
    "ui_cruise_control": "n",
    "ui_hyperdx": "n",
    "ui_argocd": "n",
    "ui_links": "n",
    "oidc_confirmed": "y",
    "public_zone": "dfe.example.com",
    "telemetry_sink": "otel",
    "lifecycle": "ephemeral",
    "az_count": "3",
    "toolbox_pod_enabled": "n",
    "toolbox_aws_enabled": "n",
}


def _fill_estate_fields(text: str) -> str:
    """What a thin caller (or an operator by hand) fills in after the wizard --
    deployment.example.yaml ships these blank on purpose, so a test proving
    `render_dial.py --tofu` accepts the dial supplies them the same way."""
    text = text.replace('repo_url: ""', 'repo_url: "https://example.invalid/deploy.git"')
    text = text.replace('bucket: ""', 'bucket: "dfe-test-state-bucket"')
    text = text.replace("region: \"\"\n", "region: us-west-2\n", 1)  # state.region only
    return text


# ---------------------------------------------------------------------------
# End-to-end: the full flow through --answers, producing a dial the real
# renderer accepts.
# ---------------------------------------------------------------------------


def test_onprem_strimzi_answers_produce_a_dial_render_dial_accepts(tmp_path: Path) -> None:
    io = wiz.WizardIO(answers=dict(ONPREM_ANSWERS))
    answers = wiz.run_wizard(io)
    text = wiz.build_dial_text(answers)

    dial_path = tmp_path / "deployment.yaml"
    dial_path.write_text(text, encoding="utf-8")
    env_path = tmp_path / "bootstrap.env"

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_dial.py"), "--dial", str(dial_path), "--out", str(env_path)],
        capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert env_path.is_file()
    env_text = env_path.read_text(encoding="utf-8")
    assert 'DFE_KUBE_CONTEXT="rke2-devex"' in env_text
    assert 'DFE_NAMESPACE="dfe"' in env_text


def test_aws_msk_answers_produce_a_tofu_dial_once_estate_fields_are_filled(tmp_path: Path) -> None:
    io = wiz.WizardIO(answers=dict(AWS_MSK_ANSWERS))
    answers = wiz.run_wizard(io)
    text = wiz.build_dial_text(answers)

    dial_path = tmp_path / "deployment.yaml"
    dial_path.write_text(text, encoding="utf-8")
    tfvars_path = tmp_path / "dial.auto.tfvars.json"

    # Before estate fields are supplied, the renderer refuses by NAME on the
    # one field the wizard deliberately leaves blank -- never a surprise.
    refused = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_dial.py"), "--dial", str(dial_path), "--tofu",
         "--out", str(tfvars_path)],
        capture_output=True, text=True,
    )
    assert refused.returncode == 1
    assert "k8s.repo_url" in refused.stderr

    dial_path.write_text(_fill_estate_fields(text), encoding="utf-8")
    accepted = subprocess.run(
        [sys.executable, str(SCRIPTS / "render_dial.py"), "--dial", str(dial_path), "--tofu",
         "--out", str(tfvars_path)],
        capture_output=True, text=True,
    )
    assert accepted.returncode == 0, accepted.stderr
    tfvars = json.loads(tfvars_path.read_text(encoding="utf-8"))
    assert tfvars["kafka"]["provider"] == "msk"
    assert tfvars["kafka"]["msk"]["broker_count"] == 3
    assert tfvars["network"]["az_count"] == 3


# ---------------------------------------------------------------------------
# The OIDC refusal
# ---------------------------------------------------------------------------


def test_public_ui_without_oidc_confirmation_refuses() -> None:
    answers = dict(ONPREM_ANSWERS)
    answers["ui_dfe_ui"] = "y"  # public, and oidc_confirmed left unset (default false)
    io = wiz.WizardIO(answers=answers)

    with pytest.raises(wiz.InitError, match="OIDC"):
        wiz.run_wizard(io)


def test_no_public_ui_needs_no_oidc_confirmation() -> None:
    io = wiz.WizardIO(answers=dict(ONPREM_ANSWERS))  # every ui_* flag is "n"
    answers = wiz.run_wizard(io)
    assert not any(answers.ui_public.values())


# ---------------------------------------------------------------------------
# The overwrite refusal
# ---------------------------------------------------------------------------


def test_cmd_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    out = tmp_path / "deployment.yaml"
    out.write_text("# pre-existing\n", encoding="utf-8")
    answers_file = tmp_path / "answers.env"
    answers_file.write_text(_answers_as_file(ONPREM_ANSWERS), encoding="utf-8")

    rc = wiz.cmd_init(_args(out=str(out), answers=str(answers_file)))

    assert rc == 2
    assert out.read_text(encoding="utf-8") == "# pre-existing\n"  # untouched


def test_cmd_init_overwrites_with_force(tmp_path: Path) -> None:
    out = tmp_path / "deployment.yaml"
    out.write_text("# pre-existing\n", encoding="utf-8")
    answers_file = tmp_path / "answers.env"
    answers_file.write_text(_answers_as_file(ONPREM_ANSWERS), encoding="utf-8")

    rc = wiz.cmd_init(_args(out=str(out), answers=str(answers_file), force=True))

    assert rc == 0
    assert "substrate: k8s" in out.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# --dry-run writes nothing
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    out = tmp_path / "deployment.yaml"
    answers_file = tmp_path / "answers.env"
    answers_file.write_text(_answers_as_file(ONPREM_ANSWERS), encoding="utf-8")

    rc = wiz.cmd_init(_args(out=str(out), answers=str(answers_file), dry_run=True))

    assert rc == 0
    assert not out.exists()
    assert "substrate: k8s" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# An invalid answer re-asks interactively, and refuses outright non-interactively
# ---------------------------------------------------------------------------


def test_invalid_kafka_provider_is_reasked_interactively() -> None:
    answers = wiz.Answers(target="on-prem")
    # "strimzi" (not "msk"/"confluent-cloud"/"redpanda-cloud") so the wizard
    # asks nothing further -- the re-ask itself is what this test proves.
    responses = iter(["bogus-provider", "strimzi"])
    messages: list[str] = []
    io = wiz.WizardIO(read_line=lambda _prompt: next(responses), write_out=messages.append)

    wiz.step_kafka(io, answers)

    assert answers.kafka_provider == "strimzi"
    assert any("bogus-provider" in m for m in messages)


def test_invalid_kafka_provider_refuses_immediately_through_answers() -> None:
    io = wiz.WizardIO(answers={"kafka_provider": "not-a-real-provider"})
    answers = wiz.Answers(target="on-prem")

    with pytest.raises(wiz.InitError, match="not-a-real-provider"):
        wiz.step_kafka(io, answers)


# ---------------------------------------------------------------------------
# Validator reuse -- the wizard calls the renderer's own functions
# ---------------------------------------------------------------------------


def test_az_count_uses_render_dial_validator() -> None:
    io = wiz.WizardIO(answers={"az_count": "7"})  # render_dial._az_count caps at 6
    answers = wiz.Answers()
    with pytest.raises(wiz.InitError, match="between 2 and 6"):
        wiz.step_lifecycle(io, answers)


def test_telemetry_sink_uses_render_dial_validator() -> None:
    io = wiz.WizardIO(answers={"telemetry_sink": "splunk"})
    answers = wiz.Answers(target="aws")
    with pytest.raises(wiz.InitError, match="otel or cloudwatch"):
        wiz.step_telemetry(io, answers)


def test_ingest_gb_per_day_uses_resolve_sizing_validator() -> None:
    io = wiz.WizardIO(answers={"ingest_gb_per_day": "not-a-number"})
    answers = wiz.Answers()
    with pytest.raises(wiz.InitError):
        wiz.step_tier(io, answers)


def test_ingest_gb_per_day_blank_means_no_estimate() -> None:
    io = wiz.WizardIO(answers={"profile": "single", "focus": "economy", "ingest_gb_per_day": ""})
    answers = wiz.Answers()
    wiz.step_tier(io, answers)
    assert answers.ingest_gb_per_day == ""


# ---------------------------------------------------------------------------
# The default Kafka provider per tier/target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "tier", "expected"),
    [
        ("on-prem", "slim", "strimzi"),
        ("on-prem", "scale", "strimzi"),
        ("aws", "slim", "msk"),
        ("aws", "single", "msk"),
        ("aws", "scale", "confluent-cloud"),
    ],
)
def test_default_kafka_provider(target: str, tier: str, expected: str) -> None:
    assert wiz._default_kafka_provider(target, tier) == expected


# ---------------------------------------------------------------------------
# Sizing overrides -- collected via --answers, validated through the SAME
# function resolve_sizing.py's own read_dial calls.
# ---------------------------------------------------------------------------


def test_overrides_from_answers_collects_dotted_keys() -> None:
    raw = {
        "override.kafka-broker.cpu": "16",
        "override.kafka-broker.memory": "64",
        "override.clickhouse.instance_type": "r9gd.4xlarge",
        "unrelated": "ignored",
    }
    collected = wiz._overrides_from_answers(raw)
    assert collected == {
        "kafka-broker": {"cpu": "16", "memory": "64"},
        "clickhouse": {"instance_type": "r9gd.4xlarge"},
    }


def test_step_overrides_refuses_an_unknown_workload() -> None:
    io = wiz.WizardIO(answers={"override.not-a-workload.cpu": "16"})
    answers = wiz.Answers()
    with pytest.raises(wiz.InitError, match="not a use case"):
        wiz.step_overrides(io, answers)


def test_step_overrides_refuses_an_unknown_field() -> None:
    io = wiz.WizardIO(answers={"override.kafka-broker.bogus_field": "16"})
    answers = wiz.Answers()
    with pytest.raises(wiz.InitError, match="not a field"):
        wiz.step_overrides(io, answers)


def test_clickhouse_instance_override_folds_into_sizing_overrides() -> None:
    answers = wiz.Answers(clickhouse_instance_override="r9gd.4xlarge")
    text = wiz.build_dial_text(answers)
    assert "clickhouse:\n      instance_type: r9gd.4xlarge" in text


# ---------------------------------------------------------------------------
# run_resolve -- scale tier only, and the report survives a fatal finding
# ---------------------------------------------------------------------------


def test_run_resolve_skips_non_scale_tiers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dial = tmp_path / "deployment.yaml"
    dial.write_text(wiz.build_dial_text(wiz.Answers(profile="single")), encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(wiz, "_run", lambda cmd, **_kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))

    rc = wiz.run_resolve(dial, fixtures=FIXTURES, live=False, out_dir=tmp_path)

    assert rc == 0
    assert calls == []


def test_run_resolve_prints_the_report_even_on_a_fatal_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    dial = tmp_path / "deployment.yaml"
    dial.write_text(wiz.build_dial_text(wiz.Answers(profile="scale")), encoding="utf-8")
    report_dir = tmp_path / "sizing"
    report_dir.mkdir()
    (report_dir / "scale.report.md").write_text(
        "| **total compute** | | | | | | | **1,234** |\n", encoding="utf-8",
    )

    def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="resolve_sizing: A4 fatal\n")

    monkeypatch.setattr(wiz, "_run", fake_run)

    rc = wiz.run_resolve(dial, fixtures=FIXTURES, live=False, out_dir=tmp_path)

    assert rc == 1
    err = capsys.readouterr().err
    assert "total compute" in err
    assert "1,234" in err


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _answers_as_file(answers: dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in answers.items()) + "\n"


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "out": str(wiz.DEFAULT_DIAL_OUT), "force": False, "answers": None, "dry_run": False,
        "fixtures": None, "live": False, "sizing_out": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)
