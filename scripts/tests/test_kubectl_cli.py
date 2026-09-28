#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_kubectl_cli.py
#  Purpose:      Prove run_kubectl decodes kubectl's output as UTF-8 and survives
#                a byte that is not UTF-8.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/kubectl_cli.py.

    python3 -m pytest scripts/tests/test_kubectl_cli.py -q

A fake `kubectl` first on PATH writes fixed bytes, so no cluster is involved.
"""

import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import kubectl_cli  # noqa: E402

# Echoes its argv, a UTF-8 e-acute, then a byte no UTF-8 decoder accepts.
FAKE_KUBECTL = """#!/usr/bin/env python3
import sys

sys.stdout.buffer.write(" ".join(sys.argv[1:]).encode("utf-8") + b" caf\\xc3\\xa9 \\xff\\n")
"""


@pytest.fixture
def fake_kubectl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


@pytest.mark.usefixtures("fake_kubectl")
def test_output_is_utf8_and_a_bad_byte_is_replaced_not_raised() -> None:
    done = kubectl_cli.run_kubectl(["get", "nodes"])
    e_acute, replacement = chr(0xE9), chr(0xFFFD)
    assert done.returncode == 0
    assert done.stdout == f"get nodes caf{e_acute} {replacement}\n"
