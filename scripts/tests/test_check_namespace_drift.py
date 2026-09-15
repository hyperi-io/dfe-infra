#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_namespace_drift.py
#  Purpose:      Prove the namespace guard's FAILURE paths fire, including the
#                exact #136 shape -- a guard nobody has seen fail is not a guard.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Failure-path tests for scripts/check_namespace_drift.py.

The defect this guard exists for is invisible: a namespaceSelector naming a
namespace that does not exist is valid YAML, matches nothing, and raises no
error, so a PASS must be earned rather than assumed. These tests feed the
comparison the #136 shape and its siblings and assert it NOTICES.

Everything runs in memory -- no tracked file is edited, so a failed run leaves
no mess to clean up.

    python3 scripts/tests/test_check_namespace_drift.py

No third-party deps and no test runner, matching the check it tests.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_namespace_drift.py"

spec = importlib.util.spec_from_file_location("nsdrift", SCRIPT)
nsdrift = importlib.util.module_from_spec(spec)
sys.modules["nsdrift"] = nsdrift
spec.loader.exec_module(nsdrift)

# The agreeing baseline every test mutates one fact away from.
SSOT = {
    "clickhouse": "clickhouse-operator-system",
    "strimzi": "strimzi",
    "cnpg": "cnpg-system",
}
NETPOL = dict(SSOT)
APPSETS = {
    "clickhouse-operator-helm": {"clickhouse-operator-system"},
    "strimzi-kafka-operator": {"strimzi"},
    "cloudnative-pg": {"cnpg-system"},
}


def test_baseline_agrees() -> None:
    problems = nsdrift.compare(SSOT, NETPOL, APPSETS)
    expect("baseline agrees", problems == [], f"got {problems}")


def test_committed_tree_agrees() -> None:
    """The real files, not a fixture -- the fixture could drift from them."""
    ssot = nsdrift._operator_namespaces(
        nsdrift._load_yaml(nsdrift.COMMON_VALUES), nsdrift.COMMON_VALUES
    )
    netpol = nsdrift._operator_namespaces(
        nsdrift._load_yaml(nsdrift.NETPOL_VALUES), nsdrift.NETPOL_VALUES
    )
    problems = nsdrift.compare(ssot, netpol, nsdrift._appset_namespaces())
    expect("committed tree agrees", problems == [], f"got {problems}")


def test_appset_installs_elsewhere() -> None:
    """The #136 shape: the SSoT and the appset name different namespaces."""
    appsets = dict(APPSETS, **{"clickhouse-operator-helm": {"clickhouse-operator"}})
    problems = nsdrift.compare(SSOT, NETPOL, appsets)
    expect(
        "appset installing elsewhere is caught",
        any("clickhouse" in p and "clickhouse-operator" in p for p in problems),
        f"got {problems}",
    )


def test_netpol_default_diverges() -> None:
    netpol = dict(NETPOL, cnpg="cnpg")
    problems = nsdrift.compare(SSOT, netpol, APPSETS)
    expect(
        "diverged network-policies default is caught",
        any("cnpg" in p for p in problems),
        f"got {problems}",
    )


def test_netpol_missing_an_operator() -> None:
    """A dropped entry silently removes an ingress rule, so it must fail."""
    netpol = {k: v for k, v in NETPOL.items() if k != "strimzi"}
    problems = nsdrift.compare(SSOT, netpol, APPSETS)
    expect(
        "operator missing from the netpol defaults is caught",
        any("strimzi" in p for p in problems),
        f"got {problems}",
    )


def test_operator_absent_from_every_appset() -> None:
    appsets = {k: v for k, v in APPSETS.items() if k != "strimzi-kafka-operator"}
    problems = nsdrift.compare(SSOT, NETPOL, appsets)
    expect(
        "operator in no appset is caught",
        any("strimzi" in p for p in problems),
        f"got {problems}",
    )


def test_unmapped_operator_is_not_a_silent_pass() -> None:
    """An SSoT key with no chart cannot be verified, so it must not read green."""
    ssot = dict(SSOT, ferretdb="ferretdb-operator-system")
    netpol = dict(NETPOL, ferretdb="ferretdb-operator-system")
    problems = nsdrift.compare(ssot, netpol, APPSETS)
    expect(
        "unmapped operator is caught",
        any("ferretdb" in p for p in problems),
        f"got {problems}",
    )


def main() -> int:
    with standalone():
        test_baseline_agrees()
        test_committed_tree_agrees()
        test_appset_installs_elsewhere()
        test_netpol_default_diverges()
        test_netpol_missing_an_operator()
        test_operator_absent_from_every_appset()
        test_unmapped_operator_is_not_a_silent_pass()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
