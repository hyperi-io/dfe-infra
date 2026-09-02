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
    matched = {m.group("depName"): m.group("currentValue") for m in re.finditer(pattern, text)}

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


_CUT_FIXTURE = """\
current: "9.9.0-rc.2"
stacks:
  9.9.0-rc.1:
    maturity: "rc"
    apps:
      an-app: "v1.0.0"
    digests:
      an-app: "sha256:aaa"
    stack:
      previous: ""
      upgrade-order: "an-app"
  9.9.0-rc.2:
    maturity: "rc"
    apps:
      an-app: "v2.0.0"
    digests:
      an-app: "sha256:bbb"
    stack:
      previous: "9.9.0-rc.1"
      upgrade-order: "an-app"
"""


def test_cut_repoints_previous_at_the_stack_it_was_cut_from() -> None:
    """A cut clones the source block, so the source IS the new predecessor.

    Left to the clone, `stack.previous` carries the SOURCE's predecessor
    forward and every later cut repeats it, so check-upgrade reads a real
    consecutive upgrade as NOT A VERIFIED PATH.
    """
    import argparse
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "versions.yaml").write_text(_CUT_FIXTURE, encoding="utf-8", newline="\n")
        original_root, original_published = stack.REPO_ROOT, stack._latest_published
        stack.REPO_ROOT = tmp
        # Offline: no published release, so every app pin holds and no registry is hit.
        stack._latest_published = lambda org, app: None
        try:
            rc = stack.cmd_cut(
                argparse.Namespace(
                    version="9.9.0-rc.3",
                    from_stack=None,
                    maturity=None,
                    apps=None,
                    org="test-org",
                    dry_run=False,
                )
            )
        finally:
            stack.REPO_ROOT, stack._latest_published = original_root, original_published

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


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'FAILED' if _failures else 'ALL PASSED'} -- {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
