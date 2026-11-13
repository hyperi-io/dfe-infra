#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         docs-site/scripts/cli_reference.py
#  Purpose:      Render the dfe CLI reference page from the engine image a
#                build pins, so no repo carries a derived page.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""cli_reference -- the dfe CLI reference page, rendered at build time.

The engine builds its `dfe` command tree from its OpenAPI spec when it starts, so
no repository holds the page. aggregate.py renders it from the engine image the
build pins, the way the HTTP API pages come from the committed spec: `typer utils
docs` runs inside the image and prints every command group as Markdown, which
aggregate.py makes MDX-safe and writes beside the engine's docs. A build at HEAD
renders from the newest release's image, because main publishes none.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path

IMAGE = "ghcr.io/hyperi-io/dfe-engine"
# What a build at HEAD renders from: main publishes no image, the newest release does.
HEAD_TAG = "latest"
PAGE = Path("reference") / "cli.md"
TITLE = "# dfe CLI reference"
# Runs inside the image. SpecCli is not a Typer app, but its .app() is one typer can walk.
_SCRIPT = (
    'printf "from dfe_engine.cli.auto.app import build_cli\\napp = build_cli().app()\\n"'
    " > /tmp/dfe_cli_docs.py && typer /tmp/dfe_cli_docs.py utils docs --name dfe"
)

Runner = Callable[[list[str]], subprocess.CompletedProcess]


class RenderError(RuntimeError):
    """The engine image ran and did not render the page: an older engine, or no command group."""


class ImageError(RenderError):
    """docker itself failed, before the engine ran: no image, no pull, no daemon."""


# docker's own exit codes. Anything lower came from the command inside the image.
_DOCKER_EXIT = 125


def _reason(stderr: str) -> str:
    """The line of docker's or Python's stderr that names the error."""
    lines = stderr.strip().splitlines() or ["no command group rendered"]
    return next((line for line in reversed(lines) if "rror" in line), lines[-1]).strip()


def image_tag(ref: str, head: str) -> str:
    """The image tag a member ref renders from: the ref, or HEAD_TAG at HEAD."""
    return HEAD_TAG if ref == head else ref


def render(tag: str, run: Runner) -> str:
    """The Markdown typer prints for the engine at one image tag.

    Args:
        tag: The engine image tag.
        run: Runs a command and captures its text output.

    Returns:
        The page, one h2 per command group.

    Raises:
        ImageError: docker could not run the image.
        RenderError: the engine in the image did not render the page.
    """
    done = run(["docker", "run", "--rm", "--entrypoint", "sh", f"{IMAGE}:{tag}", "-c", _SCRIPT])
    if done.returncode >= _DOCKER_EXIT:
        raise ImageError(f"{IMAGE}:{tag}: {_reason(done.stderr)}")
    if done.returncode != 0 or "\n## " not in done.stdout:
        raise RenderError(f"{IMAGE}:{tag}: {_reason(done.stderr)}")
    return done.stdout


def page_text(rendered: str, tag: str) -> str:
    """The rendered page as the site shows it: titled, and naming the image it came from.

    Args:
        rendered: What `render` returned.
        tag: The engine image tag it came from.

    Returns:
        The page, not yet MDX-safe.
    """
    first, _, rest = rendered.partition("\n")
    body = rest if first.startswith("# ") else rendered
    lead = f"Rendered from the `dfe` command tree of `{IMAGE}:{tag}`.\n"
    return f"{TITLE}\n\n{lead}{body}"
