#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_engine_docs_surface.py
#  Purpose:      Prove the engine, the gateway and the links page agree on
#                whether the engine's Swagger surface exists.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for api.docsEnabled across the three renders that read it.

The engine serves Swagger UI at /docs and ReDoc at /redoc when api.docsEnabled is
true, or when it is unset and `env` is a dev posture (dfe-engine settings.py,
api.docs_enabled and is_dev_posture). The gateway routes the two paths and the
links page lists the Swagger link by the same rule. A render that disagrees either
publishes a page the engine does not serve, or serves one nobody can reach.

The engine's side is its deployed Application: dfe-extras writes the setting into
the dfe-engine-env ConfigMap the engine pod reads, with the posture the cluster
secret declares. dfe-extras' own posture check is read through its e2eServer
guard, which renders only on a dev posture, so the three posture lists are
compared by what they render rather than by reading templates.

    python3 scripts/tests/test_engine_docs_surface.py

Needs `helm` on PATH.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary
from _weave import render_extras, weave

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMMON = REPO_ROOT / "argocd" / "values" / "common.yaml"
ENGINE = "dfe-engine"
GATEWAY = chart_dir("envoy-gateway-config")
LINKS = chart_dir("links")
E2E_ANNOTATION = "dfe.hyperi.io/e2e_server"

DOMAIN = "dfe.example.test"
SWAGGER = "Engine API (Swagger)"
DOCS_PATHS = ("/docs", "/redoc")

# dfe-engine settings.py _NON_PROD_ENVS, compared after strip().lower().
DEV_POSTURES = ("dev", "development", "local", "test", "ci")
POSTURES = (*DEV_POSTURES, " Dev ", "production", "staging", "")
SETTINGS = ("", "true", "false")


def served(env: str, setting: str) -> bool:
    """The engine's rule, restated from its settings module."""
    if setting:
        return setting == "true"
    return env.strip().lower() in DEV_POSTURES


def helm(chart: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", chart.name, str(chart), "-f", str(COMMON), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def render(chart: Path, *args: str) -> list[dict]:
    out = helm(chart, *args)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart.name} {args}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def dials(env: str, setting: str) -> list[str]:
    # --set-string, because an env of "" or " Dev " must arrive as written.
    args = ["--set-string", f"env={env}"]
    if setting:
        args += ["--set", f"api.docsEnabled={setting}"]
    return args


def engine_render(env: str, setting: str, **options: object) -> list[dict]:
    """The engine's dfe-extras objects for a cluster secret's posture and a deployer's setting."""
    overlay = {"api": {"docsEnabled": setting}} if setting else None
    return render_extras(ENGINE, "slim", "local", env=env, overlay=overlay, **options)


def engine_docs_env(env: str, setting: str) -> str | None:
    docs = engine_render(env, setting)
    literal = next(
        d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == f"{ENGINE}-env"
    )["data"]
    return literal.get("DFE_API_DOCS_ENABLED")


def engine_refusal(env: str, setting: str, **options: object) -> str:
    """The engine render's refusal, or empty when it rendered."""
    try:
        engine_render(env, setting, **options)
    except weave().WeaveError as exc:
        return str(exc)
    return ""


def engine_chart_is_dev(env: str) -> bool:
    """dfe-extras' isDevPosture for the engine, observed through the e2eServer guard."""
    return engine_refusal(env, "", annotations={E2E_ANNOTATION: "true"}) == ""


def gateway_engine_paths(env: str, setting: str) -> list[str]:
    """Every path the engine's two internal routes match, the browser's and the CLI's."""
    docs = render(GATEWAY, *dials(env, setting), "--set", f"domain={DOMAIN}",
                  "--set", "appNamespace=dfe-local")
    routes = [
        d for d in docs
        if d.get("kind") == "HTTPRoute" and d["metadata"]["name"] in ("dfe-engine", "dfe-engine-cli")
    ]
    return [
        m["path"]["value"] for route in routes for rule in route["spec"]["rules"]
        for m in rule.get("matches", [])
    ]


def links_names(env: str, setting: str, *extra: str) -> list[str]:
    docs = render(LINKS, *dials(env, setting), *extra)
    page = next(d for d in docs if d.get("kind") == "ConfigMap")["data"]["index.html"]
    found = re.search(r'<script type="application/json" id="model">(.*?)</script>', page, re.S)
    if found is None:
        raise SystemExit("the links page carries no model")
    model = json.loads(found.group(1))
    return [link["name"] for group in model["groups"] for link in group["links"]]


def test_the_engine_chart_passes_the_setting_through_and_nothing_when_unset() -> None:
    for setting in SETTINGS:
        got = engine_docs_env("production", setting)
        want = setting or None
        expect(f"api.docsEnabled={setting!r} renders DFE_API_DOCS_ENABLED={want!r}",
               got == want, f"got {got!r}")


def test_the_engine_charts_posture_list_is_the_engines() -> None:
    for env in POSTURES:
        want = env.strip().lower() in DEV_POSTURES
        expect(f"the engine's deployment reads env={env!r} as dev={want}",
               engine_chart_is_dev(env) == want, "")


def test_the_gateway_routes_the_docs_exactly_where_the_engine_serves_them() -> None:
    for env in POSTURES:
        for setting in SETTINGS:
            paths = gateway_engine_paths(env, setting)
            want = served(env, setting)
            for path in DOCS_PATHS:
                expect(f"gateway env={env!r} docsEnabled={setting!r}: {path} routed={want}",
                       (path in paths) == want, f"got {paths}")
            expect(f"gateway env={env!r} docsEnabled={setting!r}: /openapi.json always routed",
                   "/openapi.json" in paths, f"got {paths}")


def test_the_links_page_lists_swagger_exactly_where_the_engine_serves_it() -> None:
    for env in POSTURES:
        for setting in SETTINGS:
            want = served(env, setting)
            for layout, extra in (("gateway", ("--set", f"domain={DOMAIN}")), ("port-forward", ())):
                names = links_names(env, setting, *extra)
                expect(f"links {layout} env={env!r} docsEnabled={setting!r}: listed={want}",
                       (SWAGGER in names) == want, f"got {names}")


def test_a_setting_the_three_could_read_differently_is_refused_by_each() -> None:
    err = engine_refusal("local", "yes")
    expect(f"{ENGINE} refuses api.docsEnabled=yes by name", "api.docsEnabled" in err, err[-300:])
    for chart in (GATEWAY, LINKS):
        out = helm(chart, "--set", "api.docsEnabled=yes", "--set", f"domain={DOMAIN}")
        expect(f"{chart.name} refuses api.docsEnabled=yes by name",
               out.returncode != 0 and "api.docsEnabled" in out.stderr, out.stderr[-300:])


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
