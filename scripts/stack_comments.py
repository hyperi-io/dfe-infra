#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         stack_comments.py
#  Purpose:      Drop the comments a versions.yaml stack block copied from its
#                predecessor, so each pin's reason is written once.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Keep a pin's reason in the block that introduced or last moved it.

`dfe-stack cut` clones the previous block as the shape, comments included, so
every held pin repeated its reason in every later block and versions.yaml grew
past the 1 MB the GitHub contents API serves. A comment stays in a block only
when the line under it is new or changed against the predecessor, or when it is
a machine-read annotation (`# renovate:`, `# image:`), which the image map and
Renovate read above the pin in every block, a section note above a map, or a
commented-out pin (`# key: "v"`), which is uncommented when the app ships.

    python3 scripts/stack_comments.py            # report what would shrink
    python3 scripts/stack_comments.py --write    # rewrite versions.yaml
"""

import argparse
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import yaml_subset  # noqa: E402
from stack_images import block_span  # noqa: E402

VERSIONS_FILE = SCRIPTS.parent / "versions.yaml"

_ANNOTATION = re.compile(
    r'^\s*#\s*(?:renovate:|image:|[A-Za-z0-9_.-]+:\s*"[^"]*"\s*(?:#.*)?$)'
)
_OPENER = re.compile(r"^ *[A-Za-z0-9_.-]+:\s*(?:#.*)?$")
_ENTRY = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_.-]+):\s*(?P<rest>.*)$")


def _value(rest: str) -> str:
    """The value of `key: "value"   # note`, without the note."""
    quoted = re.match(r'^\s*"[^"]*"', rest)
    return quoted[0].strip() if quoted else rest.split(" #", 1)[0].strip()


def _paths(block: list[str], version: str) -> list[tuple[str, str] | None]:
    """Per line: (block-relative path, value with the block's own version masked), or None."""
    found: list[tuple[str, str] | None] = [None]
    path: list[tuple[int, str]] = []
    for line in block[1:]:
        text = line.rstrip("\n")
        entry = None if line.lstrip().startswith("#") else _ENTRY.match(text)
        if entry is None:
            found.append(None)
            continue
        indent, key = len(entry["indent"]), entry["key"]
        while path and path[-1][0] >= indent:
            path.pop()
        full = ".".join([k for _, k in path] + [key])
        found.append((full, _value(entry["rest"]).replace(version, "@")))
        if _OPENER.match(text):
            path.append((indent, key))
    return found


def strip_held_comments(
    block: list[str], version: str, previous: list[str], previous_version: str
) -> list[str]:
    """`block` without the comments on pins it holds unchanged from `previous`.

    Args:
        block: The stack block's lines, header first.
        version: The block's own version, which a held line may name.
        previous: The predecessor block's lines, header first.
        previous_version: The predecessor's version.

    Returns:
        The block's lines with each held pin's trailing comment and the comment
        lines above it removed. Annotation lines, section notes above a map, comments above a line the
        predecessor lacks or spells differently, and a trailing comment with no
        line under it all stay.
    """
    before = {p[0]: p[1] for p in _paths(previous, previous_version) if p}
    now = _paths(block, version)
    out: list[str] = [block[0]]
    pending: list[str] = []

    def flush(changed: bool) -> None:
        out.extend(c for c in pending if changed or _ANNOTATION.match(c))
        pending.clear()

    for line, here in list(zip(block, now, strict=True))[1:]:
        if line.lstrip().startswith("#"):
            pending.append(line)
        elif here is None:
            flush(changed=False)
            if line.strip() or out[-1].strip():
                out.append(line)
        else:
            path, value = here
            changed = before.get(path) != value
            section = bool(_OPENER.match(line.rstrip("\n")))
            flush(changed or section)
            entry = _ENTRY.match(line.rstrip("\n"))
            if not changed and entry and not _OPENER.match(line.rstrip("\n")):
                held = _value(entry["rest"])
                if held != entry["rest"]:
                    line = f"{entry['indent']}{entry['key']}: {held}\n"
            out.append(line)
    flush(changed=True)
    return out


def _predecessor(root: dict, names: list[str], name: str) -> str | None:
    """The stack a block was cut from: its first `stack.previous`, else file order."""
    declared = str(root["stacks"][name].get("stack", {}).get("previous", ""))
    first = declared.split(",")[0].strip()
    if first in names:
        return first
    at = names.index(name)
    return names[at - 1] if at else None


def strip_file(text: str) -> str:
    """versions.yaml with each block's held-pin comments removed.

    The first block, which has no predecessor, keeps every comment.
    """
    lines = text.splitlines(keepends=True)
    root = yaml_subset.parse(text)
    names = list(root["stacks"])
    spans = {n: block_span(lines, n) for n in names}
    original = {n: lines[s:e] for n, (s, e) in spans.items()}
    out = list(lines)
    for name in sorted(names, key=lambda n: -spans[n][0]):
        prev = _predecessor(root, names, name)
        if prev is not None:
            s, e = spans[name]
            out[s:e] = strip_held_comments(original[name], name, original[prev], prev)
    return "".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--write", action="store_true", help="rewrite versions.yaml")
    args = parser.parse_args()
    text = VERSIONS_FILE.read_text(encoding="utf-8")
    new = strip_file(text)
    if yaml_subset.parse(text) != yaml_subset.parse(new):
        raise SystemExit("error: stripping comments changed a parsed value")
    print(f"versions.yaml {len(text.encode())} -> {len(new.encode())} bytes")
    if args.write:
        VERSIONS_FILE.write_text(new, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
