#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_smoke_raw_and_hyperdx_checks.py
#  Purpose:      Prove the integration smoke test asserts the loader's current _raw
#                contract and asks HyperDX's readiness through the node runtime.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""CORE 2's `_raw` checks and the ancillary HyperDX readiness check.

dfe-loader stores a JSON payload once, in `_json`, and leaves `_raw` NULL unless
the payload carries a `_raw` field of its own, which it keeps. The check used to
demand a populated `_raw` on a plain POST, so a loader doing the right thing
failed it. The HyperDX image ships node and neither wget nor curl, so the
readiness check asks /readyz through node. A fake `kubectl` first on PATH answers
from the POSTs it receives and from a `hyperdx_ready` verdict.

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
HYPERDX = "hyperdx API reaches its ferretdb backend"


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


def test_hyperdx_readiness_is_asked_through_node() -> None:
    stdout = smoke({"hyperdx_ready": True})

    expect("a ready API passes", f"[PASS] {HYPERDX}" in stdout, stdout)


def test_hyperdx_that_does_not_answer_ready_fails() -> None:
    stdout = smoke({"hyperdx_ready": False})

    expect("a not-ready API fails", f"[FAIL] {HYPERDX}" in stdout, stdout)


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
