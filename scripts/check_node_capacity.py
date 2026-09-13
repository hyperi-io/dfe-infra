#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/check_node_capacity.py
#  Purpose:      Refuse an on-prem bootstrap when the cluster's real nodes fall
#                short of the sizing resolver's demand.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""check_node_capacity -- the on-prem sizing gate.

On-prem is the one target the resolver never creates nodes for (see
sizing/targets/onprem.yaml and compute-shapes.yaml's onprem stub): it emits a
DEMAND -- ``sizing/<tier>.nodes.json``, one entry per use case with ``count``,
``cpu``, ``memory_gib`` and ``disk_gb`` -- and something else has to check the
cluster the operator actually built can carry it. This is that something else.

    python3 scripts/check_node_capacity.py --nodes-file sizing/scale.nodes.json
    python3 scripts/check_node_capacity.py --nodes-file sizing/scale.nodes.json \
        --kubeconfig /path/to/kubeconfig

Exit status is 0 when the cluster carries the demand, when the nodes file is
missing (nothing to check -- a clean skip), or when ``--override``
(``DFE_SIZING_OVERRIDE=1``) accepted a shortfall; 1 when the cluster's
allocatable CPU or memory falls short and no override was given.

Disk is reported, never enforced: a node's ``ephemeral-storage`` names the
whole filesystem the kubelet shares with every other pod on it, not a volume
this checker can attribute to one use case, so a shortfall there is for the
operator to read, not for this script to refuse on.

Stdlib only, like every other script here: ``kubectl`` is read from PATH via
subprocess, exactly as resolve_sizing.py reads ``aws``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# A Kubernetes resource quantity: a decimal, then an optional suffix. cpu
# carries `m` (millicores) or nothing (whole cores); memory carries the binary
# (Ki/Mi/Gi/Ti/Pi/Ei) or decimal (k/M/G/T/P/E) SI suffixes, or nothing (bytes).
# https://kubernetes.io/docs/reference/kubernetes-api/common-definitions/quantity/
QUANTITY = re.compile(r"^(?P<number>[0-9.eE+-]+)(?P<suffix>[A-Za-z]*)$")

BINARY_SUFFIXES = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
DECIMAL_SUFFIXES = {"k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18}

BYTES_PER_GIB = 1024**3

# The fields resolve_sizing.py's build_node_requirements writes per use case.
DEMAND_FIELDS = ("count", "cpu", "memory_gib", "disk_gb")


class CapacityError(ValueError):
    """A quantity, a nodes file or a kubectl answer this checker cannot read."""


def parse_quantity(raw: str) -> float:
    """A Kubernetes resource quantity, as a bare number -- cores, or bytes.

    Args:
        raw: The quantity string from a node's `status.allocatable`, e.g.
            "3920m", "16", "65932988Ki".

    Returns:
        The value in whole units: cores for cpu, bytes for memory.

    Raises:
        CapacityError: `raw` is not a quantity this parser understands.
    """
    match = QUANTITY.match(raw.strip())
    if not match:
        raise CapacityError(f"{raw!r} is not a Kubernetes resource quantity")
    try:
        value = float(match["number"])
    except ValueError as err:
        raise CapacityError(f"{raw!r} is not a Kubernetes resource quantity") from err
    suffix = match["suffix"]
    if suffix == "m":
        return value / 1000
    if suffix == "":
        return value
    if suffix in BINARY_SUFFIXES:
        return value * BINARY_SUFFIXES[suffix]
    if suffix in DECIMAL_SUFFIXES:
        return value * DECIMAL_SUFFIXES[suffix]
    raise CapacityError(f"{raw!r} carries an unknown suffix {suffix!r}")


def kubectl_get_nodes(kubeconfig: str | None) -> object:
    """`kubectl get nodes -o json`, from PATH."""
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += ["get", "nodes", "-o", "json"]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    except FileNotFoundError as err:
        raise CapacityError("kubectl is not on PATH") from err
    if done.returncode != 0:
        tail = done.stderr.strip().splitlines()[-1:] or ["no stderr"]
        raise CapacityError(f"kubectl get nodes failed: {tail[0]}")
    return json.loads(done.stdout or "{}")


def cluster_allocatable(nodes_doc: object) -> tuple[float, float]:
    """Total allocatable vCPU and memory (GiB), summed across every node."""
    items = nodes_doc.get("items") if isinstance(nodes_doc, dict) else None
    cpu_total = mem_total = 0.0
    for node in items or []:
        allocatable = ((node or {}).get("status") or {}).get("allocatable") or {}
        cpu_total += parse_quantity(str(allocatable.get("cpu", "0")))
        mem_total += parse_quantity(str(allocatable.get("memory", "0")))
    return cpu_total, mem_total / BYTES_PER_GIB


def read_demand(path: Path) -> dict[str, dict[str, float]]:
    """The node-requirements demand resolve_sizing.py wrote, refusing anything
    that is not the map-keyed-by-use-case shape build_node_requirements emits.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise CapacityError(f"{path}: cannot read -- {err}") from err
    if not isinstance(doc, dict):
        raise CapacityError(f"{path}: expected a map keyed by use case")
    for use_case, body in doc.items():
        if not isinstance(body, dict) or not set(DEMAND_FIELDS) <= set(body):
            raise CapacityError(f"{path}: {use_case} is missing one of {DEMAND_FIELDS}")
    return doc


def total_demand(demand: dict[str, dict[str, float]]) -> tuple[float, float]:
    """Total demanded vCPU and memory (GiB) -- count times per-node, summed."""
    cpu = sum(float(body["count"]) * float(body["cpu"]) for body in demand.values())
    mem = sum(float(body["count"]) * float(body["memory_gib"]) for body in demand.values())
    return cpu, mem


def render_table(
    demand: dict[str, dict[str, float]], cpu_allocatable: float, mem_allocatable: float
) -> str:
    """The demanded-vs-allocatable table -- one line per use case, plus the total."""
    lines = [f"{'use case':<20} {'count':>6} {'cpu':>8} {'memory_gib':>12} {'disk_gb':>10}"]
    cpu_total = mem_total = 0.0
    for use_case, body in sorted(demand.items()):
        count, cpu, mem = float(body["count"]), float(body["cpu"]), float(body["memory_gib"])
        cpu_total += count * cpu
        mem_total += count * mem
        lines.append(f"{use_case:<20} {count:>6.0f} {cpu:>8.1f} {mem:>12.1f} {body['disk_gb']!s:>10}")
    lines.append("-" * 60)
    # Disk is shown per use case above but never summed or enforced here -- see
    # the module docstring.
    lines.append(f"{'TOTAL demanded':<20} {'':>6} {cpu_total:>8.1f} {mem_total:>12.1f}")
    lines.append(f"{'cluster allocatable':<20} {'':>6} {cpu_allocatable:>8.1f} {mem_allocatable:>12.1f}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """The CLI."""
    parser = argparse.ArgumentParser(
        prog="check_node_capacity.py",
        description="Refuse an on-prem bootstrap when the real nodes fall short of the sizing demand.",
    )
    parser.add_argument("--nodes-file", type=Path, required=True, help="sizing/<tier>.nodes.json")
    parser.add_argument("--kubeconfig", default=None, help="passed to kubectl --kubeconfig")
    parser.add_argument(
        "--override",
        action="store_true",
        help="accept an undersized cluster: print the table as a warning and exit 0",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns:
        0 when the nodes file is absent, the cluster carries the demand, or
        --override / DFE_SIZING_OVERRIDE=1 accepted a shortfall; 1 when the
        cluster's allocatable CPU or memory falls short of the demand.
    """
    args = build_parser().parse_args(argv)

    if not args.nodes_file.is_file():
        print(f"check_node_capacity: no {args.nodes_file} -- nothing to check, skipping")
        return 0

    override = args.override or os.environ.get("DFE_SIZING_OVERRIDE") == "1"

    try:
        demand = read_demand(args.nodes_file)
        nodes_doc = kubectl_get_nodes(args.kubeconfig)
        cpu_allocatable, mem_allocatable = cluster_allocatable(nodes_doc)
    except CapacityError as err:
        print(f"check_node_capacity: {err}", file=sys.stderr)
        return 1

    cpu_demand, mem_demand = total_demand(demand)
    table = render_table(demand, cpu_allocatable, mem_allocatable)
    short = cpu_demand > cpu_allocatable or mem_demand > mem_allocatable

    if short and not override:
        print(
            "check_node_capacity: REFUSED -- the cluster cannot carry the sized demand",
            file=sys.stderr,
        )
        print(table, file=sys.stderr)
        return 1
    if short:
        print("check_node_capacity: WARNING -- undersized, but DFE_SIZING_OVERRIDE accepted it")
        print(table)
        return 0
    print("check_node_capacity: the cluster carries the sized demand")
    print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
