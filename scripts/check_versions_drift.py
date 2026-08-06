#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         check_versions_drift.py
#  Purpose:      Fail CI if any appset / chart / manifest version pin drifts
#                from versions.yaml (the single source of truth).
#  Language:     Python
#
#  License:      BUSL-1.1
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
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import cache
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


@dataclass(frozen=True)
class Check:
    """One pin: what it is called, what it must equal, and where to find it.

    File and pattern are DATA rather than a closure so the reverse sweep can ask
    a check which literal it reads. A closure can only answer "what value did
    you get", which is not enough to tell two same-valued pins in one file apart.
    """

    label: str
    key: str  # dotted key into the flattened versions.yaml
    file: Path  # repo-relative
    pattern: str  # capture group 1 is the pin itself


@cache
def read_source(file_path: Path) -> str:
    """File text, cached -- the sweep and the checks read the same files."""
    return (REPO_ROOT / file_path).read_text()


def appset_chart_pattern(chart: str) -> str:
    """`chart: <chart>` then the nearest following `version: "X"`.

    Bounded lookahead so it cannot cross into the next chart in the list.
    """
    return r"chart:\s*" + re.escape(chart) + r"\b[\s\S]{0,300}?version:\s*\"([^\"]+)\""


def provider_pattern(provider: str) -> str:
    """`<name> = {` then the nearest following `version = "..."`.

    Bounded so it cannot run into the next provider block.
    """
    return (
        r"\b" + re.escape(provider) + r"\s*=\s*\{[\s\S]{0,120}?"
        r'version\s*=\s*"([^"]+)"'
    )


def extract_span(check: Check) -> tuple[str, int, int] | None:
    """The pin's value plus the offsets of the LITERAL itself, or None.

    The span is capture group 1, not the whole match: the reverse sweep asks
    "is the literal at this offset one a check already reads?", and a match
    typically starts lines earlier, at its anchor.
    """
    m = re.search(check.pattern, read_source(check.file))
    return (m.group(1), m.start(1), m.end(1)) if m else None


def extract_value(check: Check) -> str | None:
    """Just the pin's value."""
    found = extract_span(check)
    return found[0] if found else None


# Operator addons whose chart version the ApplicationSet hardcodes.
#
# cert-manager + external-secrets are intentionally NOT drift-checked here: they
# moved out of the Argo appset to bootstrap.sh (dedup fix, dfe-infra#4 --
# bootstrap owns them so Argo does not install a second copy). bootstrap.sh reads
# their versions straight from versions.yaml at runtime (read_versions.py), so
# there is no hardcoded pin that can drift -- nothing to check.
_APPSET_PINS = [
    # (label, versions.yaml key, appset file, chart name as the appset spells it)
    (
        "external-dns appset",
        "operators.external-dns",
        "argocd/appsets/layer1-addons.yaml",
        "external-dns",
    ),
    (
        "keda appset",
        "operators.keda",
        "argocd/appsets/layer1-addons.yaml",
        "keda",
    ),
    (
        "metrics-server appset",
        "operators.metrics-server",
        "argocd/appsets/layer1-addons.yaml",
        "metrics-server",
    ),
    (
        "reloader appset",
        "operators.reloader",
        "argocd/appsets/layer1-addons.yaml",
        "reloader",
    ),
    (
        "cloudnative-pg appset",
        "operators.cloudnative-pg",
        "argocd/appsets/layer1-addons.yaml",
        "cloudnative-pg",
    ),
    (
        "strimzi appset (scale)",
        "operators.strimzi-kafka-operator",
        "argocd/appsets/layer-scale.yaml",
        "strimzi-kafka-operator",
    ),
    (
        "clickhouse-operator appset (scale)",
        "operators.clickhouse-operator",
        "argocd/appsets/layer-scale.yaml",
        "clickhouse-operator-helm",
    ),
]

CHECKS: list[Check] = [
    Check(label, key, Path(f), appset_chart_pattern(chart))
    for label, key, f, chart in _APPSET_PINS
]

# Pins with a one-off shape, each needing its own anchor.
CHECKS += [
    # opt-in redpanda operator: source.chart/targetRevision, not a list element
    Check(
        "redpanda-operator appset (scale, opt-in)",
        "operators.redpanda-operator",
        Path("argocd/appsets/layer-scale.yaml"),
        r"charts\.redpanda\.com\n\s*chart: operator\n\s*targetRevision:\s*\"([^\"]+)\"",
    ),
    Check(
        "redpanda broker tag (kafka values)",
        "services.redpanda-version",
        Path("helm/charts/kafka/values.yaml"),
        r"redpandadata/redpanda\n\s*#[^\n]*\n\s*tag:\s*\"([^\"]+)\"",
    ),
    # Kafka logical version (our kafka chart -> strimzi Kafka CR spec.kafka.version)
    Check(
        "kafka version (kafka values)",
        "services.kafka-version",
        Path("helm/charts/kafka/values.yaml"),
        r"name: dfe-kafka\n\s*version:\s*\"([^\"]+)\"",
    ),
    # kafbat (class D): chart value is tag@digest -- compare the TAG part to SSoT
    Check(
        "kafbat image tag",
        "services.kafbat",
        Path("helm/charts/kafbat/values.yaml"),
        r"kafka-ui\n\s*tag:\s*\"([^\"@]+)",
    ),
    # ferretdb: EXCLUDED from the loop-closer -- the k8s ferretdb chart is still
    # appVersion 1.24.0 while SSoT services.ferretdb is 2.7.0 (the 1.x->2.x
    # DocumentDB migration is out of scope; see docs/stack-components.md). A check
    # now would either fail CI or force that migration. Re-add when k8s ferretdb
    # moves to 2.x.
    # ClickHouse chart values: server version + keeper tag
    Check(
        "clickhouse server version",
        "services.clickhouse-version",
        Path("helm/charts/clickhouse-cluster/values.yaml"),
        r"\n  version:\s*\"([^\"]+)\"",
    ),
    Check(
        "clickhouse keeper tag",
        "services.clickhouse-version",
        Path("helm/charts/clickhouse-cluster/values.yaml"),
        r"clickhouse-keeper\n\s*tag:\s*\"([^\"]+)\"",
    ),
    # otel-collector: explicit image tag + chart appVersion
    Check(
        "otel image tag",
        "services.otel-collector",
        Path("helm/charts/otel-collector/values.yaml"),
        r"opentelemetry-collector-contrib\n\s*tag:\s*\"([^\"]+)\"",
    ),
    Check(
        "otel chart appVersion",
        "services.otel-collector",
        Path("helm/charts/otel-collector/Chart.yaml"),
        r"appVersion:\s*\"([^\"]+)\"",
    ),
    # valkey plain manifest image tag
    Check(
        "valkey manifest image",
        "bootstrap.valkey",
        Path("bootstrap/templates/valkey.yaml"),
        r"valkey/valkey:([^\s\"]+)",
    ),
    # services.forgejo cascades to the chart's appVersion: image.tag is empty,
    # so dfe-common.image falls back to it.
    Check(
        "forgejo chart appVersion",
        "services.forgejo",
        Path("helm/charts/forgejo/Chart.yaml"),
        r'appVersion:\s*"([^"]+)"',
    ),
    # The four below were found by the reverse sweep, not by anyone adding them.
    # dfe-common.labels stamps app.kubernetes.io/version from .Chart.AppVersion
    # onto every object a chart renders, so a stale appVersion is a wrong version
    # label on live objects even where no image tag depends on it.
    Check(
        "clickhouse-cluster chart appVersion",
        "services.clickhouse-version",
        Path("helm/charts/clickhouse-cluster/Chart.yaml"),
        r'appVersion:\s*"([^"]+)"',
    ),
    Check(
        "kafka chart appVersion",
        "services.kafka-version",
        Path("helm/charts/kafka/Chart.yaml"),
        r'appVersion:\s*"([^"]+)"',
    ),
    Check(
        "cnpg-cluster chart appVersion",
        "services.postgresql",
        Path("helm/charts/cnpg-cluster/Chart.yaml"),
        r'appVersion:\s*"([^"]+)"',
    ),
    Check(
        "documentdb-pg image tag (cnpg values)",
        "services.documentdb-pg",
        Path("helm/charts/cnpg-cluster/values.yaml"),
        r'postgres-documentdb\n\s*tag:\s*"([^"]+)"',
    ),
    Check(
        "kafbat chart appVersion",
        "services.kafbat",
        Path("helm/charts/kafbat/Chart.yaml"),
        r'appVersion:\s*"([^"]+)"',
    ),
]

# OpenTofu provider constraints, which versions.yaml records as a mirror of the
# required_providers blocks. Every declaration must equal the record.
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
    # The optional OIDC modules. Nothing instantiates them, so the constraint in
    # the module is the whole of the pin -- there is no lock file behind it.
    ("providers.okta-okta", "okta", ["terraform/modules/tf-oidc-okta/main.tf"]),
    (
        "providers.hashicorp-google",
        "google",
        ["terraform/modules/tf-oidc-google/main.tf"],
    ),
    (
        "providers.hashicorp-azuread",
        "azuread",
        ["terraform/modules/tf-oidc-entra/main.tf"],
    ),
]

CHECKS += [
    Check(
        f"{prov} provider in {f.split('/')[-2]}",
        key,
        Path(f),
        provider_pattern(prov),
    )
    for key, prov, files in _PROVIDER_MIRRORS
    for f in files
]

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
CHECKS += [
    Check(
        f"{app} chart appVersion",
        f"apps.{app}",
        Path(f"helm/charts/{app}/Chart.yaml"),
        r'appVersion:\s*"([^"]+)"',
    )
    for app in _APP_CHARTS
]


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
    "services.cnpg-cluster-instances": "replica count, overridden per profile",
    "services.kafka-replicas": "replica count, overridden per profile",
    "services.clickhouse-replicas": "replica count, overridden per profile",
    "services.ferretdb": "docker path only; the k8s chart lags on 1.24.0 under a dated waiver",
    "services.hyperdx": "the hyperdx chart's appVersion carries it and agrees, but CLAUDE.md routes hyperdx-chart work through a dfe-engine issue first, so it is waived rather than checked here",
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


# CHECKS runs SSoT -> file, so a literal in a file no check points at is
# invisible to it -- which is how a component ends up with a second, unwatched
# pin. The sweep runs the other way: find the pin-shaped literals on the
# deployable surface, then require each one to be read by a check or waived.
SWEEP_ROOTS = ("argocd", "bootstrap", "helm", "terraform")

# The deployable surface only. Deliberately OUT, each verified to hold no SSoT
# pin today: `.sh` (bootstrap reads versions.yaml at runtime rather than
# hardcoding), `.tpl` (carries `default "0.0.0"`, a sentinel), `Chart.lock` and
# the vendored `.tgz` (both pin dfe-common, which is first-party and not an SSoT
# key -- the same reason the Chart.yaml waiver gives). A pin annotated into a
# shell or python file is Renovate's custom.regex manager's job, grouped there
# as `annotated-pins`; catching it here would need its own pattern.
SWEEP_SUFFIXES = (".yaml", ".yml", ".tf")

# Structural pin shapes rather than bare semver, which over a whole tree drowns
# in changelogs, fixtures and dates. Capture group 1 is the literal itself.
SWEEP_PATTERNS = (
    ("image tag", r'(?m)^[^\S\n]*tag:[^\S\n]*"?([^"\s#]+)"?'),
    ("chart version", r'(?m)^[^\S\n]*version:[^\S\n]*"?([^"\s#]+)"?'),
    ("targetRevision", r'(?m)^[^\S\n]*targetRevision:[^\S\n]*"?([^"\s#]+)"?'),
    ("appVersion", r'(?m)^[^\S\n]*appVersion:[^\S\n]*"?([^"\s#]+)"?'),
    ("image ref", r'(?m)^[^\S\n]*image:[^\S\n]*"?[\w./-]+:([^"\s#@]+)'),
    ("provider constraint", r'(?m)^[^\S\n]*version[^\S\n]*=[^\S\n]*"([^"]+)"'),
)

_HAS_DIGIT = re.compile(r"\d")

# A swept literal that no check reads, with the reason it is not a second copy
# of an SSoT pin. Same discipline as UNCONSUMED: either state is a decision, so
# it gets written down. Paths are fnmatch patterns, where `*` spans separators;
# a kind of `*` waives every shape in the file.
SWEEP_WAIVERS: tuple[tuple[str, str, str], ...] = (
    (
        "*/Chart.yaml",
        "chart version",
        "a chart's own version and its dfe-common dependency version; neither pins anything upstream",
    ),
    (
        "argocd/previews/EXAMPLE-*",
        "*",
        "worked example whose values are placeholders by design",
    ),
    (
        "helm/library/dfe-common/tests/lint-test/*",
        "*",
        "helm-lint fixture, not a deployed chart",
    ),
    (
        "helm/charts/ferretdb/Chart.yaml",
        "appVersion",
        "the k8s ferretdb chart lags on 1.24.0 under a dated waiver; see docs/stack-components.md",
    ),
    (
        "helm/charts/hyperdx/Chart.yaml",
        "appVersion",
        "agrees with services.hyperdx, but CLAUDE.md routes hyperdx-chart work through a dfe-engine issue first",
    ),
    (
        "helm/charts/forgejo/values.yaml",
        "image ref",
        "curl for the PostSync setup Job; the tools block was deliberately dropped, and Renovate's infra-pins group watches helm-values",
    ),
    (
        "helm/charts/dfe-vpn/Chart.yaml",
        "appVersion",
        "first-party chart with no upstream image -- appVersion is its own version",
    ),
    (
        "helm/charts/envoy-gateway-config/Chart.yaml",
        "appVersion",
        "configures a gateway that is already present, and installs no upstream image",
    ),
    (
        "helm/charts/network-policies/Chart.yaml",
        "appVersion",
        "policy-only chart with no upstream to track",
    ),
)


@cache
def sweep_files() -> tuple[Path, ...]:
    """Every file on the deployable surface the sweep reads, repo-relative.

    A missing root raises rather than being skipped: a renamed directory would
    otherwise shrink the sweep silently, and a sweep of nothing prints the same
    OK as a sweep that worked.
    """
    found: list[Path] = []
    for root in SWEEP_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            raise SystemExit(
                f"check_versions_drift: SWEEP_ROOTS names '{root}', which is not "
                f"a directory -- fix the path or drop it from the list"
            )
        found += [
            p.relative_to(REPO_ROOT)
            for p in base.rglob("*")
            if p.is_file() and p.suffix in SWEEP_SUFFIXES
        ]
    return tuple(sorted(found))


def checked_spans() -> dict[Path, list[tuple[int, int]]]:
    """Where each check's literal sits, keyed by file.

    Offsets rather than values: two pins in one file that happen to share a
    value are distinct here, which comparing values alone could not manage.
    """
    spans: dict[Path, list[tuple[int, int]]] = {}
    for check in CHECKS:
        found = extract_span(check)
        if found:
            spans.setdefault(check.file, []).append((found[1], found[2]))
    return spans


def reverse_sweep() -> list[str]:
    """Pin-shaped literals no check reads, plus any waiver that matched nothing.

    A check claims its FIRST match only, so a second literal matching the same
    check's pattern in the same file surfaces here rather than hiding behind it.
    """
    spans = checked_spans()
    problems: list[str] = []
    fired: set[int] = set()

    for rel in sweep_files():
        text = read_source(rel)
        posix = rel.as_posix()
        for kind, pattern in SWEEP_PATTERNS:
            for m in re.finditer(pattern, text):
                value = m.group(1)
                if not _HAS_DIGIT.search(value):
                    continue
                start, end = m.start(1), m.end(1)
                if any(s < end and start < e for s, e in spans.get(rel, [])):
                    continue
                waived = [
                    i
                    for i, (path_pat, waived_kind, _) in enumerate(SWEEP_WAIVERS)
                    if waived_kind in (kind, "*") and fnmatch(posix, path_pat)
                ]
                if waived:
                    fired.update(waived)
                    continue
                line = text.count("\n", 0, start) + 1
                problems.append(
                    f"  [unswept] {rel}:{line} {kind} '{value}' is read by no "
                    f"check -- add a CHECKS entry, or a SWEEP_WAIVERS reason "
                    f"saying why it is not an SSoT pin"
                )

    # A waiver that excuses nothing is rot, exactly like a stale UNCONSUMED key.
    for i, (path_pat, waived_kind, _) in enumerate(SWEEP_WAIVERS):
        if i not in fired:
            problems.append(
                f"  [stale]   SWEEP_WAIVERS excuses {waived_kind} in "
                f"'{path_pat}', which matches nothing -- delete it"
            )
    return problems


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

    for check in CHECKS:
        label, key = check.label, check.key
        expected = versions.get(key)
        if expected is None:
            failures.append(f"  [config] {label}: versions.yaml key '{key}' not found")
            continue
        actual = extract_value(check)
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
    covered = {check.key for check in CHECKS}
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

    # The other direction: a literal in a file no check points at.
    failures.extend(reverse_sweep())

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

    # Say what the sweep looked at, not just that it found nothing: a silent
    # pass reads the same whether it swept the tree or swept nothing.
    print(
        f"OK -- all {checked} version pins match versions.yaml; "
        f"{len(versions)} key(s) accounted for; "
        f"{len(sweep_files())} file(s) swept for pins no check reads."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
