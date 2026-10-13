#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_values_migration.py
#  Purpose:      Prove a 2.2.0 deploy repo's values carry onto the thin-chart
#                keys: each rule's new key reads back as its old value, the thin
#                charts render the 2.2.0 object names, claims, consumer groups
#                and service names from the carried overlay, the 2.2.0 charts
#                render it unchanged, and dfe-extras refuses an overlay nothing
#                carried.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""scripts/values_migration.py and the readers of values-migrations.yaml.

    python3 -m pytest scripts/tests/test_values_migration.py -q

fixtures/values-2.2.0 is a deploy repo a 2.2.0 engine wrote, in the shapes it
writes them, with the transform and table bodies cut short. fixtures/charts-2.2.0
is the 2.2.0 tag's chart for each of its apps, which render on their own. The
thin-chart renders need helm and the scalo-service library (_weave.library).
"""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from _weave import helm, render_app, render_extras, weave

TESTS = Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import dfe_ops_upgrade as u  # noqa: E402
import values_migration as vm  # noqa: E402

FIXTURES = TESTS / "fixtures" / "values-2.2.0"
CHARTS_220 = TESTS / "fixtures" / "charts-2.2.0"
CLUSTER = "in-cluster"
FROM_STACK = "2.2.0"
TO_STACK = "2.2.1-rc.9"

# Each app the fixture deploy repo carries: its instance, and a cloud it renders on.
# culvert takes local-dfe: local.yaml's listener list is the receiver's, at either tag.
APPS = (
    ("dfe-transform-vrl", "merakivrl", "local"),
    ("dfe-transform-vector", "umbrellavec", "local"),
    ("dfe-transform-elastic", "ioselastic", "local"),
    ("dfe-fetcher", "pypiwatch", "local"),
    ("culvert", "default", "local-dfe"),
    ("dfe-receiver", "default", "local"),
    ("dfe-engine", "default", "local"),
)
APP_IDS = [service for service, _, _ in APPS]

# Where the 2.2.0 vrl chart mounted the merakivrl tables, and where both charts do once carried.
LEGACY_ENRICHMENT = "/etc/dfe-transform-vrl-merakivrl-enrichment"
ENRICHMENT = "/etc/dfe-transform-vrl-enrichment"
CONFIG_CHECKSUM = re.compile(r"checksum/config: [0-9a-f]{64}")


def _copy(tmp_path: Path, name: str = "deploy") -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURES, target)
    return target


@pytest.fixture
def deploy(tmp_path: Path) -> Path:
    """The 2.2.0 deploy repo, as the engine left it."""
    return _copy(tmp_path)


@pytest.fixture(scope="module")
def carried(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The 2.2.0 deploy repo after expand: both keys of every rule side by side."""
    root = _copy(tmp_path_factory.mktemp("carried"))
    vm.expand(root, vm.load_map())
    return root


@pytest.fixture(scope="module")
def contracted(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The 2.2.0 deploy repo after expand, then contract: the 2.2.0 keys gone."""
    root = _copy(tmp_path_factory.mktemp("contracted"))
    migration = vm.load_map()
    vm.expand(root, migration)
    vm.contract(root, migration)
    return root


def overlay(root: Path, service: str, instance: str) -> Path:
    return root / "values" / f"{service}-{instance}-values.yaml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def dig(doc: dict, dotted: str) -> object:
    node: object = doc
    for step in dotted.split("."):
        if not isinstance(node, dict) or step not in node:
            return None
        node = node[step]
    return node


def write(root: Path, service: str, instance: str, text: str) -> Path:
    path = overlay(root, service, instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------ renders


def render_220(service: str, instance: str, values: Path) -> str:
    """The 2.2.0 tag's chart rendered with one overlay, under the release the Application names."""
    chart = CHARTS_220 / service
    release = f"{service}-{instance}-{CLUSTER}"
    cmd = [helm(), "template", release, str(chart), "-f", str(values), "--namespace", "dfe"]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    assert out.returncode == 0, out.stderr
    return out.stdout


def docs_of(text: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


def render_thin(service: str, instance: str, cloud: str, root: Path) -> list[dict]:
    """The app's thin chart beside dfe-extras, as its Application deploys it from `root`."""
    return render_app(service, "single", cloud, deploy_repo=root, instance=instance, cluster=CLUSTER)


def workload(docs: list[dict], name: str) -> dict | None:
    return next(
        (d for d in docs if d.get("kind") in ("Deployment", "StatefulSet") and d["metadata"]["name"] == name),
        None,
    )


def env(work: dict) -> dict[str, str]:
    found: dict[str, str] = {}
    for container in work["spec"]["template"]["spec"].get("containers") or []:
        for entry in container.get("env") or []:
            if "value" in entry:
                found[entry["name"]] = str(entry["value"])
    return found


def claims(docs: list[dict]) -> set[str]:
    return {d["metadata"]["name"] for d in docs if d.get("kind") == "PersistentVolumeClaim"}


def fullname_220(service: str, values: dict) -> str:
    """{project}-{component}, the name every 2.2.0 chart gave its objects."""
    defaults = load(CHARTS_220 / service / "values.yaml")
    return f"dfe-{values.get('component') or defaults['component']}"


# ------------------------------------------------- the thin charts keep the 2.2.0 identity


@pytest.mark.parametrize(("service", "instance", "cloud"), APPS, ids=APP_IDS)
def test_the_thin_chart_renders_the_220_identity_from_the_carried_overlay(
    service: str, instance: str, cloud: str, carried: Path
) -> None:
    """Same workload name, selector, service account, claims, consumer group and service name.

    A selector is immutable, and a renamed claim is pruned, so any difference here
    is a delete and recreate, or a lost volume, on the first sync after the retarget.
    """
    original = overlay(FIXTURES, service, instance)
    values = load(original)
    old = docs_of(render_220(service, instance, original))
    new = render_thin(service, instance, cloud, carried)
    name = fullname_220(service, values)

    before, after = workload(old, name), workload(new, name)
    assert before is not None, f"the 2.2.0 chart renders no {name}"
    assert after is not None, f"the thin chart renders no {name}: {[d['metadata']['name'] for d in new]}"
    assert after["spec"]["selector"] == before["spec"]["selector"]
    sa = after["spec"]["template"]["spec"].get("serviceAccountName")
    assert sa == before["spec"]["template"]["spec"].get("serviceAccountName")
    if name != service:
        assert workload(new, service) is None, f"a {service} workload renders under the bare chart name"
    assert claims(old) <= claims(new)
    old_env, new_env = env(before), env(after)
    for key, value in old_env.items():
        if key.endswith("GROUP_ID"):
            assert new_env.get(key) == value, key
    if "otelServiceName" in values:
        assert new_env.get("OTEL_SERVICE_NAME") == values["otelServiceName"]


@pytest.mark.parametrize(("service", "instance"), [("dfe-transform-vrl", "merakivrl"), ("dfe-fetcher", "pypiwatch")])
def test_without_the_migration_the_thin_chart_renames_the_instance_and_drops_its_claim(
    service: str, instance: str
) -> None:
    """The defect the identity test guards, rendered: the 2.2.0 overlay past dfe-extras' guard."""
    old = docs_of(render_220(service, instance, overlay(FIXTURES, service, instance)))
    new = render_app(
        service, "single", "local", deploy_repo=FIXTURES, instance=instance, cluster=CLUSTER, extras=False
    )
    assert workload(old, f"{service}-{instance}") is not None
    assert workload(new, f"{service}-{instance}") is None
    assert workload(new, service) is not None
    if service == "dfe-fetcher":
        assert claims(old) == {"dfe-fetcher-pypiwatch-cursor"}
        assert claims(new) == set()


@pytest.mark.parametrize("service", ["dfe-transform-vrl", "dfe-transform-vector"])
def test_a_transform_keeps_its_consumer_group_so_it_does_not_replay(service: str, carried: Path) -> None:
    """A new group starts at the oldest record, so the group has to be the 2.2.0 one by name."""
    instance = next(i for s, i, _ in APPS if s == service)
    component = load(overlay(FIXTURES, service, instance))["component"]
    after = workload(render_thin(service, instance, "local", carried), f"dfe-{component}")
    assert after is not None
    assert env(after)["DFE_TRANSFORM_SOURCE_GROUP_ID"] == f"dfe-{component}"


def test_the_fetcher_keeps_its_cursor_claim_and_argo_never_prunes_it(carried: Path) -> None:
    docs = render_thin("dfe-fetcher", "pypiwatch", "local", carried)
    claim = next(d for d in docs if d.get("kind") == "PersistentVolumeClaim")
    assert claim["metadata"]["name"] == "dfe-fetcher-pypiwatch-cursor"
    assert claim["metadata"]["annotations"]["argocd.argoproj.io/sync-options"] == "Prune=false"


@pytest.mark.parametrize("service", ["dfe-transform-vrl", "dfe-transform-vector"])
def test_a_transform_mounts_the_files_the_220_chart_mounted(service: str, carried: Path) -> None:
    instance = next(i for s, i, _ in APPS if s == service)
    values = load(overlay(FIXTURES, service, instance))
    docs = render_thin(service, instance, "local", carried)
    data = {
        d["metadata"]["name"].rsplit("-", 1)[-1]: d.get("data") or {}
        for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith(("-transforms", "-enrichment"))
    }
    assert data["transforms"] == {f["name"]: f["content"] for f in values["transformFiles"]}
    assert data["enrichment"] == {f["name"]: f["content"] for f in values["enrichmentTables"]}


def test_vrl_registers_every_table_at_the_directory_it_mounts(carried: Path) -> None:
    docs = render_thin("dfe-transform-vrl", "merakivrl", "local", carried)
    config = next(d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-config"))
    body = yaml.safe_load(config["data"]["config.yaml"])
    assert body["enrichment_tables"] == [{"name": "timezones", "path": f"{ENRICHMENT}/timezones.csv"}]
    mounts = [
        m["mountPath"]
        for c in workload(docs, "dfe-transform-vrl-merakivrl")["spec"]["template"]["spec"]["containers"]
        for m in c.get("volumeMounts") or []
    ]
    assert ENRICHMENT in mounts


# --------------------------------------------------- the 2.2.0 charts ignore the carried keys


@pytest.mark.parametrize(("service", "instance", "cloud"), APPS, ids=APP_IDS)
def test_the_220_chart_renders_the_carried_overlay_unchanged(
    service: str, instance: str, cloud: str, carried: Path
) -> None:
    """The stage push lands under the 2.2.0 charts, so it must not move a 2.2.0 object.

    vrl is the one difference by design: enrichmentTablesDir moves its table mount
    to the directory the thin chart mounts, the entries move with it, and the
    config checksum with them, so the 2.2.0 vrl pod rolls once at the stage push.
    """
    del cloud
    before = render_220(service, instance, overlay(FIXTURES, service, instance))
    after = render_220(service, instance, overlay(carried, service, instance))
    if service == "dfe-transform-vrl":
        assert LEGACY_ENRICHMENT in before
        before = CONFIG_CHECKSUM.sub("checksum/config: X", before.replace(LEGACY_ENRICHMENT, ENRICHMENT))
        after = CONFIG_CHECKSUM.sub("checksum/config: X", after)
    assert after == before


# ------------------------------------------------------------------ contract


@pytest.mark.parametrize(("service", "instance", "cloud"), APPS, ids=APP_IDS)
def test_dropping_the_220_keys_leaves_the_thin_render_unchanged(
    service: str, instance: str, cloud: str, carried: Path, contracted: Path
) -> None:
    assert render_thin(service, instance, cloud, contracted) == render_thin(service, instance, cloud, carried)


def test_contract_drops_the_moved_keys_and_keeps_what_is_still_read(contracted: Path) -> None:
    vrl = load(overlay(contracted, "dfe-transform-vrl", "merakivrl"))
    for gone in ("transformFiles", "enrichmentTables", "enrichmentTablesDir"):
        assert gone not in vrl
    assert vrl["component"] == "transform-vrl-merakivrl"
    assert vrl["otelServiceName"] == "dfe-transform-vrl-merakivrl"
    fetcher = load(overlay(contracted, "dfe-fetcher", "pypiwatch"))
    assert "persistence" not in fetcher
    assert fetcher["writablePaths"] == {"cursor": {"persistence": {"enabled": True}}}


def test_contract_twice_changes_nothing(contracted: Path) -> None:
    before = {p.name: p.read_text(encoding="utf-8") for p in (contracted / "values").iterdir()}
    hits = vm.contract(contracted, vm.load_map())
    assert [h for h in hits if h.state == vm.CONTRACT] == []
    assert {p.name: p.read_text(encoding="utf-8") for p in (contracted / "values").iterdir()} == before


def test_contract_keeps_an_old_key_whose_new_key_is_missing(deploy: Path) -> None:
    """Dropping it would lose the value, so it stays and reads as still to carry."""
    hits = vm.contract(deploy, vm.load_map())
    assert [h for h in hits if h.state == vm.CONTRACT] == []
    assert any(h.state == vm.MIGRATE and "transformFiles" in h.detail for h in hits)
    assert "transformFiles" in load(overlay(deploy, "dfe-transform-vrl", "merakivrl"))


# ------------------------------------------------------------------ the guard


@pytest.mark.parametrize(
    ("service", "instance", "cloud", "named"),
    [
        ("dfe-transform-vrl", "merakivrl", "local", "component is set and fullnameOverride is not"),
        ("dfe-fetcher", "pypiwatch", "local", "component is set and fullnameOverride is not"),
        ("culvert", "default", "local-dfe", "persistence.enabled is set and writablePaths.pki.persistence.enabled"),
    ],
    ids=["vrl", "fetcher", "culvert"],
)
def test_the_render_refuses_an_overlay_nothing_carried_naming_the_fix(
    service: str, instance: str, cloud: str, named: str, deploy: Path, carried: Path
) -> None:
    with pytest.raises(weave().WeaveError) as raised:
        render_extras(service, "single", cloud, deploy_repo=deploy, instance=instance, cluster=CLUSTER)
    assert named in str(raised.value)
    assert "dfe-ops upgrade migrate-values" in str(raised.value)
    assert render_extras(service, "single", cloud, deploy_repo=carried, instance=instance, cluster=CLUSTER) is not None


def test_the_guard_takes_a_new_key_an_integration_layer_always_sets_only_when_it_matches(tmp_path: Path) -> None:
    """fileSets.transforms.mountPath is always set by apps/dfe-transform-vrl, so absence proves nothing."""
    root = _copy(tmp_path)
    path = overlay(root, "dfe-transform-vrl", "merakivrl")
    vm.expand(root, vm.load_map())
    path.write_text(path.read_text(encoding="utf-8") + "transformFilesDir: /etc/custom\n", encoding="utf-8")
    with pytest.raises(
        weave().WeaveError, match=re.escape("transformFilesDir is set and fileSets.transforms.mountPath is not")
    ):
        render_extras("dfe-transform-vrl", "single", "local", deploy_repo=root, instance="merakivrl", cluster=CLUSTER)
    vm.expand(root, vm.load_map())
    assert load(path)["fileSets"]["transforms"]["mountPath"] == "/etc/custom"
    docs = render_extras("dfe-transform-vrl", "single", "local", deploy_repo=root, instance="merakivrl", cluster=CLUSTER)
    assert docs is not None


def test_the_guard_passes_what_the_engine_writes_today() -> None:
    """The engine writes otelServiceName alone on a single-deployment app, so that rule carries no guard."""
    migration = vm.load_map()
    guarded = {rule.sources[0] for rule in migration.rules if rule.guard}
    assert "otelServiceName" not in guarded
    docs = render_extras(
        "dfe-receiver", "single", "local",
        overlay={"otelServiceName": "dfe-receiver"}, infra={"exposure": {"groups": {"ingest": {"allow": []}}}},
    )
    assert docs is not None


# --------------------------------------------------------------- each rule, read back


OPERATOR_KEYS = {
    ("dfe-fetcher", "cursor"): (
        "deploy:\n  service: dfe-fetcher\n  instance: cursor\ncomponent: fetcher-cursor\n"
        "persistence:\n  enabled: true\n  size: 2Gi\n  storageClass: fast-ssd\n"
    ),
    ("culvert", "default"): (
        "deploy:\n  service: culvert\n  instance: default\n"
        "persistence:\n  enabled: true\n  size: 1Gi\n  storageClass: \"local-path\"\n  accessMode: ReadWriteOnce\n"
    ),
    ("dfe-engine", "default"): (
        "deploy:\n  service: dfe-engine\n  instance: default\n"
        "config:\n  persistence:\n    enabled: true\n    size: 5Gi  # the runtime store\n"
    ),
    ("dfe-receiver", "default"): (
        "deploy:\n  service: dfe-receiver\n  instance: default\n"
        "receiver:\n  buffer:\n    memoryLimit: 1073741824\n    spillover:\n      enabled: true\n"
        "      path: /var/spool/dfe-receiver\n      sizeLimit: 20Gi\n"
    ),
    ("dfe-archiver", "default"): (
        "deploy:\n  service: dfe-archiver\n  instance: default\nspool:\n  path: /var/spool/dfe/archiver\n"
        "  sizeLimit: 15Gi\n"
    ),
    ("dfe-transform-vrl", "offsets"): (
        "deploy:\n  service: dfe-transform-vrl\n  instance: offsets\ncomponent: transform-vrl-offsets\n"
        "kafka:\n  autoOffsetReset: latest\ntransformFilesDir: /etc/vrl-programs\n"
        "nodeScheduling:\n  nodeSelector:\n    pool: transforms\n  tolerations:\n  - key: pool\n    operator: Equal\n"
        "    value: transforms\n    effect: NoSchedule\n"
    ),
}

# Each rule's old key in an OPERATOR_KEYS overlay, and the key it is carried to.
CARRIED = [
    ("dfe-fetcher", "cursor", "persistence.enabled", "writablePaths.cursor.persistence.enabled"),
    ("dfe-fetcher", "cursor", "persistence.size", "writablePaths.cursor.persistence.size"),
    ("dfe-fetcher", "cursor", "persistence.storageClass", "writablePaths.cursor.persistence.storageClass"),
    ("culvert", "default", "persistence.enabled", "writablePaths.pki.persistence.enabled"),
    ("culvert", "default", "persistence.size", "writablePaths.pki.persistence.size"),
    ("culvert", "default", "persistence.storageClass", "writablePaths.pki.persistence.storageClass"),
    ("dfe-engine", "default", "config.persistence", "writablePaths.config.persistence"),
    ("dfe-receiver", "default", "receiver.buffer.memoryLimit", "configOverrides.buffer.memory_limit"),
    ("dfe-receiver", "default", "receiver.buffer.spillover.enabled", "configOverrides.buffer.spillover.enabled"),
    ("dfe-receiver", "default", "receiver.buffer.spillover.path", "configOverrides.buffer.spillover.path"),
    ("dfe-receiver", "default", "receiver.buffer.spillover.sizeLimit", "writablePaths.spool.sizeLimit"),
    ("dfe-archiver", "default", "spool.sizeLimit", "writablePaths.spool.sizeLimit"),
    ("dfe-transform-vrl", "offsets", "kafka.autoOffsetReset", "config.source.auto_offset_reset"),
    ("dfe-transform-vrl", "offsets", "transformFilesDir", "fileSets.transforms.mountPath"),
    ("dfe-transform-vrl", "offsets", "nodeScheduling.nodeSelector", "nodeSelector"),
    ("dfe-transform-vrl", "offsets", "nodeScheduling.tolerations", "tolerations"),
]


@pytest.fixture
def operator_keys(tmp_path: Path) -> Path:
    root = tmp_path / "operator"
    for (service, instance), text in OPERATOR_KEYS.items():
        write(root, service, instance, text)
    (root / "infra").mkdir()
    (root / "infra" / "common.yaml").write_text(
        "nodeScheduling:\n  nodeSelector:\n    dfe.example.com/pool: dfe\n", encoding="utf-8"
    )
    return root


@pytest.mark.parametrize(("service", "instance", "old", "new"), CARRIED, ids=[f"{s}:{o}" for s, _, o, _ in CARRIED])
def test_each_rule_carries_the_old_value_to_the_new_key(
    service: str, instance: str, old: str, new: str, operator_keys: Path
) -> None:
    before = load(overlay(operator_keys, service, instance))
    vm.expand(operator_keys, vm.load_map())
    after = load(overlay(operator_keys, service, instance))
    assert dig(after, new) == dig(before, old)
    assert dig(after, old) == dig(before, old)


def test_an_access_mode_becomes_the_list_the_thin_chart_reads(operator_keys: Path) -> None:
    vm.expand(operator_keys, vm.load_map())
    assert dig(load(overlay(operator_keys, "culvert", "default")), "writablePaths.pki.persistence.accessModes") == [
        "ReadWriteOnce"
    ]


def test_infra_common_takes_the_placement_rules_and_no_other(operator_keys: Path) -> None:
    hits = vm.expand(operator_keys, vm.load_map())
    common = [h for h in hits if h.file == "infra/common.yaml"]
    assert [h.rule.id for h in common] == ["55-node-selector"]
    assert load(operator_keys / "infra" / "common.yaml")["nodeSelector"] == {"dfe.example.com/pool": "dfe"}


def test_identity_is_the_220_name_for_every_per_config_app(carried: Path) -> None:
    for service, instance, _ in APPS:
        values = load(overlay(carried, service, instance))
        if service in ("dfe-fetcher", "dfe-transform-vrl", "dfe-transform-vector", "dfe-transform-elastic"):
            assert values["fullnameOverride"] == f"dfe-{values['component']}"
        else:
            assert "fullnameOverride" not in values


def test_otel_service_name_is_copied_for_every_app_that_set_one(carried: Path) -> None:
    for service, instance, _ in APPS:
        values = load(overlay(carried, service, instance))
        assert dig(values, "otel.serviceName") == values.get("otelServiceName")


def test_the_carried_files_read_back_as_the_220_lists(carried: Path) -> None:
    for service, instance in (("dfe-transform-vrl", "merakivrl"), ("dfe-transform-vector", "umbrellavec")):
        before, after = load(overlay(FIXTURES, service, instance)), load(overlay(carried, service, instance))
        assert dig(after, "fileSets.transforms.files") == before["transformFiles"]
        assert dig(after, "fileSets.enrichment.files") == before["enrichmentTables"]


def test_expand_twice_changes_nothing(carried: Path) -> None:
    before = {p.name: p.read_text(encoding="utf-8") for p in (carried / "values").iterdir()}
    hits = vm.expand(carried, vm.load_map())
    assert vm.pending(hits) == []
    assert {p.name: p.read_text(encoding="utf-8") for p in (carried / "values").iterdir()} == before


@pytest.mark.parametrize("width", [80, 40])
def test_an_overlay_in_pyyaml_s_own_style_carries_the_same(tmp_path: Path, width: int) -> None:
    """Indentless lists and folded quoted scalars, as a re-dumped or hand-edited overlay carries them."""
    root = _copy(tmp_path)
    for path in (root / "values").iterdir():
        path.write_text(yaml.safe_dump(load(path), sort_keys=False, width=width), encoding="utf-8")
    before = {path.name: load(path) for path in (root / "values").iterdir()}
    migration = vm.load_map()
    assert vm.refused(vm.expand(root, migration)) == []
    assert vm.pending(vm.expand(root, migration)) == []
    for path in (root / "values").iterdir():
        old, new = before[path.name], load(path)
        for rule in migration.rules:
            carried_value = dig(old, rule.sources[0])
            if rule.kind not in ("move", "copy") or carried_value in (None, {}, []):
                continue
            if rule.applies(old["deploy"]["service"], common=False):
                assert dig(new, rule.to) == carried_value, (path.name, rule.id)


def test_expand_removes_nothing_and_leaves_every_other_line_as_written(carried: Path) -> None:
    for path in (FIXTURES / "values").iterdir():
        before = path.read_text(encoding="utf-8").splitlines()
        after = (carried / "values" / path.name).read_text(encoding="utf-8").splitlines()
        it = iter(after)
        assert all(line in it for line in before), path.name


def test_an_author_entry_under_the_220_mount_moves_with_the_mount(tmp_path: Path) -> None:
    root = tmp_path / "deploy"
    write(root, "dfe-transform-vrl", "a", (
        "deploy:\n  service: dfe-transform-vrl\n  instance: a\ncomponent: transform-vrl-a\n"
        "config:\n  enrichment_tables:\n    - name: geo\n      path: /etc/dfe-transform-vrl-a-enrichment/geo.csv\n"
        "      key_columns: [ip]\n"
        "enrichmentTables:\n  - name: geo.csv\n    content: \"ip,cc\\n\"\n  - name: tz.csv\n    content: \"a\\n\"\n"
    ))
    vm.expand(root, vm.load_map())
    values = load(overlay(root, "dfe-transform-vrl", "a"))
    assert values["config"]["enrichment_tables"] == [
        {"name": "geo", "path": f"{ENRICHMENT}/geo.csv", "key_columns": ["ip"]},
        {"name": "tz", "path": f"{ENRICHMENT}/tz.csv"},
    ]
    assert values["enrichmentTablesDir"] == ENRICHMENT


def test_an_author_entry_under_another_instances_mount_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "deploy"
    text = (
        "deploy:\n  service: dfe-transform-vrl\n  instance: a\ncomponent: transform-vrl-a\n"
        "config:\n  enrichment_tables:\n    - name: geo\n      path: /etc/dfe-transform-vrl-b-enrichment/geo.csv\n"
        "enrichmentTables:\n  - name: geo.csv\n    content: \"ip,cc\\n\"\n"
    )
    path = write(root, "dfe-transform-vrl", "a", text)
    hits = vm.expand(root, vm.load_map())
    refused = vm.refused(hits)
    assert [h.rule.id for h in refused] == ["26-enrichment-entries"]
    assert "/etc/dfe-transform-vrl-b-enrichment/geo.csv" in refused[0].detail
    assert "enrichmentTablesDir" not in load(path)


def test_a_parent_written_on_one_line_is_refused_rather_than_rewritten(tmp_path: Path) -> None:
    root = tmp_path / "deploy"
    path = write(root, "dfe-fetcher", "x", (
        "deploy:\n  service: dfe-fetcher\n  instance: x\ncomponent: fetcher-x\n"
        "persistence:\n  enabled: true\nwritablePaths: {cursor: {persistence: {size: 1Gi}}}\n"
    ))
    hits = vm.expand(root, vm.load_map())
    assert [h.rule.id for h in vm.refused(hits)] == ["40-cursor-enabled"]
    assert "written on one line" in vm.refused(hits)[0].detail
    assert load(path)["writablePaths"] == {"cursor": {"persistence": {"size": "1Gi"}}}


def test_an_empty_flow_parent_is_opened_to_take_the_key(tmp_path: Path) -> None:
    root = tmp_path / "deploy"
    path = write(root, "dfe-fetcher", "x", (
        "deploy:\n  service: dfe-fetcher\n  instance: x\ncomponent: fetcher-x\notel: {}\notelServiceName: dfe-fetcher-x\n"
    ))
    vm.expand(root, vm.load_map())
    assert load(path)["otel"] == {"serviceName": "dfe-fetcher-x"}


def test_an_image_pin_is_refused_and_nothing_else_is_held_back(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    path = overlay(root, "dfe-receiver", "default")
    path.write_text(path.read_text(encoding="utf-8") + "image:\n  tag: v1.15.0\n", encoding="utf-8")
    hits = vm.expand(root, vm.load_map())
    assert [(h.file, h.rule.id) for h in vm.refused(hits)] == [("values/dfe-receiver-default-values.yaml", "70-image-pin")]
    assert "fullnameOverride" in load(overlay(root, "dfe-fetcher", "pypiwatch"))


def test_a_dropped_key_is_named_with_why_and_hyperdx_connections_as_config_loss(tmp_path: Path) -> None:
    root = tmp_path / "deploy"
    write(root, "hyperdx", "default", (
        "deploy:\n  service: hyperdx\n  instance: default\ndefaultConnections:\n  enabled: true\n"
        "  secretName: dfe-hyperdx-connections\n"
    ))
    write(root, "dfe-archiver", "default", OPERATOR_KEYS[("dfe-archiver", "default")])
    dropped = [h for h in vm.scan(root, vm.load_map()) if h.state == vm.DROPPED]
    assert {h.rule.id for h in dropped} == {"92-dropped-hyperdx-connections", "89-dropped-archiver-spool-path"}
    connections = next(h for h in dropped if h.rule.id.startswith("92"))
    assert "CONFIG LOSS" in connections.detail
    assert "DEFAULT_CONNECTIONS" in connections.detail


# ------------------------------------------------------------------ resync


def test_resync_carries_a_220_engine_edit_made_after_the_stage_push(tmp_path: Path) -> None:
    """Until the retarget the 2.2.0 engine writes transformFiles alone, so the new key follows the old."""
    root = _copy(tmp_path)
    migration = vm.load_map()
    vm.expand(root, migration)
    path = overlay(root, "dfe-transform-vrl", "merakivrl")
    edited = path.read_text(encoding="utf-8").replace(". = parse_json!(.message)", ". = parse_json!(.event)", 1)
    path.write_text(edited, encoding="utf-8")
    assert vm.pending(vm.expand(root, migration)) == []
    assert load(path)["fileSets"]["transforms"]["files"] != load(path)["transformFiles"]

    hits = vm.expand(root, migration, resync=True)
    assert [h.rule.id for h in hits if h.state == vm.RESYNC] == ["20-transform-files"]
    assert load(path)["fileSets"]["transforms"]["files"] == load(path)["transformFiles"]


def test_resync_drops_an_entry_for_a_table_the_220_engine_removed(tmp_path: Path) -> None:
    root = tmp_path / "deploy"
    path = write(root, "dfe-transform-vrl", "a", (
        "deploy:\n  service: dfe-transform-vrl\n  instance: a\ncomponent: transform-vrl-a\n"
        "enrichmentTables:\n  - name: geo.csv\n    content: \"ip,cc\\n\"\n  - name: tz.csv\n    content: \"a\\n\"\n"
    ))
    migration = vm.load_map()
    vm.expand(root, migration)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('  - name: tz.csv\n    content: "a\\n"\n', "", 1), encoding="utf-8")
    vm.expand(root, migration, resync=True)
    values = load(path)
    assert values["config"]["enrichment_tables"] == [{"name": "geo", "path": f"{ENRICHMENT}/geo.csv"}]
    assert values["fileSets"]["enrichment"]["files"] == values["enrichmentTables"]


# ------------------------------------------------------------------ the map


# Every key a 2.2.0 chart reads that the thin charts read nothing at, or read
# elsewhere: each 2.2.0 chart's values.yaml and templates against scalo-service,
# dfe-extras and argocd/values at 2.2.1-rc.9, plus DESIGN's moved keys that some
# rc.9 reader other than the thin chart still names.
FROZEN_220 = {
    "dfe-archiver": ["spool.path", "spool.sizeLimit", "service.pushPort", "keda.pressure.enabled",
                     "waitForEngine.enabled", "waitForEngine.resources", "otelServiceName", "nodeScheduling.nodeSelector",
                     "nodeScheduling.tolerations", "image.repository", "image.tag", "image.digest"],
    "dfe-engine": ["config.persistence.enabled", "config.persistence.size", "config.persistence.storageClass",
                   "service.port", "otelServiceName", "nodeScheduling.nodeSelector", "nodeScheduling.tolerations",
                   "image.repository", "image.tag", "image.digest"],
    "dfe-fetcher": ["component", "otelServiceName", "persistence.enabled", "persistence.size",
                    "persistence.storageClass", "imageComponent", "waitForEngine.enabled", "waitForEngine.resources",
                    "nodeScheduling.nodeSelector", "nodeScheduling.tolerations", "image.repository", "image.tag",
                    "image.digest"],
    "dfe-loader": ["keda.pressure.enabled", "waitForEngine.enabled", "waitForEngine.resources", "otelServiceName",
                   "nodeScheduling.nodeSelector", "nodeScheduling.tolerations", "image.repository", "image.tag",
                   "image.digest"],
    "dfe-receiver": ["receiver.buffer", "receiver.buffer.memoryLimit", "receiver.buffer.spillover",
                     "receiver.buffer.spillover.enabled", "receiver.buffer.spillover.path",
                     "receiver.buffer.spillover.sizeLimit", "receiver.listenAddress", "receiver.loaderPort",
                     "keda.pressure.enabled", "waitForEngine.enabled", "waitForEngine.resources", "otelServiceName",
                     "nodeScheduling.nodeSelector", "nodeScheduling.tolerations", "image.repository", "image.tag",
                     "image.digest"],
    "dfe-transform-elastic": ["component", "configMountPath", "otelServiceName", "imageComponent",
                              "service.pushPort", "keda.pressure.enabled", "waitForEngine.enabled",
                              "waitForEngine.resources", "nodeScheduling.nodeSelector", "nodeScheduling.tolerations",
                              "image.repository", "image.tag", "image.digest"],
    "dfe-transform-vector": ["component", "loaderPort", "otelServiceName", "transformFiles", "enrichmentTables",
                             "transformFilesDir", "enrichmentTablesDir", "kafka.autoOffsetReset", "imageComponent",
                             "service.pushPort", "keda.pressure.enabled", "waitForEngine.enabled",
                             "waitForEngine.resources", "nodeScheduling.nodeSelector", "nodeScheduling.tolerations",
                             "image.repository", "image.tag", "image.digest"],
    "dfe-transform-vrl": ["component", "loaderPort", "otelServiceName", "transformFiles", "enrichmentTables",
                          "transformFilesDir", "enrichmentTablesDir", "kafka.autoOffsetReset", "imageComponent",
                          "service.pushPort", "keda.pressure.enabled", "waitForEngine.enabled",
                          "waitForEngine.resources", "nodeScheduling.nodeSelector", "nodeScheduling.tolerations",
                          "image.repository", "image.tag", "image.digest"],
    "dfe-ui": ["service.port", "metricsPort", "otelServiceName", "nodeScheduling.nodeSelector",
               "nodeScheduling.tolerations", "image.repository", "image.tag", "image.digest"],
    "hyperdx": ["dashboards.resources", "dashboards.resources.limits.cpu", "dashboards.resources.limits.memory",
                "dashboards.resources.requests.cpu", "dashboards.resources.requests.memory",
                "defaultConnections.enabled", "defaultConnections.secretName", "otlp.enabled", "otlp.grpcPort",
                "otlp.httpPort", "otelServiceName", "nodeScheduling.nodeSelector", "nodeScheduling.tolerations",
                "image.repository", "image.tag", "image.digest"],
    "culvert": ["ipForward.enabled", "persistence.accessMode", "persistence.enabled", "persistence.size",
                "persistence.storageClass", "tunDevice.enabled", "tuning", "otelServiceName",
                "nodeScheduling.nodeSelector", "nodeScheduling.tolerations", "image.repository", "image.tag",
                "image.digest"],
}


def _covers(rule: vm.Rule, key: str) -> bool:
    return any(key == s or key.startswith(f"{s}.") or s.startswith(f"{key}.") for s in rule.sources)


@pytest.mark.parametrize("service", sorted(FROZEN_220))
def test_every_frozen_220_key_has_a_rule(service: str) -> None:
    rules = [r for r in vm.load_map().rules if "*" in r.apps or service in r.apps]
    missing = [key for key in FROZEN_220[service] if not any(_covers(r, key) for r in rules)]
    assert missing == []


def test_the_map_reads_the_same_through_the_stdlib_reader_and_yaml() -> None:
    """dfe-ops reads it with yaml_subset and the chart with fromYaml, so the two must agree."""
    migration = vm.load_map()
    parsed = yaml.safe_load(vm.MAP_FILE.read_text(encoding="utf-8"))
    assert parsed["since"] == migration.since
    assert sorted(parsed["rules"]) == [rule.id for rule in migration.rules]
    for rule in migration.rules:
        entry = parsed["rules"][rule.id]
        assert entry["kind"] == rule.kind
        assert tuple(s.strip() for s in str(entry["from"]).split(",")) == rule.sources
        assert entry.get("to", "") == rule.to
        assert str(entry.get("guard", "")) == rule.guard


def test_the_map_names_a_certified_stack() -> None:
    assert vm.load_map().since in u.load_versions_root()["stacks"]


def test_every_guarded_rule_moves_a_key_into_place() -> None:
    for rule in vm.load_map().rules:
        if rule.guard:
            assert rule.kind in ("move", "copy", "derive-identity"), rule.id


# ------------------------------------------------------------ migrate-values and plan


def _args(**fields: object) -> argparse.Namespace:
    base = {"deploy": "", "check": False}
    base.update(fields)
    return argparse.Namespace(**base)


def test_migrate_values_check_exits_1_until_the_keys_are_carried(deploy: Path, capsys: pytest.CaptureFixture) -> None:
    before = {p: p.read_text(encoding="utf-8") for p in (deploy / "values").iterdir()}
    assert u.cmd_upgrade_migrate_values(_args(deploy=str(deploy), check=True)) == u.EXIT_BLOCKED
    out = capsys.readouterr()
    assert "[MIGRATE] values/dfe-fetcher-pypiwatch-values.yaml: persistence.enabled -> " in out.out
    assert {p: p.read_text(encoding="utf-8") for p in before} == before

    assert u.cmd_upgrade_migrate_values(_args(deploy=str(deploy))) == u.EXIT_OK
    assert "commit and push them" in capsys.readouterr().err
    assert u.cmd_upgrade_migrate_values(_args(deploy=str(deploy), check=True)) == u.EXIT_OK
    assert "nothing to carry" in capsys.readouterr().err


def test_migrate_values_exits_1_on_a_key_it_cannot_carry(deploy: Path, capsys: pytest.CaptureFixture) -> None:
    path = overlay(deploy, "dfe-engine", "default")
    path.write_text(path.read_text(encoding="utf-8") + "image:\n  digest: sha256:abc\n", encoding="utf-8")
    assert u.cmd_upgrade_migrate_values(_args(deploy=str(deploy))) == u.EXIT_BLOCKED
    captured = capsys.readouterr()
    assert "[REFUSED] values/dfe-engine-default-values.yaml: image.digest" in captured.out
    assert "1 need a hand edit" in captured.err


def _plan(monkeypatch: pytest.MonkeyPatch, deploy: Path, tmp_path: Path) -> int:
    (deploy / "pins.yaml").write_text(f'base:\n  dfe-infra: "{FROM_STACK}"\n', encoding="utf-8")
    monkeypatch.setattr(u, "run_compat_check", lambda *_a, **_k: (True, "ok"))
    args = argparse.Namespace(
        deploy=str(deploy), to=TO_STACK, kubeconfig=None, argocd_namespace="argocd", cloud="local",
        dial=None, fixtures=None, live=False, out=str(tmp_path / "plan.md"),
    )
    return u.cmd_upgrade_plan(args)


def test_plan_lists_every_key_and_passes_when_apply_can_carry_them(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    assert _plan(monkeypatch, deploy, tmp_path) == u.EXIT_OK
    out = capsys.readouterr().out
    assert "## values migration (2.2.0 keys)" in out
    assert "[MIGRATE] values/dfe-transform-vrl-merakivrl-values.yaml: transformFiles -> fileSets.transforms.files" in out
    assert "[MIGRATE] values/culvert-default-values.yaml: persistence.enabled -> writablePaths.pki.persistence.enabled" in out


def test_plan_is_blocked_on_an_image_pin(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = overlay(deploy, "dfe-fetcher", "pypiwatch")
    path.write_text(path.read_text(encoding="utf-8") + "image:\n  tag: v1.4.0\n", encoding="utf-8")
    assert _plan(monkeypatch, deploy, tmp_path) == u.EXIT_BLOCKED
    captured = capsys.readouterr()
    assert "[REFUSED] values/dfe-fetcher-pypiwatch-values.yaml: image.tag -- the thin chart would run" in captured.out
    assert "values migration" in captured.err


def test_plan_names_the_on_prem_door_the_upgrade_drops(
    monkeypatch: pytest.MonkeyPatch, deploy: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    (deploy / "infra" / "common.yaml").unlink()
    assert _plan(monkeypatch, deploy, tmp_path) == u.EXIT_BLOCKED
    out = capsys.readouterr().out
    assert "door loadbalancer drops the receiver.{domain} route" in out
    assert "door gateway drops the load balancer" in out


@pytest.mark.parametrize(
    ("from_stack", "to_stack", "crosses"),
    [("2.2.0", "2.2.1-rc.9", True), ("2.2.1-rc.1", "2.2.1-rc.9", False), ("2.2.0-rc.14", "2.2.0", False)],
)
def test_the_migration_runs_only_across_the_first_thin_chart_stack(
    from_stack: str, to_stack: str, crosses: bool
) -> None:
    assert u.values_cutover(from_stack, to_stack, vm.load_map().since)[0] is crosses
