#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         private_file.py
#  Purpose:      Write a credential file only its owner can read, private before
#                the first byte lands, for every tool that puts a password, a
#                kubeconfig or a key on the operator's disk. Stdlib only.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""private_file -- a credential on disk at mode 0600, never wider, not even briefly.

A write followed by a chmod leaves the text readable at the umask's mode until
the chmod lands. An existing file keeps its old mode through an O_CREAT open, so
a file that was ever wider stays wider while it is rewritten. Setting the mode on
the open descriptor, before anything is written, closes both.

    from pathlib import Path
    import private_file

    private_file.write_private(Path(".tmp/access-summary.md"), body)
"""

import os
from pathlib import Path

PRIVATE_MODE = 0o600


def write_private(path: Path, text: str) -> Path:
    """Write *text* to *path*, readable and writable by this user alone.

    A new file is created at 0600 and an existing one is narrowed to 0600 before
    any of *text* is written, whatever the umask. Missing parent directories are
    created.

    Args:
        path: Where to write.
        text: The content, written as UTF-8 with LF line endings.

    Returns:
        The path written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        # O_CREAT's mode applies only to a new file, so an existing one is narrowed here.
        os.fchmod(fd, PRIVATE_MODE)
        handle.write(text)
    return path
