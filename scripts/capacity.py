#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         capacity.py
#  Purpose:      Read how much memory a lane can still have, on either lane's
#                terms -- Kubernetes allocatable minus what is already
#                requested, or a docker host's available memory -- so the
#                parsing and the verdict exist once and are testable without a
#                cluster or a daemon. Stdlib only.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""capacity -- what is left, and whether a lane may take it.

A docker lane and a Kubernetes lane can run at once on one hypervisor, and the
host must not be driven into swap by the second one starting while the first is
still coming up. Both lanes therefore answer the same question before they
deploy, and keep answering it while they run.

Nothing here runs a command. It takes the text or the JSON a reading produced
and turns it into bytes and a verdict, so ``scripts/tests/test_capacity.py``
exercises every branch with fabricated readings.
"""

from __future__ import annotations

from dataclasses import dataclass

# BinarySI + DecimalSI suffixes (k8s resource.Quantity). DecimalSI kilo is
# LOWERCASE k; the uppercase forms are kept liberally for hand-written values.
MEM_SUFFIX = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}


def parse_cpu(quantity: str) -> float:
    """Parse a Kubernetes CPU quantity ('4', '3500m', '1500000000n') to cores.

    Args:
        quantity: A resource.Quantity in any of its CPU forms.

    Returns:
        The value in cores.

    Raises:
        ValueError: The text is not a quantity.
    """
    q = quantity.strip()
    if q.endswith("n"):
        return float(q[:-1]) / 1_000_000_000.0
    if q.endswith("u"):
        return float(q[:-1]) / 1_000_000.0
    if q.endswith("m"):
        return float(q[:-1]) / 1000.0
    return float(q)


def parse_mem(quantity: str) -> int:
    """Parse a Kubernetes memory quantity ('16Gi', '104857600k', '2000000') to bytes.

    Args:
        quantity: A resource.Quantity in any of its memory forms.

    Returns:
        The value in bytes.

    Raises:
        ValueError: The text is not a quantity.
    """
    q = quantity.strip()
    if q.endswith("m"):  # milli-bytes -- rare but a legal Quantity emission
        return int(float(q[:-1]) / 1000)
    for suffix in ("Ki", "Mi", "Gi", "Ti", "k", "K", "M", "G", "T"):
        if q.endswith(suffix):
            return int(float(q[: -len(suffix)]) * MEM_SUFFIX[suffix])
    return int(float(q))


@dataclass(frozen=True, slots=True)
class Reading:
    """One lane's memory picture, in bytes."""

    lane: str
    total: int
    """Allocatable across Ready nodes, or the host's total."""

    committed: int
    """Already requested by scheduled pods, or already in use on the host."""

    free: int
    """What a new lane can still take."""

    detail: str
    """One line naming where the numbers came from."""


def meminfo_available(text: str) -> Reading:
    """A docker host's memory from /proc/meminfo.

    MemAvailable is the kernel's own estimate of what a new workload can have
    without swapping, which is the question here - MemFree is not, because
    reclaimable page cache counts as free capacity.

    Args:
        text: The contents of /proc/meminfo.

    Returns:
        The reading, in bytes.

    Raises:
        ValueError: Neither MemTotal nor MemAvailable is present.
    """
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if key in ("MemTotal", "MemAvailable") and parts:
            # meminfo reports kB, whatever the label says.
            values[key] = int(parts[0]) * 1024
    if "MemTotal" not in values or "MemAvailable" not in values:
        raise ValueError("meminfo carried neither MemTotal nor MemAvailable")
    total, free = values["MemTotal"], values["MemAvailable"]
    return Reading(
        lane="docker",
        total=total,
        committed=total - free,
        free=free,
        detail=f"/proc/meminfo: {free / 1024**3:.1f}Gi available of {total / 1024**3:.1f}Gi",
    )


def free_output_available(text: str) -> Reading:
    """A docker host's memory from ``free -b``.

    The fallback for a host with no readable /proc/meminfo. The `available`
    column is the same estimate MemAvailable carries; a `free` old enough not to
    print it is refused rather than guessed at, because `free` alone would gate
    on a number that ignores reclaimable cache.

    Args:
        text: The output of ``free -b``.

    Returns:
        The reading, in bytes.

    Raises:
        ValueError: The output carries no Mem: row, or no available column.
    """
    header: list[str] = []
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if not header and not fields[0].endswith(":"):
            header = [f.lower() for f in fields]
            continue
        if fields[0].rstrip(":").lower() == "mem":
            if "available" not in header:
                raise ValueError("free printed no available column, so it cannot gate a lane")
            total = int(fields[1])
            free = int(fields[header.index("available") + 1])
            return Reading(
                lane="docker",
                total=total,
                committed=total - free,
                free=free,
                detail=f"free -b: {free / 1024**3:.1f}Gi available of {total / 1024**3:.1f}Gi",
            )
    raise ValueError("free printed no Mem: row")


def _ready(node: dict) -> bool:
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in (node.get("status") or {}).get("conditions") or []
    )


def node_headroom(nodes: dict, pods: dict) -> Reading:
    """What a Kubernetes lane can still schedule.

    Allocatable across Ready nodes minus the memory REQUESTS of the pods already
    placed on them - the scheduler's own arithmetic, not current usage, because
    that is what decides whether the next lane's pods fit. Succeeded and Failed
    pods hold nothing and are left out.

    Args:
        nodes: ``kubectl get nodes -o json``.
        pods: ``kubectl get pods -A -o json``.

    Returns:
        The reading, in bytes.

    Raises:
        ValueError: No node is Ready, or a quantity will not parse.
    """
    ready = [n for n in nodes.get("items") or [] if _ready(n)]
    if not ready:
        raise ValueError("no node is Ready, so nothing can be scheduled")
    names = {(n.get("metadata") or {}).get("name") for n in ready}
    allocatable = sum(
        parse_mem(((n.get("status") or {}).get("allocatable") or {}).get("memory", "0"))
        for n in ready
    )

    requested = 0
    for pod in pods.get("items") or []:
        spec = pod.get("spec") or {}
        if spec.get("nodeName") not in names:
            continue
        if ((pod.get("status") or {}).get("phase")) in ("Succeeded", "Failed"):
            continue
        containers = (spec.get("containers") or []) + (spec.get("initContainers") or [])
        requested += sum(
            parse_mem((((c.get("resources") or {}).get("requests")) or {}).get("memory", "0"))
            for c in containers
        )

    free = max(allocatable - requested, 0)
    return Reading(
        lane="k8s",
        total=allocatable,
        committed=requested,
        free=free,
        detail=(
            f"{len(ready)} Ready node(s): {free / 1024**3:.1f}Gi unrequested of "
            f"{allocatable / 1024**3:.1f}Gi allocatable"
        ),
    )


def verdict(reading: Reading, floor: int) -> tuple[bool, str]:
    """Whether a lane may start (or keep running) on this reading.

    Args:
        reading: What the lane's host has left.
        floor: Bytes that must remain free.

    Returns:
        (ok, one line saying so).
    """
    ok = reading.free >= floor
    verb = "clears" if ok else "is UNDER"
    return ok, (
        f"{reading.lane}: {reading.detail} - {verb} the {floor / 1024**3:.1f}Gi floor"
    )
