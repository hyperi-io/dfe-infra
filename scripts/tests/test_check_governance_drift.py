#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_governance_drift.py
#  Purpose:      Prove the governance drift check resolves a dial against the
#                values the thin charts read, and fails on a key nothing reads.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""scripts/check_governance_drift.py on actions written here, against this tree's real layers.

    python3 -m pytest scripts/tests/test_check_governance_drift.py -q

The resolution sources are the committed ones (argocd/values, dfe-extras' defaults, the
contract fixtures), so each case names a dial those hold today. A dial only the 2.2.0 chart
holds comes from a chart directory written here.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "check_governance_drift.py"
spec = importlib.util.spec_from_file_location("governance_drift", SCRIPT)
drift = importlib.util.module_from_spec(spec)
sys.modules["governance_drift"] = drift
spec.loader.exec_module(drift)


def check(tmp_path: Path, changes: list[dict], *args: str) -> int:
    """Exit code of the check on one action holding ``changes``."""
    actions = tmp_path / "governance" / "actions"
    actions.mkdir(parents=True, exist_ok=True)
    doc = {"name": "probe", "changes": changes}
    (actions / "probe.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return drift.main(["--deploy-root", str(tmp_path), "--require", *args])


def dial(name: str, path: str, value: object = True) -> dict:
    return {"cls": "helmvars", "name": name, "path": path, "value": value}


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("dfe-engine-default-values", "huntRunner.enabled"),
        ("dfe-engine-default-values", "huntRunner.cap"),
        ("dfe-receiver-default-values", "keda.maxReplicaCount"),
        ("dfe-engine-default-values", "secrets.hunt-runner.enabled"),
        ("dfe-engine-default-values", "secrets.hunt-runner.keys.password"),
    ],
)
def test_a_dial_the_thin_path_reads_resolves(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, path: str
) -> None:
    assert check(tmp_path, [dial(name, path)]) == 0
    assert "[ok]" in capsys.readouterr().err


@pytest.mark.parametrize(
    "path",
    ["huntRunner.cpa", "secrets.no-such-group.enabled", "secrets.hunt-runner.enable"],
    ids=["typo", "group-not-in-contract", "key-the-library-does-not-read"],
)
def test_a_dial_nothing_reads_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    assert check(tmp_path, [dial("dfe-engine-default-values", path)]) == 1
    assert f"holds {path!r}" in capsys.readouterr().err


def test_a_key_only_the_2_2_0_chart_holds_is_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chart = tmp_path / "charts" / "dfe-engine"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text("name: dfe-engine\nversion: 0.0.0\n", encoding="utf-8")
    (chart / "values.yaml").write_text("legacyOnly: true\n", encoding="utf-8")
    code = check(
        tmp_path,
        [dial("dfe-engine-default-values", "legacyOnly")],
        "--charts-dir",
        str(tmp_path / "charts"),
    )
    err = capsys.readouterr().err
    assert (code, "[warn]" in err, "1 warning(s)" in err) == (0, True, True)


def test_a_service_with_no_thin_chart_still_resolves_against_its_chart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chart = tmp_path / "charts" / "edge-only"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text("name: edge-only\nversion: 0.0.0\n", encoding="utf-8")
    (chart / "values.yaml").write_text("replicas: 2\n", encoding="utf-8")
    code = check(
        tmp_path,
        [dial("edge-only-default-values", "replicas", 3)],
        "--charts-dir",
        str(tmp_path / "charts"),
    )
    assert (code, "[ok]" in capsys.readouterr().err) == (0, True)


def test_a_name_that_matches_no_service_fails(tmp_path: Path) -> None:
    assert check(tmp_path, [dial("no-such-service-default-values", "x")]) == 1
