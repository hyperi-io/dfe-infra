#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_hunt_runner_clickhouse.py
#  Purpose:      Prove the hunt runner dials ClickHouse as its own user, on a password
#                the engine is handed from the same Secret.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for huntRunner.clickhouse in the dfe-engine chart.

The runner's worker runs INSERT ... SELECT built from rule text, so the user it
connects as decides what a crafted rule can reach. The engine creates
dfe_hunt_runner, and a password it minted would sit in a store the runner's pod
cannot read, so the chart supplies one Secret and both pods read it. What has to
hold, and what each check reads off the render:

1. The runner dials as dfe_hunt_runner on that Secret, never the admin account.
2. The engine is handed the same Secret and key, so the two cannot disagree.
3. The engine and the keda shim keep the admin account.
4. The Secret is minted once by ESO (CreatedOnce), not by the template.
5. A pinned password renders a plain Secret; passwordSecretCreate=false renders
   nothing for the deployment to collide with.
6. huntRunner.enabled=false renders neither the Secret nor the engine's reference
   to it, so no pod waits on a Secret nothing makes.
7. Clearing the user or the Secret name fails the render instead of falling back.

    python3 scripts/tests/test_hunt_runner_clickhouse.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "dfe-engine"
REGISTRY = "ghcr.io/hyperi-io"
# What argocd/values/common.yaml sets for every deployment: the admin account.
ADMIN = ("clickhouse.user=default", "clickhouse.passwordSecretName=clickhouse-admin-password")
SECRET = "hunt-runner-clickhouse"
KEY = "password"


def _helm(*sets: str) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "dfe-engine", str(CHART), "--set", f"global.registry={REGISTRY}"]
    for s in sets:
        cmd += ["--set", s]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def render(*sets: str) -> list[dict]:
    out = _helm(*sets)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def refused(*sets: str) -> str:
    """The stderr of a render expected to fail, or "" when it rendered."""
    out = _helm(*sets)
    return out.stderr if out.returncode != 0 else ""


def named(docs: list[dict], kind: str, name: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind and d["metadata"]["name"] == name]


def env(docs: list[dict], deployment: str, container: str) -> dict[str, dict]:
    """name -> env entry, for one container of one Deployment."""
    found = named(docs, "Deployment", deployment)
    if not found:
        return {}
    for c in found[0]["spec"]["template"]["spec"]["containers"]:
        if c["name"] == container:
            return {e["name"]: e for e in c.get("env") or []}
    return {}


def secret_ref(entry: dict | None) -> tuple[str, str] | None:
    ref = ((entry or {}).get("valueFrom") or {}).get("secretKeyRef")
    return (ref["name"], ref["key"]) if ref else None


def test_the_runner_dials_as_its_own_user() -> None:
    runner = env(render(*ADMIN), "dfe-hunt-runner", "hunt-runner")
    expect(
        "the runner's username is dfe_hunt_runner",
        (runner.get("DFE_CLICKHOUSE_USERNAME") or {}).get("value") == "dfe_hunt_runner",
        f"{runner.get('DFE_CLICKHOUSE_USERNAME')}",
    )
    expect(
        "its password comes from the runner's Secret, not the admin one",
        secret_ref(runner.get("DFE_CLICKHOUSE_PASSWORD")) == (SECRET, KEY),
        f"{runner.get('DFE_CLICKHOUSE_PASSWORD')}",
    )


def test_the_engine_is_handed_the_same_secret() -> None:
    engine = env(render(*ADMIN), "dfe-engine", "engine")
    expect(
        "the engine reads the runner's password off the same Secret and key",
        secret_ref(engine.get("DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD")) == (SECRET, KEY),
        f"{engine.get('DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD')}",
    )


def test_the_engine_and_the_shim_keep_the_admin_account() -> None:
    docs = render(*ADMIN)
    for deployment, container in (("dfe-engine", "engine"), ("dfe-keda-shim", "keda-shim")):
        found = env(docs, deployment, container)
        expect(
            f"{deployment} still dials as default",
            (found.get("DFE_CLICKHOUSE_USERNAME") or {}).get("value") == "default"
            and secret_ref(found.get("DFE_CLICKHOUSE_PASSWORD"))
            == ("clickhouse-admin-password", "password"),
            f"{found.get('DFE_CLICKHOUSE_USERNAME')} {found.get('DFE_CLICKHOUSE_PASSWORD')}",
        )


def test_the_password_is_minted_once_by_eso() -> None:
    docs = render()
    gens = named(docs, "Password", f"{SECRET}-gen")
    es = named(docs, "ExternalSecret", SECRET)
    expect("the default renders the generator", len(gens) == 1, f"got {len(gens)}")
    expect("and the ExternalSecret", len(es) == 1, f"got {len(es)}")
    expect("and no template-minted Secret", named(docs, "Secret", SECRET) == [], "a Secret rendered")
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
    docs = render("huntRunner.clickhouse.password=pinned-runner-pw")
    plain = named(docs, "Secret", SECRET)
    expect("one plain Secret", len(plain) == 1, f"got {len(plain)}")
    expect("and no generator", named(docs, "Password", f"{SECRET}-gen") == [], "a generator rendered")
    if plain:
        expect(
            "carrying the pinned value",
            plain[0]["stringData"].get(KEY) == "pinned-runner-pw",
            f"{plain[0]['stringData']}",
        )


def test_creation_can_be_handed_to_the_deployment() -> None:
    docs = render("huntRunner.clickhouse.passwordSecretCreate=false")
    for kind, name in (("Password", f"{SECRET}-gen"), ("ExternalSecret", SECRET), ("Secret", SECRET)):
        expect(f"passwordSecretCreate=false renders no {kind}", named(docs, kind, name) == [], f"a {kind}")
    engine = env(docs, "dfe-engine", "engine")
    expect(
        "both pods still read the deployment's Secret",
        secret_ref(engine.get("DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD")) == (SECRET, KEY),
        f"{engine.get('DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD')}",
    )


def test_no_runner_means_no_secret_and_no_reference() -> None:
    docs = render("huntRunner.enabled=false")
    for kind, name in (("Password", f"{SECRET}-gen"), ("ExternalSecret", SECRET), ("Secret", SECRET)):
        expect(f"huntRunner.enabled=false renders no {kind}", named(docs, kind, name) == [], f"a {kind}")
    engine = env(docs, "dfe-engine", "engine")
    expect(
        "and the engine does not wait on it",
        "DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD" not in engine,
        f"{engine.get('DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD')}",
    )


def test_a_cleared_identity_fails_the_render() -> None:
    for cleared in ("huntRunner.clickhouse.user=", "huntRunner.clickhouse.passwordSecretName="):
        expect(
            f"{cleared} is refused rather than falling back to the admin account",
            "never the admin account" in refused(cleared),
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
