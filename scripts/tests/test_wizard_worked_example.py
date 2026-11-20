#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_wizard_worked_example.py
#  Purpose:      Hold docs/deployment/wizard.md's worked example to what dfe-ops init writes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The page's answers file, run through `dfe-ops init --dry-run`, writes the page's dial."""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PAGE = REPO_ROOT / "docs" / "deployment" / "wizard.md"
# version.pin follows versions.yaml's `current`, which the page says, so it is the one line compared loosely.
PIN_LINE = re.compile(r"^  pin: .*$", re.MULTILINE)


def _fence(title: str) -> str:
    """The body of the page's fenced block carrying `title="<title>"`."""
    match = re.search(
        rf'^```\w+ title="{re.escape(title)}"\n(.*?)^```$',
        PAGE.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    assert match, f'{PAGE.name} has no fenced block titled "{title}"'
    return match.group(1)


def test_the_worked_examples_answers_write_its_dial(tmp_path: Path) -> None:
    answers = tmp_path / "onprem-small.answers"
    answers.write_text(_fence("onprem-small.answers"), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/dfe-ops",
            "init",
            "--answers",
            str(answers),
            "--dry-run",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    written = PIN_LINE.sub("  pin: <current>", result.stdout)
    documented = PIN_LINE.sub("  pin: <current>", _fence("deployment.yaml"))
    assert written == documented, (
        f"{PAGE.relative_to(REPO_ROOT)}'s deployment.yaml no longer matches what its "
        "answers file writes: re-run the answers file with --dry-run and paste the output"
    )
