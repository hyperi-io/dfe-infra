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
# by a family pin, a digest with no tag, a third-party service and two operators.
_PINS = {
    "apps": {"an-app": "v1.0.0", "unshipped": "v0.1.0"},
    "toolbox": {"dfe-toolbox": "v2.0.0"},
    "digests": {
        "an-app": "sha256:aaa",
        "dfe-toolbox-base": "sha256:bbb",
        "orphan": "sha256:ccc",
    },
    "services": {"kafbat": "v1.5.0", "git-sync": "v4.7.1"},
    "bootstrap": {"cert-manager": "v1.21.2"},
    "operators": {"strimzi-kafka-operator": "1.2.0", "keda": "2.20.1"},
}


def _refs(third_party_only: bool = False) -> tuple[dict[str, str], list[str], list[str]]:
    refs, skipped, broken = arch.image_refs(_PINS, resolve_charts=False, third_party_only=third_party_only)
    return dict(refs), skipped, broken


def test_dfe_images_are_read_by_their_pinned_digest() -> None:
    refs, _, _ = _refs()
    expect(
        "an app is read by tag@digest",
        refs.get("digests.an-app") == "ghcr.io/hyperi-io/an-app:v1.0.0@sha256:aaa",
        f"{refs}",
    )
    expect(
        "the toolbox base image takes the family tag",
        refs.get("digests.dfe-toolbox-base") == "ghcr.io/hyperi-io/dfe-toolbox-base:v2.0.0@sha256:bbb",
        f"{refs}",
    )


def test_an_unpublished_app_is_noted_not_failed() -> None:
    refs, skipped, broken = _refs()
    expect("the unpublished app is not read", not any("unshipped" in r for r in refs.values()), f"{refs}")
    expect("it is named in the skip notes", any("apps.unshipped" in s for s in skipped), f"{skipped}")
    expect("and is not a failure", not any("unshipped" in b for b in broken), f"{broken}")


def test_a_digest_with_no_tag_fails() -> None:
    _, _, broken = _refs()
    expect("an untagged digest is a failure, not a silent drop", any("digests.orphan" in b for b in broken), f"{broken}")


def test_third_party_images_come_from_the_shared_tables() -> None:
    refs, _, _ = _refs()
    expect("a docker-path service", refs.get("services.kafbat") == "ghcr.io/kafbat/kafka-ui:v1.5.0", f"{refs}")
    expect(
        "a k8s-only service",
        refs.get("services.git-sync") == "registry.k8s.io/git-sync/git-sync:v4.7.1",
        f"{refs}",
    )
    expect(
        "an operator whose tag is its chart version",
        refs.get("operators.strimzi-kafka-operator") == "quay.io/strimzi/operator:1.2.0",
        f"{refs}",
    )
    expect(
        "a bootstrap operator already carrying its v prefix keeps one",
        refs.get("operators.cert-manager") == "quay.io/jetstack/cert-manager-controller:v1.21.2",
        f"{refs}",
    )


def test_a_chart_resolved_operator_waits_for_resolve_charts() -> None:
    refs, skipped, broken = _refs()
    expect("keda is not read without --resolve-charts", "operators.keda" not in refs, f"{refs}")
    expect("and says how to read it", any("operators.keda" in s and "--resolve-charts" in s for s in skipped), f"{skipped}")
    expect("and is not a failure", broken == ["digests.orphan: digest pinned with no tag to read it by"], f"{broken}")


def test_third_party_only_reads_nothing_under_the_dfe_registry() -> None:
    refs, skipped, broken = _refs(third_party_only=True)
    dfe = sorted(r for r in refs.values() if r.startswith("ghcr.io/hyperi-io/"))
    expect("no DFE image is read", dfe == [], f"{dfe}")
    expect("the third-party images still are", "services.kafbat" in refs, f"{refs}")
    expect("the skip is said, with the count", any(s.startswith("2 DFE image(s)") for s in skipped), f"{skipped}")
    expect("and a DFE pin problem is left to dfe-stack", broken == [], f"{broken}")


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
