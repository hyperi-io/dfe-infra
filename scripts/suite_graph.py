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

from manifest_deps import PACKAGE_KINDS

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITE_FILE = REPO_ROOT / "suite.yaml"

EDGE_TYPES = ("potential", "lockstep", "derived")
AUDIENCES = ("general", "suite")
NODE_REQUIRED = (
    "repo",
    "role",
    "language",
    "audience",
    "maturity",
    "support",
    "licence",
    "classification",
    "classification_source",
    "default_in_pass",
    "artefacts",
)
# Fields that are a yes or a no, wherever they appear. Only lowercase
# `true`/`false` reads as a boolean, so `True` or `yes` lands here as a string
# and is named rather than silently taken for truth.
NODE_FLAGS = ("default_in_pass", "optional")
ARTEFACT_FLAGS = ("public", "published")

# The `:<line>` or `:<start>-<end>` a citation may end in.
_CITED_LINE = re.compile(r":\d+(?:-\d+)?$")

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
                if (
                    i < len(lines)
                    and lines[i][0] == child_indent
                    and not lines[i][1].startswith("- ")
                ):
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
        if "unconsumed" in node:
            note = node["unconsumed"]
            if not isinstance(note, str) or not note.strip():
                problems.append(
                    f"node {name}: `unconsumed` is empty, so it says nothing"
                )
            elif not node.get("package"):
                problems.append(
                    f"node {name}: `unconsumed` needs a `package`, or no lock can contradict it"
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
        if (
            node.get("maturity") in ("alpha", "beta")
            and node.get("default_in_pass") is True
        ):
            problems.append(
                f"node {name}: {node['maturity']} members must not be default_in_pass"
            )
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
        elif edge.get("kind") in PACKAGE_KINDS and _CITED_LINE.search(
            str(edge["evidence"])
        ):
            problems.append(
                f"{where}: a {edge['kind']} edge cites the manifest alone, never a line -- "
                f"the declaration is found by the producer's `package`, and a line moves "
                f"with every dependency added above it"
            )
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
                problems.append(
                    f"lane {lane.get('name')}: member {member} is not a node"
                )
    problems += _waiver_problems(graph.get("pin_waivers", []), nodes)
    return problems


WAIVER_FIELDS = ("member", "file", "image", "reason")
# A version or a digest in a waiver: the stack moves on and the waiver would not.
_VERSION_LITERAL = re.compile(r"\d+\.\d+|sha256:")


def _waiver_problems(waivers: object, nodes: dict) -> list[str]:
    if not isinstance(waivers, list):
        return [f"pin_waivers: is {type(waivers).__name__}, not a list"]
    problems = []
    for n, waiver in enumerate(waivers, start=1):
        if not isinstance(waiver, dict):
            problems.append(
                f"pin_waivers #{n}: not a map of {', '.join(WAIVER_FIELDS)}"
            )
            continue
        where = f"pin_waivers #{n} ({waiver.get('member')} {waiver.get('file')})"
        problems += [
            f"{where}: missing `{f}`" for f in WAIVER_FIELDS if not waiver.get(f)
        ]
        if waiver.get("member") and waiver["member"] not in nodes:
            problems.append(f"{where}: member {waiver['member']} is not a node")
        problems += [
            f"{where}: `{f}` carries a version literal ({waiver[f]!r}); name the image, not a version"
            for f in WAIVER_FIELDS
            if isinstance(waiver.get(f), str) and _VERSION_LITERAL.search(waiver[f])
        ]
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
        out.append(
            {
                "producer": producer,
                "consumers": sorted(set(consumers)),
                "kind": kind,
                "type": etype,
                "check": spec.get("check", ""),
                "gates": spec.get("gates", []),
            }
        )
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
        lanes = {entry["name"]: entry for entry in graph.get("lanes", [])}
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

# The house frontmatter and class lines every Mermaid diagram in the DFE docs
# carries, byte for byte. A block copies all of them, used or not.
HOUSE_FRONTMATTER = """---
config:
  theme: base
  look: neo
  fontFamily: Inter, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif
  flowchart:
    curve: linear
    nodeSpacing: 40
    rankSpacing: 64
    padding: 18
    wrappingWidth: 280
    htmlLabels: true
  elk:
    preset: modelOrder
  themeVariables:
    fontSize: 15px
    lineColor: "#64748B"
    primaryTextColor: "#0F172A"
    clusterBorder: "#0072B2"
    edgeLabelBackground: "#FFFFFF"
  themeCSS: >-
    p{margin:0}
    .node[data-look] rect,.node[data-look] path,.node[data-look] polygon,.node[data-look] .outer-path{filter:drop-shadow(0 1px 2px rgba(15,23,42,.10)) drop-shadow(0 6px 14px rgba(15,23,42,.10))}
    .node rect{rx:10px;ry:10px}
    .cluster rect{rx:16px;ry:16px}
    .node .label div{min-width:150px}
    .node.decision .label div{min-width:0}
    .cluster[data-look] rect{filter:none}
    .nodeLabel{line-height:1.35}
    .nodeLabel b{font-weight:600;font-size:15px;letter-spacing:-0.01em}
    .nodeLabel span{font-size:12.5px;font-weight:400;color:#334155}
    .cluster-label span.nodeLabel{display:inline-block;background:#FFFFFF;color:#0B2A3F;border:1.5px solid #0072B2;border-radius:999px;padding:2px 12px;font-size:11.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}
    .flowchart-link{stroke-width:1.75px}
    .labelBkg{background:transparent!important}
    span.edgeLabel{display:inline-block;background:#FFFFFF!important;color:#1E293B;border:1px solid #94A3B8;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:500}
    span.edgeLabel p{background:transparent!important}
    .label-icon{width:20px;height:20px;vertical-align:-4px;margin-right:6px}
    .ingest .label-icon{color:#0E8A7D}
    .xform .label-icon{color:#7B4FB0}
    .load .label-icon{color:#0072B2}
    .bus .label-icon{color:#A8517F}
    .store .label-icon{color:#00785A}
    .ui .label-icon{color:#D9480F}
    .ext .label-icon{color:#64748B}
---
"""
CLASS_LINES = {
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
# The dashed boundary line a subgraph takes.
BOUNDARY_STYLE = (
    "style {id} fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4"
)

# A named DFE component keeps its own colour in every diagram. Any other node
# takes the colour of its role.
IDENTITY_CLASS = {
    "dfe-receiver": "ingest",
    "dfe-fetcher": "ingest",
    "dfe-transform-vrl": "xform",
    "dfe-transform-vector": "xform",
    "dfe-transform-elastic": "xform",
    "dfe-loader": "load",
    "dfe-archiver": "load",
    "dfe-ui": "ui",
    "dfe-hyperdx": "ui",
    "dfe-engine": "control",
    "dfe-infra": "control",
    "dfe-docker": "control",
    "dfe-deploy": "control",
    "culvert": "control",
}
ROLE_CLASS = {
    "service": "part",
    "library": "part",
    "infra": "control",
    "content": "store",
    "ui": "ui",
}

INDENT = "    "
LEVEL = "L4 Detail"
# The most a group's grid holds. Three columns keep a block under the docs
# column's 860 px at full text size.
GRID_COLUMNS = 3
GRID_ROWS = 3

_LANGUAGE = {"rust": "Rust", "python": "Python", "typescript": "TypeScript"}
_ROLE_TITLE = {
    "service": "Services",
    "library": "Libraries",
    "infra": "Infra",
    "content": "Content",
    "ui": "UI",
}

OVERVIEW_BLOCKS = ("overview:services", "overview:platform")


def _mid(name: str) -> str:
    return name.replace("-", "_").replace(".", "_")


def _label(name: str, node: dict) -> str:
    return name + (" (optional)" if node.get("optional") else "")


def _node(name: str, node: dict) -> str:
    """`id("<b>name</b>")`, with an `optional` subtitle where the node is."""
    sub = "<br/><span>optional</span>" if node.get("optional") else ""
    return f'{_mid(name)}("<b>{name}</b>{sub}")'


def _class_of(name: str, node: dict) -> str:
    return IDENTITY_CLASS.get(name) or ROLE_CLASS.get(node["role"], "part")


def _class_lines(members: list[str], graph: dict) -> dict[str, list[str]]:
    """Node ids by class, for every named member."""
    out: dict[str, list[str]] = {}
    for name in members:
        out.setdefault(_class_of(name, graph["nodes"][name]), []).append(_mid(name))
    return out


def _names(names: list[str]) -> str:
    """`a`, `a and b`, `a, b and c`."""
    if len(names) < 2:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")


def _grid(ids: list[str]) -> list[str]:
    """Invisible links that lay a group's members out in rows of a grid.

    A subgraph that an arrow ends on loses its own direction and stacks its
    members, so the rows are set here, with `~~~`. The grid has the fewest
    empty cells within GRID_COLUMNS wide and GRID_ROWS high, then the fewest
    columns. Rows are as even as they can be, and never a lone member beside
    other rows, which Mermaid puts at the top of the group.
    """
    n = len(ids)
    best: tuple[int, int, list[int]] | None = None
    for columns in range(1, GRID_COLUMNS + 1):
        rows = -(-n // columns)
        base, extra = divmod(n, rows)
        sizes = [base + (row < extra) for row in range(rows)]
        if rows > GRID_ROWS or (columns > 1 and 1 in sizes):
            continue
        candidate = (columns * rows - n, columns, sizes)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    sizes = best[2] if best else [n]
    chains, at = [], 0
    for size in sizes:
        chains.append(ids[at : at + size])
        at += size
    return [" ~~~ ".join(chain) for chain in chains if len(chain) > 1]


def _block(
    title: str,
    descr: str,
    body: list[str],
    *,
    classes: dict[str, list[str]],
    groups: list[str],
) -> str:
    """One flowchart: frontmatter, header, nodes and arrows in `body`, then the house class lines."""
    lines = [*HOUSE_FRONTMATTER.rstrip("\n").split("\n")]
    # Groups stack top to bottom with their members in rows, which keeps the block
    # inside 860 px. A block with no group is a short chain and reads left to right.
    lines += [
        f"flowchart {'TB' if groups else 'LR'}",
        f"{INDENT}accTitle: {LEVEL} -- {title}",
    ]
    lines.append(f"{INDENT}accDescr: {descr}")
    lines.append("")
    lines += [f"{INDENT}{line}" if line else "" for line in body]
    lines.append("")
    lines += [
        f"{INDENT}class {','.join(ids)} {name}" for name, ids in sorted(classes.items())
    ]
    lines += [f"{INDENT[:2]}{line}" for line in CLASS_LINES.values()]
    lines += [f"{INDENT[:2]}{BOUNDARY_STYLE.format(id=gid)}" for gid in groups]
    return "\n".join(lines)


def _subgraph(gid: str, title: str, members: list[str], graph: dict) -> list[str]:
    lines = [f'subgraph {gid}["{title}"]', f"{INDENT}direction LR"]
    lines += [f"{INDENT}{_node(name, graph['nodes'][name])}" for name in members]
    lines += [f"{INDENT}{chain}" for chain in _grid([_mid(n) for n in members])]
    lines.append("end")
    return lines


def _group_title(members: list[str], graph: dict) -> str:
    """What the members share: their language and role where both agree."""
    nodes = [graph["nodes"][name] for name in members]
    languages = {node["language"] for node in nodes}
    roles = {node["role"] for node in nodes}
    language = _LANGUAGE.get(next(iter(languages)), "") if len(languages) == 1 else ""
    role = "members"
    if len(roles) == 1:
        only = next(iter(roles))
        role = _ROLE_TITLE.get(only, only).lower()
    title = f"{language} {role}".strip()
    return title[:1].upper() + title[1:]


def mermaid_overview(graph: dict, block: str) -> str:
    """Nodes only, in boxes by what they share. Edges are the per-producer diagrams' job.

    `services` is the service nodes boxed by language. `platform` is every other
    node boxed by role.
    """
    nodes = graph["nodes"]
    if block == "services":
        key, titles = "language", _LANGUAGE
        picked = [n for n in nodes if nodes[n]["role"] == "service"]
        scope = "Suite overview, services"
    else:
        key, titles = "role", _ROLE_TITLE
        picked = [n for n in nodes if nodes[n]["role"] != "service"]
        scope = "Suite overview, platform"
    picked_sorted = sorted(picked)
    boxes: dict[str, list[str]] = {}
    for name in picked_sorted:
        boxes.setdefault(nodes[name][key], []).append(name)
    order = sorted(boxes, key=lambda k: (-len(boxes[k]), k))
    body: list[str] = []
    ids: list[str] = []
    descr = []
    for k in order:
        title = titles.get(k, k.capitalize())
        if key == "language":
            title += " services"
        ids.append(_slug(title))
        body += _subgraph(ids[-1], title, boxes[k], graph)
        listed = [_label(n, nodes[n]) for n in boxes[k]]
        descr.append(f"{_names(listed)} sit in {title}.")
    body.append(" ~~~ ".join(ids))
    what = "services" if key == "language" else "members that are not services"
    return _block(
        scope,
        f"The suite has {len(picked)} {what}, in {len(order)} boxes. {' '.join(descr)}",
        body,
        classes=_class_lines(picked_sorted, graph),
        groups=ids,
    )


def _edge_label(edges: list[dict]) -> tuple[str, str]:
    """The arrow for the edges between one producer and one consumer.

    The label is the kinds alone, with a count for a repeat. Pipe labels take
    neither quotes nor parentheses, so it is plain words. The arrow is dotted
    only when every edge is derived: a consumer holding no copy at all.
    """
    counts: dict[str, int] = {}
    for edge in edges:
        counts[edge["kind"]] = counts.get(edge["kind"], 0) + 1
    label = ", ".join(
        kind + (f" x{n}" if n > 1 else "") for kind, n in sorted(counts.items())
    )
    dotted = all(edge["type"] == "derived" for edge in edges)
    return label, "-.->" if dotted else "-->"


def mermaid_producer(graph: dict, producer: str) -> str:
    """One producer and everything a release there puts in motion.

    Consumers that get the same arrow merge into one box, with one arrow to it.
    """
    nodes = graph["nodes"]
    arrows: dict[str, tuple[str, str]] = {}
    for consumer in sorted({e["to"] for e in out_edges(graph, producer)}):
        edges = [e for e in out_edges(graph, producer) if e["to"] == consumer]
        arrows[consumer] = _edge_label(edges)
    shared: dict[tuple[str, str], list[str]] = {}
    for consumer, arrow in arrows.items():
        shared.setdefault(arrow, []).append(consumer)
    items = sorted(shared.items(), key=lambda item: item[1][0])
    body = [_node(producer, nodes[producer])]
    named = [producer]
    links: list[str] = []
    groups: list[str] = []
    taken = {_mid(n) for n in nodes}
    descr = []
    for (label, arrow), members in items:
        if len(members) == 1:
            target = _mid(members[0])
            body.append(_node(members[0], nodes[members[0]]))
        else:
            title = _group_title(members, graph)
            target = _slug(title)
            while target in taken:
                target += "_2"
            taken.add(target)
            groups.append(target)
            body += _subgraph(target, title, members, graph)
        named += members
        links.append(f"{_mid(producer)} {arrow}|{label}| {target}")
        listed = [_label(m, nodes[m]) for m in members]
        take = "takes" if len(members) == 1 else "take"
        descr.append(f"{_names(listed)} {take} {label.replace(', ', ' and ')}.")
    consumers = len(arrows)
    return _block(
        f"{producer} build cycle",
        f"A {producer} release puts {consumers} consumer{'s' * (consumers != 1)} "
        f"in motion. {' '.join(descr)}",
        [*body, "", *links],
        classes=_class_lines(named, graph),
        groups=groups,
    )


def block_names(graph: dict) -> list[str]:
    """Every block the page can carry: the overview halves, then one per producer."""
    producers = sorted({e["from"] for e in graph.get("edges", [])})
    return [*OVERVIEW_BLOCKS, *(f"producer:{p}" for p in producers)]


def render_block(graph: dict, name: str) -> str:
    if name in OVERVIEW_BLOCKS:
        return mermaid_overview(graph, name.split(":", 1)[1])
    if name.startswith("producer:") and name.split(":", 1)[1] in graph["nodes"]:
        return mermaid_producer(graph, name.split(":", 1)[1])
    raise KeyError(
        f"unknown block {name!r}; use {' or '.join(OVERVIEW_BLOCKS)} or producer:<node>"
    )


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
