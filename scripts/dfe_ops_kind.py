#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/dfe_ops_kind.py
#  Purpose:      `dfe-ops kind` -- up/status/down for a throwaway local kind
#                cluster that runs one DFE tier from a named dfe-infra git ref.
#                Registered on dfe-ops the way dfe_ops_bastion.py is.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-ops kind -- a DFE tier on a local kind cluster.

    dfe-ops kind up --ref <git ref> [--mode single] [--env-file FILE ...]
    dfe-ops kind status [--name dfe-kind]
    dfe-ops kind down [--name dfe-kind]

`up` deploys through the same path every other cluster takes. It checks the ref
out into its own directory and runs THAT checkout's `dfe-ops stack-deploy`, so
the bootstrap, the smoke tests and the charts Argo syncs all come from the one
commit. What it adds is only what a kind cluster lacks:

  * the cluster itself, on a docker network of its own (named after it by
    default), so `down` can remove the network without touching anyone else's;
  * kubelet serving certificates signed by the cluster CA, because kind's
    self-signed ones carry no IP SAN and metrics-server refuses them;
  * the StorageClass the cloud overlay's charts ask for, served by the
    provisioner kind already runs for its own default class;
  * the two front-door addresses (gateway, receiver), taken from the top of
    that network's subnet, for the MetalLB that bootstrap installs to announce.

On Linux both addresses are reachable from the host. Docker Desktop cannot route
to a container network, so there the services are reached with port-forwards,
which `dfe-ops acceptance` opens itself. `dfe-ops ui` drives the console through
its gateway hostname, so it needs that name to resolve from the host.

Credentials come from `--env-file`, exactly as for `stack-deploy`: the registry
pull secret (DFE_PULL_SECRET_*), the chart-repo read credential (DFE_REPO_TOKEN
or DFE_REPO_SSH_KEY) and the secrets-backend facts. Nothing is read from this
shell's environment: every DFE_* variable is stripped before the deploy runs, so
an estate value exported in the operator's shell cannot reach a kind cluster.

Four facts are forced whatever the env files say, because each is wrong for a
cluster on a docker bridge only this host can reach: no DNS provider, the
bundled in-cluster deploy repo rather than an external one, no internal-CA
persistence to the secrets store (`--ca-persist` opts back in), and the kind
cluster's own context, addresses and StorageClass.

The ref is resolved to a commit and Argo is pinned to that commit, so a push to
the branch during a proof cannot change what the proof ran. `--track` hands Argo
the ref name instead, for iterating with `dfe-ops refresh`.

Everything `up` writes lives under .tmp/kind/<name>/ (gitignored): the kind
config, the kubeconfig, the ref checkout, the two generated env files and
state.json. `down` deletes the cluster, its node volumes, the network `up`
created and that directory, then proves each is gone.
"""

import argparse
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import capacity
import envfile
import profiles

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_ROOT = REPO_ROOT / ".tmp" / "kind"

# --- pins --------------------------------------------------------------------
# A local test cluster, not a stack component, so the pin lives here rather than
# in versions.yaml. kind publishes node images per release and only guarantees
# the ones built for it: this is the v1.36.1 image kind v0.32.0 published,
# digest verified against Docker Hub on 2026-09-24 (pushed 2026-06-02).
KIND_RELEASE = "v0.32.0"
KIND_NODE_IMAGE = (
    "kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5"
)

DEFAULT_NAME = "dfe-kind"
DEFAULT_MODE = "single"
# The overlay for a cluster DFE owns outright: no node pinning, the gateway on a
# LoadBalancer, the self-signed internal CA at the edge.
DEFAULT_CLOUD_OVERLAY = "local-dfe"
# The class argocd/values/local-dfe.yaml's charts request their volumes from.
DEFAULT_STORAGE_CLASS = "local-path"
DEFAULT_NAMESPACE = "dfe-local"
DEFAULT_ENV = "local"
DEFAULT_REGION = "local"
# RFC 2606 reserves .test, so no hostname this deploy publishes resolves anywhere.
DEFAULT_BASE_DOMAIN = "dfe.test"
# A fresh node has pulled no image yet, so the gate gets twice stack-deploy's 900s.
DEFAULT_READINESS_TIMEOUT = 1800
DEFAULT_REMOTE = "origin"
ARGOCD_NAMESPACE = "argocd"
KIND_CLUSTER_LABEL = "io.x-k8s.kind.cluster"
MANAGED_BY = "dfe-ops-kind"
REQUIRED_TOOLS = ("kind", "docker", "kubectl", "helm", "git", "envsubst", "openssl")
# Required by bootstrap, recorded on the cluster secret, and named identically on
# every cluster, so they are read from the ref's own env template.
TEMPLATE_KEYS = ("DFE_CLICKHOUSE_HOST", "DFE_OTEL_ENDPOINT")
# A subnet this small leaves no room above the node addresses for the two doors.
MIN_SUBNET_ADDRESSES = 16
KUBELET_SERVING_PATCH = "kind: KubeletConfiguration\nserverTLSBootstrap: true\n"
KUBELET_SERVING_SIGNER = "kubernetes.io/kubelet-serving"
# Bound on waiting for every node to file its serving CSR once kind reports it Ready.
CSR_WAIT_SECONDS = 120


class KindError(RuntimeError):
    """A kind step that cannot go on, with the reason an operator can act on."""


# --- process helpers ---------------------------------------------------------
def _run(
    cmd: list[str], *, env: dict[str, str] | None = None, input_text: str | None = None
) -> subprocess.CompletedProcess:
    """Run a command, capturing text output; never raises on a non-zero exit."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        input=input_text,
        check=False,
    )


def _stream(cmd: list[str], *, env: dict[str, str] | None = None) -> int:
    """Run a long command with live output; returns its exit code."""
    print(f"==> {' '.join(cmd)}", file=sys.stderr)
    return subprocess.run(cmd, env=env, check=False).returncode


def _kubectl(
    kubeconfig: Path, *argv: str, input_text: str | None = None
) -> subprocess.CompletedProcess:
    return _run(["kubectl", "--kubeconfig", str(kubeconfig), *argv], input_text=input_text)


# --- pure helpers (unit-tested) ----------------------------------------------
def state_dir(name: str, root: Path | None = None) -> Path:
    """Where everything `up` writes for cluster *name* lives."""
    return (root or STATE_ROOT) / name


def render_cluster_config(name: str, node_image: str, api_port: int, workers: int) -> str:
    """The kind cluster config, as JSON (a YAML subset kind parses as-is).

    The API server binds loopback only; port 0 lets docker choose a free one, so
    the cluster claims no fixed host port. Each kubelet asks the cluster CA for
    its serving certificate: kind's self-signed one carries no IP SAN, so
    metrics-server cannot verify it and never turns Ready.
    """
    if workers < 0:
        raise KindError(f"--workers must be 0 or more, not {workers}")
    if not 0 <= api_port <= 65535:
        raise KindError(f"--api-port must be 0-65535, not {api_port}")
    nodes = [{"role": "control-plane", "image": node_image}]
    nodes += [{"role": "worker", "image": node_image} for _ in range(workers)]
    config = {
        "kind": "Cluster",
        "apiVersion": "kind.x-k8s.io/v1alpha4",
        "name": name,
        "networking": {"apiServerAddress": "127.0.0.1", "apiServerPort": api_port},
        "kubeadmConfigPatches": [KUBELET_SERVING_PATCH],
        "nodes": nodes,
    }
    return json.dumps(config, indent=2) + "\n"


def pending_serving_csrs(csrs: dict, nodes: set[str]) -> list[str]:
    """Kubelet-serving CSRs from this cluster's own nodes that nobody has decided on."""
    names = []
    for csr in csrs.get("items") or []:
        spec = csr.get("spec") or {}
        decided = (csr.get("status") or {}).get("conditions")
        requestor = spec.get("username", "").removeprefix("system:node:")
        if spec.get("signerName") == KUBELET_SERVING_SIGNER and requestor in nodes and not decided:
            names.append((csr.get("metadata") or {}).get("name", ""))
    return sorted(name for name in names if name)


def serving_nodes(csrs: dict) -> set[str]:
    """Nodes holding an approved kubelet-serving CSR."""
    served = set()
    for csr in csrs.get("items") or []:
        spec = csr.get("spec") or {}
        conditions = (csr.get("status") or {}).get("conditions") or []
        if spec.get("signerName") == KUBELET_SERVING_SIGNER and any(
            c.get("type") == "Approved" for c in conditions
        ):
            served.add(spec.get("username", "").removeprefix("system:node:"))
    return served


def network_subnet(inspect: list[dict]) -> ipaddress.IPv4Network:
    """The IPv4 subnet of a `docker network inspect` result."""
    for network in inspect:
        for config in (network.get("IPAM") or {}).get("Config") or []:
            try:
                subnet = ipaddress.ip_network(config.get("Subnet") or "", strict=False)
            except ValueError:
                continue
            if isinstance(subnet, ipaddress.IPv4Network):
                return subnet
    raise KindError("the kind network carries no IPv4 subnet, so MetalLB has nothing to announce")


def network_addresses(inspect: list[dict]) -> set[ipaddress.IPv4Address]:
    """Every IPv4 address a container on the network already holds."""
    taken: set[ipaddress.IPv4Address] = set()
    for network in inspect:
        for endpoint in (network.get("Containers") or {}).values():
            raw = (endpoint.get("IPv4Address") or "").split("/")[0]
            if raw:
                taken.add(ipaddress.IPv4Address(raw))
    return taken


def front_door_addresses(
    subnet: ipaddress.IPv4Network, taken: set[ipaddress.IPv4Address]
) -> tuple[str, str]:
    """(gateway, receiver): the two highest free host addresses in the subnet.

    Docker hands out node addresses from the bottom of the range, so the top is
    where a LoadBalancer address cannot collide with a node joining later.
    """
    if subnet.num_addresses < MIN_SUBNET_ADDRESSES:
        raise KindError(
            f"subnet {subnet} is too small to hold the nodes and two front-door addresses"
        )
    picked: list[str] = []
    candidate = subnet.broadcast_address - 1
    while len(picked) < 2 and candidate > subnet.network_address + 1:
        if candidate not in taken:
            picked.append(str(candidate))
        candidate -= 1
    if len(picked) < 2:
        raise KindError(f"subnet {subnet} has no two free addresses at its top")
    return picked[0], picked[1]


def check_in_subnet(address: str, subnet: ipaddress.IPv4Network, flag: str) -> None:
    """Refuse a front-door address the host cannot route to."""
    try:
        parsed = ipaddress.IPv4Address(address)
    except ValueError as exc:
        raise KindError(f"{flag} {address!r} is not an IPv4 address") from exc
    if parsed not in subnet:
        raise KindError(
            f"{flag} {address} is outside the kind network {subnet}, so nothing can reach it"
        )


def default_provisioner(storage_classes: dict) -> str | None:
    """The provisioner behind the cluster's default StorageClass, if it has one."""
    for sc in storage_classes.get("items") or []:
        annotations = (sc.get("metadata") or {}).get("annotations") or {}
        if annotations.get("storageclass.kubernetes.io/is-default-class") == "true":
            return sc.get("provisioner")
    return None


def render_storage_class(name: str, provisioner: str) -> str:
    """A non-default StorageClass *name*, served by *provisioner*, as JSON."""
    manifest = {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {"name": name, "labels": {"app.kubernetes.io/managed-by": MANAGED_BY}},
        "provisioner": provisioner,
        "reclaimPolicy": "Delete",
        "volumeBindingMode": "WaitForFirstConsumer",
    }
    return json.dumps(manifest, indent=2) + "\n"


def child_env(kubeconfig: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """This process's environment with every DFE_* and KUBECONFIG replaced.

    The deploy then reads DFE_* from the env files alone, and cannot aim at any
    cluster but the one `up` made.
    """
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if not k.startswith("DFE_") and k != "KUBECONFIG"}
    env["KUBECONFIG"] = str(kubeconfig)
    return env


def plan_env(
    args: argparse.Namespace,
    operator_env: dict[str, str],
    template_env: dict[str, str],
    gateway_ip: str,
    receiver_ip: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """(defaults, facts): the env files loaded first and last around the operator's.

    A flag the operator gave is a fact; one left unset is a default the
    operator's env files may override. The kind facts are forced regardless.
    """
    defaults: dict[str, str] = {"DFE_REGION": DEFAULT_REGION}
    facts: dict[str, str] = {}

    for key in TEMPLATE_KEYS:
        if template_env.get(key):
            defaults[key] = template_env[key]

    for key, flag, fallback in (
        ("DFE_ENV", args.env, DEFAULT_ENV),
        ("DFE_NAMESPACE", args.namespace, DEFAULT_NAMESPACE),
        ("DFE_REPO_URL", args.repo_url, args.default_repo_url),
    ):
        if flag is not None:
            facts[key] = flag
        elif fallback:
            defaults[key] = fallback

    # A declared domain in the operator's files wins; a base domain beside it
    # would make stack-deploy refuse the pair as contradictory.
    if args.base_domain is not None:
        facts["DFE_BASE_DOMAIN"] = args.base_domain
    elif not (operator_env.get("DFE_DOMAIN") or operator_env.get("DFE_BASE_DOMAIN")):
        defaults["DFE_BASE_DOMAIN"] = DEFAULT_BASE_DOMAIN

    facts.update(
        {
            "DFE_CLOUD": args.cloud_overlay,
            "DFE_STORAGE_CLASS": args.storage_class,
            "DFE_GATEWAY_IP": gateway_ip,
            "DFE_RECEIVER_IP": receiver_ip,
            "DFE_KUBE_CONTEXT": f"kind-{args.name}",
            # No cloud identity exists on a docker node.
            "DFE_WORKLOAD_IDENTITY_ANNOTATIONS": "{}",
            # The addresses live on a docker bridge only this host reaches.
            "DFE_DNS_PROVIDER": "none",
            # Empty selects the bundled in-cluster deploy repo, so a throwaway
            # engine never writes to a deploy repo a real deployment reads.
            "DFE_CONFIG_REPO_URL": "",
            # A throwaway root saved under the same store key would replace the
            # one a long-lived deployment restores on its next rebuild.
            "DFE_CA_PERSIST": "true" if args.ca_persist else "false",
        }
    )
    return defaults, facts


def render_env_file(values: dict[str, str]) -> str:
    """KEY="value" lines envfile.parse_env_file reads back unchanged."""
    lines = []
    for key, value in values.items():
        if '"' in value or "\n" in value:
            raise KindError(f"{key} carries a quote or newline, which an env file cannot hold")
        lines.append(f'{key}="{value}"')
    return "\n".join(lines) + "\n"


def write_private(path: Path, text: str) -> None:
    """Write *text* to *path*, readable by this user alone."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def stack_deploy_argv(
    args: argparse.Namespace,
    checkout: Path,
    kubeconfig: Path,
    commit: str,
    defaults_file: Path,
    facts_file: Path,
    access_out: Path,
) -> list[str]:
    """The ref's own `dfe-ops stack-deploy`, env files ordered defaults < operator < facts."""
    argv = [
        sys.executable,
        str(checkout / "scripts" / "dfe-ops"),
        "stack-deploy",
        "--mode",
        args.mode,
        "--kubeconfig",
        str(kubeconfig),
        "--target-revision",
        args.ref if args.track else commit,
        "--readiness-timeout",
        str(args.readiness_timeout),
        "--access-out",
        str(access_out),
        "--env-file",
        str(defaults_file),
    ]
    for path in args.env_file:
        argv += ["--env-file", str(Path(path).resolve())]
    argv += ["--env-file", str(facts_file)]
    if args.stack:
        argv += ["--stack", args.stack]
    if args.registry:
        argv += ["--registry", args.registry]
    return argv


def node_volumes(containers: list[dict]) -> list[str]:
    """Docker volumes a `docker inspect` of the node containers shows them mounting.

    kind's node volume is anonymous and carries no kind label, so it is found by
    the container that mounts it rather than by a label filter.
    """
    names = {
        mount["Name"]
        for container in containers
        for mount in container.get("Mounts") or []
        if mount.get("Type") == "volume" and mount.get("Name")
    }
    return sorted(names)


def removes_network(state: dict, network: str, name: str) -> bool:
    """Whether `down` owns the network: it made it, or it is the default one it would make."""
    if state:
        return bool(state.get("network_created"))
    return network == name


# --- cluster-facing steps ----------------------------------------------------
def _missing_tools() -> list[str]:
    return [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]


def _clusters() -> list[str]:
    done = _run(["kind", "get", "clusters"])
    if done.returncode != 0:
        raise KindError(f"kind get clusters failed: {done.stderr.strip()}")
    return [
        line.strip()
        for line in done.stdout.splitlines()
        if line.strip() and " " not in line.strip()
    ]


def _network_inspect(network: str) -> list[dict] | None:
    done = _run(["docker", "network", "inspect", network])
    if done.returncode != 0:
        return None
    return json.loads(done.stdout)


def _live_node_volumes(name: str) -> list[str]:
    """Volumes mounted by every container kind labels as part of cluster *name*."""
    ids = _run(["docker", "ps", "-aq", "--filter", f"label={KIND_CLUSTER_LABEL}={name}"])
    if not ids.stdout.split():
        return []
    done = _run(["docker", "inspect", *ids.stdout.split()])
    return node_volumes(json.loads(done.stdout)) if done.returncode == 0 else []


def _kind_version() -> str:
    done = _run(["kind", "version"])
    parts = done.stdout.split()
    return parts[1] if len(parts) > 1 else ""


def _capacity_gate(mode: str) -> None:
    """Refuse to start a lane this host cannot carry beside what it already runs."""
    floor = profiles.lane_floor(mode)
    meminfo = Path("/proc/meminfo")
    try:
        if meminfo.is_file():
            reading = capacity.meminfo_available(meminfo.read_text(encoding="utf-8"))
        else:
            done = _run(["free", "-b"])
            if done.returncode != 0:
                print(
                    "  capacity check skipped: this host has no /proc/meminfo or free",
                    file=sys.stderr,
                )
                return
            reading = capacity.free_output_available(done.stdout)
    except (OSError, ValueError) as exc:
        print(f"  capacity check skipped: {exc}", file=sys.stderr)
        return
    ok, line = capacity.verdict(reading, floor)
    print(f"  {line}", file=sys.stderr)
    if not ok:
        raise KindError(
            f"this host cannot carry the {mode} tier now; free memory or pass --skip-capacity-check"
        )


def _resolve_commit(ref: str, remote: str, fetch: bool) -> str:
    """The commit *ref* names, fetched from *remote* first unless told not to."""
    if fetch:
        done = _run(["git", "-C", str(REPO_ROOT), "fetch", "--quiet", remote, ref])
        if done.returncode != 0:
            raise KindError(f"git fetch {remote} {ref} failed: {done.stderr.strip()}")
        target = "FETCH_HEAD^{commit}"
    else:
        target = f"{ref}^{{commit}}"
    done = _run(["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "--quiet", target])
    if done.returncode != 0 or not done.stdout.strip():
        raise KindError(f"{ref!r} does not name a commit here")
    return done.stdout.strip()


def _checkout(commit: str, dest: Path) -> None:
    """A private checkout of *commit*, sharing this clone's objects."""
    common = _run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"]
    )
    if common.returncode != 0:
        raise KindError(f"cannot find this checkout's git directory: {common.stderr.strip()}")
    if dest.exists():
        shutil.rmtree(dest)
    for cmd in (
        ["git", "clone", "--quiet", "--shared", "--no-checkout", common.stdout.strip(), str(dest)],
        ["git", "-C", str(dest), "checkout", "--quiet", "--detach", commit],
    ):
        done = _run(cmd)
        if done.returncode != 0:
            raise KindError(f"{' '.join(cmd[:4])} failed: {done.stderr.strip()}")


def _default_repo_url(remote: str) -> str:
    done = _run(["git", "-C", str(REPO_ROOT), "remote", "get-url", remote])
    return done.stdout.strip() if done.returncode == 0 else ""


def _ensure_storage_class(kubeconfig: Path, name: str) -> None:
    """Add StorageClass *name* on the provisioner the cluster's default class uses."""
    done = _kubectl(kubeconfig, "get", "storageclass", "-o", "json")
    if done.returncode != 0:
        raise KindError(f"cannot list StorageClasses: {done.stderr.strip()}")
    classes = json.loads(done.stdout)
    names = {(sc.get("metadata") or {}).get("name") for sc in classes.get("items") or []}
    if name in names:
        print(f"  StorageClass {name} already present", file=sys.stderr)
        return
    provisioner = default_provisioner(classes)
    if not provisioner:
        raise KindError("the kind cluster has no default StorageClass to take a provisioner from")
    done = _kubectl(
        kubeconfig, "apply", "-f", "-", input_text=render_storage_class(name, provisioner)
    )
    if done.returncode != 0:
        raise KindError(f"cannot create StorageClass {name}: {done.stderr.strip()}")
    print(f"  StorageClass {name} -> {provisioner}", file=sys.stderr)


def _approve_kubelet_serving(kubeconfig: Path) -> None:
    """Approve each node's kubelet-serving CSR, and wait until every node holds one.

    Nothing in a cluster approves these by itself, and until one is approved the
    kubelet serves no TLS, so `kubectl logs` and metrics-server both fail.
    """
    done = _kubectl(kubeconfig, "get", "nodes", "-o", "jsonpath={.items[*].metadata.name}")
    if done.returncode != 0:
        raise KindError(f"cannot list nodes: {done.stderr.strip()}")
    nodes = set(done.stdout.split())
    deadline = time.monotonic() + CSR_WAIT_SECONDS
    while True:
        done = _kubectl(kubeconfig, "get", "csr", "-o", "json")
        if done.returncode != 0:
            raise KindError(f"cannot list CSRs: {done.stderr.strip()}")
        csrs = json.loads(done.stdout)
        missing = nodes - serving_nodes(csrs)
        if not missing:
            print(
                f"  kubelet serving certificates signed for {len(nodes)} node(s)", file=sys.stderr
            )
            return
        for name in pending_serving_csrs(csrs, nodes):
            approved = _kubectl(kubeconfig, "certificate", "approve", name)
            if approved.returncode != 0:
                raise KindError(f"cannot approve CSR {name}: {approved.stderr.strip()}")
        if time.monotonic() >= deadline:
            raise KindError(
                f"no kubelet-serving CSR from {', '.join(sorted(missing))} within "
                f"{CSR_WAIT_SECONDS}s"
            )
        time.sleep(3)


def _read_state(name: str) -> dict:
    path = state_dir(name) / "state.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(name: str, state: dict) -> None:
    write_private(state_dir(name) / "state.json", json.dumps(state, indent=2) + "\n")


def cmd_kind_up(args: argparse.Namespace) -> int:
    missing = _missing_tools()
    if missing:
        print(f"kind up: not on PATH: {', '.join(missing)}", file=sys.stderr)
        return 2
    if args.node_image == KIND_NODE_IMAGE and (found := _kind_version()) != KIND_RELEASE:
        print(
            f"  WARNING: the default node image was built for kind {KIND_RELEASE}; this host runs "
            f"{found or 'an unknown kind'}. Pass --node-image with an image from that release if "
            "creation fails.",
            file=sys.stderr,
        )

    root = state_dir(args.name)
    kubeconfig = root / "kubeconfig"
    network = args.network or args.name
    try:
        existing = args.name in _clusters()
        if existing and not _read_state(args.name):
            raise KindError(
                f"kind cluster {args.name} exists but `dfe-ops kind up` has no record of making it "
                f"({root / 'state.json'}); pass another --name, or delete that cluster first"
            )
        if existing:
            print(f"==> kind cluster {args.name} exists; deploying onto it", file=sys.stderr)
        elif not args.skip_capacity_check:
            _capacity_gate(args.mode)

        print(f"==> resolving {args.ref}", file=sys.stderr)
        commit = _resolve_commit(args.ref, args.remote, not args.no_fetch)
        checkout = root / "src"
        _checkout(commit, checkout)
        print(f"  {args.ref} -> {commit} (checked out at {checkout})", file=sys.stderr)

        state = _read_state(args.name) if existing else {}
        if not existing:
            network_existed = _network_inspect(network) is not None
            state = {
                "name": args.name,
                "network": network,
                "network_created": not network_existed,
                "kubeconfig": str(kubeconfig),
                "node_image": args.node_image,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            # Written before the create, so a create that fails half way is still
            # something `down` can find and remove.
            _write_state(args.name, state)
            config = root / "kind-config.json"
            write_private(
                config,
                render_cluster_config(args.name, args.node_image, args.api_port, args.workers),
            )
            env = dict(os.environ)
            env["KIND_EXPERIMENTAL_DOCKER_NETWORK"] = network
            rc = _stream(
                [
                    "kind",
                    "create",
                    "cluster",
                    "--name",
                    args.name,
                    "--config",
                    str(config),
                    "--kubeconfig",
                    str(kubeconfig),
                    "--wait",
                    "300s",
                ],
                env=env,
            )
            if rc != 0:
                print(
                    f"kind up: kind create failed (rc {rc}); `dfe-ops kind down --name {args.name}` "
                    "removes what it left",
                    file=sys.stderr,
                )
                return rc
            os.chmod(kubeconfig, 0o600)
            state["volumes"] = _live_node_volumes(args.name)
            _write_state(args.name, state)
        elif not kubeconfig.is_file():
            done = _run(["kind", "get", "kubeconfig", "--name", args.name])
            if done.returncode != 0:
                raise KindError(f"cannot read the kubeconfig of {args.name}: {done.stderr.strip()}")
            write_private(kubeconfig, done.stdout)

        print("==> kind glue", file=sys.stderr)
        _approve_kubelet_serving(kubeconfig)
        _ensure_storage_class(kubeconfig, args.storage_class)
        inspect = _network_inspect(state["network"])
        if inspect is None:
            raise KindError(f"docker network {state['network']} is gone")
        subnet = network_subnet(inspect)
        derived = front_door_addresses(subnet, network_addresses(inspect))
        gateway_ip = args.gateway_ip or derived[0]
        receiver_ip = args.receiver_ip or derived[1]
        check_in_subnet(gateway_ip, subnet, "--gateway-ip")
        check_in_subnet(receiver_ip, subnet, "--receiver-ip")
        print(
            f"  front door on {subnet}: gateway {gateway_ip}, receiver {receiver_ip}",
            file=sys.stderr,
        )

        operator_env = envfile.load_env_files(args.env_file)
        template = checkout / "bootstrap" / "local.env.example"
        template_env = envfile.parse_env_file(template) if template.is_file() else {}
        args.default_repo_url = _default_repo_url(args.remote)
        defaults, facts = plan_env(args, operator_env, template_env, gateway_ip, receiver_ip)
        defaults_file = root / "kind-defaults.env"
        facts_file = root / "kind-facts.env"
        write_private(defaults_file, render_env_file(defaults))
        write_private(facts_file, render_env_file(facts))

        state.update(
            {
                "ref": args.ref,
                "commit": commit,
                "target_revision": args.ref if args.track else commit,
                "mode": args.mode,
                "gateway_ip": gateway_ip,
                "receiver_ip": receiver_ip,
                "env_files": [
                    str(defaults_file),
                    *[str(Path(p).resolve()) for p in args.env_file],
                    str(facts_file),
                ],
            }
        )
        _write_state(args.name, state)
    except (KindError, OSError, json.JSONDecodeError) as exc:
        print(f"kind up: {exc}", file=sys.stderr)
        return 1

    argv = stack_deploy_argv(
        args, checkout, kubeconfig, commit, defaults_file, facts_file, root / "dfe-access.md"
    )
    rc = _stream(argv, env=child_env(kubeconfig))
    env_flags = " ".join(f"--env-file {p}" for p in state["env_files"])
    print("", file=sys.stderr)
    print(f"=== kind up {'OK' if rc == 0 else f'FAILED (rc {rc})'} ===", file=sys.stderr)
    print(f"  cluster:    {args.name} (network {state['network']})", file=sys.stderr)
    print(f"  deployed:   {args.mode} from {args.ref} at {commit}", file=sys.stderr)
    print(f"  kubeconfig: {kubeconfig}", file=sys.stderr)
    print(f"  gateway:    {gateway_ip}   receiver: {receiver_ip}", file=sys.stderr)
    print(f"  env files:  {env_flags}", file=sys.stderr)
    print(f"  status:     dfe-ops kind status --name {args.name}", file=sys.stderr)
    print(f"  remove:     dfe-ops kind down --name {args.name}", file=sys.stderr)
    return rc


# --- status ------------------------------------------------------------------
def app_rows(applications: dict) -> list[tuple[str, str, str]]:
    """(name, sync, health) per Argo Application."""
    rows = []
    for app in applications.get("items") or []:
        status = app.get("status") or {}
        rows.append(
            (
                (app.get("metadata") or {}).get("name", "?"),
                (status.get("sync") or {}).get("status", "Unknown"),
                (status.get("health") or {}).get("status", "Unknown"),
            )
        )
    return sorted(rows)


def load_balancers(services: dict) -> list[tuple[str, str, int]]:
    """(namespace/name, address, first port) for every LoadBalancer holding an address."""
    found = []
    for svc in services.get("items") or []:
        spec = svc.get("spec") or {}
        if spec.get("type") != "LoadBalancer":
            continue
        meta = svc.get("metadata") or {}
        ports = [p.get("port") for p in spec.get("ports") or [] if p.get("port")]
        for ingress in (svc.get("status") or {}).get("loadBalancer", {}).get("ingress") or []:
            if ingress.get("ip") and ports:
                found.append(
                    (f"{meta.get('namespace')}/{meta.get('name')}", ingress["ip"], int(ports[0]))
                )
    return sorted(found)


def _reachable(address: str, port: int) -> bool:
    try:
        with socket.create_connection((address, port), timeout=2):
            return True
    except OSError:
        return False


def cmd_kind_status(args: argparse.Namespace) -> int:
    state = _read_state(args.name)
    try:
        present = args.name in _clusters()
    except KindError as exc:
        print(f"kind status: {exc}", file=sys.stderr)
        return 2
    if not present:
        print(f"kind cluster {args.name}: absent", file=sys.stderr)
        return 2
    kubeconfig = Path(state.get("kubeconfig") or state_dir(args.name) / "kubeconfig")
    print(f"kind cluster {args.name}: present")
    if state:
        print(
            f"  deployed: {state.get('mode', '?')} from {state.get('ref', '?')} "
            f"at {state.get('commit', '?')}"
        )

    nodes = _run(
        [
            "docker",
            "ps",
            "--filter",
            f"label={KIND_CLUSTER_LABEL}={args.name}",
            "--format",
            "{{.Names}}",
        ]
    ).stdout.split()
    if nodes:
        stats = _run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}",
                *nodes,
            ]
        )
        for line in stats.stdout.splitlines():
            print(f"  node {line}")

    healthy = True
    done = _kubectl(kubeconfig, "get", "nodes", "-o", "json")
    if done.returncode != 0:
        print(f"  nodes: unreadable ({done.stderr.strip()})")
        return 1
    items = json.loads(done.stdout).get("items") or []
    ready = [
        n
        for n in items
        if any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in (n.get("status") or {}).get("conditions") or []
        )
    ]
    print(f"  nodes: {len(ready)}/{len(items)} Ready")
    healthy &= bool(items) and len(ready) == len(items)

    done = _kubectl(
        kubeconfig, "-n", ARGOCD_NAMESPACE, "get", "applications.argoproj.io", "-o", "json"
    )
    if done.returncode != 0:
        print("  argo: no Applications (not deployed yet?)")
        healthy = False
    else:
        rows = app_rows(json.loads(done.stdout))
        bad = [r for r in rows if r[1] != "Synced" or r[2] != "Healthy"]
        print(f"  argo: {len(rows) - len(bad)}/{len(rows)} Applications Synced and Healthy")
        for name, sync, health in bad:
            print(f"    {name:<44} {sync:<10} {health}")
        healthy &= bool(rows) and not bad

    done = _kubectl(kubeconfig, "get", "services", "-A", "-o", "json")
    if done.returncode == 0:
        for svc, address, port in load_balancers(json.loads(done.stdout)):
            verdict = "reachable" if _reachable(address, port) else "NOT reachable from this host"
            print(f"  door {svc}: {address}:{port} {verdict}")
    return 0 if healthy else 1


# --- down --------------------------------------------------------------------
def cmd_kind_down(args: argparse.Namespace) -> int:
    state = _read_state(args.name)
    network = args.network or state.get("network") or args.name
    root = state_dir(args.name)
    problems = 0

    try:
        present = args.name in _clusters()
    except KindError as exc:
        print(f"kind down: {exc}", file=sys.stderr)
        return 1
    volumes = sorted(set(state.get("volumes") or []) | set(_live_node_volumes(args.name)))
    if present:
        rc = _stream(["kind", "delete", "cluster", "--name", args.name])
        if rc != 0:
            print(f"kind down: kind delete failed (rc {rc})", file=sys.stderr)
            problems += 1
    else:
        print(f"  kind cluster {args.name}: already absent", file=sys.stderr)
    for volume in volumes:
        if _run(["docker", "volume", "inspect", volume]).returncode == 0:
            _run(["docker", "volume", "rm", volume])

    owned = removes_network(state, network, args.name)
    if owned and _network_inspect(network) is not None:
        done = _run(["docker", "network", "rm", network])
        if done.returncode != 0:
            print(f"kind down: docker network rm {network}: {done.stderr.strip()}", file=sys.stderr)
            problems += 1
    elif not owned:
        print(f"  docker network {network}: not created by kind up, left in place", file=sys.stderr)

    if root.exists():
        shutil.rmtree(root)

    print("=== kind down: what remains ===", file=sys.stderr)
    checks = [
        (f"kind cluster {args.name}", args.name not in _clusters()),
        (
            f"containers labelled {KIND_CLUSTER_LABEL}={args.name}",
            not _run(
                ["docker", "ps", "-aq", "--filter", f"label={KIND_CLUSTER_LABEL}={args.name}"]
            ).stdout.strip(),
        ),
        (f"state directory {root}", not root.exists()),
    ]
    checks += [
        (f"node volume {volume}", _run(["docker", "volume", "inspect", volume]).returncode != 0)
        for volume in volumes
    ]
    if owned:
        checks.append((f"docker network {network}", _network_inspect(network) is None))
    for what, gone in checks:
        print(
            f"  [{'PASS' if gone else 'FAIL'}] {what} {'gone' if gone else 'STILL PRESENT'}",
            file=sys.stderr,
        )
        problems += 0 if gone else 1
    return 1 if problems else 0


# --- parser ------------------------------------------------------------------
def add_kind_subparser(sub: argparse._SubParsersAction) -> None:
    """Register `dfe-ops kind` and its actions."""
    kind = sub.add_parser(
        "kind",
        help="up/status/down for a local kind cluster running one DFE tier from a named git ref",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    actions = kind.add_subparsers(dest="kind_action", required=True, metavar="<action>")

    def with_name(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
        parser.add_argument("--name", default=DEFAULT_NAME, help="kind cluster name")
        return parser

    up = with_name(
        actions.add_parser(
            "up",
            help="create the cluster, add the kind glue, then run the ref's own stack-deploy",
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )
    )
    up.add_argument(
        "--ref",
        required=True,
        help="dfe-infra branch, tag or commit to deploy; bootstrap, smoke tests and charts all come from it",
    )
    up.add_argument(
        "--remote",
        default=DEFAULT_REMOTE,
        help="git remote the ref is fetched from, and the default chart repo URL",
    )
    up.add_argument(
        "--no-fetch", action="store_true", help="resolve --ref locally without fetching"
    )
    up.add_argument(
        "--track",
        action="store_true",
        help="point Argo at the ref name instead of the commit it resolved to",
    )
    up.add_argument(
        "--mode", default=DEFAULT_MODE, choices=list(profiles.MODES), help="deploy profile"
    )
    up.add_argument(
        "--stack", default="", help="versions.yaml stack to deploy (default: the ref's `current`)"
    )
    up.add_argument(
        "--env-file",
        action="append",
        default=[],
        metavar="PATH",
        help="DFE_* env file: registry pull, chart-repo and secrets-backend credentials "
        "(repeatable; later wins)",
    )
    up.add_argument(
        "--registry", default=None, help="image registry prefix, passed to stack-deploy"
    )
    up.add_argument(
        "--repo-url",
        default=None,
        help="chart repo Argo reads (default: the --remote URL of this checkout)",
    )
    up.add_argument(
        "--base-domain",
        default=None,
        help=f"base domain the tier publishes under as <mode>.<base> (default {DEFAULT_BASE_DOMAIN} "
        "unless an env file declares a domain)",
    )
    up.add_argument(
        "--namespace", default=None, help=f"app namespace (default {DEFAULT_NAMESPACE})"
    )
    up.add_argument(
        "--env", default=None, help=f"deployment posture, DFE_ENV (default {DEFAULT_ENV})"
    )
    up.add_argument(
        "--cloud-overlay",
        default=DEFAULT_CLOUD_OVERLAY,
        help="argocd/values/<name>.yaml overlay the charts take, DFE_CLOUD",
    )
    up.add_argument(
        "--storage-class",
        default=DEFAULT_STORAGE_CLASS,
        help="StorageClass the overlay requests; created on the kind provisioner if absent",
    )
    up.add_argument(
        "--network", default=None, help="docker network the nodes join (default: the cluster name)"
    )
    up.add_argument(
        "--gateway-ip",
        default=None,
        help="gateway LoadBalancer address (default: the top free address of the network)",
    )
    up.add_argument(
        "--receiver-ip",
        default=None,
        help="receiver LoadBalancer address (default: the next free address below the gateway's)",
    )
    up.add_argument(
        "--api-port",
        type=int,
        default=0,
        help="host port for the API server on 127.0.0.1; 0 lets docker choose a free one",
    )
    up.add_argument("--workers", type=int, default=0, help="worker nodes beside the control plane")
    up.add_argument(
        "--node-image", default=KIND_NODE_IMAGE, help="kind node image, pinned by digest"
    )
    up.add_argument(
        "--readiness-timeout",
        type=int,
        default=DEFAULT_READINESS_TIMEOUT,
        help="bounded readiness wait in seconds, passed to stack-deploy",
    )
    up.add_argument(
        "--ca-persist",
        action="store_true",
        help="save the internal CA root to the secrets store (off: a throwaway root must not "
        "replace a real deployment's)",
    )
    up.add_argument(
        "--skip-capacity-check",
        action="store_true",
        help="create the cluster without asking whether this host can carry the tier",
    )
    up.set_defaults(func=cmd_kind_up)

    status = with_name(
        actions.add_parser(
            "status",
            help="nodes, node memory, Argo Application health and front-door reachability",
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )
    )
    status.set_defaults(func=cmd_kind_status)

    down = with_name(
        actions.add_parser(
            "down",
            help="delete the cluster, the network up created and the state directory, then prove each is gone",
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )
    )
    down.add_argument(
        "--network",
        default=None,
        help="docker network to remove (default: the one up recorded, else the cluster name)",
    )
    down.set_defaults(func=cmd_kind_down)
