#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/check_tofu_literals.py
#  Purpose:      Refuse a sizing, type, capacity, retention, placement or version
#                literal in the OpenTofu tree: those values arrive in tfvars.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Find the literals the OpenTofu code may not carry.

An instance type, a node count, a volume's size, type, IOPS or throughput, a
retention period, a CIDR, a region, a tier or an engine version is a value a
deployment chooses. It arrives in tfvars -- the dial's render or the shape
resolver's -- and never as a literal in a module or a root. A module variable's
default for one is the same literal in another place.

    python3 scripts/check_tofu_literals.py

Exit 0 clean, 1 on a finding or on an ALLOWED entry that no longer matches one.

What it reads, in every .tf file under terraform/:
  - every attribute of a resource, data or module block, nested blocks and
    object constructors included;
  - every local;
  - every variable's default, and every optional() default in its type.

The NAME decides whether a value is in scope (SCOPE below). A literal number or
string in its expression is the finding; a comparison operand, an index, a
lookup key, an empty value and the any-address range are not values. ALLOWED
holds the contract constants -- fixed by a vendor, a protocol or another
component rather than chosen -- each with the reason it stays.

Stdlib only, like every other script here: the scanner reads the HCL subset
this tree uses, and a construct it cannot read fails loudly rather than being
skipped.
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# A name in scope: one of these words, bounded by an underscore or the end.
SCOPE = re.compile(
    r"(?:^|_)("
    r"instance_types?|node_type|broker_type|machine_type|vm_size|ami_type|"
    r"volume_type|disk_type|storage_type|capacity_type|tier|sku|profile|"
    r"size|sizes|size_gib|size_gb|disk_gb|"
    r"iops|throughput|capacity|headroom|mb_s|"
    r"count|replicas|brokers|nodes|partitions|step|"
    r"retention|days|period|periods|window|"
    r"cidrs?|cidr_block|cidr_ipv4|regions?|zones?|azs|"
    r"versions?|runtime"
    r")(?:_|$)"
)

# Meta-arguments: their value is structure, not a size.
META = frozenset({"count", "for_each", "depends_on", "provider", "providers", "source"})

# Every address, written in the one spelling a rule allows for a whole internet.
ANY_ADDRESS = frozenset({"0.0.0.0/0", "::/0"})

COMPARISON = frozenset({"==", "!=", "<", ">", "<=", ">="})

# A literal argument to one of these is a position or a key, never a size.
POSITIONAL_CALLS = frozenset({"slice", "substr", "element", "index", "lookup", "split", "regex", "regexall"})

# A literal argument to one of these is the value the expression falls back to.
FALLBACK_CALLS = frozenset({"try", "coalesce", "coalescelist"})

# The blocks whose attributes are scanned, and the ones that are not.
SCANNED_BLOCKS = frozenset({"resource", "data", "module", "variable", "locals"})
UNSCANNED_BLOCKS = frozenset({"terraform", "provider", "output", "moved", "check", "import", "removed"})

# The contract constants, keyed "<path>:<address>:<attribute>". A value here is
# fixed by a vendor, a protocol or another component, and the reason says which.
_AWS_ROOT = "terraform/environments/aws"
_CLUSTER = "terraform/modules/kubernetes-cluster/aws"
_KAFKA = "terraform/modules/managed-kafka"
_SECRET_WINDOW = (
    "Secrets Manager's own 30-day window, or 0 for an ephemeral deployment rebuilt under "
    "the same names, picked by tags.lifecycle."
)
ALLOWED: dict[str, str] = {
    f"{_AWS_ROOT}/main.tf:locals:secret_recovery_window_days": _SECRET_WINDOW,
    f"{_AWS_ROOT}/main.tf:locals:kafka_secret_recovery_window_days": _SECRET_WINDOW,
    "terraform/environments/local/main.tf:locals:region": (
        "On-prem has no cloud region, so the region tag carries the cloud token."
    ),
    f"{_CLUSTER}/eks.tf:locals:node_ami_type": (
        "EKS's enum name for the arm64 Amazon Linux 2023 image, which the arm64 shape rule fixes."
    ),
    f"{_CLUSTER}/karpenter.tf:locals:karpenter_interruption_retention_seconds": (
        "Karpenter's published retention for its interruption queue."
    ),
    f"{_CLUSTER}/kms.tf:locals:kms_deletion_window_days": (
        "KMS's own 7-day floor for an ephemeral deployment and its 30-day default otherwise, "
        "picked by tags.lifecycle."
    ),
    f"{_CLUSTER}/object-store.tf:locals:multipart_abort_days": (
        "Housekeeping for the parts of inserts ClickHouse has already retried, never a window "
        "over data it keeps."
    ),
    f"{_KAFKA}/confluent-cloud/variables.tf:variable.private_network_interfaces_per_zone:default": (
        "Confluent's documented interface count per subnet for a Freight Private Network "
        "Interface attachment."
    ),
    f"{_KAFKA}/msk/autoscaler.tf:locals:broker_scaler_runtime": (
        "The Lambda runtime the inline handler beside it is written for."
    ),
    f"{_KAFKA}/redpanda-cloud/variables.tf:variable.scram_password_version:default": (
        "A rotation counter the caller bumps to push a new password, starting at the first."
    ),
}


class ScanError(ValueError):
    """A .tf file holds a construct the scanner cannot read."""


@dataclass(frozen=True, slots=True)
class Token:
    """One lexical token of an HCL file."""

    kind: str
    text: str
    line: int
    interpolated: bool = False


@dataclass(slots=True)
class Attribute:
    """An `name = expression` line inside a block body."""

    name: str
    tokens: list[Token]
    line: int


@dataclass(slots=True)
class Block:
    """A block: its type, its labels and its body."""

    kind: str
    labels: list[str]
    line: int
    attributes: list[Attribute] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Hit:
    """One literal in scope, where it sits and what it is."""

    path: str
    line: int
    address: str
    attribute: str
    literal: str

    @property
    def key(self) -> str:
        """The ALLOWED key for this literal: line numbers drift, addresses do not."""
        return f"{self.path}:{self.address}:{self.attribute}"


_NUMBER = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_HEREDOC = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_]*)[ \t]*\n")
_PUNCT_LONG = ("...", "==", "!=", "<=", ">=", "=>", "&&", "||")


def _string_end(text: str, start: int) -> tuple[int, bool]:
    """Index of the closing quote of the string opened at `start`, and whether it interpolates."""
    index = start + 1
    interpolated = False
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index, interpolated
        if text.startswith(("${", "%{"), index):
            interpolated = True
            index = _template_end(text, index)
            continue
        index += 1
    raise ScanError(f"unterminated string opened at offset {start}")


def _template_end(text: str, start: int) -> int:
    """Index just past the `}` closing the `${` or `%{` at `start`."""
    index = start + 2
    depth = 1
    while index < len(text) and depth:
        char = text[index]
        if char == '"':
            index = _string_end(text, index)[0] + 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return index


def lex(text: str) -> list[Token]:
    """Turn HCL source into tokens, dropping comments and keeping newlines.

    Args:
        text: The file's content.

    Returns:
        The token list, in source order.

    Raises:
        ScanError: An unterminated string or heredoc.
    """
    tokens: list[Token] = []
    index = 0
    line = 1
    while index < len(text):
        char = text[index]
        if char == "\n":
            tokens.append(Token("newline", "\n", line))
            line += 1
            index += 1
        elif char in " \t\r":
            index += 1
        elif char == "#" or text.startswith("//", index):
            end = text.find("\n", index)
            index = len(text) if end == -1 else end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                raise ScanError(f"line {line}: unterminated block comment")
            line += text.count("\n", index, end)
            index = end + 2
        elif char == '"':
            end, interpolated = _string_end(text, index)
            tokens.append(Token("string", text[index + 1 : end], line, interpolated))
            index = end + 1
        elif heredoc := _HEREDOC.match(text, index):
            marker = heredoc.group(1)
            body_start = heredoc.end()
            closing = re.compile(rf"^[ \t]*{re.escape(marker)}[ \t]*$", re.MULTILINE)
            found = closing.search(text, body_start)
            if found is None:
                raise ScanError(f"line {line}: unterminated heredoc {marker}")
            tokens.append(Token("heredoc", text[body_start : found.start()], line, True))
            line += text.count("\n", index, found.end())
            index = found.end()
        elif number := _NUMBER.match(text, index) if char.isdigit() else None:
            tokens.append(Token("number", number.group(), line))
            index = number.end()
        elif ident := _IDENT.match(text, index):
            tokens.append(Token("ident", ident.group(), line))
            index = ident.end()
        else:
            punct = next((p for p in _PUNCT_LONG if text.startswith(p, index)), char)
            tokens.append(Token("punct", punct, line))
            index += len(punct)
    return tokens


def _read_expression(tokens: list[Token], pos: int) -> tuple[list[Token], int]:
    """The tokens of one attribute's expression, ending at a newline outside brackets."""
    depth = 0
    out: list[Token] = []
    while pos < len(tokens):
        token = tokens[pos]
        if token.kind == "newline" and depth == 0:
            break
        if token.kind == "punct" and token.text in "([{":
            depth += 1
        elif token.kind == "punct" and token.text in ")]}":
            if depth == 0:
                break
            depth -= 1
        out.append(token)
        pos += 1
    return out, pos


def _parse_body(tokens: list[Token], pos: int) -> tuple[list[Attribute], list[Block], int]:
    """Parse attributes and blocks until the closing brace or the end of the file."""
    attributes: list[Attribute] = []
    blocks: list[Block] = []
    while pos < len(tokens):
        token = tokens[pos]
        if token.kind == "newline" or token.text == ";":
            pos += 1
            continue
        if token.kind == "punct" and token.text == "}":
            return attributes, blocks, pos
        if token.kind != "ident":
            raise ScanError(f"line {token.line}: expected an attribute or a block, got {token.text!r}")
        pos += 1
        if pos < len(tokens) and tokens[pos].text == "=":
            expression, pos = _read_expression(tokens, pos + 1)
            attributes.append(Attribute(token.text, expression, token.line))
            continue
        labels: list[str] = []
        while pos < len(tokens) and tokens[pos].kind in ("string", "ident"):
            labels.append(tokens[pos].text)
            pos += 1
        if pos >= len(tokens) or tokens[pos].text != "{":
            raise ScanError(f"line {token.line}: block {token.text} opens no body")
        inner_attributes, inner_blocks, pos = _parse_body(tokens, pos + 1)
        if pos >= len(tokens):
            raise ScanError(f"line {token.line}: block {token.text} is never closed")
        blocks.append(Block(token.text, labels, token.line, inner_attributes, inner_blocks))
        pos += 1
    return attributes, blocks, pos


def parse(text: str) -> list[Block]:
    """The top-level blocks of one .tf file.

    Raises:
        ScanError: A construct outside the subset this scanner reads.
    """
    tokens = lex(text)
    attributes, blocks, pos = _parse_body(tokens, 0)
    if attributes:
        raise ScanError(f"line {attributes[0].line}: an attribute outside any block")
    if pos < len(tokens):
        raise ScanError(f"line {tokens[pos].line}: an unmatched closing brace")
    return blocks


def in_scope(name: str) -> bool:
    """Whether a value under this name is one a deployment chooses."""
    return name not in META and bool(SCOPE.search(name.lower().replace("-", "_").replace(".", "_")))


def _punct(token: Token | None) -> str:
    """A punctuation token's text, or empty for anything else."""
    return token.text if token is not None and token.kind == "punct" else ""


def _neighbour_index(tokens: list[Token], index: int, step: int) -> int | None:
    """The position of the nearest token to tokens[index] in direction `step` that is not a newline."""
    probe = index + step
    while 0 <= probe < len(tokens):
        if tokens[probe].kind != "newline":
            return probe
        probe += step
    return None


def _neighbour(tokens: list[Token], index: int, step: int) -> Token | None:
    """The nearest token to tokens[index] in direction `step` that is not a newline."""
    found = _neighbour_index(tokens, index, step)
    return None if found is None else tokens[found]


def _enclosing_call(tokens: list[Token], index: int) -> tuple[str, int] | None:
    """The function whose argument list holds tokens[index], and the argument's position."""
    depth = 0
    position = 0
    for back in range(index - 1, -1, -1):
        text = _punct(tokens[back])
        if text in (")", "]", "}"):
            depth += 1
        elif text in ("[", "{"):
            if depth == 0:
                return None
            depth -= 1
        elif text == "(":
            if depth:
                depth -= 1
                continue
            before = _neighbour(tokens, back, -1)
            if before is not None and before.kind == "ident":
                return before.text, position
            return None
        elif text == "," and depth == 0:
            position += 1
    return None


def _is_index(tokens: list[Token], index: int) -> bool:
    """Whether tokens[index] sits alone between index brackets, as in `list[0]`."""
    opens = _neighbour_index(tokens, index, -1)
    if opens is None or _punct(tokens[opens]) != "[" or _punct(_neighbour(tokens, index, 1)) != "]":
        return False
    before = _neighbour(tokens, opens, -1)
    return before is not None and (before.kind in ("ident", "string") or _punct(before) in ("]", ")"))


def _significant(tokens: list[Token]) -> list[Token]:
    """The tokens of an expression without its line breaks."""
    return [token for token in tokens if token.kind != "newline"]


def _is_literal_expression(tokens: list[Token]) -> bool:
    """A lone literal, or a list of nothing but literals."""
    body = _significant(tokens)
    if len(body) == 1:
        return body[0].kind in ("string", "number")
    if len(body) == 2 and _punct(body[0]) == "-" and body[1].kind == "number":
        return True
    if body and _punct(body[0]) == "[" and _punct(body[-1]) == "]":
        inner = [t for t in body[1:-1] if _punct(t) != ","]
        return all(t.kind in ("string", "number") and not t.interpolated for t in inner)
    return False


def _switch_positions(tokens: list[Token]) -> set[int]:
    """Positions of each `? 1 : 0` pair: an instance count switched on or off, not a size."""
    positions: set[int] = set()
    for index, token in enumerate(tokens):
        if _punct(token) != "?":
            continue
        then = _neighbour_index(tokens, index, 1)
        colon = None if then is None else _neighbour_index(tokens, then, 1)
        other = None if colon is None else _neighbour_index(tokens, colon, 1)
        if then is None or colon is None or other is None or _punct(tokens[colon]) != ":":
            continue
        branches = {tokens[then].text, tokens[other].text}
        if tokens[then].kind == tokens[other].kind == "number" and branches == {"0", "1"}:
            positions.update((then, other))
    return positions


def literal_values(tokens: list[Token]) -> list[Token]:
    """The literal numbers and strings an expression carries as values.

    Args:
        tokens: One expression's tokens, line breaks included.

    Returns:
        Each literal that is a value: not a comparison operand, an index, an
        object key, a lookup key, an empty string or the any-address range.
    """
    whole = _is_literal_expression(tokens)
    switches = _switch_positions(tokens)
    found: list[Token] = []
    for index, token in enumerate(tokens):
        if token.kind not in ("number", "string") or index in switches:
            continue
        previous = tokens[index - 1] if index else None
        before = _punct(_neighbour(tokens, index, -1))
        after = _punct(_neighbour(tokens, index, 1))
        if before in COMPARISON or after in COMPARISON:
            continue
        item_start = previous is None or previous.kind == "newline" or before in ("{", ",")
        if after == "=" or (after == ":" and item_start):
            continue
        if token.kind == "string" and (token.interpolated or token.text in ("", *ANY_ADDRESS)):
            continue
        if token.kind == "number" and _is_index(tokens, index):
            continue
        call = _enclosing_call(tokens, index)
        lookup_default = call is not None and call[0] == "lookup" and call[1] == 2
        if call is not None and call[0] in POSITIONAL_CALLS and not lookup_default:
            continue
        list_element = call is None and before in ("[", ",")
        fallback = call is not None and (call[0] in FALLBACK_CALLS or lookup_default)
        if token.kind == "number" or whole or before in ("?", ":", "=") or list_element or fallback:
            found.append(token)
    return found


def _object_items(tokens: list[Token]) -> list[tuple[str, list[Token]]] | None:
    """The `key = value` pairs of an object constructor, or None for anything else."""
    positions = [index for index, token in enumerate(tokens) if token.kind != "newline"]
    if not positions:
        return None
    first, last = positions[0], positions[-1]
    if _punct(tokens[first]) != "{" or _punct(tokens[last]) != "}":
        return None
    if len(positions) > 1 and tokens[positions[1]].kind == "ident" and tokens[positions[1]].text == "for":
        return None
    inner = tokens[first + 1 : last]
    items: list[tuple[str, list[Token]]] = []
    pos = 0
    while pos < len(inner):
        token = inner[pos]
        if token.kind == "newline" or _punct(token) == ",":
            pos += 1
            continue
        is_key = token.kind in ("ident", "string") and not token.interpolated
        if not is_key or pos + 1 >= len(inner) or _punct(inner[pos + 1]) not in ("=", ":"):
            return None
        key = token.text
        value: list[Token] = []
        depth = 0
        pos += 2
        while pos < len(inner):
            current = inner[pos]
            if depth == 0 and (current.kind == "newline" or _punct(current) == ","):
                break
            if current.kind == "punct" and current.text in "([{":
                depth += 1
            elif current.kind == "punct" and current.text in ")]}":
                depth -= 1
            value.append(current)
            pos += 1
        items.append((key, value))
    return items


def _value_hits(tokens: list[Token], active: bool, attribute: str) -> list[tuple[str, Token]]:
    """Literal values under a name in scope, recursing through object constructors."""
    items = _object_items(tokens)
    if items is not None:
        hits: list[tuple[str, Token]] = []
        for key, value in items:
            hits.extend(_value_hits(value, active or in_scope(key), f"{attribute}.{key}"))
        return hits
    if not active:
        return []
    return [(attribute, token) for token in literal_values(tokens)]


def _optional_defaults(tokens: list[Token]) -> list[tuple[str, list[Token]]]:
    """Each `name = optional(type, default)` in a type expression, as (name, default tokens)."""
    found: list[tuple[str, list[Token]]] = []
    body = _significant(tokens)
    for index, token in enumerate(body):
        if token.text != "optional" or index + 1 >= len(body) or body[index + 1].text != "(":
            continue
        depth = 0
        arguments: list[list[Token]] = [[]]
        for current in body[index + 2 :]:
            if current.kind == "punct" and current.text in "([{":
                depth += 1
            elif current.kind == "punct" and current.text in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif current.text == "," and depth == 0:
                arguments.append([])
                continue
            arguments[-1].append(current)
        name = body[index - 2].text if index >= 2 and body[index - 1].text == "=" else ""
        if len(arguments) == 2 and name:
            found.append((name, arguments[1]))
    return found


def _block_hits(block: Block, address: str, prefix: str) -> list[tuple[str, int, str, Token]]:
    """Every in-scope literal in a resource, data or module body, nested blocks included."""
    hits: list[tuple[str, int, str, Token]] = []
    for attribute in block.attributes:
        name = f"{prefix}{attribute.name}"
        for path, token in _value_hits(attribute.tokens, in_scope(attribute.name), name):
            hits.append((address, token.line, path, token))
    for inner in block.blocks:
        inner_name = inner.labels[0] if inner.kind == "dynamic" and inner.labels else inner.kind
        hits.extend(_block_hits(inner, address, f"{prefix}{inner_name}."))
    return hits


def scan_text(text: str, path: str) -> list[Hit]:
    """Every in-scope literal in one .tf file.

    Args:
        text: The file's content.
        path: Its path relative to the repository root, carried into each hit.

    Returns:
        One Hit per literal value under a name in scope.

    Raises:
        ScanError: A construct outside the subset this scanner reads.
    """
    hits: list[Hit] = []
    for block in parse(text):
        if block.kind in UNSCANNED_BLOCKS:
            continue
        if block.kind not in SCANNED_BLOCKS:
            raise ScanError(f"{path}:{block.line}: a top-level {block.kind} block this scanner does not know")
        if block.kind in ("resource", "data"):
            address = ".".join([block.kind, *block.labels])
            found = _block_hits(block, address, "")
        elif block.kind == "module":
            found = _block_hits(block, f"module.{block.labels[0]}", "")
        elif block.kind == "locals":
            found = [
                ("locals", token.line, attribute_path, token)
                for attribute in block.attributes
                for attribute_path, token in _value_hits(
                    attribute.tokens, in_scope(attribute.name), attribute.name
                )
            ]
        else:
            found = _variable_hits(block)
        hits.extend(Hit(path, line, address, attribute, token.text) for address, line, attribute, token in found)
    return hits


def _variable_hits(block: Block) -> list[tuple[str, int, str, Token]]:
    """A variable's default, and each optional() default in its type, under a name in scope."""
    name = block.labels[0]
    address = f"variable.{name}"
    hits: list[tuple[str, int, str, Token]] = []
    for attribute in block.attributes:
        if attribute.name == "default":
            for path, token in _value_hits(attribute.tokens, in_scope(name), "default"):
                hits.append((address, token.line, path, token))
        elif attribute.name == "type":
            for field_name, default in _optional_defaults(attribute.tokens):
                active = in_scope(field_name) or in_scope(name)
                for path, token in _value_hits(default, active, f"type.{field_name}"):
                    hits.append((address, token.line, path, token))
    return hits


def scan_tree(repo_root: Path = REPO_ROOT) -> list[Hit]:
    """Every in-scope literal in every .tf file under `repo_root`/terraform, keyed relative to it."""
    hits: list[Hit] = []
    for path in sorted((repo_root / "terraform").rglob("*.tf")):
        if ".terraform" in path.parts:
            continue
        relative = path.relative_to(repo_root).as_posix()
        hits.extend(scan_text(path.read_text(encoding="utf-8"), relative))
    return hits


def main(repo_root: Path = REPO_ROOT) -> int:
    """Print every finding and every stale allowance, and exit 1 on either."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    try:
        hits = scan_tree(repo_root)
    except ScanError as error:
        print(f"check_tofu_literals: cannot read the tree: {error}", file=sys.stderr)
        return 1
    findings = [hit for hit in hits if hit.key not in ALLOWED]
    stale = sorted(set(ALLOWED) - {hit.key for hit in hits})
    for hit in findings:
        print(f"{hit.path}:{hit.line}: {hit.address} {hit.attribute} = {hit.literal!r} -- move it to tfvars")
    for key in stale:
        print(f"ALLOWED entry matches no literal any more, so drop it: {key}")
    if findings or stale:
        print(
            f"check_tofu_literals: {len(findings)} literal(s) in scope and {len(stale)} stale allowance(s)",
            file=sys.stderr,
        )
        return 1
    print(f"check_tofu_literals: OK -- {len(hits)} allowed contract constant(s), no other literal in scope")
    return 0


if __name__ == "__main__":
    sys.exit(main())
