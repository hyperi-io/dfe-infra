#  Project:      dfe-infra
#  File:         scripts/dfe_suite/graph.py
#  Purpose:      Read the suite graph, whole or one producer's slice, from dfe-infra.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The graph lives in dfe-infra, and so does its parser.

``suite.yaml`` and ``scripts/dfe-stack`` are both in dfe-infra on purpose: the
product's own dependency picture has to be readable by anyone with that
checkout, with no hyperi-ai installed and nothing to install. So nothing here
parses YAML. This asks dfe-stack for a slice, or for the whole graph, and reads
the JSON it answers with, which also means a change to the graph's file format
costs nothing on this side.
"""

from __future__ import annotations

import json
from pathlib import Path

from dfe_suite.proc import FleetError, run
from dfe_suite.repos import find_repo

DFE_STACK = Path("scripts") / "dfe-stack"


def stack_script(dfe_infra: Path | None = None) -> Path:
    """The dfe-stack script inside a dfe-infra checkout.

    Args:
        dfe_infra: The checkout, or None to resolve it the usual way.

    Raises:
        FleetError: If the checkout has no dfe-stack in it.
    """
    root = Path(dfe_infra).expanduser() if dfe_infra else find_repo("dfe-infra")
    script = root / DFE_STACK
    if not script.is_file():
        raise FleetError(f"{script} is not there -- is {root} a dfe-infra checkout?")
    return script


def _ask(args: list[str], *, dfe_infra: Path | None) -> dict:
    """Run one ``dfe-stack suite`` invocation and parse the object it answers with.

    Args:
        args: The flags after ``suite``. Empty asks for the whole graph.
        dfe_infra: The dfe-infra checkout, or None to resolve it the usual way.

    Returns:
        The parsed payload.

    Raises:
        FleetError: If dfe-stack fails, or answers with something that is not
            a JSON object.
    """
    script = stack_script(dfe_infra)
    proc = run(
        ["python3", str(script), "suite", *args],
        cwd=script.parent.parent,
        check=False,
    )
    label = " ".join(["suite", *args])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise FleetError(f"dfe-stack {label} failed ({proc.returncode}): {detail}")
    try:
        payload = json.loads(proc.stdout or "")
    except json.JSONDecodeError as exc:
        raise FleetError(
            f"dfe-stack {label} did not return JSON: {proc.stdout[:200]}"
        ) from exc
    if not isinstance(payload, dict):
        raise FleetError(
            f"dfe-stack {label} returned {type(payload).__name__}, not an object"
        )
    return payload


def load_producer(producer: str, *, dfe_infra: Path | None = None) -> dict:
    """The graph slice for one producer: its nodes, edge kinds and out-edges.

    Args:
        producer: The node name, e.g. ``scalo-rs``.
        dfe_infra: The dfe-infra checkout, or None to resolve it the usual way.

    Returns:
        The parsed slice: ``schema``, ``verified``, ``edge_kinds``, ``nodes``
        and ``edges``.

    Raises:
        FleetError: If dfe-stack fails, or answers with something that is not
            a JSON object.
    """
    return _ask(["--producer", producer], dfe_infra=dfe_infra)


def load_graph(*, dfe_infra: Path | None = None) -> dict:
    """The whole graph in one read: every node, edge and lane.

    A slice per member costs one subprocess each, and a plan across a language
    group reads most of the graph anyway, so it is asked for once.

    Args:
        dfe_infra: The dfe-infra checkout, or None to resolve it the usual way.

    Returns:
        The parsed graph: ``schema``, ``verified``, ``edge_kinds``, ``nodes``,
        ``edges`` and ``lanes``.

    Raises:
        FleetError: If dfe-stack fails, or answers with something that is not
            a JSON object.
    """
    return _ask([], dfe_infra=dfe_infra)


def _edges(payload: dict, field: str, node: str) -> list[dict]:
    """The edges whose ``field`` names ``node``, in the graph's own order."""
    edges = payload.get("edges")
    if not isinstance(edges, list):
        return []
    return [
        edge for edge in edges if isinstance(edge, dict) and edge.get(field) == node
    ]


def out_edges(slice_: dict, producer: str) -> list[dict]:
    """The edges leaving ``producer``, in the order the graph records them."""
    return _edges(slice_, "from", producer)


def in_edges(graph: dict, consumer: str) -> list[dict]:
    """The edges reaching ``consumer``, in the order the graph records them."""
    return _edges(graph, "to", consumer)
