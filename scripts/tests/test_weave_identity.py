#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_weave_identity.py
#  Purpose:      Prove each switched component's thin chart beside dfe-extras
#                keeps every 2.2.0 object, claim name and selector, so Argo
#                adopts the live objects rather than pruning them.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The identity gate of the chart switch, per component, profile and cloud.

    python3 -m pytest scripts/tests/test_weave_identity.py -q

Argo adopts a live object by (kind, name). A lost or renamed object is pruned and
recreated, a renamed claim comes back empty, and a changed selector cannot be
applied at all. Each cell of _gate.MATRIX holds:

- every 2.2.0 (kind, name) is still rendered, or listed removed in
  fixtures/weave-accepted-diffs.yaml, and every claim name whatever that lists
- every 2.2.0 selector is byte-equal
- an object only the new render holds is listed in fixtures/weave-accepted-diffs.yaml
- no (kind, name) renders twice across the thin chart and dfe-extras

dfe-engine's config claim keeps its 2.2.0 name on every profile and cloud, and
the contract's singleton runs exactly one pod.

Render (b) needs the scalo-service library (_weave.library).
"""

import copy
from pathlib import Path

import pytest
import yaml

import _gate
from _gate import (
    CLOUDS,
    MATRIX,
    PROFILES,
    AcceptedError,
    accepted,
    accepts,
    cell,
    identity_problems,
    object_id,
)
from _weave import render_app, weave


@pytest.mark.parametrize(("service", "profile", "cloud"), MATRIX)
def test_the_thin_chart_adopts_every_2_2_0_object(service: str, profile: str, cloud: str) -> None:
    c = cell(service, profile, cloud)
    found = accepts(service)
    assert identity_problems(c.old, c.new, found.added, found.removed) == []


def test_every_removed_object_is_lost_somewhere() -> None:
    """An entry no cell loses would let a later loss of that object pass unread."""
    lost: dict[str, set[str]] = {}
    for service, profile, cloud in MATRIX:
        c = cell(service, profile, cloud)
        facets = weave().diff_docs(c.old, c.new)
        lost.setdefault(service, set()).update(facets["objects"]["only_old"])
    stale = [
        f"{section}: {obj}"
        for section, body in accepted().items()
        for obj in body.removed
        if not any(obj in found for service, found in lost.items() if section in ("*", service))
    ]
    assert stale == []


# ------------------------------------------------------------------- dfe-engine

ENGINE = "dfe-engine"
ENGINE_CELLS = [(p, k) for p in PROFILES for k in CLOUDS]


def _claims(docs: list[dict]) -> dict[str, list[str]]:
    """Each claim the render holds, and the claim each workload mounts."""
    held = sorted(d["metadata"]["name"] for d in docs if d["kind"] == "PersistentVolumeClaim")
    mounted = sorted(
        v["persistentVolumeClaim"]["claimName"]
        for d in docs
        if d["kind"] == "Deployment"
        for v in d["spec"]["template"]["spec"].get("volumes") or []
        if "persistentVolumeClaim" in v
    )
    return {"held": held, "mounted": mounted}


@pytest.mark.parametrize(("profile", "cloud"), ENGINE_CELLS)
def test_the_engine_config_claim_keeps_its_2_2_0_name(profile: str, cloud: str) -> None:
    """A renamed claim is pruned and comes back empty, factory-resetting accounts and orgs."""
    c = cell(ENGINE, profile, cloud)
    want = {"held": ["dfe-engine-config"], "mounted": ["dfe-engine-config"]}
    assert (_claims(c.old), _claims(c.new)) == (want, want)


@pytest.mark.parametrize(("profile", "cloud"), ENGINE_CELLS)
def test_the_engine_runs_exactly_one_pod(profile: str, cloud: str) -> None:
    """The contract's singleton: one replica, Recreate, and nothing that scales or budgets it."""
    new = cell(ENGINE, profile, cloud).new
    spec = next(d for d in new if object_id(d) == f"Deployment/{ENGINE}")["spec"]
    assert (spec["replicas"], spec["strategy"]) == (1, {"type": "Recreate"})
    scaling = {"PodDisruptionBudget", "HorizontalPodAutoscaler", "ScaledObject"}
    assert [d["metadata"]["name"] for d in new if d["kind"] in scaling] == []


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        ({"keda": {"enabled": True}}, "keda.enabled is set"),
        ({"autoscaling": {"enabled": True}}, "autoscaling.enabled is set"),
    ],
    ids=["keda", "hpa"],
)
def test_scaling_the_engine_fails_the_render(tmp_path: Path, overlay: dict, message: str) -> None:
    instance = tmp_path / "values" / "dfe-engine-default-values.yaml"
    instance.parent.mkdir()
    body = {"deploy": {"service": ENGINE, "instance": "default"}, **overlay}
    instance.write_text(yaml.safe_dump(body), encoding="utf-8", newline="\n")
    with pytest.raises(weave().WeaveError, match=message):
        render_app(ENGINE, "scale", "aws", "new", deploy_repo=tmp_path)


# ---------------------------------------------------------------- expected fails


def _claim(name: str) -> dict:
    return {"kind": "PersistentVolumeClaim", "metadata": {"name": name}, "spec": {}}


def _mount(workload: dict, claim: str) -> None:
    pod = workload["spec"]["template"]["spec"]
    pod.setdefault("volumes", []).append(
        {"name": "data", "persistentVolumeClaim": {"claimName": claim}}
    )


def _deployment(docs: list[dict]) -> dict:
    return next(d for d in docs if d["kind"] == "Deployment")


def test_a_renamed_pvc_fails() -> None:
    c = cell("dfe-ui", "single", "aws").mutable()
    c.old.append(_claim("dfe-ui-data"))
    _mount(_deployment(c.old), "dfe-ui-data")
    c.new.append(_claim("dfe-ui-data-0"))
    _mount(_deployment(c.new), "dfe-ui-data-0")
    problems = identity_problems(c.old, c.new, accepts("dfe-ui").added)
    assert any("PersistentVolumeClaim/dfe-ui-data'" in p for p in problems), problems
    assert any("claimName/dfe-ui-data'" in p for p in problems), problems


def test_a_changed_selector_fails() -> None:
    c = cell("hyperdx", "scale", "aws").mutable()
    selector = _deployment(c.new)["spec"]["selector"]["matchLabels"]
    selector["app.kubernetes.io/instance"] = "hyperdx-default-in-cluster"
    problems = identity_problems(c.old, c.new, accepts("hyperdx").added)
    assert problems == [
        "Deployment/dfe-hyperdx selector "
        '{"matchLabels":{"app.kubernetes.io/name":"dfe-hyperdx"}} -> '
        '{"matchLabels":{"app.kubernetes.io/instance":"hyperdx-default-in-cluster",'
        '"app.kubernetes.io/name":"dfe-hyperdx"}}'
    ]


def test_a_lost_object_fails() -> None:
    c = cell("dfe-ui", "slim", "local").mutable()
    new = [d for d in c.new if d["kind"] != "ExternalSecret"]
    problems = identity_problems(c.old, new, accepts("dfe-ui").added)
    assert problems == ["lost or renamed: ['ExternalSecret/dfe-ui-nextauth']"]


def test_an_unlisted_added_object_fails() -> None:
    c = cell("hyperdx", "slim", "local")
    added = dict(accepts("hyperdx").added)
    del added["ServiceAccount/dfe-hyperdx"]
    problems = identity_problems(c.old, c.new, added)
    assert problems == ["added and not accepted: ['ServiceAccount/dfe-hyperdx']"]


def test_an_object_both_sources_render_fails() -> None:
    c = cell("hyperdx", "slim", "local")
    new = [*c.new, copy.deepcopy(next(d for d in c.new if d["kind"] == "ServiceAccount"))]
    problems = identity_problems(c.old, new, accepts("hyperdx").added)
    assert problems == ["rendered twice: ['ServiceAccount/dfe-hyperdx']"]


def test_a_removed_object_not_listed_fails() -> None:
    """Without its entry, the engine's budget lost on scale fails, and so does its selector."""
    c = cell(ENGINE, "scale", "aws")
    problems = identity_problems(c.old, c.new, accepts(ENGINE).added, removed={})
    assert problems == [
        "lost or renamed: ['PodDisruptionBudget/dfe-engine']",
        "PodDisruptionBudget/dfe-engine selector "
        '{"matchLabels":{"app.kubernetes.io/name":"dfe-engine"}} -> None',
    ]


def test_a_renamed_claim_fails_whatever_is_listed_removed() -> None:
    c = cell(ENGINE, "single", "local").mutable()
    claim = next(d for d in c.new if d["kind"] == "PersistentVolumeClaim")
    claim["metadata"]["name"] = "dfe-engine-config-0"
    added = {**accepts(ENGINE).added, "PersistentVolumeClaim/dfe-engine-config-0": "a reason"}
    removed = {"PersistentVolumeClaim/dfe-engine-config": "a reason"}
    problems = identity_problems(c.old, c.new, added, removed)
    assert problems == ["claims lost or renamed: ['PersistentVolumeClaim/dfe-engine-config']"]


def test_a_claim_listed_removed_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = tmp_path / "accepted.yaml"
    body = {ENGINE: {"removed": {"PersistentVolumeClaim/dfe-engine-config": "a reason"}}}
    fixture.write_text(yaml.safe_dump(body), encoding="utf-8", newline="\n")
    monkeypatch.setattr(_gate, "ACCEPTED", fixture)
    with pytest.raises(AcceptedError, match="cannot be accepted as removed"):
        accepted.__wrapped__()
