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
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from test_clickhouse_replica_spread import keeper_pod_labels as ch_keeper_labels
from test_clickhouse_replica_spread import render as ch_render
from test_clickhouse_replica_spread import server_pod_labels as ch_server_labels

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


@pytest.fixture(autouse=True)
def _no_real_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test whose kubectl call reaches the real subprocess boundary,
    which would read whatever cluster this host's kubeconfig names. git still
    runs for real, for the tests built on a real deploy repo."""
    real_run = u._run

    def guarded(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if cmd[:1] == ["kubectl"]:
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
    base = dict(dial=None, fixtures=None, live=False, kubeconfig=None, argocd_namespace="argocd")
    base.update(overrides)
    return _Args(**base)


def test_cmd_upgrade_plan_writes_file_and_reports_compat_check(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, order_path: Path, versions_path: Path, capsys: pytest.CaptureFixture
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
    written = deploy / "upgrades" / "1.0.0-to-1.1.0.md"
    assert written.is_file()
    assert "before:   kafka.strimzi.io stored-version conversion" in written.read_text(encoding="utf-8")


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
    capsys: pytest.CaptureFixture,
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
    written = (bundled / "upgrades" / "1.0.0-to-1.1.0.md").read_text(encoding="utf-8")
    assert "the deploy repo carries no pins.yaml, so apply writes one at its first stage" in written
    assert "1. bootstrap.cert-manager: v1.0.0 -> v1.1.0" in written
    assert not (bundled / "pins.yaml").exists()


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
        from_stack=None, target_revision=None,
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
        clickhouse_selector=u.DEFAULT_CLICKHOUSE_SELECTOR, clickhouse_merge_threshold=300.0,
        clickhouse_credentials=u.DEFAULT_CLICKHOUSE_CREDENTIALS,
        nodes_file=None, backup_marker=u.DEFAULT_BACKUP_MARKER,
        yes=False, push=False, timeout=900, dry_run=True, finalise=False, stop_before=None,
        from_stack=None, target_revision=None,
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
        from_stack=None, target_revision=None,
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
        from_stack=None, target_revision=None,
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


def _stub_cluster_facing_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(u, "run_compat_check", lambda *_a, **_k: (True, "ok"))
    monkeypatch.setattr(u, "run_preflight", lambda *_a, **_k: [])
    monkeypatch.setattr(u, "wait_for_argo", lambda *_a, **_k: (True, "converged"))
    # A branch-tracking secret: nothing to retarget, so no --push is needed.
    monkeypatch.setattr(u, "read_target_revision", lambda *_a, **_k: "main")


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
