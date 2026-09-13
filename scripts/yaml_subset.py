#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/yaml_subset.py
#  Purpose:      Read the restricted YAML subset the SSoT files are written in --
#                nested maps and scalar values, never lists -- with the stdlib
#                alone, so every ops script parses config the one way.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""yaml_subset -- the dependency-free reader for dfe-infra's YAML SSoT files.

dfe-infra ships no third-party Python, so every file an ops script reads is
written in a subset one small parser handles: ``key: value`` scalars, ``key:``
nesting by indentation, ``#`` comments, and nothing else. A list is expressed as
a map keyed by name, or as a comma-separated scalar ``split_list`` splits.

    from yaml_subset import parse, split_list

    tree = parse(path.read_text(encoding="utf-8"), source=str(path))

Two values a line can carry: quoted, where only what is inside the quotes counts
(so ``key: "" # note`` is empty), and unquoted, where a trailing `` # comment``
is stripped.

Duplicate keys inside one map RAISE rather than silently overwrite. These files
are sizing tables, and a second ``value:`` quietly winning is how a wrong number
ships -- dfe-core carried a duplicated ``max_replica_memory_gb`` for exactly that
reason. Callers that must tolerate one pass ``allow_duplicate_keys=True``.
"""

from __future__ import annotations

from collections.abc import Iterable

REPEATED = "duplicate key"


class YamlSubsetError(ValueError):
    """A line the subset cannot represent, or a key repeated inside one map."""


def parse(
    text: str,
    *,
    source: str = "<yaml>",
    allow_duplicate_keys: bool = False,
) -> dict[str, object]:
    """Parse the subset -- nested maps and scalar values -- into plain dicts.

    Args:
        text: The file body.
        source: Path or label used in error messages.
        allow_duplicate_keys: Let a repeated key overwrite its earlier value.

    Returns:
        The parsed tree; every leaf is a str, every branch a dict.

    Raises:
        YamlSubsetError: A line outside the subset (a list, an inline
            collection) or a duplicate key.
    """
    root: dict[str, object] = {}
    stack: list[tuple[dict[str, object], int]] = [(root, -1)]
    for line_num, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        while len(stack) > 1 and stack[-1][1] >= indent:
            stack.pop()
        parent = stack[-1][0]
        if ": " in line:
            key, value = line.split(": ", 1)
            value = value.strip()
            if value and value[0] in ("'", '"'):
                quote = value[0]
                end = value.find(quote, 1)
                value = value[1:end] if end != -1 else value[1:]
            else:
                value = value.split(" #", 1)[0].strip()
            _place(parent, key.strip(), value, line_num, source, allow_duplicate_keys)
        elif line.endswith(":"):
            child: dict[str, object] = {}
            _place(parent, line[:-1].strip(), child, line_num, source, allow_duplicate_keys)
            stack.append((child, indent))
        else:
            raise YamlSubsetError(f"{source} line {line_num}: cannot parse {line!r}")
    return root


def at(tree: object, path: Iterable[str]) -> object:
    """Walk a parsed tree along `path`, answering None rather than raising.

    Every SSoT reader in this repo (resolve_sizing.py's `_at`,
    render_dial.py's `_scalar` and `_node`) descended a parsed tree the same
    way -- stop and answer None the moment a step is not a map -- so this is
    the one copy of that walk; each caller keeps its own wrapper for what it
    does with the leaf (strip a scalar, default an absent branch to `{}`).

    Args:
        tree: A tree `parse` returned, or any nested-dict structure shaped
            like one.
        path: The keys to descend, in order.

    Returns:
        The value at `path`, or None the moment a step is missing or a
        branch is not a map.
    """
    node = tree
    for step in path:
        if not isinstance(node, dict):
            return None
        node = node.get(step)
    return node


def split_list(value: object) -> tuple[str, ...]:
    """Split a comma-separated scalar into its items.

    Args:
        value: A scalar from a parsed tree; anything else answers empty.

    Returns:
        The non-empty items, stripped, in file order.
    """
    if not isinstance(value, str):
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _place(
    parent: dict[str, object],
    key: str,
    value: object,
    line_num: int,
    source: str,
    allow_duplicate_keys: bool,
) -> None:
    """Set one key on its map, refusing a repeat unless the caller allows it."""
    if key in parent and not allow_duplicate_keys:
        raise YamlSubsetError(f"{source} line {line_num}: {REPEATED} {key!r}")
    parent[key] = value
