#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_render_env.py
#  Purpose:      Prove bootstrap/render_env.py renders every bootstrap template
#                byte for byte as envsubst does, and that bootstrap.sh renders
#                through it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for bootstrap/render_env.py.

The parity tests run both renderers as real processes over every template
bootstrap.sh renders, under the same environment, with every variable the
template names set, then unset, then empty. They skip where envsubst is not
installed. The behaviour tests below them hold the rules on their own and run
everywhere.

    python3 -m pytest scripts/tests/test_render_env.py -q
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "bootstrap"
RENDER_ENV = BOOTSTRAP / "render_env.py"
TEMPLATES = sorted((BOOTSTRAP / "templates").glob("*.tpl"))

ENVSUBST = shutil.which("envsubst")
needs_envsubst = pytest.mark.skipif(
    ENVSUBST is None, reason="envsubst is not installed, so there is nothing to compare against"
)

# Every name a template writes after a `$`, `${NAME:-x}` forms included, so those are
# set too and the test sees them left alone.
_NAMED = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")

# A value carrying what a careless renderer would act on: a reference, a regex
# replacement escape, quotes and a newline.
AWKWARD = "v$HOME \\1 \\g<0> ${PATH} 'q' \"dq\"\nnext"

_spec = importlib.util.spec_from_file_location("render_env", RENDER_ENV)
render_env = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_env)


def _names(template: Path) -> list[str]:
    return sorted(set(_NAMED.findall(template.read_text(encoding="utf-8"))))


def _render(command: list[str], stdin: bytes, env: dict[str, str]) -> bytes:
    done = subprocess.run(command, input=stdin, env=env, capture_output=True, check=False)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return done.stdout


def _both(stdin: bytes, env: dict[str, str]) -> tuple[bytes, bytes]:
    """(envsubst's output, render_env.py's output) for the same input and environment."""
    return (
        _render([ENVSUBST], stdin, env),
        _render([sys.executable, str(RENDER_ENV)], stdin, env),
    )


def _env_for(names: list[str], state: str) -> dict[str, str]:
    """A minimal environment where every name is set to its own value, unset, or empty."""
    env = {"PATH": os.environ.get("PATH", os.defpath)}
    for index, name in enumerate(names):
        if state == "set":
            env[name] = f"{name}-{index}-{AWKWARD}"
        elif state == "empty":
            env[name] = ""
    return env


def test_every_template_bootstrap_renders_is_covered() -> None:
    """The parity tests below render exactly the templates bootstrap.sh names, no fewer."""
    script = (BOOTSTRAP / "bootstrap.sh").read_text(encoding="utf-8")
    rendered = set(re.findall(r"\$\{TEMPLATES_DIR\}/([\w.-]+\.tpl)\b", script))
    assert rendered, "bootstrap.sh names no template"
    assert rendered == {t.name for t in TEMPLATES}


@needs_envsubst
@pytest.mark.parametrize("state", ["set", "unset", "empty"])
@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_every_template_renders_as_envsubst_renders_it(template: Path, state: str) -> None:
    names = _names(template)
    assert names, f"{template.name} names no variable"
    expected, got = _both(template.read_bytes(), _env_for(names, state))
    assert got == expected


@needs_envsubst
def test_a_template_with_half_its_names_set_renders_as_envsubst_renders_it() -> None:
    for template in TEMPLATES:
        names = _names(template)
        env = _env_for(names[::2], "set")
        expected, got = _both(template.read_bytes(), env)
        assert got == expected, template.name


EDGE_CASES = [
    b"",
    b"$",
    b"${",
    b"$$",
    b"$$SET",
    b"${SET}X",
    b"$SETX",
    b"${UNSET}|$UNSET|",
    b"${SET:-default} ${SET-default} ${SET:+alt}",
    b"${SET$SET}",
    b"${{ .metadata.annotations.x }}",
    b"{{ $x := 1 }}",
    b"$1 $_a ${_a} $9a",
    b"${ SET }",
    b"${SET",
    b"${SET}}",
    b"cost $5.00 and 100$",
    b"\xff\xfe $SET \xe2\x80\x94 ${EMPTY}",
    b"$SET\n${SET}\r\n",
    b"\\$SET \\${SET}",
    b"$(SET) $[SET]",
    b"$Set $set $SET",
]


@needs_envsubst
@pytest.mark.parametrize("text", EDGE_CASES, ids=repr)
def test_edge_cases_render_as_envsubst_renders_them(text: bytes) -> None:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "SET": AWKWARD,
        "EMPTY": "",
        "_a": "underscore",
        "Set": "mixed",
        "set": "lower",
    }
    expected, got = _both(text, env)
    assert got == expected


def test_both_forms_take_the_value_and_an_unset_name_renders_empty() -> None:
    env = {b"NAME": b"value"}
    assert (
        render_env.render(b"a=$NAME b=${NAME} c=${GONE} d=$GONE.", env) == b"a=value b=value c= d=."
    )


def test_a_name_runs_to_its_last_name_character() -> None:
    env = {b"NAME": b"short", b"NAME_2": b"long"}
    assert render_env.render(b"$NAME_2 $NAME-2 ${NAME}_2", env) == b"long short-2 short_2"


def test_a_shell_default_and_a_go_template_are_left_as_written() -> None:
    text = b"${NAME:-fallback} ${NAME-fallback} ${{ .metadata.name }} $ ${"
    assert render_env.render(text, {b"NAME": b"set"}) == text


def test_a_value_is_written_once_and_never_read_for_references() -> None:
    env = {b"OUTER": b"${INNER} $INNER \\1 \\g<0>", b"INNER": b"expanded"}
    assert render_env.render(b"[$OUTER]", env) == b"[${INNER} $INNER \\1 \\g<0>]"


def test_bytes_that_are_not_utf8_pass_through() -> None:
    env = {b"RAW": b"\xff\x00\xfe"}
    assert render_env.render(b"\xe2\x80\x94 $RAW \xc3", env) == b"\xe2\x80\x94 \xff\x00\xfe \xc3"


def test_the_cli_renders_stdin_to_stdout_from_its_environment() -> None:
    env = {"PATH": os.environ.get("PATH", os.defpath), "DFE_PROFILE": "scale"}
    got = _render(
        [sys.executable, str(RENDER_ENV)], b'profile: "${DFE_PROFILE}"\nzone: "$GONE"\n', env
    )
    assert got == b'profile: "scale"\nzone: ""\n'


def test_the_cli_refuses_an_argument_it_does_not_take() -> None:
    done = subprocess.run(
        [sys.executable, str(RENDER_ENV), "DFE_PROFILE"],
        input=b"",
        capture_output=True,
        check=False,
    )
    assert done.returncode == 2


def test_bootstrap_renders_every_template_through_render_env_and_never_envsubst() -> None:
    lines = (BOOTSTRAP / "bootstrap.sh").read_text(encoding="utf-8").splitlines()
    code = [line for line in lines if not line.lstrip().startswith("#")]
    assert [line for line in code if re.search(r"\benvsubst\b", line)] == []
    renders = [
        line for line in code if "render_env.py" in line and not line.lstrip().startswith("echo ")
    ]
    # The cluster secret, the AWS StorageClass, the MetalLB pool and both secret store bodies.
    assert len(renders) == 5, renders
    assert all('python3 "${SCRIPT_DIR}/render_env.py" < "' in line for line in renders)
