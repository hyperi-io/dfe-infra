#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_image_digest_pin.py
#  Purpose:      Prove the versions.yaml digests reach a rendered manifest, so a
#                re-pushed tag cannot land bytes the stack never certified.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for dfe-common.image and the digest half of every app pin.

versions.yaml pins tag@sha256, `dfe-stack render` emits tag@sha256 and
`dfe-stack verify` re-fetches every digest -- and none of that reaches the
cluster, because the charts are what deploy. Registry tags are mutable, so a
chart that renders a bare tag can pull bytes other than the ones verified.

Three things are checked:

1. Every app whose digest versions.yaml records renders `:tag@sha256:<that>`.
2. The library helper is OPTIONAL -- a chart with no digest renders exactly the
   reference it rendered before, including with no `global:` block at all.
3. The digest a chart carries IS the SSoT's, not a stale copy of it.

    python3 scripts/tests/test_image_digest_pin.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHARTS = REPO_ROOT / "helm" / "charts"
REGISTRY = "ghcr.io/hyperi-io"

_failures = 0


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


def _parse_nested(text: str) -> dict:
    """The versions.yaml reader the drift check uses, kept local to stay dep-free."""
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        m = re.match(r'^([A-Za-z0-9_.-]+):\s*(?:"([^"]*)"|([^#]*?))?\s*(?:#.*)?$', raw.strip())
        if not m:
            continue
        key, quoted = m.group(1), m.group(2)
        value = quoted if quoted is not None else (m.group(3) or "").strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "" and quoted is None:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = value
    return root


def current_stack() -> dict:
    root = _parse_nested((REPO_ROOT / "versions.yaml").read_text(encoding="utf-8"))
    return root["stacks"][root["current"]]


def render(chart: str, *sets: str) -> list[dict]:
    cmd = ["helm", "template", chart, str(CHARTS / chart)]
    for s in sets:
        cmd += ["--set", s]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart} {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def images(docs: list[dict]) -> list[str]:
    """Every container image a render emits, init containers included."""
    found: list[str] = []
    for doc in docs:
        spec = doc.get("spec", {})
        pod = spec.get("template", {}).get("spec") or spec.get("spec")
        if not isinstance(pod, dict):
            continue
        for key in ("initContainers", "containers"):
            for container in pod.get(key) or []:
                if container.get("image"):
                    found.append(container["image"])
    return found


def test_every_published_app_renders_its_digest() -> None:
    """The pin has to survive the chart, which is the only thing that deploys."""
    stack = current_stack()
    apps, digests = stack["apps"], stack["digests"]
    for app in sorted(digests):
        chart = CHARTS / app
        if not (chart / "Chart.yaml").exists():
            continue
        rendered = images(render(app, f"global.registry={REGISTRY}"))
        want = f"{REGISTRY}/{app}:{apps[app]}@{digests[app]}"
        expect(
            f"{app} renders tag@sha256 from the SSoT",
            rendered and all(i == want for i in rendered),
            f"wanted {want}, got {rendered}",
        )


def test_the_engine_sidecar_workloads_carry_the_digest_too() -> None:
    """hunt-runner and keda-shim run the ENGINE image out of the same chart."""
    stack = current_stack()
    want = f"{REGISTRY}/dfe-engine:{stack['apps']['dfe-engine']}@{stack['digests']['dfe-engine']}"
    for template in ("hunt-runner", "keda-shim"):
        docs = render("dfe-engine", f"global.registry={REGISTRY}", f"{template}.enabled=true")
        rendered = [
            i
            for doc in docs
            if doc.get("metadata", {}).get("name", "").endswith(template)
            for i in images([doc])
        ]
        expect(
            f"dfe-{template} renders the pinned engine digest",
            rendered and all(i == want for i in rendered),
            f"wanted {want}, got {rendered}",
        )


def test_the_schema_job_runs_the_pinned_engine_image() -> None:
    """dfe-schema is the engine image under another entry point, not its own app."""
    stack = current_stack()
    want = f"{REGISTRY}/dfe-engine:{stack['apps']['dfe-engine']}@{stack['digests']['dfe-engine']}"
    rendered = images(render("dfe-schema", f"global.registry={REGISTRY}"))
    expect(
        "the dfe-schema Job renders the pinned engine digest",
        rendered and all(i == want for i in rendered),
        f"wanted {want}, got {rendered}",
    )


def test_the_hyperdx_dashboards_init_container_carries_the_digest() -> None:
    """A second copy of the engine pin, in a chart named after another app.

    Its own helper builds the reference, so dfe-common.image's digest branch
    does not cover it -- this is the only thing that reads the rendered result.
    """
    stack = current_stack()
    want = f"{REGISTRY}/dfe-engine:{stack['apps']['dfe-engine']}@{stack['digests']['dfe-engine']}"
    rendered = [
        container["image"]
        for doc in render("hyperdx", f"global.registry={REGISTRY}")
        for container in (doc.get("spec", {}).get("template", {}).get("spec", {}) or {}).get(
            "initContainers"
        )
        or []
    ]
    expect(
        "the hyperdx dashboards init container renders the pinned engine digest",
        rendered and all(i == want for i in rendered),
        f"wanted {want}, got {rendered}",
    )


def test_a_chart_with_no_digest_renders_what_it_always_did() -> None:
    """The helper is optional: an unpublished app has no digest to carry.

    Rendered with no `global:` block at all, which is what bare `helm lint` and
    a chart-only template do -- the nil-guard case.
    """
    rendered = images(render("dfe-transform-wasm"))
    expect(
        "no digest and no global block still renders a plain repo:tag",
        rendered == ["dfe-transform-wasm:2.2.0"],
        f"{rendered}",
    )


def test_the_links_page_keeps_its_third_party_digest() -> None:
    """The one third-party chart on this path; its tag floats, so the digest is
    the only immutable half it has."""
    stack = current_stack()
    want = (
        f"nginxinc/nginx-unprivileged:{stack['services']['nginx-unprivileged']}"
        f"@{stack['services-digests']['nginx-unprivileged']}"
    )
    rendered = images(render("links"))
    expect(
        "links renders the floating tag pinned by digest",
        rendered == [want],
        f"wanted {want}, got {rendered}",
    )


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'FAILED' if _failures else 'ALL PASSED'} -- {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
