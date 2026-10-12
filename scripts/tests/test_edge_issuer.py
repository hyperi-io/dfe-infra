#  Project:      dfe-infra
#  File:         scripts/tests/test_edge_issuer.py
#  Purpose:      Prove every edge flavour's cascade names a wildcard issuer the
#                gateway chart itself renders, so the https listener can come up.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The edge wildcard's issuer, per flavour, as the chart renders it.

The Gateway's https listener terminates with dfe-wildcard-tls. When the issuer
the cascade names has no ClusterIssuer, that Secret is never issued, Envoy
Gateway drops the listener as not ready (internal/gatewayapi/listener.go at
v1.9.2), and the load balancer carries no 443 at all -- a client's connect
times out rather than failing TLS.

    python3 -m pytest scripts/tests/test_edge_issuer.py -q

Needs `helm` on PATH.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

from _charts import chart_dir

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATEWAY = chart_dir("envoy-gateway-config")
VALUES = REPO_ROOT / "argocd" / "values"

# Flavour file -> the cloud overlay layer2-edge.yaml layers before it.
FLAVOURS = {"aws": "aws", "gcp": "gcp", "azure": "azure", "onprem": "local"}


def render(flavour: str, *args: str) -> list[dict]:
    """The gateway chart through the value files layer2-edge.yaml layers for one flavour."""
    out = subprocess.run(
        [
            "helm", "template", "envoy-gateway-config", str(GATEWAY),
            "--namespace", "envoy-gateway-system",
            "-f", str(VALUES / "common.yaml"),
            "-f", str(VALUES / f"{FLAVOURS[flavour]}.yaml"),
            "-f", str(VALUES / f"edge-{flavour}.yaml"),
            "-f", str(VALUES / "profile-scale.yaml"),
            "--set", "appNamespace=dfe",
            "--set", "domain=dfe.example.com",
            *args,
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def wildcard_issuer(docs: list[dict]) -> str:
    """The ClusterIssuer dfe-wildcard-tls is requested from, by annotation or by Certificate."""
    for doc in docs:
        if doc.get("kind") == "Certificate" and doc["spec"].get("secretName") == "dfe-wildcard-tls":
            return doc["spec"]["issuerRef"]["name"]
    gateway = next(d for d in docs if d.get("kind") == "Gateway")
    return (gateway["metadata"].get("annotations") or {}).get("cert-manager.io/cluster-issuer", "")


def rendered_issuers(docs: list[dict]) -> set[str]:
    return {d["metadata"]["name"] for d in docs if d.get("kind") == "ClusterIssuer"}


@pytest.mark.parametrize("flavour", sorted(FLAVOURS))
def test_every_flavour_names_a_wildcard_issuer_the_chart_renders(flavour: str) -> None:
    docs = render(flavour)
    issuer = wildcard_issuer(docs)
    assert issuer, f"edge-{flavour}: nothing requests dfe-wildcard-tls"
    assert issuer in rendered_issuers(docs), (
        f"edge-{flavour}: dfe-wildcard-tls is requested from ClusterIssuer {issuer}, which this render "
        f"does not create (it creates {sorted(rendered_issuers(docs))})"
    )


def test_expected_fail_the_chart_default_issuer_with_no_acme_or_vault_renders_none() -> None:
    """The shape a cloud flavour had: dfe-letsencrypt named, nothing configured to back it."""
    docs = render("aws", "--set", "tls.issuerName=dfe-letsencrypt")
    assert wildcard_issuer(docs) == "dfe-letsencrypt"
    assert "dfe-letsencrypt" not in rendered_issuers(docs)


def test_a_deployment_that_configures_acme_gets_its_issuer_rendered() -> None:
    docs = render("aws", "--set", "tls.issuerName=dfe-letsencrypt", "--set", "tls.acme.email=ops@example.com")
    assert wildcard_issuer(docs) == "dfe-letsencrypt"
    assert "dfe-letsencrypt" in rendered_issuers(docs)
