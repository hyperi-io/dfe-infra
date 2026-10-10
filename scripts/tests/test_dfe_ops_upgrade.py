#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_upgrade.py
#  Purpose:      Guard `dfe-ops upgrade` -- the diff is ordered by
#                upgrade-order.yaml and carries its notes, compat-check and
#                the sizing locked-change classifier gate the plan, every
#                preflight check reports PASS/FAIL with evidence, apply
#                --dry-run prints the ordered commands and touches nothing,
#                and rollback refuses a one-way step by name.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe_ops_upgrade.py.

    python3 -m pytest scripts/tests/test_dfe_ops_upgrade.py -q

Every kubectl/git/dfe-stack/resolve_sizing.py/check_node_capacity.py call is
mocked at this module's own `_run` -- the same single subprocess boundary
test_dfe_ops_bastion.py mocks for tofu/render_dial.py. No real cluster,
repo or resolver is touched.
"""

import argparse
import base64
import itertools
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_clickhouse_replica_spread import keeper_pod_labels as ch_keeper_labels
from test_clickhouse_replica_spread import render as ch_render
from test_clickhouse_replica_spread import server_pod_labels as ch_server_labels

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
BOOTSTRAP_DIR = REPO_ROOT / "bootstrap"
BOOTSTRAP_SH = BOOTSTRAP_DIR / "bootstrap.sh"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(1, str(BOOTSTRAP_DIR))
import argocd_login  # noqa: E402
import dfe_ops_upgrade as u  # noqa: E402

# The real wait, for the tests that drive apply through it rather than past it.
REAL_WAIT_FOR_ARGO = u.wait_for_argo

# ---------------------------------------------------------------------------
# Fixture data -- small, self-contained versions.yaml / upgrade-order.yaml /
# pins.yaml, so these tests do not depend on the repo's real (large, moving)
# versions.yaml.
# ---------------------------------------------------------------------------

ORDER_YAML = """
stages:
  "10-bootstrap":
    "10-cert-manager":
      key: bootstrap.cert-manager
  "20-operators":
    "10-strimzi-kafka-operator":
      key: operators.strimzi-kafka-operator
      before: "kafka.strimzi.io stored-version conversion on the running cluster: bin/v1-api-conversion.sh convert-resource, then crd-upgrade"
  "30-services":
    "10-kafka-brokers":
      key: services.kafka-version
      finalise: "metadata.version bump after a soak; one way"
      rollback: "none"
    "20-clickhouse-server":
      key: services.clickhouse-version
      rollback: "within the same LTS line only"
"""

VERSIONS_YAML = """
current: "2.0.0"
stacks:
  1.0.0:
    bootstrap:
      cert-manager: "v1.0.0"
    operators:
      strimzi-kafka-operator: "0.51.0"
    services:
      kafka-version: "4.2.0"
      clickhouse-version: "26.3.17.56"
  1.1.0:
    bootstrap:
      cert-manager: "v1.1.0"
    operators:
      strimzi-kafka-operator: "1.2.0"
    services:
      kafka-version: "4.2.0"
      clickhouse-version: "26.3.17.56"
  2.0.0:
    bootstrap:
      cert-manager: "v1.1.0"
    operators:
      strimzi-kafka-operator: "1.2.0"
    services:
      kafka-version: "4.3.1"
      clickhouse-version: "26.3.32.14"
"""

PINS_YAML = """base:
  dfe-infra: "1.0.0"
  dfe-schemas: "v0.2.0"
channel: "release"
"""


@pytest.fixture
def order_path(tmp_path: Path) -> Path:
    path = tmp_path / "upgrade-order.yaml"
    path.write_text(ORDER_YAML, encoding="utf-8")
    return path


@pytest.fixture
def versions_path(tmp_path: Path) -> Path:
    path = tmp_path / "versions.yaml"
    path.write_text(VERSIONS_YAML, encoding="utf-8")
    return path


@pytest.fixture
def deploy(tmp_path: Path) -> Path:
    d = tmp_path / "deploy"
    d.mkdir()
    (d / "pins.yaml").write_text(PINS_YAML, encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def plan_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Where `plan` writes by default, kept out of this checkout's own .tmp/."""
    path = tmp_path / "plans"
    monkeypatch.setattr(u, "DEFAULT_PLAN_DIR", path)
    return path


@pytest.fixture(autouse=True)
def _no_real_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test whose kubectl or helm call reaches the real subprocess
    boundary, which would read whatever cluster this host's kubeconfig names.
    git still runs for real, for the tests built on a real deploy repo."""
    real_run = u._run

    def guarded(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if cmd[:1] in (["kubectl"], ["helm"]):
            raise AssertionError(f"test reached a real cluster: {cmd}")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(u, "_run", guarded)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=stderr)


def _mock_run(monkeypatch: pytest.MonkeyPatch, *responses: subprocess.CompletedProcess) -> list[list[str]]:
    """Replace this module's own subprocess boundary with one that returns
    `responses` in call order and records each call's argv."""
    queue = list(responses)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        return queue.pop(0)

    monkeypatch.setattr(u, "_run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# upgrade-order.yaml loading -- quoted block keys, stage/order sort
# ---------------------------------------------------------------------------


def test_load_steps_unquotes_and_orders(order_path: Path) -> None:
    steps = u.load_steps(order_path)
    assert [s.stage for s in steps] == ["10-bootstrap", "20-operators", "30-services", "30-services"]
    assert steps[0].key == "bootstrap.cert-manager"
    assert steps[0].stage == "10-bootstrap"  # no stray quotes
    assert steps[1].key == "operators.strimzi-kafka-operator"
    assert "stored-version conversion" in steps[1].before
    assert steps[2].key == "services.kafka-version"
    assert steps[2].finalise
    assert steps[2].rollback == "none"
    assert steps[2].one_way is True
    assert steps[3].rollback == "within the same LTS line only"
    assert steps[3].one_way is False


def test_step_one_way_true_for_finalise_with_no_explicit_rollback() -> None:
    step = u.Step(stage="s", order="o", key="k", finalise="one way bump")
    assert step.one_way is True


# ---------------------------------------------------------------------------
# versions.yaml + pins.yaml reading
# ---------------------------------------------------------------------------


def test_resolve_stack_exact_and_v_prefix_tolerant(versions_path: Path) -> None:
    root = u.load_versions_root(versions_path)
    name, pins = u.resolve_stack(root, "1.0.0")
    assert name == "1.0.0"
    assert pins["bootstrap"]["cert-manager"] == "v1.0.0"
    # v-prefix tolerant
    name2, _ = u.resolve_stack(root, "v1.0.0")
    assert name2 == "1.0.0"


def test_resolve_stack_unknown_raises(versions_path: Path) -> None:
    root = u.load_versions_root(versions_path)
    with pytest.raises(u.UpgradeError, match=r"not in versions\.yaml"):
        u.resolve_stack(root, "9.9.9")


def test_current_stack(versions_path: Path) -> None:
    root = u.load_versions_root(versions_path)
    assert u.current_stack(root) == "2.0.0"


def test_flatten_stack_only_upgrade_sections(versions_path: Path) -> None:
    root = u.load_versions_root(versions_path)
    _, pins = u.resolve_stack(root, "1.0.0")
    flat = u.flatten_stack(pins)
    assert flat == {
        "bootstrap.cert-manager": "v1.0.0",
        "operators.strimzi-kafka-operator": "0.51.0",
        "services.kafka-version": "4.2.0",
        "services.clickhouse-version": "26.3.17.56",
    }


def test_read_deploy_pin(deploy: Path) -> None:
    assert u.read_deploy_pin(deploy) == "1.0.0"


def test_read_deploy_pin_no_pins_file(tmp_path: Path) -> None:
    with pytest.raises(u.UpgradeError, match="not a dfe-deploy checkout"):
        u.read_deploy_pin(tmp_path)


STACK_VERSION_JSONPATH = "jsonpath={.metadata.annotations.dfe\\.hyperi\\.io/stack_version}"


def test_read_stack_version_reads_only_that_annotation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_run(monkeypatch, _proc(0, stdout="2.2.0\n"))
    assert u.read_stack_version("kc", "argocd") == "2.2.0"
    assert calls[0][-2:] == ["-o", STACK_VERSION_JSONPATH]
    assert ["secret", "dfe-cluster"] == calls[0][calls[0].index("get") + 1 : calls[0].index("get") + 3]
    assert calls[0][calls[0].index("-n") + 1] == "argocd"


def test_from_stack_without_pins_yaml_is_the_cluster_secrets_stack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mock_run(monkeypatch, _proc(0, stdout="2.2.0\n"))
    stack, note = u.read_from_stack(tmp_path, None, kubeconfig="kc", argocd_namespace="argocd")
    assert stack == "2.2.0"
    # The value came out of a Secret, so the note never repeats it. resolve_stack's key is what prints.
    assert note == (
        "FROM is secret/dfe-cluster's dfe.hyperi.io/stack_version: the deploy repo carries "
        "no pins.yaml, so apply writes one at its first stage"
    )


def test_from_stack_reads_no_cluster_when_pins_yaml_or_from_names_it(deploy: Path, tmp_path: Path) -> None:
    # The autouse guard fails any kubectl call, so reaching the cluster here fails the test.
    assert u.read_from_stack(deploy, None, kubeconfig="kc", argocd_namespace="argocd") == ("1.0.0", "")
    assert u.read_from_stack(deploy, "0.9.0", kubeconfig="kc", argocd_namespace="argocd") == ("0.9.0", "")
    assert u.read_from_stack(tmp_path, "0.9.0", kubeconfig="kc", argocd_namespace="argocd") == ("0.9.0", "")


def test_from_stack_still_refuses_a_pins_yaml_apply_cannot_bump(tmp_path: Path) -> None:
    (tmp_path / "pins.yaml").write_text('channel: "release"\n', encoding="utf-8")
    with pytest.raises(u.UpgradeError, match=r"no base\.dfe-infra pin"):
        u.read_from_stack(tmp_path, "1.0.0", kubeconfig="kc", argocd_namespace="argocd")


def test_from_stack_refuses_with_neither_pins_yaml_nor_a_stack_annotation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mock_run(monkeypatch, _proc(0, stdout=""))
    with pytest.raises(u.UpgradeError) as caught:
        u.read_from_stack(tmp_path, None, kubeconfig="kc", argocd_namespace="argocd")
    assert str(caught.value) == (
        f"no pins.yaml in {tmp_path}, and secret/dfe-cluster carries no dfe.hyperi.io/stack_version -- "
        "commit a pins.yaml whose base.dfe-infra names the stack this deployment runs"
    )


def test_from_stack_names_a_cluster_secret_it_could_not_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mock_run(monkeypatch, _proc(1, stderr='Error from server (NotFound): secrets "dfe-cluster" not found'))
    unreadable = re.escape("cannot read dfe.hyperi.io/stack_version on secret/dfe-cluster in argocd: Error from server")
    with pytest.raises(u.UpgradeError, match=unreadable):
        u.read_from_stack(tmp_path, None, kubeconfig="kc", argocd_namespace="argocd")


# ---------------------------------------------------------------------------
# plan_moves / render_plan -- the two-key move with a before hook
# ---------------------------------------------------------------------------


def test_plan_moves_two_key_move_with_before_hook(order_path: Path, versions_path: Path) -> None:
    steps = u.load_steps(order_path)
    root = u.load_versions_root(versions_path)
    _, from_pins = u.resolve_stack(root, "1.0.0")
    _, to_pins = u.resolve_stack(root, "1.1.0")
    moves = u.plan_moves(steps, u.flatten_stack(from_pins), u.flatten_stack(to_pins))

    # Exactly two keys move between 1.0.0 and 1.1.0: cert-manager (no note)
    # and strimzi-kafka-operator (carries the before hook). kafka-version and
    # clickhouse-version are unchanged between these two stacks.
    assert [m.step.key for m in moves] == ["bootstrap.cert-manager", "operators.strimzi-kafka-operator"]
    assert moves[0].old == "v1.0.0"
    assert moves[0].new == "v1.1.0"
    assert moves[1].step.before

    rendered = u.render_plan(moves, from_stack="1.0.0", to_stack="1.1.0")
    assert "# Upgrade plan: 1.0.0 -> 1.1.0" in rendered
    assert "1. bootstrap.cert-manager: v1.0.0 -> v1.1.0" in rendered
    assert "2. operators.strimzi-kafka-operator: 0.51.0 -> 1.2.0" in rendered
    assert "before:   kafka.strimzi.io stored-version conversion" in rendered
    # stage headers present, in upgrade-order.yaml order
    assert rendered.index("## stage 10-bootstrap") < rendered.index("## stage 20-operators")


def test_render_plan_no_moves() -> None:
    rendered = u.render_plan([], from_stack="1.0.0", to_stack="1.0.0")
    assert "No pinned key moves" in rendered


def test_render_plan_shows_scope_for_disambiguation() -> None:
    step = u.Step(stage="30-services", order="10", key="services.clickhouse-version", scope="keeper")
    move = u.Move(step=step, old="a", new="b")
    rendered = u.render_plan([move], from_stack="x", to_stack="y")
    assert "services.clickhouse-version (keeper): a -> b" in rendered


# ---------------------------------------------------------------------------
# Every pinned DFE component moves with a step, its image and chart digests
# included; one no step names rolls on the retarget with no stage in the plan.
# ---------------------------------------------------------------------------

# Two stacks that differ in dfe-hyperdx's tag, image digest and chart digest alone.
HYPERDX_VERSIONS_YAML = """
current: "2.0.0"
stacks:
  1.0.0:
    apps:
      dfe-engine: "v1.0.0"
    content:
      dfe-hyperdx: "v0.3.2"
    digests:
      dfe-engine: "sha256:e1"
      dfe-hyperdx: "sha256:h1"
    chart-digests:
      dfe-engine: "sha256:ce1"
      hyperdx: "sha256:ch1"
  2.0.0:
    apps:
      dfe-engine: "v1.0.0"
    content:
      dfe-hyperdx: "v0.3.3"
    digests:
      dfe-engine: "sha256:e1"
      dfe-hyperdx: "sha256:h2"
    chart-digests:
      dfe-engine: "sha256:ce1"
      hyperdx: "sha256:ch2"
"""

# An operator shell image tagged by toolbox.dfe-toolbox, which no stage deploys.
UNDEPLOYED_DIGESTS = {"dfe-toolbox-base"}


def _hyperdx_stacks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str = HYPERDX_VERSIONS_YAML) -> None:
    """The fixture stacks against the repo's own upgrade-order.yaml."""
    versions = tmp_path / "versions.yaml"
    versions.write_text(text, encoding="utf-8")
    monkeypatch.setattr(u, "VERSIONS_FILE", versions)
    monkeypatch.setattr(u, "run_compat_check", lambda *_a, **_k: (True, "ok"))


def test_a_stack_moving_only_dfe_hyperdx_yields_a_plan_stage(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _hyperdx_stacks(monkeypatch, tmp_path)

    assert u.cmd_upgrade_plan(_plan_args(deploy=str(deploy), to="2.0.0")) == u.EXIT_OK

    out = capsys.readouterr().out
    assert "## stage 40-apps" in out
    assert "1. content.dfe-hyperdx: v0.3.2 -> v0.3.3" in out
    assert "   image:    digests.dfe-hyperdx: sha256:h1 -> sha256:h2" in out
    assert "   chart:    chart-digests.hyperdx: sha256:ch1 -> sha256:ch2" in out
    assert "2. " not in out
    assert "No pinned key moves" not in out


def test_apply_walks_a_dfe_hyperdx_move_as_a_stage(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _hyperdx_stacks(monkeypatch, tmp_path)

    assert u.cmd_upgrade_apply(_apply_args(deploy=str(deploy), to="2.0.0")) == u.EXIT_OK

    err = capsys.readouterr().err
    assert "=== stage 1/1: 40-apps ===" in err
    assert "  content.dfe-hyperdx: v0.3.2 -> v0.3.3" in err
    assert "    chart chart-digests.hyperdx: sha256:ch1 -> sha256:ch2" in err
    assert "chore(upgrade): 2.0.0 stage 1 -- content.dfe-hyperdx" in err


def test_a_digest_moving_under_an_unchanged_tag_still_moves_its_step(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    republished = HYPERDX_VERSIONS_YAML.replace('dfe-hyperdx: "v0.3.3"', 'dfe-hyperdx: "v0.3.2"')
    republished = republished.replace('dfe-hyperdx: "sha256:h2"', 'dfe-hyperdx: "sha256:h1"')
    _hyperdx_stacks(monkeypatch, tmp_path, republished)
    root = u.load_versions_root()
    _, old = u.resolve_stack(root, "1.0.0")
    _, new = u.resolve_stack(root, "2.0.0")

    (move,) = u.plan_moves(u.load_steps(), u.flatten_stack(old), u.flatten_stack(new))

    assert (move.step.key, move.old, move.new) == ("content.dfe-hyperdx", "v0.3.2", "v0.3.2")
    assert move.pins == (("chart", "chart-digests.hyperdx", "sha256:ch1", "sha256:ch2"),)


def test_every_dfe_component_the_current_stack_pins_moves_with_a_step() -> None:
    """A component pinned with no step moves on the retarget with no stage, which dfe-hyperdx did."""
    root = u.load_versions_root()
    _, stack = u.resolve_stack(root, u.current_stack(root))
    steps = u.load_steps()
    keys = {step.key for step in steps}
    images = {step.image for step in steps if step.image}
    charts = {step.chart for step in steps if step.chart}

    assert sorted(f"apps.{name}" for name in stack["apps"] if f"apps.{name}" not in keys) == []
    deployed = set(stack["digests"]) - UNDEPLOYED_DIGESTS
    assert sorted(f"digests.{name}" for name in deployed if f"digests.{name}" not in images) == []
    assert sorted(f"chart-digests.{name}" for name in stack["chart-digests"] if f"chart-digests.{name}" not in charts) == []
    # A digest a step names must be a pin the stack carries, or the step watches nothing.
    flat = u.flatten_stack(stack)
    assert sorted(pin for pin in images | charts if pin not in flat) == []
    # The step key carries the component's own tag: digests.X goes with apps.X or content.X.
    assert [s.key for s in steps if s.image and s.key.split(".", 1)[1] != s.image.split(".", 1)[1]] == []


def test_dfe_hyperdx_moves_after_the_engine_whose_jwks_it_verifies_against() -> None:
    order = [step.key for step in u.load_steps() if step.stage == "40-apps"]
    assert order.index("content.dfe-hyperdx") == order.index("apps.dfe-engine") + 1


# ---------------------------------------------------------------------------
# the pin-file editor -- surgical, idempotent, refuses a block-less file
# ---------------------------------------------------------------------------


def test_set_pin_stack_edits_only_dfe_infra() -> None:
    updated = u.set_pin_stack(PINS_YAML, "2.0.0")
    assert 'dfe-infra: "2.0.0"' in updated
    assert 'dfe-schemas: "v0.2.0"' in updated  # sibling survives
    assert 'channel: "release"' in updated  # sibling block survives


def test_set_pin_stack_idempotent_when_already_at_target() -> None:
    updated = u.set_pin_stack(PINS_YAML, "1.0.0")
    assert updated == PINS_YAML


def test_set_pin_stack_refuses_no_base_block() -> None:
    with pytest.raises(u.PinFileError, match=r"base\.dfe-infra"):
        u.set_pin_stack("channel: release\n", "2.0.0")


def test_bump_pin_file_writes_and_reports_change(deploy: Path) -> None:
    changed = u.bump_pin_file(deploy, "2.0.0")
    assert changed is True
    assert u.read_deploy_pin(deploy) == "2.0.0"
    changed_again = u.bump_pin_file(deploy, "2.0.0")
    assert changed_again is False


def test_bump_pin_file_writes_dfe_deploys_shape_where_there_is_none(tmp_path: Path) -> None:
    assert u.bump_pin_file(tmp_path, "2.2.1", channel="release") is True
    written = (tmp_path / "pins.yaml").read_text(encoding="utf-8")
    assert u.read_deploy_pin(tmp_path) == "2.2.1"
    tree = u.yaml_subset.parse(written, source="pins.yaml")
    assert tree == {"base": {"dfe-infra": "2.2.1"}, "channel": "release"}
    assert "# overrides:\n#   apps:\n" in written
    assert written.isascii()
    # The next upgrade edits the written file in place, like any other pins.yaml.
    assert u.bump_pin_file(tmp_path, "2.2.2", channel="release") is True
    assert (tmp_path / "pins.yaml").read_text(encoding="utf-8") == written.replace('"2.2.1"', '"2.2.2"')


def test_bump_pin_file_names_no_channel_for_a_stack_with_no_maturity(tmp_path: Path) -> None:
    u.bump_pin_file(tmp_path, "2.2.1")
    tree = u.yaml_subset.parse((tmp_path / "pins.yaml").read_text(encoding="utf-8"), source="pins.yaml")
    assert tree == {"base": {"dfe-infra": "2.2.1"}}


# ---------------------------------------------------------------------------
# compat-check block
# ---------------------------------------------------------------------------


def test_run_compat_check_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_run(monkeypatch, _proc(0, stdout="ok     rule-a: x = y\n\ncompat-check 2.0.0: 1 rule(s) checked"))
    ok, output = u.run_compat_check("2.0.0")
    assert ok is True
    assert "rule-a" in output
    assert calls[0][0] == sys.executable
    assert "compat-check" in calls[0]
    assert "--strict" in calls[0]
    assert calls[0][calls[0].index("--stack") + 1] == "2.0.0"


def test_run_compat_check_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _proc(1, stdout="FAIL  rule-a: x violates >=2\n"))
    ok, output = u.run_compat_check("2.0.0")
    assert ok is False
    assert "FAIL" in output


# ---------------------------------------------------------------------------
# the sizing locked-change block
# ---------------------------------------------------------------------------


def test_check_locked_sizing_skips_when_no_previous_file(tmp_path: Path) -> None:
    ok, detail, blocked = u.check_locked_sizing(
        tmp_path / "deployment.yaml", tmp_path / "missing-resolved.yaml", fixtures=None, live=False
    )
    assert ok is True
    assert blocked is False
    assert "skipped" in detail


def test_check_locked_sizing_skips_when_no_fixtures_or_live(tmp_path: Path) -> None:
    previous = tmp_path / "resolved.yaml"
    previous.write_text("locked: {}\n", encoding="utf-8")
    ok, detail, blocked = u.check_locked_sizing(tmp_path / "deployment.yaml", previous, fixtures=None, live=False)
    assert ok is True
    assert blocked is False
    assert "neither --fixtures nor --live" in detail


def test_check_locked_sizing_reports_locked_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    previous = tmp_path / "resolved.yaml"
    previous.write_text("locked: {}\n", encoding="utf-8")
    _mock_run(
        monkeypatch,
        _proc(3, stdout="resolve_sizing: LOCKED partition_count: 12 -> 24 (immutable after create)\n"),
    )
    ok, detail, blocked = u.check_locked_sizing(
        tmp_path / "deployment.yaml", previous, fixtures=tmp_path / "fixtures", live=False
    )
    assert ok is True  # a refusal is not a resolver failure
    assert blocked is True
    assert "LOCKED" in detail
    assert "--migrate required" in detail


def test_check_locked_sizing_ok_no_locked_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    previous = tmp_path / "resolved.yaml"
    previous.write_text("locked: {}\n", encoding="utf-8")
    _mock_run(monkeypatch, _proc(0, stdout="sizing resolved cleanly"))
    ok, detail, blocked = u.check_locked_sizing(
        tmp_path / "deployment.yaml", previous, fixtures=tmp_path / "fixtures", live=False
    )
    assert ok is True
    assert blocked is False
    assert "sizing resolved cleanly" in detail


def test_check_locked_sizing_resolver_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    previous = tmp_path / "resolved.yaml"
    previous.write_text("locked: {}\n", encoding="utf-8")
    _mock_run(monkeypatch, _proc(1, stderr="bad dial\n"))
    ok, detail, blocked = u.check_locked_sizing(
        tmp_path / "deployment.yaml", previous, fixtures=tmp_path / "fixtures", live=False
    )
    assert ok is False
    assert blocked is True
    assert "resolver failed" in detail


# Everything resolve_sizing.py writes under --out on a populated cloud, sizing/
# and the two root-level artefacts that belong beside the OpenTofu root.
RESOLVER_OUTPUT = {
    "sizing/resolved.yaml": "locked:\n  partition_count: 24\n",
    "sizing/scale.values.yaml": "kafka: {}\n",
    "sizing/scale.report.md": "# report\n",
    "sizing.auto.tfvars.json": "{}\n",
    "shapes/resolved/aws-us-west-2.json": "{}\n",
}

PREVIOUS_RESOLVED = "locked:\n  partition_count: 12\n"


def _write_resolver_output(cmd: list[str]) -> None:
    """Write RESOLVER_OUTPUT under the --out the resolver argv names."""
    out = Path(cmd[cmd.index("--out") + 1])
    for rel, text in RESOLVER_OUTPUT.items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _fake_resolver(monkeypatch: pytest.MonkeyPatch, returncode: int) -> list[list[str]]:
    """Stand in for resolve_sizing.py: write its artefacts, then exit `returncode`.

    The real resolver writes before it exits 1 on a fatal finding, so the
    artefacts are written whatever the code.
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        _write_resolver_output(cmd)
        return _proc(returncode, stderr="resolve_sizing: MIGRATING partition_count: 12 -> 24\n")

    monkeypatch.setattr(u, "_run", fake_run)
    return calls


@pytest.fixture
def committed_sizing(deploy: Path) -> Path:
    """The deploy's committed sizing/resolved.yaml, at its pre-resolve value."""
    previous = deploy / "sizing" / "resolved.yaml"
    previous.parent.mkdir()
    previous.write_text(PREVIOUS_RESOLVED, encoding="utf-8")
    return previous


def test_check_locked_sizing_refresh_copies_only_sizing_into_the_deploy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, deploy: Path, committed_sizing: Path
) -> None:
    calls = _fake_resolver(monkeypatch, 0)
    ok, detail, blocked = u.check_locked_sizing(
        tmp_path / "deployment.yaml", committed_sizing, fixtures=tmp_path / "fixtures",
        live=False, migrate=True, refresh=deploy,
    )
    assert ok is True
    assert blocked is False
    assert committed_sizing.read_text(encoding="utf-8") == RESOLVER_OUTPUT["sizing/resolved.yaml"]
    assert (deploy / "sizing" / "scale.values.yaml").is_file()
    assert (deploy / "sizing" / "scale.report.md").is_file()
    # The OpenTofu inputs and the shape answer stay out of the deploy repo.
    assert not (deploy / "sizing.auto.tfvars.json").exists()
    assert not (deploy / "shapes").exists()
    assert "--migrate" in calls[0]
    assert Path(calls[0][calls[0].index("--out") + 1]) != deploy
    assert f"refreshed {deploy / 'sizing'}" in detail


def test_check_locked_sizing_without_refresh_leaves_the_deploy_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, deploy: Path, committed_sizing: Path
) -> None:
    _fake_resolver(monkeypatch, 0)
    ok, detail, _blocked = u.check_locked_sizing(
        tmp_path / "deployment.yaml", committed_sizing, fixtures=tmp_path / "fixtures", live=False
    )
    assert ok is True
    assert committed_sizing.read_text(encoding="utf-8") == PREVIOUS_RESOLVED
    assert sorted(p.name for p in (deploy / "sizing").iterdir()) == ["resolved.yaml"]
    assert "refreshed" not in detail


def test_check_locked_sizing_refresh_copies_nothing_when_the_resolver_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, deploy: Path, committed_sizing: Path
) -> None:
    _fake_resolver(monkeypatch, 1)
    ok, _detail, blocked = u.check_locked_sizing(
        tmp_path / "deployment.yaml", committed_sizing, fixtures=tmp_path / "fixtures",
        live=False, migrate=True, refresh=deploy,
    )
    assert ok is False
    assert blocked is True
    assert committed_sizing.read_text(encoding="utf-8") == PREVIOUS_RESOLVED
    assert sorted(p.name for p in (deploy / "sizing").iterdir()) == ["resolved.yaml"]


# ---------------------------------------------------------------------------
# preflight checks -- each PASS and FAIL
# ---------------------------------------------------------------------------


def test_check_deploy_clean_pass(monkeypatch: pytest.MonkeyPatch, deploy: Path) -> None:
    _mock_run(monkeypatch, _proc(0, stdout=""))
    ok, detail = u.check_deploy_clean(deploy)
    assert ok is True
    assert "clean" in detail


def test_check_deploy_clean_fail(monkeypatch: pytest.MonkeyPatch, deploy: Path) -> None:
    _mock_run(monkeypatch, _proc(0, stdout="?? pins.yaml\n M sizing/resolved.yaml\n"))
    ok, detail = u.check_deploy_clean(deploy)
    assert ok is False
    assert "2 uncommitted" in detail


def test_check_cluster_reachable_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _proc(0, stdout="Client Version: v1.31.0\nServer Version: v1.31.0"))
    ok, detail = u.check_cluster_reachable("kc")
    assert ok is True
    assert "Server Version" in detail


def test_check_cluster_reachable_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _proc(1, stderr="Unable to connect to the server\n"))
    ok, detail = u.check_cluster_reachable("kc")
    assert ok is False
    assert "Unable to connect" in detail


def test_check_argo_apps_all_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = {
        "items": [
            {"metadata": {"name": "a"}, "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}},
            {"metadata": {"name": "b"}, "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}},
        ]
    }
    import json

    _mock_run(monkeypatch, _proc(0, stdout=json.dumps(doc)))
    ok, detail = u.check_argo_apps("kc")
    assert ok is True
    assert "2 Application(s)" in detail


def test_check_argo_apps_some_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    doc = {
        "items": [
            {"metadata": {"name": "a"}, "status": {"sync": {"status": "OutOfSync"}, "health": {"status": "Progressing"}}},
        ]
    }
    _mock_run(monkeypatch, _proc(0, stdout=json.dumps(doc)))
    ok, detail = u.check_argo_apps("kc")
    assert ok is False
    assert "a (sync OutOfSync, health Progressing)" in detail


def test_check_no_kafka_rebalance_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    _mock_run(monkeypatch, _proc(0, stdout=json.dumps({"items": []})))
    ok, detail = u.check_no_kafka_rebalance("kc")
    assert ok is True
    assert "no KafkaRebalance" in detail


def test_check_no_kafka_rebalance_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    doc = {"items": [{"metadata": {"name": "rb1"}, "status": {"conditions": [{"type": "Rebalancing"}]}}]}
    _mock_run(monkeypatch, _proc(0, stdout=json.dumps(doc)))
    ok, detail = u.check_no_kafka_rebalance("kc")
    assert ok is False
    assert "rb1" in detail


CH_PASSWORD = "s3cr3t-Value"


def _ch_pods(*names: str) -> subprocess.CompletedProcess:
    return _proc(0, stdout=json.dumps({"items": [{"metadata": {"name": n}} for n in names]}))


def _ch_password(value: str = CH_PASSWORD) -> subprocess.CompletedProcess:
    return _proc(0, stdout=base64.b64encode(value.encode()).decode())


def _ch_auth(call: list[str]) -> list[str]:
    """The arguments between clickhouse-client and its -q."""
    return call[call.index("clickhouse-client") + 1 : call.index("-q")]


def test_check_clickhouse_merges_queries_every_server_pod_with_the_resolved_credential(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    calls = _mock_run(
        monkeypatch, _ch_pods("ch-0-0", "ch-0-1", "ch-0-2"), _ch_password(), _proc(0, stdout="1\n"),
        _proc(0, stdout="0\n"), _proc(0, stdout="0\n"), _proc(0, stdout="0\n"),
    )
    ok, detail = u.check_clickhouse_merges("kc")
    assert (ok, detail) == (True, "no merge running longer than 300s on 3 ClickHouse pod(s)")
    secret = calls[1]
    assert secret[secret.index("get") + 1 : secret.index("get") + 3] == ["secret", "clickhouse-admin-password"]
    assert secret[-1] == "jsonpath={.data.password}"
    queries = calls[3:]
    assert [c[c.index("exec") + 1] for c in queries] == ["ch-0-0", "ch-0-1", "ch-0-2"]
    assert all(_ch_auth(c) == ["--user", "default", "--password", CH_PASSWORD] for c in calls[2:])
    assert CH_PASSWORD not in detail
    captured = capsys.readouterr()
    assert CH_PASSWORD not in captured.out + captured.err


def test_preflight_and_apply_take_the_clickhouse_credentials_flag() -> None:
    parser = argparse.ArgumentParser()
    u.add_upgrade_subparser(parser.add_subparsers())
    for verb in ("preflight", "apply"):
        args = parser.parse_args(["upgrade", verb, "--deploy", "d", "--clickhouse-credentials", "ch-admin"])
        assert args.clickhouse_credentials == "ch-admin"
        assert parser.parse_args(["upgrade", verb, "--deploy", "d"]).clickhouse_credentials == "clickhouse-admin-password"


def test_check_clickhouse_merges_refuses_when_one_pod_of_several_is_merging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_run(
        monkeypatch, _ch_pods("ch-0-0", "ch-0-1", "ch-0-2"), _ch_password(), _proc(0, stdout="1\n"),
        _proc(0, stdout="0\n"), _proc(0, stdout="2\n"), _proc(0, stdout="0\n"),
    )
    ok, detail = u.check_clickhouse_merges("kc", threshold_seconds=300)
    assert ok is False
    assert detail == "1 of 3 ClickHouse pod(s) carry a merge running longer than 300s: ch-0-1 (2)"


def test_check_clickhouse_merges_refuses_a_pod_that_does_not_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(
        monkeypatch, _ch_pods("ch-0-0", "ch-0-1"), _ch_password(), _proc(0, stdout="1\n"),
        _proc(0, stdout="0\n"), _proc(1, stderr='error: unable to upgrade connection: container not found ("x")'),
    )
    ok, detail = u.check_clickhouse_merges("kc")
    assert ok is False
    assert detail == 'ch-0-1: error: unable to upgrade connection: container not found ("x")'


def test_check_clickhouse_merges_falls_back_to_the_admin_user(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_run(
        monkeypatch, _ch_pods("ch-0"), _ch_password(), _proc(1, stderr="Code: 516. Authentication failed"),
        _proc(0, stdout="1\n"), _proc(0, stdout="0\n"),
    )
    assert u.check_clickhouse_merges("kc")[0] is True
    assert _ch_auth(calls[-1]) == ["--user", "admin", "--password", CH_PASSWORD]


def test_check_clickhouse_merges_without_the_secret_tries_default_with_no_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _mock_run(
        monkeypatch, _ch_pods("ch-0"), _proc(1, stderr='secrets "clickhouse-admin-password" not found'),
        _proc(0, stdout="1\n"), _proc(0, stdout="0\n"),
    )
    assert u.check_clickhouse_merges("kc", credentials="clickhouse-admin-password")[0] is True
    assert _ch_auth(calls[2]) == []
    assert _ch_auth(calls[3]) == []


def test_check_clickhouse_merges_refuses_when_no_credential_answers_and_never_echoes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_run(
        monkeypatch, _ch_pods("ch-0"), _ch_password(), *[_proc(1, stderr="Code: 516") for _ in range(3)],
    )
    ok, detail = u.check_clickhouse_merges("kc")
    assert ok is False
    assert detail == (
        "no ClickHouse credential answers on ch-0: tried secret/clickhouse-admin-password as default, "
        "secret/clickhouse-admin-password as admin, default with no password"
    )
    assert CH_PASSWORD not in detail


def test_check_clickhouse_merges_names_an_unreadable_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _ch_pods("ch-0"), _proc(1, stderr="Error from server (Forbidden): nope"), _proc(1))
    ok, detail = u.check_clickhouse_merges("kc")
    assert ok is False
    assert detail == (
        "no ClickHouse credential answers on ch-0: secret/clickhouse-admin-password unreadable "
        "(Error from server (Forbidden): nope), tried default with no password"
    )


def test_check_clickhouse_merges_names_a_secret_with_no_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing `password` key reads back empty with exit 0, so it is said, not passed over."""
    _mock_run(monkeypatch, _ch_pods("ch-0"), _proc(0, stdout=""), _proc(1))
    ok, detail = u.check_clickhouse_merges("kc")
    assert ok is False
    assert detail == (
        "no ClickHouse credential answers on ch-0: secret/clickhouse-admin-password holds no password, "
        "tried default with no password"
    )


def test_check_clickhouse_merges_no_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    _mock_run(monkeypatch, _proc(0, stdout=json.dumps({"items": []})))
    ok, detail = u.check_clickhouse_merges("kc")
    assert ok is False
    assert "no pod matching" in detail


PROFILES = sorted(
    path.stem.removeprefix("profile-") for path in (REPO_ROOT / "argocd" / "values").glob("profile-*.yaml")
)


def _selects(selector: str, labels: dict[str, str]) -> bool:
    """Whether a kubectl label selector matches `labels`, every requirement ANDed."""
    for requirement in re.split(r",(?![^(]*\))", selector):
        if found := re.fullmatch(r"\s*([\w./-]+)\s+(in|notin)\s+\(([^)]*)\)\s*", requirement):
            key, op, values = found.groups()
            member = labels.get(key) in {v.strip() for v in values.split(",")}
            if member != (op == "in"):
                return False
        elif found := re.fullmatch(r"\s*([\w./-]+)\s*(==|=|!=)\s*([\w./-]*)\s*", requirement):
            key, op, value = found.groups()
            if (labels.get(key) == value) != (op != "!="):
                return False
        else:
            raise AssertionError(f"selector requirement this test does not model: {requirement!r}")
    return True


def _clickhouse_pods(profile: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(server pod labels, every other pod's labels) for the ClickHouse a profile deploys,
    as the chart writes them in single mode and the operator writes them in cluster mode."""
    servers: list[dict[str, str]] = []
    others: list[dict[str, str]] = []
    for doc in ch_render(profile):
        kind, spec = doc.get("kind"), doc.get("spec") or {}
        name = (doc.get("metadata") or {}).get("name", "")
        if kind == "ClickHouseCluster":
            servers.append({**(spec.get("labels") or {}), **ch_server_labels(name, 0, 0)})
        elif kind == "KeeperCluster":
            others.append({**(spec.get("labels") or {}), **ch_keeper_labels(name, 0)})
        elif kind == "StatefulSet":
            servers.append(spec["template"]["metadata"]["labels"])
        elif kind in ("Deployment", "DaemonSet", "Job"):
            others.append(spec["template"]["metadata"]["labels"])
    return servers, others


@pytest.mark.parametrize("profile", PROFILES)
def test_default_clickhouse_selector_finds_the_server_on_every_profile(profile: str) -> None:
    servers, others = _clickhouse_pods(profile)
    assert servers, f"profile-{profile} renders no ClickHouse server"
    for labels in servers:
        assert _selects(u.DEFAULT_CLICKHOUSE_SELECTOR, labels), (profile, labels)
    for labels in others:
        assert not _selects(u.DEFAULT_CLICKHOUSE_SELECTOR, labels), (profile, labels)


def test_the_selector_model_reads_both_requirement_forms() -> None:
    assert _selects("app in (a,b)", {"app": "b"})
    assert not _selects("app in (a,b)", {"app": "c"})
    assert _selects("app notin (a,b), tier=db", {"app": "c", "tier": "db"})
    assert not _selects("app=a", {"app": "b"})
    assert _selects("app!=a", {"app": "b"})


def test_check_strimzi_conversion_no_crds_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, *[_proc(1, stderr="NotFound") for _ in u.STRIMZI_CRDS])
    ok, detail = u.check_strimzi_conversion("kc")
    assert ok is True
    assert "nothing to convert" in detail


def test_check_strimzi_conversion_all_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    doc = json.dumps({"status": {"storedVersions": ["v1"]}})
    _mock_run(monkeypatch, *[_proc(0, stdout=doc) for _ in u.STRIMZI_CRDS])
    ok, detail = u.check_strimzi_conversion("kc")
    assert ok is True
    assert "store v1 only" in detail


def test_check_strimzi_conversion_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    responses = [_proc(0, stdout=json.dumps({"status": {"storedVersions": ["v1beta2", "v1"]}}))]
    responses += [_proc(1, stderr="NotFound") for _ in u.STRIMZI_CRDS[1:]]
    _mock_run(monkeypatch, *responses)
    ok, detail = u.check_strimzi_conversion("kc")
    assert ok is False
    assert "pre-v1" in detail
    assert u.STRIMZI_CRDS[0] in detail


def test_check_node_capacity_absent_file_is_clean_skip(tmp_path: Path) -> None:
    ok, detail = u.check_node_capacity("kc", tmp_path / "missing.nodes.json")
    assert ok is True
    assert "nothing to check" in detail


def test_check_node_capacity_delegates_to_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    nodes_file = tmp_path / "scale.nodes.json"
    nodes_file.write_text("{}", encoding="utf-8")
    calls = _mock_run(monkeypatch, _proc(0, stdout="check_node_capacity: the cluster carries the sized demand"))
    ok, detail = u.check_node_capacity("kc", nodes_file)
    assert ok is True
    assert "carries the sized demand" in detail
    assert str(u.CHECK_NODE_CAPACITY) in calls[0]


def test_check_node_capacity_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    nodes_file = tmp_path / "scale.nodes.json"
    nodes_file.write_text("{}", encoding="utf-8")
    _mock_run(monkeypatch, _proc(1, stderr="check_node_capacity: REFUSED"))
    ok, detail = u.check_node_capacity("kc", nodes_file)
    assert ok is False
    assert "REFUSED" in detail


def test_check_backup_marker_not_required_when_no_one_way_step(deploy: Path) -> None:
    step = u.Step(stage="s", order="o", key="services.clickhouse-version", rollback="within the same LTS line only")
    ok, detail = u.check_backup_marker(deploy, [u.Move(step=step, old="a", new="b")])
    assert ok is True
    assert "no one-way step" in detail


def test_check_backup_marker_required_and_missing(deploy: Path) -> None:
    step = u.Step(stage="s", order="o", key="services.kafka-version", finalise="one way", rollback="none")
    ok, detail = u.check_backup_marker(deploy, [u.Move(step=step, old="a", new="b")])
    assert ok is False
    assert "services.kafka-version" in detail


def test_check_backup_marker_required_and_present(deploy: Path) -> None:
    marker = deploy / u.DEFAULT_BACKUP_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ok\n", encoding="utf-8")
    step = u.Step(stage="s", order="o", key="services.kafka-version", finalise="one way", rollback="none")
    ok, detail = u.check_backup_marker(deploy, [u.Move(step=step, old="a", new="b")])
    assert ok is True
    assert "backup marker present" in detail


# ---------------------------------------------------------------------------
# cmd_upgrade_plan -- writes the file, reports compat-check + sizing blocks
# ---------------------------------------------------------------------------


class _Args:
    """A tiny stand-in for argparse.Namespace, keyword-built per test."""

    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


def _plan_args(**overrides: object) -> _Args:
    """The plan _Args shape every test shares, with per-test overrides."""
    base = dict(dial=None, fixtures=None, live=False, kubeconfig=None, argocd_namespace="argocd", out=None)
    base.update(overrides)
    return _Args(**base)


def test_cmd_upgrade_plan_writes_file_and_reports_compat_check(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path,
    capsys: pytest.CaptureFixture, plan_dir: Path,
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    _mock_run(monkeypatch, _proc(0, stdout="ok     rule-a: x = y\n\ncompat-check 1.1.0: 1 rule(s) checked"))

    args = _plan_args(deploy=str(deploy), to="1.1.0")
    rc = u.cmd_upgrade_plan(args)

    assert rc == u.EXIT_OK
    out = capsys.readouterr().out
    assert "Upgrade plan: 1.0.0 -> 1.1.0" in out
    assert "compat-check (1.1.0, --strict)" in out
    assert "sizing locked-change check" in out
    assert "skipped: no --dial given" in out
    written = plan_dir / "1.0.0-to-1.1.0.md"
    assert written.is_file()
    assert "before:   kafka.strimzi.io stored-version conversion" in written.read_text(encoding="utf-8")
    assert not (deploy / "upgrades").exists()


def test_a_plan_leaves_the_deploy_repo_clean_for_preflight(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, order_path: Path, versions_path: Path, plan_dir: Path,
) -> None:
    """plan wrote into <deploy>/upgrades/, and preflight then refused the untracked file it left."""
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    monkeypatch.setattr(u, "run_compat_check", lambda *_a, **_k: (True, "ok"))
    assert u.check_deploy_clean(real_git_deploy) == (True, "working tree clean")

    assert u.cmd_upgrade_plan(_plan_args(deploy=str(real_git_deploy), to="1.1.0")) == u.EXIT_OK

    assert (plan_dir / "1.0.0-to-1.1.0.md").is_file()
    assert u.check_deploy_clean(real_git_deploy) == (True, "working tree clean")


def test_plan_out_names_the_file(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, tmp_path: Path,
    plan_dir: Path,
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    monkeypatch.setattr(u, "run_compat_check", lambda *_a, **_k: (True, "ok"))
    out = tmp_path / "elsewhere" / "plan.md"
    assert u.cmd_upgrade_plan(_plan_args(deploy=str(deploy), to="1.1.0", out=str(out))) == u.EXIT_OK
    assert out.read_text(encoding="utf-8").startswith("# Upgrade plan: 1.0.0 -> 1.1.0")
    assert not plan_dir.exists()

    parser = argparse.ArgumentParser()
    u.add_upgrade_subparser(parser.add_subparsers())
    assert parser.parse_args(["upgrade", "plan", "--deploy", "d", "--out", "p.md"]).out == "p.md"
    assert parser.parse_args(["upgrade", "plan", "--deploy", "d"]).out is None


def test_cmd_upgrade_plan_blocked_by_compat_check(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    _mock_run(monkeypatch, _proc(1, stdout="FAIL   rule-a: x violates >=2\n"))

    args = _plan_args(deploy=str(deploy), to="1.1.0")
    rc = u.cmd_upgrade_plan(args)
    assert rc == u.EXIT_BLOCKED


def test_cmd_upgrade_plan_bad_stack_is_preflight_failure(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    args = _plan_args(deploy=str(deploy), to="9.9.9")
    rc = u.cmd_upgrade_plan(args)
    assert rc == u.EXIT_PREFLIGHT_FAILED


def test_cmd_upgrade_plan_without_pins_yaml_plans_from_the_cluster_secrets_stack(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, order_path: Path, versions_path: Path,
    capsys: pytest.CaptureFixture, plan_dir: Path,
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    calls = _mock_run(monkeypatch, _proc(0, stdout="1.0.0"), _proc(0, stdout="compat-check 1.1.0: 0 rule(s) checked"))

    rc = u.cmd_upgrade_plan(_plan_args(deploy=str(bundled), to="1.1.0", kubeconfig="kc", argocd_namespace="cd"))

    out = capsys.readouterr().out
    assert rc == u.EXIT_OK, out
    assert calls[0][:3] == ["kubectl", "--kubeconfig", "kc"]
    assert calls[0][calls[0].index("-n") + 1] == "cd"
    assert calls[0][-1] == STACK_VERSION_JSONPATH
    assert out.startswith(
        "# Upgrade plan: 1.0.0 -> 1.1.0\n\nFROM is secret/dfe-cluster's dfe.hyperi.io/stack_version"
    )
    written = (plan_dir / "1.0.0-to-1.1.0.md").read_text(encoding="utf-8")
    assert "the deploy repo carries no pins.yaml, so apply writes one at its first stage" in written
    assert "1. bootstrap.cert-manager: v1.0.0 -> v1.1.0" in written
    assert list(bundled.iterdir()) == []


def test_cmd_upgrade_plan_without_pins_yaml_or_a_stack_annotation_is_preflight_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, order_path: Path, versions_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    calls = _mock_run(monkeypatch, _proc(0, stdout=""))
    rc = u.cmd_upgrade_plan(_plan_args(deploy=str(tmp_path), to="1.1.0"))
    assert rc == u.EXIT_PREFLIGHT_FAILED
    assert "carries no dfe.hyperi.io/stack_version" in capsys.readouterr().err
    assert len(calls) == 1
    assert not (tmp_path / "upgrades").exists()


def test_the_plan_verb_takes_the_cluster_secret_flags() -> None:
    parser = argparse.ArgumentParser()
    u.add_upgrade_subparser(parser.add_subparsers())
    args = parser.parse_args(["upgrade", "plan", "--deploy", "d", "--kubeconfig", "kc", "--argocd-namespace", "cd"])
    assert (args.kubeconfig, args.argocd_namespace) == ("kc", "cd")
    assert parser.parse_args(["upgrade", "plan", "--deploy", "d"]).argocd_namespace == "argocd"


def test_cmd_upgrade_preflight_without_pins_yaml_says_where_from_came_from(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, order_path: Path, versions_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    monkeypatch.setattr(u, "read_stack_version", lambda *_a, **_k: "1.0.0")
    monkeypatch.setattr(u, "run_preflight", lambda *_a, **_k: [("cluster reachable", True, "ok")])
    rc = u.cmd_upgrade_preflight(_apply_args(deploy=str(tmp_path), to="1.1.0"))
    out = capsys.readouterr().out
    assert rc == u.EXIT_OK
    assert out.startswith("FROM is secret/dfe-cluster's dfe.hyperi.io/stack_version")


# ---------------------------------------------------------------------------
# cmd_upgrade_apply --dry-run -- prints the ordered commands, runs nothing
# ---------------------------------------------------------------------------


def test_cmd_upgrade_apply_dry_run_prints_ordered_commands_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    # Only compat-check runs in a dry-run (preflight is skipped); one call.
    calls = _mock_run(monkeypatch, _proc(0, stdout="compat-check 2.0.0: 0 rule(s) checked"))

    before_pins = (deploy / "pins.yaml").read_text(encoding="utf-8")
    args = _Args(
        deploy=str(deploy), to="2.0.0", dial=None, fixtures=None, live=False,
        kubeconfig=None, argocd_namespace="argocd", clickhouse_namespace="clickhouse",
        clickhouse_selector=u.DEFAULT_CLICKHOUSE_SELECTOR, clickhouse_merge_threshold=300.0,
        clickhouse_credentials=u.DEFAULT_CLICKHOUSE_CREDENTIALS,
        nodes_file=None, backup_marker=u.DEFAULT_BACKUP_MARKER,
        yes=False, push=False, timeout=900, dry_run=True, finalise=False, stop_before=None,
        from_stack=None, target_revision=None, namespace=None,
    )
    rc = u.cmd_upgrade_apply(args)
    assert rc == u.EXIT_OK

    err = capsys.readouterr().err
    # Ordered: stage 1 (bootstrap) before stage 2 (operators) before stage 3 (services).
    i_stage1 = err.index("stage 1/3: 10-bootstrap")
    i_stage2 = err.index("stage 2/3: 20-operators")
    i_stage3 = err.index("stage 3/3: 30-services")
    assert i_stage1 < i_stage2 < i_stage3
    assert '[dry-run] set pins.yaml base.dfe-infra = "2.0.0"' in err
    assert "# verify before-hook: kafka.strimzi.io stored-version conversion" in err
    assert "chore(upgrade): 2.0.0 stage 1 -- bootstrap.cert-manager" in err
    assert "wait for Argo Applications in argocd" in err
    assert (
        "[dry-run] # bootstrap.cert-manager is installed by bootstrap.sh, not Argo; unless it runs v1.1.0: "
        "helm -n cert-manager upgrade cert-manager cert-manager --repo https://charts.jetstack.io --version v1.1.0 "
        "--reset-values --set crds.enabled=true --set config.enableGatewayAPI=true --wait --timeout 5m"
    ) in err
    assert "3 command(s) would run" in err or "command(s) would run" in err

    # Nothing was actually executed beyond the one compat-check call: no git,
    # no kubectl, and pins.yaml on disk is untouched.
    assert len(calls) == 1
    assert (deploy / "pins.yaml").read_text(encoding="utf-8") == before_pins


def test_cmd_upgrade_apply_nothing_to_apply(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    args = _Args(
        deploy=str(deploy), to="1.0.0", dial=None, fixtures=None, live=False,
        kubeconfig=None, argocd_namespace="argocd", clickhouse_namespace="clickhouse",
        clickhouse_selector=u.DEFAULT_CLICKHOUSE_SELECTOR, clickhouse_merge_threshold=300.0,
        clickhouse_credentials=u.DEFAULT_CLICKHOUSE_CREDENTIALS,
        nodes_file=None, backup_marker=u.DEFAULT_BACKUP_MARKER,
        yes=False, push=False, timeout=900, dry_run=True, finalise=False, stop_before=None,
        from_stack=None, target_revision=None, namespace=None,
    )
    rc = u.cmd_upgrade_apply(args)
    assert rc == u.EXIT_OK
    assert "nothing to apply" in capsys.readouterr().out


def test_cmd_upgrade_apply_refuses_when_compat_check_fails(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    _mock_run(monkeypatch, _proc(1, stdout="FAIL   rule-a: x violates >=2\n"))
    args = _Args(
        deploy=str(deploy), to="2.0.0", dial=None, fixtures=None, live=False,
        kubeconfig=None, argocd_namespace="argocd", clickhouse_namespace="clickhouse",
        clickhouse_selector=u.DEFAULT_CLICKHOUSE_SELECTOR, clickhouse_merge_threshold=300.0,
        clickhouse_credentials=u.DEFAULT_CLICKHOUSE_CREDENTIALS,
        nodes_file=None, backup_marker=u.DEFAULT_BACKUP_MARKER,
        yes=False, push=False, timeout=900, dry_run=True, finalise=False, stop_before=None,
        from_stack=None, target_revision=None, namespace=None,
    )
    rc = u.cmd_upgrade_apply(args)
    assert rc == u.EXIT_BLOCKED
    assert "REFUSED" in capsys.readouterr().err


def _apply_args(**overrides: object) -> _Args:
    """The apply _Args shape every dry-run test shares, with per-test overrides."""
    base = dict(
        dial=None, fixtures=None, live=False,
        kubeconfig=None, argocd_namespace="argocd", clickhouse_namespace="clickhouse",
        clickhouse_selector=u.DEFAULT_CLICKHOUSE_SELECTOR, clickhouse_merge_threshold=300.0,
        clickhouse_credentials=u.DEFAULT_CLICKHOUSE_CREDENTIALS,
        nodes_file=None, backup_marker=u.DEFAULT_BACKUP_MARKER,
        yes=False, push=False, timeout=900, dry_run=True, finalise=False, stop_before=None,
        from_stack=None, target_revision=None, namespace=None,
    )
    base.update(overrides)
    return _Args(**base)


def test_cmd_upgrade_apply_dry_run_without_pins_yaml_writes_it_at_the_first_stage_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, order_path: Path, versions_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    calls = _mock_run(monkeypatch, _proc(0, stdout="1.0.0"), _proc(0, stdout="compat-check 1.1.0: 0 rule(s) checked"))
    bundled = tmp_path / "bundled"
    bundled.mkdir()

    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(bundled), to="1.1.0"))

    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert err.startswith("FROM is secret/dfe-cluster's dfe.hyperi.io/stack_version")
    assert err.index('[dry-run] write pins.yaml with base.dfe-infra = "1.1.0"') < err.index("stage 2/2: 20-operators")
    assert err.count("[dry-run] write pins.yaml") == 1
    assert '[dry-run] set pins.yaml base.dfe-infra = "1.1.0"' in err[err.index("stage 2/2: 20-operators") :]
    assert calls[0][-1] == STACK_VERSION_JSONPATH
    assert len(calls) == 2
    assert list(bundled.iterdir()) == []


def test_cmd_upgrade_apply_from_without_pins_yaml_reads_no_cluster(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, order_path: Path, versions_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    calls = _mock_run(monkeypatch, _proc(0, stdout="compat-check 1.1.0: 0 rule(s) checked"))
    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(tmp_path), to="1.1.0", from_stack="1.0.0"))
    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert "FROM is" not in err
    assert '[dry-run] write pins.yaml with base.dfe-infra = "1.1.0"' in err
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# cmd_upgrade_apply --finalise / --stop-before -- dry-run output, stage control
# ---------------------------------------------------------------------------


def test_cmd_upgrade_apply_dry_run_finalise_prints_finalise_line_not_pending(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    _mock_run(monkeypatch, _proc(0, stdout="compat-check 2.0.0: 0 rule(s) checked"))

    args = _apply_args(deploy=str(deploy), to="2.0.0", finalise=True)
    rc = u.cmd_upgrade_apply(args)
    assert rc == u.EXIT_OK
    err = capsys.readouterr().err
    assert "[dry-run] finalise services.kafka-version: metadata.version bump after a soak; one way" in err
    assert "finalise pending (manual, after a soak)" not in err


def test_cmd_upgrade_apply_dry_run_without_finalise_still_prints_pending(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    _mock_run(monkeypatch, _proc(0, stdout="compat-check 2.0.0: 0 rule(s) checked"))

    args = _apply_args(deploy=str(deploy), to="2.0.0", finalise=False)
    rc = u.cmd_upgrade_apply(args)
    assert rc == u.EXIT_OK
    err = capsys.readouterr().err
    assert "finalise pending (manual, after a soak): services.kafka-version" in err


def test_cmd_upgrade_apply_stop_before_stops_short(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    calls = _mock_run(monkeypatch, _proc(0, stdout="compat-check 2.0.0: 0 rule(s) checked"))

    args = _apply_args(deploy=str(deploy), to="2.0.0", stop_before="30-services")
    rc = u.cmd_upgrade_apply(args)
    assert rc == u.EXIT_OK
    err = capsys.readouterr().err
    assert "stage 1/3: 10-bootstrap" in err
    assert "stage 2/3: 20-operators" in err
    assert "stage 3/3: 30-services" not in err
    assert "stopping before stage 3/3 (30-services)" in err
    # Only the one compat-check call ran -- no git, no kubectl, for any stage.
    assert len(calls) == 1


def test_cmd_upgrade_apply_stop_before_unknown_stage_refuses(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    _mock_run(monkeypatch, _proc(0, stdout="compat-check 2.0.0: 0 rule(s) checked"))

    args = _apply_args(deploy=str(deploy), to="2.0.0", stop_before="99-nonexistent")
    rc = u.cmd_upgrade_apply(args)
    assert rc == u.EXIT_BLOCKED
    err = capsys.readouterr().err
    assert "does not match any stage" in err


# ---------------------------------------------------------------------------
# cmd_upgrade_apply --dial -- the stage commit carries the refreshed sizing
# ---------------------------------------------------------------------------


def test_cmd_upgrade_apply_dial_commits_the_refreshed_sizing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    deploy: Path,
    committed_sizing: Path,
    order_path: Path,
    versions_path: Path,
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    monkeypatch.setattr(u, "run_preflight", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(u, "wait_for_argo", lambda *_args, **_kwargs: (True, "converged"))
    _bootstrap_runs(monkeypatch, "v1.1.0")
    calls: list[list[str]] = []
    staged: dict[str, str] = {}

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        if str(u.RESOLVE_SIZING) in cmd:
            _write_resolver_output(cmd)
        if cmd[:1] == ["git"] and "add" in cmd:
            staged["resolved.yaml"] = committed_sizing.read_text(encoding="utf-8")
        return _proc(0)

    monkeypatch.setattr(u, "_run", fake_run)
    # 1.0.0 -> 1.1.0 reaches 10-bootstrap then 20-operators; stopping before the
    # second keeps the run to one stage with no before-hook to satisfy.
    args = _apply_args(
        deploy=str(deploy), to="1.1.0", dial=str(tmp_path / "deployment.yaml"),
        fixtures=str(tmp_path / "fixtures"), yes=True, dry_run=False, stop_before="20-operators",
        namespace="dfe",
    )
    rc = u.cmd_upgrade_apply(args)

    assert rc == u.EXIT_OK
    assert staged["resolved.yaml"] == RESOLVER_OUTPUT["sizing/resolved.yaml"]
    git_add = next(cmd for cmd in calls if cmd[:1] == ["git"] and "add" in cmd)
    # upgrades/ does not exist in this deploy, and naming it would make git stage nothing.
    assert git_add[git_add.index("add") + 1 :] == ["pins.yaml", "sizing"]
    assert not (deploy / "sizing.auto.tfvars.json").exists()


def test_cmd_upgrade_apply_dry_run_dial_names_where_the_sizing_lands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    deploy: Path,
    order_path: Path,
    versions_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    calls = _mock_run(monkeypatch, _proc(0, stdout="compat-check 1.1.0: 0 rule(s) checked"))

    args = _apply_args(
        deploy=str(deploy), to="1.1.0", dial=str(tmp_path / "deployment.yaml"),
        fixtures=str(tmp_path / "fixtures"),
    )
    rc = u.cmd_upgrade_apply(args)

    assert rc == u.EXIT_OK
    err = capsys.readouterr().err
    assert f"then copy <tmp>/sizing/ over {deploy / 'sizing'}" in err
    assert f"--out {deploy}" not in err
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# cmd_upgrade_rollback -- refuses a one-way step by name
# ---------------------------------------------------------------------------


def _rollback_args(**overrides: object) -> _Args:
    """The rollback _Args shape every test shares, with per-test overrides."""
    base = dict(
        kubeconfig=None, push=False, dry_run=True, argocd_namespace="argocd",
        skip_cluster_check=True, target_revision=None,
    )
    base.update(overrides)
    return _Args(**base)


def test_cmd_upgrade_rollback_allowed_before_finalise_marker(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    # Deploy is currently pinned at 2.0.0 (post-upgrade); roll back to 1.0.0
    # crosses services.kafka-version, a finalise-bearing step -- but its
    # finalise has not run (no marker), so the pin move itself is reversible.
    u.bump_pin_file(deploy, "2.0.0")

    args = _rollback_args(deploy=str(deploy), to="1.0.0")
    rc = u.cmd_upgrade_rollback(args)
    assert rc == u.EXIT_OK
    err = capsys.readouterr().err
    assert "REFUSED" not in err
    assert "services.kafka-version" in err
    assert "has not run yet" in err
    assert "soak can be abandoned safely" in err


def test_cmd_upgrade_rollback_refused_once_finalise_marker_exists(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    u.bump_pin_file(deploy, "2.0.0")
    # The forward apply (1.0.0 -> 2.0.0) already ran --finalise for the kafka
    # step: its marker exists, so rollback must refuse by name -- the whole
    # point of keying the refusal on the marker rather than the pin diff.
    u.write_finalise_marker(deploy, "1.0.0", "2.0.0", "services.kafka-version", timestamp="2026-09-11T00:00:00+00:00")

    args = _rollback_args(deploy=str(deploy), to="1.0.0")
    rc = u.cmd_upgrade_rollback(args)
    assert rc == u.EXIT_BLOCKED
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "services.kafka-version" in err
    assert "finalise already ran" in err


def test_cmd_upgrade_rollback_allows_a_reversible_step(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    # 1.1.0 -> 1.0.0 only touches cert-manager and strimzi-kafka-operator,
    # neither of which is one-way in this fixture.
    u.bump_pin_file(deploy, "1.1.0")

    args = _rollback_args(deploy=str(deploy), to="1.0.0")
    rc = u.cmd_upgrade_rollback(args)
    assert rc == u.EXIT_OK
    out = capsys.readouterr().out
    assert "Upgrade plan: 1.1.0 -> 1.0.0" in out


def test_cmd_upgrade_rollback_unconditional_one_way_step_still_refused(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, versions_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A step with `rollback: none` and NO `finalise` note has no soak to wait
    out at all -- it stays refused unconditionally, marker or not."""
    order_path = tmp_path / "upgrade-order-unconditional.yaml"
    order_path.write_text(
        """
stages:
  "10-bootstrap":
    "10-cert-manager":
      key: bootstrap.cert-manager
      rollback: "none"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    u.bump_pin_file(deploy, "1.1.0")

    args = _rollback_args(deploy=str(deploy), to="1.0.0")
    rc = u.cmd_upgrade_rollback(args)
    assert rc == u.EXIT_BLOCKED
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "bootstrap.cert-manager" in err


# ---------------------------------------------------------------------------
# finalise markers -- read/write, and the rollback's live metadata check
# ---------------------------------------------------------------------------


def test_read_finalised_keys_empty_when_no_upgrades_dir(deploy: Path) -> None:
    assert u.read_finalised_keys(deploy) == {}


def test_write_finalise_marker_then_read_back(deploy: Path) -> None:
    path = u.write_finalise_marker(
        deploy, "1.0.0", "2.0.0", "services.kafka-version", timestamp="2026-09-11T00:00:00+00:00"
    )
    assert path == deploy / "upgrades" / "1.0.0-to-2.0.0.finalised"
    assert path.is_file()
    finalised = u.read_finalised_keys(deploy)
    assert finalised == {"services.kafka-version": "2026-09-11T00:00:00+00:00"}


def test_write_finalise_marker_defaults_a_real_timestamp(deploy: Path) -> None:
    path = u.write_finalise_marker(deploy, "1.0.0", "2.0.0", "services.kafka-version")
    line = path.read_text(encoding="utf-8").strip()
    key, _, timestamp = line.partition(" ")
    assert key == "services.kafka-version"
    assert datetime.fromisoformat(timestamp)  # a real ISO-8601 stamp, not a placeholder


def test_write_finalise_marker_updates_in_place_no_duplicate(deploy: Path) -> None:
    u.write_finalise_marker(deploy, "1.0.0", "2.0.0", "services.kafka-version", timestamp="t1")
    path = u.write_finalise_marker(deploy, "1.0.0", "2.0.0", "services.kafka-version", timestamp="t2")
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines == ["services.kafka-version t2"]


def test_write_finalise_marker_keeps_sibling_keys(deploy: Path) -> None:
    u.write_finalise_marker(deploy, "1.0.0", "2.0.0", "services.kafka-version", timestamp="t1")
    u.write_finalise_marker(deploy, "1.0.0", "2.0.0", "operators.strimzi-kafka-operator", timestamp="t2")
    finalised = u.read_finalised_keys(deploy)
    assert finalised == {"services.kafka-version": "t1", "operators.strimzi-kafka-operator": "t2"}


KAFKA_STEP = u.Step(
    stage="30-services", order="10", key="services.kafka-version",
    finalise="metadata.version bump after a soak; one way", rollback="none",
)
KAFKA_MOVE = u.Move(step=KAFKA_STEP, old="4.2.0", new="4.3.1")


def test_run_finalise_hook_confirmed_writes_marker(deploy: Path) -> None:
    ran, detail = u.run_finalise_hook(deploy, KAFKA_MOVE, from_name="1.0.0", to_name="2.0.0", assume_yes=True)
    assert ran is True
    assert "marker written" in detail
    assert "not held" in detail  # no infra/kafka.yaml, so there was no hold to drop
    finalised = u.read_finalised_keys(deploy)
    assert "services.kafka-version" in finalised
    assert datetime.fromisoformat(finalised["services.kafka-version"])


def test_run_finalise_hook_declined_writes_no_marker(monkeypatch: pytest.MonkeyPatch, deploy: Path) -> None:
    monkeypatch.setattr("builtins.input", lambda *_a: "n")
    ran, detail = u.run_finalise_hook(deploy, KAFKA_MOVE, from_name="1.0.0", to_name="2.0.0", assume_yes=False)
    assert ran is False
    assert "declined" in detail
    assert u.read_finalised_keys(deploy) == {}


def _kafka_list(*statuses: dict) -> subprocess.CompletedProcess:
    """`kubectl get kafkas -A -o json` carrying one Kafka CR per status dict."""
    items = [
        {"metadata": {"name": f"dfe-kafka-{i}", "namespace": "strimzi"}, "status": status}
        for i, status in enumerate(statuses)
    ]
    return _proc(0, stdout=json.dumps({"items": items}))


def test_check_rollback_metadata_refuses_a_metadata_version_above_the_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unpinned, Strimzi moved the metadata to 4.3-IV0 the moment the roll to
    # 4.3.1 finished; Kafka 4.2.0 cannot run it, marker or no marker.
    _mock_run(monkeypatch, _kafka_list({"kafkaMetadataVersion": "4.3-IV0"}))
    ok, detail = u.check_rollback_metadata("kc", "4.2.0")
    assert ok is False
    assert "strimzi/dfe-kafka-0 (kafkaMetadataVersion 4.3-IV0)" in detail


def test_check_rollback_metadata_passes_a_held_metadata_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _kafka_list({"kafkaMetadataVersion": "4.2-IV1"}))
    ok, detail = u.check_rollback_metadata("kc", "4.2.0")
    assert ok is True
    assert "Kafka 4.2.0 runs" in detail


def test_check_rollback_metadata_refuses_a_cr_with_no_metadata_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _kafka_list({"kafkaVersion": "4.3.1"}))
    ok, detail = u.check_rollback_metadata("kc", "4.2.0")
    assert ok is False
    assert "unset" in detail


def test_check_rollback_metadata_refuses_an_unreachable_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _proc(1, stderr="Unable to connect to the server\n"))
    ok, detail = u.check_rollback_metadata("kc", "4.2.0")
    assert ok is False
    assert "cannot list Kafka CRs" in detail


def test_check_rollback_metadata_passes_with_no_strimzi_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _proc(1, stderr='error: the server doesn\'t have a resource type "kafkas"'))
    ok, detail = u.check_rollback_metadata("kc", "4.2.0")
    assert ok is True
    assert "no Kafka CRD" in detail


def test_check_rollback_metadata_reads_every_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_run(monkeypatch, _kafka_list({"kafkaMetadataVersion": "4.2-IV1"}))
    u.check_rollback_metadata("kc", "4.2.0")
    assert "-A" in calls[0]


def test_cmd_upgrade_rollback_refuses_without_a_marker_when_the_metadata_moved(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    u.bump_pin_file(deploy, "2.0.0")
    # No marker and no opt-in flag: the live read is the default.
    calls = _mock_run(monkeypatch, _kafka_list({"kafkaMetadataVersion": "4.3-IV0"}))

    args = _rollback_args(deploy=str(deploy), to="1.0.0", kubeconfig="kc", skip_cluster_check=False)
    rc = u.cmd_upgrade_rollback(args)
    assert rc == u.EXIT_BLOCKED
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "Kafka 4.2.0" in err
    assert len(calls) == 1  # refused before the cluster secret was read
    assert u.read_deploy_pin(deploy) == "2.0.0"


def test_cmd_upgrade_rollback_passes_a_held_metadata_version_and_names_the_retarget(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    u.bump_pin_file(deploy, "2.0.0")
    _mock_run(monkeypatch, _kafka_list({"kafkaMetadataVersion": "4.2-IV1"}), _proc(0, stdout="2.0.0"))

    args = _rollback_args(deploy=str(deploy), to="1.0.0", kubeconfig="kc", skip_cluster_check=False)
    rc = u.cmd_upgrade_rollback(args)
    assert rc == u.EXIT_OK
    out = capsys.readouterr().out
    assert (
        "[dry-run] kubectl -n argocd annotate --overwrite secret/dfe-cluster "
        "dfe.hyperi.io/target_revision=1.0.0 dfe.hyperi.io/stack_version=1.0.0"
    ) in out


def test_cmd_upgrade_rollback_retarget_needs_push(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    u.bump_pin_file(deploy, "2.0.0")
    _mock_run(monkeypatch, _kafka_list({"kafkaMetadataVersion": "4.2-IV1"}), _proc(0, stdout="2.0.0"))

    args = _rollback_args(deploy=str(deploy), to="1.0.0", kubeconfig="kc", skip_cluster_check=False, dry_run=False)
    rc = u.cmd_upgrade_rollback(args)
    assert rc == u.EXIT_BLOCKED
    assert "needs --push" in capsys.readouterr().err
    assert u.read_deploy_pin(deploy) == "2.0.0"


def test_cmd_upgrade_rollback_skip_cluster_check_reads_nothing(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    u.bump_pin_file(deploy, "2.0.0")
    calls = _mock_run(monkeypatch)

    rc = u.cmd_upgrade_rollback(_rollback_args(deploy=str(deploy), to="1.0.0", kubeconfig="kc"))
    assert rc == u.EXIT_OK
    assert calls == []
    assert "only the pin moves" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# wait_for_argo -- bounded, injected clock
# ---------------------------------------------------------------------------


def test_wait_for_argo_converges_before_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    not_healthy = _proc(0, stdout=json.dumps({"items": [{"metadata": {"name": "a"}, "status": {"sync": {"status": "OutOfSync"}, "health": {"status": "Progressing"}}}]}))
    healthy = _proc(0, stdout=json.dumps({"items": [{"metadata": {"name": "a"}, "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}}]}))
    _mock_run(monkeypatch, not_healthy, healthy)

    clock = {"t": 0.0}
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    ok, detail = u.wait_for_argo(
        "kc", argocd_namespace="argocd", timeout=120, sleep=fake_sleep, now=lambda: clock["t"]
    )
    assert ok is True
    assert "Synced and Healthy" in detail
    assert sleeps  # it waited at least once between polls


def test_wait_for_argo_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    stuck = _proc(0, stdout=json.dumps({"items": [{"metadata": {"name": "a"}, "status": {"sync": {"status": "OutOfSync"}, "health": {"status": "Progressing"}}}]}))
    _mock_run(monkeypatch, stuck, stuck, stuck, stuck, stuck, stuck, stuck, stuck, stuck, stuck)

    clock = {"t": 0.0}

    def fake_sleep(seconds: float) -> None:
        clock["t"] += seconds

    ok, detail = u.wait_for_argo("kc", argocd_namespace="argocd", timeout=15, sleep=fake_sleep, now=lambda: clock["t"])
    assert ok is False
    assert "still not converged" in detail


# ---------------------------------------------------------------------------
# Rollouts: Argo can read every Application Synced and Healthy while a
# Deployment it synced is still rolling, so a healthy wait reads each rollout.
# ---------------------------------------------------------------------------


def _deployment(
    name: str = "dfe-engine",
    *,
    replicas: int = 1,
    updated: int = 1,
    available: int = 1,
    total: int | None = None,
    generation: int = 2,
    observed: int = 2,
    conditions: list[dict] | None = None,
) -> dict:
    """One Deployment as `kubectl get -o json` lists it; finished unless told otherwise."""
    status: dict[str, object] = {
        "observedGeneration": observed,
        "replicas": updated if total is None else total,
        "updatedReplicas": updated,
        "availableReplicas": available,
    }
    if conditions is not None:
        status["conditions"] = conditions
    return {
        "kind": "Deployment",
        "metadata": {"name": name, "generation": generation},
        "spec": {"replicas": replicas},
        "status": status,
    }


def _statefulset(
    name: str = "dfe-fetcher",
    *,
    replicas: int = 1,
    updated: int = 1,
    available: int = 1,
    observed: int = 2,
    strategy: dict | None = None,
) -> dict:
    """One StatefulSet as `kubectl get -o json` lists it, with the API server's default strategy."""
    return {
        "kind": "StatefulSet",
        "metadata": {"name": name, "generation": 2},
        "spec": {
            "replicas": replicas,
            "updateStrategy": strategy or {"type": "RollingUpdate", "rollingUpdate": {"partition": 0}},
        },
        "status": {"observedGeneration": observed, "updatedReplicas": updated, "availableReplicas": available},
    }


@pytest.mark.parametrize(
    ("item", "why"),
    [
        (_deployment(), ""),
        (_deployment(replicas=0, updated=0, available=0), ""),
        (_deployment(observed=1), "generation 2 not yet observed, at 1"),
        (_deployment(updated=0, total=1), "0 of 1 replicas updated"),
        (_deployment(total=2), "1 old replica(s) still running"),
        (_deployment(available=0), "0 of 1 updated replicas available"),
        (
            _deployment(available=0, conditions=[{"type": "Progressing", "reason": "ProgressDeadlineExceeded"}]),
            "past its progress deadline",
        ),
        (_statefulset(), ""),
        (_statefulset(observed=1), "generation 2 not yet observed, at 1"),
        (_statefulset(replicas=3, updated=2, available=3), "2 of 3 replicas updated"),
        (_statefulset(replicas=3, updated=3, available=2), "2 of 3 replicas available"),
        (
            _statefulset(
                replicas=3, updated=1, available=3,
                strategy={"type": "RollingUpdate", "rollingUpdate": {"partition": 2}},
            ),
            "",
        ),
        (_statefulset(replicas=3, updated=0, available=3, strategy={"type": "OnDelete"}), ""),
    ],
    ids=[
        "deployment-finished", "deployment-scaled-to-zero", "deployment-generation-unobserved",
        "deployment-not-updated", "deployment-old-replica-left", "deployment-new-pod-unavailable",
        "deployment-past-deadline", "statefulset-finished", "statefulset-generation-unobserved",
        "statefulset-not-updated", "statefulset-unavailable", "statefulset-partition-reached",
        "statefulset-on-delete",
    ],
)
def test_rollout_pending_reads_what_kubectl_rollout_status_waits_on(item: dict, why: str) -> None:
    assert u.rollout_pending(item) == why


def test_check_rollouts_names_each_rollout_still_running(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shape apply reported as settled: a surge pod Pending beside the old one, and a new pod not yet up.
    items = [_deployment(total=2), _deployment("dfe-hyperdx", available=0), _deployment("dfe-ui"), _statefulset()]
    calls = _mock_run(monkeypatch, _proc(0, stdout=json.dumps({"items": items})))

    ok, detail = u.check_rollouts("kc", "dfe-from-the-secret")

    assert ok is False
    assert detail == (
        "2 of 4 rollout(s) not finished: deployment/dfe-engine (1 old replica(s) still running), "
        "deployment/dfe-hyperdx (0 of 1 updated replicas available)"
    )
    assert calls[0][:7] == [
        "kubectl", "--kubeconfig", "kc", "-n", "dfe-from-the-secret", "get", "deployments.apps,statefulsets.apps",
    ]


def test_check_rollouts_passes_once_every_rollout_has_finished(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _proc(0, stdout=json.dumps({"items": [_deployment(), _statefulset()]})))
    assert u.check_rollouts(None, "dfe-from-the-secret") == (True, "2 rollout(s) finished")


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        (_proc(0, stdout=json.dumps({"items": []})), "no Deployment or StatefulSet in the DFE namespace"),
        (
            _proc(1, stderr="Unable to connect to the server"),
            "cannot list Deployments and StatefulSets in the DFE namespace: Unable",
        ),
    ],
    ids=["empty-namespace", "unreadable"],
)
def test_check_rollouts_fails_what_it_cannot_show_settled(
    monkeypatch: pytest.MonkeyPatch, response: subprocess.CompletedProcess, detail: str
) -> None:
    _mock_run(monkeypatch, response)
    ok, why = u.check_rollouts(None, "dfe-from-the-secret")
    assert ok is False
    assert why.startswith(detail)
    # apply can have read the namespace off the cluster secret, so no detail repeats it.
    assert "dfe-from-the-secret" not in why


ONE_HEALTHY_APP = _proc(0, stdout=json.dumps({"items": [
    {"metadata": {"name": "a"}, "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}}
]}))


def _clock() -> tuple[dict, list[float], Callable[[float], None]]:
    clock = {"t": 0.0}
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    return clock, sleeps, fake_sleep


def test_wait_for_argo_waits_on_the_rollouts_after_argo_reads_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    rolling = _proc(0, stdout=json.dumps({"items": [_deployment(available=0)]}))
    rolled = _proc(0, stdout=json.dumps({"items": [_deployment()]}))
    _mock_run(monkeypatch, ONE_HEALTHY_APP, rolling, ONE_HEALTHY_APP, rolled)
    clock, sleeps, fake_sleep = _clock()

    ok, detail = u.wait_for_argo(
        "kc", argocd_namespace="argocd", timeout=120, rollout_namespace="dfe", sleep=fake_sleep, now=lambda: clock["t"]
    )

    assert ok is True
    assert detail == "1 Application(s) Synced and Healthy; 1 rollout(s) finished"
    assert len(sleeps) == 1


def test_wait_for_argo_times_out_naming_the_rollout_that_did_not_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    rolling = _proc(0, stdout=json.dumps({"items": [_deployment(available=0)]}))
    _mock_run(monkeypatch, *[ONE_HEALTHY_APP, rolling] * 10)
    clock, _sleeps, fake_sleep = _clock()

    ok, detail = u.wait_for_argo(
        "kc", argocd_namespace="argocd", timeout=15, rollout_namespace="dfe", sleep=fake_sleep, now=lambda: clock["t"]
    )

    assert ok is False
    assert detail == (
        "still not converged after 15s -- 1 Application(s) Synced and Healthy; 1 of 1 rollout(s) not "
        "finished: deployment/dfe-engine (0 of 1 updated replicas available)"
    )


def test_a_wait_that_asks_only_for_synced_reads_no_rollout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_run(monkeypatch, ONE_HEALTHY_APP)
    ok, _ = u.wait_for_argo("kc", argocd_namespace="argocd", timeout=15, rollout_namespace="dfe", require_healthy=False)
    assert ok is True
    assert len(calls) == 1


DFE_NAMESPACE_JSONPATH = "jsonpath={.metadata.annotations.dfe\\.hyperi\\.io/dfe_namespace}"


def test_read_dfe_namespace_reads_only_that_annotation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_run(monkeypatch, _proc(0, stdout="dfe-apps\n"))
    assert u.read_dfe_namespace("kc", "cd") == "dfe-apps"
    assert calls[0][-2:] == ["-o", DFE_NAMESPACE_JSONPATH]
    assert calls[0][calls[0].index("-n") + 1] == "cd"


# ---------------------------------------------------------------------------
# The Strimzi operator upgrade's two traps: a Kafka CR whose Ready condition is
# stale across the lift, and a conversion tool fetched at the wrong version.
# ---------------------------------------------------------------------------


def _kafka_cr(operator_version: str | None, *, name: str = "dfe-kafka", ready: bool = True) -> dict:
    """One Kafka CR as kubectl prints it -- Ready by default, because Ready is
    exactly what stays True and stale while the operator version lags."""
    status: dict[str, object] = {
        "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]
    }
    if operator_version is not None:
        status["operatorLastSuccessfulVersion"] = operator_version
    return {"metadata": {"name": name, "namespace": "kafka"}, "status": status}


def test_check_kafka_operator_version_refuses_a_ready_cr_still_on_the_old_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    _mock_run(monkeypatch, _proc(0, stdout=json.dumps({"items": [_kafka_cr("0.51.0")]})))
    ok, detail = u.check_kafka_operator_version("kc", "1.2.0")
    assert ok is False
    assert "operatorLastSuccessfulVersion 0.51.0" in detail
    assert "kafka/dfe-kafka" in detail


def test_check_kafka_operator_version_refuses_a_cr_carrying_no_version_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    _mock_run(monkeypatch, _proc(0, stdout=json.dumps({"items": [_kafka_cr(None)]})))
    ok, detail = u.check_kafka_operator_version("kc", "1.2.0")
    assert ok is False
    assert "unset" in detail


def test_check_kafka_operator_version_passes_once_every_cr_reports_the_new_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    items = [_kafka_cr("1.2.0"), _kafka_cr("1.2.0", name="other")]
    _mock_run(monkeypatch, _proc(0, stdout=json.dumps({"items": items})))
    ok, detail = u.check_kafka_operator_version("kc", "1.2.0")
    assert ok is True
    assert "2 Kafka CR(s) report operatorLastSuccessfulVersion 1.2.0" in detail


def test_check_kafka_operator_version_reads_every_namespace_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    calls = _mock_run(monkeypatch, _proc(0, stdout=json.dumps({"items": []})))
    u.check_kafka_operator_version("kc", "1.2.0")
    assert "-A" in calls[0]
    assert "-n" not in calls[0]


def test_check_kafka_operator_version_no_crd_is_a_clean_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _proc(1, stderr='the server doesn\'t have a resource type "kafkas"'))
    ok, detail = u.check_kafka_operator_version("kc", "1.2.0")
    assert ok is True
    assert "no Kafka CRD" in detail


def test_wait_for_kafka_operator_version_times_out_on_a_stale_ready_cr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    stale = _proc(0, stdout=json.dumps({"items": [_kafka_cr("0.51.0")]}))
    _mock_run(monkeypatch, *[stale for _ in range(10)])

    clock = {"t": 0.0}

    def fake_sleep(seconds: float) -> None:
        clock["t"] += seconds

    ok, detail = u.wait_for_kafka_operator_version(
        "kc", "1.2.0", timeout=15, sleep=fake_sleep, now=lambda: clock["t"]
    )
    assert ok is False
    assert "still not reconciled" in detail


def test_wait_for_kafka_operator_version_returns_once_the_field_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    stale = _proc(0, stdout=json.dumps({"items": [_kafka_cr("0.51.0")]}))
    moved = _proc(0, stdout=json.dumps({"items": [_kafka_cr("1.2.0")]}))
    _mock_run(monkeypatch, stale, moved)

    clock = {"t": 0.0}
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    ok, detail = u.wait_for_kafka_operator_version(
        "kc", "1.2.0", timeout=600, sleep=fake_sleep, now=lambda: clock["t"]
    )
    assert ok is True
    assert "operatorLastSuccessfulVersion 1.2.0" in detail
    assert sleeps


def test_strimzi_conversion_tool_names_the_version_it_is_given() -> None:
    assert u.strimzi_conversion_tool("0.51.0") == "strimzi-v1-api-conversion-0.51.0.tar.gz"


def test_conversion_tool_line_names_the_from_version_and_refuses_the_target() -> None:
    step = u.Step(stage="20-operators", order="10", key=u.STRIMZI_OPERATOR_KEY, before="convert")
    line = u.conversion_tool_line(u.Move(step=step, old="0.51.0", new="1.2.0"))
    assert "strimzi-v1-api-conversion-0.51.0.tar.gz" in line
    assert "strimzi-v1-api-conversion-1.2.0.tar.gz" not in line
    assert "never the target 1.2.0" in line


def test_check_strimzi_conversion_before_carries_both_the_verdict_and_the_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    doc = json.dumps({"status": {"storedVersions": ["v1"]}})
    _mock_run(monkeypatch, *[_proc(0, stdout=doc) for _ in u.STRIMZI_CRDS])
    step = u.Step(stage="20-operators", order="10", key=u.STRIMZI_OPERATOR_KEY, before="convert")
    ok, detail = u.check_strimzi_conversion_before("kc", u.Move(step=step, old="0.51.0", new="1.2.0"))
    assert ok is True
    assert "store v1 only" in detail
    assert "strimzi-v1-api-conversion-0.51.0.tar.gz" in detail


def test_cmd_upgrade_apply_dry_run_names_the_from_version_tool_and_the_operator_wait(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    _mock_run(monkeypatch, _proc(0, stdout="compat-check 2.0.0: 0 rule(s) checked"))

    args = _apply_args(deploy=str(deploy), to="2.0.0", dry_run=True)
    assert u.cmd_upgrade_apply(args) == u.EXIT_OK

    err = capsys.readouterr().err
    # The fixture moves the operator 0.51.0 -> 1.2.0, so the tool is the FROM one.
    assert "strimzi-v1-api-conversion-0.51.0.tar.gz" in err
    assert "strimzi-v1-api-conversion-1.2.0.tar.gz" not in err
    assert "wait for every Kafka CR to report operatorLastSuccessfulVersion 1.2.0" in err
    # Stage 2 is the operators stage; stages 1 and 3 move no operator pin, so
    # the extra wait must not be emitted for them.
    assert err.count("operatorLastSuccessfulVersion") == 1


# ---------------------------------------------------------------------------
# cmd_upgrade_apply against a REAL git repo -- stage 2+ stage nothing new
# ---------------------------------------------------------------------------
# bump_pin_file always sets base.dfe-infra to the overall TARGET stack, so an
# earlier stage already carries the whole pin move. Only git itself is real
# here; compat-check, preflight and the Argo wait are stubbed the same way
# test_cmd_upgrade_apply_dial_commits_the_refreshed_sizing stubs them.

TWO_STAGE_ORDER_YAML = """
stages:
  "10-first":
    "10-a":
      key: bootstrap.cert-manager
  "20-second":
    "10-b":
      key: services.clickhouse-version
      rollback: "within the same LTS line only"
"""

TWO_STAGE_VERSIONS_YAML = """
current: "2.0.0"
stacks:
  1.0.0:
    bootstrap:
      cert-manager: "v1.0.0"
    services:
      clickhouse-version: "26.3.17.56"
  2.0.0:
    bootstrap:
      cert-manager: "v1.1.0"
    services:
      clickhouse-version: "26.3.32.14"
"""


@pytest.fixture
def real_git_deploy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A real git repo standing in for the deploy checkout, with pins.yaml,
    sizing/ and upgrades/ committed so a stage with nothing new hits a real
    `git commit` on a clean tree rather than a mocked one.

    The identity env vars are set here, before the first commit -- a CI
    runner carries no global git identity, so the commit this fixture makes
    needs its own, the same way the commits under test do.
    """
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "Test")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, "test@example.invalid")
    d = tmp_path / "real-deploy"
    d.mkdir()
    (d / "pins.yaml").write_text(PINS_YAML, encoding="utf-8")
    (d / "sizing").mkdir()
    (d / "sizing" / "resolved.yaml").write_text("locked: {}\n", encoding="utf-8")
    (d / "upgrades").mkdir()
    (d / "upgrades" / ".gitkeep").write_text("", encoding="utf-8")
    assert u._git(d, "init", "-q").returncode == 0
    assert u._git(d, "add", "-A").returncode == 0
    assert u._git(d, "commit", "-q", "-m", "initial").returncode == 0
    return d


def _bootstrap_runs(monkeypatch: pytest.MonkeyPatch, version: str) -> list[str]:
    """Every bootstrap release reads as running chart `version`; returns the releases asked about."""
    asked: list[str] = []

    def read(_kubeconfig: object, release: u.helm_releases.HelmRelease) -> tuple[str, str]:
        asked.append(release.release)
        return version, ""

    monkeypatch.setattr(u, "read_bootstrap_chart", read)
    return asked


def _stub_cluster_facing_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(u, "run_compat_check", lambda *_a, **_k: (True, "ok"))
    monkeypatch.setattr(u, "run_preflight", lambda *_a, **_k: [])
    monkeypatch.setattr(u, "wait_for_argo", lambda *_a, **_k: (True, "converged"))
    # A branch-tracking secret: nothing to retarget, so no --push is needed.
    monkeypatch.setattr(u, "read_target_revision", lambda *_a, **_k: "main")
    monkeypatch.setattr(u, "read_dfe_namespace", lambda *_a, **_k: "dfe")
    # cert-manager already upgraded by hand to the 2.0.0 pin.
    _bootstrap_runs(monkeypatch, "v1.1.0")


def test_cmd_upgrade_apply_stage_two_commits_nothing_new(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    """Stage 1 bumps the pin to the final target; stage 2's `git add` stages
    nothing (same pin, unchanged sizing/upgrades), so `git commit` must be
    skipped there rather than failing the whole apply."""
    order_path = real_git_deploy.parent / "upgrade-order.yaml"
    order_path.write_text(TWO_STAGE_ORDER_YAML, encoding="utf-8")
    versions_path = real_git_deploy.parent / "versions.yaml"
    versions_path.write_text(TWO_STAGE_VERSIONS_YAML, encoding="utf-8")
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    _stub_cluster_facing_calls(monkeypatch)

    args = _apply_args(deploy=str(real_git_deploy), to="2.0.0", yes=True, dry_run=False, push=False)
    rc = u.cmd_upgrade_apply(args)

    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert "dfe-ops upgrade apply OK: 1.0.0 -> 2.0.0 (2 stage(s))" in err
    assert "[DONE] bootstrap.cert-manager runs v1.1.0 (deployment/cert-manager helm.sh/chart)" in err
    assert "stage 2 (20-second) changed nothing" in err
    log = u._git(real_git_deploy, "log", "--oneline").stdout
    assert "stage 1 -- bootstrap.cert-manager" in log
    assert "stage 2 -- services.clickhouse-version" not in log
    assert (real_git_deploy / "pins.yaml").read_text(encoding="utf-8") == PINS_YAML.replace("1.0.0", "2.0.0")


# The engine as it sat while Argo read every Application Healthy: its new pod Pending beside the old one.
ENGINE_ROLLING = json.dumps({"items": [_deployment(total=2), _deployment("dfe-ui")]})
ENGINE_ROLLED = json.dumps({"items": [_deployment(), _deployment("dfe-ui")]})


def _argo_healthy_while(
    monkeypatch: pytest.MonkeyPatch, listings: list[str], *, after: str | None = None
) -> list[str]:
    """Argo reads every Application Healthy throughout, and each read of the rollouts in
    namespace dfe answers the next of `listings` (then `after`, or the last, for ever).
    git runs for real; any other kubectl or helm call fails the test. Returns the
    namespaces each rollout read named."""
    monkeypatch.setattr(u, "wait_for_argo", REAL_WAIT_FOR_ARGO)
    monkeypatch.setattr(u, "check_argo_apps", lambda *_a, **_k: (True, "28 Application(s) Synced and Healthy"))
    monkeypatch.setattr(u, "_SYNC_POLL_INTERVAL", 0.0)
    queue = list(listings)
    read: list[str] = []
    real_run = u._run

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if cmd[:1] == ["kubectl"] and "deployments.apps,statefulsets.apps" in cmd:
            read.append(cmd[cmd.index("-n") + 1])
            listing = queue.pop(0) if queue else (after or listings[-1])
            return _proc(0, stdout=listing)
        if cmd[:1] in (["kubectl"], ["helm"]):
            raise AssertionError(f"test reached a real cluster: {cmd}")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(u, "_run", fake_run)
    return read


def test_apply_reports_ok_only_once_every_rollout_has_finished(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    """apply printed OK over a Deployment Argo called Healthy and kubectl still showed rolling."""
    _two_stage(monkeypatch, real_git_deploy)
    read = _argo_healthy_while(monkeypatch, [ENGINE_ROLLING, ENGINE_ROLLING, ENGINE_ROLLED])

    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(real_git_deploy), to="2.0.0", yes=True, dry_run=False))

    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    # Stage 1 read the rollouts three times before it settled, stage 2 once.
    assert read == ["dfe"] * 4
    assert "argo: 28 Application(s) Synced and Healthy; 2 rollout(s) finished" in err
    assert err.index("2 rollout(s) finished") < err.index("dfe-ops upgrade apply OK")


def test_apply_fails_naming_a_rollout_that_does_not_finish_within_the_timeout(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    _two_stage(monkeypatch, real_git_deploy)
    _argo_healthy_while(monkeypatch, [ENGINE_ROLLING])

    rc = u.cmd_upgrade_apply(_apply_args(
        deploy=str(real_git_deploy), to="2.0.0", yes=True, dry_run=False, timeout=0.05,
    ))

    err = capsys.readouterr().err
    assert rc == u.EXIT_BLOCKED, err
    assert (
        "28 Application(s) Synced and Healthy; 1 of 2 rollout(s) not finished: "
        "deployment/dfe-engine (1 old replica(s) still running)"
    ) in err
    assert "FAILED at stage 1: Argo and the rollouts did not settle" in err
    assert "apply OK" not in err


def test_apply_waits_on_the_namespace_the_cluster_secret_names(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    _two_stage(monkeypatch, real_git_deploy)
    asked: list[tuple[object, str]] = []

    def read_namespace(kubeconfig: object, namespace: str) -> str:
        asked.append((kubeconfig, namespace))
        return "dfe-apps"

    monkeypatch.setattr(u, "read_dfe_namespace", read_namespace)
    waits: list[str] = []

    def wait(*_a: object, rollout_namespace: str = "", **_k: object) -> tuple[bool, str]:
        waits.append(rollout_namespace)
        return True, "ok"

    monkeypatch.setattr(u, "wait_for_argo", wait)

    rc = u.cmd_upgrade_apply(_apply_args(
        deploy=str(real_git_deploy), to="2.0.0", yes=True, dry_run=False, kubeconfig="kc", argocd_namespace="cd",
    ))

    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert asked == [("kc", "cd")]
    assert waits == ["dfe-apps", "dfe-apps"]
    # It came out of a Secret, so it is named by where it came from and never printed.
    assert (
        "rollouts: every Deployment and StatefulSet in the namespace secret/dfe-cluster's "
        "dfe.hyperi.io/dfe_namespace names"
    ) in err
    assert "dfe-apps" not in err


def test_namespace_names_the_rollouts_without_reading_the_cluster_secret(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    _two_stage(monkeypatch, real_git_deploy)

    def unread(*_a: object) -> str:
        raise AssertionError("--namespace names the namespace; the cluster secret is not read for it")

    monkeypatch.setattr(u, "read_dfe_namespace", unread)
    read = _argo_healthy_while(monkeypatch, [ENGINE_ROLLED])

    rc = u.cmd_upgrade_apply(_apply_args(
        deploy=str(real_git_deploy), to="2.0.0", yes=True, dry_run=False, namespace="dfe-named",
    ))

    assert rc == u.EXIT_OK, capsys.readouterr().err
    assert read == ["dfe-named", "dfe-named"]


def test_apply_refuses_before_anything_moves_when_no_namespace_is_known(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    _two_stage(monkeypatch, real_git_deploy)
    monkeypatch.setattr(u, "read_dfe_namespace", lambda *_a: "")

    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(real_git_deploy), to="2.0.0", yes=True, dry_run=False))

    err = capsys.readouterr().err
    assert rc == u.EXIT_BLOCKED, err
    assert (
        "REFUSED -- secret/dfe-cluster carries no dfe.hyperi.io/dfe_namespace, so no rollout can be waited on "
        "-- pass --namespace"
    ) in err
    assert u.read_deploy_pin(real_git_deploy) == "1.0.0"
    assert u._git(real_git_deploy, "rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_a_dry_run_names_the_rollouts_each_wait_waits_on(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    monkeypatch.setattr(u, "run_compat_check", lambda *_a, **_k: (True, "ok"))

    assert u.cmd_upgrade_apply(_apply_args(deploy=str(deploy), to="2.0.0")) == u.EXIT_OK
    unnamed = capsys.readouterr().err
    assert u.cmd_upgrade_apply(_apply_args(deploy=str(deploy), to="2.0.0", namespace="dfe-named")) == u.EXIT_OK
    named = capsys.readouterr().err

    # One wait per stage, all three before the cluster secret has been read.
    assert unnamed.count(
        "wait for Argo Applications in argocd, then every Deployment and StatefulSet in the namespace "
        "secret/dfe-cluster's dfe.hyperi.io/dfe_namespace names to finish rolling out (timeout 900s)"
    ) == 3
    assert (
        "wait for Argo Applications to leave 1.0.0, each reconciled after the retarget, then every Deployment "
        "and StatefulSet in dfe-named to finish rolling out (timeout 900s)"
    ) in named


# An Application Argo last compared before any change this suite makes.
OLD_RECONCILE = "2020-01-01T00:00:00Z"


@pytest.fixture
def pushed_real_git_deploy(real_git_deploy: Path, tmp_path: Path) -> Path:
    """real_git_deploy with a bare local remote as its upstream, so `git push` and `@{upstream}` are real."""
    remote = tmp_path / "real-remote.git"
    assert u._run(["git", "init", "-q", "--bare", str(remote)]).returncode == 0
    for args in (["remote", "add", "origin", str(remote)], ["push", "-q", "-u", "origin", "HEAD"]):
        assert u._git(real_git_deploy, *args).returncode == 0, args
    return real_git_deploy


def _argo_comparing_after_the_push(monkeypatch: pytest.MonkeyPatch, *, stale_reads: int | None) -> dict[str, int]:
    """Argo reads the one Application Synced and Healthy throughout, but stamps `reconciledAt` as it would.

    The real wait and the real Application read run, and so does git. Until the deploy repo is pushed the
    Application carries an old stamp, and after it the next `stale_reads` reads still do, since Argo has not
    compared the new commit yet. Then it carries a fresh one (never, for `stale_reads=None`). Returns the
    count of Application reads, and of those made after the first push.
    """
    monkeypatch.setattr(u, "wait_for_argo", REAL_WAIT_FOR_ARGO)
    monkeypatch.setattr(u, "_SYNC_POLL_INTERVAL", 0.0)
    seen = {"pushes": 0, "reads": 0, "reads_after_push": 0}
    real_run = u._run

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if cmd[:1] == ["git"] and cmd[-1] == "push":
            seen["pushes"] += 1
        listed = cmd[cmd.index("get") + 1 :][:1] if "get" in cmd else []
        if cmd[:1] == ["kubectl"] and listed == ["applications.argoproj.io"]:
            seen["reads"] += 1
            seen["reads_after_push"] += 1 if seen["pushes"] else 0
            compared = stale_reads is not None and seen["reads_after_push"] > stale_reads
            stamp = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ") if compared else OLD_RECONCILE
            return _apps(_reconciled_app("kafka-dfe", stamp))
        if cmd[:1] == ["kubectl"] and "deployments.apps,statefulsets.apps" in cmd:
            return _proc(0, stdout=ENGINE_ROLLED)
        if cmd[:1] in (["kubectl"], ["helm"]):
            raise AssertionError(f"test reached a real cluster: {cmd}")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(u, "_run", fake_run)
    return seen


def test_apply_does_not_trust_a_healthy_app_argo_has_not_compared_since_the_push(
    monkeypatch: pytest.MonkeyPatch, pushed_real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    """Both waits passed on the Synced and Healthy Argo reported from before the pushed commit."""
    _two_stage(monkeypatch, pushed_real_git_deploy)
    seen = _argo_comparing_after_the_push(monkeypatch, stale_reads=2)

    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(pushed_real_git_deploy), to="2.0.0", yes=True, dry_run=False, push=True))

    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    # Stage 1 pushed a commit and read the Application three times, twice on the old stamp.
    # Stage 2 pushed nothing new, so it asked Argo for nothing and read once.
    assert seen["pushes"] == 2
    assert seen["reads"] == 4
    assert err.count("1 Application(s) Synced and Healthy, each reconciled since this change; 2 rollout(s) finished") == 1
    assert err.count("1 Application(s) Synced and Healthy; 2 rollout(s) finished") == 1


def test_apply_fails_naming_the_app_argo_never_compared_against_the_push(
    monkeypatch: pytest.MonkeyPatch, pushed_real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    _two_stage(monkeypatch, pushed_real_git_deploy)
    _argo_comparing_after_the_push(monkeypatch, stale_reads=None)

    rc = u.cmd_upgrade_apply(_apply_args(
        deploy=str(pushed_real_git_deploy), to="2.0.0", yes=True, dry_run=False, push=True, timeout=0.05,
    ))

    err = capsys.readouterr().err
    assert rc == u.EXIT_BLOCKED, err
    assert f"1 app(s) not Synced/Healthy: kafka-dfe (last reconciled {OLD_RECONCILE}, before this change)" in err
    assert "FAILED at stage 1: Argo and the rollouts did not settle" in err
    assert "apply OK" not in err


def test_apply_without_push_asks_for_no_reconcile_because_argo_has_nothing_new(
    monkeypatch: pytest.MonkeyPatch, pushed_real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    _two_stage(monkeypatch, pushed_real_git_deploy)
    seen = _argo_comparing_after_the_push(monkeypatch, stale_reads=None)

    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(pushed_real_git_deploy), to="2.0.0", yes=True, dry_run=False))

    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert seen == {"pushes": 0, "reads": 2, "reads_after_push": 0}
    assert "each reconciled" not in err


def test_apply_pushes_a_commit_an_earlier_run_left_and_waits_on_the_reconcile(
    monkeypatch: pytest.MonkeyPatch, pushed_real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    """A rerun commits nothing new, but its push still carries the earlier run's commit."""
    _two_stage(monkeypatch, pushed_real_git_deploy)
    _argo_comparing_after_the_push(monkeypatch, stale_reads=None)
    assert u.cmd_upgrade_apply(_apply_args(deploy=str(pushed_real_git_deploy), to="2.0.0", yes=True, dry_run=False)) == u.EXIT_OK
    capsys.readouterr()

    rc = u.cmd_upgrade_apply(_apply_args(
        deploy=str(pushed_real_git_deploy), to="2.0.0", from_stack="1.0.0", yes=True, dry_run=False, push=True,
        timeout=0.05,
    ))

    err = capsys.readouterr().err
    assert rc == u.EXIT_BLOCKED, err
    assert "stage 1 (10-first) changed nothing" in err
    assert f"kafka-dfe (last reconciled {OLD_RECONCILE}, before this change)" in err


def test_a_pushing_dry_run_names_the_reconcile_each_wait_asks_for(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    monkeypatch.setattr(u, "run_compat_check", lambda *_a, **_k: (True, "ok"))

    assert u.cmd_upgrade_apply(_apply_args(deploy=str(deploy), to="2.0.0", namespace="dfe-named", push=True)) == u.EXIT_OK

    assert capsys.readouterr().err.count(
        "wait for Argo Applications in argocd, each reconciled after any change it pushes, then every "
        "Deployment and StatefulSet in dfe-named to finish rolling out (timeout 900s)"
    ) == 3


# ---------------------------------------------------------------------------
# Bootstrap-section pins: bootstrap.sh installs them and Argo never does, so a
# stage that moves only the pin is not done until the cluster runs it.
# ---------------------------------------------------------------------------

CERT_MANAGER_UPGRADE = (
    "helm --kubeconfig kc -n cert-manager upgrade cert-manager cert-manager --repo https://charts.jetstack.io "
    "--version v1.1.0 --reset-values --set crds.enabled=true --set config.enableGatewayAPI=true "
    "--wait --timeout 5m"
)
DOMAIN = "single.dfe.test"
# Every value bootstrap.sh's [6/7] install sets, its login values piped in on stdin, and the
# domain read off the cluster secret as the command runs.
ARGOCD_UPGRADE = (
    "DFE_DOMAIN=\"$(kubectl -n argocd get secret dfe-cluster -o "
    "'jsonpath={.metadata.annotations.dfe\\.hyperi\\.io/domain}')\" && test -n \"$DFE_DOMAIN\" && "
    f"python3 {shlex.quote(str(BOOTSTRAP_DIR / 'argocd_login.py'))} values --domain \"$DFE_DOMAIN\" | "
    "helm -n argocd upgrade argocd argo-cd --repo https://argoproj.github.io/argo-helm --version 10.10.0 "
    "--reset-values --set redis.enabled=false --set externalRedis.host=valkey.argocd.svc.cluster.local "
    "--set externalRedis.port=6379 --set-string global.domain=argocd.\"$DFE_DOMAIN\" "
    "--set-string 'configs.params.server\\.insecure=true' "
    "--set-string 'configs.params.reposerver\\.disable\\.git\\.modules=true' "
    "--set-string 'configs.cm.timeout\\.reconciliation=300s' "
    "--set-string 'configs.params.controller\\.self\\.heal\\.timeout\\.seconds=30' "
    "--set-string 'configs.params.controller\\.repo\\.server\\.timeout\\.seconds=60' "
    "--set-string 'configs.params.controller\\.diff\\.server\\.side=true' "
    "--values - --wait --timeout 10m"
)
ARGO_STEP = u.Step(stage="10-bootstrap", order="30", key="bootstrap.argocd")
# The user-supplied values bootstrap.sh's step [6/7] installs Argo with.
BOOTSTRAP_ARGO_VALUES = {"redis": {"enabled": False}, "externalRedis": {"host": "valkey.argocd.svc.cluster.local"}}
ARGO_RELEASES = [{"name": "argocd", "namespace": "argocd", "chart": "argo-cd-10.9.6"}]


def _two_stage(monkeypatch: pytest.MonkeyPatch, deploy: Path) -> None:
    order_path = deploy.parent / "upgrade-order.yaml"
    order_path.write_text(TWO_STAGE_ORDER_YAML, encoding="utf-8")
    versions_path = deploy.parent / "versions.yaml"
    versions_path.write_text(TWO_STAGE_VERSIONS_YAML, encoding="utf-8")
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    _stub_cluster_facing_calls(monkeypatch)


# Both stacks pin cert-manager v1.1.0, so the plan moves ClickHouse alone.
HELD_BOOTSTRAP_VERSIONS_YAML = TWO_STAGE_VERSIONS_YAML.replace('cert-manager: "v1.0.0"', 'cert-manager: "v1.1.0"')


def _held_bootstrap(monkeypatch: pytest.MonkeyPatch, deploy: Path) -> None:
    _two_stage(monkeypatch, deploy)
    versions_path = deploy.parent / "versions.yaml"
    versions_path.write_text(HELD_BOOTSTRAP_VERSIONS_YAML, encoding="utf-8")


def test_a_bootstrap_pin_an_earlier_upgrade_left_uninstalled_still_blocks_ok(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    """A later plan moving no bootstrap pin reported OK over an Argo CD still on the previous stack's chart."""
    _held_bootstrap(monkeypatch, real_git_deploy)
    asked = _bootstrap_runs(monkeypatch, "v1.0.0")

    rc = u.cmd_upgrade_apply(_apply_args(
        deploy=str(real_git_deploy), to="2.0.0", yes=True, dry_run=False, kubeconfig="kc",
    ))

    err = capsys.readouterr().err
    assert rc == u.EXIT_BLOCKED, err
    assert "stage 1/1: 20-second" in err
    assert asked == ["cert-manager"]
    assert "=== bootstrap releases this plan does not move ===" in err
    assert (
        "[PENDING] bootstrap.cert-manager v1.1.0 (unchanged by this upgrade): bootstrap.sh installs it, not "
        f"Argo, and the cluster runs v1.0.0. Run, where this deploy installed it: {CERT_MANAGER_UPGRADE}"
    ) in err
    assert "NOT complete -- bootstrap.cert-manager" in err
    assert "apply OK" not in err


def test_a_bootstrap_pin_the_plan_does_not_move_and_the_cluster_runs_is_done(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    _held_bootstrap(monkeypatch, real_git_deploy)
    _bootstrap_runs(monkeypatch, "v1.1.0")

    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(real_git_deploy), to="2.0.0", yes=True, dry_run=False))

    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert "[DONE] bootstrap.cert-manager runs v1.1.0" in err
    assert "dfe-ops upgrade apply OK: 1.0.0 -> 2.0.0 (1 stage(s))" in err


def test_a_dry_run_names_the_read_of_a_bootstrap_pin_the_plan_does_not_move(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    _held_bootstrap(monkeypatch, real_git_deploy)
    asked = _bootstrap_runs(monkeypatch, "v1.0.0")

    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(real_git_deploy), to="2.0.0"))

    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert asked == []
    assert (
        "[dry-run] # bootstrap.cert-manager is installed by bootstrap.sh, not Argo; unless it runs v1.1.0: "
        "helm -n cert-manager upgrade cert-manager cert-manager"
    ) in err


def test_a_bootstrap_pin_the_target_stack_lacks_is_not_read(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    _two_stage(monkeypatch, real_git_deploy)
    versions_path = real_git_deploy.parent / "versions.yaml"
    unpinned = TWO_STAGE_VERSIONS_YAML
    for version in ("v1.0.0", "v1.1.0"):
        unpinned = unpinned.replace(f'    bootstrap:\n      cert-manager: "{version}"\n', "")
    versions_path.write_text(unpinned, encoding="utf-8")
    asked = _bootstrap_runs(monkeypatch, "v1.0.0")

    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(real_git_deploy), to="2.0.0", yes=True, dry_run=False))

    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert asked == []
    assert "bootstrap releases this plan does not move" not in err


def test_a_bootstrap_pin_the_cluster_does_not_run_ends_not_complete_with_the_command(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    """Argo CD stayed on its old chart while apply reported the bootstrap stage done and the run OK."""
    _two_stage(monkeypatch, real_git_deploy)
    asked = _bootstrap_runs(monkeypatch, "v1.0.0")

    rc = u.cmd_upgrade_apply(_apply_args(
        deploy=str(real_git_deploy), to="2.0.0", yes=True, dry_run=False, kubeconfig="kc",
    ))

    err = capsys.readouterr().err
    assert rc == u.EXIT_BLOCKED, err
    assert asked == ["cert-manager"]
    assert (
        "[PENDING] bootstrap.cert-manager v1.0.0 -> v1.1.0: bootstrap.sh installs it, not Argo, and the "
        f"cluster runs v1.0.0. Run, where this deploy installed it: {CERT_MANAGER_UPGRADE}"
    ) in err
    # The walk carries on past it: the later stage still runs.
    assert "stage 2/2: 20-second" in err
    assert "NOT complete -- bootstrap.cert-manager not shown running the pinned chart" in err
    assert "re-run this apply with --from 1.0.0" in err
    assert "apply OK" not in err
    assert u.read_deploy_pin(real_git_deploy) == "2.0.0"


def test_a_stop_before_after_a_pending_bootstrap_pin_is_not_ok_either(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    _two_stage(monkeypatch, real_git_deploy)
    _bootstrap_runs(monkeypatch, "v1.0.0")

    rc = u.cmd_upgrade_apply(_apply_args(
        deploy=str(real_git_deploy), to="2.0.0", yes=True, dry_run=False, stop_before="20-second",
    ))

    err = capsys.readouterr().err
    assert rc == u.EXIT_BLOCKED, err
    assert "stopping before stage 2/2 (20-second)" in err
    assert "NOT complete -- bootstrap.cert-manager" in err


def test_the_resumed_apply_confirms_a_bootstrap_pin_once_it_runs(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    _two_stage(monkeypatch, real_git_deploy)
    _bootstrap_runs(monkeypatch, "v1.0.0")
    assert u.cmd_upgrade_apply(_apply_args(
        deploy=str(real_git_deploy), to="2.0.0", yes=True, dry_run=False,
    )) == u.EXIT_BLOCKED
    capsys.readouterr()

    _bootstrap_runs(monkeypatch, "v1.1.0")
    rc = u.cmd_upgrade_apply(_apply_args(
        deploy=str(real_git_deploy), to="2.0.0", from_stack="1.0.0", yes=True, dry_run=False,
    ))

    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert "[DONE] bootstrap.cert-manager runs v1.1.0" in err
    assert "dfe-ops upgrade apply OK: 1.0.0 -> 2.0.0" in err


def test_read_bootstrap_chart_reads_the_chart_label(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = {"metadata": {"labels": {"helm.sh/chart": "argo-cd-10.9.6", "app.kubernetes.io/version": "v3.5.3"}}}
    calls = _mock_run(monkeypatch, _proc(0, stdout=json.dumps(doc)))

    assert u.read_bootstrap_chart("kc", u.BOOTSTRAP_RELEASES["bootstrap.argocd"]) == ("10.9.6", "")
    assert calls[0][:3] == ["kubectl", "--kubeconfig", "kc"]
    assert calls[0][3:8] == ["-n", "argocd", "get", "deployment", "argocd-server"]


@pytest.mark.parametrize(
    "labels",
    [{}, {"app.kubernetes.io/managed-by": "Helm"}, {"helm.sh/chart": "cert-manager-v1.21.2"}],
)
def test_a_deployment_without_this_charts_label_reads_as_adopted(
    monkeypatch: pytest.MonkeyPatch, labels: dict[str, str]
) -> None:
    _mock_run(monkeypatch, _proc(0, stdout=json.dumps({"metadata": {"labels": labels}})))
    running, why = u.read_bootstrap_chart(None, u.BOOTSTRAP_RELEASES["bootstrap.argocd"])
    assert running == ""
    assert "carries no helm.sh/chart label for argo-cd" in why
    assert "adopted" in why


def test_an_unreadable_deployment_is_pending_with_the_reason_and_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DFE_VALKEY_SERVICE", raising=False)
    monkeypatch.setattr(u, "argocd_installed_by_bootstrap", lambda _kc: (True, "ours"))
    _mock_run(monkeypatch, _proc(1, stderr='Error from server (NotFound): deployments.apps "argocd-server" not found'))
    state, detail = u.check_bootstrap_move(None, u.Move(step=ARGO_STEP, old="10.9.6", new="10.10.0"))
    assert state == u.BOOTSTRAP_PENDING
    assert "cannot read deployment/argocd-server in argocd: Error from server (NotFound)" in detail
    assert detail.endswith(f"Run: {ARGOCD_UPGRADE}")


def test_a_bootstrap_pin_with_no_known_release_points_at_bootstrap_sh() -> None:
    step = u.Step(stage="10-bootstrap", order="40", key="bootstrap.metallb")
    state, detail = u.check_bootstrap_move(None, u.Move(step=step, old="0.15.0", new="0.16.1"))
    assert state == u.BOOTSTRAP_PENDING
    assert "re-run bootstrap/bootstrap.sh with DFE_STACK_VERSION" in detail


def test_bootstraps_own_argo_is_recognised_the_way_bootstrap_sh_recognises_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DFE_VALKEY_SERVICE", raising=False)
    calls = _mock_run(
        monkeypatch, _proc(0, stdout=json.dumps(ARGO_RELEASES)), _proc(0, stdout=json.dumps(BOOTSTRAP_ARGO_VALUES))
    )
    ours, why = u.argocd_installed_by_bootstrap("kc")
    assert ours is True, why
    assert calls[0] == [
        "helm", "list", "--namespace", "argocd", "--filter", "^argocd$", "-o", "json", "--kubeconfig", "kc",
    ]
    assert calls[1][:4] == ["helm", "get", "values", "argocd"]


def test_a_stock_argo_install_reads_adopted_and_gets_no_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host's `helm install argocd argo/argo-cd` carries the same release, chart and label."""
    calls = _mock_run(monkeypatch, _proc(0, stdout=json.dumps(ARGO_RELEASES)), _proc(0, stdout="null"))
    state, detail = u.check_bootstrap_move("kc", u.Move(step=ARGO_STEP, old="10.9.6", new="10.10.0"))
    assert state == u.BOOTSTRAP_ADOPTED
    assert detail.endswith("so this bootstrap did not install it, so its owner upgrades it")
    assert "helm --kubeconfig" not in detail
    assert [c[0] for c in calls] == ["helm", "helm"]  # no kubectl read of the version


def test_an_argo_helm_cannot_read_is_pending_with_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DFE_VALKEY_SERVICE", raising=False)
    monkeypatch.setattr(u, "read_bootstrap_chart", lambda *_a: ("10.9.6", ""))

    def no_helm(_cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        raise FileNotFoundError("helm")

    monkeypatch.setattr(u, "_run", no_helm)
    state, detail = u.check_bootstrap_move(None, u.Move(step=ARGO_STEP, old="10.9.6", new="10.10.0"))
    assert state == u.BOOTSTRAP_PENDING
    assert (
        "the cluster runs 10.9.6. Run (cannot list helm releases in argocd: helm is not on PATH), "
        f"where this deploy installed it: {ARGOCD_UPGRADE}"
    ) in detail


def test_an_argo_on_the_pin_is_done(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(u, "argocd_installed_by_bootstrap", lambda _kc: (True, "ours"))
    monkeypatch.setattr(u, "read_bootstrap_chart", lambda *_a: ("10.10.0", ""))
    state, detail = u.check_bootstrap_move(None, u.Move(step=ARGO_STEP, old="10.9.6", new="10.10.0"))
    assert (state, detail) == (u.BOOTSTRAP_DONE, "bootstrap.argocd runs 10.10.0 (deployment/argocd-server helm.sh/chart)")


def test_every_bootstrap_release_matches_the_install_bootstrap_sh_runs() -> None:
    """The printed upgrade must name the release, chart, repository and namespace bootstrap.sh installed."""
    script = (REPO_ROOT / "bootstrap" / "bootstrap.sh").read_text(encoding="utf-8")
    steps = {step.key for step in u.load_steps() if step.key.startswith(u.BOOTSTRAP_PREFIX)}
    assert steps == set(u.BOOTSTRAP_RELEASES)
    for key, release in u.BOOTSTRAP_RELEASES.items():
        install = re.search(
            rf"helm upgrade --install {re.escape(release.release)} (\S+)/{re.escape(release.chart)} \\\n"
            rf"\s+--namespace {re.escape(release.namespace)} ",
            script,
        )
        assert install, key
        assert re.search(rf"helm repo add {re.escape(install.group(1))} {re.escape(release.repo)} ", script), key
        assert re.search(r"--timeout (\S+)", script[install.end():]).group(1) == release.timeout, key
        # The deployment bootstrap.sh's own detect-or-install gate reads.
        gate = rf"dfe_should_install \S+ \S+ {re.escape(release.namespace)} {re.escape(release.deployment)}\b"
        assert re.search(gate, script), key


# ---------------------------------------------------------------------------
# A printed bootstrap upgrade is --reset-values plus exactly the values
# bootstrap.sh's install sets, both read from bootstrap/helm_releases.py.
# ---------------------------------------------------------------------------

SPLICE = '${DFE_RELEASE_VALUES[@]+"${DFE_RELEASE_VALUES[@]}"}'


def _function(lines: list[str], name: str) -> list[str]:
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return lines[start:end + 1]


def _install(release: u.helm_releases.HelmRelease) -> tuple[str, str]:
    """(the dfe_release_values call bootstrap.sh makes for `release`, the helm install it feeds)."""
    script = BOOTSTRAP_SH.read_text(encoding="utf-8")
    start = re.search(rf"helm upgrade --install {re.escape(release.release)} ", script).start()
    install = script[start:script.index("--timeout", start)]
    calls = list(re.finditer(r"(?m)^\s*(dfe_release_values .+)$", script[:start]))
    assert calls, f"bootstrap.sh calls dfe_release_values before no install of {release.release}"
    return calls[-1].group(1).strip(), install


def _bootstrap_values(release: u.helm_releases.HelmRelease) -> list[str]:
    """The values bootstrap.sh's own install of `release` passes helm: its helper, run on its call line."""
    lines = BOOTSTRAP_SH.read_text(encoding="utf-8").splitlines()
    call, _install_text = _install(release)
    script = "\n".join([
        "set -euo pipefail", *_function(lines, "dfe_release_values"), call, f'printf "%s\\n" {SPLICE}',
    ])
    env = {**os.environ, "SCRIPT_DIR": str(BOOTSTRAP_DIR), "DFE_DOMAIN": DOMAIN, "VALKEY_SVC": "valkey"}
    out = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return [line for line in out.stdout.splitlines() if line]


def _printed_helm(printed: str) -> list[str]:
    """The helm call of a printed upgrade, with DOMAIN where it reads the domain at run time."""
    return shlex.split(printed.split(" | ")[-1].replace('"$DFE_DOMAIN"', DOMAIN))


def _printed_values(printed: str) -> list[str]:
    """The value arguments a printed upgrade passes helm, between --reset-values and --wait."""
    helm = _printed_helm(printed)
    return helm[helm.index("--reset-values") + 1 : helm.index("--wait")]


@pytest.mark.parametrize("key", sorted(u.BOOTSTRAP_RELEASES))
def test_the_printed_upgrade_sets_exactly_what_bootstrap_sh_installs_with(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.delenv("DFE_VALKEY_SERVICE", raising=False)
    release = u.BOOTSTRAP_RELEASES[key]
    call, install = _install(release)

    # The splice is the install's only source of values, so nothing hand-written can differ.
    assert re.findall(r"--set\S*|--values|\s-f\s", install) == [], install
    assert SPLICE in install
    assert call.split()[1] == release.release

    printed = u.bootstrap_upgrade_command(release, "9.9.9", "kc")
    assert _printed_values(printed) == _bootstrap_values(release)
    assert "--reset-then-reuse-values" not in printed
    assert "--reuse-values" not in printed


def test_the_cert_manager_upgrade_is_the_one_the_stack_note_and_bootstrap_sh_agree_on() -> None:
    printed = u.bootstrap_upgrade_command(u.BOOTSTRAP_RELEASES["bootstrap.cert-manager"], "v1.21.2", None)
    assert printed == (
        "helm -n cert-manager upgrade cert-manager cert-manager --repo https://charts.jetstack.io --version v1.21.2 "
        "--reset-values --set crds.enabled=true --set config.enableGatewayAPI=true --wait --timeout 5m"
    )


def test_the_argo_upgrade_pipes_in_the_login_bootstrap_sh_feeds_its_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DFE_VALKEY_SERVICE", raising=False)
    printed = u.bootstrap_upgrade_command(u.BOOTSTRAP_RELEASES["bootstrap.argocd"], "10.10.0", None)
    assert printed == ARGOCD_UPGRADE
    script = BOOTSTRAP_SH.read_text(encoding="utf-8")
    assert 'python3 "${SCRIPT_DIR}/argocd_login.py" values --domain "${DFE_DOMAIN}"' in script
    assert re.search(r'--wait --timeout 10m <<<"\$\{ARGOCD_LOGIN_VALUES\}"', script)


def test_the_argo_upgrade_follows_a_renamed_cache_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DFE_VALKEY_SERVICE", "cache")
    printed = u.bootstrap_upgrade_command(u.BOOTSTRAP_RELEASES["bootstrap.argocd"], "10.10.0", None)
    assert "externalRedis.host=cache.argocd.svc.cluster.local" in _printed_helm(printed)


def test_the_argo_upgrade_reads_the_domain_off_the_cluster_secret_it_was_told(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value read from a Secret is never printed, so the command reads the domain itself."""
    printed = u.bootstrap_upgrade_command(u.BOOTSTRAP_RELEASES["bootstrap.argocd"], "10.10.0", "kc", "cd")
    assert printed.startswith(
        "DFE_DOMAIN=\"$(kubectl --kubeconfig kc -n cd get secret dfe-cluster -o "
        "'jsonpath={.metadata.annotations.dfe\\.hyperi\\.io/domain}')\" && test -n \"$DFE_DOMAIN\" && "
    )


def test_an_upgrade_no_value_of_which_names_the_domain_reads_none() -> None:
    printed = u.bootstrap_upgrade_command(u.BOOTSTRAP_RELEASES["bootstrap.cert-manager"], "v1.1.0", "kc")
    assert printed == CERT_MANAGER_UPGRADE


FAKE_KUBECTL = """#!/usr/bin/env python3
import os, sys
if "secret" in sys.argv and "dfe-cluster" in sys.argv:
    print(os.environ["FAKE_DOMAIN"], end="")
"""

FAKE_HELM = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_LOG"], "w", encoding="utf-8") as log:
    json.dump({"argv": sys.argv[1:], "stdin": sys.stdin.read()}, log)
"""


def _run_printed(tmp_path: Path, printed: str, domain: str) -> tuple[subprocess.CompletedProcess, dict | None]:
    """The printed command, run under bash with a kubectl naming `domain` and a helm that records its call."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("kubectl", FAKE_KUBECTL), ("helm", FAKE_HELM)):
        (bindir / name).write_text(body, encoding="utf-8", newline="\n")
        (bindir / name).chmod(0o755)
    log = tmp_path / "helm.json"
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "FAKE_DOMAIN": domain,
           "FAKE_LOG": str(log)}
    out = subprocess.run(
        ["bash", "-c", printed], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    return out, json.loads(log.read_text(encoding="utf-8")) if log.is_file() else None


def test_the_printed_argo_upgrade_runs_and_hands_helm_what_bootstrap_sh_installs_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DFE_VALKEY_SERVICE", raising=False)
    release = u.BOOTSTRAP_RELEASES["bootstrap.argocd"]
    out, helm = _run_printed(tmp_path, u.bootstrap_upgrade_command(release, "10.10.0", None), DOMAIN)

    assert out.returncode == 0, out.stderr
    argv = helm["argv"]
    assert argv[argv.index("--reset-values") + 1 : argv.index("--wait")] == _bootstrap_values(release)
    assert json.loads(helm["stdin"]) == argocd_login.helm_values(None)


def test_the_printed_argo_upgrade_stops_before_helm_when_the_secret_names_no_domain(tmp_path: Path) -> None:
    printed = u.bootstrap_upgrade_command(u.BOOTSTRAP_RELEASES["bootstrap.argocd"], "10.10.0", None)
    out, helm = _run_printed(tmp_path, printed, "")
    assert out.returncode != 0
    assert helm is None


def _set_values(helm: list[str]) -> dict:
    """The values helm's --set and --set-string arguments in `helm` build, typed as helm types them."""
    values: dict = {}
    for flag, pair in itertools.pairwise(helm):
        if flag not in ("--set", "--set-string"):
            continue
        path, raw = pair.split("=", 1)
        value: object = raw
        if flag == "--set":
            value = {"true": True, "false": False}.get(raw, int(raw) if raw.isdigit() else raw)
        *parents, leaf = [part.replace("\\.", ".") for part in re.split(r"(?<!\\)\.", path)]
        node = values
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return values


def _merged(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        out[key] = _merged(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


# What `helm get values argocd` holds for a release bootstrap.sh installed while no OIDC provider fronted Argo.
BOOTSTRAP_ARGO_RELEASE_VALUES = {
    "configs": {
        "cm": {"timeout.reconciliation": "300s"},
        "params": {
            "controller.diff.server.side": "true",
            "controller.repo.server.timeout.seconds": "60",
            "controller.self.heal.timeout.seconds": "30",
            "reposerver.disable.git.modules": "true",
            "server.insecure": "true",
        },
        "rbac": {"policy.csv": argocd_login.policy_csv()},
    },
    "externalRedis": {"host": "valkey.argocd.svc.cluster.local", "port": 6379},
    "global": {"domain": f"argocd.{DOMAIN}"},
    "redis": {"enabled": False},
}


def test_the_argo_upgrade_carries_every_value_a_bootstrap_install_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    """--reset-values drops whatever the command does not set again, so it must set all of it."""
    monkeypatch.delenv("DFE_VALKEY_SERVICE", raising=False)
    printed = u.bootstrap_upgrade_command(u.BOOTSTRAP_RELEASES["bootstrap.argocd"], "10.10.0", None)
    sets = _set_values(_printed_helm(printed))
    assert _merged(sets, argocd_login.helm_values(None)) == BOOTSTRAP_ARGO_RELEASE_VALUES


def test_a_pending_argo_names_the_cluster_secret_in_the_argo_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(u, "argocd_installed_by_bootstrap", lambda _kc: (True, "ours"))
    monkeypatch.setattr(u, "read_bootstrap_chart", lambda *_a: ("10.9.6", ""))
    state, detail = u.check_bootstrap_move(None, u.Move(step=ARGO_STEP, old="10.9.6", new="10.10.0"), "cd")
    assert state == u.BOOTSTRAP_PENDING
    assert "Run: DFE_DOMAIN=\"$(kubectl -n cd get secret dfe-cluster -o " in detail


# ---------------------------------------------------------------------------
# The Strimzi conversion preflight -- every CRD 1.x requires stored as v1
# ---------------------------------------------------------------------------

# CRD_NAMES in v1-api-conversion/.../cli/AbstractCommand.java at Strimzi tag
# 1.0.0, the set the tool's crd-upgrade rewrites to store v1 only.
CONVERSION_TOOL_CRDS = {
    "kafkas.kafka.strimzi.io",
    "kafkaconnects.kafka.strimzi.io",
    "kafkabridges.kafka.strimzi.io",
    "kafkamirrormaker2s.kafka.strimzi.io",
    "kafkatopics.kafka.strimzi.io",
    "kafkausers.kafka.strimzi.io",
    "kafkaconnectors.kafka.strimzi.io",
    "kafkarebalances.kafka.strimzi.io",
    "kafkanodepools.kafka.strimzi.io",
    "strimzipodsets.core.strimzi.io",
}


def test_strimzi_crds_is_the_conversion_tool_set() -> None:
    assert len(u.STRIMZI_CRDS) == len(set(u.STRIMZI_CRDS)) == 10
    assert set(u.STRIMZI_CRDS) == CONVERSION_TOOL_CRDS


def _crd_responses(stale: str, stored: list[str]) -> list[subprocess.CompletedProcess]:
    """One `kubectl get crd` answer per STRIMZI_CRDS entry, all v1 except `stale`."""
    return [
        _proc(0, stdout=json.dumps({"status": {"storedVersions": stored if crd == stale else ["v1"]}}))
        for crd in u.STRIMZI_CRDS
    ]


@pytest.mark.parametrize("stale", ["kafkarebalances.kafka.strimzi.io", "strimzipodsets.core.strimzi.io"])
def test_check_strimzi_conversion_refuses_the_crds_the_four_crd_list_missed(
    monkeypatch: pytest.MonkeyPatch, stale: str
) -> None:
    # Rebalancing is on by default, so a 0.51 cluster carries both of these on v1beta2.
    calls = _mock_run(monkeypatch, *_crd_responses(stale, ["v1beta2"]))
    ok, detail = u.check_strimzi_conversion("kc")
    assert ok is False
    assert f"{stale} (stored: v1beta2)" in detail
    assert [call[call.index("crd") + 1] for call in calls] == list(u.STRIMZI_CRDS)


def test_check_strimzi_conversion_fails_a_crd_it_could_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = _crd_responses("", [])
    responses[3] = _proc(1, stderr="Unable to connect to the server: dial tcp: i/o timeout")
    _mock_run(monkeypatch, *responses)
    ok, detail = u.check_strimzi_conversion("kc")
    assert ok is False
    assert f"cannot read 1 Strimzi CRD(s): {u.STRIMZI_CRDS[3]}" in detail


def test_conversion_refusal_carries_the_exact_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, *_crd_responses("kafkarebalances.kafka.strimzi.io", ["v1beta2", "v1"]))
    step = u.Step(stage="20-operators", order="50", key=u.STRIMZI_OPERATOR_KEY, before="stored-version conversion")
    ok, detail = u.check_strimzi_conversion_before("kc", u.Move(step=step, old="0.51.0", new="1.2.0"))
    assert ok is False
    assert "strimzi-v1-api-conversion-0.51.0.tar.gz" in detail
    assert "`bin/v1-api-conversion.sh convert-resource --all-namespaces`" in detail
    assert "`bin/v1-api-conversion.sh crd-upgrade`" in detail
    assert detail.index("convert-resource") < detail.index("crd-upgrade")


def test_preflight_runs_the_conversion_check_with_the_operator_move(monkeypatch: pytest.MonkeyPatch, deploy: Path) -> None:
    seen: list[u.Move] = []

    def fake_before(_kubeconfig: str | None, move: u.Move) -> tuple[bool, str]:
        seen.append(move)
        return False, "1 CRD(s) still store a pre-v1 version"

    for name in ("check_deploy_clean", "check_cluster_reachable", "check_argo_apps", "check_no_kafka_rebalance",
                 "check_clickhouse_merges", "check_node_capacity"):
        monkeypatch.setattr(u, name, lambda *_a, **_k: (True, "ok"))
    monkeypatch.setattr(u, "check_strimzi_conversion_before", fake_before)
    step = u.Step(stage="20-operators", order="50", key=u.STRIMZI_OPERATOR_KEY, before="x stored-version conversion y")
    move = u.Move(step=step, old="0.51.0", new="1.2.0")
    checks = u.run_preflight(deploy, [move], kubeconfig="kc")
    assert ("strimzi stored-version conversion", False, "1 CRD(s) still store a pre-v1 version") in checks
    assert seen == [move]


def test_real_upgrade_order_names_the_conversion_commands() -> None:
    steps = {s.key: s for s in u.load_steps()}
    before = steps[u.STRIMZI_OPERATOR_KEY].before
    assert "stored-version conversion" in before  # what run_preflight keys on
    for command in u.CONVERSION_COMMANDS:
        assert command in before


# ---------------------------------------------------------------------------
# The infra/kafka.yaml holds -- surgical, deployer values untouched
# ---------------------------------------------------------------------------

DEPLOYER_OVERLAY = """# the deployer's own Kafka shape
kafka:
    storageModel: tiered-object
    tieredObject:
        className: io.example.RemoteStorageManager
"""


def test_set_overlay_hold_creates_a_file_from_nothing() -> None:
    text = u.set_overlay_hold("", "metadataVersion", "4.2-IV1")
    assert text == f'kafka:\n  metadataVersion: "4.2-IV1"  {u.HOLD_MARK}\n'
    assert u.overlay_entry(text, "metadataVersion") == ("4.2-IV1", True)


def test_set_overlay_hold_keeps_the_deployers_block_and_its_indent() -> None:
    text = u.set_overlay_hold(DEPLOYER_OVERLAY, "metadataVersion", "4.2-IV1")
    text = u.set_overlay_hold(text, "version", "4.2.0")
    assert f'    metadataVersion: "4.2-IV1"  {u.HOLD_MARK}' in text
    assert f'    version: "4.2.0"  {u.HOLD_MARK}' in text
    for line in DEPLOYER_OVERLAY.splitlines():
        assert line in text.splitlines()
    assert u.overlay_entry(text, "storageModel") == ("tiered-object", False)


def test_set_overlay_hold_appends_a_kafka_block_after_other_keys() -> None:
    text = u.set_overlay_hold("clickhouse:\n  mode: cluster\n", "metadataVersion", "4.2-IV1")
    assert text == f'clickhouse:\n  mode: cluster\n\nkafka:\n  metadataVersion: "4.2-IV1"  {u.HOLD_MARK}\n'


def test_set_overlay_hold_rewrites_its_own_hold_in_place() -> None:
    text = u.set_overlay_hold(u.set_overlay_hold("", "version", "4.2.0"), "version", "4.2.1")
    assert text.count("version:") == 1
    assert u.overlay_entry(text, "version") == ("4.2.1", True)


def test_set_overlay_hold_refuses_a_value_the_deployer_set() -> None:
    with pytest.raises(u.UpgradeError, match=r"sets kafka\.metadataVersion itself"):
        u.set_overlay_hold('kafka:\n  metadataVersion: "4.1-IV1"\n', "metadataVersion", "4.2-IV1")


def test_set_overlay_hold_refuses_a_flow_style_kafka_key() -> None:
    with pytest.raises(u.UpgradeError, match="not a block mapping"):
        u.set_overlay_hold("kafka: {mode: cluster}\n", "metadataVersion", "4.2-IV1")


def test_drop_overlay_hold_leaves_the_deployers_lines() -> None:
    held = u.set_overlay_hold(DEPLOYER_OVERLAY, "metadataVersion", "4.2-IV1")
    assert u.drop_overlay_hold(held, "metadataVersion") == DEPLOYER_OVERLAY


def test_drop_overlay_hold_never_drops_a_deployer_value() -> None:
    text = 'kafka:\n  metadataVersion: "4.1-IV1"\n'
    assert u.drop_overlay_hold(text, "metadataVersion") == text


def test_drop_overlay_hold_takes_an_emptied_kafka_key_with_it() -> None:
    # A bare `kafka:` is null, and Helm reads a null as deleting every chart default under it.
    held = u.set_overlay_hold("# holds only\n", "metadataVersion", "4.2-IV1")
    assert u.drop_overlay_hold(held, "metadataVersion") == "# holds only\n\n"


# ---------------------------------------------------------------------------
# plan_kafka_hold -- decided from the live CRs before any stage moves
# ---------------------------------------------------------------------------


def _live_kafka(monkeypatch: pytest.MonkeyPatch, *statuses: dict) -> None:
    _mock_run(monkeypatch, _kafka_list(*statuses))


def test_plan_kafka_hold_pins_the_running_metadata_and_holds_the_brokers(
    monkeypatch: pytest.MonkeyPatch, deploy: Path
) -> None:
    _live_kafka(monkeypatch, {"kafkaVersion": "4.2.0", "kafkaMetadataVersion": "4.2-IV1"})
    hold, detail = u.plan_kafka_hold("kc", deploy, KAFKA_MOVE, {})
    assert hold == u.KafkaHold(metadata="4.2-IV1", version="4.2.0")
    assert "metadata held at 4.2-IV1" in detail


def test_plan_kafka_hold_never_holds_brokers_that_already_rolled(monkeypatch: pytest.MonkeyPatch, deploy: Path) -> None:
    (deploy / "infra").mkdir()
    (deploy / "infra" / "kafka.yaml").write_text(u.set_overlay_hold("", "metadataVersion", "4.2-IV1"), encoding="utf-8")
    _live_kafka(monkeypatch, {"kafkaVersion": "4.3.1", "kafkaMetadataVersion": "4.2-IV1"})
    hold, detail = u.plan_kafka_hold("kc", deploy, KAFKA_MOVE, {})
    assert hold == u.KafkaHold(metadata="", version="")
    assert "metadata held at 4.2-IV1" in detail


def test_plan_kafka_hold_leaves_a_deployer_kafka_version_alone(monkeypatch: pytest.MonkeyPatch, deploy: Path) -> None:
    (deploy / "infra").mkdir()
    (deploy / "infra" / "kafka.yaml").write_text('kafka:\n  version: "4.2.0"\n', encoding="utf-8")
    _live_kafka(monkeypatch, {"kafkaVersion": "4.2.0", "kafkaMetadataVersion": "4.2-IV1"})
    hold, _detail = u.plan_kafka_hold("kc", deploy, KAFKA_MOVE, {})
    assert hold == u.KafkaHold(metadata="4.2-IV1", version="")


def test_plan_kafka_hold_refuses_brokers_on_neither_version(monkeypatch: pytest.MonkeyPatch, deploy: Path) -> None:
    _live_kafka(monkeypatch, {"kafkaVersion": "4.1.1", "kafkaMetadataVersion": "4.1-IV1"})
    with pytest.raises(u.UpgradeError, match=r"neither 4\.2\.0 nor 4\.3\.1"):
        u.plan_kafka_hold("kc", deploy, KAFKA_MOVE, {})


def test_plan_kafka_hold_refuses_crs_that_disagree(monkeypatch: pytest.MonkeyPatch, deploy: Path) -> None:
    _live_kafka(
        monkeypatch,
        {"kafkaVersion": "4.2.0", "kafkaMetadataVersion": "4.2-IV1"},
        {"kafkaVersion": "4.2.0", "kafkaMetadataVersion": "4.1-IV1"},
    )
    with pytest.raises(u.UpgradeError, match="disagree"):
        u.plan_kafka_hold("kc", deploy, KAFKA_MOVE, {})


def test_plan_kafka_hold_refuses_a_cr_with_no_metadata_version(monkeypatch: pytest.MonkeyPatch, deploy: Path) -> None:
    _live_kafka(monkeypatch, {"kafkaVersion": "4.2.0"})
    with pytest.raises(u.UpgradeError, match=r"no status\.kafkaMetadataVersion"):
        u.plan_kafka_hold("kc", deploy, KAFKA_MOVE, {})


def test_plan_kafka_hold_reads_nothing_once_finalised_or_without_a_move(
    monkeypatch: pytest.MonkeyPatch, deploy: Path
) -> None:
    calls = _mock_run(monkeypatch)
    assert u.plan_kafka_hold("kc", deploy, None, {})[0] is None
    assert u.plan_kafka_hold("kc", deploy, KAFKA_MOVE, {"services.kafka-version": "t"})[0] is None
    assert calls == []


def test_plan_kafka_hold_holds_nothing_without_a_strimzi_broker(monkeypatch: pytest.MonkeyPatch, deploy: Path) -> None:
    _live_kafka(monkeypatch)
    hold, detail = u.plan_kafka_hold("kc", deploy, KAFKA_MOVE, {})
    assert hold is None
    assert "no Strimzi Kafka CR" in detail


def test_release_metadata_hold_drops_only_the_hold(deploy: Path) -> None:
    (deploy / "infra").mkdir()
    overlay = deploy / "infra" / "kafka.yaml"
    overlay.write_text(u.set_overlay_hold(DEPLOYER_OVERLAY, "metadataVersion", "4.2-IV1"), encoding="utf-8")
    ok, detail = u.release_metadata_hold(deploy, KAFKA_MOVE)
    assert ok is True
    assert "dropped the kafka.metadataVersion 4.2-IV1 hold" in detail
    assert overlay.read_text(encoding="utf-8") == DEPLOYER_OVERLAY


def test_release_metadata_hold_refuses_a_deployer_pin_and_finalise_writes_no_marker(deploy: Path) -> None:
    (deploy / "infra").mkdir()
    (deploy / "infra" / "kafka.yaml").write_text('kafka:\n  metadataVersion: "4.2-IV1"\n', encoding="utf-8")
    ran, detail = u.run_finalise_hook(deploy, KAFKA_MOVE, from_name="1.0.0", to_name="2.0.0", assume_yes=True)
    assert ran is False
    assert "raise it to the 4.3.1 line by hand" in detail
    assert u.read_finalised_keys(deploy) == {}


# ---------------------------------------------------------------------------
# The cluster secret's target_revision
# ---------------------------------------------------------------------------


def test_decide_retarget_moves_a_tag_pinned_secret() -> None:
    assert u.decide_retarget("2.2.0-rc.13", "2.2.0-rc.13", "2.2.0-rc.14", None)[0] == "2.2.0-rc.14"
    assert u.decide_retarget("v1.0.0", "1.0.0", "2.0.0", None)[0] == "2.0.0"


def test_decide_retarget_leaves_a_branch_and_a_secret_already_there() -> None:
    ref, why = u.decide_retarget("release-train-xyzzy", "1.0.0", "2.0.0", None)
    assert ref is None
    assert "tracks a branch the charts already follow -- left as it is" in why
    assert u.decide_retarget("2.0.0", "1.0.0", "2.0.0", None)[0] is None


def test_decide_retarget_never_repeats_the_value_it_read_from_the_secret() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    for current in ("release-train-xyzzy", "1.0.0", "2.0.0", "v1.0.0"):
        _ref, why = u.decide_retarget(current, "0.9.0" if current == "release-train-xyzzy" else "1.0.0", "2.0.0", None)
        assert "secret/dfe-cluster dfe.hyperi.io/target_revision" in why
        assert "release-train-xyzzy" not in why
        assert "v1.0.0" not in why
    with pytest.raises(u.UpgradeError) as refused:
        u.decide_retarget(sha, "1.0.0", "2.0.0", None)
    assert sha not in str(refused.value)


def test_decide_retarget_refuses_a_commit_pin_without_an_explicit_ref() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    with pytest.raises(u.UpgradeError, match="pins a commit"):
        u.decide_retarget(sha, "1.0.0", "2.0.0", None)
    assert u.decide_retarget(sha, "1.0.0", "2.0.0", "2.0.0")[0] == "2.0.0"


def test_decide_retarget_refuses_an_unset_annotation() -> None:
    with pytest.raises(u.UpgradeError, match="is unset"):
        u.decide_retarget("", "1.0.0", "2.0.0", None)


def test_read_target_revision_reads_only_the_annotation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_run(monkeypatch, _proc(0, stdout="2.2.0-rc.13\n"))
    assert u.read_target_revision("kc", "argocd") == "2.2.0-rc.13"
    assert calls[0][-2:] == ["-o", "jsonpath={.metadata.annotations.dfe\\.hyperi\\.io/target_revision}"]
    assert ["secret", "dfe-cluster"] == calls[0][calls[0].index("get") + 1 : calls[0].index("get") + 3]


def test_write_target_revision_moves_the_ref_and_the_stack_version(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _mock_run(monkeypatch, _proc(0))
    ok, _detail = u.write_target_revision("kc", "argocd", "2.2.0-rc.14", "2.2.0-rc.14")
    assert ok is True
    assert calls[0][-5:] == [
        "annotate", "--overwrite", "secret/dfe-cluster",
        "dfe.hyperi.io/target_revision=2.2.0-rc.14", "dfe.hyperi.io/stack_version=2.2.0-rc.14",
    ]


def test_check_argo_apps_fails_an_app_still_on_the_old_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    synced = {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}
    doc = {"items": [
        {"metadata": {"name": "kafka-dfe"}, "spec": {"sources": [{"targetRevision": "1.0.0"}, {"targetRevision": "main"}]},
         "status": synced},
        {"metadata": {"name": "strimzi"}, "spec": {"source": {"targetRevision": "1.2.0"}}, "status": synced},
    ]}
    _mock_run(monkeypatch, _proc(0, stdout=json.dumps(doc)))
    ok, detail = u.check_argo_apps("kc", stale_revision="1.0.0")
    assert ok is False
    assert "1 app(s) not Synced/Healthy: kafka-dfe (still renders from the previous" in detail
    assert "1.0.0" not in detail  # the stale ref came out of the cluster Secret


def test_check_argo_apps_without_health_counts_a_synced_app_but_not_a_stale_or_unsynced_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def app(name: str, revision: str, sync: str, health: str) -> dict:
        return {"metadata": {"name": name}, "spec": {"source": {"targetRevision": revision}},
                "status": {"sync": {"status": sync}, "health": {"status": health}}}

    degraded = {"items": [app("vrl", "2.0.0", "Synced", "Degraded"), app("ui", "2.0.0", "Synced", "Healthy")]}
    _mock_run(monkeypatch, _proc(0, stdout=json.dumps(degraded)), _proc(0, stdout=json.dumps(degraded)))
    assert u.check_argo_apps("kc", stale_revision="1.0.0", require_healthy=False) == (True, "2 Application(s) Synced")
    ok, detail = u.check_argo_apps("kc", stale_revision="1.0.0")
    assert ok is False
    assert "1 app(s) not Synced/Healthy: vrl (sync Synced, health Degraded)" in detail

    behind = {"items": [app("vrl", "2.0.0", "OutOfSync", "Healthy"), app("ui", "1.0.0", "Synced", "Healthy")]}
    _mock_run(monkeypatch, _proc(0, stdout=json.dumps(behind)))
    ok, detail = u.check_argo_apps("kc", stale_revision="1.0.0", require_healthy=False)
    assert ok is False
    assert detail.startswith("2 app(s) not Synced: vrl (sync OutOfSync, health Healthy), ui (still renders")


# ---------------------------------------------------------------------------
# Reconcile floor: Argo reads Synced and Healthy from the state it last
# compared, so right after a push or a retarget that is the state from before.
# ---------------------------------------------------------------------------

CHANGE = datetime(2026, 10, 11, 3, 0, 0, tzinfo=UTC)


def _reconciled_app(name: str, reconciled_at: str | None, sync: str = "Synced", health: str = "Healthy") -> dict:
    """One Application as `kubectl get -o json` lists it, with the `status.reconciledAt` Argo stamped."""
    status: dict[str, object] = {"sync": {"status": sync}, "health": {"status": health}}
    if reconciled_at is not None:
        status["reconciledAt"] = reconciled_at
    return {"metadata": {"name": name}, "status": status}


def _apps(*apps: dict) -> subprocess.CompletedProcess:
    return _proc(0, stdout=json.dumps({"items": list(apps)}))


def test_check_argo_apps_fails_a_healthy_app_reconciled_before_the_change(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _apps(
        _reconciled_app("kafka-dfe", "2026-10-11T02:59:59Z"),
        _reconciled_app("never-compared", None),
        _reconciled_app("loader", "2026-10-11T03:00:07Z"),
    ))

    ok, detail = u.check_argo_apps("kc", reconciled_after=CHANGE)

    assert ok is False
    assert detail == (
        "2 app(s) not Synced/Healthy: kafka-dfe (last reconciled 2026-10-11T02:59:59Z, before this change), "
        "never-compared (last reconciled never, before this change)"
    )


def test_check_argo_apps_passes_apps_reconciled_at_or_after_the_change(monkeypatch: pytest.MonkeyPatch) -> None:
    # Argo stamps whole seconds, so a reconcile in the second the change landed counts.
    _mock_run(monkeypatch, _apps(
        _reconciled_app("kafka-dfe", "2026-10-11T03:00:00Z"), _reconciled_app("loader", "2026-10-11T03:04:12Z"),
    ))
    assert u.check_argo_apps("kc", reconciled_after=CHANGE) == (
        True, "2 Application(s) Synced and Healthy, each reconciled since this change"
    )


def test_check_argo_apps_reads_reconcile_time_only_when_asked_to(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _apps(_reconciled_app("kafka-dfe", "2020-01-01T00:00:00Z"), _reconciled_app("loader", None)))
    assert u.check_argo_apps("kc") == (True, "2 Application(s) Synced and Healthy")


def test_a_reconciled_app_that_is_still_out_of_sync_fails_on_its_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, _apps(_reconciled_app("kafka-dfe", "2026-10-11T03:00:07Z", sync="OutOfSync")))
    ok, detail = u.check_argo_apps("kc", reconciled_after=CHANGE)
    assert ok is False
    assert detail == "1 app(s) not Synced/Healthy: kafka-dfe (sync OutOfSync, health Healthy)"


def test_wait_for_argo_holds_on_a_healthy_app_until_it_is_reconciled_after_the_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _apps(_reconciled_app("kafka-dfe", "2026-10-11T02:50:00Z"))
    fresh = _apps(_reconciled_app("kafka-dfe", "2026-10-11T03:00:09Z"))
    calls = _mock_run(monkeypatch, stale, stale, fresh)
    clock, sleeps, fake_sleep = _clock()

    ok, detail = u.wait_for_argo(
        "kc", argocd_namespace="argocd", timeout=120, reconciled_after=CHANGE, sleep=fake_sleep, now=lambda: clock["t"]
    )

    assert ok is True
    assert detail == "1 Application(s) Synced and Healthy, each reconciled since this change"
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_wait_for_argo_times_out_naming_each_app_still_on_the_state_from_before_the_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _apps(_reconciled_app("kafka-dfe", "2026-10-11T02:50:00Z"), _reconciled_app("loader", "2026-10-11T03:00:01Z"))
    _mock_run(monkeypatch, *[stale] * 10)
    clock, _sleeps, fake_sleep = _clock()

    ok, detail = u.wait_for_argo(
        "kc", argocd_namespace="argocd", timeout=15, reconciled_after=CHANGE, sleep=fake_sleep, now=lambda: clock["t"]
    )

    assert ok is False
    assert detail == (
        "still not converged after 15s -- 1 app(s) not Synced/Healthy: "
        "kafka-dfe (last reconciled 2026-10-11T02:50:00Z, before this change)"
    )


def test_change_moment_drops_the_fraction_argo_does_not_stamp() -> None:
    moment = u.change_moment()
    assert moment.microsecond == 0
    assert moment.tzinfo is UTC
    assert timedelta(0) <= datetime.now(UTC) - moment < timedelta(seconds=2)


@pytest.mark.parametrize("stamp", [None, "", "not a time", 17, {"at": "2026-10-11T03:00:00Z"}])
def test_an_unreadable_reconcile_time_counts_as_never_reconciled(stamp: object) -> None:
    assert u._argo_time(stamp) is None


def test_a_reconcile_time_without_an_offset_is_read_as_utc() -> None:
    assert u._argo_time("2026-10-11T03:00:00") == CHANGE


# ---------------------------------------------------------------------------
# The in-place Strimzi 0.51 -> 1.2.0 path end to end, against a real deploy
# repo pushed to a real (local) remote: holds, retarget order, version roll,
# then a finalise after the soak.
# ---------------------------------------------------------------------------


@pytest.fixture
def pushed_deploy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """A deploy repo holding pins.yaml alone -- no sizing/ or upgrades/, the
    dfe-deploy template's own shape -- tracking a bare local remote."""
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "Test")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, "test@example.invalid")
    remote = tmp_path / "remote.git"
    assert u._run(["git", "init", "-q", "--bare", str(remote)]).returncode == 0
    d = tmp_path / "deploy-template"
    d.mkdir()
    (d / "pins.yaml").write_text(PINS_YAML, encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["add", "pins.yaml"], ["commit", "-q", "-m", "initial"],
                 ["remote", "add", "origin", str(remote)], ["push", "-q", "-u", "origin", "main"]):
        assert u._git(d, *args).returncode == 0, args
    return d, remote


def _remote_file(remote: Path, rev: str, path: str) -> str:
    shown = u._run(["git", "--git-dir", str(remote), "show", f"{rev}:{path}"])
    return shown.stdout if shown.returncode == 0 else ""


def _ga_cluster(monkeypatch: pytest.MonkeyPatch, live: dict[str, str]) -> list[tuple[str, str]]:
    """Stand in for the cluster: every cluster-facing call is recorded in order,
    and the remote HEAD at each one is captured so the order can be checked
    against what Argo could actually read."""
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(u, "run_compat_check", lambda *_a, **_k: (True, "ok"))
    monkeypatch.setattr(u, "run_preflight", lambda *_a, **_k: [("strimzi stored-version conversion", True, "ok")])
    monkeypatch.setattr(u, "check_strimzi_conversion", lambda *_a, **_k: (True, "10 Strimzi CRD(s) store v1 only"))
    monkeypatch.setattr(u, "read_target_revision", lambda *_a, **_k: live["target_revision"])
    monkeypatch.setattr(u, "read_dfe_namespace", lambda *_a, **_k: "dfe")
    monkeypatch.setattr(
        u, "read_kafka_state", lambda *_a, **_k: u.KafkaState(crs=1, version=live["kafka"], metadata=live["metadata"])
    )

    def argo(*_a: object, stale_revision: str = "", **_k: object) -> tuple[bool, str]:
        events.append(("argo", stale_revision))
        return True, "converged"

    def retarget(_kc: object, _ns: str, ref: str, stack: str) -> tuple[bool, str]:
        events.append(("retarget", ref))
        live["target_revision"] = ref
        return True, f"{ref} {stack}"

    def operator(*_a: object, **_k: object) -> tuple[bool, str]:
        events.append(("operator", ""))
        return True, "reconciled"

    def kafka_version(_kc: object, version: str) -> tuple[bool, str]:
        events.append(("kafka-version", version))
        live["kafka"] = version
        return True, "rolled"

    def metadata(_kc: object, version: str) -> tuple[bool, str]:
        events.append(("metadata", version))
        return True, "moved"

    monkeypatch.setattr(u, "wait_for_argo", argo)
    monkeypatch.setattr(u, "write_target_revision", retarget)
    monkeypatch.setattr(u, "wait_for_kafka_operator_version", operator)
    monkeypatch.setattr(u, "check_kafka_version", kafka_version)
    monkeypatch.setattr(u, "check_kafka_metadata_moved", metadata)
    _bootstrap_runs(monkeypatch, "v1.1.0")
    return events


def _commit_with(remote: Path, subject: str) -> str:
    log = u._run(["git", "--git-dir", str(remote), "log", "--format=%H %s", "main"]).stdout
    return next(line.split(" ", 1)[0] for line in log.splitlines() if subject in line)


def test_apply_holds_the_metadata_then_retargets_then_rolls_the_brokers(
    monkeypatch: pytest.MonkeyPatch,
    pushed_deploy: tuple[Path, Path],
    order_path: Path,
    versions_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    deploy, remote = pushed_deploy
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    live = {"target_revision": "1.0.0", "kafka": "4.2.0", "metadata": "4.2-IV1"}
    events = _ga_cluster(monkeypatch, live)

    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(deploy), to="2.0.0", yes=True, dry_run=False, push=True))
    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err

    # Stage 1 (bootstrap) commits the pin; the pin alone moves no chart.
    stage1 = _commit_with(remote, "stage 1 -- bootstrap.cert-manager")
    assert _remote_file(remote, stage1, "infra/kafka.yaml") == ""
    # Stage 2 (operators) pushes both holds BEFORE the retarget moves any chart.
    stage2 = _commit_with(remote, "stage 2 -- operators.strimzi-kafka-operator")
    held = _remote_file(remote, stage2, "infra/kafka.yaml")
    assert u.overlay_entry(held, "metadataVersion") == ("4.2-IV1", True)
    assert u.overlay_entry(held, "version") == ("4.2.0", True)
    # Stage 3 (services) drops only the version hold, so the brokers roll under the held metadata.
    stage3 = _commit_with(remote, "stage 3 -- services.kafka-version")
    rolled = _remote_file(remote, stage3, "infra/kafka.yaml")
    assert u.overlay_entry(rolled, "metadataVersion") == ("4.2-IV1", True)
    assert u.overlay_entry(rolled, "version") is None

    assert events == [
        ("argo", ""),          # stage 1
        ("argo", ""),          # stage 2, the holds applied on the old charts
        ("retarget", "2.0.0"),
        ("argo", "1.0.0"),     # until no Application renders from the old tag
        ("operator", ""),
        ("argo", ""),          # stage 3, the version hold dropped
        ("kafka-version", "4.3.1"),
    ]
    assert "finalise pending (manual, after a soak): services.kafka-version" in err
    assert u.read_finalised_keys(deploy) == {}

    # After the soak: pins.yaml already names 2.0.0, so --from names the start.
    live["kafka"] = "4.3.1"
    events.clear()
    rc = u.cmd_upgrade_apply(_apply_args(
        deploy=str(deploy), to="2.0.0", from_stack="1.0.0", yes=True, dry_run=False, push=True, finalise=True,
    ))
    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert ("retarget", "2.0.0") not in events  # already there
    assert ("metadata", "4.3.1") in events
    assert events.index(("metadata", "4.3.1")) > events.index(("kafka-version", "4.3.1"))
    finalised = _remote_file(remote, "main", "infra/kafka.yaml")
    assert u.overlay_entry(finalised, "metadataVersion") is None
    assert "kafka:" not in finalised
    assert "services.kafka-version" in u.read_finalised_keys(deploy)
    assert _remote_file(remote, "main", "upgrades/1.0.0-to-2.0.0.finalised").startswith("services.kafka-version ")


def test_apply_asks_each_wait_for_a_reconcile_after_the_change_that_started_it(
    monkeypatch: pytest.MonkeyPatch,
    pushed_deploy: tuple[Path, Path],
    order_path: Path,
    versions_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    deploy, _remote = pushed_deploy
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    _ga_cluster(monkeypatch, {"target_revision": "1.0.0", "kafka": "4.2.0", "metadata": "4.2-IV1"})
    started = datetime.now(UTC).replace(microsecond=0)
    waits: list[tuple[str, datetime | None]] = []
    retargeted: list[datetime] = []
    argo, retarget = u.wait_for_argo, u.write_target_revision

    def spy_wait(*a: object, stale_revision: str = "", reconciled_after: datetime | None = None, **k: object):
        waits.append((stale_revision, reconciled_after))
        return argo(*a, stale_revision=stale_revision, **k)

    def spy_retarget(*a: object) -> tuple[bool, str]:
        retargeted.append(datetime.now(UTC))
        return retarget(*a)

    monkeypatch.setattr(u, "wait_for_argo", spy_wait)
    monkeypatch.setattr(u, "write_target_revision", spy_retarget)

    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(deploy), to="2.0.0", yes=True, dry_run=False, push=True))
    assert rc == u.EXIT_OK, capsys.readouterr().err

    # Every stage commits and pushes, and the one retarget leaves the old tag.
    assert [stale for stale, _ in waits] == ["", "", "1.0.0", ""]
    floors = [floor for _, floor in waits]
    assert all(floor is not None for floor in floors)
    assert floors == sorted(floors)
    assert started <= floors[0]
    retarget_floor = floors[2]
    assert len(retargeted) == 1
    assert floors[1] <= retarget_floor <= retargeted[0]


def test_apply_without_push_gives_no_wait_a_reconcile_to_ask_for(
    monkeypatch: pytest.MonkeyPatch, real_git_deploy: Path, capsys: pytest.CaptureFixture
) -> None:
    _two_stage(monkeypatch, real_git_deploy)
    floors: list[datetime | None] = []

    def wait(*_a: object, reconciled_after: datetime | None = None, **_k: object) -> tuple[bool, str]:
        floors.append(reconciled_after)
        return True, "ok"

    monkeypatch.setattr(u, "wait_for_argo", wait)

    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(real_git_deploy), to="2.0.0", yes=True, dry_run=False))

    assert rc == u.EXIT_OK, capsys.readouterr().err
    assert floors == [None, None]


def test_apply_refuses_a_retarget_without_push_before_anything_moves(
    monkeypatch: pytest.MonkeyPatch,
    pushed_deploy: tuple[Path, Path],
    order_path: Path,
    versions_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    deploy, _remote = pushed_deploy
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    events = _ga_cluster(monkeypatch, {"target_revision": "1.0.0", "kafka": "4.2.0", "metadata": "4.2-IV1"})

    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(deploy), to="2.0.0", yes=True, dry_run=False, push=False))
    assert rc == u.EXIT_BLOCKED
    assert "needs --push" in capsys.readouterr().err
    assert events == []
    assert u.read_deploy_pin(deploy) == "1.0.0"
    assert u._git(deploy, "rev-list", "--count", "HEAD").stdout.strip() == "1"


def test_apply_refuses_brokers_in_an_unexpected_state_before_anything_moves(
    monkeypatch: pytest.MonkeyPatch,
    pushed_deploy: tuple[Path, Path],
    order_path: Path,
    versions_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    deploy, _remote = pushed_deploy
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    _ga_cluster(monkeypatch, {"target_revision": "1.0.0", "kafka": "4.1.1", "metadata": "4.1-IV1"})

    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(deploy), to="2.0.0", yes=True, dry_run=False, push=True))
    assert rc == u.EXIT_BLOCKED
    assert "neither 4.2.0 nor 4.3.1" in capsys.readouterr().err
    assert u.read_deploy_pin(deploy) == "1.0.0"


def test_apply_dry_run_names_the_holds_and_the_retarget_in_order(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    calls = _mock_run(monkeypatch, _proc(0, stdout="compat-check 2.0.0: 0 rule(s) checked"))

    assert u.cmd_upgrade_apply(_apply_args(deploy=str(deploy), to="2.0.0")) == u.EXIT_OK
    err = capsys.readouterr().err
    order = [
        "hold kafka.metadataVersion at the live status.kafkaMetadataVersion",
        "hold kafka.version at 4.2.0 in infra/kafka.yaml until stage 30-services",
        "chore(upgrade): 2.0.0 stage 2 -- operators.strimzi-kafka-operator",
        "if secret/dfe-cluster targets 1.0.0: kubectl -n argocd annotate --overwrite secret/dfe-cluster "
        "dfe.hyperi.io/target_revision=2.0.0",
        "wait for every Kafka CR to report operatorLastSuccessfulVersion 1.2.0",
        "drop the kafka.version hold from infra/kafka.yaml, so the brokers roll to 4.3.1",
        "wait for every Kafka CR to report kafkaVersion 4.3.1",
    ]
    positions = [err.index(line) for line in order]
    assert positions == sorted(positions)
    assert len(calls) == 1


def test_apply_on_a_deploy_repo_without_pins_yaml_commits_one_at_the_first_stage(
    monkeypatch: pytest.MonkeyPatch,
    pushed_deploy: tuple[Path, Path],
    order_path: Path,
    versions_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    deploy, remote = pushed_deploy
    # The bundled repo's own shape: seeded values files and the marker, no pins.yaml.
    (deploy / "values").mkdir()
    (deploy / "values" / "dfe-engine-default-values.yaml").write_text("deploy:\n  service: dfe-engine\n", "utf-8")
    (deploy / ".seeded-apps").write_text("dfe-engine\n", encoding="utf-8")
    for args in (["rm", "-q", "pins.yaml"], ["add", "values", ".seeded-apps"], ["commit", "-q", "-m", "seed"],
                 ["push", "-q"]):
        assert u._git(deploy, *args).returncode == 0, args
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    live = {"target_revision": "1.0.0", "kafka": "4.2.0", "metadata": "4.2-IV1"}
    events = _ga_cluster(monkeypatch, live)
    monkeypatch.setattr(u, "read_stack_version", lambda *_a, **_k: "1.0.0")

    rc = u.cmd_upgrade_apply(_apply_args(deploy=str(deploy), to="2.0.0", yes=True, dry_run=False, push=True))

    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert "FROM is secret/dfe-cluster's dfe.hyperi.io/stack_version" in err
    stage1 = _commit_with(remote, "stage 1 -- bootstrap.cert-manager")
    pinned = u.yaml_subset.parse(_remote_file(remote, stage1, "pins.yaml"), source="pins.yaml")
    assert pinned == {"base": {"dfe-infra": "2.0.0"}}
    assert ("retarget", "2.0.0") in events
    assert u.read_deploy_pin(deploy) == "2.0.0"
    assert u._git(deploy, "status", "--porcelain").stdout == ""
