#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_tool_gate.py
#  Purpose:      Prove a test marked requires_tool fails under CI and skips locally
#                when its outside binary is missing.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the requires_tool gate in conftest.py."""

import pytest
from conftest import on_ci, tool_gate


@pytest.mark.parametrize(
    ("found", "ci", "outcome"),
    [
        ("/usr/bin/argocd", False, None),
        ("/usr/bin/argocd", True, None),
        (None, False, "skip"),
        (None, True, "fail"),
    ],
)
def test_a_missing_tool_fails_under_ci_and_skips_elsewhere(found, ci, outcome):
    gate = tool_gate("argocd", found, ci)

    assert (gate[0] if gate else None) == outcome
    if gate:
        assert "argocd" in gate[1]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), ("0", False), ("false", False), ("False", False),
     ("true", True), ("1", True)],
)
def test_ci_is_any_value_but_empty_zero_or_false(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", value)

    assert on_ci() is expected
