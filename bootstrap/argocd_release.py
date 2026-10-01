#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         argocd_release.py
#  Purpose:      Decide whether the Argo CD running in a cluster is the helm
#                release bootstrap.sh installs, so bootstrap upgrades its own
#                Argo and never reconfigures one it adopted.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Is this cluster's Argo CD the one bootstrap.sh installed?

    python3 bootstrap/argocd_release.py [--cache-service valkey]

Exits 0 when it is, 1 when it is not or helm cannot say, and prints one line
giving the reason either way. bootstrap.sh upgrades the release in place on 0
and leaves Argo alone on 1; `dfe-ops preflight` previews the same decision.

The install is recognised by what it sets, because helm records nothing about
who ran it: release `argocd` in namespace `argocd`, chart `argo-cd`, the bundled
Redis off and externalRedis pointed at the Valkey step [5/7] deploys. A foreign
Argo installed with the stock `helm install argocd argo/argo-cd` carries the
same name and chart but none of that wiring, so it stays adopted.
"""

import argparse
import json
import subprocess
import sys

RELEASE = "argocd"
NAMESPACE = "argocd"
CHART = "argo-cd"
# bootstrap.sh's VALKEY_SVC default, which DFE_VALKEY_SERVICE overrides.
DEFAULT_CACHE_SERVICE = "valkey"


def cache_host(service: str) -> str:
    """The externalRedis.host bootstrap.sh installs Argo with."""
    return f"{service}.{NAMESPACE}.svc.cluster.local"


def chart_name(chart: str) -> str:
    """`argo-cd-10.9.0` -> `argo-cd`: helm list joins the name and version with a dash."""
    name, _, version = chart.rpartition("-")
    while name and not version[:1].isdigit():
        name, _, version = name.rpartition("-")
    return name or chart


def verdict(releases: list[dict], values: dict | None, cache_service: str) -> tuple[bool, str]:
    """(ours, why) from `helm list -o json` and `helm get values -o json`.

    Args:
        releases: Every release helm lists in the Argo namespace.
        values: The user-supplied values of the `argocd` release; None when it has none.
        cache_service: The Valkey Service this bootstrap wires Argo to.

    Returns:
        Whether bootstrap.sh installed this release, and the one-line reason.
    """
    release = next((r for r in releases if r.get("name") == RELEASE), None)
    if release is None:
        return False, f"no helm release {RELEASE} in namespace {NAMESPACE}"
    chart = str(release.get("chart", ""))
    if chart_name(chart) != CHART:
        return False, f"helm release {NAMESPACE}/{RELEASE} runs chart {chart or '<unknown>'}, not {CHART}"
    values = values or {}
    redis = values.get("redis") or {}
    host = (values.get("externalRedis") or {}).get("host", "")
    want = cache_host(cache_service)
    if redis.get("enabled") is not False or host != want:
        return False, (
            f"helm release {NAMESPACE}/{RELEASE} ({chart}) is not wired to {want} with the "
            "bundled Redis off, so this bootstrap did not install it"
        )
    return True, f"helm release {NAMESPACE}/{RELEASE} ({chart}) is wired to {want}, as this bootstrap installs it"


def _helm_json(argv: list[str]) -> tuple[object, str]:
    """(parsed output, error) for one read-only helm call."""
    try:
        done = subprocess.run(
            ["helm", *argv, "-o", "json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        return None, "helm is not on PATH"
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines()
        return None, lines[-1] if lines else f"helm {argv[0]} exited {done.returncode}"
    try:
        return json.loads(done.stdout or "null"), ""
    except json.JSONDecodeError as exc:
        return None, f"helm {argv[0]} printed no JSON: {exc}"


def read(cache_service: str, kubeconfig: str = "", context: str = "") -> tuple[bool, str]:
    """verdict() against the cluster helm is pointed at. Reads only."""
    scope = ["--namespace", NAMESPACE]
    if kubeconfig:
        scope += ["--kubeconfig", kubeconfig]
    if context:
        scope += ["--kube-context", context]
    releases, error = _helm_json(["list", *scope, "--filter", f"^{RELEASE}$"])
    if error:
        return False, f"cannot list helm releases in {NAMESPACE}: {error}"
    if not isinstance(releases, list) or not any(
        isinstance(r, dict) and r.get("name") == RELEASE for r in releases
    ):
        return verdict([], None, cache_service)
    values, error = _helm_json(["get", "values", RELEASE, *scope])
    if error:
        return False, f"cannot read the values of helm release {NAMESPACE}/{RELEASE}: {error}"
    return verdict(releases, values if isinstance(values, dict) else None, cache_service)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--cache-service",
        default=DEFAULT_CACHE_SERVICE,
        help="the Valkey Service in the argocd namespace this bootstrap wires Argo to",
    )
    parser.add_argument("--kubeconfig", default="", help="kubeconfig helm reads (default: helm's own)")
    parser.add_argument("--kube-context", default="", help="kubeconfig context helm reads")
    args = parser.parse_args()
    ours, why = read(args.cache_service, args.kubeconfig, args.kube_context)
    print(f"  [argocd] {why}")
    return 0 if ours else 1


if __name__ == "__main__":
    sys.exit(main())
