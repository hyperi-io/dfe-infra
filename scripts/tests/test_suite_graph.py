#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_suite_graph.py
#  Purpose:      Cover the suite.yaml reader and the queries dfe-stack suite
#                and check_suite_drift.py build on, plus the live file's
#                structural soundness.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/suite_graph.py.

The reader is a hand-written subset parser, so the shapes suite.yaml uses --
nested maps, block sequences of maps and of scalars, flow lists, quoted strings
with hashes and colons inside -- are each asserted, the shapes it REFUSES are
asserted too, and the live file is loaded and validated so a shape the reader
cannot handle fails here before it fails in a tool.

    python3 scripts/tests/test_suite_graph.py

No third-party deps and no test runner, matching the tools it tests.
"""

from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import suite_graph  # noqa: E402

_failures = 0

# The vocabulary the file documents and the tooling on both sides branches on.
# A kind added to suite.yaml without a check on the hyperi-ai side is the
# failure this pins down.
EDGE_KIND_NAMES = [
    "cargo-dep",
    "contract-guard",
    "derived-pins",
    "generated-file",
    "image-pin",
    "mirrored-logic",
    "python-dep",
    "python-dep-undeclared",
    "vendored-file",
    "version-pin",
]

RUNTIME_KIND_NAMES = ["content-provision", "http-api", "management-api"]


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


def expect_raises(name: str, call, needle: str = "") -> None:
    """The reader must REFUSE a shape, naming it, rather than coerce a value."""
    global _failures
    try:
        call()
    except ValueError as exc:
        if needle and needle not in str(exc):
            _failures += 1
            print(f"FAIL  {name}  raised without {needle!r}: {exc}")
        else:
            print(f"PASS  {name}")
        return
    _failures += 1
    print(f"FAIL  {name}  did not raise")


SAMPLE = '''
schema: "1"
tags:
  audience:
    general: "Stands alone: value with a colon # and a hash"
edge_kinds:
  cargo-dep:
    means: "range"
    gates: [cargo-build, cargo-test]
runtime_kinds:
  http-api:
    means: "The consumer calls the producer's API at run time."
nodes:
  lib-a:
    repo: org/lib-a
    package: lib_a
    role: library
    language: rust
    audience: general
    maturity: ga
    support: standard
    licence: Apache-2.0
    classification: oss
    classification_source: in-repo
    default_in_pass: true
    artefacts:
      - kind: crate
        registry: crates.io
        public: true
    # a comment between nodes
  app-b:
    repo: org/app-b
    role: service
    language: rust
    audience: suite
    maturity: alpha
    support: standard
    licence: BUSL-1.1
    classification: product
    classification_source: org-property
    default_in_pass: false
    artefacts:
      - kind: container
        registry: ghcr
        public: false
        published: false
  side-c:
    repo: org/side-c
    role: service
    language: python
    audience: general
    maturity: ga
    support: standard
    licence: Apache-2.0
    classification: general-oss
    classification_source: org-property
    optional: true
    default_in_pass: true
    artefacts:
      - kind: container
        registry: ghcr
        public: true
edges:
  - from: lib-a
    to: app-b
    kind: cargo-dep
    type: potential
    evidence: "app-b/Cargo.toml:35"
    note: "second range at Cargo.toml:180"
runtime_edges:
  - from: app-b
    to: side-c
    kind: http-api
    note: "side-c calls app-b"
lanes:
  - name: libraries
    members: [lib-a]
    why: "first"
  - name: consumers
    members:
      - app-b
      - side-c
    why: "second"
'''


def test_reader_shapes() -> None:
    g = suite_graph.parse_block_yaml(SAMPLE)
    expect("top-level scalar", g["schema"] == "1")
    expect("quoted string keeps its colon and hash",
           g["tags"]["audience"]["general"] == "Stands alone: value with a colon # and a hash")
    expect("flow list of words", g["edge_kinds"]["cargo-dep"]["gates"] == ["cargo-build", "cargo-test"])
    expect("booleans", g["nodes"]["lib-a"]["default_in_pass"] is True
           and g["nodes"]["app-b"]["default_in_pass"] is False)
    expect("block sequence of maps", g["nodes"]["lib-a"]["artefacts"] == [
        {"kind": "crate", "registry": "crates.io", "public": True}])
    expect("comment between nodes does not end the map", "app-b" in g["nodes"])
    expect("edge map with note", g["edges"][0]["note"] == "second range at Cargo.toml:180")
    expect("block sequence of scalars", g["lanes"][1]["members"] == ["app-b", "side-c"])
    expect("two lanes", [lane["name"] for lane in g["lanes"]] == ["libraries", "consumers"])
    expect("runtime edge with no evidence", g["runtime_edges"][0]["kind"] == "http-api")


def test_reader_refuses_what_pyyaml_would_read_differently() -> None:
    parse = suite_graph.parse_block_yaml
    expect_raises("a single-quoted scalar is refused",
                  lambda: parse("role: 'library'\n"), "single-quoted")
    expect_raises("an escaped quote inside a quoted string is refused",
                  lambda: parse('note: "he said \\"hi\\" then left"\n'), "escaped quote")
    expect_raises("a quoted item in a flow list is refused",
                  lambda: parse('gates: ["a,b", c]\n'), "flow list")
    expect_raises("a tab indent is refused",
                  lambda: parse("nodes:\n\ta: 1\n"), "tab")
    expect_raises("a duplicate key is refused",
                  lambda: parse("nodes:\n  a:\n    x: 1\n  a:\n    x: 2\n"), "duplicate")
    expect("only lowercase true and false are booleans",
           parse("flag: True\nother: yes\n") == {"flag": "True", "other": "yes"})


def test_validate_catches_the_obvious() -> None:
    g = suite_graph.parse_block_yaml(SAMPLE)
    expect("sample validates clean", suite_graph.validate(g) == [], str(suite_graph.validate(g)))
    g["edges"].append({"from": "lib-a", "to": "ghost", "kind": "cargo-dep", "type": "potential", "evidence": "x/y:1"})
    expect("unknown consumer is a problem", any("ghost" in p for p in suite_graph.validate(g)))
    g["edges"][0]["type"] = "maybe"
    expect("unknown edge type is a problem", any("type must be" in p for p in suite_graph.validate(g)))
    g["nodes"]["app-b"]["default_in_pass"] = True
    expect("alpha in a default pass is a problem", any("alpha" in p for p in suite_graph.validate(g)))


def test_validate_negative_cases() -> None:
    """One case per branch, each starting from the clean sample."""

    def broken(mutate) -> list[str]:
        g = suite_graph.parse_block_yaml(SAMPLE)
        mutate(g)
        return suite_graph.validate(g)

    def drop_required(g):
        del g["nodes"]["lib-a"]["support"]

    expect("a missing required field is named",
           any("missing `support`" in p for p in broken(drop_required)))
    expect("an unknown edge kind is named", any(
        "not in edge_kinds" in p
        for p in broken(lambda g: g["edges"][0].__setitem__("kind", "telepathy"))))
    expect("an edge citing nothing is named", any(
        "no evidence" in p
        for p in broken(lambda g: g["edges"][0].__setitem__("evidence", ""))))
    expect("a runtime edge into a non-node is named", any(
        "is not a node" in p
        for p in broken(lambda g: g["runtime_edges"][0].__setitem__("to", "ghost"))))
    expect("a runtime kind from the BUILD vocabulary is refused", any(
        "runtime_kinds" in p
        for p in broken(lambda g: g["runtime_edges"][0].__setitem__("kind", "cargo-dep"))))
    expect("a lane member that is not a node is named", any(
        "member ghost is not a node" in p
        for p in broken(lambda g: g["lanes"][0]["members"].append("ghost"))))
    expect("default_in_pass that is not a boolean is named", any(
        "not true or false" in p
        for p in broken(lambda g: g["nodes"]["lib-a"].__setitem__("default_in_pass", "yes"))))
    expect("an artefact flag that is not a boolean is named", any(
        "artefact `public`" in p
        for p in broken(lambda g: g["nodes"]["lib-a"]["artefacts"][0].__setitem__("public", "True"))))
    expect("an audience outside the vocabulary is named", any(
        "audience" in p
        for p in broken(lambda g: g["nodes"]["lib-a"].__setitem__("audience", "everyone"))))
    expect("a node in no lane is named", any(
        "in no lane" in p
        for p in broken(lambda g: g["lanes"][0]["members"].remove("lib-a"))))


def test_queries() -> None:
    g = suite_graph.parse_block_yaml(SAMPLE)
    rows = suite_graph.cycle_table(g)
    expect("the build-cycle table joins the kind", rows[0]["gates"] == ["cargo-build", "cargo-test"])
    s = suite_graph.slice_graph(g, producer="lib-a")
    expect("producer slice keeps producer and consumers", sorted(s["nodes"]) == ["app-b", "lib-a"])
    expect("producer slice keeps only the kinds it uses", list(s["edge_kinds"]) == ["cargo-dep"])
    s = suite_graph.slice_graph(g, consumer="app-b")
    expect("consumer slice keeps the consumer and what reaches it",
           sorted(s["nodes"]) == ["app-b", "lib-a"] and len(s["edges"]) == 1)
    expect("consumer slice of a node nothing reaches is just the node",
           list(suite_graph.slice_graph(g, consumer="lib-a")["nodes"]) == ["lib-a"])
    s = suite_graph.slice_graph(g, lane="libraries")
    expect("lane slice keeps only the lane's members", list(s["nodes"]) == ["lib-a"] and s["edges"] == [])
    expect("in_edges answers what reaches a consumer",
           [e["from"] for e in suite_graph.in_edges(g, "app-b")] == ["lib-a"])


def test_unknown_slice_names_what_exists() -> None:
    g = suite_graph.parse_block_yaml(SAMPLE)
    for label, kwargs, needle in (
        ("an unknown producer", {"producer": "ghost"}, "lib-a"),
        ("an unknown consumer", {"consumer": "ghost"}, "lib-a"),
        ("an unknown lane", {"lane": "ghost"}, "libraries"),
    ):
        try:
            suite_graph.slice_graph(g, **kwargs)
        except KeyError as exc:
            expect(f"{label} names what exists", needle in exc.args[0], exc.args[0])
        else:
            expect(f"{label} names what exists", False, "did not raise")


def test_mermaid_and_docs_render() -> None:
    g = suite_graph.parse_block_yaml(SAMPLE)
    overview = suite_graph.mermaid_overview(g)
    expect("overview groups by role", "subgraph library" in overview and "subgraph service" in overview)
    expect("overview shades by audience", ':::general' in overview and ':::suite' in overview)
    expect("an optional member is labelled as one", 'side-c (optional)' in overview)
    expect("a block declares only the classes it paints",
           "classDef producer" not in overview and "classDef suite-optional" not in overview,
           overview)
    producer = suite_graph.mermaid_producer(g, "lib-a")
    expect("producer diagram labels the edge with plain words -- no quotes, no parentheses",
           "|cargo-dep, potential|" in producer and not any(c in producer.split("\n")[3] for c in '"()'))
    expect("every class carries an explicit text colour",
           all("color:" in line for line in producer.splitlines() if "classDef" in line))
    g["edges"].append(dict(g["edges"][0], evidence="app-b/other:1"))
    expect("repeated edges collapse to one arrow with a count",
           "|cargo-dep, potential x2|" in suite_graph.mermaid_producer(g, "lib-a"))
    page = "intro\n<!-- suite-graph:begin producer:lib-a -->\nstale\n<!-- suite-graph:end producer:lib-a -->\nouttro\n"
    rendered = suite_graph.render_docs(g, page)
    expect("marked block is replaced", "stale" not in rendered and "```mermaid" in rendered)
    expect("prose outside the block survives", rendered.startswith("intro\n") and rendered.endswith("outtro\n"))
    expect("render is idempotent", suite_graph.render_docs(g, rendered) == rendered)
    empty = "intro\n<!-- suite-graph:begin producer:lib-a -->\n<!-- suite-graph:end producer:lib-a -->\nouttro\n"
    filled = suite_graph.render_docs(g, empty)
    expect("an EMPTY marker pair is rendered too", "```mermaid" in filled and "lib_a" in filled, filled)
    expect("an empty pair renders the same as a stale one", filled == rendered)


# The two canvases these diagrams are read on.
CANVASES = {"light": "#FFFFFF", "dark": "#0D1117"}

# A fill has to stand off its canvas by this much to be a plate at all.
FILL_FLOOR = 2.5

# The (class, canvas) pairs whose fill is deliberately below that floor, with
# the ratio each one measures. The tertiary stroke carries the boundary there
# and is asserted in its place. Pinning the ratio catches a fill that drifts to
# the canvas colour instead of letting it ride on the stroke.
SOFT_FILLS = {
    ("general", "light"): 1.55,
    ("general-optional", "light"): 1.32,
    ("producer", "dark"): 1.01,
    ("suite-optional", "light"): 1.72,
}


def _luminance(colour: str) -> float:
    """WCAG relative luminance of a #RRGGBB colour."""
    total = 0.0
    for offset, weight in zip((1, 3, 5), (0.2126, 0.7152, 0.0722), strict=True):
        value = int(colour[offset:offset + 2], 16) / 255
        total += weight * (value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
    return total


def contrast(one: str, other: str) -> float:
    """WCAG contrast ratio between two #RRGGBB colours."""
    high, low = sorted((_luminance(one), _luminance(other)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _class_styles() -> dict[str, dict[str, str]]:
    """Each house classDef as {name: {fill, stroke, color, ...}}."""
    styles = {}
    for line in suite_graph._CLASSES.values():
        _, name, declarations = line.split(" ", 2)
        styles[name] = dict(d.split(":", 1) for d in declarations.split(","))
    return styles


def test_contrast_on_both_canvases() -> None:
    """No class may disappear or go unreadable on either GitHub theme."""
    expect("the helper reproduces the known ratio for navy on the light canvas",
           round(contrast("#000647", "#FFFFFF"), 2) == 18.80,
           str(round(contrast("#000647", "#FFFFFF"), 2)))
    for name, style in _class_styles().items():
        ratio = contrast(style["color"], style["fill"])
        expect(f"{name} text on its own fill clears 4.5:1", ratio >= 4.5, f"{ratio:.2f}")
        for canvas, background in CANVASES.items():
            fill = contrast(style["fill"], background)
            soft = SOFT_FILLS.get((name, canvas))
            if soft is None:
                expect(f"{name} fill stands off the {canvas} canvas",
                       fill >= FILL_FLOOR, f"{fill:.2f}")
                continue
            expect(f"{name} fill is the soft one it is declared to be on the {canvas} canvas",
                   round(fill, 2) == soft, f"{fill:.2f}, declared {soft:.2f}")
            edge = contrast(style["stroke"], background)
            expect(f"{name} stroke carries its boundary on the {canvas} canvas",
                   edge >= FILL_FLOOR, f"{edge:.2f}")
    navy_fills = [n for n, s in _class_styles().items() if s["fill"] == suite_graph.PALETTE["navy"]]
    expect("navy is a fill for the producer alone", navy_fills == ["producer"], str(navy_fills))


def test_brand_palette() -> None:
    """Every colour a diagram draws is a graphics.hyperi.io token or a 0.45 fade of one."""
    expect("PALETTE is the brand tokens",
           suite_graph.PALETTE == {"navy": "#000647", "tertiary": "#2EA4F6",
                                   "accent": "#2DED88", "white": "#FFFFFF",
                                   "black": "#000000"},
           str(suite_graph.PALETTE))
    expect("the fade mixes 45 per cent white into the brand tertiary",
           suite_graph.faded("#2EA4F6") == "#8CCDFA", suite_graph.faded("#2EA4F6"))
    expect("fading white leaves white", suite_graph.faded("#FFFFFF") == "#FFFFFF")
    g = suite_graph.parse_block_yaml(SAMPLE)
    g["edges"].append({"from": "lib-a", "to": "side-c", "kind": "cargo-dep",
                       "type": "potential", "evidence": "side-c/pyproject.toml:1"})
    allowed = set(suite_graph.PALETTE.values()) | {
        suite_graph.faded(c) for c in suite_graph.PALETTE.values()}
    for label, block in (("overview", suite_graph.mermaid_overview(g)),
                         ("producer diagram", suite_graph.mermaid_producer(g, "lib-a"))):
        found = set(re.findall(r"#[0-9A-Fa-f]{3,6}", block))
        expect(f"the {label} draws no colour outside the palette and its fades",
               found <= allowed, str(sorted(found - allowed)))
        expect(f"every classDef in the {label} carries a text colour",
               all("color:" in line for line in block.splitlines() if "classDef" in line))
        expect(f"every classDef in the {label} strokes in the both-modes tertiary",
               all(f"stroke:{suite_graph.PALETTE['tertiary']}," in line
                   for line in block.splitlines() if "classDef" in line))
        expect(f"an optional member is faded AND labelled in the {label}",
               '["side-c (optional)"]:::general-optional' in block, block)


def test_an_unmatched_marker_is_refused() -> None:
    """A begin marker with no end marker can never be rewritten, so it must not read as in sync."""
    g = suite_graph.parse_block_yaml(SAMPLE)
    for label, page in (
        ("no end marker at all",
         "<!-- suite-graph:begin producer:lib-a -->\nstale\nouttro\n"),
        ("the end marker names a different block",
         "<!-- suite-graph:begin producer:lib-a -->\nstale\n"
         "<!-- suite-graph:end producer:lib_a -->\n"),
    ):
        expect_raises(f"{label} raises and names the block",
                      lambda page=page: suite_graph.render_docs(g, page), "producer:lib-a")


def test_live_file() -> None:
    g = suite_graph.load()
    problems = suite_graph.validate(g)
    expect("live suite.yaml validates", problems == [], "; ".join(problems))
    expect("live file has a verified date", bool(g.get("verified")))
    try:
        datetime.date.fromisoformat(str(g.get("verified")))
        parsed = True
    except ValueError:
        parsed = False
    expect("the verified date parses as YYYY-MM-DD", parsed, str(g.get("verified")))
    expect("every lane member is a node", all(
        m in g["nodes"] for lane in g["lanes"] for m in lane["members"]))
    expect("the edge-kind vocabulary is the documented ten",
           sorted(g["edge_kinds"]) == EDGE_KIND_NAMES, str(sorted(g["edge_kinds"])))
    expect("the runtime-kind vocabulary is separate and declared",
           sorted(g["runtime_kinds"]) == RUNTIME_KIND_NAMES, str(sorted(g.get("runtime_kinds", {}))))
    expect("every library node names the package it publishes", all(
        "package" in g["nodes"][e["from"]]
        for e in g["edges"] if e["kind"] in ("cargo-dep", "python-dep")))


def test_no_version_literal_in_the_live_file() -> None:
    """The rule is about the raw TEXT: a semver token anywhere is a second place to look a pin up."""
    text = suite_graph.SUITE_FILE.read_text(encoding="utf-8", errors="replace")
    verified = str(suite_graph.load().get("verified") or "")
    found = [
        token
        for token in re.findall(r"\b\d+\.\d+\.\d+[A-Za-z0-9.+-]*", text)
        if token != verified
    ]
    expect("no version literal anywhere in the raw file", found == [], str(found[:5]))


if __name__ == "__main__":
    test_reader_shapes()
    test_reader_refuses_what_pyyaml_would_read_differently()
    test_validate_catches_the_obvious()
    test_validate_negative_cases()
    test_queries()
    test_unknown_slice_names_what_exists()
    test_mermaid_and_docs_render()
    test_brand_palette()
    test_contrast_on_both_canvases()
    test_an_unmatched_marker_is_refused()
    test_live_file()
    test_no_version_literal_in_the_live_file()
    if _failures:
        print(f"\n{_failures} failure(s)")
        raise SystemExit(1)
    print("\nall passed")
