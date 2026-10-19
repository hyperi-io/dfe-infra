#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_render_dial_disruption.py
#  Purpose:      Guard the dial's k8s.disruption block -- the posture and the
#                maintenance window read through one vocabulary, and a window
#                no pools renderer can carry is refused by name before a resolve.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for render_dial.py's `read_disruption` and the summary line it prints.

    python3 -m pytest scripts/tests/test_render_dial_disruption.py -q

The budgets the block becomes are tested in test_resolve_sizing.py, and their
NodePool render in test_node_pools.py.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
EXAMPLE = REPO_ROOT / "deployment.example.yaml"

sys.path.insert(0, str(SCRIPTS))
import render_dial  # noqa: E402
from yaml_subset import parse as parse_dial  # noqa: E402

# The example's own block, edited in place by the render tests below.
EXAMPLE_POSTURE = "    posture: balanced        # balanced | conservative\n"
EXAMPLE_SCHEDULE = (
    '      schedule: ""           # UTC cron, daily "M H * * *" or weekly "M H * * D" '
    '(0 is Sunday), e.g. "0 2 * * 6"\n'
)
EXAMPLE_DURATION = '      duration: ""           # hours and minutes, e.g. 4h or 3h30m, set together with schedule\n'


def disruption(body: str) -> render_dial.Disruption:
    """read_disruption over a k8s slice whose disruption block is `body`."""
    return render_dial.read_disruption(
        parse_dial(f"k8s:\n  cloud: aws\n  disruption:\n{body}", source="test-dial")
    )


def window(schedule: str, duration: str) -> str:
    """A disruption block body carrying one maintenance window."""
    return f'    maintenance_window:\n      schedule: "{schedule}"\n      duration: "{duration}"\n'


def test_a_dial_with_no_block_is_balanced_with_no_window() -> None:
    read = render_dial.read_disruption(parse_dial("k8s:\n  cloud: aws\n", source="test-dial"))
    assert read == render_dial.Disruption("balanced", None)


def test_the_example_dial_declares_the_block_and_reads_as_the_default() -> None:
    """deployment.example.yaml is the dial's schema, so every key it reads is declared there."""
    text = EXAMPLE.read_text(encoding="utf-8")
    for line in (EXAMPLE_POSTURE, EXAMPLE_SCHEDULE, EXAMPLE_DURATION):
        assert text.count(line) == 1, line
    read = render_dial.read_disruption(parse_dial(text, source=str(EXAMPLE)))
    assert read == render_dial.Disruption("balanced", None)


@pytest.mark.parametrize("posture", render_dial.DISRUPTION_POSTURES)
def test_each_posture_reads_through(posture: str) -> None:
    assert disruption(f"    posture: {posture}\n").posture == posture


def test_a_daily_window_reads_its_start_and_length() -> None:
    read = disruption(window("0 2 * * *", "4h")).window
    assert read == render_dial.MaintenanceWindow(minute=0, hour=2, weekday=None, minutes=240)
    assert read.period_minutes == 24 * 60


def test_a_weekly_window_reads_its_day() -> None:
    read = disruption(window("30 22 * * 6", "3h30m")).window
    assert read == render_dial.MaintenanceWindow(minute=30, hour=22, weekday=6, minutes=210)
    assert read.period_minutes == 7 * 24 * 60


@pytest.mark.parametrize(
    ("schedule", "duration", "minutes"),
    [("0 0 * * *", "1m", 1), ("0 2 * * *", "23h59m", 1439), ("59 23 * * 0", "167h59m", 10079)],
)
def test_the_shortest_and_longest_windows_are_accepted(schedule: str, duration: str, minutes: int) -> None:
    assert disruption(window(schedule, duration)).window.minutes == minutes


@pytest.mark.parametrize("posture", ["aggressive", "Balanced", "none"])
def test_expected_fail_a_posture_outside_the_vocabulary_is_refused_by_name(posture: str) -> None:
    with pytest.raises(render_dial.DialError, match=r"k8s\.disruption\.posture must be one of"):
        disruption(f"    posture: {posture}\n")


@pytest.mark.parametrize(
    "body",
    [
        '    maintenance_window:\n      schedule: "0 2 * * *"\n',
        '    maintenance_window:\n      duration: "4h"\n',
        '    maintenance_window:\n      schedule: "0 2 * * *"\n      duration: ""\n',
    ],
)
def test_expected_fail_half_a_window_is_refused(body: str) -> None:
    """A window is when it opens and how long it stays open, so either alone means nothing."""
    with pytest.raises(render_dial.DialError, match="needs both schedule and duration"):
        disruption(body)


@pytest.mark.parametrize(
    "schedule",
    [
        "0 2 * * 1-5",
        "0 2 * * 1,3",
        "0 2 1 * *",
        "0 2 * 6 *",
        "@daily",
        "0 2 * * mon",
        "0 2 * *",
        "*/15 2 * * *",
        "60 2 * * *",
        "0 24 * * *",
        "0 2 * * 7",
    ],
)
def test_expected_fail_a_schedule_with_no_single_complement_is_refused(schedule: str) -> None:
    """The drift hold outside the window is one cron line, so only a daily or one-weekday start has one."""
    with pytest.raises(render_dial.DialError, match=r"k8s\.disruption\.maintenance_window\.schedule"):
        disruption(window(schedule, "4h"))


@pytest.mark.parametrize(
    ("schedule", "duration"),
    [
        ("0 2 * * *", "0m"),
        ("0 2 * * *", "0h0m"),
        ("0 2 * * *", "24h"),
        ("0 2 * * *", "1d"),
        ("0 2 * * *", "90s"),
        ("0 2 * * *", "4"),
        ("0 2 * * *", "4h0s"),
        ("0 2 * * 0", "168h"),
    ],
)
def test_expected_fail_a_duration_that_is_empty_or_never_closes_is_refused(schedule: str, duration: str) -> None:
    with pytest.raises(render_dial.DialError, match=r"k8s\.disruption\.maintenance_window\.duration"):
        disruption(window(schedule, duration))


def _render(tmp_path: Path, text: str) -> subprocess.CompletedProcess:
    dial = tmp_path / "deployment.yaml"
    dial.write_text(text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "render_dial.py"), "--dial", str(dial), "--out", str(tmp_path / "bootstrap.env")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_the_render_says_drift_runs_any_time_with_no_window(tmp_path: Path) -> None:
    result = _render(tmp_path, EXAMPLE.read_text(encoding="utf-8"))
    assert result.returncode == 0, result.stderr
    assert "Pool disruption (k8s.disruption): balanced, drift any time" in result.stderr


def test_the_render_names_the_window_drift_waits_for(tmp_path: Path) -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    text = text.replace(EXAMPLE_SCHEDULE, '      schedule: "0 2 * * 6"\n')
    text = text.replace(EXAMPLE_DURATION, '      duration: "4h"\n')
    result = _render(tmp_path, text)
    assert result.returncode == 0, result.stderr
    assert (
        "Pool disruption (k8s.disruption): balanced, drift only inside 0 2 * * 6 for 4h (UTC), "
        "ci-burst excepted" in result.stderr
    )
    assert "WARNING -- k8s.disruption" not in result.stderr


def test_the_render_warns_that_conservative_with_no_window_holds_stateful_drift(tmp_path: Path) -> None:
    """A stateful node that never takes its drift is the posture working, and the operator needs telling."""
    text = EXAMPLE.read_text(encoding="utf-8").replace(EXAMPLE_POSTURE, "    posture: conservative\n")
    result = _render(tmp_path, text)
    assert result.returncode == 0, result.stderr
    assert "render_dial: WARNING -- k8s.disruption.posture is conservative and sets no" in result.stderr


def test_expected_fail_the_render_refuses_a_bad_block_before_the_env_file_exists(tmp_path: Path) -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace(EXAMPLE_POSTURE, "    posture: fast\n")
    result = _render(tmp_path, text)
    assert result.returncode == 1
    assert "k8s.disruption.posture must be one of" in result.stderr
    assert not (tmp_path / "bootstrap.env").exists()
