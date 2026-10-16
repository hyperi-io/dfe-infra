#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         envfile.py
#  Purpose:      Shared KEY=VALUE env-file reader for the repo's tools, so
#                dfe-ops and its subcommands parse an operator's env file the
#                same way instead of each carrying its own copy.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""envfile -- read a flat KEY=VALUE file the way the DFE tools expect.

Deliberately NOT a shell interpreter: no ``$VAR`` expansion and no command
substitution. These are pin/credential files read by automation, not scripts, so
anything that could execute is out of scope by design.

The shape it accepts (matching bootstrap/local.env.example):

  * ``KEY=value``, with an optional ``export `` prefix
  * ``#`` comment lines, and blank lines
  * one layer of surrounding single or double quotes stripped off the value
  * a line with no ``=`` is skipped rather than raising

    from pathlib import Path
    import envfile

    envfile.parse_env_file(Path("bootstrap/.env"))
    # {'DFE_NAMESPACE': 'dfe', 'DFE_PROFILE': 'single'}

    # Several files, later wins:
    envfile.load_env_files(["bootstrap/.env", "local.env"])

A missing file raises FileNotFoundError -- a tool told to read an env file that
is not there has been mis-invoked, and silently returning {} would show up much
later as a confusing failure.

stdlib only.
"""

from collections.abc import Iterable
from pathlib import Path


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a flat KEY=VALUE env file into a dict.

    Args:
        path: Path to the env file.

    Returns:
        Mapping of variable name to value, in file order.

    Raises:
        FileNotFoundError: The file does not exist.
    """
    out: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def load_env_files(
    paths: Iterable[str | Path], into: dict[str, str] | None = None
) -> dict[str, str]:
    """Merge several env files into one mapping, later files winning.

    Args:
        paths: Env files to read, in precedence order (last wins).
        into: Existing mapping to merge into, updated in place. A new dict when
            omitted.

    Returns:
        The merged mapping (``into`` itself when one was given).

    Raises:
        FileNotFoundError: Any of the files does not exist.
    """
    merged = {} if into is None else into
    for path in paths:
        merged.update(parse_env_file(Path(path)))
    return merged
