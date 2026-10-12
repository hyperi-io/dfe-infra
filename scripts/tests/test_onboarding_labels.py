#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_onboarding_labels.py
#  Purpose:      Prove the onboarding run finds a console field by its label
#                wherever the browser puts the required-field marker
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the onboarding run's field-label pattern.

    python3 -m pytest scripts/tests/test_onboarding_labels.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from acceptance.onboarding.run import _label  # noqa: E402


@pytest.mark.parametrize(
    "accessible_name",
    ["Username", "Username *", "Username*", "* Username", "*Username", "  Username  "],
)
def test_a_required_marker_on_either_side_still_matches(accessible_name):
    assert _label("Username").match(accessible_name)


def test_any_listed_label_matches():
    pattern = _label("Username", "User name")
    assert pattern.match("* User name")
    assert pattern.match("Username *")


@pytest.mark.parametrize("accessible_name", ["Username", "* Username", "Name of user", "Names"])
def test_a_label_does_not_match_inside_a_longer_name(accessible_name):
    assert not _label("Name").match(accessible_name)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
