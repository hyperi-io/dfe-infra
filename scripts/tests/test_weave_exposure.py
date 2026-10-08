#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_weave_exposure.py
#  Purpose:      Prove no switched component's thin chart opens a door to
#                outside the cluster that the exposure table does not list.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The exposure gate of the chart switch, per component, profile and cloud.

    python3 -m pytest scripts/tests/test_weave_exposure.py -q

A Service of type LoadBalancer or NodePort is reachable from outside the cluster.
Only dfe-receiver takes untrusted input from there (docs/THREAT-MODEL.md), so its
row of EXPOSURE is the only one that may hold any. A contract can mark a port
public (dfe-hyperdx marks 8080), and scalo-service renders its load balancer only
where `publicService.enabled` is set, so this holds the integration values to it.

The receiver's own load balancer is held to 2.2.0's on every cloud values file and
on a deployment's opt-in: the same Service name, address, class, source ranges and
annotations, and the same ports less 8443, which nothing in the receiver binds.

Render (b) needs the scalo-service library (_weave.library).
"""

import tempfile
from pathlib import Path

import pytest
import yaml

from _gate import (
    COMPONENTS,
    DEFAULT,
    MATRIX,
    accepts,
    cell,
    exposed,
    object_id,
    runtime_diffs,
    unaccepted,
)
from _weave import render_app, weave

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
    # Reachable by operators, not by the public (docs/THREAT-MODEL.md).
    "dfe-engine": {"local": {}, "aws": {}},
}
INTERNET_FACING = {"dfe-receiver"}

# Every cloud values file under argocd/values; local-dfe is the one that leaves the
# receiver on its public default.
RECEIVER_CLOUDS = ("local", "local-dfe", "aws", "gcp", "azure")
# The port 2.2.0 opened for OTLP/gRPC, which nothing in dfe-receiver binds.
DEAD_PORT = "8443/TCP"
LB_FIELDS = (
    "type",
    "loadBalancerIP",
    "loadBalancerClass",
    "loadBalancerSourceRanges",
    "externalTrafficPolicy",
)
# RFC 5737 addresses and an illustrative class: no deployment's own.
ADDRESS = "203.0.113.10"
SHAPE = {
    "loadBalancerClass": "service.k8s.aws/nlb",
    "loadBalancerSourceRanges": ["198.51.100.0/24"],
    "annotations": {"service.beta.kubernetes.io/aws-load-balancer-scheme": "internet-facing"},
}
OPT_IN = {
    "exposure": {"mode": "public", "public": SHAPE},
    "publicService": {"enabled": True, "loadBalancerIP": ADDRESS, **SHAPE},
}


@pytest.mark.parametrize(("service", "profile", "cloud"), MATRIX)
def test_the_thin_chart_exposes_only_what_the_table_lists(
    service: str, profile: str, cloud: str
) -> None:
    c = cell(service, profile, cloud)
    assert exposed(c.new) == EXPOSURE[service][cloud]


def test_every_gated_component_has_an_exposure_row() -> None:
    assert set(COMPONENTS) <= set(EXPOSURE)


@pytest.mark.parametrize("service", sorted(set(EXPOSURE) - INTERNET_FACING))
def test_only_the_receiver_is_exposed(service: str) -> None:
    assert all(rows == {} for rows in EXPOSURE[service].values())


# ------------------------------------------------------- the receiver's front door


def _receiver(
    which: str, cloud: str, overlay: dict | None = None, annotations: dict | None = None
) -> list[dict]:
    """The receiver on scale, with a deploy repo holding ``overlay`` as its instance file."""
    with tempfile.TemporaryDirectory(prefix="dfe-exposure-") as tmp:
        repo = None
        if overlay is not None:
            path = Path(tmp) / "values" / "dfe-receiver-default-values.yaml"
            path.parent.mkdir()
            body = {"deploy": {"service": "dfe-receiver", "instance": "default"}, **overlay}
            path.write_text(yaml.safe_dump(body), encoding="utf-8", newline="\n")
            repo = Path(tmp)
        return render_app(
            "dfe-receiver", "scale", cloud, which, deploy_repo=repo, annotations=annotations or {}
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


def without_dead_port(balancers: dict[str, dict]) -> dict[str, dict]:
    return {
        name: {**lb, "ports": [p for p in lb["ports"] if p != DEAD_PORT]}
        for name, lb in balancers.items()
    }


@pytest.mark.parametrize("cloud", RECEIVER_CLOUDS)
def test_the_receiver_front_door_matches_2_2_0_on_each_cloud(cloud: str) -> None:
    old, new = _receiver("old", cloud), _receiver("new", cloud)
    assert load_balancers(new) == without_dead_port(load_balancers(old))
    assert ingest_ports(new) == [p for p in ingest_ports(old) if p != DEAD_PORT]
    assert DEAD_PORT not in str(load_balancers(new)), "8443 reached a load balancer"


def test_the_local_dfe_load_balancer_departs_from_2_2_0_only_where_listed() -> None:
    """The one cloud file with a receiver load balancer sits outside the gate's matrix.

    Its public Service and ingest policy are held here to the receiver's accepted
    diffs plus three that the matrix never meets: the cloud label 2.2.0 wrote as
    local everywhere, the exposure label nothing selects on, and 8443 off the
    public-mode policy.
    """
    old, new = _receiver("old", "local-dfe"), _receiver("new", "local-dfe")
    door = ("Service/dfe-receiver-public", "NetworkPolicy/dfe-receiver-ingest")
    diffs = [d for d in runtime_diffs(old, new) if d.object in door]
    left = unaccepted(diffs, accepts("dfe-receiver").diffs, DEFAULT)
    assert sorted(left) == [
        "NetworkPolicy/dfe-receiver-ingest spec.ingress: "
        '[{"ports":[{"port":8080,"protocol":"TCP"},{"port":8443,"protocol":"TCP"}]}] -> '
        '[{"ports":[{"port":8080,"protocol":"TCP"}]}]',
        "Service/dfe-receiver-public metadata.labels[dfe.hyperi.io/cloud]: "
        '"local" -> "local-dfe"',
        "Service/dfe-receiver-public metadata.labels[dfe.hyperi.io/exposure]: "
        '"public" -> "<absent>"',
        'Service/dfe-receiver-public spec.ports[8443/TCP].name: "grpc" -> "<absent>"',
        'Service/dfe-receiver-public spec.ports[8443/TCP].targetPort: 8443 -> "<absent>"',
    ]


def test_only_local_dfe_gives_the_receiver_a_load_balancer() -> None:
    """Every other cloud file reaches the receiver through the Gateway or culvert."""
    found = {cloud: sorted(load_balancers(_receiver("new", cloud))) for cloud in RECEIVER_CLOUDS}
    assert found == {
        "local": [],
        "local-dfe": ["Service/dfe-receiver-public"],
        "aws": [],
        "gcp": [],
        "azure": [],
    }


@pytest.mark.parametrize("cloud", ["aws", "gcp", "azure"])
def test_a_deployment_opting_into_a_load_balancer_keeps_its_shape(cloud: str) -> None:
    """The opt-in a cloud file's comment documents, with a pinned address.

    The old render takes the address from the cluster secret, as the appset hands
    it today; the new render takes it from publicService.loadBalancerIP, set here
    in the instance file the way the reworked appset's parameter will set it.
    """
    facts = {"dfe.hyperi.io/receiver_address": ADDRESS}
    old = _receiver("old", cloud, OPT_IN, facts)
    new = _receiver("new", cloud, OPT_IN, facts)
    balancers = load_balancers(new)
    assert balancers == without_dead_port(load_balancers(old))
    assert balancers["Service/dfe-receiver-public"]["loadBalancerIP"] == ADDRESS
    assert ingest_ports(new) == ["8080/TCP"]


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="the layer 2 appset hands the address as exposure.public.loadBalancerIP, "
    "which the thin chart does not read",
)
def test_the_cluster_secret_address_reaches_the_thin_load_balancer() -> None:
    facts = {"dfe.hyperi.io/receiver_address": ADDRESS}
    balancers = load_balancers(_receiver("new", "local-dfe", annotations=facts))
    assert balancers["Service/dfe-receiver-public"].get("loadBalancerIP") == ADDRESS


# ---------------------------------------------------------------- expected fails


def test_a_public_service_switched_on_is_caught() -> None:
    """dfe-hyperdx's contract marks 8080 public; one overlay line would put it on a load balancer."""
    with tempfile.TemporaryDirectory(prefix="dfe-exposure-") as tmp:
        overlay = Path(tmp) / "values" / "hyperdx-default-values.yaml"
        overlay.parent.mkdir()
        body = {
            "deploy": {"service": "hyperdx", "instance": "default"},
            "publicService": {"enabled": True},
        }
        overlay.write_text(yaml.safe_dump(body), encoding="utf-8", newline="\n")
        docs = render_app("hyperdx", "scale", "aws", "new", deploy_repo=Path(tmp))
    assert exposed(docs) == {
        "Service/dfe-hyperdx-public": {"type": "LoadBalancer", "ports": ["8080/TCP"]}
    }
    assert exposed(docs) != EXPOSURE["hyperdx"]["aws"]


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        (
            {"exposure": {"mode": "public"}, "publicService": {"enabled": False}},
            'exposure.mode is "public" but publicService.enabled is not true',
        ),
        (
            {"exposure": {"mode": "vpn"}, "publicService": {"enabled": True}},
            'publicService.enabled is true but exposure.mode is "vpn"',
        ),
        (
            {"exposure": {"mode": "internal"}, "publicService": {"enabled": True}},
            'publicService.enabled is true but exposure.mode is "internal"',
        ),
    ],
    ids=["public-mode-no-balancer", "balancer-in-vpn-mode", "balancer-in-internal-mode"],
)
def test_a_load_balancer_and_a_mode_that_disagree_are_refused(overlay: dict, message: str) -> None:
    """A policy opened for a load balancer that does not exist, or one no policy admits."""
    with pytest.raises(weave().WeaveError) as refused:
        _receiver("new", "aws", overlay)
    assert message in str(refused.value)


def test_a_load_balancer_carrying_8443_is_caught() -> None:
    """The dead port put back on the load balancer fails the front-door comparison."""
    dead = {"extraPorts": [{"name": "grpc-dead", "port": 8443, "public": True}]}
    old, new = _receiver("old", "local-dfe"), _receiver("new", "local-dfe", dead)
    assert DEAD_PORT in load_balancers(new)["Service/dfe-receiver-public"]["ports"]
    assert load_balancers(new) != without_dead_port(load_balancers(old))
