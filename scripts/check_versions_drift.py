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
    python3 scripts/check_versions_drift.py --stack 2.2.0-rc.12

`--stack` picks which `stacks.<version>` block the appset pins, Chart.yaml
appVersions and every other mirror are checked against; it defaults to the
`current` pointer, so an existing caller with no flag sees no change. Some
CHECKS entries watch a key that is pinned starting the 2.2.0-rc.14 block (cut
on a separate branch, not merged here): a stack older than rc.14 carries no
value for that key at all, so auditing it there is NOTED rather than failed --
see PENDING_MIRRORS. A stack that does carry the key is compared strictly,
like any other pin.

One pair of literals has no versions.yaml key at all: the HELM_VERSION that
helm-lint.yml and release.yml each install. The check holds them equal to each
other, so the release gate resolves charts with the Helm the PR check used.

No third-party deps required: versions.yaml and the structured files are parsed
with a tiny purpose-built reader (the values we check are all simple
`key: "value"` lines), so this runs on a bare CI image without PyYAML.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import cache
from itertools import pairwise
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS_FILE = REPO_ROOT / "versions.yaml"

# Imported by path so `python3 scripts/check_versions_drift.py` works from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import version_range  # noqa: E402


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


def load_versions(stack: str | None = None) -> dict[str, str]:
    """Flatten the SELECTED stack's sections into dotted keys -> value.

    versions.yaml is NESTED (stacks: -> <version> -> <section> -> key). Read
    the `current` pointer, resolve the selected stack (the `stack` argument if
    given, else `current`), descend into stacks[selected], and flatten THAT
    stack's sections (e.g. operators.keda, services.clickhouse-version). The
    default keeps every existing caller checking the stack under development;
    `--stack` lets a caller audit a different block (e.g. an in-flight cut)
    without moving `current`.

    The selected stack id is also returned, as `pointers.current` -- the name
    is kept rather than renamed to `pointers.selected` because every existing
    Check against deployment.example.yaml / dfe-stack/Chart.yaml already reads
    that key, and what those files SHOULD say is exactly "the stack being
    audited", default or overridden.
    """
    root = _parse_nested(VERSIONS_FILE.read_text())
    current = root.get("current")
    stacks = root.get("stacks", {})
    selected = stack or current
    if not selected or selected not in stacks:
        raise SystemExit(
            f"versions.yaml: stack {selected!r} not found in stacks: "
            f"({', '.join(stacks) or 'none'})"
        )
    flat: dict[str, str] = {"pointers.current": selected}
    for section, body in stacks[selected].items():
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


def docker_arg_pattern(arg: str) -> str:
    """`ARG <name>="X"` in a Dockerfile -- capture group 1 is the pin."""
    return r"ARG\s+" + re.escape(arg) + r'="([^"]+)"'


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
    (
        "aws-load-balancer-controller appset (aws)",
        "operators.aws-load-balancer-controller",
        "argocd/appsets/layer1-addons.yaml",
        "aws-load-balancer-controller",
    ),
    (
        "karpenter appset (aws)",
        "operators.karpenter",
        "argocd/appsets/layer1-addons.yaml",
        "karpenter",
    ),
]

CHECKS: list[Check] = [
    Check(label, key, Path(f), appset_chart_pattern(chart)) for label, key, f, chart in _APPSET_PINS
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
    # The mesh appset repeats the operator pin, and a check reads its FIRST
    # match only, so this one is anchored on that appset's profile selector.
    Check(
        "clickhouse-operator appset (mesh)",
        "operators.clickhouse-operator",
        Path("argocd/appsets/layer-scale.yaml"),
        r"dfe\.hyperi\.io/profile: mesh[\s\S]{0,400}?chart: clickhouse-operator-helm"
        r"[\s\S]{0,300}?version:\s*\"([^\"]+)\"",
    ),
    Check(
        "redpanda broker tag (kafka values)",
        "services.redpanda-version",
        Path("helm/charts/kafka/values.yaml"),
        r"redpandadata/redpanda\n\s*#[^\n]*\n\s*tag:\s*\"([^\"]+)\"",
    ),
    # The single-mode StatefulSet pulls by it; the cluster-mode CR cannot carry it.
    Check(
        "redpanda broker digest (kafka values)",
        "services-digests.redpanda-version",
        Path("helm/charts/kafka/values.yaml"),
        r'redpandadata/redpanda\n(?:\s*#[^\n]*\n)*\s*tag:[^\n]*\n(?:\s*#[^\n]*\n)*\s*digest:\s*"([^"]+)"',
    ),
    # Kafka logical version (our kafka chart -> strimzi Kafka CR spec.kafka.version)
    Check(
        "kafka version (kafka values)",
        "services.kafka-version",
        Path("helm/charts/kafka/values.yaml"),
        r"name: dfe-kafka\n\s*version:\s*\"([^\"]+)\"",
    ),
    # apache/kafka at that version, which single mode and the MSK bootstrap Job run.
    Check(
        "kafka image digest (kafka values)",
        "services-digests.kafka-version",
        Path("helm/charts/kafka/values.yaml"),
        r'name: dfe-kafka\n\s*version:[^\n]*\n(?:\s*#[^\n]*\n)*\s*imageDigest:\s*"([^"]+)"',
    ),
    # The kafka chart's own copy of the Strimzi operator version, which gates
    # kafka.storageModel=tiered-object at render time (spec.kafka.tieredStorage needs
    # >= 0.38.0). Helm cannot read the appset, so the pin is duplicated here and
    # this check is what stops the two diverging.
    Check(
        "strimzi operator version (kafka values)",
        "operators.strimzi-kafka-operator",
        Path("helm/charts/kafka/values.yaml"),
        r"operatorVersion:\s*\"([^\"]+)\"",
    ),
    # The MSK bootstrap Job's SASL/IAM login module: a jar, not an image, so the
    # immutable half is the release sha256 the chart pins beside this version.
    # Anchored on iamAuth because comment lines sit between the key and the pin.
    Check(
        "aws-msk-iam-auth jar version (kafka values)",
        "services.aws-msk-iam-auth",
        Path("helm/charts/kafka/values.yaml"),
        r"iamAuth:\n(?:\s*#[^\n]*\n)*\s*version:\s*\"([^\"]+)\"",
    ),
    # The Cruise Control UI: a release tarball, not an image, on the same terms
    # as the jar above -- the integrity half is the sha256 the chart pins beside
    # it. Anchored on `release:` because comments sit between the key and the pin.
    Check(
        "cruise-control-ui release (kafka values)",
        "services.cruise-control-ui",
        Path("helm/charts/kafka/values.yaml"),
        r"release:\n(?:\s*#[^\n]*\n)*\s*version:\s*\"([^\"]+)\"",
    ),
    # It is SERVED by the same nginx image the links page runs, so the kafka
    # chart carries a second copy of that pin and both halves are checked here.
    Check(
        "cruise-control-ui server image tag (kafka values)",
        "services.nginx-unprivileged",
        Path("helm/charts/kafka/values.yaml"),
        r"nginx-unprivileged\n\s*tag:\s*\"([^\"@]+)",
    ),
    Check(
        "cruise-control-ui server image digest (kafka values)",
        "services-digests.nginx-unprivileged",
        Path("helm/charts/kafka/values.yaml"),
        r'nginx-unprivileged\n\s*tag:[^\n]*\n(?:\s*#[^\n]*\n)*\s*digest:\s*"([^"]+)"',
    ),
    # kafbat (class D): chart value is tag@digest -- compare the TAG part to SSoT
    Check(
        "kafbat image tag",
        "services.kafbat",
        Path("helm/charts/kafbat/values.yaml"),
        r"kafka-ui\n\s*tag:\s*\"([^\"@]+)",
    ),
    # envoy-gateway operator: standalone bootstrap app pulls the OCI chart.
    Check(
        "envoy-gateway operator app",
        "operators.envoy-gateway",
        Path("argocd/bootstrap/envoy-gateway-app.yaml"),
        r"chart: gateway-helm\n\s*targetRevision:\s*\"([^\"]+)\"",
    ),
    # links page (class D shape): the tag floats upstream, so both halves of the
    # pin are checked; appVersion cascades the tag.
    Check(
        "links image tag",
        "services.nginx-unprivileged",
        Path("helm/charts/links/values.yaml"),
        r"nginx-unprivileged\n\s*tag:\s*\"([^\"@]+)",
    ),
    Check(
        "links image digest",
        "services-digests.nginx-unprivileged",
        Path("helm/charts/links/values.yaml"),
        r'nginx-unprivileged\n\s*tag:[^\n]*\n(?:\s*#[^\n]*\n)*\s*digest:\s*"([^"]+)"',
    ),
    Check(
        "links chart appVersion",
        "services.nginx-unprivileged",
        Path("helm/charts/links/Chart.yaml"),
        r'appVersion:\s*"([^"]+)"',
    ),
    # ferretdb (class D shape): appVersion cascades the image tag (image.tag is
    # empty), and the chart's documentdb backend pins its own copy of the
    # documentdb-pg tag alongside cnpg-cluster's.
    Check(
        "ferretdb chart appVersion",
        "services.ferretdb",
        Path("helm/charts/ferretdb/Chart.yaml"),
        r'appVersion:\s*"([^"]+)"',
    ),
    Check(
        "ferretdb image digest",
        "services-digests.ferretdb",
        Path("helm/charts/ferretdb/values.yaml"),
        r'ferretdb/ferretdb\n\s*tag:[^\n]*\n(?:\s*#[^\n]*\n)*\s*digest:\s*"([^"]+)"',
    ),
    Check(
        "documentdb-pg image tag (ferretdb values)",
        "services.documentdb-pg",
        Path("helm/charts/ferretdb/values.yaml"),
        r'postgres-documentdb\n(?:\s*#[^\n]*\n)*\s*tag:\s*"([^"]+)"',
    ),
    Check(
        "documentdb-pg image digest (ferretdb values)",
        "services-digests.documentdb-pg",
        Path("helm/charts/ferretdb/values.yaml"),
        r'postgres-documentdb\n(?:\s*#[^\n]*\n)*\s*tag:[^\n]*\n(?:\s*#[^\n]*\n)*\s*digest:\s*"([^"]+)"',
    ),
    # ClickHouse chart values: server version and digest, keeper tag and digest
    Check(
        "clickhouse server version",
        "services.clickhouse-version",
        Path("helm/charts/clickhouse-cluster/values.yaml"),
        r"\n  version:\s*\"([^\"]+)\"",
    ),
    Check(
        "clickhouse server image digest",
        "services-digests.clickhouse-version",
        Path("helm/charts/clickhouse-cluster/values.yaml"),
        r'\n  imageDigest:\s*"([^"]+)"',
    ),
    Check(
        "clickhouse keeper tag",
        "services.clickhouse-keeper",
        Path("helm/charts/clickhouse-cluster/values.yaml"),
        r"clickhouse-keeper\n\s*tag:\s*\"([^\"]+)\"",
    ),
    Check(
        "clickhouse keeper digest",
        "services-digests.clickhouse-keeper",
        Path("helm/charts/clickhouse-cluster/values.yaml"),
        r'clickhouse-keeper\n\s*tag:[^\n]*\n(?:\s*#[^\n]*\n)*\s*digest:\s*"([^"]+)"',
    ),
    # otel-collector: explicit image tag and digest + chart appVersion
    Check(
        "otel image tag",
        "services.otel-collector",
        Path("helm/charts/otel-collector/values.yaml"),
        r"opentelemetry-collector-contrib\n\s*tag:\s*\"([^\"]+)\"",
    ),
    Check(
        "otel image digest",
        "services-digests.otel-collector",
        Path("helm/charts/otel-collector/values.yaml"),
        r'opentelemetry-collector-contrib\n\s*tag:[^\n]*\n(?:\s*#[^\n]*\n)*\s*digest:\s*"([^"]+)"',
    ),
    Check(
        "otel chart appVersion",
        "services.otel-collector",
        Path("helm/charts/otel-collector/Chart.yaml"),
        r"appVersion:\s*\"([^\"]+)\"",
    ),
    # valkey plain manifest: one tag@sha256 string, both halves checked
    Check(
        "valkey manifest image tag",
        "bootstrap.valkey",
        Path("bootstrap/templates/valkey.yaml"),
        r"valkey/valkey:([^\s\"@]+)@",
    ),
    Check(
        "valkey manifest image digest",
        "services-digests.valkey",
        Path("bootstrap/templates/valkey.yaml"),
        r"valkey/valkey:[^\s\"@]+@([^\s\"]+)",
    ),
    # services.forgejo cascades to the chart's appVersion: image.tag is empty,
    # so dfe-common.image falls back to it.
    Check(
        "forgejo chart appVersion",
        "services.forgejo",
        Path("helm/charts/forgejo/Chart.yaml"),
        r'appVersion:\s*"([^"]+)"',
    ),
    Check(
        "forgejo image digest",
        "services-digests.forgejo",
        Path("helm/charts/forgejo/values.yaml"),
        r'forgejo/forgejo\n\s*tag:[^\n]*\n(?:\s*#[^\n]*\n)*\s*digest:\s*"([^"]+)"',
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
        "documentdb-pg image digest (cnpg values)",
        "services-digests.documentdb-pg",
        Path("helm/charts/cnpg-cluster/values.yaml"),
        r'postgres-documentdb\n\s*tag:[^\n]*\n(?:\s*#[^\n]*\n)*\s*digest:\s*"([^"]+)"',
    ),
    Check(
        "kafbat chart appVersion",
        "services.kafbat",
        Path("helm/charts/kafbat/Chart.yaml"),
        r'appVersion:\s*"([^"]+)"',
    ),
    # karpenter-pools renders no image. Its appVersion is the Karpenter release
    # whose CRDs every field in it was checked against, so the two moving apart
    # is a chart written for a schema the cluster is not running.
    Check(
        "karpenter-pools chart appVersion",
        "operators.karpenter",
        Path("helm/charts/karpenter-pools/Chart.yaml"),
        r'appVersion:\s*"([^"]+)"',
    ),
    # The node image alias is dated, not `latest`, precisely so it has a pin
    # this check can hold it to -- see the pin's own comment in versions.yaml.
    Check(
        "karpenter-pools AL2023 AMI alias",
        "operators.karpenter-al2023-ami",
        Path("helm/charts/karpenter-pools/values.yaml"),
        r'amiAlias:\s*al2023@([^\s"]+)',
    ),
    # The worked example names a stack version the same way a chart names an
    # image tag, so it goes stale the moment `current` moves.
    Check(
        "stack pin (deployment example)",
        "pointers.current",
        Path("deployment.example.yaml"),
        r"\nversion:\n\s*pin:\s*\"?([^\"\s#]+)",
    ),
    # The trial umbrella advertises the stack version it composes.
    Check(
        "dfe-stack umbrella appVersion",
        "pointers.current",
        Path("helm/dfe-stack/Chart.yaml"),
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
        [
            "terraform/modules/tf-secrets/variables.tf",
            "terraform/modules/secrets/aws-sm/versions.tf",
            "terraform/environments/aws/versions.tf",
        ],
    ),
    # The AWS path declares the provider in each module and in both roots, so a
    # lift has to move all four together or one of them resolves a different
    # major on its own `tofu init`.
    (
        "providers.hashicorp-aws",
        "aws",
        [
            "terraform/modules/kubernetes-cluster/aws/versions.tf",
            "terraform/modules/managed-kafka/msk/versions.tf",
            "terraform/modules/managed-kafka/confluent-cloud/versions.tf",
            "terraform/modules/managed-kafka/redpanda-cloud/versions.tf",
            "terraform/modules/secrets/aws-sm/versions.tf",
            "terraform/modules/toolbox/aws/versions.tf",
            "terraform/modules/edge/aws/versions.tf",
            "terraform/environments/aws/versions.tf",
            "terraform/environments/aws-state/versions.tf",
        ],
    ),
    # The two SaaS Kafka providers, each paired with aws above for the
    # private-link handshake's other end. The aws root ALSO declares both --
    # its `provider "confluent" {}` / `provider "redpanda" {}` blocks configure
    # whichever body count selects -- so a lift has to move all three together.
    (
        "providers.confluentinc-confluent",
        "confluent",
        [
            "terraform/modules/managed-kafka/confluent-cloud/versions.tf",
            "terraform/environments/aws/versions.tf",
        ],
    ),
    (
        "providers.redpanda-data-redpanda",
        "redpanda",
        [
            "terraform/modules/managed-kafka/redpanda-cloud/versions.tf",
            "terraform/environments/aws/versions.tf",
        ],
    ),
    # Zips the broker-count autoscaler's inline Lambda source. Local-only, no
    # cloud API, and msk/ is its one consumer.
    (
        "providers.hashicorp-archive",
        "archive",
        ["terraform/modules/managed-kafka/msk/versions.tf"],
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
    "dfe-transform-elastic",
    # dfe-transform-splack and dfe-transform-wasm are coming: alpha, unpublished,
    # uncomment when they ship and their versions.yaml pins come back.
    # "dfe-transform-wasm",
    # "dfe-transform-splack",
    # The one app whose image is not published under the dfe- prefix; the chart
    # spells its repository out, so only the tag and digest halves are checked.
    "culvert",
]

# The charts that do not sit under helm/charts. The edge module keeps its two
# in helm/edge, so every path built from a chart name is resolved through here
# rather than by concatenation.
_CHART_DIRS = {
    "culvert": "helm/edge/culvert",
    "envoy-gateway-config": "helm/edge/gateway",
}


def chart_dir(name: str) -> str:
    """The directory holding the chart of that name."""
    return _CHART_DIRS.get(name, f"helm/charts/{name}")


CHECKS += [
    Check(
        f"{app} chart appVersion",
        f"apps.{app}",
        Path(f"{chart_dir(app)}/Chart.yaml"),
        r'appVersion:\s*"([^"]+)"',
    )
    for app in _APP_CHARTS
]

# The other half of the same pin: a registry tag is mutable, so appVersion alone
# lets a re-pushed tag land bytes other than the ones `dfe-stack verify`
# certified. dfe-common.image appends image.digest as tag@sha256. Derived from
# versions.yaml, so an app joins the moment it first publishes a digest and a
# chart missing the mirror is reported rather than assumed deliberate.
_DIGEST_MIRRORS = [app for app in _APP_CHARTS if f"digests.{app}" in load_versions()]
CHECKS += [
    Check(
        f"{app} image digest",
        f"digests.{app}",
        Path(f"{chart_dir(app)}/values.yaml"),
        r'digest:\s*"([^"]+)"',
    )
    for app in _DIGEST_MIRRORS
]

# The engine chart mounts each app's container contract by RUNNING that app's
# pinned image, so every content entry carries another copy of the app pin.
# BOTH halves are checked: the ref is tag@sha256 and a deployment pulls by
# digest, so a tag rewritten on its own would name one release and run another.
_CONTRACT_ENTRIES = [
    "dfe-receiver",
    "dfe-loader",
    "dfe-archiver",
    "dfe-fetcher",
    "dfe-transform-vrl",
    "dfe-transform-vector",
    "dfe-transform-elastic",
]


# The source catalogue is emitted by an app image as well, so it is one more
# copy of that app's pin -- a second entry for an app already in the list above.
_CATALOGUE_ENTRIES = ["dfe-transform-elastic"]


def content_ref_pattern(role: str, app: str, half: str) -> str:
    """One half of a content entry's `ref`, anchored on the entry's role and app.

    Every ref sits in one file, so a bare `ref:` anchor would hand the first
    entry's value to every check. The role is part of the anchor because one app
    can back two entries -- its contract and its catalogue -- and a check claims
    its FIRST match only.
    """
    head = (
        r"role: " + re.escape(role) + r"\n\s*app: " + re.escape(app)
        + r"\n\s*ref: \"ghcr\.io/hyperi-io/" + re.escape(app) + ":"
    )
    return head + (r"([^\"@]+)@" if half == "tag" else r"[^\"@]+@([^\"]+)\"")


CHECKS += [
    Check(
        f"{app} {role} entry image {half}",
        f"{'apps' if half == 'tag' else 'digests'}.{app}",
        Path("helm/charts/dfe-engine/values.yaml"),
        content_ref_pattern(role, app, half),
    )
    for role, apps in (("contract", _CONTRACT_ENTRIES), ("catalogue", _CATALOGUE_ENTRIES))
    for app in apps
    for half in ("tag", "digest")
]

# The hyperdx chart runs an init container on the ENGINE image to materialise the
# dashboards the engine owns. Helm cannot read a sibling chart's appVersion, so
# the engine tag has a second copy here and needs watching like any other.
CHECKS += [
    Check(
        "hyperdx dashboards init-container engine tag",
        "apps.dfe-engine",
        Path("helm/charts/hyperdx/values.yaml"),
        r'repository:\s*""[^\n]*\n\s*tag:\s*"([^"]+)"',
    ),
    # The immutable half of that same second copy: the init container pulls the
    # engine image, so a re-pushed tag lands bytes `dfe-stack verify` never saw.
    Check(
        "hyperdx dashboards init-container engine digest",
        "digests.dfe-engine",
        Path("helm/charts/hyperdx/values.yaml"),
        r'repository:\s*""[^\n]*\n\s*tag:\s*"[^"]+"[^\n]*\n(?:\s*#[^\n]*\n)*\s*digest:\s*"([^"]+)"',
    ),
    # The chart directory is `hyperdx` while the pin is `apps.dfe-hyperdx`, so it
    # does not fit _APP_CHARTS' name-derived path. Left unchecked it kept upstream
    # HyperDX's own appVersion, which is not a tag the fork ever publishes.
    Check(
        "hyperdx chart appVersion",
        "content.dfe-hyperdx",
        Path("helm/charts/hyperdx/Chart.yaml"),
        r'appVersion:\s*"([^"]+)"',
    ),
    # The immutable half of that pin, which the same name mismatch kept out of
    # _DIGEST_MIRRORS -- so versions.yaml carried digests.dfe-hyperdx while the
    # chart rendered a bare tag. Anchored on the fork's repository line so it
    # cannot match the dashboards digest, which is a different image.
    Check(
        "hyperdx fork image digest",
        "digests.dfe-hyperdx",
        Path("helm/charts/hyperdx/values.yaml"),
        r'repository:\s*ghcr\.io/hyperi-io/dfe-hyperdx[^\n]*\n\s*tag:[^\n]*\n(?:\s*#[^\n]*\n)*\s*digest:\s*"([^"]+)"',
    ),
    # The engine reports the deployment's dfe-ui version on
    # GET /api/v1/system/deployment. Helm cannot read a sibling chart's
    # appVersion, so the engine chart carries a second copy of the ui pin.
    Check(
        "dfe-ui version (engine values)",
        "apps.dfe-ui",
        Path("helm/charts/dfe-engine/values.yaml"),
        r'uiVersion:\s*"([^"]+)"',
    ),
    # The git-sync sidecar in the hunt-runner pod: a THIRD-PARTY image in a chart
    # whose own image is dfe-engine, so both halves are anchored on the repository
    # line rather than on the file's first tag:/digest: (which are the engine's).
    Check(
        "hunt-runner git-sync image tag",
        "services.git-sync",
        Path("helm/charts/dfe-engine/values.yaml"),
        r'git-sync/git-sync\n\s*tag:\s*"([^"@]+)"',
    ),
    Check(
        "hunt-runner git-sync image digest",
        "services-digests.git-sync",
        Path("helm/charts/dfe-engine/values.yaml"),
        r'git-sync/git-sync\n\s*tag:[^\n]*\n(?:\s*#[^\n]*\n)*\s*digest:\s*"([^"]+)"',
    ),
    # bootstrap.sh writes this helperPod.yaml over upstream's, whose busybox has no tag.
    Check(
        "local-path helper pod busybox tag",
        "services.busybox",
        Path("bootstrap/templates/local-path-helper-pod.yaml"),
        r"library/busybox:([^\s\"@]+)@",
    ),
    Check(
        "local-path helper pod busybox digest",
        "services-digests.busybox",
        Path("bootstrap/templates/local-path-helper-pod.yaml"),
        r"library/busybox:[^\s\"@]+@([^\s\"]+)",
    ),
    # One tag@sha256 string that both EnvoyProxy resources in the chart read.
    Check(
        "envoy gateway proxy image tag",
        "services.envoy-gateway-proxy",
        Path("helm/edge/gateway/values.yaml"),
        r'envoyproxy/envoy:([^"@]+)@',
    ),
    Check(
        "envoy gateway proxy image digest",
        "services-digests.envoy-gateway-proxy",
        Path("helm/edge/gateway/values.yaml"),
        r'envoyproxy/envoy:[^"@]+@([^"]+)"',
    ),
]

# dfe-toolbox image family (docker/dfe-toolbox/): a standalone ops shell, not
# a deployed stack component, but every ARG default in its Dockerfiles must
# still equal the versions.yaml pin it starts from -- the workflow overrides
# each one at build time from the SAME source, but the default is what a
# plain `docker build` with no --build-arg gets, and it is what
# `check_versions_drift.py --fix` keeps current. clickhouse-client pins
# services.clickhouse-version directly rather than a toolbox.* key of its
# own, so there is only ever one ClickHouse version to keep in step with.
_TOOLBOX_ARGS = [
    # (label, versions.yaml key, Dockerfile, ARG name)
    ("toolbox base image Debian base", "toolbox.debian", "docker/dfe-toolbox/base/Dockerfile", "DEBIAN_TAG"),
    ("toolbox base image kubectl", "toolbox.kubectl", "docker/dfe-toolbox/base/Dockerfile", "KUBECTL_VERSION"),
    ("toolbox base image helm", "toolbox.helm", "docker/dfe-toolbox/base/Dockerfile", "HELM_VERSION"),
    ("toolbox base image argocd CLI", "toolbox.argocd-cli", "docker/dfe-toolbox/base/Dockerfile", "ARGOCD_VERSION"),
    ("toolbox base image tofu", "toolbox.tofu", "docker/dfe-toolbox/base/Dockerfile", "TOFU_VERSION"),
    ("toolbox base image yq", "toolbox.yq", "docker/dfe-toolbox/base/Dockerfile", "YQ_VERSION"),
    ("toolbox base image clickhouse-client", "services.clickhouse-version", "docker/dfe-toolbox/base/Dockerfile", "CLICKHOUSE_VERSION"),
    ("toolbox aws image base tag", "toolbox.dfe-toolbox", "docker/dfe-toolbox/aws/Dockerfile", "BASE_TAG"),
    ("toolbox aws image aws-cli", "toolbox.aws-cli", "docker/dfe-toolbox/aws/Dockerfile", "AWS_CLI_VERSION"),
    (
        "toolbox aws image session-manager-plugin",
        "toolbox.aws-session-manager-plugin",
        "docker/dfe-toolbox/aws/Dockerfile",
        "AWS_SSM_PLUGIN_VERSION",
    ),
    ("toolbox gcp image base tag", "toolbox.dfe-toolbox", "docker/dfe-toolbox/gcp/Dockerfile", "BASE_TAG"),
    ("toolbox gcp image gcloud", "toolbox.gcloud", "docker/dfe-toolbox/gcp/Dockerfile", "GCLOUD_VERSION"),
    ("toolbox azure image base tag", "toolbox.dfe-toolbox", "docker/dfe-toolbox/azure/Dockerfile", "BASE_TAG"),
    ("toolbox azure image az-cli", "toolbox.az-cli", "docker/dfe-toolbox/azure/Dockerfile", "AZ_CLI_VERSION"),
]
CHECKS += [
    Check(label, key, Path(f), docker_arg_pattern(arg)) for label, key, f, arg in _TOOLBOX_ARGS
]

# The in-cluster pod chart (helm/charts/dfe-toolbox) ships the base image --
# no cloud CLI, per the security pass -- so it pins the same family tag rather
# than a mirror of its own. Both the tag Helm actually renders and the
# appVersion dfe-common.image falls back to are checked, so neither can drift
# from the family's one pin or from each other.
CHECKS += [
    Check(
        "dfe-toolbox chart image tag",
        "toolbox.dfe-toolbox",
        Path("helm/charts/dfe-toolbox/values.yaml"),
        r'tag:\s*"([^"]+)"',
    ),
    Check(
        "dfe-toolbox chart appVersion",
        "toolbox.dfe-toolbox",
        Path("helm/charts/dfe-toolbox/Chart.yaml"),
        r'appVersion:\s*"([^"]+)"',
    ),
    # The immutable half: the chart's image is dfe-toolbox-base, not a
    # _APP_CHARTS name, so _DIGEST_MIRRORS never picks it up.
    Check(
        "dfe-toolbox chart image digest",
        "digests.dfe-toolbox-base",
        Path("helm/charts/dfe-toolbox/values.yaml"),
        r'digest:\s*"([^"]+)"',
    ),
]


# Keys with no hardcoded second copy anywhere, and why. A key that is neither
# checked above nor listed here fails the build: a pin nobody reads is dead
# config, and a pin read in two places with only one tracked is drift waiting to
# happen. Either state is a decision, so it has to be written down.
#
# Patterns are exact keys or `section.*`.
UNCONSUMED: dict[str, str] = {
    "platform.kubernetes": "bootstrap/check_platform.py and dfe-ops preflight both read it at runtime by name; no hardcoded copy",
    "platform.rke2": "bootstrap/check_platform.py reads it at runtime; no hardcoded copy",
    "platform.rancher": "DECLARED, not checked: nothing in a cluster reports the Rancher managing it, so there is no second copy to drift against",
    "platform.eks": "a REQUIREMENT on a cluster this repo does not build -- deployment.example.yaml takes an existing cluster and argocd/values/aws.yaml is a Plan 07 stub. Give it a Check once that stub becomes real provisioning",
    "bootstrap.cert-manager": "bootstrap.sh reads it at runtime (read_versions.py); no hardcoded copy",
    "bootstrap.external-secrets": "bootstrap.sh reads it at runtime; no hardcoded copy",
    "bootstrap.argocd": "bootstrap.sh reads it at runtime; no hardcoded copy",
    "bootstrap.local-path-provisioner": "bootstrap.sh reads it at runtime; no hardcoded copy",
    "bootstrap.metallb": "bootstrap.sh reads it at runtime; no hardcoded copy",
    "services.cnpg-cluster-instances": "replica count, overridden per profile",
    "services.kafka-replicas": "replica count, overridden per profile",
    "services.clickhouse-replicas": "replica count, overridden per profile",
    "services.hyperdx": "upstream HyperDX's own version, recorded for the fork-update workstream; the chart's appVersion tracks content.dfe-hyperdx instead, because the fork publishes its own tags and never one of upstream's",
    "services.envoy-proxy": "docker path only; the k8s gateway runs services.envoy-gateway-proxy, a different (distroless) tag",
    "digests.*": "an app with no chart mirror yet -- a published app is checked against helm/charts/<app>/values.yaml image.digest instead",
    "services-digests.*": "the immutable half of a tag@sha256 pin, rendered by dfe-stack for the docker path; the k8s consumers that have one are checked individually, and bootstrap.sh reads local-path-provisioner's at runtime",
    "content.*": "lockstep content repos; PENDING until the first release stamps them",
    "stack.*": "upgrade-graph metadata, not a version pin",
}


# The release gate must resolve charts with the Helm the PR check installs, and
# toolbox.helm is the toolbox image's own, separate pin.
HELM_WORKFLOWS = (
    Path(".github/workflows/helm-lint.yml"),
    Path(".github/workflows/release.yml"),
)
HELM_VERSION_PATTERN = r'(?m)^\s*HELM_VERSION:\s*"([^"]+)"'


def helm_version_problems(texts: dict[Path, str]) -> list[str]:
    """Problems with the HELM_VERSION literal across the given workflow texts.

    Args:
        texts: Workflow file text, keyed by its repo-relative path.

    Returns:
        One line per file that does not carry exactly one literal, plus one
        line when the literals found disagree; empty when they all match.
    """
    problems: list[str] = []
    found: dict[Path, str] = {}
    for path, text in texts.items():
        pins = re.findall(HELM_VERSION_PATTERN, text)
        if len(pins) != 1:
            problems.append(
                f"  [missing] {path}: expected one HELM_VERSION literal, found {len(pins)}"
            )
            continue
        found[path] = pins[0]
    if len(set(found.values())) > 1:
        detail = ", ".join(f"{path} has '{value}'" for path, value in found.items())
        problems.append(f"  [DRIFT]  HELM_VERSION differs between workflows: {detail}")
    return problems


def unconsumed_reason(key: str) -> str | None:
    """The recorded reason this key has no second copy, or None."""
    if key in UNCONSUMED:
        return UNCONSUMED[key]
    section = key.split(".", 1)[0]
    return UNCONSUMED.get(f"{section}.*")


# Keys whose mirror already exists in this tree but whose versions.yaml pin
# starts only with the 2.2.0-rc.14 block, so the table is permanent: any older
# selectable stack predates the key, has nothing to compare it against, and
# needs this exemption for as long as it stays selectable -- not just until
# `current` reaches rc.14.
#
# Patterns are exact keys or `section.*`, same convention as UNCONSUMED.
PENDING_MIRRORS: dict[str, str] = {
    "operators.karpenter": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "operators.aws-load-balancer-controller": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "operators.karpenter-al2023-ami": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "services.aws-msk-iam-auth": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "services.cruise-control-ui": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "toolbox.*": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "providers.hashicorp-aws": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "providers.confluentinc-confluent": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "providers.redpanda-data-redpanda": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "providers.hashicorp-archive": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "services.busybox": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "services-digests.busybox": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "services.envoy-gateway-proxy": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "services-digests.envoy-gateway-proxy": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "services.clickhouse-keeper": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "services-digests.clickhouse-keeper": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "services-digests.forgejo": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
    "services-digests.valkey": "pinned starting the 2.2.0-rc.14 block; a stack that predates rc.14 has no value to compare",
}


def pending_reason(key: str) -> str | None:
    """The recorded reason this key's mirror has not landed yet, or None."""
    if key in PENDING_MIRRORS:
        return PENDING_MIRRORS[key]
    section = key.split(".", 1)[0]
    return PENDING_MIRRORS.get(f"{section}.*")


def _pattern_is_pinned(pattern: str, versions: dict[str, str]) -> bool:
    """Whether the selected stack carries a value for a PENDING_MIRRORS entry."""
    if pattern.endswith(".*"):
        section = pattern[:-2]
        return any(k.split(".", 1)[0] == section for k in versions)
    return pattern in versions


def stacks_needing_pending_mirrors() -> list[str]:
    """Selectable stacks that do NOT pin every PENDING_MIRRORS key.

    Each one would start failing its own check the moment the list is deleted,
    which is exactly what PENDING_MIRRORS exists to stop.
    """
    stacks = _parse_nested(VERSIONS_FILE.read_text()).get("stacks", {})
    needing = []
    for stack in stacks:
        versions = load_versions(stack)
        if not all(_pattern_is_pinned(p, versions) for p in PENDING_MIRRORS):
            needing.append(stack)
    return needing


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
    ("stack pin", r'(?m)^[^\S\n]*pin:[^\S\n]*"?([^"\s#]+)"?'),
    # A content entry's ref is an image pin under a key nothing else here reads,
    # so a seventh entry added with no check would otherwise pass unseen.
    ("content ref", r'(?m)^[^\S\n]*ref:[^\S\n]*"[\w./-]+:([^"\s#@]+)'),
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
        "helm/library/dfe-common/tests/lint-test/*",
        "*",
        "helm-lint fixture, not a deployed chart",
    ),
    (
        "helm/charts/forgejo/values.yaml",
        "image ref",
        "curl for the PostSync setup Job; the tools block was deliberately dropped, and Renovate's infra-pins group watches helm-values",
    ),
    (
        "helm/edge/gateway/Chart.yaml",
        "appVersion",
        "configures a gateway that is already present, and installs no upstream image",
    ),
    (
        "helm/charts/network-policies/Chart.yaml",
        "appVersion",
        "policy-only chart with no upstream to track",
    ),
    (
        "helm/charts/dfe-transform-wasm/Chart.yaml",
        "appVersion",
        "placeholder chart for an app that is coming; its versions.yaml pin is commented out, so there is no SSoT key to check it against",
    ),
    (
        "helm/charts/dfe-transform-splack/Chart.yaml",
        "appVersion",
        "placeholder chart for an app that is coming; its versions.yaml pin is commented out, so there is no SSoT key to check it against",
    ),
)


@cache
def root_ignored_names() -> frozenset[str]:
    """Root-anchored .gitignore entries, as bare names.

    An operator's own deployment.yaml sits at the repo root and carries a stack
    pin, which is an INPUT to this checker rather than a mirror of versions.yaml
    -- no check can ever read it, so the sweep would report it unswept forever
    and refuse a deploy that followed the documented flow. Matching .gitignore
    rather than one filename keeps every other local artefact out too.

    Read from .gitignore rather than asked of git: this checker runs offline,
    and a worked tree with no git at all still has to sweep the same set.
    """
    ignore = REPO_ROOT / ".gitignore"
    if not ignore.is_file():
        return frozenset()
    names = set()
    for raw in ignore.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        # A leading slash anchors the pattern to the repo root, which is the
        # only depth this sweep reads unrecursed; a deeper path is not a name.
        if not line.startswith("/") or line.startswith("/#"):
            continue
        entry = line.lstrip("/")
        if "/" not in entry:
            names.add(entry)
    return frozenset(names)


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
    # The repo root itself, NOT recursed -- a new top-level file is then swept
    # without anyone remembering to list it. deployment.example.yaml pins a
    # stack version up here, outside every root above; the operator's own
    # deployment.yaml is gitignored and stays out.
    ignored = root_ignored_names()
    found += [
        p.relative_to(REPO_ROOT)
        for p in REPO_ROOT.iterdir()
        if p.is_file() and p.suffix in SWEEP_SUFFIXES and p.name not in ignored
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


def dead_guards(versions: dict[str, str], stack: str) -> list[str]:
    """Constraint rules whose guard no longer matches the pin it watches.

    Both guard forms go dead the same way and for the same reason -- the rule
    stays present, green and inert, which reads as passing. `when-equals` is an
    exact match, so bumping the pin it watches kills it; `when-range` is a
    comparator set, so lifting the pin outside the range kills it just as
    silently (dfe-infra#295: a strimzi lift walked out of a `when-range` guard
    and nothing said so, while the `when-equals` rule beside it WAS reported).

    The comparison uses the same `version_range` the compat-check decides with,
    so a rule cannot read live to one and dead to the other. `stack` is the
    SELECTED stack id (from `load_versions`'s `pointers.current`), so this
    follows `--stack` rather than always reading the block `current` points at.
    """
    root = _parse_nested(VERSIONS_FILE.read_text())
    block = root.get("stacks", {}).get(stack, {})
    rel = block.get("constraints") if isinstance(block, dict) else None
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
        when_key = body.get("when-key")
        pinned = body.get("when-equals")
        spec = body.get("when-range")
        if not when_key or not (pinned or spec):
            continue
        current = versions.get(when_key)
        if current is None:
            problems.append(
                f"  [dead guard] {rule_id}: when-key '{when_key}' is not in versions.yaml"
            )
        elif pinned and current != pinned:
            problems.append(
                f"  [dead guard] {rule_id}: when-equals '{pinned}' but {when_key} "
                f"is now '{current}' -- the rule can never fire; re-point or delete it"
            )
        elif spec and not version_range.satisfies(current, spec):
            problems.append(
                f"  [dead guard] {rule_id}: when-range '{spec}' but {when_key} "
                f"is now '{current}' -- the rule can never fire; re-point or delete it"
            )
    return problems


def plan_fix(
    versions: dict[str, str],
) -> tuple[dict[Path, str], list[str], list[str]]:
    """Compute each mirror file's new text. Writes NOTHING.

    The inverse of the check, off the SAME table: `extract_span` already reports
    where the literal sits, so writing it is a slice replacement. That is the
    whole reason CHECKS is data rather than closures.

    Refuses rather than guesses. A pattern that no longer matches its file means
    the file changed shape, and a write at a guessed offset would corrupt a
    chart -- so it is reported and skipped, and the caller exits non-zero.

    Returns (new text keyed by file, fixed labels, refusals).
    """
    writes: dict[Path, str] = {}
    fixed: list[str] = []
    refused: list[str] = []
    # Group by file so one file with several mirrors is read and written once,
    # and so later spans in the same file are not invalidated by an earlier write.
    by_file: dict[Path, list[Check]] = {}
    for check in CHECKS:
        by_file.setdefault(check.file, []).append(check)

    for file_path, checks in sorted(by_file.items(), key=lambda kv: str(kv[0])):
        text = read_source(file_path)
        edits: list[tuple[int, int, str, str]] = []
        for check in checks:
            expected = versions.get(check.key)
            if expected is None:
                if pending_reason(check.key):
                    continue
                refused.append(f"  [refused] {check.label}: versions.yaml has no key '{check.key}'")
                continue
            found = extract_span(check)
            if found is None:
                refused.append(
                    f"  [refused] {check.label}: its pattern no longer matches "
                    f"{file_path} -- the file changed shape; fix the pattern rather "
                    f"than letting a write land at a guessed offset"
                )
                continue
            actual, start, end = found
            if actual != expected:
                edits.append((start, end, expected, check.label))

        if not edits:
            continue
        # Overlapping spans mean two checks claim the same bytes; splicing both
        # would corrupt the file, so refuse rather than write something neither
        # check describes.
        ordered = sorted(edits)
        for (a_start, a_end, _, a_label), (b_start, _, _, b_label) in pairwise(ordered):
            if b_start < a_end:
                refused.append(
                    f"  [refused] {a_label} and {b_label} claim overlapping bytes "
                    f"in {file_path} ({a_start}-{a_end} vs {b_start}-) -- their "
                    f"patterns need narrowing before either can be written"
                )
        # Right to left, so each replacement leaves earlier offsets valid.
        for start, end, expected, label in sorted(edits, reverse=True):
            text = text[:start] + expected + text[end:]
            fixed.append(f"  [fixed]   {label}: -> '{expected}'")
        writes[file_path] = text

    return writes, fixed, refused


def apply_fix(versions: dict[str, str]) -> tuple[list[str], list[str]]:
    """Write every mirror that disagrees with the SSoT. Returns (fixed, refused).

    Nothing is written when anything was refused: a partial propagation would
    leave the tree in a state neither the SSoT nor the mirrors describe.
    """
    writes, fixed, refused = plan_fix(versions)
    if refused:
        return fixed, refused
    for file_path, text in writes.items():
        (REPO_ROOT / file_path).write_text(text, encoding="utf-8", newline="\n")
    read_source.cache_clear()
    return fixed, refused


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Strict: an unrecognised flag is an error, not something to skip past.

    A mistyped `--satck 2.2.0-rc.14` that parsed quietly would audit `current`
    instead and still exit 0, which is the one failure a drift gate must not
    have. main() takes its argv from the caller rather than reading sys.argv,
    so the test suite calling main() under the test runner's own argv never
    reaches this parser at all.
    """
    parser = argparse.ArgumentParser(
        description="Check every version pin in the tree against versions.yaml."
    )
    parser.add_argument("--fix", action="store_true", help="rewrite the mirrors that drifted, then re-verify")
    parser.add_argument("--stack", default=None, help="the stacks.<version> block to audit (default: the `current` pointer)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or [])
    fix = args.fix
    versions = load_versions(args.stack)
    stack = versions["pointers.current"]

    if fix and args.stack and args.stack != _parse_nested(VERSIONS_FILE.read_text()).get("current"):
        raise SystemExit(
            f"--fix WRITES the tree, and the tree mirrors `current`; refusing to "
            f"propagate stack {args.stack!r} over it. Audit another stack read-only "
            f"(--stack alone), or move `current` first."
        )

    if fix:
        fixed, refused = apply_fix(versions)
        if refused:
            print("\n".join(refused), file=sys.stderr)
            print(
                f"\n{len(refused)} mirror(s) REFUSED -- NOTHING was written, "
                f"including the {len(fixed)} that would otherwise have been "
                f"rewritten. A partial propagation leaves the tree matching "
                f"neither the SSoT nor the mirrors.",
                file=sys.stderr,
            )
            return 1
        print("\n".join(fixed) if fixed else "  (every mirror already matches)")
        print(f"\n{len(fixed)} mirror(s) rewritten from versions.yaml.")
        # Fall through and verify, so --fix never reports success on its own say-so.

    failures: list[str] = []
    notes: list[str] = []
    # A pending key with eight mirrors would otherwise note eight times, and a
    # wall of notes trains the reader to skip them.
    noted_pending: set[str] = set()
    checked = 0

    for check in CHECKS:
        label, key = check.label, check.key
        expected = versions.get(key)
        if expected is None:
            reason = pending_reason(key)
            if reason:
                if key not in noted_pending:
                    noted_pending.add(key)
                    notes.append(
                        f"  [note] {key}: stack '{stack}' does not pin it yet -- {reason}"
                    )
                continue
            failures.append(f"  [config] {label}: versions.yaml key '{key}' not found")
            continue
        actual = extract_value(check)
        if actual is None:
            failures.append(f"  [missing] {label}: could not locate the pin in its file")
            continue
        checked += 1
        if actual != expected:
            failures.append(
                f"  [DRIFT]  {label}: file has '{actual}', versions.yaml says '{expected}' (key {key})"
            )

    # Coverage: a key read by nothing is dead config, and it stays green forever
    # unless something asks. A PENDING_MIRRORS key is NOTED rather than failed,
    # but never skipped in silence -- a pin no check reads is the thing this
    # file exists to catch.
    covered = {check.key for check in CHECKS}
    for key in sorted(versions):
        if key in covered or unconsumed_reason(key):
            continue
        reason = pending_reason(key)
        if reason:
            notes.append(
                f"  [note] {key}: stack '{stack}' pins it with no CHECKS entry yet -- {reason}"
            )
            continue
        failures.append(
            f"  [dead]    versions.yaml key '{key}' is read by no check -- add a "
            f"CHECKS entry, or an UNCONSUMED reason saying why it has no second copy"
        )

    # A key's entry has done its job and can go once EVERY selectable stack --
    # not just the current one -- pins it, since a stack that still PREDATES
    # the key has nothing to compare it against.
    if PENDING_MIRRORS and not stacks_needing_pending_mirrors():
        failures.append(
            "  [stale]   every stack pins every PENDING_MIRRORS key -- delete "
            "PENDING_MIRRORS; each of those keys is now a normal pin"
        )

    # Stale UNCONSUMED entries rot the same way the pins do.
    for pattern in sorted(UNCONSUMED):
        if pattern.endswith(".*"):
            section = pattern[:-2]
            if any(k.split(".", 1)[0] == section for k in versions):
                continue
        elif pattern in versions:
            continue
        failures.append(f"  [stale]   UNCONSUMED lists '{pattern}', which is not in versions.yaml")

    failures.extend(dead_guards(versions, stack))
    failures.extend(helm_version_problems({path: read_source(path) for path in HELM_WORKFLOWS}))

    # The other direction: a literal in a file no check points at.
    failures.extend(reverse_sweep())

    if notes:
        print("\n".join(notes))
        print()

    if failures:
        print(
            "Version drift detected -- pins must match versions.yaml (SSoT):",
            file=sys.stderr,
        )
        print("\n".join(failures), file=sys.stderr)
        print(f"\n{len(failures)} problem(s); {checked} pin(s) matched.", file=sys.stderr)
        return 1

    # Say what the sweep looked at, not just that it found nothing: a silent
    # pass reads the same whether it swept the tree or swept nothing.
    note_suffix = f"; {len(notes)} note(s) on pins pending a mirror" if notes else ""
    print(
        f"OK -- all {checked} version pins match stack '{stack}' in versions.yaml; "
        f"{len(versions)} key(s) accounted for; "
        f"{len(sweep_files())} file(s) swept for pins no check reads{note_suffix}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
