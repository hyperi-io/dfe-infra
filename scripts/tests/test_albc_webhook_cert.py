#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/tests/test_albc_webhook_cert.py
#  Purpose:      Pin the AWS Load Balancer Controller's webhook certificate to
#                cert-manager, and the bootstrap order that setting relies on.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The load balancer controller's webhook certificate comes from cert-manager.

With the chart's default, every render runs genCA and writes a new CA into both
the webhook configurations' caBundle and the Secret the controller serves from.
Argo CD renders with `helm template`, where `keepTLSSecret`'s `lookup` answers
empty. So a re-render can leave the API server trusting a CA the running
controller does not serve from, and every TargetGroupBinding admission then
fails x509. With `enableCertManager` every render is identical, and cert-manager
issues the certificate and injects its CA.

The appset's values block is rendered by helm, which runs the same Go template
engine and Sprig functions Argo CD's goTemplate does.

    python3 -m pytest scripts/tests/test_albc_webhook_cert.py -q
"""

import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer1-addons.yaml"
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"

CHART = "aws-load-balancer-controller"
# What a cluster secret on AWS carries for the keys the values block reads.
ANNOTATIONS = {
    "dfe.hyperi.io/domain": "dfe.example.com",
    "dfe.hyperi.io/cloud": "aws",
    "dfe.hyperi.io/env": "test",
    "dfe.hyperi.io/cluster_name": "dfe-aws-test",
    "dfe.hyperi.io/region": "us-west-2",
    "dfe.hyperi.io/vpc_id": "vpc-0123456789abcdef0",
}
LABELS = {"dfe.hyperi.io/cloud": "aws", "dfe.hyperi.io/dns-provider": "route53"}


def _appset() -> dict:
    return yaml.safe_load(APPSET.read_text(encoding="utf-8"))


def _elements(appset: dict) -> list[dict]:
    found: list[dict] = []
    for generator in appset["spec"]["generators"]:
        for child in generator.get("matrix", {}).get("generators", []):
            found.extend(child.get("list", {}).get("elements", []))
    return found


def _values(tmp_path: Path, element: dict) -> dict:
    """The helm values the appset hands one element's Application on an AWS cluster."""
    block = _appset()["spec"]["template"]["spec"]["source"]["helm"]["values"]
    chart = tmp_path / element["chart"]
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: appset-values\nversion: 0.1.0\n", encoding="utf-8"
    )
    (chart / "templates" / "values.yaml").write_text(
        "{{- with .Values.ctx }}\n" + block + "\n{{- end }}\n", encoding="utf-8"
    )
    context = {
        **element,
        "name": "dfe-aws-test",
        "server": "https://kubernetes.default.svc",
        "metadata": {"annotations": ANNOTATIONS, "labels": LABELS},
    }
    ctx = tmp_path / f"{element['chart']}-ctx.yaml"
    ctx.write_text(yaml.safe_dump({"ctx": context}), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "appset-values", str(chart), "-f", str(ctx)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert out.returncode == 0, out.stderr
    return yaml.safe_load(out.stdout) or {}


def _controller() -> dict:
    return next(e for e in _elements(_appset()) if e["chart"] == CHART)


def test_the_controller_takes_its_webhook_certificate_from_cert_manager(tmp_path: Path) -> None:
    values = _values(tmp_path, _controller())
    assert values["enableCertManager"] is True, values
    # The values the setting sits beside still render, so it landed in the controller's block.
    assert values["clusterName"] == "dfe-aws-test", values


def test_no_render_time_certificate_is_configured(tmp_path: Path) -> None:
    """keepTLSSecret reads the old Secret through `lookup`, which `helm template` answers
    empty; webhookTLS would put a private key in the Application spec."""
    values = _values(tmp_path, _controller())
    assert "keepTLSSecret" not in values, values
    assert "webhookTLS" not in values, values
    assert "certManager" not in values, "the chart's own self-signed issuer chain is the one meant"


def test_no_other_addon_gets_the_setting(tmp_path: Path) -> None:
    for element in _elements(_appset()):
        if element["chart"] == CHART:
            continue
        values = _values(tmp_path, element)
        assert "enableCertManager" not in values, element["chart"]


def test_cert_manager_is_installed_and_waited_for_before_argo() -> None:
    """The controller's Certificate and Issuers need cert-manager's CRDs and webhook up
    before Argo first syncs the Application; bootstrap.sh installs it ahead of Argo."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    step_two = "] cert-manager (detect-or-install)"
    cert_manager = next(i for i, line in enumerate(lines) if step_two in line)
    argo = next(i for i, line in enumerate(lines) if "] ArgoCD with Valkey cache" in line)
    assert cert_manager < argo
    install = next(
        i for i, line in enumerate(lines)
        if i > cert_manager and "helm upgrade --install cert-manager" in line
    )
    step = "\n".join(lines[install : install + 6])
    assert "--wait" in step, step
    assert install < argo


def test_bootstrap_owns_cert_manager_rather_than_this_appset() -> None:
    charts = {e["chart"] for e in _elements(_appset())}
    assert "cert-manager" not in charts, sorted(charts)
