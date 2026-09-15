#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_profiles.py
#  Purpose:      Guard the one deploy-profile table: the generated shell
#                fragment matches the Python declaration byte for byte, every
#                mode's profile files exist, and the tools that used to keep
#                their own copy of the table now read this one.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/profiles.py -- the mode table and its generated fragment.

The fragment in bootstrap/scripts/profiles.sh is generated, so nothing stops a
hand edit or a stale commit except this comparison. Everything else here asserts
that a mode cannot be declared without the files and the answers that make it
deployable.

    python3 -m pytest scripts/tests/test_profiles.py -q
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
FRAGMENT = REPO_ROOT / "bootstrap" / "scripts" / "profiles.sh"

sys.path.insert(0, str(SCRIPTS))
import profiles  # noqa: E402
import tester_idp  # noqa: E402

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_profiles", str(SCRIPTS / "dfe-ops"))
_spec = importlib.util.spec_from_loader("dfeops_profiles", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_profiles"] = dfeops
_loader.exec_module(dfeops)

BROKERLESS = ("slim", "mesh")


def test_the_committed_shell_fragment_matches_the_table() -> None:
    """A hand edit or a stale commit of the generated file fails here."""
    assert FRAGMENT.read_text(encoding="utf-8", errors="replace") == profiles.emit_shell()


def test_the_fragment_answers_for_every_mode() -> None:
    """Sourcing it in a real shell gives the same answer as has_kafka()."""
    for mode in profiles.MODES:
        script = f'. "{FRAGMENT}"; PROFILE={mode}; profile_has_kafka'
        done = subprocess.run(["bash", "-c", script], check=False)
        assert (done.returncode == 0) is profiles.has_kafka(mode), mode


def test_the_fragment_answers_no_for_an_unset_profile() -> None:
    """An unset profile must not claim a broker, and must not error under set -u."""
    done = subprocess.run(
        ["bash", "-c", f'set -u; . "{FRAGMENT}"; profile_has_kafka'], check=False
    )
    assert done.returncode == 1


def test_every_kubernetes_mode_has_both_profile_files() -> None:
    for mode in profiles.MODES:
        profile = profiles.PROFILES[mode]
        assert (REPO_ROOT / profile.argocd_values).is_file(), mode
        assert (REPO_ROOT / profile.umbrella_profile).is_file(), mode


def test_every_compose_profile_mirrors_a_kubernetes_one() -> None:
    """A compose profile is a projection, so it has a tier to project from."""
    for mode in profiles.COMPOSE_MODES:
        profile = profiles.PROFILES[mode]
        assert profile.mirrors in profiles.MODES, mode
        assert profile.compose_profile, mode
        assert profile.has_kafka == profiles.has_kafka(profile.mirrors), mode


def test_the_two_platforms_partition_the_profile_names() -> None:
    assert profiles.MODES + profiles.COMPOSE_MODES == profiles.PROFILE_NAMES
    assert not set(profiles.MODES) & set(profiles.COMPOSE_MODES)


def test_the_brokerless_modes_deploy_no_kafka_substrate() -> None:
    for mode in BROKERLESS:
        assert not profiles.has_kafka(mode)
        assert "kafka" not in profiles.substrate(mode)
    for mode in ("single", "scale"):
        assert profiles.has_kafka(mode)
        assert "kafka" in profiles.substrate(mode)


def test_an_unknown_mode_is_not_credited_with_a_broker() -> None:
    """A typo or a retired name answers no, so the broker checks SKIP not FAIL."""
    assert not profiles.has_kafka("")
    assert not profiles.has_kafka("not-a-mode")


def test_mesh_is_sized_like_scale() -> None:
    """Dropping the broker does not shrink the app tier."""
    assert profiles.capacity("mesh") == profiles.capacity("scale")


def test_dfe_ops_reads_the_one_table() -> None:
    """A compose profile is not a cluster deploy mode, so `--mode` refuses it."""
    assert dfeops.MODES == profiles.MODES
    for mode in profiles.COMPOSE_MODES:
        assert mode not in dfeops.MODES


def test_the_tester_idp_registers_a_callback_for_every_mode() -> None:
    assert tester_idp.DEFAULT_PROFILES == profiles.MODES
