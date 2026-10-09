#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_stack.py
#  Purpose:      Cover the refresh-digests annotation parser, which is the map
#                from a services: pin to the image its digest comes from.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe-stack's annotation parsing.

refresh-digests reads the image ref out of a comment above each pin rather than
carrying its own table. That regex is the whole map, so a silent mis-parse would
resolve the wrong image and write a confident, wrong digest.

Runs offline -- the registry calls are not exercised here.

    python3 scripts/tests/test_dfe_stack.py

No third-party deps and no test runner, matching the tool it tests.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-stack"

# dfe-stack has no .py suffix, so the loader needs to be told it is source.
spec = importlib.util.spec_from_loader(
    "dfe_stack", importlib.machinery.SourceFileLoader("dfe_stack", str(SCRIPT))
)
stack = importlib.util.module_from_spec(spec)
sys.modules["dfe_stack"] = stack
spec.loader.exec_module(stack)


def test_both_annotation_forms_parse() -> None:
    text = (
        "    services:\n"
        "      # renovate: datasource=docker depName=ghcr.io/kafbat/kafka-ui\n"
        '      kafbat: "v1.5.0"               # trailing prose\n'
        "      # image: apache/kafka -- operator-coupled: moves by hand\n"
        "      # with the strimzi support matrix.\n"
        '      kafka-version: "4.2.0"\n'
    )
    found = stack.annotated_images(text)
    expect(
        "the renovate form yields ref and tag",
        found.get("kafbat") == ("ghcr.io/kafbat/kafka-ui", "v1.5.0"),
        f"{found.get('kafbat')}",
    )
    expect(
        "the image form yields ref and tag across a wrapped comment",
        found.get("kafka-version") == ("apache/kafka", "4.2.0"),
        f"{found.get('kafka-version')}",
    )


def test_unannotated_pins_are_not_invented() -> None:
    text = '    services:\n      postgresql: "17"   # no annotation\n'
    expect(
        "an unannotated pin yields nothing",
        stack.annotated_images(text) == {},
        f"{stack.annotated_images(text)}",
    )


def test_annotation_does_not_leak_across_a_blank_line() -> None:
    """A stranded annotation must not attach to an unrelated later pin."""
    text = (
        "      # renovate: datasource=docker depName=ghcr.io/example/orphan\n"
        "\n"
        '      unrelated: "1.0.0"\n'
    )
    expect(
        "a blank line breaks the annotation-to-pin binding",
        "unrelated" not in stack.annotated_images(text),
        f"{stack.annotated_images(text)}",
    )


def test_every_recorded_digest_has_an_annotation() -> None:
    """The structural guard: a digest with no image ref cannot be re-resolved.

    Without this, adding a services-digests entry and forgetting its annotation
    leaves a pin that refresh-digests silently never checks.
    """
    text = (REPO_ROOT / "versions.yaml").read_text(encoding="utf-8")
    _, pins = stack.stack_pins(stack.parse_simple_yaml(text), None)
    recorded = set(pins.get("services-digests", {}))
    annotated = set(stack.annotated_images(text))
    missing = sorted(recorded - annotated)
    expect(
        "every services-digests key carries an image annotation",
        not missing,
        f"missing: {missing}",
    )


def test_every_app_digest_has_a_tag_verify_can_resolve() -> None:
    """`verify` needs a tag per digest; a digest with none reports a false DRIFT.

    dfe-hyperdx is versioned under content:, not apps:, so an apps-only lookup
    called a correct pin "(tag not found)".
    """
    text = (REPO_ROOT / "versions.yaml").read_text(encoding="utf-8")
    _, pins = stack.stack_pins(stack.parse_simple_yaml(text), None)
    missing = stack.untagged_digests(pins)
    expect("every digests: key has a tag to resolve", not missing, f"missing: {missing}")


# A stack with an image versioned under content: (the HyperDX fork's shape), a
# content repo that is not an image, and an app that has not published yet.
_IMAGES_PINS = {
    "apps": {"an-app": "v1.0.0", "unshipped": "v0.1.0"},
    "content": {"a-fork": "v0.2.7", "a-schema-repo": "v0.2.8"},
    "digests": {"an-app": "sha256:aaa", "a-fork": "sha256:bbb"},
}


def test_the_image_list_includes_an_image_versioned_under_content() -> None:
    """The air-gap list read apps: alone, so the HyperDX fork -- tagged under
    content: with its digest under digests: -- never reached a mirror."""
    refs = stack.app_images(_IMAGES_PINS, "registry.example/org")
    expect(
        "an image pinned under content: is listed with its tag and digest",
        refs.get("a-fork") == "registry.example/org/a-fork:v0.2.7@sha256:bbb",
        f"{refs}",
    )
    expect("an apps: image is still listed", "an-app" in refs, f"{refs}")
    expect("a content repo with no digest is not an image", "a-schema-repo" not in refs, f"{refs}")
    expect("an unpublished app stays out", "unshipped" not in refs, f"{refs}")


def test_the_toolbox_base_image_takes_the_family_tag() -> None:
    """The base image publishes as dfe-toolbox-base under toolbox.dfe-toolbox, a
    family pin with a different name, so neither apps: nor content: holds it."""
    pins = {"toolbox": {"dfe-toolbox": "v1.0.0"}, "digests": {"dfe-toolbox-base": "sha256:ccc"}}
    refs = stack.app_images(pins, "registry.example/org")
    expect(
        "the base image is listed with the family tag and its own digest",
        refs.get("dfe-toolbox-base") == "registry.example/org/dfe-toolbox-base:v1.0.0@sha256:ccc",
        f"{refs}",
    )
    expect("so it is not reported untagged", stack.untagged_digests(pins) == [], f"{stack.untagged_digests(pins)}")


def test_the_committed_image_list_carries_every_digest() -> None:
    """The repo-level invariant: every pinned DFE image reaches the mirror list."""
    _, pins = stack.stack_pins(stack.load_root(), None)
    listed = set(stack.app_images(pins, stack.DEFAULT_REGISTRY))
    pinned = set(pins.get("digests", {}))
    missing = sorted(pinned - listed)
    expect("images lists every digests: key", listed == pinned, f"missing: {missing}")


# A real index shape: dfe-hyperdx v0.2.7 as GHCR serves it -- one platform image
# and the buildx attestation manifest beside it.
_SINGLE_ARCH_INDEX = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {"digest": "sha256:6c54", "platform": {"architecture": "amd64", "os": "linux"}},
        {
            "digest": "sha256:fa5e",
            "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
            "platform": {"architecture": "unknown", "os": "unknown"},
        },
    ],
}


def test_an_attestation_entry_is_not_a_platform() -> None:
    found = stack.registry_pins.index_platforms(_SINGLE_ARCH_INDEX)
    expect("only the real platform is read", found == {"linux/amd64"}, f"{found}")


def test_a_single_manifest_is_not_read_as_an_index() -> None:
    manifest = {"schemaVersion": 2, "config": {}, "layers": []}
    expect(
        "a manifest with no platform list is not an empty index",
        stack.registry_pins.index_platforms(manifest) is None,
        f"{stack.registry_pins.index_platforms(manifest)}",
    )


def test_a_single_manifest_image_reads_its_config_platform() -> None:
    """A plain manifest lists no platforms, so its config names the one it has."""
    import json

    calls: list[tuple[str, ...]] = []

    def _imagetools(ref: str, *flags: str) -> tuple[str, str]:
        calls.append(flags)
        if flags == ("--raw",):
            return json.dumps({"schemaVersion": 2, "config": {}, "layers": []}), ""
        return json.dumps({"architecture": "arm64", "os": "linux"}), ""

    original = stack.registry_pins._imagetools
    stack.registry_pins._imagetools = _imagetools
    try:
        found, err = stack.registry_pins.ref_platforms("registry.example/org/x@sha256:aaa")
    finally:
        stack.registry_pins._imagetools = original
    expect("the config's platform is returned", found == {"linux/arm64"}, f"{found} {err!r}")
    expect("after the raw read came back with no index", len(calls) == 2, f"{calls}")


def _with_platforms(reader, fn):
    """Run fn with registry_pins.ref_platforms replaced by reader."""
    original = stack.registry_pins.ref_platforms
    stack.registry_pins.ref_platforms = reader
    try:
        return fn()
    finally:
        stack.registry_pins.ref_platforms = original


def _multi_arch_except(single: str):
    """A registry where every image is multi-arch except the one named."""

    def reader(ref: str) -> tuple[set[str], str]:
        if f"/{single}:" in ref:
            return {"linux/amd64"}, ""
        return {"linux/amd64", "linux/arm64"}, ""

    return reader


def test_platforms_fail_an_index_missing_an_architecture() -> None:
    findings = _with_platforms(
        _multi_arch_except("a-fork"),
        lambda: stack.platform_findings(_IMAGES_PINS, "registry.example/org"),
    )
    by_image = {image: (status, detail) for status, image, detail in findings}
    expect(
        "the single-arch image fails, naming what it lacks",
        by_image.get("a-fork:v0.2.7", ("", ""))[0] == "FAIL"
        and "missing linux/arm64" in by_image["a-fork:v0.2.7"][1],
        f"{findings}",
    )
    passed = by_image.get("an-app:v1.0.0", ("",))[0] == "ok"
    expect("the multi-arch image passes", passed, f"{findings}")


def test_platforms_read_the_pinned_digest_not_the_tag() -> None:
    """A tag can move; the gate's verdict is about the index a deployment pulls."""
    asked: list[str] = []

    def reader(ref: str) -> tuple[set[str], str]:
        asked.append(ref)
        return {"linux/amd64", "linux/arm64"}, ""

    _with_platforms(reader, lambda: stack.platform_findings(_IMAGES_PINS, "registry.example/org"))
    by_digest = bool(asked) and all("@sha256:" in r for r in asked)
    expect("every read is by digest", by_digest, f"{asked}")


def test_an_unreadable_registry_is_an_error_not_a_pass() -> None:
    findings = _with_platforms(
        lambda ref: (None, "403 Forbidden"),
        lambda: stack.platform_findings(_IMAGES_PINS, "registry.example/org"),
    )
    expect(
        "every image reports ERROR with the registry's own words",
        bool(findings) and all(s == "ERROR" and "403" in d for s, _, d in findings),
        f"{findings}",
    )


def _release_gate_over(maturity: str, reader) -> tuple[int, str]:
    """release-gate over a throwaway stack of _IMAGES_PINS's published images."""
    import argparse
    import contextlib
    import io
    import tempfile

    pins = (
        'schema: 2\ncurrent: "9.9.9"\nstacks:\n  9.9.9:\n'
        f"    maturity: {maturity}\n"
        '    apps:\n      an-app: "v1.0.0"\n'
        '    content:\n      a-fork: "v0.2.7"\n'
        '    digests:\n      an-app: "sha256:aaa"\n      a-fork: "sha256:bbb"\n'
    )
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "versions.yaml").write_text(pins, encoding="utf-8", newline="\n")
        original = stack.REPO_ROOT
        out = io.StringIO()
        try:
            stack.REPO_ROOT = Path(td)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = _with_platforms(
                    reader,
                    lambda: stack.cmd_release_gate(
                        argparse.Namespace(stack=None, registry="registry.example/org")
                    ),
                )
        finally:
            stack.REPO_ROOT = original
    return rc, out.getvalue()


def test_release_gate_fails_a_release_with_a_single_arch_image() -> None:
    rc, out = _release_gate_over("release", _multi_arch_except("a-fork"))
    expect("the gate exits non-zero", rc == 1, f"exit {rc}\n{out}")
    expect(
        "and names the image and the missing architecture",
        "a-fork:v0.2.7" in out and "missing linux/arm64" in out,
        out,
    )


def test_release_gate_passes_a_release_that_carries_both() -> None:
    rc, out = _release_gate_over("release", lambda ref: ({"linux/amd64", "linux/arm64"}, ""))
    expect("the gate passes", rc == 0, f"exit {rc}\n{out}")
    expect("and says how many images it read", "all 2 image(s)" in out, out)


def test_release_gate_fails_closed_when_the_registry_cannot_be_read() -> None:
    rc, out = _release_gate_over("release", lambda ref: (None, "403 Forbidden"))
    expect("an unread registry fails the gate", rc == 1, f"exit {rc}\n{out}")


def test_release_gate_never_reads_the_registry_below_release() -> None:
    """The rc gate runs on every push to main with no registry credential."""
    asked: list[str] = []

    def reader(ref: str) -> tuple[set[str], str]:
        asked.append(ref)
        return {"linux/amd64"}, ""

    rc, out = _release_gate_over("rc", reader)
    expect("an rc stack passes", rc == 0, f"exit {rc}\n{out}")
    expect("without a registry read", not asked, f"{asked}")
    expect("and says the platforms were not read", "platforms not read" in out, out)


# A stack with a thin chart per image, hyperdx's under its own chart name, and a
# chart not published yet.
_VERIFY_PINS = (
    'schema: 2\ncurrent: "9.9.9"\nstacks:\n  9.9.9:\n'
    '    apps:\n      dfe-engine: "v1.22.16"\n'
    '    content:\n      dfe-hyperdx: "v0.3.1"\n'
    '    digests:\n      dfe-engine: "sha256:engine-image"\n      dfe-hyperdx: "sha256:hdx-image"\n'
    "    chart-digests:\n"
    '      dfe-engine: "sha256:engine-chart"   # 1.22.16\n'
    '      dfe-ui: "unpublished"\n'
    '      hyperdx: "sha256:hdx-chart"         # 0.3.1\n'
)

# What the registry serves for each (package, tag) the pins above name.
_VERIFY_REGISTRY = {
    ("dfe-engine", "v1.22.16"): "sha256:engine-image",
    ("dfe-hyperdx", "v0.3.1"): "sha256:hdx-image",
    ("charts/dfe-engine", "1.22.16"): "sha256:engine-chart",
    ("charts/dfe-hyperdx", "0.3.1"): "sha256:hdx-chart",
}


def _verify_over(served: dict) -> tuple[int, str, list[tuple[str, str, str]]]:
    """verify over _VERIFY_PINS with tag_digest answering from served: (exit, output, reads)."""
    import argparse
    import contextlib
    import io
    import tempfile

    asked: list[tuple[str, str, str]] = []

    def tag_digest(org: str, package: str, tag: str, registry: str = "") -> str | None:
        asked.append((org, package, tag))
        found = served.get((package, tag))
        if isinstance(found, Exception):
            raise found
        return found

    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "versions.yaml").write_text(_VERIFY_PINS, encoding="utf-8", newline="\n")
        original_root, original_read = stack.REPO_ROOT, stack.registry_pins.tag_digest
        out = io.StringIO()
        try:
            stack.REPO_ROOT = Path(td)
            stack.registry_pins.tag_digest = tag_digest
            with contextlib.redirect_stdout(out):
                rc = stack.cmd_verify(argparse.Namespace(stack=None, registry="ghcr.io/hyperi-io"))
        finally:
            stack.REPO_ROOT, stack.registry_pins.tag_digest = original_root, original_read
    return rc, out.getvalue(), asked


def test_verify_reads_each_thin_chart_under_charts_at_its_image_version() -> None:
    rc, out, asked = _verify_over(_VERIFY_REGISTRY)
    expect("every pin matches, so verify exits 0", rc == 0, f"exit {rc}\n{out}")
    expect(
        "the engine chart is read at its tag without the v",
        ("hyperi-io", "charts/dfe-engine", "1.22.16") in asked,
        f"{asked}",
    )
    expect(
        "hyperdx's chart is read as dfe-hyperdx at the content.dfe-hyperdx version",
        ("hyperi-io", "charts/dfe-hyperdx", "0.3.1") in asked,
        f"{asked}",
    )
    expect("each chart reports ok", out.count("ok    chart ") == 2, out)
    expect(
        "an unpublished chart is skipped, not read",
        "skip  chart dfe-ui" in out and not any(p == "charts/dfe-ui" for _, p, _ in asked),
        f"{asked}\n{out}",
    )


def test_verify_fails_a_chart_digest_the_registry_does_not_serve() -> None:
    wrong = dict(_VERIFY_REGISTRY)
    wrong[("charts/dfe-engine", "1.22.16")] = "sha256:another-chart"
    rc, out, _ = _verify_over(wrong)
    expect("verify exits 1", rc == 1, f"exit {rc}\n{out}")
    expect(
        "naming the chart and both digests",
        "DRIFT chart dfe-engine: versions.yaml sha256:engine-chart != registry sha256:another-chart"
        in out,
        out,
    )
    expect("and the matching chart still reports ok", "ok    chart dfe-hyperdx" in out, out)


def test_verify_fails_a_chart_tag_the_registry_does_not_have() -> None:
    missing = {k: v for k, v in _VERIFY_REGISTRY.items() if k[0] != "charts/dfe-hyperdx"}
    rc, out, _ = _verify_over(missing)
    expect("verify exits 1", rc == 1, f"exit {rc}\n{out}")
    expect("the chart reads as not found", "DRIFT chart dfe-hyperdx" in out, out)
    expect("and says why", "(tag not found)" in out, out)


def test_verify_fails_closed_when_a_chart_cannot_be_read() -> None:
    unreadable = dict(_VERIFY_REGISTRY)
    forbidden = stack.registry_pins.RegistryError("403 Forbidden")
    unreadable[("charts/dfe-engine", "1.22.16")] = forbidden
    rc, out, _ = _verify_over(unreadable)
    expect("verify exits 1", rc == 1, f"exit {rc}\n{out}")
    expect("with the registry's own words", "ERROR chart dfe-engine: 403 Forbidden" in out, out)


def test_the_chart_name_map_matches_the_appset() -> None:
    """verify and the appset must name the same chart, or verify reads a chart Argo never pulls."""
    text = (REPO_ROOT / "argocd" / "appsets" / "layer2-apps.yaml").read_text(encoding="utf-8")
    for service, chart in stack._CHART_NAMES.items():
        expect(
            f"layer2-apps.yaml maps {service} to {chart}",
            f'eq .deploy.service "{service}" }}}}{chart}{{{{ else }}}}{{{{ .deploy.service }}}}'
            in text,
            service,
        )


def _renovate_manager() -> tuple[dict, str]:
    """renovate.json's one custom manager, and the versions.yaml it reads."""
    import json

    cfg = json.loads((REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = cfg.get("customManagers", [])
    expect("renovate.json declares a custom manager", len(managers) == 1, f"{managers}")
    return (managers[0] if managers else {}), (REPO_ROOT / "versions.yaml").read_text(encoding="utf-8")


def _as_python(pattern: str) -> str:
    """Spell a Renovate pattern for python re without changing what it matches.

    Renovate/RE2 spell named groups (?<n>...); python re wants (?P<n>...). A
    trailing `$` is end of input to RE2 and to a RegExp without the m flag, but
    python's also matches before a final newline, so it becomes \\Z.
    """
    import re

    named = re.sub(r"\(\?<([A-Za-z]+)>", r"(?P<\1>", pattern)
    return re.sub(r"(?<!\\)\$$", r"\\Z", named)


def _manager_regions(manager: dict, text: str) -> list[str]:
    """Every region `recursive` hands the manager's last matchString.

    Renovate passes each level the WHOLE match of the level before it, never a
    named group (processRecursive in lib/modules/manager/custom/regex/
    strategies.ts), so a group narrows nothing.
    """
    import re

    regions = [text]
    if manager.get("matchStringsStrategy") != "recursive":
        return regions
    for pattern in manager["matchStrings"][:-1]:
        compiled = re.compile(_as_python(pattern))
        narrowed: list[str] = []
        for region in regions:
            narrowed.extend(m.group(0) for m in compiled.finditer(region))
        regions = narrowed
    return regions


def _manager_region(manager: dict, text: str) -> str:
    """The one slice of versions.yaml the manager's scoping selects."""
    regions = _manager_regions(manager, text)
    expect("the scoping pattern selects exactly one region", len(regions) == 1, f"{len(regions)} regions")
    return regions[0] if len(regions) == 1 else ""


def _manager_matches(manager: dict, text: str) -> dict[str, str]:
    """Run the manager's matchStrings the way `recursive` applies them."""
    import re

    inner = _as_python(manager["matchStrings"][-1])
    region = _manager_region(manager, text)
    return {m.group("depName"): m.group("currentValue") for m in re.finditer(inner, region)}


def test_renovate_custom_manager_matches_the_annotations() -> None:
    """Tie renovate.json's regex to the file it claims to read.

    A customManager that matches nothing proposes nothing, and Renovate reports
    no error for it -- the inversion would be dead while every run stayed green.
    The validator cannot catch this: it checks schema, not whether the pattern
    finds anything.
    """
    import re

    manager, text = _renovate_manager()
    if not manager:
        return
    matched = _manager_matches(manager, text)

    annotated = re.findall(r"#\s*renovate:.*?depName=(\S+)", text)
    expect(
        "the manager matches every `# renovate:` annotated pin",
        set(matched) == set(annotated),
        f"matched={sorted(matched)} annotated={sorted(annotated)}",
    )
    expect(
        "it captures a real version, not an empty string",
        all(v.strip() for v in matched.values()),
        f"{matched}",
    )
    expect(
        "it does NOT pick up the operator-coupled `# image:` pins",
        not any("clickhouse-server" in d or d == "apache/kafka" for d in matched),
        f"{sorted(matched)}",
    )


def test_a_tag_at_digest_pin_hands_renovate_both_halves() -> None:
    """The docker datasource reads a whole `tag@sha256:...` as one unparseable
    version and proposes nothing, so the digest has to arrive as currentDigest."""
    import re

    manager, text = _renovate_manager()
    if not manager:
        return
    inner = _as_python(manager["matchStrings"][-1])
    matches = list(re.finditer(inner, _manager_region(manager, text)))
    # A value holding `@` matches only with its digest captured; otherwise the
    # every-annotation test above loses it.
    digest_pinned = [m for m in matches if m.group("currentDigest")]
    expect("the current stack carries a tag@sha256 pin", digest_pinned != [], "none found")
    for m in digest_pinned:
        name = m.group("depName")
        expect(f"{name}'s tag carries no digest", "@" not in m.group("currentValue"), m.group(0))
        expect(
            f"{name}'s digest arrives as currentDigest",
            re.fullmatch(r"sha256:[a-f0-9]{64}", m.group("currentDigest") or "") is not None,
            m.group(0),
        )
    debian = [m for m in matches if m.group("depName") == "debian"]
    expect(
        "the toolbox Debian base is one of them",
        len(debian) == 1 and debian[0].group("currentValue") == "trixie-slim",
        f"{[m.group(0) for m in debian]}",
    )


def test_the_manager_reaches_one_stack_block_only() -> None:
    """The frozen blocks repeat most of the pin set under the same annotations, so
    an unscoped manager would rewrite the record of what shipped (#183). Renovate
    proposes against the stack under development.

    Counted by depName this invariant is untestable: a dep-keyed dict collapses
    the repeats, so the same names come back whether the manager reads one block
    or every one of them. Count the annotation LINES the pattern reaches instead,
    and bind the region to the block `current` names.

    A block is NOT required to repeat its predecessor's pins. rc.14 opened the
    AWS deploy path and the toolbox image family, neither of which any earlier
    stack carried, so their annotations sit in one block -- and the next cut to
    open a capability will do the same again.
    """
    import re

    manager, text = _renovate_manager()
    if not manager:
        return
    region = _manager_region(manager, text)
    inner = _as_python(manager["matchStrings"][-1])
    reached = len(re.findall(inner, region))
    everywhere = len(re.findall(inner, text))
    annotated_here = len(re.findall(r"^\s*#\s*renovate:", region, re.MULTILINE))
    current = re.search(r'^current:\s*"([^"]+)"', text, re.MULTILINE)
    header = f"\n  {current.group(1) if current else ''}:\n"

    expect(
        "the region is the block `current` names, from its header to the end of the file",
        bool(current) and region.startswith(header) and text.endswith(region),
        f"region opens {region[:40]!r}, expected to open {header!r}",
    )
    expect(
        "the frozen blocks are outside it",
        reached < everywhere,
        f"reached {reached} annotated pins of {everywhere} in the file",
    )
    expect(
        "and no annotation inside it is walked past",
        reached == annotated_here,
        f"reached {reached} of {annotated_here} annotations in the block",
    )
    expect("the scoped manager still finds the pins", reached >= 5, f"reached {reached}")


def test_the_current_stack_is_the_last_block_in_the_file() -> None:
    """The manager's scoping binds to the last block, so `current` has to be it.

    A cut appends the new block and moves `current` onto it; a hand edit that
    broke that would silently point Renovate at a frozen stack.
    """
    import re

    text = (REPO_ROOT / "versions.yaml").read_text(encoding="utf-8")
    current = re.search(r'^current:\s*"([^"]+)"', text, re.MULTILINE)
    blocks = re.findall(r"^  ([0-9][A-Za-z0-9_.-]*):\s*$", text, re.MULTILINE)
    expect("versions.yaml carries a current pointer", current is not None, "none found")
    expect(
        "and the stack it names is the last block in the file",
        bool(blocks) and current and blocks[-1] == current.group(1),
        f"current={current.group(1) if current else None!r} last={blocks[-1] if blocks else None!r}",
    )


_NESTED_STACKS = """\
current: "2.0.0"
stacks:
  1.0.0:
    apps:
      an-app: "v1.0.0"
      other: "v0.1.0"
    digests:
      an-app: "sha256:aaa"
      other: "sha256:bbb"
  2.0.0:
    apps:
      an-app: "v2.0.0"
      other: "v0.2.0"
    digests:
      an-app: "sha256:ccc"
      other: "sha256:ddd"
"""


def test_bump_app_targets_the_current_stack_not_the_first_one() -> None:
    """The corruption this guards: a whole-file rewrite hits the OLDEST stack.

    versions.yaml carries every stack's complete pin set, so a file-wide
    substitution with count=1 rewrites the first match -- silently editing a
    shipped, immutable record while leaving `current` untouched.
    """
    lines = _NESTED_STACKS.splitlines(keepends=True)
    start, end = stack._block_span(lines, "2.0.0")
    start, end = stack._sub_block_span(lines, start, end, "apps")
    hits = [i for i in range(start, end) if lines[i].strip().startswith("an-app:")]

    expect("exactly one apps pin in the target stack", len(hits) == 1, f"{hits}")
    if hits:
        expect(
            "it is the CURRENT stack's pin, not the first in the file",
            '"v2.0.0"' in lines[hits[0]],
            lines[hits[0]],
        )


def test_the_apps_sub_block_excludes_digests() -> None:
    """Every app name appears twice per stack -- under apps AND under digests."""
    lines = _NESTED_STACKS.splitlines(keepends=True)
    start, end = stack._block_span(lines, "2.0.0")
    whole_block = [i for i in range(start, end) if lines[i].strip().startswith("an-app:")]
    a_start, a_end = stack._sub_block_span(lines, start, end, "apps")
    apps_only = [i for i in range(a_start, a_end) if lines[i].strip().startswith("an-app:")]

    expect("the stack block holds both", len(whole_block) == 2, f"{whole_block}")
    expect("the apps sub-block holds one", len(apps_only) == 1, f"{apps_only}")
    expect(
        "and it is the version, not the digest",
        "sha256" not in lines[apps_only[0]],
        lines[apps_only[0]],
    )


def test_bump_app_moves_a_chart_kept_outside_helm_charts() -> None:
    """culvert's chart is helm/edge/culvert, so a helm/charts/<name> path finds no chart."""
    import argparse
    import contextlib
    import io
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        versions = tmp / "versions.yaml"
        versions.write_text(
            'current: "2.0.0"\n'
            "stacks:\n"
            "  2.0.0:\n"
            "    apps:\n"
            '      culvert: "v1.0.0"\n'
            "    digests:\n"
            '      culvert: "sha256:aaa"\n',
            encoding="utf-8",
            newline="\n",
        )
        chart = tmp / "helm" / "edge" / "culvert" / "Chart.yaml"
        chart.parent.mkdir(parents=True)
        chart.write_text('name: culvert\nappVersion: "v1.0.0"\n', encoding="utf-8", newline="\n")
        original = stack.REPO_ROOT
        stack.REPO_ROOT = tmp
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = stack.cmd_bump_app(
                    argparse.Namespace(app="culvert", version="v1.0.1", stack=None, dry_run=False)
                )
        finally:
            stack.REPO_ROOT = original

        written = versions.read_text(encoding="utf-8")
        expect("bump-app exits 0", rc == 0, out.getvalue())
        expect(
            "the edge chart's appVersion moves",
            'appVersion: "v1.0.1"' in chart.read_text(encoding="utf-8"),
            chart.read_text(encoding="utf-8"),
        )
        expect("the stack's apps pin moves with it", 'culvert: "v1.0.1"' in written, written)
        expect("the digest is left for resolve_pins", 'culvert: "sha256:aaa"' in written, written)
        expect(
            "and the report names the chart it wrote",
            "helm/edge/culvert/Chart.yaml" in out.getvalue(),
            out.getvalue(),
        )


def _constraints_fixture(tmp: Path, from_name: str) -> dict:
    """A minimal root + on-disk constraints file, rooted at a throwaway tree."""
    (tmp / "constraints").mkdir(parents=True, exist_ok=True)
    (tmp / "constraints" / f"{from_name}.yaml").write_text(
        f"# Constraints for DFE stack {from_name}.\n"
        f"# Validated by: dfe-stack compat-check --stack {from_name}\n"
        f'stack: "{from_name}"\n'
        "rules:\n"
        "  a-rule:\n"
        '    severity: "error"\n',
        encoding="utf-8",
        newline="\n",
    )
    return {"stacks": {from_name: {"constraints": f"constraints/{from_name}.yaml"}}}


def test_cut_carries_the_constraints_file_forward() -> None:
    """A cut re-points `constraints:` at the new version, so it must WRITE it.

    Without this the cut lands referencing a file that does not exist and
    compat-check hard-fails -- which is exactly how every cut so far ended up
    hand-copying the file.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = _constraints_fixture(tmp, "9.9.0-rc.1")
        original = stack.REPO_ROOT
        stack.REPO_ROOT = tmp
        try:
            written = stack._carry_constraints(root, "9.9.0-rc.1", "9.9.0-rc.2")
        finally:
            stack.REPO_ROOT = original

        expect(
            "it reports the path it wrote",
            written == "constraints/9.9.0-rc.2.yaml",
            f"{written}",
        )
        text = (tmp / "constraints" / "9.9.0-rc.2.yaml").read_text(encoding="utf-8")
        expect("the new file declares the NEW stack", 'stack: "9.9.0-rc.2"' in text, text)
        expect("no stale version string survives", "9.9.0-rc.1" not in text, text)
        expect("the rules carry over unchanged", "a-rule:" in text, text)


def test_cut_never_clobbers_an_existing_constraints_file() -> None:
    """Re-cutting must not overwrite hand-edited rules for a version already cut."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root = _constraints_fixture(tmp, "9.9.0-rc.1")
        target = tmp / "constraints" / "9.9.0-rc.2.yaml"
        target.write_text("hand-edited\n", encoding="utf-8", newline="\n")
        original = stack.REPO_ROOT
        stack.REPO_ROOT = tmp
        try:
            written = stack._carry_constraints(root, "9.9.0-rc.1", "9.9.0-rc.2")
        finally:
            stack.REPO_ROOT = original

        expect("it declines rather than overwriting", written is None, f"{written}")
        expect(
            "the hand-edited file is untouched",
            target.read_text(encoding="utf-8") == "hand-edited\n",
            target.read_text(encoding="utf-8"),
        )


def test_cut_is_silent_when_no_constraints_are_declared() -> None:
    """A stack with no constraints key has nothing to carry -- not an error."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        original = stack.REPO_ROOT
        stack.REPO_ROOT = Path(td)
        try:
            written = stack._carry_constraints(
                {"stacks": {"9.9.0-rc.1": {}}}, "9.9.0-rc.1", "9.9.0-rc.2"
            )
        finally:
            stack.REPO_ROOT = original
        expect("no constraints declared -> nothing written", written is None, f"{written}")


def test_every_stack_constraints_reference_resolves() -> None:
    """The repo-level invariant the carry-forward exists to hold.

    compat-check raises on a missing file, so a stack pointing at one that was
    never created is a latent failure sitting in the SSoT.
    """
    root = stack.load_root()
    dangling = []
    for name, block in root.get("stacks", {}).items():
        rel = block.get("constraints")
        if rel and not (REPO_ROOT / rel).exists():
            dangling.append(f"{name} -> {rel}")
    expect(
        "every stack's constraints file exists on disk",
        not dangling,
        f"dangling: {dangling}",
    )


_OLD_SHA = "1" * 40
_NEW_SHA = "2" * 40

_CUT_FIXTURE = f"""\
current: "9.9.0-rc.2"
stacks:
  9.9.0-rc.1:
    maturity: "rc"
    apps:
      an-app: "v1.0.0"
    digests:
      an-app: "sha256:aaa"
    content:
      a-tagged-repo: "v1.0.0"
      a-git-repo: "{_OLD_SHA}"
    stack:
      previous: ""
      upgrade-order: "an-app"
  9.9.0-rc.2:
    maturity: "rc"
    apps:
      an-app: "v2.0.0"
    digests:
      an-app: "sha256:bbb"
    content:
      a-tagged-repo: "v1.0.0"
      a-git-repo: "{_OLD_SHA}"
    stack:
      previous: "9.9.0-rc.1"
      upgrade-order: "an-app"
"""


def _cut_into(tmp: Path, head: object) -> int:
    """Run a cut over the fixture with the registry and git reads stubbed out."""
    import argparse

    (tmp / "versions.yaml").write_text(_CUT_FIXTURE, encoding="utf-8", newline="\n")
    original = (stack.REPO_ROOT, stack._latest_published, stack.registry_pins.head_commit)
    stack.REPO_ROOT = tmp
    # Offline: no published release, so every app pin holds and no registry is hit.
    stack._latest_published = lambda org, app: None
    stack.registry_pins.head_commit = head
    try:
        return stack.cmd_cut(
            argparse.Namespace(
                version="9.9.0-rc.3", from_stack=None, maturity=None,
                apps=None, org="test-org", dry_run=False,
            )
        )
    finally:
        stack.REPO_ROOT, stack._latest_published, stack.registry_pins.head_commit = original


def test_a_cut_stamps_the_git_ref_that_ran_it() -> None:
    """dfe-docker cuts no per-stack tag, so an old stack named no ref at all and
    the only pairing that reproduced its docker path was whatever main looked
    like that week (#171)."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rc = _cut_into(tmp, lambda org, repo, ref="main": _NEW_SHA)
        expect("the cut writes the file", rc == 0, f"exit {rc}")
        root = stack.parse_simple_yaml((tmp / "versions.yaml").read_text(encoding="utf-8"))
        content = root["stacks"]["9.9.0-rc.3"]["content"]
        expect(
            "the git-ref entry is re-resolved at cut time",
            content.get("a-git-repo") == _NEW_SHA,
            f"{content.get('a-git-repo')!r}",
        )
        expect(
            "and a tag entry is left to its own tagger",
            content.get("a-tagged-repo") == "v1.0.0",
            f"{content.get('a-tagged-repo')!r}",
        )
        expect(
            "the source block keeps the ref that ran IT",
            root["stacks"]["9.9.0-rc.2"]["content"]["a-git-repo"] == _OLD_SHA,
            "an old stack's recorded ref is the whole point",
        )


def test_the_key_names_the_repo_so_no_table_says_which() -> None:
    """Which repo a content entry resolves against is the key, not a list in the
    code -- a second content repo recorded by commit needs no code change."""
    import tempfile

    asked: list[tuple[str, str]] = []

    def _head(org: str, repo: str, ref: str = "main") -> str:
        asked.append((org, repo))
        return _NEW_SHA

    with tempfile.TemporaryDirectory() as td:
        _cut_into(Path(td), _head)
    expect(
        "only the git-ref entry is resolved, against its own name",
        asked == [("test-org", "a-git-repo")],
        f"{asked}",
    )


def test_cut_repoints_previous_at_the_stack_it_was_cut_from() -> None:
    """A cut clones the source block, so the source IS the new predecessor.

    Left to the clone, `stack.previous` carries the SOURCE's predecessor
    forward and every later cut repeats it, so check-upgrade reads a real
    consecutive upgrade as NOT A VERIFIED PATH.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rc = _cut_into(tmp, lambda org, repo, ref="main": _OLD_SHA)

        expect("the cut writes the file", rc == 0, f"exit {rc}")
        root = stack.parse_simple_yaml((tmp / "versions.yaml").read_text(encoding="utf-8"))
        cut = root["stacks"].get("9.9.0-rc.3", {}).get("stack", {})
        expect(
            "the new stack's previous is the stack it was cut FROM",
            cut.get("previous") == "9.9.0-rc.2",
            f"previous={cut.get('previous')!r} -- inherited the source's own predecessor",
        )
        expect(
            "the source block's previous is left alone",
            root["stacks"]["9.9.0-rc.2"]["stack"]["previous"] == "9.9.0-rc.1",
            f"{root['stacks']['9.9.0-rc.2']['stack']['previous']!r}",
        )


def test_cut_refuses_to_move_a_tag_it_cannot_pin() -> None:
    """An app publishing for the first time has an apps pin and no digests key.

    The tag rewrite succeeds and the digest rewrite has nothing to write, so
    left unchecked the cut reports MOVED, exits 0, and ships that app unpinned.
    """
    import argparse
    import tempfile

    fixture = _CUT_FIXTURE.replace('      an-app: "sha256:bbb"\n', "")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "versions.yaml").write_text(fixture, encoding="utf-8", newline="\n")
        original_root, original_published = stack.REPO_ROOT, stack._latest_published
        stack.REPO_ROOT = tmp
        # The app has just published, so the tag moves and the digest has no home.
        stack._latest_published = lambda org, app: ("v3.0.0", "sha256:ccc")
        try:
            stack.cmd_cut(
                argparse.Namespace(
                    version="9.9.0-rc.3",
                    from_stack=None,
                    maturity=None,
                    apps=None,
                    org="test-org",
                    dry_run=False,
                )
            )
        except SystemExit as exc:
            expect(
                "it refuses, naming the key to add",
                "digests.an-app" in str(exc) and "sha256:ccc" in str(exc),
                f"message={str(exc)!r}",
            )
        else:
            expect("it refuses rather than shipping an unpinned app", False, "cut returned")
        finally:
            stack.REPO_ROOT, stack._latest_published = original_root, original_published


def test_every_consecutive_stack_pair_is_a_verified_upgrade_path() -> None:
    """The repo-level invariant: no gap in the shipped upgrade chain.

    check-upgrade answers from stack.previous alone, so a stale value makes a
    real consecutive upgrade report NOT A VERIFIED PATH.
    """
    import itertools

    root = stack.load_root()
    broken = []
    for frm, to in itertools.pairwise(stack.stack_names(root)):
        declared = [
            p.strip()
            for p in root["stacks"][to].get("stack", {}).get("previous", "").split(",")
            if p.strip()
        ]
        if not any(stack._norm_version(p) == stack._norm_version(frm) for p in declared):
            broken.append(f"{frm} -> {to} (declares {declared or ['']})")
    expect(
        "every adjacent stack pair declares a verified path",
        not broken,
        f"broken: {broken}",
    )


_REFRESH_FIXTURE = """\
current: "9.9.0-rc.2"
stacks:
  9.9.0-rc.1:
    services:
      # renovate: datasource=docker depName=example/floating
      floating: "1-alpine"
      # renovate: datasource=docker depName=example/steady
      steady: "2.0.0"
    services-digests:
      floating: "sha256:aaa"   # example/floating:1-alpine
      steady: "sha256:bbb"     # example/steady:2.0.0
  9.9.0-rc.2:
    services:
      # renovate: datasource=docker depName=example/floating
      floating: "1-alpine"
      # renovate: datasource=docker depName=example/steady
      steady: "2.0.0"
    services-digests:
      floating: "sha256:aaa"   # example/floating:1-alpine
      steady: "sha256:bbb"     # example/steady:2.0.0
"""


def _run_refresh(tmp: Path, resolver) -> tuple[int, str]:
    """refresh-digests over the fixture with the registry stubbed out."""
    import argparse

    (tmp / "versions.yaml").write_text(_REFRESH_FIXTURE, encoding="utf-8", newline="\n")
    original_root = stack.REPO_ROOT
    original_resolve = stack.registry_pins.ref_digest
    stack.REPO_ROOT = tmp
    stack.registry_pins.ref_digest = resolver
    try:
        rc = stack.cmd_refresh_digests(argparse.Namespace(stack=None, check=False))
    finally:
        stack.REPO_ROOT = original_root
        stack.registry_pins.ref_digest = original_resolve
    return rc, (tmp / "versions.yaml").read_text(encoding="utf-8")


def test_refresh_rewrites_a_digest_repeated_across_stacks() -> None:
    """A pin that has not moved since the first cut repeats once per stack.

    Searching the whole file for the old digest literal then finds every copy,
    and refusing that as ambiguous left the live stack stale for as long as the
    pin stood still -- which is exactly when it is a floating tag.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rc, text = _run_refresh(tmp, lambda ref: ("sha256:new", ""))
        rc1 = text.split("9.9.0-rc.2:")[0]
        rc2 = text.split("9.9.0-rc.2:")[1]

        expect("a digest repeated across stacks is still written", rc == 0, f"exit {rc}")
        expect(
            "both of the current stack's stale digests moved",
            rc2.count('"sha256:new"') == 2,
            rc2,
        )
        expect(
            "the shipped stack's record is left alone",
            'floating: "sha256:aaa"' in rc1 and 'steady: "sha256:bbb"' in rc1,
            rc1,
        )
        expect(
            "the trailing comment survives the rewrite",
            "# example/floating:1-alpine" in rc2,
            rc2,
        )


def test_refresh_writes_the_healthy_pins_when_one_cannot_resolve() -> None:
    """One unresolvable image used to abort the run with nothing written.

    Every other pin was already resolved by then, so a single bad entry held the
    whole refresh back and the propagation job never pushed.
    """
    import tempfile

    def resolver(ref: str) -> tuple[str | None, str]:
        return (None, "no such manifest") if "floating" in ref else ("sha256:new", "")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rc, text = _run_refresh(tmp, resolver)
        rc2 = text.split("9.9.0-rc.2:")[1]

        expect("the failure is still visible in the exit code", rc == 1, f"exit {rc}")
        expect(
            "the pin that DID resolve was written anyway",
            'steady: "sha256:new"' in rc2,
            rc2,
        )
        expect(
            "the pin that could not resolve is untouched",
            'floating: "sha256:aaa"' in rc2,
            rc2,
        )


# --- --env-file -> GH_TOKEN (authenticating gh for the registry reads) --------
_FAKE_TOKEN = "ghp_scopedtokenvalue"


def _run_stack_main(argv: list[str]) -> tuple[int, str, str]:
    """dfe-stack main() with argv replaced, capturing both streams."""
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    original_argv = sys.argv
    sys.argv = argv
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = stack.main()
    finally:
        sys.argv = original_argv
    return rc, out.getvalue(), err.getvalue()


def _with_env_file(body: str, ambient: str | None) -> tuple[int, str, str, str | None]:
    """Run `dfe-stack --env-file <body> current`, returning the resulting GH_TOKEN."""
    import os
    import tempfile

    previous = os.environ.pop("GH_TOKEN", None)
    if ambient is not None:
        os.environ["GH_TOKEN"] = ambient
    try:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "creds.env"
            path.write_text(body, encoding="utf-8", newline="\n")
            rc, out, err = _run_stack_main(["dfe-stack", "--env-file", str(path), "current"])
        return rc, out, err, os.environ.get("GH_TOKEN")
    finally:
        os.environ.pop("GH_TOKEN", None)
        if previous is not None:
            os.environ["GH_TOKEN"] = previous


def test_env_file_ghcr_token_authenticates_gh() -> None:
    """The scoped token lives in a git-ignored env file, not in `gh login`."""
    rc, out, err, token = _with_env_file(f'GHCR_TOKEN="{_FAKE_TOKEN}"\n', None)
    expect("the subcommand still runs", rc == 0, f"exit {rc}")
    expect("GHCR_TOKEN becomes GH_TOKEN", token == _FAKE_TOKEN, f"{token!r}")
    expect("the token is never printed", _FAKE_TOKEN not in out + err, out + err)


def test_env_file_gh_token_is_used_directly() -> None:
    rc, _, _, token = _with_env_file(f"GH_TOKEN={_FAKE_TOKEN}\n", None)
    expect("GH_TOKEN in the file is used as-is", token == _FAKE_TOKEN, f"{token!r}")
    expect("the subcommand still runs", rc == 0, f"exit {rc}")


def test_ambient_gh_token_is_not_overwritten() -> None:
    """The file is the fallback for a host missing read:packages, not an override."""
    _, _, _, token = _with_env_file(f'GHCR_TOKEN="{_FAKE_TOKEN}"\n', "ghp_ambient")
    expect("an ambient GH_TOKEN wins", token == "ghp_ambient", f"{token!r}")


def test_env_file_without_a_token_sets_nothing() -> None:
    _, _, _, token = _with_env_file("DFE_NAMESPACE=dfe\n", None)
    expect("a token-free env file leaves GH_TOKEN unset", token is None, f"{token!r}")


def test_no_env_file_leaves_the_environment_alone() -> None:
    import os

    previous = os.environ.pop("GH_TOKEN", None)
    try:
        rc, _, _ = _run_stack_main(["dfe-stack", "current"])
        expect("the subcommand still runs without --env-file", rc == 0, f"exit {rc}")
        expect("nothing is set", "GH_TOKEN" not in os.environ, "GH_TOKEN was set")
    finally:
        if previous is not None:
            os.environ["GH_TOKEN"] = previous


def _compat_check_over(rules: str, pins: str, strict: bool) -> tuple[int, str]:
    """Run compat-check against a throwaway versions.yaml + constraints pair."""
    import argparse
    import contextlib
    import io
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "constraints").mkdir()
        (tmp / "constraints" / "9.9.9.yaml").write_text(
            'schema: 1\nstack: "9.9.9"\n\nrules:\n\n' + rules, encoding="utf-8", newline="\n"
        )
        (tmp / "versions.yaml").write_text(
            'schema: 2\ncurrent: "9.9.9"\n\nstacks:\n\n  9.9.9:\n'
            '    constraints: "constraints/9.9.9.yaml"\n' + pins,
            encoding="utf-8",
            newline="\n",
        )
        original = stack.REPO_ROOT
        out = io.StringIO()
        try:
            stack.REPO_ROOT = tmp
            with contextlib.redirect_stdout(out):
                rc = stack.cmd_compat_check(argparse.Namespace(stack=None, strict=strict))
        finally:
            stack.REPO_ROOT = original
        return rc, out.getvalue()


# An operator pin the guards below watch, sitting well outside both of them.
_MOVED_PINS = '    operators:\n      an-operator: "9.0.0"\n      a-service: "1.0.0"\n'
_ERROR_RULE = (
    "  a-ceiling:\n"
    '    severity: "error"\n'
    '    when-key: "operators.an-operator"\n'
    '    when-range: ">=0.51.0 <0.52.0"\n'
    '    require-key: "operators.a-service"\n'
    '    require-range: ">=1.0.0"\n'
)
_WARN_RULE = (
    "  a-pairing:\n"
    '    severity: "warn"\n'
    '    when-key: "operators.an-operator"\n'
    '    when-equals: "0.51.0"\n'
    '    require-key: "operators.a-service"\n'
    '    require-equals: "1.0.0"\n'
)


def test_strict_fails_an_error_rule_whose_guard_can_never_match() -> None:
    """A guard that matches nothing makes an error-severity rule inert, and an
    inert rule prints as a skip -- which reads exactly like a pass (#295)."""
    rc, out = _compat_check_over(_ERROR_RULE, _MOVED_PINS, strict=True)
    expect("strict exits non-zero", rc == 1, f"exit {rc}\n{out}")
    expect("the rule is called out as dead", "DEAD" in out, out)
    expect("and the message names the guard", "when-range" in out, out)
    expect("and says what to do about it", "re-point or delete" in out, out)


def test_a_dead_error_guard_is_advisory_without_strict() -> None:
    """compat-check without --strict reports and returns 0, as it does for a
    violated requirement -- the gate is --strict, in one place."""
    rc, out = _compat_check_over(_ERROR_RULE, _MOVED_PINS, strict=False)
    expect("the advisory run still exits 0", rc == 0, f"exit {rc}\n{out}")
    expect("and still says the rule is dead", "DEAD" in out, out)


def test_strict_still_skips_a_warn_rule_whose_guard_is_not_met() -> None:
    """A warn rule is advice, so a guard it no longer matches stays a skip --
    only the error tier is strong enough to fail a release gate on."""
    rc, out = _compat_check_over(_WARN_RULE, _MOVED_PINS, strict=True)
    expect("strict passes", rc == 0, f"exit {rc}\n{out}")
    expect("and the warn rule reads as n/a", "n/a" in out, out)


def _committed_rule(rule_id: str) -> str:
    """One rule from the current stack's constraints file, as constraints text."""
    _, pins = stack.stack_pins(stack.load_root(), None)
    text = (REPO_ROOT / pins["constraints"]).read_text(encoding="utf-8")
    body = stack.parse_simple_yaml(text)["rules"][rule_id]
    return f"  {rule_id}:\n" + "".join(f'    {key}: "{value}"\n' for key, value in body.items())


def _gateway_pins(gateway: str, proxy: str) -> str:
    return (
        f'    operators:\n      envoy-gateway: "{gateway}"\n'
        f'    services:\n      envoy-gateway-proxy: "{proxy}"\n'
    )


def test_a_gateway_bump_without_its_proxy_fails_strict() -> None:
    """The proxy image is the gateway's compiled default made explicit, so the
    two move as one: a gateway lift that leaves the proxy behind must not ship."""
    rule = _committed_rule("envoy-gateway-proxy-pairing")
    rc, out = _compat_check_over(rule, _gateway_pins("v1.9.3", "distroless-v1.39.2"), strict=True)
    expect("strict exits non-zero", rc == 1, f"exit {rc}\n{out}")
    expect("because the pairing's guard went dead", "DEAD" in out, out)


def test_a_proxy_bump_without_its_gateway_fails_strict() -> None:
    rule = _committed_rule("envoy-gateway-proxy-pairing")
    rc, out = _compat_check_over(rule, _gateway_pins("v1.9.2", "distroless-v1.39.3"), strict=True)
    expect("strict exits non-zero", rc == 1, f"exit {rc}\n{out}")
    expect("naming the proxy pin it rejects", "FAIL" in out and "distroless-v1.39.3" in out, out)


def _clickhouse_pins(server: str, keeper: str) -> str:
    return (
        f'    services:\n      clickhouse-version: "{server}"\n'
        f'      clickhouse-keeper: "{keeper}"\n'
    )


def test_a_clickhouse_bump_without_its_keeper_fails_strict() -> None:
    """Keeper ships in the server's release, so a server lift that leaves Keeper
    on the old one must not ship."""
    rule = _committed_rule("clickhouse-keeper-server-pairing")
    rc, out = _compat_check_over(rule, _clickhouse_pins("26.3.43.1", "26.3.42.3"), strict=True)
    expect("strict exits non-zero", rc == 1, f"exit {rc}\n{out}")
    expect("because the pairing's guard went dead", "DEAD" in out, out)


def test_a_keeper_bump_without_its_server_fails_strict() -> None:
    rule = _committed_rule("clickhouse-keeper-server-pairing")
    rc, out = _compat_check_over(rule, _clickhouse_pins("26.3.42.3", "26.3.43.1"), strict=True)
    expect("strict exits non-zero", rc == 1, f"exit {rc}\n{out}")
    expect("naming the keeper pin it rejects", "FAIL" in out and "26.3.43.1" in out, out)


def test_the_committed_constraints_pass_strict() -> None:
    """The repo-level invariant: no committed rule is inert against its stack."""
    import argparse
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = stack.cmd_compat_check(argparse.Namespace(stack=None, strict=True))
    expect("compat-check --strict is green as committed", rc == 0, out.getvalue())


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
