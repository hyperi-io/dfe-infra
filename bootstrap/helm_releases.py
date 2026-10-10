#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         helm_releases.py
#  Purpose:      The helm releases bootstrap.sh installs and the values each
#                install sets, read by bootstrap.sh and by `dfe-ops upgrade`,
#                so an install and the upgrade dfe-ops prints set the same values.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The helm releases bootstrap.sh installs, and the values each install sets.

    python3 bootstrap/helm_releases.py args cert-manager
    python3 bootstrap/helm_releases.py args argocd --domain slim.dfe.example.com --cache-service valkey

`args` prints one install's value arguments, one per line, which bootstrap.sh
splices into its own `helm upgrade --install`. `dfe-ops upgrade apply` prints
the same arguments after `--reset-values` in the upgrade it hands an operator,
so the two cannot set different values. Exit 2 when a value needs the
deployment's domain and none was given.
"""

import argparse
import sys
from dataclasses import dataclass

import argocd_release


@dataclass(frozen=True, slots=True)
class HelmRelease:
    """One release bootstrap.sh installs with helm.

    Attributes:
        release: The helm release name.
        namespace: The namespace the release lives in.
        chart: The chart name, also the prefix of its `helm.sh/chart` label.
        repo: The chart repository URL bootstrap.sh adds.
        deployment: The Deployment bootstrap.sh's detect-or-install gate reads,
            whose `helm.sh/chart` label names the chart it runs.
        timeout: The --timeout bootstrap.sh gives the install.
        values: Each value the install sets, as (helm flag, `path=value`);
            `{domain}` and `{cache_host}` are filled from the deployment.
        login_values: The install reads Argo's login from argocd_login.py on stdin.
    """

    release: str
    namespace: str
    chart: str
    repo: str
    deployment: str
    timeout: str
    values: tuple[tuple[str, str], ...] = ()
    login_values: bool = False


EXTERNAL_SECRETS = HelmRelease(
    release="external-secrets",
    namespace="external-secrets",
    chart="external-secrets",
    repo="https://charts.external-secrets.io",
    deployment="external-secrets",
    timeout="5m",
)

CERT_MANAGER = HelmRelease(
    release="cert-manager",
    namespace="cert-manager",
    chart="cert-manager",
    repo="https://charts.jetstack.io",
    deployment="cert-manager",
    timeout="5m",
    values=(
        ("--set", "crds.enabled=true"),
        # The gateway-shim issues the dfe-wildcard-tls Secret the https listener needs to program.
        ("--set", "config.enableGatewayAPI=true"),
    ),
)

ARGOCD = HelmRelease(
    release=argocd_release.RELEASE,
    namespace=argocd_release.NAMESPACE,
    chart=argocd_release.CHART,
    repo="https://argoproj.github.io/argo-helm",
    deployment="argocd-server",
    timeout="10m",
    values=(
        # argocd_release.py recognises bootstrap's own release by this cache wiring.
        ("--set", "redis.enabled=false"),
        ("--set", "externalRedis.host={cache_host}"),
        ("--set", "externalRedis.port=6379"),
        # argocd-cm's url, on the label the gateway publishes Argo under (hostnames.argocd).
        ("--set-string", "global.domain=argocd.{domain}"),
        # The gateway terminates TLS, and a TLS-serving argocd-server redirects plain HTTP to itself.
        ("--set-string", r"configs.params.server\.insecure=true"),
        ("--set-string", r"configs.params.reposerver\.disable\.git\.modules=true"),
        # Backed-off controller timers, so one degraded app cannot hold the control plane.
        ("--set-string", r"configs.cm.timeout\.reconciliation=300s"),
        ("--set-string", r"configs.params.controller\.self\.heal\.timeout\.seconds=30"),
        ("--set-string", r"configs.params.controller\.repo\.server\.timeout\.seconds=60"),
        ("--set-string", r"configs.params.controller\.diff\.server\.side=true"),
    ),
    login_values=True,
)

RELEASES: dict[str, HelmRelease] = {r.release: r for r in (EXTERNAL_SECRETS, CERT_MANAGER, ARGOCD)}


def needs_domain(release: HelmRelease) -> bool:
    """Whether a value of the install names the deployment's domain."""
    return any("{domain}" in pair for _flag, pair in release.values)


def install_args(
    release: HelmRelease, *, domain: str = "", cache_service: str = argocd_release.DEFAULT_CACHE_SERVICE
) -> list[str]:
    """The value arguments one install passes helm, in the order bootstrap.sh passes them.

    Args:
        release: The release being installed.
        domain: The deployment's domain (DFE_DOMAIN).
        cache_service: The Valkey Service in the Argo namespace (DFE_VALKEY_SERVICE).

    Returns:
        Each `--set`/`--set-string` flag followed by its `path=value`, then
        `--values -` where the install reads Argo's login on stdin.

    Raises:
        ValueError: A value names the domain and `domain` is empty.
    """
    if needs_domain(release) and not domain:
        raise ValueError(f"{release.release} sets a value from the deployment's domain, and no --domain was given")
    fields = {"domain": domain, "cache_host": argocd_release.cache_host(cache_service)}
    args: list[str] = []
    for flag, pair in release.values:
        args += [flag, pair.format_map(fields)]
    if release.login_values:
        args += ["--values", "-"]
    return args


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True, metavar="<action>")
    listed = sub.add_parser("args", help="print one install's value arguments, one per line")
    listed.add_argument("release", choices=sorted(RELEASES), help="the helm release bootstrap.sh installs")
    listed.add_argument("--domain", default="", help="the deployment's domain (DFE_DOMAIN)")
    listed.add_argument(
        "--cache-service",
        default=argocd_release.DEFAULT_CACHE_SERVICE,
        help="the Valkey Service in the argocd namespace Argo is wired to (DFE_VALKEY_SERVICE)",
    )
    args = parser.parse_args()
    try:
        values = install_args(RELEASES[args.release], domain=args.domain, cache_service=args.cache_service)
    except ValueError as exc:
        print(f"helm_releases.py: {exc}", file=sys.stderr)
        return 2
    for value in values:
        print(value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
