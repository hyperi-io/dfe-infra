#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         docs-site/scripts/gate.py
#  Purpose:      Report process talk, hedges, register words, semicolons, non-ASCII
#                typography and "Elasticsearch" on the pages the docs site publishes.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""gate -- the docs voice gate, over the pages aggregate.py assembled.

    python3 docs-site/scripts/gate.py                                # every build in docs-site/content
    python3 docs-site/scripts/gate.py --baseline /tmp/content-main   # fail only on findings it lacks
    python3 docs-site/scripts/gate.py --report-only --summary "$GITHUB_STEP_SUMMARY"
    python3 docs-site/scripts/gate.py docs/deployment/aws.md         # any markdown, before it merges

Each finding names the member repository, the file under its docs/ and the line,
with the rule and what to write instead. aggregate.py keeps line numbers intact,
so the line is the line in the member's own file.

Rules:

    process        notes to self and the working process (gate.yaml), release
                   candidates (rc.N), rounds, stub and placeholder admonitions
    hedge          a hedge in front of a fact (gate.yaml)
    register       words the house style replaces (gate.yaml)
    semicolon      a semicolon in prose
    ascii          non-ASCII typography in prose: dashes, curly quotes, arrows
    elasticsearch  "Elasticsearch", where the product is Elastic
    marker         an override marker the gate cannot read, which nothing overrides

Only prose is read: front matter, code blocks, inline code, link targets, HTML
tags, comments and maths are skipped.

A page overrides a rule with a marker that carries a reason after `--`:

    <!-- docs-gate: allow semicolon -- the CLI's own help text, quoted -->
    <!-- docs-gate: off ascii -- the maths section --> ... <!-- docs-gate: on ascii -->

`allow` on a line of its own covers the next line that is not blank, and at the
end of a line covers that line. `off` covers every line to the matching `on`, or
to the end of the page. `all` names every rule. An .mdx page writes the marker as
`{/* docs-gate: ... */}`. A marker before a page's first heading is dropped by
aggregate.py, so put it after the H1.

With --baseline, a finding counts as new unless the baseline tree has the same
rule, term and line text on the same page of the same build. Moving a line is
not new, editing it is.
"""

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aggregate

GATE_FILE = aggregate.SITE_ROOT / "gate.yaml"
# The site's own copy, gated beside every assembled tree.
COPY_DIR = aggregate.SITE_ROOT / "copy"
_COPY_LINE = re.compile(r"^(\s*)(-\s+)?([A-Za-z_][\w-]*):(?:\s+(.*))?$")
_FLOW_LABEL = re.compile(r"label:\s*([^,}]+)")
_BLOCK_SCALARS = ("|", "|-", "|+", ">", ">-", ">+")
_NOT_PROSE = frozenset({"to", "href", "icon", "value", "primary", "logo_alt"})
PHRASE_RULES = ("process", "hedge", "register")
RULES = frozenset({*PHRASE_RULES, "semicolon", "ascii", "elasticsearch"})
ALL = "all"
MARKER_RULE = "marker"
ROOT_SOURCE = "hyperi-io/dfe-infra docs-site/content-root"
# The most new findings the summary lists before it counts the rest.
SUMMARY_LIMIT = 300

type Phrases = dict[str, list[tuple[re.Pattern[str], str, str]]]

_MARKER = re.compile(
    r"(?:<!--|\{/\*)\s*docs-gate:\s*(?P<verb>\S+)\s+(?P<rules>[\w,-]+)"
    r"(?:\s*--\s*(?P<reason>.*?))?\s*(?:-->|\*/\})"
)
_COMMENT = re.compile(r"<!--.*?-->|\{/\*.*?\*/\}", re.DOTALL)
_LINK_TARGET = re.compile(r"\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_ENTITY = re.compile(r"&#?\w+;")
_INLINE_MATH = re.compile(r"\$[^$\n]+\$")
_EXTRA_PROCESS = (
    (re.compile(r"\brc\.\d+\b", re.IGNORECASE), "rc.N", "name the release, not a candidate"),
    (re.compile(r"\bround \d+\b", re.IGNORECASE), "round N", "describe the product, not the work"),
    (re.compile(r"\bTBD\b"), "TBD", "write it, or leave it out"),
)
_STUB_ADMONITION = re.compile(r"^\s*:::\s*\w*\s*(stub|placeholder|todo)\b", re.IGNORECASE)
_ELASTICSEARCH = re.compile(r"\belasticsearch\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Finding:
    """One rule a published line breaks.

    Attributes:
        build: The build the page is in, or "" for a file named on the command line.
        page: The page's path in the build, or the file as named.
        line: Its 1-based line number.
        rule: The rule broken.
        term: What matched.
        advice: What to write instead.
        text: The line, stripped, for the report and the baseline match.
    """

    build: str
    page: str
    line: int
    rule: str
    term: str
    advice: str
    text: str

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        """What a baseline finding has to share for this one not to be new."""
        return (self.build, self.page, self.rule, self.term.lower(), self.text)


def load_phrases(tree: dict) -> Phrases:
    """Compile gate.yaml's phrase sections into whole-word patterns.

    Args:
        tree: gate.yaml as `yaml_subset.parse` returns it.

    Returns:
        rule -> [(pattern, phrase, advice)].

    Raises:
        SystemExit: A phrase section is missing or not a map.
    """
    phrases: Phrases = {}
    for rule in PHRASE_RULES:
        section = tree.get(rule)
        if not isinstance(section, dict):
            raise SystemExit(f"error: {GATE_FILE.name}: `{rule}` is not a map of phrases")
        phrases[rule] = [
            (re.compile(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", re.IGNORECASE), phrase, advice)
            for phrase, advice in section.items()
        ]
    return phrases


def prose_lines(text: str) -> Iterator[tuple[int, str, str]]:
    """Every line of a page with the part of it the gate reads.

    Args:
        text: The page's markdown.

    Yields:
        (line number, the line as written, its prose). The prose is empty for a line
        inside front matter, a code block, a comment or a maths block.
    """
    lines = text.splitlines()
    in_front = bool(lines) and lines[0].strip() == "---"
    fence = ""
    in_comment = in_math = False
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if in_front:
            in_front = number == 1 or stripped != "---"
            yield number, line, ""
            continue
        opener = aggregate._FENCE.match(line)
        if fence:
            if (
                opener is not None
                and opener.group(1)[0] == fence[0]
                and len(opener.group(1)) >= len(fence)
                and not line[opener.end() :].strip()
            ):
                fence = ""
            yield number, line, ""
            continue
        if opener and not in_comment:
            fence = opener.group(1)
            yield number, line, ""
            continue
        if in_math or stripped.startswith("$$"):
            one_line = stripped.startswith("$$") and len(stripped) > 4 and stripped.endswith("$$")
            if stripped.startswith("$$") and not one_line:
                in_math = not in_math
            elif in_math and stripped.endswith("$$"):
                in_math = False
            yield number, line, ""
            continue
        prose = line
        if in_comment:
            end = prose.find("-->")
            if end == -1:
                yield number, line, ""
                continue
            prose = prose[end + 3 :]
            in_comment = False
        prose = _COMMENT.sub(" ", prose)
        start = prose.find("<!--")
        if start != -1:
            prose = prose[:start]
            in_comment = True
        prose = aggregate._CODE_SPAN.sub(" ", prose)
        for pattern in (_LINK_TARGET, _URL, _TAG, _ENTITY, _INLINE_MATH):
            prose = pattern.sub(" ", prose)
        yield number, line, prose


def _char_names(chars: Iterable[str]) -> str:
    return ", ".join(f"U+{ord(c):04X} {unicodedata.name(c, '?').lower()}" for c in chars)


def check_line(prose: str, phrases: Phrases) -> Iterator[tuple[str, str, str]]:
    """Every rule one line of prose breaks.

    Args:
        prose: The line's prose, as `prose_lines` gives it.
        phrases: The compiled gate.yaml phrases.

    Yields:
        (rule, term, advice).
    """
    for rule, patterns in phrases.items():
        for pattern, phrase, advice in patterns:
            if pattern.search(prose):
                yield rule, phrase, advice
    for pattern, term, advice in _EXTRA_PROCESS:
        if pattern.search(prose):
            yield "process", term, advice
    if _STUB_ADMONITION.search(prose):
        yield "process", "stub admonition", "write the section, or remove it"
    if ";" in prose:
        yield "semicolon", ";", "two sentences, or join with and, but or because"
    odd = sorted({char for char in prose if ord(char) > 127})
    if odd:
        yield "ascii", _char_names(odd), "-- or - for a dash, ' and \" for quotes, -> for an arrow"
    if _ELASTICSEARCH.search(prose):
        yield "elasticsearch", "Elasticsearch", "say Elastic"


def _marker_problem(verb: str, rules: set[str], reason: str) -> str:
    """Why a marker cannot be read, or "" when it can."""
    if verb not in ("allow", "off", "on"):
        return f"unknown verb {verb!r} (allow, off or on)"
    unknown = rules - RULES - {ALL}
    if unknown:
        return f"unknown rule {', '.join(sorted(unknown))}"
    if verb != "on" and not reason:
        return "no reason after --"
    return ""


def check_page(text: str, phrases: Phrases, *, build: str, page: str) -> list[Finding]:
    """Every finding on one page, less the ones its markers override.

    Args:
        text: The page's markdown.
        phrases: The compiled gate.yaml phrases.
        build: The build the page is in.
        page: The page's path in the build.

    Returns:
        The findings, in line order.
    """
    findings: list[Finding] = []
    off: set[str] = set()
    pending: set[str] = set()
    for number, line, prose in prose_lines(text):
        allowed = set(off)
        for marker in _MARKER.finditer(line):
            verb, reason = marker["verb"], (marker["reason"] or "").strip()
            rules = {rule.strip() for rule in marker["rules"].split(",") if rule.strip()}
            problem = _marker_problem(verb, rules, reason)
            if problem:
                findings.append(
                    Finding(build, page, number, MARKER_RULE, problem, "fix it", line.strip())
                )
                continue
            named = set(RULES) if ALL in rules else rules
            if verb == "off":
                off |= named
            elif verb == "on":
                off -= named
            elif _MARKER.sub("", line).strip():
                allowed |= named
            else:
                pending |= named
        if not prose.strip():
            continue
        allowed |= pending
        pending = set()
        for rule, term, advice in check_line(prose, phrases):
            if rule not in allowed:
                findings.append(Finding(build, page, number, rule, term, advice, line.strip()))
    return findings


def pages_in(content: Path) -> Iterator[tuple[str, str, Path]]:
    """Every published markdown page in an assembled content tree.

    Args:
        content: A tree aggregate.py wrote, holding site.json.

    Yields:
        (build, page path in the build, file). The generated API pages are skipped.
    """
    site = json.loads((content / aggregate.SITE_FILE).read_text(encoding="utf-8"))
    for build in site["builds"]:
        root = content / build["name"]
        generated = (build.get("openapi") or {}).get("output")
        skip = content / generated if generated else None
        for path in sorted(root.rglob("*")):
            if not path.is_file() or not path.name.lower().endswith(aggregate.MARKDOWN):
                continue
            if skip and skip in path.parents:
                continue
            yield build["name"], path.relative_to(root).as_posix(), path


def sources(content: Path) -> dict[str, dict[str, str]]:
    """Where each build's top-level directories come from, for the report.

    Args:
        content: A tree aggregate.py wrote.

    Returns:
        build -> member name -> owner/name.
    """
    site = json.loads((content / aggregate.SITE_FILE).read_text(encoding="utf-8"))
    return {
        build["name"]: {member["name"]: member["repo"] for member in build["members"]}
        for build in site["builds"]
    }


def source_of(finding: Finding, repos: dict[str, dict[str, str]]) -> str:
    """The member file a finding is in: owner/name, then its path under docs/.

    Args:
        finding: The finding.
        repos: `sources` for the tree it came from.

    Returns:
        Such as ``hyperi-io/dfe-infra docs/deployment/aws.md``, or the page as named.
    """
    if not finding.build:
        return finding.page
    members = repos.get(finding.build, {})
    parts = finding.page.split("/")
    if parts[0] not in members:
        return f"{ROOT_SOURCE}/{finding.page}"
    if len(parts) > 2 and parts[1] in members:
        parts = parts[1:]
    return f"{members[parts[0]]} docs/{'/'.join(parts[1:])}"


def gate_tree(content: Path, phrases: Phrases) -> list[Finding]:
    """Every finding in an assembled tree.

    Args:
        content: A tree aggregate.py wrote.
        phrases: The compiled gate.yaml phrases.

    Returns:
        The findings, build by build, page by page.
    """
    findings: list[Finding] = []
    for build, page, path in pages_in(content):
        text = path.read_text(encoding="utf-8", errors="replace")
        findings += check_page(text, phrases, build=build, page=page)
    return findings


def copy_prose(text: str) -> str:
    """The prose in a copy/ YAML file, every other line blanked so line numbers hold.

    Keys, link targets, icons, comments and `code:` blocks are not prose. A flow
    mapping keeps its label only.

    Args:
        text: The YAML file's text.

    Returns:
        The text with only prose values left, one per original line.
    """
    lines: list[str] = []
    code_indent: int | None = None
    for line in text.splitlines():
        indent = len(line) - len(line.lstrip())
        if code_indent is not None and (not line.strip() or indent > code_indent):
            lines.append("")
            continue
        code_indent = None
        stripped = line.strip()
        if stripped.startswith("- {"):
            lines.append(" ".join(_FLOW_LABEL.findall(stripped)))
            continue
        match = _COPY_LINE.match(line)
        if not stripped or stripped.startswith("#") or not match:
            lines.append("")
            continue
        _, dash, key, value = match.groups()
        value = (value or "").strip()
        if key == "code" and value in _BLOCK_SCALARS:
            code_indent = indent + len(dash or "")
            lines.append("")
        elif key in _NOT_PROSE:
            lines.append("")
        elif value.startswith("{"):
            lines.append(" ".join(_FLOW_LABEL.findall(value)))
        else:
            lines.append(value.strip("\"'"))
    return "\n".join(lines)


def gate_files(paths: Iterable[Path], phrases: Phrases) -> list[Finding]:
    """Every finding in markdown or copy/ YAML files named, or found under named directories.

    Args:
        paths: Files or directories.
        phrases: The compiled gate.yaml phrases.

    Returns:
        The findings.
    """
    findings: list[Finding] = []
    for named in paths:
        files = sorted(named.rglob("*")) if named.is_dir() else [named]
        for path in files:
            name = path.name.lower()
            if not path.is_file():
                continue
            if name.endswith(aggregate.MARKDOWN):
                text = path.read_text(encoding="utf-8", errors="replace")
            elif name.endswith((".yaml", ".yml")):
                text = copy_prose(path.read_text(encoding="utf-8", errors="replace"))
            else:
                continue
            findings += check_page(text, phrases, build="", page=path.as_posix())
    return findings


def report(
    findings: list[Finding], new: list[Finding], repos: dict, *, baselined: bool
) -> tuple[list[str], list[str]]:
    """The console lines and the markdown summary.

    A page published in more than one build is listed once, with each build named.

    Args:
        findings: Every finding.
        new: The findings the baseline lacks, or every finding without one.
        repos: `sources` for the tree.
        baselined: Whether a baseline was given.

    Returns:
        (console lines, summary lines).
    """
    grouped: dict[tuple[str, int, str, str], list[Finding]] = {}
    for finding in new:
        where = (source_of(finding, repos), finding.line, finding.rule, finding.term)
        grouped.setdefault(where, []).append(finding)
    console = []
    rows = []
    for (where, line, rule, term), same in grouped.items():
        builds = ", ".join(f.build for f in same if f.build)
        tag = f" [{builds}]" if builds else ""
        console.append(f"{where}:{line}: {rule}: {term} -> {same[0].advice}{tag}")
        rows.append(f"| `{where}:{line}` | {rule} | {term.replace('|', '/')} | {same[0].advice} |")
    counts = Counter(where[2] for where in grouped)
    label = "new against main" if baselined else "found"
    headline = f"{len(grouped)} finding(s) {label}, {len(findings)} across every build in all."
    summary = ["## Docs gate", "", headline, ""]
    if counts:
        summary += ["| rule | count |", "|---|---|"]
        summary += [f"| {rule} | {count} |" for rule, count in counts.most_common()]
        summary += ["", "| where | rule | term | instead |", "|---|---|---|---|"]
        summary += rows[:SUMMARY_LIMIT]
        if len(rows) > SUMMARY_LIMIT:
            summary += ["", f"{len(rows) - SUMMARY_LIMIT} more in the job log."]
        summary.append("")
    summary.append(
        "Override a rule on a line with `<!-- docs-gate: allow <rule> -- <reason> -->`. "
        "`docs-site/scripts/gate.py --help` has the rules and the block form."
    )
    return console, summary


def main(argv: list[str] | None = None) -> int:
    """Gate the assembled pages, or the files named.

    Args:
        argv: The command-line arguments, less the program name.

    Returns:
        The exit status: 1 when a finding is new (every finding without --baseline),
        unless --report-only.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("paths", nargs="*", type=Path, help="markdown files or directories")
    parser.add_argument(
        "--content", type=Path, default=aggregate.DEFAULT_OUT, help="the assembled tree"
    )
    parser.add_argument("--baseline", type=Path, help="an assembled tree whose findings are old")
    parser.add_argument("--summary", type=Path, help="append a markdown summary here")
    parser.add_argument("--report-only", action="store_true", help="always exit 0")
    args = parser.parse_args(argv)

    text = GATE_FILE.read_text(encoding="utf-8")
    phrases = load_phrases(aggregate.yaml_subset.parse(text, source=str(GATE_FILE)))
    if args.paths:
        findings, repos = gate_files(args.paths, phrases), {}
    else:
        if not (args.content / aggregate.SITE_FILE).is_file():
            raise SystemExit(f"error: {args.content} holds no site.json: run aggregate.py first")
        findings, repos = gate_tree(args.content, phrases), sources(args.content)
        findings += gate_files([COPY_DIR], phrases)
    new = findings
    if args.baseline:
        old = {finding.key for finding in gate_tree(args.baseline, phrases)}
        new = [finding for finding in findings if finding.key not in old]
    console, summary = report(findings, new, repos, baselined=bool(args.baseline))
    print("\n".join([*console, "", summary[2]]))
    if args.summary:
        with args.summary.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(summary) + "\n")
    return 1 if new and not args.report_only else 0


if __name__ == "__main__":
    sys.exit(main())
