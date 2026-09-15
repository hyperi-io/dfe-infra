#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_otel_self_logs.py
#  Purpose:      Prove the filelog receiver's exclude pattern names the
#                daemonset's OWN container, so the collector cannot ship
#                its own logs the way it did against `otc-container`.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The collector's self-log exclusion has to match its real container name.

Kubelet writes each container's logs to /var/log/pods/<pod>/<container>/*.log,
so the filelog receiver's `exclude:` pattern only skips the collector's own
output when the container segment matches the daemonset's actual container
name. The two used to be independent literals (`otc-container` in the
exclude, `otel-collector` in the daemonset) that drifted apart; this renders
both from the chart and checks they still agree.

    python3 scripts/tests/test_otel_self_logs.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "otel-collector"


def render() -> str:
    cmd = ["helm", "template", "otel", str(CHART)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for otel-collector:\n{out.stderr}")
    return out.stdout


def daemonset_container_name(text: str) -> str:
    for doc in yaml.safe_load_all(text):
        if doc and doc.get("kind") == "DaemonSet":
            return doc["spec"]["template"]["spec"]["containers"][0]["name"]
    raise SystemExit("no DaemonSet rendered")


def filelog_exclude(text: str) -> str:
    for doc in yaml.safe_load_all(text):
        if doc and doc.get("kind") == "ConfigMap":
            config = doc["data"].get("daemonset-config.yaml", "")
            m = re.search(r"exclude:\s*\[([^\]]+)\]", config)
            if m:
                return m.group(1)
    raise SystemExit("no filelog exclude rendered")


def test_the_exclude_names_the_real_container() -> None:
    text = render()
    container = daemonset_container_name(text)
    exclude = filelog_exclude(text)
    expect(
        "filelog exclude names the daemonset's own container",
        f"/{container}/" in exclude,
        f"container={container!r}, exclude={exclude!r}",
    )


def test_the_exclude_is_not_the_upstream_literal() -> None:
    """`otc-container` is the upstream chart's own container name, never ours."""
    exclude = filelog_exclude(render())
    expect(
        "exclude does not carry the upstream chart's container name",
        "otc-container" not in exclude,
        exclude,
    )


def main() -> int:
    with standalone():
        test_the_exclude_names_the_real_container()
        test_the_exclude_is_not_the_upstream_literal()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
