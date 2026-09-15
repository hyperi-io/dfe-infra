#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_toolbox_pod_dial.py
#  Purpose:      Prove toolbox.pod.* reaches the in-cluster chart end to end,
#                from the dial through to a real Helm render.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The toolbox pod's dial facts, end to end.

deployment.yaml's toolbox.pod.enabled/kubeApiAccess/ttlSeconds ARE the chart's
own values.yaml fields (helm/charts/dfe-toolbox/templates/_validate.tpl), not a
render_dial.py translation -- so the whole path has to carry them through
unchanged: render_dial.py's DFE_TOOLBOX_POD_* env keys, bootstrap.sh's defaults,
the cluster-secret annotations, and argocd/appsets/layer2-platform.yaml's own
parameters, which is the same shape retention.defaultTtlDays already uses in
layer2-apps.yaml (test_default_ttl.py is the sibling test this one mirrors).

    python3 scripts/tests/test_toolbox_pod_dial.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "dfe-toolbox"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
DIAL_EXAMPLE = REPO_ROOT / "deployment.example.yaml"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-platform.yaml"

# (chart field, DFE_* env key, cluster-secret annotation, bootstrap default)
FIELDS = (
    ("enabled", "DFE_TOOLBOX_POD_ENABLED", "dfe.hyperi.io/toolbox_pod_enabled", "false"),
    (
        "kubeApiAccess",
        "DFE_TOOLBOX_POD_KUBE_API_ACCESS",
        "dfe.hyperi.io/toolbox_pod_kube_api_access",
        "false",
    ),
    ("ttlSeconds", "DFE_TOOLBOX_POD_TTL_SECONDS", "dfe.hyperi.io/toolbox_pod_ttl_seconds", ""),
)

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import render_dial  # noqa: E402


def render(*sets: str) -> list[dict]:
    cmd = ["helm", "template", "dfe-toolbox", str(CHART), "--set", "global.registry=ghcr.io/example"]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


# --- the cluster secret + bootstrap defaults ------------------------------------
def test_the_cluster_secret_carries_every_annotation() -> None:
    body = CLUSTER_SECRET.read_text(encoding="utf-8")
    for _field, env_key, annotation, _default in FIELDS:
        expect(
            f"{annotation} is set from ${{{env_key}}}",
            f'{annotation}: "${{{env_key}}}"' in body,
            "annotation missing from the cluster-secret template",
        )


def test_bootstrap_defaults_every_key() -> None:
    body = BOOTSTRAP.read_text(encoding="utf-8")
    for _field, env_key, _annotation, default in FIELDS:
        expect(
            f"bootstrap.sh defaults {env_key} to {default!r} so the annotation always renders",
            f'export {env_key}="${{{env_key}:-{default}}}"' in body,
            "the default is missing",
        )


# --- the appset ------------------------------------------------------------------
def test_the_appset_passes_every_field_as_a_parameter() -> None:
    appset = yaml.safe_load(APPSET.read_text(encoding="utf-8"))
    params = {}
    force_string = {}
    for source in appset["spec"]["template"]["spec"]["sources"]:
        for entry in source.get("helm", {}).get("parameters", []):
            params[entry["name"]] = entry["value"]
            force_string[entry["name"]] = entry.get("forceString", False)

    for field, _env_key, annotation, default in FIELDS:
        name = f"toolbox.pod.{field}"
        expect(f"the appset sets {name}", name in params, f"got {sorted(params)}")
        expect(
            f"{name} reads it from {annotation}",
            annotation in params.get(name, ""),
            f"got {params.get(name)}",
        )
        expect(
            f"{name} falls back to {default!r} on a cluster secret without it",
            f'default "{default}"' in params.get(name, ""),
            f"got {params.get(name)}",
        )

    expect(
        "ttlSeconds is forced to a string -- a numeric TTL must never silently become an int",
        force_string["toolbox.pod.ttlSeconds"] is True,
        f"got {force_string['toolbox.pod.ttlSeconds']}",
    )
    expect(
        "enabled is left to Helm's own type inference, so a bare true/false becomes a real bool",
        force_string["toolbox.pod.enabled"] is False,
        f"got {force_string['toolbox.pod.enabled']}",
    )
    expect(
        "kubeApiAccess is left to Helm's own type inference too",
        force_string["toolbox.pod.kubeApiAccess"] is False,
        f"got {force_string['toolbox.pod.kubeApiAccess']}",
    )


# --- the dial ----------------------------------------------------------------
def test_the_dial_renders_every_key() -> None:
    dial = render_dial._parse_yaml_subset(DIAL_EXAMPLE.read_text(encoding="utf-8"))
    updates = render_dial._env_updates(dial)
    expect(
        "deployment.example.yaml's toolbox.pod.enabled lands in DFE_TOOLBOX_POD_ENABLED",
        updates.get("DFE_TOOLBOX_POD_ENABLED") == "false",
        f"got {updates.get('DFE_TOOLBOX_POD_ENABLED')}",
    )
    turned_on = render_dial._env_updates(
        {"toolbox": {"pod": {"enabled": "true", "kubeApiAccess": "true", "ttlSeconds": "3600"}}}
    )
    expect(
        "a dial turning the pod on renders true",
        turned_on.get("DFE_TOOLBOX_POD_ENABLED") == "true",
        f"got {turned_on.get('DFE_TOOLBOX_POD_ENABLED')}",
    )
    expect(
        "and carries kubeApiAccess through unchanged",
        turned_on.get("DFE_TOOLBOX_POD_KUBE_API_ACCESS") == "true",
        f"got {turned_on.get('DFE_TOOLBOX_POD_KUBE_API_ACCESS')}",
    )
    expect(
        "and ttlSeconds through unchanged, still a string",
        turned_on.get("DFE_TOOLBOX_POD_TTL_SECONDS") == "3600",
        f"got {turned_on.get('DFE_TOOLBOX_POD_TTL_SECONDS')}",
    )


# --- a real Helm render, the way Argo CD would actually call it ------------------
def test_the_params_argocd_would_pass_render_a_real_bool_and_string() -> None:
    """`--set toolbox.pod.enabled=true` (no --set-string) is what a bare,
    un-forced parameter value becomes -- Helm's own strvals parser reads an
    unquoted true/false as a real YAML boolean, which the chart's own
    validate.yaml requires (helm/charts/dfe-toolbox/templates/_validate.tpl)."""
    docs = render(
        "toolbox.pod.enabled=true",
        "toolbox.pod.kubeApiAccess=true",
    )
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    expect("enabled=true renders replicas: 1", deployment["spec"]["replicas"] == 1, "did not render 1")
    service_accounts = [d for d in docs if d["kind"] == "ServiceAccount"]
    expect(
        "kubeApiAccess=true renders a dedicated ServiceAccount",
        len(service_accounts) == 1,
        f"got {len(service_accounts)}",
    )


def test_a_force_stringed_numeric_ttl_still_renders_the_cronjob() -> None:
    """The forceString parameter Argo CD actually sends is --set-string, not
    --set -- confirm the chart still gates the CronJob on it correctly."""
    cmd = [
        "helm",
        "template",
        "dfe-toolbox",
        str(CHART),
        "--set",
        "global.registry=ghcr.io/example",
        "--set",
        "toolbox.pod.enabled=true",
        "--set-string",
        "toolbox.pod.ttlSeconds=3600",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed:\n{out.stderr}")
    docs = [d for d in yaml.safe_load_all(out.stdout) if d]
    cronjobs = [d for d in docs if d["kind"] == "CronJob"]
    expect("ttlSeconds set renders the TTL CronJob", len(cronjobs) == 1, f"got {len(cronjobs)}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
