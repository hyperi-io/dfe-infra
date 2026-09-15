#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_keda_trigger_url.py
#  Purpose:      Prove the KEDA ScalingPressure trigger URL dfe-common.scaledobject
#                renders is one bootstrap/keda-scale-test.sh can actually parse.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Render assertion for the ScalingPressure trigger URL.

Nothing in helm/ asserted this before: a malformed trigger URL only surfaced on
a real cluster, during the manual rc.13 test pass. keda-scale-test.sh reads the
ServiceName back out of the rendered URL with a plain shell parameter expansion
(`${REAL_URL##*service=}` then `${REAL_SERVICE%%&*}`), so this renders the
chart and applies the same extraction, proving the two sides still agree.

    python3 scripts/tests/test_keda_trigger_url.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "dfe-receiver"


def render(*args: str) -> list[dict]:
    cmd = ["helm", "template", "kt", str(CHART), *args]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for dfe-receiver {args}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def metrics_api_url(docs: list[dict]) -> str:
    for doc in docs:
        if doc.get("kind") != "ScaledObject":
            continue
        for trigger in doc.get("spec", {}).get("triggers", []):
            if trigger.get("type") == "metrics-api":
                return trigger["metadata"]["url"]
    raise SystemExit("no metrics-api trigger rendered")


def shell_extract_service(url: str) -> str:
    """Mirror keda-scale-test.sh's `${REAL_URL##*service=}` / `%%&*` parse."""
    service = url.rsplit("service=", 1)[-1] if "service=" in url else url
    return service.split("&", 1)[0]


def test_the_trigger_url_carries_a_service_param() -> None:
    url = metrics_api_url(render("--set", "keda.pressure.enabled=true"))
    expect("trigger URL has a service= param", "service=" in url, url)


def test_keda_scale_test_sh_extracts_the_right_service() -> None:
    """The bug class TS1 was: a rendered URL keda-scale-test.sh's parser mis-reads."""
    url = metrics_api_url(render("--set", "keda.pressure.enabled=true"))
    expect(
        "keda-scale-test.sh's shell extraction reads back the chart's own service",
        shell_extract_service(url) == "dfe-receiver",
        f"url={url!r}, extracted={shell_extract_service(url)!r}",
    )


def test_an_overridden_service_name_still_extracts_clean() -> None:
    url = metrics_api_url(
        render("--set", "keda.pressure.enabled=true", "--set", "keda.pressure.service=custom-svc")
    )
    expect(
        "an overridden service name survives the same shell extraction",
        shell_extract_service(url) == "custom-svc",
        f"url={url!r}, extracted={shell_extract_service(url)!r}",
    )


def main() -> int:
    with standalone():
        test_the_trigger_url_carries_a_service_param()
        test_keda_scale_test_sh_extracts_the_right_service()
        test_an_overridden_service_name_still_extracts_clean()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
