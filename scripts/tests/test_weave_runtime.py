#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_weave_runtime.py
#  Purpose:      Prove each switched component's thin chart beside dfe-extras
#                runs as its 2.2.0 chart did, apart from the differences
#                fixtures/weave-accepted-diffs.yaml lists with their reasons.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The runtime gate of the chart switch, per component, profile, cloud and scenario.

    python3 -m pytest scripts/tests/test_weave_runtime.py -q

Every object both renders hold is compared leaf by leaf in the form the cluster
runs it (_gate.runtime_view): ConfigMap content, the effective env (the
container's env over the ConfigMaps its envFrom names), probes, ports, the
ServiceAccount, the strategy, container names, volumes, resources, scheduling,
labels and annotations. Each difference must match an entry of
fixtures/weave-accepted-diffs.yaml, and each entry must still match one. Beside
that, every Secret and ConfigMap the new render names must resolve, every image
may move its registry only, and every $(NAME) in an env value must name a variable
declared before it.

Render (b) needs the scalo-service library (_weave.library).
"""

import json

import pytest
import yaml

from _gate import (
    COMPONENTS,
    EVERY,
    MATRIX,
    SCENARIOS,
    Diff,
    accepted,
    accepts,
    cell,
    cells,
    forward_references,
    image_problems,
    object_id,
    runtime_diffs,
    runtime_view,
    unaccepted,
    unresolved_refs,
)
from _weave import contract

CELLS = [(*m, "default") for m in MATRIX] + [
    (s, "slim", "local", scenario) for s in COMPONENTS for scenario in SCENARIOS
]
RECEIVER_CELLS = [c for c in CELLS if c[0] == "dfe-receiver"]
LOADER_CELLS = [c for c in CELLS if c[0] == "dfe-loader"]
BUS_PROFILES = ("single", "scale")
MAIN = "spec.template.spec.containers[main]"


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), CELLS)
def test_every_runtime_difference_is_accepted(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    c = cell(service, profile, cloud, scenario)
    diffs = runtime_diffs(c.old, c.new)
    assert unaccepted(diffs, accepts(service).diffs, scenario) == []


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), CELLS)
def test_every_secret_and_configmap_reference_resolves(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    c = cell(service, profile, cloud, scenario)
    assert unresolved_refs(c.old, c.new) == []


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), CELLS)
def test_every_image_moves_its_registry_only(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    c = cell(service, profile, cloud, scenario)
    assert image_problems(c.old, c.new) == []


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), CELLS)
def test_every_var_reference_names_an_earlier_variable(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    c = cell(service, profile, cloud, scenario)
    assert forward_references(c.new) == []


def test_mongo_password_is_declared_before_the_uri_that_expands_it() -> None:
    env = [e["name"] for e in _main(cell("hyperdx", "scale", "aws").new)["env"]]
    assert env.index("MONGO_PASSWORD") < env.index("MONGO_URI")


# ------------------------------------------------- the receiver and loader defects


def opened_ports(docs: list[dict]) -> list[str]:
    """Every port a render opens: container ports, Service ports and NetworkPolicy ports."""
    found = []
    for doc in docs:
        spec = doc.get("spec") or {}
        if doc.get("kind") == "Deployment":
            for container in spec["template"]["spec"].get("containers") or []:
                ports = container.get("ports") or []
                found += [f"{object_id(doc)} {p['containerPort']}" for p in ports]
        elif doc.get("kind") == "Service":
            found += [f"{object_id(doc)} {p['port']}" for p in spec.get("ports") or []]
        elif doc.get("kind") == "NetworkPolicy":
            for rule in spec.get("ingress") or []:
                found += [f"{object_id(doc)} {p['port']}" for p in rule.get("ports") or []]
    return found


def config_file(docs: list[dict], service: str) -> dict:
    """The app's rendered config file, parsed."""
    configmap = next(d for d in docs if object_id(d) == f"ConfigMap/{service}-config")
    (text,) = configmap["data"].values()
    return yaml.safe_load(text) or {}


def delivery_paths(docs: list[dict]) -> list[str]:
    """Where a receiver render sends records: the bus, the loader over gRPC, or both.

    The receiver's compiled defaults route to the bus with no brokers and fail its
    own validation, so a render naming neither path leaves it unable to start.
    """
    env = runtime_view(docs)["Deployment/dfe-receiver"]
    config = config_file(docs, "dfe-receiver")
    paths = []
    if env.get(f"{MAIN}.env[DFE_RECEIVER_KAFKA_BROKERS]"):
        paths.append("bus")
    over_grpc = (config.get("loader") or {}).get("transport") == "grpc"
    to_loader = (config.get("destinations") or {}).get("default") == "loader"
    if over_grpc and to_loader:
        paths.append("loader")
    return paths


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), RECEIVER_CELLS)
def test_the_receiver_opens_nothing_on_8443(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    """Nothing in dfe-receiver binds 8443; 2.2.0 opened it on the pod, its Services and policy."""
    c = cell(service, profile, cloud, scenario)
    assert [p for p in opened_ports(c.new) if p.endswith(" 8443")] == []


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), RECEIVER_CELLS)
def test_the_receiver_never_starts_on_its_compiled_defaults(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    expected = ["bus"] if profile in BUS_PROFILES else ["loader"]
    assert delivery_paths(cell(service, profile, cloud, scenario).new) == expected


@pytest.mark.parametrize("profile", BUS_PROFILES)
def test_the_receiver_reads_its_sasl_mechanism_beside_the_credential(profile: str) -> None:
    """The contract's kafka group carries the mechanism, from the Secret that holds the password."""
    env = runtime_view(cell("dfe-receiver", profile, "aws").new)["Deployment/dfe-receiver"]
    ref = f"{MAIN}.env[DFE_RECEIVER_KAFKA_SASL_%s].valueFrom.secretKeyRef.%s"
    assert env[ref % ("MECHANISM", "name")] == env[ref % ("PASSWORD", "name")] == "dfe-kafka-user"
    assert env[ref % ("MECHANISM", "key")] == "sasl.mechanism"


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), LOADER_CELLS)
def test_the_loader_config_file_carries_no_keda_block(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    """The loader process reads no keda key; the ScaledObject takes its bounds from values alone."""
    assert "keda" not in config_file(cell(service, profile, cloud, scenario).new, service)


def test_the_loader_contract_keeps_keda_out_of_its_config() -> None:
    loader = json.loads(contract("dfe-loader").read_text(encoding="utf-8"))
    assert "keda" not in loader["default_config"]
    assert loader["keda"]["cooldown_period"] == 300


def test_every_accepted_diff_still_matches_one() -> None:
    """An entry nothing matches any more accepts a difference that may come back unread."""
    sections = {EVERY: list(COMPONENTS), **{s: [s] for s in accepted() if s != EVERY}}
    stale = []
    for section, services in sections.items():
        seen = [
            (d, c.scenario)
            for service in services
            for c in cells(service)
            for d in runtime_diffs(c.old, c.new)
        ]
        stale += [
            f"{section}: {entry}"
            for entry in accepted()[section].diffs
            if not any(entry.matches(d, scenario) for d, scenario in seen)
        ]
    assert stale == []


def test_every_accepted_section_is_a_gated_component() -> None:
    assert set(accepted()) - {EVERY} <= set(COMPONENTS)


# ---------------------------------------------------------------- expected fails


def _deployment(docs: list[dict]) -> dict:
    return next(d for d in docs if d["kind"] == "Deployment")


def _main(docs: list[dict]) -> dict:
    return _deployment(docs)["spec"]["template"]["spec"]["containers"][0]


def test_an_unlisted_env_value_fails() -> None:
    c = cell("dfe-ui", "scale", "aws").mutable()
    env = _main(c.new)["env"]
    next(e for e in env if e["name"] == "INTERNAL_API_URL")["value"] = "http://elsewhere:8000"
    found = unaccepted(runtime_diffs(c.old, c.new), accepts("dfe-ui").diffs, "default")
    assert found == [
        "Deployment/dfe-ui spec.template.spec.containers[main].env[INTERNAL_API_URL]: "
        '"http://dfe-engine.dfe.svc.cluster.local:8000" -> "http://elsewhere:8000"'
    ]


def test_an_unlisted_env_variable_fails() -> None:
    c = cell("hyperdx", "single", "local").mutable()
    _main(c.new)["env"].append({"name": "EXTRA", "value": "1"})
    found = unaccepted(runtime_diffs(c.old, c.new), accepts("hyperdx").diffs, "default")
    assert found == [
        'Deployment/dfe-hyperdx spec.template.spec.containers[main].env[EXTRA]: "<absent>" -> "1"'
    ]


def test_an_env_value_moved_in_the_configmap_fails() -> None:
    """The ConfigMap envFrom names is part of the effective env, so its content is held too."""
    c = cell("hyperdx", "mesh", "aws").mutable()
    configmap = next(d for d in c.new if d["metadata"]["name"] == "dfe-hyperdx-env")
    configmap["data"]["DFE_AUTH_MODE"] = "header-dev"
    found = unaccepted(runtime_diffs(c.old, c.new), accepts("hyperdx").diffs, "default")
    assert found == [
        "Deployment/dfe-hyperdx spec.template.spec.containers[main].env[DFE_AUTH_MODE]: "
        '"oidc-proxy" -> "header-dev"'
    ]


def test_a_scenario_entry_accepts_nothing_outside_its_scenario() -> None:
    diff = Diff(
        "Deployment/dfe-ui",
        "spec.template.spec.containers[main].env[NEXTAUTH_URL]",
        "<absent>",
        "",
    )
    assert unaccepted([diff], accepts("dfe-ui").diffs, "no-domain") == []
    assert unaccepted([diff], accepts("dfe-ui").diffs, "default") == [str(diff)]


def test_a_pinned_value_accepts_no_other() -> None:
    c = cell("dfe-ui", "slim", "aws").mutable()
    _deployment(c.new)["spec"]["template"]["spec"]["securityContext"]["runAsUser"] = 0
    found = unaccepted(runtime_diffs(c.old, c.new), accepts("dfe-ui").diffs, "default")
    assert found == ["Deployment/dfe-ui spec.template.spec.securityContext.runAsUser: 1000 -> 0"]


def test_a_reference_nothing_provides_fails() -> None:
    c = cell("hyperdx", "scale", "local").mutable()
    _main(c.new)["env"].append(
        {"name": "X", "valueFrom": {"secretKeyRef": {"name": "nowhere", "key": "k"}}}
    )
    configmap_ref = _main(c.new)["envFrom"][0]["configMapRef"]
    configmap_ref["name"] = "dfe-hyperdx-env-renamed"
    assert unresolved_refs(c.old, c.new) == [
        "Deployment/dfe-hyperdx dfe-hyperdx env X -> Secret nowhere",
        "Deployment/dfe-hyperdx dfe-hyperdx envFrom -> ConfigMap dfe-hyperdx-env-renamed",
    ]


def test_an_optional_reference_resolves() -> None:
    c = cell("hyperdx", "scale", "local").mutable()
    _main(c.new)["env"].append(
        {
            "name": "X",
            "valueFrom": {"secretKeyRef": {"name": "nowhere", "key": "k", "optional": True}},
        }
    )
    assert unresolved_refs(c.old, c.new) == []


def test_a_reference_to_a_later_variable_fails() -> None:
    """A secret renamed to sort after the URI leaves $(...) unexpanded in it."""
    c = cell("hyperdx", "single", "aws").mutable()
    env = _main(c.new)["env"]
    password = next(e for e in env if e["name"] == "MONGO_PASSWORD")
    env.remove(password)
    env.insert(env.index(next(e for e in env if e["name"] == "MONGO_URI")) + 1, password)
    assert forward_references(c.new) == [
        "Deployment/dfe-hyperdx dfe-hyperdx MONGO_URI -> MONGO_PASSWORD"
    ]


def test_an_image_with_another_digest_fails() -> None:
    c = cell("hyperdx", "slim", "aws").mutable()
    main = _main(c.new)
    main["image"] = main["image"].rsplit("@", 1)[0] + "@sha256:" + "0" * 64
    assert len(image_problems(c.old, c.new)) == 1


def test_a_receiver_service_back_on_8443_is_caught() -> None:
    c = cell("dfe-receiver", "scale", "aws").mutable()
    service = next(d for d in c.new if object_id(d) == "Service/dfe-receiver")
    service["spec"]["ports"].append({"name": "grpc", "port": 8443, "targetPort": "grpc"})
    assert [p for p in opened_ports(c.new) if p.endswith(" 8443")] == ["Service/dfe-receiver 8443"]


def test_a_receiver_render_with_no_delivery_path_is_caught() -> None:
    """A direct profile whose config file lost the loader transport falls to the brokerless bus."""
    c = cell("dfe-receiver", "slim", "local").mutable()
    configmap = next(d for d in c.new if object_id(d) == "ConfigMap/dfe-receiver-config")
    config = yaml.safe_load(configmap["data"]["config.yaml"])
    del config["loader"]
    configmap["data"]["config.yaml"] = yaml.safe_dump(config)
    assert delivery_paths(c.new) == []


def test_a_loader_config_file_with_a_keda_block_is_caught() -> None:
    c = cell("dfe-loader", "scale", "aws").mutable()
    configmap = next(d for d in c.new if object_id(d) == "ConfigMap/dfe-loader-config")
    configmap["data"]["loader.yaml"] += "keda:\n  max_replicas: 4\n"
    assert "keda" in config_file(c.new, "dfe-loader")
