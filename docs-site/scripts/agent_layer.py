#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         docs-site/scripts/agent_layer.py
#  Purpose:      Finish the files an AI agent reads beside one built version of
#                the docs site, after docusaurus-plugin-llms has written them.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""agent_layer -- finish one build's markdown side for AI agents.

docusaurus-plugin-llms writes llms.txt, a <route>.md copy of every page and the
llms/<section>.txt bundles. It leaves each copy's links as the source wrote them,
relative to a file the copy no longer sits beside. build_site.py calls `finish`
after each build, which:

    points each relative link outside code that resolves to a page at that page's
    .md copy, and one that resolves to a member's other file at GitHub
    adds <link rel="alternate" type="text/markdown"> to each page's HTML
    lists the section bundles at the end of llms.txt
    copies the engine's OpenAPI spec, as the engine publishes it, to openapi/
    writes the site-root llms.txt for the newest release, naming /latest/ URLs
    fails the build on a link to a missing copy, or a task in agent-tasks.yaml
    that the build's llms.txt and copies cannot answer

The page manifest the docs plugin writes ties each copy to its source. Every
source sits under content/<build>/, which aggregate.py fills from each member's
published docs/ only, and a page from anywhere else stops the build.
"""

import json
import posixpath
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import aggregate
import yaml_subset

SITE_ROOT = Path(__file__).resolve().parents[1]
CONTENT = SITE_ROOT / "content"
BUILD = SITE_ROOT / "build"
LATEST = "/latest/"
OPENAPI_FILE = "openapi/dfe-engine.json"
BUNDLE_DIR = "llms"
LLMS = "llms.txt"
TASKS_FILE = SITE_ROOT / "agent-tasks.yaml"
_SITE_LINK = re.compile(r"\]\((/[^)#\s]+\.md)")
_HEAD_END = "</head>"


@dataclass(frozen=True, slots=True)
class Page:
    """One page the docs plugin built.

    Attributes:
        title: Its title.
        source: Its file, relative to docs-site/.
        permalink: Its route, base path included.
        description: Its meta description.
    """

    title: str
    source: str
    permalink: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class BuildLinks:
    """What a link rewrite needs to know about one build.

    Attributes:
        name: The build's name, its directory under content/.
        base_url: Its base path, such as ``/v/2.2.1/``.
        members: Each member's path in the build -> (owner/name, ref).
    """

    name: str
    base_url: str
    members: Mapping[str, tuple[str, str]]

    def markdown_url(self, permalink: str) -> str:
        """The site path of a page's markdown copy, as docusaurus-plugin-llms names it.

        Args:
            permalink: The page's route, such as ``/v/2.2.1/dfe-engine/architecture/``.

        Returns:
            ``/v/2.2.1/dfe-engine/architecture.md``, or ``/v/2.2.1/index.md`` for the
            build's own root.
        """
        if permalink.rstrip("/") == self.base_url.rstrip("/"):
            return f"{self.base_url}index.md"
        return permalink.rstrip("/") + ".md"

    def on_github(self, resolved: str) -> str | None:
        """The GitHub URL of a member's file that is not a page.

        Args:
            resolved: The file's path under content/<build>/.

        Returns:
            Its raw URL for an image, its blob URL otherwise, or None when no member
            holds it.
        """
        for path, (repo, ref) in sorted(
            self.members.items(), key=lambda item: -len(item[0])
        ):
            if resolved != path and not resolved.startswith(f"{path}/"):
                continue
            inside = resolved[len(path) :].lstrip("/")
            if resolved.lower().endswith(aggregate.IMAGES):
                return f"{aggregate.RAW_GITHUB}/{repo}/{ref}/docs/{inside}"
            return f"{aggregate.GITHUB}/{repo}/blob/{ref}/docs/{inside}"
        return None


def load_pages(entry: Mapping[str, object], content: Path = CONTENT) -> list[Page]:
    """The pages one build published, less the generated API pages.

    Args:
        entry: The build's site.json entry.
        content: The content root aggregate.py wrote.

    Returns:
        Every prose page, in route order.

    Raises:
        SystemExit: The build wrote no manifest, or a page's source is not in the
            build's content tree.
    """
    manifest = content / "_manifests" / f"{entry['name']}.json"
    if not manifest.is_file():
        raise SystemExit(
            f"error: {manifest} is missing: the page-manifest plugin did not run"
        )
    tree = f"content/{entry['name']}/"
    openapi = entry.get("openapi") or {}
    api = f"content/{openapi['output']}/" if openapi else None
    pages = []
    for raw in json.loads(manifest.read_text(encoding="utf-8")):
        source = raw["source"]
        if not source.startswith(tree):
            raise SystemExit(
                f"error: {raw['permalink']} comes from {source}, outside {tree}"
            )
        if api and source.startswith(api):
            continue
        pages.append(
            Page(raw["title"], source, raw["permalink"], raw.get("description") or "")
        )
    return sorted(pages, key=lambda page: page.permalink)


def rewrite_links(
    text: str, page: Page, by_source: Mapping[str, Page], build: BuildLinks
) -> str:
    """Point each relative link at another page's markdown copy, or at the file on GitHub.

    Args:
        text: The page's markdown, its links as the source wrote them.
        page: The page it came from.
        by_source: Every page, keyed by source path.
        build: The build the page is in.

    Returns:
        The markdown, links outside code rewritten. A link that resolves to neither
        is left as written.
    """
    tree = f"content/{build.name}/"
    page_dir = posixpath.dirname(page.source)

    def fix(match: re.Match[str]) -> str:
        target = match.group(2)
        bracketed = target.startswith("<") and target.endswith(">")
        raw = target[1:-1] if bracketed else target
        if not raw or raw.startswith(("#", "/")) or aggregate._SCHEME.match(raw):
            return match.group(0)
        path, hash_mark, fragment = raw.partition("#")
        path = path.partition("?")[0]
        resolved = posixpath.normpath(posixpath.join(page_dir, path))
        candidates = [resolved]
        if path.endswith("/") or not posixpath.splitext(path)[1]:
            candidates += [
                f"{resolved}/README.md",
                f"{resolved}/index.md",
                f"{resolved}.md",
            ]
        found = next((by_source[c] for c in candidates if c in by_source), None)
        anchor = f"#{fragment}" if hash_mark else ""
        if found:
            new = build.markdown_url(found.permalink) + anchor
        elif resolved.startswith(tree) and (
            github := build.on_github(resolved[len(tree) :])
        ):
            new = github + anchor
        else:
            return match.group(0)
        closing = match.group(3) if match.re is aggregate._HTML_TARGET else ""
        return match.group(1) + (f"<{new}>" if bracketed else new) + closing

    def prose(segment: str, at_line_start: bool) -> str:
        if at_line_start:
            segment = aggregate._REF_DEFINITION.sub(fix, segment)
        segment = aggregate._INLINE_TARGET.sub(fix, segment)
        return aggregate._HTML_TARGET.sub(fix, segment)

    return aggregate.map_outside_code(text, prose)


def add_alternate(html: Path, href: str) -> None:
    """Link a page's built HTML to its markdown copy, once."""
    text = html.read_text(encoding="utf-8")
    tag = f'<link rel="alternate" type="text/markdown" href="{href}">'
    if tag not in text and _HEAD_END in text:
        html.write_text(text.replace(_HEAD_END, tag + _HEAD_END, 1), encoding="utf-8")


def dead_links(root: Path, build_dir: Path) -> list[str]:
    """Every link in a build's markdown copies to a .md copy that does not exist.

    Args:
        root: The build's directory.
        build_dir: The merged build directory, where a site path resolves.

    Returns:
        ``<copy>: <link>`` for each one.
    """
    dead = []
    for copy in sorted(root.rglob("*.md")):
        for link in _SITE_LINK.findall(copy.read_text(encoding="utf-8")):
            if not (build_dir / link.lstrip("/")).is_file():
                dead.append(f"{copy.relative_to(build_dir)}: {link}")
    return dead


def failed_tasks(root: Path, tasks: Mapping[str, object]) -> list[str]:
    """The agent tasks a build cannot answer.

    A task passes when its page is named in the build's llms.txt and the page holds
    the expected text.

    Args:
        root: The build's directory.
        tasks: agent-tasks.yaml's `tasks`, name -> {page, expect}.

    Returns:
        One line per failed task, saying what was missing.
    """
    index = (root / LLMS).read_text(encoding="utf-8")
    failed = []
    for name, task in tasks.items():
        page, expect = str(task["page"]), str(task["expect"])
        target = root / page
        if f"/{page})" not in index and f"/{page} " not in index:
            failed.append(f"{name}: {page} is not in {LLMS}")
        elif not target.is_file():
            failed.append(f"{name}: {page} was not built")
        elif expect not in target.read_text(encoding="utf-8"):
            failed.append(f"{name}: {page} does not hold {expect!r}")
    return failed


def load_tasks(path: Path = TASKS_FILE) -> Mapping[str, object]:
    """agent-tasks.yaml's tasks, name -> {page, expect}."""
    tree = yaml_subset.parse(path.read_text(encoding="utf-8"), source=str(path))
    tasks = tree.get("tasks")
    if not isinstance(tasks, dict) or not all(
        isinstance(task, dict) and {"page", "expect"} <= task.keys()
        for task in tasks.values()
    ):
        raise SystemExit(f"error: {path.name}: every task needs a page and an expect")
    return tasks


def finish(
    entry: Mapping[str, object],
    site: Mapping[str, object],
    *,
    content: Path = CONTENT,
    build_dir: Path = BUILD,
) -> int:
    """Finish one build's agent files.

    Args:
        entry: The build's site.json entry.
        site: The whole site.json.
        content: The content root aggregate.py wrote.
        build_dir: The merged build directory.

    Returns:
        The number of markdown copies finished.

    Raises:
        SystemExit: A page has no markdown copy or no built HTML.
    """
    base_url = str(entry["base_url"])
    url = str(site["url"]).rstrip("/")
    members = {
        str(m["path"]): (str(m["repo"]), str(m["ref"]))
        for m in entry.get("members", [])
        if "path" in m
    }
    build = BuildLinks(str(entry["name"]), base_url, members)
    pages = load_pages(entry, content)
    by_source = {page.source: page for page in pages}
    missing = []
    for page in pages:
        md = build.markdown_url(page.permalink)
        copy = build_dir / md.lstrip("/")
        html = build_dir / page.permalink.lstrip("/") / "index.html"
        if not copy.is_file() or not html.is_file():
            missing.append(page.permalink)
            continue
        text = copy.read_text(encoding="utf-8")
        copy.write_text(
            rewrite_links(text, page, by_source, build), encoding="utf-8", newline="\n"
        )
        add_alternate(html, md)
    if missing:
        raise SystemExit(
            f"error: {len(missing)} page(s) with no .md copy or HTML: {missing[:5]}"
        )

    root = build_dir / base_url.strip("/")
    openapi = entry.get("openapi") or {}
    tasks = load_tasks()
    if openapi.get("raw"):
        spec = root / OPENAPI_FILE
        spec.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(content / str(openapi["raw"]), spec)
    elif entry.get("listed"):
        raise SystemExit(
            f"error: {entry['name']}: no OpenAPI spec, so no API reference"
        )
    else:
        # The preview follows each member's default branch, which can lack a spec.
        tasks = {
            name: task for name, task in tasks.items() if task["page"] != OPENAPI_FILE
        }
    bundles = sorted((root / BUNDLE_DIR).glob("*.txt"))
    index = (root / LLMS).read_text(encoding="utf-8").rstrip("\n")
    lines = ["", "", "## Section bundles", ""]
    lines += [
        f"- [{b.stem}]({url}{base_url}{BUNDLE_DIR}/{b.name}): every page in one file"
        for b in bundles
    ]
    index += "\n".join(lines) + "\n"
    (root / LLMS).write_text(index, encoding="utf-8", newline="\n")
    if entry["name"] == site.get("newest"):
        latest = index.replace(f"{url}{base_url}", f"{url}{LATEST}")
        (build_dir / LLMS).write_text(latest, encoding="utf-8", newline="\n")
    problems = dead_links(root, build_dir) + failed_tasks(root, tasks)
    if problems:
        raise SystemExit(
            f"error: {entry['name']}: the agent layer fails:\n  "
            + "\n  ".join(problems)
        )
    return len(pages)
