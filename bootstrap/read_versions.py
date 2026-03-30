#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         read_versions.py
#  Purpose:      Read pinned versions from versions.yaml (the SSOT)
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
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
    """Parse the simple 2-level YAML we use (no nested objects beyond depth 2)."""
    result = {}
    current_section = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Top-level key (no leading whitespace)
        if not line[0].isspace() and stripped.endswith(":"):
            current_section = stripped.rstrip(":").strip()
            result[current_section] = {}
        elif not line[0].isspace() and ": " in stripped:
            # Top-level key with inline value
            key, val = stripped.split(": ", 1)
            val = val.strip().strip('"').strip("'")
            if val == "{}":
                result[key.strip()] = {}
            else:
                result[key.strip()] = val
        elif current_section and ":" in stripped:
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if val == "{}":
                pass  # empty dict value, section already initialized
            else:
                result[current_section][key] = val
    return result


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
    parser.add_argument("dotpath", nargs="?", help="Dot-separated path (e.g. bootstrap.cert-manager)")
    parser.add_argument("--section", help="Output all keys in a section")
    parser.add_argument("--shell", action="store_true", help="Output as UPPER_SNAKE=value for shell eval")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--file", default=None, help="Path to versions.yaml (default: auto-detect)")
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
