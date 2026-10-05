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
  reported, not failed. A private package needs a GHCR credential in the
  docker credential store.
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
  needs helm and the chart repos. One chart can run several images.
- Images a pinned chart runs at its OWN values' defaults, with no pin of ours to
  tag them (CHART_DEFAULT_IMAGES): read from the chart archive at the pinned
  version, a subchart from the copy the parent archive vendors, again only with
  --resolve-charts.
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

Usage:
    python3 scripts/check_image_arch.py [--stack VER] [--require linux/amd64,linux/arm64]
                                        [--resolve-charts] [--third-party-only]
                                        [--image REF ...]
Requires `docker` with the buildx plugin on PATH.
"""

import argparse
import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry_pins

REPO_ROOT = Path(__file__).resolve().parent.parent

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
    "karpenter": (
        "oci://public.ecr.aws/karpenter/karpenter",
        None,
        ("public.ecr.aws/karpenter/controller",),
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
# to that chart's appVersion the way its template does). Read with
# --resolve-charts.
CHART_DEFAULT_IMAGES = {
    # Runs whenever no OIDC provider fronts Argo CD (bootstrap/argocd_login.py).
    "dex": ("argocd", "", ("dex", "image"), False),
    # The frr-k8s subchart is on by default (frrk8s.enabled) and runs both.
    "frr-k8s": ("metallb", "frr-k8s", ("frrk8s", "image"), True),
    "frr": ("metallb", "frr-k8s", ("frrk8s", "frr", "image"), True),
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
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# The DFE image list, the stack reader and the required platform set all come
# from here, so this checker and `dfe-stack platforms` cannot disagree on them.
dfe_stack = _load_dfe_stack()


def chart_app_version(chart_ref: str, repo_url: str | None, chart_version: str) -> str:
    """appVersion of a chart at a version, from `helm show chart`.

    A classic chart repo is passed with --repo so nothing has to be added to
    the local helm state; an OCI reference needs no repo.
    """
    cmd = ["helm", "show", "chart", chart_ref, "--version", chart_version]
    if repo_url:
        cmd += ["--repo", repo_url]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
    except OSError as exc:
        raise RuntimeError(f"cannot run helm: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"helm show chart {chart_ref} failed")
    for line in result.stdout.splitlines():
        if line.startswith("appVersion:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    raise RuntimeError(f"{chart_ref} {chart_version}: no appVersion in the chart")


def pull_chart(chart_ref: str, repo_url: str | None, chart_version: str, into: Path) -> Path:
    """Untar one chart version into an empty directory and return the chart's own.

    Raises:
        RuntimeError: helm is missing, the pull failed, or it left anything but
            one chart directory behind.
    """
    cmd = ["helm", "pull", chart_ref, "--version", chart_version]
    cmd += ["--untar", "--untardir", str(into)]
    if repo_url:
        cmd += ["--repo", repo_url]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
    except OSError as exc:
        raise RuntimeError(f"cannot run helm: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"helm pull {chart_ref} failed")
    charts = [path for path in into.iterdir() if path.is_dir()]
    if len(charts) != 1:
        raise RuntimeError(f"helm pull {chart_ref} left {len(charts)} directories, expected 1")
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


def default_image_ref(layers: list[dict], path: tuple[str, ...], app_version: str | None) -> str:
    """repository:tag for the image block at `path` in a chart's parsed values.

    Args:
        layers: Parsed values, highest precedence first -- a parent chart's block
            for its subchart, then the subchart's own values.
        path: Keys from the values root down to the block holding
            `repository` and `tag`.
        app_version: The chart's appVersion, which an unset tag falls back to,
            or None where the chart's template has no such fallback.

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
    return f"{repository}:{tag}"


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


def chart_dir_refs(chart: Path, images: list[tuple[str, str, tuple[str, ...], bool]]) -> list[str]:
    """The reference an untarred chart renders for each of its default images.

    Args:
        chart: The chart's directory, as `helm pull --untar` leaves it.
        images: (name, subchart, values path, tag falls back to appVersion)
            per image, as CHART_DEFAULT_IMAGES holds them.

    Returns:
        One image reference per entry of `images`, in order.

    Raises:
        RuntimeError: The chart vendors no such subchart, or its values do not
            name the image an entry expects.
    """
    refs: list[str] = []
    parent = _parsed(chart / "values.yaml")
    for name, subchart, path, falls_back in images:
        source = chart / "charts" / subchart if subchart else chart
        if not source.is_dir():
            raise RuntimeError(f"{name}: the chart vendors no {subchart} subchart")
        layers = [parent]
        if subchart:
            layers = [_block(parent, (subchart,)), _parsed(source / "values.yaml")]
        app_version = _parsed(source / "Chart.yaml").get("appVersion") if falls_back else None
        fallback = app_version if isinstance(app_version, str) else None
        try:
            refs.append(default_image_ref(layers, path, fallback))
        except ValueError as err:
            raise RuntimeError(f"{name}: {err}") from err
    return refs


def chart_default_refs(
    chart_ref: str,
    repo_url: str | None,
    chart_version: str,
    images: list[tuple[str, str, tuple[str, ...], bool]],
) -> list[str]:
    """chart_dir_refs over a chart version pulled into a scratch directory.

    The archive is read rather than `helm show values`, because a subchart's
    values and appVersion ship inside the parent, at the version it vendors.

    Raises:
        RuntimeError: The pull failed, or chart_dir_refs did.
    """
    with tempfile.TemporaryDirectory(prefix="check-image-arch-") as scratch:
        return chart_dir_refs(pull_chart(chart_ref, repo_url, chart_version, Path(scratch)), images)


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


def image_refs(
    stack_map: dict, resolve_charts: bool, third_party_only: bool = False
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """(label, reference) pairs to read, notes on what was skipped, and pins that
    cannot be read at all -- the last fail the run, because an unread image is
    not a multi-arch one."""
    refs: list[tuple[str, str]] = []
    skipped: list[str] = []
    broken: list[str] = []

    dfe = dfe_stack.app_images(stack_map, dfe_stack.DEFAULT_REGISTRY)
    if third_party_only:
        skipped.append(
            f"{len(dfe)} DFE image(s) under digests: (--third-party-only; "
            "`dfe-stack platforms` reads them with a GHCR credential)"
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
    for key, (chart_ref, repo_url, repos, prefix) in sorted(OPERATOR_VIA_CHART.items()):
        label, chart_version = _chart_pin(pins, key)
        if not chart_version:
            continue
        if not resolve_charts:
            skipped.append(
                f"{label}: chart {chart_version} resolves its own appVersion; pass --resolve-charts"
            )
            continue
        try:
            app_version = chart_app_version(chart_ref, repo_url, chart_version)
        except RuntimeError as err:
            broken.append(f"{label}: chart {chart_version} did not resolve ({err})")
            continue
        refs += [(label, f"{repo}:{prefixed(app_version, prefix)}") for repo in repos]

    by_chart: dict[str, list[tuple[str, str, tuple[str, ...], bool]]] = {}
    for name, (key, subchart, path, falls_back) in CHART_DEFAULT_IMAGES.items():
        by_chart.setdefault(key, []).append((name, subchart, path, falls_back))
    for key, images in sorted(by_chart.items()):
        label, chart_version = _chart_pin(pins, key)
        if not chart_version:
            continue
        names = ", ".join(name for name, *_ in images)
        if not resolve_charts:
            note = f"sets {names} in its own values; pass --resolve-charts"
            skipped.append(f"{label}: chart {chart_version} {note}")
            continue
        chart_ref, repo_url, _repos, _prefix = OPERATOR_VIA_CHART[key]
        try:
            defaults = chart_default_refs(chart_ref, repo_url, chart_version, images)
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
        help="skip the DFE images under digests:, which need a GHCR credential; every other image is public",
    )
    parser.add_argument("--image", action="append", help="check only these image references, ignoring versions.yaml")
    args = parser.parse_args()

    required = {item.strip() for item in args.require.split(",") if item.strip()}
    if args.image:
        refs = [(ref, ref) for ref in args.image]
        skipped: list[str] = []
        broken: list[str] = []
        stack = "(explicit)"
    else:
        stack, body = dfe_stack.stack_pins(dfe_stack.load_root(), args.stack)
        refs, skipped, broken = image_refs(body, args.resolve_charts, args.third_party_only)
    print(f"checking stack {stack} for {sorted(required)}")

    failures = list(broken)
    for note in broken:
        print(f"  ERROR  {note}")
    for label, ref in refs:
        found, err = registry_pins.ref_platforms(ref)
        status, detail = verdict(found, err, required)
        if status == "ok":
            print(f"  ok     {label} {ref}")
            continue
        failures.append(f"{label} {ref}: {detail}")
        print(f"  {status:<6} {label} {ref}: {detail}")

    for note in skipped:
        print(f"  skip   {note}")
    print(f"{len(refs)} image(s) checked, {len(failures)} failure(s), {len(skipped)} skipped")
    if failures:
        print("\nAn image missing an architecture pulls fine and dies at exec on that node.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
