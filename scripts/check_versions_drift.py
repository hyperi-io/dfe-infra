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


def _parse_nested(text: str) -> dict:
    """Indent-aware parse of the versions.yaml subset (nested maps of scalars).

    Same reader as scripts/dfe-stack -- handles the nested `stacks:` shape to
    arbitrary depth. No PyYAML (runs on a bare CI image).
    """
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        m = re.match(
            r'^([A-Za-z0-9_.-]+):\s*(?:"([^"]*)"|([^#]*?))?\s*(?:#.*)?$', raw.strip()
        )
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


def load_versions() -> dict[str, str]:
    """Flatten the CURRENT stack's sections into dotted keys -> value.

    versions.yaml is NESTED (stacks: -> <version> -> <section> -> key). Read the
    `current` pointer, descend into stacks[current], and flatten THAT stack's
    sections (e.g. operators.keda, services.clickhouse-version). The drift-check
    always validates the stack under development.
    """
    root = _parse_nested(VERSIONS_FILE.read_text())
    current = root.get("current")
    stacks = root.get("stacks", {})
    if not current or current not in stacks:
        raise SystemExit(
            f"versions.yaml: `current` ({current!r}) not found in stacks: "
            f"({', '.join(stacks) or 'none'})"
        )
    flat: dict[str, str] = {}
    for section, body in stacks[current].items():
        if isinstance(body, dict):
            for key, value in body.items():
                if isinstance(value, str):
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
    #
    # cert-manager + external-secrets are intentionally NOT drift-checked here:
    # they moved out of the Argo appset to bootstrap.sh (dedup fix, dfe-infra#4 --
    # bootstrap owns them so Argo does not install a second copy). bootstrap.sh
    # reads their versions straight from versions.yaml at runtime (read_versions.py),
    # so there is no hardcoded pin that can drift -- nothing to check.
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
    (
        "clickhouse-operator appset (scale)",
        "operators.clickhouse-operator",
        lambda: find_appset_chart_version(
            Path("argocd/appsets/layer-scale.yaml"), "clickhouse-operator-helm"
        ),
    ),
    (
        # opt-in redpanda operator: source.chart/targetRevision, not a list element
        "redpanda-operator appset (scale, opt-in)",
        "operators.redpanda-operator",
        lambda: find_regex(
            Path("argocd/appsets/layer-scale.yaml"),
            r"charts\.redpanda\.com\n\s*chart: operator\n\s*targetRevision:\s*\"([^\"]+)\"",
        ),
    ),
    (
        "redpanda broker tag (kafka values)",
        "services.redpanda-version",
        lambda: find_regex(
            Path("helm/charts/kafka/values.yaml"),
            r"redpandadata/redpanda\n\s*#[^\n]*\n\s*tag:\s*\"([^\"]+)\"",
        ),
    ),
    # Kafka logical version (our kafka chart -> strimzi Kafka CR spec.kafka.version)
    (
        "kafka version (kafka values)",
        "services.kafka-version",
        lambda: find_regex(
            Path("helm/charts/kafka/values.yaml"),
            r"name: dfe-kafka\n\s*version:\s*\"([^\"]+)\"",
        ),
    ),
    # kafbat (class D): chart value is tag@digest -- compare the TAG part to SSoT
    (
        "kafbat image tag",
        "services.kafbat",
        lambda: find_regex(
            Path("helm/charts/kafbat/values.yaml"),
            r"kafka-ui\n\s*tag:\s*\"([^\"@]+)",
        ),
    ),
    # ferretdb: EXCLUDED from the loop-closer -- the k8s ferretdb chart is still
    # appVersion 1.24.0 while SSoT services.ferretdb is 2.7.0 (the 1.x->2.x
    # DocumentDB migration is out of scope; see docs/stack-components.md). A check
    # now would either fail CI or force that migration. Re-add when k8s ferretdb
    # moves to 2.x.
    # ClickHouse chart values: server version + keeper tag
    (
        "clickhouse server version",
        "services.clickhouse-version",
        lambda: find_regex(
            Path("helm/charts/clickhouse-cluster/values.yaml"),
            r"\n  version:\s*\"([^\"]+)\"",
        ),
    ),
    (
        "clickhouse keeper tag",
        "services.clickhouse-version",
        lambda: find_regex(
            Path("helm/charts/clickhouse-cluster/values.yaml"),
            r"clickhouse-keeper\n\s*tag:\s*\"([^\"]+)\"",
        ),
    ),
    # otel-collector: explicit image tag + chart appVersion
    (
        "otel image tag",
        "services.otel-collector",
        lambda: find_regex(
            Path("helm/charts/otel-collector/values.yaml"),
            r"opentelemetry-collector-contrib\n\s*tag:\s*\"([^\"]+)\"",
        ),
    ),
    (
        "otel chart appVersion",
        "services.otel-collector",
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
    # services.forgejo cascades to the chart's appVersion: image.tag is empty,
    # so dfe-common.image falls back to it.
    (
        "forgejo chart appVersion",
        "services.forgejo",
        lambda: find_regex(
            Path("helm/charts/forgejo/Chart.yaml"), r'appVersion:\s*"([^"]+)"'
        ),
    ),
]

# OpenTofu provider constraints, which versions.yaml records as a mirror of the
# required_providers blocks. Every declaration must equal the record.
#
# Deliberately partial: the tf-oidc-* modules declare azuread, okta and google,
# which `providers:` does not record at all. Whether the optional OIDC modules
# belong in the stack manifest is an open question; until it is answered those
# three are pinned only in their module.
_PROVIDER_MIRRORS = [
    ("providers.hashicorp-null", "null", ["terraform/modules/tf-naming/variables.tf"]),
    (
        "providers.hashicorp-vault",
        "vault",
        [
            "terraform/modules/tf-iam/variables.tf",
            "terraform/modules/tf-secrets/variables.tf",
            "terraform/environments/local/main.tf",
        ],
    ),
    (
        "providers.hashicorp-random",
        "random",
        ["terraform/modules/tf-secrets/variables.tf"],
    ),
]

for _key, _prov, _files in _PROVIDER_MIRRORS:
    for _f in _files:
        CHECKS.append(
            (
                f"{_prov} provider in {_f.split('/')[-2]}",
                _key,
                # `<name> = {` then the nearest following `version = "..."`,
                # bounded so it cannot run into the next provider block.
                lambda prov=_prov, f=_f: find_regex(
                    Path(f),
                    r"\b" + re.escape(prov) + r"\s*=\s*\{[\s\S]{0,120}?"
                    r'version\s*=\s*"([^"]+)"',
                ),
            )
        )

# DFE app charts: each chart's appVersion MUST equal versions.yaml apps.<name>.
# This is the pin that actually drives the deployed image tag -- dfe-common.image
# falls back to .Chart.AppVersion when image.tag is empty (the deploy default), so
# drift here is a silent ImagePullBackOff on a real cluster. (This is exactly the
# blanket-"2.2.0" bug the apps section warns about.) Generated from one list so a
# new app chart is covered the moment its versions.yaml pin + chart exist.
_APP_CHARTS = [
    "dfe-engine",
    "dfe-ui",
    "dfe-receiver",
    "dfe-loader",
    "dfe-archiver",
    "dfe-fetcher",
    "dfe-transform-vrl",
    "dfe-transform-vector",
    "dfe-transform-wasm",
    "dfe-transform-elastic",
    "dfe-transform-splack",
]
for _app in _APP_CHARTS:
    CHECKS.append(
        (
            f"{_app} chart appVersion",
            f"apps.{_app}",
            # bind _app per-iteration (default-arg closure) so each lambda checks
            # its own chart, not the last loop value.
            lambda app=_app: find_regex(
                Path(f"helm/charts/{app}/Chart.yaml"), r'appVersion:\s*"([^"]+)"'
            ),
        )
    )


# Keys with no hardcoded second copy anywhere, and why. A key that is neither
# checked above nor listed here fails the build: a pin nobody reads is dead
# config, and a pin read in two places with only one tracked is drift waiting to
# happen. Either state is a decision, so it has to be written down.
#
# Patterns are exact keys or `section.*`.
UNCONSUMED: dict[str, str] = {
    "bootstrap.cert-manager": "bootstrap.sh reads it at runtime (read_versions.py); no hardcoded copy",
    "bootstrap.external-secrets": "bootstrap.sh reads it at runtime; no hardcoded copy",
    "bootstrap.argocd": "bootstrap.sh reads it at runtime; no hardcoded copy",
    "bootstrap.local-path-provisioner": "bootstrap.sh reads it at runtime; no hardcoded copy",
    "services.postgresql": "CNPG major version, consumed by dfe-stack render",
    "services.cnpg-cluster-instances": "replica count, overridden per profile",
    "services.kafka-replicas": "replica count, overridden per profile",
    "services.clickhouse-replicas": "replica count, overridden per profile",
    "services.ferretdb": "docker path only; the k8s chart lags on 1.24.0 under a dated waiver",
    "services.documentdb-pg": "docker path only, consumed by dfe-stack render",
    "services.hyperdx": "upstream reference for the dfe-hyperdx fork; nothing deploys it",
    "services.nginx-proxy": "docker path only; k8s uses envoy-gateway",
    "operators.envoy-gateway": "nothing here installs gateway-helm; the chart only configures a gateway already present",
    "digests.*": "the immutable half of a tag@sha256 pin, rendered by dfe-stack",
    "services-digests.*": "the immutable half of a tag@sha256 pin, rendered by dfe-stack",
    "content.*": "lockstep content repos; PENDING until the first release stamps them",
    "stack.*": "upgrade-graph metadata, not a version pin",
}


def unconsumed_reason(key: str) -> str | None:
    """The recorded reason this key has no second copy, or None."""
    if key in UNCONSUMED:
        return UNCONSUMED[key]
    section = key.split(".", 1)[0]
    return UNCONSUMED.get(f"{section}.*")


def dead_guards(versions: dict[str, str]) -> list[str]:
    """Constraint rules whose `when-equals` no longer matches its key.

    A `when-equals` guard is an exact match, so bumping the pin it watches
    leaves the rule present, green and inert. Any such rule is reported: either
    re-point it at the new value or delete it.
    """
    root = _parse_nested(VERSIONS_FILE.read_text())
    stack = root.get("stacks", {}).get(root.get("current"), {})
    rel = stack.get("constraints") if isinstance(stack, dict) else None
    if not rel:
        return []
    path = REPO_ROOT / rel
    if not path.is_file():
        return [f"  [config]  constraints file not found: {rel}"]

    problems = []
    rules = _parse_nested(path.read_text()).get("rules", {})
    for rule_id, body in rules.items():
        if not isinstance(body, dict):
            continue
        pinned = body.get("when-equals")
        when_key = body.get("when-key")
        if not pinned or not when_key:
            continue
        current = versions.get(when_key)
        if current is None:
            problems.append(
                f"  [dead guard] {rule_id}: when-key '{when_key}' is not in versions.yaml"
            )
        elif current != pinned:
            problems.append(
                f"  [dead guard] {rule_id}: when-equals '{pinned}' but {when_key} "
                f"is now '{current}' -- the rule can never fire; re-point or delete it"
            )
    return problems


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

    # Coverage: a key read by nothing is dead config, and it stays green forever
    # unless something asks.
    covered = {key for _, key, _ in CHECKS}
    for key in sorted(versions):
        if key in covered or unconsumed_reason(key):
            continue
        failures.append(
            f"  [dead]    versions.yaml key '{key}' is read by no check -- add a "
            f"CHECKS entry, or an UNCONSUMED reason saying why it has no second copy"
        )

    # Stale UNCONSUMED entries rot the same way the pins do.
    for pattern in sorted(UNCONSUMED):
        if pattern.endswith(".*"):
            section = pattern[:-2]
            if any(k.split(".", 1)[0] == section for k in versions):
                continue
        elif pattern in versions:
            continue
        failures.append(
            f"  [stale]   UNCONSUMED lists '{pattern}', which is not in versions.yaml"
        )

    failures.extend(dead_guards(versions))

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

    print(
        f"OK -- all {checked} version pins match versions.yaml; "
        f"{len(versions)} key(s) accounted for."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
