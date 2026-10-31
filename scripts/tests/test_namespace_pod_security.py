#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_namespace_pod_security.py
#  Purpose:      Hold every namespace an Application creates to the Pod Security
#                labels it is meant to carry.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Pod Security Admission labels on the namespaces Argo CD creates.

Every Application that syncs with CreateNamespace=true states the labels through
managedNamespaceMetadata, since a namespace Argo creates carries none of its own.
A DFE namespace enforces baseline and warns and audits at restricted. The otel
collector's namespace enforces privileged, because its DaemonSet mounts the
node's / and /var/log/pods, and so does the tunnel's, because its pod holds
NET_ADMIN, a tun hostPath and a privileged init container. kube-system and default are Kubernetes's own, so
they are labelled privileged on all three modes, the cluster's default, because
Argo cannot leave a key out of managedNamespaceMetadata.

    python3 -m pytest scripts/tests/test_namespace_pod_security.py -q

Needs helm on PATH: the label expressions are Go templates, expanded as Argo does.
"""

import pytest
import yaml

from _weave import REPO_ROOT, helm, weave

PREFIX = "pod-security.kubernetes.io/"
BASELINE = {"enforce": "baseline", "warn": "restricted", "audit": "restricted"}
PRIVILEGED = {"enforce": "privileged", "warn": "privileged", "audit": "privileged"}
# The two namespaces holding a workload the baseline level refuses: the collector's DaemonSet mounts
# node paths, and the tunnel holds NET_ADMIN, a tun hostPath and a privileged init container.
ADMITTED_PRIVILEGED = {"enforce": "privileged", "warn": "restricted", "audit": "restricted"}
EXPECTED = {
    "otel": ADMITTED_PRIVILEGED,
    "culvert": ADMITTED_PRIVILEGED,
    "kube-system": PRIVILEGED,
    "default": PRIVILEGED,
}
FILES = sorted(
    [*(REPO_ROOT / "argocd" / "appsets").glob("*.yaml"), REPO_ROOT / "argocd/bootstrap/envoy-gateway-app.yaml"]
)


def templates() -> list[tuple[str, dict]]:
    """(label, Application template) of each Application the files declare."""
    found = []
    for path in FILES:
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(doc, dict):
                continue
            body = doc["spec"]["template"] if doc.get("kind") == "ApplicationSet" else doc
            name = doc["metadata"]["name"]
            found.append((f"{path.relative_to(REPO_ROOT)}:{name}", body))
    return found


def namespaces(body: dict, path: str) -> list[str]:
    """The namespaces an Application template can deploy into: its list elements', or the literal it names."""
    destination = body["spec"]["destination"]["namespace"]
    if destination != "{{ .namespace }}":
        return [destination if "{{" not in destination else "dfe"]
    doc = next(
        d
        for d in yaml.safe_load_all((REPO_ROOT / path.split(":")[0]).read_text(encoding="utf-8"))
        if d["metadata"]["name"] == path.split(":")[1]
    )
    return sorted(
        {
            e["namespace"]
            for g in doc["spec"]["generators"]
            for n in (g.get("matrix") or {}).get("generators") or []
            for e in (n.get("list") or {}).get("elements") or []
        }
    )


CASES = [(label, body) for label, body in templates() if "CreateNamespace=true" in body["spec"]["syncPolicy"].get("syncOptions", [])]


def labels_for(body: dict, namespace: str) -> dict[str, str]:
    """The Pod Security labels the Application sets on `namespace`, its Go templates expanded."""
    declared = body["spec"]["syncPolicy"]["managedNamespaceMetadata"]["labels"]
    expanded = weave().expand_template({"labels": declared}, {"namespace": namespace}, helm())["labels"]
    return {k.removeprefix(PREFIX): v for k, v in expanded.items() if k.startswith(PREFIX)}


@pytest.mark.parametrize(("label", "body"), CASES, ids=[label for label, _ in CASES])
def test_a_namespace_an_application_creates_carries_its_pod_security_labels(label: str, body: dict) -> None:
    for namespace in namespaces(body, label):
        assert labels_for(body, namespace) == EXPECTED.get(namespace, BASELINE), (label, namespace)


def test_no_application_creating_a_namespace_leaves_its_labels_unstated() -> None:
    """A new Application that creates a namespace without stating its labels fails here."""
    assert [label for label, body in CASES if "managedNamespaceMetadata" not in body["spec"]["syncPolicy"]] == []
