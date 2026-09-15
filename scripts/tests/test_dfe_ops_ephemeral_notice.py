#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_ephemeral_notice.py
#  Purpose:      Prove every dfe-ops command against an ephemeral deployment
#                reports how long it has been up and what its compute costs,
#                and that a persistent one gets no such line.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The ephemeral-deployment banner.

An ephemeral deployment is a create-prove-destroy cycle, and what decides
whether to keep proving is how long it has been up against what it costs an
hour. Both readings come off disk -- a local backend's own state file, and the
resolver's `sizing/resolved.yaml` -- so this calls no cloud API, and neither
reading being absent may fail a command. A remote backend leaves no apply-time
file in the tree, so the age is reported as unavailable rather than taken from
`.terraform/terraform.tfstate`, whose mtime dates the last `tofu init`.

    python3 -m pytest scripts/tests/test_dfe_ops_ephemeral_notice.py -q
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_ephemeral", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_ephemeral", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_ephemeral"] = dfeops
_loader.exec_module(dfeops)

DIAL = """substrate: k8s
metadata:
  name: dfe
target:
  provision:
    cloud: aws
    region: us-west-2
tags:
  service-name: dfe
  lifecycle: {lifecycle}
"""

RESOLVED = """## Written by scripts/resolve_sizing.py -- do not edit by hand.
tier: scale
compute_usd_per_hour: 4.2117
locked:
  partition_count: 170
"""


def _deployment(tmp_path: Path, monkeypatch, *, lifecycle: str, age_hours: float | None,
                resolved: bool) -> None:
    dial = tmp_path / "deployment.yaml"
    dial.write_text(DIAL.format(lifecycle=lifecycle), encoding="utf-8")
    monkeypatch.setattr(dfeops, "DIAL", dial)

    environments = tmp_path / "terraform" / "environments"
    monkeypatch.setattr(dfeops, "TF_ENVIRONMENTS", environments)
    if age_hours is not None:
        state = environments / "aws" / "terraform.tfstate"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("{}\n", encoding="utf-8")
        then = time.time() - age_hours * 3600
        os.utime(state, (then, then))

    sizing = tmp_path / "sizing" / "resolved.yaml"
    monkeypatch.setattr(dfeops, "SIZING_RESOLVED", sizing)
    if resolved:
        sizing.parent.mkdir(parents=True, exist_ok=True)
        sizing.write_text(RESOLVED, encoding="utf-8")


def test_an_ephemeral_deployment_reports_its_age_and_its_rate(tmp_path: Path, monkeypatch) -> None:
    _deployment(tmp_path, monkeypatch, lifecycle="ephemeral", age_hours=2.75, resolved=True)
    notice = dfeops._ephemeral_notice()
    assert notice is not None
    assert "ephemeral deployment (aws)" in notice
    assert "last apply 2h45m ago" in notice
    assert "4.2117 USD/hour" in notice


def test_a_reinit_does_not_move_the_reported_age(tmp_path: Path, monkeypatch) -> None:
    """The property, not the mechanism: `tofu init` rewrites the backend record,
    so an age taken from it resets on every cycle and on every fresh clone."""
    _deployment(tmp_path, monkeypatch, lifecycle="ephemeral", age_hours=9.0, resolved=True)
    before = dfeops._ephemeral_notice()
    record = tmp_path / "terraform" / "environments" / "aws" / ".terraform" / "terraform.tfstate"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{}\n", encoding="utf-8")
    assert dfeops._ephemeral_notice() == before
    assert "last apply 9h00m ago" in before


def test_a_persistent_deployment_gets_no_line(tmp_path: Path, monkeypatch) -> None:
    """The banner is for the deployments meant to be torn down, not every one."""
    _deployment(tmp_path, monkeypatch, lifecycle="persistent", age_hours=2.0, resolved=True)
    assert dfeops._ephemeral_notice() is None


def test_a_missing_state_or_resolve_still_answers(tmp_path: Path, monkeypatch) -> None:
    """Neither reading may fail a command -- an unknown is said, not raised."""
    _deployment(tmp_path, monkeypatch, lifecycle="ephemeral", age_hours=None, resolved=False)
    notice = dfeops._ephemeral_notice()
    assert notice is not None
    assert "age unavailable (remote state)" in notice
    assert "compute rate not resolved" in notice


def test_no_dial_means_no_line(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dfeops, "DIAL", tmp_path / "absent.yaml")
    assert dfeops._ephemeral_notice() is None


def test_a_dial_that_provisions_nothing_gets_no_line(tmp_path: Path, monkeypatch) -> None:
    """target.existing runs no tofu, so there is no environment to read and no
    reason to stat one an unrelated deployment happens to have left behind."""
    dial = tmp_path / "deployment.yaml"
    dial.write_text(
        "substrate: k8s\ntarget:\n  existing:\n    kubeconfig: x\ntags:\n  lifecycle: ephemeral\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dfeops, "DIAL", dial)
    assert dfeops._ephemeral_notice() is None


def test_the_committed_dial_template_parses(monkeypatch) -> None:
    """The banner reads the real dial, so the reader has to cope with the whole
    committed template, not just the slice this file's fixture carries."""
    monkeypatch.setattr(dfeops, "DIAL", REPO_ROOT / "deployment.example.yaml")
    tree = dfeops._dial_tree()
    assert tree, "the committed dial template must parse"
    assert dfeops.yaml_subset.at(tree, ("tags", "lifecycle")) == "ephemeral"
