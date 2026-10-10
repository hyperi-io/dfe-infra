#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_upgrade_overlay_migration.py
#  Purpose:      Guard the dfe-ops upgrade overlay-vocabulary stage: every value-map
#                entry shape and migration, set against empty values, no overwrite,
#                idempotence, and where the stage sits in apply and plan.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for the overlay-vocabulary stage of scripts/dfe_ops_upgrade.py.

    python3 -m pytest scripts/tests/test_upgrade_overlay_migration.py -q

The entry semantics run against a small value map written here, so each shape
is held whatever the real map later says. The real map's derivations (oidc, the
fullname of a per-config instance) run against scripts/weave/value-map.yaml.
apply and plan run against a real git deploy repo with the cluster-facing calls
stubbed. test_weave_overlay_migration.py renders the migrated overlays.
"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import dfe_ops_upgrade as u  # noqa: E402

MAP_YAML = """
apps:
  svc:
    chart: charts/svc
    keys:
      project:
        to: project
      component:
        to: fullnameOverride
        migrate: fullname
      moved.key:
        to: new.key
        note: The new home.
      listed:
        to: [listed, other.a, other.b]
      same:
        to: same
      gone:
        dropped: Nothing reads it.
      secretName:
        to: secretName
        when-off: {secrets.x.enabled: false}
      optional:
        to: optional
        when-off: by-hand
        note: Empty no longer drops it.
      hand.enabled:
        to: initContainers
        migrate: by-hand
        note: Off is a list without it.
      trigger.enabled:
        to: extraTriggers
        migrate: by-hand
        when-off: {extraTriggers: []}
        note: False is an empty extraTriggers.
      mode:
        to: [mode, publicService.enabled]
        migrate: public
      protocol:
        to: [tls.source, tls.sink]
        migrate: tls
      accessMode:
        to: wp.accessModes
        migrate: list
      sec.profileType:
        to: sec.profile.type
        migrate: move
        default: RuntimeDefault
"""

CHART_VALUES = "project: dfe\ncomponent: svc\n"
DEPLOY_BLOCK = "deploy:\n  service: svc\n  instance: x\n"
OVERLAY = "values/svc-x-values.yaml"


@pytest.fixture
def value_map(tmp_path: Path) -> Path:
    path = tmp_path / "value-map.yaml"
    path.write_text(MAP_YAML, encoding="utf-8")
    chart = tmp_path / "charts" / "svc"
    chart.mkdir(parents=True)
    (chart / "values.yaml").write_text(CHART_VALUES, encoding="utf-8")
    return path


def _deploy(tmp_path: Path, body: str, common: str | None = None) -> Path:
    deploy = tmp_path / "deploy"
    (deploy / "values").mkdir(parents=True)
    (deploy / OVERLAY).write_text(DEPLOY_BLOCK + body, encoding="utf-8")
    if common is not None:
        (deploy / "infra").mkdir()
        (deploy / "infra" / "common.yaml").write_text(common, encoding="utf-8")
    return deploy


def _migrate(
    tmp_path: Path, value_map: Path, body: str, common: str | None = None, *, write: bool = True
) -> tuple[u.OverlayPlan, dict, str]:
    """The overlay's plan, its document afterwards, and its text afterwards."""
    deploy = _deploy(tmp_path, body, common)
    result = u.migrate_overlays(deploy, write=write, value_map=value_map, root=tmp_path)
    text = (deploy / OVERLAY).read_text(encoding="utf-8")
    return result.plans[0], yaml.safe_load(text), text


def _written(plan: u.OverlayPlan) -> dict[str, object]:
    return {target: u._plain(value) for target, value, _why in plan.writes}


def _project_line(project: str) -> str:
    """The by-hand line for a project the dfe-extras guard refuses."""
    return (
        f'project is "{project}", but the thin charts accept only "dfe" (the dfe-extras guard). '
        f"Unsetting it renames every object from {project}-* to dfe-* and prunes the old ones, "
        "PVCs and fetcher cursors included, so it is a planned migration, not a hand edit."
    )


# ------------------------------------------------------------------- the value map


def test_the_real_value_map_loads_and_covers_every_gated_component() -> None:
    maps = u.load_value_map()
    gated = {
        "dfe-ui", "hyperdx", "dfe-receiver", "dfe-loader", "dfe-archiver", "dfe-transform-vrl",
        "dfe-transform-vector", "dfe-transform-elastic", "dfe-fetcher", "culvert", "dfe-engine",
    }
    assert gated <= set(maps)
    fullnames = {s for s, app in maps.items() if any(e.migrate == "fullname" for e in app.entries)}
    assert fullnames == gated


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"to": "a", "migrate": "rename"}, "migrate is one of"),
        ({"dropped": "x", "migrate": "list"}, "there is no `to`"),
        ({"to": "a", "when-off": "later"}, "when-off is by-hand or a mapping"),
        ({"to": "a", "when-off": [1]}, "when-off is by-hand or a mapping"),
        ({"to": "a", "migrate": "move"}, "needs the default 2.2.0 renders without it"),
    ],
)
def test_a_malformed_migration_field_is_refused(tmp_path: Path, entry: dict, message: str) -> None:
    path = tmp_path / "map.yaml"
    path.write_text(yaml.safe_dump({"apps": {"x": {"chart": "c", "keys": {"k": entry}}}}), encoding="utf-8")
    with pytest.raises(u.UpgradeError, match=message):
        u.load_value_map(path)


# ------------------------------------------------------------------ the to: shapes


def test_a_single_to_copies_the_value_and_keeps_the_key(tmp_path: Path, value_map: Path) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, "moved:\n  key: {a: 1}\n")
    assert _written(plan) == {"new.key": {"a": 1}}
    assert doc["moved"] == {"key": {"a": 1}}
    assert doc["new"] == {"key": {"a": 1}}


def test_a_to_list_writes_every_path_but_the_key_itself(tmp_path: Path, value_map: Path) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, "listed: v\n")
    assert _written(plan) == {"other.a": "v", "other.b": "v"}
    assert doc["listed"] == "v"


def test_a_key_mapped_to_itself_writes_nothing(tmp_path: Path, value_map: Path) -> None:
    plan, _, text = _migrate(tmp_path, value_map, "same: v\n")
    assert plan.writes == []
    assert text == DEPLOY_BLOCK + "same: v\n"


def test_a_dropped_key_is_reported_and_left_in_place(tmp_path: Path, value_map: Path) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, "gone: 3\n")
    assert plan.writes == []
    assert plan.dropped == ["gone: Nothing reads it."]
    assert doc["gone"] == 3


# -------------------------------------------------------------- set against empty


@pytest.mark.parametrize("empty", ['""', "null", "~", ""])
def test_an_empty_or_null_value_is_not_moved(tmp_path: Path, value_map: Path, empty: str) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, f"moved:\n  key: {empty}\n")
    assert plan.writes == []
    assert "new" not in doc


@pytest.mark.parametrize(("raw", "value"), [("false", False), ("0", 0), ("[]", []), ("{}", {})])
def test_a_false_zero_or_empty_collection_is_a_set_value(
    tmp_path: Path, value_map: Path, raw: str, value: object
) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, f"moved:\n  key: {raw}\n")
    assert _written(plan) == {"new.key": value}
    assert doc["new"]["key"] == value


# ----------------------------------------------------------------------- when-off


@pytest.mark.parametrize("off", ['""', "null", "false"])
def test_an_off_value_writes_what_the_note_says(tmp_path: Path, value_map: Path, off: str) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, f"secretName: {off}\n")
    assert _written(plan) == {"secrets.x.enabled": False}
    assert doc["secrets"] == {"x": {"enabled": False}}
    assert "secretName" in doc


def test_a_named_value_writes_no_off_keys(tmp_path: Path, value_map: Path) -> None:
    plan, _, _ = _migrate(tmp_path, value_map, "secretName: my-secret\n")
    assert plan.writes == []


def test_an_off_value_with_no_write_is_left_for_a_hand_edit(tmp_path: Path, value_map: Path) -> None:
    plan, _, _ = _migrate(tmp_path, value_map, 'optional: ""\n')
    assert plan.writes == []
    assert plan.by_hand == ['optional is "": Empty no longer drops it.']


def test_when_off_beats_by_hand_for_a_false_toggle(tmp_path: Path, value_map: Path) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, "trigger:\n  enabled: false\n")
    assert _written(plan) == {"extraTriggers": []}
    assert plan.by_hand == []
    assert doc["extraTriggers"] == []


# ------------------------------------------------------------------ the migrations


def test_by_hand_writes_nothing_and_prints_the_note(tmp_path: Path, value_map: Path) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, "hand:\n  enabled: false\n")
    assert plan.writes == []
    assert plan.by_hand == ["hand.enabled -> initContainers: Off is a list without it."]
    assert "initContainers" not in doc


@pytest.mark.parametrize(("mode", "enabled"), [("public", True), ("internal", False), ("vpn", False)])
def test_public_derives_whether_the_load_balancer_renders(
    tmp_path: Path, value_map: Path, mode: str, enabled: bool
) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, f"mode: {mode}\n")
    assert _written(plan) == {"publicService.enabled": enabled}
    assert doc["mode"] == mode


@pytest.mark.parametrize(
    ("protocol", "written"),
    [("SASL_SSL", {"tls.source": True, "tls.sink": True}), ("ssl", {"tls.source": True, "tls.sink": True}),
     ("SASL_PLAINTEXT", {}), ("PLAINTEXT", {})],
)
def test_tls_turns_on_for_a_protocol_carrying_ssl_and_is_never_written_false(
    tmp_path: Path, value_map: Path, protocol: str, written: dict
) -> None:
    plan, _, _ = _migrate(tmp_path, value_map, f"protocol: {protocol}\n")
    assert _written(plan) == written


def test_list_wraps_the_one_value(tmp_path: Path, value_map: Path) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, "accessMode: ReadWriteOnce\n")
    assert _written(plan) == {"wp.accessModes": ["ReadWriteOnce"]}
    assert doc["accessMode"] == "ReadWriteOnce"


def test_move_writes_the_new_key_and_takes_the_old_one_out(tmp_path: Path, value_map: Path) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, "sec:\n  runAs: 1\n  profileType: RuntimeDefault\n")
    assert _written(plan) == {"sec.profile.type": "RuntimeDefault"}
    assert plan.removes == [("sec.profileType", "moved to sec.profile.type")]
    assert plan.by_hand == []
    assert doc["sec"] == {"runAs": 1, "profile": {"type": "RuntimeDefault"}}


def test_move_of_a_value_2_2_0_would_not_render_on_rollback_asks_for_a_hand_edit(
    tmp_path: Path, value_map: Path
) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, "sec:\n  profileType: Unconfined\n")
    assert _written(plan) == {"sec.profile.type": "Unconfined"}
    assert doc["sec"] == {"profile": {"type": "Unconfined"}}
    assert plan.by_hand == [
        'sec.profileType "Unconfined" -> sec.profile.type: a rollback to 2.2.0 renders '
        '"RuntimeDefault" in its place, so restore it there by hand'
    ]


@pytest.mark.parametrize("empty", ['""', "null"])
def test_move_takes_an_empty_old_key_out_and_writes_nothing(
    tmp_path: Path, value_map: Path, empty: str
) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, f"sec:\n  profileType: {empty}\n")
    assert plan.writes == []
    assert plan.removes == [("sec.profileType", "moved to sec.profile.type")]
    assert doc["sec"] == {}


def test_move_keeps_a_new_key_already_set_and_still_takes_the_old_one_out(
    tmp_path: Path, value_map: Path
) -> None:
    body = "sec:\n  profileType: RuntimeDefault\n  profile:\n    type: Localhost\n"
    plan, doc, _ = _migrate(tmp_path, value_map, body)
    assert plan.writes == []
    assert doc["sec"] == {"profile": {"type": "Localhost"}}
    assert plan.conflicts == [
        'sec.profile.type holds "Localhost", kept over "RuntimeDefault" from sec.profileType'
    ]


def test_a_second_move_run_changes_nothing(tmp_path: Path, value_map: Path) -> None:
    deploy = _deploy(tmp_path, "sec:\n  profileType: RuntimeDefault\n")
    first = u.migrate_overlays(deploy, write=True, value_map=value_map, root=tmp_path)
    after = (deploy / OVERLAY).read_bytes()
    second = u.migrate_overlays(deploy, write=True, value_map=value_map, root=tmp_path)
    assert (first.changed, second.changed) == ([OVERLAY], [])
    assert (deploy / OVERLAY).read_bytes() == after


# ---------------------------------------------------------------------- fullname


@pytest.mark.parametrize(
    ("body", "common", "name"),
    [
        ("component: svc-acme\n", None, "dfe-svc-acme"),
        ("component: svc-acme\nproject: acme\n", None, "acme-svc-acme"),
        ("component: svc-acme\n", "project: corp\n", "corp-svc-acme"),
        ("project: acme\n", None, "acme-svc"),
        ("", "project: corp\n", "corp-svc"),
    ],
    ids=["component", "overlay-project", "common-project", "project-alone", "common-alone"],
)
def test_fullname_is_the_2_2_0_name(
    tmp_path: Path, value_map: Path, body: str, common: str | None, name: str
) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, body, common)
    assert doc["fullnameOverride"] == name
    assert _written(plan) == {"fullnameOverride": name}


def test_no_fullname_where_the_thin_chart_renders_that_name_anyway(
    tmp_path: Path, value_map: Path
) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, "project: dfe\n", "project: dfe\n")
    assert plan.writes == []
    assert "fullnameOverride" not in doc


def test_fullname_truncates_as_dfe_common_does(tmp_path: Path, value_map: Path) -> None:
    component = "c" * 58 + "-x"
    plan, _, _ = _migrate(tmp_path, value_map, f"component: {component}\n")
    # trunc 63 cuts "dfe-" + 58 c's + "-" there, and trimSuffix "-" drops the dash.
    assert _written(plan) == {"fullnameOverride": "dfe-" + "c" * 58}


def test_fullname_without_the_2_2_0_chart_falls_back_to_the_dfe_project(tmp_path: Path) -> None:
    path = tmp_path / "value-map.yaml"
    path.write_text(MAP_YAML.replace("charts/svc", "charts/gone"), encoding="utf-8")
    plan, _, _ = _migrate(tmp_path, path, "component: svc-acme\n")
    assert _written(plan) == {"fullnameOverride": "dfe-svc-acme"}


def test_a_renamed_project_with_no_chart_to_name_the_component_is_left_for_a_hand_edit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "value-map.yaml"
    path.write_text(MAP_YAML.replace("charts/svc", "charts/gone"), encoding="utf-8")
    plan, doc, _ = _migrate(tmp_path, path, "project: acme\n")
    assert plan.writes == []
    assert "fullnameOverride" not in doc
    assert plan.by_hand == [
        _project_line("acme"),
        "fullnameOverride: acme names the objects, and the 2.2.0 chart that held the component "
        "default is gone -- set <project>-<component> by hand",
    ]


@pytest.mark.parametrize(
    ("body", "common"),
    [
        ("component: svc-acme\nproject: corp\n", None),
        ("component: svc-acme\n", "project: corp\n"),
        ("project: corp\n", None),
        ("", "project: corp\n"),
    ],
    ids=["overlay-project", "common-project", "project-alone", "common-alone"],
)
def test_a_project_other_than_dfe_is_printed_as_a_planned_migration(
    tmp_path: Path, value_map: Path, body: str, common: str | None
) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, body, common)
    assert plan.by_hand == [_project_line("corp")]
    assert doc["fullnameOverride"].startswith("corp-")
    report = u.render_overlay_report(u.OverlayMigration(plans=[plan]))
    assert f"  by hand  {_project_line('corp')}" in report


@pytest.mark.parametrize(
    ("body", "common"),
    [
        ("component: svc-acme\nproject: dfe\n", None),
        ("component: svc-acme\n", "project: dfe\n"),
        ("component: svc-acme\n", None),
        ("project: dfe\n", "project: corp\n"),
    ],
    ids=["overlay-dfe", "common-dfe", "unset", "overlay-beats-common"],
)
def test_the_project_dfe_prints_nothing_by_hand(
    tmp_path: Path, value_map: Path, body: str, common: str | None
) -> None:
    plan, _, _ = _migrate(tmp_path, value_map, body, common)
    assert plan.by_hand == []


# ------------------------------------------------------------ no overwrite, no churn


def test_a_target_already_holding_another_value_is_kept(tmp_path: Path, value_map: Path) -> None:
    body = "component: svc-acme\nfullnameOverride: svc-chosen\n"
    plan, doc, _ = _migrate(tmp_path, value_map, body)
    assert plan.writes == []
    assert doc["fullnameOverride"] == "svc-chosen"
    assert plan.conflicts == [
        'fullnameOverride holds "svc-chosen", kept over "dfe-svc-acme" from component, as '
        "<project>-<component>"
    ]


def test_a_target_already_holding_the_same_value_is_not_a_conflict(
    tmp_path: Path, value_map: Path
) -> None:
    plan, _, _ = _migrate(tmp_path, value_map, "listed: v\nother:\n  a: v\n")
    assert _written(plan) == {"other.b": "v"}
    assert plan.conflicts == []


def test_a_target_beneath_a_scalar_is_a_conflict_and_written_nowhere(
    tmp_path: Path, value_map: Path
) -> None:
    plan, doc, _ = _migrate(tmp_path, value_map, "listed: v\nother: plain\n")
    assert plan.writes == []
    assert doc["other"] == "plain"
    assert plan.conflicts[0].startswith('other.a: other holds "plain", not a map')


def test_a_second_run_writes_nothing(tmp_path: Path, value_map: Path) -> None:
    body = "component: svc-acme\nmoved:\n  key: [1, 2]\nsecretName: ''\nmode: public\n"
    deploy = _deploy(tmp_path, body)
    first = u.migrate_overlays(deploy, write=True, value_map=value_map, root=tmp_path)
    after_first = (deploy / OVERLAY).read_bytes()
    second = u.migrate_overlays(deploy, write=True, value_map=value_map, root=tmp_path)
    assert first.changed == [OVERLAY]
    assert second.changed == []
    assert second.plans[0].conflicts == []
    assert (deploy / OVERLAY).read_bytes() == after_first


def test_the_rewrite_keeps_every_line_it_did_not_add(tmp_path: Path, value_map: Path) -> None:
    body = (
        "# placement for the acme pool\n"
        "moved:\n"
        "  key: 'quoted'   # stays quoted\n"
        "same: \"double\"\n"
    )
    _, _, text = _migrate(tmp_path, value_map, body)
    assert text.startswith(DEPLOY_BLOCK + body)
    assert text[len(DEPLOY_BLOCK + body) :] == "new:\n  key: 'quoted'\n"


def test_a_read_only_run_writes_nothing(tmp_path: Path, value_map: Path) -> None:
    plan, _, text = _migrate(tmp_path, value_map, "listed: v\n", write=False)
    assert _written(plan) == {"other.a": "v", "other.b": "v"}
    assert text == DEPLOY_BLOCK + "listed: v\n"


# --------------------------------------------------------------- what is skipped


def test_an_overlay_the_migration_cannot_read_stops_it(tmp_path: Path, value_map: Path) -> None:
    deploy = _deploy(tmp_path, "listed: [unclosed\n")
    with pytest.raises(u.UpgradeError, match=r"values/svc-x-values\.yaml is not YAML"):
        u.migrate_overlays(deploy, write=True, value_map=value_map, root=tmp_path)


def test_an_overlay_that_is_not_a_mapping_stops_it(tmp_path: Path, value_map: Path) -> None:
    deploy = _deploy(tmp_path, "")
    (deploy / OVERLAY).write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(u.UpgradeError, match="is not a YAML mapping"):
        u.migrate_overlays(deploy, write=True, value_map=value_map, root=tmp_path)


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("", "empty"),
        ("listed: v\n", "carries no deploy.service"),
        ("deploy:\n  service: other\nlisted: v\n", "holds no other"),
    ],
)
def test_an_overlay_no_entry_reaches_is_left_as_it_is(
    tmp_path: Path, value_map: Path, text: str, why: str
) -> None:
    deploy = _deploy(tmp_path, "")
    (deploy / OVERLAY).write_text(text, encoding="utf-8")
    result = u.migrate_overlays(deploy, write=True, value_map=value_map, root=tmp_path)
    assert why in result.plans[0].skipped
    assert (deploy / OVERLAY).read_text(encoding="utf-8") == text


def test_infra_common_is_reported_and_never_written(tmp_path: Path, value_map: Path) -> None:
    common = "moved:\n  key: zone-a\nhand:\n  enabled: false\n"
    deploy = _deploy(tmp_path, "same: v\n", common)
    result = u.migrate_overlays(deploy, write=True, value_map=value_map, root=tmp_path)
    assert result.common == [
        'moved.key -> new.key = "zone-a" (svc)',
        "hand.enabled -> initContainers: Off is a list without it. (svc)",
    ]
    assert (deploy / "infra" / "common.yaml").read_text(encoding="utf-8") == common
    report = "\n".join(u.render_overlay_report(result))
    assert "infra/common.yaml sets keys the thin charts read elsewhere" in report


def test_the_report_prints_writes_conflicts_hand_edits_drops_and_notes(
    tmp_path: Path, value_map: Path
) -> None:
    body = "moved:\n  key: 1\nhand:\n  enabled: false\ngone: 1\ncomponent: svc-a\nfullnameOverride: x\n"
    deploy = _deploy(tmp_path, body)
    result = u.migrate_overlays(deploy, write=False, value_map=value_map, root=tmp_path)
    report = u.render_overlay_report(result)
    assert report[0].startswith("1 of 1 overlay(s) take 1 key(s)")
    assert f"{OVERLAY} (svc)" in report
    assert "  write    new.key = 1  (from moved.key)" in report
    assert any(line.startswith("  conflict fullnameOverride holds") for line in report)
    assert "  by hand  hand.enabled -> initContainers: Off is a list without it." in report
    assert "  dropped  gone: Nothing reads it." in report
    assert "  svc moved.key (1 file(s)): The new home." in report


def test_a_long_value_is_summarised_in_the_report(tmp_path: Path, value_map: Path) -> None:
    content = "x" * 200
    plan, _, _ = _migrate(tmp_path, value_map, f"moved:\n  key: [{{name: a, content: {content}}}]\n")
    report = u.render_overlay_report(u.OverlayMigration(plans=[plan]))
    assert "  write    new.key = <a list of 1>  (from moved.key)" in report
    assert content not in "\n".join(report)


# --------------------------------------------------------------- the real value map


def _real(tmp_path: Path, service: str, instance: str, body: dict) -> tuple[u.OverlayPlan, dict]:
    deploy = tmp_path / "real"
    (deploy / "values").mkdir(parents=True)
    path = deploy / "values" / f"{service}-{instance}-values.yaml"
    doc = {"deploy": {"service": service, "instance": instance}, **body}
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    result = u.migrate_overlays(deploy, write=True)
    return result.plans[0], yaml.safe_load(path.read_text(encoding="utf-8"))


def _contains(doc: object, part: object) -> bool:
    """Whether every key `part` holds is in `doc` with the same value."""
    if isinstance(part, dict):
        return isinstance(doc, dict) and all(k in doc and _contains(doc[k], v) for k, v in part.items())
    return doc == part


# The 2.2.0 overlay and the thin-chart overlay test_weave_fetcher_culvert.py renders side
# by side, migrated by hand.
HAND_MIGRATED = [
    (
        "dfe-fetcher",
        "acme",
        {"component": "fetcher-acme", "persistence": {"enabled": True, "size": "2Gi"}},
        {
            "fullnameOverride": "dfe-fetcher-acme",
            "writablePaths": {"cursor": {"persistence": {"enabled": True, "size": "2Gi"}}},
        },
    ),
    (
        "culvert",
        "default",
        {"persistence": {"enabled": True}},
        {"writablePaths": {"pki": {"persistence": {"enabled": True}}}},
    ),
]


@pytest.mark.parametrize(("service", "instance", "old", "new"), HAND_MIGRATED, ids=["fetcher", "culvert"])
def test_the_migration_reproduces_the_gates_hand_migrated_overlays(
    tmp_path: Path, service: str, instance: str, old: dict, new: dict
) -> None:
    _, doc = _real(tmp_path, service, instance, old)
    assert _contains(doc, new)
    assert _contains(doc, old)


def test_an_engine_with_oidc_on_takes_one_extra_env_per_mapping(tmp_path: Path) -> None:
    oidc = {
        "enabled": True,
        "providers": [
            {
                "name": "google",
                "secretName": "dfe-oidc-google",
                "envMappings": {"DFE_OIDC_GOOGLE_CLIENT_ID": "client-id", "DFE_OIDC_GOOGLE_CLIENT_SECRET": "client-secret"},
            }
        ],
    }
    _, doc = _real(tmp_path, "dfe-engine", "default", {"oidc": oidc})
    ref = {"valueFrom": {"secretKeyRef": {"name": "dfe-oidc-google", "key": "client-id"}}}
    assert doc["extraEnv"]["DFE_OIDC_GOOGLE_CLIENT_ID"] == ref
    assert set(doc["extraEnv"]) == {"DFE_OIDC_GOOGLE_CLIENT_ID", "DFE_OIDC_GOOGLE_CLIENT_SECRET"}
    assert doc["oidc"] == oidc


def test_an_engine_with_oidc_off_takes_none(tmp_path: Path) -> None:
    oidc = {"enabled": False, "providers": [{"secretName": "s", "envMappings": {"A": "b"}}]}
    plan, doc = _real(tmp_path, "dfe-engine", "default", {"oidc": oidc})
    assert "extraEnv" not in doc
    assert plan.writes == []


def test_a_single_instance_app_takes_no_fullname(tmp_path: Path) -> None:
    plan, doc = _real(tmp_path, "dfe-receiver", "default", {"replicaCount": 2})
    assert "fullnameOverride" not in doc
    assert plan.writes == []


@pytest.mark.parametrize("service", sorted(u.load_value_map()))
def test_every_component_prints_a_project_other_than_dfe(tmp_path: Path, service: str) -> None:
    renamed, _ = _real(tmp_path / "corp", service, "acme", {"project": "corp"})
    assert _project_line("corp") in renamed.by_hand
    spelled_out, _ = _real(tmp_path / "dfe", service, "acme", {"project": "dfe"})
    assert spelled_out.by_hand == []


# ------------------------------------------------------------------ apply and plan

ORDER_YAML = """
stages:
  "10-bootstrap":
    "10-cert-manager":
      key: bootstrap.cert-manager
  "40-apps":
    "70-dfe-fetcher":
      key: apps.dfe-fetcher
"""

DIGEST = "sha256:" + "ab" * 32
VERSIONS_YAML = f"""
current: "2.0.0"
stacks:
  1.0.0:
    bootstrap:
      cert-manager: "v1.0.0"
    apps:
      dfe-fetcher: "v1.0.0"
  1.1.0:
    bootstrap:
      cert-manager: "v1.1.0"
    apps:
      dfe-fetcher: "v1.1.0"
  2.0.0:
    bootstrap:
      cert-manager: "v1.1.0"
    apps:
      dfe-fetcher: "v1.1.0"
    chart-digests:
      dfe-fetcher: "{DIGEST}"
  3.0.0:
    bootstrap:
      cert-manager: "v1.2.0"
    apps:
      dfe-fetcher: "v1.2.0"
    chart-digests:
      dfe-fetcher: "{DIGEST}"
"""

PINS_YAML = 'base:\n  dfe-infra: "1.0.0"\n'
FETCHER = "values/dfe-fetcher-acme-values.yaml"
FETCHER_OVERLAY = (
    "deploy:\n  service: dfe-fetcher\n  instance: acme\n"
    "component: fetcher-acme\n"
    "persistence:\n  enabled: true\n"
)
VRL = "values/dfe-transform-vrl-acme-values.yaml"
VRL_OVERLAY = (
    "deploy:\n  service: dfe-transform-vrl\n  instance: acme\n"
    "component: transform-vrl-acme\n"
    "enrichmentTables:\n  - name: geo.csv\n    content: 'a,b'\n"
    "config:\n  enrichment_tables:\n    - name: zones\n      path: /srv/zones.csv\n"
    "      key_columns: [id]\n"
)
TABLE_MOUNT = "/etc/dfe-transform-vrl-enrichment"
# The apps.yaml file sets for the two transforms, each naming where its thin chart mounts it.
MANIFEST_YAML = f"""
apps:
  dfe-transform-vrl:
    files:
      - name: transforms
        values_path: fileSets.transforms.files
        mount_path: /etc/dfe-transform-vrl-transforms
        dir_setting: config.transforms.dir
      - name: enrichment
        values_path: fileSets.enrichment.files
        mount_path: {TABLE_MOUNT}
        entries_path: config.enrichment_tables
  dfe-transform-vector:
    files:
      - name: enrichment
        values_path: fileSets.enrichment.files
        mount_path: /etc/dfe-transform-vector/data
"""


@pytest.fixture(autouse=True)
def _no_real_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test whose kubectl call reaches this host's real cluster, while git runs for real."""
    real_run = u._run

    def guarded(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if cmd[:1] == ["kubectl"]:
            raise AssertionError(f"test reached a real cluster: {cmd}")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(u, "_run", guarded)


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git deploy repo pinned at 1.0.0 with one fetcher overlay, and a fake stack history."""
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "Test")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, "test@example.invalid")
    order = tmp_path / "upgrade-order.yaml"
    order.write_text(ORDER_YAML, encoding="utf-8")
    versions = tmp_path / "versions.yaml"
    versions.write_text(VERSIONS_YAML, encoding="utf-8")
    monkeypatch.setattr(u, "UPGRADE_ORDER", order)
    monkeypatch.setattr(u, "VERSIONS_FILE", versions)
    monkeypatch.setattr(u, "run_compat_check", lambda *_a, **_k: (True, "ok"))
    monkeypatch.setattr(u, "run_preflight", lambda *_a, **_k: [])
    monkeypatch.setattr(u, "wait_for_argo", lambda *_a, **_k: (True, "converged"))
    monkeypatch.setattr(u, "read_target_revision", lambda *_a, **_k: "main")
    manifest = tmp_path / "apps.yaml"
    manifest.write_text(MANIFEST_YAML, encoding="utf-8")
    monkeypatch.setattr(u, "APPS_MANIFEST", manifest)
    deploy = tmp_path / "deploy"
    (deploy / "values").mkdir(parents=True)
    (deploy / "pins.yaml").write_text(PINS_YAML, encoding="utf-8")
    (deploy / FETCHER).write_text(FETCHER_OVERLAY, encoding="utf-8")
    (deploy / VRL).write_text(VRL_OVERLAY, encoding="utf-8")
    for args in (["init", "-q"], ["add", "pins.yaml", "values"], ["commit", "-q", "-m", "initial"]):
        assert u._git(deploy, *args).returncode == 0
    return deploy


class _Args:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


def _apply_args(deploy: Path, **overrides: object) -> _Args:
    base = dict(
        deploy=str(deploy), to="2.0.0", dial=None, fixtures=None, live=False,
        kubeconfig=None, argocd_namespace="argocd", clickhouse_namespace="clickhouse",
        clickhouse_selector=u.DEFAULT_CLICKHOUSE_SELECTOR, clickhouse_merge_threshold=300.0,
        nodes_file=None, backup_marker=u.DEFAULT_BACKUP_MARKER,
        yes=True, push=False, timeout=900, dry_run=False, finalise=False, stop_before=None,
        from_stack=None, target_revision=None,
    )
    base.update(overrides)
    return _Args(**base)


def _log(deploy: Path) -> list[str]:
    return u._git(deploy, "log", "--format=%s").stdout.splitlines()


def test_stop_before_the_switch_stage_leaves_the_migration_committed_and_nothing_else_moved(
    stack: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = u.cmd_upgrade_apply(_apply_args(stack, stop_before="40-apps"))
    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert "stage 2/4: overlay-vocabulary" in err
    assert "stopping before stage 3/4 (40-apps)" in err
    assert _log(stack) == [
        "chore(upgrade): 2.0.0 stage 2 -- overlay vocabulary",
        "chore(upgrade): 2.0.0 stage 1 -- bootstrap.cert-manager",
        "initial",
    ]
    doc = yaml.safe_load((stack / FETCHER).read_text(encoding="utf-8"))
    assert doc["fullnameOverride"] == "dfe-fetcher-acme"
    assert doc["writablePaths"] == {"cursor": {"persistence": {"enabled": True}}}
    assert doc["component"] == "fetcher-acme"
    vrl = yaml.safe_load((stack / VRL).read_text(encoding="utf-8"))
    assert vrl["fileSets"]["enrichment"]["files"] == [{"name": "geo.csv", "content": "a,b"}]
    assert [e["name"] for e in vrl["config"]["enrichment_tables"]] == ["zones"]
    assert u._git(stack, "status", "--porcelain").stdout == ""


def _retarget_waits(
    stack: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: object
) -> tuple[int, list[tuple[str, bool]]]:
    """Apply across a retarget from the 1.0.0 tag, recording each Argo wait's stale ref and health ask."""
    remote = tmp_path / "remote.git"
    assert u._run(["git", "init", "-q", "--bare", str(remote)]).returncode == 0
    for args in (["remote", "add", "origin", str(remote)], ["push", "-q", "-u", "origin", "HEAD"]):
        assert u._git(stack, *args).returncode == 0, args
    waits: list[tuple[str, bool]] = []

    def argo(*_a: object, stale_revision: str = "", require_healthy: bool = True, **_k: object) -> tuple[bool, str]:
        waits.append((stale_revision, require_healthy))
        return True, "converged"

    monkeypatch.setattr(u, "wait_for_argo", argo)
    monkeypatch.setattr(u, "read_target_revision", lambda *_a, **_k: "1.0.0")
    monkeypatch.setattr(u, "write_target_revision", lambda *_a, **_k: (True, "moved"))
    rc = u.cmd_upgrade_apply(_apply_args(stack, push=True, **overrides))
    return rc, waits


def test_the_retarget_wait_leaves_health_to_the_tables_stage(
    stack: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    rc, waits = _retarget_waits(stack, tmp_path, monkeypatch)
    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert ("1.0.0", False) in waits
    # Every other wait, the tables stage's last of all, still asks for health.
    assert [healthy for stale, healthy in waits if not stale] == [True, True, True, True]
    assert waits[-1] == ("", True)
    assert _log(stack)[0] == "chore(upgrade): 2.0.0 stage 4 -- enrichment tables"


def test_the_retarget_wait_asks_for_health_when_the_tables_stage_does_not_run(
    stack: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    rc, waits = _retarget_waits(stack, tmp_path, monkeypatch, stop_before=u.TABLES_STAGE)
    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert ("1.0.0", True) in waits
    assert "stopping before stage 4/4 (enrichment-tables)" in err


def test_a_second_apply_commits_nothing(stack: Path, capsys: pytest.CaptureFixture) -> None:
    assert u.cmd_upgrade_apply(_apply_args(stack, stop_before="40-apps")) == u.EXIT_OK
    before = _log(stack)
    rc = u.cmd_upgrade_apply(_apply_args(stack, stop_before="40-apps", from_stack="1.0.0"))
    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert "stage 2 (overlay-vocabulary) changed nothing" in err
    assert _log(stack) == before


def test_the_dry_run_orders_the_stage_before_the_switch_and_touches_nothing(
    stack: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = u.cmd_upgrade_apply(_apply_args(stack, dry_run=True, yes=False))
    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    first = err.index("stage 1/4: 10-bootstrap")
    migration = err.index("stage 2/4: overlay-vocabulary")
    switch = err.index("stage 3/4: 40-apps")
    tables = err.index("stage 4/4: enrichment-tables")
    assert first < migration < switch < tables
    assert 'write    fullnameOverride = "dfe-fetcher-acme"' in err
    assert f"[dry-run] git -C {stack} add {FETCHER} {VRL}" in err
    assert "commit -m 'chore(upgrade): 2.0.0 stage 2 -- overlay vocabulary'" in err
    assert "commit -m 'chore(upgrade): 2.0.0 stage 4 -- enrichment tables'" in err
    assert (stack / FETCHER).read_text(encoding="utf-8") == FETCHER_OVERLAY
    assert (stack / VRL).read_text(encoding="utf-8") == VRL_OVERLAY
    assert _log(stack) == ["initial"]


def test_stop_before_the_migration_itself_leaves_the_overlays_alone(
    stack: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = u.cmd_upgrade_apply(_apply_args(stack, stop_before=u.MIGRATION_STAGE))
    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert "stopping before stage 2/4 (overlay-vocabulary)" in err
    assert (stack / FETCHER).read_text(encoding="utf-8") == FETCHER_OVERLAY
    assert _log(stack)[0] == "chore(upgrade): 2.0.0 stage 1 -- bootstrap.cert-manager"


@pytest.mark.parametrize(
    ("to", "from_stack"), [("1.1.0", None), ("3.0.0", "2.0.0")], ids=["to-has-none", "both-have"]
)
def test_no_stage_unless_only_the_target_carries_chart_digests(
    stack: Path, capsys: pytest.CaptureFixture, to: str, from_stack: str | None
) -> None:
    rc = u.cmd_upgrade_apply(_apply_args(stack, to=to, from_stack=from_stack, dry_run=True))
    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert "overlay-vocabulary" not in err
    assert "enrichment-tables" not in err
    assert "stage 2/2: 40-apps" in err


def test_a_failed_migration_stops_apply_with_nothing_of_it_committed(
    stack: Path, capsys: pytest.CaptureFixture
) -> None:
    (stack / "values" / "dfe-fetcher-bad-values.yaml").write_text("x: [\n", encoding="utf-8")
    assert u._git(stack, "add", "values").returncode == 0
    assert u._git(stack, "commit", "-q", "-m", "a broken overlay").returncode == 0
    rc = u.cmd_upgrade_apply(_apply_args(stack))
    err = capsys.readouterr().err
    assert rc == u.EXIT_BLOCKED
    assert "FAILED at stage 2: values/dfe-fetcher-bad-values.yaml is not YAML" in err
    assert _log(stack)[0] == "chore(upgrade): 2.0.0 stage 1 -- bootstrap.cert-manager"
    assert (stack / FETCHER).read_text(encoding="utf-8") == FETCHER_OVERLAY


def test_plan_lists_the_stage_where_apply_runs_it(stack: Path, capsys: pytest.CaptureFixture) -> None:
    args = _Args(deploy=str(stack), to="2.0.0", dial=None, fixtures=None, live=False, kubeconfig=None, argocd_namespace="argocd")
    assert u.cmd_upgrade_plan(args) == u.EXIT_OK
    written = (stack / "upgrades" / "1.0.0-to-2.0.0.md").read_text(encoding="utf-8")
    assert "## overlay vocabulary (stage overlay-vocabulary, before 40-apps)" in written
    assert "Stage enrichment-tables runs after 40-apps" in written
    assert f"{FETCHER} (dfe-fetcher)" in written
    assert "  write    fullnameOverride = \"dfe-fetcher-acme\"" in written
    assert (stack / FETCHER).read_text(encoding="utf-8") == FETCHER_OVERLAY
    capsys.readouterr()


def test_plan_has_no_overlay_section_without_the_switch(stack: Path, capsys: pytest.CaptureFixture) -> None:
    args = _Args(deploy=str(stack), to="1.1.0", dial=None, fixtures=None, live=False, kubeconfig=None, argocd_namespace="argocd")
    assert u.cmd_upgrade_plan(args) == u.EXIT_OK
    assert "overlay vocabulary" not in capsys.readouterr().out


def test_plan_is_blocked_by_an_overlay_the_migration_cannot_read(
    stack: Path, capsys: pytest.CaptureFixture
) -> None:
    (stack / "values" / "dfe-fetcher-bad-values.yaml").write_text("x: [\n", encoding="utf-8")
    args = _Args(deploy=str(stack), to="2.0.0", dial=None, fixtures=None, live=False, kubeconfig=None, argocd_namespace="argocd")
    assert u.cmd_upgrade_plan(args) == u.EXIT_BLOCKED
    assert "BLOCKED: values/dfe-fetcher-bad-values.yaml is not YAML" in capsys.readouterr().out


# ------------------------------------------------------------ the enrichment tables


def _thin_vrl(tmp_path: Path, files: list[str], entries: list[dict] | None = None) -> Path:
    """A deploy repo whose one vrl overlay already carries the thin-chart keys."""
    deploy = tmp_path / "tables"
    (deploy / "values").mkdir(parents=True)
    doc: dict = {
        "deploy": {"service": "dfe-transform-vrl", "instance": "acme"},
        "fileSets": {"enrichment": {"files": [{"name": n, "content": "a,b"} for n in files]}},
        "config": {"source": {"transport": "kafka"}},
    }
    if entries is not None:
        doc["config"]["enrichment_tables"] = entries
    (deploy / VRL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return deploy


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    path = tmp_path / "apps.yaml"
    path.write_text(MANIFEST_YAML, encoding="utf-8")
    return path


def _entries(deploy: Path) -> list[dict] | None:
    doc = yaml.safe_load((deploy / VRL).read_text(encoding="utf-8"))
    return doc["config"].get("enrichment_tables")


def test_each_table_file_is_named_under_the_thin_mount(tmp_path: Path, manifest: Path) -> None:
    deploy = _thin_vrl(tmp_path, ["geo.csv", "asn.v2.json"])
    result = u.name_tables(deploy, write=True, manifest=manifest)
    assert result.changed == [VRL]
    assert _entries(deploy) == [
        {"name": "geo", "path": f"{TABLE_MOUNT}/geo.csv"},
        {"name": "asn.v2", "path": f"{TABLE_MOUNT}/asn.v2.json"},
    ]
    doc = yaml.safe_load((deploy / VRL).read_text(encoding="utf-8"))
    assert doc["config"]["source"] == {"transport": "kafka"}


def test_an_authors_entry_is_never_touched(tmp_path: Path, manifest: Path) -> None:
    own = {"name": "geo", "path": "/srv/geo.csv", "key_columns": ["ip"]}
    deploy = _thin_vrl(tmp_path, ["geo.csv", "zones.csv"], [own])
    u.name_tables(deploy, write=True, manifest=manifest)
    assert _entries(deploy) == [own, {"name": "zones", "path": f"{TABLE_MOUNT}/zones.csv"}]


def test_a_second_naming_run_changes_nothing(tmp_path: Path, manifest: Path) -> None:
    deploy = _thin_vrl(tmp_path, ["geo.csv"])
    u.name_tables(deploy, write=True, manifest=manifest)
    after = (deploy / VRL).read_bytes()
    assert u.name_tables(deploy, write=True, manifest=manifest).changed == []
    assert (deploy / VRL).read_bytes() == after


def test_a_read_only_naming_run_writes_nothing(tmp_path: Path, manifest: Path) -> None:
    deploy = _thin_vrl(tmp_path, ["geo.csv"])
    before = (deploy / VRL).read_bytes()
    result = u.name_tables(deploy, write=False, manifest=manifest)
    assert result.changed == [VRL]
    assert (deploy / VRL).read_bytes() == before


def test_a_set_with_no_entries_path_is_never_named(tmp_path: Path, manifest: Path) -> None:
    deploy = tmp_path / "vector"
    (deploy / "values").mkdir(parents=True)
    doc = {
        "deploy": {"service": "dfe-transform-vector", "instance": "acme"},
        "fileSets": {"enrichment": {"files": [{"name": "geo.csv", "content": "a"}]}},
    }
    path = deploy / "values" / "dfe-transform-vector-acme-values.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert u.name_tables(deploy, write=True, manifest=manifest).changed == []


def test_a_set_apps_yaml_names_no_mount_for_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "apps.yaml"
    unmounted = MANIFEST_YAML.replace(f"        mount_path: {TABLE_MOUNT}\n", "")
    path.write_text(unmounted, encoding="utf-8")
    deploy = _thin_vrl(tmp_path, ["geo.csv"])
    result = u.name_tables(deploy, write=True, manifest=path)
    assert result.changed == []
    assert result.lines == [
        "apps.yaml names no mount_path for dfe-transform-vrl enrichment, so its entries are "
        "left as they are"
    ]


def test_unnaming_strips_only_the_derived_entries(tmp_path: Path, manifest: Path) -> None:
    own = {"name": "zones", "path": "/srv/zones.csv", "key_columns": ["id"]}
    pinned = {"name": "asn", "path": f"{TABLE_MOUNT}/asn.csv", "key_columns": ["n"]}
    deploy = _thin_vrl(tmp_path, ["geo.csv", "asn.csv"], [own, pinned])
    u.name_tables(deploy, write=True, manifest=manifest)
    assert _entries(deploy) == [own, pinned, {"name": "geo", "path": f"{TABLE_MOUNT}/geo.csv"}]
    result = u.unname_tables(deploy, write=True, manifest=manifest)
    assert _entries(deploy) == [own, pinned]
    assert result.lines == [
        f'{VRL}: strip config.enrichment_tables {{"name": "geo", "path": "{TABLE_MOUNT}/geo.csv"}}',
        f"{VRL}: by hand config.enrichment_tables asn names {TABLE_MOUNT}/asn.csv, which the "
        "2.2.0 chart does not mount",
    ]


def test_unnaming_the_last_entry_takes_the_list_out(tmp_path: Path, manifest: Path) -> None:
    deploy = _thin_vrl(tmp_path, ["geo.csv"])
    before = yaml.safe_load((deploy / VRL).read_text(encoding="utf-8"))
    u.name_tables(deploy, write=True, manifest=manifest)
    u.unname_tables(deploy, write=True, manifest=manifest)
    assert yaml.safe_load((deploy / VRL).read_text(encoding="utf-8")) == before


def test_the_tables_stage_names_the_files_once_the_charts_have_moved(
    stack: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = u.cmd_upgrade_apply(_apply_args(stack))
    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    # Stage 1 moved the whole pin, so stage 3 has nothing of its own to commit.
    assert _log(stack)[:2] == [
        "chore(upgrade): 2.0.0 stage 4 -- enrichment tables",
        "chore(upgrade): 2.0.0 stage 2 -- overlay vocabulary",
    ]
    assert _entries(stack)[-1] == {"name": "geo", "path": f"{TABLE_MOUNT}/geo.csv"}
    assert _entries(stack)[0]["name"] == "zones"


def test_stop_before_the_tables_stage_leaves_the_entries_unwritten(
    stack: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = u.cmd_upgrade_apply(_apply_args(stack, stop_before=u.TABLES_STAGE))
    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert "stopping before stage 4/4 (enrichment-tables)" in err
    assert [e["name"] for e in _entries(stack)] == ["zones"]


def test_a_rollback_off_the_thin_charts_strips_the_derived_entries_in_its_commit(
    stack: Path, capsys: pytest.CaptureFixture
) -> None:
    assert u.cmd_upgrade_apply(_apply_args(stack)) == u.EXIT_OK
    migrated = yaml.safe_load(u._git(stack, "show", f"HEAD~1:{VRL}").stdout)
    args = _Args(
        deploy=str(stack), to="1.0.0", kubeconfig=None, push=False, dry_run=False,
        argocd_namespace="argocd", skip_cluster_check=True, target_revision=None,
    )
    rc = u.cmd_upgrade_rollback(args)
    err = capsys.readouterr().err
    assert rc == u.EXIT_OK, err
    assert 'strip config.enrichment_tables {"name": "geo"' in err
    assert yaml.safe_load((stack / VRL).read_text(encoding="utf-8")) == migrated
    assert u._git(stack, "show", "--name-only", "--format=", "HEAD").stdout.split() == [
        "pins.yaml",
        VRL,
    ]


def test_a_rollback_strips_the_entries_before_it_moves_the_charts_back(
    stack: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert u.cmd_upgrade_apply(_apply_args(stack)) == u.EXIT_OK
    capsys.readouterr()
    monkeypatch.setattr(u, "read_target_revision", lambda *_a, **_k: "2.0.0")
    args = _Args(
        deploy=str(stack), to="1.0.0", kubeconfig=None, push=False, dry_run=True,
        argocd_namespace="argocd", skip_cluster_check=False, target_revision=None,
    )
    assert u.cmd_upgrade_rollback(args) == u.EXIT_OK
    out = capsys.readouterr().out
    strip = out.index(f"[dry-run] strip the derived table entries from {VRL}")
    retarget = out.index("secret/dfe-cluster dfe.hyperi.io/target_revision=1.0.0")
    assert strip < retarget
    assert _entries(stack)[-1] == {"name": "geo", "path": f"{TABLE_MOUNT}/geo.csv"}


def test_a_rollback_between_thin_stacks_keeps_the_entries(
    stack: Path, capsys: pytest.CaptureFixture
) -> None:
    assert u.cmd_upgrade_apply(_apply_args(stack)) == u.EXIT_OK
    assert u.cmd_upgrade_apply(_apply_args(stack, to="3.0.0", from_stack="2.0.0")) == u.EXIT_OK
    args = _Args(
        deploy=str(stack), to="2.0.0", kubeconfig=None, push=False, dry_run=False,
        argocd_namespace="argocd", skip_cluster_check=True, target_revision=None,
    )
    assert u.cmd_upgrade_rollback(args) == u.EXIT_OK
    capsys.readouterr()
    assert _entries(stack)[-1] == {"name": "geo", "path": f"{TABLE_MOUNT}/geo.csv"}
