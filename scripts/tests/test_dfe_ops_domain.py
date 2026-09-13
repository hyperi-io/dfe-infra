#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_domain.py
#  Purpose:      Prove the deploy domain follows <mode>.<base> and a stale
#                explicit override is refused rather than published.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions on dfe-ops' domain derivation.

    python3 -m pytest scripts/tests/test_dfe_ops_domain.py -q
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"
TEMPLATE = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-apps.yaml"

_loader = importlib.machinery.SourceFileLoader("dfeops_domain", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_domain", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_domain"] = dfeops
_loader.exec_module(dfeops)


def test_base_domain_derives_the_mode_prefixed_domain() -> None:
    env = dfeops._apply_domain({"DFE_BASE_DOMAIN": "dfe.example.com"}, "scale")
    assert env["DFE_DOMAIN"] == "scale.dfe.example.com"


def test_explicit_domain_alone_is_a_supported_deploy_shape() -> None:
    env = dfeops._apply_domain({"DFE_DOMAIN": "dfe.example.com"}, "single")
    assert env["DFE_DOMAIN"] == "dfe.example.com"


def test_explicit_domain_matching_the_convention_passes() -> None:
    env = dfeops._apply_domain(
        {"DFE_DOMAIN": "slim.dfe.example.com", "DFE_BASE_DOMAIN": "dfe.example.com"}, "slim"
    )
    assert env["DFE_DOMAIN"] == "slim.dfe.example.com"


def test_explicit_domain_contradicting_the_base_is_refused() -> None:
    with pytest.raises(SystemExit) as refused:
        dfeops._apply_domain(
            {"DFE_DOMAIN": "example.com", "DFE_BASE_DOMAIN": "dfe.example.com"}, "scale"
        )
    message = str(refused.value)
    assert "scale.dfe.example.com" in message
    assert "DFE_DOMAIN=example.com" in message


def test_a_verify_observes_the_deployed_domain_without_refusing() -> None:
    env = dfeops._apply_domain(
        {"DFE_DOMAIN": "example.com", "DFE_BASE_DOMAIN": "dfe.example.com"},
        "scale",
        deploying=False,
    )
    assert env["DFE_DOMAIN"] == "example.com"


def test_no_mode_leaves_the_domain_alone() -> None:
    env = dfeops._apply_domain(
        {"DFE_DOMAIN": "example.com", "DFE_BASE_DOMAIN": "dfe.example.com"}, None
    )
    assert env["DFE_DOMAIN"] == "example.com"


def test_stack_version_is_its_own_annotation() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    assert 'dfe.hyperi.io/stack_version: "${DFE_STACK_VERSION}"' in template
    appset = APPSET.read_text(encoding="utf-8")
    assert 'index .metadata.annotations "dfe.hyperi.io/stack_version"' in appset
    assert "stackVersion" in appset
