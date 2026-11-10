#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         docs-site/scripts/page_meta.py
#  Purpose:      Search metadata the Docusaurus build leaves out: structured data
#                on each page, robots.txt, the root sitemap index and a not-found
#                page that asks not to be indexed.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""page_meta -- the search metadata build_site.py adds to the merged build.

`add_structured_data` puts one JSON-LD block in each prose page of a build: a
TechArticle published by HyperI, about the Data Fusion Engine, and on the
overview page the SoftwareApplication node itself. The @id values are the ones
hyperi.io uses, so both sites describe the same organisation and product.

`write_root_files` writes robots.txt and sitemap.xml, an index of every listed
build's own sitemap. `mark_not_found` keeps the 404 pages out of the index.
"""

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

import agent_layer

ORGANIZATION = "https://hyperi.io/#organization"
SOFTWARE = "https://hyperi.io/data-fusion-engine/#software"
PRODUCT_PAGE = "https://hyperi.io/data-fusion-engine/"
# The page the docs open on, which carries the SoftwareApplication node.
LANDING_SLUG = "overview/"
# search and ai-input are yes for the published docs. ai-train is left unset.
CONTENT_SIGNAL = "search=yes, ai-input=yes"
_HEAD_END = "</head>"
_CANONICAL = re.compile(r'<link[^>]*rel="canonical"[^>]*>')


def _script(node: Mapping[str, object]) -> str:
    text = json.dumps(node, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f'<script type="application/ld+json">{text}</script>'


def article(page: agent_layer.Page, url: str) -> dict[str, object]:
    """The TechArticle node for one page.

    Args:
        page: The page.
        url: Its full URL.

    Returns:
        The node, published by HyperI and about the Data Fusion Engine.
    """
    node: dict[str, object] = {
        "@context": "https://schema.org",
        "@type": "TechArticle",
        "headline": page.title,
        "url": url,
        "inLanguage": "en",
        "publisher": {"@id": ORGANIZATION},
        "about": {"@id": SOFTWARE},
    }
    if page.description:
        node["description"] = page.description
    return node


def software() -> dict[str, object]:
    """The SoftwareApplication node, the same @id hyperi.io gives the product."""
    return {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "@id": SOFTWARE,
        "name": "Data Fusion Engine",
        "alternateName": "DFE",
        "applicationCategory": "DeveloperApplication",
        "operatingSystem": "Kubernetes, Docker",
        "url": PRODUCT_PAGE,
        "publisher": {"@id": ORGANIZATION},
    }


def add_structured_data(
    entry: Mapping[str, object],
    site: Mapping[str, object],
    *,
    content: Path = agent_layer.CONTENT,
    build_dir: Path = agent_layer.BUILD,
) -> int:
    """Put a JSON-LD block in each prose page of one build.

    Args:
        entry: The build's site.json entry.
        site: The whole site.json.
        content: The content root aggregate.py wrote.
        build_dir: The merged build directory.

    Returns:
        The number of pages given one.
    """
    url = str(site["url"]).rstrip("/")
    landing = f"{entry['base_url']}{LANDING_SLUG}".rstrip("/")
    count = 0
    for page in agent_layer.load_pages(entry, content):
        html = build_dir / page.permalink.lstrip("/") / "index.html"
        text = html.read_text(encoding="utf-8")
        nodes = []
        if '"@type":"TechArticle"' not in text:
            nodes.append(article(page, url + page.permalink))
        if page.permalink.rstrip("/") == landing and '"@type":"SoftwareApplication"' not in text:
            nodes.append(software())
        if not nodes:
            continue
        tags = "".join(_script(node) for node in nodes)
        html.write_text(text.replace(_HEAD_END, tags + _HEAD_END, 1), encoding="utf-8")
        count += 1
    return count


def robots(url: str) -> str:
    """robots.txt: everything crawlable, the sitemap index named, the content signal set."""
    return (
        f"User-agent: *\nContent-Signal: {CONTENT_SIGNAL}\nAllow: /\n\nSitemap: {url}/sitemap.xml\n"
    )


def sitemap_index(url: str, base_urls: Iterable[str]) -> str:
    """The root sitemap.xml: an index of each listed build's own sitemap."""
    entries = "".join(
        f"  <sitemap><loc>{url}{base}sitemap.xml</loc></sitemap>\n" for base in base_urls
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}</sitemapindex>\n"
    )


def write_root_files(site: Mapping[str, object], build_dir: Path = agent_layer.BUILD) -> None:
    """Write robots.txt and the sitemap index for the listed builds that have a sitemap."""
    url = str(site["url"]).rstrip("/")
    listed = [
        str(entry["base_url"])
        for entry in site["builds"]
        if entry.get("listed")
        and (build_dir / str(entry["base_url"]).strip("/") / "sitemap.xml").is_file()
    ]
    (build_dir / "robots.txt").write_text(robots(url), encoding="utf-8", newline="\n")
    (build_dir / "sitemap.xml").write_text(
        sitemap_index(url, listed), encoding="utf-8", newline="\n"
    )


def mark_not_found(html: Path) -> None:
    """Ask search engines not to index a not-found page, and drop its canonical link."""
    text = _CANONICAL.sub("", html.read_text(encoding="utf-8"))
    tag = '<meta name="robots" content="noindex">'
    if tag not in text:
        text = text.replace(_HEAD_END, tag + _HEAD_END, 1)
    html.write_text(text, encoding="utf-8")
