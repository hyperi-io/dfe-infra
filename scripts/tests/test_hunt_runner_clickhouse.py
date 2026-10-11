#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_hunt_runner_clickhouse.py
#  Purpose:      Prove the hunt runner dials ClickHouse as its own user, on a password
#                the engine is handed from the same Secret.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for huntRunner.clickhouse on the deployed dfe-engine Application.

The runner's worker runs INSERT ... SELECT built from rule text, so the user it
connects as decides what a crafted rule can reach. The engine creates
dfe_hunt_runner, and a password it minted would sit in a store the runner's pod
cannot read, so the deployment supplies one Secret and both pods read it.
dfe-extras renders the runner, the keda shim and that Secret; the engine's thin
chart reads it through the contract's hunt-runner secret group. What has to hold,
and what each check reads off the render:

1. The runner dials as dfe_hunt_runner on that Secret, never the admin account.
2. The engine is handed the same Secret and key, so the two cannot disagree.
3. The engine and the keda shim keep the admin account common.yaml names.
4. The Secret is minted once by ESO (CreatedOnce), not by the template.
5. A pinned password renders a plain Secret; passwordSecretCreate=false renders
   nothing for the deployment to collide with.
6. A deployment without the runner renders neither the Secret nor the engine's
   reference to it, so no pod waits on a Secret nothing makes.
7. Clearing the user or the Secret name fails the render instead of falling back.

    python3 scripts/tests/test_hunt_runner_clickhouse.py

Needs `helm` on PATH; the engine-side cases render the thin chart, which needs
the scalo-service library (_weave.library).
"""

import sys

import yaml

from _expect import expect, standalone, summary
from _gate import CLOUDS, PROFILES, effective_env, golden
from _weave import REPO_ROOT, render_app, render_extras, weave

ENGINE = "dfe-engine"
COMMON = yaml.safe_load((REPO_ROOT / "argocd" / "values" / "common.yaml").read_text("utf-8"))
# The admin account common.yaml hands every deployment.
ADMIN = COMMON["clickhouse"]
ADMIN_USER = ADMIN["user"]
ADMIN_SECRET = (ADMIN["passwordSecretName"], ADMIN["passwordSecretKey"])
SECRET = "hunt-runner-clickhouse"
KEY = "password"
RUNNER_USER = "dfe_hunt_runner"
# The instance values that leave a deployment without the runner: the runner and its
# Secret, and the engine's reference through the contract's hunt-runner group.
NO_RUNNER = {"huntRunner": {"enabled": False}, "secrets": {"hunt-runner": {"enabled": False}}}
MINTED = (("Password", f"{SECRET}-gen"), ("ExternalSecret", SECRET), ("Secret", SECRET))


def refused(**options: object) -> str:
    """The message of a dfe-extras render expected to fail, or "" when it rendered."""
    try:
        render_extras(ENGINE, "slim", "local", **options)
    except weave().WeaveError as exc:
        return str(exc)
    return ""


def named(docs: list[dict], kind: str, name: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind and d["metadata"]["name"] == name]


def env(docs: list[dict], deployment: str) -> dict[str, object]:
    """The main container's effective env of one Deployment, or {} when none renders."""
    if not named(docs, "Deployment", deployment):
        return {}
    return effective_env(docs, f"Deployment/{deployment}")


def secret_ref(entry: object) -> tuple[str, str] | None:
    ref = ((entry if isinstance(entry, dict) else {}).get("valueFrom") or {}).get("secretKeyRef")
    return (ref["name"], ref["key"]) if ref else None


def cells() -> list[tuple[str, str]]:
    return [(p, k) for p in PROFILES for k in CLOUDS]


def test_the_runner_dials_as_its_own_user() -> None:
    for profile, cloud in cells():
        runner = env(golden(ENGINE, profile, cloud), "dfe-hunt-runner")
        expect(
            f"{profile}/{cloud}: the runner's username is {RUNNER_USER}",
            runner.get("DFE_CLICKHOUSE_USERNAME") == RUNNER_USER,
            f"{runner.get('DFE_CLICKHOUSE_USERNAME')}",
        )
        expect(
            f"{profile}/{cloud}: its password comes from the runner's Secret, not the admin one",
            secret_ref(runner.get("DFE_CLICKHOUSE_PASSWORD")) == (SECRET, KEY),
            f"{runner.get('DFE_CLICKHOUSE_PASSWORD')}",
        )


def test_the_engine_is_handed_the_same_secret() -> None:
    for profile, cloud in cells():
        docs = golden(ENGINE, profile, cloud)
        engine = env(docs, ENGINE)
        runner = env(docs, "dfe-hunt-runner")
        expect(
            f"{profile}/{cloud}: the engine reads the runner's password off the same Secret",
            secret_ref(engine.get("DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD"))
            == secret_ref(runner.get("DFE_CLICKHOUSE_PASSWORD"))
            == (SECRET, KEY),
            f"engine {engine.get('DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD')} "
            f"runner {runner.get('DFE_CLICKHOUSE_PASSWORD')}",
        )


def test_the_engine_and_the_shim_keep_the_admin_account() -> None:
    for profile, cloud in cells():
        docs = golden(ENGINE, profile, cloud)
        for deployment in (ENGINE, "dfe-keda-shim"):
            found = env(docs, deployment)
            expect(
                f"{profile}/{cloud}: {deployment} still dials as {ADMIN_USER}",
                found.get("DFE_CLICKHOUSE_USERNAME") == ADMIN_USER
                and secret_ref(found.get("DFE_CLICKHOUSE_PASSWORD")) == ADMIN_SECRET,
                f"{found.get('DFE_CLICKHOUSE_USERNAME')} {found.get('DFE_CLICKHOUSE_PASSWORD')}",
            )


def test_the_password_is_minted_once_by_eso() -> None:
    docs = golden(ENGINE, "slim", "local")
    gens = named(docs, "Password", f"{SECRET}-gen")
    es = named(docs, "ExternalSecret", SECRET)
    expect("the default renders the generator", len(gens) == 1, f"got {len(gens)}")
    expect("and the ExternalSecret", len(es) == 1, f"got {len(es)}")
    expect(
        "and no template-minted Secret", named(docs, "Secret", SECRET) == [], "a Secret rendered"
    )
    if gens:
        spec = gens[0]["spec"]
        expect(
            "alphanumeric, at the 24-character credential length",
            spec.get("symbols") == 0 and spec.get("length") == 24,
            f"{spec}",
        )
    if es:
        spec = es[0]["spec"]
        expect(
            "written once and never refreshed",
            spec.get("refreshPolicy") == "CreatedOnce" and str(spec.get("refreshInterval")) == "0",
            f"{spec.get('refreshPolicy')} {spec.get('refreshInterval')}",
        )
        expect(
            "under the key both pods read",
            KEY in spec["target"]["template"]["data"],
            f"{spec['target']}",
        )


def test_a_pinned_password_renders_that_password_alone() -> None:
    overlay = {"huntRunner": {"clickhouse": {"password": "pinned-runner-pw"}}}
    docs = render_extras(ENGINE, "slim", "local", overlay=overlay)
    plain = named(docs, "Secret", SECRET)
    expect("one plain Secret", len(plain) == 1, f"got {len(plain)}")
    expect(
        "and no generator", named(docs, "Password", f"{SECRET}-gen") == [], "a generator rendered"
    )
    if plain:
        expect(
            "carrying the pinned value",
            plain[0]["stringData"].get(KEY) == "pinned-runner-pw",
            f"{plain[0]['stringData']}",
        )


def test_creation_can_be_handed_to_the_deployment() -> None:
    overlay = {"huntRunner": {"clickhouse": {"passwordSecretCreate": False}}}
    docs = render_app(ENGINE, "slim", "local", overlay=overlay)
    for kind, name in MINTED:
        expect(
            f"passwordSecretCreate=false renders no {kind}",
            named(docs, kind, name) == [],
            f"a {kind}",
        )
    engine = env(docs, ENGINE)
    runner = env(docs, "dfe-hunt-runner")
    expect(
        "both pods still read the deployment's Secret",
        secret_ref(engine.get("DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD"))
        == secret_ref(runner.get("DFE_CLICKHOUSE_PASSWORD"))
        == (SECRET, KEY),
        f"engine {engine.get('DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD')} "
        f"runner {runner.get('DFE_CLICKHOUSE_PASSWORD')}",
    )


def test_no_runner_means_no_secret_and_no_reference() -> None:
    docs = render_app(ENGINE, "slim", "local", overlay=NO_RUNNER)
    for kind, name in MINTED:
        expect(
            f"a deployment without the runner renders no {kind}",
            named(docs, kind, name) == [],
            f"a {kind}",
        )
    expect(
        "nor the runner itself",
        named(docs, "Deployment", "dfe-hunt-runner") == [],
        "a dfe-hunt-runner Deployment rendered",
    )
    engine = env(docs, ENGINE)
    expect(
        "and the engine does not wait on it",
        "DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD" not in engine,
        f"{engine.get('DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD')}",
    )


def test_a_cleared_identity_fails_the_render() -> None:
    for field in ("user", "passwordSecretName"):
        overlay = {"huntRunner": {"clickhouse": {field: ""}}}
        expect(
            f"huntRunner.clickhouse.{field}= is refused rather than falling back to the admin user",
            "never the admin account" in refused(overlay=overlay),
            "the render succeeded",
        )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
