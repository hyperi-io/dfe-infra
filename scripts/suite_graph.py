#  Project:      dfe-infra
#  File:         suite_graph.py
#  Purpose:      Read suite.yaml (membership + build-cycle edges) with the
#                standard library and answer the questions the tooling asks:
#                out-edges of a producer, in-edges of a consumer, the
#                build-cycle table, a lane / producer / consumer slice, and
#                the Mermaid blocks docs/suite-graph.md carries.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""suite.yaml reader and queries, shared by dfe-stack and check_suite_drift.py.

suite.yaml is written in a block-style subset -- nested maps, block sequences
of scalars or maps, one-line flow sequences of plain words, double-quoted
one-line strings -- so this reader stays small and runs on a bare CI image.
PyYAML reads the same file; the subset is chosen so the two agree on every
value once the numerals it uses are quoted. Anything outside the subset that
the two would read DIFFERENTLY -- a single-quoted scalar, an escaped quote
inside a quoted string, a quoted item in a flow list, a tab-indented line --
is rejected here rather than coerced into a value PyYAML would disagree with.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITE_FILE = REPO_ROOT / "suite.yaml"

EDGE_TYPES = ("potential", "lockstep", "derived")
AUDIENCES = ("general", "suite")
NODE_REQUIRED = ("repo", "role", "language", "audience", "maturity", "support",
                 "licence", "classification", "classification_source",
                 "default_in_pass", "artefacts")
# Fields that are a yes or a no, wherever they appear. Only lowercase
# `true`/`false` reads as a boolean, so `True` or `yes` lands here as a string
# and is named rather than silently taken for truth.
NODE_FLAGS = ("default_in_pass", "optional")
ARTEFACT_FLAGS = ("public", "published")

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
        if '\\"' in text:
            raise ValueError(
                f"suite.yaml: escaped quote in {text!r}; this reader does not "
                f"unescape, and PyYAML would -- rewrite the value without one"
            )
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        items = [p.strip() for p in inner.split(",")]
        for item in items:
            if item.startswith(('"', "'")):
                raise ValueError(
                    f"suite.yaml: quoted item {item!r} in the flow list {text!r}; "
                    f"flow lists take plain words only -- use a block sequence"
                )
        return [_scalar(p) for p in items]
    if text.startswith("'"):
        raise ValueError(
            f"suite.yaml: single-quoted scalar {text!r}; this reader keeps the "
            f"quotes and PyYAML would strip them -- use double quotes"
        )
    if text in ("true", "false"):
        return text == "true"
    return text


def parse_block_yaml(text: str):
    """Parse the block-style subset suite.yaml is written in.

    Raises:
        ValueError: On a line outside the subset, or a shape this reader and
            PyYAML would disagree about.
    """
    lines: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        leading = raw[: len(raw) - len(raw.lstrip())]
        if "\t" in leading:
            raise ValueError(
                f"suite.yaml line {number}: indented with a tab; YAML indents "
                f"with spaces and a tab is not an indent at all"
            )
        lines.append((len(leading), raw.strip()))

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
            if key in out:
                raise ValueError(
                    f"suite.yaml: duplicate key {key!r}; the second one would "
                    f"silently replace the first"
                )
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
    runtime_kinds = graph.get("runtime_kinds", {})
    in_a_lane = {m for lane in graph.get("lanes", []) for m in lane.get("members", [])}
    for name, node in nodes.items():
        for field in NODE_REQUIRED:
            if field not in node:
                problems.append(f"node {name}: missing `{field}`")
        for field in NODE_FLAGS:
            if field in node and not isinstance(node[field], bool):
                problems.append(
                    f"node {name}: `{field}` is {node[field]!r}, which is not true or false"
                )
        for artefact in node.get("artefacts", []) or []:
            if not isinstance(artefact, dict):
                continue
            for field in ARTEFACT_FLAGS:
                if field in artefact and not isinstance(artefact[field], bool):
                    problems.append(
                        f"node {name}: artefact `{field}` is {artefact[field]!r}, "
                        f"which is not true or false"
                    )
        if "audience" in node and node["audience"] not in AUDIENCES:
            problems.append(
                f"node {name}: audience {node['audience']!r} is not one of "
                f"{', '.join(AUDIENCES)}"
            )
        if node.get("maturity") in ("alpha", "beta") and node.get("default_in_pass") is True:
            problems.append(f"node {name}: {node['maturity']} members must not be default_in_pass")
        if name not in in_a_lane:
            problems.append(f"node {name}: in no lane, so no pass ever reaches it")
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
        where = f"runtime edge {edge.get('from')} -> {edge.get('to')}"
        for end in ("from", "to"):
            if edge.get(end) not in nodes:
                problems.append(f"{where}: `{end}` is not a node")
        if edge.get("kind") not in runtime_kinds:
            problems.append(
                f"{where}: kind {edge.get('kind')!r} is not in runtime_kinds -- "
                f"the two vocabularies are separate"
            )
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


def cycle_table(graph: dict) -> list[dict]:
    """The BUILD-CYCLE table -- not graph cycles.

    One row per (producer, kind, type), consumers merged: what a release at
    the producer puts in motion, and the gates that prove each check.
    """
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


def slice_graph(
    graph: dict,
    *,
    producer: str | None = None,
    consumer: str | None = None,
    lane: str | None = None,
) -> dict:
    """The subgraph one task needs.

    A producer's out-edges (what a release there puts in motion), a consumer's
    in-edges (what reaches it, which is what a person picking up one member
    wants), or one lane's members and the edges among them.

    Raises:
        KeyError: If the node or lane is not in the graph, naming what is.
    """
    nodes = graph.get("nodes", {})
    if producer:
        if producer not in nodes:
            raise KeyError(
                f"no node named {producer!r}; nodes are {', '.join(sorted(nodes))}"
            )
        edges = out_edges(graph, producer)
        keep = {producer} | {e["to"] for e in edges}
    elif consumer:
        if consumer not in nodes:
            raise KeyError(
                f"no node named {consumer!r}; nodes are {', '.join(sorted(nodes))}"
            )
        edges = in_edges(graph, consumer)
        keep = {consumer} | {e["from"] for e in edges}
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


# The HyperI brand tokens, verbatim from https://graphics.hyperi.io/tokens/tokens.css.
# Every colour the diagrams below draw is one of these or a 0.45 fade of one.
# `tertiary` is `--hyperi-tertiary`, the ink of the `<lockup>/tertiary/` icon
# delivery: the one colour that reads on a light background and a dark one, so
# it is what a diagram outlines with.
PALETTE = {
    "navy": "#000647",
    "tertiary": "#2EA4F6",
    "accent": "#2DED88",
    "white": "#FFFFFF",
    "black": "#000000",
}

FADE = 0.45


def faded(colour: str, ratio: float = FADE) -> str:
    """A brand colour mixed toward white, which is how an optional member is drawn."""
    channels = colour.lstrip("#")
    values = (int(channels[i:i + 2], 16) for i in (0, 2, 4))
    return "#" + "".join(f"{round(c + (255 - c) * ratio):02X}" for c in values)


# Fill, text colour and stroke width per class, against GitHub's light (#FFFFFF)
# and dark (#0D1117) canvases.
# Navy is an ink and the one-node emphasis plate rather than a fill for the many,
# because a navy box is 1.01:1 against the dark canvas.
# Text is navy on every chromatic or faded fill (6.9:1 or better) and white only
# on navy (18.8:1).
_CLASS_FILL = {
    "general": (PALETTE["accent"], PALETTE["navy"], 2),
    "suite": (PALETTE["tertiary"], PALETTE["navy"], 2),
    "producer": (PALETTE["navy"], PALETTE["white"], 3),
}

# Every class strokes in the tertiary, the one colour that reads on both canvases
# (2.71 light, 6.99 dark), at 2px because 2.71 is marginal for a hairline.
# The producer is the single focal node of its diagram, so its outline is 3px.
# An optional member takes the faded version of its audience's fill and keeps the
# " (optional)" suffix, so colour is never the only signal.
# The fade is soft against the light canvas by design, where the unfaded stroke
# carries the boundary.
_CLASSES = {
    name: f"classDef {name} fill:{fill},stroke:{PALETTE['tertiary']},"
          f"stroke-width:{width}px,color:{text}"
    for name, (fill, text, width) in _CLASS_FILL.items()
} | {
    f"{audience}-optional":
        f"classDef {audience}-optional fill:{faded(_CLASS_FILL[audience][0])},"
        f"stroke:{PALETTE['tertiary']},stroke-width:2px,color:{PALETTE['navy']}"
    for audience in AUDIENCES
}


def _classdefs(used: set[str]) -> list[str]:
    """The classDef lines for the classes a block paints, in one fixed order.

    A block emits only what it references: an unused classDef is a legend entry
    for a colour that is not on the picture.
    """
    return [f"  {line}" for name, line in _CLASSES.items() if name in used]


def _label(name: str, node: dict) -> str:
    return name + (" (optional)" if node.get("optional") else "")


def _node_class(node: dict) -> str:
    return node["audience"] + ("-optional" if node.get("optional") else "")


def mermaid_overview(graph: dict) -> str:
    """Nodes only, grouped by role, coloured by audience. Edges are the per-producer diagrams' job."""
    by_role: dict[str, list[str]] = {}
    for name, node in graph["nodes"].items():
        by_role.setdefault(node["role"], []).append(name)
    lines = ["flowchart TB"]
    used: set[str] = set()
    for role in sorted(by_role):
        lines.append(f'  subgraph {role}["{role}"]')
        lines.append("    direction LR")
        for name in sorted(by_role[role]):
            node = graph["nodes"][name]
            used.add(_node_class(node))
            lines.append(f'    {_mid(name)}["{_label(name, node)}"]:::{_node_class(node)}')
        lines.append("  end")
    lines.extend(_classdefs(used))
    return "\n".join(lines)


_ARROW = {"lockstep": "==>", "potential": "-->", "derived": "-.->"}


def mermaid_producer(graph: dict, producer: str) -> str:
    """One producer and everything a release there puts in motion."""
    label = _label(producer, graph["nodes"][producer])
    lines = ["flowchart LR", f'  {_mid(producer)}["{label}"]:::producer']
    used = {"producer"}
    # Several edges of one kind to one consumer (three copies of a tag, say)
    # draw as one arrow with a count rather than three identical arrows.
    grouped: dict[tuple[str, str, str], int] = {}
    for edge in out_edges(graph, producer):
        key = (edge["to"], edge["kind"], edge["type"])
        grouped[key] = grouped.get(key, 0) + 1
    seen: set[str] = set()
    for (to, kind, etype), count in sorted(grouped.items()):
        if to not in seen:
            node = graph["nodes"][to]
            used.add(_node_class(node))
            lines.append(f'  {_mid(to)}["{_label(to, node)}"]:::{_node_class(node)}')
            seen.add(to)
        # Pipe labels take neither quotes nor parentheses, so the label is plain words.
        edge_label = f"{kind}, {etype}" + (f" x{count}" if count > 1 else "")
        lines.append(f"  {_mid(producer)} {_ARROW[etype]}|{edge_label}| {_mid(to)}")
    lines.extend(_classdefs(used))
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

    Raises:
        ValueError: If a begin marker has no matching end marker. That block
            can never be rewritten, so render and check would both report
            green over content that is stale for good.
    """
    pattern = re.compile(
        r"<!-- suite-graph:begin (?P<name>[A-Za-z0-9_:.-]+) -->\n(?:.*?\n)?<!-- suite-graph:end (?P=name) -->",
        re.DOTALL,
    )
    replaced: list[str] = []

    def replace(m: re.Match) -> str:
        name = m.group("name")
        body = render_block(graph, name)
        replaced.append(name)
        return f"{BEGIN.format(name=name)}\n```mermaid\n{body}\n```\n{END.format(name=name)}"

    fresh = pattern.sub(replace, text)
    unmatched = []
    pending = list(replaced)
    for name in doc_block_names(text):
        if name in pending:
            pending.remove(name)
        else:
            unmatched.append(name)
    if unmatched:
        raise ValueError(
            f"suite-graph block {unmatched[0]!r} has a begin marker and no "
            f"matching end marker, so it can never be rewritten"
        )
    return fresh


def doc_block_names(text: str) -> list[str]:
    return re.findall(r"<!-- suite-graph:begin ([A-Za-z0-9_:.-]+) -->", text)
