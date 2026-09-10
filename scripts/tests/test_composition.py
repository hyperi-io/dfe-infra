#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_composition.py
#  Purpose:      Guard the one default-composition declaration: apps.yaml names
#                only real profiles, an app is never default where it is not
#                offered, and the seeded app set committed in each profile's
#                values file matches what the manifest derives.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/composition.py and apps.yaml's composition axes.

The seeded app set in each argocd/values/profile-<mode>.yaml is generated, so
nothing stops a hand edit or a stale commit except the comparison here.

    python3 -m pytest scripts/tests/test_composition.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import composition  # noqa: E402
import profiles  # noqa: E402

# The core data path Derek settled on: every profile stands these up with no
# further configuration, so a fresh deploy ingests, loads, archives, and serves
# the control plane and the console.
CORE_COMPOSITION = frozenset(
    {"dfe-receiver", "dfe-loader", "dfe-archiver", "dfe-engine", "dfe-ui", "hyperdx"}
)

# Deployed on demand -- one instance per source, written by the engine.
ON_DEMAND = ("dfe-fetcher", "dfe-transform-vrl", "dfe-transform-vector", "culvert")


def test_the_manifest_names_only_declared_profiles() -> None:
    """A typo'd or retired profile name in apps.yaml is dead composition."""
    apps = composition._apps()
    for name, raw in apps.items():
        app = raw or {}
        for key in ("profiles", "default_in"):
            for value in app.get(key) or []:
                assert value in profiles.PROFILE_NAMES, f"{name}.{key}: {value}"


def test_an_app_is_never_default_where_it_is_not_offered() -> None:
    """`default_in` outside `profiles` deploys nothing and reports nothing."""
    apps = composition._apps()
    for name, raw in apps.items():
        app = raw or {}
        offered = app.get("profiles")
        default = app.get("default_in")
        if offered is None or default is None:
            continue
        assert set(default) <= set(offered), name


def test_every_profile_stands_up_the_core_composition() -> None:
    for profile in profiles.PROFILE_NAMES:
        assert CORE_COMPOSITION <= set(composition.default_apps(profile)), profile


def test_the_archiver_is_default_on_slim() -> None:
    """The tier that used to omit it, with nothing recording why."""
    assert "dfe-archiver" in composition.default_apps("slim")
    assert "dfe-archiver" in composition.default_apps("docker-slim")


def test_the_on_demand_apps_are_seeded_nowhere() -> None:
    for profile in profiles.PROFILE_NAMES:
        deployed = composition.default_apps(profile)
        for app in ON_DEMAND:
            assert app not in deployed, f"{app} in {profile}"


def test_culvert_is_offered_only_on_the_ha_tiers() -> None:
    assert "culvert" in composition.offered_in("scale")
    assert "culvert" in composition.offered_in("mesh")
    assert "culvert" not in composition.offered_in("slim")


def test_the_committed_seed_blocks_match_the_manifest() -> None:
    """A hand edit or a stale commit of the generated block fails here."""
    assert composition.write_seed(check_only=True) == 0


def test_the_seeded_set_is_exactly_the_manifests_default() -> None:
    """Read back what the chart will see, not what the generator produced."""
    from ruamel.yaml import YAML

    for mode in profiles.MODES:
        path = REPO_ROOT / profiles.PROFILES[mode].argocd_values
        values = YAML(typ="safe").load(path.read_text(encoding="utf-8", errors="replace"))
        seeded = values["deployRepo"]["seedApps"]
        assert tuple(seeded) == composition.default_apps(mode), mode


def test_the_engine_chart_carries_the_current_manifest() -> None:
    """A stale chart copy deploys the apps of whichever commit last rendered it."""
    assert composition.write_catalogue(check_only=True) == 0


def test_the_chart_copy_is_the_manifest_byte_for_byte() -> None:
    """Re-serialising it could change a value; only the banner is added."""
    copy = composition.CHART_MANIFEST.read_text(encoding="utf-8")
    assert copy.startswith(composition.CATALOGUE_BANNER)
    assert copy[len(composition.CATALOGUE_BANNER) :] == (
        composition.MANIFEST.read_text(encoding="utf-8")
    )


def test_the_chart_default_carries_no_second_copy_of_the_composition() -> None:
    """The profile layer is the only place the seeded set is stated."""
    from ruamel.yaml import YAML

    chart = REPO_ROOT / "helm" / "charts" / "forgejo" / "values.yaml"
    values = YAML(typ="safe").load(chart.read_text(encoding="utf-8", errors="replace"))
    assert values["deployRepo"]["seedApps"] == []


def test_an_unknown_profile_is_refused_rather_than_answered_empty() -> None:
    with pytest.raises(composition.CompositionError):
        composition.default_apps("not-a-profile")


def test_the_idling_apps_declare_what_empty_means() -> None:
    """An app the deploy stands up before it has work says which keys say so."""
    assert composition.idle_when("dfe-archiver")
    assert composition.idle_when("dfe-fetcher")


def test_an_app_whose_work_arrives_without_a_config_change_never_idles() -> None:
    """The gate wakes on a config change, so a discovery-fed or listening app
    that idled there would stay idle when its work turned up."""
    assert composition.idle_when("dfe-receiver") == ()
    assert composition.idle_when("dfe-loader") == ()
