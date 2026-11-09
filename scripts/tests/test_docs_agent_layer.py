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
