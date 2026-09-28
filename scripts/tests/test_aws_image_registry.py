#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_aws_image_registry.py
#  Purpose:      Prove the AWS cascade gives every dfe-* image a registry, and
#                that a pod names a pull secret only when bootstrap.sh made one.
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

The pull secret follows the same chain. bootstrap.sh creates ghcr-pull-secret
only from DFE_PULL_SECRET_TOKEN and records the name, or nothing, on the
dfe.hyperi.io/image_pull_secret annotation, which the appsets hand to the charts.
A public-image deploy creates none, and a pod naming one anyway warns
FailedToRetrieveImagePullSecret on every pull.

    python3 scripts/tests/test_aws_image_registry.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
APPSETS = REPO_ROOT / "argocd" / "appsets"
BOOTSTRAP = REPO_ROOT / "bootstrap" / "bootstrap.sh"
CLUSTER_SECRET = REPO_ROOT / "bootstrap" / "templates" / "cluster-secret.yaml.tpl"

REGISTRY = "registry.example.com/dfe"
PULL_NAME = "ghcr-pull-secret"
ANNOTATION = "dfe.hyperi.io/registry"
PULL_ANNOTATION = "dfe.hyperi.io/image_pull_secret"

# (appset file, ApplicationSet name) for every appset that layers a cloud overlay
# over charts rendering dfe-common.imagePullSecrets.
PULL_APPSETS = (
    ("layer2-apps.yaml", "dfe-layer2-apps"),
    ("layer2-data.yaml", "dfe-layer2-data"),
    ("layer2-platform.yaml", "dfe-layer2-platform"),
    ("layer2-deploy-repo.yaml", "dfe-layer2-deploy-repo"),
    ("layer-scale.yaml", "dfe-scale-apps"),
)

# One first-party chart per layer 2 appset, so a chart left without the
# parameter shows up as its own failure rather than hiding behind a sibling.
CHARTS = ("dfe-ui", "otel-collector", "dfe-toolbox")

# Every cloud values file an appset layers on common.yaml.
CLOUDS = ("aws.yaml", "azure.yaml", "gcp.yaml", "local.yaml", "local-dfe.yaml")


def render(
    chart: str, registry: str, cloud: str = "aws.yaml", pull_name: str = ""
) -> list[dict]:
    """Every object one chart renders on a cloud cascade the appsets layer.

    pull_name stands in for what the appsets' values block hands the chart
    when the cluster secret records one.
    """
    cmd = [
        "helm", "template", "t", str(chart_dir(chart)),
        "-f", str(VALUES / "common.yaml"),
        "-f", str(VALUES / cloud),
    ]
    if registry:
        cmd += ["--set", f"global.registry={registry}"]
    if pull_name:
        cmd += ["--set", f"imagePullSecrets[0]={pull_name}"]
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


def pull_refs(spec: dict) -> list[str]:
    return [entry.get("name") for entry in spec.get("imagePullSecrets") or []]


def test_no_cloud_overlay_names_a_pull_secret_bootstrap_may_not_create() -> None:
    """A public-image deploy creates no secret, so the cascade alone names none."""
    for cloud in CLOUDS:
        for chart in CHARTS:
            for spec in pod_specs(render(chart, REGISTRY, cloud)):
                expect(
                    f"{cloud} {chart}: a pod names no pull secret without the appset's",
                    pull_refs(spec) == [],
                    "the pod names a pull secret nothing created",
                )


def test_every_pod_carries_the_pull_secret_bootstrap_created() -> None:
    """With the appset handing the name on, every dfe-* pod names it."""
    for chart in CHARTS:
        for spec in pod_specs(render(chart, REGISTRY, pull_name=PULL_NAME)):
            if not dfe_images(spec):
                continue
            expect(
                f"{chart} pod names {PULL_NAME}",
                PULL_NAME in pull_refs(spec),
                f"the pod does not name {PULL_NAME}",
            )


def values_block(appset: str, name: str) -> str:
    """The Go-templated `values` block one ApplicationSet hands its charts."""
    for doc in yaml.safe_load_all((APPSETS / appset).read_text(encoding="utf-8")):
        if doc and doc.get("metadata", {}).get("name") == name:
            spec = doc["spec"]["template"]["spec"]
            source = spec.get("source") or spec["sources"][0]
            return source["helm"].get("values") or ""
    raise SystemExit(f"{appset} carries no ApplicationSet {name}")


def evaluate_block(block: str, annotations: dict[str, str]) -> dict:
    """The block as Argo renders it for one cluster secret, through helm's own Go
    template engine. The appset reads cluster facts at .metadata and .app, which a
    chart template reaches under .Values."""
    text = re.sub(r"(?<![\w$)\]])\.(metadata|app)\b", r".Values.\1", block)
    with tempfile.TemporaryDirectory() as tmp:
        chart = Path(tmp) / "block"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text(
            "apiVersion: v2\nname: block\nversion: 0.1.0\n", encoding="utf-8"
        )
        (chart / "templates" / "values.yaml").write_text(text, encoding="utf-8")
        context = Path(tmp) / "context.yaml"
        context.write_text(
            yaml.safe_dump({"metadata": {"annotations": annotations}, "app": "dfe-ui"}),
            encoding="utf-8",
        )
        out = subprocess.run(
            ["helm", "template", "t", str(chart), "-f", str(context)],
            capture_output=True, text=True, check=False,
        )
    if out.returncode != 0:
        raise SystemExit(f"the values block does not render:\n{out.stderr}")
    docs = [d for d in yaml.safe_load_all(out.stdout) if isinstance(d, dict)]
    return docs[0] if docs else {}


def test_every_appset_names_the_pull_secret_only_when_one_was_created() -> None:
    """A recorded name is named and a recorded empty names none. A cluster secret
    written before the fact existed keeps the name the cloud overlays used to set."""
    base = {ANNOTATION: REGISTRY}
    cases = (
        ("recorded", {**base, PULL_ANNOTATION: PULL_NAME}, [PULL_NAME]),
        ("recorded empty", {**base, PULL_ANNOTATION: ""}, None),
        ("an older cluster secret", base, [PULL_NAME]),
    )
    for appset, name in PULL_APPSETS:
        block = values_block(appset, name)
        for label, facts, want in cases:
            got = evaluate_block(block, facts).get("imagePullSecrets")
            expect(f"{appset} {label}: imagePullSecrets is {want}", got == want,
                   "the block rendered a different list")


def test_bootstrap_records_the_pull_secret_only_when_it_creates_one() -> None:
    """The annotation's value is derived from the token that creates the secret."""
    script = BOOTSTRAP.read_text(encoding="utf-8")
    line = re.search(r"^export DFE_IMAGE_PULL_SECRET=.*$", script, re.M)
    expect("bootstrap.sh derives DFE_IMAGE_PULL_SECRET", line is not None)
    for given, want in (("placeholder", PULL_NAME), ("", "")):
        out = subprocess.run(
            ["env", f"DFE_PULL_SECRET_TOKEN={given}", "bash", "-c",
             f'{line.group(0) if line else ""}\nprintf %s "$DFE_IMAGE_PULL_SECRET"'],
            capture_output=True, text=True, check=False,
        )
        expect(f"{'set' if given else 'unset'} records {want!r}", out.stdout == want,
               f"got {out.stdout!r}")
    template = CLUSTER_SECRET.read_text(encoding="utf-8")
    expect(f"cluster-secret.yaml.tpl writes {PULL_ANNOTATION}",
           f'{PULL_ANNOTATION}: "${{DFE_IMAGE_PULL_SECRET}}"' in template,
           "the annotation is not written from DFE_IMAGE_PULL_SECRET")


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
