#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_governance_actions.py
#  Purpose:      Render the thin path with each dfe-deploy governance action
#                applied and prove no pod is left reading a Secret the action
#                stopped minting.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The dfe-deploy governance actions, rendered through the thin path.

    env DFE_DEPLOY_ROOT=../dfe-deploy python3 -m pytest scripts/tests/test_governance_actions.py -q

An action writes dials into values/<service>-<instance>-values.yaml, the file the layer 2 appset
layers last. scripts/check_governance_drift.py proves each dial's key is one the deployed chart
reads, and a key that exists says nothing about whether the keys beside it agree. hunts-pause once
set huntRunner.enabled false and left secrets.hunt-runner.enabled on: dfe-extras stopped minting
hunt-runner-clickhouse, the engine Deployment still read it through a required secretKeyRef, and
the engine pod stayed in CreateContainerConfigError.

Every helmvars change of every action is applied over the instance file and rendered, dfe-engine
on slim/local and scale/aws, through the appset, value files and dfe-extras the goldens render. A
pod may not read a Secret the unchanged render mints and the action's render no longer does, and
hunts-resume must render what hunts-pause started from.

The actions come from a dfe-deploy checkout: DFE_DEPLOY_ROOT, else ../dfe-deploy. With neither the
tests that read them skip, and a DFE_DEPLOY_ROOT that holds no governance/actions fails. The
cases that apply an action written in this file need no checkout.

Rendering needs the scalo-service library (_weave.library).
"""

import functools
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from _gate import COMPONENTS, WORKLOAD_KINDS, object_id, render_differences
from _weave import REPO_ROOT, render_app

CELLS = (("slim", "local"), ("scale", "aws"))
SLIM = ("slim", "local")

PAUSE_THE_RUNNER = {
    "cls": "helmvars",
    "name": "dfe-engine-default-values",
    "path": "huntRunner.enabled",
    "value": False,
}
PAUSE_THE_SECRET = {**PAUSE_THE_RUNNER, "path": "secrets.hunt-runner.enabled"}
RESUME_THE_RUNNER = {**PAUSE_THE_RUNNER, "value": True}
RESUME_THE_SECRET = {**PAUSE_THE_SECRET, "value": True}


# ------------------------------------------------------------------ applying an action


def resolve(value: object, params: dict) -> object:
    """A change's value: a literal, or its parameter's default through the optional ``map``."""
    if not (isinstance(value, dict) and "$param" in value):
        return value
    default = params[value["$param"]]["default"]
    return value["map"][default] if "map" in value else default


def apply_actions(actions: list[dict]) -> dict[str, dict]:
    """The values each instance file holds after the actions' helmvars changes, in order.

    The engine sets a dotted path by creating each parent mapping, so a change to
    ``secrets.hunt-runner.enabled`` lands as ``secrets: {hunt-runner: {enabled: ...}}``.
    """
    files: dict[str, dict] = {}
    for action in actions:
        params = action.get("params") or {}
        for change in action.get("changes") or []:
            if change.get("cls") != "helmvars":
                continue
            node = files.setdefault(change["name"], {})
            *parents, leaf = change["path"].split(".")
            for key in parents:
                node = node.setdefault(key, {})
            node[leaf] = resolve(change["value"], params)
    return files


def instance_of(name: str) -> tuple[str, str]:
    """``dfe-engine-default-values`` as (``dfe-engine``, ``default``), the service longest first."""
    stem = name.removesuffix("-values")
    services = [s for s in COMPONENTS if stem == s or stem.startswith(f"{s}-")]
    if not services:
        pytest.fail(f"{name} names no service the gate renders: {', '.join(COMPONENTS)}")
    service = max(services, key=len)
    return service, stem[len(service) + 1 :] or "default"


def render_with(service: str, instance: str, profile: str, cloud: str, values: dict) -> list[dict]:
    """The thin path of one instance with ``values`` in its deploy repo file."""
    return render_app(service, profile, cloud, overlay=values, instance=instance)


@functools.cache
def unchanged(service: str, instance: str, profile: str, cloud: str) -> list[dict]:
    """The render with the instance file holding nothing but its ``deploy`` block."""
    return render_with(service, instance, profile, cloud, {})


# ---------------------------------------------------------------------- Secrets


def pod_of(doc: dict) -> dict:
    """The pod spec of a workload, empty for any other object."""
    if doc.get("kind") not in WORKLOAD_KINDS:
        return {}
    return (doc.get("spec", {}).get("template", {}) or {}).get("spec", {}) or {}


def minted(docs: list[dict]) -> set[str]:
    """The Secrets the render creates: a Secret, or what an ExternalSecret or Certificate writes."""
    names = set()
    for doc in docs:
        name, spec = doc["metadata"]["name"], doc.get("spec") or {}
        match doc.get("kind"):
            case "Secret":
                names.add(name)
            case "ExternalSecret":
                names.add((spec.get("target") or {}).get("name") or name)
            case "Certificate" if spec.get("secretName"):
                names.add(spec["secretName"])
    return names


def container_reads(container: dict) -> Iterator[tuple[str, str]]:
    """(where, Secret name) for each Secret a container needs before it can start."""
    for var in container.get("env") or []:
        ref = (var.get("valueFrom") or {}).get("secretKeyRef")
        if ref and not ref.get("optional"):
            yield f"{container['name']} env {var['name']}", ref["name"]
    for source in container.get("envFrom") or []:
        ref = source.get("secretRef")
        if ref and not ref.get("optional"):
            yield f"{container['name']} envFrom", ref["name"]


def volume_reads(volume: dict) -> Iterator[tuple[str, str]]:
    """(where, Secret name) for each Secret a volume needs before the pod can mount it."""
    sources = [volume, *(volume.get("projected") or {}).get("sources", [])]
    for source in sources:
        ref = source.get("secret")
        if ref and not ref.get("optional"):
            yield f"volume {volume['name']}", ref.get("secretName") or ref["name"]


def required_reads(docs: list[dict]) -> Iterator[tuple[str, str, str]]:
    """(workload, where, Secret name) for every Secret a pod cannot start without."""
    for doc in docs:
        pod = pod_of(doc)
        for container in (pod.get("initContainers") or []) + (pod.get("containers") or []):
            for where, secret in container_reads(container):
                yield object_id(doc), where, secret
        for volume in pod.get("volumes") or []:
            for where, secret in volume_reads(volume):
                yield object_id(doc), where, secret


def orphaned(before: list[dict], after: list[dict]) -> list[str]:
    """Each required read in ``after`` of a Secret ``before`` minted and ``after`` does not.

    A Secret neither render mints belongs to another chart (the ClickHouse admin password, the
    deploy repo credentials), which no action here switches off.
    """
    lost = minted(before) - minted(after)
    reads = required_reads(after)
    return sorted(f"{who} {where} reads {secret}" for who, where, secret in reads if secret in lost)


# ---------------------------------------------------------------------- the checks


def action_problems(action: dict, profile: str, cloud: str) -> list[str]:
    """Every pod the action leaves reading a Secret nothing mints, per instance it dials."""
    found = []
    for name, values in apply_actions([action]).items():
        service, instance = instance_of(name)
        after = render_with(service, instance, profile, cloud, values)
        found += orphaned(unchanged(service, instance, profile, cloud), after)
    return found


def restore_problems(pause: dict, resume: dict, profile: str, cloud: str) -> list[str]:
    """How the render after ``pause`` then ``resume`` differs from the render before both."""
    found = []
    for name, values in apply_actions([pause, resume]).items():
        service, instance = instance_of(name)
        after = render_with(service, instance, profile, cloud, values)
        found += render_differences(unchanged(service, instance, profile, cloud), after)
    return found


def action(name: str, *changes: dict) -> dict:
    """An action document as dfe-deploy ships them, for a case written here."""
    return {"name": name, "changes": list(changes)}


# ---------------------------------------------------------------- the shipped actions


@pytest.fixture(scope="module")
def shipped() -> dict[str, dict]:
    """The dfe-deploy actions by name: DFE_DEPLOY_ROOT, else ../dfe-deploy, else a skip."""
    given = os.environ.get("DFE_DEPLOY_ROOT")
    root = Path(given).resolve() if given else REPO_ROOT.parent / "dfe-deploy"
    directory = root / "governance" / "actions"
    if not directory.is_dir():
        if given:
            pytest.fail(f"DFE_DEPLOY_ROOT={given} holds no governance/actions")
        pytest.skip("no dfe-deploy checkout beside this repo; set DFE_DEPLOY_ROOT")
    files = sorted(directory.glob("*.yaml"))
    assert files, f"{directory} holds no action"
    return {p.stem: yaml.safe_load(p.read_text(encoding="utf-8")) for p in files}


@pytest.mark.parametrize(("profile", "cloud"), CELLS)
def test_no_shipped_action_leaves_a_pod_waiting_on_a_secret(
    shipped: dict[str, dict], profile: str, cloud: str
) -> None:
    found = {name: action_problems(doc, profile, cloud) for name, doc in sorted(shipped.items())}
    assert {name: problems for name, problems in found.items() if problems} == {}


@pytest.mark.parametrize(("profile", "cloud"), CELLS)
def test_hunts_resume_renders_what_hunts_pause_started_from(
    shipped: dict[str, dict], profile: str, cloud: str
) -> None:
    assert {"hunts-pause", "hunts-resume"} <= set(shipped), sorted(shipped)
    assert restore_problems(shipped["hunts-pause"], shipped["hunts-resume"], profile, cloud) == []


# ------------------------------------------------------------- written here, no checkout


def test_a_pause_that_leaves_the_secret_group_on_renders_clean() -> None:
    """The runner off, the engine still reading its password: dfe-extras keeps minting it."""
    pause = action("hunts-pause", PAUSE_THE_RUNNER)
    assert action_problems(pause, *SLIM) == []


def test_a_pause_that_turns_the_secret_group_off_too_renders_clean() -> None:
    pause = action("hunts-pause", PAUSE_THE_RUNNER, PAUSE_THE_SECRET)
    assert action_problems(pause, *SLIM) == []


def test_a_dial_that_touches_no_secret_renders_clean() -> None:
    throttle = action(
        "hunts-throttle",
        {**PAUSE_THE_RUNNER, "path": "huntRunner.cap", "value": {"$param": "cap"}},
    )
    throttle["params"] = {"cap": {"type": "int", "default": 4}}
    assert action_problems(throttle, *SLIM) == []


def test_a_resume_that_leaves_the_secret_group_off_is_caught() -> None:
    """The runner comes back reading a password the engine no longer adopts."""
    pause = action("hunts-pause", PAUSE_THE_RUNNER, PAUSE_THE_SECRET)
    resume = action("hunts-resume", RESUME_THE_RUNNER)
    problems = restore_problems(pause, resume, *SLIM)
    assert any("DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD" in found for found in problems), problems


def test_a_resume_that_turns_the_secret_group_on_too_restores_the_render() -> None:
    pause = action("hunts-pause", PAUSE_THE_RUNNER, PAUSE_THE_SECRET)
    resume = action("hunts-resume", RESUME_THE_RUNNER, RESUME_THE_SECRET)
    assert restore_problems(pause, resume, *SLIM) == []
