#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_weave_fetcher_culvert.py
#  Purpose:      Prove the dfe-fetcher and culvert thin charts keep their claim
#                names under a deployment's own instance values, and open what
#                their configs ask for, which a default render cannot show.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the dfe-fetcher and culvert thin charts hold beyond the default renders.

    python3 -m pytest scripts/tests/test_weave_fetcher_culvert.py -q

- a fetcher instance with its cursor claim on names it dfe-fetcher-<instance>-cursor,
  and culvert's PKI claim stays dfe-culvert-pki, kept through a prune
- the fetcher opens its vector-grpc port 6000 on the pod and a Service where its
  config turns the extractor on
- the fetcher runs one pod on every profile, and its DLQ and broker list follow
  the instance's own config
- culvert's public Service pins the nodePorts terraform/modules/edge/aws forwards
  to, from one default listener list held equal to dfe-extras' copy
- culvert renders from layer2-edge.yaml, whose tunnel ApplicationSet is told
  from the gateway's by the instance file its git generator matches
- culvert's image, and its init container's, follow global.registry like the
  dfe-* images, and fall back to the contract's registry where none is set
- culvert's contract opens its WireGuard port only where config.protocol starts
  WireGuard, and its profile mounts as one file beside the image's own

The default renders are the goldens (_gate.golden). The overlay cases render
live, which needs the scalo-service library (_weave.library).
"""

import json
import re
from pathlib import Path

import pytest
import yaml

from _gate import APPSETS, INSTANCE, PROFILES, configmap_files, golden
from _weave import CONTRACTS, REPO_ROOT, appset, helm, library, render_app, weave

APPS = REPO_ROOT / "argocd" / "values" / "apps"
COMMON = REPO_ROOT / "argocd" / "values" / "common.yaml"
EXTRAS_VALUES = REPO_ROOT / "helm" / "charts" / "dfe-extras" / "values.yaml"
FORWARDER_VARIABLES = REPO_ROOT / "terraform" / "modules" / "edge" / "aws" / "variables.tf"


def _render(
    service: str,
    profile: str,
    cloud: str,
    overlay: dict,
    instance: str = "default",
    **facts: str,
) -> list[dict]:
    """One render with ``overlay`` as the instance file, over the matrix's own instance values.

    ``facts`` are cluster-secret facts, such as ``registry``.
    """
    body = {**INSTANCE.get(service, {}).get(cloud, {}), **overlay}
    options: dict = {"instance": instance, **facts}
    if service in APPSETS:
        options["appset"] = APPSETS[service]
    return render_app(service, profile, cloud, overlay=body, **options)


def _named(docs: list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def _deployment(docs: list[dict]) -> dict:
    return next(iter(_named(docs, "Deployment").values()))


def _pod(docs: list[dict]) -> dict:
    return _deployment(docs)["spec"]["template"]["spec"]


def _env(docs: list[dict]) -> dict[str, dict]:
    return {e["name"]: e for e in _pod(docs)["containers"][0].get("env") or []}


def _claims(docs: list[dict]) -> set[str]:
    return {
        v["persistentVolumeClaim"]["claimName"]
        for v in _pod(docs).get("volumes") or []
        if "persistentVolumeClaim" in v
    }


def _ports(doc: dict) -> dict[str, dict]:
    if doc["kind"] == "Service":
        return {p["name"]: p for p in doc["spec"]["ports"]}
    return {p["name"]: p for p in doc["spec"]["template"]["spec"]["containers"][0]["ports"]}


# --------------------------------------------------------------------- the claims


@pytest.mark.parametrize("profile", ["single", "mesh"])
def test_a_fetcher_instance_keeps_its_cursor_claim(profile: str) -> None:
    new = _render(
        "dfe-fetcher",
        profile,
        "aws",
        {
            "fullnameOverride": "dfe-fetcher-acme",
            "writablePaths": {"cursor": {"persistence": {"enabled": True, "size": "2Gi"}}},
        },
        instance="acme",
    )
    claims = _named(new, "PersistentVolumeClaim")
    assert set(claims) == _claims(new) == {"dfe-fetcher-acme-cursor"}
    assert claims["dfe-fetcher-acme-cursor"]["spec"] == {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "2Gi"}},
    }
    assert _deployment(new)["spec"]["strategy"] == {"type": "Recreate"}


def test_culverts_pki_claim_keeps_its_name_and_its_keep_policy() -> None:
    docs = golden("culvert", "scale", "aws")
    claims = _named(docs, "PersistentVolumeClaim")
    assert set(claims) == _claims(docs) == {"dfe-culvert-pki"}
    assert claims["dfe-culvert-pki"]["metadata"]["annotations"] == {
        "helm.sh/resource-policy": "keep",
        "argocd.argoproj.io/sync-options": "Prune=false,Delete=false",
    }
    assert claims["dfe-culvert-pki"]["spec"] == {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "1Gi"}},
    }


def test_a_culvert_turning_its_pki_claim_on_on_prem_keeps_the_name() -> None:
    new = _render(
        "culvert", "single", "local", {"writablePaths": {"pki": {"persistence": {"enabled": True}}}}
    )
    assert set(_named(new, "PersistentVolumeClaim")) == _claims(new) == {"dfe-culvert-pki"}


@pytest.mark.parametrize("profile", PROFILES)
def test_culvert_on_prem_takes_no_claim_by_default(profile: str) -> None:
    assert _named(golden("culvert", profile, "local"), "PersistentVolumeClaim") == {}


# ----------------------------------------------------------------- the fetcher


def test_the_vector_extractor_opens_its_port_and_a_service() -> None:
    """A Vector agent dials the fetcher's vector-grpc port: it needs the pod port and a Service."""
    overlay = {"config": {"extractors": {"vector": {"enabled": True}}}}
    new = _render("dfe-fetcher", "single", "aws", overlay)
    assert _ports(_deployment(new))["vector-grpc"] == {
        "name": "vector-grpc",
        "containerPort": 6000,
        "protocol": "TCP",
    }
    service = _named(new, "Service")["dfe-fetcher"]
    assert _ports(service)["vector-grpc"] == {
        "name": "vector-grpc",
        "port": 6000,
        "targetPort": "vector-grpc",
        "protocol": "TCP",
        "appProtocol": "kubernetes.io/h2c",
    }


def test_the_extractor_ports_stay_shut_until_the_config_opens_them() -> None:
    docs = golden("dfe-fetcher", "single", "aws")
    assert set(_ports(_deployment(docs))) == {"metrics"}
    assert set(_ports(_named(docs, "Service")["dfe-fetcher"])) == {"metrics"}


def test_the_ingest_listener_opens_its_port() -> None:
    new = _render("dfe-fetcher", "single", "aws", {"config": {"ingest": {"enabled": True}}})
    assert _ports(_deployment(new))["ingest"]["containerPort"] == 8080
    assert _ports(_named(new, "Service")["dfe-fetcher"])["ingest"]["port"] == 8080


@pytest.mark.parametrize("profile", PROFILES)
def test_one_fetcher_pod_on_every_profile(profile: str) -> None:
    """N replicas of one config are N duplicate polls, whatever replicaCount the profile sets."""
    assert _deployment(golden("dfe-fetcher", profile, "aws"))["spec"]["replicas"] == 1


def test_an_instance_compiled_to_grpc_on_the_bus_turns_its_dlq_off() -> None:
    overlay = {"config": {"output": {"type": "grpc"}}}
    env = _env(_render("dfe-fetcher", "scale", "aws", overlay))
    assert env["DFE_FETCHER_DLQ_ENABLED"]["value"] == "false"
    assert env["DFE_FETCHER_DLQ_TOPIC"]["value"] == env["DFE_FETCHER_DLQ_MODE"]["value"] == ""


def test_an_overlays_own_brokers_win() -> None:
    """Flat env beats the file, so the chart's broker list stands back where the overlay names one."""
    overlay = {"config": {"kafka": {"brokers": ["broker.example.com:9092"]}}}
    new = _render("dfe-fetcher", "single", "aws", overlay)
    assert _env(new)["DFE_FETCHER_KAFKA_BROKERS"]["value"] == ""
    configmap = configmap_files(_named(new, "ConfigMap")["dfe-fetcher-config"])["fetcher.yaml"]
    assert yaml.safe_load(configmap)["kafka"]["brokers"] == ["broker.example.com:9092"]


def test_the_bus_hands_the_fetcher_the_deployments_broker() -> None:
    env = _env(golden("dfe-fetcher", "scale", "aws"))
    assert env["DFE_FETCHER_KAFKA_BROKERS"]["value"] == (
        "dfe-kafka-kafka-bootstrap.strimzi.svc.cluster.local:9092"
    )


def test_the_fetcher_takes_the_upstream_credentials_whole_and_optional() -> None:
    want = [{"secretRef": {"name": "dfe-fetcher-credentials", "optional": True}}]
    assert _pod(golden("dfe-fetcher", "single", "aws"))["containers"][0]["envFrom"] == want


# ------------------------------------------------------------------- culvert


def test_culvert_renders_from_the_edge_appset_with_its_flavour_file() -> None:
    """layer2-edge.yaml holds two ApplicationSets, and the tunnel's is the one read."""
    w = weave()
    target = w.Target("culvert", "single", "aws")
    app = w.application(REPO_ROOT, appset(APPSETS["culvert"]), target, None, helm())
    source = app["spec"]["sources"][0]
    assert app["metadata"]["name"] == "culvert-default-in-cluster"
    assert (source["repoURL"].rsplit("/", 1)[-1], source["path"]) == ("culvert", ".")
    assert "$infra/argocd/values/edge-aws.yaml" in source["helm"]["valueFiles"]


@pytest.mark.parametrize(
    "overlay",
    [{}, {"global": {"registry": "other.example.org/dfe"}}],
    ids=["cluster-registry", "over-an-instance-registry"],
)
def test_culverts_image_follows_global_registry(overlay: dict) -> None:
    """The tunnel and its sysctl init container pull <registry>/culvert, so a mirror moves them too.

    The edge appset hands global.registry from the cluster secret, the registry the
    chart itself comes from, and a parameter beats the instance file.
    """
    registry = "mirror.example.net/dfe"
    pod = _pod(_render("culvert", "slim", "local", overlay, registry=registry))
    images = [c["image"] for c in [*pod["initContainers"], *pod["containers"]]]
    assert len(images) == 2
    assert all(image.startswith(f"{registry}/culvert:") for image in images), images


def test_culvert_falls_back_to_its_contract_registry_with_none_set(bare_culvert: Path) -> None:
    """The thin chart and its integration values, with common.yaml's empty global.registry."""
    w = weave()
    cmd = [helm(), "template", "culvert", str(bare_culvert)]
    for layer in (COMMON, APPS / "_common.yaml", APPS / "culvert" / "values.yaml"):
        cmd += ["--values", str(layer)]
    pod = _pod(w.documents(w.run(cmd, "helm template culvert")))
    images = [c["image"] for c in [*pod["initContainers"], *pod["containers"]]]
    assert len(images) == 2
    assert all(image.startswith("ghcr.io/hyperi-io/culvert:") for image in images), images


def test_an_image_repository_still_replaces_culverts_whole_image() -> None:
    overlay = {"global": {"registry": "mirror.example.net/dfe"}, "image": {"repository": "r/c"}}
    pod = _pod(_render("culvert", "slim", "local", overlay))
    assert all(c["image"].startswith("r/c:") for c in [*pod["initContainers"], *pod["containers"]])


def _two_appsets(root: Path, *patterns: str) -> Path:
    """A tree whose one appset file holds an ApplicationSet per git-files pattern."""
    docs = [
        {
            "kind": "ApplicationSet",
            "metadata": {"name": f"set-{i}"},
            "spec": {
                "goTemplate": True,
                "generators": [{"git": {"files": [{"path": pattern}]}}],
                "template": {"metadata": {"name": f"from-set-{i}"}},
            },
        }
        for i, pattern in enumerate(patterns)
    ]
    path = root / "argocd" / "appsets" / "two.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump_all(docs), encoding="utf-8", newline="\n")
    return Path("argocd/appsets/two.yaml")


def _culverts_appset(tmp_path: Path, *patterns: str) -> dict:
    w = weave()
    appset = _two_appsets(tmp_path, *patterns)
    return w.load_appset(tmp_path, appset, w.Target("culvert", "slim", "local"))


def test_the_appset_generating_the_instance_file_is_the_one_read(tmp_path: Path) -> None:
    found = _culverts_appset(tmp_path, "values/dfe-*-values.yaml", "values/culvert-*-values.yaml")
    assert found["metadata"]["name"] == "set-1"


@pytest.mark.parametrize(
    ("patterns", "count"),
    [
        (("values/dfe-*-values.yaml", "values/hyperdx-*-values.yaml"), 0),
        (("values/*-values.yaml", "values/culvert-*-values.yaml"), 2),
    ],
    ids=["none", "both"],
)
def test_an_appset_file_that_does_not_name_one_is_refused(
    tmp_path: Path, patterns: tuple[str, str], count: int
) -> None:
    with pytest.raises(weave().WeaveError, match=f"{count} ApplicationSets generate"):
        _culverts_appset(tmp_path, *patterns)


def _tf_node_port(name: str) -> int:
    text = FORWARDER_VARIABLES.read_text(encoding="utf-8")
    found = re.search(rf"{name}\s*=\s*optional\(number,\s*(\d+)\)", text)
    assert found, f"no {name} node port default in {FORWARDER_VARIABLES}"
    return int(found.group(1))


def node_port_problems(service: dict) -> list[str]:
    """Each tunnel port of a public Service not pinned where the forwarder DNATs; empty is a pass."""
    want = {"wireguard": _tf_node_port("wireguard"), "openvpn-udp": _tf_node_port("openvpn")}
    pins = {p["name"]: p.get("nodePort") for p in service["spec"]["ports"]}
    return [
        f"{name}: {pins.get(name)} -> {port}"
        for name, port in want.items()
        if pins.get(name) != port
    ]


def listener_lists() -> dict[str, list]:
    """culvert's default listeners as the thin chart's values and dfe-extras hold them."""
    apps = yaml.safe_load((APPS / "culvert" / "values.yaml").read_text(encoding="utf-8"))
    extras = yaml.safe_load(EXTRAS_VALUES.read_text(encoding="utf-8"))["extras"]["defaults"]
    return {
        "apps/culvert defaultListeners": apps["defaultListeners"],
        "dfe-extras defaults": extras["culvert"]["listeners"],
    }


def drifted(lists: dict[str, list]) -> list[str]:
    """The lists that differ from the first; empty is a pass."""
    first = next(iter(lists.values()))
    return sorted(where for where, listeners in lists.items() if listeners != first)


def test_culverts_nodeports_are_the_ones_the_forwarder_dials() -> None:
    service = _named(golden("culvert", "scale", "aws"), "Service")["dfe-culvert-public-udp"]
    assert service["spec"]["type"] == "NodePort"
    assert node_port_problems(service) == []


def test_one_default_listener_list_for_culvert() -> None:
    """The thin chart's default and dfe-extras' are the same list."""
    assert drifted(listener_lists()) == []


@pytest.fixture(scope="module")
def bare_culvert(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """culvert's thin chart from its contract alone, with no DFE values and no dfe-extras."""
    w = weave()
    chart = w.assemble(
        (CONTRACTS / "culvert.json").read_bytes(),
        library(),
        tmp_path_factory.mktemp("bare-culvert"),
        version="0.0.0",
        tag="v0.0.0",
        digest=f"sha256:{'0' * 64}",
    )
    w.build_dependency(helm(), chart)
    return chart


@pytest.mark.parametrize(
    ("protocol", "opened"),
    [(None, False), ("openvpn", False), ("wireguard", True), ("both", True)],
    ids=["unset", "openvpn", "wireguard", "both"],
)
def test_the_contract_opens_wireguard_only_where_the_profile_starts_it(
    bare_culvert: Path, protocol: str | None, opened: bool
) -> None:
    """A server on the default OpenVPN has no WireGuard port, so none reaches a balancer.

    The bare chart, because DFE declares the port itself and dfe-extras refuses a
    config.protocol (test_dfe_extras.py).
    """
    w = weave()
    cmd = [helm(), "template", "culvert", str(bare_culvert), "--set", "publicService.enabled=true"]
    if protocol is not None:
        cmd += ["--set", f"config.protocol={protocol}"]
    docs = w.documents(w.run(cmd, "helm template culvert"))
    balancer = _named(docs, "Service")["culvert-public-udp"]
    assert ("wireguard" in _ports(_deployment(docs))) is opened
    assert ("wireguard" in _ports(balancer)) is opened
    assert "openvpn-udp" in _ports(balancer)


def test_the_profile_mounts_beside_the_shipped_ones() -> None:
    """The ConfigMap mounts as culvert.yaml alone, so /etc/vpn/profiles keeps the image's profiles."""
    container = _pod(golden("culvert", "single", "aws"))["containers"][0]
    mounts = {m["mountPath"]: m for m in container["volumeMounts"]}
    assert "/etc/vpn/profiles" not in mounts
    assert mounts["/etc/vpn/profiles/culvert.yaml"]["subPath"] == "culvert.yaml"


def test_culvert_runs_as_the_contract_says() -> None:
    contract = json.loads((CONTRACTS / "culvert.json").read_text(encoding="utf-8"))
    pod = _pod(golden("culvert", "single", "aws"))
    assert contract["singleton"] is True
    assert pod["securityContext"]["runAsUser"] == contract["security"]["run_as_user"] == 0
    added = pod["containers"][0]["securityContext"]["capabilities"]["add"]
    assert added == contract["security"]["capabilities_add"]
    assert [i["name"] for i in pod["initContainers"]] == ["enable-ip-forward"]


# ---------------------------------------------------------------- expected fails


def test_a_renamed_cursor_claim_is_seen() -> None:
    """The same overlay without fullnameOverride names the claim after the chart, not the instance."""
    new = _render(
        "dfe-fetcher",
        "single",
        "aws",
        {"writablePaths": {"cursor": {"persistence": {"enabled": True}}}},
        instance="acme",
    )
    assert set(_named(new, "PersistentVolumeClaim")) == {"dfe-fetcher-cursor"}


def test_a_drifted_default_listener_list_is_seen() -> None:
    lists = listener_lists()
    first = lists["apps/culvert defaultListeners"]
    lists["apps/culvert defaultListeners"] = [{**first[0], "nodePort": 31821}, *first[1:]]
    assert drifted(lists) == ["dfe-extras defaults"]


def test_a_nodeport_the_forwarder_does_not_dial_is_seen() -> None:
    service = _named(golden("culvert", "scale", "aws"), "Service")["dfe-culvert-public-udp"]
    moved = {
        **service,
        "spec": {**service["spec"], "ports": [dict(p) for p in service["spec"]["ports"]]},
    }
    next(p for p in moved["spec"]["ports"] if p["name"] == "wireguard")["nodePort"] = 31821
    assert node_port_problems(moved) == ["wireguard: 31821 -> 31820"]
