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

- every 2.2.0 (kind, name) is still rendered, and every claim name
- every 2.2.0 selector is byte-equal
- an object only the new render holds is listed in fixtures/weave-accepted-diffs.yaml
- no (kind, name) renders twice across the thin chart and dfe-extras

Render (b) needs the scalo-service library (_weave.library).
"""

import copy

import pytest

from _gate import MATRIX, accepts, cell, identity_problems


@pytest.mark.parametrize(("service", "profile", "cloud"), MATRIX)
def test_the_thin_chart_adopts_every_2_2_0_object(service: str, profile: str, cloud: str) -> None:
    c = cell(service, profile, cloud)
    assert identity_problems(c.old, c.new, accepts(service).added) == []


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
