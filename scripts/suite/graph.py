#  Project:      dfe-infra
#  File:         scripts/suite/graph.py
#  Purpose:      Read the suite graph, whole or one producer's slice, straight
#                from suite_graph -- the graph and its reader live here.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One producer's slice of the suite graph, and the whole graph in one read.

``suite.yaml`` and ``scripts/suite_graph.py`` are in this repo, so this imports
the reader rather than shelling out to ``dfe-stack suite`` and parsing its JSON.
The validation is the same one ``dfe-stack suite`` applies: a graph that does
not hold together is refused here, before anything walks it, rather than
producing a walk over a broken picture.
"""

from __future__ import annotations

from pathlib import Path

import suite_graph

from suite.proc import FleetError


def load_raw(path: Path | None = None) -> dict:
    """The whole validated graph.

    Args:
        path: A suite.yaml to read instead of this repo's own.

    Raises:
        FleetError: If the file cannot be read, or the graph does not hold
            together -- every problem named, not just the first.
    """
    source = Path(path).expanduser() if path else suite_graph.SUITE_FILE
    try:
        graph = suite_graph.load(source)
    except (OSError, ValueError) as exc:
        raise FleetError(f"cannot read {source}: {exc}") from exc
    problems = suite_graph.validate(graph)
    if problems:
        raise FleetError(f"{source} does not hold together: " + "; ".join(problems))
    return graph


def load_producer(producer: str, *, path: Path | None = None) -> dict:
    """The graph slice for one producer: its nodes, edge kinds and out-edges.

    Args:
        producer: The node name, e.g. ``scalo-rs``.
        path: A suite.yaml to read instead of this repo's own.

    Returns:
        The slice: ``schema``, ``verified``, ``edge_kinds``, ``nodes`` and
        ``edges``.

    Raises:
        FleetError: If the graph cannot be read, or names no such producer.
    """
    graph = load_raw(path)
    try:
        return suite_graph.slice_graph(graph, producer=producer)
    except KeyError as exc:
        raise FleetError(str(exc.args[0])) from exc


def load_graph(*, path: Path | None = None) -> dict:
    """The whole graph in one read: every node, edge and lane.

    A slice per member costs one read each, and a plan across a language group
    reads most of the graph anyway, so it is asked for once.

    Raises:
        FleetError: If the graph cannot be read or does not hold together.
    """
    return load_raw(path)


def _edges(payload: dict, field: str, node: str) -> list[dict]:
    """The edges whose ``field`` names ``node``, in the graph's own order."""
    edges = payload.get("edges")
    if not isinstance(edges, list):
        return []
    return [edge for edge in edges if isinstance(edge, dict) and edge.get(field) == node]


def out_edges(slice_: dict, producer: str) -> list[dict]:
    """The edges leaving ``producer``, in the order the graph records them."""
    return _edges(slice_, "from", producer)


def in_edges(graph: dict, consumer: str) -> list[dict]:
    """The edges reaching ``consumer``, in the order the graph records them."""
    return _edges(graph, "to", consumer)
