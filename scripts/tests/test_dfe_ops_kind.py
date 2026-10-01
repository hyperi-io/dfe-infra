#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_kind.py
#  Purpose:      Guard `dfe-ops kind` -- the rendered kind config, the front-door
#                address choice, the StorageClass glue, the env-file precedence
#                that keeps estate values off a kind cluster, the stack-deploy
#                argv, and which network `down` may remove.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe_ops_kind.py.

    python3 -m pytest scripts/tests/test_dfe_ops_kind.py -q

Everything here is cluster-free: the pure render and planning functions, and the
parser dfe-ops builds. `dfe-ops kind up` itself is proven by running it.
"""

import argparse
import importlib.machinery
import importlib.util
import ipaddress
import json
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
import dfe_ops_kind as kind  # noqa: E402
import envfile  # noqa: E402
import private_file  # noqa: E402

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_kind", str(SCRIPTS / "dfe-ops"))
_spec = importlib.util.spec_from_loader("dfeops_kind", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_kind"] = dfeops
_loader.exec_module(dfeops)


def _up_args(**overrides: object) -> argparse.Namespace:
    argv = ["kind", "up", "--ref", "fix/some-cut"]
    args = dfeops.build_parser().parse_args(argv)
    args.default_repo_url = "https://git.example.com/org/dfe-infra.git"
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


# ---------------------------------------------------------------------------
# The kind cluster config
# ---------------------------------------------------------------------------


def test_cluster_config_pins_the_node_image_and_binds_loopback() -> None:
    config = json.loads(kind.render_cluster_config("dfe-kind", kind.KIND_NODE_IMAGE, 0, 0))
    assert config["kind"] == "Cluster"
    assert config["apiVersion"] == "kind.x-k8s.io/v1alpha4"
    assert config["name"] == "dfe-kind"
    assert config["networking"] == {"apiServerAddress": "127.0.0.1", "apiServerPort": 0}
    assert config["nodes"] == [{"role": "control-plane", "image": kind.KIND_NODE_IMAGE}]


def test_cluster_config_adds_workers_on_the_same_image() -> None:
    config = json.loads(kind.render_cluster_config("x", "img@sha256:" + "a" * 64, 16443, 2))
    assert [n["role"] for n in config["nodes"]] == ["control-plane", "worker", "worker"]
    assert {n["image"] for n in config["nodes"]} == {"img@sha256:" + "a" * 64}
    assert config["networking"]["apiServerPort"] == 16443


@pytest.mark.parametrize(("api_port", "workers"), [(-1, 0), (65536, 0), (0, -1)])
def test_cluster_config_refuses_an_impossible_port_or_worker_count(
    api_port: int, workers: int
) -> None:
    with pytest.raises(kind.KindError):
        kind.render_cluster_config("x", kind.KIND_NODE_IMAGE, api_port, workers)


def test_cluster_config_has_kubelets_ask_the_cluster_ca_for_serving_certs() -> None:
    config = json.loads(kind.render_cluster_config("x", kind.KIND_NODE_IMAGE, 0, 0))
    assert config["kubeadmConfigPatches"] == [
        "kind: KubeletConfiguration\nserverTLSBootstrap: true\n"
    ]


def _csr(name: str, node: str, signer: str, conditions: list | None = None) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"signerName": signer, "username": f"system:node:{node}"},
        "status": {"conditions": conditions} if conditions else {},
    }


SERVING = "kubernetes.io/kubelet-serving"
CLIENT = "kubernetes.io/kube-apiserver-client-kubelet"
APPROVED = [{"type": "Approved", "status": "True"}]


def test_only_undecided_serving_csrs_from_our_nodes_are_approved() -> None:
    csrs = {
        "items": [
            _csr("csr-b", "dfe-kind-control-plane", SERVING),
            _csr("csr-a", "dfe-kind-worker", SERVING),
            _csr("csr-client", "dfe-kind-control-plane", CLIENT),
            _csr("csr-done", "dfe-kind-control-plane", SERVING, APPROVED),
            _csr("csr-denied", "dfe-kind-worker", SERVING, [{"type": "Denied"}]),
            _csr("csr-stranger", "someone-else", SERVING),
        ]
    }
    nodes = {"dfe-kind-control-plane", "dfe-kind-worker"}
    assert kind.pending_serving_csrs(csrs, nodes) == ["csr-a", "csr-b"]


def test_serving_nodes_counts_approved_serving_csrs_only() -> None:
    csrs = {
        "items": [
            _csr("a", "n1", SERVING, APPROVED),
            _csr("b", "n2", SERVING),
            _csr("c", "n3", CLIENT, APPROVED),
        ]
    }
    assert kind.serving_nodes(csrs) == {"n1"}


def test_node_image_is_pinned_by_digest() -> None:
    name, _, digest = kind.KIND_NODE_IMAGE.partition("@")
    assert name.startswith("kindest/node:v")
    assert digest.startswith("sha256:")
    assert len(digest.removeprefix("sha256:")) == 64


# ---------------------------------------------------------------------------
# The front door: two addresses at the top of the kind network
# ---------------------------------------------------------------------------

INSPECT = [
    {
        "Name": "dfe-kind",
        "IPAM": {
            "Config": [
                {"Subnet": "fc00:f853:ccd:e793::/64"},
                {"Subnet": "198.51.100.0/24", "Gateway": "198.51.100.1"},
            ]
        },
        "Containers": {
            "abc": {"IPv4Address": "198.51.100.2/24"},
            "def": {"IPv4Address": "198.51.100.254/24"},
        },
    }
]


def test_network_subnet_takes_the_ipv4_one_beside_ipv6() -> None:
    assert kind.network_subnet(INSPECT) == ipaddress.ip_network("198.51.100.0/24")


def test_network_subnet_refuses_a_network_with_no_ipv4() -> None:
    with pytest.raises(kind.KindError, match="IPv4"):
        kind.network_subnet([{"IPAM": {"Config": [{"Subnet": "fc00::/64"}]}}])


def test_front_door_takes_the_top_two_free_addresses() -> None:
    subnet = ipaddress.ip_network("198.51.100.0/24")
    assert kind.front_door_addresses(subnet, set()) == ("198.51.100.254", "198.51.100.253")


def test_front_door_skips_an_address_a_container_already_holds() -> None:
    subnet = kind.network_subnet(INSPECT)
    taken = kind.network_addresses(INSPECT)
    assert kind.front_door_addresses(subnet, taken) == ("198.51.100.253", "198.51.100.252")


def test_front_door_refuses_a_subnet_too_small_to_share() -> None:
    with pytest.raises(kind.KindError, match="too small"):
        kind.front_door_addresses(ipaddress.ip_network("198.51.100.0/29"), set())


def test_front_door_refuses_a_full_subnet() -> None:
    subnet = ipaddress.ip_network("198.51.100.0/28")
    with pytest.raises(kind.KindError, match="no two free"):
        kind.front_door_addresses(subnet, set(subnet.hosts()))


def test_an_address_outside_the_network_is_refused() -> None:
    subnet = ipaddress.ip_network("198.51.100.0/24")
    kind.check_in_subnet("198.51.100.10", subnet, "--gateway-ip")
    with pytest.raises(kind.KindError, match="outside"):
        kind.check_in_subnet("203.0.113.10", subnet, "--gateway-ip")
    with pytest.raises(kind.KindError, match="not an IPv4"):
        kind.check_in_subnet("gateway", subnet, "--gateway-ip")


# ---------------------------------------------------------------------------
# The StorageClass the overlay asks for, on kind's own provisioner
# ---------------------------------------------------------------------------

DEFAULT = "storageclass.kubernetes.io/is-default-class"


def test_default_provisioner_reads_the_default_class() -> None:
    classes = {
        "items": [
            {"metadata": {"name": "other"}, "provisioner": "example.com/other"},
            {
                "metadata": {"name": "standard", "annotations": {DEFAULT: "true"}},
                "provisioner": "rancher.io/local-path",
            },
        ]
    }
    assert kind.default_provisioner(classes) == "rancher.io/local-path"


def test_default_provisioner_is_none_without_a_default() -> None:
    assert (
        kind.default_provisioner({"items": [{"metadata": {"name": "x"}, "provisioner": "p"}]})
        is None
    )


def test_storage_class_is_not_marked_default() -> None:
    manifest = json.loads(kind.render_storage_class("local-path", "rancher.io/local-path"))
    assert manifest["kind"] == "StorageClass"
    assert manifest["metadata"]["name"] == "local-path"
    assert manifest["provisioner"] == "rancher.io/local-path"
    assert manifest["volumeBindingMode"] == "WaitForFirstConsumer"
    assert DEFAULT not in manifest["metadata"].get("annotations", {})


# ---------------------------------------------------------------------------
# What the deploy reads: env files only, kind facts last
# ---------------------------------------------------------------------------


def test_child_env_drops_every_dfe_variable_and_the_ambient_kubeconfig(tmp_path: Path) -> None:
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "KUBECONFIG": "/home/x/.kube/other",
        "DFE_CONFIG_REPO_URL": "https://git.example.com/estate/deploy.git",
        "DFE_VAULT_SECRET_ID": "estate",
    }
    env = kind.child_env(tmp_path / "kubeconfig", base)
    assert env["KUBECONFIG"] == str(tmp_path / "kubeconfig")
    assert env["PATH"] == "/usr/bin"
    assert not [key for key in env if key.startswith("DFE_")]


def test_kind_facts_are_forced_over_the_operator_files() -> None:
    operator = {
        "DFE_CLOUD": "local",
        "DFE_GATEWAY_IP": "192.0.2.10",
        "DFE_DNS_PROVIDER": "rfc2136",
        "DFE_CONFIG_REPO_URL": "https://git.example.com/estate/deploy.git",
        "DFE_CA_PERSIST": "true",
    }
    defaults, facts = kind.plan_env(_up_args(), operator, {}, "198.51.100.254", "198.51.100.253")
    assert facts["DFE_CLOUD"] == kind.DEFAULT_CLOUD_OVERLAY
    assert facts["DFE_STORAGE_CLASS"] == kind.DEFAULT_STORAGE_CLASS
    assert facts["DFE_GATEWAY_IP"] == "198.51.100.254"
    assert facts["DFE_RECEIVER_IP"] == "198.51.100.253"
    assert facts["DFE_KUBE_CONTEXT"] == "kind-dfe-kind"
    assert facts["DFE_DNS_PROVIDER"] == "none"
    assert facts["DFE_CONFIG_REPO_URL"] == ""
    assert facts["DFE_CA_PERSIST"] == "false"
    assert facts["DFE_WORKLOAD_IDENTITY_ANNOTATIONS"] == "{}"
    assert not set(defaults) & set(facts)


def test_ca_persist_is_an_explicit_opt_in() -> None:
    _, facts = kind.plan_env(_up_args(ca_persist=True), {}, {}, "a", "b")
    assert facts["DFE_CA_PERSIST"] == "true"


def test_an_unset_flag_is_a_default_the_operator_may_override() -> None:
    defaults, facts = kind.plan_env(_up_args(), {}, {}, "a", "b")
    assert defaults["DFE_NAMESPACE"] == kind.DEFAULT_NAMESPACE
    assert defaults["DFE_ENV"] == kind.DEFAULT_ENV
    assert defaults["DFE_REPO_URL"] == "https://git.example.com/org/dfe-infra.git"
    assert defaults["DFE_BASE_DOMAIN"] == kind.DEFAULT_BASE_DOMAIN
    assert "DFE_NAMESPACE" not in facts


def test_a_given_flag_beats_the_operator_files() -> None:
    args = _up_args(
        namespace="dfe-kind",
        env="test",
        repo_url="ssh://git.example.com/o/r.git",
        base_domain="kind.example.com",
    )
    defaults, facts = kind.plan_env(args, {"DFE_NAMESPACE": "dfe-estate"}, {}, "a", "b")
    assert facts["DFE_NAMESPACE"] == "dfe-kind"
    assert facts["DFE_ENV"] == "test"
    assert facts["DFE_REPO_URL"] == "ssh://git.example.com/o/r.git"
    assert facts["DFE_BASE_DOMAIN"] == "kind.example.com"
    assert "DFE_NAMESPACE" not in defaults


def test_no_base_domain_is_added_beside_a_declared_domain() -> None:
    defaults, facts = kind.plan_env(_up_args(), {"DFE_DOMAIN": "dfe.example.com"}, {}, "a", "b")
    assert "DFE_BASE_DOMAIN" not in defaults
    assert "DFE_BASE_DOMAIN" not in facts


def test_in_cluster_service_names_come_from_the_refs_env_template() -> None:
    template = {
        "DFE_CLICKHOUSE_HOST": "ch.clickhouse.svc.cluster.local",
        "DFE_OTEL_ENDPOINT": "otel.otel.svc.cluster.local:4317",
        "DFE_VAULT_ADDR": "https://bao.example.com:8200",
    }
    defaults, facts = kind.plan_env(_up_args(), {}, template, "a", "b")
    assert defaults["DFE_CLICKHOUSE_HOST"] == "ch.clickhouse.svc.cluster.local"
    assert defaults["DFE_OTEL_ENDPOINT"] == "otel.otel.svc.cluster.local:4317"
    # A template placeholder for anything else would reach a real deploy.
    assert "DFE_VAULT_ADDR" not in defaults
    assert "DFE_VAULT_ADDR" not in facts


def test_env_file_round_trips_and_is_private(tmp_path: Path) -> None:
    values = {
        "DFE_CONFIG_REPO_URL": "",
        "DFE_WORKLOAD_IDENTITY_ANNOTATIONS": "{}",
        "DFE_ENV": "local",
    }
    path = tmp_path / "facts.env"
    private_file.write_private(path, kind.render_env_file(values))
    assert envfile.parse_env_file(path) == values
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("value", ['a"b', "a\nb"])
def test_env_file_refuses_a_value_it_cannot_hold(value: str) -> None:
    with pytest.raises(kind.KindError):
        kind.render_env_file({"DFE_X": value})


# ---------------------------------------------------------------------------
# The ref's own stack-deploy
# ---------------------------------------------------------------------------


def _argv(args: argparse.Namespace, tmp_path: Path) -> list[str]:
    return kind.stack_deploy_argv(
        args,
        tmp_path / "src",
        tmp_path / "kubeconfig",
        "a" * 40,
        tmp_path / "defaults.env",
        tmp_path / "facts.env",
        tmp_path / "access.md",
    )


def _env_files(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, token in enumerate(argv) if token == "--env-file"]


def test_stack_deploy_runs_from_the_ref_checkout_pinned_to_the_commit(tmp_path: Path) -> None:
    argv = _argv(_up_args(), tmp_path)
    assert argv[1] == str(tmp_path / "src" / "scripts" / "dfe-ops")
    assert argv[2] == "stack-deploy"
    assert argv[argv.index("--target-revision") + 1] == "a" * 40
    assert argv[argv.index("--mode") + 1] == "single"
    assert argv[argv.index("--kubeconfig") + 1] == str(tmp_path / "kubeconfig")
    assert "--stack" not in argv
    assert "--registry" not in argv


def test_track_hands_argo_the_ref_name(tmp_path: Path) -> None:
    argv = _argv(_up_args(track=True), tmp_path)
    assert argv[argv.index("--target-revision") + 1] == "fix/some-cut"


def test_operator_env_files_sit_between_the_defaults_and_the_facts(tmp_path: Path) -> None:
    creds = tmp_path / "creds.env"
    argv = _argv(
        _up_args(env_file=[str(creds)], stack="2.2.0-rc.99", registry="r.example.com/dfe"), tmp_path
    )
    assert _env_files(argv) == [
        str(tmp_path / "defaults.env"),
        str(creds),
        str(tmp_path / "facts.env"),
    ]
    assert argv[argv.index("--stack") + 1] == "2.2.0-rc.99"
    assert argv[argv.index("--registry") + 1] == "r.example.com/dfe"


# ---------------------------------------------------------------------------
# down: which network it may remove
# ---------------------------------------------------------------------------


def test_node_volumes_are_found_by_the_containers_that_mount_them() -> None:
    containers = [
        {
            "Mounts": [
                {"Type": "volume", "Name": "b" * 64, "Destination": "/var"},
                {"Type": "bind", "Source": "/lib/modules", "Destination": "/lib/modules"},
            ]
        },
        {"Mounts": [{"Type": "volume", "Name": "a" * 64, "Destination": "/var"}]},
        {"Mounts": None},
    ]
    assert kind.node_volumes(containers) == ["a" * 64, "b" * 64]


def test_down_removes_a_network_up_created() -> None:
    assert kind.removes_network({"network_created": True}, "kind", "dfe-kind")


def test_down_leaves_a_network_that_existed_before_up() -> None:
    assert not kind.removes_network({"network_created": False}, "dfe-kind", "dfe-kind")


def test_down_without_state_removes_only_the_network_named_after_the_cluster() -> None:
    assert kind.removes_network({}, "dfe-kind", "dfe-kind")
    assert not kind.removes_network({}, "kind", "dfe-kind")


# ---------------------------------------------------------------------------
# status readings
# ---------------------------------------------------------------------------


def test_app_rows_default_a_missing_status_to_unknown() -> None:
    apps = {
        "items": [
            {
                "metadata": {"name": "b"},
                "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
            },
            {"metadata": {"name": "a"}},
        ]
    }
    assert kind.app_rows(apps) == [("a", "Unknown", "Unknown"), ("b", "Synced", "Healthy")]


def test_load_balancers_list_only_services_holding_an_address() -> None:
    services = {
        "items": [
            {
                "metadata": {"namespace": "gw", "name": "front"},
                "spec": {"type": "LoadBalancer", "ports": [{"port": 443}, {"port": 80}]},
                "status": {"loadBalancer": {"ingress": [{"ip": "198.51.100.254"}]}},
            },
            {
                "metadata": {"namespace": "gw", "name": "pending"},
                "spec": {"type": "LoadBalancer", "ports": [{"port": 8080}]},
                "status": {},
            },
            {
                "metadata": {"namespace": "x", "name": "internal"},
                "spec": {"type": "ClusterIP", "ports": [{"port": 80}]},
            },
        ]
    }
    assert kind.load_balancers(services) == [("gw/front", "198.51.100.254", 443)]


# ---------------------------------------------------------------------------
# The verbs dfe-ops registers
# ---------------------------------------------------------------------------


def test_up_needs_a_named_ref() -> None:
    with pytest.raises(SystemExit):
        dfeops.build_parser().parse_args(["kind", "up"])


def test_up_defaults() -> None:
    args = dfeops.build_parser().parse_args(["kind", "up", "--ref", "main"])
    assert args.func is kind.cmd_kind_up
    assert args.name == kind.DEFAULT_NAME
    assert args.mode == "single"
    assert args.node_image == kind.KIND_NODE_IMAGE
    assert args.api_port == 0
    assert args.ca_persist is False
    assert args.track is False


def test_status_and_down_are_registered() -> None:
    parser = dfeops.build_parser()
    assert parser.parse_args(["kind", "status"]).func is kind.cmd_kind_status
    down = parser.parse_args(["kind", "down", "--name", "other"])
    assert down.func is kind.cmd_kind_down
    assert down.name == "other"


def test_state_lives_under_the_gitignored_tmp_tree() -> None:
    assert kind.state_dir("dfe-kind").relative_to(REPO_ROOT).parts[:2] == (".tmp", "kind")
