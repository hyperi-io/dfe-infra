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

Three image classes are checked:

- DFE images: every `digests:` key, read by its pinned digest, from the list
  `dfe-stack images` prints. An app with no digest is NOT YET PUBLISHED and is
  reported, not failed. A private package needs a GHCR credential in the
  docker credential store.
- Third-party services from `services:` through the same key-to-repository
  table `dfe-stack` renders the docker path from, plus the k8s-only images
  listed in K8S_ONLY below.
- Operator images whose chart version IS the app version (strimzi, the
  redpanda operator, envoy-gateway). Operators whose chart resolves a
  different appVersion are resolved with `helm show chart` only when
  --resolve-charts is given, because that needs helm and the chart repos.

The third-party images are public, so --third-party-only reads them with no
credential at all. CI runs it that way on every pin change (helm-lint.yml);
`dfe-stack release-gate` gates the DFE images.

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

# k8s-only third-party images that dfe-stack's docker table does not carry:
# versions.yaml services key -> image repository.
K8S_ONLY = {
    "nginx-unprivileged": "nginxinc/nginx-unprivileged",
    "git-sync": "registry.k8s.io/git-sync/git-sync",
    "forgejo": "code.forgejo.org/forgejo/forgejo",
}

# Operators whose chart version IS the image tag, so no chart lookup is needed:
# versions.yaml key (under bootstrap: or operators:) -> (image repository, the
# tag prefix the registry expects when the pin omits it).
OPERATOR_TAG_IS_CHART = {
    "cert-manager": ("quay.io/jetstack/cert-manager-controller", "v"),
    "external-secrets": ("ghcr.io/external-secrets/external-secrets", "v"),
    "strimzi-kafka-operator": ("quay.io/strimzi/operator", ""),
    "redpanda-operator": ("docker.redpanda.com/redpandadata/redpanda-operator", "v"),
    "envoy-gateway": ("docker.io/envoyproxy/gateway", ""),
}

# Operators whose chart resolves its own appVersion: key -> (chart reference,
# classic repo URL or None for OCI, image repository). Read with --resolve-charts.
OPERATOR_VIA_CHART = {
    "keda": ("keda", "https://kedacore.github.io/charts", "ghcr.io/kedacore/keda"),
    "clickhouse-operator": ("oci://ghcr.io/clickhouse/clickhouse-operator-helm", None, "ghcr.io/clickhouse/clickhouse-operator"),
    "cloudnative-pg": ("oci://ghcr.io/cloudnative-pg/charts/cloudnative-pg", None, "ghcr.io/cloudnative-pg/cloudnative-pg"),
}


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

    services = _section(stack_map, "services")
    thirdparty = {key: repo for key, _var, repo in dfe_stack._DOCKER_THIRDPARTY}
    thirdparty.update(K8S_ONLY)
    for key, repo in sorted(thirdparty.items()):
        tag = services.get(key)
        if tag:
            refs.append((f"services.{key}", f"{repo}:{tag}"))

    # cert-manager, external-secrets and argocd are pinned under bootstrap:,
    # the rest under operators:; one lookup covers both.
    operators = {**_section(stack_map, "bootstrap"), **_section(stack_map, "operators")}
    for key, (repo, prefix) in sorted(OPERATOR_TAG_IS_CHART.items()):
        tag = operators.get(key)
        if tag:
            full_tag = tag if not prefix or tag.startswith(prefix) else prefix + tag
            refs.append((f"operators.{key}", f"{repo}:{full_tag}"))
    for key, (chart_ref, repo_url, repo) in sorted(OPERATOR_VIA_CHART.items()):
        chart_version = operators.get(key)
        if not chart_version:
            continue
        if not resolve_charts:
            skipped.append(f"operators.{key}: chart {chart_version} resolves its own appVersion; pass --resolve-charts")
            continue
        try:
            app_version = chart_app_version(chart_ref, repo_url, chart_version)
        except RuntimeError as err:
            broken.append(f"operators.{key}: chart {chart_version} did not resolve ({err})")
            continue
        refs.append((f"operators.{key}", f"{repo}:{app_version}"))
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
