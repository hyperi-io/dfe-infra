#  Project:      dfe-infra
#  File:         scripts/suite/__init__.py
#  Purpose:      Suite maintenance helpers: release a library node, rebuild a
#                consumer onto it, and check one edge of the suite graph.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The release and rebuild half of the suite tooling, over dfe-infra's own graph.

Merging to main, cutting a release and publishing an artefact are exactly the
operations that are otherwise refused. They are safe here because the shape is
fixed and narrow: a change reaches main only as a branch, a PR and a squash
merge whose message this package authors; every wait early-fails on a red job
rather than blocking to a deadline; and a green run is never accepted as proof
of a release -- the published version has to actually move.

The layers, bottom up:

* :mod:`suite.proc` -- output, subprocess, HTTP, poll cadences.
* :mod:`suite.repos` -- find a checkout, read and sync git.
* :mod:`suite.artefacts` -- the live published version, and the assertion that
  it moved.
* :mod:`suite.landing` -- branch, PR, squash merge, follow the run. The ONE PR
  landing implementation; ``scripts/dfe-release.py`` drives its GHCR verbs from
  the same functions.
* :mod:`suite.rebuild` -- move one consumer onto a new producer.
* :mod:`suite.ship` -- release a library node itself.
* :mod:`suite.graph` -- the suite graph, read straight from ``suite_graph``.
* :mod:`suite.kinds` -- what checking one edge involves.

``scripts/dfe-suite`` is the CLI over all of it.

WHAT IS AND IS NOT TESTED HERE. The pure halves are covered directly in
``scripts/tests``: the matchers and edge checks in :mod:`suite.kinds`, the repo
lookup in :mod:`suite.repos`, the chart-contract detection in
:mod:`suite.rebuild`, and every gh orchestration through a recorder standing in
for ``subprocess.run``. What is NOT covered is a real release: cargo, uv and
the two registries are exercised by the dry-run path and by real releases, not
by a test standing in for the toolchain.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The sibling scripts (suite_graph, envfile, registry_pins) are flat modules in
# scripts/, so that directory has to be importable however this package was
# reached. Done once here rather than in each submodule.
_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from suite.artefacts import Artefact, await_artefact  # noqa: E402
from suite.graph import in_edges, load_graph, load_producer, out_edges  # noqa: E402
from suite.kinds import CheckResult, check_edge, parse_evidence  # noqa: E402
from suite.landing import follow_release, land_via_pr  # noqa: E402
from suite.proc import FleetError, tag  # noqa: E402
from suite.rebuild import rebuild_python, rebuild_rust  # noqa: E402
from suite.repos import find_repo  # noqa: E402
from suite.ship import SHIP_PY, SHIP_RS, ShipSpec, ship_library  # noqa: E402

__all__ = [
    "SHIP_PY",
    "SHIP_RS",
    "Artefact",
    "CheckResult",
    "FleetError",
    "ShipSpec",
    "await_artefact",
    "check_edge",
    "find_repo",
    "follow_release",
    "in_edges",
    "land_via_pr",
    "load_graph",
    "load_producer",
    "out_edges",
    "parse_evidence",
    "rebuild_python",
    "rebuild_rust",
    "ship_library",
    "tag",
]
