#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_karpenter_ami.py
#  Purpose:      Prove the Karpenter AMI lag check names the lag, warns rather
#                than fails, and reads the stack's minor the way bootstrap does.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/check_karpenter_ami.py.

``eks-ami-releases.json`` is the verbatim answer of the check's own ``gh api``
call (RELEASES_JQ) for awslabs/amazon-eks-ami, captured 2026-10-18. The ssm
case reads the recorded EKS ``recommended`` parameter test_node_images.py uses.

    python3 -m pytest scripts/tests/test_check_karpenter_ami.py -q
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_karpenter_ami as check  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "node-images"
RELEASES = FIXTURES / "eks-ami-releases.json"


def _versions(tmp_path: Path, pin: str = "v20261003", platform: str = "1.34-1.36") -> Path:
    path = tmp_path / "versions.yaml"
    path.write_text(
        'current: "9.9.9"\n'
        "stacks:\n"
        "  9.9.9:\n"
        "    platform:\n"
        f'      kubernetes: "{platform}"\n'
        "    operators:\n"
        f'      karpenter-al2023-ami: "{pin}"\n',
        encoding="utf-8",
    )
    return path


def _run(*argv: str) -> int:
    return check.main(list(argv))


def test_the_lag_names_the_pin_and_the_newest_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """v20261012 shipped for 1.36 on 2026-10-14, nine days of releases after v20261003."""
    assert _run("--versions", str(_versions(tmp_path)), "--releases-json", str(RELEASES)) == 0
    err = capsys.readouterr().err
    assert "WARNING stack 9.9.9 pins operators.karpenter-al2023-ami v20261003, 9 day(s) behind" in err
    assert "newest AL2023 release for Kubernetes 1.36, v20261012 (github, published 2026-10-14)" in err


def test_the_real_stack_reads_against_the_recorded_releases(capsys: pytest.CaptureFixture[str]) -> None:
    """Whatever the current stack pins, the check names it, and only a pin behind v20261012 warns."""
    _stack, pin, _minor = check.stack_facts(check.VERSIONS_FILE, None)
    assert _run("--releases-json", str(RELEASES)) == 0
    captured = capsys.readouterr()
    if pin < "v20261012":
        assert f"pins operators.karpenter-al2023-ami {pin}," in captured.err
    else:
        assert "OK stack" in captured.out
        assert pin in captured.out


def test_the_ssm_source_reports_the_same_lag(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run("--source", "ssm", "--region", "us-west-2", "--fixtures", str(FIXTURES)) == 0
    err = capsys.readouterr().err
    assert "v20261003, 9 day(s) behind" in err
    assert "v20261012 (ssm us-west-2)" in err


def test_a_lag_is_an_annotation_under_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert _run("--versions", str(_versions(tmp_path)), "--releases-json", str(RELEASES)) == 0
    assert capsys.readouterr().out.startswith("::warning::stack 9.9.9 pins")


def test_a_current_pin_is_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    versions = _versions(tmp_path, pin="v20261012")
    assert _run("--versions", str(versions), "--releases-json", str(RELEASES)) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("check_karpenter_ami: OK stack 9.9.9 pins v20261012")
    assert "WARNING" not in captured.err


def test_the_minor_is_the_platform_ceiling() -> None:
    _stack, _pin, minor = check.stack_facts(check.VERSIONS_FILE, None)
    assert minor == "1.36"


def test_a_floor_with_no_ceiling_is_the_minor(tmp_path: Path) -> None:
    _stack, _pin, minor = check.stack_facts(_versions(tmp_path, platform=">=1.35"), None)
    assert minor == "1.35"


def test_a_minor_is_measured_against_the_releases_that_ship_it() -> None:
    """The recorded v20260917 ships no 1.37 image, so it never answers for 1.37."""
    releases = json.loads(RELEASES.read_text(encoding="utf-8"))
    older = [r for r in releases if r["tag_name"] <= "v20260923"]
    newest = check.newest_from_releases(older, "1.37", "arm64")
    assert newest is not None
    assert newest.tag == "v20260923"
    assert check.newest_from_releases(older, "1.38", "arm64") is None


def test_a_draft_or_prerelease_is_never_the_newest() -> None:
    releases = json.loads(RELEASES.read_text(encoding="utf-8"))
    releases[0]["prerelease"] = True
    newest = check.newest_from_releases(releases, "1.36", "arm64")
    assert newest is not None
    assert newest.tag == "v20261003"


def test_expected_fail_an_unreadable_source_is_a_loud_skip_and_not_a_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "no-answers"
    empty.mkdir()
    assert _run("--source", "ssm", "--fixtures", str(empty)) == 0
    assert "This is a skip, not a pass." in capsys.readouterr().err


def test_expected_fail_a_pin_that_is_not_dated_warns_and_still_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    versions = _versions(tmp_path, pin="latest")
    assert _run("--versions", str(versions), "--releases-json", str(RELEASES)) == 0
    assert "pins operators.karpenter-al2023-ami 'latest', not v<YYYYMMDD>" in capsys.readouterr().err


def test_expected_fail_a_stack_with_no_pin_warns_and_still_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    versions = tmp_path / "versions.yaml"
    versions.write_text(
        'current: "9.9.9"\nstacks:\n  9.9.9:\n    platform:\n      kubernetes: "1.36"\n', encoding="utf-8"
    )
    assert _run("--versions", str(versions), "--releases-json", str(RELEASES)) == 0
    assert "carries no operators.karpenter-al2023-ami pin" in capsys.readouterr().err


def test_the_check_runs_in_ci_as_a_step_that_cannot_fail_the_job() -> None:
    """Wired beside the other pin checks, and advisory by its own exit status rather than `|| true`."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "helm-lint.yml").read_text(encoding="utf-8")
    step = workflow[workflow.index("name: Karpenter AMI alias currency (advisory)") :]
    step = step[: step.index("\n      - name:")]
    assert "run: python3 scripts/check_karpenter_ami.py" in step
    assert "GH_TOKEN: ${{ github.token }}" in step
    assert "|| true" not in step
    assert "continue-on-error" not in step
