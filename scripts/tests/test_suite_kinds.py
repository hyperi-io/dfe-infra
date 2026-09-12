#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_suite_kinds.py
#  Purpose:      Prove the per-edge checks in scripts/suite/kinds.py: the two
#                version matchers, the range readers that have to name the
#                RIGHT package, the blob-hash compare, and that a kind this
#                cannot answer says so instead of guessing.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/suite/kinds.py -- the pure half of the edge checks.

Every check here reads files and compares versions; nothing shells out except
`git hash-object`, which is what the vendored-file kind is. Offline.

    python3 -m pytest scripts/tests/test_suite_kinds.py
    python3 scripts/tests/test_suite_kinds.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _expect import expect, standalone, summary  # noqa: E402
from suite import kinds  # noqa: E402


def _edge(kind: str, **over) -> dict:
    edge = {
        "from": "producer",
        "to": "consumer",
        "kind": kind,
        "type": "potential",
        "evidence": "consumer/Cargo.toml:6",
    }
    edge.update(over)
    return edge


# --- the evidence citation ----------------------------------------------------
def test_an_evidence_reference_parses_to_its_three_shapes() -> None:
    one = kinds.parse_evidence("dfe-loader/Cargo.toml:35")
    expect("a single line", (one.repo, one.path, one.start, one.end) == (
        "dfe-loader", "Cargo.toml", 35, None
    ))
    span = kinds.parse_evidence("dfe-loader/Dockerfile:8-12")
    expect("a line range", (span.start, span.end) == (8, 12))
    whole = kinds.parse_evidence("dfe-loader/Cargo.toml")
    expect("a whole file", whole.start is None)
    expect("it renders as it was written", span.describe() == "dfe-loader/Dockerfile:8-12")


def test_junk_is_dropped_rather_than_guessed_at() -> None:
    # A field holds exactly ONE reference; two crammed in is no longer a citation.
    expect("two references", kinds.parse_evidence("a/b.toml c/d.toml") is None)
    expect("no repo", kinds.parse_evidence("Cargo.toml") is None)
    expect("empty", kinds.parse_evidence("") is None)


def test_every_check_has_a_summary_and_the_reverse() -> None:
    expect("no check is unexplained", set(kinds.CHECKS) == set(kinds.KIND_SUMMARY))


# --- the Cargo matcher --------------------------------------------------------
def test_a_cargo_range_answers_inside_and_outside_its_bounds() -> None:
    expect("inside", kinds.semver_admits(">=2.10.14, <3", "2.11.0") is True)
    expect("the next major", kinds.semver_admits(">=2.10.14, <3", "3.0.0") is False)
    expect("below the floor", kinds.semver_admits(">=2.12.1, <3", "2.11.0") is False)


def test_caret_and_tilde_stop_where_cargo_says() -> None:
    expect("caret holds the major", kinds.semver_admits("^1.2.3", "1.9.0") is True)
    expect("caret stops at the next", kinds.semver_admits("^1.2.3", "2.0.0") is False)
    # Below one the leftmost non-zero component is the minor.
    expect("caret below one", kinds.semver_admits("^0.2.3", "0.3.0") is False)
    expect("tilde holds the minor", kinds.semver_admits("~1.2.3", "1.2.9") is True)
    expect("tilde stops at the next", kinds.semver_admits("~1.2.3", "1.3.0") is False)


def test_a_bare_cargo_version_is_a_caret_range() -> None:
    expect("bare admits within the major", kinds.semver_admits("1.2.3", "1.9.0") is True)
    expect("bare stops at the next major", kinds.semver_admits("1.2.3", "2.0.0") is False)


def test_a_pre_release_is_not_admitted_by_a_plain_range() -> None:
    """Answering yes there would move a consumer onto a release candidate."""
    expect("plain range", kinds.semver_admits(">=2.10.14, <3", "2.11.0-rc.1") is None)
    expect("a range that names one", kinds.semver_admits(">=2.0.0-0, <3", "2.11.0-rc.1") is True)
    expect("build metadata is not one", kinds.semver_admits(">=2.10.0, <3", "2.11.0+abc") is True)


def test_a_requirement_this_cannot_read_is_not_a_verdict() -> None:
    expect("a git ref", kinds.semver_admits("branch = main", "2.11.0") is None)
    expect("empty", kinds.semver_admits("", "2.11.0") is None)
    expect("a word for a version", kinds.semver_admits(">=2.0.0", "latest") is None)


# --- the PEP 440 matcher ------------------------------------------------------
def test_a_pep440_specifier_answers_its_own_forms() -> None:
    expect("floor", kinds.pep440_admits(">=2.29.7", "2.29.10") is True)
    expect("bounded", kinds.pep440_admits(">=2.29.7,<3", "3.0.0") is False)
    expect("exact", kinds.pep440_admits("==2.29.7", "2.29.7") is True)
    expect("exclusion", kinds.pep440_admits("!=2.29.7", "2.29.7") is False)
    expect("compatible release", kinds.pep440_admits("~=2.29.7", "2.29.10") is True)
    expect("compatible release upper", kinds.pep440_admits("~=2.29.7", "2.30.0") is False)
    expect("prefix match", kinds.pep440_admits("==2.29.*", "2.29.10") is True)
    expect("prefix match upper", kinds.pep440_admits("==2.29.*", "2.30.0") is False)


def test_a_bare_version_is_not_a_pep440_specifier() -> None:
    expect("bare", kinds.pep440_admits("2.29.7", "2.29.10") is None)
    expect("one-component compatible release", kinds.pep440_admits("~=2", "2.1") is None)


def test_a_post_or_dev_release_orders_with_the_release_it_hangs_off() -> None:
    expect("post", kinds.pep440_admits(">=2.29.7", "2.29.10.post1") is True)
    expect("dev", kinds.pep440_admits(">=2.29.7", "2.29.10.dev3") is True)
    # A pre-release written with no separator is not one this reads.
    expect("no separator", kinds.pep440_admits(">=2.29.7", "2.29.10rc1") is None)


# --- the range readers --------------------------------------------------------
def test_a_cargo_line_is_read_only_when_it_declares_our_crate() -> None:
    table = 'scalo = { version = ">=2.10.14, <3", features = ["config"] }'
    expect("table form", kinds.cargo_range(table, "scalo") == ">=2.10.14, <3")
    expect("bare string", kinds.cargo_range('scalo = "1.2.3"', "scalo") == "1.2.3")
    # A neighbouring dependency's line has a range on it too.
    expect("a neighbour", kinds.cargo_range('tokio = { version = "1" }', "scalo") is None)
    expect("workspace", kinds.cargo_range("scalo.workspace = true", "scalo") is None)
    expect("commented out", kinds.cargo_range('# scalo = "1.2.3"', "scalo") is None)


def test_a_python_requirement_is_read_only_when_it_names_our_distribution() -> None:
    extras = '    "scalo[expression,http]>=2.29.7",'
    expect("with extras", kinds.python_range(extras, "scalo") == ">=2.29.7")
    expect("with a marker", kinds.python_range('"scalo>=2.0; python_version>=\'3.12\'"', "scalo")
           == ">=2.0")
    # scalo-extras starts with scalo but is a different distribution.
    expect("a prefix is not a name", kinds.python_range('"scalo-extras>=1.0"', "scalo") is None)
    expect("no range", kinds.python_range('"scalo"', "scalo") is None)
    expect("commented out", kinds.python_range('# "scalo>=2.0"', "scalo") is None)


# --- the checks that read a file ----------------------------------------------
def _consumer(root: Path, line: str) -> Path:
    repo = root / "consumer"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "Cargo.toml").write_text(
        f'[package]\nname = "consumer"\n\n[dependencies]\ntokio = "1"\n{line}\n',
        encoding="utf-8",
        newline="\n",
    )
    return repo


def test_a_cargo_edge_asks_for_a_rebuild_or_a_wider_range() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        repo = _consumer(Path(scratch), 'scalo = { version = ">=2.10.14, <3" }')
        admits = kinds.check_cargo_dep(
            _edge("cargo-dep"), producer_version="2.11.0", consumer_repo=repo, package="scalo"
        )
        expect("admits -> rebuild", admits.action == kinds.ACTION_REBUILD_RUST)
        expect("admits -> moved", admits.moved is True)
        wider = kinds.check_cargo_dep(
            _edge("cargo-dep"), producer_version="3.0.0", consumer_repo=repo, package="scalo"
        )
        expect("does not admit -> widen", wider.action == kinds.ACTION_WIDEN_RANGE)


def test_the_edges_own_note_rides_along_with_the_verdict() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        repo = _consumer(Path(scratch), 'scalo = { version = ">=2.10.14, <3" }')
        result = kinds.check_cargo_dep(
            _edge("cargo-dep", note="A second range sits at Cargo.toml:180."),
            producer_version="2.11.0",
            consumer_repo=repo,
            package="scalo",
        )
        expect("the note is carried", "Cargo.toml:180" in result.detail)


def test_a_producer_with_no_package_in_the_graph_says_so() -> None:
    """Without it the range at the cited line cannot be attributed to the producer."""
    with tempfile.TemporaryDirectory() as scratch:
        repo = _consumer(Path(scratch), 'scalo = { version = ">=2.10.14, <3" }')
        result = kinds.check_cargo_dep(
            _edge("cargo-dep"), producer_version="2.11.0", consumer_repo=repo, package=None
        )
        expect("undetermined", result.moved is None)
        expect("names the fix", "add `package:`" in result.detail)


def test_a_missing_file_is_a_verdict_not_a_crash() -> None:
    """One unreadable file must not abort a whole walk."""
    with tempfile.TemporaryDirectory() as scratch:
        repo = Path(scratch) / "consumer"
        repo.mkdir()
        result = kinds.check_edge(
            _edge("cargo-dep"), producer_version="2.11.0", consumer_repo=repo, package="scalo"
        )
        expect("undetermined", result.moved is None)
        expect("human-read", result.action == kinds.ACTION_HUMAN_READ)


def test_a_generated_file_hands_back_the_command_its_own_header_names() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        repo = Path(scratch) / "consumer"
        repo.mkdir()
        (repo / "Dockerfile").write_text(
            "# Generated file\n# Schema version: 3\n"
            "# Regenerate with: `dfe-loader emit-dockerfile > Dockerfile`\n",
            encoding="utf-8",
            newline="\n",
        )
        result = kinds.check_generated_file(
            _edge("generated-file", evidence="consumer/Dockerfile:1-3"),
            producer_version="2.11.0",
            consumer_repo=repo,
        )
        expect("regenerate", result.action == kinds.ACTION_REGENERATE)
        expect("the schema version", "schema version 3" in result.detail)
        expect("the command", "emit-dockerfile" in result.detail)


def test_a_vendored_file_is_answered_by_comparing_blob_hashes() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        repo = Path(scratch) / "consumer"
        (repo / "vendor").mkdir(parents=True)
        same = _edge(
            "vendored-file",
            evidence="consumer/vendor/a.json",
            source="consumer/vendor/b.json",
        )
        (repo / "vendor" / "a.json").write_text("{}\n", encoding="utf-8", newline="\n")
        (repo / "vendor" / "b.json").write_text("{}\n", encoding="utf-8", newline="\n")
        equal = kinds.check_vendored_file(same, producer_version="1", consumer_repo=repo)
        expect("equal copies move nothing", equal.moved is False)
        expect("equal copies need nothing", equal.action is None)

        (repo / "vendor" / "b.json").write_text('{"x":1}\n', encoding="utf-8", newline="\n")
        different = kinds.check_vendored_file(same, producer_version="1", consumer_repo=repo)
        expect("unequal copies move", different.moved is True)
        expect("unequal copies re-vendor", different.action == kinds.ACTION_REVENDOR)


def test_a_vendored_edge_citing_one_side_has_nothing_to_compare() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        repo = Path(scratch) / "consumer"
        (repo / "vendor").mkdir(parents=True)
        (repo / "vendor" / "a.json").write_text("{}\n", encoding="utf-8", newline="\n")
        result = kinds.check_vendored_file(
            _edge("vendored-file", evidence="consumer/vendor/a.json"),
            producer_version="1",
            consumer_repo=repo,
        )
        expect("undetermined", result.moved is None)
        expect("names the fix", "`source`" in result.detail)


def test_the_kinds_no_script_can_answer_hand_back_a_person() -> None:
    mirrored = kinds.check_mirrored_logic(
        _edge("mirrored-logic"), producer_version="2.11.0", consumer_repo=Path("/nowhere")
    )
    expect("mirrored-logic", mirrored.action == kinds.ACTION_HUMAN_READ)
    expect("says why", "no script can answer" in mirrored.detail)

    derived = kinds.check_derived_pins(
        _edge("derived-pins"), producer_version="2.11.0", consumer_repo=Path("/nowhere")
    )
    expect("derived-pins moves nothing", derived.moved is False)
    expect("derived-pins asks nothing", derived.action is None)


def test_an_image_pin_hands_back_bump_app_naming_the_app() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        repo = Path(scratch) / "consumer"
        repo.mkdir()
        (repo / "Cargo.toml").write_text("\n" * 10, encoding="utf-8", newline="\n")
        result = kinds.check_image_pin(
            _edge("image-pin", **{"from": "dfe-loader"}),
            producer_version="v1.18.22",
            consumer_repo=repo,
        )
        expect("bump-app", result.action == kinds.ACTION_BUMP_APP)
        expect("names the app", "dfe-stack bump-app dfe-loader v1.18.22" in result.detail)


def test_an_unknown_kind_is_reported_not_guessed() -> None:
    result = kinds.check_edge(
        _edge("not-a-kind-xyzzy"), producer_version="2.11.0", consumer_repo=Path("/nowhere")
    )
    expect("undetermined", result.moved is None)
    expect("names the kind", "not-a-kind-xyzzy" in result.detail)


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
