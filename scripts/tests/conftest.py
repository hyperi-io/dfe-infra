#  Project:      dfe-infra
#  File:         conftest.py
#  Purpose:      Size pytest-xdist's `-n auto` to the cores and memory the host has free
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Resolve `-n auto` to a worker count a shared host can carry.

A CI runner is dedicated, so it gets every CPU and 80% of the free memory. Any other host
is shared, so a run gets at most half the CPUs, never more than are idle by the one-minute
load average, and half the free memory. An explicit `-n 4` never reaches this hook.
"""

import os
import shutil
from pathlib import Path

import pytest

# Peak RSS of one worker on this suite, with headroom.
WORKER_MEM_MB = 1024


def on_ci() -> bool:
    return os.environ.get("CI", "").lower() not in ("", "0", "false")


def _cpus() -> int:
    return os.process_cpu_count() or 1


def _available_mb() -> float | None:
    """MemAvailable, or the cgroup v2 memory.max headroom when that is lower. None where unreadable."""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                available = int(line.split()[1]) / 1024
                break
        else:
            return None
    except (OSError, ValueError):
        return None
    try:
        limit = Path("/sys/fs/cgroup/memory.max").read_text(encoding="utf-8").strip()
        used = int(Path("/sys/fs/cgroup/memory.current").read_text(encoding="utf-8"))
        if limit != "max":
            available = min(available, (int(limit) - used) / 1024 / 1024)
    except (OSError, ValueError):
        pass
    return available


def auto_workers() -> int:
    cpus = _cpus()
    cpu_budget = cpus
    memory_share = 0.8
    if not on_ci():
        cpu_budget = cpus // 2
        memory_share = 0.5
        try:
            cpu_budget = min(cpu_budget, int(cpus - os.getloadavg()[0]))
        except OSError:
            pass
    available = _available_mb()
    if available is None:
        return max(1, cpu_budget)
    return max(1, min(cpu_budget, int(available * memory_share // WORKER_MEM_MB)))


@pytest.hookimpl(optionalhook=True)  # absent under `-p no:xdist`, where the hook has no spec
def pytest_xdist_auto_num_workers(config):
    if config.option.numprocesses not in ("auto", "logical"):
        return None
    return auto_workers()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_tool(name): needs the outside binary on PATH. Skips locally when it is"
        " missing and fails under CI, where the workflow installs it.",
    )


def tool_gate(tool: str, found: str | None, ci: bool) -> tuple[str, str] | None:
    """The outcome a missing `tool` forces: ("fail" | "skip", reason), or None when it is present."""
    if found is not None:
        return None
    if ci:
        return "fail", f"{tool} is not on PATH under CI, so the workflow's install step has not run"
    return "skip", f"needs {tool} on PATH"


def pytest_runtest_setup(item):
    for marker in item.iter_markers("requires_tool"):
        tool = marker.args[0]
        gate = tool_gate(tool, shutil.which(tool), on_ci())
        if gate is not None:
            (pytest.fail if gate[0] == "fail" else pytest.skip)(gate[1])
