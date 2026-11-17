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

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import suite_graph  # noqa: E402

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
    "vendored-file",
    "version-pin",
]

RUNTIME_KIND_NAMES = ["content-provision", "http-api", "management-api"]


def expect_raises(name: str, call, needle: str = "") -> None:
    """The reader must REFUSE a shape, naming it, rather than coerce a value."""
    try:
        call()
    except ValueError as exc:
        expect(
            name, not needle or needle in str(exc), f"raised without {needle!r}: {exc}"
        )
        return
    expect(name, False, "did not raise")


SAMPLE = """
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
    evidence: "app-b/Cargo.toml"
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
"""


def test_reader_shapes() -> None:
    g = suite_graph.parse_block_yaml(SAMPLE)
    expect("top-level scalar", g["schema"] == "1")
    expect(
        "quoted string keeps its colon and hash",
        g["tags"]["audience"]["general"]
        == "Stands alone: value with a colon # and a hash",
    )
    expect(
        "flow list of words",
        g["edge_kinds"]["cargo-dep"]["gates"] == ["cargo-build", "cargo-test"],
    )
    expect(
        "booleans",
        g["nodes"]["lib-a"]["default_in_pass"] is True
        and g["nodes"]["app-b"]["default_in_pass"] is False,
    )
    expect(
        "block sequence of maps",
        g["nodes"]["lib-a"]["artefacts"]
        == [{"kind": "crate", "registry": "crates.io", "public": True}],
    )
    expect("comment between nodes does not end the map", "app-b" in g["nodes"])
    expect(
        "edge map with note", g["edges"][0]["note"] == "second range at Cargo.toml:180"
    )
    expect("block sequence of scalars", g["lanes"][1]["members"] == ["app-b", "side-c"])
    expect(
        "two lanes", [lane["name"] for lane in g["lanes"]] == ["libraries", "consumers"]
    )
    expect("runtime edge with no evidence", g["runtime_edges"][0]["kind"] == "http-api")


def test_reader_refuses_what_pyyaml_would_read_differently() -> None:
    parse = suite_graph.parse_block_yaml
    expect_raises(
        "a single-quoted scalar is refused",
        lambda: parse("role: 'library'\n"),
        "single-quoted",
    )
    expect_raises(
        "an escaped quote inside a quoted string is refused",
        lambda: parse('note: "he said \\"hi\\" then left"\n'),
        "escaped quote",
    )
    expect_raises(
        "a quoted item in a flow list is refused",
        lambda: parse('gates: ["a,b", c]\n'),
        "flow list",
    )
    expect_raises("a tab indent is refused", lambda: parse("nodes:\n\ta: 1\n"), "tab")
    expect_raises(
        "a duplicate key is refused",
        lambda: parse("nodes:\n  a:\n    x: 1\n  a:\n    x: 2\n"),
        "duplicate",
    )
    expect(
        "only lowercase true and false are booleans",
        parse("flag: True\nother: yes\n") == {"flag": "True", "other": "yes"},
    )


def test_validate_catches_the_obvious() -> None:
    g = suite_graph.parse_block_yaml(SAMPLE)
    expect(
        "sample validates clean",
        suite_graph.validate(g) == [],
        str(suite_graph.validate(g)),
    )
    g["edges"].append(
        {
            "from": "lib-a",
            "to": "ghost",
            "kind": "cargo-dep",
            "type": "potential",
            "evidence": "x/y:1",
        }
    )
    expect(
        "unknown consumer is a problem",
        any("ghost" in p for p in suite_graph.validate(g)),
    )
    g["edges"][0]["type"] = "maybe"
    expect(
        "unknown edge type is a problem",
        any("type must be" in p for p in suite_graph.validate(g)),
    )
    g["nodes"]["app-b"]["default_in_pass"] = True
    expect(
        "alpha in a default pass is a problem",
        any("alpha" in p for p in suite_graph.validate(g)),
    )


def test_validate_negative_cases() -> None:
    """One case per branch, each starting from the clean sample."""

    def broken(mutate) -> list[str]:
        g = suite_graph.parse_block_yaml(SAMPLE)
        mutate(g)
        return suite_graph.validate(g)

    def drop_required(g):
        del g["nodes"]["lib-a"]["support"]

    expect(
        "a missing required field is named",
        any("missing `support`" in p for p in broken(drop_required)),
    )
    expect(
        "an unknown edge kind is named",
        any(
            "not in edge_kinds" in p
            for p in broken(lambda g: g["edges"][0].__setitem__("kind", "telepathy"))
        ),
    )
    expect(
        "an edge citing nothing is named",
        any(
            "no evidence" in p
            for p in broken(lambda g: g["edges"][0].__setitem__("evidence", ""))
        ),
    )
    for cited in ("app-b/Cargo.toml:35", "app-b/Cargo.toml:35-36"):
        expect(
            f"a cargo-dep edge citing {cited} is named, the manifest alone being the citation",
            any(
                "cites the manifest alone" in p
                for p in broken(
                    lambda g, cited=cited: g["edges"][0].__setitem__("evidence", cited)
                )
            ),
        )
    expect(
        "a line on a kind that cites a code site is not refused",
        not any(
            "cites the manifest alone" in p
            for p in broken(
                lambda g: g["edges"][0].update(
                    kind="mirrored-logic", evidence="app-b/src/config.rs:35"
                )
            )
        ),
    )
    expect(
        "a runtime edge into a non-node is named",
        any(
            "is not a node" in p
            for p in broken(lambda g: g["runtime_edges"][0].__setitem__("to", "ghost"))
        ),
    )
    expect(
        "a runtime kind from the BUILD vocabulary is refused",
        any(
            "runtime_kinds" in p
            for p in broken(
                lambda g: g["runtime_edges"][0].__setitem__("kind", "cargo-dep")
            )
        ),
    )
    expect(
        "a lane member that is not a node is named",
        any(
            "member ghost is not a node" in p
            for p in broken(lambda g: g["lanes"][0]["members"].append("ghost"))
        ),
    )
    expect(
        "default_in_pass that is not a boolean is named",
        any(
            "not true or false" in p
            for p in broken(
                lambda g: g["nodes"]["lib-a"].__setitem__("default_in_pass", "yes")
            )
        ),
    )
    expect(
        "an artefact flag that is not a boolean is named",
        any(
            "artefact `public`" in p
            for p in broken(
                lambda g: g["nodes"]["lib-a"]["artefacts"][0].__setitem__(
                    "public", "True"
                )
            )
        ),
    )
    expect(
        "an audience outside the vocabulary is named",
        any(
            "audience" in p
            for p in broken(
                lambda g: g["nodes"]["lib-a"].__setitem__("audience", "everyone")
            )
        ),
    )
    expect(
        "a node in no lane is named",
        any(
            "in no lane" in p
            for p in broken(lambda g: g["lanes"][0]["members"].remove("lib-a"))
        ),
    )
    expect(
        "an empty unconsumed note is named",
        any(
            "`unconsumed` is empty" in p
            for p in broken(lambda g: g["nodes"]["lib-a"].__setitem__("unconsumed", ""))
        ),
    )
    expect(
        "an unconsumed note on a node with no package is named",
        any(
            "`unconsumed` needs a `package`" in p
            for p in broken(
                lambda g: g["nodes"]["app-b"].__setitem__(
                    "unconsumed", "nothing uses it"
                )
            )
        ),
    )
    expect(
        "an unconsumed note on a node with a package is sound",
        not any(
            "unconsumed" in p
            for p in broken(
                lambda g: g["nodes"]["lib-a"].__setitem__(
                    "unconsumed", "nothing uses it"
                )
            )
        ),
    )


def test_queries() -> None:
    g = suite_graph.parse_block_yaml(SAMPLE)
    rows = suite_graph.cycle_table(g)
    expect(
        "the build-cycle table joins the kind",
        rows[0]["gates"] == ["cargo-build", "cargo-test"],
    )
    s = suite_graph.slice_graph(g, producer="lib-a")
    expect(
        "producer slice keeps producer and consumers",
        sorted(s["nodes"]) == ["app-b", "lib-a"],
    )
    expect(
        "producer slice keeps only the kinds it uses",
        list(s["edge_kinds"]) == ["cargo-dep"],
    )
    s = suite_graph.slice_graph(g, consumer="app-b")
    expect(
        "consumer slice keeps the consumer and what reaches it",
        sorted(s["nodes"]) == ["app-b", "lib-a"] and len(s["edges"]) == 1,
    )
    expect(
        "consumer slice of a node nothing reaches is just the node",
        list(suite_graph.slice_graph(g, consumer="lib-a")["nodes"]) == ["lib-a"],
    )
    s = suite_graph.slice_graph(g, lane="libraries")
    expect(
        "lane slice keeps only the lane's members",
        list(s["nodes"]) == ["lib-a"] and s["edges"] == [],
    )
    expect(
        "in_edges answers what reaches a consumer",
        [e["from"] for e in suite_graph.in_edges(g, "app-b")] == ["lib-a"],
    )


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


# The house class lines, byte for byte. A block copies all of them, so each must
# equal the line the diagram house style gives.
HOUSE_CLASS_LINES = {
    "ext": "classDef ext fill:#F4F6F8,stroke:#94A3B8,stroke-width:1.5px,stroke-dasharray:5 4,color:#0F172A",
    "ingest": "classDef ingest fill:#FFFFFF,stroke:#0E8A7D,stroke-width:3px,color:#0F172A",
    "xform": "classDef xform fill:#FFFFFF,stroke:#7B4FB0,stroke-width:3px,color:#0F172A",
    "load": "classDef load fill:#FFFFFF,stroke:#0072B2,stroke-width:3px,color:#0F172A",
    "bus": "classDef bus fill:#FFFFFF,stroke:#A8517F,stroke-width:3px,color:#0F172A",
    "store": "classDef store fill:#FFFFFF,stroke:#00785A,stroke-width:3px,color:#0F172A",
    "ui": "classDef ui fill:#FFFFFF,stroke:#D9480F,stroke-width:3px,color:#0F172A",
    "decision": "classDef decision fill:#FFFFFF,stroke:#9A6700,stroke-width:3px,color:#0F172A",
    "error": "classDef error fill:#FFFFFF,stroke:#C2410C,stroke-width:3px,color:#0F172A",
    "control": "classDef control fill:#FFFFFF,stroke:#334155,stroke-width:3px,color:#0F172A",
    "part": "classDef part fill:#FFFFFF,stroke:#94A3B8,stroke-width:1.5px,color:#0F172A",
}


def block_lines(block: str) -> list[str]:
    return [line.strip() for line in block.splitlines()]


def header_at(lines: list[str]) -> int:
    """Index of the `flowchart TB` or `flowchart LR` line."""
    return next(
        i for i, line in enumerate(lines) if line in ("flowchart TB", "flowchart LR")
    )


def test_mermaid_and_docs_render() -> None:
    g = suite_graph.parse_block_yaml(SAMPLE)
    services = suite_graph.mermaid_overview(g, "services")
    platform = suite_graph.mermaid_overview(g, "platform")
    expect(
        "the services overview holds the service nodes, boxed by language",
        'subgraph rust_services["Rust services"]' in services
        and 'subgraph python_services["Python services"]' in services
        and "<b>lib-a</b>" not in services,
        services,
    )
    expect(
        "the platform overview holds every other node, boxed by role",
        'subgraph libraries["Libraries"]' in platform
        and "<b>app-b</b>" not in platform
        and "<b>lib-a</b>" in platform,
        platform,
    )
    expect(
        "an optional member says so in a subtitle and takes its role colour",
        'side_c("<b>side-c</b><br/><span>optional</span>")' in services
        and "class app_b,side_c part" in block_lines(services),
        services,
    )
    producer = suite_graph.mermaid_producer(g, "lib-a")
    lines = block_lines(producer)
    expect(
        "a producer block opens its flowchart with an accTitle and a one-line accDescr",
        lines[header_at(lines) + 1] == "accTitle: L4 Detail -- lib-a build cycle"
        and lines[header_at(lines) + 2].startswith("accDescr: ")
        and ":" not in lines[header_at(lines) + 2].removeprefix("accDescr: "),
        producer,
    )
    expect(
        "the producer takes its colour and a one-consumer block draws one solid labelled arrow",
        "class lib_a,app_b part" in lines and "lib_a -->|cargo-dep| app_b" in lines,
        producer,
    )
    g["edges"].append(dict(g["edges"][0], evidence="app-b/other:1"))
    expect(
        "repeated edges of one kind collapse to one arrow with a count",
        "lib_a -->|cargo-dep x2| app_b" in suite_graph.mermaid_producer(g, "lib-a"),
    )
    page = "intro\n<!-- suite-graph:begin producer:lib-a -->\nstale\n<!-- suite-graph:end producer:lib-a -->\nouttro\n"
    rendered = suite_graph.render_docs(g, page)
    expect(
        "marked block is replaced", "stale" not in rendered and "```mermaid" in rendered
    )
    expect(
        "prose outside the block survives",
        rendered.startswith("intro\n") and rendered.endswith("outtro\n"),
    )
    expect("render is idempotent", suite_graph.render_docs(g, rendered) == rendered)
    empty = "intro\n<!-- suite-graph:begin producer:lib-a -->\n<!-- suite-graph:end producer:lib-a -->\nouttro\n"
    filled = suite_graph.render_docs(g, empty)
    expect(
        "an EMPTY marker pair is rendered too",
        "```mermaid" in filled and "lib_a" in filled,
        filled,
    )
    expect("an empty pair renders the same as a stale one", filled == rendered)


def test_consumers_with_the_same_arrow_share_one_box() -> None:
    """Fan-out: one box and one arrow for consumers that get the same arrow."""

    def edge(to: str, kind: str, kind_type: str) -> dict:
        return {"from": "lib-a", "to": to, "kind": kind, "type": kind_type}

    cargo = ("cargo-dep", "potential")
    pin = ("image-pin", "lockstep")
    nodes = {
        name: {"role": role, "language": language, "audience": "suite"}
        for name, role, language in (
            ("a-1", "service", "rust"),
            ("a-2", "service", "rust"),
            ("b-1", "library", "python"),
            ("b-2", "service", "rust"),
            ("c-1", "service", "rust"),
            ("lib-a", "library", "rust"),
        )
    }
    base = {"nodes": nodes, "edge_kinds": {}}
    for label, edges, expected, absent in (
        (
            "two consumers with one kind become a box with one arrow",
            [edge("a-1", *cargo), edge("a-2", *cargo)],
            [
                'subgraph rust_services["Rust services"]',
                "lib_a -->|cargo-dep| rust_services",
            ],
            ["lib_a -->|cargo-dep| a_1"],
        ),
        (
            "a consumer alone stays a plain node",
            [edge("a-1", *cargo)],
            ["lib_a -->|cargo-dep| a_1"],
            ["subgraph"],
        ),
        (
            "two kinds to every consumer give one arrow labelled with both",
            [edge(to, *kind) for to in ("a-1", "a-2") for kind in (cargo, pin)],
            ["lib_a -->|cargo-dep, image-pin| rust_services"],
            ["potential", "lockstep"],
        ),
        (
            "a derived edge is dotted and no other is",
            [edge("a-1", "derived-pins", "derived"), edge("a-2", *cargo)],
            ["lib_a -.->|derived-pins| a_1", "lib_a -->|cargo-dep| a_2"],
            ["subgraph"],
        ),
        (
            "members of mixed language and role are titled by what they share",
            [edge("a-1", *cargo), edge("b-1", *cargo)],
            ['subgraph members["Members"]'],
            ["Rust"],
        ),
        (
            "the same title twice takes a second box id",
            [
                edge("a-1", *cargo),
                edge("a-2", *cargo),
                edge("b-2", *pin),
                edge("c-1", *pin),
            ],
            [
                'subgraph rust_services["Rust services"]',
                'subgraph rust_services_2["Rust services"]',
            ],
            [],
        ),
    ):
        block = suite_graph.mermaid_producer(dict(base, edges=edges), "lib-a")
        expect(
            label,
            all(want in block for want in expected)
            and not any(bad in block for bad in absent),
            block,
        )


def test_block_names_and_unknown_blocks() -> None:
    g = suite_graph.parse_block_yaml(SAMPLE)
    expect(
        "the page can carry both overview halves and one block per producer",
        suite_graph.block_names(g)
        == ["overview:services", "overview:platform", "producer:lib-a"],
        str(suite_graph.block_names(g)),
    )
    for name in ("overview", "producer:ghost", "nonsense"):
        try:
            suite_graph.render_block(g, name)
        except KeyError as exc:
            expect(
                f"the unknown block {name!r} raises and names what exists",
                "overview:services" in exc.args[0],
                exc.args[0],
            )
        else:
            expect(f"the unknown block {name!r} raises", False, "did not raise")
    page = "<!-- suite-graph:begin ghost -->\n<!-- suite-graph:end ghost -->\n"
    try:
        suite_graph.render_docs(g, page)
    except KeyError:
        expect("an unknown block on a page raises", True)
    else:
        expect("an unknown block on a page raises", False, "did not raise")


def _live_blocks() -> list[tuple[str, str]]:
    g = suite_graph.load()
    return [
        (name, suite_graph.render_block(g, name)) for name in suite_graph.block_names(g)
    ]


def test_class_lines_are_the_house_style() -> None:
    """A block copies the house frontmatter and class lines byte for byte."""
    expect(
        "the generator's class lines are the house style's",
        suite_graph.CLASS_LINES == HOUSE_CLASS_LINES,
        str(suite_graph.CLASS_LINES),
    )
    g = suite_graph.parse_block_yaml(SAMPLE)
    blocks = [
        suite_graph.mermaid_overview(g, "services"),
        suite_graph.mermaid_overview(g, "platform"),
        suite_graph.mermaid_producer(g, "lib-a"),
        *(block for _, block in _live_blocks()),
    ]
    for block in blocks:
        expect(
            "a block opens with the house frontmatter",
            block.startswith(
                suite_graph.HOUSE_FRONTMATTER.rstrip("\n") + "\nflowchart"
            ),
            block[:80],
        )
        defined = [ln for ln in block_lines(block) if ln.startswith("classDef")]
        expect(
            "every house classDef is in a block, in order",
            defined == list(HOUSE_CLASS_LINES.values()),
            block,
        )
        used = {ln.split()[2] for ln in block_lines(block) if ln.startswith("class ")}
        expect(
            "every class a block applies has its line, and no brand colour appears",
            used <= {ln.split()[1] for ln in defined}
            and not re.search(r"#(?:2EA4F6|2DED88|000647)", block, re.IGNORECASE),
            block,
        )
        expect(
            "no thick arrow, linkStyle, click or init directive, and style only draws a boundary",
            not re.search(r"==>|^\s*(linkStyle|click)\b|%%\{", block, re.MULTILINE)
            and all(
                re.fullmatch(
                    r"style \w+ fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4",
                    ln,
                )
                for ln in block_lines(block)
                if ln.startswith("style ")
            ),
            block,
        )


def test_generated_blocks_follow_the_standard() -> None:
    """Rules the checker reads from the source, held without a browser."""
    g = suite_graph.load()
    for name, block in _live_blocks():
        lines = block_lines(block)
        at = header_at(lines)
        expect(
            f"{name} has an accTitle at L4 and an accDescr on one line",
            lines[at + 1].startswith("accTitle: L4 Detail -- ")
            and lines[at + 2].startswith("accDescr: "),
            block,
        )
        labels = re.findall(r"-(?:->|\.->)\|([^|]*)\|", block)
        expect(
            f"{name} labels every arrow, with no parenthesis or quote in a label",
            len(labels) == len(re.findall(r"-(?:->|\.->)", block))
            and not any(c in label for label in labels for c in '("'),
            str(labels),
        )
        nodes = re.findall(r'^\s*\w+\("<b>', block, re.MULTILINE)
        expect(f"{name} stays within 12 nodes", len(nodes) <= 12, str(len(nodes)))
        expect(
            f"{name} renders the same twice",
            block == suite_graph.render_block(g, name),
        )


def test_live_page_is_in_sync() -> None:
    """The committed page equals a fresh render, and carries every block."""
    g = suite_graph.load()
    page = (REPO_ROOT / "docs" / "suite-graph.md").read_text(
        encoding="utf-8", errors="replace"
    )
    expect(
        "docs/suite-graph.md equals a fresh render of suite.yaml",
        suite_graph.render_docs(g, page) == page,
        "run `scripts/dfe-stack suite --render-docs`",
    )
    carried = suite_graph.doc_block_names(page)
    expect(
        "the page carries each block once",
        sorted(carried) == sorted(suite_graph.block_names(g)),
        str(sorted(set(suite_graph.block_names(g)) ^ set(carried))),
    )


def test_an_unmatched_marker_is_refused() -> None:
    """A begin marker with no end marker can never be rewritten, so it must not read as in sync."""
    g = suite_graph.parse_block_yaml(SAMPLE)
    for label, page in (
        (
            "no end marker at all",
            "<!-- suite-graph:begin producer:lib-a -->\nstale\nouttro\n",
        ),
        (
            "the end marker names a different block",
            "<!-- suite-graph:begin producer:lib-a -->\nstale\n"
            "<!-- suite-graph:end producer:lib_a -->\n",
        ),
    ):
        expect_raises(
            f"{label} raises and names the block",
            lambda page=page: suite_graph.render_docs(g, page),
            "producer:lib-a",
        )


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
    expect(
        "every lane member is a node",
        all(m in g["nodes"] for lane in g["lanes"] for m in lane["members"]),
    )
    expect(
        "the edge-kind vocabulary is the documented nine",
        sorted(g["edge_kinds"]) == EDGE_KIND_NAMES,
        str(sorted(g["edge_kinds"])),
    )
    expect(
        "the runtime-kind vocabulary is separate and declared",
        sorted(g["runtime_kinds"]) == RUNTIME_KIND_NAMES,
        str(sorted(g.get("runtime_kinds", {}))),
    )
    expect(
        "every library node names the package it publishes",
        all(
            "package" in g["nodes"][e["from"]]
            for e in g["edges"]
            if e["kind"] in ("cargo-dep", "python-dep")
        ),
    )


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


def main() -> int:
    with standalone():
        test_reader_shapes()
        test_reader_refuses_what_pyyaml_would_read_differently()
        test_validate_catches_the_obvious()
        test_validate_negative_cases()
        test_queries()
        test_unknown_slice_names_what_exists()
        test_mermaid_and_docs_render()
        test_consumers_with_the_same_arrow_share_one_box()
        test_block_names_and_unknown_blocks()
        test_class_lines_are_the_house_style()
        test_generated_blocks_follow_the_standard()
        test_live_page_is_in_sync()
        test_an_unmatched_marker_is_refused()
        test_live_file()
        test_no_version_literal_in_the_live_file()
        return summary()


if __name__ == "__main__":
    raise SystemExit(main())
