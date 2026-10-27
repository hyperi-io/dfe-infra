#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         docs-site/scripts/build_site.py
#  Purpose:      Build every version aggregate.py assembled and merge them into the
#                one static tree the docs Worker serves.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""build_site -- one Docusaurus build per version, merged for a single deploy.

    python3 docs-site/scripts/build_site.py               # every build in content/site.json
    python3 docs-site/scripts/build_site.py --only 2.2.1  # one build, the others left as built

For each build content/site.json lists: generate the engine's API pages from its
spec (`docusaurus gen-api-docs`), then `docusaurus build` with DFE_DOCS_BUILD naming
the build, into build/<its base path>. Then write build/_redirects so / and every
dropped stack's paths reach the newest release, copy that release's 404 page to the root, and count the files
against the Workers static-asset limits.

Needs the site's npm dependencies installed (npm ci) and aggregate.py run first.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SITE_ROOT = Path(__file__).resolve().parents[1]
CONTENT = SITE_ROOT / "content"
BUILD = SITE_ROOT / "build"
DOCUSAURUS = SITE_ROOT / "node_modules" / ".bin" / "docusaurus"
# Workers static assets on the Free plan: files per deployed version, and bytes per file.
FILE_LIMIT = 20_000
FILE_SIZE_LIMIT = 25 * 1024 * 1024


def docusaurus(*args: str, build: str) -> None:
    """Run one Docusaurus command for one build, streaming its output.

    Args:
        *args: The command and its arguments.
        build: The build's name, passed as DFE_DOCS_BUILD.

    Raises:
        SystemExit: The command failed.
    """
    env = {**os.environ, "DFE_DOCS_BUILD": build}
    done = subprocess.run([str(DOCUSAURUS), *args], cwd=SITE_ROOT, env=env, check=False)
    if done.returncode != 0:
        raise SystemExit(f"error: docusaurus {' '.join(args)} failed for {build}")


def build_one(entry: dict) -> Path:
    """Generate the API pages and build one version into its base path.

    Args:
        entry: The build's site.json entry.

    Returns:
        The build's output directory.
    """
    if entry.get("openapi"):
        docusaurus("gen-api-docs", "all", build=entry["name"])
    out = BUILD / entry["base_url"].strip("/")
    docusaurus("build", "--out-dir", str(out), build=entry["name"])
    return out


def redirects(newest: str, dropped: list[str] | None = None) -> str:
    """The Workers _redirects file: the site root and /v/ go to the newest release.

    A dropped stack's pages go permanently to the same path under the newest release,
    where the version menu's own switch would have sent the reader.

    Args:
        newest: The newest listed build.
        dropped: Stacks publish.yaml drops.

    Returns:
        The file's text.
    """
    target = f"/v/{newest}/"
    lines = [f"{source} {target} 302" for source in ("/", "/v", "/v/")]
    for stack in dropped or []:
        old = f"/v/{stack}"
        lines += [f"{old} {target} 301", f"{old}/ {target} 301", f"{old}/* {target}:splat 301"]
    return "".join(f"{line}\n" for line in lines)


def count(tree: Path) -> tuple[int, int, int]:
    """Count the pages, the files and the largest file's bytes under `tree`.

    Args:
        tree: A build output directory.

    Returns:
        (HTML pages, files, the largest file's size in bytes).
    """
    files = [path for path in tree.rglob("*") if path.is_file()]
    pages = sum(1 for path in files if path.suffix == ".html")
    largest = max((path.stat().st_size for path in files), default=0)
    return pages, len(files), largest


def main(argv: list[str] | None = None) -> int:
    """Build, merge and count.

    Args:
        argv: The command-line arguments, less the program name.

    Returns:
        The exit status: 1 when the merged tree passes a Workers limit.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--only", help="build this one version and keep the others as built")
    parser.add_argument(
        "--file-limit",
        type=int,
        default=FILE_LIMIT,
        help=f"files allowed per deployed Worker version (default {FILE_LIMIT})",
    )
    args = parser.parse_args(argv)

    site_file = CONTENT / "site.json"
    if not site_file.is_file():
        raise SystemExit(f"error: {site_file} is missing: run scripts/aggregate.py first")
    site = json.loads(site_file.read_text(encoding="utf-8"))
    entries = [entry for entry in site["builds"] if not args.only or entry["name"] == args.only]
    if not entries:
        raise SystemExit(f"error: content/site.json lists no build named {args.only}")
    if not DOCUSAURUS.is_file():
        raise SystemExit(f"error: {DOCUSAURUS} is missing: run npm ci in docs-site first")
    if not args.only and BUILD.exists():
        shutil.rmtree(BUILD)

    for entry in entries:
        out = build_one(entry)
        pages, files, largest = count(out)
        print(f"{entry['name']}: {pages} pages, {files} files, largest {largest} bytes")

    if site["newest"]:
        newest_404 = BUILD / "v" / site["newest"] / "404.html"
        if newest_404.is_file():
            shutil.copyfile(newest_404, BUILD / "404.html")
        text = redirects(site["newest"], site.get("dropped", []))
        (BUILD / "_redirects").write_text(text, encoding="utf-8", newline="\n")

    pages, files, largest = count(BUILD)
    print(f"build/: {pages} pages, {files} files, largest {largest} bytes")
    print(f"Workers limits: {args.file_limit} files per version, {FILE_SIZE_LIMIT} bytes per file")
    if files > args.file_limit or largest > FILE_SIZE_LIMIT:
        print("error: build/ passes a Workers static-asset limit", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
