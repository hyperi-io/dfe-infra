#  Project:      dfe-infra
#  File:         scripts/tests/test_private_file.py
#  Purpose:      Prove a credential file is never on disk at a mode wider than
#                0600, not even between the write and a chmod
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The window between writing a credential and narrowing its mode, measured.

A file that ends at 0600 proves nothing about the moment its bytes landed: a
write followed by a chmod passes a final-mode check while the password sat
readable at the umask's mode in between. So every chmod the process makes is
observed through an audit hook, which fires before the call, and the file's
size and mode at that moment are recorded. Any non-empty file seen wider than
0600 is the window.

The umask is set to 0 while a write is watched, so the mode the writer asks for
at creation is the mode the file gets.

    python3 -m pytest scripts/tests/test_private_file.py -q
"""

import contextlib
import importlib.machinery
import importlib.util
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import access_summary  # noqa: E402
import private_file  # noqa: E402

GROUP_OR_OTHER = 0o077
SECRET = "hunter2-not-a-real-password"

# (size, mode) of the target at each chmod while a test is watching; None when none is.
_seen: list[tuple[int, int]] | None = None


def _on_audit(event: str, args: tuple) -> None:
    if _seen is None or event != "os.chmod":
        return
    with contextlib.suppress(OSError, TypeError):
        info = os.stat(args[0])
        _seen.append((info.st_size, stat.S_IMODE(info.st_mode)))


sys.addaudithook(_on_audit)


@contextlib.contextmanager
def open_umask() -> Iterator[None]:
    """A umask that narrows nothing, so a file gets exactly the mode its writer asked for."""
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


@contextlib.contextmanager
def watching() -> Iterator[list[tuple[int, int]]]:
    """Record every chmod's view of its file, under a umask that narrows nothing."""
    global _seen
    _seen = []
    try:
        with open_umask():
            yield _seen
    finally:
        _seen = None


def exposed(seen: list[tuple[int, int]]) -> list[str]:
    """The observations where content sat in a file someone else could read."""
    return [f"{size} bytes at {oct(mode)}" for size, mode in seen if size and mode & GROUP_OR_OTHER]


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


WRITERS = {
    "private_file.write_private": private_file.write_private,
    "access_summary.write": access_summary.write,
}


@pytest.mark.parametrize("writer", WRITERS.values(), ids=WRITERS.keys())
def test_a_new_file_is_never_wider_than_0600(writer, tmp_path: Path) -> None:
    path = tmp_path / "run" / "creds.md"

    with watching() as seen:
        writer(path, f"| Admin | `admin` | `{SECRET}` |\n")

    assert exposed(seen) == []
    assert mode_of(path) == 0o600
    assert SECRET in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("writer", WRITERS.values(), ids=WRITERS.keys())
def test_an_existing_wider_file_is_narrowed_before_the_new_text_lands(
    writer, tmp_path: Path
) -> None:
    path = tmp_path / "creds.md"
    path.write_text("from an earlier run\n", encoding="utf-8")
    path.chmod(0o644)

    with watching() as seen:
        writer(path, f"{SECRET}\n")

    assert exposed(seen) == []
    assert mode_of(path) == 0o600
    assert path.read_text(encoding="utf-8") == f"{SECRET}\n"


def test_the_text_lands_as_utf8_exactly_as_given(tmp_path: Path) -> None:
    text = "K=v\nL=caf" + chr(0xE9) + "\n"

    path = private_file.write_private(tmp_path / "a" / "b" / "creds.env", text)

    assert path == tmp_path / "a" / "b" / "creds.env"
    assert path.read_bytes() == text.encode("utf-8")


# A node kubeconfig as RKE2 writes it: loopback server, cluster-admin client key.
NODE_KUBECONFIG = """apiVersion: v1
clusters:
- cluster:
    server: https://127.0.0.1:6443
  name: default
users:
- name: default
  user:
    client-key-data: FAKE-CLUSTER-ADMIN-KEY
"""


def _fake_tool(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _dfe_ops():
    """dfe-ops carries no extension, so it is loaded by path rather than imported."""
    loader = importlib.machinery.SourceFileLoader("dfeops_private_file", str(SCRIPTS / "dfe-ops"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def test_a_fetched_node_kubeconfig_is_never_wider_than_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dfe-ops kubeconfig` puts a cluster-admin client key on the operator's disk."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_tool(bindir, "bao", "printf '%s' ZmFrZS1zc2gta2V5\n")
    _fake_tool(bindir, "ssh", f"cat <<'EOF'\n{NODE_KUBECONFIG}EOF\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example")
    ops = _dfe_ops()
    out = tmp_path / "node.kubeconfig"
    args = ops.build_parser().parse_args(
        ["kubeconfig", "--node", "node.example", "--vault-path", "kv/ssh", "--out", str(out)]
    )

    with watching() as seen:
        rc = ops.cmd_kubeconfig(args)

    assert rc == 0
    assert exposed(seen) == []
    assert mode_of(out) == 0o600
    text = out.read_text(encoding="utf-8")
    assert "server: https://node.example:6443" in text
    assert "FAKE-CLUSTER-ADMIN-KEY" in text


EXAMPLE_DIAL = SCRIPTS.parent / "deployment.example.yaml"


def _render_env(tmp_path: Path, out: Path) -> subprocess.CompletedProcess:
    """render_dial.py's env render of the committed example dial, under a umask of 0."""
    dial = tmp_path / "deployment.yaml"
    dial.write_text(EXAMPLE_DIAL.read_text(encoding="utf-8"), encoding="utf-8")
    argv = [sys.executable, str(SCRIPTS / "render_dial.py"), "--dial", str(dial), "--out", str(out)]
    with open_umask():
        return subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )


def test_the_env_file_render_dial_seeds_is_never_wider_than_0600(tmp_path: Path) -> None:
    """The seeded env file is the one an operator types the estate's secrets into."""
    out = tmp_path / "bootstrap.env"

    result = _render_env(tmp_path, out)

    assert result.returncode == 0, result.stderr
    assert "seeded" in result.stderr
    assert mode_of(out) == 0o600, oct(mode_of(out))


def test_a_render_narrows_an_existing_env_file_before_rewriting_it(tmp_path: Path) -> None:
    """The merge rewrites every line of the file, the secrets filled into it included."""
    out = tmp_path / "bootstrap.env"
    out.write_text(f'DFE_EXAMPLE_TOKEN="{SECRET}"\n', encoding="utf-8")
    out.chmod(0o644)

    result = _render_env(tmp_path, out)

    assert result.returncode == 0, result.stderr
    assert "merged" in result.stderr
    assert mode_of(out) == 0o600, oct(mode_of(out))
    assert f'DFE_EXAMPLE_TOKEN="{SECRET}"' in out.read_text(encoding="utf-8")
