#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_image_pins.py
#  Purpose:      Prove the image-pin checker reads the `current` stack (and an
#                explicit --stack), not the first one in versions.yaml, and that
#                it verdicts fresh / moved-tag / missing-tag / no-digest.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/check_image_pins.py.

The GH packages API is mocked in every test -- no network, so the suite is
hermetic and runs on a bare CI image. Runs under pytest, and standalone via the
main() runner at the bottom (matching the other tests in this dir).

    python3 -m pytest scripts/tests/test_check_image_pins.py
    python3 scripts/tests/test_check_image_pins.py
"""

from __future__ import annotations

import argparse
import importlib.util
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


resolve_pins = _load("resolve_pins")
check_image_pins = _load("check_image_pins")


# --- fixtures -----------------------------------------------------------------
# The OLD stack pins a version that no longer exists on the registry, so a check
# that reads it instead of `current` fails loudly rather than silently passing.
SYNTHETIC_VERSIONS = """\
schema: 2
current: "9.9.0-rc.2"
latest: ""
stacks:

  9.9.0-rc.1:
    apps:
      dfe-engine: "v0.0.1"
    digests:
      dfe-engine: "sha256:oldstackdigest"

  9.9.0-rc.2:
    apps:
      dfe-engine: "v1.15.1"
      dfe-loader: "v1.18.21"
      dfe-fetcher: "v1.4.10"
      dfe-transform-wasm: "2.2.0"   # unpublished, no digest
    digests:
      dfe-engine: "sha256:aaaa"
      dfe-loader: "sha256:MOVED"
      dfe-fetcher: "sha256:dddd"
"""

# app -> {tag: digest} as the registry has it. dfe-engine agrees with the pin,
# dfe-loader's tag now resolves elsewhere, dfe-fetcher's tag is absent.
FAKE_TAGS = {
    "dfe-engine": {"v1.15.1": "sha256:aaaa", "v1.15.0": "sha256:0000"},
    "dfe-loader": {"v1.18.21": "sha256:bbbb"},
    "dfe-fetcher": {"v1.4.99": "sha256:dddd"},
}


def _install(monkeypatch, tmp_path) -> Path:
    """Point the checker at a temp versions.yaml and mock the registry."""
    vfile = tmp_path / "versions.yaml"
    vfile.write_text(SYNTHETIC_VERSIONS, encoding="utf-8")
    monkeypatch.setattr(resolve_pins, "VERSIONS", vfile)
    monkeypatch.setattr(check_image_pins, "package_tags", lambda org, app: FAKE_TAGS.get(app, {}))
    return vfile


def _args(**over) -> argparse.Namespace:
    ns = argparse.Namespace(app=None, org="hyperi-io", stack=None)
    for key, value in over.items():
        setattr(ns, key, value)
    return ns


def _run(monkeypatch, args) -> int:
    monkeypatch.setattr(check_image_pins.argparse.ArgumentParser, "parse_args", lambda self: args)
    return check_image_pins.main()


# --- stack selection ----------------------------------------------------------
def test_load_pins_reads_current_not_the_first_stack(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    stack, apps, digests = check_image_pins.load_pins(None)
    assert stack == "9.9.0-rc.2"
    assert apps["dfe-engine"] == "v1.15.1"
    assert digests["dfe-engine"] == "sha256:aaaa"


def test_load_pins_honours_an_explicit_stack(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    stack, apps, _ = check_image_pins.load_pins("9.9.0-rc.1")
    assert stack == "9.9.0-rc.1"
    assert apps == {"dfe-engine": "v0.0.1"}


def test_load_pins_refuses_an_unknown_stack(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    raised = False
    try:
        check_image_pins.load_pins("9.9.0-rc.404")
    except SystemExit as exc:
        raised = "not in versions.yaml" in str(exc)
    assert raised


def test_section_is_empty_when_the_stack_lacks_it():
    assert check_image_pins._section({"apps": {"a": 1}}, "digests") == {}
    assert check_image_pins._section({"apps": {"a": 1}}, "apps") == {"a": "1"}


# --- verdicts -----------------------------------------------------------------
def test_matching_digest_passes(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)
    assert _run(monkeypatch, _args(app=["dfe-engine"])) == 0


def test_moved_tag_fails(monkeypatch, tmp_path, capsys):
    _install(monkeypatch, tmp_path)
    assert _run(monkeypatch, _args(app=["dfe-loader"])) == 1
    assert "[DIGEST]" in capsys.readouterr().err


def test_absent_tag_fails(monkeypatch, tmp_path, capsys):
    _install(monkeypatch, tmp_path)
    assert _run(monkeypatch, _args(app=["dfe-fetcher"])) == 1
    assert "[MISSING]" in capsys.readouterr().err


def test_pin_without_a_digest_is_skipped(monkeypatch, tmp_path, capsys):
    _install(monkeypatch, tmp_path)
    assert _run(monkeypatch, _args(app=["dfe-transform-wasm"])) == 0
    assert "[skip]" in capsys.readouterr().out


def test_the_old_stacks_pins_are_never_checked(monkeypatch, tmp_path, capsys):
    """v0.0.1 lives only in the rc.1 stack; the default run must not see it."""
    _install(monkeypatch, tmp_path)
    _run(monkeypatch, _args())
    combined = capsys.readouterr()
    assert "v0.0.1" not in combined.out + combined.err
    assert "checking stack 9.9.0-rc.2" in combined.out


# --- standalone runner (mirrors the other tests in this dir) ------------------
def main() -> int:
    import contextlib
    import io
    import tempfile

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
        """capsys over the buffers this run redirects into."""

        def __init__(self, out, err):
            self._out, self._err = out, err

        def readouterr(self):
            return type("R", (), {"out": self._out.getvalue(), "err": self._err.getvalue()})()

    failures = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        mp = _MP()
        out_buf, err_buf = io.StringIO(), io.StringIO()
        params = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        with tempfile.TemporaryDirectory() as td:
            kw = {}
            if "monkeypatch" in params:
                kw["monkeypatch"] = mp
            if "tmp_path" in params:
                kw["tmp_path"] = Path(td)
            if "capsys" in params:
                kw["capsys"] = _Caps(out_buf, err_buf)
            try:
                with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
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
