#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_acceptance_footprint.py
#  Purpose:      Prove the source and edge suites leave the deployment as they
#                found it, on a pass and on a forced failure, and that a
#                leftover fails the run with a named row.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for acceptance/footprint.py and its use by the source and edge runners.

A small in-memory cluster stands in for the deployment: it holds the sources the
engine lists, the Deployments, the receiver's ConfigMap, the receiver overlay and
the harness namespaces. The runners are driven against it with the failure forced
at feed, at prove and in the edge rows, and the cluster is read before and after.

    python3 -m pytest scripts/tests/test_acceptance_footprint.py -q
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from acceptance import footprint  # noqa: E402
from acceptance.clients import Reply  # noqa: E402
from acceptance.edge import run as edge_run  # noqa: E402
from acceptance.onboarding import wizard  # noqa: E402
from acceptance.source import beats  # noqa: E402
from acceptance.source import run as source_run  # noqa: E402

NAME = "fb-0a1b2c3d"
LISTENER = dict(beats.LUMBERJACK_SWITCHES)


def printed(footprint_: footprint.Footprint) -> dict:
    return {
        "configmaps": dict(footprint_.configmaps), "replicas": dict(footprint_.replicas),
        "sources": footprint_.sources, "overlay": dict(footprint_.overlay),
        "namespaces": footprint_.namespaces,
    }


# --- the cluster ----------------------------------------------------------------------


class Cluster:
    """What the deployment holds, and the engine and kubectl answers read off it."""

    def __init__(self) -> None:
        self.sources: set[str] = {"main"}
        self.deployments: dict[str, int] = {"dfe-receiver": 2, "dfe-engine": 1, "dfe-loader": 3}
        self.autoscaled: set[str] = {"dfe-loader"}
        self.configmaps: dict[str, dict] = {"dfe-receiver": {"routes": "main"}}
        self.overlay: dict[str, object] = {}
        self.namespaces: dict[str, str] = {}

    # engine
    def call(self, method: str, path: str, body: object = None) -> Reply:
        if path == "/sources":
            return Reply(200, {"items": [{"name": n} for n in sorted(self.sources)]})
        if path == footprint.RECEIVER_ROUTE and method == "GET":
            fields = [{"path": p, "is_set": p in self.overlay, "value": self.overlay.get(p)} for p in LISTENER]
            return Reply(200, {"fields": fields, "unknown": []})
        if path == f"/sources/{NAME}":
            return Reply(200 if NAME in self.sources else 404, {})
        return Reply(200, {})

    # kubectl
    def kubectl_json(self, kube: list[str], *argv: str) -> dict:
        if "configmap" in argv:
            return {"items": [{"metadata": {"name": n}, "data": d} for n, d in self.configmaps.items()]}
        if "deployment" in argv:
            return {"items": [{"metadata": {"name": n}, "spec": {"replicas": r}} for n, r in self.deployments.items()]}
        if "hpa" in argv:
            return {"items": [
                {"spec": {"scaleTargetRef": {"kind": "Deployment", "name": n}}} for n in self.autoscaled
            ]}
        if "namespace" in argv:
            return {"items": [{"metadata": {"name": n}, "status": {"phase": p}} for n, p in self.namespaces.items()]}
        raise AssertionError(argv)

    def read(self) -> footprint.Footprint:
        return footprint.capture(["kubectl"], "dfe", self)

    # what the harness does to it
    def create_source(self) -> None:
        self.sources.add(NAME)
        self.deployments[f"dfe-transform-vrl-{NAME}"] = 1
        self.configmaps[f"dfe-transform-vrl-{NAME}"] = {"program": "x"}
        self.configmaps["dfe-receiver"] = {"routes": f"main,{NAME}"}

    def remove_source(self) -> None:
        self.sources.discard(NAME)
        self.deployments.pop(f"dfe-transform-vrl-{NAME}", None)
        self.configmaps.pop(f"dfe-transform-vrl-{NAME}", None)
        self.configmaps["dfe-receiver"] = {"routes": "main"}


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> Cluster:
    held = Cluster()
    monkeypatch.setattr(footprint, "kubectl_json", held.kubectl_json)
    # A leftover is read once, not waited on for the real settle time.
    monkeypatch.setattr(footprint, "SETTLE_DEADLINE", 0.0)
    return held


# --- the readings and their diff ---------------------------------------------------------


def test_a_reading_ignores_what_it_should_not_compare(cluster: Cluster) -> None:
    cluster.configmaps["unrelated"] = {"a": "b"}
    cluster.namespaces = {"gone": "Terminating", "probe": "Active"}
    reading = cluster.read()
    assert set(reading.configmaps) == {"dfe-receiver"}
    assert reading.replicas == {"dfe-receiver": 2, "dfe-engine": 1, "dfe-loader": None}
    assert reading.namespaces == ("probe",)
    assert reading.sources == ("main",)


def _leave(cluster: Cluster, what: str) -> None:
    if what == "clean":
        return
    if what == "source":
        cluster.sources.add(NAME)
    elif what == "deployment":
        cluster.deployments["dfe-transform-vrl-x"] = 1
    elif what == "scaled":
        cluster.deployments["dfe-receiver"] = 0
    elif what == "autoscaled-moves":
        cluster.deployments["dfe-loader"] = 9
    elif what == "configmap":
        cluster.configmaps["dfe-receiver"] = {"routes": "main,leak"}
    elif what == "listener":
        cluster.overlay = dict(LISTENER)
    elif what == "namespace":
        cluster.namespaces["x-probe"] = "Active"
    elif what == "namespace-terminating":
        cluster.namespaces["x-probe"] = "Terminating"
    elif what == "deployment-gone":
        del cluster.deployments["dfe-engine"]
    else:
        raise AssertionError(what)


@pytest.mark.parametrize(
    ("what", "parts"),
    [
        ("clean", []),
        ("autoscaled-moves", []),
        ("namespace-terminating", []),
        ("source", ["sources"]),
        ("deployment", ["deployments"]),
        ("scaled", ["deployments"]),
        ("deployment-gone", ["deployments"]),
        ("configmap", ["configmaps"]),
        ("listener", ["receiver-overlay"]),
        ("namespace", ["namespaces"]),
    ],
)
def test_any_difference_is_named_by_part(cluster: Cluster, what: str, parts: list[str]) -> None:
    before = cluster.read()
    _leave(cluster, what)
    assert sorted(footprint.diff(before, cluster.read())) == parts


def test_keep_allows_the_kept_source_and_what_is_named_after_it_and_nothing_else(cluster: Cluster) -> None:
    before = cluster.read()
    cluster.create_source()
    assert footprint.diff(before, cluster.read(), keep_name=NAME) == {}
    assert sorted(footprint.diff(before, cluster.read())) == ["configmaps", "deployments", "sources"]
    cluster.overlay = dict(LISTENER)
    cluster.deployments["dfe-other"] = 1
    assert sorted(footprint.diff(before, cluster.read(), keep_name=NAME)) == ["deployments", "receiver-overlay"]


def test_settle_waits_for_the_apps_to_leave(cluster: Cluster) -> None:
    before = cluster.read()
    cluster.create_source()
    reads: list[int] = []

    def read() -> footprint.Footprint:
        reads.append(1)
        if len(reads) == 3:
            cluster.remove_source()
        return cluster.read()

    now = [0.0]
    rows = footprint.settle(
        before, read, deadline=60, interval=5, sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0]
    )
    assert [(r.slug, r.status) for r in rows] == [("no-trace", "done")]
    assert len(reads) == 3


def test_settle_names_what_is_still_there_at_the_deadline(cluster: Cluster) -> None:
    before = cluster.read()
    cluster.create_source()
    now = [0.0]
    rows = footprint.settle(
        before, cluster.read, deadline=10, interval=5, sleep=lambda s: now.__setitem__(0, now[0] + s),
        clock=lambda: now[0],
    )
    assert {r.slug for r in rows} == {"no-trace-configmaps", "no-trace-deployments", "no-trace-sources"}
    assert all(r.status == "failed" for r in rows)
    assert NAME in next(r for r in rows if r.slug == "no-trace-sources").detail


def test_a_reading_that_cannot_be_taken_fails_the_run(cluster: Cluster) -> None:
    before = cluster.read()

    def broken() -> footprint.Footprint:
        raise footprint.FootprintError("the engine did not answer: refused")

    rows = footprint.settle(before, broken)
    assert [(r.slug, r.status) for r in rows] == [("no-trace", "failed")]
    taken, rows = footprint.baseline(broken)
    assert taken is None
    assert [(r.slug, r.status) for r in rows] == [("no-trace", "failed")]


def test_an_engine_that_is_down_is_a_footprint_error(cluster: Cluster) -> None:
    class Down:
        def call(self, *_a: object) -> Reply:
            raise OSError("connection refused")

    with pytest.raises(footprint.FootprintError, match="did not answer"):
        footprint.capture([], "dfe", Down())


# --- the source runner, failing at feed and at prove -----------------------------------------


class RecordingDriver:
    def __init__(self) -> None:
        self.results: list[wizard.StepResult] = []

    def record(self, slug: str, status: str, detail: str) -> None:
        self.results.append(wizard.StepResult(slug, status, detail))


class FakeCase:
    """A case that opens the receiver listener and deploys a source, then fails where told to."""

    name = NAME

    def __init__(self, cluster: Cluster, fail_at: str, *, restores: bool = True) -> None:
        self.cluster, self.fail_at, self.restores = cluster, fail_at, restores

    def step(self, at: str) -> None:
        if self.fail_at == at:
            raise RuntimeError(f"the engine went away at {at}")

    def cleanup(self, run: object) -> list[wizard.StepResult]:
        if self.restores:
            self.cluster.overlay = {}
        return [wizard.StepResult("receiver-lumberjack-restored", "done", "put back")]


def _drive(monkeypatch: pytest.MonkeyPatch, cluster: Cluster, case: FakeCase, *, keep: bool = False,
           removes: bool = True) -> tuple[footprint.Footprint, list[wizard.StepResult]]:
    """Run the source runner's attempt and conclude against the cluster and return the reading before and the rows."""

    def walk(run: object, c: FakeCase, *_a: object) -> None:
        cluster.create_source()
        cluster.overlay = dict(LISTENER)
        c.step("feed")
        c.step("prove")

    def remove_source(*_a: object) -> str:
        if removes:
            cluster.remove_source()
        return f"removed {NAME}"

    monkeypatch.setattr(source_run, "walk", walk)
    monkeypatch.setattr(source_run, "remove_source", remove_source)
    monkeypatch.setattr(source_run.steps, "assert_hyperdx_source_gone", lambda *_a: ("done", "gone"))
    driver = RecordingDriver()
    run = types.SimpleNamespace(
        driver=driver, engine=types.SimpleNamespace(call=cluster.call, verify=False, token=None),
        args=types.SimpleNamespace(keep=keep, diagnostics_exec="", diagnostics_timeout=1.0),
    )
    before = cluster.read()
    taken = source_run.attempt(run, case, [], [], lambda: None, cluster.read)
    assert taken == before, "the reading taken before the walk is the deployment as it stood"
    source_run.conclude(run, case, "https://engine", taken, cluster.read)
    return before, driver.results


@pytest.mark.parametrize("fail_at", ["feed", "prove", "never"])
def test_the_source_run_leaves_the_deployment_as_it_found_it(
    monkeypatch: pytest.MonkeyPatch, cluster: Cluster, fail_at: str
) -> None:
    case = FakeCase(cluster, fail_at)
    before, rows = _drive(monkeypatch, cluster, case)
    after = cluster.read()
    assert printed(after) == printed(before)
    by_slug = {r.slug: r for r in rows}
    assert by_slug["no-trace"].status == "done"
    assert ("run" in by_slug) is (fail_at != "never")


@pytest.mark.parametrize("fail_at", ["feed", "prove"])
def test_keep_leaves_only_the_documented_source(monkeypatch: pytest.MonkeyPatch, cluster: Cluster, fail_at: str) -> None:
    case = FakeCase(cluster, fail_at)
    _, rows = _drive(monkeypatch, cluster, case, keep=True, removes=False)
    assert {r.slug: r.status for r in rows}["no-trace"] == "done"
    assert "apart from the kept source" in next(r for r in rows if r.slug == "no-trace").detail


@pytest.mark.parametrize("fail_at", ["feed", "prove"])
def test_a_cleanup_that_leaves_the_listener_open_fails_by_name(
    monkeypatch: pytest.MonkeyPatch, cluster: Cluster, fail_at: str
) -> None:
    """Before the teardown ran its cleanup on every path, an engine failure left exactly this."""
    case = FakeCase(cluster, fail_at, restores=False)
    _, rows = _drive(monkeypatch, cluster, case)
    failed = {r.slug: r for r in rows if r.status == "failed"}
    assert "no-trace-receiver-overlay" in failed
    assert "config.lumberjack.accept_unauthenticated" in failed["no-trace-receiver-overlay"].detail


def test_a_source_the_delete_leaves_behind_fails_by_name(monkeypatch: pytest.MonkeyPatch, cluster: Cluster) -> None:
    case = FakeCase(cluster, "feed")
    _, rows = _drive(monkeypatch, cluster, case, removes=False)
    failed = {r.slug for r in rows if r.status == "failed"}
    assert {"no-trace-sources", "no-trace-deployments"} <= failed


# --- the edge runner ---------------------------------------------------------------------------


def _edge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cluster: Cluster, prove) -> tuple[int, str]:
    monkeypatch.setenv("DFE_E2E_ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(edge_run.Datastore, "from_env", classmethod(lambda cls, _env: types.SimpleNamespace(password="")))
    monkeypatch.setattr(
        edge_run, "Engine",
        lambda *_a, **_k: types.SimpleNamespace(call=cluster.call, password="", token="", base="https://e"),
    )
    monkeypatch.setattr(edge_run.onboarding, "_apply_resolve_map", lambda _pairs: None)
    monkeypatch.setattr(edge_run, "prove", prove)
    args = edge_run.build_parser().parse_args([
        "--ui-url", "https://dfe.example", "--engine-url", "https://dfe.example", "--namespace", "dfe",
        "--kubectl", "kubectl", "--allow", "203.0.113.7/32", "--egress-cidrs", "198.51.100.1/32",
        "--shots-dir", str(tmp_path / "shots"),
    ])
    return edge_run.run(args), (tmp_path / "shots" / "steps.txt").read_text(encoding="utf-8")


def test_an_edge_run_that_fails_midway_still_leaves_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cluster: Cluster
) -> None:
    before = cluster.read()

    def prove(args: object, results: list, *_a: object) -> None:
        cluster.namespaces["run-probe"] = "Active"
        cluster.namespaces.pop("run-probe")
        raise RuntimeError("the gateway went away")

    code, table = _edge(monkeypatch, tmp_path, cluster, prove)
    assert code == 1
    assert "the gateway went away" in table
    assert printed(cluster.read()) == printed(before)
    assert "no-trace" in table
    assert "no-trace-" not in table


def test_an_edge_probe_namespace_left_behind_fails_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cluster: Cluster
) -> None:
    def prove(args: object, results: list, *_a: object) -> None:
        cluster.namespaces["run-probe"] = "Active"
        raise RuntimeError("the gateway went away")

    code, table = _edge(monkeypatch, tmp_path, cluster, prove)
    assert code == 1
    assert "FAILED: run, no-trace-namespaces" in table
