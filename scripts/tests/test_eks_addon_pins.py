#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_eks_addon_pins.py
#  Purpose:      Hold the EKS add-on pins to the Kubernetes minor the shipped
#                dials run, and to the add-on list the cluster module installs,
#                so a Kubernetes bump cannot leave the add-ons behind.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The EKS add-on pins in versions.yaml against what the AWS cluster actually runs.

    python3 -m pytest scripts/tests/test_eks_addon_pins.py -q

kube-proxy is built for exactly one Kubernetes minor and its version names it,
so it stands in for the whole set: when the minor a shipped dial runs moves and
the pins do not, these fail before a plan does.
"""

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import check_versions_drift as drift  # noqa: E402

EKS_TF = REPO_ROOT / "terraform" / "modules" / "kubernetes-cluster" / "aws" / "eks.tf"
EXAMPLE_DIAL = REPO_ROOT / "deployment.example.yaml"
EXAMPLE_TFVARS = REPO_ROOT / "terraform" / "environments" / "aws" / "example.tfvars.json"


def _minor(version: str) -> str:
    """`1.36` from `v1.36.0-eksbuild.45` or from `1.36`."""
    found = re.match(r"^v?(\d+\.\d+)(?:\.|$)", version)
    assert found, f"{version!r} names no Kubernetes minor"
    return found.group(1)


def _pins() -> dict[str, str]:
    return {
        key.split(".", 1)[1]: value
        for key, value in drift.load_versions().items()
        if key.startswith("eks-addons.")
    }


def _installed() -> list[str]:
    """The add-on names eks.tf's `addons` local installs."""
    found = re.search(r"(?m)^\s*addons\s*=\s*\[([^\]]*)\]", EKS_TF.read_text(encoding="utf-8"))
    assert found, f"{EKS_TF} carries no `addons = [...]` local"
    return sorted(re.findall(r'"([^"]+)"', found.group(1)))


def _shipped_minors() -> dict[str, str]:
    """The Kubernetes minor each committed dial provisions, keyed by the file that says so."""
    text = EXAMPLE_DIAL.read_text(encoding="utf-8")
    found = re.search(r'(?m)^kubernetes_version:\s*"?([^"\s#]+)', text)
    assert found, f"{EXAMPLE_DIAL} names no kubernetes_version"
    tfvars = json.loads(EXAMPLE_TFVARS.read_text(encoding="utf-8"))
    return {
        "deployment.example.yaml": found.group(1),
        "terraform/environments/aws/example.tfvars.json": tfvars["kubernetes_version"],
    }


def _as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def test_every_addon_the_module_installs_has_a_pin_and_a_drift_check() -> None:
    assert sorted(_pins()) == _installed()
    assert sorted(drift._EKS_ADDONS) == _installed()


@pytest.mark.parametrize("where", sorted(_shipped_minors()))
def test_the_kube_proxy_pin_is_built_for_the_minor_the_shipped_dials_run(where: str) -> None:
    pinned, runs = _minor(_pins()["kube-proxy"]), _shipped_minors()[where]
    assert pinned == runs, (
        f"versions.yaml eks-addons pin kube-proxy for Kubernetes {pinned}, but {where} runs "
        f"{runs}: move every eks-addons pin to what `aws eks describe-addon-versions "
        f"--kubernetes-version {runs}` lists, then run check_versions_drift.py --fix"
    )


def test_the_kube_proxy_pin_sits_inside_the_supported_platform_window() -> None:
    window = drift.load_versions()["platform.kubernetes"]
    low, _, high = window.partition("-")
    pinned = _minor(_pins()["kube-proxy"])
    assert _as_tuple(low) <= _as_tuple(pinned) <= _as_tuple(high or low), (pinned, window)


def test_expected_fail_a_pin_for_another_minor_is_caught() -> None:
    """The comparison above is only worth something if a mismatch reads as one."""
    assert _minor("v1.35.4-eksbuild.2") != _minor("1.36")
    assert _minor("v1.36.0-eksbuild.45") == _minor("1.36")
