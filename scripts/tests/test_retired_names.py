#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_retired_names.py
#  Purpose:      Fail when a retired product name comes back into the tree
#                outside the files that record history.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A retired name stays out of the tree except where history is the point.

FerretDB was HyperDX's document store until DocumentDB replaced it on Kubernetes.
Its name survives in the stacks that shipped it (constraints/ and the older stacks
in versions.yaml), in the upgrade note that explains the swap, and in a few
fixtures that parse those historical tags. Anywhere else it is a leftover that
sends a reader to a component that no longer deploys.

    python3 -m pytest scripts/tests/test_retired_names.py -q

Needs `git` on PATH.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

RETIRED = {"ferretdb": re.compile(r"ferretdb", re.IGNORECASE)}

# History recorded as history: path prefix or exact path -> why the name stays.
HISTORY = {
    "constraints/": "one file per cut stack, kept as shipped",
    "docs/deployment/upgrades.md": "the upgrade note explaining the swap to DocumentDB",
    "scripts/check_versions_drift.py": "a message naming the stacks that predate DocumentDB",
    "scripts/tests/test_version_range.py": "parses the tag shape the older stacks carry",
    "scripts/tests/test_retired_names.py": "names what it looks for",
}
# Another builder's tree, not scanned here.
SKIPPED = ("docs-site/",)
BINARY = (".tgz", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff", ".woff2", ".zip", ".gz")
STACK_HEADER = re.compile(r"^  ([^\s#:][^:]*):\s*(?:#.*)?$")


def tracked() -> list[str]:
    done = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    return [p for p in done.stdout.split("\0") if p]


def versions_history_end(lines: list[str]) -> int:
    """The 1-based line where the first stack that no longer carries the retired key begins.

    Every stack before it is history. Derived from the file, so cutting a newer stack
    needs no edit here.
    """
    data = yaml.safe_load("\n".join(lines))
    carrying = [name for name, stack in (data.get("stacks") or {}).items() if "ferretdb" in (stack.get("services") or {})]
    if not carrying:
        return 0
    last = carrying[-1]
    headers = [(index + 1, m.group(1)) for index, line in enumerate(lines) if (m := STACK_HEADER.match(line))]
    names = [name for _, name in headers]
    following = headers[names.index(last) + 1][0]
    return following


def hits() -> list[str]:
    found = []
    for path in tracked():
        if path.endswith(BINARY) or path.startswith(SKIPPED):
            continue
        if any(path == key or (key.endswith("/") and path.startswith(key)) for key in HISTORY):
            continue
        file = REPO_ROOT / path
        if not file.is_file():
            continue
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        limit = versions_history_end(lines) if path == "versions.yaml" else 0
        for number, line in enumerate(lines, 1):
            if number < limit:
                continue
            for name, pattern in RETIRED.items():
                if pattern.search(line):
                    found.append(f"{path}:{number}: {name}: {line.strip()[:100]}")
    return found


def test_a_retired_name_stays_inside_the_files_that_record_history() -> None:
    assert hits() == [], "\n".join(hits())


@pytest.mark.parametrize("path", sorted(HISTORY))
def test_every_history_exemption_still_holds_the_name(path: str) -> None:
    """An exemption nothing needs any more is a hole the next leftover walks through."""
    target = REPO_ROOT / path
    files = [p for p in target.rglob("*") if p.is_file()] if target.is_dir() else [target]
    assert any(RETIRED["ferretdb"].search(f.read_text(encoding="utf-8", errors="replace")) for f in files), path


def test_versions_yaml_history_ends_where_the_key_does() -> None:
    lines = (REPO_ROOT / "versions.yaml").read_text(encoding="utf-8").splitlines()
    end = versions_history_end(lines)
    assert end > 0
    assert any(RETIRED["ferretdb"].search(line) for line in lines[: end - 1]), "no history left to exempt"
    assert not any(RETIRED["ferretdb"].search(line) for line in lines[end - 1 :])
