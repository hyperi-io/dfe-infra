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

A Helm values file is outside the subset: it carries lists and flow
collections. ``walk_keys`` reads one by indentation instead, and ``keys_under``
and ``value_at`` answer the two questions the ops scripts ask of it.

    door = value_at(text, ("exposure", "groups", "ingest", "door"))
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence

REPEATED = "duplicate key"

# A block-style mapping key line: indent, an unquoted key, and the rest of the line.
_VALUES_KEY = re.compile(r"^(\s*)([A-Za-z_][\w.-]*)\s*:(?:\s+(.*?))?\s*$")
# A key inside a flow mapping, such as `{door: gateway, allow: []}`.
_FLOW_KEY = re.compile(r"[{,]\s*([A-Za-z_][\w-]*)\s*:")


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
            _place(parent, key.strip(), _scalar(value), line_num, source, allow_duplicate_keys)
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


def walk_keys(text: str) -> Iterator[tuple[tuple[str, ...], str]]:
    """Walk a values file's block-style mapping keys by indentation.

    A line that is not an unquoted ``key:`` -- a list item, a comment, a quoted
    key -- is skipped, so a key under a list item reads as a child of the
    mapping above the list. The value is what follows the colon on the same
    line, read by `parse`'s scalar rule.

    Args:
        text: The values file's body.

    Yields:
        (the key's path from the root, its inline value), in file order. The
        value is empty for a key whose value is a nested block.
    """
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        match = _VALUES_KEY.match(line)
        if not match:
            continue
        indent = len(match[1])
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, match[2]))
        yield tuple(key for _, key in stack), _scalar(match[3] or "")


def keys_under(text: str, path: Sequence[str]) -> list[str]:
    """List the keys a values file sets directly under `path`, in file order.

    Reads block-style children, and a flow mapping written on the `path` line
    itself. Any other spelling, such as a flow mapping on an ancestor's line,
    reads as unset.

    Args:
        text: The values file's body.
        path: The mapping's keys from the root.

    Returns:
        Each key set under `path`; empty when it is absent, null or ``{}``.
    """
    target = tuple(path)
    keys: list[str] = []
    for key_path, value in walk_keys(text):
        if key_path[:-1] == target:
            keys.append(key_path[-1])
        elif key_path == target:
            keys += _FLOW_KEY.findall(value)
    return keys


def value_at(text: str, path: Sequence[str]) -> str | None:
    """Read the scalar a values file sets at `path`.

    Args:
        text: The values file's body.
        path: The key's path from the root.

    Returns:
        The first non-empty inline value set at `path`, or None when no line
        sets one.
    """
    target = tuple(path)
    return next((value for key_path, value in walk_keys(text) if key_path == target and value), None)


def _scalar(raw: str) -> str:
    """One inline value: inside the quotes when quoted, else up to a `` #`` comment."""
    value = raw.strip()
    if value and value[0] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end != -1 else value[1:]
    return value.split(" #", 1)[0].strip()


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
