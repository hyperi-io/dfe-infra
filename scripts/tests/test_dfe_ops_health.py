#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_health.py
#  Purpose:      Prove the onboarding idle check reads scalo's /healthz in both
#                shapes it is served in, and treats an absent endpoint as an
#                answer rather than a failure.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the /healthz readers in scripts/dfe-ops.

scalo 2.12 serves no /healthz (scalo-rs#66), so the idle check has to survive a
404 rather than fail the deploy on it -- and when the endpoint does arrive, the
components block is a list in some scalo versions and a map in others. Both
shapes and the absent case are covered here; no network, so the suite is
hermetic.

    python3 -m pytest scripts/tests/test_dfe_ops_health.py -q
    python3 scripts/tests/test_dfe_ops_health.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_health", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_health", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_health"] = dfeops
_loader.exec_module(dfeops)

URL = "http://localhost:9090/healthz"

LIST_SHAPE = {
    "status": "Degraded",
    "components": [
        {"name": "work_config", "status": "Degraded", "message": "no destination configured"},
        {"name": "broker", "status": "Healthy"},
    ],
}
MAP_SHAPE = {
    "status": "Degraded",
    "components": {
        "work_config": {"status": "Degraded", "detail": "no destination configured"},
        "broker": {"status": "Healthy"},
    },
}


class _Response(io.BytesIO):
    """The context-manager shape urlopen returns."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _serving(payload) -> object:
    body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    text = body.encode("utf-8") if isinstance(body, str) else body
    return lambda url, timeout=None: _Response(text)


def _raising(exc: Exception) -> object:
    def _open(url, timeout=None):
        raise exc

    return _open


def _with_urlopen(opener, call):
    original = dfeops.urllib.request.urlopen
    dfeops.urllib.request.urlopen = opener
    try:
        return call()
    finally:
        dfeops.urllib.request.urlopen = original


def test_a_missing_endpoint_reads_as_no_body() -> None:
    """scalo 2.12 serves no /healthz, and that is not a deploy failure."""
    exc = urllib.error.HTTPError(URL, 404, "Not Found", {}, None)
    body = _with_urlopen(_raising(exc), lambda: dfeops._health_body(URL))
    expect("a 404 is None", body is None, repr(body))
    got = _with_urlopen(_raising(exc), lambda: dfeops._health_component(URL, "work_config"))
    expect("and so is the component", got is None, repr(got))


def test_a_refused_connection_reads_as_no_body() -> None:
    got = _with_urlopen(_raising(OSError("connection refused")), lambda: dfeops._health_body(URL))
    expect("an unreachable port is None", got is None, repr(got))


def test_a_non_json_answer_reads_as_no_body() -> None:
    got = _with_urlopen(_serving("not json at all"), lambda: dfeops._health_body(URL))
    expect("a non-JSON body is None", got is None, repr(got))


def test_a_json_array_reads_as_no_body() -> None:
    """The reader indexes the body, so anything but an object is no body."""
    got = _with_urlopen(_serving([1, 2, 3]), lambda: dfeops._health_body(URL))
    expect("a JSON array is None", got is None, repr(got))


def test_the_body_comes_back_whole() -> None:
    got = _with_urlopen(_serving(LIST_SHAPE), lambda: dfeops._health_body(URL))
    expect("the whole body is returned", got == LIST_SHAPE, repr(got))


def test_a_components_list_is_matched_by_name() -> None:
    got = _with_urlopen(
        _serving(LIST_SHAPE), lambda: dfeops._health_component(URL, "work_config")
    )
    expect("the named entry comes back", (got or {}).get("status") == "Degraded", repr(got))
    expect(
        "with its reason",
        (got or {}).get("message") == "no destination configured",
        repr(got),
    )


def test_a_components_map_is_keyed_by_name() -> None:
    got = _with_urlopen(_serving(MAP_SHAPE), lambda: dfeops._health_component(URL, "work_config"))
    expect("the keyed entry comes back", (got or {}).get("status") == "Degraded", repr(got))
    expect("with its reason", (got or {}).get("detail") == "no destination configured", repr(got))


def test_an_absent_component_is_none() -> None:
    for shape in (LIST_SHAPE, MAP_SHAPE):
        got = _with_urlopen(_serving(shape), lambda: dfeops._health_component(URL, "nothing"))
        expect("an unnamed component is None", got is None, repr(got))


def test_a_body_without_components_is_none() -> None:
    got = _with_urlopen(_serving({"status": "Healthy"}), lambda: dfeops._health_component(URL, "x"))
    expect("no components block is None", got is None, repr(got))


def main() -> int:
    with standalone():
        test_a_missing_endpoint_reads_as_no_body()
        test_a_refused_connection_reads_as_no_body()
        test_a_non_json_answer_reads_as_no_body()
        test_a_json_array_reads_as_no_body()
        test_the_body_comes_back_whole()
        test_a_components_list_is_matched_by_name()
        test_a_components_map_is_keyed_by_name()
        test_an_absent_component_is_none()
        test_a_body_without_components_is_none()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
