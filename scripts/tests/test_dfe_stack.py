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
    tags = {**pins.get("apps", {}), **pins.get("content", {})}
    missing = sorted(name for name in pins.get("digests", {}) if not tags.get(name))
    expect("every digests: key has a tag to resolve", not missing, f"missing: {missing}")


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
    original_root, original_resolve = stack.REPO_ROOT, stack.resolve_digest
    stack.REPO_ROOT = tmp
    stack.resolve_digest = resolver
    try:
        rc = stack.cmd_refresh_digests(argparse.Namespace(stack=None, check=False))
    finally:
        stack.REPO_ROOT, stack.resolve_digest = original_root, original_resolve
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


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
