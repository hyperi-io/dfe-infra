#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/keda_table.py
#  Purpose:      The per-app KEDA table, read from the chart goldens' rendered ScaledObjects.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""keda_table -- the per-app KEDA table the docs site shows, from the rendered charts.

    python3 scripts/keda_table.py            # print the table
    python3 scripts/keda_table.py --write    # write docs-site/generated/keda-table.md
    python3 scripts/keda_table.py --check    # exit 1 when the written table is stale

Each app's ScaledObject is read from the goldens test_thin_renders.py checks against a
live render, so the table shows what each profile deploys.
"""

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tests"))
from _gate import GOLDENS, PIN_TOKEN_RE  # noqa: E402

OUT = REPO_ROOT / "docs-site" / "generated" / "keda-table.md"
PROFILES = ("slim", "single", "scale")
_PRESSURE = "/keda/pressure"


def scaled_object(path: Path) -> dict | None:
    """The ScaledObject a golden renders, or None when it renders none."""
    if not path.is_file():
        return None
    docs = yaml.safe_load_all(PIN_TOKEN_RE.sub("pin", path.read_text(encoding="utf-8")))
    return next((d for d in docs if isinstance(d, dict) and d.get("kind") == "ScaledObject"), None)


def describe(so: dict | None) -> tuple[str, str, str, str]:
    """(replicas, CPU target, pressure trigger, other triggers) for one profile."""
    if so is None:
        return "off", "", "", ""
    spec = so.get("spec", {})
    triggers = spec.get("triggers", [])
    cpu = next((t["metadata"].get("value", "") for t in triggers if t.get("type") == "cpu"), "")
    pressure = any(_PRESSURE in str(t.get("metadata", {}).get("url", "")) for t in triggers)
    other = sorted(
        {
            t.get("type", "")
            for t in triggers
            if t.get("type") != "cpu" and _PRESSURE not in str(t.get("metadata", {}).get("url", ""))
        }
    )
    replicas = f"{spec.get('minReplicaCount', '?')}-{spec.get('maxReplicaCount', '?')}"
    return replicas, f"{cpu}%" if cpu else "", "yes" if pressure else "no", ", ".join(other)


def table() -> str:
    """The markdown table, one row per app that scales in any profile."""
    lines = [
        "| App | Profile | Replicas (min-max) | CPU target | Pressure trigger | Other triggers |",
        "|---|---|---|---|---|---|",
    ]
    for app in sorted(p.name for p in GOLDENS.iterdir() if p.is_dir()):
        rows = [
            (profile, describe(scaled_object(GOLDENS / app / f"{profile}-local.yaml")))
            for profile in PROFILES
        ]
        if all(row[1][0] == "off" for row in rows):
            continue
        for profile, (replicas, cpu, pressure, other) in rows:
            lines.append(f"| `{app}` | `{profile}` | {replicas} | {cpu} | {pressure} | {other} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help=f"write {OUT.relative_to(REPO_ROOT)}")
    mode.add_argument("--check", action="store_true", help="exit 1 when the written table is stale")
    args = parser.parse_args(argv)
    text = table()
    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text, encoding="utf-8", newline="\n")
        return 0
    if args.check:
        if not OUT.is_file() or OUT.read_text(encoding="utf-8") != text:
            print(
                f"error: {OUT.relative_to(REPO_ROOT)} is stale: run python3 scripts/keda_table.py --write",
                file=sys.stderr,
            )
            return 1
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
