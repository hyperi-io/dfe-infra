#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         _weave.py
#  Purpose:      Render and diff an app through scripts/dfe-weave for the
#                chart-switch gate tests, so each gate test is a call and an
#                assert.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""render_app and diff_app -- scripts/dfe-weave, called from a test.

Render (a) is the 2.2.0 chart under helm/charts, render (b) the thin chart
assembled from the app's contract beside helm/charts/dfe-extras, both layered as
argocd/appsets/layer2-apps.yaml layers them, with the per-app integration values
from argocd/values/apps. A gate test is then a few lines:

    from _weave import diff_app

    def test_receiver_keeps_its_objects() -> None:
        report = diff_app("dfe-receiver", "scale", "aws")
        assert report["failed"] == [], report["facets"]["objects"]

Contracts are read from fixtures/contracts/<service>.json, keyed by the
``deploy.service`` the appset generates from (``hyperdx``, not ``dfe-hyperdx``).
Render (b) needs the scalo-service library: DFE_WEAVE_LIBRARY names a chart
directory, and without it the pinned release is pulled anonymously from GHCR by
its manifest digest, once per run. DFE_WEAVE_HELM picks the helm binary, default
``helm`` on PATH.

The leading underscore keeps pytest from collecting this module as a test file.
"""

import functools
import importlib.machinery
import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path
from types import ModuleType

import pytest
import yaml

TESTS = Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-weave"
CONTRACTS = TESTS / "fixtures" / "contracts"

# The scalo-service release render (b) uses when DFE_WEAVE_LIBRARY is unset. The
# pull names the manifest digest, so a re-pushed tag cannot change what renders.
LIBRARY_CHART = "oci://ghcr.io/hyperi-io/charts/scalo-service"
LIBRARY_VERSION = "2.14.3"
LIBRARY_DIGEST = "sha256:efa38d0f4e01858a9f8a02c34d99ea1ac27707dda7bab006496b3255149d8258"


@functools.cache
def weave() -> ModuleType:
    """scripts/dfe-weave as a module; it has no .py suffix, so the loader is told it is source."""
    loader = importlib.machinery.SourceFileLoader("dfe_weave", str(SCRIPT))
    spec = importlib.util.spec_from_loader("dfe_weave", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def helm() -> str:
    """The helm binary the renders use."""
    return os.environ.get("DFE_WEAVE_HELM", "helm")


@functools.cache
def _pulled_library() -> tuple[tempfile.TemporaryDirectory | None, Path | str]:
    """The pinned library pulled into a scratch directory, or why it could not be.

    The directory object is returned with the path so it lives as long as the
    cache does, and goes when the run ends. A failure is cached too, so an
    offline run reports it once per test rather than retrying the pull.
    """
    scratch = tempfile.TemporaryDirectory(prefix="dfe-weave-library-")
    reference = f"{LIBRARY_CHART}@{LIBRARY_DIGEST}"
    cmd = [helm(), "pull", reference, "--untar", "--destination", scratch.name]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
    except OSError as exc:
        return None, f"cannot run {cmd[0]}: {exc}"
    if out.returncode != 0:
        return None, out.stderr.strip()
    chart = Path(scratch.name) / "scalo-service"
    meta = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8")) or {}
    if str(meta.get("version")) != LIBRARY_VERSION:
        return None, f"{reference} is version {meta.get('version')}, not {LIBRARY_VERSION}"
    return scratch, chart


def library() -> Path:
    """The scalo-service chart directory: DFE_WEAVE_LIBRARY, else the pinned release from GHCR."""
    value = os.environ.get("DFE_WEAVE_LIBRARY")
    if value:
        path = Path(value).resolve()
        if not (path / "Chart.yaml").is_file():
            pytest.fail(f"DFE_WEAVE_LIBRARY={value} holds no Chart.yaml")
        return path
    _, found = _pulled_library()
    if isinstance(found, str):
        pytest.fail(
            f"cannot pull {LIBRARY_CHART} {LIBRARY_VERSION} ({LIBRARY_DIGEST}): {found}\n"
            "set DFE_WEAVE_LIBRARY to a scalo-service chart directory to render offline"
        )
    return found


def contract(service: str) -> Path:
    """The committed contract for an app, by its deploy.service."""
    return CONTRACTS / f"{service}.json"


def _inputs(service: str, which: str, options: dict) -> object:
    assembles = which in ("new", "both") and options.get("chart") is None
    chart_library = options.pop("library", None)
    if assembles and chart_library is None:
        chart_library = library()
    return weave().Inputs(
        repo=options.pop("repo", REPO_ROOT),
        appset=options.pop("appset", weave().APPSET),
        old_ref=options.pop("old_ref", None),
        deploy_repo=options.pop("deploy_repo", None),
        apps_dir=options.pop("apps_dir", None),
        contract=options.pop("contract", contract(service) if assembles else None),
        library=chart_library,
        chart=options.pop("chart", None),
        extras=options.pop("extras", True),
        helm=options.pop("helm", helm()),
    )


def _target(service: str, profile: str, cloud: str, options: dict) -> object:
    w = weave()
    annotations = tuple(sorted(options.pop("annotations", {}).items()))
    labels = tuple(sorted(options.pop("labels", {}).items()))
    return w.Target(service, profile, cloud, annotations=annotations, labels=labels, **options)


def render_app(service: str, profile: str, cloud: str, which: str, **options: object) -> list[dict]:
    """One render's objects: ``which`` is ``old`` (the 2.2.0 chart) or ``new`` (the thin chart).

    Options are the dfe-weave inputs (``appset``, ``apps_dir``, ``deploy_repo``,
    ``old_ref``, ``contract``, ``library``, ``chart``, ``extras``, ``helm``) and the cluster
    facts (``instance``, ``namespace``, ``registry``, ``domain``, ``env``, and
    dicts ``annotations`` and ``labels``).
    """
    options = dict(options)
    inputs = _inputs(service, which, options)
    target = _target(service, profile, cloud, options)
    return weave().render_app(target, which, inputs).docs


def diff_app(service: str, profile: str, cloud: str, **options: object) -> dict:
    """The dfe-weave diff report for one app: ``report["facets"][name]["status"]`` and ``failed``.

    Takes the same options as render_app.
    """
    options = dict(options)
    inputs = _inputs(service, "both", options)
    target = _target(service, profile, cloud, options)
    return weave().diff_app(target, inputs)
