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

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import dfe_ops_upgrade as u  # noqa: E402

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


def test_check_clickhouse_merges_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    calls = _mock_run(
        monkeypatch,
        _proc(0, stdout=json.dumps({"items": [{"metadata": {"name": "ch-0"}}]})),
        _proc(0, stdout="0\n"),
    )
    ok, detail = u.check_clickhouse_merges("kc")
    assert ok is True
    assert "no merge running" in detail
    assert "clickhouse-client" in calls[1]


def test_check_clickhouse_merges_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    _mock_run(
        monkeypatch,
        _proc(0, stdout=json.dumps({"items": [{"metadata": {"name": "ch-0"}}]})),
        _proc(0, stdout="2\n"),
    )
    ok, detail = u.check_clickhouse_merges("kc", threshold_seconds=300)
    assert ok is False
    assert "2 merge(s)" in detail


def test_check_clickhouse_merges_no_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    _mock_run(monkeypatch, _proc(0, stdout=json.dumps({"items": []})))
    ok, detail = u.check_clickhouse_merges("kc")
    assert ok is False
    assert "no pod matching" in detail


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


def test_cmd_upgrade_plan_writes_file_and_reports_compat_check(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    _mock_run(monkeypatch, _proc(0, stdout="ok     rule-a: x = y\n\ncompat-check 1.1.0: 1 rule(s) checked"))

    args = _Args(deploy=str(deploy), to="1.1.0", dial=None, fixtures=None, live=False)
    rc = u.cmd_upgrade_plan(args)

    assert rc == u.EXIT_OK
    out = capsys.readouterr().out
    assert "Upgrade plan: 1.0.0 -> 1.1.0" in out
    assert "compat-check (1.1.0, --strict)" in out
    assert "sizing locked-change check" in out
    assert "skipped: no --dial given" in out
    written = deploy / "upgrades" / "1.0.0-to-1.1.0.md"
    assert written.is_file()
    assert "before:   kafka.strimzi.io stored-version conversion" in written.read_text(encoding="utf-8")


def test_cmd_upgrade_plan_blocked_by_compat_check(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    _mock_run(monkeypatch, _proc(1, stdout="FAIL   rule-a: x violates >=2\n"))

    args = _Args(deploy=str(deploy), to="1.1.0", dial=None, fixtures=None, live=False)
    rc = u.cmd_upgrade_plan(args)
    assert rc == u.EXIT_BLOCKED


def test_cmd_upgrade_plan_bad_stack_is_preflight_failure(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path
) -> None:
    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    args = _Args(deploy=str(deploy), to="9.9.9", dial=None, fixtures=None, live=False)
    rc = u.cmd_upgrade_plan(args)
    assert rc == u.EXIT_PREFLIGHT_FAILED


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
        clickhouse_selector="app.kubernetes.io/name=clickhouse", clickhouse_merge_threshold=300.0,
        nodes_file=None, backup_marker=u.DEFAULT_BACKUP_MARKER,
        yes=False, push=False, timeout=900, dry_run=True, finalise=False, stop_before=None,
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
        clickhouse_selector="app.kubernetes.io/name=clickhouse", clickhouse_merge_threshold=300.0,
        nodes_file=None, backup_marker=u.DEFAULT_BACKUP_MARKER,
        yes=False, push=False, timeout=900, dry_run=True, finalise=False, stop_before=None,
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
        clickhouse_selector="app.kubernetes.io/name=clickhouse", clickhouse_merge_threshold=300.0,
        nodes_file=None, backup_marker=u.DEFAULT_BACKUP_MARKER,
        yes=False, push=False, timeout=900, dry_run=True, finalise=False, stop_before=None,
    )
    rc = u.cmd_upgrade_apply(args)
    assert rc == u.EXIT_BLOCKED
    assert "REFUSED" in capsys.readouterr().err


def _apply_args(**overrides: object) -> _Args:
    """The apply _Args shape every dry-run test shares, with per-test overrides."""
    base = dict(
        dial=None, fixtures=None, live=False,
        kubeconfig=None, argocd_namespace="argocd", clickhouse_namespace="clickhouse",
        clickhouse_selector="app.kubernetes.io/name=clickhouse", clickhouse_merge_threshold=300.0,
        nodes_file=None, backup_marker=u.DEFAULT_BACKUP_MARKER,
        yes=False, push=False, timeout=900, dry_run=True, finalise=False, stop_before=None,
    )
    base.update(overrides)
    return _Args(**base)


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
    )
    rc = u.cmd_upgrade_apply(args)

    assert rc == u.EXIT_OK
    assert staged["resolved.yaml"] == RESOLVER_OUTPUT["sizing/resolved.yaml"]
    git_add = next(cmd for cmd in calls if cmd[:1] == ["git"] and "add" in cmd)
    assert git_add[-3:] == ["pins.yaml", "sizing", "upgrades"]
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
        kubeconfig=None, push=False, dry_run=True,
        check_cluster=False, kafka_name=u.DEFAULT_KAFKA_NAME, kafka_namespace=u.DEFAULT_KAFKA_NAMESPACE,
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
# finalise markers -- read/write, and the rollback --check-cluster guard
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


def test_run_finalise_hook_confirmed_writes_marker(deploy: Path) -> None:
    step = u.Step(
        stage="30-services", order="10", key="services.kafka-version",
        finalise="metadata.version bump after a soak; one way", rollback="none",
    )
    move = u.Move(step=step, old="4.2.0", new="4.3.1")
    ran, detail = u.run_finalise_hook(
        deploy, move, from_name="1.0.0", to_name="2.0.0", kubeconfig=None, assume_yes=True
    )
    assert ran is True
    assert "marker written" in detail
    finalised = u.read_finalised_keys(deploy)
    assert "services.kafka-version" in finalised
    assert datetime.fromisoformat(finalised["services.kafka-version"])


def test_run_finalise_hook_declined_writes_no_marker(monkeypatch: pytest.MonkeyPatch, deploy: Path) -> None:
    step = u.Step(
        stage="30-services", order="10", key="services.kafka-version",
        finalise="metadata.version bump after a soak; one way", rollback="none",
    )
    move = u.Move(step=step, old="4.2.0", new="4.3.1")
    monkeypatch.setattr("builtins.input", lambda *_a: "n")
    ran, detail = u.run_finalise_hook(
        deploy, move, from_name="1.0.0", to_name="2.0.0", kubeconfig=None, assume_yes=False
    )
    assert ran is False
    assert "declined" in detail
    assert u.read_finalised_keys(deploy) == {}


def test_check_cluster_metadata_version_no_finalise_moves() -> None:
    ok, detail = u.check_cluster_metadata_version(None, [])
    assert ok is True
    assert "nothing to check" in detail


def test_check_cluster_metadata_version_refuses_when_bumped(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    step = u.Step(
        stage="30-services", order="10", key="services.kafka-version",
        finalise="metadata.version bump after a soak; one way", rollback="none",
    )
    move = u.Move(step=step, old="4.2.0", new="4.3.1")
    doc = {"status": {"kafkaMetadataVersion": "4.3.1"}}
    _mock_run(monkeypatch, _proc(0, stdout=json.dumps(doc)))
    ok, detail = u.check_cluster_metadata_version("kc", [move])
    assert ok is False
    assert "services.kafka-version" in detail


def test_check_cluster_metadata_version_passes_when_not_yet_bumped(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    step = u.Step(
        stage="30-services", order="10", key="services.kafka-version",
        finalise="metadata.version bump after a soak; one way", rollback="none",
    )
    move = u.Move(step=step, old="4.2.0", new="4.3.1")
    doc = {"status": {"kafkaMetadataVersion": "4.2.0"}}
    _mock_run(monkeypatch, _proc(0, stdout=json.dumps(doc)))
    ok, _detail = u.check_cluster_metadata_version("kc", [move])
    assert ok is True


def test_check_cluster_metadata_version_unreachable_is_a_clean_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    step = u.Step(
        stage="30-services", order="10", key="services.kafka-version",
        finalise="metadata.version bump after a soak; one way", rollback="none",
    )
    move = u.Move(step=step, old="4.2.0", new="4.3.1")
    _mock_run(monkeypatch, _proc(1, stderr="Unable to connect to the server\n"))
    ok, detail = u.check_cluster_metadata_version("kc", [move])
    assert ok is True
    assert "cannot read Kafka CR" in detail


def test_cmd_upgrade_rollback_check_cluster_refuses_without_marker(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    import json

    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    u.bump_pin_file(deploy, "2.0.0")
    # No marker exists, but the live cluster already shows the bumped value --
    # e.g. an operator ran the metadata.version bump by hand.
    doc = {"status": {"kafkaMetadataVersion": "4.3.1"}}
    _mock_run(monkeypatch, _proc(0, stdout=json.dumps(doc)))

    args = _rollback_args(deploy=str(deploy), to="1.0.0", kubeconfig="kc", check_cluster=True)
    rc = u.cmd_upgrade_rollback(args)
    assert rc == u.EXIT_BLOCKED
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "check-cluster" in err


def test_cmd_upgrade_rollback_check_cluster_passes_when_not_bumped(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
) -> None:
    import json

    monkeypatch.setattr(u, "UPGRADE_ORDER", order_path)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions_path)
    u.bump_pin_file(deploy, "2.0.0")
    doc = {"status": {"kafkaMetadataVersion": "4.2.0"}}
    _mock_run(monkeypatch, _proc(0, stdout=json.dumps(doc)))

    args = _rollback_args(deploy=str(deploy), to="1.0.0", kubeconfig="kc", check_cluster=True)
    rc = u.cmd_upgrade_rollback(args)
    assert rc == u.EXIT_OK


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
def real_git_deploy(tmp_path: Path) -> Path:
    """A real git repo standing in for the deploy checkout, with pins.yaml,
    sizing/ and upgrades/ committed so a stage with nothing new hits a real
    `git commit` on a clean tree rather than a mocked one."""
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


def _stub_cluster_facing_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(u, "run_compat_check", lambda *_a, **_k: (True, "ok"))
    monkeypatch.setattr(u, "run_preflight", lambda *_a, **_k: [])
    monkeypatch.setattr(u, "wait_for_argo", lambda *_a, **_k: (True, "converged"))
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "Test")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, "test@example.invalid")


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
    assert "stage 2 (20-second) changed nothing" in err
    log = u._git(real_git_deploy, "log", "--oneline").stdout
    assert "stage 1 -- bootstrap.cert-manager" in log
    assert "stage 2 -- services.clickhouse-version" not in log
    assert (real_git_deploy / "pins.yaml").read_text(encoding="utf-8") == PINS_YAML.replace("1.0.0", "2.0.0")
