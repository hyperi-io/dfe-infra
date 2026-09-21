#  Project:      dfe-infra
#  File:         scripts/dfe_suite/__init__.py
#  Purpose:      Suite helpers for the DFE suite: release, rebuild, and check an edge.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Suite helpers -- the git operations a release needs, tied to the use cases.

Merging to main, cutting a release and publishing an artefact are exactly the
operations that are otherwise refused. They are safe here because the shape is
fixed and narrow: a change reaches main only as a branch, a PR and a squash
merge whose message this package authors; every wait early-fails on a red job
rather than blocking to a deadline; and a green run is never accepted as proof
of a release -- the published version has to actually move.

The layers, bottom up:

* :mod:`~hyperi_ai.suite.proc` -- output, subprocess, HTTP, poll cadences.
* :mod:`~hyperi_ai.suite.repos` -- find a checkout, read and sync git.
* :mod:`~hyperi_ai.suite.artefacts` -- the live published version, and the
  assertion that it moved.
* :mod:`~hyperi_ai.suite.landing` -- branch, PR, squash merge, follow the run.
* :mod:`~hyperi_ai.suite.rebuild` -- move one consumer onto a new producer.
* :mod:`~hyperi_ai.suite.ship` -- release a library node itself.
* :mod:`~hyperi_ai.suite.graph` -- one producer's slice of the suite graph.
* :mod:`~hyperi_ai.suite.kinds` -- what checking one edge involves.

``tools/scalo_fleet.py`` is the compatibility CLI over the release half;
``tools/dfe_suite.py`` is the graph-driven one.

WHAT IS AND IS NOT TESTED HERE. The pure halves are covered directly: the
matchers and every edge check in :mod:`~hyperi_ai.suite.kinds`, the repo lookup
and slug read in :mod:`~hyperi_ai.suite.repos`, the chart-contract detection in
:mod:`~hyperi_ai.suite.rebuild`. The rest of ``rebuild``, ``ship``, ``landing``
and ``artefacts`` is toolchain-bound -- it drives cargo, uv, gh and the two
registries -- so it is exercised by its dry-run path and by real releases, not
by a test that would have to stand in for the toolchain to say anything.
"""

from __future__ import annotations

from dfe_suite.artefacts import Artefact, await_artefact
from dfe_suite.graph import in_edges, load_graph, load_producer, out_edges
from dfe_suite.kinds import CheckResult, check_edge, parse_evidence
from dfe_suite.landing import follow_release, land_via_pr
from dfe_suite.proc import FleetError, tag
from dfe_suite.rebuild import rebuild_python, rebuild_rust
from dfe_suite.repos import find_repo
from dfe_suite.ship import SHIP_PY, SHIP_RS, ShipSpec, ship_library

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
