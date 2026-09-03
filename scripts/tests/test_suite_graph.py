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
with hashes and colons inside -- are each asserted, and the live file is loaded
and validated so a shape the reader cannot handle fails here before it fails
in a tool.

    python3 scripts/tests/test_suite_graph.py

No third-party deps and no test runner, matching the tools it tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import suite_graph  # noqa: E402

_failures = 0


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


SAMPLE = '''
schema: 1
tags:
  audience:
    general: "Stands alone: value with a colon # and a hash"
edge_kinds:
  cargo-dep:
    means: "range"
    gates: [cargo-build, cargo-test]
nodes:
  lib-a:
    repo: org/lib-a
    role: library
    language: rust
    audience: general
    maturity: ga
    support: standard
    licence: Apache-2.0
    classification: oss
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
    default_in_pass: false
    artefacts:
      - kind: container
        registry: ghcr
        public: false
        published: false
edges:
  - from: lib-a
    to: app-b
    kind: cargo-dep
    type: potential
    evidence: "app-b/Cargo.toml:35"
    note: "second range at Cargo.toml:180"
lanes:
  - name: libraries
    members: [lib-a]
    why: "first"
  - name: consumers
    members:
      - app-b
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
    expect("block sequence of scalars", g["lanes"][1]["members"] == ["app-b"])
    expect("two lanes", [l["name"] for l in g["lanes"]] == ["libraries", "consumers"])


def test_validate_catches_the_obvious() -> None:
    g = suite_graph.parse_block_yaml(SAMPLE)
    expect("sample validates clean", suite_graph.validate(g) == [], str(suite_graph.validate(g)))
    g["edges"].append({"from": "lib-a", "to": "ghost", "kind": "cargo-dep", "type": "potential", "evidence": "x/y:1"})
    expect("unknown consumer is a problem", any("ghost" in p for p in suite_graph.validate(g)))
    g["edges"][0]["type"] = "maybe"
    expect("unknown edge type is a problem", any("type must be" in p for p in suite_graph.validate(g)))
    g["nodes"]["app-b"]["default_in_pass"] = True
    expect("alpha in a default pass is a problem", any("alpha" in p for p in suite_graph.validate(g)))


def test_queries() -> None:
    g = suite_graph.parse_block_yaml(SAMPLE)
    rows = suite_graph.cycles(g)
    expect("cycle table joins the kind", rows[0]["gates"] == ["cargo-build", "cargo-test"])
    s = suite_graph.slice_graph(g, producer="lib-a")
    expect("producer slice keeps producer and consumers", sorted(s["nodes"]) == ["app-b", "lib-a"])
    expect("producer slice keeps only the kinds it uses", list(s["edge_kinds"]) == ["cargo-dep"])
    s = suite_graph.slice_graph(g, lane="libraries")
    expect("lane slice keeps only the lane's members", list(s["nodes"]) == ["lib-a"] and s["edges"] == [])


def test_mermaid_and_docs_render() -> None:
    g = suite_graph.parse_block_yaml(SAMPLE)
    overview = suite_graph.mermaid_overview(g)
    expect("overview groups by role", "subgraph library" in overview and "subgraph service" in overview)
    expect("overview shades by audience", ':::general' in overview and ':::suite' in overview)
    producer = suite_graph.mermaid_producer(g, "lib-a")
    expect("producer diagram labels the edge", "|cargo-dep (potential)|" in producer)
    page = "intro\n<!-- suite-graph:begin producer:lib-a -->\nstale\n<!-- suite-graph:end producer:lib-a -->\nouttro\n"
    rendered = suite_graph.render_docs(g, page)
    expect("marked block is replaced", "stale" not in rendered and "```mermaid" in rendered)
    expect("prose outside the block survives", rendered.startswith("intro\n") and rendered.endswith("outtro\n"))
    expect("render is idempotent", suite_graph.render_docs(g, rendered) == rendered)


def test_live_file() -> None:
    g = suite_graph.load()
    problems = suite_graph.validate(g)
    expect("live suite.yaml validates", problems == [], "; ".join(problems))
    expect("live file has a verified date", bool(g.get("verified")))
    expect("no version literal in a node", not any(
        "version" in n for n in g["nodes"].values()))
    expect("every lane member is a node", all(
        m in g["nodes"] for l in g["lanes"] for m in l["members"]))


if __name__ == "__main__":
    test_reader_shapes()
    test_validate_catches_the_obvious()
    test_queries()
    test_mermaid_and_docs_render()
    test_live_file()
    if _failures:
        print(f"\n{_failures} failure(s)")
        raise SystemExit(1)
    print("\nall passed")
