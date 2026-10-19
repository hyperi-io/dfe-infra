#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_default_ttl.py
#  Purpose:      Prove the deployment-wide retention reaches the engine pod,
#                from the env file through to the chart.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The default TTL dial, end to end.

DFE_CLICKHOUSE_DEFAULT_TTL_DAYS is the TTL every time-series table takes unless a
source or a dfe-schemas definition declares its own. ONE workload applies it:
dfe-engine, which is the only thing that applies schema at all. The value still
has to travel env file -> cluster secret -> appset -> chart, or a deployment's
retention silently stays at the shipped default. On the deployed Application
dfe-extras writes it into the dfe-engine-env ConfigMap the engine pod reads.

    python3 scripts/tests/test_default_ttl.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary
from _gate import CELLS, effective_env, golden
from _weave import render_extras

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENGINE = "dfe-engine"
EXTRAS_VALUES = REPO_ROOT / "helm" / "charts" / "dfe-extras" / "values.yaml"
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


def literal_env(annotations: dict[str, str]) -> dict[str, str]:
    """The literal env dfe-extras hands the engine for a cluster secret's annotations."""
    docs = render_extras(ENGINE, "slim", "local", annotations=annotations)
    return next(
        d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == f"{ENGINE}-env"
    )["data"]


# --- the deployed engine -------------------------------------------------------
def test_the_engine_ships_90_days() -> None:
    """A cluster secret carrying no retention leaves every deployed cell at 90."""
    for cell in (c for c in CELLS if c[0] == ENGINE):
        env = effective_env(golden(*cell), f"Deployment/{ENGINE}")
        expect(f"{cell}: the engine Deployment carries the TTL env", ENV_KEY in env,
               f"{sorted(env)}")
        expect(f"{cell}: and it defaults to 90", env.get(ENV_KEY) == "90", f"{env.get(ENV_KEY)}")


def test_the_engine_takes_the_value_it_is_given() -> None:
    env = literal_env({ANNOTATION: "30"})
    expect("a cluster secret's value renders", env.get(ENV_KEY) == "30", f"{env.get(ENV_KEY)}")
    env = literal_env({ANNOTATION: "0"})
    expect("zero renders as zero, not as the default", env.get(ENV_KEY) == "0",
           f"{env.get(ENV_KEY)}")


def test_the_engine_ships_the_documented_default() -> None:
    values = yaml.safe_load(EXTRAS_VALUES.read_text(encoding="utf-8"))
    retention = values["extras"]["defaults"][ENGINE]["retention"]
    expect(
        "dfe-extras ships the engine's retention.defaultTtlDays at 90",
        retention["defaultTtlDays"] == 90,
        f"{retention}",
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
