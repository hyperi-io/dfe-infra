#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_weave_overlay_migration.py
#  Purpose:      Prove the dfe-ops upgrade overlay migration against real renders:
#                the 2.2.0 chart renders a migrated overlay exactly as before, and
#                the thin chart renders it with every 2.2.0 object and claim name.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The overlay migration under scripts/dfe-weave, per component.

    python3 -m pytest scripts/tests/test_weave_overlay_migration.py -q

Each fixture is a 2.2.0 overlay a deployment could hold, setting keys the value
map moves. migrate_overlays rewrites it in a scratch deploy repo, and then:

- the 2.2.0 chart renders the migrated overlay as it rendered the original, so a
  rollback to 2.2.0 needs nothing undone
- the thin chart renders the migrated overlay with every 2.2.0 (kind, name) and
  claim name, against the gate's accepted diffs named for the instance
- a per-config instance's thin render loses objects without the migration, so the
  migration is what keeps them
- the keys a note derives reach the thin chart where it reads them
- every pod and container securityContext the thin chart renders holds only
  Kubernetes fields, so no 2.2.0 key lands verbatim in a pod spec
- the enrichment-table entries written once the thin charts render reach the thin
  config file under the mounted set, and stripping them again gives the 2.2.0
  render back

No old key the migration keeps sits under a thin-chart schema object that refuses
it. Render (b) needs the scalo-service library (_weave.library).
"""

import copy
import functools
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from _gate import APPSETS, COMPONENTS, INSTANCE, accepts, identity_problems
from _weave import REPO_ROOT, contract, library, render_app, weave

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import dfe_ops_upgrade as u

DOMAIN = "dfe.example.com"
TOLERATION = {"key": "dfe.example.com/pool", "operator": "Exists", "effect": "NoSchedule"}

# The PodSecurityContext and SecurityContext fields of the Kubernetes 1.34 API, the
# platform floor versions.yaml declares.
POD_SECURITY_FIELDS = {
    "appArmorProfile", "fsGroup", "fsGroupChangePolicy", "runAsGroup", "runAsNonRoot",
    "runAsUser", "seLinuxChangePolicy", "seLinuxOptions", "seccompProfile",
    "supplementalGroups", "supplementalGroupsPolicy", "sysctls", "windowsOptions",
}
CONTAINER_SECURITY_FIELDS = {
    "allowPrivilegeEscalation", "appArmorProfile", "capabilities", "privileged", "procMount",
    "readOnlyRootFilesystem", "runAsGroup", "runAsNonRoot", "runAsUser", "seLinuxOptions",
    "seccompProfile", "windowsOptions",
}

# The table-by-table file set apps.yaml declares for dfe-transform-vrl, and where its thin
# chart mounts it.
TABLE_MOUNT = "/etc/dfe-transform-vrl-enrichment"
TABLES_MANIFEST = f"""
apps:
  dfe-transform-vrl:
    files:
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
# Each state a fixture's deploy repo passes through, and the step that makes it from the last.
STATES = ("original", "migrated", "named", "unnamed")


@dataclass(frozen=True)
class Fixture:
    """One overlay on one cell: the service's deploy.service, instance, profile and cloud."""

    name: str
    service: str
    profile: str
    cloud: str
    overlay: dict
    instance: str = "default"
    per_config: bool = False
    expect: tuple[Callable[[list[dict]], None], ...] = field(default=())


def _named(docs: list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == kind}


def _pod(docs: list[dict]) -> dict:
    return next(iter(_named(docs, "Deployment").values()))["spec"]["template"]["spec"]


def _env(docs: list[dict]) -> dict[str, dict]:
    return {e["name"]: e for e in _pod(docs)["containers"][0].get("env") or []}


def _ui(docs: list[dict]) -> None:
    env = _env(docs)
    assert env["NEXTAUTH_URL"]["value"] == f"https://console.{DOMAIN}"
    assert env["HYPERDX_URL"]["value"] == f"https://observe.{DOMAIN}"
    assert env["NODE_OPTIONS"]["value"] == "--max-old-space-size-percentage=60"
    assert _pod(docs)["nodeSelector"]["dfe.example.com/pool"] == "ui"


def _hyperdx(docs: list[dict]) -> None:
    assert not _pod(docs).get("initContainers")
    assert TOLERATION in _pod(docs)["tolerations"]
    session = _env(docs)["EXPRESS_SESSION_SECRET"]["valueFrom"]["secretKeyRef"]
    assert session["key"] == "session"
    assert _env(docs)["MONGO_PASSWORD"] == {"name": "MONGO_PASSWORD", "value": ""}


def _receiver(docs: list[dict]) -> None:
    balancers = [d for d in docs if d["kind"] == "Service" and d["spec"].get("type") == "LoadBalancer"]
    assert len(balancers) == 1
    assert balancers[0]["spec"]["loadBalancerSourceRanges"] == ["198.51.100.0/24"]
    assert all("SASL" not in name for name in _env(docs))


def _loader(docs: list[dict]) -> None:
    assert _env(docs)["DFE_LOADER__CLICKHOUSE__PASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == "pw"
    assert _pod(docs)["nodeSelector"]["dfe.example.com/pool"] == "loader"
    assert _pod(docs)["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    container = _pod(docs)["containers"][0]["securityContext"]
    assert container["capabilities"]["add"] == ["NET_BIND_SERVICE"]


def _archiver(docs: list[dict]) -> None:
    spool = next(v for v in _pod(docs)["volumes"] if v["name"] == "writable-spool")
    assert spool["emptyDir"]["sizeLimit"] == "3Gi"
    assert not [e for e in _env(docs).values() if "S3" in json.dumps(e.get("valueFrom") or {})]


def _config_files(docs: list[dict]) -> list[dict]:
    found = []
    for configmap in _named(docs, "ConfigMap").values():
        for text in (configmap.get("data") or {}).values():
            parsed = yaml.safe_load(text) if isinstance(text, str) else None
            if isinstance(parsed, dict):
                found.append(parsed)
    return found


def _vrl_tls(docs: list[dict]) -> None:
    files = [f for f in _config_files(docs) if "source" in f and "sink" in f]
    assert files
    assert all(f["source"]["tls"]["enabled"] is True and f["sink"]["tls"]["enabled"] is True for f in files)


def _otel_name(name: str) -> Callable[[list[dict]], None]:
    def check(docs: list[dict]) -> None:
        assert _env(docs)["OTEL_SERVICE_NAME"]["value"] == name

    return check


def _claim_size(name: str, size: str, modes: list[str] | None = None) -> Callable[[list[dict]], None]:
    def check(docs: list[dict]) -> None:
        spec = _named(docs, "PersistentVolumeClaim")[name]["spec"]
        assert spec["resources"]["requests"]["storage"] == size
        if modes is not None:
            assert spec["accessModes"] == modes

    return check


def _engine(docs: list[dict]) -> None:
    env = _env(docs)
    ref = env["DFE_OIDC_GOOGLE_CLIENT_ID"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "dfe-oidc-google", "key": "client-id"}
    assert env["DFE_API_JWT_SECRET"]["valueFrom"]["secretKeyRef"]["key"] == "jwt"
    assert "DFE_CLICKHOUSE_HUNT_RUNNER_PASSWORD" not in env


OIDC = {
    "enabled": True,
    "providers": [
        {
            "name": "google",
            "secretName": "dfe-oidc-google",
            "envMappings": {"DFE_OIDC_GOOGLE_CLIENT_ID": "client-id", "DFE_OIDC_GOOGLE_CLIENT_SECRET": "client-secret"},
        }
    ],
}
TRANSFORMS = [{"name": "route.vrl", "content": ". = .\n"}]
TABLES = [{"name": "geo.csv", "content": "a,b\n1,2\n"}]

FIXTURES = [
    *(
        Fixture(
            f"ui-{profile}-{cloud}",
            "dfe-ui",
            profile,
            cloud,
            {
                "config": {
                    "hostname": "console",
                    "hyperdxHostname": "observe",
                    "nodeOptions": "--max-old-space-size-percentage=60",
                },
                "nodeScheduling": {"nodeSelector": {"dfe.example.com/pool": "ui"}},
                "podSecurityContext": {"seccompProfileType": "RuntimeDefault"},
            },
            expect=(_ui,),
        )
        for profile, cloud in (("single", "aws"), ("slim", "local"))
    ),
    Fixture(
        "hyperdx",
        "hyperdx",
        "scale",
        "aws",
        {
            "sessionSecret": {"key": "session"},
            "tokenEncryption": {"key": "encryption"},
            "mongodb": {"passwordSecretName": "", "uri": "mongodb://u:p@mongo.example.com/hyperdx"},
            "dashboards": {"enabled": False},
            "nodeScheduling": {"tolerations": [TOLERATION]},
        },
        expect=(_hyperdx,),
    ),
    Fixture(
        "receiver",
        "dfe-receiver",
        "scale",
        "aws",
        {
            "exposure": {"mode": "public", "public": {"loadBalancerSourceRanges": ["198.51.100.0/24"]}},
            "receiver": {"buffer": {"memoryLimit": "256MiB", "spillover": {"enabled": True, "sizeLimit": "5Gi"}}},
            "keda": {"pressure": {"enabled": False}},
            "kafka": {"saslSecretName": ""},
        },
        expect=(_receiver,),
    ),
    Fixture(
        "loader",
        "dfe-loader",
        "scale",
        "aws",
        {
            "clickhouse": {"passwordSecretKey": "pw"},
            "keda": {"pressure": {"enabled": False}},
            "nodeScheduling": {"nodeSelector": {"dfe.example.com/pool": "loader"}},
            # Every podSecurityContext and containerSecurityContext key a 2.2.0 chart reads.
            "podSecurityContext": {
                "enabled": True,
                "runAsNonRoot": True,
                "runAsUser": 10001,
                "runAsGroup": 10001,
                "fsGroup": 10001,
                "seccompProfileType": "RuntimeDefault",
            },
            "containerSecurityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"add": ["NET_BIND_SERVICE"]},
            },
        },
        expect=(_loader,),
    ),
    Fixture(
        "archiver",
        "dfe-archiver",
        "scale",
        "aws",
        {"spool": {"sizeLimit": "3Gi"}, "s3": {"secretName": ""}, "waitForEngine": {"enabled": False}},
        expect=(_archiver,),
    ),
    Fixture(
        "vrl-bus",
        "dfe-transform-vrl",
        "scale",
        "aws",
        {
            "component": "transform-vrl-acme",
            "otelServiceName": "dfe-transform-vrl-acme",
            "transformFiles": TRANSFORMS,
            "enrichmentTables": TABLES,
            "kafka": {"securityProtocol": "SASL_SSL"},
        },
        instance="acme",
        per_config=True,
        expect=(_vrl_tls, _otel_name("dfe-transform-vrl-acme")),
    ),
    Fixture(
        "vrl-direct",
        "dfe-transform-vrl",
        "slim",
        "local",
        {
            "component": "transform-vrl-acme",
            "otelServiceName": "dfe-transform-vrl-acme",
            "transformFiles": TRANSFORMS,
        },
        instance="acme",
        per_config=True,
        expect=(_otel_name("dfe-transform-vrl-acme"),),
    ),
    Fixture(
        "vrl-project",
        "dfe-transform-vrl",
        "scale",
        "aws",
        # dfe-extras refuses any project but dfe, so the explicit key carries the default.
        {"project": "dfe", "component": "transform-vrl-acme", "transformFiles": TRANSFORMS},
        instance="acme",
        per_config=True,
    ),
    Fixture(
        "vector",
        "dfe-transform-vector",
        "scale",
        "aws",
        {
            "component": "transform-vector-acme",
            "otelServiceName": "dfe-transform-vector-acme",
            "transformFiles": TRANSFORMS,
            "enrichmentTables": TABLES,
        },
        instance="acme",
        per_config=True,
        expect=(_otel_name("dfe-transform-vector-acme"),),
    ),
    Fixture(
        "elastic",
        "dfe-transform-elastic",
        "scale",
        "aws",
        {
            "component": "transform-elastic-acme",
            "otelServiceName": "dfe-transform-elastic-acme",
            "kafka": {"securityProtocol": ""},
        },
        instance="acme",
        per_config=True,
        expect=(_otel_name("dfe-transform-elastic-acme"),),
    ),
    *(
        Fixture(
            f"fetcher-{profile}",
            "dfe-fetcher",
            profile,
            "aws",
            {
                "component": "fetcher-acme",
                "otelServiceName": "dfe-fetcher-acme",
                "persistence": {"enabled": True, "size": "2Gi"},
            },
            instance="acme",
            per_config=True,
            expect=(_claim_size("dfe-fetcher-acme-cursor", "2Gi"), _otel_name("dfe-fetcher-acme")),
        )
        for profile in ("single", "mesh")
    ),
    Fixture(
        "culvert-local",
        "culvert",
        "single",
        "local",
        {
            "persistence": {"enabled": True, "size": "2Gi", "accessMode": "ReadWriteOnce"},
            "tuning": {"keepalive": 25},
        },
        expect=(_claim_size("dfe-culvert-pki", "2Gi", ["ReadWriteOnce"]),),
    ),
    Fixture(
        "culvert-aws",
        "culvert",
        "scale",
        "aws",
        {"persistence": {"enabled": True, "accessMode": "ReadWriteOnce"}},
        expect=(_claim_size("dfe-culvert-pki", "1Gi", ["ReadWriteOnce"]),),
    ),
    Fixture(
        "engine",
        "dfe-engine",
        "scale",
        "aws",
        {
            "config": {"persistence": {"enabled": True, "size": "5Gi", "storageClass": ""}},
            "auth": {"jwtSecretKey": "jwt"},
            "oidc": OIDC,
            "huntRunner": {"enabled": False},
            "nodeScheduling": {"tolerations": [TOLERATION]},
        },
        expect=(_engine, _claim_size("dfe-engine-config", "5Gi")),
    ),
]
BY_NAME = {f.name: f for f in FIXTURES}


# ------------------------------------------------------------------- the renders


class Renders:
    """Each fixture's deploy repo in every state of STATES, and their renders, made once.

    `migrated` is the overlay-vocabulary stage's work, `named` the enrichment-tables
    stage's after it, and `unnamed` a rollback's strip of those entries.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest = root / "apps.yaml"
        self.manifest.write_text(TABLES_MANIFEST, encoding="utf-8")
        self.cache: dict[tuple[str, str, str], list[dict]] = {}

    def _file(self, name: str, state: str) -> Path:
        fixture = BY_NAME[name]
        return self.root / name / state / "values" / f"{fixture.service}-{fixture.instance}-values.yaml"

    def repo(self, name: str, state: str) -> Path:
        """The fixture's deploy repo in one state, made from the state before it on first use."""
        path = self._file(name, state)
        repo = path.parent.parent
        if path.is_file():
            return repo
        path.parent.mkdir(parents=True)
        if state == "original":
            fixture = BY_NAME[name]
            body = {
                "deploy": {"service": fixture.service, "instance": fixture.instance},
                **INSTANCE.get(fixture.service, {}).get(fixture.cloud, {}),
                **copy.deepcopy(fixture.overlay),
            }
            path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8", newline="\n")
            return repo
        previous = STATES[STATES.index(state) - 1]
        self.repo(name, previous)
        path.write_bytes(self._file(name, previous).read_bytes())
        if state == "migrated":
            plan = u.migrate_overlays(repo, write=True).plans[0]
            assert plan.conflicts == [], plan.conflicts
        elif state == "named":
            u.name_tables(repo, write=True, manifest=self.manifest)
        else:
            u.unname_tables(repo, write=True, manifest=self.manifest)
        return repo

    def overlay(self, name: str, state: str) -> str:
        self.repo(name, state)
        return self._file(name, state).read_text(encoding="utf-8")

    def render(self, name: str, which: str, state: str) -> list[dict]:
        """One render of the overlay in one state: ``old`` (2.2.0) or ``new`` (thin)."""
        key = (name, which, state)
        if key not in self.cache:
            fixture = BY_NAME[name]
            options: dict = {"deploy_repo": self.repo(name, state), "instance": fixture.instance}
            if fixture.service in APPSETS:
                options["appset"] = APPSETS[fixture.service]
            self.cache[key] = render_app(
                fixture.service, fixture.profile, fixture.cloud, which, **options
            )
        return self.cache[key]


@pytest.fixture(scope="module")
def renders(tmp_path_factory: pytest.TempPathFactory) -> Renders:
    return Renders(tmp_path_factory.mktemp("overlay-migration"))


def _default_name(service: str) -> str:
    return json.loads(contract(service).read_text(encoding="utf-8"))["app_name"]


def _for_instance(names: dict[str, str], default: str, fullname: str) -> dict[str, str]:
    """The gate's accepted objects, named for an instance where they carry the chart's name."""
    found = {}
    for obj, reason in names.items():
        kind, _, name = obj.partition("/")
        if name == default or name.startswith(f"{default}-"):
            name = fullname + name[len(default) :]
        found[f"{kind}/{name}"] = reason
    return found


def _accepted(renders: Renders, fixture: Fixture) -> tuple[dict[str, str], dict[str, str]]:
    found = accepts(fixture.service)
    if not fixture.per_config:
        return found.added, found.removed
    doc = yaml.safe_load(renders.overlay(fixture.name, "migrated"))
    default = _default_name(fixture.service)
    fullname = doc["fullnameOverride"]
    return (
        _for_instance(found.added, default, fullname),
        _for_instance(found.removed, default, fullname),
    )


# --------------------------------------------------------------------- the gate


@pytest.mark.parametrize("name", list(BY_NAME))
def test_the_2_2_0_chart_renders_the_migrated_overlay_as_before(renders: Renders, name: str) -> None:
    assert renders.overlay(name, "migrated") != renders.overlay(name, "original")
    assert renders.render(name, "old", "migrated") == renders.render(name, "old", "original")


@pytest.mark.parametrize("name", list(BY_NAME))
def test_the_thin_chart_adopts_every_2_2_0_object_from_the_migrated_overlay(
    renders: Renders, name: str
) -> None:
    old = renders.render(name, "old", "original")
    new = renders.render(name, "new", "named")
    added, removed = _accepted(renders, BY_NAME[name])
    assert identity_problems(old, new, added, removed) == []
    assert weave()._claims(new) == weave()._claims(old)


@pytest.mark.parametrize("name", [f.name for f in FIXTURES if f.per_config])
def test_a_per_config_instance_loses_its_objects_without_the_migration(
    renders: Renders, name: str
) -> None:
    old = renders.render(name, "old", "original")
    unmigrated = renders.render(name, "new", "original")
    added, removed = _accepted(renders, BY_NAME[name])
    assert identity_problems(old, unmigrated, added, removed) != []


@pytest.mark.parametrize("name", [f.name for f in FIXTURES if f.expect])
def test_the_thin_chart_reads_what_the_migration_wrote(renders: Renders, name: str) -> None:
    new = renders.render(name, "new", "named")
    for check in BY_NAME[name].expect:
        check(new)


def test_every_component_has_a_fixture() -> None:
    assert {f.service for f in FIXTURES} == set(COMPONENTS)


def test_the_fetcher_fixture_is_the_gates_own_hand_migrated_pair(renders: Renders) -> None:
    """test_weave_fetcher_culvert.py renders this overlay pair, and the migration reproduces it."""
    doc = yaml.safe_load(renders.overlay("fetcher-single", "migrated"))
    assert doc["fullnameOverride"] == "dfe-fetcher-acme"
    assert doc["writablePaths"] == {"cursor": {"persistence": {"enabled": True, "size": "2Gi"}}}


# ------------------------------------------------------------ security contexts


def unknown_security_fields(docs: list[dict]) -> list[str]:
    """Each pod or container securityContext key that is not a Kubernetes field; empty is a pass."""
    found = []
    for doc in docs:
        template = (doc.get("spec") or {}).get("template") or {}
        pod = template.get("spec") or {}
        where = f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')}"
        pod_context = pod.get("securityContext") or {}
        found += [f"{where} {key}" for key in pod_context if key not in POD_SECURITY_FIELDS]
        for container in (pod.get("initContainers") or []) + (pod.get("containers") or []):
            context = container.get("securityContext") or {}
            found += [
                f"{where} {container.get('name')} {key}"
                for key in context
                if key not in CONTAINER_SECURITY_FIELDS
            ]
    return found


@pytest.mark.parametrize("name", list(BY_NAME))
def test_no_2_2_0_key_lands_in_a_thin_pod_spec(renders: Renders, name: str) -> None:
    assert unknown_security_fields(renders.render(name, "new", "named")) == []


def test_every_2_2_0_security_key_is_a_kubernetes_field_or_moves_out() -> None:
    """scalo-service merges both blocks into the pod whole, less podSecurityContext.enabled,
    which it reads and takes out first."""
    blocks = {
        "podSecurityContext": POD_SECURITY_FIELDS | {"enabled"},
        "containerSecurityContext": CONTAINER_SECURITY_FIELDS,
    }
    kept = []
    for service, app in u.load_value_map().items():
        for entry in app.entries:
            block, _, rest = entry.key.partition(".")
            fields = blocks.get(block)
            if fields and rest and rest.split(".")[0] not in fields and entry.migrate != u.MOVE:
                kept.append(f"{service} {entry.key}")
    assert kept == []


def test_a_2_2_0_key_left_in_place_is_seen(renders: Renders) -> None:
    """The original dfe-ui overlay keeps podSecurityContext.seccompProfileType, which the
    thin chart merges into the pod verbatim."""
    found = unknown_security_fields(renders.render("ui-single-aws", "new", "original"))
    assert found == ["Deployment/dfe-ui seccompProfileType"]


# ------------------------------------------------------------- enrichment tables


def _table_entries(docs: list[dict]) -> list[dict]:
    files = [f for f in _config_files(docs) if "source" in f and "sink" in f]
    assert len(files) == 1
    return files[0].get("enrichment_tables") or []


def _mounts(docs: list[dict]) -> set[str]:
    return {m["mountPath"] for m in _pod(docs)["containers"][0].get("volumeMounts") or []}


def test_the_table_entries_name_each_file_where_the_thin_chart_mounts_it(renders: Renders) -> None:
    new = renders.render("vrl-bus", "new", "named")
    assert _table_entries(new) == [{"name": "geo", "path": f"{TABLE_MOUNT}/geo.csv"}]
    assert TABLE_MOUNT in _mounts(new)
    assert _table_entries(renders.render("vrl-bus", "new", "migrated")) == []


def test_the_table_entries_wait_for_the_switch_because_2_2_0_reads_them(renders: Renders) -> None:
    """2.2.0 renders a declared entry as written and derives none of its own beside it."""
    original = renders.render("vrl-bus", "old", "original")
    named = renders.render("vrl-bus", "old", "named")
    assert _table_entries(original) == [
        {"name": "geo", "path": "/etc/dfe-transform-vrl-acme-enrichment/geo.csv"}
    ]
    assert _table_entries(named) == [{"name": "geo", "path": f"{TABLE_MOUNT}/geo.csv"}]


def test_a_rollback_strip_gives_the_2_2_0_render_back(renders: Renders) -> None:
    assert yaml.safe_load(renders.overlay("vrl-bus", "unnamed")) == yaml.safe_load(
        renders.overlay("vrl-bus", "migrated")
    )
    assert renders.render("vrl-bus", "old", "unnamed") == renders.render("vrl-bus", "old", "original")


def test_a_set_with_no_entries_path_is_left_alone(renders: Renders) -> None:
    assert renders.overlay("vector", "named") == renders.overlay("vector", "migrated")


def test_the_table_mount_is_the_one_the_integration_values_mount() -> None:
    values = REPO_ROOT / "argocd" / "values" / "apps" / "dfe-transform-vrl" / "values.yaml"
    integration = yaml.safe_load(values.read_text(encoding="utf-8"))
    assert integration["fileSets"]["enrichment"]["mountPath"] == TABLE_MOUNT


# ---------------------------------------------------------- old keys, thin schema


def _resolve(node: dict, root: dict) -> dict:
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        target: object = root
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
    return node if isinstance(node, dict) else {}


def refused(schema: dict, path: str) -> str:
    """Why the thin chart's values schema refuses a value at `path`, or empty where it takes one."""
    node = schema
    walked: list[str] = []
    for part in path.split("."):
        node = _resolve(node, schema)
        kinds = node.get("type")
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if walked and None not in kinds and "object" not in kinds:
            return f"{'.'.join(walked)} is {node.get('type')}, so it holds no {part}"
        properties = node.get("properties") or {}
        extra = node.get("additionalProperties")
        if part in properties:
            node = properties[part]
        elif extra is False:
            return f"{'.'.join(walked) or 'the root'} takes no {part}"
        else:
            node = extra if isinstance(extra, dict) else {}
        walked.append(part)
    return ""


@functools.cache
def thin_schema(service: str) -> dict:
    """The values schema dfe-weave assembles for a component's thin chart."""
    w = weave()
    base = json.loads((library() / "skeleton" / "values.schema.json").read_text(encoding="utf-8"))
    config = json.loads(contract(service).read_text(encoding="utf-8")).get("config_schema")
    config = config if isinstance(config, dict) else {}
    return w.values_schema(config, w.find_dials(config), base)


@pytest.mark.parametrize("service", COMPONENTS)
def test_no_old_key_the_migration_keeps_is_refused_by_the_thin_schema(service: str) -> None:
    schema = thin_schema(service)
    reasons = {e.key: refused(schema, e.key) for e in u.load_value_map()[service].entries}
    assert {key: why for key, why in reasons.items() if why} == {}


def test_a_key_under_a_closed_object_is_refused() -> None:
    schema = {
        "type": "object",
        "$defs": {"closed": {"type": "object", "properties": {"a": {}}, "additionalProperties": False}},
        "properties": {"box": {"$ref": "#/$defs/closed"}, "n": {"type": "integer"}},
    }
    assert refused(schema, "box.a") == ""
    assert refused(schema, "box.b") == "box takes no b"
    assert refused(schema, "n.x") == "n is integer, so it holds no x"
    assert refused(schema, "free.x") == ""
