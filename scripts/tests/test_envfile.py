#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_envfile.py
#  Purpose:      Prove the shared env-file reader: the quoting and comment rules,
#                that later files win, that nothing is ever expanded as shell,
#                and the GH_TOKEN rule the gh-calling tools depend on.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/envfile.py.

The reader is deliberately not a shell, so the expansion tests are the point:
an operator's env file holds a scoped registry token, and a parser that ran
anything in it would turn a credential file into an execution path.

Runs offline. Under pytest, and standalone via the main() runner at the bottom
(matching the other tests in this dir).

    python3 -m pytest scripts/tests/test_envfile.py
    python3 scripts/tests/test_envfile.py
"""

from __future__ import annotations

import importlib.util
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


envfile = _load("envfile")


# --- parse_env_file -----------------------------------------------------------
def test_quotes_export_and_comments(tmp_path):
    path = tmp_path / "a.env"
    path.write_text(
        "# a comment\n"
        "\n"
        "PLAIN=one\n"
        'DOUBLE="two"\n'
        "SINGLE='three'\n"
        "export EXPORTED=four\n"
        'export EXPORTED_QUOTED="five"\n'
        "  SPACED  =  six  \n"
        "NO_EQUALS_HERE\n"
        "# TRAILING=comment-not-a-key\n",
        encoding="utf-8",
        newline="\n",
    )
    got = envfile.parse_env_file(path)
    assert got == {
        "PLAIN": "one",
        "DOUBLE": "two",
        "SINGLE": "three",
        "EXPORTED": "four",
        "EXPORTED_QUOTED": "five",
        "SPACED": "six",
    }


def test_only_one_layer_of_quotes_is_stripped(tmp_path):
    """A value that is itself quoted keeps its inner quotes."""
    path = tmp_path / "a.env"
    path.write_text("NESTED=\"'inner'\"\nMISMATCHED=\"unclosed\n", encoding="utf-8", newline="\n")
    got = envfile.parse_env_file(path)
    assert got["NESTED"] == "'inner'"
    assert got["MISMATCHED"] == '"unclosed'


def test_values_are_never_expanded_as_shell(tmp_path):
    """The whole point: a token file is data, never a script."""
    path = tmp_path / "a.env"
    path.write_text(
        "HOME_REF=$HOME\n"
        'BRACED="${OTHER}/tail"\n'
        "SUBST=$(id -u)\n"
        "OTHER=set\n",
        encoding="utf-8",
        newline="\n",
    )
    got = envfile.parse_env_file(path)
    assert got["HOME_REF"] == "$HOME"
    assert got["BRACED"] == "${OTHER}/tail"
    assert got["SUBST"] == "$(id -u)"


def test_an_equals_in_the_value_survives(tmp_path):
    """Base64 and JWT-shaped tokens carry '=' -- only the FIRST one splits."""
    path = tmp_path / "a.env"
    path.write_text("GHCR_TOKEN=abc=def==\n", encoding="utf-8", newline="\n")
    assert envfile.parse_env_file(path)["GHCR_TOKEN"] == "abc=def=="


def test_missing_file_raises(tmp_path):
    raised = False
    try:
        envfile.parse_env_file(tmp_path / "absent.env")
    except FileNotFoundError:
        raised = True
    assert raised


# --- load_env_files -----------------------------------------------------------
def test_later_file_wins_and_earlier_keys_survive(tmp_path):
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    first.write_text("SHARED=from-first\nONLY_FIRST=1\n", encoding="utf-8", newline="\n")
    second.write_text("SHARED=from-second\nONLY_SECOND=2\n", encoding="utf-8", newline="\n")
    got = envfile.load_env_files([first, second])
    assert got == {"SHARED": "from-second", "ONLY_FIRST": "1", "ONLY_SECOND": "2"}


def test_into_is_the_base_the_files_override(tmp_path):
    path = tmp_path / "a.env"
    path.write_text("SHARED=from-file\n", encoding="utf-8", newline="\n")
    base = {"SHARED": "from-base", "KEPT": "yes"}
    got = envfile.load_env_files([path], base)
    assert got is base
    assert got == {"SHARED": "from-file", "KEPT": "yes"}


def test_no_paths_is_an_empty_mapping():
    assert envfile.load_env_files([]) == {}


# --- apply_gh_token -----------------------------------------------------------
def _without_gh_token():
    """Drop GH_TOKEN from this process, returning the value to restore."""
    return os.environ.pop("GH_TOKEN", None)


def _restore_gh_token(previous):
    os.environ.pop("GH_TOKEN", None)
    if previous is not None:
        os.environ["GH_TOKEN"] = previous


def test_ghcr_token_becomes_gh_token(tmp_path):
    path = tmp_path / "a.env"
    path.write_text('GHCR_TOKEN="ghp_fromfile"\n', encoding="utf-8", newline="\n")
    previous = _without_gh_token()
    try:
        envfile.apply_gh_token([path])
        assert os.environ["GH_TOKEN"] == "ghp_fromfile"
    finally:
        _restore_gh_token(previous)


def test_file_gh_token_beats_file_ghcr_token(tmp_path):
    path = tmp_path / "a.env"
    path.write_text(
        "GHCR_TOKEN=ghp_ghcr\nGH_TOKEN=ghp_gh\n", encoding="utf-8", newline="\n"
    )
    previous = _without_gh_token()
    try:
        envfile.apply_gh_token([path])
        assert os.environ["GH_TOKEN"] == "ghp_gh"
    finally:
        _restore_gh_token(previous)


def test_an_ambient_gh_token_is_not_overwritten(tmp_path):
    """The file is the fallback for a host missing read:packages, not an override."""
    path = tmp_path / "a.env"
    path.write_text("GHCR_TOKEN=ghp_fromfile\n", encoding="utf-8", newline="\n")
    previous = os.environ.get("GH_TOKEN")
    os.environ["GH_TOKEN"] = "ghp_ambient"
    try:
        envfile.apply_gh_token([path])
        assert os.environ["GH_TOKEN"] == "ghp_ambient"
    finally:
        _restore_gh_token(previous)


def test_no_env_file_is_a_no_op(tmp_path):
    previous = _without_gh_token()
    try:
        envfile.apply_gh_token(None)
        envfile.apply_gh_token([])
        assert "GH_TOKEN" not in os.environ
    finally:
        _restore_gh_token(previous)


def test_a_file_without_a_token_sets_nothing(tmp_path):
    path = tmp_path / "a.env"
    path.write_text("DFE_NAMESPACE=dfe\n", encoding="utf-8", newline="\n")
    previous = _without_gh_token()
    try:
        envfile.apply_gh_token([path])
        assert "GH_TOKEN" not in os.environ
    finally:
        _restore_gh_token(previous)


# --- standalone runner (mirrors the other tests in this dir) ------------------
def main() -> int:
    import tempfile

    failures = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        params = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        with tempfile.TemporaryDirectory() as td:
            kw = {"tmp_path": Path(td)} if "tmp_path" in params else {}
            try:
                fn(**kw)
                print(f"PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"FAIL  {name}  {exc}")
    print(f"\n{'FAILED' if failures else 'ALL PASSED'} -- {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
