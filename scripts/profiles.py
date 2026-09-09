#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         profiles.py
#  Purpose:      The deploy-profile table, declared ONCE. Every tool that has to
#                know what a mode is -- dfe-ops, the matrix harness, the
#                bootstrap smoke tests -- reads it from here instead of carrying
#                its own copy of the mode list, the broker answer, the substrate
#                charts or the capacity floor. Stdlib only.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""profiles -- the four DFE deploy modes, in one place.

A mode is the single lever a deploy turns: DFE_PROFILE selects the cluster
secret's label (which gates the scale-only operator appsets) and its annotation
(which selects argocd/values/profile-<mode>.yaml). Everything else about a tier
follows from that one word, so the word and its consequences are declared here
and nowhere else.

    import profiles

    profiles.MODES                  # ('slim', 'single', 'scale', 'scale-mesh')
    profiles.has_kafka("scale-mesh")  # False
    profiles.substrate("single")    # ('clickhouse-cluster', 'kafka')
    profiles.capacity("scale")      # (6.0, 12884901888, 3)

Shell callers get the same answer from a GENERATED fragment rather than a second
hand-written copy:

    python3 scripts/profiles.py --shell --out bootstrap/scripts/profiles.sh

scripts/tests/test_profiles.py fails when the committed fragment and this table
disagree, so the generated file cannot quietly drift.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Where the generated shell fragment is committed, relative to the repo root.
SHELL_FRAGMENT = Path("bootstrap") / "scripts" / "profiles.sh"


@dataclass(frozen=True, slots=True)
class Profile:
    """One deploy mode and everything that follows from choosing it."""

    # Whether the tier runs a broker. False = the receiver feeds the loader over
    # direct gRPC.
    has_kafka: bool
    # Substrate charts the tier actually deploys -- the offline helm-render
    # pre-flight renders exactly these.
    substrate_charts: tuple[str, ...]
    # Guidance floor (cores, bytes, ready nodes) of allocatable capacity.
    # Operating experience, not a published minimum: a breach WARNs, never FAILs.
    capacity_floor: tuple[float, int, int]
    # The appset profile values file the cluster annotation selects.
    argocd_values: str
    # The values file that shapes the same tier under the dfe-stack umbrella.
    umbrella_profile: str
    description: str


# slim is the umbrella chart's DEFAULT values, so its umbrella_profile is
# values.yaml itself -- an overlay restating the defaults would be a second copy.
PROFILES: dict[str, Profile] = {
    "slim": Profile(
        has_kafka=False,
        substrate_charts=("clickhouse-cluster",),
        capacity_floor=(2.0, 4 * 1024**3, 1),
        argocd_values="argocd/values/profile-slim.yaml",
        umbrella_profile="helm/dfe-stack/values.yaml",
        description="one node of everything, gRPC transport, no broker",
    ),
    "single": Profile(
        has_kafka=True,
        substrate_charts=("clickhouse-cluster", "kafka"),
        capacity_floor=(4.0, 8 * 1024**3, 1),
        argocd_values="argocd/values/profile-single.yaml",
        umbrella_profile="helm/dfe-stack/profiles/single.yaml",
        description="one node of everything WITH a non-operator broker",
    ),
    "scale": Profile(
        has_kafka=True,
        substrate_charts=("clickhouse-cluster", "kafka"),
        capacity_floor=(6.0, 12 * 1024**3, 3),
        argocd_values="argocd/values/profile-scale.yaml",
        umbrella_profile="helm/dfe-stack/profiles/scale.yaml",
        description="HA replicas, operator ClickHouse cluster and Strimzi Kafka",
    ),
    "scale-mesh": Profile(
        has_kafka=False,
        # Same HA sizing as scale: dropping the broker does not shrink the apps.
        substrate_charts=("clickhouse-cluster",),
        capacity_floor=(6.0, 12 * 1024**3, 3),
        argocd_values="argocd/values/profile-scale-mesh.yaml",
        umbrella_profile="helm/dfe-stack/profiles/scale-mesh.yaml",
        description="scale's HA sizing and ClickHouse cluster, gRPC transport, no broker",
    ),
}

MODES: tuple[str, ...] = tuple(PROFILES)


def has_kafka(mode: str) -> bool:
    """Whether the mode runs a broker; an unknown mode answers no.

    Args:
        mode: A deploy mode name.

    Returns:
        True when the tier deploys a broker, False for the gRPC-direct tiers and
        for any name that is not a mode.
    """
    profile = PROFILES.get(mode)
    return bool(profile and profile.has_kafka)


def substrate(mode: str) -> tuple[str, ...]:
    """The substrate charts the mode deploys.

    Args:
        mode: A deploy mode name.

    Returns:
        Chart directory names under helm/charts.

    Raises:
        KeyError: The mode is not one of MODES.
    """
    return PROFILES[mode].substrate_charts


def capacity(mode: str) -> tuple[float, int, int]:
    """The mode's guidance capacity floor.

    Args:
        mode: A deploy mode name.

    Returns:
        (cores, bytes of memory, ready nodes) across the cluster.

    Raises:
        KeyError: The mode is not one of MODES.
    """
    return PROFILES[mode].capacity_floor


def emit_shell() -> str:
    """The POSIX-sh fragment the bootstrap smoke tests source.

    Returns:
        The whole file body, newline-terminated, ready to write to
        bootstrap/scripts/profiles.sh.
    """
    brokered = "|".join(m for m in MODES if PROFILES[m].has_kafka)
    return "\n".join(
        (
            "#  Project:      dfe-infra",
            "#  File:         profiles.sh",
            "#  Purpose:      The deploy-profile facts the shell smoke tests need.",
            "#  Language:     POSIX sh",
            "#",
            "#  License:      BUSL-1.1",
            "#  Copyright:    (c) 2026 HYPERI PTY LIMITED",
            "#",
            "# GENERATED from scripts/profiles.py -- do not edit. Regenerate with:",
            f"#     python3 scripts/profiles.py --shell --out {SHELL_FRAGMENT.as_posix()}",
            "",
            "# Sourced, never executed, so it carries a shell directive not a shebang.",
            "# shellcheck shell=sh",
            "",
            "# Whether the deploy profile in PROFILE runs a broker. An unset or unknown",
            "# profile answers no, so a broker check reports SKIP rather than a false FAIL.",
            "profile_has_kafka() {",
            '    case "${PROFILE:-}" in',
            f"        {brokered}) return 0 ;;",
            "        *) return 1 ;;",
            "    esac",
            "}",
            "",
        )
    )


def _table() -> str:
    """One line per mode -- the table this module exists to declare, printed."""
    rows = ["mode        broker  substrate charts                floor (cpu/mem/nodes)"]
    for mode in MODES:
        p = PROFILES[mode]
        cpu, mem, nodes = p.capacity_floor
        rows.append(
            f"{mode:<11} {'yes' if p.has_kafka else 'no':<7} "
            f"{','.join(p.substrate_charts):<31} "
            f"{cpu:g}/{mem // 1024**3}Gi/{nodes}"
        )
        rows.append(f"{'':<12}{p.description}")
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments, defaulting to sys.argv[1:].

    Returns:
        Process exit status.
    """
    parser = argparse.ArgumentParser(
        prog="profiles.py",
        description="The DFE deploy-profile table, and the shell fragment generated from it.",
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="emit the POSIX-sh profile_has_kafka fragment instead of the table",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help=f"write the fragment to PATH instead of stdout (implies --shell; canonical target {SHELL_FRAGMENT.as_posix()})",
    )
    args = parser.parse_args(argv)

    if args.out:
        Path(args.out).write_text(emit_shell(), encoding="utf-8", newline="\n")
        print(f"wrote {args.out}", file=sys.stderr)
        return 0
    sys.stdout.write(emit_shell() if args.shell else _table())
    return 0


if __name__ == "__main__":
    sys.exit(main())
