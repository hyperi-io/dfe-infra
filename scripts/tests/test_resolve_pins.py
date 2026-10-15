#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_resolve_pins.py
#  Purpose:      Prove the tag -> digest resolver + writer: the registry lookup
#                (a loopback registry, never the network), that a registry nobody
#                could reach raises instead of reading as absent, the
#                fresh/stale/missing verdict, the cooldown hold, and that --write
#                rewrites ONLY the current stack's digests: while preserving
#                comments and formatting.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/registry_pins.py + scripts/resolve_pins.py (dfe-infra#116).

The registry reads run against a loopback OCI registry (_oci.FakeRegistry) that
answers the way ghcr.io answers an anonymous caller, and `docker buildx imagetools`
is stubbed at subprocess.run, so the suite is hermetic and runs on a bare CI image.
Runs under pytest, and standalone via the main() runner at the bottom (matching the
other tests in this dir).

    python3 -m pytest scripts/tests/test_resolve_pins.py
    python3 scripts/tests/test_resolve_pins.py
"""

import datetime
import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _oci import MANIFEST, FakeRegistry  # noqa: E402


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


registry_pins = _load("registry_pins")
resolve_pins = _load("resolve_pins")

ORG = "hyperi-io"
TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "GHCR_TOKEN")


# --- fixtures (published images + a synthetic versions.yaml) ------------------
def _iso(days_ago: float) -> str:
    when = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


# app -> (digest, tags, days since the image was built). dfe-engine is fresh (2d)
# and its recorded pin is STALE; dfe-loader is old (30d) and FRESH; dfe-ui is fresh
# (1d) and STALE (the cooldown target); dfe-fetcher's pinned tag is absent.
FAKE_PACKAGES = {
    "dfe-engine": [
        ("sha256:" + "a" * 64, ["v1.15.1"], 2.0),
        ("sha256:" + "0" * 64, ["v1.15.0"], 40.0),
    ],
    "dfe-loader": [
        ("sha256:" + "b" * 64, ["v1.18.21"], 30.0),
    ],
    "dfe-ui": [
        ("sha256:" + "c" * 64, ["v1.3.7"], 1.0),
    ],
    "dfe-fetcher": [
        ("sha256:" + "d" * 64, ["v1.4.99"], 10.0),  # not the pinned tag
    ],
}

# Recorded digests in the synthetic file: loader matches (fresh), engine + ui are
# wrong (stale), fetcher's tag will be missing.
SYNTHETIC_VERSIONS = """\
schema: 2
current: "9.9.0-rc.1"
latest: ""
stacks:

  9.9.0-rc.0:
    apps:
      dfe-engine: "v0.0.1"
    digests:
      dfe-engine: "sha256:oldstackdigest"

  9.9.0-rc.1:
    apps:
      dfe-engine: "v1.15.1"      # keep this comment
      dfe-loader: "v1.18.21"
      dfe-ui: "v1.3.7"
      dfe-fetcher: "v1.4.10"
      dfe-transform-wasm: "2.2.0"   # unpublished, no digest
    # digests block below -- immutable half of the pin
    digests:
      dfe-engine: "sha256:WRONGENGINE"
      dfe-loader: "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      dfe-ui: "sha256:WRONGUI"
      dfe-fetcher: "sha256:WRONGFETCHER"
"""


def _published() -> FakeRegistry:
    """A registry carrying every image FAKE_PACKAGES describes."""
    registry = FakeRegistry()
    for app, versions in FAKE_PACKAGES.items():
        for digest, tags, days_ago in versions:
            for tag in tags:
                registry.add(f"{ORG}/{app}", tag, digest, created=_iso(days_ago))
    return registry


def _fake_resolve(org, app, tag, registry=registry_pins.GHCR):
    for digest, tags, days_ago in FAKE_PACKAGES.get(app, []):
        if tag in tags:
            published = datetime.datetime.fromisoformat(_iso(days_ago))
            return registry_pins.Resolved(digest=digest, published=published)
    return None


def _install(monkeypatch, tmp_path):
    """Point resolve_pins at a temp versions.yaml and an in-memory registry."""
    vfile = tmp_path / "versions.yaml"
    vfile.write_text(SYNTHETIC_VERSIONS, encoding="utf-8")
    monkeypatch.setattr(resolve_pins, "VERSIONS", vfile)
    monkeypatch.setattr(resolve_pins, "resolve", _fake_resolve)
    return vfile


def _without_tokens():
    """Drop every GitHub token from this process, returning the values to restore."""
    return {name: os.environ.pop(name) for name in TOKEN_VARIABLES if name in os.environ}


def _restore_tokens(saved: dict[str, str]) -> None:
    os.environ.update(saved)


# --- registry_pins core, over the OCI API -------------------------------------
def test_resolve_returns_digest_and_build_time(monkeypatch):
    registry = _published()
    with registry.serving(monkeypatch):
        found = registry_pins.resolve(ORG, "dfe-engine", "v1.15.1")
    assert found is not None
    assert found.digest == "sha256:" + "a" * 64
    age = datetime.datetime.now(datetime.UTC) - found.published
    assert datetime.timedelta(days=1.9) < age < datetime.timedelta(days=2.1)


def test_resolve_reads_the_build_time_from_the_image_config(monkeypatch):
    """An index with no created annotation is dated by its platform image's config,
    a blob served from another host that refuses a request carrying the token."""
    registry = FakeRegistry()
    registry.add(f"{ORG}/dfe-loader", "v1.18.19", "sha256:" + "e" * 64, built="2026-08-18T04:19:10.6Z")
    with registry.serving(monkeypatch):
        found = registry_pins.resolve(ORG, "dfe-loader", "v1.18.19")
    assert found is not None
    assert found.published == datetime.datetime(2026, 8, 18, 4, 19, 10, 600000, tzinfo=datetime.UTC)
    stored = [r for r in registry.requests if r.path.startswith("/sha256:")]
    assert stored, "the config blob was never read from the store"
    assert all(r.authorization == "" for r in stored), "the token followed the redirect"


def test_resolve_is_none_for_an_absent_tag(monkeypatch):
    with _published().serving(monkeypatch):
        assert registry_pins.resolve(ORG, "dfe-engine", "v9.9.9") is None
        assert registry_pins.resolve_digest(ORG, "dfe-engine", "v9.9.9") is None


def test_package_tags_reads_every_page(monkeypatch):
    registry = FakeRegistry(page_size=2)
    for patch in range(5):
        registry.add(f"{ORG}/dfe-engine", f"v1.15.{patch}", "sha256:" + str(patch) * 64)
    with registry.serving(monkeypatch):
        tags = registry_pins.package_tags(ORG, "dfe-engine")
    assert sorted(tags) == [f"v1.15.{patch}" for patch in range(5)]
    pages = [r for r in registry.requests if "/tags/list" in r.path and r.authorization]
    assert len(pages) == 3


def test_an_unpublished_package_has_no_tags_and_no_digest(monkeypatch):
    """The registry grants no anonymous token for a repository it does not hold."""
    with _published().serving(monkeypatch):
        assert registry_pins.package_tags(ORG, "dfe-transform-wasm") == []
        assert registry_pins.resolve_digest(ORG, "dfe-transform-wasm", "2.2.0") is None


def test_every_read_is_anonymous(monkeypatch):
    """No GitHub token in the process, none sent: the only bearer is the one the
    registry issued for the pull."""
    registry = _published()
    saved = _without_tokens()
    try:
        with registry.serving(monkeypatch):
            assert registry_pins.resolve_digest(ORG, "dfe-loader", "v1.18.21") == "sha256:" + "b" * 64
            assert "v1.15.0" in registry_pins.package_tags(ORG, "dfe-engine")
    finally:
        _restore_tokens(saved)
    assert registry.token_requests(), "the registry was never asked for a token"
    assert all(r.authorization == "" for r in registry.token_requests())
    sent = {r.authorization for r in registry.requests} - {""}
    assert sent == {f"Bearer anon-{ORG}/dfe-loader", f"Bearer anon-{ORG}/dfe-engine"}, sent


def test_a_nested_chart_is_read_at_its_repository_path(monkeypatch):
    """A thin chart is the package charts/<chart>, which the registry reads as written."""
    chart_digest = "sha256:" + "e" * 64
    registry = FakeRegistry()
    registry.add(f"{ORG}/charts/dfe-engine", "1.22.16", chart_digest, kind=MANIFEST)
    with registry.serving(monkeypatch):
        assert registry_pins.resolve_digest(ORG, "charts/dfe-engine", "1.22.16") == chart_digest
    read = [r.path for r in registry.requests if r.authorization]
    assert read == [f"/v2/{ORG}/charts/dfe-engine/manifests/1.22.16"]


def test_a_registry_asking_for_credentials_is_an_error(monkeypatch):
    """A Basic challenge is a private registry, never an absent tag."""
    registry = _published()
    registry.basic = True
    raised = ""
    with registry.serving(monkeypatch):
        try:
            registry_pins.resolve_digest(ORG, "dfe-loader", "v1.18.21")
        except registry_pins.RegistryError as exc:
            raised = str(exc)
    assert "Basic credentials" in raised


def test_a_busy_registry_is_retried(monkeypatch):
    registry = _published()
    registry.fail = [503, 429]
    with registry.serving(monkeypatch):
        assert registry_pins.resolve_digest(ORG, "dfe-loader", "v1.18.21") == "sha256:" + "b" * 64


def test_a_registry_that_stays_busy_raises(monkeypatch):
    registry = _published()
    registry.fail = [503, 503, 503]
    raised = ""
    with registry.serving(monkeypatch):
        try:
            registry_pins.resolve_digest(ORG, "dfe-loader", "v1.18.21")
        except registry_pins.RegistryError as exc:
            raised = str(exc)
    assert "HTTP 503" in raised


def test_version_key_orders_numerically():
    assert registry_pins.version_key("v1.18.19") > registry_pins.version_key("v1.18.9")


# --- registry_pins digest resolution ------------------------------------------
_INDEX = "sha256:" + "9" * 64


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _fake_run(docker=None):
    """A subprocess.run stub for `docker`; an Exception value is raised.

    `docker` is either one value returned on every call, or a list consumed one
    entry per call -- popping past the end raises, which proves a caller did not
    make more calls than the list scripts.
    """

    def run(cmd, *a, **k):
        if cmd[0] != "docker":
            raise AssertionError(f"{cmd[0]} must not be called")
        if isinstance(docker, list):
            if not docker:
                raise AssertionError("docker called more times than scripted")
            proc = docker.pop(0)
        else:
            proc = docker
        if isinstance(proc, Exception):
            raise proc
        if proc is None:
            raise AssertionError("docker must not be called")
        return proc

    return run


def _no_registry(host):
    raise AssertionError(f"the OCI API must not be read for {host}")


def test_ref_digest_reads_the_digest_imagetools_reports(monkeypatch):
    monkeypatch.setattr(
        registry_pins.subprocess, "run", _fake_run(docker=_FakeProc(stdout=_INDEX + "\n"))
    )
    assert registry_pins.ref_digest("ghcr.io/hyperi-io/dfe-engine:v1.15.1") == (_INDEX, "")


def test_ref_raw_returns_what_imagetools_prints(monkeypatch):
    raw = '{"schemaVersion": 2, "manifests": []}'
    monkeypatch.setattr(registry_pins.subprocess, "run", _fake_run(docker=_FakeProc(stdout=raw)))
    assert registry_pins.ref_raw("ghcr.io/hyperi-io/dfe-engine:v1.15.1") == (raw, "")


def test_is_absent_tells_a_missing_tag_from_a_failed_read():
    assert registry_pins.is_absent("ERROR: ghcr.io/hyperi-io/x:v9: not found")
    assert registry_pins.is_absent("manifest unknown")
    assert not registry_pins.is_absent("cannot run docker buildx: [Errno 2] No such file")
    assert not registry_pins.is_absent("unexpected status: 403 Forbidden")


def test_tag_digest_prefers_imagetools_and_leaves_the_api_alone(monkeypatch):
    monkeypatch.setattr(
        registry_pins.subprocess, "run", _fake_run(docker=_FakeProc(stdout=_INDEX))
    )
    monkeypatch.setattr(registry_pins.oci_registry, "base_url", _no_registry)
    assert registry_pins.tag_digest(ORG, "dfe-engine", "v1.15.1") == _INDEX


def test_tag_digest_falls_back_to_the_oci_api_without_docker(monkeypatch):
    monkeypatch.setattr(
        registry_pins.subprocess, "run", _fake_run(docker=FileNotFoundError("docker"))
    )
    with _published().serving(monkeypatch):
        assert registry_pins.tag_digest(ORG, "dfe-engine", "v1.15.1") == "sha256:" + "a" * 64


def test_tag_digest_is_none_only_when_the_registry_says_not_found(monkeypatch):
    monkeypatch.setattr(
        registry_pins.subprocess,
        "run",
        _fake_run(docker=_FakeProc(returncode=1, stderr="ERROR: ghcr.io/o/a:v9.9.9: not found")),
    )
    registry = _published()
    registry.fail = [500, 500, 500]
    with registry.serving(monkeypatch):
        assert registry_pins.tag_digest(ORG, "dfe-engine", "v9.9.9") is None


def test_tag_digest_raises_when_neither_read_could_answer(monkeypatch):
    """An unreachable registry must not read as an absent tag, let alone a match."""
    monkeypatch.setattr(
        registry_pins.subprocess,
        "run",
        _fake_run(
            docker=_FakeProc(returncode=1, stderr="failed to do request: dial tcp: no such host")
        ),
    )
    registry = _published()
    registry.fail = [502, 502, 502]
    raised = ""
    with registry.serving(monkeypatch):
        try:
            registry_pins.tag_digest(ORG, "dfe-engine", "v1.15.1")
        except registry_pins.RegistryError as exc:
            raised = str(exc)
    assert "no such host" in raised
    assert "HTTP 502" in raised


# --- registry_pins retry -------------------------------------------------------
def _no_sleep(monkeypatch):
    """Make the retry backoff instant and deterministic for a test."""
    monkeypatch.setattr(registry_pins, "_sleep", lambda seconds: None)
    monkeypatch.setattr(registry_pins, "_random", lambda: 0.5)


def test_imagetools_retries_a_502_then_succeeds(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        registry_pins.subprocess,
        "run",
        _fake_run(
            docker=[
                _FakeProc(returncode=1, stderr="Error response from daemon: 502 Bad Gateway"),
                _FakeProc(stdout=_INDEX),
            ]
        ),
    )
    assert registry_pins.ref_digest("ghcr.io/hyperi-io/dfe-engine:v1.15.1") == (_INDEX, "")


def test_imagetools_gives_up_after_three_502s(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        registry_pins.subprocess,
        "run",
        _fake_run(docker=[_FakeProc(returncode=1, stderr="502 Bad Gateway")] * 3),
    )
    out, err = registry_pins._imagetools("ghcr.io/hyperi-io/dfe-engine:v1.15.1")
    assert out is None
    assert "502 Bad Gateway" in err
    assert "ghcr.io kept failing and gave up after 3 attempts" in err


def test_imagetools_does_not_retry_manifest_unknown(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        registry_pins.subprocess,
        "run",
        _fake_run(docker=[_FakeProc(returncode=1, stderr="manifest unknown")]),
    )
    out, err = registry_pins._imagetools("ghcr.io/hyperi-io/dfe-engine:v9.9.9")
    assert out is None
    assert registry_pins.is_absent(err)


def test_imagetools_does_not_retry_a_missing_docker_binary(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(
        registry_pins.subprocess,
        "run",
        _fake_run(docker=[OSError("No such file or directory: 'docker'")]),
    )
    out, err = registry_pins._imagetools("ghcr.io/hyperi-io/dfe-engine:v1.15.1")
    assert out is None
    assert "cannot run docker buildx" in err


def test_imagetools_does_not_retry_401_or_403(monkeypatch):
    _no_sleep(monkeypatch)
    for code in ("401 Unauthorized", "403 Forbidden"):
        monkeypatch.setattr(
            registry_pins.subprocess,
            "run",
            _fake_run(docker=[_FakeProc(returncode=1, stderr=code)]),
        )
        out, err = registry_pins._imagetools("ghcr.io/hyperi-io/dfe-engine:v1.15.1")
        assert out is None
        assert code in err


# --- resolve_pins verdicts ----------------------------------------------------
def test_default_selection_is_published_apps_only(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    _, _, _, stack_map = resolve_pins.load_stack(None)
    apps = resolve_pins._selected_apps(stack_map, [])
    # transform-wasm has no digest -> excluded from the default set.
    assert "dfe-transform-wasm" not in apps
    assert set(apps) == {"dfe-engine", "dfe-loader", "dfe-ui", "dfe-fetcher"}


def test_rows_classify_fresh_stale_missing(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    _, _, _, stack_map = resolve_pins.load_stack(None)
    apps = resolve_pins._selected_apps(stack_map, [])
    rows = {r.app: r for r in resolve_pins.resolve_rows(stack_map, ORG, apps, None)}
    assert rows["dfe-loader"].outcome == resolve_pins.Outcome.FRESH
    assert rows["dfe-engine"].outcome == resolve_pins.Outcome.STALE
    assert rows["dfe-ui"].outcome == resolve_pins.Outcome.STALE
    assert rows["dfe-fetcher"].outcome == resolve_pins.Outcome.MISSING


def test_load_stack_resolves_current_not_first(monkeypatch, tmp_path):
    """The write target is the `current` stack, not the first stack in the file."""
    _install(monkeypatch, tmp_path)
    _, _, name, stack_map = resolve_pins.load_stack(None)
    assert name == "9.9.0-rc.1"
    # the rc.0 stack's stale digest must be untouched by resolution
    assert stack_map["digests"]["dfe-engine"] == "sha256:WRONGENGINE"


# --- write path ---------------------------------------------------------------
def test_check_mode_exits_nonzero_when_stale(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    args = _args(check=True)
    assert resolve_pins.cmd_pin(args) == 1


def test_write_updates_stale_and_holds_fresh(monkeypatch, tmp_path):
    vfile = _install(monkeypatch, tmp_path)
    # cooldown 7d, no allow-fresh: engine (2d) and ui (1d) are inside cooldown.
    args = _args(write=True)
    rc = resolve_pins.cmd_pin(args)
    text = vfile.read_text(encoding="utf-8")
    # Both stale-but-fresh images are HELD -> not written, exit incomplete.
    assert "WRONGENGINE" in text
    assert "WRONGUI" in text
    assert rc == 1


def test_write_with_allow_fresh_pins_the_digest(monkeypatch, tmp_path):
    vfile = _install(monkeypatch, tmp_path)
    args = _args(write=True, allow_fresh=True)
    resolve_pins.cmd_pin(args)
    text = vfile.read_text(encoding="utf-8")
    assert "sha256:" + "a" * 64 in text  # engine digest resolved + written
    assert "sha256:" + "c" * 64 in text  # ui digest resolved + written
    assert "WRONGENGINE" not in text
    assert "WRONGUI" not in text


def test_write_preserves_comments_and_other_stacks(monkeypatch, tmp_path):
    vfile = _install(monkeypatch, tmp_path)
    args = _args(write=True, allow_fresh=True)
    resolve_pins.cmd_pin(args)
    text = vfile.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert "# digests block below -- immutable half of the pin" in text
    # the OTHER stack's digest is untouched (write is scoped to `current`)
    assert "sha256:oldstackdigest" in text
    # the fresh (matching) loader pin is left exactly as it was
    assert "sha256:" + "b" * 64 in text


def test_write_leaves_quoting_intact(monkeypatch, tmp_path):
    vfile = _install(monkeypatch, tmp_path)
    args = _args(write=True, allow_fresh=True)
    resolve_pins.cmd_pin(args)
    text = vfile.read_text(encoding="utf-8")
    # the rewritten digest keeps the surrounding double quotes
    assert f'dfe-engine: "sha256:{"a" * 64}"' in text


def test_ad_hoc_version_prints_ref(monkeypatch, tmp_path, capsys):
    _install(monkeypatch, tmp_path)
    args = _args(app=["dfe-loader"], version="v1.18.21")
    rc = resolve_pins.cmd_pin(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert f"ghcr.io/hyperi-io/dfe-loader:v1.18.21@sha256:{'b' * 64}" in out


def test_ad_hoc_write_refuses_tag_mismatch(monkeypatch, tmp_path):
    """--write on an arbitrary --version must not silently diverge from apps:."""
    _install(monkeypatch, tmp_path)
    # dfe-engine v1.15.0 exists on the registry but apps: pins v1.15.1.
    args = _args(app=["dfe-engine"], version="v1.15.0", write=True, allow_fresh=True)
    rc = resolve_pins.cmd_pin(args)
    assert rc == 1  # refused -> use bump-app first


# --- the whole command against a registry, with no token ----------------------
def test_check_reads_the_registry_with_no_token(monkeypatch, tmp_path, capsys):
    """The pin check an outsider runs: a published registry, no GitHub token."""
    vfile = tmp_path / "versions.yaml"
    vfile.write_text(SYNTHETIC_VERSIONS, encoding="utf-8")
    monkeypatch.setattr(resolve_pins, "VERSIONS", vfile)
    registry = _published()
    saved = _without_tokens()
    try:
        with registry.serving(monkeypatch):
            rc = resolve_pins.cmd_pin(_args(check=True))
    finally:
        _restore_tokens(saved)
    out = capsys.readouterr().out
    assert rc == 1  # the synthetic stack carries stale pins; the reads themselves ran
    assert "ok    dfe-loader v1.18.21" in out
    assert "STALE dfe-engine v1.15.1" in out
    assert "MISS  dfe-fetcher v1.4.10" in out
    assert all(r.authorization == "" for r in registry.token_requests())


# --- arg helper ---------------------------------------------------------------
def _args(**over):
    import argparse

    ns = argparse.Namespace(
        app=[],
        version=None,
        stack=None,
        org=ORG,
        registry="ghcr.io/hyperi-io",
        check=False,
        write=False,
        cooldown_days=7,
        allow_fresh=False,
        func=None,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


# --- standalone runner (mirrors the other tests in this dir) ------------------
def main() -> int:
    import contextlib
    import io

    class _MP:
        """A minimal monkeypatch: records and restores attribute sets."""

        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        def restore(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)

    class _Caps:
        def readouterr(self):
            return type("R", (), {"out": _buf.getvalue(), "err": ""})()

    import tempfile

    failures = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        mp = _MP()
        _buf = io.StringIO()
        params = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        with tempfile.TemporaryDirectory() as td:
            kw = {}
            if "monkeypatch" in params:
                kw["monkeypatch"] = mp
            if "tmp_path" in params:
                kw["tmp_path"] = Path(td)
            if "capsys" in params:
                kw["capsys"] = _Caps()
            try:
                with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(io.StringIO()):
                    fn(**kw)
                print(f"PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"FAIL  {name}  {exc}")
            finally:
                mp.restore()
    print(f"\n{'FAILED' if failures else 'ALL PASSED'} -- {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
