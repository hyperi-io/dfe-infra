#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         docs-site/scripts/preview.py
#  Purpose:      Serve one version of the docs site locally and copy each saved
#                content-root/ page into it, so the dev server reloads the page.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""preview -- the docs site on localhost, reloading as content-root/ pages are saved.

    python3 docs-site/scripts/preview.py                      # the newest release
    python3 docs-site/scripts/preview.py --port 3001
    python3 docs-site/scripts/preview.py --work-dir .tmp/docs-clones   # reuse the clones
    python3 docs-site/scripts/preview.py --no-aggregate       # reuse docs-site/content/

Runs aggregate.py for the newest release, generates its API pages, then
`docusaurus start`. The build reads a copy of content-root/, so every second this
copies a saved, added or deleted page across and Docusaurus reloads it.

Member pages come from each repository at the tag the release pins, so an edit
to a member's docs/ does not show here.

Needs the site's npm dependencies installed (npm ci). Ctrl-C stops it.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aggregate

DOCUSAURUS = aggregate.SITE_ROOT / "node_modules" / ".bin" / "docusaurus"
# The dev server runs out of the default heap once the API pages are generated.
HEAP_MB = 8192
POLL_SECONDS = 1.0


def sync(build_root: Path, seen: dict[str, float]) -> dict[str, float]:
    """Copy each content-root/ page saved since `seen` into the build, and remove deleted ones.

    Args:
        build_root: The build's content directory, such as docs-site/content/2.2.1.
        seen: Each page's modification time at the last sync.

    Returns:
        Each page's modification time now.
    """
    pages = aggregate.root_pages()
    now = {relative: path.stat().st_mtime for relative, path in pages.items()}
    for relative, mtime in now.items():
        if seen.get(relative) != mtime:
            target = build_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(pages[relative].read_bytes())
            print(f"preview: copied {relative}", flush=True)
    for relative in seen.keys() - now.keys():
        (build_root / relative).unlink(missing_ok=True)
        print(f"preview: removed {relative}", flush=True)
    return now


def run(*cmd: str, env: dict[str, str] | None = None) -> None:
    """Run one command in the site root, stopping on failure.

    Args:
        *cmd: The command and its arguments.
        env: The environment, when it is not this process's own.

    Raises:
        SystemExit: The command failed.
    """
    if subprocess.run(cmd, cwd=aggregate.SITE_ROOT, env=env, check=False).returncode != 0:
        raise SystemExit(f"error: {' '.join(cmd)} failed")


def main(argv: list[str] | None = None) -> int:
    """Assemble, generate the API pages, then serve and sync until stopped.

    Args:
        argv: The command-line arguments, less the program name.

    Returns:
        The dev server's exit status.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", default="3000", help="the dev server's port (default 3000)")
    parser.add_argument("--work-dir", type=Path, help="keep the member checkouts here")
    parser.add_argument(
        "--no-aggregate", action="store_true", help="serve docs-site/content/ as it stands"
    )
    args = parser.parse_args(argv)

    if not DOCUSAURUS.is_file():
        raise SystemExit(f"error: {DOCUSAURUS} is missing: run npm ci in docs-site first")
    if not args.no_aggregate:
        work = ["--work-dir", str(args.work_dir)] if args.work_dir else []
        run(sys.executable, str(Path(aggregate.__file__)), *work)
    site = json.loads((aggregate.DEFAULT_OUT / aggregate.SITE_FILE).read_text(encoding="utf-8"))
    name = site["newest"]
    if not name:
        raise SystemExit("error: content/site.json names no release to preview")
    env = {**os.environ, "DFE_DOCS_BUILD": name}
    heap = f"--max-old-space-size={HEAP_MB}"
    env["NODE_OPTIONS"] = f"{env.get('NODE_OPTIONS', '')} {heap}".strip()
    run(str(DOCUSAURUS), "gen-api-docs", "all", env=env)

    build_root = aggregate.DEFAULT_OUT / name
    seen = sync(build_root, {})
    server = subprocess.Popen(
        [str(DOCUSAURUS), "start", "--port", args.port, "--no-open"],
        cwd=aggregate.SITE_ROOT,
        env=env,
    )
    # A terminate stops the dev server too, rather than leaving it serving alone.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        while server.poll() is None:
            time.sleep(POLL_SECONDS)
            seen = sync(build_root, seen)
    except KeyboardInterrupt:
        server.terminate()
    return server.wait()


if __name__ == "__main__":
    sys.exit(main())
