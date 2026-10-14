#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         _weave.py
#  Purpose:      Render an app through scripts/dfe-weave for the thin-chart
#                tests, so each test is a call and an assert.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""render_app and render_extras -- scripts/dfe-weave, called from a test.

render_app is the thin chart assembled from the app's contract beside
helm/charts/dfe-extras, layered as argocd/appsets/layer2-apps.yaml layers it, with
the per-app integration values from argocd/values/apps. render_extras is the
dfe-extras source alone, which needs no contract or library. A test turning a
dial hands the deployer's instance values as ``overlay`` and the deploy repo's
infra/common.yaml as ``infra``:

    from _weave import render_app, render_extras

    def test_the_loader_takes_its_grace() -> None:
        overlay = {"terminationGracePeriodSeconds": 0}
        docs = render_app("dfe-loader", "slim", "local", overlay=overlay)
        ...

Contracts are read from fixtures/contracts/<service>.json, keyed by the
``deploy.service`` the appset generates from (``hyperdx``, not ``dfe-hyperdx``).
The thin chart needs the scalo-service library: DFE_WEAVE_LIBRARY names a chart
directory, and without it the pinned release is pulled anonymously from GHCR by
its manifest digest, once per run. Each component's thin chart is assembled once
per run with the image pins versions.yaml carries. DFE_WEAVE_HELM picks the helm
binary, default ``helm`` on PATH.

An appset under argocd/appsets is read from a copy whose chart pins not yet
published carry RENDER_DIGEST, because the appset refuses to render them.
appset() gives that path to a test calling dfe-weave itself.

published_chart() pulls the thin chart the registry serves, by the digest
versions.yaml chart-digests pins, for render_app to take as ``chart``. It uses
whatever credentials helm already holds, which a private chart needs.

The leading underscore keeps pytest from collecting this module as a test file.
"""

import contextlib
import functools
import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import yaml

TESTS = Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-weave"
CONTRACTS = TESTS / "fixtures" / "contracts"
APPSETS = Path("argocd/appsets")

# The chart digest a render reads for a map entry not yet published. The thin
# chart is assembled locally, so the digest only has to have the shape the appset
# accepts.
RENDER_DIGEST = "sha256:" + "ab" * 32
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

# The scalo-service release a render uses when DFE_WEAVE_LIBRARY is unset. The
# pull names the manifest digest, so a re-pushed tag cannot change what renders.
LIBRARY_CHART = "oci://ghcr.io/hyperi-io/charts/scalo-service"
LIBRARY_VERSION = "2.14.13"
LIBRARY_DIGEST = "sha256:4de9923ea3c5eb1b0a8887121207c68c41526839c0f958dbb740b8f5c9d7e7ec"

# Where each component's thin chart is published, the registry its images come from.
CHART_REGISTRY = "oci://ghcr.io/hyperi-io/charts"


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


def chart_name(service: str) -> str:
    """The thin chart's name, which its contract's app_name sets."""
    return "dfe-hyperdx" if service == "hyperdx" else service


@functools.cache
def _assembled(service: str, library_dir: Path) -> tuple[tempfile.TemporaryDirectory, Path]:
    """One component's thin chart, assembled and built once per run.

    Cached with its scratch directory, as _pulled_library is, so every render of a
    component shares the one assembly.
    """
    w = weave()
    raw, parsed = w.read_contract(contract(service))
    tag, digest = w.pinned_image(REPO_ROOT, str(parsed.get("app_name")))
    scratch = tempfile.TemporaryDirectory(prefix=f"dfe-weave-thin-{service}-")
    chart = w.assemble(
        raw, library_dir, Path(scratch.name), version=tag.removeprefix("v"), tag=tag, digest=digest
    )
    w.build_dependency(helm(), chart)
    return scratch, chart


def assembled_chart(service: str) -> Path:
    """The thin chart for a component, from its committed contract and the pinned library."""
    return _assembled(service, library())[1]


def chart_digest(service: str) -> str:
    """versions.yaml chart-digests.<service> in the current stack; empty where it has none."""
    return drift().load_versions().get(f"chart-digests.{service}", "")


@functools.cache
def _pulled_chart(
    service: str, digest: str
) -> tuple[tempfile.TemporaryDirectory | None, Path | str]:
    """One published thin chart pulled into a scratch directory, or why it could not be.

    Cached with the directory object, as _pulled_library is, so every cell of a
    component renders the one pull and a failed pull is reported once.
    """
    name = chart_name(service)
    scratch = tempfile.TemporaryDirectory(prefix=f"dfe-weave-{name}-")
    reference = f"{CHART_REGISTRY}/{name}@{digest}"
    cmd = [helm(), "pull", reference, "--untar", "--destination", scratch.name]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
    except OSError as exc:
        return None, f"cannot run {cmd[0]}: {exc}"
    if out.returncode != 0:
        return None, out.stderr.strip()
    chart = Path(scratch.name) / name
    meta = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8")) or {}
    if meta.get("name") != name:
        return None, f"{reference} is chart {meta.get('name')!r}, not {name}"
    return scratch, chart


def published_chart(service: str) -> Path:
    """The thin chart the registry serves for a component, pulled by its chart-digests pin.

    Skips a component whose pin is not a digest yet, since there is nothing published
    to pull.
    """
    digest = chart_digest(service)
    if not DIGEST_RE.fullmatch(digest):
        pytest.skip(f"chart-digests.{service} is {digest!r}, not a published digest")
    _, found = _pulled_chart(service, digest)
    if isinstance(found, str):
        pytest.fail(f"cannot pull {CHART_REGISTRY}/{chart_name(service)}@{digest}: {found}")
    return found


@functools.cache
def drift() -> ModuleType:
    """scripts/check_versions_drift.py, which owns the format of an appset's chart pin map."""
    scripts = str(REPO_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("check_versions_drift")


def fill_chart_pins(text: str, digest: str = RENDER_DIGEST) -> str:
    """An appset's text with every chart pin that is not a sha256 digest set to ``digest``."""
    pin_map = drift().CHART_PIN_MAP.search(text)
    if pin_map is None:
        return text

    def fill(entry: re.Match[str]) -> str:
        if DIGEST_RE.fullmatch(entry["pin"]):
            return entry.group(0)
        return f'"{entry["service"]}" "{digest}"'

    body = drift().CHART_PIN_ENTRY.sub(fill, pin_map["body"])
    return text[: pin_map.start("body")] + body + text[pin_map.end("body") :]


@functools.cache
def _pinned_appsets() -> tuple[tempfile.TemporaryDirectory, Path]:
    """argocd/appsets with fill_chart_pins applied, written once per run.

    The directory object is returned with the path so it lives as long as the
    cache does, as _pulled_library's does.
    """
    scratch = tempfile.TemporaryDirectory(prefix="dfe-weave-appsets-")
    root = Path(scratch.name)
    for path in sorted((REPO_ROOT / APPSETS).glob("*.yaml")):
        text = fill_chart_pins(path.read_text(encoding="utf-8"))
        (root / path.name).write_text(text, encoding="utf-8", newline="\n")
    return scratch, root


def appset(name: str | Path = "layer2-apps.yaml") -> Path:
    """argocd/appsets/<name> as a render reads it, every unpublished chart pinned."""
    return _pinned_appsets()[1] / Path(name).name


def _routed(given: Path | None) -> Path:
    """The appset a render reads: one under argocd/appsets goes to its pinned copy."""
    path = Path(given) if given is not None else weave().APPSET
    if path.is_absolute() or path.parent != APPSETS:
        return path
    return appset(path)


@contextlib.contextmanager
def deploy_repo(
    service: str, instance: str = "default", overlay: dict | None = None, infra: dict | None = None
) -> Iterator[Path]:
    """A deploy repo holding ``overlay`` as the instance file and ``infra`` as infra/common.yaml.

    Either may be None, which leaves that layer missing as the appset allows.
    """
    with tempfile.TemporaryDirectory(prefix="dfe-weave-deploy-") as tmp:
        root = Path(tmp)
        if overlay is not None:
            body = {"deploy": {"service": service, "instance": instance}, **overlay}
            values = root / "values"
            values.mkdir()
            (values / f"{service}-{instance}-values.yaml").write_text(
                yaml.safe_dump(body), encoding="utf-8", newline="\n"
            )
        if infra is not None:
            (root / "infra").mkdir()
            (root / "infra" / "common.yaml").write_text(
                yaml.safe_dump(infra), encoding="utf-8", newline="\n"
            )
        yield root


def _inputs(service: str, options: dict) -> object:
    thin = options.pop("thin", True)
    chart = options.pop("chart", None)
    given_contract = options.pop("contract", None)
    chart_library = options.pop("library", None)
    if thin and chart is None and given_contract is None and chart_library is None:
        chart = assembled_chart(service)
    elif thin and chart is None:
        given_contract = given_contract or contract(service)
        chart_library = chart_library or library()
    return weave().Inputs(
        repo=options.pop("repo", REPO_ROOT),
        appset=_routed(options.pop("appset", None)),
        deploy_repo=options.pop("deploy_repo", None),
        apps_dir=options.pop("apps_dir", None),
        contract=given_contract,
        library=chart_library,
        chart=chart,
        extras=options.pop("extras", True),
        thin=thin,
        helm=options.pop("helm", helm()),
    )


def _target(service: str, profile: str, cloud: str, options: dict) -> object:
    w = weave()
    annotations = tuple(sorted(options.pop("annotations", {}).items()))
    labels = tuple(sorted(options.pop("labels", {}).items()))
    return w.Target(service, profile, cloud, annotations=annotations, labels=labels, **options)


def render_app(service: str, profile: str, cloud: str, **options: object) -> list[dict]:
    """One app's objects: its thin chart beside dfe-extras, as its Application deploys them.

    Options are the dfe-weave inputs (``appset``, ``apps_dir``, ``deploy_repo``,
    ``contract``, ``library``, ``chart``, ``extras``, ``thin``, ``helm``), the
    deployer's ``overlay`` and ``infra`` values in place of a ``deploy_repo``, and
    the cluster facts (``instance``, ``namespace``, ``registry``, ``domain``,
    ``env``, and dicts ``annotations`` and ``labels``). ``appset`` is routed as the
    module says.

    Raises:
        WeaveError: The appset or a chart refused the render, with its message.
    """
    options = dict(options)
    overlay = options.pop("overlay", None)
    infra = options.pop("infra", None)
    if overlay is None and infra is None:
        inputs = _inputs(service, options)
        target = _target(service, profile, cloud, options)
        return weave().render_app(target, inputs).docs
    if options.get("deploy_repo") is not None:
        raise ValueError("pass overlay and infra, or deploy_repo, not both")
    instance = str(options.get("instance", "default"))
    with deploy_repo(service, instance, overlay, infra) as root:
        options["deploy_repo"] = root
        inputs = _inputs(service, options)
        target = _target(service, profile, cloud, options)
        return weave().render_app(target, inputs).docs


def render_extras(service: str, profile: str, cloud: str, **options: object) -> list[dict]:
    """The dfe-extras objects alone for one app, with render_app's options.

    No contract or library is read, so the render runs offline.
    """
    return render_app(service, profile, cloud, thin=False, **options)
