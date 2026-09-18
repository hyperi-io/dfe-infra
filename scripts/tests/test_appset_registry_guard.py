#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_appset_registry_guard.py
#  Purpose:      Prove an appset refuses to render when the cluster secret
#                carries no registry, instead of rolling every dfe-* image
#                onto Docker Hub.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The global.registry parameter each appset hands its charts, rendered for real.

An appset reads the registry with `index .metadata.annotations`, and `index`
returns an empty string for a missing key even under missingkey=error. An empty
helm parameter beats every values file, so a cluster secret written before the
registry fact existed rolls every dfe-* image onto Docker Hub, and the only
symptom is a pull denial naming the wrong registry.

Each parameter string is rendered through helm's template engine, which carries
the same Sprig `fail` the Argo CD appset controller keeps, so a missing or empty
annotation has to stop the render with the guard's own message.

    python3 scripts/tests/test_appset_registry_guard.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APPSETS = REPO_ROOT / "argocd" / "appsets"

ANNOTATION = "dfe.hyperi.io/registry"
REGISTRY = "registry.example.com/dfe"

# Every appset that deploys a chart building a dfe-* image from global.registry.
# layer2-data, edge and deploy-repo are absent on purpose: their charts name
# their images outright, so a missing registry fact changes nothing they render.
GUARDED = (
    "layer2-apps.yaml",
    "layer2-platform.yaml",
    "layer-scale.yaml",
)

CHART_YAML = "apiVersion: v2\nname: guard\nversion: 0.0.0\n"
TEMPLATE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: guard
data:
  registry: {{ tpl .Values.expr (dict "metadata" .Values.metadata "Template" .Template) | quote }}
"""


def registry_params(appset: str) -> list[str]:
    """Every global.registry parameter value one appset file sets."""
    found: list[str] = []
    pending: list[object] = list(yaml.safe_load_all((APPSETS / appset).read_text(encoding="utf-8")))
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if node.get("name") == "global.registry" and "value" in node:
                found.append(str(node["value"]))
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return found


def render(expr: str, annotations: dict[str, str]) -> subprocess.CompletedProcess:
    """Render one parameter string against a cluster secret's annotations."""
    with tempfile.TemporaryDirectory(prefix="appset-guard-") as tmp:
        chart = Path(tmp)
        (chart / "templates").mkdir()
        (chart / "Chart.yaml").write_text(CHART_YAML, encoding="utf-8")
        (chart / "templates" / "cm.yaml").write_text(TEMPLATE, encoding="utf-8")
        values = {"expr": expr, "metadata": {"annotations": annotations}}
        (chart / "values.yaml").write_text(yaml.safe_dump(values), encoding="utf-8")
        return subprocess.run(
            ["helm", "template", "guard", str(chart)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )


def rendered_registry(out: subprocess.CompletedProcess) -> str | None:
    for doc in yaml.safe_load_all(out.stdout):
        if isinstance(doc, dict) and doc.get("kind") == "ConfigMap":
            return (doc.get("data") or {}).get("registry")
    return None


def test_every_guarded_appset_passes_the_registry() -> None:
    for appset in GUARDED:
        expect(
            f"{appset} sets global.registry",
            registry_params(appset) != [],
            "its charts fall back to a values-file registry, and aws.yaml carries none",
        )


def test_a_missing_registry_stops_the_render() -> None:
    for appset in GUARDED:
        for expr in registry_params(appset):
            for label, annotations in (("absent", {}), ("empty", {ANNOTATION: ""})):
                out = render(expr, annotations)
                expect(
                    f"{appset}: an {label} {ANNOTATION} fails the render",
                    out.returncode != 0 and ANNOTATION in out.stderr,
                    "it renders an empty global.registry, which beats every values file "
                    "and sends each dfe-* image to Docker Hub",
                )


def test_a_present_registry_renders_through() -> None:
    for appset in GUARDED:
        for expr in registry_params(appset):
            out = render(expr, {ANNOTATION: REGISTRY})
            expect(
                f"{appset} renders {ANNOTATION} as the registry",
                out.returncode == 0 and rendered_registry(out) == REGISTRY,
                f"exit {out.returncode}, stderr {out.stderr.strip()!r}",
            )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
