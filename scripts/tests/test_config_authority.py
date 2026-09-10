#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_config_authority.py
#  Purpose:      Prove a chart default never outranks the config the engine's
#                overlay authored, where the app resolves env above its file.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Who wins when the chart and the engine both name a setting.

The engine is the config authority: it publishes each app's settings as the
per-instance Argo `$values` overlay, and the chart writes that blob verbatim to
the file the app reads. Some apps resolve a flat env var ABOVE that file --
dfe-archiver applies `S3_BUCKET` / `S3_ENDPOINT` after loading the YAML
(crates/core/src/config.rs `apply_archive_env`, called from
`load_config`) -- so any value the chart puts in those env vars silently beats
the overlay. `s3.bucket: dfe-archive` was such a default, and every deployment
archived to that bucket whatever the operator set through the API.

The rule this pins: where the chart renders an env the app ranks above its
config file, the chart ships NO default for it. The key stays in values.yaml so
a deployment that owns its own destination can still set one, and setting it is
then a deliberate override rather than a shipped surprise.

Asserted from renders, not from reading the templates:

  chart defaults        -> no S3_ENDPOINT, no S3_BUCKET env at all
  overlay authors it    -> the value reaches archiver.yaml, with no env to shadow it
  deployment authors it -> the env renders, and the deployment wins

    python3 scripts/tests/test_config_authority.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"

# The archiver env vars that outrank the mounted config file, and the overlay
# key each one shadows. Both empty in values.yaml so the overlay wins.
SHADOWING_ENV = {
    "S3_BUCKET": "bucket",
    "S3_ENDPOINT": "endpoint",
}


def render(chart: str, template: str, *sets: str) -> list[dict]:
    cmd = ["helm", "template", chart, str(CHARTS / chart), "--show-only", template]
    for s in sets:
        cmd += ["--set-string", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart} {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def container_env(docs: list[dict]) -> dict[str, str]:
    container = docs[0]["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value", "") for e in container.get("env", [])}


def mounted_config(docs: list[dict]) -> dict:
    return yaml.safe_load(docs[0]["data"]["archiver.yaml"]) or {}


def test_the_archiver_ships_no_s3_env_at_chart_defaults() -> None:
    env = container_env(render("dfe-archiver", "templates/deployment.yaml"))
    for name in sorted(SHADOWING_ENV):
        expect(
            f"a default render carries no {name}",
            name not in env,
            f"got {name}={env.get(name)!r}, which would beat the overlay",
        )


def test_an_overlay_authored_destination_reaches_the_config_file() -> None:
    sets = [f"config.archive.s3.{key}=overlay-{key}" for key in SHADOWING_ENV.values()]
    written = mounted_config(render("dfe-archiver", "templates/configmap.yaml", *sets))
    s3 = written.get("archive", {}).get("s3", {})
    for key in sorted(SHADOWING_ENV.values()):
        expect(
            f"archive.s3.{key} is written verbatim to archiver.yaml",
            s3.get(key) == f"overlay-{key}",
            f"got {s3.get(key)!r}",
        )


def test_an_overlay_authored_destination_has_no_env_shadowing_it() -> None:
    """The overlay sets `config`, never `s3`, so nothing appears to override it."""
    sets = [f"config.archive.s3.{key}=overlay-{key}" for key in SHADOWING_ENV.values()]
    env = container_env(render("dfe-archiver", "templates/deployment.yaml", *sets))
    for name in sorted(SHADOWING_ENV):
        expect(
            f"an overlay-authored destination renders no {name}",
            name not in env,
            f"got {name}={env.get(name)!r}",
        )


def test_a_deployment_that_names_its_own_destination_still_wins() -> None:
    """Emptying the defaults must not remove the dial -- an operator running an
    in-cluster object store sets it in their own values and expects it applied."""
    sets = [f"s3.{key}=deployment-{key}" for key in SHADOWING_ENV.values()]
    env = container_env(render("dfe-archiver", "templates/deployment.yaml", *sets))
    for name, key in sorted(SHADOWING_ENV.items()):
        expect(
            f"a deployment-set s3.{key} renders {name}",
            env.get(name) == f"deployment-{key}",
            f"got {env.get(name)!r}",
        )


def main() -> int:
    with standalone():
        test_the_archiver_ships_no_s3_env_at_chart_defaults()
        test_an_overlay_authored_destination_reaches_the_config_file()
        test_an_overlay_authored_destination_has_no_env_shadowing_it()
        test_a_deployment_that_names_its_own_destination_still_wins()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
