#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/tests/test_check_image_arch.py
#  Purpose:      Unit tests for which images the multi-arch gate reads and how
#                it judges each one; no network, no docker.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The registry read itself is registry_pins.ref_platforms, covered in
test_dfe_stack.py. These hold the checker's own half: the image list it builds
from a stack, and the verdict it reaches on what the registry returned.

    python3 scripts/tests/test_check_image_arch.py
"""

import sys
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_image_arch as arch  # noqa: E402

# One of each image class: a published app, an unpublished one, an image tagged
# by a family pin, a digest with no tag, third-party services, the bootstrap
# manifests' images, charts tagged by their version and a chart read for its
# appVersion.
_PINS = {
    "apps": {"an-app": "v1.0.0", "unshipped": "v0.1.0"},
    "toolbox": {"dfe-toolbox": "v2.0.0"},
    "digests": {
        "an-app": "sha256:aaa",
        "dfe-toolbox-base": "sha256:bbb",
        "orphan": "sha256:ccc",
    },
    "services": {
        "kafbat": "v1.5.0",
        "git-sync": "v4.7.1",
        "clickhouse-version": "26.3.32.14",
        "kafka-version": "4.3.1",
    },
    "bootstrap": {
        "cert-manager": "v1.21.2",
        "argocd": "10.9.0",
        "valkey": "8.1.9-alpine",
        "local-path-provisioner": "v0.0.36",
    },
    "operators": {"strimzi-kafka-operator": "1.2.0", "keda": "2.20.1"},
}

# Keys under bootstrap:, operators: and services: that pin something other than
# a container image, so the checker has nothing to read for them.
NOT_AN_IMAGE = {
    "operators.karpenter-al2023-ami": "an EC2 AMI alias for Karpenter's node class",
    "services.postgresql": "the Postgres major; the image is services.documentdb-pg",
    "services.cnpg-cluster-instances": "a replica count",
    "services.kafka-replicas": "a replica count",
    "services.clickhouse-replicas": "a replica count",
    "services.hyperdx": "the upstream version the fork tracks; the image is digests.dfe-hyperdx",
    "services.aws-msk-iam-auth": "a jar the MSK bootstrap Job fetches at run time",
    "services.cruise-control-ui": "a tarball served from the nginx-unprivileged image",
}


def _refs(third_party_only: bool = False) -> tuple[set[tuple[str, str]], list[str], list[str]]:
    refs, skipped, broken = arch.image_refs(_PINS, resolve_charts=False, third_party_only=third_party_only)
    return set(refs), skipped, broken


def test_dfe_images_are_read_by_their_pinned_digest() -> None:
    refs, _, _ = _refs()
    expect(
        "an app is read by tag@digest",
        ("digests.an-app", "ghcr.io/hyperi-io/an-app:v1.0.0@sha256:aaa") in refs,
        f"{refs}",
    )
    expect(
        "the toolbox base image takes the family tag",
        ("digests.dfe-toolbox-base", "ghcr.io/hyperi-io/dfe-toolbox-base:v2.0.0@sha256:bbb") in refs,
        f"{refs}",
    )


def test_an_unpublished_app_is_noted_not_failed() -> None:
    refs, skipped, broken = _refs()
    expect("the unpublished app is not read", not any("unshipped" in r for _, r in refs), f"{refs}")
    expect("it is named in the skip notes", any("apps.unshipped" in s for s in skipped), f"{skipped}")
    expect("and is not a failure", not any("unshipped" in b for b in broken), f"{broken}")


def test_a_digest_with_no_tag_fails() -> None:
    _, _, broken = _refs()
    expect("an untagged digest is a failure, not a silent drop", any("digests.orphan" in b for b in broken), f"{broken}")


def test_third_party_images_come_from_the_shared_tables() -> None:
    refs, _, _ = _refs()
    expect("a docker-path service", ("services.kafbat", "ghcr.io/kafbat/kafka-ui:v1.5.0") in refs, f"{refs}")
    expect(
        "a k8s-only service",
        ("services.git-sync", "registry.k8s.io/git-sync/git-sync:v4.7.1") in refs,
        f"{refs}",
    )
    expect(
        "an operator whose tag is its chart version",
        ("operators.strimzi-kafka-operator", "quay.io/strimzi/operator:1.2.0") in refs,
        f"{refs}",
    )
    expect(
        "a bootstrap chart already carrying its v prefix keeps one, under its own section",
        ("bootstrap.cert-manager", "quay.io/jetstack/cert-manager-controller:v1.21.2") in refs,
        f"{refs}",
    )


def test_bootstrap_manifest_images_take_their_pin_as_the_tag() -> None:
    refs, _, _ = _refs()
    expect("valkey", ("bootstrap.valkey", "valkey/valkey:8.1.9-alpine") in refs, f"{refs}")
    expect(
        "local-path-provisioner",
        ("bootstrap.local-path-provisioner", "docker.io/rancher/local-path-provisioner:v0.0.36") in refs,
        f"{refs}",
    )


def test_one_pin_tags_every_image_its_chart_runs() -> None:
    refs, _, _ = _refs()
    clickhouse = sorted(r for label, r in refs if label == "services.clickhouse-version")
    expect(
        "the ClickHouse pin reads the server and Keeper",
        clickhouse
        == ["clickhouse/clickhouse-server:26.3.32.14", "docker.io/clickhouse/clickhouse-keeper:26.3.32.14"],
        f"{clickhouse}",
    )
    cert_manager = sorted(r for label, r in refs if label == "bootstrap.cert-manager")
    expect(
        "every cert-manager image takes the chart version",
        cert_manager
        == [
            f"quay.io/jetstack/cert-manager-{name}:v1.21.2"
            for name in ("cainjector", "controller", "startupapicheck", "webhook")
        ],
        f"{cert_manager}",
    )


def test_the_strimzi_broker_tag_joins_the_operator_and_kafka_pins() -> None:
    refs, _, _ = _refs()
    expect(
        "the broker image Strimzi picks for the pinned Kafka",
        ("operators.strimzi-kafka-operator+services.kafka-version", "quay.io/strimzi/kafka:1.2.0-kafka-4.3.1")
        in refs,
        f"{refs}",
    )
    no_kafka = {**_PINS, "services": {"kafbat": "v1.5.0"}}
    refs, _, _ = arch.image_refs(no_kafka, resolve_charts=False)
    expect(
        "no broker image is guessed without a Kafka pin",
        not any(r.startswith(f"{arch.STRIMZI_KAFKA_IMAGE}:") for _, r in refs),
        f"{refs}",
    )


def test_prefixed_adds_a_missing_prefix_only() -> None:
    expect("an appVersion without the prefix gets it", arch.prefixed("0.21.0", "v") == "v0.21.0")
    expect("one already carrying it is unchanged", arch.prefixed("v1.4.19", "v") == "v1.4.19")
    expect("no prefix leaves the tag alone", arch.prefixed("2.20.1", "") == "2.20.1")


def test_a_chart_resolved_operator_waits_for_resolve_charts() -> None:
    refs, skipped, broken = _refs()
    labels = {label for label, _ in refs}
    expect("keda is not read without --resolve-charts", "operators.keda" not in labels, f"{refs}")
    expect("and says how to read it", any("operators.keda" in s and "--resolve-charts" in s for s in skipped), f"{skipped}")
    expect(
        "a bootstrap chart is named under its own section",
        any(s.startswith("bootstrap.argocd: chart 10.9.0") for s in skipped),
        f"{skipped}",
    )
    expect("and is not a failure", broken == ["digests.orphan: digest pinned with no tag to read it by"], f"{broken}")


def test_third_party_only_reads_nothing_under_the_dfe_registry() -> None:
    refs, skipped, broken = _refs(third_party_only=True)
    dfe = sorted(r for _, r in refs if r.startswith("ghcr.io/hyperi-io/"))
    expect("no DFE image is read", dfe == [], f"{dfe}")
    expect("the third-party images still are", any(label == "services.kafbat" for label, _ in refs), f"{refs}")
    expect("the skip is said, with the count", any(s.startswith("2 DFE image(s)") for s in skipped), f"{skipped}")
    expect("and a DFE pin problem is left to dfe-stack", broken == [], f"{broken}")


def test_every_committed_image_pin_is_read() -> None:
    """A pin added under bootstrap:, operators: or services: with no table entry
    would ship unchecked, so every such key is read or named in NOT_AN_IMAGE."""
    _, pins = arch.dfe_stack.stack_pins(arch.dfe_stack.load_root(), None)
    refs, skipped, _ = arch.image_refs(pins, resolve_charts=False, third_party_only=True)
    read = {path for label, _ in refs for path in label.split("+")}
    read |= {note.split(":", 1)[0] for note in skipped}
    for section in ("bootstrap", "operators", "services"):
        for key in pins.get(section, {}):
            path = f"{section}.{key}"
            expect(f"{path} is checked or named as not an image", path in read or path in NOT_AN_IMAGE)
    for path in NOT_AN_IMAGE:
        section, key = path.split(".", 1)
        expect(f"{path} is still pinned", key in pins.get(section, {}))
        expect(f"{path} is not also read", path not in read)


def test_the_committed_stack_needs_no_credential_in_third_party_mode() -> None:
    """CI runs --third-party-only with no registry credential, so the committed
    pins must not put a private package in that set."""
    _, pins = arch.dfe_stack.stack_pins(arch.dfe_stack.load_root(), None)
    refs, _, broken = arch.image_refs(pins, resolve_charts=False, third_party_only=True)
    dfe = sorted(ref for _, ref in refs if ref.startswith(f"{arch.dfe_stack.DEFAULT_REGISTRY}/"))
    expect("no ghcr.io/hyperi-io image is read", dfe == [], f"{dfe}")
    expect("the current stack lists third-party images to read", len(refs) > 0, f"{refs}")
    expect("and nothing is unreadable before the registry is asked", broken == [], f"{broken}")


def test_verdict_passes_an_image_carrying_every_platform() -> None:
    status, _ = arch.verdict({"linux/amd64", "linux/arm64"}, "", {"linux/amd64", "linux/arm64"})
    expect("both platforms pass", status == "ok", status)


def test_verdict_names_the_missing_platform() -> None:
    status, detail = arch.verdict({"linux/amd64"}, "", {"linux/amd64", "linux/arm64"})
    expect("a single-arch image fails", status == "FAIL", status)
    expect("naming what it lacks", "linux/arm64" in detail, detail)


def test_an_unreadable_registry_is_an_error_not_a_pass() -> None:
    status, detail = arch.verdict(None, "401 Unauthorized", {"linux/amd64", "linux/arm64"})
    expect("an unread image is an ERROR", status == "ERROR", status)
    expect("carrying the registry's own words", "401" in detail, detail)


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
