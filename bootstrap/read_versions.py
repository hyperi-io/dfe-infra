#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         read_versions.py
#  Purpose:      Read pinned versions from versions.yaml (the SSOT)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Read version pins from versions.yaml.

Usage:
    # Get a single version:
    python3 read_versions.py bootstrap.cert-manager
    # Output: v1.17.2

    # Get all bootstrap versions as KEY=VALUE (for shell eval):
    python3 read_versions.py --section bootstrap --shell
    # Output:
    # CERT_MANAGER_VERSION="v1.17.2"
    # EXTERNAL_SECRETS_VERSION="0.17.0"
    # ARGOCD_VERSION="7.8.26"
    # VALKEY_VERSION="1.0.0"

    # Get as JSON:
    python3 read_versions.py --section operators --json
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


def load_versions(versions_file: Path) -> dict:
    """Load versions.yaml, with or without PyYAML."""
    text = versions_file.read_text()
    if yaml:
        return yaml.safe_load(text)
    return _parse_simple_yaml(text)


def _parse_simple_yaml(text: str) -> dict:
    """Indent-aware parse of the versions.yaml subset (nested maps of scalars).

    Handles the nested `stacks:` shape to arbitrary depth WITHOUT PyYAML (for a
    bare cluster image). Same reader as scripts/dfe-stack.
    """
    root: dict = {}
    stack: list = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        m = re.match(r'^([A-Za-z0-9_.-]+):\s*(?:"([^"]*)"|([^#]*?))?\s*(?:#.*)?$', raw.strip())
        if not m:
            continue
        key, quoted = m.group(1), m.group(2)
        value = quoted if quoted is not None else (m.group(3) or "").strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "" and quoted is None:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = value
    return root


def get_dotpath(data: dict, path: str) -> str:
    """Navigate a.b.c dotpath into nested dict."""
    parts = path.split(".")
    current = data
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            print(f"ERROR: path '{path}' not found in versions.yaml", file=sys.stderr)
            sys.exit(1)
        current = current[part]
    return str(current)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read versions from versions.yaml")
    parser.add_argument(
        "dotpath", nargs="?", help="Dot-separated path (e.g. bootstrap.cert-manager)"
    )
    parser.add_argument("--section", help="Output all keys in a section")
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Output as UPPER_SNAKE=value for shell eval",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--file", default=None, help="Path to versions.yaml (default: auto-detect)")
    parser.add_argument(
        "--stack",
        default=None,
        help="Stack version to read (default: the `current` pointer)",
    )
    args = parser.parse_args()

    # Find versions.yaml
    if args.file:
        versions_file = Path(args.file)
    else:
        search = Path(__file__).resolve().parent
        while search != search.parent:
            candidate = search / "versions.yaml"
            if candidate.exists():
                versions_file = candidate
                break
            search = search.parent
        else:
            print("ERROR: versions.yaml not found", file=sys.stderr)
            sys.exit(1)

    data = load_versions(versions_file)

    # versions.yaml is NESTED (stacks: -> <version> -> sections). Descend into
    # the requested stack (default: the `current` pointer) so callers keep asking
    # for section-relative paths like bootstrap.cert-manager.
    if isinstance(data, dict) and "stacks" in data:
        name = args.stack or data.get("current")
        stacks = data.get("stacks", {}) or {}
        if name not in stacks:
            print(
                f"ERROR: stack '{name}' not found in versions.yaml stacks",
                file=sys.stderr,
            )
            sys.exit(1)
        data = stacks[name]

    if args.dotpath:
        print(get_dotpath(data, args.dotpath))
    elif args.section:
        section = data.get(args.section, {})
        if not isinstance(section, dict):
            print(f"ERROR: section '{args.section}' is not a dict", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(section, indent=2))
        elif args.shell:
            for k, v in section.items():
                env_name = k.upper().replace("-", "_") + "_VERSION"
                print(f'{env_name}="{v}"')
        else:
            for k, v in section.items():
                print(f"{k}: {v}")
    else:
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
