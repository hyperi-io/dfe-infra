#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/tests/test_registry_cache_upstreams.py
#  Purpose:      Hold the registry cache's upstream list to every registry host
#                the stack's images come from.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every image host the stack pulls is cached, cacheable with a credential, or uncacheable.

A host the cache does not know is pulled straight from its upstream, rate limits
and all, and nothing at deploy time says so. The inventory is the image tables
scripts/check_image_arch.py reads for the multi-arch gate, every image a chart
renders (test_pod_spec_invariants' render set), and the Kafka client pod the
integration smoke test and the cycle diagnostics apply beside dfe-engine. It reads
no network, but the chart renders need `helm` on PATH.

    python3 -m pytest scripts/tests/test_registry_cache_upstreams.py -q
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_image_arch as arch  # noqa: E402
import dfe_ops_diagnostics  # noqa: E402
import render_dial  # noqa: E402
import test_pod_spec_invariants as pod_specs  # noqa: E402
import test_smoke_kafka_client_image as smoke_client  # noqa: E402

# Hosts ECR's pull-through cache has no rule type for, so their images always come from upstream.
UNCACHEABLE = {
    "code.forgejo.org": "the bundled deploy repo's Forgejo, deployed only when no git server is supplied",
    "docker.redpanda.com": "the Redpanda broker and operator, deployed only on kafka.provider redpanda",
}

CONTAINER_LISTS = ("containers", "initContainers")


def _host(reference: str) -> str:
    """The registry host an image reference resolves to, docker.io for a bare one."""
    first, _, rest = reference.partition("/")
    return first if rest and ("." in first or ":" in first) else "docker.io"


def _repository(reference: str) -> str:
    """An image reference without its tag or digest."""
    name = reference.split("@", 1)[0]
    head, slash, last = name.rpartition("/")
    return head + slash + last.split(":", 1)[0]


def _container_images(node: object) -> list[str]:
    """The image of every container in a containers or initContainers list under node."""
    found: list[str] = []
    if isinstance(node, list):
        for item in node:
            found += _container_images(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in CONTAINER_LISTS and isinstance(value, list):
                found += [c["image"] for c in value if isinstance(c, dict) and c.get("image")]
            found += _container_images(value)
    return found


def _rendered_images() -> list[str]:
    """Each image reference a chart renders a container from, the deployment's DFE images aside."""
    # A DFE image renders with no host until the appset hands the chart the dial's registry.
    dfe_images = set(arch.dfe_stack.versions_at().get("digests", {}))
    covered, _skipped = pod_specs.rendered()
    found = []
    for _chart, docs in covered:
        found += _container_images(list(docs))
    return [image for image in found if _repository(image) not in dfe_images]


def _rendered_repositories() -> list[str]:
    """Each repository a chart renders a container from, the deployment's DFE images aside."""
    return [_repository(image) for image in _rendered_images()]


def _client_pod_repositories() -> list[str]:
    """The repository each of the two managed-broker Kafka client pods runs."""
    values = smoke_client.KAFKA_VALUES.read_text(encoding="utf-8")
    smoke = smoke_client._image(smoke_client.KAFKA_VALUES).stdout.strip()
    return [_repository(smoke), _repository(dfe_ops_diagnostics.kafka_image({}, values))]


def _stack_repositories() -> list[str]:
    repositories = [arch.dfe_stack.DEFAULT_REGISTRY + "/dfe-engine", arch.STRIMZI_KAFKA_IMAGE]
    repositories += [repository for _key, _var, repository in arch.dfe_stack._DOCKER_THIRDPARTY]
    repositories += list(arch.DIGEST_PINNED.values())
    repositories += [
        repo for repos, _prefix in arch.OPERATOR_TAG_IS_CHART.values() for repo in repos
    ]
    repositories += [
        repo for _chart, _url, repos, _prefix in arch.OPERATOR_VIA_CHART.values() for repo in repos
    ]
    repositories += _rendered_repositories()
    repositories += _client_pod_repositories()
    return repositories


def _docker_official_on_docker_hub(repositories: list[str]) -> list[str]:
    """Each repository naming Docker Hub's library/ path, bare or spelled out."""
    return [
        repository
        for repository in repositories
        if _host(repository) == "docker.io"
        and "/" not in repository.removeprefix("docker.io/").removeprefix("library/")
    ]


def _stack_hosts() -> set[str]:
    return {_host(repository) for repository in _stack_repositories()}


def _hosts(table: dict[str, tuple[str, str]]) -> set[str]:
    return {host for _url, host in table.values()}


def test_every_stack_image_host_is_cached_cacheable_or_named_uncacheable() -> None:
    known = (
        _hosts(render_dial._REGISTRY_CACHE_UPSTREAMS)
        | _hosts(render_dial._REGISTRY_CACHE_CREDENTIALED)
        | set(UNCACHEABLE)
    )
    missing = _stack_hosts() - known
    assert missing == set(), f"images from {sorted(missing)} would bypass the registry cache"


def test_every_cached_host_is_one_the_stack_pulls_from() -> None:
    """An upstream nothing pulls from is a rule, and maybe a credential, for no image."""
    cached = _hosts(render_dial._REGISTRY_CACHE_UPSTREAMS) | _hosts(
        render_dial._REGISTRY_CACHE_CREDENTIALED
    )
    assert cached - _stack_hosts() == set()


def test_the_default_cache_takes_no_upstream_that_needs_a_credential() -> None:
    """ECR refuses a docker.io or ghcr.io rule without a Secrets Manager credential."""
    default = _hosts(render_dial._REGISTRY_CACHE_UPSTREAMS)
    assert default == {"quay.io", "registry.k8s.io", "public.ecr.aws"}
    assert default.isdisjoint(_hosts(render_dial._REGISTRY_CACHE_CREDENTIALED))


def test_no_stack_image_is_a_docker_official_image_on_docker_hub() -> None:
    """A Docker Official Image comes from its public.ecr.aws mirror, which the default cache covers."""
    on_docker_hub = _docker_official_on_docker_hub(_stack_repositories())
    assert on_docker_hub == [], (
        f"{on_docker_hub} name Docker Hub's library/ path: read each from "
        "public.ecr.aws/docker/library/<name> once that mirror carries the same tag and digest"
    )


def test_expected_fail_a_docker_official_image_read_from_docker_hub_is_caught() -> None:
    repositories = [
        "busybox",
        "docker.io/library/busybox",
        "library/busybox",
        "public.ecr.aws/docker/library/busybox",
        "apache/kafka",
    ]
    assert _docker_official_on_docker_hub(repositories) == [
        "busybox",
        "docker.io/library/busybox",
        "library/busybox",
    ]


def test_the_inventory_reaches_every_chart_render_and_both_client_pods() -> None:
    """A render or reader that stopped contributing would leave the checks above passing on less."""
    rendered = _rendered_repositories()
    assert "otel/opentelemetry-collector-contrib" in rendered
    assert "apache/kafka" in rendered
    assert not [repository for repository in rendered if repository.startswith("dfe-")]
    assert _client_pod_repositories() == ["apache/kafka", "apache/kafka"]


def test_an_image_reference_loses_its_tag_and_digest_but_keeps_its_host() -> None:
    assert _repository("busybox:1.38.0@sha256:" + "0" * 64) == "busybox"
    assert _repository("localhost:5000/team/app:v1") == "localhost:5000/team/app"
    assert _repository("public.ecr.aws/docker/library/busybox:1.38.0") == (
        "public.ecr.aws/docker/library/busybox"
    )


def test_the_host_of_a_bare_reference_is_docker_hub() -> None:
    assert _host("valkey/valkey") == "docker.io"
    assert _host("busybox") == "docker.io"
    assert _host("quay.io/argoproj/argocd") == "quay.io"
