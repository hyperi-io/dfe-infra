#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_resolve_pins.py
#  Purpose:      Prove the tag -> digest resolver + writer: the registry lookup
#                (mocked, never the network), that a registry nobody could reach
#                raises instead of reading as absent, the fresh/stale/missing
#                verdict, the cooldown hold, and that --write rewrites ONLY the
#                current stack's digests: while preserving comments and
#                formatting.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/registry_pins.py + scripts/resolve_pins.py (dfe-infra#116).

Both registry reads -- `docker buildx imagetools` and the GH packages API -- are
mocked in every test, so the suite is hermetic and runs on a bare CI image. Runs
under pytest, and standalone via the main() runner at the bottom (matching the
other tests in this dir).

    python3 -m pytest scripts/tests/test_resolve_pins.py
    python3 scripts/tests/test_resolve_pins.py
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


registry_pins = _load("registry_pins")
resolve_pins = _load("resolve_pins")


# --- fixtures (in-memory GH records + a synthetic versions.yaml) --------------
def _iso(days_ago: float) -> str:
    when = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(digest: str, tags: list[str], days_ago: float) -> dict:
    return {
        "name": digest,
        "created_at": _iso(days_ago),
        "metadata": {"container": {"tags": tags}},
    }


# app -> its GH version records. dfe-engine is fresh (2d) and its recorded pin is
# STALE; dfe-loader is old (30d) and FRESH; dfe-ui is fresh (1d) and STALE (the
# cooldown target); dfe-fetcher's pinned tag is absent from the registry.
FAKE_PACKAGES = {
    "dfe-engine": [
        _record("sha256:" + "a" * 64, ["v1.15.1"], 2.0),
        _record("sha256:" + "0" * 64, ["v1.15.0"], 40.0),
    ],
    "dfe-loader": [
        _record("sha256:" + "b" * 64, ["v1.18.21"], 30.0),
    ],
    "dfe-ui": [
        _record("sha256:" + "c" * 64, ["v1.3.7"], 1.0),
    ],
    "dfe-fetcher": [
        _record("sha256:" + "d" * 64, ["v1.4.99"], 10.0),  # not the pinned tag
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


def _install(monkeypatch, tmp_path):
    """Point resolve_pins at a temp versions.yaml and mock the registry."""
    vfile = tmp_path / "versions.yaml"
    vfile.write_text(SYNTHETIC_VERSIONS, encoding="utf-8")
    monkeypatch.setattr(resolve_pins, "VERSIONS", vfile)

    def fake_versions(org, app):
        return tuple(FAKE_PACKAGES.get(app, []))

    monkeypatch.setattr(registry_pins, "package_versions", fake_versions)
    return vfile


# --- registry_pins core -------------------------------------------------------
def test_resolve_returns_digest_and_publish_date(monkeypatch):
    monkeypatch.setattr(registry_pins, "package_versions", lambda o, a: tuple(FAKE_PACKAGES[a]))
    found = registry_pins.resolve("hyperi-io", "dfe-engine", "v1.15.1")
    assert found is not None
    assert found.digest == "sha256:" + "a" * 64
    assert found.published is not None


def test_resolve_digest_is_none_for_absent_tag(monkeypatch):
    monkeypatch.setattr(registry_pins, "package_versions", lambda o, a: tuple(FAKE_PACKAGES[a]))
    assert registry_pins.resolve_digest("hyperi-io", "dfe-engine", "v9.9.9") is None


def test_package_tags_maps_every_tag(monkeypatch):
    monkeypatch.setattr(registry_pins, "package_versions", lambda o, a: tuple(FAKE_PACKAGES[a]))
    tags = registry_pins.package_tags("hyperi-io", "dfe-engine")
    assert tags["v1.15.1"] == "sha256:" + "a" * 64
    assert tags["v1.15.0"] == "sha256:" + "0" * 64


def test_version_key_orders_numerically():
    assert registry_pins.version_key("v1.18.19") > registry_pins.version_key("v1.18.9")


def test_gh_api_parses_concatenated_pages(monkeypatch):
    """--paginate concatenates one JSON array per page with no separator."""

    class FakeProc:
        returncode = 0
        stdout = '[{"name":"x"}]\n[{"name":"y"}]'
        stderr = ""

    monkeypatch.setattr(registry_pins.subprocess, "run", lambda *a, **k: FakeProc())
    records = registry_pins._gh_api("/whatever")
    assert [r["name"] for r in records] == ["x", "y"]


def test_gh_api_raises_on_failure(monkeypatch):
    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "gh: not authenticated"

    monkeypatch.setattr(registry_pins.subprocess, "run", lambda *a, **k: FakeProc())
    raised = False
    try:
        registry_pins._gh_api("/whatever")
    except registry_pins.RegistryError as exc:
        raised = "not authenticated" in str(exc)
    assert raised


# --- registry_pins digest resolution ------------------------------------------
_INDEX = "sha256:" + "9" * 64


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _fake_run(docker=None, gh=None):
    """A subprocess.run stub routing on argv[0]; an Exception value is raised.

    `docker`/`gh` is either one value returned on every call, or a list
    consumed one entry per call -- popping past the end raises, which proves
    a caller did not make more calls than the list scripts.
    """
    calls = {"docker": docker, "gh": gh}

    def run(cmd, *a, **k):
        key = "docker" if cmd[0] == "docker" else "gh"
        value = calls[key]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"{cmd[0]} called more times than scripted")
            proc = value.pop(0)
        else:
            proc = value
        if isinstance(proc, Exception):
            raise proc
        if proc is None:
            raise AssertionError(f"{cmd[0]} must not be called")
        return proc

    return run


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
    assert registry_pins.tag_digest("hyperi-io", "dfe-engine", "v1.15.1") == _INDEX


def test_tag_digest_falls_back_to_the_api_without_docker(monkeypatch):
    registry_pins.package_versions.cache_clear()
    monkeypatch.setattr(
        registry_pins.subprocess,
        "run",
        _fake_run(
            docker=FileNotFoundError("docker"),
            gh=_FakeProc(stdout=json.dumps(FAKE_PACKAGES["dfe-engine"])),
        ),
    )
    assert registry_pins.tag_digest("hyperi-io", "dfe-engine", "v1.15.1") == "sha256:" + "a" * 64


def test_the_api_reads_a_nested_package_as_one_path_segment(monkeypatch):
    """A thin chart is the package charts/<chart>, and the API answers 404 to a raw slash."""
    registry_pins.package_versions.cache_clear()
    chart_digest = "sha256:" + "e" * 64
    asked: list[list[str]] = []

    def run(cmd, *a, **k):
        asked.append(cmd)
        if cmd[0] == "docker":
            raise FileNotFoundError("docker")
        return _FakeProc(stdout=json.dumps([_record(chart_digest, ["1.22.16"], 1.0)]))

    monkeypatch.setattr(registry_pins.subprocess, "run", run)
    assert registry_pins.tag_digest("hyperi-io", "charts/dfe-engine", "1.22.16") == chart_digest
    gh_paths = [cmd[-1] for cmd in asked if cmd[0] == "gh"]
    want = "/orgs/hyperi-io/packages/container/charts%2Fdfe-engine/versions?per_page=100"
    assert gh_paths == [want]
    registry_pins.package_versions.cache_clear()


def test_tag_digest_is_none_only_when_the_registry_says_not_found(monkeypatch):
    registry_pins.package_versions.cache_clear()
    monkeypatch.setattr(
        registry_pins.subprocess,
        "run",
        _fake_run(
            docker=_FakeProc(returncode=1, stderr="ERROR: ghcr.io/o/a:v9.9.9: not found"),
            gh=_FakeProc(returncode=1, stderr="gh: You need at least read:packages scope"),
        ),
    )
    assert registry_pins.tag_digest("hyperi-io", "dfe-engine", "v9.9.9") is None


def test_tag_digest_raises_when_neither_read_could_answer(monkeypatch):
    """An unreachable registry must not read as an absent tag, let alone a match."""
    registry_pins.package_versions.cache_clear()
    monkeypatch.setattr(
        registry_pins.subprocess,
        "run",
        _fake_run(
            docker=_FakeProc(returncode=1, stderr="failed to do request: dial tcp: no such host"),
            gh=_FakeProc(returncode=1, stderr="gh: You need at least read:packages scope"),
        ),
    )
    raised = False
    try:
        registry_pins.tag_digest("hyperi-io", "dfe-engine", "v1.15.1")
    except registry_pins.RegistryError as exc:
        raised = "no such host" in str(exc)
    assert raised


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
        _fake_run(
            docker=[
                _FakeProc(returncode=1, stderr="502 Bad Gateway"),
                _FakeProc(returncode=1, stderr="502 Bad Gateway"),
                _FakeProc(returncode=1, stderr="502 Bad Gateway"),
            ]
        ),
    )
    out, err = registry_pins._imagetools("ghcr.io/hyperi-io/dfe-engine:v1.15.1")
    assert out is None
    assert "502 Bad Gateway" in err
    assert "3 attempts" in err


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
    rows = {r.app: r for r in resolve_pins.resolve_rows(stack_map, "hyperi-io", apps, None)}
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


# --- registry token from an env file ------------------------------------------
_FILE_TOKEN = "ghp_fromtheenvfile"


def test_pin_takes_repeatable_env_files():
    import argparse

    ap = argparse.ArgumentParser()
    resolve_pins.add_pin_subparser(ap.add_subparsers(dest="command"))
    args = ap.parse_args(["pin", "--env-file", "a.env", "--env-file", "b.env"])
    assert args.env_file == ["a.env", "b.env"]


def test_the_env_file_token_reaches_the_registry_and_is_never_printed(
    monkeypatch, tmp_path, capsys
):
    """gh without read:packages gets a 403 from ghcr; the scoped token lives in a file."""
    _install(monkeypatch, tmp_path)
    token_file = tmp_path / "ghcr.env"
    token_file.write_text(f"GHCR_TOKEN={_FILE_TOKEN}\n", encoding="utf-8", newline="\n")
    seen_tokens: list[str | None] = []

    def reading_versions(org, app):
        seen_tokens.append(os.environ.get("GH_TOKEN"))
        return tuple(FAKE_PACKAGES.get(app, []))

    monkeypatch.setattr(registry_pins, "package_versions", reading_versions)
    previous = os.environ.pop("GH_TOKEN", None)
    try:
        rc = resolve_pins.cmd_pin(_args(check=True, env_file=[str(token_file)]))
    finally:
        os.environ.pop("GH_TOKEN", None)
        if previous is not None:
            os.environ["GH_TOKEN"] = previous

    out = capsys.readouterr()
    assert rc == 1  # the synthetic stack carries stale pins; the read itself ran
    assert seen_tokens
    assert all(token == _FILE_TOKEN for token in seen_tokens)
    assert _FILE_TOKEN not in out.out
    assert _FILE_TOKEN not in out.err


# --- arg helper ---------------------------------------------------------------
def _args(**over):
    import argparse

    ns = argparse.Namespace(
        app=[],
        version=None,
        stack=None,
        org="hyperi-io",
        registry="ghcr.io/hyperi-io",
        check=False,
        write=False,
        cooldown_days=7,
        allow_fresh=False,
        env_file=[],
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
