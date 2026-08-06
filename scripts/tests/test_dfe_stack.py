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

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-stack"

# dfe-stack has no .py suffix, so the loader needs to be told it is source.
spec = importlib.util.spec_from_loader(
    "dfe_stack", importlib.machinery.SourceFileLoader("dfe_stack", str(SCRIPT))
)
stack = importlib.util.module_from_spec(spec)
sys.modules["dfe_stack"] = stack
spec.loader.exec_module(stack)

_failures = 0


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


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


def test_renovate_custom_manager_matches_the_annotations() -> None:
    """Tie renovate.json's regex to the file it claims to read.

    A customManager that matches nothing proposes nothing, and Renovate reports
    no error for it -- the inversion would be dead while every run stayed green.
    The validator cannot catch this: it checks schema, not whether the pattern
    finds anything.
    """
    import json
    import re

    cfg = json.loads((REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = cfg.get("customManagers", [])
    expect("renovate.json declares a custom manager", len(managers) == 1, f"{managers}")
    if not managers:
        return

    text = (REPO_ROOT / "versions.yaml").read_text(encoding="utf-8")
    # Renovate/RE2 spell named groups (?<n>...); python re wants (?P<n>...).
    pattern = re.sub(r"\(\?<([A-Za-z]+)>", r"(?P<\1>", managers[0]["matchStrings"][0])
    matched = {
        m.group("depName"): m.group("currentValue") for m in re.finditer(pattern, text)
    }

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


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'FAILED' if _failures else 'ALL PASSED'} -- {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
