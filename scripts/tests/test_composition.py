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

# The core data path: every profile stands these up with no further
# configuration, so a fresh deploy ingests, loads, and serves the control plane
# and the console.
CORE_COMPOSITION = frozenset({"dfe-receiver", "dfe-loader", "dfe-engine", "dfe-ui", "hyperdx"})

# The leanest Compose tier runs the core data path alone.
ARCHIVERLESS = "docker-slim"

# Deployed on demand -- one instance per source, written by the engine.
# dfe-transform-vector belongs here rather than in SEEDED_IDLE because it
# declares no `idle_when`, so it has no empty state to be stood up in.
ON_DEMAND = ("dfe-fetcher", "dfe-transform-vector", "culvert")

# Compose declares its services in a committed file and creates none at run time,
# so the apps a source would otherwise deploy start idle instead, one each.
COMPOSE_IDLE: dict[str, tuple[str, ...]] = {"docker-single": ("dfe-fetcher",)}

# Seeded empty wherever a transform runs, rather than arriving with a source.
# Each declares `idle_when` and carries the matching predicate in its own code,
# so it starts, stays Ready and holds no consumer group until a source fills it.
SEEDED_IDLE = ("dfe-transform-vrl", "dfe-transform-elastic")
SEEDED_IDLE_PROFILES = ("single", "scale", "docker-single")


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


def test_the_archiver_is_default_everywhere_but_the_leanest_compose_tier() -> None:
    """Absent from one tier by declaration rather than by omission."""
    for profile in profiles.PROFILE_NAMES:
        deployed = composition.default_apps(profile)
        if profile == ARCHIVERLESS:
            assert "dfe-archiver" not in deployed
            continue
        assert "dfe-archiver" in deployed, profile


def test_the_on_demand_apps_are_seeded_only_where_a_source_cannot_deploy_one() -> None:
    for profile in profiles.PROFILE_NAMES:
        deployed = composition.default_apps(profile)
        idle = COMPOSE_IDLE.get(profile, ())
        for app in ON_DEMAND:
            if app in idle:
                assert app in deployed, f"{app} not in {profile}"
                continue
            assert app not in deployed, f"{app} in {profile}"


def test_the_transforms_are_seeded_idle_rather_than_waiting_for_a_source() -> None:
    """A tier gets one transform of each kind, stood up empty.

    The pairing is what matters: seeding an app that cannot idle would
    crash-loop it, so anything listed here must also declare `idle_when`.
    """
    for app in SEEDED_IDLE:
        assert composition.idle_when(app), f"{app} is seeded but declares no idle_when"
        for profile in SEEDED_IDLE_PROFILES:
            assert app in composition.default_apps(profile), f"{app} not seeded in {profile}"


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
    assert composition.idle_when("dfe-transform-vrl")


def test_an_app_whose_work_arrives_without_a_config_change_never_idles() -> None:
    """The gate wakes on a config change, so a discovery-fed or listening app
    that idled there would stay idle when its work turned up."""
    assert composition.idle_when("dfe-receiver") == ()
    assert composition.idle_when("dfe-loader") == ()


def _snapshot_checkout(tmp_path: Path, text: str) -> Path:
    """A fake dfe-engine checkout whose bundled apps.yaml holds *text*."""
    checkout = tmp_path / "engine-checkout"
    snapshot = checkout / composition.ENGINE_SNAPSHOT_PATH
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(text, encoding="utf-8")
    return checkout


def _set_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> Path:
    """Point `composition.MANIFEST` at a throwaway apps.yaml holding *text*."""
    manifest = tmp_path / "apps.yaml"
    manifest.write_text(text, encoding="utf-8")
    monkeypatch.setattr(composition, "MANIFEST", manifest)
    return manifest


def test_the_engine_snapshot_check_agrees_regardless_of_key_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same declarations, keys in a different order, still agree."""
    _set_manifest(
        monkeypatch,
        tmp_path,
        "apps:\n  dfe-engine:\n    multiplicity: single\n    scale_deployed: true\n",
    )
    checkout = _snapshot_checkout(
        tmp_path,
        "apps:\n  dfe-engine:\n    scale_deployed: true\n    multiplicity: single\n",
    )
    assert composition.check_engine_snapshot(str(checkout)) == 0


def test_the_engine_snapshot_check_fails_and_names_the_differing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real disagreement -- the fetcher's ingest default, once -- fails CI."""
    _set_manifest(
        monkeypatch, tmp_path, "apps:\n  dfe-fetcher:\n    multiplicity: per_config\n"
    )
    checkout = _snapshot_checkout(
        tmp_path, "apps:\n  dfe-fetcher:\n    multiplicity: single\n"
    )
    assert composition.check_engine_snapshot(str(checkout)) == 1
    err = capsys.readouterr().err
    assert "apps.dfe-fetcher.multiplicity" in err
    assert "per_config" in err
    assert "single" in err


def test_the_engine_snapshot_check_ignores_a_comment_only_difference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last real divergence was a comment hunk; a comment alone must pass."""
    _set_manifest(
        monkeypatch,
        tmp_path,
        "# this repo's own re-render command, meaningless inside the engine image\n"
        "apps:\n  dfe-engine:\n    multiplicity: single\n",
    )
    checkout = _snapshot_checkout(tmp_path, "apps:\n  dfe-engine:\n    multiplicity: single\n")
    assert composition.check_engine_snapshot(str(checkout)) == 0


def test_an_unreadable_engine_snapshot_is_exit_2_not_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A network or checkout failure is unverified, never reported as drift."""
    _set_manifest(monkeypatch, tmp_path, "apps:\n  dfe-engine:\n    multiplicity: single\n")
    empty_checkout = tmp_path / "no-such-snapshot-here"
    empty_checkout.mkdir()
    assert composition.check_engine_snapshot(str(empty_checkout)) == 2
    err = capsys.readouterr().err
    assert "UNVERIFIED" in err
