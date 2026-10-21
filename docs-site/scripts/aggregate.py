#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         docs-site/scripts/aggregate.py
#  Purpose:      Gather every public suite member's docs/ at the refs each
#                released stack pins into the docs site's content tree.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""aggregate -- one content tree per released DFE stack, pulled from the members.

    python3 docs-site/scripts/aggregate.py                  # every released stack
    python3 docs-site/scripts/aggregate.py --stack 2.2.0    # one of them
    python3 docs-site/scripts/aggregate.py --next           # plus /next/ from each default branch
    python3 docs-site/scripts/aggregate.py --work-dir .tmp/docs-clones   # keep the clones

A released stack is a versions.yaml block at `maturity: release`, not withdrawn,
named X.Y.Z with no prerelease suffix. Membership and the sidebar order come from
`dfe-stack suite`, lane by lane, first appearance winning. Each member is fetched
anonymously at the ref its stack pins: `apps.<member>` or `content.<member>`, and
the stack's own tag for dfe-infra. A member the stack does not pin, or one an
anonymous fetch refuses, is reported and left out.

Writes, under --out (default docs-site/content/):

    <build>/<member>/...              the member's docs/, links that leave docs/ sent to GitHub
    <build>/<member>/_category_.json  its sidebar label and lane position
    <build>/...                       docs-site/content-root/, the suite's own pages
    _specs/<build>/dfe-engine.json    the engine's committed OpenAPI spec at its pin
    _reports/strays-<build>.md        every docs/ file outside the documentation standard
    site.json                         the builds, newest release first, for docusaurus.config.js

Needs python3 and git, and reads public repositories only.
"""

import argparse
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SITE_ROOT = REPO_ROOT / "docs-site"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import yaml_subset  # noqa: E402

VERSIONS_FILE = REPO_ROOT / "versions.yaml"
DFE_STACK = REPO_ROOT / "scripts" / "dfe-stack"
CONTENT_ROOT = SITE_ROOT / "content-root"
DEFAULT_OUT = SITE_ROOT / "content"

GITHUB = "https://github.com"
RAW_GITHUB = "https://raw.githubusercontent.com"
RELEASE = "release"
WITHDRAWN = "withdrawn"
NEXT = "next"
HEAD = "HEAD"
# release.yml cuts the stack version as this repo's own tag.
STACK_REPO_MEMBER = "dfe-infra"
PIN_SECTIONS = ("apps", "content")
# Working artefacts some docs/ trees carry, which are not product documentation.
WORKING_DIRS = frozenset({"plans", "superpowers"})
# The documentation standard binds the repos we control upstream. A fork follows its upstream.
UPSTREAM_LED = frozenset({"fork"})
SPEC_MEMBER = "dfe-engine"
SPEC_FILE = "openapi-spec/openapi.json"
API_DIR = "api"
API_LABEL = "HTTP API reference"
SPARSE_PATTERNS = ("/docs/", f"/{SPEC_FILE}")
# The root pages hold the sidebar positions below this one, and members follow in lane order.
FIRST_MEMBER_POSITION = 10
MARKDOWN = (".md", ".mdx")
NOT_RENDERED = (".rst", ".adoc", ".txt")
IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
BANNED_NAMES = frozenset({"design.md", "context.md", "notes.md", "todo.md", "scratch.md"})
LANDING = "README.md"
# What git prints when an anonymous fetch meets a private or missing repository.
REFUSED = ("could not read Username", "Repository not found", "Authentication failed")
CATEGORY_FILE = "_category_.json"
SITE_FILE = "site.json"

_RELEASE_NAME = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)")
_INLINE_TARGET = re.compile(r"(\]\(\s*)(<[^>\n]*>|[^)\s]+)")
_REF_DEFINITION = re.compile(r"^( {0,3}\[[^\]\n]+\]:[ \t]*)(<[^>\n]*>|\S+)")
_HTML_TARGET = re.compile(r"(\b(?:href|src)\s*=\s*\")([^\"]*)(\")")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# A brace opens a JSX expression, and a less-than opens a tag only before a letter, / or !.
_MDX_SPECIAL = re.compile(r"(?<!\\)([{}]|<(?=[A-Za-z/!]))")
_MDX_REFERENCE = {"{": "&#123;", "}": "&#125;", "<": "&lt;"}
_FRONT_MATTER = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*\r?\n", re.DOTALL)
_LEADING_COMMENT = re.compile(r"\A\s*<!--.*?-->[ \t]*(?:\r?\n)*", re.DOTALL)


class Rule(StrEnum):
    """Why a docs/ file sits outside the documentation standard, or a member was left out."""

    LANDING = "no README.md landing page"
    INDEX_LANDING = "landing page named index.md (the standard lands on README.md)"
    CASE = "uppercase in the filename (lowercase-kebab .md)"
    UNDERSCORE = "underscore in the filename (separate words with a hyphen)"
    BANNED = "a file the standard bans from docs/"
    NOT_MARKDOWN = "prose that is not markdown, so it is not rendered"
    WORKING = "working artefacts, not published"
    SYMLINK = "a symbolic link, not published"
    LINK = "a link that leaves the repository"
    NO_PIN = "no pin in this stack"
    REFUSED = "an anonymous fetch was refused"
    NO_DOCS = "no docs/ at the pinned ref"
    NO_SPEC = "no committed OpenAPI spec at the pinned ref"


LEFT_OUT = frozenset({Rule.NO_PIN, Rule.REFUSED, Rule.NO_DOCS})


class FetchError(RuntimeError):
    """A git command failed for a reason other than the repository being private."""


class RefusedError(FetchError):
    """An anonymous fetch was refused: the repository is private, renamed or gone."""


@dataclass(frozen=True, slots=True)
class Member:
    """One suite member, in sidebar order.

    Attributes:
        name: The suite.yaml node name.
        repo: The GitHub owner/name it lives at.
        lane: The first lane that lists it.
        position: Its sidebar position.
        classification: The node's declared classification, such as product or fork.
    """

    name: str
    repo: str
    lane: str
    position: int
    classification: str = ""


@dataclass(frozen=True, slots=True)
class Build:
    """One static build of the site: a released stack, or the unlisted preview.

    Attributes:
        name: The stack version, or ``next``.
        base_url: Where the build is served.
        listed: Whether the version dropdown shows it.
        refs: The ref each member is fetched at. A member absent here has no pin.
    """

    name: str
    base_url: str
    listed: bool
    refs: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Checkout:
    """A sparse checkout of one member at one ref.

    Attributes:
        path: The checkout's root.
        commit: The commit the ref resolved to.
    """

    path: Path
    commit: str


@dataclass(frozen=True, slots=True)
class Finding:
    """One stray: a file outside the standard, or a member left out.

    Attributes:
        member: The member it belongs to.
        path: The repo path it concerns.
        rule: What is wrong with it.
        detail: The link target or other specifics, when there are any.
    """

    member: str
    path: str
    rule: Rule
    detail: str = ""


@dataclass(slots=True)
class BuildResult:
    """What one build assembled, for site.json and the stray report.

    Attributes:
        build: The build assembled.
        members: One site.json entry per member included.
        findings: Every stray and every member left out.
        openapi: The OpenAPI paths for docusaurus.config.js, when the engine has a spec.
    """

    build: Build
    members: list[dict[str, str]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    openapi: dict[str, str] | None = None


def _run(cmd: list[str], env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a command and capture its text output as UTF-8, never raising on a bad byte."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )


def anonymous_git_env() -> dict[str, str]:
    """An environment in which git can reach no credential and prompt for none.

    Returns:
        The current environment less every GIT_* variable, with no global or system
        config (so no credential helper or URL rewrite) and terminal prompts off.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


class Fetcher:
    """Anonymous, shallow, sparse checkouts of member repositories, one per repo and ref."""

    def __init__(self, work: Path, git_base: str = GITHUB) -> None:
        """Fetch into `work`, from repositories under `git_base`.

        Args:
            work: The directory the checkouts are made in. A checkout already there,
                at a ref other than HEAD, is reused.
            git_base: The URL the owner/name repo paths are appended to.
        """
        self._work = work
        self._base = git_base.rstrip("/")
        self._env = anonymous_git_env()
        self._done: dict[tuple[str, str], Checkout] = {}

    def fetch(self, repo: str, ref: str) -> Checkout:
        """Check `repo` out at `ref`, docs/ and the OpenAPI spec only.

        Args:
            repo: The owner/name to fetch.
            ref: A tag, a commit or HEAD.

        Returns:
            The checkout.

        Raises:
            RefusedError: The anonymous fetch was refused.
            FetchError: Any other git failure, a missing ref among them.
        """
        key = (repo, ref)
        if key in self._done:
            return self._done[key]
        dest = self._work / f"{repo.replace('/', '__')}@{ref}"
        marker = dest / ".git" / "dfe-docs-commit"
        if ref != HEAD and marker.is_file():
            checkout = Checkout(dest, marker.read_text(encoding="utf-8").strip())
        else:
            if dest.exists():
                shutil.rmtree(dest)
            checkout = self._checkout(repo, ref, dest)
            marker.write_text(checkout.commit + "\n", encoding="utf-8", newline="\n")
        self._done[key] = checkout
        return checkout

    def _checkout(self, repo: str, ref: str, dest: Path) -> Checkout:
        url = f"{self._base}/{repo}.git"
        self._git("init", "-q", str(dest))
        self._git("-C", str(dest), "remote", "add", "origin", url)
        sparse = dest / ".git" / "info" / "sparse-checkout"
        sparse.parent.mkdir(parents=True, exist_ok=True)
        sparse.write_text("\n".join(SPARSE_PATTERNS) + "\n", encoding="utf-8", newline="\n")
        self._git("-C", str(dest), "config", "core.sparseCheckout", "true")
        self._git(
            "-C", str(dest), "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", ref
        )
        self._git("-C", str(dest), "checkout", "-q", "FETCH_HEAD")
        commit = self._git("-C", str(dest), "rev-parse", "FETCH_HEAD^{commit}").strip()
        return Checkout(dest, commit)

    def _git(self, *args: str) -> str:
        done = _run(["git", *args], env=self._env)
        if done.returncode == 0:
            return done.stdout
        message = f"git {' '.join(args)} failed ({done.returncode}): {done.stderr.strip()}"
        if any(sign in done.stderr for sign in REFUSED):
            raise RefusedError(message)
        raise FetchError(message)


def released_stacks(tree: Mapping[str, object]) -> list[str]:
    """The released stacks in a parsed versions.yaml, newest first.

    Args:
        tree: versions.yaml as `yaml_subset.parse` returns it.

    Returns:
        Every stack at `maturity: release`, not withdrawn, named X.Y.Z.
    """
    stacks = tree.get("stacks")
    if not isinstance(stacks, dict):
        return []
    found: list[tuple[tuple[int, ...], str]] = []
    for name, block in stacks.items():
        match = _RELEASE_NAME.fullmatch(name)
        if not match or not isinstance(block, dict):
            continue
        if block.get("maturity") == RELEASE and block.get("support") != WITHDRAWN:
            found.append((tuple(int(part) for part in match.groups()), name))
    return [name for _, name in sorted(found, reverse=True)]


def load_suite(dfe_stack: Path = DFE_STACK) -> dict:
    """Read suite.yaml's whole graph through `dfe-stack suite`, which validates it first.

    Args:
        dfe_stack: The dfe-stack CLI.

    Returns:
        The graph as JSON: nodes, edges and lanes.

    Raises:
        SystemExit: dfe-stack refused the graph.
    """
    done = _run([sys.executable, str(dfe_stack), "suite"])
    if done.returncode != 0:
        raise SystemExit(f"error: dfe-stack suite failed:\n{done.stderr.strip()}")
    return json.loads(done.stdout)


def ordered_members(graph: Mapping[str, object]) -> list[Member]:
    """Every member, in the order the lanes first list it.

    Args:
        graph: The suite graph `load_suite` returns.

    Returns:
        One Member per node a lane names, positioned from FIRST_MEMBER_POSITION.
    """
    nodes = graph.get("nodes", {})
    first_lane: dict[str, str] = {}
    for lane in graph.get("lanes", []):
        for name in lane.get("members", []):
            first_lane.setdefault(name, lane["name"])
    return [
        Member(
            name,
            nodes[name]["repo"],
            lane,
            FIRST_MEMBER_POSITION + index,
            nodes[name].get("classification", ""),
        )
        for index, (name, lane) in enumerate(first_lane.items())
    ]


def stack_refs(
    stack: str, block: Mapping[str, object], members: Iterable[Member]
) -> dict[str, str]:
    """The ref one stack pins for each member it pins.

    Args:
        stack: The stack version, which is also dfe-infra's tag.
        block: The stack's block from versions.yaml.
        members: The suite members.

    Returns:
        member name -> ref, for each member the stack pins.
    """
    refs: dict[str, str] = {}
    for member in members:
        if member.name == STACK_REPO_MEMBER:
            refs[member.name] = stack
            continue
        for section in PIN_SECTIONS:
            pins = block.get(section)
            ref = pins.get(member.name) if isinstance(pins, dict) else None
            if isinstance(ref, str) and ref:
                refs[member.name] = ref
                break
    return refs


def relink(target: str, *, doc_dir: str, repo: str, ref: str) -> tuple[str, bool]:
    """Point one link target that leaves docs/ at the file on GitHub, at `ref`.

    Args:
        target: The link target as written, optionally in angle brackets.
        doc_dir: The repo path of the directory holding the linking file.
        repo: The member's owner/name.
        ref: The tag or commit the file was fetched at.

    Returns:
        The target to write, and whether it escaped the repository (left unchanged).
    """
    bracketed = target.startswith("<") and target.endswith(">")
    raw = target[1:-1] if bracketed else target
    if not raw or raw.startswith(("#", "//")) or _SCHEME.match(raw):
        return target, False
    path, hash_mark, fragment = raw.partition("#")
    path, query_mark, query = path.partition("?")
    if not path:
        return target, False
    absolute = path.startswith("/")
    joined = path.lstrip("/") if absolute else posixpath.join(doc_dir, path)
    resolved = posixpath.normpath(joined)
    if resolved == ".." or resolved.startswith("../"):
        return target, True
    suffix = f"{query_mark}{query}{hash_mark}{fragment}"
    parts = resolved.split("/")
    published = parts[0] == "docs" and not WORKING_DIRS.intersection(parts[1:])
    if published:
        if not absolute:
            return target, False
        local = posixpath.relpath(resolved, doc_dir) + ("/" if path.endswith("/") else "")
        new = local + suffix
    elif resolved == ".":
        new = f"{GITHUB}/{repo}/tree/{ref}{suffix}"
    elif resolved.lower().endswith(IMAGES):
        new = f"{RAW_GITHUB}/{repo}/{ref}/{resolved}{suffix}"
    else:
        kind = "tree" if path.endswith("/") else "blob"
        new = f"{GITHUB}/{repo}/{kind}/{ref}/{resolved}{suffix}"
    return (f"<{new}>" if bracketed else new), False


def rewrite_links(text: str, *, doc_path: str, repo: str, ref: str) -> tuple[str, list[str]]:
    """Rewrite every link in a markdown file that leaves docs/, outside code.

    Inline links and images, reference definitions and HTML href/src attributes are
    read. Fenced code blocks and inline code spans are left as written.

    Args:
        text: The file's markdown.
        doc_path: The file's repo path, such as ``docs/deployment/aws.md``.
        repo: The member's owner/name.
        ref: The tag or commit the file was fetched at.

    Returns:
        The rewritten markdown, and each target that escaped the repository.
    """
    doc_dir = posixpath.dirname(doc_path)
    escaped: list[str] = []

    def fix(match: re.Match[str]) -> str:
        new, escapes = relink(match.group(2), doc_dir=doc_dir, repo=repo, ref=ref)
        if escapes:
            escaped.append(match.group(2))
        closing = match.group(3) if match.re is _HTML_TARGET else ""
        return match.group(1) + new + closing

    def prose(segment: str, at_line_start: bool) -> str:
        if at_line_start:
            segment = _REF_DEFINITION.sub(fix, segment)
        segment = _INLINE_TARGET.sub(fix, segment)
        return _HTML_TARGET.sub(fix, segment)

    return map_outside_code(text, prose), escaped


def map_outside_code(text: str, prose: Callable[[str, bool], str]) -> str:
    """Apply `prose` to every stretch of markdown outside fenced blocks and code spans.

    Args:
        text: The markdown.
        prose: Called with each stretch and whether it starts its line, returning the
            stretch to write.

    Returns:
        The markdown, code left exactly as written.
    """
    out: list[str] = []
    fence = ""
    for line in text.splitlines(keepends=True):
        opener = _FENCE.match(line)
        if fence:
            closes = (
                opener is not None
                and opener.group(1)[0] == fence[0]
                and len(opener.group(1)) >= len(fence)
                and not line[opener.end() :].strip()
            )
            fence = "" if closes else fence
            out.append(line)
            continue
        if opener:
            fence = opener.group(1)
            out.append(line)
            continue
        pieces: list[str] = []
        last = 0
        for span in _CODE_SPAN.finditer(line):
            pieces.append(prose(line[last : span.start()], last == 0))
            pieces.append(span.group(0))
            last = span.end()
        pieces.append(prose(line[last:], last == 0))
        out.append("".join(pieces))
    return "".join(out)


def mdx_safe(text: str) -> str:
    """Replace the characters MDX reads as code with character references, outside code.

    MDX and CommonMark both decode a character reference to the literal character, and
    a YAML front-matter string carries one as plain text, where a backslash escape is
    invalid.

    Args:
        text: Markdown prose, such as an OpenAPI description.

    Returns:
        The prose with each unescaped ``{`` and ``}``, and each ``<`` that would open a
        JSX tag, replaced.
    """
    return map_outside_code(
        text, lambda segment, _: _MDX_SPECIAL.sub(lambda m: _MDX_REFERENCE[m[1][0]], segment)
    )


def mdx_safe_spec(node: object) -> object:
    """An OpenAPI document with every `description` made safe to place in MDX.

    gen-api-docs writes descriptions into MDX pages as written, where ``{name}`` is
    evaluated as JavaScript and fails the build.

    Args:
        node: The parsed spec, or any part of it.

    Returns:
        A copy with each string `description` passed through `mdx_safe`.
    """
    if isinstance(node, dict):
        return {
            key: mdx_safe(value)
            if key == "description" and isinstance(value, str)
            else mdx_safe_spec(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [mdx_safe_spec(value) for value in node]
    return node


def find_strays(docs: Path, member: str) -> list[Finding]:
    """Every file and directory under one member's docs/ that sits outside the standard.

    Args:
        docs: The member's docs/ directory.
        member: The member's name.

    Returns:
        The findings, in path order.
    """
    findings: list[Finding] = []
    directories = [docs, *sorted(path for path in docs.rglob("*") if path.is_dir())]
    for directory in directories:
        relative = directory.relative_to(docs)
        repo_dir = "docs" if not relative.parts else f"docs/{relative.as_posix()}"
        if WORKING_DIRS.intersection(relative.parts):
            if relative.name in WORKING_DIRS:
                findings.append(Finding(member, f"{repo_dir}/", Rule.WORKING))
            continue
        if any(part.startswith(".") for part in relative.parts) or directory.is_symlink():
            continue
        names = {path.name for path in directory.iterdir() if path.is_file()}
        prose = {name for name in names if name.lower().endswith(MARKDOWN)}
        if "index.md" in names and LANDING not in names:
            findings.append(Finding(member, f"{repo_dir}/index.md", Rule.INDEX_LANDING))
        elif prose and LANDING not in names:
            findings.append(Finding(member, f"{repo_dir}/", Rule.LANDING))
        for path in sorted(directory.iterdir()):
            findings += _file_strays(path, f"{repo_dir}/{path.name}", member)
    return findings


def _file_strays(path: Path, repo_path: str, member: str) -> list[Finding]:
    """The standard's naming rules, applied to one entry of a docs/ directory."""
    if path.is_symlink():
        return [Finding(member, repo_path, Rule.SYMLINK)]
    if not path.is_file() or path.name.startswith("."):
        return []
    lowered = path.name.lower()
    if lowered.endswith(NOT_RENDERED):
        return [Finding(member, repo_path, Rule.NOT_MARKDOWN)]
    if not lowered.endswith(MARKDOWN) or path.name == LANDING:
        return []
    if lowered in BANNED_NAMES:
        return [Finding(member, repo_path, Rule.BANNED)]
    found = []
    if path.stem != path.stem.lower():
        found.append(Finding(member, repo_path, Rule.CASE))
    if "_" in path.stem:
        found.append(Finding(member, repo_path, Rule.UNDERSCORE))
    return found


def copy_docs(docs: Path, dest: Path, *, member: Member, ref: str) -> list[Finding]:
    """Copy one member's docs/ into the build, rewriting links in its markdown.

    Working-artefact directories, dotfiles and symbolic links are not copied: a link
    could name any file on the build host.

    Args:
        docs: The member's docs/ directory.
        dest: Where the member's pages go in the build.
        member: The member.
        ref: The tag or commit the docs were fetched at, for the GitHub links.

    Returns:
        A LINK finding for each link that escaped the repository.
    """
    findings: list[Finding] = []
    # rglob does not descend into a linked directory, so the entry itself is the only check.
    for path in sorted(docs.rglob("*")):
        relative = path.relative_to(docs)
        hidden = any(part.startswith(".") for part in relative.parts)
        if path.is_symlink() or hidden or WORKING_DIRS.intersection(relative.parts[:-1]):
            continue
        if not path.is_file():
            continue
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not path.name.lower().endswith(MARKDOWN):
            shutil.copyfile(path, target)
            continue
        repo_path = f"docs/{relative.as_posix()}"
        text = drop_leading_comments(path.read_text(encoding="utf-8", errors="replace"))
        rewritten, escaped = rewrite_links(text, doc_path=repo_path, repo=member.repo, ref=ref)
        target.write_text(rewritten, encoding="utf-8", newline="\n")
        findings += [Finding(member.name, repo_path, Rule.LINK, link) for link in escaped]
    return findings


def drop_leading_comments(text: str) -> str:
    """Remove the HTML comments that open a page, after any front matter.

    Docusaurus titles a page from its H1 only when the H1 comes first, and a comment
    renders nothing, so a file-header comment would otherwise leave the page titled by
    its filename.

    Args:
        text: The page's markdown.

    Returns:
        The markdown with its leading comments gone and everything else untouched.
    """
    front = _FRONT_MATTER.match(text)
    head = front.group(0) if front else ""
    body = text[len(head) :]
    while comment := _LEADING_COMMENT.match(body):
        body = body[comment.end() :]
    return head + body


def write_json(path: Path, data: object) -> None:
    """Write `data` as indented JSON with a trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def category(member: Member, *, ref: str, has_landing: bool) -> dict[str, object]:
    """The `_category_.json` for one member's sidebar entry.

    Args:
        member: The member.
        ref: The ref its docs came from.
        has_landing: Whether its docs/ carries a README.md, which then becomes the
            category's page.

    Returns:
        The category, with a generated index page when there is no landing page.
    """
    data: dict[str, object] = {
        "label": member.name,
        "position": member.position,
        "collapsed": True,
        "customProps": {"lane": member.lane, "ref": ref},
    }
    if not has_landing:
        data["link"] = {
            "type": "generated-index",
            "description": f"The {member.name} documentation at {ref}.",
        }
    return data


def assemble(build: Build, members: Iterable[Member], fetcher: Fetcher, out: Path) -> BuildResult:
    """Assemble one build's content tree under `out`.

    Args:
        build: The build.
        members: The suite members, in sidebar order.
        fetcher: Where the checkouts come from.
        out: The content root. The build lands in ``out/<build name>``.

    Returns:
        What was assembled.

    Raises:
        SystemExit: A member's docs/ collides with the generated API directory or with
            the root pages.
    """
    members = list(members)
    result = BuildResult(build)
    root = out / build.name
    root.mkdir(parents=True)
    for member in members:
        ref = build.refs.get(member.name)
        if ref is None:
            result.findings.append(Finding(member.name, "", Rule.NO_PIN))
            continue
        try:
            checkout = fetcher.fetch(member.repo, ref)
        except RefusedError:
            result.findings.append(Finding(member.name, "", Rule.REFUSED, f"{member.repo}@{ref}"))
            continue
        docs = checkout.path / "docs"
        if docs.is_symlink():
            result.findings.append(Finding(member.name, "docs", Rule.SYMLINK))
            continue
        if not docs.is_dir():
            result.findings.append(Finding(member.name, "docs/", Rule.NO_DOCS, ref))
            continue
        link_ref = checkout.commit if ref == HEAD else ref
        dest = root / member.name
        if member.classification not in UPSTREAM_LED:
            result.findings += find_strays(docs, member.name)
        result.findings += copy_docs(docs, dest, member=member, ref=link_ref)
        has_landing = (docs / LANDING).is_file()
        write_json(dest / CATEGORY_FILE, category(member, ref=link_ref, has_landing=has_landing))
        result.members.append(
            {"name": member.name, "repo": member.repo, "ref": link_ref, "commit": checkout.commit}
        )
        if member.name == SPEC_MEMBER:
            result.openapi = _place_spec(checkout, build, dest, out, result.findings)
    _copy_root(root, {member.name for member in members})
    return result


def _place_spec(
    checkout: Checkout, build: Build, dest: Path, out: Path, findings: list[Finding]
) -> dict[str, str] | None:
    """Copy the engine's spec beside the builds and mark the directory its pages go in."""
    spec = checkout.path / SPEC_FILE
    if not spec.is_file():
        findings.append(Finding(SPEC_MEMBER, SPEC_FILE, Rule.NO_SPEC))
        return None
    api = dest / API_DIR
    if api.exists():
        raise SystemExit(f"error: {SPEC_MEMBER}'s docs/ carries {API_DIR}/, the API pages' home")
    placed = out / "_specs" / build.name / f"{SPEC_MEMBER}.json"
    document = json.loads(spec.read_text(encoding="utf-8"))
    write_json(placed, mdx_safe_spec(document))
    slice_path = f"{SPEC_MEMBER}/{API_DIR}/sidebar.ts"
    # The sidebar generator swaps this category's items for the slice gen-api-docs writes.
    write_json(
        api / CATEGORY_FILE,
        {"label": API_LABEL, "position": 10_000, "customProps": {"openapiSlice": slice_path}},
    )
    return {
        "spec": placed.relative_to(out).as_posix(),
        "output": api.relative_to(out).as_posix(),
        "slice": slice_path,
    }


def _copy_root(root: Path, member_names: set[str]) -> None:
    """Copy the suite's own pages to the top of a build, refusing to overwrite a member."""
    if not CONTENT_ROOT.is_dir():
        raise SystemExit(f"error: {CONTENT_ROOT} is missing")
    clash = sorted({path.name for path in CONTENT_ROOT.iterdir()} & member_names)
    if clash:
        raise SystemExit(f"error: content-root/ entries collide with members: {', '.join(clash)}")
    shutil.copytree(CONTENT_ROOT, root, dirs_exist_ok=True)


def stray_report(result: BuildResult) -> str:
    """Render one build's findings as a markdown warning list.

    Args:
        result: The assembled build.

    Returns:
        The report: a per-member summary table, then every finding.
    """
    by_member: dict[str, list[Finding]] = {}
    for finding in result.findings:
        by_member.setdefault(finding.member, []).append(finding)
    lines = [
        f"# Stray report: {result.build.name}",
        "",
        "Each member's docs/ checked against the documentation standard. Warnings only.",
        "",
        "| member | findings | most common |",
        "|---|---|---|",
    ]
    for member, findings in by_member.items():
        top_rule, top_count = Counter(finding.rule for finding in findings).most_common(1)[0]
        lines.append(f"| {member} | {len(findings)} | {top_rule} ({top_count}) |")
    for member, findings in by_member.items():
        lines += ["", f"## {member}", ""]
        for finding in findings:
            where = f"`{finding.path}`: " if finding.path else ""
            detail = f" (`{finding.detail}`)" if finding.detail else ""
            lines.append(f"- {where}{finding.rule}{detail}")
    return "\n".join(lines) + "\n"


def site_manifest(results: Iterable[BuildResult]) -> dict[str, object]:
    """The site.json docusaurus.config.js and build_site.py read.

    Args:
        results: The assembled builds, listed ones newest first.

    Returns:
        The newest listed build's name (None without one) and every build.
    """
    builds = [
        {
            "name": result.build.name,
            "base_url": result.build.base_url,
            "listed": result.build.listed,
            "members": result.members,
            "openapi": result.openapi,
        }
        for result in results
    ]
    newest = next((build["name"] for build in builds if build["listed"]), None)
    return {"newest": newest, "builds": builds}


def prepare_out(out: Path) -> None:
    """Empty the content root, refusing a non-empty directory this script did not write.

    Args:
        out: The content root.

    Raises:
        SystemExit: `out` holds files but no site.json.
    """
    if out.exists():
        if any(out.iterdir()) and not (out / SITE_FILE).is_file():
            raise SystemExit(f"error: refusing to clear {out}: it holds no {SITE_FILE}")
        shutil.rmtree(out)
    out.mkdir(parents=True)


def plan_builds(
    tree: Mapping[str, object], members: list[Member], stacks: list[str], with_next: bool
) -> list[Build]:
    """The builds to assemble: the chosen released stacks newest first, then /next/.

    Args:
        tree: The parsed versions.yaml.
        members: The suite members.
        stacks: Released stacks to build. Empty means every one.
        with_next: Whether to add the unlisted preview from each default branch.

    Returns:
        The builds.

    Raises:
        SystemExit: A named stack is not a released one, or there is nothing to build.
    """
    released = released_stacks(tree)
    unknown = sorted(set(stacks) - set(released))
    if unknown:
        have = ", ".join(released) or "none"
        raise SystemExit(f"error: not a released stack: {', '.join(unknown)} (released: {have})")
    chosen = [name for name in released if not stacks or name in stacks]
    blocks = tree["stacks"]
    builds = [
        Build(name, f"/v/{name}/", True, stack_refs(name, blocks[name], members)) for name in chosen
    ]
    if with_next:
        heads = {member.name: HEAD for member in members}
        builds.append(Build(NEXT, f"/{NEXT}/", False, heads))
    if not builds:
        raise SystemExit("error: versions.yaml holds no released stack, and --next was not given")
    return builds


def summarise(result: BuildResult, emit: Callable[[str], None] = print) -> None:
    """Print one build's member count, who was left out, and its strays per member.

    Args:
        result: The assembled build.
        emit: Where each line goes.
    """
    left_out = [finding for finding in result.findings if finding.rule in LEFT_OUT]
    emit(f"{result.build.name}: {len(result.members)} members included, {len(left_out)} left out")
    for finding in left_out:
        emit(f"  left out {finding.member}: {finding.rule}")
    strays = Counter(f.member for f in result.findings if f.rule not in LEFT_OUT)
    for member, count in strays.most_common():
        emit(f"  warning: {member}: {count} stray finding(s)")


def main(argv: list[str] | None = None) -> int:
    """Assemble the content tree for every requested build.

    Args:
        argv: The command-line arguments, less the program name.

    Returns:
        The exit status.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--stack",
        action="append",
        default=[],
        help="a released stack to build (repeatable, default every one)",
    )
    parser.add_argument(
        "--next",
        action="store_true",
        help="also build the unlisted /next/ preview from each default branch",
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="the content root (default docs-site/content)"
    )
    parser.add_argument(
        "--work-dir", type=Path, help="keep the member checkouts here and reuse them next run"
    )
    parser.add_argument(
        "--git-base",
        default=GITHUB,
        help="the URL member repos are fetched under, such as a mirror",
    )
    args = parser.parse_args(argv)

    text = VERSIONS_FILE.read_text(encoding="utf-8")
    tree = yaml_subset.parse(text, source=str(VERSIONS_FILE))
    members = ordered_members(load_suite())
    builds = plan_builds(tree, members, args.stack, args.next)
    prepare_out(args.out)
    with tempfile.TemporaryDirectory(prefix="dfe-docs-") as scratch:
        work = args.work_dir or Path(scratch)
        work.mkdir(parents=True, exist_ok=True)
        fetcher = Fetcher(work, args.git_base)
        try:
            results = [assemble(build, members, fetcher, args.out) for build in builds]
        except FetchError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    for result in results:
        report = args.out / "_reports" / f"strays-{result.build.name}.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(stray_report(result), encoding="utf-8", newline="\n")
        summarise(result)
    write_json(args.out / SITE_FILE, site_manifest(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
