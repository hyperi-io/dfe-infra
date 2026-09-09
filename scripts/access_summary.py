#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         access_summary.py
#  Purpose:      The file a deploy hands the person who ran it -- the two minted
#                logins in plaintext, on their machine, mode 0600. Rendered and
#                read back in one place, so dfe-ops writes exactly what the
#                onboarding suite signs in with. Stdlib only.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""access_summary -- how the launcher gets in.

Every other route to these passwords needs access to the deployment the operator
has not logged into yet: a kubectl fetch command is no use before the first
login. So the deploy writes them out, once, where the person who ran it is
standing, and tells them to delete the file.

Nothing here talks to a cluster. It renders the file, reads one back, and says
where it goes, which is what makes both halves testable and what stops the
writer and the reader drifting apart.
"""

from __future__ import annotations

from pathlib import Path

FILENAME = "access-summary.md"
ADMIN_LABEL = "admin"
BREAKGLASS_LABEL = "break-glass"

# The three things the person who ran the deploy has to do next. In the file
# rather than a doc page: this file is the only thing they are holding.
NEXT_STEPS = (
    "Log in at the console URL above and finish the setup wizard.",
    "Retire the bootstrap admin from the wizard's last step once your own admin exists.",
    "Keep the break-glass password somewhere safe, then delete this file.",
)


def render(
    console_url: str,
    engine_url: str,
    admin: tuple[str, str],
    breakglass: tuple[str, str],
) -> str:
    """The launcher's copy of the deployment's two minted logins.

    Args:
        console_url: Where the operator logs in.
        engine_url: The engine API base.
        admin: (username, password) of the minted admin.
        breakglass: (username, password) of the minted break-glass account.

    Returns:
        The Markdown body.
    """
    lines = [
        "# DFE access -- first login",
        "",
        f"- Console: {console_url}",
        f"- Engine API: {engine_url}",
        "",
        "| Account | Username | Password |",
        "|---------|----------|----------|",
        f"| {ADMIN_LABEL.title()} | `{admin[0]}` | `{admin[1]}` |",
        f"| {BREAKGLASS_LABEL.title()} | `{breakglass[0]}` | `{breakglass[1]}` |",
        "",
    ]
    lines += [f"{n}. {line}" for n, line in enumerate(NEXT_STEPS, start=1)]
    return "\n".join(lines) + "\n"


def parse(text: str) -> dict[str, tuple[str, str]]:
    """The logins out of an access summary, keyed by account label.

    Args:
        text: The file's contents.

    Returns:
        Lowercased label -> (username, password), for every complete row.
    """
    found: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3 or not all(c.startswith("`") and c.endswith("`") for c in cells[1:]):
            continue
        found[cells[0].lower()] = (cells[1].strip("`"), cells[2].strip("`"))
    return found


def run_dir(repo: Path, stack: str, mode: str) -> Path:
    """Where one deploy's artefacts go on the launcher's machine.

    ``.tmp/`` is the repo's scratch directory and is gitignored, so a credential
    file written there cannot be committed by accident.

    Args:
        repo: The dfe-infra checkout the deploy ran from.
        stack: The stack version deployed.
        mode: The deploy mode.

    Returns:
        The run directory.
    """
    return repo / ".tmp" / f"{stack}-{mode}"


def write(path: Path, body: str) -> Path:
    """Write the summary readable only by the person who ran the deploy.

    Args:
        path: Where to write it.
        body: The rendered Markdown.

    Returns:
        The path written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(0o600)
    return path
