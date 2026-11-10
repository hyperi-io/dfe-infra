#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_docs_agent_layer.py
#  Purpose:      Cover docs-site/scripts/agent_layer.py: markdown copy names,
#                link rewriting, the source boundary, dead links and agent tasks.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the docs site's agent layer.

python3 -m pytest scripts/tests/test_docs_agent_layer.py -q
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "docs-site" / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("docs_agent_layer", SCRIPTS / "agent_layer.py")
agent_layer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_layer)

BUILD = agent_layer.BuildLinks(
    "9.9.9", "/v/9.9.9/", {"svc": ("o/svc", "v1.0.0"), "ui/hd": ("o/hd", "v2.0.0")}
)
PAGES = {
    page.source: page
    for page in (
        agent_layer.Page("Svc", "content/9.9.9/svc/index.md", "/v/9.9.9/svc/"),
        agent_layer.Page("Arch", "content/9.9.9/svc/architecture.md", "/v/9.9.9/svc/architecture/"),
        agent_layer.Page("Ops", "content/9.9.9/svc/ops/README.md", "/v/9.9.9/svc/ops/"),
        agent_layer.Page("Home", "content/9.9.9/README.md", "/v/9.9.9/overview/"),
    )
}
ARCH = PAGES["content/9.9.9/svc/architecture.md"]


@pytest.mark.parametrize(
    ("permalink", "expected"),
    [
        ("/v/9.9.9/", "/v/9.9.9/index.md"),
        ("/v/9.9.9", "/v/9.9.9/index.md"),
        ("/v/9.9.9/svc/", "/v/9.9.9/svc.md"),
        ("/v/9.9.9/svc/ops/", "/v/9.9.9/svc/ops.md"),
    ],
)
def test_a_copy_is_named_after_its_route(permalink: str, expected: str) -> None:
    assert BUILD.markdown_url(permalink) == expected


@pytest.mark.parametrize(
    ("resolved", "expected"),
    [
        ("svc/img/flow.png", "https://raw.githubusercontent.com/o/svc/v1.0.0/docs/img/flow.png"),
        ("svc/config.yaml", "https://github.com/o/svc/blob/v1.0.0/docs/config.yaml"),
        ("ui/hd/x.json", "https://github.com/o/hd/blob/v2.0.0/docs/x.json"),
        ("quickstart/notes.txt", None),
        ("svcx/a.md", None),
    ],
)
def test_a_non_page_file_points_at_its_member_on_github(
    resolved: str, expected: str | None
) -> None:
    assert BUILD.on_github(resolved) == expected


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("[a](ops/README.md)", "[a](/v/9.9.9/svc/ops.md)"),
        ("[a](ops/)", "[a](/v/9.9.9/svc/ops.md)"),
        ("[a](index.md#top)", "[a](/v/9.9.9/svc.md#top)"),
        ("[a](../README.md)", "[a](/v/9.9.9/overview.md)"),
        ("[a](<ops/README.md>)", "[a](</v/9.9.9/svc/ops.md>)"),
        ("[a](img/x.png)", "[a](https://raw.githubusercontent.com/o/svc/v1.0.0/docs/img/x.png)"),
        ("[a](https://example.com/x.md)", "[a](https://example.com/x.md)"),
        ("[a](#local)", "[a](#local)"),
        ("[a](../../outside.md)", "[a](../../outside.md)"),
        ('<a href="ops/README.md">a</a>', '<a href="/v/9.9.9/svc/ops.md">a</a>'),
        ("`[a](ops/README.md)`", "`[a](ops/README.md)`"),
    ],
)
def test_links_point_at_the_target_copy_or_github(link: str, expected: str) -> None:
    assert agent_layer.rewrite_links(link, ARCH, PAGES, BUILD) == expected


def test_a_fenced_block_keeps_its_links() -> None:
    text = "```\n[a](ops/README.md)\n```\n"
    assert agent_layer.rewrite_links(text, ARCH, PAGES, BUILD) == text


def _manifest(tmp_path: Path, pages: list[dict]) -> Path:
    path = tmp_path / "_manifests" / "9.9.9.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(pages), encoding="utf-8")
    return tmp_path


def test_api_pages_are_left_to_the_spec(tmp_path: Path) -> None:
    content = _manifest(
        tmp_path,
        [
            {"title": "B", "source": "content/9.9.9/svc/b.md", "permalink": "/v/9.9.9/svc/b/"},
            {
                "title": "A",
                "source": "content/9.9.9/svc/api/x.mdx",
                "permalink": "/v/9.9.9/svc/api/x/",
            },
        ],
    )
    entry = {"name": "9.9.9", "openapi": {"output": "9.9.9/svc/api"}}
    assert [page.title for page in agent_layer.load_pages(entry, content)] == ["B"]


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        (
            [{"title": "X", "source": "src/main.md", "permalink": "/v/9.9.9/x/"}],
            "outside content/9.9.9/",
        ),
        (None, "is missing"),
    ],
)
def test_a_page_from_outside_the_published_tree_stops_the_build(
    tmp_path: Path, pages: list[dict] | None, message: str
) -> None:
    content = _manifest(tmp_path, pages) if pages is not None else tmp_path
    with pytest.raises(SystemExit, match=message):
        agent_layer.load_pages({"name": "9.9.9"}, content)


def test_a_link_to_a_missing_copy_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "v" / "9.9.9"
    root.mkdir(parents=True)
    (root / "a.md").write_text("[ok](/v/9.9.9/b.md) [gone](/v/9.9.9/c.md#x)\n", encoding="utf-8")
    (root / "b.md").write_text("# B\n", encoding="utf-8")
    assert agent_layer.dead_links(root, tmp_path) == ["v/9.9.9/a.md: /v/9.9.9/c.md"]


@pytest.mark.parametrize(
    ("index", "files", "expected"),
    [
        ("- [D](https://x/v/9.9.9/q/docker.md)\n", {"q/docker.md": "run make up"}, []),
        ("", {"q/docker.md": "run make up"}, ["Docker: q/docker.md is not in llms.txt"]),
        ("- [D](https://x/v/9.9.9/q/docker.md)\n", {}, ["Docker: q/docker.md was not built"]),
        (
            "- [D](https://x/v/9.9.9/q/docker.md)\n",
            {"q/docker.md": "nothing here"},
            ["Docker: q/docker.md does not hold 'make up'"],
        ),
    ],
)
def test_an_agent_task_needs_its_page_listed_built_and_holding_the_answer(
    tmp_path: Path, index: str, files: dict[str, str], expected: list[str]
) -> None:
    (tmp_path / "llms.txt").write_text(index, encoding="utf-8")
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    tasks = {"Docker": {"page": "q/docker.md", "expect": "make up"}}
    assert agent_layer.failed_tasks(tmp_path, tasks) == expected


def test_the_real_task_file_parses() -> None:
    tasks = agent_layer.load_tasks()
    assert tasks
    assert all(task["page"] and task["expect"] for task in tasks.values())


# --- search metadata -----------------------------------------------------------------

meta_spec = importlib.util.spec_from_file_location("docs_page_meta", SCRIPTS / "page_meta.py")
page_meta = importlib.util.module_from_spec(meta_spec)
meta_spec.loader.exec_module(page_meta)
LONG = "word " * 50


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "# Title\n\nThe loader writes batches to ClickHouse.\n",
            "The loader writes batches to ClickHouse.",
        ),
        (
            "Status: Adopted\n\nThe receiver takes events in over HTTPS and gRPC.\n",
            "The receiver takes events in over HTTPS and gRPC.",
        ),
        (
            "Operations\n\nMarch 2026\n\nThis page covers the archiver and its buckets today.\n",
            "This page covers the archiver and its buckets today.",
        ),
        (
            "> A blockquote summary that\n> wraps over two lines here.\n",
            "A blockquote summary that wraps over two lines here.",
        ),
        (
            "```\nnot this code block at all here\n```\n\nSee the [guide](g.md) and `make up` now.\n",
            "See the guide and make up now.",
        ),
        (
            "<!-- a\ncomment -->\n- a list item first\n\n**Bold** words in a real paragraph here.\n",
            "Bold words in a real paragraph here.",
        ),
        (
            "A nested <scr<b>ipt> tag leaves no angle bracket in the text.\n",
            "A nested tag leaves no angle bracket in the text.",
        ),
        ("# Only headings\n\n## And more\n", ""),
        (LONG, ("word " * 31).strip() + "..."),
    ],
)
def test_a_description_is_the_first_real_paragraph_cut_at_a_word(body: str, expected: str) -> None:
    script = "process.stdout.write(require(process.argv[1]).describe(process.argv[2]))"
    done = subprocess.run(
        ["node", "-e", script, str(REPO_ROOT / "docs-site" / "description.js"), body],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    assert done.stdout == expected
    assert len(done.stdout) <= 160


def test_every_page_gets_an_article_and_the_landing_the_product(tmp_path: Path) -> None:
    content = _manifest(
        tmp_path / "content",
        [
            {
                "title": "Home",
                "source": "content/9.9.9/README.md",
                "permalink": "/v/9.9.9/overview",
                "description": "DFE </script>",
            },
            {"title": "Ops", "source": "content/9.9.9/ops.md", "permalink": "/v/9.9.9/ops/"},
        ],
    )
    build = tmp_path / "build"
    for route in ("overview", "ops"):
        (build / "v/9.9.9" / route).mkdir(parents=True)
        (build / "v/9.9.9" / route / "index.html").write_text(
            '<head><script type="application/ld+json">{"@type":"BreadcrumbList"}</script></head>',
            encoding="utf-8",
        )
    entry = {"name": "9.9.9", "base_url": "/v/9.9.9/"}
    site = {"url": "https://docs.example"}
    assert page_meta.add_structured_data(entry, site, content=content, build_dir=build) == 2
    assert page_meta.add_structured_data(entry, site, content=content, build_dir=build) == 0
    home = (build / "v/9.9.9/overview/index.html").read_text(encoding="utf-8")
    ops = (build / "v/9.9.9/ops/index.html").read_text(encoding="utf-8")
    assert home.count("application/ld+json") == 3
    assert page_meta.SOFTWARE in home
    assert "<\\/script>" in home
    assert ops.count("application/ld+json") == 2
    assert '"url":"https://docs.example/v/9.9.9/ops/"' in ops
    assert '"description"' not in ops


def test_the_root_files_name_only_listed_builds_with_a_sitemap(tmp_path: Path) -> None:
    for base in ("v/2.2.1", "v/2.2.0"):
        (tmp_path / base).mkdir(parents=True)
    (tmp_path / "v/2.2.1/sitemap.xml").write_text("", encoding="utf-8")
    site = {
        "url": "https://docs.example",
        "builds": [
            {"base_url": "/v/2.2.1/", "listed": True},
            {"base_url": "/v/2.2.0/", "listed": True},
            {"base_url": "/next/", "listed": False},
        ],
    }
    page_meta.write_root_files(site, tmp_path)
    index = (tmp_path / "sitemap.xml").read_text(encoding="utf-8")
    assert index.count("<sitemap>") == 1
    assert "https://docs.example/v/2.2.1/sitemap.xml" in index
    robots = (tmp_path / "robots.txt").read_text(encoding="utf-8")
    assert "Sitemap: https://docs.example/sitemap.xml" in robots
    assert "Content-Signal: search=yes, ai-input=yes\n" in robots
    assert "ai-train" not in robots


def test_a_not_found_page_drops_its_canonical_and_asks_for_noindex(tmp_path: Path) -> None:
    html = tmp_path / "404.html"
    html.write_text(
        '<head><link data-rh="true" rel="canonical" href="/x/404.html/"></head>', encoding="utf-8"
    )
    page_meta.mark_not_found(html)
    page_meta.mark_not_found(html)
    text = html.read_text(encoding="utf-8")
    assert "canonical" not in text
    assert text.count('name="robots" content="noindex"') == 1
