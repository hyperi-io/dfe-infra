#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/tests/test_addon_digests.py
#  Purpose:      Hold layer1-addons.yaml's digest wiring to both of its paths:
#                no digest renders the chart-repository source and the chart's
#                own tag, a pinned digest renders the OCI source or tag@digest.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""karpenter's chart digest and the load balancer controller's image digest.

Each Application is rendered by scripts/dfe-weave's expand_template, which runs
the appset template through helm's Go template engine and Sprig functions, the
ones Argo CD's goTemplate uses. Argo drops an empty `chart` or `path` as unset
(both are omitempty in its ApplicationSource), so a render here compares with
empty strings removed.

    python3 -m pytest scripts/tests/test_addon_digests.py
"""

import re
import sys
from pathlib import Path

import pytest
import yaml

from _weave import helm, weave

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_versions_drift as drift  # noqa: E402

ADDONS = Path("argocd/appsets/layer1-addons.yaml")
APPS = Path("argocd/appsets/layer2-apps.yaml")
CHART_DIGEST = "sha256:" + "c" * 64
IMAGE_DIGEST = "sha256:" + "e" * 64
ANNOTATIONS = {
    "dfe.hyperi.io/domain": "dfe.example.com",
    "dfe.hyperi.io/cloud": "aws",
    "dfe.hyperi.io/env": "test",
    "dfe.hyperi.io/cluster_name": "dfe-aws-test",
    "dfe.hyperi.io/region": "us-west-2",
    "dfe.hyperi.io/vpc_id": "vpc-0123456789abcdef0",
}
LABELS = {"dfe.hyperi.io/cloud": "aws", "dfe.hyperi.io/dns-provider": "route53"}


def _appset(text: str | None = None) -> dict:
    return yaml.safe_load(text if text is not None else (REPO_ROOT / ADDONS).read_text())


def _elements(appset: dict) -> list[dict]:
    found: list[dict] = []
    for generator in appset["spec"]["generators"]:
        for child in generator.get("matrix", {}).get("generators", []):
            found.extend(child.get("list", {}).get("elements", []))
    return found


def _element(chart: str, appset: dict | None = None) -> dict:
    return next(e for e in _elements(appset or _appset()) if e["chart"] == chart)


def _omit_empty(node: object) -> object:
    if isinstance(node, dict):
        return {k: _omit_empty(v) for k, v in node.items() if v != ""}
    return node


def _render(element: dict, appset: dict | None = None, **params: str) -> dict:
    """The Application the appset generates for one element on an AWS cluster."""
    context = {
        **element,
        **params,
        "name": "dfe-aws-test",
        "server": "https://kubernetes.default.svc",
        "metadata": {"annotations": ANNOTATIONS, "labels": LABELS},
    }
    template = (appset or _appset())["spec"]["template"]
    return weave().expand_template(template, context, helm())


def _source(app: dict) -> dict:
    return _omit_empty({k: v for k, v in app["spec"]["source"].items() if k != "helm"})


def _values(app: dict) -> dict:
    return yaml.safe_load(app["spec"]["source"]["helm"]["values"]) or {}


@pytest.mark.parametrize("chart", [e["chart"] for e in _elements(_appset())])
def test_with_no_digest_every_addon_comes_from_its_chart_repository_by_version(chart) -> None:
    element = _element(chart)
    assert _source(_render(element, chartDigest="")) == {
        "repoURL": element["repo"],
        "chart": element["chart"],
        "targetRevision": element["version"],
    }


def test_a_karpenter_chart_digest_renders_argos_oci_source_at_that_digest() -> None:
    element = _element("karpenter")
    by_version = _render(element)
    by_digest = _render(element, chartDigest=CHART_DIGEST)

    assert _source(by_digest) == {
        "repoURL": "oci://public.ecr.aws/karpenter/karpenter",
        "path": ".",
        "targetRevision": CHART_DIGEST,
    }
    assert by_digest["spec"]["source"]["helm"] == by_version["spec"]["source"]["helm"]
    assert by_digest["metadata"] == by_version["metadata"]


def test_the_oci_source_has_the_shape_the_thin_charts_deploy_with() -> None:
    """layer2-apps.yaml's first source is the one OCI shape this repo already runs."""
    thin = _appset((REPO_ROOT / APPS).read_text())["spec"]["template"]["spec"]["sources"][0]
    assert thin["repoURL"].startswith("oci://")
    assert thin["path"] == "."
    assert "chart" not in thin
    oci = _source(_render(_element("karpenter"), chartDigest=CHART_DIGEST))
    assert set(oci) == {"repoURL", "path", "targetRevision"}
    assert oci["repoURL"].startswith("oci://")


@pytest.mark.parametrize(
    ("chart", "digest", "message"),
    [
        ("keda", CHART_DIGEST, "keda comes from a classic chart repository"),
        ("karpenter", "sha256:abc", 'the chart digest pinned for karpenter is "sha256:abc"'),
        ("karpenter", "1.14.1", 'the chart digest pinned for karpenter is "1.14.1"'),
    ],
)
def test_a_digest_the_source_cannot_honour_fails_the_render(chart, digest, message) -> None:
    with pytest.raises(weave().WeaveError, match=re.escape(message)):
        _render(_element(chart), chartDigest=digest)


def test_with_no_image_digest_the_load_balancer_controller_runs_the_charts_own_tag() -> None:
    values = _values(_render(_element("aws-load-balancer-controller"), imageTag="", imageDigest=""))
    assert "image" not in values, values
    assert values["clusterName"] == "dfe-aws-test"


def test_a_pinned_image_digest_reaches_the_load_balancer_controller_as_tag_at_digest() -> None:
    element = _element("aws-load-balancer-controller")
    values = _values(_render(element, imageTag="v3.6.0", imageDigest=IMAGE_DIGEST))
    assert values["image"] == {"tag": f"v3.6.0@{IMAGE_DIGEST}"}
    assert values["enableCertManager"] is True, "the rest of the block still renders"


def test_an_image_digest_with_no_tag_fails_the_render() -> None:
    message = "imageDigest is set for aws-load-balancer-controller with no imageTag"
    with pytest.raises(weave().WeaveError, match=re.escape(message)):
        _render(_element("aws-load-balancer-controller"), imageTag="", imageDigest=IMAGE_DIGEST)


def test_no_other_addon_takes_the_image_override() -> None:
    for element in _elements(_appset()):
        if element["chart"] == "aws-load-balancer-controller":
            continue
        values = _values(_render(element, imageTag="v1", imageDigest=IMAGE_DIGEST))
        assert "image" not in values, element["chart"]


def test_a_stack_that_pins_the_digests_writes_them_where_the_render_reads_them() -> None:
    """check_versions_drift.py --fix is what carries a versions.yaml pin into the appset."""
    versions = {
        **drift.load_versions(),
        "services-digests.karpenter": CHART_DIGEST,
        "services.aws-load-balancer-controller-image": "v3.6.0",
        "services-digests.aws-load-balancer-controller-image": IMAGE_DIGEST,
    }
    writes, _fixed, refused = drift.plan_fix(versions)
    assert refused == []
    appset = _appset(writes[ADDONS])

    karpenter = _source(_render(_element("karpenter", appset), appset))
    assert karpenter["targetRevision"] == CHART_DIGEST
    alb = _values(_render(_element("aws-load-balancer-controller", appset), appset))
    assert alb["image"] == {"tag": f"v3.6.0@{IMAGE_DIGEST}"}
    keda = _source(_render(_element("keda", appset), appset))
    assert keda["targetRevision"] == _element("keda")["version"]
