#  Project:      dfe-infra
#  File:         scripts/tests/test_dfe_ops_forward_ports.py
#  Purpose:      Prove a stage that port-forwards refuses to start on a local port
#                another process already holds, rather than testing whatever
#                answers there.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What `dfe-ops ui` and `dfe-ops acceptance` do when a forward's local port is taken.

kubectl cannot bind a taken port and exits, but the process holding it goes on
answering, so the readiness probe passes against another deployment. A second
stack's engine port-forward on the same host was the live case: the ui stage
probed that engine instead of its own.

    python3 -m pytest scripts/tests/test_dfe_ops_forward_ports.py -q
"""

import importlib.machinery
import importlib.util
import socket
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_forward_ports", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_forward_ports", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_forward_ports"] = dfeops
_loader.exec_module(dfeops)


@contextmanager
def _held_port() -> Iterator[int]:
    """A loopback port another listener holds for the duration."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        yield holder.getsockname()[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _no_forwards(monkeypatch, tmp_path: Path) -> list[tuple]:
    """Stub every way out to a cluster, so a refusal that regresses fails here instead."""
    started: list[tuple] = []

    def no_cluster(cmd, *_a, **_k):
        raise AssertionError(f"reached a cluster before refusing: {cmd}")

    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "no-such-kubeconfig"))
    monkeypatch.setattr(dfeops.subprocess, "run", no_cluster)
    monkeypatch.setattr(dfeops, "_resolves", lambda _host: True)
    monkeypatch.setattr(dfeops, "_forward", lambda *a, **_k: started.append(a))
    monkeypatch.setattr(dfeops, "_forward_ready", lambda *_a, **_k: True)
    return started


def test_a_held_port_is_reported_taken() -> None:
    with _held_port() as port:
        assert dfeops._taken_ports([port]) == [port]


def test_a_free_port_is_not() -> None:
    assert dfeops._taken_ports([_free_port()]) == []


def test_only_the_held_ports_are_named() -> None:
    free = _free_port()
    with _held_port() as port:
        assert dfeops._taken_ports([free, port]) == [port]


def test_the_ui_stage_refuses_a_taken_engine_port(monkeypatch, capsys, tmp_path: Path) -> None:
    started = _no_forwards(monkeypatch, tmp_path)
    with _held_port() as port:
        args = dfeops.build_parser().parse_args(
            ["ui", "--ui-repo", "/nonexistent/dfe-ui", "--ui-url", "https://dfe.example",
             "--engine-port", str(port)]
        )
        assert dfeops.cmd_ui(args) == 1

    assert started == []
    assert str(port) in capsys.readouterr().err


def test_the_acceptance_stage_refuses_a_taken_datastore_port(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    started = _no_forwards(monkeypatch, tmp_path)
    with _held_port() as port:
        args = dfeops.build_parser().parse_args(
            ["acceptance", "--repo", "/nonexistent/dfe-engine", "--suite", "flows", "--mode", "single",
             "--receiver-port", str(_free_port()), "--ch-port", str(port),
             "--engine-port", str(_free_port())]
        )
        assert dfeops.cmd_acceptance(args) == 1

    assert started == []
    assert str(port) in capsys.readouterr().err


def test_the_idle_check_refuses_a_taken_port(monkeypatch, capsys, tmp_path: Path) -> None:
    started = _no_forwards(monkeypatch, tmp_path)
    monkeypatch.setattr(dfeops.composition, "default_apps", lambda _profile: ())
    with _held_port() as port:
        assert dfeops._idle_check(["kubectl"], "dfe-local", "single", port) == 1

    assert started == []
    assert str(port) in capsys.readouterr().err
