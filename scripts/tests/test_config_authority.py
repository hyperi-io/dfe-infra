#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_config_authority.py
#  Purpose:      Prove a deployment default never outranks the config the
#                engine's overlay authored, where the app resolves env above its file.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Who wins when the deployment and the engine both name a setting.

The engine is the config authority: it publishes each app's settings as the
per-instance Argo `$values` overlay, and the thin chart writes that blob verbatim
to the file the app reads. Some apps resolve a flat env var ABOVE that file --
dfe-archiver applies `S3_BUCKET` / `S3_ENDPOINT` after loading the YAML
(crates/core/src/config.rs `apply_archive_env`, called from `load_config`) -- so
any value the deployment puts in those env vars silently beats the overlay.
`s3.bucket: dfe-archive` was such a default, and every deployment archived to
that bucket whatever the operator set through the API.

The rule this pins: where the deployment renders an env the app ranks above its
config file, it ships NO value for it. The app reads the names through scalo's
flat env, which takes an empty value as unset (crates/core/src/config.rs:690 and
:706), so the names may render empty. The `s3.*` keys stay in
argocd/values/apps/dfe-archiver/values.yaml so a deployment that owns its own
destination can still set one, and setting it is then a deliberate override
rather than a shipped surprise.

Asserted from the goldens under fixtures/thin-renders and from live renders:

  every default render  -> no S3_ENDPOINT, no S3_BUCKET value at all
  overlay authors it    -> the value reaches archiver.yaml, with no env to shadow it
  deployment authors it -> the env renders, and the deployment wins

    python3 scripts/tests/test_config_authority.py

The live renders need `helm` on PATH and the scalo-service library (_weave.library).
"""

import sys

import pytest
import yaml

from _expect import expect, standalone, summary
from _gate import effective_env, goldens, object_id
from _weave import render_app

ARCHIVER = "dfe-archiver"
DIAL_CELL = ("single", "local")

# The archiver env vars that outrank the mounted config file, and the overlay
# key each one shadows.
SHADOWING_ENV = {
    "S3_BUCKET": "bucket",
    "S3_ENDPOINT": "endpoint",
}

ARCHIVER_S3_REFS = {
    "S3_ACCESS_KEY_ID": "access_key_id",
    "S3_SECRET_ACCESS_KEY": "secret_access_key",
}


def env_of(docs: list[dict]) -> dict[str, object]:
    return effective_env(docs, f"Deployment/{ARCHIVER}")


def mounted_config(docs: list[dict]) -> dict:
    configmap = next(d for d in docs if object_id(d) == f"ConfigMap/{ARCHIVER}-config")
    return yaml.safe_load(configmap["data"]["archiver.yaml"]) or {}


def env_refs(docs: list[dict]) -> dict[str, dict]:
    """Each env entry the archiver takes from a Secret, by name."""
    refs = {}
    for name, value in env_of(docs).items():
        if isinstance(value, dict) and "secretKeyRef" in value.get("valueFrom", {}):
            refs[name] = value["valueFrom"]["secretKeyRef"]
    return refs


def overlay_destination() -> dict:
    s3 = {key: f"overlay-{key}" for key in SHADOWING_ENV.values()}
    return {"config": {"archive": {"s3": s3}}}


def test_the_archiver_ships_no_s3_env_at_chart_defaults() -> None:
    for where, docs in goldens(ARCHIVER):
        env = env_of(docs)
        for name in sorted(SHADOWING_ENV):
            expect(
                f"a default render carries no {name} value [{'/'.join(where[1:])}]",
                env.get(name) in (None, ""),
                f"got {name}={env.get(name)!r}, which would beat the overlay",
            )


def test_an_overlay_authored_destination_reaches_the_config_file() -> None:
    written = mounted_config(render_app(ARCHIVER, *DIAL_CELL, overlay=overlay_destination()))
    s3 = written.get("archive", {}).get("s3", {})
    for key in sorted(SHADOWING_ENV.values()):
        expect(
            f"archive.s3.{key} is written verbatim to archiver.yaml",
            s3.get(key) == f"overlay-{key}",
            f"got {s3.get(key)!r}",
        )


def test_an_overlay_authored_destination_has_no_env_shadowing_it() -> None:
    """The overlay sets `config`, never `s3`, so nothing appears to override it."""
    env = env_of(render_app(ARCHIVER, *DIAL_CELL, overlay=overlay_destination()))
    for name in sorted(SHADOWING_ENV):
        expect(
            f"an overlay-authored destination renders no {name} value",
            env.get(name) in (None, ""),
            f"got {name}={env.get(name)!r}",
        )


def test_a_deployment_that_names_its_own_destination_still_wins() -> None:
    """Emptying the defaults must not remove the dial -- an operator running an
    in-cluster object store sets it in their own values and expects it applied."""
    overlay = {"s3": {key: f"deployment-{key}" for key in SHADOWING_ENV.values()}}
    env = env_of(render_app(ARCHIVER, *DIAL_CELL, overlay=overlay))
    for name, key in sorted(SHADOWING_ENV.items()):
        expect(
            f"a deployment-set s3.{key} renders {name}",
            env.get(name) == f"deployment-{key}",
            f"got {env.get(name)!r}",
        )


def test_the_archiver_reads_static_keys_from_its_s3_secret() -> None:
    """An optional ref: an absent Secret leaves the SDK's credential chain alone."""
    for where, docs in goldens(ARCHIVER):
        refs = env_refs(docs)
        for name, field in sorted(ARCHIVER_S3_REFS.items()):
            ref = refs.get(name, {})
            expect(
                f"{name} comes from dfe-archiver-s3/{field}, optional [{'/'.join(where[1:])}]",
                ref.get("name") == "dfe-archiver-s3"
                and ref.get("key") == field
                and ref.get("optional") is True,
                "missing, pointed elsewhere, or not optional",
            )


def test_the_s3_group_switched_off_renders_no_static_keys() -> None:
    """The contract's s3 group switched off reads no static keys at all."""
    overlay = {"secrets": {"s3": {"enabled": False}}}
    refs = env_refs(render_app(ARCHIVER, *DIAL_CELL, overlay=overlay))
    for name in sorted(ARCHIVER_S3_REFS):
        expect(f"secrets.s3.enabled=false renders no {name}", name not in refs, "still rendered")


@pytest.mark.xfail(
    strict=True,
    reason="secrets.s3.existingSecret tpl-renders an empty s3.secretName to an unnamed Secret",
)
def test_a_cleared_s3_secret_name_renders_no_unnamed_reference() -> None:
    refs = env_refs(render_app(ARCHIVER, *DIAL_CELL, overlay={"s3": {"secretName": ""}}))
    unnamed = sorted(name for name, ref in refs.items() if not ref.get("name"))
    expect("an empty s3.secretName names no empty Secret", not unnamed, f"got {unnamed}")


def main() -> int:
    with standalone():
        test_the_archiver_ships_no_s3_env_at_chart_defaults()
        test_an_overlay_authored_destination_reaches_the_config_file()
        test_an_overlay_authored_destination_has_no_env_shadowing_it()
        test_a_deployment_that_names_its_own_destination_still_wins()
        test_the_archiver_reads_static_keys_from_its_s3_secret()
        test_the_s3_group_switched_off_renders_no_static_keys()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
