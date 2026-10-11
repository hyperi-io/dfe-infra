#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/cycle_backend.py
#  Purpose:      Apply a managed-backend overlay to a cloud-cycle dial, so one
#                run picks its brokers and datastore without editing the dial.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""cycle_backend -- which backend one guarded cloud cycle runs against.

    python3 scripts/cycle_backend.py --list
    python3 scripts/cycle_backend.py --dial "$RUNNER_TEMP/deployment.yaml" --backend msk

`default` leaves the dial as the e2e-runner environment wrote it. Every other
backend is a committed overlay, `scripts/cycle-backends/<backend>.yaml`, written
in the dial's own YAML subset: a key the overlay sets replaces the dial's, and a
map merges into the map below it. The overlay commits the SHAPE only. A value
written `env:<NAME>` is read from the environment, because an account-specific
value -- a host, a cluster id -- is an e2e-runner secret and this repository's
logs are public; an unset one refuses the run by name before anything renders.

The dial is rewritten in place at mode 0600, since it can now hold those values.
What is printed is the dotted path of every key that changed, never a value.
"""

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import private_file
from yaml_subset import YamlSubsetError
from yaml_subset import parse as parse_yaml_subset

OVERLAYS = Path(__file__).resolve().parent / "cycle-backends"
DEFAULT = "default"
ENV_PREFIX = "env:"

type Tree = dict[str, object]


class BackendError(ValueError):
    """A backend, overlay or dial this script refuses to combine."""


def available() -> tuple[str, ...]:
    """Every backend a run may name: `default`, then each committed overlay."""
    return (DEFAULT, *sorted(path.stem for path in OVERLAYS.glob("*.yaml")))


def load_overlay(backend: str) -> Tree:
    """The overlay tree for `backend`, empty for `default`.

    Raises:
        BackendError: The backend has no overlay, or the overlay is outside the subset.
    """
    if backend == DEFAULT:
        return {}
    if backend not in available():
        raise BackendError(
            f"no backend {backend!r} -- choose one of {', '.join(available())}"
        )
    path = OVERLAYS / f"{backend}.yaml"
    try:
        return parse_yaml_subset(path.read_text(encoding="utf-8"), source=str(path))
    except YamlSubsetError as error:
        raise BackendError(str(error)) from error


def resolve(tree: Tree, env: Mapping[str, str]) -> Tree:
    """`tree` with every `env:<NAME>` value read from `env`.

    Raises:
        BackendError: Naming every referenced variable that is unset or empty.
    """
    missing: list[str] = []

    def walk(node: Tree, path: tuple[str, ...]) -> Tree:
        out: Tree = {}
        for key, value in node.items():
            here = (*path, key)
            if isinstance(value, dict):
                out[key] = walk(value, here)
            elif isinstance(value, str) and value.startswith(ENV_PREFIX):
                name = value.removeprefix(ENV_PREFIX)
                if not env.get(name):
                    missing.append(f"{name} (for {'.'.join(here)})")
                out[key] = env.get(name, "")
            else:
                out[key] = value
        return out

    resolved = walk(tree, ())
    if missing:
        raise BackendError(
            "the e2e-runner environment does not set " + ", ".join(missing)
        )
    return resolved


def _is_empty_map(value: object) -> bool:
    """The subset writes an empty map as `{}` or leaves it blank."""
    return value == {} or (isinstance(value, str) and value.strip() in ("", "{}"))


def merge(dial: Tree, overlay: Tree, at: tuple[str, ...] = ()) -> list[str]:
    """Merge `overlay` into `dial` in place; return the dotted path of each key changed.

    Raises:
        BackendError: The overlay puts a scalar over a map, or a map over a scalar
            that is not an empty map -- either would drop part of the dial silently.
    """
    changed: list[str] = []
    for key, value in overlay.items():
        here = (*at, key)
        label = ".".join(here)
        current = dial.get(key)
        if isinstance(value, dict):
            if isinstance(current, dict):
                changed += merge(current, value, here)
                continue
            if current is not None and not _is_empty_map(current):
                raise BackendError(f"the overlay makes {label} a map, and the dial holds a value there")
            branch: Tree = {}
            dial[key] = branch
            changed += merge(branch, value, here)
            continue
        if isinstance(current, dict):
            raise BackendError(f"the overlay makes {label} a value, and the dial holds a map there")
        if current != value:
            dial[key] = value
            changed.append(label)
    return changed


def _quoted(value: str, label: str) -> str:
    """One scalar in the quoting the subset reads back unchanged."""
    if "\n" in value:
        raise BackendError(f"{label} holds a line break, which the dial's YAML subset cannot carry")
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    raise BackendError(f"{label} holds both quote characters, which the dial's YAML subset cannot carry")


def dump(tree: Tree, at: tuple[str, ...] = (), indent: int = 0) -> str:
    """`tree` in the dial's YAML subset, every scalar quoted."""
    lines: list[str] = []
    pad = "  " * indent
    for key, value in tree.items():
        here = (*at, key)
        if isinstance(value, dict):
            if value:
                lines.append(f"{pad}{key}:")
                lines.append(dump(value, here, indent + 1).rstrip("\n"))
            else:
                lines.append(f"{pad}{key}: {{}}")
        else:
            lines.append(f"{pad}{key}: {_quoted(str(value), '.'.join(here))}")
    return "\n".join(lines) + "\n"


def apply(dial_path: Path, backend: str, env: Mapping[str, str]) -> list[str]:
    """Rewrite the dial at `dial_path` with `backend`'s overlay; return the keys changed."""
    overlay = resolve(load_overlay(backend), env)
    if not overlay:
        return []
    try:
        dial = parse_yaml_subset(dial_path.read_text(encoding="utf-8"), source=str(dial_path))
    except YamlSubsetError as error:
        raise BackendError(str(error)) from error
    changed = merge(dial, overlay)
    private_file.write_private(dial_path, dump(dial))
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="cycle_backend.py",
        description="Apply a managed-backend overlay to a cloud-cycle dial, in place.",
    )
    parser.add_argument("--dial", type=Path, help="the dial to rewrite")
    parser.add_argument("--backend", default=DEFAULT, help=f"one of {', '.join(available())}")
    parser.add_argument("--list", action="store_true", help="print every backend and stop")
    args = parser.parse_args()

    if args.list:
        print("\n".join(available()))
        return 0
    if args.dial is None:
        parser.error("--dial is required unless --list is given")
    try:
        changed = apply(args.dial, args.backend, os.environ)
    except (BackendError, OSError) as error:
        print(f"cycle_backend: {error}. Nothing was created.", file=sys.stderr)
        return 1
    if args.backend == DEFAULT:
        print("cycle_backend: default -- the dial runs as the e2e-runner environment wrote it",
              file=sys.stderr)
    else:
        print(f"cycle_backend: {args.backend} set {len(changed)} dial key(s): "
              + (", ".join(changed) or "none, the dial already matched"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
