#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_pod_identity_egress.py
#  Purpose:      Prove allow-baseline-egress admits the EKS Pod Identity agent
#                on TCP 80 in every namespace it renders, and that the rule
#                drops out again when the value is disabled.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Pod Identity agent egress in allow-baseline-egress.

    python3 scripts/tests/test_pod_identity_egress.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "network-policies"
COMMON = REPO_ROOT / "argocd" / "values" / "common.yaml"

POD_IDENTITY_ADDRESS = "169.254.170.23"
POD_IDENTITY_PORT = 80


def render(*sets: str) -> list[dict]:
    """Render the chart with the shared SSoT values plus --set overrides."""
    cmd = ["helm", "template", "network-policies", str(CHART), "-f", str(COMMON)]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def baseline_policies(docs: list[dict]) -> list[dict]:
    return [
        d
        for d in docs
        if d.get("kind") == "NetworkPolicy" and d["metadata"]["name"] == "allow-baseline-egress"
    ]


def pod_identity_rules(policy: dict) -> list[dict]:
    """Egress rules whose destination is the Pod Identity agent's /32."""
    rules = []
    for rule in policy["spec"]["egress"]:
        for to in rule.get("to", []):
            ip_block = to.get("ipBlock")
            if ip_block and ip_block.get("cidr") == f"{POD_IDENTITY_ADDRESS}/32":
                rules.append(rule)
    return rules


def test_baseline_admits_pod_identity_everywhere() -> None:
    policies = baseline_policies(render())
    expect("a baseline policy renders in at least one namespace", len(policies) >= 1,
           f"got {len(policies)}")
    for policy in policies:
        ns = policy["metadata"]["namespace"]
        rules = pod_identity_rules(policy)
        expect(f"{ns} baseline egress admits the Pod Identity agent /32",
               len(rules) == 1, f"got {rules}")
        ports = {(p["port"], p.get("protocol", "TCP")) for p in rules[0].get("ports", [])}
        expect(f"{ns} Pod Identity rule opens only TCP {POD_IDENTITY_PORT}",
               ports == {(POD_IDENTITY_PORT, "TCP")}, f"got {ports}")


def test_disabling_removes_the_rule() -> None:
    policies = baseline_policies(render("podIdentityAgent.enabled=false"))
    expect("disabling still renders the baseline policy itself", len(policies) >= 1,
           f"got {len(policies)}")
    for policy in policies:
        ns = policy["metadata"]["namespace"]
        rules = pod_identity_rules(policy)
        expect(f"{ns} drops the Pod Identity rule when disabled", rules == [],
               f"got {rules}")


def refused(*sets: str) -> str:
    """The render's own error, for a values pair the chart must not accept."""
    cmd = ["helm", "template", "network-policies", str(CHART), "-f", str(COMMON)]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode == 0:
        raise AssertionError(f"helm template accepted {sets}, which it must refuse")
    return out.stderr


def test_the_metadata_service_cannot_be_opened_to_every_namespace() -> None:
    """169.254.169.254 on port 80 hands the caller the NODE's role, and policy is
    additive -- so every pod in every DFE namespace would hold it and no later
    policy could take it back. One character from the agent's own address."""
    err = refused("podIdentityAgent.address=169.254.169.254")
    expect("the metadata service address is refused by name",
           "169.254.169.254 is the instance metadata service" in err, err.strip()[-300:])


def test_only_the_credentials_port_may_be_opened() -> None:
    """2703 is the agent's own webhook port; no application pod calls it."""
    err = refused("podIdentityAgent.port=2703")
    expect("a port other than 80 is refused by name",
           "only 80, the credentials endpoint" in err, err.strip()[-300:])


def main() -> int:
    with standalone():
        test_baseline_admits_pod_identity_everywhere()
        test_disabling_removes_the_rule()
        test_the_metadata_service_cannot_be_opened_to_every_namespace()
        test_only_the_credentials_port_may_be_opened()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
