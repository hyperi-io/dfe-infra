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
hour. The age is the cluster's own creation stamp, read from the root's
`cluster_created_at` output, because nothing in the tree dates a deployment: a
local state file's mtime dates the last apply, and an S3 backend leaves none
here at all. The output is stubbed here -- what these prove is that both
spellings the provider renders a stamp in are read, that an absent or
unreadable one is said rather than guessed, and that neither reading may fail
a command.

    python3 -m pytest scripts/tests/test_dfe_ops_ephemeral_notice.py -q
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import time
import types
from datetime import UTC, datetime
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


def _stamp(age_hours: float, spelling: str = "rfc3339") -> str:
    """A creation stamp `age_hours` old, in either spelling the provider renders."""
    then = datetime.fromtimestamp(time.time() - age_hours * 3600, tz=UTC)
    if spelling == "go":
        return then.strftime("%Y-%m-%d %H:%M:%S.123456789 +0000 UTC")
    return then.strftime("%Y-%m-%dT%H:%M:%SZ")


def _deployment(tmp_path: Path, monkeypatch, *, lifecycle: str, created_at: str | None,
                resolved: bool) -> None:
    dial = tmp_path / "deployment.yaml"
    dial.write_text(DIAL.format(lifecycle=lifecycle), encoding="utf-8")
    monkeypatch.setattr(dfeops, "DIAL", dial)

    monkeypatch.setattr(dfeops, "TF_ENVIRONMENTS", tmp_path / "terraform" / "environments")
    monkeypatch.setattr(dfeops, "_tf_output", lambda _cloud, _name: created_at)

    sizing = tmp_path / "sizing" / "resolved.yaml"
    monkeypatch.setattr(dfeops, "SIZING_RESOLVED", sizing)
    if resolved:
        sizing.parent.mkdir(parents=True, exist_ok=True)
        sizing.write_text(RESOLVED, encoding="utf-8")


def test_an_ephemeral_deployment_reports_its_age_and_its_rate(tmp_path: Path, monkeypatch) -> None:
    _deployment(tmp_path, monkeypatch, lifecycle="ephemeral", created_at=_stamp(2.75), resolved=True)
    notice = dfeops._ephemeral_notice()
    assert notice is not None
    assert "ephemeral deployment (aws)" in notice
    assert "up 2h45m" in notice
    assert "4.2117 USD/hour" in notice


def test_both_spellings_of_the_stamp_read_the_same(tmp_path: Path, monkeypatch) -> None:
    """The provider renders a creation stamp as RFC 3339 on some resources and
    as a Go time on others, and the banner cannot tell which it will be given."""
    _deployment(tmp_path, monkeypatch, lifecycle="ephemeral",
                created_at=_stamp(9.0, "go"), resolved=True)
    assert "up 9h00m" in dfeops._ephemeral_notice()


def test_a_persistent_deployment_gets_no_line(tmp_path: Path, monkeypatch) -> None:
    """The banner is for the deployments meant to be torn down, not every one."""
    _deployment(tmp_path, monkeypatch, lifecycle="persistent", created_at=_stamp(2.0), resolved=True)
    assert dfeops._ephemeral_notice() is None


def test_a_missing_output_or_resolve_still_answers(tmp_path: Path, monkeypatch) -> None:
    """Neither reading may fail a command -- an unknown is said, not raised."""
    _deployment(tmp_path, monkeypatch, lifecycle="ephemeral", created_at=None, resolved=False)
    notice = dfeops._ephemeral_notice()
    assert notice is not None
    assert "age unavailable (no cluster_created_at output)" in notice
    assert "compute rate not resolved" in notice


def test_a_stamp_in_no_spelling_at_all_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    """An output that is not a timestamp is an unknown age, never an exception
    in front of every command."""
    _deployment(tmp_path, monkeypatch, lifecycle="ephemeral", created_at="(known after apply)",
                resolved=True)
    assert "age unavailable" in dfeops._ephemeral_notice()


def test_a_tofu_read_that_exits_does_not_take_the_command_with_it(tmp_path: Path, monkeypatch) -> None:
    """bridge.get_tf_outputs exits the process when tofu fails -- an
    uninitialised root, a deployment never applied -- and a banner may not."""
    environments = tmp_path / "terraform" / "environments"
    (environments / "aws").mkdir(parents=True)
    monkeypatch.setattr(dfeops, "TF_ENVIRONMENTS", environments)

    def _exit(_tf_dir: str) -> dict[str, tuple[str, bool]]:
        print("ERROR: tofu output failed", file=sys.stderr)
        raise SystemExit(1)

    fake = types.ModuleType("bridge")
    fake.get_tf_outputs = _exit
    monkeypatch.setitem(sys.modules, "bridge", fake)

    assert dfeops._tf_output("aws", "cluster_created_at") is None


def test_an_environment_that_does_not_exist_is_never_read(tmp_path: Path, monkeypatch) -> None:
    """A dial naming a cloud this checkout has no root for reads nothing at all,
    rather than shelling out to find that out."""
    monkeypatch.setattr(dfeops, "TF_ENVIRONMENTS", tmp_path / "terraform" / "environments")

    def _never(_tf_dir: str) -> dict[str, tuple[str, bool]]:
        raise AssertionError("no root directory means no tofu call")

    fake = types.ModuleType("bridge")
    fake.get_tf_outputs = _never
    monkeypatch.setitem(sys.modules, "bridge", fake)

    assert dfeops._tf_output("gcp", "cluster_created_at") is None


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
