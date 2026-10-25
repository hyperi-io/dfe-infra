#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_smoke_raw_and_hyperdx_checks.py
#  Purpose:      Prove the integration smoke test asserts the loader's current _raw
#                contract and pings HyperDX's DocumentDB store through the node runtime.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""CORE 2's `_raw` checks and the ancillary HyperDX store checks.

dfe-loader stores a JSON payload once, in `_json`, and leaves `_raw` NULL unless
the payload carries a `_raw` field of its own, which it keeps. The check used to
demand a populated `_raw` on a plain POST, so a loader doing the right thing
failed it. HyperDX's /readyz never asks its store, so the store check logs in with
the pod's own MONGO_URI through node and the image's mongodb driver, and a failure
prints the line that names the layer. Both it and the engine schema check exec into
the container the thin chart names after itself. A fake `kubectl` first on PATH
answers from the POSTs it receives and from the `hyperdx_store` and `engine_schema`
verdicts.

    python3 scripts/tests/test_smoke_raw_and_hyperdx_checks.py

No third-party deps and no test runner, matching the script it tests.
"""

import sys
import tempfile
from pathlib import Path

from _expect import expect, standalone, summary
from _smoke import run_smoke

KEPT = "[PASS] a payload's own _raw is kept as sent (1 of 3 fixture lines carry one)"
NULLED = "[PASS] payloads without a _raw field land with _raw NULL (stored once, in _json)"
HYPERDX = "hyperdx logs in to its DocumentDB store and pings it"
# The line the probe prints when the DocumentDB gateway rejects the password.
REFUSED = "MongoServerError (code 18): Invalid key"
COLLECTIONS = "hyperdx's collections are in the DocumentDB store's PostgreSQL"
ENGINE_SCHEMA = "dfe-engine reports schema converged"


def smoke(fixture: dict, **env: str) -> str:
    out, _ = run_smoke({"otel_zero_answers": 0, **fixture}, DFE_OTEL_WAIT="0", **env)
    return out.stdout


def test_the_raw_a_payload_carries_is_kept_and_the_others_stay_null() -> None:
    stdout = smoke({})

    expect("the fixture's own _raw is asserted", KEPT in stdout, stdout)
    expect("the lines without one are asserted NULL", NULLED in stdout, stdout)


def test_a_loader_that_copies_the_payload_into_raw_fails_both() -> None:
    stdout = smoke({"loader_copies_payload_to_raw": True})

    expect("the kept-as-sent check fails", KEPT.replace("[PASS]", "[FAIL]") in stdout, stdout)
    expect("the stored-once check fails", NULLED.replace("[PASS]", "[FAIL]") in stdout, stdout)


def test_a_fixture_with_no_raw_line_skips_the_kept_check() -> None:
    line = '{"_source":"main","org_id":"zaphod","post_run_id":"__MARK__","answer":42}\n'
    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp) / "no-raw.ndjson"
        fixture.write_text(line * 2, encoding="utf-8", newline="\n")
        stdout = smoke({}, DFE_POST_FIXTURE=str(fixture))

    expect("the kept check is skipped", "[SKIP] payload-supplied _raw" in stdout, stdout)
    expect("every line is asserted NULL", "[PASS] payloads without a _raw field" in stdout, stdout)


def test_a_store_that_answers_the_ping_passes() -> None:
    stdout = smoke({"hyperdx_store": True})

    expect("a store that answers passes", f"[PASS] {HYPERDX}" in stdout, stdout)


def test_a_store_that_refuses_the_login_fails_naming_the_layer() -> None:
    stdout = smoke({"hyperdx_store": REFUSED})

    expect("a refused login fails", f"[FAIL] {HYPERDX}" in stdout, stdout)
    expect("the FAIL names the code", f"output: {REFUSED}" in stdout, stdout)


def test_the_probe_pings_with_the_pods_own_uri_and_never_asks_readyz() -> None:
    _out, counts = run_smoke({"otel_zero_answers": 0, "hyperdx_store": True}, DFE_OTEL_WAIT="0")
    script = counts.get("hyperdx_probe", "")

    expect("the probe reads MONGO_URI", "process.env.MONGO_URI" in script, script)
    expect("the probe pings the store", "ping: 1" in script, script)
    expect("the probe does not ask /readyz", "readyz" not in script, script)


def test_hyperdx_collections_in_the_store_pass() -> None:
    stdout = smoke({"hyperdx_store": True, "documentdb_collections": 4})

    expect("collections on the primary pass", f"[PASS] {COLLECTIONS}" in stdout, stdout)


def test_a_store_holding_no_hyperdx_collection_fails() -> None:
    stdout = smoke({"hyperdx_store": True, "documentdb_collections": 0})

    expect("an empty catalogue fails", f"[FAIL] {COLLECTIONS}" in stdout, stdout)


def test_the_engine_schema_check_asks_the_thin_chart_container() -> None:
    # The thin chart names the container dfe-engine, after the chart, so a check
    # asking for `-c engine` fails a converged engine.
    stdout = smoke({"engine_schema": True}, DFE_ENGINE_WAIT="0")

    expect("a converged engine passes", f"[PASS] {ENGINE_SCHEMA}" in stdout, stdout)


def test_an_engine_whose_schema_has_not_converged_fails() -> None:
    stdout = smoke({"engine_schema": False}, DFE_ENGINE_WAIT="0")

    expect("an unconverged engine fails", f"[FAIL] {ENGINE_SCHEMA}" in stdout, stdout)


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
