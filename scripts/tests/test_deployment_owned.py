#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_deployment_owned.py
#  Purpose:      Hold apps.yaml's deployment_owned paths and file sets equal to
#                what the thin-chart integration values and dfe-extras set.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""apps.yaml against the thin-chart integration values, in both directions.

    python3 -m pytest scripts/tests/test_deployment_owned.py -q

dfe-engine reads `deployment_owned` to report a config path as the deployment's
and refuse a write to it, naming what sets it. That holds only while the
declaration matches argocd/values/apps and the dfe-extras env ConfigMaps, so
this fails when:

- an integration file sets a configOverrides leaf, or an env var reaching the
  app's config, that `deployment_owned` does not list
- `deployment_owned` names a supplier nothing sets
- a `deployment_owned_when` gate names a values path no integration file holds
- a file set's values path or mount path is not the chart's fileSets entry

An env var reaches the app's config unless NOT_APP_CONFIG says otherwise, so a
new name fails here until someone decides which it is. The env ConfigMaps are
rendered through test_dfe_extras.render_pair, with helm.
"""

import functools
import json
import re
from pathlib import Path

import pytest
import yaml
from test_dfe_extras import ENV_CONFIGMAP, PROFILES, chart_name, render_pair

from _weave import CONTRACTS, REPO_ROOT

MANIFEST = REPO_ROOT / "apps.yaml"
APPS_VALUES = REPO_ROOT / "argocd" / "values" / "apps"
VALUE_MAP = REPO_ROOT / "scripts" / "weave" / "value-map.yaml"

# The env-name shape dfe-engine tells a supplier env var from a values path by.
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Env vars an integration file sets that reach nothing in the app's own config
# file, by app ("*" for every app), each with what reads it instead.
NOT_APP_CONFIG: dict[str, dict[str, str]] = {
    "*": {"OTEL_EXPORTER_OTLP_ENDPOINT": "the OTel SDK's exporter"},
    "dfe-loader": {"SSL_CERT_FILE": "the TLS stack's trust store"},
    "dfe-transform-elastic": {
        name: "scalo's KafkaConfig::from_env, outside the config file"
        for name in (
            "KAFKA_BOOTSTRAP_SERVERS",
            "KAFKA_CONSUMER_PROTOCOL",
            "KAFKA_SECURITY_PROTOCOL",
            "KAFKA_SASL_MECHANISM",
            "KAFKA_SASL_USERNAME",
            "KAFKA_SASL_PASSWORD",
        )
    },
}

# Env vars that render empty once the overlay sets the config path they supply,
# so the overlay's value is the one the app reads and the path stays writable.
YIELDS_TO_THE_OVERLAY: dict[str, dict[str, str]] = {
    "dfe-fetcher": {"DFE_FETCHER_KAFKA_BROKERS": "config.kafka.brokers"},
}

# The env ConfigMaps carry some variables only on a TLS broker listener.
INFRA_VARIANTS = (None, {"kafka": {"securityProtocol": "SASL_SSL"}})
CLOUDS = ("local", "aws")

_MISSING = object()


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@functools.cache
def _apps() -> dict[str, dict]:
    return {name: raw or {} for name, raw in _load(MANIFEST)["apps"].items()}


def _config_apps() -> list[str]:
    """The apps whose config file dfe-engine writes, which is what it can refuse."""
    return sorted(name for name, app in _apps().items() if app.get("consumes"))


def _integration_files(service: str) -> list[Path]:
    return [APPS_VALUES / "_common.yaml", *sorted((APPS_VALUES / service).glob("*.yaml"))]


def _dig(doc: object, path: str) -> object:
    for part in path.split("."):
        if not isinstance(doc, dict) or part not in doc:
            return _MISSING
        doc = doc[part]
    return doc


def _leaves(node: object, prefix: str = "") -> set[str]:
    """Every dotted path a mapping sets a non-mapping value at."""
    if not isinstance(node, dict):
        return {prefix} if prefix else set()
    found: set[str] = set()
    for key, value in node.items():
        found |= _leaves(value, f"{prefix}.{key}" if prefix else str(key))
    return found


def _config_overrides(service: str) -> set[str]:
    found: set[str] = set()
    for path in _integration_files(service):
        found |= _leaves(_load(path).get("configOverrides") or {})
    return found


def _extra_env(service: str) -> set[str]:
    found: set[str] = set()
    for path in _integration_files(service):
        found |= set(_load(path).get("extraEnv") or {})
    return found


def _group_enabled(service: str, group: str) -> bool:
    """Whether some profile leaves a contract secret group on, as the files layer it."""
    key = f"secrets.{group}.enabled"
    base = _dig(_load(APPS_VALUES / service / "values.yaml"), key)
    for profile in PROFILES:
        path = APPS_VALUES / service / f"profile-{profile}.yaml"
        layered = _dig(_load(path), key) if path.is_file() else _MISSING
        if (base if layered is _MISSING else layered) is not False:
            return True
    return False


def _secret_env(service: str, *, required_only: bool) -> set[str]:
    """The env vars the contract's secret groups set where a profile leaves them on.

    A required group's Secret has to exist for the pod to start, so its variables
    are always set; an optional one's only when a deployer creates the Secret.
    """
    contract = json.loads((CONTRACTS / f"{service}.json").read_text(encoding="utf-8"))
    found: set[str] = set()
    for group in contract.get("secrets") or []:
        if required_only and group.get("optional", False):
            continue
        if _group_enabled(service, group["group_name"]):
            found |= {var["env_var"] for var in group["env_vars"]}
    return found


@functools.cache
def _env_configmap(service: str) -> frozenset[str]:
    """Every name the service's dfe-extras <fullname>-env ConfigMap sets, any profile."""
    if chart_name(service) not in ENV_CONFIGMAP:
        return frozenset()
    found: set[str] = set()
    for profile in PROFILES:
        for cloud in CLOUDS:
            for infra in INFRA_VARIANTS:
                extras = render_pair(service, profile, cloud, infra=infra, old=False).extras
                for (kind, name), doc in extras.items():
                    if kind == "ConfigMap" and name.endswith("-env"):
                        found |= set(doc.get("data") or {})
    return frozenset(found)


def _not_app_config(service: str) -> dict[str, str]:
    return {**NOT_APP_CONFIG["*"], **NOT_APP_CONFIG.get(service, {})}


# ----------------------------------------------------------- direction one


@pytest.mark.parametrize("service", _config_apps())
def test_every_config_override_the_deployment_sets_is_listed(service: str) -> None:
    owned = set(_apps()[service].get("deployment_owned") or {})
    overrides = sorted(f"config.{leaf}" for leaf in _config_overrides(service))
    missing = [path for path in overrides if path not in owned]
    assert missing == [], (
        f"{service}: argocd/values/apps sets configOverrides for {missing}, which "
        "apps.yaml deployment_owned does not list -- the engine would let a write "
        "through that changes nothing"
    )


@pytest.mark.parametrize("service", _config_apps())
def test_every_env_var_reaching_the_config_is_listed(service: str) -> None:
    suppliers = set((_apps()[service].get("deployment_owned") or {}).values())
    set_here = (
        _extra_env(service) | _env_configmap(service) | _secret_env(service, required_only=True)
    )
    yields = set(YIELDS_TO_THE_OVERLAY.get(service, {}))
    missing = sorted(set_here - suppliers - set(_not_app_config(service)) - yields)
    assert missing == [], (
        f"{service}: the deployment sets {missing}, which no apps.yaml "
        "deployment_owned entry names -- list the config path each sets, or add it "
        "to NOT_APP_CONFIG with what reads it"
    )


@pytest.mark.parametrize("service", sorted(YIELDS_TO_THE_OVERLAY))
def test_a_variable_that_yields_to_the_overlay_leaves_its_path_writable(service: str) -> None:
    owned = set(_apps()[service].get("deployment_owned") or {})
    for name, path in YIELDS_TO_THE_OVERLAY[service].items():
        assert name in _extra_env(service), f"{service}: nothing sets {name} any more"
        assert path not in owned, f"{service}: {path} is listed, so its write is refused"


# ----------------------------------------------------------- direction two


@pytest.mark.parametrize("service", _config_apps())
def test_every_supplier_is_one_the_deployment_sets(service: str) -> None:
    owned = _apps()[service].get("deployment_owned") or {}
    env = (
        _extra_env(service) | _env_configmap(service) | _secret_env(service, required_only=False)
    )
    documents = [_load(path) for path in _integration_files(service)]
    unset = []
    for path, supplier in sorted(owned.items()):
        if ENV_NAME.match(supplier):
            found = supplier in env
        else:
            found = any(_dig(doc, supplier) is not _MISSING for doc in documents)
        if not found:
            unset.append(f"{path}: {supplier}")
    assert unset == [], (
        f"{service}: apps.yaml deployment_owned names suppliers nothing in "
        f"argocd/values/apps, the dfe-extras env ConfigMap or the contract sets: {unset}"
    )


@pytest.mark.parametrize("service", _config_apps())
def test_every_gate_is_a_value_the_integration_files_hold(service: str) -> None:
    gates = _apps()[service].get("deployment_owned_when") or {}
    documents = [_load(path) for path in _integration_files(service)]
    unknown = sorted(
        f"{path}: {gate}"
        for path, gate in gates.items()
        if all(_dig(doc, gate) is _MISSING for doc in documents)
    )
    assert unknown == [], f"{service}: gates on values no integration file holds: {unknown}"


def test_only_an_app_whose_config_the_engine_writes_owns_config_paths() -> None:
    owners = {
        name
        for name, app in _apps().items()
        if app.get("deployment_owned") or app.get("deployment_owned_when")
    }
    assert owners <= set(_config_apps())


# ----------------------------------------------------------------- file sets


def _file_sets() -> list[tuple[str, dict]]:
    return [(name, fs) for name, app in _apps().items() for fs in app.get("files") or []]


@pytest.mark.parametrize(
    ("service", "file_set"),
    _file_sets(),
    ids=[f"{name}-{fs['name']}" for name, fs in _file_sets()],
)
def test_a_file_set_is_the_thin_chart_s_mounted_set(service: str, file_set: dict) -> None:
    name = file_set["name"]
    assert file_set["values_path"] == f"fileSets.{name}.files"
    mounts = {
        path.name: _dig(_load(path), f"fileSets.{name}.mountPath")
        for path in _integration_files(service)
    }
    declared = {where: mount for where, mount in mounts.items() if mount is not _MISSING}
    assert declared, f"{service}: no integration file mounts fileSets.{name}"
    assert set(declared.values()) == {file_set["mount_path"]}, declared


@pytest.mark.parametrize("service", sorted({name for name, _ in _file_sets()}))
def test_every_mounted_set_is_one_the_engine_writes(service: str) -> None:
    declared = set()
    for path in _integration_files(service):
        declared |= set(_load(path).get("fileSets") or {})
    managed = {fs["name"] for fs in _apps()[service].get("files") or []}
    assert declared <= managed


@pytest.mark.parametrize(
    ("service", "file_set"),
    _file_sets(),
    ids=[f"{name}-{fs['name']}" for name, fs in _file_sets()],
)
def test_the_value_map_moves_the_2_2_0_files_where_the_engine_writes(
    service: str, file_set: dict
) -> None:
    keys = _load(VALUE_MAP)["apps"][service]["keys"]
    targets = set()
    for entry in keys.values():
        to = (entry or {}).get("to")
        targets |= set(to) if isinstance(to, list) else {to}
    assert file_set["values_path"] in targets
