#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_tools.py
#  Purpose:      Prove bootstrap/check_tools.py refuses a PATH missing any tool
#                bootstrap.sh or destroy.sh runs, names it and what needs it,
#                and passes a PATH that has them all
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for bootstrap/check_tools.py.

Each test runs the script as a real process with PATH set to a directory of
stand-in executables, so the answer depends on that directory alone and never
on what the machine running the tests has installed.

    python3 -m pytest scripts/tests/test_check_tools.py -q
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap"
CHECK_TOOLS = BOOTSTRAP / "check_tools.py"

_spec = importlib.util.spec_from_file_location("check_tools", CHECK_TOOLS)
check_tools = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_tools)

EVERY_CLOUD = ["kubectl", "helm", "python3", "openssl"]


def _bin(tmp_path: Path, tools: list[str]) -> Path:
    """A directory holding one executable stand-in per tool and nothing else."""
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    for tool in tools:
        stand_in = directory / tool
        stand_in.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stand_in.chmod(0o755)
    return directory


def _check(path: Path, *args: str, cloud: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": str(path)}
    if cloud is not None:
        env["DFE_CLOUD"] = cloud
    return subprocess.run(
        [sys.executable, str(CHECK_TOOLS), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_a_path_with_every_tool_passes_and_names_them(tmp_path: Path) -> None:
    done = _check(_bin(tmp_path, EVERY_CLOUD))
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "deploy tools on PATH: kubectl helm python3 openssl"
    assert done.stderr == ""


@pytest.mark.parametrize("absent", EVERY_CLOUD)
def test_expected_fail_each_missing_tool_is_named_with_what_needs_it(
    tmp_path: Path, absent: str
) -> None:
    done = _check(_bin(tmp_path, [t for t in EVERY_CLOUD if t != absent]))
    assert done.returncode == 1
    named = re.findall(r"^  (\S+): ", done.stderr, re.M)
    assert named == [absent]
    why = dict(check_tools.TOOLS)[absent]
    assert f"  {absent}: {why}" in done.stderr
    assert done.stdout == ""


def test_expected_fail_every_missing_tool_is_named_at_once(tmp_path: Path) -> None:
    done = _check(_bin(tmp_path, ["python3"]))
    assert done.returncode == 1
    assert re.findall(r"^  (\S+): ", done.stderr, re.M) == ["kubectl", "helm", "openssl"]


def test_expected_fail_an_empty_path_names_every_tool(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    done = _check(empty)
    assert done.returncode == 1
    assert re.findall(r"^  (\S+): ", done.stderr, re.M) == EVERY_CLOUD


def test_expected_fail_a_tool_that_is_not_executable_does_not_count(tmp_path: Path) -> None:
    path = _bin(tmp_path, EVERY_CLOUD)
    (path / "helm").chmod(0o644)
    done = _check(path)
    assert done.returncode == 1
    assert re.findall(r"^  (\S+): ", done.stderr, re.M) == ["helm"]


def test_expected_fail_an_aws_deployment_needs_the_aws_cli(tmp_path: Path) -> None:
    done = _check(_bin(tmp_path, EVERY_CLOUD), cloud="aws")
    assert done.returncode == 1
    assert re.findall(r"^  (\S+): ", done.stderr, re.M) == ["aws"]
    assert "destroy.sh step 9" in done.stderr


def test_an_aws_deployment_with_the_aws_cli_passes(tmp_path: Path) -> None:
    done = _check(_bin(tmp_path, [*EVERY_CLOUD, "aws"]), cloud="aws")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "deploy tools on PATH: kubectl helm python3 openssl aws"


@pytest.mark.parametrize("cloud", ["local", "gcp", "azure", ""])
def test_a_cloud_without_its_own_cli_needs_no_aws(tmp_path: Path, cloud: str) -> None:
    assert _check(_bin(tmp_path, EVERY_CLOUD), cloud=cloud).returncode == 0


def test_the_cloud_flag_wins_over_dfe_cloud(tmp_path: Path) -> None:
    path = _bin(tmp_path, EVERY_CLOUD)
    assert _check(path, "--cloud", "local", cloud="aws").returncode == 0
    assert _check(path, "--cloud", "aws", cloud="local").returncode == 1


def test_every_tool_is_one_bootstrap_or_destroy_actually_runs() -> None:
    """A tool on the list that neither script runs would refuse a deploy for nothing."""
    scripts = "\n".join(
        (BOOTSTRAP / name).read_text(encoding="utf-8") for name in ("bootstrap.sh", "destroy.sh")
    )
    listed = [tool for tool, _ in check_tools.TOOLS]
    listed += [tool for tools in check_tools.CLOUD_TOOLS.values() for tool, _ in tools]
    for tool in listed:
        assert re.search(rf"(^|[\s|(;]){re.escape(tool)} ", scripts, re.M), tool


def test_bootstrap_checks_its_tools_before_anything_else_runs() -> None:
    """Anything ahead of the check could reach the cluster with a tool missing."""
    lines = (BOOTSTRAP / "bootstrap.sh").read_text(encoding="utf-8").splitlines()
    code = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    check = code.index('python3 "${SCRIPT_DIR}/check_tools.py"')
    before = code[:check]
    assert before[0] == "set -euo pipefail"
    assert before[1].startswith("SCRIPT_DIR=")
    assert before[2].startswith("command -v python3 ")
    assert len(before) == 3, before
