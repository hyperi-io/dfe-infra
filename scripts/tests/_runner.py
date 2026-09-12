#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         _runner.py
#  Purpose:      The standalone runner the recorder-style tests share: minimal
#                monkeypatch, capsys and tmp_path so one file runs under pytest
#                AND as `python3 scripts/tests/<file>.py`. Stdlib only.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""run_module -- drive a test file's own test_* functions with no pytest.

The tests in this directory each run two ways, and the ones that install a
recorder over ``subprocess.run`` need three of pytest's fixtures to do it. This
supplies just enough of each -- by parameter NAME, the way pytest resolves them
-- so the same function body serves both runs and there is one copy of the
machinery rather than one per file.

The leading underscore keeps pytest from collecting this module as a test file.

    from _runner import run_module

    if __name__ == "__main__":
        sys.exit(run_module(globals()))
"""

from __future__ import annotations

import contextlib
import io
import tempfile
from pathlib import Path


class MonkeyPatch:
    """A minimal monkeypatch: records and restores attribute sets."""

    def __init__(self) -> None:
        self._undo: list[tuple[object, str, object]] = []

    def setattr(self, obj: object, name: str, val: object) -> None:
        """Set an attribute, remembering what was there."""
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def restore(self) -> None:
        """Put every patched attribute back, newest first."""
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)


class Captured:
    """A minimal capsys: reads the redirected buffers and drains them."""

    def __init__(self, out: io.StringIO, err: io.StringIO) -> None:
        self._out, self._err = out, err

    def readouterr(self):
        """The output since the last read, then empty the buffers."""
        result = type("R", (), {"out": self._out.getvalue(), "err": self._err.getvalue()})()
        for buf in (self._out, self._err):
            buf.truncate(0)
            buf.seek(0)
        return result


def run_module(namespace: dict) -> int:
    """Run every ``test_*`` in a module namespace, printing one line each.

    Args:
        namespace: The calling module's ``globals()``.

    Returns:
        The process exit code -- non-zero means a test failed.
    """
    failures = 0
    tests = [
        (name, fn)
        for name, fn in sorted(namespace.items())
        if name.startswith("test_") and callable(fn)
    ]
    for name, fn in tests:
        patcher = MonkeyPatch()
        out_buf, err_buf = io.StringIO(), io.StringIO()
        params = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        with tempfile.TemporaryDirectory() as scratch:
            kwargs = {}
            if "monkeypatch" in params:
                kwargs["monkeypatch"] = patcher
            if "tmp_path" in params:
                kwargs["tmp_path"] = Path(scratch)
            if "capsys" in params:
                kwargs["capsys"] = Captured(out_buf, err_buf)
            try:
                with (
                    contextlib.redirect_stdout(out_buf),
                    contextlib.redirect_stderr(err_buf),
                ):
                    fn(**kwargs)
                print(f"PASS  {name}")
            # A failing test is a counted line here, not a traceback that ends the run.
            except Exception as exc:
                failures += 1
                print(f"FAIL  {name}  {exc}")
            finally:
                patcher.restore()
    print(f"\n{'FAILED' if failures else 'ALL PASSED'} -- {failures} failure(s)")
    return 1 if failures else 0
