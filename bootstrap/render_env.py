#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         render_env.py
#  Purpose:      Fill a bootstrap template's $NAME and ${NAME} references from
#                the environment, exactly as envsubst does with no arguments
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Render a template on stdin to stdout, filling `$NAME` and `${NAME}` from the environment.

bootstrap.sh renders every template under bootstrap/templates/ through this, so
a deploy needs nothing beyond Python 3 to do it. The rules are GNU envsubst's
with no SHELL-FORMAT argument:

- `$NAME` and `${NAME}`, NAME being `[A-Za-z_][A-Za-z0-9_]*`, become the
  variable's value, or nothing when it is unset.
- Anything else stays as written, `${NAME:-default}`, `${{ ... }}` and a lone `$`
  included.
- A value is written as it is and never read again for references.

The template and the values are handled as bytes, so a value that is not UTF-8
comes through unchanged.

Usage:
    python3 bootstrap/render_env.py < bootstrap/templates/cluster-secret.yaml.tpl
"""

import argparse
import os
import re
import sys

REFERENCE = re.compile(rb"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def environment() -> dict[bytes, bytes]:
    """The process environment as bytes, which is how envsubst reads it."""
    if os.supports_bytes_environ:
        return dict(os.environb)
    return {key.encode(): value.encode() for key, value in os.environ.items()}


def render(template: bytes, env: dict[bytes, bytes]) -> bytes:
    """Return `template` with every `$NAME` and `${NAME}` replaced from `env`.

    Args:
        template: The template's bytes.
        env: Variable name to value; a name it does not hold renders empty.

    Returns:
        The rendered bytes.
    """
    return REFERENCE.sub(lambda ref: env.get(ref.group(1) or ref.group(2), b""), template)


def main() -> int:
    """Render stdin to stdout from this process's environment."""
    argparse.ArgumentParser(
        description="Fill $NAME and ${NAME} in a template on stdin from the environment, as "
        "envsubst does with no arguments, and write it to stdout."
    ).parse_args()
    sys.stdout.buffer.write(render(sys.stdin.buffer.read(), environment()))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
