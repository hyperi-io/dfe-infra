#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/tests/test_registry_cache_upstreams.py
#  Purpose:      Hold the registry cache's upstream list to every registry host
#                the stack's images come from.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every image host the stack pulls is cached, or named as one ECR cannot cache.

A host the cache does not know is pulled straight from its upstream, rate limits
and all, and nothing at deploy time says so. The image tables are the ones
scripts/check_image_arch.py reads for the multi-arch gate; no network.

    python3 -m pytest scripts/tests/test_registry_cache_upstreams.py -q
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_image_arch as arch  # noqa: E402
import render_dial  # noqa: E402

# Hosts ECR's pull-through cache has no rule type for, so their images always come from upstream.
UNCACHEABLE = {
    "code.forgejo.org": "the bundled deploy repo's Forgejo, deployed only when no git server is supplied",
    "docker.redpanda.com": "the Redpanda broker and operator, deployed only on kafka.provider redpanda",
}


def _host(reference: str) -> str:
    """The registry host an image reference resolves to, docker.io for a bare one."""
    first, _, rest = reference.partition("/")
    return first if rest and ("." in first or ":" in first) else "docker.io"


def _stack_hosts() -> set[str]:
    repositories = [arch.dfe_stack.DEFAULT_REGISTRY + "/dfe-engine", arch.STRIMZI_KAFKA_IMAGE]
    repositories += [repository for _key, _var, repository in arch.dfe_stack._DOCKER_THIRDPARTY]
    repositories += list(arch.DIGEST_PINNED.values())
    repositories += [
        repo for repos, _prefix in arch.OPERATOR_TAG_IS_CHART.values() for repo in repos
    ]
    repositories += [
        repo for _chart, _url, repos, _prefix in arch.OPERATOR_VIA_CHART.values() for repo in repos
    ]
    return {_host(repository) for repository in repositories}


def test_every_stack_image_host_is_cached_or_named_uncacheable() -> None:
    cached = {host for _url, host, _needs in render_dial._REGISTRY_CACHE_UPSTREAMS.values()}
    missing = _stack_hosts() - cached - set(UNCACHEABLE)
    assert missing == set(), f"images from {sorted(missing)} would bypass the registry cache"


def test_every_cached_host_is_one_the_stack_pulls_from() -> None:
    """An upstream nothing pulls from is a rule and a credential for no image."""
    cached = {host for _url, host, _needs in render_dial._REGISTRY_CACHE_UPSTREAMS.values()}
    assert cached - _stack_hosts() == set()


def test_the_host_of_a_bare_reference_is_docker_hub() -> None:
    assert _host("valkey/valkey") == "docker.io"
    assert _host("busybox") == "docker.io"
    assert _host("quay.io/argoproj/argocd") == "quay.io"
