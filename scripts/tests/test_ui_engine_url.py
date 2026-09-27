#  Project:      dfe-infra
#  File:         scripts/tests/test_ui_engine_url.py
#  Purpose:      Prove the console's server side is pointed at the engine in the
#                namespace the release actually lands in.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Where dfe-ui's server side looks for the engine.

The console's login and every server-rendered page fetch INTERNAL_API_URL. The
chart derived its host from {project}-{env}, which is the namespace only when
DFE_NAMESPACE happens to be dfe-<DFE_ENV>. A deploy with DFE_ENV=test and
DFE_NAMESPACE=dfe-local rendered dfe-engine.dfe-test and the login page failed
before it drew the form.

    python3 -m pytest scripts/tests/test_ui_engine_url.py -q

Needs `helm` on PATH.
"""

import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
UI_CHART = REPO_ROOT / "helm" / "charts" / "dfe-ui"


def _internal_api_url(namespace: str, *sets: str) -> str:
    cmd = ["helm", "template", "dfe-ui", str(UI_CHART), "--namespace", namespace,
           "--show-only", "templates/deployment.yaml"]
    for value in sets:
        cmd += ["--set", value]
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True)
    deployment = yaml.safe_load(done.stdout)
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    return next(item["value"] for item in env if item["name"] == "INTERNAL_API_URL")


def test_the_engine_is_found_in_the_release_namespace_not_one_built_from_env() -> None:
    assert _internal_api_url("dfe-local", "env=test") == (
        "http://dfe-engine.dfe-local.svc.cluster.local:8000"
    )


def test_a_namespace_matching_the_env_convention_still_resolves() -> None:
    assert _internal_api_url("dfe-prod", "env=prod") == (
        "http://dfe-engine.dfe-prod.svc.cluster.local:8000"
    )


def test_an_explicit_url_still_wins() -> None:
    assert _internal_api_url(
        "dfe-local", "env=test", "config.internalApiUrl=http://engine.example:8000"
    ) == "http://engine.example:8000"
