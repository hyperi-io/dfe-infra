#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_chart_render_gates.py
#  Purpose:      Two render gates that were wrong in opposite directions -- one
#                refused a render that deploys nothing, one accepted a render that
#                drops half its input.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rendered assertions for the two gates in dfe-infra #296 and #297.

**#296 -- one SecurityPolicy per route, not per provider.** The gateway chart
ranged its policies over `oidc.providers` inside the per-route range, so every
provider produced its own SecurityPolicy naming the SAME HTTPRoute with no
sectionName. Envoy Gateway calls that a conflict and resolves it by keeping the
oldest creationTimestamp, so a second provider stopped working the moment it
was added and nothing in the apply output said so. `spec.oidc` is a single
object rather than a list, so the policy now carries the designated provider's
login and every provider as a JWT issuer with an Allow rule of its own.

**#297 -- the Redpanda licence gate fired where no Redpanda deploys.** The
`fail` sat above the `mode: cluster` gate, keyed on the provider alone, so a
deployment that moved to `mode: external` and kept `provider: redpanda` could
not render a chart whose Redpanda body was switched off anyway. Single mode
keeps its own check, beside the StatefulSet it actually deploys.

Both are render-time, which is why they are asserted here: the first ships a
manifest that is quietly incomplete, the second refuses to ship at all.

    python3 scripts/tests/test_chart_render_gates.py

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
# The gateway is an edge-module chart, not an app chart -- helm/edge/, not helm/charts/.
GATEWAY = REPO_ROOT / "helm" / "edge" / "gateway"
KAFKA = CHARTS / "kafka"
COMMON = REPO_ROOT / "argocd" / "values" / "common.yaml"

# Two providers on the same routes: the shape that used to render two policies
# per route and keep one of them.
TWO_PROVIDERS = [
    "oidc.enabled=true",
    "oidc.providers[0].name=okta",
    "oidc.providers[0].issuerUrl=https://okta.example.com",
    "oidc.providers[0].clientId=okta-client",
    "oidc.providers[1].name=entra",
    "oidc.providers[1].issuerUrl=https://entra.example.com",
    "oidc.providers[1].clientId=entra-client",
]


def render(chart: Path, *sets: str, values: Path | None = None) -> list[dict]:
    """Rendered docs. Raises with helm's own message when the chart refuses."""
    out = _helm(chart, *sets, values=values)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart.name} {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def render_error(chart: Path, *sets: str, values: Path | None = None) -> str:
    """helm's stderr when the chart refuses to render, or "" when it rendered."""
    out = _helm(chart, *sets, values=values)
    return out.stderr if out.returncode != 0 else ""


def _helm(chart: Path, *sets: str, values: Path | None) -> subprocess.CompletedProcess[str]:
    cmd = ["helm", "template", chart.name, str(chart)]
    if values is not None:
        cmd += ["-f", str(values)]
    for s in sets:
        cmd += ["--set", s]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def policies(docs: list[dict]) -> list[dict]:
    return [d for d in docs if d.get("kind") == "SecurityPolicy"]


def target_names(policy: dict) -> list[str]:
    return [t["name"] for t in policy["spec"]["targetRefs"]]


# --- #296: one SecurityPolicy per route ------------------------------------


def test_two_providers_render_one_policy_per_route() -> None:
    """The defect: a second provider added a second policy on the same route."""
    docs = render(GATEWAY, *TWO_PROVIDERS, values=COMMON)
    targeted = [(p["metadata"]["namespace"], t) for p in policies(docs) for t in target_names(p)]
    expect(
        "no HTTPRoute is targeted by more than one SecurityPolicy",
        len(targeted) == len(set(targeted)),
        f"got {sorted(targeted)}",
    )
    expect("the providers still produce policies", len(targeted) > 1, f"got {targeted}")


def test_the_designated_provider_owns_the_login() -> None:
    """spec.oidc takes one provider, so which one is a declaration, not list order."""
    docs = render(GATEWAY, *TWO_PROVIDERS, "oidc.loginProvider=entra", values=COMMON)
    issuers = {p["spec"]["oidc"]["provider"]["issuer"] for p in policies(docs)}
    expect(
        "every policy logs in through the designated provider",
        issuers == {"https://entra.example.com"},
        f"got {sorted(issuers)}",
    )
    clients = {p["spec"]["oidc"]["clientID"] for p in policies(docs)}
    expect("and carries that provider's client", clients == {"entra-client"}, f"got {clients}")


def test_an_unset_designation_takes_the_first_provider() -> None:
    docs = render(GATEWAY, *TWO_PROVIDERS, values=COMMON)
    issuers = {p["spec"]["oidc"]["provider"]["issuer"] for p in policies(docs)}
    expect("an empty loginProvider takes the first entry", issuers == {"https://okta.example.com"},
           f"got {sorted(issuers)}")


def test_a_designation_naming_nothing_fails_the_render() -> None:
    """Silently falling back is what this whole issue is about."""
    err = render_error(GATEWAY, *TWO_PROVIDERS, "oidc.loginProvider=nosuch", values=COMMON)
    expect("an unknown loginProvider refuses to render", "nosuch" in err, f"got {err!r}")
    expect("and the message names what is configured", "okta" in err and "entra" in err,
           f"got {err!r}")


def test_every_provider_still_validates_on_the_infra_policy() -> None:
    """Folding the extras in is what stops the second provider being dropped."""
    docs = render(GATEWAY, *TWO_PROVIDERS, values=COMMON)
    admin = [p for p in policies(docs) if p["metadata"]["name"].endswith("-admin")]
    expect("the infra routes render admin policies", len(admin) >= 1, f"got {len(admin)}")
    for p in admin:
        name = p["metadata"]["name"]
        jwt_names = [j["name"] for j in p["spec"]["jwt"]["providers"]]
        expect(f"{name}: every provider is a JWT issuer", jwt_names == ["okta", "entra"],
               f"got {jwt_names}")
        rule_providers = [r["principal"]["jwt"]["provider"] for r in
                          p["spec"]["authorization"]["rules"]]
        expect(f"{name}: every provider gets an Allow rule", rule_providers == ["okta", "entra"],
               f"got {rule_providers}")
        expect(f"{name}: unmatched requests are still denied",
               p["spec"]["authorization"]["defaultAction"] == "Deny",
               f"got {p['spec']['authorization']['defaultAction']}")


# --- #297: the Redpanda licence gate ---------------------------------------


def test_external_mode_renders_with_a_leftover_redpanda_provider() -> None:
    """mode: external deploys no broker, so the licence has nothing to cover."""
    docs = render(KAFKA, "kafka.mode=external", "kafka.provider=redpanda")
    kinds = {d.get("kind") for d in docs}
    expect("external mode renders without acceptLicense", "Redpanda" not in kinds, f"got {kinds}")
    expect("and still renders its external credentials", "ExternalSecret" in kinds, f"got {kinds}")


def test_cluster_mode_still_refuses_without_the_licence() -> None:
    err = render_error(KAFKA, "kafka.mode=cluster", "kafka.provider=redpanda")
    expect("cluster mode refuses without acceptLicense", "acceptLicense" in err, f"got {err!r}")


def test_single_mode_still_refuses_without_the_licence() -> None:
    """Single deploys the Redpanda StatefulSet from kafka-single.yaml, which owns this check."""
    err = render_error(KAFKA, "kafka.mode=single", "kafka.provider=redpanda")
    expect("single mode refuses without acceptLicense", "acceptLicense" in err, f"got {err!r}")


def test_accepting_the_licence_renders_the_redpanda_cluster() -> None:
    docs = render(
        KAFKA,
        "kafka.mode=cluster",
        "kafka.provider=redpanda",
        "kafka.redpanda.acceptLicense=true",
    )
    expect("cluster mode renders the Redpanda CR once accepted",
           any(d.get("kind") == "Redpanda" for d in docs),
           f"got {sorted({d.get('kind') for d in docs})}")


def test_the_kafka_provider_is_untouched_by_the_gate() -> None:
    """The gate is Redpanda's licence, not a mode check: Strimzi must be unaffected."""
    # The Strimzi path renders a KafkaUser credential, which needs the namespace
    # the appset passes; the gate under test is upstream of that.
    docs = render(KAFKA, "kafka.mode=cluster", "kafka.provider=strimzi", "appNamespace=dfe-local")
    kinds = {d.get("kind") for d in docs}
    expect("a strimzi cluster renders with no licence value", "Redpanda" not in kinds, f"got {kinds}")


def main() -> int:
    with standalone():
        test_two_providers_render_one_policy_per_route()
        test_the_designated_provider_owns_the_login()
        test_an_unset_designation_takes_the_first_provider()
        test_a_designation_naming_nothing_fails_the_render()
        test_every_provider_still_validates_on_the_infra_policy()
        test_external_mode_renders_with_a_leftover_redpanda_provider()
        test_cluster_mode_still_refuses_without_the_licence()
        test_single_mode_still_refuses_without_the_licence()
        test_accepting_the_licence_renders_the_redpanda_cluster()
        test_the_kafka_provider_is_untouched_by_the_gate()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
