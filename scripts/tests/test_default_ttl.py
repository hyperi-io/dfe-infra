#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_default_ttl.py
#  Purpose:      Prove the deployment-wide retention reaches the engine pod,
#                from the env file through to the chart.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The default TTL knob, end to end.

DFE_CLICKHOUSE_DEFAULT_TTL_DAYS is the TTL every time-series table takes unless a
source or a dfe-schemas definition declares its own. ONE workload applies it:
dfe-engine, which is the only thing that applies schema at all. The value still
has to travel env file -> cluster secret -> appset -> chart, or a deployment's
retention silently stays at the shipped default.

    python3 scripts/tests/test_default_ttl.py

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
ENGINE = CHARTS / "dfe-engine"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
ENV_EXAMPLE = REPO_ROOT / "bootstrap" / "local.env.example"
DIAL_EXAMPLE = REPO_ROOT / "deployment.example.yaml"
APPSETS = REPO_ROOT / "argocd" / "appsets"

ENV_KEY = "DFE_CLICKHOUSE_DEFAULT_TTL_DAYS"
ANNOTATION = "dfe.hyperi.io/clickhouse_default_ttl_days"
PARAM = "retention.defaultTtlDays"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import render_dial  # noqa: E402


def render(chart: Path, *sets: str) -> list[dict]:
    cmd = ["helm", "template", chart.name, str(chart), "--set", "global.registry=ghcr.io/example"]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart.name} {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def container_env(docs: list[dict], kind: str, container: str) -> dict[str, str]:
    """name -> literal value for one container's env, across the docs of a kind."""
    for doc in docs:
        if doc.get("kind") != kind:
            continue
        for c in doc["spec"]["template"]["spec"]["containers"]:
            if c["name"] == container:
                return {e["name"]: e["value"] for e in c.get("env") or [] if "value" in e}
    raise SystemExit(f"no {kind} container {container!r} rendered")


# --- the charts ----------------------------------------------------------------
def test_the_engine_ships_90_days() -> None:
    env = container_env(render(ENGINE), "Deployment", "engine")
    expect("the engine Deployment carries the TTL env", ENV_KEY in env, f"{sorted(env)}")
    expect("and it defaults to 90", env.get(ENV_KEY) == "90", f"{env.get(ENV_KEY)}")


def test_the_engine_takes_the_value_it_is_given() -> None:
    env = container_env(render(ENGINE, f"{PARAM}=30"), "Deployment", "engine")
    expect("a set value renders", env.get(ENV_KEY) == "30", f"{env.get(ENV_KEY)}")
    env = container_env(render(ENGINE, f"{PARAM}=0"), "Deployment", "engine")
    expect("zero renders as zero, not as the default", env.get(ENV_KEY) == "0", f"{env.get(ENV_KEY)}")


def test_the_engine_ships_the_documented_default() -> None:
    engine = yaml.safe_load((ENGINE / "values.yaml").read_text(encoding="utf-8"))
    expect(
        "dfe-engine ships retention.defaultTtlDays at 90",
        engine["retention"]["defaultTtlDays"] == 90,
        f"{engine['retention']}",
    )


# --- the value reaches the chart from the deploy's own env ---------------------
def test_the_cluster_secret_carries_the_annotation() -> None:
    body = CLUSTER_SECRET.read_text(encoding="utf-8")
    expect(
        f"{ANNOTATION} is set from ${{{ENV_KEY}}}",
        f'{ANNOTATION}: "${{{ENV_KEY}}}"' in body,
        "annotation missing from the template",
    )


def test_the_apps_appset_passes_the_annotation_to_the_engine() -> None:
    """layer2-apps is the one appset that carries it -- it deploys the engine.

    layer2-data deliberately carries none: no chart it deploys applies a TTL.
    """
    filename = "layer2-apps.yaml"
    appset = yaml.safe_load((APPSETS / filename).read_text(encoding="utf-8"))
    params = {}
    for source in appset["spec"]["template"]["spec"]["sources"]:
        for entry in source.get("helm", {}).get("parameters", []):
            params[entry["name"]] = entry["value"]
    expect(f"{filename} sets {PARAM}", PARAM in params, f"got {sorted(params)}")
    expect(
        f"{filename} reads it from {ANNOTATION}",
        ANNOTATION in params.get(PARAM, ""),
        f"got {params.get(PARAM)}",
    )
    expect(
        f"{filename} falls back to 90 on a cluster secret without it",
        'default "90"' in params.get(PARAM, ""),
        f"got {params.get(PARAM)}",
    )

    data = yaml.safe_load((APPSETS / "layer2-data.yaml").read_text(encoding="utf-8"))
    data_params = {
        entry["name"]
        for source in data["spec"]["template"]["spec"]["sources"]
        for entry in source.get("helm", {}).get("parameters", [])
    }
    expect(
        f"layer2-data.yaml no longer passes {PARAM} -- nothing there reads it",
        PARAM not in data_params,
        f"got {sorted(data_params)}",
    )


def test_bootstrap_defaults_and_validates_the_key() -> None:
    body = BOOTSTRAP.read_text(encoding="utf-8")
    expect(
        "bootstrap.sh defaults the key so the annotation always renders",
        f'export {ENV_KEY}="${{{ENV_KEY}:-90}}"' in body,
        "the default is missing",
    )
    expect(
        "and rejects anything but whole days",
        f'[[ "${{{ENV_KEY}}}" =~ ^[0-9]+$ ]]' in body,
        "the integer check is missing",
    )


def test_the_env_contract_documents_the_key() -> None:
    body = ENV_EXAMPLE.read_text(encoding="utf-8")
    expect("local.env.example declares the key at 90", f'{ENV_KEY}="90"' in body, "key missing")


# --- the dial ------------------------------------------------------------------
def test_the_dial_renders_the_key() -> None:
    dial = render_dial._parse_yaml_subset(DIAL_EXAMPLE.read_text(encoding="utf-8"))
    updates = render_dial._env_updates(dial)
    expect(
        "deployment.example.yaml retention.default_ttl_days lands in the env key",
        updates.get(ENV_KEY) == "90",
        f"got {updates.get(ENV_KEY)}",
    )
    zero = render_dial._env_updates({"retention": {"default_ttl_days": "0"}})
    expect("a dial saying 0 renders 0", zero.get(ENV_KEY) == "0", f"got {zero.get(ENV_KEY)}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
