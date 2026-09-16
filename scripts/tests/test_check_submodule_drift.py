#  Project:      dfe-infra
#  File:         scripts/tests/test_check_submodule_drift.py
#  Purpose:      Guard the drift guard: unreachable is a skip, absent is a fail
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The distinction this check exists to get right.

CI's default GITHUB_TOKEN is scoped to the repo it runs in, so every sibling
private repo 404s exactly as a deleted one would. Reporting that as drift makes
the gate cry wolf on every run; reporting it as a pass makes it useless. It is a
SKIP, and these tests pin that apart from the two cases that are real failures.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE = Path(__file__).resolve().parents[1] / "check_submodule_drift.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_submodule_drift", _MODULE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def guard(monkeypatch):
    module = _load()
    monkeypatch.setattr(
        module,
        "SHARED_SUBMODULES",
        {"dfe-schemas": {"path": "schemas", "consumers": ["dfe-engine"]}},
    )
    return module


def _api(responses: dict[str, object]):
    """A _gh_json stand-in: endpoints absent from the map resolve to None."""

    def fake(endpoint: str) -> object | None:
        return responses.get(endpoint)

    return fake


def test_unreadable_consumer_is_a_skip_not_a_failure(guard, monkeypatch):
    monkeypatch.setattr(
        guard,
        "_gh_json",
        _api(
            {
                "repos/hyperi-io/dfe-schemas": {"name": "dfe-schemas"},
                "repos/hyperi-io/dfe-schemas/commits/HEAD": {"sha": "a" * 40},
            }
        ),
    )
    failures, rows, skips = guard.audit()
    assert failures == []
    assert rows == []
    assert skips == ["dfe-engine: not readable by this token"]


def test_readable_consumer_missing_the_submodule_is_a_failure(guard, monkeypatch):
    monkeypatch.setattr(
        guard,
        "SHARED_SUBMODULES",
        {"dfe-schemas": {"path": "schemas", "consumers": ["dfe-engine", "dfe-loader"]}},
    )
    monkeypatch.setattr(
        guard,
        "_gh_json",
        _api(
            {
                "repos/hyperi-io/dfe-schemas": {"name": "dfe-schemas"},
                "repos/hyperi-io/dfe-schemas/commits/HEAD": {"sha": "a" * 40},
                "repos/hyperi-io/dfe-engine": {"name": "dfe-engine"},
                # A directory where a submodule belongs: the path exists, so this
                # is a real removal rather than a permissions artefact.
                "repos/hyperi-io/dfe-engine/contents/schemas": {"type": "dir"},
                "repos/hyperi-io/dfe-loader": {"name": "dfe-loader"},
                "repos/hyperi-io/dfe-loader/contents/schemas": {
                    "type": "submodule",
                    "sha": "a" * 40,
                },
            }
        ),
    )
    failures, _rows, skips = guard.audit()
    assert skips == []
    assert len(failures) == 1
    assert "removed or moved" in failures[0]


def test_a_module_no_consumer_vendors_any_more_says_to_delete_the_entry(guard, monkeypatch):
    """The whole class retiring is one stale entry, not N repos disagreeing.

    dfe-schemas became a pinned wheel and every consumer dropped the submodule;
    reported as drift, the same output read as three repos to go and fix.
    """
    monkeypatch.setattr(
        guard,
        "_gh_json",
        _api(
            {
                "repos/hyperi-io/dfe-schemas": {"name": "dfe-schemas"},
                "repos/hyperi-io/dfe-schemas/commits/HEAD": {"sha": "a" * 40},
                "repos/hyperi-io/dfe-engine": {"name": "dfe-engine"},
                "repos/hyperi-io/dfe-engine/contents/schemas": {"type": "dir"},
            }
        ),
    )
    failures, _rows, skips = guard.audit()
    assert skips == []
    assert len(failures) == 1
    assert "delete the entry from SHARED_SUBMODULES" in failures[0]


def test_an_empty_table_is_a_pass_that_says_so(guard, monkeypatch, capsys):
    """Nothing in the suite vendors a shared submodule, and a check that printed
    a SKIP for that would be claiming something went unverified."""
    monkeypatch.setattr(guard, "SHARED_SUBMODULES", {})
    monkeypatch.setattr(sys, "argv", ["check_submodule_drift.py"])
    assert guard.main() == 0
    assert "no shared submodule is declared" in capsys.readouterr().out


def test_a_stale_pin_is_a_failure_with_the_gap(guard, monkeypatch):
    monkeypatch.setattr(
        guard,
        "_gh_json",
        _api(
            {
                "repos/hyperi-io/dfe-schemas": {"name": "dfe-schemas"},
                "repos/hyperi-io/dfe-schemas/commits/HEAD": {"sha": "a" * 40},
                "repos/hyperi-io/dfe-engine": {"name": "dfe-engine"},
                "repos/hyperi-io/dfe-engine/contents/schemas": {
                    "type": "submodule",
                    "sha": "b" * 40,
                },
                f"repos/hyperi-io/dfe-schemas/compare/{'b' * 40}...{'a' * 40}": {"ahead_by": 26},
            }
        ),
    )
    failures, rows, _skips = guard.audit()
    assert len(failures) == 1
    assert "26 commit(s) behind" in failures[0]
    assert rows[0]["behind"] == 26


def test_an_agreeing_pin_passes(guard, monkeypatch):
    monkeypatch.setattr(
        guard,
        "_gh_json",
        _api(
            {
                "repos/hyperi-io/dfe-schemas": {"name": "dfe-schemas"},
                "repos/hyperi-io/dfe-schemas/commits/HEAD": {"sha": "a" * 40},
                "repos/hyperi-io/dfe-engine": {"name": "dfe-engine"},
                "repos/hyperi-io/dfe-engine/contents/schemas": {
                    "type": "submodule",
                    "sha": "a" * 40,
                },
            }
        ),
    )
    failures, rows, skips = guard.audit()
    assert failures == []
    assert skips == []
    assert rows[0]["behind"] == 0


def test_unreadable_module_skips_rather_than_failing(guard, monkeypatch):
    monkeypatch.setattr(guard, "_gh_json", _api({}))
    failures, rows, skips = guard.audit()
    assert failures == []
    assert rows == []
    assert skips == ["dfe-schemas: not readable by this token"]
