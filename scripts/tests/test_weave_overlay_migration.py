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


def _receiver(docs: list[dict]) -> None:
    balancers = [d for d in docs if d["kind"] == "Service" and d["spec"].get("type") == "LoadBalancer"]
    assert len(balancers) == 1
    assert balancers[0]["spec"]["loadBalancerSourceRanges"] == ["198.51.100.0/24"]
    assert all("SASL" not in name for name in _env(docs))


def _loader(docs: list[dict]) -> None:
    assert _env(docs)["DFE_LOADER__CLICKHOUSE__PASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == "pw"
    assert _pod(docs)["nodeSelector"]["dfe.example.com/pool"] == "loader"


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
    """Each fixture's deploy repo before and after the migration, and their renders, made once."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cache: dict[tuple[str, str, bool], list[dict]] = {}

    def repo(self, name: str, migrated: bool) -> Path:
        """The fixture's deploy repo, written on first use, and migrated where `migrated` says."""
        fixture = BY_NAME[name]
        repo = self.root / name / ("migrated" if migrated else "original")
        path = repo / "values" / f"{fixture.service}-{fixture.instance}-values.yaml"
        if not path.is_file():
            body = {
                "deploy": {"service": fixture.service, "instance": fixture.instance},
                **INSTANCE.get(fixture.service, {}).get(fixture.cloud, {}),
                **copy.deepcopy(fixture.overlay),
            }
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8", newline="\n")
            if migrated:
                plan = u.migrate_overlays(repo, write=True).plans[0]
                assert plan.conflicts == [], plan.conflicts
        return repo

    def overlay(self, name: str, migrated: bool) -> str:
        fixture = BY_NAME[name]
        values = self.repo(name, migrated) / "values"
        return (values / f"{fixture.service}-{fixture.instance}-values.yaml").read_text(encoding="utf-8")

    def render(self, name: str, which: str, migrated: bool) -> list[dict]:
        """One render of the original or migrated overlay: ``old`` (2.2.0) or ``new`` (thin)."""
        key = (name, which, migrated)
        if key not in self.cache:
            fixture = BY_NAME[name]
            options: dict = {"deploy_repo": self.repo(name, migrated), "instance": fixture.instance}
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
    doc = yaml.safe_load(renders.overlay(fixture.name, migrated=True))
    default = _default_name(fixture.service)
    fullname = doc["fullnameOverride"]
    return (
        _for_instance(found.added, default, fullname),
        _for_instance(found.removed, default, fullname),
    )


# --------------------------------------------------------------------- the gate


@pytest.mark.parametrize("name", list(BY_NAME))
def test_the_2_2_0_chart_renders_the_migrated_overlay_as_before(renders: Renders, name: str) -> None:
    assert renders.overlay(name, migrated=True) != renders.overlay(name, migrated=False)
    assert renders.render(name, "old", migrated=True) == renders.render(name, "old", migrated=False)


@pytest.mark.parametrize("name", list(BY_NAME))
def test_the_thin_chart_adopts_every_2_2_0_object_from_the_migrated_overlay(
    renders: Renders, name: str
) -> None:
    old = renders.render(name, "old", migrated=False)
    new = renders.render(name, "new", migrated=True)
    added, removed = _accepted(renders, BY_NAME[name])
    assert identity_problems(old, new, added, removed) == []
    assert weave()._claims(new) == weave()._claims(old)


@pytest.mark.parametrize("name", [f.name for f in FIXTURES if f.per_config])
def test_a_per_config_instance_loses_its_objects_without_the_migration(
    renders: Renders, name: str
) -> None:
    old = renders.render(name, "old", migrated=False)
    unmigrated = renders.render(name, "new", migrated=False)
    added, removed = _accepted(renders, BY_NAME[name])
    assert identity_problems(old, unmigrated, added, removed) != []


@pytest.mark.parametrize("name", [f.name for f in FIXTURES if f.expect])
def test_the_thin_chart_reads_what_the_migration_wrote(renders: Renders, name: str) -> None:
    new = renders.render(name, "new", migrated=True)
    for check in BY_NAME[name].expect:
        check(new)


def test_every_component_has_a_fixture() -> None:
    assert {f.service for f in FIXTURES} == set(COMPONENTS)


def test_the_fetcher_fixture_is_the_gates_own_hand_migrated_pair(renders: Renders) -> None:
    """test_weave_fetcher_culvert.py renders this overlay pair, and the migration reproduces it."""
    doc = yaml.safe_load(renders.overlay("fetcher-single", migrated=True))
    assert doc["fullnameOverride"] == "dfe-fetcher-acme"
    assert doc["writablePaths"] == {"cursor": {"persistence": {"enabled": True, "size": "2Gi"}}}


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
