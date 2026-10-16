#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/check_image_arch.py
#  Purpose:      Assert every image a stack pins ships a manifest for BOTH
#                linux/amd64 and linux/arm64, so a deploy never fails at exec
#                on whichever architecture the node happens to be.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The multi-arch gate over every image a stack pins.

Cloud nodes are ARM by default and on-prem nodes are x86_64 most of the time,
so an image missing either architecture pulls fine and dies at container
start with `exec format error`, after the cluster looks healthy. This check
reads every image the selected stack block of versions.yaml pins and fails on
any that lacks a platform in the required set.

The registry read is registry_pins.ref_platforms, the one `dfe-stack platforms`
and `dfe-stack release-gate` use, so there is a single definition of what an
image's platforms are.

The image classes checked:

- DFE images: every `digests:` key, read by its pinned digest, from the list
  `dfe-stack images` prints. An app with no digest is NOT YET PUBLISHED and is
  reported, not failed. The packages are public, so the read needs no login.
- Third-party images a pin tags, each pinned tag@sha256 across that pin and the
  same key under `services-digests:`: the `services:` keys in the
  key-to-repository table `dfe-stack` renders the docker path from, plus the
  k8s-path images in DIGEST_PINNED below, which include the plain manifests
  bootstrap.sh applies. Each is read by the digest a deploy pulls, and a tag
  with no digest is a failure.
- Images installed by a chart. Where the chart version IS the image tag
  (cert-manager, external-secrets, strimzi, the redpanda operator,
  envoy-gateway) no lookup is needed. Every other chart's appVersion is read
  with `helm show chart` only when --resolve-charts is given, because that
  needs helm and the chart repos. One chart can run several images. An OCI
  chart whose key also has a `services-digests:` entry is read at that digest,
  and the version the archive declares must equal the pin. A chart-run image a
  stack pins tag@sha256 under `services:` (PINNED_CHART_IMAGES) is read there,
  and its tag must equal the one the chart would render.
- Images a pinned chart runs at its OWN values' defaults, with no pin of ours to
  tag them (CHART_DEFAULT_IMAGES): read from the chart archive at the pinned
  version, a subchart from the copy the parent archive vendors, again only with
  --resolve-charts. Where the chart's template renders the block's own digest,
  the image is read by that tag@sha256.
- Images one of our charts pins in its own values with no versions.yaml key
  (CHART_VALUES_IMAGES), read from that file as committed.
- Strimzi's Kafka broker image, whose tag joins the operator and Kafka pins.

An image a chart names but no deploy pulls is not read. Those are listed, with
the reason each is never pulled, in docs/deployment/aws-operations.md.

The third-party images are public, so --third-party-only reads them with no
credential at all. CI runs it that way on every pin change (helm-lint.yml) and
in the stack release gate (release.yml); `dfe-stack release-gate` gates the
DFE images.

Network-dependent by design, so it is NOT part of the offline drift check.

A read a registry rate limits (429) or fails with a 5xx is retried with jittered
backoff by registry_pins.run_with_retry, at most five attempts a read and
RETRY_BUDGET_SECONDS of backoff across the run. A registry that still refuses then
fails that read with a message naming the registry and the rate limit.

--verdict-cache names a JSON file of verdicts an earlier run proved. A reference
pinned by digest cannot change what it points at, so one the file records as
carrying every required platform is not read again; a reference with no digest is
always read. What a chart read at a digest declared is recorded the same way. The
run writes the file back with everything digest-pinned it proved, read or
recalled, and prints one line of cache hits, registry reads and reads per host.

Usage:
    python3 scripts/check_image_arch.py [--stack VER] [--require linux/amd64,linux/arm64]
                                        [--resolve-charts] [--third-party-only]
                                        [--verdict-cache FILE] [--image REF ...]
Requires `docker` with the buildx plugin on PATH.
"""

import argparse
import importlib.machinery
import importlib.util
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry_pins

REPO_ROOT = Path(__file__).resolve().parent.parent

# Backoff the whole run may spend on retried registry reads, so a registry that
# stays rate limited fails the gate in minutes rather than once per image.
RETRY_BUDGET_SECONDS = 240.0

# A reference that names its digest, the only kind whose verdict cannot change.
DIGEST_REF = re.compile(r"@sha256:[0-9a-f]{64}$")

# Bumped when the verdict cache's layout changes, so an older file is ignored rather than misread.
VERDICT_CACHE_SCHEMA = 1

# Images the k8s path pulls tag@sha256 that dfe-stack's docker table does not
# name: versions.yaml section.key holding the tag -> image repository. The digest
# sits under the same key in services-digests:.
DIGEST_PINNED = {
    "services.nginx-unprivileged": "nginxinc/nginx-unprivileged",
    "services.git-sync": "registry.k8s.io/git-sync/git-sync",
    "services.forgejo": "code.forgejo.org/forgejo/forgejo",
    # helm/charts/clickhouse-cluster's KeeperCluster.
    "services.clickhouse-keeper": "docker.io/clickhouse/clickhouse-keeper",
    # bootstrap/templates/local-path-helper-pod.yaml, written over upstream's untagged helper.
    "services.busybox": "docker.io/library/busybox",
    # The data-plane proxy both EnvoyProxy resources in helm/edge/gateway run.
    "services.envoy-gateway-proxy": "docker.io/envoyproxy/envoy",
    # The image bootstrap/templates/valkey.yaml runs.
    "bootstrap.valkey": "valkey/valkey",
    # The image upstream's deploy/local-path-storage.yaml names, which bootstrap/local_path_image.py pins.
    "bootstrap.local-path-provisioner": "docker.io/rancher/local-path-provisioner",
    # layer1-addons.yaml deploys it by this tag@sha256 in place of the chart's own tag.
    "services.aws-load-balancer-controller-image": (
        "public.ecr.aws/eks/aws-load-balancer-controller"
    ),
}

# Chart-run images a stack may pin under DIGEST_PINNED: image repository -> that key.
# The chart's tag is still resolved, and the pinned tag must equal it.
PINNED_CHART_IMAGES = {
    "public.ecr.aws/eks/aws-load-balancer-controller": (
        "services.aws-load-balancer-controller-image"
    ),
}

# Charts whose version IS the image tag, so no chart lookup is needed:
# versions.yaml key (under bootstrap: or operators:) -> (image repositories the
# chart runs, the tag prefix the registry expects when the pin omits it).
OPERATOR_TAG_IS_CHART = {
    "cert-manager": (
        (
            "quay.io/jetstack/cert-manager-controller",
            "quay.io/jetstack/cert-manager-cainjector",
            "quay.io/jetstack/cert-manager-webhook",
            "quay.io/jetstack/cert-manager-startupapicheck",
        ),
        "v",
    ),
    "external-secrets": (("ghcr.io/external-secrets/external-secrets",), "v"),
    "strimzi-kafka-operator": (("quay.io/strimzi/operator",), ""),
    "redpanda-operator": (("docker.redpanda.com/redpandadata/redpanda-operator",), "v"),
    "envoy-gateway": (("docker.io/envoyproxy/gateway",), ""),
}

# Charts that tag their images with their own appVersion: key -> (chart
# reference, classic repo URL or None for OCI, image repositories the chart
# runs, the prefix the chart's template puts on an appVersion that lacks it).
# Read with --resolve-charts.
OPERATOR_VIA_CHART = {
    "keda": (
        "keda",
        "https://kedacore.github.io/charts",
        (
            "ghcr.io/kedacore/keda",
            "ghcr.io/kedacore/keda-metrics-apiserver",
            "ghcr.io/kedacore/keda-admission-webhooks",
        ),
        "",
    ),
    "clickhouse-operator": (
        "oci://ghcr.io/clickhouse/clickhouse-operator-helm",
        None,
        ("ghcr.io/clickhouse/clickhouse-operator",),
        "",
    ),
    "argocd": (
        "argo-cd",
        "https://argoproj.github.io/argo-helm",
        ("quay.io/argoproj/argocd",),
        "v",
    ),
    "metallb": (
        "metallb",
        "https://metallb.github.io/metallb",
        ("quay.io/metallb/controller", "quay.io/metallb/speaker"),
        "v",
    ),
    "external-dns": (
        "external-dns",
        "https://kubernetes-sigs.github.io/external-dns/",
        ("registry.k8s.io/external-dns/external-dns",),
        "v",
    ),
    "metrics-server": (
        "metrics-server",
        "https://kubernetes-sigs.github.io/metrics-server/",
        ("registry.k8s.io/metrics-server/metrics-server",),
        "v",
    ),
    "reloader": (
        "reloader",
        "https://stakater.github.io/stakater-charts",
        ("ghcr.io/stakater/reloader",),
        "v",
    ),
    # The controller is read at the digest the chart's own values pin, in CHART_DEFAULT_IMAGES.
    "karpenter": (
        "oci://public.ecr.aws/karpenter/karpenter",
        None,
        (),
        "",
    ),
    "aws-load-balancer-controller": (
        "aws-load-balancer-controller",
        "https://aws.github.io/eks-charts",
        ("public.ecr.aws/eks/aws-load-balancer-controller",),
        "v",
    ),
}

# Images a chart above runs at its own values' defaults, which no pin of ours
# tags: name -> (OPERATOR_VIA_CHART key, subchart directory under charts/ or "",
# path to the image block in that chart's values, whether an unset tag falls back
# to that chart's appVersion the way its template does, whether its template
# renders the block's digest as tag@digest). Read with --resolve-charts.
CHART_DEFAULT_IMAGES = {
    # Runs whenever no OIDC provider fronts Argo CD (bootstrap/argocd_login.py).
    "dex": ("argocd", "", ("dex", "image"), False, False),
    # The frr-k8s subchart is on by default (frrk8s.enabled) and runs both.
    "frr-k8s": ("metallb", "frr-k8s", ("frrk8s", "image"), True, False),
    "frr": ("metallb", "frr-k8s", ("frrk8s", "frr", "image"), True, False),
    # templates/_helpers.tpl renders controller.image as repository:tag@digest.
    "karpenter controller": ("karpenter", "", ("controller", "image"), True, True),
}

# Images one of our charts pins in its own values with no versions.yaml key, so
# Renovate's helm-values manager moves them: name -> (values file, path to the
# full reference).
CHART_VALUES_IMAGES = {
    "forgejo setup Job curl": (Path("helm/charts/forgejo/values.yaml"), ("setup", "image")),
}

# Strimzi runs its brokers from this repository, tagged
# <operators.strimzi-kafka-operator>-kafka-<services.kafka-version>.
STRIMZI_KAFKA_IMAGE = "quay.io/strimzi/kafka"


def _load_dfe_stack():
    """Import the extensionless scripts/dfe-stack the way its tests do."""
    path = REPO_ROOT / "scripts" / "dfe-stack"
    loader = importlib.machinery.SourceFileLoader("dfe_stack", str(path))
    spec = importlib.util.spec_from_loader("dfe_stack", loader)
    if spec is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# The DFE image list, the stack reader and the required platform set all come
# from here, so this checker and `dfe-stack platforms` cannot disagree on them.
dfe_stack = _load_dfe_stack()


def chart_registry(chart_ref: str, repo_url: str | None) -> str:
    """The host a chart is fetched from: its repo's, or an OCI reference's own."""
    return urlsplit(repo_url or chart_ref).hostname or chart_ref


def chart_source(chart_ref: str, chart_version: str, digest: str = "") -> list[str]:
    """The chart arguments helm reads: an OCI reference at its digest, else the ref and version."""
    if digest:
        return [f"{chart_ref}@{digest}"]
    return [chart_ref, "--version", chart_version]


def _top_level(text: str, name: str) -> str:
    """The unindented `name:` value in Chart.yaml text, quotes stripped, or ""."""
    for line in text.splitlines():
        if line.startswith(f"{name}:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def _require_version(declared: str, chart_ref: str, digest: str, chart_version: str) -> None:
    """Fail a chart read at a digest whose archive declares another version than the pin."""
    if digest and declared != chart_version:
        raise RuntimeError(
            f"{chart_ref}@{digest} is chart version {declared or '(none)'}, "
            f"not the pinned {chart_version}"
        )


def chart_app_version(
    chart_ref: str,
    repo_url: str | None,
    chart_version: str,
    budget: registry_pins.RetryBudget | None = None,
    digest: str = "",
) -> str:
    """appVersion of a chart at a version, from `helm show chart`.

    A classic chart repo is passed with --repo so nothing has to be added to
    the local helm state; an OCI reference needs no repo, and with ``digest`` is
    read at that digest and must declare ``chart_version``. A rate limited or
    failing registry is retried with backoff, drawing on ``budget`` when given.
    """
    cmd = ["helm", "show", "chart", *chart_source(chart_ref, chart_version, digest)]
    if repo_url:
        cmd += ["--repo", repo_url]
    try:
        result, gave_up = registry_pins.run_with_retry(
            cmd, chart_registry(chart_ref, repo_url), budget
        )
    except OSError as exc:
        raise RuntimeError(f"cannot run helm: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            gave_up or result.stderr.strip() or f"helm show chart {chart_ref} failed"
        )
    _require_version(_top_level(result.stdout, "version"), chart_ref, digest, chart_version)
    app_version = _top_level(result.stdout, "appVersion")
    if not app_version:
        raise RuntimeError(f"{chart_ref} {chart_version}: no appVersion in the chart")
    return app_version


def pull_chart(
    chart_ref: str,
    repo_url: str | None,
    chart_version: str,
    into: Path,
    budget: registry_pins.RetryBudget | None = None,
    digest: str = "",
) -> Path:
    """Untar one chart version into an empty directory and return the chart's own.

    With ``digest`` an OCI chart is pulled at that digest. A rate limited or
    failing registry is retried with backoff, drawing on ``budget`` when given.

    Raises:
        RuntimeError: helm is missing, the pull failed, or it left anything but
            one directory holding a Chart.yaml behind.
    """
    cmd = ["helm", "pull", *chart_source(chart_ref, chart_version, digest)]
    cmd += ["--untar", "--untardir", str(into)]
    if repo_url:
        cmd += ["--repo", repo_url]
    try:
        result, gave_up = registry_pins.run_with_retry(
            cmd, chart_registry(chart_ref, repo_url), budget
        )
    except OSError as exc:
        raise RuntimeError(f"cannot run helm: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(gave_up or result.stderr.strip() or f"helm pull {chart_ref} failed")
    # Helm 4 pulling by digest also leaves an empty `<chart>@sha256:...` directory beside the chart.
    charts = [path for path in into.iterdir() if (path / "Chart.yaml").is_file()]
    if len(charts) != 1:
        raise RuntimeError(f"helm pull {chart_ref} left {len(charts)} charts, expected 1")
    return charts[0]


def _block(values: object, path: tuple[str, ...]) -> dict:
    """The map at `path` in parsed values, or an empty one where any step is not a map."""
    node = values
    for step in path:
        node = node.get(step) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def _field(layers: list[dict], path: tuple[str, ...], name: str) -> str:
    """The first non-empty string `name` takes in the image block across the layers."""
    for layer in layers:
        value = _block(layer, path).get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def default_image_ref(
    layers: list[dict],
    path: tuple[str, ...],
    app_version: str | None,
    with_digest: bool = False,
) -> str:
    """repository:tag, or repository:tag@digest, for the image block at `path` in a chart's values.

    Args:
        layers: Parsed values, highest precedence first -- a parent chart's block
            for its subchart, then the subchart's own values.
        path: Keys from the values root down to the block holding
            `repository` and `tag`.
        app_version: The chart's appVersion, which an unset tag falls back to,
            or None where the chart's template has no such fallback.
        with_digest: Whether the chart's template appends the block's `digest`
            when one is set, as `tag@digest`.

    Returns:
        The image reference the chart renders.

    Raises:
        ValueError: No layer names a repository, or no tag is set and there is
            no appVersion to fall back to.
    """
    where = ".".join(path)
    repository = _field(layers, path, "repository")
    if not repository:
        raise ValueError(f"{where}.repository is not set")
    tag = _field(layers, path, "tag") or app_version
    if not tag:
        raise ValueError(f"{where}.tag is not set and the chart has no appVersion fallback")
    digest = _field(layers, path, "digest") if with_digest else ""
    return f"{repository}:{tag}@{digest}" if digest else f"{repository}:{tag}"


def values_image_ref(values: dict, path: tuple[str, ...]) -> str:
    """The full image reference at `path` in parsed values.

    Raises:
        ValueError: The value is missing, not a string, or carries no tag.
    """
    *parent, leaf = path
    ref = _block(values, tuple(parent)).get(leaf)
    if not isinstance(ref, str) or ":" not in ref.rsplit("/", 1)[-1]:
        raise ValueError(f"{'.'.join(path)} is {ref!r}, not an image reference with a tag")
    return ref


def _parsed(path: Path) -> dict:
    """A chart file read with the one nested-YAML reader the stack tooling shares."""
    return dfe_stack.parse_simple_yaml(path.read_text(encoding="utf-8"))


# One CHART_DEFAULT_IMAGES entry with its name: (name, subchart, values path,
# tag falls back to appVersion, template renders tag@digest).
type DefaultImage = tuple[str, str, tuple[str, ...], bool, bool]


def chart_dir_refs(chart: Path, images: list[DefaultImage]) -> list[str]:
    """The reference an untarred chart renders for each of its default images.

    Args:
        chart: The chart's directory, as `helm pull --untar` leaves it.
        images: One DefaultImage per image, as CHART_DEFAULT_IMAGES holds them.

    Returns:
        One image reference per entry of `images`, in order.

    Raises:
        RuntimeError: The chart vendors no such subchart, or its values do not
            name the image an entry expects.
    """
    refs: list[str] = []
    parent = _parsed(chart / "values.yaml")
    for name, subchart, path, falls_back, with_digest in images:
        source = chart / "charts" / subchart if subchart else chart
        if not source.is_dir():
            raise RuntimeError(f"{name}: the chart vendors no {subchart} subchart")
        layers = [parent]
        if subchart:
            layers = [_block(parent, (subchart,)), _parsed(source / "values.yaml")]
        app_version = _parsed(source / "Chart.yaml").get("appVersion") if falls_back else None
        fallback = app_version if isinstance(app_version, str) else None
        try:
            refs.append(default_image_ref(layers, path, fallback, with_digest))
        except ValueError as err:
            raise RuntimeError(f"{name}: {err}") from err
    return refs


def chart_default_refs(
    chart_ref: str,
    repo_url: str | None,
    chart_version: str,
    images: list[DefaultImage],
    budget: registry_pins.RetryBudget | None = None,
    digest: str = "",
) -> list[str]:
    """chart_dir_refs over a chart version pulled into a scratch directory.

    The archive is read rather than `helm show values`, because a subchart's
    values and appVersion ship inside the parent, at the version it vendors.
    With ``digest`` the archive is pulled at that digest and must declare
    ``chart_version``.

    Raises:
        RuntimeError: The pull failed, the archive declares another version, or
            chart_dir_refs failed.
    """
    with tempfile.TemporaryDirectory(prefix="check-image-arch-") as scratch:
        chart = pull_chart(chart_ref, repo_url, chart_version, Path(scratch), budget, digest)
        if digest:
            declared = _top_level((chart / "Chart.yaml").read_text(encoding="utf-8"), "version")
            _require_version(declared, chart_ref, digest, chart_version)
        return chart_dir_refs(chart, images)


def _section(stack_map: object, name: str) -> dict[str, str]:
    section = stack_map.get(name) if isinstance(stack_map, dict) else None
    if not isinstance(section, dict):
        return {}
    return {str(k): str(v) for k, v in section.items()}


def prefixed(tag: str, prefix: str) -> str:
    """The tag carrying the prefix the registry expects, added only where it is missing."""
    return tag if not prefix or tag.startswith(prefix) else prefix + tag


def _chart_pin(pins: dict[str, str], key: str) -> tuple[str, str]:
    """(section.key label, version) for a chart pinned under operators: or bootstrap:."""
    for section in ("operators", "bootstrap"):
        version = pins.get(f"{section}.{key}")
        if version:
            return f"{section}.{key}", version
    return "", ""


def is_digest_pinned(ref: str) -> bool:
    """Whether a reference names its sha256 digest, so what it points at cannot change."""
    return bool(DIGEST_REF.search(ref))


@dataclass(slots=True)
class VerdictCache:
    """What an earlier run proved about digest-pinned reads, and this run's use of it.

    Attributes:
        recalled: Image reference -> platforms, as the cache file recorded them.
        proven: Image reference -> platforms this run proved, read or recalled.
        recalled_charts: `<chart>@<digest>` -> what that archive declared, as the
            file recorded it: its `version`, and its `appVersion` or default
            `images` (image name -> reference).
        proven_charts: The same, for the charts this run read or recalled.
        hits: Image references answered from ``recalled`` without a registry read.
        misses: Digest-pinned image references the cache could not answer.
        by_tag: Image references with no digest, read every run and never recorded.
        chart_hits: Chart reads at a digest answered from ``recalled_charts``.
        chart_misses: Chart reads at a digest the cache could not answer.
    """

    recalled: dict[str, frozenset[str]] = field(default_factory=dict)
    proven: dict[str, frozenset[str]] = field(default_factory=dict)
    recalled_charts: dict[str, dict[str, object]] = field(default_factory=dict)
    proven_charts: dict[str, dict[str, object]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0
    by_tag: int = 0
    chart_hits: int = 0
    chart_misses: int = 0

    def recall(self, ref: str, required: set[str]) -> set[str] | None:
        """The platforms an earlier run proved for ``ref``, or None when it must be read.

        Args:
            ref: The image reference about to be checked.
            required: The platforms this run requires.

        Returns:
            The recorded platforms when ``ref`` names its digest and the record
            carries every required platform; None otherwise, counted as a miss
            or a by-tag read.
        """
        if not is_digest_pinned(ref):
            self.by_tag += 1
            return None
        found = self.recalled.get(ref)
        if found is None or not required <= found:
            self.misses += 1
            return None
        self.hits += 1
        self.proven[ref] = found
        return set(found)

    def record(self, ref: str, found: set[str]) -> None:
        """Keep a verdict this run proved, when the reference names its digest."""
        if is_digest_pinned(ref):
            self.proven[ref] = frozenset(found)

    def _recalled_field(self, source: str, version: str, name: str) -> object:
        record = self.recalled_charts.get(source, {})
        return record.get(name) if record.get("version") == version else None

    def recall_app_version(self, source: str, version: str) -> str | None:
        """The appVersion an earlier run read from ``source`` at ``version``, or None."""
        value = self._recalled_field(source, version, "appVersion")
        found = value if isinstance(value, str) and value else None
        if found is None:
            self.chart_misses += 1
            return None
        self.chart_hits += 1
        self.record_chart(source, version, "appVersion", found)
        return found

    def recall_images(self, source: str, version: str, names: list[str]) -> list[str] | None:
        """The default image refs an earlier run read from ``source``, in ``names`` order."""
        value = self._recalled_field(source, version, "images")
        if not isinstance(value, dict) or not all(isinstance(value.get(n), str) for n in names):
            self.chart_misses += 1
            return None
        self.chart_hits += 1
        found = [str(value[n]) for n in names]
        self.record_chart(source, version, "images", dict(zip(names, found, strict=True)))
        return found

    def record_chart(self, source: str, version: str, name: str, value: object) -> None:
        """Keep what a chart read at a digest declared, under the version it declared."""
        if is_digest_pinned(source):
            self.proven_charts.setdefault(source, {"version": version})[name] = value


def _chart_record(record: object) -> dict[str, object] | None:
    """A chart record from the cache file with only the fields of known shape, or None."""
    if not isinstance(record, dict) or not isinstance(record.get("version"), str):
        return None
    kept: dict[str, object] = {"version": record["version"]}
    if isinstance(record.get("appVersion"), str):
        kept["appVersion"] = record["appVersion"]
    images = record.get("images")
    if isinstance(images, dict) and all(isinstance(v, str) for v in images.values()):
        kept["images"] = {str(k): v for k, v in images.items()}
    return kept


def load_verdicts(path: Path) -> tuple[VerdictCache, str]:
    """The verdict cache at ``path``, and a note when it could not be used.

    A missing, unreadable or foreign file yields an empty cache, so the run reads
    every reference rather than trusting a record it cannot parse. An entry not
    keyed by a digest is dropped, because only a digest names fixed content.

    Args:
        path: The cache file an earlier run wrote.

    Returns:
        The cache, and "" or one line saying why nothing was recalled.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return VerdictCache(), f"no verdict cache at {path}; every reference is read"
    except (OSError, json.JSONDecodeError) as exc:
        note = f"verdict cache {path} is unreadable ({exc})"
        return VerdictCache(), f"{note}; every reference is read"
    schema = doc.get("schema") if isinstance(doc, dict) else None
    images = doc.get("images") if isinstance(doc, dict) else None
    charts = doc.get("charts", {}) if isinstance(doc, dict) else None
    if (
        schema != VERDICT_CACHE_SCHEMA
        or not isinstance(images, dict)
        or not isinstance(charts, dict)
    ):
        note = f"verdict cache {path} is not schema {VERDICT_CACHE_SCHEMA}"
        return VerdictCache(), f"{note}; every reference is read"
    recalled = {
        ref: frozenset(platforms)
        for ref, platforms in images.items()
        if is_digest_pinned(ref)
        and isinstance(platforms, list)
        and all(isinstance(p, str) for p in platforms)
    }
    recalled_charts = {
        source: kept
        for source, record in charts.items()
        if is_digest_pinned(source) and (kept := _chart_record(record)) is not None
    }
    return VerdictCache(recalled=recalled, recalled_charts=recalled_charts), ""


def save_verdicts(path: Path, cache: VerdictCache) -> None:
    """Write what this run proved to ``path``, replacing what was there."""
    doc = {
        "schema": VERDICT_CACHE_SCHEMA,
        "images": {ref: sorted(found) for ref, found in sorted(cache.proven.items())},
        "charts": dict(sorted(cache.proven_charts.items())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")


def cache_summary(cache: VerdictCache, reads: dict[str, int]) -> str:
    """One line: image and chart cache hits and misses, and registry reads per host."""
    hosts = ", ".join(f"{host}={count}" for host, count in sorted(reads.items())) or "none"
    return (
        f"verdict cache: {cache.hits} hit, {cache.misses} miss, "
        f"{cache.by_tag} by tag (never cached); "
        f"charts at a digest: {cache.chart_hits} hit, {cache.chart_misses} miss; "
        f"registry reads: {sum(reads.values())} ({hosts})"
    )


def chart_digests(
    digests: dict[str, str], pins: dict[str, str]
) -> tuple[dict[str, str], list[str]]:
    """(OPERATOR_VIA_CHART key -> pinned chart digest, problems) for the stack.

    Only an OCI chart has a digest to pin; one set for a chart from a classic
    repository is a problem, because no read or deploy could honour it.
    """
    found: dict[str, str] = {}
    problems: list[str] = []
    for key, (chart_ref, _repo_url, _repos, _prefix) in sorted(OPERATOR_VIA_CHART.items()):
        digest = digests.get(key, "")
        label, chart_version = _chart_pin(pins, key)
        if not digest or not chart_version:
            continue
        if not chart_ref.startswith("oci://"):
            problems.append(
                f"{label}: services-digests.{key} is set, but chart {chart_version} comes from "
                f"a classic repository, which serves no digest"
            )
            continue
        found[key] = digest
    return found, problems


def _app_version(
    key: str,
    chart_version: str,
    digest: str,
    budget: registry_pins.RetryBudget | None,
    cache: VerdictCache | None,
) -> str:
    """The appVersion of one OPERATOR_VIA_CHART chart, recalled when it is read at a digest."""
    chart_ref, repo_url, _repos, _prefix = OPERATOR_VIA_CHART[key]
    source = f"{chart_ref}@{digest}" if digest else ""
    recalled = cache.recall_app_version(source, chart_version) if cache and source else None
    if recalled:
        return recalled
    app_version = chart_app_version(chart_ref, repo_url, chart_version, budget, digest)
    if cache and source:
        cache.record_chart(source, chart_version, "appVersion", app_version)
    return app_version


def _default_refs(
    key: str,
    chart_version: str,
    digest: str,
    images: list[DefaultImage],
    budget: registry_pins.RetryBudget | None,
    cache: VerdictCache | None,
) -> list[str]:
    """The default image refs of one chart, recalled when the chart is read at a digest."""
    chart_ref, repo_url, _repos, _prefix = OPERATOR_VIA_CHART[key]
    source = f"{chart_ref}@{digest}" if digest else ""
    names = [name for name, *_ in images]
    recalled = cache.recall_images(source, chart_version, names) if cache and source else None
    if recalled:
        return recalled
    refs = chart_default_refs(chart_ref, repo_url, chart_version, images, budget, digest)
    if cache and source:
        cache.record_chart(source, chart_version, "images", dict(zip(names, refs, strict=True)))
    return refs


def image_refs(
    stack_map: dict,
    resolve_charts: bool,
    third_party_only: bool = False,
    budget: registry_pins.RetryBudget | None = None,
    cache: VerdictCache | None = None,
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """(label, reference) pairs to read, notes on what was skipped, and pins that
    cannot be read at all -- the last fail the run, because an unread image is
    not a multi-arch one. Chart lookups retry within ``budget`` when given, and a
    chart read at a digest is recalled from ``cache`` when it holds that read."""
    refs: list[tuple[str, str]] = []
    skipped: list[str] = []
    broken: list[str] = []

    dfe = dfe_stack.app_images(stack_map, dfe_stack.DEFAULT_REGISTRY)
    if third_party_only:
        skipped.append(
            f"{len(dfe)} DFE image(s) under digests: (--third-party-only; "
            "`dfe-stack platforms` reads them)"
        )
    else:
        refs += [(f"digests.{name}", ref) for name, ref in sorted(dfe.items())]
        broken += [
            f"digests.{name}: digest pinned with no tag to read it by"
            for name in dfe_stack.untagged_digests(stack_map)
        ]
        skipped += [
            f"apps.{app}: no digest in versions.yaml (NOT YET PUBLISHED)"
            for app in dfe_stack.unpublished_apps(stack_map)
        ]

    pins = {
        f"{name}.{key}": value
        for name in ("services", "bootstrap", "operators")
        for key, value in _section(stack_map, name).items()
    }
    digests = _section(stack_map, "services-digests")
    pinned = [(f"services.{key}", repo) for key, _var, repo in dfe_stack._DOCKER_THIRDPARTY]
    pinned += DIGEST_PINNED.items()
    for path, repo in sorted(pinned):
        tag = pins.get(path)
        if not tag:
            continue
        key = path.split(".", 1)[1]
        if not digests.get(key):
            broken.append(f"{path}: tag {tag} pinned with no services-digests.{key}")
            continue
        refs.append((path, f"{repo}:{tag}@{digests[key]}"))

    for name, (values_file, path) in sorted(CHART_VALUES_IMAGES.items()):
        label = f"{values_file} ({name})"
        try:
            refs.append((label, values_image_ref(_parsed(REPO_ROOT / values_file), path)))
        except (OSError, ValueError) as err:
            broken.append(f"{label}: {err}")

    for key, (repos, prefix) in sorted(OPERATOR_TAG_IS_CHART.items()):
        label, chart_version = _chart_pin(pins, key)
        if chart_version:
            refs += [(label, f"{repo}:{prefixed(chart_version, prefix)}") for repo in repos]
    pinned_charts, problems = chart_digests(digests, pins)
    broken += problems
    for key, (_chart_ref, _repo_url, repos, prefix) in sorted(OPERATOR_VIA_CHART.items()):
        label, chart_version = _chart_pin(pins, key)
        if not chart_version or not repos:
            continue
        if not resolve_charts:
            skipped.append(
                f"{label}: chart {chart_version} resolves its own appVersion; pass --resolve-charts"
            )
            continue
        try:
            app_version = _app_version(
                key, chart_version, pinned_charts.get(key, ""), budget, cache
            )
        except RuntimeError as err:
            broken.append(f"{label}: chart {chart_version} did not resolve ({err})")
            continue
        for repo in repos:
            tag = prefixed(app_version, prefix)
            override = PINNED_CHART_IMAGES.get(repo, "")
            if not pins.get(override):
                refs.append((label, f"{repo}:{tag}"))
            elif pins[override] != tag:
                broken.append(
                    f"{override}: pins {repo}:{pins[override]}, but {label} chart "
                    f"{chart_version} renders {tag}"
                )

    by_chart: dict[str, list[DefaultImage]] = {}
    for name, (key, subchart, path, falls_back, with_digest) in CHART_DEFAULT_IMAGES.items():
        by_chart.setdefault(key, []).append((name, subchart, path, falls_back, with_digest))
    for key, images in sorted(by_chart.items()):
        label, chart_version = _chart_pin(pins, key)
        if not chart_version:
            continue
        names = ", ".join(name for name, *_ in images)
        if not resolve_charts:
            note = f"sets {names} in its own values; pass --resolve-charts"
            skipped.append(f"{label}: chart {chart_version} {note}")
            continue
        try:
            defaults = _default_refs(
                key, chart_version, pinned_charts.get(key, ""), images, budget, cache
            )
        except RuntimeError as err:
            broken.append(f"{label}: chart {chart_version} images {names} did not resolve ({err})")
            continue
        refs += [(label, ref) for ref in defaults]

    operator = pins.get("operators.strimzi-kafka-operator")
    kafka = pins.get("services.kafka-version")
    if operator and kafka:
        label = "operators.strimzi-kafka-operator+services.kafka-version"
        refs.append((label, f"{STRIMZI_KAFKA_IMAGE}:{operator}-kafka-{kafka}"))
    return refs, skipped, broken


def verdict(found: set[str] | None, err: str, required: set[str]) -> tuple[str, str]:
    """(status, detail) for one image: ok, FAIL when a required platform is
    missing, ERROR when the registry could not be read."""
    if found is None:
        return "ERROR", err
    missing = sorted(required - found)
    if missing:
        return "FAIL", f"missing {missing} (has {sorted(found)})"
    return "ok", ""


def _check(
    args: argparse.Namespace,
    required: set[str],
    budget: registry_pins.RetryBudget,
    cache: VerdictCache,
) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """Read and judge every reference the arguments select.

    Returns:
        (failures, the (label, reference) pairs checked, skip notes).
    """
    if args.image:
        refs = [(ref, ref) for ref in args.image]
        skipped: list[str] = []
        broken: list[str] = []
        stack = "(explicit)"
    else:
        stack, body = dfe_stack.stack_pins(dfe_stack.load_root(), args.stack)
        refs, skipped, broken = image_refs(
            body, args.resolve_charts, args.third_party_only, budget, cache
        )
    print(f"checking stack {stack} for {sorted(required)}")

    failures = list(broken)
    for note in broken:
        print(f"  ERROR  {note}")
    for label, ref in refs:
        found = cache.recall(ref, required)
        source, err = " (proven by an earlier run)", ""
        if found is None:
            source = ""
            found, err = registry_pins.ref_platforms(ref, budget)
        status, detail = verdict(found, err, required)
        if status != "ok" or found is None:
            failures.append(f"{label} {ref}: {detail}")
            print(f"  {status:<6} {label} {ref}: {detail}")
            continue
        cache.record(ref, found)
        print(f"  ok     {label} {ref}{source}")

    for note in skipped:
        print(f"  skip   {note}")
    return failures, refs, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stack", help="stack version to check (default: the `current` pointer)")
    parser.add_argument(
        "--require",
        default=",".join(dfe_stack.REQUIRED_PLATFORMS),
        help="comma-separated os/arch set every image must carry",
    )
    parser.add_argument("--resolve-charts", action="store_true", help="resolve operator appVersions with helm show chart (network)")
    parser.add_argument(
        "--third-party-only",
        action="store_true",
        help="skip the DFE images under digests:, which dfe-stack release-gate checks",
    )
    parser.add_argument("--image", action="append", help="check only these image references, ignoring versions.yaml")
    parser.add_argument(
        "--verdict-cache",
        type=Path,
        help="JSON file of verdicts an earlier run proved; read first, rewritten with this run's",
    )
    args = parser.parse_args()

    required = {item.strip() for item in args.require.split(",") if item.strip()}
    budget = registry_pins.RetryBudget(RETRY_BUDGET_SECONDS)
    cache = VerdictCache()
    if args.verdict_cache:
        cache, note = load_verdicts(args.verdict_cache)
        if note:
            print(note)
    try:
        failures, refs, skipped = _check(args, required, budget, cache)
    finally:
        # Written even when the run fails, so every verdict it did prove is kept.
        if args.verdict_cache:
            save_verdicts(args.verdict_cache, cache)
    print(cache_summary(cache, budget.reads))
    print(f"{len(refs)} image(s) checked, {len(failures)} failure(s), {len(skipped)} skipped")
    if failures:
        print("\nAn image missing an architecture pulls fine and dies at exec on that node.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
