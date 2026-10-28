#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_signing_keys.py
#  Purpose:      Prove the signing-key watch fails inside its window and passes outside it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""scripts/check_signing_keys.py: the expiry window, and the vendored keys as they stand.

    python3 -m pytest scripts/tests/test_check_signing_keys.py

Needs `gpg` on PATH.
"""

import datetime
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_signing_keys as watch

NOW = datetime.datetime(2026, 10, 29, tzinfo=datetime.UTC)


def at(days: float) -> int:
    return int((NOW + datetime.timedelta(days=days)).timestamp())


@pytest.mark.parametrize(
    ("expires", "flagged"),
    [(at(-1), True), (at(10), True), (at(watch.WARN_DAYS - 1), True), (at(watch.WARN_DAYS + 1), False), (None, False)],
)
def test_a_key_is_flagged_only_inside_the_window(expires: int | None, flagged: bool) -> None:
    findings = watch.expiry_findings("k", [{"expires": expires, "fingerprint": "AB" * 20}], NOW)
    assert bool(findings) is flagged


def test_an_expired_key_says_so() -> None:
    (finding,) = watch.expiry_findings("k", [{"expires": at(-1), "fingerprint": "AB" * 20}], NOW)
    assert "has expired" in finding


def test_gpg_expiry_round_trips_into_a_finding() -> None:
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "GNUPGHOME": home}
        home_dir = Path(home)
        home_dir.chmod(0o700)
        run = lambda *args: subprocess.run(  # noqa: E731
            ["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", "", *args],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        )
        run("--quick-generate-key", "watch@example.invalid", "ed25519", "sign", "10d")
        armoured = run("--armor", "--export", "watch@example.invalid").stdout
    (key,) = watch.show_keys(armoured)
    assert len(key["fingerprint"]) == 40
    assert watch.expiry_findings("k", [key], datetime.datetime.now(datetime.UTC))


def test_the_vendored_keys_are_valid_past_the_window() -> None:
    assert watch.vendored_findings(datetime.datetime.now(datetime.UTC)) == []


def test_the_dockerfile_still_names_the_kubernetes_pin() -> None:
    minor, fingerprint = watch.pinned_kubernetes()
    assert minor.startswith("v1.")
    assert len(fingerprint) == 40
