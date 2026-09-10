#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_modes.py
#  Purpose:      Prove the deploy MODES and the profile files a deploy in that
#                mode actually reads agree -- the values each profile sets, the
#                appsets its label selects, and the transport it implies.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions binding MODES to the files a deploy in that mode actually reads.

MODES is declared in scripts/profiles.py (covered by test_profiles.py); this
file checks the CONTENTS of the files a mode selects, so a profile that no
longer matches its mode fails here instead of mid-deploy.

    python3 -m pytest scripts/tests/test_dfe_ops_modes.py -q
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"
ARGO_VALUES = REPO_ROOT / "argocd" / "values"
STACK_PROFILES = REPO_ROOT / "helm" / "dfe-stack" / "profiles"
LAYER_SCALE = REPO_ROOT / "argocd" / "appsets" / "layer-scale.yaml"

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_modes", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_modes", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_modes"] = dfeops
_loader.exec_module(dfeops)

BROKERLESS = ("slim", "mesh")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))


def _appsets() -> dict[str, dict]:
    docs = yaml.safe_load_all(LAYER_SCALE.read_text(encoding="utf-8", errors="replace"))
    return {doc["metadata"]["name"]: doc for doc in docs}


def _charts(appset: dict) -> list[dict]:
    """The chart list a matrix appset installs, past the cluster generator."""
    return appset["spec"]["generators"][0]["matrix"]["generators"][1]["list"]["elements"]


def test_mesh_is_a_declared_mode() -> None:
    assert "mesh" in dfeops.MODES


def test_the_argocd_profiles_disable_kafka_on_the_brokerless_modes() -> None:
    for mode in BROKERLESS:
        assert _load(ARGO_VALUES / f"profile-{mode}.yaml")["kafka"]["mode"] == "disabled"
    assert _load(ARGO_VALUES / "profile-scale.yaml")["kafka"]["mode"] == "cluster"


def test_the_mesh_argocd_profile_keeps_the_scale_clickhouse_cluster() -> None:
    mesh = _load(ARGO_VALUES / "profile-mesh.yaml")
    scale = _load(ARGO_VALUES / "profile-scale.yaml")
    assert mesh["clickhouse"] == scale["clickhouse"]
    assert mesh["kafbat"]["enabled"] is False
    assert mesh["clusterTelemetry"]["kafka"]["enabled"] is False


def test_the_stack_profile_disables_kafka_and_keeps_the_cluster() -> None:
    mesh = _load(STACK_PROFILES / "mesh.yaml")
    scale = _load(STACK_PROFILES / "scale.yaml")
    assert "kafka" not in mesh
    assert mesh["kafbat"]["enabled"] is False
    assert mesh["dfe-receiver"]["kafka"]["mode"] == "disabled"
    assert mesh["dfe-loader"]["kafka"]["mode"] == "disabled"
    assert mesh["clickhouse-cluster"] == scale["clickhouse-cluster"]
    assert scale["kafka"]["kafka"]["mode"] == "cluster"


def test_the_mesh_operators_appset_selects_its_own_profile() -> None:
    appset = _appsets()["dfe-mesh-operators"]
    selector = appset["spec"]["generators"][0]["matrix"]["generators"][0]["clusters"]["selector"]
    assert selector["matchLabels"]["dfe.hyperi.io/profile"] == "mesh"


def test_the_mesh_operators_appset_installs_no_broker_operator() -> None:
    charts = [element["chart"] for element in _charts(_appsets()["dfe-mesh-operators"])]
    assert charts == ["clickhouse-operator-helm"]


def test_the_clickhouse_operator_pin_is_the_same_on_both_scale_tiers() -> None:
    appsets = _appsets()

    def pin(name: str) -> tuple[str, str]:
        elements = _charts(appsets[name])
        entry = next(e for e in elements if e["chart"] == "clickhouse-operator-helm")
        return entry["repo"], entry["version"]

    assert pin("dfe-mesh-operators") == pin("dfe-scale-operators")


def test_the_scale_apps_appset_covers_both_scale_tiers() -> None:
    appset = _appsets()["dfe-scale-apps"]
    selector = appset["spec"]["generators"][0]["matrix"]["generators"][0]["clusters"]["selector"]
    expression = next(
        e for e in selector["matchExpressions"] if e["key"] == "dfe.hyperi.io/profile"
    )
    assert expression["operator"] == "In"
    assert sorted(expression["values"]) == ["mesh", "scale"]


def test_the_acceptance_transport_defaults_follow_the_mode() -> None:
    import argparse

    def resolve(mode: str | None, transport: str | None) -> str:
        return dfeops._acceptance_transport(
            argparse.Namespace(mode=mode, transport=transport)
        )

    assert resolve(None, None) == "both"
    assert resolve("mesh", None) == "grpc"
    assert resolve("slim", None) == "grpc"
    assert resolve("single", None) == "kafka"
    assert resolve("scale", None) == "kafka"
    # An explicit flag beats the mode it disagrees with.
    assert resolve("scale", "grpc") == "grpc"
