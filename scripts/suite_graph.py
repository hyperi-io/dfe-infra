#  Project:      dfe-infra
#  File:         suite_graph.py
#  Purpose:      Read suite.yaml (membership + build-cycle edges) with the
#                standard library and answer the questions the tooling asks:
#                out-edges of a producer, the cycle table, a lane or producer
#                slice, and the Mermaid blocks docs/suite-graph.md carries.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""suite.yaml reader and queries, shared by dfe-stack and check_suite_drift.py.

suite.yaml is written in a block-style subset -- nested maps, block sequences
of scalars or maps, one-line flow sequences of plain words, quoted one-line
strings -- so this reader stays small and runs on a bare CI image. PyYAML
reads the same file identically; nothing here is a second dialect.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITE_FILE = REPO_ROOT / "suite.yaml"

EDGE_TYPES = ("potential", "lockstep", "derived")
NODE_REQUIRED = ("repo", "role", "language", "audience", "maturity", "support",
                 "licence", "classification", "default_in_pass", "artefacts")

BEGIN = "<!-- suite-graph:begin {name} -->"
END = "<!-- suite-graph:end {name} -->"


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

_KEY = re.compile(r"^([A-Za-z0-9_.-]+):(?:\s+(.*))?$")


def _strip_comment(text: str) -> str:
    """Drop a trailing ` # ...` outside quotes; a quoted value keeps its hashes."""
    if text.startswith('"'):
        end = text.find('"', 1)
        return text if end < 0 else text[: end + 1]
    return text.split(" #", 1)[0].rstrip()


def _scalar(text: str):
    text = _strip_comment(text.strip())
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [] if not inner else [_scalar(p) for p in inner.split(",")]
    if text in ("true", "false"):
        return text == "true"
    return text


def parse_block_yaml(text: str):
    """Parse the block-style subset suite.yaml is written in."""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        lines.append((len(raw) - len(raw.lstrip()), raw.strip()))

    def parse_at(i: int, indent: int):
        if i >= len(lines):
            return {}, i
        if lines[i][1].startswith("- "):
            return parse_seq(i, indent)
        return parse_map(i, indent, {})

    def parse_map(i: int, indent: int, out: dict):
        while i < len(lines) and lines[i][0] == indent:
            content = lines[i][1]
            if content.startswith("- "):
                break
            m = _KEY.match(content)
            if not m:
                raise ValueError(f"suite.yaml: cannot read line {content!r}")
            key, rest = m.group(1), m.group(2)
            if rest is None or not _strip_comment(rest.strip()):
                if i + 1 < len(lines) and lines[i + 1][0] > indent:
                    out[key], i = parse_at(i + 1, lines[i + 1][0])
                else:
                    out[key], i = {}, i + 1
            else:
                out[key], i = _scalar(rest), i + 1
        return out, i

    def parse_seq(i: int, indent: int):
        items = []
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            rest = lines[i][1][2:].strip()
            m = _KEY.match(rest)
            if m and (m.group(2) is not None or rest.endswith(":")):
                first: dict = {}
                key, val = m.group(1), m.group(2)
                child_indent = indent + 2
                if val is None or not _strip_comment(val.strip()):
                    if i + 1 < len(lines) and lines[i + 1][0] > child_indent:
                        first[key], i = parse_at(i + 1, lines[i + 1][0])
                    else:
                        first[key], i = {}, i + 1
                else:
                    first[key], i = _scalar(val), i + 1
                if i < len(lines) and lines[i][0] == child_indent and not lines[i][1].startswith("- "):
                    first, i = parse_map(i, child_indent, first)
                items.append(first)
            else:
                items.append(_scalar(rest))
                i += 1
        return items, i

    result, i = parse_at(0, lines[0][0] if lines else 0)
    if i != len(lines):
        raise ValueError(f"suite.yaml: unread content from line {lines[i][1]!r}")
    return result


def load(path: Path = SUITE_FILE) -> dict:
    return parse_block_yaml(path.read_text(encoding="utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Validation -- the graph must be internally consistent before anything walks it
# ---------------------------------------------------------------------------


def validate(graph: dict) -> list[str]:
    """Structural problems, each a one-line sentence. Empty means sound."""
    problems: list[str] = []
    nodes = graph.get("nodes", {})
    kinds = graph.get("edge_kinds", {})
    for name, node in nodes.items():
        for field in NODE_REQUIRED:
            if field not in node:
                problems.append(f"node {name}: missing `{field}`")
        if node.get("maturity") in ("alpha", "beta") and node.get("default_in_pass") is True:
            problems.append(f"node {name}: {node['maturity']} members must not be default_in_pass")
    for edge in graph.get("edges", []):
        where = f"edge {edge.get('from')} -> {edge.get('to')}"
        for end in ("from", "to"):
            if edge.get(end) not in nodes:
                problems.append(f"{where}: `{end}` is not a node")
        if edge.get("kind") not in kinds:
            problems.append(f"{where}: kind {edge.get('kind')!r} is not in edge_kinds")
        if edge.get("type") not in EDGE_TYPES:
            problems.append(f"{where}: type must be one of {', '.join(EDGE_TYPES)}")
        if not edge.get("evidence"):
            problems.append(f"{where}: no evidence cited")
    for edge in graph.get("runtime_edges", []):
        for end in ("from", "to"):
            if edge.get(end) not in nodes:
                problems.append(f"runtime edge {edge.get('from')} -> {edge.get('to')}: `{end}` is not a node")
    for lane in graph.get("lanes", []):
        for member in lane.get("members", []):
            if member not in nodes:
                problems.append(f"lane {lane.get('name')}: member {member} is not a node")
    return problems


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def out_edges(graph: dict, producer: str) -> list[dict]:
    return [e for e in graph.get("edges", []) if e.get("from") == producer]


def in_edges(graph: dict, consumer: str) -> list[dict]:
    return [e for e in graph.get("edges", []) if e.get("to") == consumer]


def cycles(graph: dict) -> list[dict]:
    """The cycle table: one row per (producer, kind, type), consumers merged."""
    rows: dict[tuple[str, str, str], list[str]] = {}
    for edge in graph.get("edges", []):
        key = (edge["from"], edge["kind"], edge["type"])
        rows.setdefault(key, []).append(edge["to"])
    out = []
    for (producer, kind, etype), consumers in sorted(rows.items()):
        spec = graph["edge_kinds"].get(kind, {})
        out.append({
            "producer": producer,
            "consumers": sorted(set(consumers)),
            "kind": kind,
            "type": etype,
            "check": spec.get("check", ""),
            "gates": spec.get("gates", []),
        })
    return out


def slice_graph(graph: dict, *, producer: str | None = None, lane: str | None = None) -> dict:
    """The subgraph one task needs: a producer's out-edges, or one lane's members."""
    if producer:
        edges = out_edges(graph, producer)
        keep = {producer} | {e["to"] for e in edges}
    elif lane:
        lanes = {l["name"]: l for l in graph.get("lanes", [])}
        if lane not in lanes:
            raise KeyError(f"no lane named {lane!r}; lanes are {', '.join(lanes)}")
        keep = set(lanes[lane]["members"])
        edges = [e for e in graph["edges"] if e["from"] in keep and e["to"] in keep]
    else:
        return graph
    kinds = {e["kind"] for e in edges}
    return {
        "schema": graph.get("schema"),
        "verified": graph.get("verified"),
        "edge_kinds": {k: v for k, v in graph["edge_kinds"].items() if k in kinds},
        "nodes": {n: graph["nodes"][n] for n in sorted(keep)},
        "edges": edges,
    }


# ---------------------------------------------------------------------------
# Mermaid -- the blocks docs/suite-graph.md carries between markers
# ---------------------------------------------------------------------------


def _mid(name: str) -> str:
    return name.replace("-", "_").replace(".", "_")


# Okabe-Ito colours with an explicit text colour on every class, so the
# diagrams read on GitHub's dark theme as well as the light one.
_CLASSES = [
    "classDef general fill:#009E73,stroke:#333,color:#fff",
    "classDef suite fill:#999999,stroke:#333,color:#000",
    "classDef producer fill:#E69F00,stroke:#333,color:#000",
]


def mermaid_overview(graph: dict) -> str:
    """Nodes only, grouped by role, coloured by audience. Edges are the per-producer diagrams' job."""
    by_role: dict[str, list[str]] = {}
    for name, node in graph["nodes"].items():
        by_role.setdefault(node["role"], []).append(name)
    lines = ["flowchart TB"]
    for role in sorted(by_role):
        lines.append(f'  subgraph {role}["{role}"]')
        lines.append("    direction LR")
        for name in sorted(by_role[role]):
            node = graph["nodes"][name]
            suffix = " (optional)" if node.get("optional") else ""
            lines.append(f'    {_mid(name)}["{name}{suffix}"]:::{node["audience"]}')
        lines.append("  end")
    lines.extend(f"  {c}" for c in _CLASSES)
    return "\n".join(lines)


_ARROW = {"lockstep": "==>", "potential": "-->", "derived": "-.->"}


def mermaid_producer(graph: dict, producer: str) -> str:
    """One producer and everything a release there puts in motion."""
    lines = ["flowchart LR", f'  {_mid(producer)}["{producer}"]:::producer']
    # Several edges of one kind to one consumer (three copies of a tag, say)
    # draw as one arrow with a count rather than three identical arrows.
    grouped: dict[tuple[str, str, str], int] = {}
    for edge in out_edges(graph, producer):
        key = (edge["to"], edge["kind"], edge["type"])
        grouped[key] = grouped.get(key, 0) + 1
    seen: set[str] = set()
    for (to, kind, etype), count in sorted(grouped.items()):
        if to not in seen:
            lines.append(f'  {_mid(to)}["{to}"]:::{graph["nodes"][to]["audience"]}')
            seen.add(to)
        # Pipe labels take neither quotes nor parentheses, so the label is plain words.
        label = f"{kind}, {etype}" + (f" x{count}" if count > 1 else "")
        lines.append(f"  {_mid(producer)} {_ARROW[etype]}|{label}| {_mid(to)}")
    lines.extend(f"  {c}" for c in _CLASSES)
    return "\n".join(lines)


def render_block(graph: dict, name: str) -> str:
    if name == "overview":
        return mermaid_overview(graph)
    if name.startswith("producer:"):
        return mermaid_producer(graph, name.split(":", 1)[1])
    raise KeyError(f"unknown block {name!r}; use overview or producer:<node>")


def render_docs(graph: dict, text: str) -> str:
    """Replace every marked block in a docs page with its freshly generated Mermaid.

    An empty pair of markers is a block too: that is how a page asks for a
    diagram it has never held.
    """
    pattern = re.compile(
        r"<!-- suite-graph:begin (?P<name>[A-Za-z0-9_:.-]+) -->\n(?:.*?\n)?<!-- suite-graph:end (?P=name) -->",
        re.DOTALL,
    )

    def replace(m: re.Match) -> str:
        name = m.group("name")
        body = render_block(graph, name)
        return f"{BEGIN.format(name=name)}\n```mermaid\n{body}\n```\n{END.format(name=name)}"

    return pattern.sub(replace, text)


def doc_block_names(text: str) -> list[str]:
    return re.findall(r"<!-- suite-graph:begin ([A-Za-z0-9_:.-]+) -->", text)
