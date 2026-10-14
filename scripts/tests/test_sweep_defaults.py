#  Project:      dfe-infra
#  File:         scripts/tests/test_sweep_defaults.py
#  Purpose:      Prove the committed sweep defaults carry the Slack headline's title
#                and time zone, in values dfe-sweep's own validators accept.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for config/defaults.yaml's `sweep.slack` block.

dfe-sweep refuses to run when `slack.title` or `slack.timezone` resolves to nothing
usable, and the committed defaults are the layer every run falls back to. The file is
read here as YAML, the way scalo's cascade reads it, and each value goes through the
validator the CLI applies to it.

    python3 -m pytest scripts/tests/test_sweep_defaults.py -q
"""

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULTS = REPO_ROOT / "config" / "defaults.yaml"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# The CLI has no .py suffix, so it is loaded by path; it imports scalo only when it runs.
_CLI = REPO_ROOT / "scripts" / "dfe-sweep"
_SPEC = importlib.util.spec_from_file_location(
    "dfe_sweep_defaults_cli", _CLI, loader=SourceFileLoader("dfe_sweep_defaults_cli", str(_CLI))
)
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)


def _slack() -> dict:
    """The `slack` map under the sweep's own section of the committed defaults."""
    settings = yaml.safe_load(DEFAULTS.read_text(encoding="utf-8"))[cli.SECTION]
    assert isinstance(settings.get("slack"), dict), settings
    return settings["slack"]


def test_the_defaults_name_a_slack_title_the_cli_accepts() -> None:
    title = _slack().get("title")
    assert isinstance(title, str), title
    assert title.strip(), title
    assert cli._slack_title(title) == title.strip()


def test_the_defaults_name_a_slack_timezone_the_cli_accepts() -> None:
    zone = _slack().get("timezone")
    assert isinstance(zone, str), zone
    assert zone.strip(), zone
    assert cli._slack_zone(zone) is not None
