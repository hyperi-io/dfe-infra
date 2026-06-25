#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         check_versions_drift.py
#  Purpose:      Fail CI if any appset / chart / manifest version pin drifts
#                from versions.yaml (the single source of truth).
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Drift-check: every real version pin must match versions.yaml.

versions.yaml is the SSoT, but ApplicationSets / chart values / plain manifests
cannot read it at runtime -- they hardcode their pins. This script enforces that
those hardcoded pins equal versions.yaml, so the two never silently diverge.

Run from the repo root (or anywhere -- paths resolve relative to this file's
parent's parent). Exits non-zero on any mismatch, listing each one.

    python3 scripts/check_versions_drift.py

No third-party deps required: versions.yaml and the structured files are parsed
with a tiny purpose-built reader (the values we check are all simple
`key: "value"` lines), so this runs on a bare CI image without PyYAML.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_FILE = REPO_ROOT / "versions.yaml"


def load_versions() -> dict[str, str]:
    """Flatten versions.yaml into dotted keys -> value (e.g. operators.keda)."""
    flat: dict[str, str] = {}
    section: str | None = None
    for raw in VERSIONS_FILE.read_text().splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        # Strip inline comments outside quotes.
        if not line.startswith(" "):  # top-level section header e.g. "operators:"
            m = re.match(r"^([a-zA-Z0-9_-]+):\s*$", line)
            if m:
                section = m.group(1)
            continue
        m = re.match(r'^\s+([a-zA-Z0-9_.-]+):\s*"?([^"#]+?)"?\s*(?:#.*)?$', line)
        if m and section:
            key, value = m.group(1), m.group(2).strip()
            flat[f"{section}.{key}"] = value
    return flat


def find_appset_chart_version(file_path: Path, chart: str) -> str | None:
    """In an ApplicationSet list element, find `version: "X"` after `chart: <chart>`."""
    text = (REPO_ROOT / file_path).read_text()
    # Match `chart: <chart>` then the nearest following `version: "X"` (within the
    # same list element -- bounded lookahead avoids crossing into the next chart).
    pattern = re.compile(
        r"chart:\s*" + re.escape(chart) + r"\b[\s\S]{0,300}?version:\s*\"([^\"]+)\""
    )
    m = pattern.search(text)
    return m.group(1) if m else None


def find_regex(file_path: Path, pattern: str) -> str | None:
    """Return capture group 1 of the first regex match in a file."""
    text = (REPO_ROOT / file_path).read_text()
    m = re.search(pattern, text)
    return m.group(1) if m else None


# Each check: (label, versions.yaml dotted key, actual-value extractor).
CHECKS: list[tuple[str, str, "callable"]] = [
    # layer1 operator addons (appset hardcodes; must equal versions.yaml)
    (
        "cert-manager appset",
        "bootstrap.cert-manager",
        lambda: find_appset_chart_version(
            Path("argocd/appsets/layer1-addons.yaml"), "cert-manager"
        ),
    ),
    (
        "external-secrets appset",
        "bootstrap.external-secrets",
        lambda: find_appset_chart_version(
            Path("argocd/appsets/layer1-addons.yaml"), "external-secrets"
        ),
    ),
    (
        "external-dns appset",
        "operators.external-dns",
        lambda: find_appset_chart_version(
            Path("argocd/appsets/layer1-addons.yaml"), "external-dns"
        ),
    ),
    (
        "keda appset",
        "operators.keda",
        lambda: find_appset_chart_version(
            Path("argocd/appsets/layer1-addons.yaml"), "keda"
        ),
    ),
    (
        "metrics-server appset",
        "operators.metrics-server",
        lambda: find_appset_chart_version(
            Path("argocd/appsets/layer1-addons.yaml"), "metrics-server"
        ),
    ),
    (
        "reloader appset",
        "operators.reloader",
        lambda: find_appset_chart_version(
            Path("argocd/appsets/layer1-addons.yaml"), "reloader"
        ),
    ),
    (
        "cloudnative-pg appset",
        "operators.cloudnative-pg",
        lambda: find_appset_chart_version(
            Path("argocd/appsets/layer1-addons.yaml"), "cloudnative-pg"
        ),
    ),
    (
        "strimzi appset (scale)",
        "operators.strimzi-kafka-operator",
        lambda: find_appset_chart_version(
            Path("argocd/appsets/layer-scale.yaml"), "strimzi-kafka-operator"
        ),
    ),
    # ClickHouse chart values: server version + keeper tag
    (
        "clickhouse server version",
        "data.clickhouse-version",
        lambda: find_regex(
            Path("helm/charts/clickhouse-cluster/values.yaml"),
            r"\n  version:\s*\"([^\"]+)\"",
        ),
    ),
    (
        "clickhouse keeper tag",
        "data.clickhouse-version",
        lambda: find_regex(
            Path("helm/charts/clickhouse-cluster/values.yaml"),
            r"clickhouse-keeper\n\s*tag:\s*\"([^\"]+)\"",
        ),
    ),
    # otel-collector: explicit image tag + chart appVersion
    (
        "otel image tag",
        "data.otel-collector",
        lambda: find_regex(
            Path("helm/charts/otel-collector/values.yaml"),
            r"opentelemetry-collector-contrib\n\s*tag:\s*\"([^\"]+)\"",
        ),
    ),
    (
        "otel chart appVersion",
        "data.otel-collector",
        lambda: find_regex(
            Path("helm/charts/otel-collector/Chart.yaml"), r"appVersion:\s*\"([^\"]+)\""
        ),
    ),
    # valkey plain manifest image tag
    (
        "valkey manifest image",
        "bootstrap.valkey",
        lambda: find_regex(
            Path("bootstrap/templates/valkey.yaml"), r"valkey/valkey:([^\s\"]+)"
        ),
    ),
]


def main() -> int:
    versions = load_versions()
    failures: list[str] = []
    checked = 0

    for label, key, extractor in CHECKS:
        expected = versions.get(key)
        if expected is None:
            failures.append(f"  [config] {label}: versions.yaml key '{key}' not found")
            continue
        actual = extractor()
        if actual is None:
            failures.append(
                f"  [missing] {label}: could not locate the pin in its file"
            )
            continue
        checked += 1
        if actual != expected:
            failures.append(
                f"  [DRIFT]  {label}: file has '{actual}', versions.yaml says '{expected}' (key {key})"
            )

    if failures:
        print(
            "Version drift detected -- pins must match versions.yaml (SSoT):",
            file=sys.stderr,
        )
        print("\n".join(failures), file=sys.stderr)
        print(
            f"\n{len(failures)} problem(s); {checked} pin(s) matched.", file=sys.stderr
        )
        return 1

    print(f"OK -- all {checked} version pins match versions.yaml.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
