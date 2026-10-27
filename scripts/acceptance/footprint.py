#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         acceptance/footprint.py
#  Purpose:      Snapshot what an acceptance run may change, and diff it after
#                the run, so a run that leaves anything behind fails by name.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""footprint -- what an acceptance run leaves behind, measured rather than assumed.

The source and edge suites change a live deployment: they open the receiver's
lumberjack listener, create sources and their apps, and run a probe in a
namespace of their own. Each undoes that in its own teardown, and a teardown
that stops early leaves an open listener or a running pair. This module reads
the same state before and after the run and turns any difference into a named
row, so the report cannot be green over a deployment that was left changed.

What is read:

- the receiver's ConfigMaps in the app namespace (a hash of each one's data)
- every Deployment in the app namespace and its replicas, except one an HPA
  (KEDA included) scales, whose replica count moves on its own
- the sources the engine lists
- the receiver overlay values the harness switches (the lumberjack listener)
- namespaces the harness labels as its own that are not already terminating

``--keep`` is the one documented result: the run's own source stays, with the
apps and ConfigMaps named after it and the receiver routing that follows it.
Everything else must match the before snapshot.

    before = capture(kube, namespace, engine)
    ... the run ...
    rows = settle(before, lambda: capture(kube, namespace, engine), keep_name="")
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from acceptance.onboarding import wizard
from acceptance.source import beats

# The receiver instance whose overlay carries the lumberjack switches.
RECEIVER_ROUTE = "/apps/dfe-receiver/default/config"

# The label every namespace the harness creates carries.
HARNESS_LABEL = "app.kubernetes.io/managed-by=dfe-acceptance"

# Seconds a deployment may take to settle back to its before state, and the poll between reads.
SETTLE_DEADLINE = 180.0
SETTLE_INTERVAL = 5.0

# The row every run ends on when nothing differs.
CLEAN_ROW = "no-trace"

# What a part of the footprint can be asked to cover.
PARTS = ("configmaps", "replicas", "sources", "overlay", "namespaces")


class FootprintError(RuntimeError):
    """The state could not be read, so the run cannot be shown to have left nothing."""


@dataclass(frozen=True)
class Footprint:
    """One reading of everything a run may change."""

    configmaps: Mapping[str, str] = field(default_factory=dict)
    # Deployment -> spec.replicas, None for a deployment an HPA scales.
    replicas: Mapping[str, int | None] = field(default_factory=dict)
    sources: tuple[str, ...] = ()
    overlay: Mapping[str, object] = field(default_factory=dict)
    namespaces: tuple[str, ...] = ()


def kubectl_json(kube: list[str], *argv: str) -> dict:
    """kubectl ... -o json, parsed.

    Raises:
        FootprintError: kubectl failed or printed something that is not JSON.
    """
    done = subprocess.run(
        [*kube, *argv, "-o", "json"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if done.returncode != 0:
        first = (done.stderr or "").strip().splitlines()
        raise FootprintError(f"kubectl {' '.join(argv)} failed: {first[0] if first else done.returncode}")
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise FootprintError(f"kubectl {' '.join(argv)} printed unparseable output: {exc}") from exc


def _items(document: dict) -> list[dict]:
    return [item for item in document.get("items") or [] if isinstance(item, dict)]


def _name(item: dict) -> str:
    return str((item.get("metadata") or {}).get("name") or "")


def configmap_hashes(document: dict, prefix: str) -> dict[str, str]:
    """name -> hash of the data, for each ConfigMap whose name starts with *prefix*."""
    found = {}
    for item in _items(document):
        name = _name(item)
        if not name.startswith(prefix):
            continue
        body = json.dumps(
            {"data": item.get("data") or {}, "binaryData": item.get("binaryData") or {}}, sort_keys=True
        )
        found[name] = hashlib.sha256(body.encode()).hexdigest()
    return found


def deployment_replicas(deployments: dict, autoscalers: dict) -> dict[str, int | None]:
    """name -> replicas, None where an HPA targets the Deployment."""
    scaled = {
        str((item.get("spec") or {}).get("scaleTargetRef", {}).get("name") or "")
        for item in _items(autoscalers)
        if (item.get("spec") or {}).get("scaleTargetRef", {}).get("kind") == "Deployment"
    }
    return {
        _name(item): None if _name(item) in scaled else (item.get("spec") or {}).get("replicas")
        for item in _items(deployments)
    }


def live_namespaces(document: dict) -> tuple[str, ...]:
    """The harness namespaces that are not already on their way out."""
    return tuple(sorted(
        _name(item) for item in _items(document)
        if (item.get("status") or {}).get("phase") != "Terminating"
    ))


def source_names(reply: object) -> tuple[str, ...]:
    """The names a ``GET /sources`` reply lists."""
    body = getattr(reply, "body", None)
    items = body.get("items", []) if isinstance(body, dict) else []
    return tuple(sorted(str(item.get("name")) for item in items if isinstance(item, dict) and item.get("name")))


def capture(kube: list[str], namespace: str, engine, *, include: tuple[str, ...] = PARTS,
            receiver: str = "dfe-receiver") -> Footprint:
    """Read the parts of the footprint named in *include*.

    Args:
        kube: kubectl and the flags aiming it at the cluster. Empty reads only what the engine reports.
        namespace: The app namespace.
        engine: The engine client, with ``call(method, path)``.
        include: The parts to read, a subset of PARTS.
        receiver: The receiver's name, which its ConfigMaps start with.

    Returns:
        The reading. A part not asked for, or needing kubectl when none was given, is empty.

    Raises:
        FootprintError: A read failed.
    """
    want = set(include)
    configmaps: dict[str, str] = {}
    replicas: dict[str, int | None] = {}
    sources: tuple[str, ...] = ()
    overlay: dict[str, object] = {}
    namespaces: tuple[str, ...] = ()
    if kube and "configmaps" in want:
        configmaps = configmap_hashes(kubectl_json(kube, "-n", namespace, "get", "configmap"), receiver)
    if kube and "replicas" in want:
        replicas = deployment_replicas(
            kubectl_json(kube, "-n", namespace, "get", "deployment"),
            kubectl_json(kube, "-n", namespace, "get", "hpa"),
        )
    if kube and "namespaces" in want:
        namespaces = live_namespaces(kubectl_json(kube, "get", "namespace", "-l", HARNESS_LABEL))
    try:
        if "sources" in want:
            listing = engine.call("GET", "/sources")
            if listing.status != 200:
                raise FootprintError(f"GET /sources answered {listing.status}")
            sources = source_names(listing)
        if "overlay" in want:
            view = engine.call("GET", RECEIVER_ROUTE)
            overlay = beats.held(view.body, beats.LUMBERJACK_SWITCHES) if view.status == 200 else {}
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, FootprintError):
            raise
        raise FootprintError(f"the engine did not answer: {exc}") from exc
    return Footprint(configmaps, replicas, sources, overlay, namespaces)


def _added_removed(kind: str, before: set[str], after: set[str], allowed: Callable[[str], bool]) -> list[str]:
    found = [f"{kind} {name} is new" for name in sorted(after - before) if not allowed(name)]
    found += [f"{kind} {name} is gone" for name in sorted(before - after)]
    return found


def diff(before: Footprint, after: Footprint, *, keep_name: str = "") -> dict[str, list[str]]:
    """What changed between two readings, by part, leaving out the documented ``--keep`` result.

    Args:
        before: The reading taken before the run.
        after: The reading taken after its teardown.
        keep_name: The source the run left deployed on ``--keep``, empty when it removes its own.

    Returns:
        Part -> one line per difference. A part that matches is absent.
    """

    def owned(name: str) -> bool:
        return bool(keep_name) and keep_name in name

    found: dict[str, list[str]] = {}
    lines = _added_removed("source", set(before.sources), set(after.sources), lambda n: n == keep_name)
    if lines:
        found["sources"] = lines
    lines = _added_removed("deployment", set(before.replicas), set(after.replicas), owned)
    lines += [
        f"deployment {name} replicas {before.replicas[name]} -> {after.replicas[name]}"
        for name in sorted(set(before.replicas) & set(after.replicas))
        if before.replicas[name] != after.replicas[name]
    ]
    if lines:
        found["deployments"] = lines
    lines = _added_removed("configmap", set(before.configmaps), set(after.configmaps), owned)
    # The receiver's routing follows the kept source, so its data is the documented result there.
    if not keep_name:
        lines += [
            f"configmap {name} data changed"
            for name in sorted(set(before.configmaps) & set(after.configmaps))
            if before.configmaps[name] != after.configmaps[name]
        ]
    if lines:
        found["configmaps"] = lines
    if dict(before.overlay) != dict(after.overlay):
        found["receiver-overlay"] = [
            f"receiver overlay {dict(before.overlay) or 'unset'} -> {dict(after.overlay) or 'unset'}"
        ]
    lines = _added_removed("namespace", set(before.namespaces), set(after.namespaces), lambda _n: False)
    if lines:
        found["namespaces"] = lines
    return found


def settle(before: Footprint, read: Callable[[], Footprint], *, keep_name: str = "",
           deadline: float | None = None, interval: float | None = None,
           sleep: Callable[[float], None] = time.sleep,
           clock: Callable[[], float] = time.monotonic) -> list[wizard.StepResult]:
    """Rows for the run's footprint, waiting for the deployment to settle back.

    A removed source's apps leave on the controller's schedule, so the diff is
    read again until it is empty or *deadline* passes, and what is left then is
    the finding.

    Args:
        before: The reading taken before the run.
        read: Takes a reading now.
        keep_name: The source left deployed on ``--keep``, else empty.
        deadline: Seconds to wait for a diff to clear, SETTLE_DEADLINE when unset.
        interval: Seconds between readings, SETTLE_INTERVAL when unset.
        sleep: Pauses between readings.
        clock: Monotonic seconds.

    Returns:
        ``no-trace`` done when nothing differs, else one failed row per differing part,
        named ``no-trace-<part>``. A reading that fails is one failed ``no-trace`` row.
    """
    deadline = SETTLE_DEADLINE if deadline is None else deadline
    interval = SETTLE_INTERVAL if interval is None else interval
    until = clock() + deadline
    while True:
        try:
            found = diff(before, read(), keep_name=keep_name)
        except FootprintError as exc:
            return [wizard.StepResult(CLEAN_ROW, "failed", f"could not read what the run left behind: {exc}")]
        if not found or clock() >= until:
            break
        sleep(interval)
    if not found:
        kept = f", apart from the kept source {keep_name}" if keep_name else ""
        return [wizard.StepResult(CLEAN_ROW, "done", f"the deployment matches its state before the run{kept}")]
    return [
        wizard.StepResult(f"{CLEAN_ROW}-{part}", "failed", "; ".join(lines))
        for part, lines in sorted(found.items())
    ]


def baseline(read: Callable[[], Footprint]) -> tuple[Footprint | None, list[wizard.StepResult]]:
    """The reading taken before a run, or a failed row saying why there is none.

    Returns:
        (the reading, no rows) or (None, one failed ``no-trace`` row).
    """
    try:
        return read(), []
    except FootprintError as exc:
        row = wizard.StepResult(CLEAN_ROW, "failed", f"could not read the deployment before the run: {exc}")
        return None, [row]
