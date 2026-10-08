#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_weave_fetcher_culvert.py
#  Purpose:      Prove the dfe-fetcher and culvert thin charts keep their claim
#                names under a deployment's own instance values, and carry what
#                their 2.2.0 charts lacked, which a default render cannot show.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the dfe-fetcher and culvert thin charts hold beyond the default renders.

    python3 -m pytest scripts/tests/test_weave_fetcher_culvert.py -q

- a fetcher instance with its cursor claim on keeps the claim 2.2.0 named,
  dfe-fetcher-<instance>-cursor, and culvert's PKI claim stays dfe-culvert-pki,
  each given the overlay in the vocabulary scripts/weave/value-map.yaml migrates to
- the fetcher opens its vector-grpc port 6000 on the pod and a Service where its
  config turns the extractor on, which 2.2.0 never declared
- the fetcher runs one pod on every profile, and its DLQ and broker list follow
  the instance's own config
- culvert's public Service pins the nodePorts terraform/modules/edge/aws forwards
  to, from one default listener list held equal to the others
- culvert renders from layer2-edge.yaml, whose tunnel ApplicationSet is told
  from the gateway's by the instance file its git generator matches

Render (b) needs the scalo-service library (_weave.library).
"""

import json
import re
import tempfile
from pathlib import Path

import pytest
import yaml

from _gate import APPSETS, INSTANCE, PROFILES, cell
from _weave import CONTRACTS, REPO_ROOT, helm, render_app, weave

APPS = REPO_ROOT / "argocd" / "values" / "apps"
EXTRAS_VALUES = REPO_ROOT / "helm" / "charts" / "dfe-extras" / "values.yaml"
CULVERT_CHART_VALUES = REPO_ROOT / "helm" / "edge" / "culvert" / "values.yaml"
FORWARDER_VARIABLES = REPO_ROOT / "terraform" / "modules" / "edge" / "aws" / "variables.tf"


def _render(
    service: str,
    profile: str,
    cloud: str,
    which: str,
    overlay: dict,
    instance: str = "default",
) -> list[dict]:
    """One render with ``overlay`` as the instance file, over the gate's own instance values."""
    with tempfile.TemporaryDirectory(prefix="dfe-weave-fetcher-culvert-") as tmp:
        body = {
            "deploy": {"service": service, "instance": instance},
            **INSTANCE.get(service, {}).get(cloud, {}),
            **overlay,
        }
        path = Path(tmp) / "values" / f"{service}-{instance}-values.yaml"
        path.parent.mkdir()
        path.write_text(yaml.safe_dump(body), encoding="utf-8", newline="\n")
        options: dict = {"deploy_repo": Path(tmp), "instance": instance}
        if service in APPSETS:
            options["appset"] = APPSETS[service]
        return render_app(service, profile, cloud, which, **options)


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
    old = _render(
        "dfe-fetcher",
        profile,
        "aws",
        "old",
        {"component": "fetcher-acme", "persistence": {"enabled": True, "size": "2Gi"}},
        instance="acme",
    )
    new = _render(
        "dfe-fetcher",
        profile,
        "aws",
        "new",
        {
            "fullnameOverride": "dfe-fetcher-acme",
            "writablePaths": {"cursor": {"persistence": {"enabled": True, "size": "2Gi"}}},
        },
        instance="acme",
    )
    old_claims, new_claims = (
        _named(old, "PersistentVolumeClaim"),
        _named(new, "PersistentVolumeClaim"),
    )
    assert set(old_claims) == set(new_claims) == {"dfe-fetcher-acme-cursor"}
    assert _claims(old) == _claims(new) == {"dfe-fetcher-acme-cursor"}
    claim = new_claims["dfe-fetcher-acme-cursor"]["spec"]
    assert claim == {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "2Gi"}},
    }
    assert old_claims["dfe-fetcher-acme-cursor"]["spec"] == claim
    assert _deployment(new)["spec"]["strategy"] == {"type": "Recreate"}


def test_culverts_pki_claim_keeps_its_name_and_its_keep_policy() -> None:
    c = cell("culvert", "scale", "aws")
    old, new = _named(c.old, "PersistentVolumeClaim"), _named(c.new, "PersistentVolumeClaim")
    assert set(old) == set(new) == {"dfe-culvert-pki"}
    assert _claims(c.old) == _claims(c.new) == {"dfe-culvert-pki"}
    assert new["dfe-culvert-pki"]["metadata"]["annotations"] == {
        "helm.sh/resource-policy": "keep",
        "argocd.argoproj.io/sync-options": "Prune=false,Delete=false",
    }
    assert new["dfe-culvert-pki"]["spec"] == old["dfe-culvert-pki"]["spec"]


def test_a_culvert_turning_its_pki_claim_on_on_prem_keeps_the_name() -> None:
    old = _render("culvert", "single", "local", "old", {"persistence": {"enabled": True}})
    new = _render(
        "culvert",
        "single",
        "local",
        "new",
        {"writablePaths": {"pki": {"persistence": {"enabled": True}}}},
    )
    assert set(_named(old, "PersistentVolumeClaim")) == {"dfe-culvert-pki"}
    assert set(_named(new, "PersistentVolumeClaim")) == {"dfe-culvert-pki"}
    assert _claims(old) == _claims(new) == {"dfe-culvert-pki"}


@pytest.mark.parametrize("profile", PROFILES)
def test_culvert_on_prem_takes_no_claim_by_default(profile: str) -> None:
    c = cell("culvert", profile, "local")
    assert _named(c.old, "PersistentVolumeClaim") == _named(c.new, "PersistentVolumeClaim") == {}


# ----------------------------------------------------------------- the fetcher


def test_the_vector_extractor_opens_its_port_and_a_service() -> None:
    """2.2.0 declared no vector-grpc port and rendered no Service, so a Vector agent had no address."""
    overlay = {"config": {"extractors": {"vector": {"enabled": True}}}}
    old = _render("dfe-fetcher", "single", "aws", "old", overlay)
    new = _render("dfe-fetcher", "single", "aws", "new", overlay)
    assert "vector-grpc" not in _ports(_deployment(old))
    assert "Service" not in {d["kind"] for d in old}
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
    c = cell("dfe-fetcher", "single", "aws")
    assert set(_ports(_deployment(c.new))) == {"metrics"}
    assert set(_ports(_named(c.new, "Service")["dfe-fetcher"])) == {"metrics"}


def test_the_ingest_listener_opens_its_port() -> None:
    new = _render("dfe-fetcher", "single", "aws", "new", {"config": {"ingest": {"enabled": True}}})
    assert _ports(_deployment(new))["ingest"]["containerPort"] == 8080
    assert _ports(_named(new, "Service")["dfe-fetcher"])["ingest"]["port"] == 8080


@pytest.mark.parametrize("profile", PROFILES)
def test_one_fetcher_pod_on_every_profile(profile: str) -> None:
    """N replicas of one config are N duplicate polls, whatever replicaCount the profile sets."""
    c = cell("dfe-fetcher", profile, "aws")
    assert _deployment(c.old)["spec"]["replicas"] == _deployment(c.new)["spec"]["replicas"] == 1


def test_an_instance_compiled_to_grpc_on_the_bus_turns_its_dlq_off() -> None:
    overlay = {"config": {"output": {"type": "grpc"}}}
    old, new = (_env(_render("dfe-fetcher", "scale", "aws", w, overlay)) for w in ("old", "new"))
    assert (
        old["DFE_FETCHER_DLQ_ENABLED"]["value"]
        == new["DFE_FETCHER_DLQ_ENABLED"]["value"]
        == "false"
    )
    assert "DFE_FETCHER_DLQ_TOPIC" not in old
    assert new["DFE_FETCHER_DLQ_TOPIC"]["value"] == new["DFE_FETCHER_DLQ_MODE"]["value"] == ""


def test_an_overlays_own_brokers_win() -> None:
    """Flat env beats the file, so the chart's broker list stands back where the overlay names one."""
    overlay = {"config": {"kafka": {"brokers": ["broker.example.com:9092"]}}}
    new = _render("dfe-fetcher", "single", "aws", "new", overlay)
    assert _env(new)["DFE_FETCHER_KAFKA_BROKERS"]["value"] == ""
    configmap = _named(new, "ConfigMap")["dfe-fetcher-config"]["data"]["fetcher.yaml"]
    assert yaml.safe_load(configmap)["kafka"]["brokers"] == ["broker.example.com:9092"]


def test_the_bus_hands_the_fetcher_the_deployments_broker() -> None:
    env = _env(cell("dfe-fetcher", "scale", "aws").new)
    assert env["DFE_FETCHER_KAFKA_BROKERS"]["value"] == (
        "dfe-kafka-kafka-bootstrap.strimzi.svc.cluster.local:9092"
    )


def test_the_fetcher_takes_the_upstream_credentials_whole_and_optional() -> None:
    c = cell("dfe-fetcher", "single", "aws")
    want = [{"secretRef": {"name": "dfe-fetcher-credentials", "optional": True}}]
    assert _pod(c.old)["containers"][0]["envFrom"] == want
    assert _pod(c.new)["containers"][0]["envFrom"] == want


# ------------------------------------------------------------------- culvert


def test_culvert_renders_from_the_edge_appset_with_its_flavour_file() -> None:
    """layer2-edge.yaml holds two ApplicationSets, and the tunnel's is the one read."""
    w = weave()
    target = w.Target("culvert", "single", "aws")
    app = w.application(REPO_ROOT, APPSETS["culvert"], target, None, helm())
    source = app["spec"]["sources"][0]
    assert app["metadata"]["name"] == "culvert-default-in-cluster"
    assert source["path"] == "helm/edge/culvert"
    assert "../../../argocd/values/edge-aws.yaml" in source["helm"]["valueFiles"]


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
    """culvert's default listeners as the thin chart, dfe-extras and the culvert chart hold them."""
    apps = yaml.safe_load((APPS / "culvert" / "values.yaml").read_text(encoding="utf-8"))
    extras = yaml.safe_load(EXTRAS_VALUES.read_text(encoding="utf-8"))["extras"]["defaults"]
    chart = yaml.safe_load(CULVERT_CHART_VALUES.read_text(encoding="utf-8"))
    return {
        "apps/culvert defaultListeners": apps["defaultListeners"],
        "dfe-extras defaults": extras["culvert"]["listeners"],
        "helm/edge/culvert listeners": chart["listeners"],
    }


def drifted(lists: dict[str, list]) -> list[str]:
    """The lists that differ from the first; empty is a pass."""
    first = next(iter(lists.values()))
    return sorted(where for where, listeners in lists.items() if listeners != first)


def test_culverts_nodeports_are_the_ones_the_forwarder_dials() -> None:
    service = _named(cell("culvert", "scale", "aws").new, "Service")["dfe-culvert-public-udp"]
    assert service["spec"]["type"] == "NodePort"
    assert node_port_problems(service) == []


def test_one_default_listener_list_for_culvert() -> None:
    """The thin chart's default, dfe-extras' and the culvert chart's own are the same list."""
    assert drifted(listener_lists()) == []


def test_culvert_runs_as_the_contract_says() -> None:
    contract = json.loads((CONTRACTS / "culvert.json").read_text(encoding="utf-8"))
    pod = _pod(cell("culvert", "single", "aws").new)
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
        "new",
        {"writablePaths": {"cursor": {"persistence": {"enabled": True}}}},
        instance="acme",
    )
    assert set(_named(new, "PersistentVolumeClaim")) == {"dfe-fetcher-cursor"}


def test_a_drifted_default_listener_list_is_seen() -> None:
    lists = listener_lists()
    first = lists["apps/culvert defaultListeners"]
    lists["apps/culvert defaultListeners"] = [{**first[0], "nodePort": 31821}, *first[1:]]
    assert drifted(lists) == ["dfe-extras defaults", "helm/edge/culvert listeners"]


def test_a_nodeport_the_forwarder_does_not_dial_is_seen() -> None:
    service = _named(cell("culvert", "scale", "aws").new, "Service")["dfe-culvert-public-udp"]
    moved = {
        **service,
        "spec": {**service["spec"], "ports": [dict(p) for p in service["spec"]["ports"]]},
    }
    next(p for p in moved["spec"]["ports"] if p["name"] == "wireguard")["nodePort"] = 31821
    assert node_port_problems(moved) == ["wireguard: 31821 -> 31820"]
