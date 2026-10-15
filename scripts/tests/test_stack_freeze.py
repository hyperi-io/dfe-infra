#  Project:      dfe-infra
#  File:         test_stack_freeze.py
#  Purpose:      Hold a released stack to its tag: the writers refuse to edit it,
#                and its block and constraints file still equal their bytes at
#                the tag.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the stack freeze (scripts/stack_freeze.py and its callers).

    python3 -m pytest scripts/tests/test_stack_freeze.py -q

A stack whose git tag exists is a released record. dfe-stack refresh-digests and
bump-app, and resolve_pins --write, refuse to rewrite one, and every released
block in versions.yaml and its constraints/<stack>.yaml must equal their bytes
at the tag. The block is compared as text, not parsed data, because the parser
drops comments and a comment edit changes what the record says.

The checkout needs the stack tags (CI: fetch-depth 0). A checkout with none
fails here rather than comparing nothing.
"""

import argparse
import functools
import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import resolve_pins  # noqa: E402
import stack_freeze  # noqa: E402
from stack_images import block_span  # noqa: E402


def _load_dfe_stack():
    # dfe-stack has no .py suffix, so the loader is told it is source.
    loader = importlib.machinery.SourceFileLoader("dfe_stack_freeze", str(SCRIPTS / "dfe-stack"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


dfe = _load_dfe_stack()


# Released stacks already edited after their tag when the freeze was added. None may
# join. A stack leaves once its block and constraints are back to the tag's bytes, and
# test_a_legacy_exemption_is_still_needed fails until it does.
LEGACY_EDITED = frozenset(
    {
        *(f"2.2.0-rc.{n}" for n in range(1, 15)),
        "2.2.0",
        *(f"2.2.1-rc.{n}" for n in range(1, 7)),
        "2.2.1",
    }
)


def _git_show(spec: str) -> bytes | None:
    """The file at `<tag>:<path>` in this checkout, or None when the tag lacks it."""
    done = subprocess.run(["git", "-C", str(REPO_ROOT), "show", spec], capture_output=True)
    return done.stdout if done.returncode == 0 else None


def _block(text: str, stack: str) -> str | None:
    """The lines of one stack block, header included, or None when the file has no such block."""
    lines = text.splitlines(keepends=True)
    try:
        start, end = block_span(lines, stack)
    except SystemExit:
        return None
    return "".join(lines[start:end])


def _stacks() -> list[str]:
    text = (REPO_ROOT / "versions.yaml").read_text(encoding="utf-8")
    return list(dfe.parse_simple_yaml(text).get("stacks", {}))


def _released() -> list[str]:
    return [stack for stack in _stacks() if stack_freeze.is_tagged(stack, REPO_ROOT)]


@functools.cache
def _edits_since_tag(stack: str) -> tuple[str, ...]:
    """Which of a released stack's two records differ from their bytes at its tag."""
    differs = []
    now = (REPO_ROOT / "versions.yaml").read_text(encoding="utf-8")
    at_tag = _git_show(f"{stack}:versions.yaml")
    tagged_block = None if at_tag is None else _block(at_tag.decode("utf-8", "replace"), stack)
    if tagged_block is None or tagged_block != _block(now, stack):
        differs.append("block")
    constraints = REPO_ROOT / "constraints" / f"{stack}.yaml"
    current = constraints.read_bytes() if constraints.exists() else None
    if _git_show(f"{stack}:constraints/{stack}.yaml") != current:
        differs.append("constraints")
    return tuple(differs)


def test_the_checkout_carries_the_stack_tags():
    assert _released(), (
        "no stack in versions.yaml has a git tag in this checkout, so the freeze comparison "
        "would read nothing and pass. Fetch the tags: git fetch --tags (CI: fetch-depth 0)."
    )


def test_a_released_stack_still_equals_its_tag():
    edited = {
        stack: _edits_since_tag(stack)
        for stack in _released()
        if stack not in LEGACY_EDITED and _edits_since_tag(stack)
    }
    assert not edited, (
        f"released stacks edited after their tag: {edited}. A tagged stack is the record of "
        f"what shipped, so revert the edit and put the change in the next stack "
        f"(dfe-stack cut)."
    )


def test_a_legacy_exemption_is_still_needed():
    cleared = [
        stack
        for stack in sorted(LEGACY_EDITED)
        if stack_freeze.is_tagged(stack, REPO_ROOT) and not _edits_since_tag(stack)
    ]
    assert not cleared, (
        f"{cleared} equal their tag again. Delete them from LEGACY_EDITED, or a later edit "
        f"to them goes unnoticed."
    )


# --- the writers -------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
            "-c", "core.hooksPath=/dev/null",
            *args,
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )


def _repo(path: Path, versions: str, tag: str | None) -> Path:
    """A checkout holding versions.yaml, with `tag` cut the way release.yml cuts one."""
    path.mkdir()
    _git(path, "init", "-q")
    (path / "versions.yaml").write_text(versions, encoding="utf-8", newline="\n")
    _git(path, "add", "versions.yaml")
    _git(path, "commit", "-q", "-m", "seed")
    if tag:
        _git(path, "tag", "-a", tag, "-m", f"DFE stack {tag}")
    return path


_REFRESH = """\
current: "9.9.0-rc.2"
stacks:
  9.9.0-rc.1:
    services:
      # image: example/floating
      floating: "1-alpine"
    services-digests:
      floating: "sha256:aaa"   # example/floating:1-alpine
  9.9.0-rc.2:
    services:
      # image: example/floating
      floating: "1-alpine"
    services-digests:
      floating: "sha256:aaa"   # example/floating:1-alpine
"""


def _refresh(monkeypatch, repo: Path, digest: str, check: bool = False) -> int:
    monkeypatch.setattr(dfe, "REPO_ROOT", repo)
    monkeypatch.setattr(dfe.registry_pins, "ref_digest", lambda ref: (digest, ""))
    return dfe.cmd_refresh_digests(argparse.Namespace(stack=None, check=check))


def test_refresh_digests_refuses_to_move_a_digest_in_a_released_stack(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo", _REFRESH, "9.9.0-rc.2")
    with pytest.raises(SystemExit, match=r"stack 9\.9\.0-rc\.2 is released.*dfe-stack cut"):
        _refresh(monkeypatch, repo, "sha256:new")
    assert (repo / "versions.yaml").read_text(encoding="utf-8") == _REFRESH


def test_refresh_digests_writes_when_the_tag_is_not_cut_yet(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo", _REFRESH, "9.9.0-rc.1")
    assert _refresh(monkeypatch, repo, "sha256:new") == 0
    text = (repo / "versions.yaml").read_text(encoding="utf-8")
    assert text.split("9.9.0-rc.2:")[1].count('"sha256:new"') == 1
    assert text.split("9.9.0-rc.2:")[0].count("sha256:new") == 0


def test_refresh_digests_leaves_a_released_stack_alone_when_nothing_moved(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo", _REFRESH, "9.9.0-rc.2")
    assert _refresh(monkeypatch, repo, "sha256:aaa") == 0
    assert (repo / "versions.yaml").read_text(encoding="utf-8") == _REFRESH


def test_refresh_digests_check_reports_a_released_stack_without_refusing(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo", _REFRESH, "9.9.0-rc.2")
    assert _refresh(monkeypatch, repo, "sha256:new", check=True) == 1
    assert (repo / "versions.yaml").read_text(encoding="utf-8") == _REFRESH


_BUMP = """\
current: "9.9.0-rc.3"
stacks:
  9.9.0-rc.2:
    apps:
      dfe-new: "v0.1.0"
    chart-digests:
      dfe-new: "unpublished"
  9.9.0-rc.3:
    apps:
      dfe-new: "v0.1.0"
    chart-digests:
      dfe-new: "unpublished"
"""


def _bump(
    monkeypatch, repo: Path, stack: str | None, reads: list, dry_run: bool = False
) -> int:
    """bump-app over `repo`, recording every registry read. No chart is published."""
    monkeypatch.setattr(dfe, "REPO_ROOT", repo)
    monkeypatch.setattr(dfe.registry_pins, "tag_digest", lambda *a, **k: reads.append(a))
    args = argparse.Namespace(
        app="dfe-new", version="v0.2.0", stack=stack, dry_run=dry_run, registry="ghcr.io/hyperi-io"
    )
    return dfe.cmd_bump_app(args)


@pytest.mark.parametrize("dry_run", [False, True])
def test_bump_app_refuses_a_released_current(tmp_path, monkeypatch, dry_run):
    repo = _repo(tmp_path / "repo", _BUMP, "9.9.0-rc.3")
    reads: list = []
    with pytest.raises(SystemExit, match=r"stack 9\.9\.0-rc\.3 is released.*dfe-stack cut"):
        _bump(monkeypatch, repo, None, reads, dry_run)
    assert (repo / "versions.yaml").read_text(encoding="utf-8") == _BUMP
    assert not reads


def test_bump_app_refuses_an_older_released_stack_named_by_flag(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo", _BUMP, "9.9.0-rc.2")
    reads: list = []
    with pytest.raises(SystemExit, match=r"stack 9\.9\.0-rc\.2 is released"):
        _bump(monkeypatch, repo, "9.9.0-rc.2", reads)
    assert (repo / "versions.yaml").read_text(encoding="utf-8") == _BUMP
    assert not reads


def test_bump_app_writes_when_only_an_older_stack_is_released(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo", _BUMP, "9.9.0-rc.2")
    assert _bump(monkeypatch, repo, None, []) == 0
    older, current = (repo / "versions.yaml").read_text(encoding="utf-8").split("  9.9.0-rc.3:")
    assert 'dfe-new: "v0.2.0"' in current
    assert 'dfe-new: "v0.2.0"' not in older


_RESOLVE = """\
schema: 2
current: "9.9.0-rc.1"
stacks:
  9.9.0-rc.1:
    apps:
      dfe-engine: "v1.0.0"
    digests:
      dfe-engine: "sha256:old"
"""


def _pin_write(monkeypatch, repo: Path) -> int:
    stale = resolve_pins.Row(
        "dfe-engine",
        "v1.0.0",
        "sha256:old",
        resolve_pins.Outcome.STALE,
        SimpleNamespace(digest="sha256:new", published=None),
    )
    monkeypatch.setattr(resolve_pins, "VERSIONS", repo / "versions.yaml")
    monkeypatch.setattr(resolve_pins, "resolve_rows", lambda *args, **kwargs: [stale])
    monkeypatch.setattr(resolve_pins.envfile, "apply_gh_token", lambda files: None)
    args = argparse.Namespace(
        version=None, app=[], check=False, write=True, env_file=[], stack=None,
        org="hyperi-io", registry="ghcr.io/hyperi-io", cooldown_days=7, allow_fresh=True,
    )
    return resolve_pins.cmd_pin(args)


def test_resolve_pins_write_refuses_a_released_stack(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo", _RESOLVE, "9.9.0-rc.1")
    with pytest.raises(SystemExit, match=r"stack 9\.9\.0-rc\.1 is released.*dfe-stack cut"):
        _pin_write(monkeypatch, repo)
    assert (repo / "versions.yaml").read_text(encoding="utf-8") == _RESOLVE


def test_resolve_pins_write_writes_when_the_tag_is_not_cut_yet(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo", _RESOLVE, None)
    assert _pin_write(monkeypatch, repo) == 0
    assert 'dfe-engine: "sha256:new"' in (repo / "versions.yaml").read_text(encoding="utf-8")
