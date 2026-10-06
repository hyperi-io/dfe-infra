#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_smoke_node_counts.py
#  Purpose:      Prove CORE 2 of the integration smoke test prints the marker's
#                count on each ClickHouse node under its per-node counts line.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The per-node counts CORE 2 reports beside its every-node assertion.

The counts are what tells a split brain from a dead pipeline when the check
fails, and check() discards its command's output, so they need printing apart
from it. The fake `kubectl` in _smoke counts the rows it was posted, on its one pod.

    python3 scripts/tests/test_smoke_node_counts.py

No third-party deps and no test runner, matching the script it tests.
"""

import sys

from _expect import expect, standalone, summary
from _smoke import run_smoke


def test_each_node_count_is_printed_under_its_heading() -> None:
    out, counts = run_smoke({"otel_zero_answers": 0}, DFE_OTEL_WAIT="0")
    lines = out.stdout.splitlines()
    heading = next((i for i, line in enumerate(lines) if "per-node counts:" in line), None)
    expect("CORE 2 prints its per-node counts heading", heading is not None, out.stdout)
    following = lines[heading + 1] if heading is not None and heading + 1 < len(lines) else ""
    expect("the node's count is the next line",
           following == f"    dfe-clickhouse-0: {counts['posted']}", f"got {following!r}")


def test_the_assertion_still_runs() -> None:
    out, _ = run_smoke({"otel_zero_answers": 0}, DFE_OTEL_WAIT="0")
    expect("the every-node check still reports",
           "[PASS] fixture events posted to receiver land in dfe.main on EVERY ClickHouse node"
           in out.stdout, out.stdout)


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
