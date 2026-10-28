#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_deploy_repo_session.py
#  Purpose:      Prove `dfe-ops upgrade --bundled-forgejo` reaches the bundled
#                deploy repo with its own credential, kept off argv and out of
#                the checkout.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""scripts/deploy_repo_session.py and the upgrade verbs that use it.

    python3 -m pytest scripts/tests/test_deploy_repo_session.py -q

Real git against a local bare repo, and a kubectl stand-in on PATH. No cluster.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import deploy_repo_session as drs  # noqa: E402
import dfe_ops_upgrade as u  # noqa: E402

USER, PASSWORD = "dfe-admin", "s3cret-value"


def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True, env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", **(env or {})},
    ).stdout.strip()


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    origin = tmp_path / "deploy.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "--quiet", str(origin), str(seed)], check=True, capture_output=True)
    (seed / "pins.yaml").write_text("base: {}\n", encoding="utf-8")
    git(seed, "add", "pins.yaml")
    git(seed, "commit", "-q", "-m", "chore: seed")
    git(seed, "push", "-q", "origin", "main")
    return origin


@pytest.fixture
def fake_kubectl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A kubectl whose `get secret` prints $SECRET_JSON, or fails when $SECRET_FAILS is set."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "kubectl"
    script.write_text(
        '#!/bin/sh\n[ -n "$SECRET_FAILS" ] && { echo "Error from server (NotFound)" >&2; exit 1; }\n'
        'printf "%s" "$SECRET_JSON"\n',
        encoding="utf-8", newline="\n",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return script


def secret_json(**data: str) -> str:
    return json.dumps({"data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}})


def test_the_credential_rides_the_environment_and_nothing_else() -> None:
    env = drs.git_auth_env(USER, PASSWORD)
    header = env["GIT_CONFIG_VALUE_0"]
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert base64.b64decode(header.removeprefix("Authorization: Basic ")).decode() == f"{USER}:{PASSWORD}"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_the_admin_secret_is_read_in_process(fake_kubectl: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_JSON", secret_json(username=USER, password=PASSWORD))
    assert drs.read_admin(None, "forgejo", "dfe-forgejo-admin") == (USER, PASSWORD)


@pytest.mark.parametrize(
    ("env", "why"),
    [
        ({"SECRET_FAILS": "1"}, "cannot read secret"),
        ({"SECRET_JSON": secret_json(username=USER)}, "lacks a username and password"),
        ({"SECRET_JSON": "not json"}, "lacks a username and password"),
    ],
)
def test_an_unreadable_or_incomplete_secret_is_refused(
    fake_kubectl: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], why: str
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(drs.SessionError, match=why):
        drs.read_admin(None, "forgejo", "dfe-forgejo-admin")


def test_an_absent_checkout_is_cloned_and_a_present_one_is_retargeted(bare: Path, tmp_path: Path) -> None:
    checkout = tmp_path / "work" / "deploy"
    with drs.BundledRepo(url_override=str(bare)).open(checkout):
        assert (checkout / "pins.yaml").exists()
    elsewhere = tmp_path / "moved.git"
    bare.rename(elsewhere)
    with drs.BundledRepo(url_override=str(elsewhere)).open(checkout):
        assert git(checkout, "remote", "get-url", "origin") == str(elsewhere)
        git(checkout, "pull", "-q")


def test_a_checkout_that_cannot_be_attached_says_why(tmp_path: Path) -> None:
    with pytest.raises(drs.SessionError, match="cannot attach"):
        with drs.BundledRepo(url_override=str(tmp_path / "missing.git")).open(tmp_path / "work"):
            pass


def test_the_credential_never_reaches_the_checkouts_config(bare: Path, tmp_path: Path, fake_kubectl: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_JSON", secret_json(username=USER, password=PASSWORD))
    checkout = tmp_path / "deploy"
    repo = drs.BundledRepo(url_override=str(bare))
    session = drs.Session(str(bare), drs.git_auth_env(USER, PASSWORD))
    repo._attach(checkout, session)
    config = (checkout / ".git" / "config").read_text(encoding="utf-8")
    assert PASSWORD not in config
    assert base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode() not in config


def test_git_calls_carry_the_session_environment_only_while_it_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, str] | None] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        seen.append(kwargs.get("env"))  # type: ignore[arg-type]
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(u, "_run", fake_run)
    monkeypatch.setitem(u._GIT_ENV, "GIT_CONFIG_COUNT", "1")
    u._git(Path("d"), "push")
    assert seen[-1] is not None
    assert seen[-1]["GIT_CONFIG_COUNT"] == "1"
    u._GIT_ENV.clear()
    u._git(Path("d"), "push")
    assert seen[-1] is None


def _args(**over: object) -> argparse.Namespace:
    base = {"bundled_forgejo": True, "deploy": "deploy", "kubeconfig": None, "forgejo_repo": "deploy", "forgejo_owner": None}
    return argparse.Namespace(**{**base, **over})


def test_the_verb_runs_inside_the_session_and_leaves_no_credential_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inside: list[dict[str, str]] = []

    class FakeRepo:
        def __init__(self, **_: object) -> None: ...

        def open(self, _checkout: Path):
            import contextlib

            @contextlib.contextmanager
            def cm():
                yield drs.Session("http://127.0.0.1:1/x/deploy.git", {"GIT_CONFIG_COUNT": "1"})

            return cm()

    monkeypatch.setattr(u, "BundledRepo", FakeRepo)

    def verb(_args: argparse.Namespace) -> int:
        inside.append(dict(u._GIT_ENV))
        raise RuntimeError("the verb blew up")

    with pytest.raises(RuntimeError):
        u._with_bundled_repo(verb)(_args())
    assert inside == [{"GIT_CONFIG_COUNT": "1"}]
    assert u._GIT_ENV == {}


def test_a_session_that_cannot_open_fails_the_verb_before_it_runs(monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    class FailingRepo:
        def __init__(self, **_: object) -> None: ...

        def open(self, _checkout: Path):
            raise drs.SessionError("port-forward to forgejo/svc/dfe-forgejo exited")

    monkeypatch.setattr(u, "BundledRepo", FailingRepo)
    ran: list[bool] = []
    code = u._with_bundled_repo(lambda _a: ran.append(True) or 0)(_args())
    assert code == u.EXIT_PREFLIGHT_FAILED
    assert not ran
    assert "port-forward" in capsys.readouterr().err


def test_without_the_flag_the_verb_is_untouched() -> None:
    assert u._with_bundled_repo(lambda _a: 7)(_args(bundled_forgejo=False)) == 7
