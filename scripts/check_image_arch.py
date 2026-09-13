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

"""The multi-arch gate.

Cloud nodes are ARM by default and on-prem nodes are x86_64 most of the time,
so an image missing either architecture pulls fine and dies at container
start with `exec format error`, after the cluster looks healthy. This check
runs `docker manifest inspect` over every image the CURRENT stack block of
versions.yaml pins and fails on any that lacks a platform in the required set.

Three image classes are checked:

- DFE apps from `apps:` (ghcr.io/hyperi-io/<app>:<tag>); an app with no digest
  is declared NOT YET PUBLISHED in versions.yaml and is reported, not failed.
- Third-party services from `services:` through the same key-to-repository
  table `dfe-stack` renders the docker path from, plus the k8s-only images
  listed in K8S_ONLY below.
- Operator images whose chart version IS the app version (strimzi, the
  redpanda operator, envoy-gateway). Operators whose chart resolves a
  different appVersion are resolved with `helm show chart` only when
  --resolve-charts is given, because that needs network and a chart repo.

Network-dependent by design, so it is NOT part of the offline drift check:
run it when pins change and in the CI job that gates a stack cut.

Usage:
    python3 scripts/check_image_arch.py [--stack VER] [--require linux/amd64,linux/arm64]
                                        [--resolve-charts] [--image REF ...]
Requires `docker` on PATH with registry access for the images it inspects.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from resolve_pins import load_stack  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DFE_REGISTRY = "ghcr.io/hyperi-io"
DEFAULT_REQUIRE = ("linux/amd64", "linux/arm64")

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


def platforms_of(manifest_json: str) -> set[str]:
    """The `os/arch` set a `docker manifest inspect` document advertises.

    An OCI index lists one entry per platform; attestation entries report
    `unknown/unknown` and are dropped. A single-manifest image carries no
    `manifests` list, so the caller re-runs with `-v` and reads the platform
    from the verbose document instead.
    """
    doc = json.loads(manifest_json)
    found: set[str] = set()
    entries = doc.get("manifests") if isinstance(doc, dict) else None
    if isinstance(entries, list):
        for entry in entries:
            plat = entry.get("platform") or {}
            os_name, arch = plat.get("os"), plat.get("architecture")
            if os_name and arch and os_name != "unknown" and arch != "unknown":
                found.add(f"{os_name}/{arch}")
        return found
    # docker manifest inspect -v on a single manifest returns one object (or a
    # list of one) carrying Descriptor.platform.
    docs = doc if isinstance(doc, list) else [doc]
    for item in docs:
        plat = (item.get("Descriptor") or {}).get("platform") or {}
        os_name, arch = plat.get("os"), plat.get("architecture")
        if os_name and arch:
            found.add(f"{os_name}/{arch}")
    return found


def inspect(ref: str) -> set[str]:
    """Platforms for one image reference, via the docker CLI."""
    first = subprocess.run(["docker", "manifest", "inspect", ref], capture_output=True, text=True)
    if first.returncode != 0:
        raise RuntimeError(first.stderr.strip() or f"docker manifest inspect {ref} failed")
    found = platforms_of(first.stdout)
    if found:
        return found
    verbose = subprocess.run(["docker", "manifest", "inspect", "-v", ref], capture_output=True, text=True)
    if verbose.returncode != 0:
        raise RuntimeError(verbose.stderr.strip() or f"docker manifest inspect -v {ref} failed")
    return platforms_of(verbose.stdout)


def chart_app_version(chart_ref: str, repo_url: str | None, chart_version: str) -> str:
    """appVersion of a chart at a version, from `helm show chart`.

    A classic chart repo is passed with --repo so nothing has to be added to
    the local helm state; an OCI reference needs no repo.
    """
    cmd = ["helm", "show", "chart", chart_ref, "--version", chart_version]
    if repo_url:
        cmd += ["--repo", repo_url]
    result = subprocess.run(cmd, capture_output=True, text=True)
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


def image_refs(stack_map: object, resolve_charts: bool) -> tuple[list[tuple[str, str]], list[str]]:
    """(label, reference) pairs to inspect, and the refs skipped with a reason."""
    refs: list[tuple[str, str]] = []
    skipped: list[str] = []

    # dfe-hyperdx is pinned under content:, the rest under apps:; both are
    # ghcr.io/hyperi-io images with their digest under digests:.
    apps = {**_section(stack_map, "content"), **_section(stack_map, "apps")}
    digests = _section(stack_map, "digests")
    for app, tag in sorted(apps.items()):
        if app not in digests:
            skipped.append(f"{app}: no digest in versions.yaml (NOT YET PUBLISHED)")
            continue
        refs.append((f"apps.{app}", f"{DFE_REGISTRY}/{app}:{tag}"))

    services = _section(stack_map, "services")
    thirdparty = {key: repo for key, _var, repo in _load_dfe_stack()._DOCKER_THIRDPARTY}
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
            refs.append((f"operators.{key}", f"{repo}:{chart_app_version(chart_ref, repo_url, chart_version)}"))
        except RuntimeError as err:
            skipped.append(f"operators.{key}: chart {chart_version} did not resolve ({err}); UNCHECKED")
    return refs, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stack", help="stack version to check (default: the `current` pointer)")
    parser.add_argument("--require", default=",".join(DEFAULT_REQUIRE), help="comma-separated os/arch set every image must carry")
    parser.add_argument("--resolve-charts", action="store_true", help="resolve operator appVersions with helm show chart (network)")
    parser.add_argument("--image", action="append", help="check only these image references, ignoring versions.yaml")
    args = parser.parse_args()

    required = {item.strip() for item in args.require.split(",") if item.strip()}
    if args.image:
        refs = [(ref, ref) for ref in args.image]
        skipped: list[str] = []
        stack = "(explicit)"
    else:
        _, _, stack, body = load_stack(args.stack)
        refs, skipped = image_refs(body, args.resolve_charts)
    print(f"checking stack {stack} for {sorted(required)}")

    failures: list[str] = []
    for label, ref in refs:
        try:
            found = inspect(ref)
        except RuntimeError as err:
            failures.append(f"{label} {ref}: {err}")
            print(f"  ERROR  {label} {ref}: {err}")
            continue
        missing = sorted(required - found)
        if missing:
            failures.append(f"{label} {ref}: missing {missing}")
            print(f"  FAIL   {label} {ref}: missing {missing} (has {sorted(found)})")
        else:
            print(f"  ok     {label} {ref}")

    for note in skipped:
        print(f"  skip   {note}")
    print(f"{len(refs)} image(s) checked, {len(failures)} failure(s), {len(skipped)} skipped")
    if failures:
        print("\nAn image missing an architecture pulls fine and dies at exec on that node.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
