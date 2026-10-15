#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         _bootstrap.py
#  Purpose:      Shared plumbing for the tests that run a slice of bootstrap.sh under
#                bash: pulling one shell function's lines out of the script.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the tests that run a slice of bootstrap.sh share.

`test_argocd_login.py`, `test_argocd_upgrade.py`, `test_dfe_ops_upgrade.py` and
`test_gateway_api_crds_first.py` each build a script from bootstrap.sh's own helper
functions and steps, then run it under bash against fakes. This module holds the
function extraction they all use.

The leading underscore keeps pytest from collecting this module as a test file.

    from _bootstrap import function_lines
"""


def function_lines(lines: list[str], name: str) -> list[str]:
    """The lines of the shell function `name`, from `name() {` to its closing `}`.

    The function is either a one-liner or closes on a `}` alone in the first column.
    """
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{name}() {{"))
    if lines[start].rstrip().endswith("}"):
        return [lines[start]]
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")
    return lines[start : end + 1]
