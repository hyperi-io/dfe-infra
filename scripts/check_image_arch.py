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
- Third-party images a pin tags directly: `services:` through the same
  key-to-repository table `dfe-stack` renders the docker path from, plus the
  k8s-path images listed in K8S_ONLY below, which include the plain manifests
  bootstrap.sh applies.
- Images installed by a chart. Where the chart version IS the image tag
  (cert-manager, external-secrets, strimzi, the redpanda operator,
  envoy-gateway) no lookup is needed. Every other chart's appVersion is read
  with `helm show chart` only when --resolve-charts is given, because that
  needs helm and the chart repos. One chart can run several images.
- Strimzi's Kafka broker image, whose tag joins the operator and Kafka pins.

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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry_pins

REPO_ROOT = Path(__file__).resolve().parent.parent

# Images the k8s path pulls that dfe-stack's docker table does not name, each
# tagged by its pin as it stands: versions.yaml section.key -> image repository.
K8S_ONLY = {
    "services.nginx-unprivileged": "nginxinc/nginx-unprivileged",
    "services.git-sync": "registry.k8s.io/git-sync/git-sync",
    "services.forgejo": "code.forgejo.org/forgejo/forgejo",
    # helm/charts/clickhouse-cluster runs Keeper on the server's own tag.
    "services.clickhouse-version": "docker.io/clickhouse/clickhouse-keeper",
    # The image bootstrap/templates/valkey.yaml runs, which the drift check holds to this pin.
    "bootstrap.valkey": "valkey/valkey",
    # The image upstream's deploy/local-path-storage.yaml names at the pinned git tag.
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
    "cloudnative-pg": (
        "oci://ghcr.io/cloudnative-pg/charts/cloudnative-pg",
        None,
        ("ghcr.io/cloudnative-pg/cloudnative-pg",),
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
    tagged = [(f"services.{key}", repo) for key, _var, repo in dfe_stack._DOCKER_THIRDPARTY]
    tagged += K8S_ONLY.items()
    for path, repo in sorted(tagged):
        tag = pins.get(path)
        if tag:
            refs.append((path, f"{repo}:{tag}"))

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
