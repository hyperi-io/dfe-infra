#  Project:      dfe-infra
#  File:         scripts/tests/test_ui_engine_url.py
#  Purpose:      Prove the console's server side is pointed at the engine in the
#                namespace the release actually lands in.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Where dfe-ui's server side looks for the engine.

The console's login and every server-rendered page fetch INTERNAL_API_URL. A host
derived from {project}-{env} is the namespace only when DFE_NAMESPACE happens to
be dfe-<DFE_ENV>: with DFE_ENV=test and DFE_NAMESPACE=dfe-local it names
dfe-engine.dfe-test, and the login page fails before it draws the form.
The deployed dfe-ui Application takes its namespace and posture from the cluster
secret, and a deployer names an engine elsewhere in its instance values.

    python3 -m pytest scripts/tests/test_ui_engine_url.py -q

Needs `helm` on PATH and the scalo-service library (_weave.library).
"""

from _gate import effective_env
from _weave import render_app


def _internal_api_url(namespace: str, env: str, overlay: dict | None = None) -> object:
    docs = render_app("dfe-ui", "slim", "local", namespace=namespace, env=env, overlay=overlay)
    return effective_env(docs, "Deployment/dfe-ui")["INTERNAL_API_URL"]


def test_the_engine_is_found_in_the_release_namespace_not_one_built_from_env() -> None:
    assert _internal_api_url("dfe-local", "test") == (
        "http://dfe-engine.dfe-local.svc.cluster.local:8000"
    )


def test_a_namespace_matching_the_env_convention_still_resolves() -> None:
    assert _internal_api_url("dfe-prod", "prod") == (
        "http://dfe-engine.dfe-prod.svc.cluster.local:8000"
    )


def test_an_explicit_url_still_wins() -> None:
    overlay = {"extraEnv": {"INTERNAL_API_URL": "http://engine.example:8000"}}
    assert _internal_api_url("dfe-local", "test", overlay) == "http://engine.example:8000"
