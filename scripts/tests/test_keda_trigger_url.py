#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_keda_trigger_url.py
#  Purpose:      Prove the KEDA ScalingPressure trigger URL the pressure-trigger
#                fragment renders is one bootstrap/keda-scale-test.sh can parse.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Render assertion for the ScalingPressure trigger URL.

Nothing in helm/ asserted this before: a malformed trigger URL only surfaced on
a real cluster, during the manual rc.13 test pass. keda-scale-test.sh reads the
ServiceName back out of the rendered URL with a plain shell parameter expansion
(`${url##*service=}` then `${service%%&*}`), so this reads the URL the
receiver's ScaledObject carries and applies the same extraction, proving the two
sides still agree. The trigger is argocd/values/apps/_fragments/pressure-trigger.yaml,
which the receiver's integration values append on every tier but slim.

The tiers are read from the goldens under fixtures/thin-renders; the override
renders live through the scalo-service library (_weave.library).

    python3 scripts/tests/test_keda_trigger_url.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

import sys

from _expect import expect, standalone, summary
from _gate import CLOUDS, golden
from _weave import render_app

RECEIVER = "dfe-receiver"
# The tiers the receiver scales on pressure; slim scales on CPU alone.
PRESSURE_PROFILES = ("single", "scale", "mesh")


def metrics_api_url(docs: list[dict]) -> str:
    for doc in docs:
        if doc.get("kind") != "ScaledObject":
            continue
        for trigger in doc.get("spec", {}).get("triggers", []):
            if trigger.get("type") == "metrics-api":
                return trigger["metadata"]["url"]
    raise SystemExit("no metrics-api trigger rendered")


def shell_extract_service(url: str) -> str:
    """Mirror keda-scale-test.sh's `${url##*service=}` / `%%&*` parse."""
    service = url.rsplit("service=", 1)[-1] if "service=" in url else url
    return service.split("&", 1)[0]


def pressure_urls() -> list[tuple[str, str]]:
    return [
        (f"{profile}/{cloud}", metrics_api_url(golden(RECEIVER, profile, cloud)))
        for profile in PRESSURE_PROFILES
        for cloud in CLOUDS
    ]


def test_the_trigger_url_carries_a_service_param() -> None:
    for at, url in pressure_urls():
        expect(f"trigger URL has a service= param [{at}]", "service=" in url, url)


def test_keda_scale_test_sh_extracts_the_right_service() -> None:
    """The bug class TS1 was: a rendered URL keda-scale-test.sh's parser mis-reads."""
    for at, url in pressure_urls():
        expect(
            f"keda-scale-test.sh's shell extraction reads back the receiver's own service [{at}]",
            shell_extract_service(url) == RECEIVER,
            f"url={url!r}, extracted={shell_extract_service(url)!r}",
        )


def test_an_overridden_service_name_still_extracts_clean() -> None:
    overlay = {"keda": {"pressure": {"service": "custom-svc"}}}
    url = metrics_api_url(render_app(RECEIVER, "single", "local", overlay=overlay))
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
