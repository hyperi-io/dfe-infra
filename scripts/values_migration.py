#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/values_migration.py
#  Purpose:      Carry a deploy repo's 2.2.0 value keys onto the keys the thin
#                charts read, by the rules in dfe-extras' values-migrations.yaml.
#                Stdlib only: every edit is made on the lines as written.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""values_migration -- the 2.2.0 values rewrite `dfe-ops upgrade` runs.

    import values_migration as vm

    rules = vm.load_map()
    hits = vm.scan(deploy, rules)       # what each rule would do, with nothing written
    hits = vm.expand(deploy, rules)     # each new key written beside its old one
    hits = vm.contract(deploy, rules)   # each `move` rule's old key dropped

The rules are helm/charts/dfe-extras/files/values-migrations.yaml, the file the
chart's render guard reads too. They apply to every values/*-values.yaml in the
deploy repo, by its deploy.service, and the rules naming `common` to
infra/common.yaml as well.

A values file carries lists, block scalars and flow collections, which
yaml_subset does not parse, so a key is found by indentation: its block is its
own line and every deeper line after it. A value is copied by shifting that
block's indent, so its text, quoting and comments carry over as written, and
nothing else in the file moves.

expand never overwrites a key that is already there, so a second run changes
nothing. With `resync`, used while the 2.2.0 charts still render, a new key that
differs from its old one takes the old one's value again: until the retarget the
2.2.0 engine writes the old keys only.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml_subset

REPO_ROOT = Path(__file__).resolve().parent.parent
MAP_FILE = REPO_ROOT / "helm" / "charts" / "dfe-extras" / "files" / "values-migrations.yaml"

# The deploy repo's instance overlays, and the overlay every appset layers under them.
OVERLAY_DIR = Path("values")
OVERLAY_GLOB = "*-values.yaml"
COMMON_OVERLAY = Path("infra") / "common.yaml"

KINDS = ("move", "copy", "derive-identity", "derive-entries", "refuse", "report-dropped")
GUARDS = ("", "empty", "equal")

# What a rule found. MIGRATE adds a key, RESYNC rewrites one from its old key,
# CONTRACT drops an old key, DONE has nothing left to do.
MIGRATE = "MIGRATE"
RESYNC = "RESYNC"
CONTRACT = "CONTRACT"
DONE = "DONE"
REFUSED = "REFUSED"
DROPPED = "DROPPED"

# A block-style mapping key, quoted or not, and what follows its colon.
_KEY = re.compile(
    r"""^(?P<indent> *)(?P<key>[A-Za-z_][\w.\-/]*|"[^"\n]*"|'[^'\n]*')[ \t]*:(?:[ \t]+(?P<rest>.*?))?[ \t]*$"""
)
# A key on a sequence item's dash line: `- name: x`.
_ITEM_KEY = re.compile(r"""^(?P<key>[A-Za-z_][\w.\-]*)[ \t]*:(?:[ \t]+(?P<rest>.*?))?[ \t]*$""")
# A value written on a key's line that holds nothing.
_NOTHING = ("", "~", "null", "Null", "NULL", "{}", "[]")
# A scalar safe to write unquoted: no YAML indicator, never read as another type.
_PLAIN = re.compile(r"^[A-Za-z/_][A-Za-z0-9/._-]*$")
_RESERVED = {"true", "false", "yes", "no", "on", "off", "null", "y", "n"}


class MigrationError(RuntimeError):
    """An edit the line surgery cannot make. The message says what to do by hand."""


class MigrationMapError(ValueError):
    """values-migrations.yaml carries a rule this module cannot apply."""


@dataclass(frozen=True, slots=True)
class Rule:
    """One rule of values-migrations.yaml. `sources` is its `from`, split on commas."""

    id: str
    kind: str
    apps: tuple[str, ...]
    sources: tuple[str, ...]
    to: str = ""
    files: tuple[str, ...] = ("overlays",)
    guard: str = ""
    shape: str = ""
    prefix: str = ""
    directory: str = ""
    dir_key: str = ""
    legacy_dir: str = ""
    why: str = ""

    def applies(self, service: str | None, *, common: bool) -> bool:
        """Whether the rule reads a file: infra/common.yaml, or an overlay of `service`."""
        if common:
            return "common" in self.files
        if "overlays" not in self.files or not service:
            return False
        return "*" in self.apps or service in self.apps


@dataclass(frozen=True, slots=True)
class MigrationMap:
    """The rules, in their file order, and the stack whose charts read the new keys."""

    since: str
    rules: tuple[Rule, ...]


@dataclass(frozen=True, slots=True)
class Hit:
    """What one rule found in one file. `file` is relative to the deploy repo."""

    file: str
    rule: Rule
    state: str
    detail: str

    def line(self) -> str:
        """The hit as a report line."""
        return f"[{self.state}] {self.file}: {self.detail}"


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def load_map(path: Path | None = None) -> MigrationMap:
    """Read values-migrations.yaml.

    Args:
        path: The map, else MAP_FILE as it stands at call time.

    Returns:
        The rules in id order.

    Raises:
        MigrationMapError: A rule of an unknown kind, or one missing a key its kind needs.
    """
    path = path if path is not None else MAP_FILE
    tree = yaml_subset.parse(path.read_text(encoding="utf-8"), source=str(path))
    since = str(tree.get("since") or "")
    body = tree.get("rules")
    if not since or not isinstance(body, dict):
        raise MigrationMapError(f"{path}: needs `since` and a `rules:` map")
    rules: list[Rule] = []
    for key in sorted(body, key=_unquote):
        entry = body[key]
        if not isinstance(entry, dict):
            raise MigrationMapError(f"{path}: rule {_unquote(key)} is not a map")
        rule = Rule(
            id=_unquote(key),
            kind=str(entry.get("kind", "")),
            apps=yaml_subset.split_list(entry.get("apps", "")),
            sources=yaml_subset.split_list(entry.get("from", "")),
            to=str(entry.get("to", "")),
            files=yaml_subset.split_list(entry.get("files", "overlays")),
            guard=str(entry.get("guard", "")),
            shape=str(entry.get("shape", "")),
            prefix=str(entry.get("prefix", "")),
            directory=str(entry.get("dir", "")),
            dir_key=str(entry.get("dirKey", "")),
            legacy_dir=str(entry.get("legacyDir", "")),
            why=str(entry.get("why", "")),
        )
        _check_rule(path, rule)
        rules.append(rule)
    return MigrationMap(since=since, rules=tuple(rules))


def _check_rule(path: Path, rule: Rule) -> None:
    where = f"{path}: rule {rule.id}"
    if rule.kind not in KINDS:
        raise MigrationMapError(f"{where}: kind {rule.kind!r} is not one of {', '.join(KINDS)}")
    if rule.guard not in GUARDS:
        raise MigrationMapError(f"{where}: guard {rule.guard!r} is not empty or equal")
    if not rule.apps or not rule.sources:
        raise MigrationMapError(f"{where}: needs `apps` and `from`")
    if rule.kind in ("move", "copy", "derive-identity", "derive-entries") and not rule.to:
        raise MigrationMapError(f"{where}: a {rule.kind} rule needs `to`")
    if rule.kind == "derive-entries" and not (rule.directory and rule.dir_key and rule.legacy_dir):
        raise MigrationMapError(f"{where}: a derive-entries rule needs `dir`, `dirKey` and `legacyDir`")


# --- the line model -----------------------------------------------------------


def _strip_comment(rest: str) -> str:
    """A key line's value less any trailing comment, quotes kept."""
    text = rest.strip()
    if not text or text.startswith("#"):
        return ""
    if text[0] == '"':
        i = 1
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == '"':
                return text[: i + 1]
            i += 1
        return text
    if text[0] == "'":
        i = 1
        while i < len(text):
            if text[i] == "'" and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            if text[i] == "'":
                return text[: i + 1]
            i += 1
        return text
    return text.split(" #", 1)[0].rstrip()


def scalar(rest: str) -> str:
    """The string a key line's value reads as: unquoted, less any trailing comment."""
    text = _strip_comment(rest)
    if text.startswith('"'):
        try:
            return str(json.loads(text))
        except json.JSONDecodeError:
            return text.strip('"')
    if text.startswith("'"):
        return text[1:-1].replace("''", "'")
    return text


def yaml_scalar(value: str) -> str:
    """`value` as a YAML scalar that reads back as the same string."""
    if _PLAIN.match(value) and value.lower() not in _RESERVED:
        return value
    return json.dumps(value)


def _path(dotted: str) -> tuple[str, ...]:
    return tuple(dotted.split("."))


def _shift(lines: list[str], by: int) -> list[str]:
    """Each line moved `by` columns, a blank line left blank."""
    out: list[str] = []
    for line in lines:
        if not line.strip():
            out.append("")
        elif by >= 0:
            out.append(" " * by + line)
        elif line[:-by].strip():
            raise MigrationError(f"cannot move {line.strip()!r} left by {-by} columns")
        else:
            out.append(line[-by:])
    return out


@dataclass(frozen=True, slots=True)
class _Node:
    """A key found in the file: its line, the end of its block, its indent and its inline value."""

    start: int
    end: int
    indent: int
    key: str
    rest: str


@dataclass(slots=True)
class _Item:
    """One item of a block sequence: its line range, and the line and value of each key of it."""

    start: int
    end: int
    fields: dict[str, tuple[int, str]] = field(default_factory=dict)


class _Doc:
    """A values file's lines, edited in place."""

    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()

    @property
    def text(self) -> str:
        return "\n".join(self.lines) + "\n" if self.lines else ""

    def _indent(self, i: int) -> int:
        line = self.lines[i]
        return len(line) - len(line.lstrip(" "))

    def _content(self, i: int) -> bool:
        stripped = self.lines[i].strip()
        return bool(stripped) and not stripped.startswith("#")

    def _end(self, i: int, indent: int, rest: str) -> int:
        """One past the last line of the block whose key sits on line `i`.

        An indentless sequence (`key:` then `- item` at the key's own indent)
        belongs to the key. A comment at or left of the key does not end the block.
        """
        open_block = _strip_comment(rest) == ""
        last = i
        for j in range(i + 1, len(self.lines)):
            stripped = self.lines[j].strip()
            if not stripped:
                continue
            column = self._indent(j)
            if column > indent or (
                column == indent and open_block and (stripped == "-" or stripped.startswith("- "))
            ):
                last = j
                continue
            if stripped.startswith("#"):
                continue
            break
        return last + 1

    def _span(self, node: _Node | None) -> tuple[int, int]:
        return (-1, len(self.lines)) if node is None else (node.start, node.end)

    def child_indent(self, node: _Node | None) -> int | None:
        """The indent of a mapping's keys, or None when it holds no block mapping."""
        start, end = self._span(node)
        for j in range(start + 1, end):
            if self._content(j):
                stripped = self.lines[j].strip()
                if stripped == "-" or stripped.startswith("- "):
                    return None
                return self._indent(j)
        return None

    def find(self, path: tuple[str, ...]) -> _Node | None:
        """The key at `path`, through block mappings only, or None when any step is missing."""
        node: _Node | None = None
        for step in path:
            indent = self.child_indent(node)
            if indent is None:
                return None
            start, end = self._span(node)
            found = None
            for j in range(start + 1, end):
                match = _KEY.match(self.lines[j])
                if match and len(match["indent"]) == indent and _unquote(match["key"]) == step:
                    rest = match["rest"] or ""
                    found = _Node(j, self._end(j, indent, rest), indent, match["key"], rest)
                    break
            if found is None:
                return None
            node = found
        return node

    def block(self, node: _Node) -> list[str]:
        return self.lines[node.start + 1 : node.end]

    def holds_value(self, node: _Node) -> bool:
        """Whether the key holds anything: an inline value other than null or empty, or content below it."""
        if _strip_comment(node.rest) not in _NOTHING:
            return True
        return any(self._content(j) for j in range(node.start + 1, node.end))

    def value_text(self, node: _Node) -> tuple[str, list[str]]:
        """The key's value as written, comment-free inline and indent-free block, for comparing two keys."""
        block = [line for line in _shift(self.block(node), -node.indent) if line.strip()]
        return _strip_comment(node.rest), block

    def items(self, node: _Node) -> list[_Item] | None:
        """Each item of the block sequence under `node`, or None when its value is not one."""
        items: list[_Item] = []
        item_indent: int | None = None
        field_indent: int | None = None
        current: _Item | None = None
        for j in range(node.start + 1, node.end):
            if not self._content(j):
                continue
            stripped = self.lines[j].strip()
            column = self._indent(j)
            if item_indent is None:
                if not (stripped == "-" or stripped.startswith("- ")):
                    return None
                item_indent = column
            if column == item_indent and (stripped == "-" or stripped.startswith("- ")):
                if current is not None:
                    current.end = j
                current = _Item(start=j, end=node.end)
                items.append(current)
                body = stripped[1:].lstrip()
                field_indent = column + len(stripped) - len(body) if body else None
                match = _ITEM_KEY.match(body)
                if match:
                    current.fields[match["key"]] = (j, match["rest"] or "")
                continue
            if current is None:
                continue
            if field_indent is None:
                field_indent = column
            if column == field_indent:
                match = _KEY.match(self.lines[j])
                if match:
                    current.fields[_unquote(match["key"])] = (j, match["rest"] or "")
        if current is not None:
            current.end = self._block_end_of_item(current.start, node.end)
        return items

    def _block_end_of_item(self, start: int, limit: int) -> int:
        last = start
        for j in range(start + 1, limit):
            if self._content(j):
                last = j
        return last + 1

    def insert(
        self,
        path: tuple[str, ...],
        rest: str,
        block: list[str] | None = None,
        *,
        source_indent: int = 0,
        beside: _Node | None = None,
    ) -> None:
        """Add the key at `path`, creating its missing parents.

        `block` is the value's lines as they sit under a key at `source_indent`.
        A new top-level key goes after the top-level block holding `beside`, else
        at the end of the file. Under an existing parent it goes after that
        parent's last line.

        Raises:
            MigrationError: The key is there already, or a parent is written inline
                or holds a sequence, which only a hand edit can extend.
        """
        depth = 0
        parent: _Node | None = None
        for k in range(len(path), 0, -1):
            found = self.find(path[:k])
            if found is not None:
                parent, depth = found, k
                break
        if depth == len(path):
            raise MigrationError(f"{'.'.join(path)} is already set")
        if parent is not None:
            parent = self._open(parent, path[:depth])
            indent = self.child_indent(parent)
            if indent is None:
                indent = parent.indent + 2
            at = parent.end
        else:
            indent = self.child_indent(None) or 0
            at = len(self.lines)
            if beside is not None:
                top = next((n for n in self._top_level() if n.start <= beside.start < n.end), None)
                at = top.end if top is not None else at
        new: list[str] = []
        for step in path[depth:-1]:
            new.append(f"{' ' * indent}{step}:")
            indent += 2
        new.append(f"{' ' * indent}{path[-1]}:" + (f" {rest}" if rest else ""))
        new += _shift(block or [], indent - source_indent)
        self.lines[at:at] = new

    def _open(self, node: _Node, path: tuple[str, ...]) -> _Node:
        """`node` ready to take a child key: an inline null or {} is dropped from its line."""
        inline = _strip_comment(node.rest)
        if inline in _NOTHING and inline != "[]":
            if inline:
                self.lines[node.start] = f"{' ' * node.indent}{node.key}:"
                refound = self.find(path)
                if refound is None:
                    raise MigrationError(f"{'.'.join(path)} could not be read back")
                node = refound
            if any(self._content(j) for j in range(node.start + 1, node.end)) and self.child_indent(node) is None:
                raise MigrationError(f"{'.'.join(path)} holds a list, so no key can go under it")
            return node
        raise MigrationError(
            f"{'.'.join(path)} is written on one line ({inline}), so it takes no new key -- "
            "write it as a block, then run this again"
        )

    def _top_level(self) -> list[_Node]:
        nodes: list[_Node] = []
        indent = self.child_indent(None)
        if indent is None:
            return nodes
        for j, line in enumerate(self.lines):
            match = _KEY.match(line)
            if match and len(match["indent"]) == indent:
                rest = match["rest"] or ""
                nodes.append(_Node(j, self._end(j, indent, rest), indent, match["key"], rest))
        return nodes

    def replace_value(self, node: _Node, rest: str, block: list[str], *, source_indent: int) -> None:
        """Give the key on `node` a new value, written as `insert` writes one."""
        head = f"{' ' * node.indent}{node.key}:" + (f" {rest}" if rest else "")
        self.lines[node.start : node.end] = [head, *_shift(block, node.indent - source_indent)]

    def delete(self, path: tuple[str, ...]) -> bool:
        """Remove the key at `path` and its block, then each parent it leaves empty."""
        node = self.find(path)
        if node is None:
            return False
        del self.lines[node.start : node.end]
        for k in range(len(path) - 1, 0, -1):
            parent = self.find(path[:k])
            if parent is None or _strip_comment(parent.rest):
                break
            if any(self._content(j) for j in range(parent.start + 1, parent.end)):
                break
            del self.lines[parent.start : parent.end]
        return True


# --- the rules ----------------------------------------------------------------


def _shaped(doc: _Doc, node: _Node, shape: str) -> tuple[str, list[str], int]:
    """(inline value, block, the indent it sits under) the new key takes from the old one."""
    if shape == "list":
        inline = _strip_comment(node.rest)
        if not inline or doc.block(node):
            raise MigrationError(f"{node.key} is not a one-line value, so it cannot become a one-item list")
        return f"[{inline}]", [], node.indent
    return node.rest.strip(), doc.block(node), node.indent


def _move(doc: _Doc, rule: Rule, *, resync: bool) -> tuple[str, str] | None:
    source = rule.sources[0]
    old = doc.find(_path(source))
    if old is None or not doc.holds_value(old):
        return None
    label = f"{source} -> {rule.to}"
    rest, block, indent = _shaped(doc, old, rule.shape)
    new = doc.find(_path(rule.to))
    if new is None:
        doc.insert(_path(rule.to), rest, block, source_indent=indent, beside=old)
        return MIGRATE, label
    target = (_strip_comment(rest), [line for line in _shift(block, -indent) if line.strip()])
    if resync and doc.value_text(new) != target:
        doc.replace_value(new, rest, block, source_indent=indent)
        return RESYNC, f"{label} (the new key differed, and the 2.2.0 charts still read the old one)"
    return DONE, label


def _identity(doc: _Doc, rule: Rule) -> tuple[str, str] | None:
    source = rule.sources[0]
    old = doc.find(_path(source))
    if old is None or not doc.holds_value(old):
        return None
    if doc.block(old):
        raise MigrationError(f"{source} is not a one-line value, so {rule.to} cannot be derived from it")
    value = rule.prefix + scalar(old.rest)
    new = doc.find(_path(rule.to))
    if new is not None:
        return DONE, f"{rule.to} is {scalar(new.rest)}"
    doc.insert(_path(rule.to), yaml_scalar(value), beside=old)
    return MIGRATE, f"{rule.to}: {value} (from {source})"


def _table_name(filename: str) -> str:
    """The name a VRL program looks a mounted table up by: the file name less its extension."""
    return filename.rsplit(".", 1)[0]


def _entries(doc: _Doc, rule: Rule, service: str, *, resync: bool) -> tuple[str, str] | None:
    """An enrichment_tables entry for each table file, at the directory both charts mount.

    The 2.2.0 chart mounted the tables at legacyDir and added an entry for each
    file the config did not name. The thin chart adds none, so each becomes an
    explicit entry at `dir`, and dirKey moves the 2.2.0 mount there too, so both
    charts render the same entries. An author's entry under legacyDir moves with
    it, and one under some other /etc/dfe-*-enrichment directory is refused,
    because no mount can be shown to serve it.
    """
    tables = doc.find(_path(rule.sources[0]))
    if tables is None:
        return None
    listed = doc.items(tables) or []
    files = [scalar(item.fields["name"][1]) for item in listed if "name" in item.fields]
    if not files:
        return None
    dir_node = doc.find((rule.dir_key,))
    directory = scalar(dir_node.rest) if dir_node is not None and _strip_comment(dir_node.rest) else rule.directory
    component_node = doc.find(("component",))
    component = scalar(component_node.rest) if component_node is not None else service.removeprefix("dfe-")
    legacy = rule.legacy_dir.replace("<component>", component)

    entries_path = _path(rule.to)
    entries = doc.find(entries_path)
    declared: list[_Item] = []
    if entries is not None:
        inline = _strip_comment(entries.rest)
        if inline not in ("", "[]", "~", "null"):
            raise MigrationError(f"{rule.to} is written on one line, so no entry can be added -- write it as a block list")
        declared = doc.items(entries) or []

    moved: list[tuple[int, str]] = []
    unknown: list[str] = []
    for item in declared:
        if "path" not in item.fields:
            continue
        line, rest = item.fields["path"]
        written = scalar(rest)
        if written.startswith(f"{directory}/"):
            continue
        if dir_node is None and written.startswith(f"{legacy}/"):
            moved.append((line, directory + written[len(legacy) :]))
        elif re.match(r"^/etc/dfe-[^/]+-enrichment/", written):
            unknown.append(written)
    if unknown:
        raise MigrationError(
            f"{rule.to} names {', '.join(unknown)}, which neither {legacy} nor {directory} mounts -- "
            f"point each entry at {directory}/<file> by hand"
        )

    names = {scalar(item.fields["name"][1]) for item in declared if "name" in item.fields}
    added: list[tuple[str, str]] = []
    for name in files:
        table = _table_name(name)
        if table not in names and table not in {a for a, _ in added}:
            added.append((table, f"{directory}/{name}"))
    dead = [
        item for item in declared
        if resync and _derived_and_gone(item, directory, files)
    ]
    if not (moved or added or dead or dir_node is None):
        return DONE, f"{rule.to} names every table under {directory}"

    for line, value in moved:
        head = re.match(r"^(\s*(?:-\s+)?path[ \t]*:[ \t]*)", doc.lines[line])
        if head is None:
            raise MigrationError(f"cannot rewrite the path on {doc.lines[line].strip()!r}")
        doc.lines[line] = head[1] + yaml_scalar(value)
    for item in sorted(dead, key=lambda i: i.start, reverse=True):
        del doc.lines[item.start : item.end]
    if added:
        entries = doc.find(entries_path)
        new_items = [line for table, path in added for line in (f"- name: {yaml_scalar(table)}", f"  path: {yaml_scalar(path)}")]
        if entries is None:
            doc.insert(entries_path, "", [f"  {line}" for line in new_items], beside=doc.find(_path(rule.sources[0])))
        else:
            if _strip_comment(entries.rest):
                doc.lines[entries.start] = f"{' ' * entries.indent}{entries.key}:"
                entries = doc.find(entries_path)
            if entries is None:
                raise MigrationError(f"{rule.to} could not be read back")
            current = doc.items(entries) or []
            item_indent = doc._indent(current[0].start) if current else entries.indent + 2
            doc.lines[entries.end : entries.end] = [f"{' ' * item_indent}{line}" for line in new_items]
    if dir_node is None:
        doc.insert((rule.dir_key,), yaml_scalar(directory), beside=doc.find(_path(rule.sources[0])))

    detail = [f"{rule.to}: {', '.join(table for table, _ in added)} at {directory}"] if added else []
    if moved:
        detail.append(f"{len(moved)} entry path(s) moved from {legacy} to {directory}")
    if dead:
        detail.append(f"{len(dead)} entry(ies) for a table no longer carried dropped")
    if dir_node is None:
        detail.append(f"{rule.dir_key}: {directory}, so the 2.2.0 chart mounts the tables there too")
    return (RESYNC if dead and not added and not moved and dir_node is not None else MIGRATE), "; ".join(detail)


def _derived_and_gone(item: _Item, directory: str, files: list[str]) -> bool:
    """An entry this rule would have written, for a table file the overlay no longer carries."""
    if set(item.fields) != {"name", "path"}:
        return False
    path = scalar(item.fields["path"][1])
    if not path.startswith(f"{directory}/"):
        return False
    filename = path[len(directory) + 1 :]
    return scalar(item.fields["name"][1]) == _table_name(filename) and filename not in files


def _present(doc: _Doc, rule: Rule) -> list[str]:
    found = []
    for source in rule.sources:
        node = doc.find(_path(source))
        if node is not None and doc.holds_value(node):
            found.append(source)
    return found


def _apply(doc: _Doc, rule: Rule, service: str | None, *, resync: bool) -> tuple[str, str] | None:
    if rule.kind in ("move", "copy"):
        return _move(doc, rule, resync=resync)
    if rule.kind == "derive-identity":
        return _identity(doc, rule)
    if rule.kind == "derive-entries":
        return _entries(doc, rule, service or "", resync=resync)
    found = _present(doc, rule)
    if not found:
        return None
    reason = f" -- {rule.why}" if rule.why else ""
    if rule.kind == "refuse":
        return REFUSED, f"{', '.join(found)}{reason}"
    return DROPPED, f"{', '.join(found)}: the thin charts from {{since}} read nothing in its place{reason}"


def _contract(doc: _Doc, rule: Rule) -> tuple[str, str] | None:
    if rule.kind != "move":
        return None
    source = rule.sources[0]
    old = doc.find(_path(source))
    if old is None:
        return None
    label = f"{source} (now {rule.to})"
    if doc.find(_path(rule.to)) is None:
        return (MIGRATE, f"{source} -> {rule.to}, which must be there before {source} can go") if doc.holds_value(old) else None
    doc.delete(_path(source))
    return CONTRACT, label


def _files(deploy: Path) -> list[tuple[str, Path, str | None, bool]]:
    """(name in the deploy repo, path, deploy.service, whether it is infra/common.yaml) of each file the rules read."""
    found: list[tuple[str, Path, str | None, bool]] = []
    overlays = deploy / OVERLAY_DIR
    if overlays.is_dir():
        for path in sorted(overlays.glob(OVERLAY_GLOB)):
            service = yaml_subset.value_at(path.read_text(encoding="utf-8"), ("deploy", "service"))
            found.append(((OVERLAY_DIR / path.name).as_posix(), path, service, False))
    common = deploy / COMMON_OVERLAY
    if common.is_file():
        found.append((COMMON_OVERLAY.as_posix(), common, None, True))
    return found


def _walk(deploy: Path, migration: MigrationMap, *, write: bool, step: str, resync: bool = False) -> list[Hit]:
    hits: list[Hit] = []
    for name, path, service, common in _files(deploy):
        original = path.read_text(encoding="utf-8")
        doc = _Doc(original)
        for rule in migration.rules:
            if not rule.applies(service, common=common):
                continue
            saved = list(doc.lines)
            try:
                found = _contract(doc, rule) if step == "contract" else _apply(doc, rule, service, resync=resync)
            except MigrationError as err:
                doc.lines = saved
                found = (REFUSED, f"{rule.id}: {err}")
            if found is not None:
                state, detail = found
                hits.append(Hit(name, rule, state, detail.replace("{since}", migration.since)))
        if write and doc.text != original:
            path.write_text(doc.text, encoding="utf-8", newline="\n")
    return hits


def scan(deploy: Path, migration: MigrationMap, *, resync: bool = False) -> list[Hit]:
    """What expand would do to each file, with nothing written."""
    return _walk(deploy, migration, write=False, step="expand", resync=resync)


def expand(deploy: Path, migration: MigrationMap, *, resync: bool = False) -> list[Hit]:
    """Write each new key beside its old one, never removing one.

    Args:
        deploy: The deploy repo checkout.
        migration: The rules.
        resync: Rewrite a new key that differs from its old one, from the old one.
            Only while the 2.2.0 charts render, whose engine writes the old keys alone.

    Returns:
        Every hit, its state MIGRATE or RESYNC where the file changed.
    """
    return _walk(deploy, migration, write=True, step="expand", resync=resync)


def contract(deploy: Path, migration: MigrationMap, *, write: bool = True) -> list[Hit]:
    """Drop each `move` rule's old key wherever its new key is there.

    An old key whose new key is missing stays, reported MIGRATE, since dropping
    it would lose the value.
    """
    return _walk(deploy, migration, write=write, step="contract")


def list_items(text: str, dotted: str) -> list[dict[str, str]] | None:
    """Each item of the block list at `dotted` in a YAML file, as its one-line fields, or None where no list is.

    Args:
        text: The file's body.
        dotted: The list's path, such as fileSets.transforms.files.

    Returns:
        Each item's fields that hold a one-line value, read as strings, in file order.
    """
    doc = _Doc(text)
    node = doc.find(_path(dotted))
    if node is None:
        return None
    if _strip_comment(node.rest) == "[]":
        return []
    items = doc.items(node)
    if items is None:
        return None
    return [{key: scalar(rest) for key, (_line, rest) in item.fields.items()} for item in items]


def child_keys(text: str, dotted: str) -> list[str]:
    """The keys of the block mapping at `dotted` in a YAML file, in file order."""
    doc = _Doc(text)
    node = doc.find(_path(dotted))
    indent = doc.child_indent(node) if node is not None else None
    if node is None or indent is None:
        return []
    keys = []
    for j in range(node.start + 1, node.end):
        match = _KEY.match(doc.lines[j])
        if match and len(match["indent"]) == indent:
            keys.append(_unquote(match["key"]))
    return keys


def value_of(text: str, dotted: str) -> str | None:
    """The one-line value at `dotted` in a values file, as it reads, or None where it is not set."""
    node = _Doc(text).find(_path(dotted))
    if node is None or not _strip_comment(node.rest):
        return None
    return scalar(node.rest)


def pending(hits: list[Hit]) -> list[Hit]:
    """The hits that still need a write or a person: MIGRATE, RESYNC and REFUSED."""
    return [hit for hit in hits if hit.state in (MIGRATE, RESYNC, REFUSED)]


def refused(hits: list[Hit]) -> list[Hit]:
    return [hit for hit in hits if hit.state == REFUSED]
