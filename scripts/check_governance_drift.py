#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/check_governance_drift.py
#  Purpose:      Drift guard: resolve the dfe-deploy template's governance
#                actions (cls: helmvars changes) against the values the
#                deployed charts read, so a rename of a dial var cannot
#                orphan an action silently.
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
must resolve to a key the deployed chart reads.

An app is deployed from its thin chart (layer2-apps.yaml), so a path resolves
when it is set in a values layer under argocd/values, in dfe-extras' defaults
for the chart, or is one of the scalo-service library's per-group keys
(secrets.<group>.enabled and the like) for a group the app's contract declares.
A service with no thin chart resolves against its own chart's values under
helm/charts or helm/edge.

This is key presence. Whether the keys of one action agree with each other (a
Secret group left on beside the component that mints it) is
scripts/tests/test_governance_actions.py's, which renders them.

A checkout of the deploy template may legitimately be absent (an operator run
with no clone beside this repo). Absence is a LOUD skip, never a silent pass --
and --require turns it into a failure for CI legs that are expected to have
the checkout.

Usage:
    python3 scripts/check_governance_drift.py --deploy-root ../dfe-deploy
    python3 scripts/check_governance_drift.py --deploy-root .dfe-deploy --require

Needs PyYAML (same dependency as the sibling validators).
"""

import argparse
import json
import sys
from collections.abc import Collection
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("ERROR: PyYAML is required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent

# What the layer 2 appset layers for an app, and where dfe-extras keeps its defaults.
THIN_VALUES = REPO_ROOT / "argocd" / "values"
EXTRAS_VALUES = REPO_ROOT / "helm" / "charts" / "dfe-extras" / "values.yaml"
CONTRACTS = REPO_ROOT / "scripts" / "tests" / "fixtures" / "contracts"

# The keys scalo-service reads under secrets.<group> for each group a contract declares
# (values.schema.json, secrets.additionalProperties.properties).
LIBRARY_SECRET_KEYS = {"enabled", "existingSecret", "keys", "optional"}

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


def _chart_values(*roots: Path) -> dict[str, dict]:
    """Map chart NAME -> parsed values.yaml (missing/empty -> {}).

    Keyed on the name Chart.yaml declares rather than the directory, because a
    helmvars target names the service, and the edge module's gateway sits in a
    directory that is not its chart name.
    """
    out: dict[str, dict] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for chart_dir in sorted(root.iterdir()):
            chart_file = chart_dir / "Chart.yaml"
            if not chart_file.is_file():
                continue
            declared = _load_yaml(chart_file)
            name = declared.get("name") if isinstance(declared, dict) else None
            values_file = chart_dir / "values.yaml"
            parsed = _load_yaml(values_file) if values_file.is_file() else {}
            out[name or chart_dir.name] = parsed if isinstance(parsed, dict) else {}
    return out


def _thin_services() -> set[str]:
    """The services layer2-apps deploys from a thin chart: a directory under argocd/values/apps."""
    apps = THIN_VALUES / "apps"
    if not apps.is_dir():
        return set()
    return {p.name for p in apps.iterdir() if p.is_dir() and not p.name.startswith("_")}


def _thin_layers(service: str) -> list[dict]:
    """The values layers the appset reads for a service, then dfe-extras' defaults for its chart."""
    apps = THIN_VALUES / "apps"
    files = [
        *sorted(THIN_VALUES.glob("*.yaml")),
        apps / "_common.yaml",
        *sorted((apps / service).glob("*.yaml")),
    ]
    layers = [doc for f in files if f.is_file() and isinstance(doc := _load_yaml(f), dict)]
    extras = _load_yaml(EXTRAS_VALUES) if EXTRAS_VALUES.is_file() else None
    defaults = ((extras or {}).get("extras") or {}).get("defaults") or {}
    chart = "dfe-hyperdx" if service == "hyperdx" else service
    return [*layers, defaults.get(chart) or {}]


def _contract_groups(service: str) -> set[str]:
    """The secret groups the service's deployment contract declares."""
    contract = CONTRACTS / f"{service}.json"
    if not contract.is_file():
        return set()
    declared = json.loads(contract.read_text(encoding="utf-8"))
    return {group["group_name"] for group in declared.get("secrets") or []}


def _thin_source(service: str, path: str) -> str | None:
    """What declares ``path`` for a service on the thin path, or None where nothing does."""
    if any(_path_exists(layer, path) for layer in _thin_layers(service)):
        return "the layer 2 values or dfe-extras defaults"
    parts = path.split(".")
    is_group_key = (
        len(parts) >= 3
        and parts[0] == "secrets"
        and parts[1] in _contract_groups(service)
        and parts[2] in LIBRARY_SECRET_KEYS
    )
    return "the contract's secret group" if is_group_key else None


def _resolve_chart(name: str, charts: Collection[str]) -> tuple[str, str] | None:
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
        nargs="+",
        default=[str(REPO_ROOT / "helm" / "charts"), str(REPO_ROOT / "helm" / "edge")],
        help="chart trees to resolve against; a tree left out is a target that cannot resolve",
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
            "governance drift NOT checked (supply --deploy-root or DFE_DEPLOY_ROOT)"
        )
        if args.require:
            print(f"FAIL: {msg}", file=sys.stderr)
            return 1
        print(f"SKIPPED: {msg}", file=sys.stderr)
        return 0

    charts = _chart_values(*(Path(root) for root in args.charts_dir))
    if not charts:
        print(f"FAIL: no charts found under {' '.join(args.charts_dir)}", file=sys.stderr)
        return 1

    thin_services = _thin_services()
    known = {*charts, *thin_services}
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
            resolved = _resolve_chart(name, known)
            if resolved is None:
                print(
                    f"  [FAIL] {where}: name {name!r} does not resolve to any chart in "
                    f"{sorted(known)} (expected <service>-<instance>-values)",
                    file=sys.stderr,
                )
                failures += 1
                continue
            chart, _instance = resolved
            checked += 1
            if chart in thin_services:
                declared_by = _thin_source(chart, str(path))
            elif _path_exists(charts.get(chart, {}), str(path)):
                declared_by = f"the {chart} chart's values"
            else:
                declared_by = None
            if declared_by:
                print(f"  [ok]   {where}: {path} is declared by {declared_by}", file=sys.stderr)
            else:
                print(
                    f"  [FAIL] {where}: no values layer, dfe-extras default or chart for "
                    f"{chart!r} holds {path!r} -- the action would write a dial nothing "
                    "reads (orphaned action)",
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
