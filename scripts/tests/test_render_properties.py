#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_render_properties.py
#  Purpose:      Prove what every deployed render holds -- object identity,
#                resolvable references, pinned images, ordered env, served
#                routes and the doors to outside -- read from the goldens.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What each cell's render holds, read from its golden under fixtures/thin-renders.

    python3 -m pytest scripts/tests/test_render_properties.py -q

test_thin_renders.py holds every golden equal to the live render, so a check here
holds for what Argo deploys. The checks:

- no (kind, name) renders twice across the thin chart and dfe-extras
- every Secret and ConfigMap a workload names is rendered beside it, provided by
  the cluster, or listed in ELSEWHERE with the file that provides it
- every image carries the tag and digest versions.yaml pins for it
- every $(NAME) in an env value names a variable declared before it
- every GRPCRoute backend names a port its Service serves
- only the receiver and culvert open a door to outside the cluster, and only
  where EXPOSURE lists it

The dial cases at the end render live, which needs the scalo-service library
(_weave.library).
"""

import json
import re
from pathlib import Path

import pytest
import yaml

from _gate import (
    CELLS,
    CLOUDS,
    COMPONENTS,
    MATRIX,
    PIN_TOKEN_RE,
    PROFILES,
    WORKLOAD_KINDS,
    effective_env,
    exposed,
    forward_references,
    golden,
    golden_path,
    object_id,
    rendered_twice,
    route_problems,
    unresolved_refs,
)
from _weave import REPO_ROOT, contract, render_app, weave

# Stack objects a workload names that another Application or bootstrap provides:
# (kind, name) -> the file that provides it, and the scenario it is limited to.
ELSEWHERE: dict[tuple[str, str], tuple[str, str | None]] = {
    ("Secret", "clickhouse-admin-password"): ("helm/charts/clickhouse-cluster/values.yaml", None),
    ("Secret", "dfe-clickhouse-ca"): ("helm/charts/clickhouse-cluster/values.yaml", None),
    ("Secret", "dfe-ferretdb-password"): ("helm/charts/ferretdb/values.yaml", None),
    ("Secret", "dfe-kafka-user"): ("helm/charts/kafka/templates/kafka-user.yaml", None),
    ("Secret", "dfe-deploy-repo-auth"): ("bootstrap/bootstrap.sh", None),
    ("Secret", "ghcr-pull-secret"): ("bootstrap/bootstrap.sh", None),
    # The deployer's own CA bundle, which the scenario names and supplies.
    ("ConfigMap", "example-ca-bundle"): ("scripts/tests/_gate.py", "clickhouse-ca-configmap"),
}

# Each component's Services reachable from outside, per cloud: name -> type and ports.
EXPOSURE: dict[str, dict[str, dict]] = {
    "dfe-ui": {"local": {}, "aws": {}},
    "hyperdx": {"local": {}, "aws": {}},
    # local.yaml routes ingest through the cluster Gateway, aws.yaml through culvert.
    "dfe-receiver": {"local": {}, "aws": {}},
    "dfe-loader": {"local": {}, "aws": {}},
    "dfe-archiver": {"local": {}, "aws": {}},
    "dfe-transform-vrl": {"local": {}, "aws": {}},
    "dfe-transform-vector": {"local": {}, "aws": {}},
    "dfe-transform-elastic": {"local": {}, "aws": {}},
    "dfe-fetcher": {"local": {}, "aws": {}},
    # The tunnel door: MetalLB's load balancer on-prem, a NodePort behind the
    # deployer's own address on aws (edge-aws.yaml).
    "culvert": {
        "local": {
            "Service/dfe-culvert-public-udp": {
                "type": "LoadBalancer",
                "ports": ["1194/UDP", "51820/UDP"],
            }
        },
        "aws": {
            "Service/dfe-culvert-public-udp": {
                "type": "NodePort",
                "ports": ["1194/UDP", "51820/UDP"],
            }
        },
    },
    # Reachable by operators, not by the public (docs/THREAT-MODEL.md).
    "dfe-engine": {"local": {}, "aws": {}},
}
INTERNET_FACING = {"dfe-receiver", "culvert"}

BUS_PROFILES = ("single", "scale")
RECEIVER_CELLS = [c for c in CELLS if c[0] == "dfe-receiver"]
LOADER_CELLS = [c for c in CELLS if c[0] == "dfe-loader"]
ENGINE_CELLS = [c for c in CELLS if c[0] == "dfe-engine"]
ENGINE = "dfe-engine"


def _deployment(docs: list[dict], name: str | None = None) -> dict:
    return next(
        d for d in docs if d["kind"] == "Deployment" and name in (None, d["metadata"]["name"])
    )


def _main(docs: list[dict], name: str | None = None) -> dict:
    return _deployment(docs, name)["spec"]["template"]["spec"]["containers"][0]


def config_file(docs: list[dict], service: str) -> dict:
    """The app's rendered config file, parsed."""
    configmap = next(d for d in docs if object_id(d) == f"ConfigMap/{service}-config")
    (text,) = configmap["data"].values()
    return yaml.safe_load(text) or {}


# ------------------------------------------------------------------ identity


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), CELLS)
def test_no_object_renders_twice(service: str, profile: str, cloud: str, scenario: str) -> None:
    """Argo applies one object per (kind, name); a second copy fights the first on every sync."""
    assert rendered_twice(golden(service, profile, cloud, scenario)) == []


def test_an_object_both_sources_render_is_caught() -> None:
    docs = golden("hyperdx", "slim", "local")
    account = next(d for d in docs if d["kind"] == "ServiceAccount")
    assert rendered_twice([*docs, account]) == ["ServiceAccount/dfe-hyperdx"]


@pytest.mark.parametrize(("profile", "cloud"), [(p, k) for p in PROFILES for k in CLOUDS])
def test_the_engine_config_claim_keeps_its_name(profile: str, cloud: str) -> None:
    """A renamed claim is pruned and comes back empty, factory-resetting accounts and orgs."""
    docs = golden(ENGINE, profile, cloud)
    held = sorted(d["metadata"]["name"] for d in docs if d["kind"] == "PersistentVolumeClaim")
    mounted = sorted(
        v["persistentVolumeClaim"]["claimName"]
        for v in _deployment(docs, ENGINE)["spec"]["template"]["spec"].get("volumes") or []
        if "persistentVolumeClaim" in v
    )
    assert (held, mounted) == (["dfe-engine-config"], ["dfe-engine-config"])


@pytest.mark.parametrize(("profile", "cloud"), [(p, k) for p in PROFILES for k in CLOUDS])
def test_the_engine_runs_exactly_one_pod(profile: str, cloud: str) -> None:
    """The contract's singleton: one replica, Recreate, and nothing that scales or budgets it."""
    docs = golden(ENGINE, profile, cloud)
    spec = _deployment(docs, ENGINE)["spec"]
    assert (spec["replicas"], spec["strategy"]) == (1, {"type": "Recreate"})
    scaling = {"PodDisruptionBudget", "HorizontalPodAutoscaler", "ScaledObject"}
    assert [d["metadata"]["name"] for d in docs if d["kind"] in scaling] == []


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        ({"keda": {"enabled": True}}, "keda.enabled is set"),
        ({"autoscaling": {"enabled": True}}, "autoscaling.enabled is set"),
    ],
    ids=["keda", "hpa"],
)
def test_scaling_the_engine_fails_the_render(overlay: dict, message: str) -> None:
    with pytest.raises(weave().WeaveError, match=message):
        render_app(ENGINE, "scale", "aws", overlay=overlay)


# ---------------------------------------------------------------- references


def _elsewhere(scenario: str) -> set[tuple[str, str]]:
    return {key for key, (_, only) in ELSEWHERE.items() if only in (None, scenario)}


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), CELLS)
def test_every_secret_and_configmap_reference_resolves(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    docs = golden(service, profile, cloud, scenario)
    assert unresolved_refs(docs, _elsewhere(scenario)) == []


@pytest.mark.parametrize(("kind", "name"), sorted(ELSEWHERE))
def test_each_stack_object_is_named_where_the_table_says(kind: str, name: str) -> None:
    """An entry whose provider stopped naming the object would let a dangling reference pass."""
    provider, _ = ELSEWHERE[(kind, name)]
    assert name in (REPO_ROOT / provider).read_text(encoding="utf-8"), provider


def test_a_reference_nothing_provides_fails() -> None:
    docs = golden("hyperdx", "scale", "local")
    main = _main(docs)
    nowhere = {"secretKeyRef": {"name": "nowhere", "key": "k"}}
    main["env"].append({"name": "X", "valueFrom": nowhere})
    main["envFrom"][0]["configMapRef"]["name"] = "dfe-hyperdx-env-renamed"
    assert unresolved_refs(docs, _elsewhere("default")) == [
        "Deployment/dfe-hyperdx dfe-hyperdx env X -> Secret nowhere",
        "Deployment/dfe-hyperdx dfe-hyperdx envFrom -> ConfigMap dfe-hyperdx-env-renamed",
    ]


def test_an_optional_reference_resolves() -> None:
    docs = golden("hyperdx", "scale", "local")
    _main(docs)["env"].append(
        {
            "name": "X",
            "valueFrom": {"secretKeyRef": {"name": "nowhere", "key": "k", "optional": True}},
        }
    )
    assert unresolved_refs(docs, _elsewhere("default")) == []


# -------------------------------------------------------------------- images


def _images(path: Path) -> list[tuple[str, str]]:
    """Each container image of each workload in a golden as committed, pins as tokens."""
    found = []
    for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        if not isinstance(doc, dict) or doc.get("kind") not in WORKLOAD_KINDS:
            continue
        pod = doc["spec"]["template"]["spec"]
        for container in (pod.get("initContainers") or []) + (pod.get("containers") or []):
            found.append((f"{object_id(doc)} {container['name']}", container["image"]))
    return found


# An image reference whose tag and digest may each be a pin token, which holds a colon.
IMAGE_RE = re.compile(r"^(?P<name>.+?):(?P<tag><pin:[^>]+>|[^:@<>]+)(?:@(?P<digest>.+))?$")


def image_problems(images: list[tuple[str, str]]) -> list[str]:
    """Each image whose tag or digest is not a versions.yaml pin; empty is a pass.

    A pin left as its token is one the render took from versions.yaml. A tag or
    digest written out in full is one a file carries by hand, which a release
    leaves behind.
    """
    found = []
    for where, image in images:
        parts = IMAGE_RE.fullmatch(image)
        tag = parts["tag"] if parts else ""
        digest = (parts["digest"] if parts else None) or ""
        if not PIN_TOKEN_RE.fullmatch(digest):
            found.append(f"{where}: {image} carries no pinned digest")
        elif not PIN_TOKEN_RE.fullmatch(tag):
            found.append(f"{where}: {image} carries a tag no pin names")
    return found


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), CELLS)
def test_every_image_runs_the_pinned_tag_and_digest(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    assert image_problems(_images(golden_path(service, profile, cloud, scenario))) == []


def test_an_image_pinned_by_hand_is_caught() -> None:
    images = [
        ("Deployment/x main", "registry.example.com/dfe/x:<pin:apps.dfe-ui>@<pin:digests.dfe-ui>"),
        ("Deployment/x a", "registry.example.com/dfe/x:v0.0.1@<pin:digests.dfe-ui>"),
        ("Deployment/x b", "registry.example.com/dfe/x:<pin:apps.dfe-ui>@sha256:" + "0" * 64),
        ("Deployment/x c", "registry.example.com/dfe/x:<pin:apps.dfe-ui>"),
    ]
    assert image_problems(images) == [
        "Deployment/x a: registry.example.com/dfe/x:v0.0.1@<pin:digests.dfe-ui> "
        "carries a tag no pin names",
        f"Deployment/x b: registry.example.com/dfe/x:<pin:apps.dfe-ui>@sha256:{'0' * 64} "
        "carries no pinned digest",
        "Deployment/x c: registry.example.com/dfe/x:<pin:apps.dfe-ui> carries no pinned digest",
    ]


# ----------------------------------------------------------------------- env


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), CELLS)
def test_every_var_reference_names_an_earlier_variable(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    assert forward_references(golden(service, profile, cloud, scenario)) == []


def test_mongo_password_is_declared_before_the_uri_that_expands_it() -> None:
    env = [e["name"] for e in _main(golden("hyperdx", "scale", "aws"))["env"]]
    assert env.index("MONGO_PASSWORD") < env.index("MONGO_URI")


def test_a_reference_to_a_later_variable_fails() -> None:
    """A secret renamed to sort after the URI leaves $(...) unexpanded in it."""
    docs = golden("hyperdx", "single", "aws")
    env = _main(docs)["env"]
    password = next(e for e in env if e["name"] == "MONGO_PASSWORD")
    env.remove(password)
    env.insert(env.index(next(e for e in env if e["name"] == "MONGO_URI")) + 1, password)
    assert forward_references(docs) == [
        "Deployment/dfe-hyperdx dfe-hyperdx MONGO_URI -> MONGO_PASSWORD"
    ]


# -------------------------------------------------------------------- routes


@pytest.mark.parametrize(("service", "profile", "cloud"), MATRIX)
def test_every_route_backend_port_is_served(service: str, profile: str, cloud: str) -> None:
    """dfe-extras renders the mesh route, the thin chart the Service it sends to."""
    assert route_problems(golden(service, profile, cloud)) == []


def test_a_route_to_a_port_the_service_lost_is_caught() -> None:
    docs = golden("dfe-transform-vrl", "mesh", "aws")
    service = next(d for d in docs if object_id(d) == "Service/dfe-transform-vrl")
    service["spec"]["ports"] = [p for p in service["spec"]["ports"] if p["port"] != 6000]
    assert route_problems(docs) == [
        "GRPCRoute/dfe-transform-vrl-mesh -> Service dfe-transform-vrl port 6000, "
        "which it does not serve"
    ]


def test_a_route_to_a_service_the_render_lacks_is_caught() -> None:
    docs = [
        d
        for d in golden("dfe-transform-vrl", "mesh", "aws")
        if object_id(d) != "Service/dfe-transform-vrl"
    ]
    assert route_problems(docs) == [
        "GRPCRoute/dfe-transform-vrl-mesh -> Service dfe-transform-vrl, which the render lacks"
    ]


# ------------------------------------------------------- the receiver and loader


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


def delivery_paths(docs: list[dict]) -> list[str]:
    """Where a receiver render sends records: the bus, the loader over gRPC, or both.

    The receiver's compiled defaults route to the bus with no brokers and fail its
    own validation, so a render naming neither path leaves it unable to start.
    """
    env = effective_env(docs, "Deployment/dfe-receiver")
    config = config_file(docs, "dfe-receiver")
    paths = []
    if env.get("DFE_RECEIVER_KAFKA_BROKERS"):
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
    """Nothing in dfe-receiver binds 8443, so no pod port, Service or policy opens it."""
    docs = golden(service, profile, cloud, scenario)
    assert [p for p in opened_ports(docs) if p.endswith(" 8443")] == []


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), RECEIVER_CELLS)
def test_the_receiver_never_starts_on_its_compiled_defaults(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    expected = ["bus"] if profile in BUS_PROFILES else ["loader"]
    assert delivery_paths(golden(service, profile, cloud, scenario)) == expected


@pytest.mark.parametrize("profile", BUS_PROFILES)
def test_the_receiver_reads_its_sasl_mechanism_beside_the_credential(profile: str) -> None:
    """The contract's kafka group carries the mechanism, from the Secret that holds the password."""
    env = effective_env(golden("dfe-receiver", profile, "aws"), "Deployment/dfe-receiver")
    mechanism = env["DFE_RECEIVER_KAFKA_SASL_MECHANISM"]["valueFrom"]["secretKeyRef"]
    password = env["DFE_RECEIVER_KAFKA_SASL_PASSWORD"]["valueFrom"]["secretKeyRef"]
    assert mechanism["name"] == password["name"] == "dfe-kafka-user"
    assert mechanism["key"] == "sasl.mechanism"


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), LOADER_CELLS)
def test_the_loader_config_file_carries_no_keda_block(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    """The loader process reads no keda key; the ScaledObject takes its bounds from values alone."""
    assert "keda" not in config_file(golden(service, profile, cloud, scenario), service)


def test_the_loader_contract_keeps_keda_out_of_its_config() -> None:
    loader = json.loads(contract("dfe-loader").read_text(encoding="utf-8"))
    assert "keda" not in loader["default_config"]
    assert loader["keda"]["cooldown_period"] == 300


def test_a_receiver_service_back_on_8443_is_caught() -> None:
    docs = golden("dfe-receiver", "scale", "aws")
    service = next(d for d in docs if object_id(d) == "Service/dfe-receiver")
    service["spec"]["ports"].append({"name": "grpc", "port": 8443, "targetPort": "grpc"})
    assert [p for p in opened_ports(docs) if p.endswith(" 8443")] == ["Service/dfe-receiver 8443"]


def test_a_receiver_render_with_no_delivery_path_is_caught() -> None:
    """A direct profile whose config file lost the loader transport falls to the brokerless bus."""
    docs = golden("dfe-receiver", "slim", "local")
    configmap = next(d for d in docs if object_id(d) == "ConfigMap/dfe-receiver-config")
    config = yaml.safe_load(configmap["data"]["config.yaml"])
    del config["loader"]
    configmap["data"]["config.yaml"] = yaml.safe_dump(config)
    assert delivery_paths(docs) == []


def test_a_loader_config_file_with_a_keda_block_is_caught() -> None:
    docs = golden("dfe-loader", "scale", "aws")
    configmap = next(d for d in docs if object_id(d) == "ConfigMap/dfe-loader-config")
    configmap["data"]["loader.yaml"] += "keda:\n  max_replicas: 4\n"
    assert "keda" in config_file(docs, "dfe-loader")


# ---------------------------------------------------------------- the engine


@pytest.mark.parametrize(("service", "profile", "cloud", "scenario"), ENGINE_CELLS)
def test_the_engine_keeps_its_service_account_token(
    service: str, profile: str, cloud: str, scenario: str
) -> None:
    """A deployment may log the engine in to OpenBao with the pod's token."""
    docs = golden(service, profile, cloud, scenario)
    pod = _deployment(docs, ENGINE)["spec"]["template"]["spec"]
    account = next(d for d in docs if object_id(d) == "ServiceAccount/dfe-engine")
    assert pod.get("automountServiceAccountToken", True) is True
    assert account.get("automountServiceAccountToken", True) is True
    assert [v["name"] for v in pod.get("volumes") or [] if "serviceaccount" in v["name"]] == []


# ------------------------------------------------------------------- exposure


@pytest.mark.parametrize(("service", "profile", "cloud"), MATRIX)
def test_the_render_exposes_only_what_the_table_lists(
    service: str, profile: str, cloud: str
) -> None:
    assert exposed(golden(service, profile, cloud)) == EXPOSURE[service][cloud]


def test_every_component_has_an_exposure_row() -> None:
    assert set(COMPONENTS) <= set(EXPOSURE)


@pytest.mark.parametrize("service", sorted(set(EXPOSURE) - INTERNET_FACING))
def test_only_the_receiver_and_the_tunnel_are_exposed(service: str) -> None:
    assert all(rows == {} for rows in EXPOSURE[service].values())


def test_a_public_service_switched_on_is_caught() -> None:
    """dfe-hyperdx's contract marks 8080 public; one overlay line puts it on a load balancer."""
    docs = render_app("hyperdx", "scale", "aws", overlay={"publicService": {"enabled": True}})
    assert exposed(docs) == {
        "Service/dfe-hyperdx-public": {"type": "LoadBalancer", "ports": ["8080/TCP"]}
    }
    assert exposed(docs) != EXPOSURE["hyperdx"]["aws"]


# ------------------------------------------------------- the receiver's front door

# Every cloud values file under argocd/values. The on-prem files set the gateway
# door and the clouds the load balancer door with no senders, so none renders a
# load balancer by default.
RECEIVER_CLOUDS = ("local", "local-dfe", "aws", "gcp", "azure")
LB_FIELDS = (
    "type",
    "loadBalancerIP",
    "loadBalancerClass",
    "loadBalancerSourceRanges",
    "externalTrafficPolicy",
)
# The receiver's load balancers and ingest policy ports on scale, per cloud file.
FRONT_DOOR: dict[str, tuple[dict[str, dict], list[str]]] = {
    cloud: ({}, []) for cloud in RECEIVER_CLOUDS
}
# RFC 5737 addresses and an illustrative class: no deployment's own.
ADDRESS = "203.0.113.10"
SENDERS = ["198.51.100.0/24"]
SCHEME = {"service.beta.kubernetes.io/aws-load-balancer-scheme": "internet-facing"}
SHAPE = {"loadBalancerClass": "service.k8s.aws/nlb", "annotations": SCHEME}
OPT_IN = {"exposure": {"groups": {"ingest": {"door": "loadbalancer", "allow": SENDERS}}, "public": SHAPE}}
# What each cloud's apps/dfe-receiver/<cloud>.yaml adds to the load balancer.
CLOUD_SHAPE: dict[str, dict] = {
    "aws": {"annotations": {
        "service.beta.kubernetes.io/aws-load-balancer-type": "external",
        "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
        "service.beta.kubernetes.io/aws-load-balancer-target-group-attributes": "preserve_client_ip.enabled=true",
        "service.beta.kubernetes.io/aws-load-balancer-manage-backend-security-group-rules": "true",
    }},
    "gcp": {"externalTrafficPolicy": "Local"},
    "azure": {"externalTrafficPolicy": "Local"},
}
DEAD_PORT = "8443/TCP"


def _receiver(cloud: str, overlay: dict | None = None, annotations: dict | None = None) -> list:
    return render_app(
        "dfe-receiver", "scale", cloud, overlay=overlay, annotations=annotations or {}
    )


def load_balancers(docs: list[dict]) -> dict[str, dict]:
    """Each LoadBalancer Service: its ports, the fields that shape it, and its annotations."""
    found = {}
    for doc in docs:
        spec = doc.get("spec") or {}
        if doc.get("kind") != "Service" or spec.get("type") != "LoadBalancer":
            continue
        listed = spec.get("ports") or []
        ports = sorted(f"{p.get('port')}/{p.get('protocol', 'TCP')}" for p in listed)
        found[object_id(doc)] = {
            "ports": ports,
            "annotations": doc["metadata"].get("annotations") or {},
            **{field: spec[field] for field in LB_FIELDS if field in spec},
        }
    return found


def ingest_ports(docs: list[dict]) -> list[str]:
    """The ports the receiver's ingest NetworkPolicy opens; empty where none renders."""
    ports = []
    for doc in docs:
        if object_id(doc) != "NetworkPolicy/dfe-receiver-ingest":
            continue
        for rule in doc["spec"].get("ingress") or []:
            ports += [f"{p['port']}/{p.get('protocol', 'TCP')}" for p in rule.get("ports") or []]
    return sorted(ports)


@pytest.mark.parametrize("cloud", RECEIVER_CLOUDS)
def test_the_receiver_front_door_on_each_cloud(cloud: str) -> None:
    docs = _receiver(cloud)
    assert (load_balancers(docs), ingest_ports(docs)) == FRONT_DOOR[cloud]


@pytest.mark.parametrize("cloud", ["aws", "gcp", "azure"])
def test_a_deployment_naming_its_senders_keeps_its_shape(cloud: str) -> None:
    """The door docs/INGEST-EDGE.md documents, with the cluster secret's pinned address."""
    facts = {"dfe.hyperi.io/receiver_address": ADDRESS}
    balancers = load_balancers(_receiver(cloud, OPT_IN, facts))
    shape = CLOUD_SHAPE[cloud]
    annotations = {**shape.get("annotations", {}), **SCHEME, "dfe.hyperi.io/ingest-fence": "listed"}
    assert balancers == {
        "Service/dfe-receiver-public": {
            "ports": ["8080/TCP"],
            "type": "LoadBalancer",
            "loadBalancerIP": ADDRESS,
            "loadBalancerClass": SHAPE["loadBalancerClass"],
            "loadBalancerSourceRanges": SENDERS,
            "annotations": annotations,
            **{k: v for k, v in shape.items() if k != "annotations"},
        }
    }
    assert ingest_ports(_receiver(cloud, OPT_IN, facts)) == ["8080/TCP"]


def test_the_cluster_secret_address_reaches_the_thin_load_balancer() -> None:
    """The appset hands the cluster secret's receiver address to publicService.loadBalancerIP."""
    facts = {"dfe.hyperi.io/receiver_address": ADDRESS}
    balancers = load_balancers(_receiver("local-dfe", OPT_IN, annotations=facts))
    assert balancers["Service/dfe-receiver-public"].get("loadBalancerIP") == ADDRESS


def test_the_overlay_address_applies_where_the_cluster_secret_has_none() -> None:
    other = "203.0.113.20"
    overlay = {"exposure": {**OPT_IN["exposure"], "public": {"loadBalancerIP": other}}}
    alone = load_balancers(_receiver("local-dfe", overlay))
    assert alone["Service/dfe-receiver-public"].get("loadBalancerIP") == other
    facts = {"dfe.hyperi.io/receiver_address": ADDRESS}
    pinned = load_balancers(_receiver("local-dfe", overlay, facts))
    assert pinned["Service/dfe-receiver-public"].get("loadBalancerIP") == ADDRESS


@pytest.mark.parametrize(
    ("cloud", "ingest"),
    [
        ("aws", {"door": "tunnel"}),
        ("aws", {"enabled": False}),
        ("aws", {"allow": []}),
        ("local", {"door": "gateway"}),
    ],
    ids=["tunnel", "group-off", "no-senders", "gateway"],
)
def test_expected_fail_a_receiver_with_no_load_balancer_door_renders_none(cloud: str, ingest: dict) -> None:
    group = {"door": "loadbalancer", "allow": SENDERS, **ingest}
    overlay = {"exposure": {"groups": {"ingest": group}, "public": SHAPE}}
    assert load_balancers(_receiver(cloud, overlay)) == {}


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        (
            {"publicService": {"enabled": True}},
            "publicService.enabled is true",
        ),
        (
            {"exposure": {"groups": {"ingest": {"door": "tunnel"}}}, "publicService": {"enabled": True}},
            "publicService.enabled is true",
        ),
        (
            {"publicService": SHAPE},
            "set exposure.public.",
        ),
        (
            {"publicService": {"type": "NodePort"}},
            "set exposure.public.serviceType instead",
        ),
        (
            {"exposure": {"groups": {"ingest": {"allow": SENDERS}}, "public": {"serviceType": "NodePort"}}},
            "Kubernetes only filters on loadBalancerSourceRanges for a LoadBalancer",
        ),
        (
            {"exposure": {"mode": "public"}},
            "exposure.mode is \"public\" and retired",
        ),
    ],
    ids=[
        "library-balancer",
        "library-balancer-tunnel",
        "library-keys",
        "library-type",
        "allow-list-on-a-node-port",
        "retired-mode",
    ],
)
def test_a_front_door_key_that_would_reach_nothing_is_refused(overlay: dict, message: str) -> None:
    """A load balancer with no allow-list, or an allow-list Kubernetes ignores, never renders."""
    with pytest.raises(weave().WeaveError) as refused:
        _receiver("aws", overlay)
    assert message in " ".join(str(refused.value).split())


def test_a_load_balancer_carrying_8443_is_caught() -> None:
    """The dead port put back on the load balancer fails the front-door table."""
    dead = {**OPT_IN, "extraPorts": [{"name": "grpc-dead", "port": 8443, "public": True}]}
    docs = _receiver("local-dfe", dead)
    assert DEAD_PORT in load_balancers(docs)["Service/dfe-receiver-public"]["ports"]
    assert ingest_ports(docs) != ["8080/TCP"]
