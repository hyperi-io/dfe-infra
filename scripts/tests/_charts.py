#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         _charts.py
#  Purpose:      Resolve a chart's directory from its NAME, so a chart that
#                moves is one edit here rather than one per test module.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Where a chart lives, for the tests that render it by name.

Most charts sit at `helm/charts/<name>`, so most callers need nothing from
this. The edge module keeps its two under `helm/edge`, and the gateway's
directory name is not its chart name, so a path built by concatenation is
wrong for them.

The leading underscore keeps pytest from collecting this module as a test file.

    from _charts import chart_dir

    subprocess.run(["helm", "template", name, str(chart_dir(name))])
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HELM = REPO_ROOT / "helm"

# chart name -> directory, relative to helm/, for every chart outside
# helm/charts. A name absent here takes the helm/charts default.
ELSEWHERE = {
    "culvert": "edge/culvert",
    "envoy-gateway-config": "edge/gateway",
}

# Every tree holding a first-party chart, for the repo-wide sweeps that assert
# an invariant over all of them -- a tree left out of one is a silent hole in
# the check rather than a failure.
CHART_TREES = (HELM / "charts", HELM / "edge")


def chart_dir(name: str) -> Path:
    """The directory holding the chart of that name."""
    return HELM / ELSEWHERE.get(name, f"charts/{name}")
