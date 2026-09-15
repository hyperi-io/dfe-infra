#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_argo_sync_blockers.py
#  Purpose:      Hold the two renders that the API server REJECTS outright, so
#                an Application cannot fail to sync at all.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Renders the API server refuses, which stop a sync rather than degrade it.

Both of these are worse than a wrong value. An object the API server rejects
takes the WHOLE Application down with it: nothing that Application manages is
applied, so a rejected Deployment costs the UI and a rejected NetworkPolicy
costs every policy in the deployment, not just the one that was wrong.

    python3 scripts/tests/test_argo_sync_blockers.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
APPSETS = REPO_ROOT / "argocd" / "appsets"

FILLED_OIDC = (
    "oidc.enabled=true",
    "oidc.issuerUri=https://idp.example.com",
    "oidc.clientId=dfe-kafbat",
    "oidc.clientSecretName=dfe-kafbat-oidc",
)


def render(chart: str, *sets: str) -> list[dict]:
    cmd = [
        "helm", "template", "t", str(chart_dir(chart)),
        "-f", str(VALUES / "common.yaml"),
        "-f", str(VALUES / "aws.yaml"),
    ]
    for entry in sets:
        cmd += ["--set", entry]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template {chart} failed:\n{out.stderr}")
    return [doc for doc in yaml.safe_load_all(out.stdout) if isinstance(doc, dict)]


def secret_refs(docs: list[dict]) -> list[tuple[str, str]]:
    """(env var, secret name) for every secretKeyRef in every container."""
    refs = []
    for doc in docs:
        template = (doc.get("spec") or {}).get("template") or {}
        for container in (template.get("spec") or {}).get("containers") or []:
            for env in container.get("env") or []:
                ref = ((env.get("valueFrom") or {}).get("secretKeyRef") or {})
                if ref:
                    refs.append((env["name"], ref.get("name", "")))
    return refs


def test_kafbat_names_no_empty_secret_when_the_oidc_block_is_bare() -> None:
    """The cloud overlays turn oidc on for the whole deployment, so a
    deployment with no kafbat provider yet reached the API server with an empty
    secretKeyRef name, which it refuses."""
    empty = [name for name, secret in secret_refs(render("kafbat")) if not secret]
    expect("no env var references an unnamed secret", empty == [], f"empty for {empty}")


def test_kafbat_takes_the_oidc_secret_once_the_block_is_filled() -> None:
    """The degrade must not become a ceiling -- a filled block still gets
    OAUTH2 and the secret the deployer named."""
    refs = dict(secret_refs(render("kafbat", *FILLED_OIDC)))
    expect("the client secret is taken from the named secret",
           refs.get("OAUTH_CLIENT_SECRET") == "dfe-kafbat-oidc", f"got {refs}")


def test_the_platform_appset_sets_the_namespace_the_policies_target() -> None:
    """network-policies reads dfeNamespaces, never appNamespace, and a policy
    aimed at an absent namespace is rejected -- which leaves the cluster with
    NO policies rather than with wrong ones."""
    lines = (APPSETS / "layer2-platform.yaml").read_text(encoding="utf-8").splitlines()
    read = '.metadata.annotations "dfe.hyperi.io/dfe_namespace"'
    paired = [
        i for i, ln in enumerate(lines)
        if read in ln and i and "name: dfeNamespaces[0]" in lines[i - 1]
    ]
    expect("layer2-platform lands dfe_namespace on dfeNamespaces[0]", paired != [],
           "the chart would keep its own default namespace")


def test_both_layer2_appsets_state_the_deployments_posture() -> None:
    """Every chart labels what it renders from env and cloud, and the chart
    defaults name a local deployment."""
    for appset in ("layer2-data.yaml", "layer2-platform.yaml"):
        body = (APPSETS / appset).read_text(encoding="utf-8")
        for key, annotation in (("env", "dfe.hyperi.io/env"), ("cloud", "dfe.hyperi.io/cloud")):
            lines = body.splitlines()
            paired = [
                i for i, ln in enumerate(lines)
                if f'.metadata.annotations "{annotation}"' in ln
                and i and f"name: {key}" in lines[i - 1]
            ]
            expect(f"{appset} passes {key} from {annotation}", paired != [],
                   f"{key} stays at the chart default")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
