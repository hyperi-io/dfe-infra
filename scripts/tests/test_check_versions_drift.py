#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_versions_drift.py
#  Purpose:      Prove the drift check's FAILURE paths fire, not just its
#                happy path -- a guard nobody has seen fail is not a guard.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Failure-path tests for scripts/check_versions_drift.py.

A drift check that only ever prints OK proves nothing: the same output comes
from a check that swept the tree and one whose patterns match nothing. These
tests regress the value a pin used to hold, or drop a waiver, and assert the
check NOTICES.

Everything runs in memory -- no tracked file is edited, so a failed run leaves
no mess to clean up.

    python3 scripts/tests/test_check_versions_drift.py

No third-party deps and no test runner, matching the check it tests: both run
on a bare CI image.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_versions_drift.py"

spec = importlib.util.spec_from_file_location("drift", SCRIPT)
drift = importlib.util.module_from_spec(spec)
# @dataclass resolves sys.modules[cls.__module__] while decorating, so the
# module must be registered before exec_module rather than after.
sys.modules["drift"] = drift
spec.loader.exec_module(drift)

_failures = 0


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


def test_sweep_is_clean_as_committed() -> None:
    problems = drift.reverse_sweep()
    expect("sweep is clean as committed", problems == [], f"got {problems}")


def test_sweep_reads_a_real_tree() -> None:
    """Guards the failure mode where the roots stop resolving and it sweeps air."""
    files = drift.sweep_files()
    expect(
        "sweep reaches a non-trivial file set", len(files) > 100, f"got {len(files)}"
    )


def test_sweep_reaches_the_repo_root() -> None:
    """The root is swept non-recursively, so top-level pins are not invisible."""
    files = drift.sweep_files()
    expect(
        "deployment.example.yaml is in the swept set",
        Path("deployment.example.yaml") in files,
        f"root files swept: {[f for f in files if f.parent == Path('.')]}",
    )


def test_stack_pin_surfaces_without_its_check() -> None:
    """Drop the check and the example's stack pin must come back as unswept.

    Proves the root sweep and the `pin:` pattern both work, rather than the
    literal being invisible to the sweep and merely covered by a check.
    """
    original = drift.CHECKS
    try:
        drift.CHECKS = [
            c for c in original if c.label != "stack pin (deployment example)"
        ]
        unswept = [
            p
            for p in drift.reverse_sweep()
            if "[unswept]" in p and "deployment.example.yaml" in p
        ]
        expect(
            "the example's stack pin is visible to the sweep",
            len(unswept) == 1,
            f"got {unswept}",
        )
    finally:
        drift.CHECKS = original


def test_stale_stack_pin_is_caught() -> None:
    """A worked example left on the previous stack must be reported."""
    versions = drift.load_versions()
    check = next(c for c in drift.CHECKS if c.label == "stack pin (deployment example)")
    real_read = drift.read_source

    def poisoned(file_path: Path) -> str:
        text = real_read(file_path)
        if file_path.as_posix() == "deployment.example.yaml":
            return text.replace(versions["pointers.current"], "2.1.0-rc.9")
        return text

    try:
        drift.read_source = poisoned
        expect(
            "an example pinning a superseded stack is caught",
            drift.extract_value(check) != versions["pointers.current"],
        )
    finally:
        drift.read_source = real_read

    expect(
        "the example matches the current stack as committed",
        drift.extract_value(check) == versions["pointers.current"],
    )


def test_missing_sweep_root_is_fatal() -> None:
    """A renamed directory must stop the run, not quietly shrink the sweep."""
    original = drift.SWEEP_ROOTS
    try:
        drift.SWEEP_ROOTS = original + ("does-not-exist",)
        drift.sweep_files.cache_clear()
        raised = False
        try:
            drift.sweep_files()
        except SystemExit:
            raised = True
        expect("a missing sweep root is fatal", raised, "no SystemExit raised")
    finally:
        drift.SWEEP_ROOTS = original
        drift.sweep_files.cache_clear()


def test_waiver_that_excuses_nothing_is_reported() -> None:
    original = drift.SWEEP_WAIVERS
    try:
        drift.SWEEP_WAIVERS = original + (
            ("helm/charts/does-not-exist/values.yaml", "image tag", "nothing"),
        )
        problems = drift.reverse_sweep()
        expect(
            "a waiver matching nothing is reported as rot",
            any("[stale]" in p and "does-not-exist" in p for p in problems),
            f"got {problems}",
        )
    finally:
        drift.SWEEP_WAIVERS = original


def test_dropping_a_waiver_resurfaces_its_literals() -> None:
    original = drift.SWEEP_WAIVERS
    try:
        drift.SWEEP_WAIVERS = tuple(w for w in original if w[0] != "*/Chart.yaml")
        unswept = [p for p in drift.reverse_sweep() if "[unswept]" in p]
        expect(
            "dropping the Chart.yaml waiver resurfaces its literals",
            len(unswept) > 20,
            f"got {len(unswept)}",
        )
    finally:
        drift.SWEEP_WAIVERS = original


def test_regressed_appversions_are_caught() -> None:
    """The values these charts carried before the reverse sweep found them.

    dfe-common.labels stamps app.kubernetes.io/version from .Chart.AppVersion,
    so a stale value here ships a wrong version label onto every rendered
    object -- silent, because no image tag depends on it.
    """
    versions = drift.load_versions()
    regressions = {
        "helm/charts/clickhouse-cluster/Chart.yaml": (
            "services.clickhouse-version",
            "clickhouse-cluster chart appVersion",
            '"24.8"',
        ),
        "helm/charts/kafka/Chart.yaml": (
            "services.kafka-version",
            "kafka chart appVersion",
            '"3.9.0"',
        ),
    }
    real_read = drift.read_source

    def poisoned(file_path: Path) -> str:
        text = real_read(file_path)
        entry = regressions.get(file_path.as_posix())
        if not entry:
            return text
        key, _, old = entry
        return text.replace(f'"{versions[key]}"', old)

    try:
        drift.read_source = poisoned
        for key, label, old in regressions.values():
            check = next(c for c in drift.CHECKS if c.label == label)
            actual = drift.extract_value(check)
            expect(
                f"{label}: the pre-fix value {old} is caught as drift",
                actual != versions[key],
                f"{actual!r} vs SSoT {versions[key]!r}",
            )
    finally:
        drift.read_source = real_read

    for key, label, _ in regressions.values():
        check = next(c for c in drift.CHECKS if c.label == label)
        expect(
            f"{label}: matches SSoT as committed",
            drift.extract_value(check) == versions[key],
        )


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'FAILED' if _failures else 'ALL PASSED'} -- {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
