#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         _spread.py
#  Purpose:      Shared plumbing for the ClickHouse and Kafka spread tests: the
#                pod tuple type, the label-selector counter, and the two
#                constraint-shape assertions both tests check identically.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the ClickHouse and Kafka spread tests check identically.

Both `test_clickhouse_replica_spread.py` and `test_kafka_pool_spread.py` render a
chart, build the pods its operator would create, then check that each pod's
hostname-keyed topologySpreadConstraint selects exactly the peers it must be
kept apart from. This module holds the parts that are the same in both: the
pod tuple type, the selector-vs-pods counter, and the two shape assertions
(refuses a skewed placement, allows a skew of one).

Each test keeps its own selector-evaluation and peer-set logic -- ClickHouse's
constraints merge in `matchLabelKeys`, Strimzi's use `matchLabels` alone -- so
that stays local to each file.

The leading underscore keeps pytest from collecting this module as a test file.

    from _spread import Pod, counted, hostname_constraint, assert_constraint_shape
"""

from _expect import expect

# (pod name, labels, spread constraints) for one pod an operator would build.
type Pod = tuple[str, dict[str, str], list[dict]]


def counted(selector: dict[str, str], pods: list[Pod]) -> set[str]:
    """The pods a label selector counts, exactly as the scheduler would."""
    return {n for n, labels, _ in pods if all(labels.get(k) == v for k, v in selector.items())}


def hostname_constraint(label: str, name: str, constraints: list[dict]) -> dict | None:
    """The pod's own hostname-keyed spread constraint, if there is exactly one."""
    hostname = [c for c in constraints if c.get("topologyKey") == "kubernetes.io/hostname"]
    expect(f"{label}: {name} has one hostname spread constraint", len(hostname) == 1,
           f"got {len(hostname)}")
    return hostname[0] if len(hostname) == 1 else None


def assert_constraint_shape(label: str, name: str, constraint: dict) -> None:
    """The two checks every hostname spread constraint must pass."""
    expect(f"{label}: {name} spread refuses a skewed placement",
           constraint.get("whenUnsatisfiable") == "DoNotSchedule",
           f"whenUnsatisfiable is {constraint.get('whenUnsatisfiable')!r}")
    expect(f"{label}: {name} spread allows a skew of one", constraint.get("maxSkew") == 1,
           f"maxSkew is {constraint.get('maxSkew')!r}")
