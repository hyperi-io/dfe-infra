#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/check_governance_drift.py
#  Purpose:      Drift guard: resolve the dfe-deploy template's governance
#                actions (cls: helmvars changes) against the charts' values,
#                so a chart renaming a dial var cannot orphan an action
#                silently.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Validate dfe-deploy governance actions against chart values (issue #22).

The dfe-deploy template ships governance/actions/*.yaml whose changes[] point
at chart value paths (e.g. keda.maxReplicaCount, huntRunner.cap). dfe-deploy's
own CI validates their STRUCTURE (tools/validate_governance.py there) and
explicitly leaves chart-path drift to this repo -- the charts these paths dial
into live here, and the dial rule (docs/architecture.md) says every dial must
be a chart value. This check closes the loop: every `cls: helmvars` change
must resolve to an existing key in the referenced chart's values.yaml.

The deploy template is a PRIVATE repo, so a checkout may legitimately be
absent (public forks, tokenless CI). Absence is a LOUD skip, never a silent
pass -- and --require turns it into a failure for CI legs that are expected
to have the checkout.

Usage:
    python3 scripts/check_governance_drift.py --deploy-root ../dfe-deploy
    python3 scripts/check_governance_drift.py --deploy-root .dfe-deploy --require

Needs PyYAML (same dependency as the sibling validators).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("ERROR: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent

# changes[] classes owned by the engine's governance registry -- not chart
# dials, so not this check's business (dfe-deploy's structural validator and
# the engine's own loader police them).
_ENGINE_CLASSES = {
    "accounts",
    "groups",
    "roles",
    "actions",
    "policies",
    "ch_tiers",
    "ch_service_roles",
    "gov_settings",
}


def _load_yaml(path: Path) -> object:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _chart_values(charts_dir: Path) -> dict[str, dict]:
    """Map chart-dir name -> parsed values.yaml (missing/empty -> {})."""
    out: dict[str, dict] = {}
    for chart_dir in sorted(charts_dir.iterdir()):
        values_file = chart_dir / "values.yaml"
        if not (chart_dir / "Chart.yaml").is_file():
            continue
        parsed = _load_yaml(values_file) if values_file.is_file() else {}
        out[chart_dir.name] = parsed if isinstance(parsed, dict) else {}
    return out


def _resolve_chart(name: str, charts: dict[str, dict]) -> tuple[str, str] | None:
    """Resolve a helmvars target name to (chart, instance).

    The layer2-apps appset fans out values/<service>-<instance>-values.yaml,
    so an action's `name` is `<service>-<instance>-values`. Service names may
    themselves contain hyphens, so match against the ACTUAL chart list:
    longest chart name that the stem equals or prefixes with a '-'.
    """
    if not name.endswith("-values"):
        return None
    stem = name[: -len("-values")]
    candidates = [c for c in charts if stem == c or stem.startswith(c + "-")]
    if not candidates:
        return None
    chart = max(candidates, key=len)
    instance = stem[len(chart) + 1 :] or "default"
    return chart, instance


def _path_exists(values: dict, dotpath: str) -> bool:
    node: object = values
    for part in dotpath.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Resolve dfe-deploy governance-action helmvars paths against chart values.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument(
        "--deploy-root",
        default=None,
        help="checkout of the dfe-deploy template (default: $DFE_DEPLOY_ROOT, else ../dfe-deploy beside this repo)",
    )
    ap.add_argument(
        "--charts-dir",
        default=str(REPO_ROOT / "helm" / "charts"),
        help="chart fleet to resolve against",
    )
    ap.add_argument(
        "--require",
        action="store_true",
        help="a missing deploy checkout FAILS instead of skipping (CI)",
    )
    args = ap.parse_args(argv)

    import os

    deploy_root = Path(
        args.deploy_root or os.environ.get("DFE_DEPLOY_ROOT") or (REPO_ROOT.parent / "dfe-deploy")
    )
    actions_dir = deploy_root / "governance" / "actions"
    if not actions_dir.is_dir():
        msg = (
            f"dfe-deploy governance actions not found at {actions_dir} -- "
            "governance drift NOT checked (private template repo; supply --deploy-root or DFE_DEPLOY_ROOT)"
        )
        if args.require:
            print(f"FAIL: {msg}", file=sys.stderr)
            return 1
        print(f"SKIPPED: {msg}", file=sys.stderr)
        return 0

    charts = _chart_values(Path(args.charts_dir))
    if not charts:
        print(f"FAIL: no charts found under {args.charts_dir}", file=sys.stderr)
        return 1

    failures = 0
    checked = 0
    skipped_classes = 0
    for action_file in sorted(actions_dir.glob("*.yaml")):
        doc = _load_yaml(action_file)
        if not isinstance(doc, dict):
            print(f"  [FAIL] {action_file.name}: not a mapping", file=sys.stderr)
            failures += 1
            continue
        changes = doc.get("changes")
        if not isinstance(changes, list):
            # Structure is dfe-deploy's validator's job; note and move on.
            print(
                f"  [warn] {action_file.name}: no changes[] list (structure is dfe-deploy CI's job)",
                file=sys.stderr,
            )
            continue
        for i, change in enumerate(changes):
            if not isinstance(change, dict):
                continue
            cls = change.get("cls")
            if cls in _ENGINE_CLASSES:
                skipped_classes += 1
                continue
            if cls != "helmvars":
                print(
                    f"  [FAIL] {action_file.name} changes[{i}]: unknown cls {cls!r}",
                    file=sys.stderr,
                )
                failures += 1
                continue
            name, path = change.get("name", ""), change.get("path", "")
            where = f"{action_file.name} changes[{i}]"
            resolved = _resolve_chart(name, charts)
            if resolved is None:
                print(
                    f"  [FAIL] {where}: name {name!r} does not resolve to any chart in "
                    f"{sorted(charts)} (expected <service>-<instance>-values)",
                    file=sys.stderr,
                )
                failures += 1
                continue
            chart, _instance = resolved
            checked += 1
            if _path_exists(charts[chart], str(path)):
                print(f"  [ok]   {where}: {chart} values has {path}", file=sys.stderr)
            else:
                print(
                    f"  [FAIL] {where}: chart {chart!r} has NO values key {path!r} -- "
                    "the action would write a dial the chart no longer reads (orphaned action)",
                    file=sys.stderr,
                )
                failures += 1

    verdict = "FAILED" if failures else "PASSED"
    print(
        f"=== governance drift check {verdict}: {checked} helmvars path(s) checked, "
        f"{failures} failure(s), {skipped_classes} engine-class change(s) skipped ===",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
