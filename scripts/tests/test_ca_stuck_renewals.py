#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_ca_stuck_renewals.py
#  Purpose:      Prove `dfe-ops ca --status` names the renewals a READY Certificate hides
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""cert-manager keeps a Certificate READY True until it expires, even when its renewal can't be signed.

The stuck part shows on the CertificateRequest (Ready False, reason Pending) and on
the issuer (Ready False). Every case is an offline kubectl list document.

    python3 -m pytest scripts/tests/test_ca_stuck_renewals.py -q
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ops():
    loader = importlib.machinery.SourceFileLoader(
        "dfeops_ca_stuck", str(REPO_ROOT / "scripts" / "dfe-ops")
    )
    spec = importlib.util.spec_from_loader("dfeops_ca_stuck", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dfeops_ca_stuck"] = module
    loader.exec_module(module)
    return module


OPS = _ops()


def _item(kind: str, name: str, status: str, reason: str, namespace: str = "") -> dict:
    meta = {"name": name, **({"namespace": namespace} if namespace else {})}
    condition = {"type": "Ready", "status": status, "reason": reason, "message": "m"}
    return {"kind": kind, "metadata": meta, "status": {"conditions": [condition]}}


@pytest.mark.parametrize(
    ("requests", "issuers", "expected"),
    [
        (
            [_item("CertificateRequest", "edge-1", "True", "Issued", "gw")],
            [_item("ClusterIssuer", "internal-ca", "True", "KeyPairVerified")],
            ["cert-manager renewals: none stuck"],
        ),
        (
            [_item("CertificateRequest", "edge-2", "False", "Pending", "gw")],
            [_item("ClusterIssuer", "internal-ca", "False", "ErrGetKeyPair")],
            [
                "cert-manager renewals: 2 stuck, so a Certificate showing READY True will not renew",
                "  CertificateRequest gw/edge-2: Pending m",
                "  ClusterIssuer internal-ca: ErrGetKeyPair m",
            ],
        ),
        (
            [_item("CertificateRequest", "edge-0", "False", "Failed", "gw")],
            [],
            ["cert-manager renewals: none stuck"],
        ),
        (
            [
                {
                    "kind": "CertificateRequest",
                    "metadata": {"name": "new", "namespace": "gw"},
                }
            ],
            [{"kind": "Issuer", "metadata": {"name": "local", "namespace": "gw"}}],
            [
                "cert-manager renewals: 1 stuck, so a Certificate showing READY True will not renew",
                "  Issuer gw/local: no status",
            ],
        ),
        ([], [], ["cert-manager renewals: none stuck"]),
    ],
    ids=[
        "all-ready",
        "pending-and-issuer-down",
        "old-failed-request",
        "no-status-yet",
        "no-cert-manager",
    ],
)
def test_stuck_renewals_are_named(
    requests: list, issuers: list, expected: list[str]
) -> None:
    assert OPS.stuck_renewal_lines({"items": requests}, {"items": issuers}) == expected
