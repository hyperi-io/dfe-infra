#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_aws_image_registry.py
#  Purpose:      Prove the AWS cascade gives every dfe-* image a registry and
#                every pod the pull secret bootstrap.sh creates.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""global.registry and imagePullSecrets on the AWS values cascade.

An image reference with no registry prefix is not an error anywhere in the
chain: helm renders `dfe-<component>:<tag>`, containerd resolves that against
Docker Hub, and the kubelet reports a pull denial naming Docker Hub rather than
the missing setting -- so the obvious diagnosis is a bad pull secret, which is
the wrong one.

No cloud values file carries a registry literal, because the dial is the one
source. The value reaches a chart as the dfe.hyperi.io/registry cluster-secret
annotation, which the appsets pass back as the global.registry parameter. These
tests hold the bootstrap and chart ends of that chain;
test_appset_registry_guard.py holds the appset end.

    python3 scripts/tests/test_aws_image_registry.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"

REGISTRY = "registry.example.com/dfe"
PULL_SECRET = "ghcr-pull-secret"
ANNOTATION = "dfe.hyperi.io/registry"

# One first-party chart per appset that renders a dfe-* image (layer2-apps and
# layer2-platform), so a chart left without the parameter shows up as its own
# failure rather than hiding behind a sibling.
CHARTS = ("dfe-ui", "dfe-toolbox")

# Every cloud values file an appset layers on common.yaml.
CLOUDS = ("aws.yaml", "azure.yaml", "gcp.yaml", "local.yaml", "local-dfe.yaml")


def render(chart: str, registry: str, cloud: str = "aws.yaml") -> list[dict]:
    """Every object one chart renders on a cloud cascade the appsets layer."""
    cmd = [
        "helm", "template", "t", str(chart_dir(chart)),
        "-f", str(VALUES / "common.yaml"),
        "-f", str(VALUES / cloud),
    ]
    if registry:
        cmd += ["--set", f"global.registry={registry}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template {chart} failed:\n{out.stderr}")
    return [doc for doc in yaml.safe_load_all(out.stdout) if isinstance(doc, dict)]


def pod_specs(docs: list[dict]) -> list[dict]:
    """The pod spec of every workload kind the charts here render."""
    specs = []
    for doc in docs:
        spec = doc.get("spec") or {}
        template = spec.get("template") or {}
        # A CronJob nests one more level than a Deployment or a Job.
        job = (spec.get("jobTemplate") or {}).get("spec") or {}
        template = template or (job.get("template") or {})
        if template.get("spec"):
            specs.append(template["spec"])
    return specs


def dfe_images(spec: dict) -> list[str]:
    containers = list(spec.get("containers") or []) + list(spec.get("initContainers") or [])
    return [c["image"] for c in containers if "dfe-" in c.get("image", "")]


def test_every_dfe_image_carries_the_registry() -> None:
    for chart in CHARTS:
        specs = pod_specs(render(chart, REGISTRY))
        expect(f"{chart} renders a pod spec", specs != [], "nothing to inspect")
        for spec in specs:
            for image in dfe_images(spec):
                expect(
                    f"{chart}: {image} starts with the registry",
                    image.startswith(f"{REGISTRY}/"),
                    f"{image!r} resolves against Docker Hub, which publishes no DFE image",
                )


def test_the_cascade_alone_names_no_registry() -> None:
    """Every cloud file stays literal-free, so the appset parameter is the only
    source and a chart deployed without it is a caught failure."""
    for cloud in CLOUDS:
        for chart in CHARTS:
            for spec in pod_specs(render(chart, "", cloud)):
                for image in dfe_images(spec):
                    expect(
                        f"{cloud} {chart}: {image} has no host without the parameter",
                        "/" not in image.split(":")[0],
                        f"{cloud} has grown a registry literal, a second source beside the dial",
                    )


def test_every_pod_carries_the_pull_secret() -> None:
    """bootstrap.sh creates ghcr-pull-secret in every DFE namespace, and a pod
    that does not name it pulls anonymously."""
    for chart in CHARTS:
        for spec in pod_specs(render(chart, REGISTRY)):
            if not dfe_images(spec):
                continue
            names = [entry.get("name") for entry in spec.get("imagePullSecrets") or []]
            expect(
                f"{chart} pod names {PULL_SECRET}",
                PULL_SECRET in names,
                f"imagePullSecrets was {names}",
            )


def test_bootstrap_refuses_an_empty_registry() -> None:
    """Silence here is the whole failure: an unset registry deploys and only
    shows up as a pull denial against the wrong host."""
    script = BOOTSTRAP.read_text()
    expect("bootstrap.sh requires DFE_REGISTRY", "\n  DFE_REGISTRY\n" in script,
           "DFE_REGISTRY is not in required_vars")


def test_the_cluster_secret_carries_the_registry() -> None:
    template = CLUSTER_SECRET.read_text()
    expect(f"cluster-secret.yaml.tpl writes {ANNOTATION}",
           f'{ANNOTATION}: "${{DFE_REGISTRY}}"' in template,
           "the annotation is not written from DFE_REGISTRY")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
