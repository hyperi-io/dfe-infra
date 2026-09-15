#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/dfe_ops_bastion.py
#  Purpose:      `dfe-ops bastion` -- up/join/peers/hub/shell/forward/down/
#                status for the on-demand SSM-managed toolbox instance
#                (terraform/modules/toolbox/aws). Split into its own module
#                the way tester_idp.py and resolve_pins.py already are, and
#                imported into dfe-ops's build_parser() the same way.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-ops bastion -- the on-demand toolbox's operator surface.

    dfe-ops bastion up [--ttl MIN]     flip toolbox.enabled on in the dial,
                                       apply just the toolbox target, wait for
                                       SSM to report PingStatus Online.
    dfe-ops bastion join [--ttl MIN]  mint an admin peer on the fleet tunnel --
                                       REFUSED until hyperi-io/culvert#40 lands.
    dfe-ops bastion peers             the hub's peers: name, tunnel address and
                                       last handshake.
    dfe-ops bastion hub <peer>        route the tunnel's client range at the
                                       culvert pod and reach one appliance.
    dfe-ops bastion shell             open the logged shell Session.
    dfe-ops bastion forward <t> <p>   port-forward to a named target on local
                                       port <p> -- NOT recorded by Session
                                       Manager (toolbox/aws/CONTRACT.md #6).
    dfe-ops bastion down              revoke the admin peer, flip
                                       toolbox.enabled off, destroy the toolbox
                                       target, then PROVE nothing remains.
    dfe-ops bastion status            report the toolbox's current state.

`up`/`down` edit deployment.yaml (the dial) in place, then shell out to
render_dial.py --tofu and tofu -- the same steps an operator would run by hand,
just for the toolbox target alone. `up` applies; `down` DESTROYS that target,
because an apply has to reconcile every other resource in the target set first
and a failure there would leave the instance running and billing.
`shell`/`forward` shell out to
the real `aws ssm start-session`, inheriting this process's stdio, because a
Session Manager session needs a real terminal.

`join`, `peers`, `hub` and `down`'s revocation drive culvert's own CLI inside
its pod, so they share one kubectl boundary the way `_run` is the tofu one. The
admin peer's PRIVATE key is minted on the instance and never leaves it: culvert
takes the public half (`generate-client --pubkey`) and writes a placeholder the
instance substitutes locally, so no private key ever rides an SSM parameter.

`hub` NEEDS NO JOIN. culvert's admin exception matches a source arriving off the
pod's ethernet side and its client-to-client verdict drops a peer source before
that exception is reached, so the reach-back that works today is a ROUTE: the
toolbox sends the tunnel's client range at the culvert pod, which the VPC CNI
gives a VPC address, and culvert forwards it to the appliance. The route is
re-programmed on every `hub` call, because a roll gives the pod a new address.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import aws_cli

REPO_ROOT = Path(__file__).resolve().parent.parent
DIAL = REPO_ROOT / "deployment.yaml"
DIAL_TEMPLATE = REPO_ROOT / "deployment.example.yaml"
RENDER_DIAL = REPO_ROOT / "scripts" / "render_dial.py"
AWS_ROOT = REPO_ROOT / "terraform" / "environments" / "aws"
SCRATCH_KUBECONFIG = REPO_ROOT / ".tmp" / "toolbox-eks-api.kubeconfig"

# What `up`/`down` apply -- the toolbox module and nothing else at the aws root.
# A targeted apply still refreshes whatever the target DEPENDS on and evaluates
# their outputs; what it skips is every other resource in the root.
# The two EKS access-entry resources (kubernetes-cluster/aws/eks.tf, "Toolbox
# operator") are deliberately NOT targeted: either one failing aborts the apply
# with the instance already created and billing, and before the toolbox's own
# forward documents and target egress rules exist. They belong to the full
# apply that builds the cluster, which is also the apply that knows whether the
# principal already holds the creator entry.
# A root OUTPUT whose dependencies fall entirely outside the target set is not
# written at all, so a deployment whose first-ever apply was `bastion up`
# carries no value for one until a full apply runs.
TOOLBOX_TARGETS = ("-target=module.toolbox",)

DEFAULT_WAIT_TIMEOUT = 300.0
DEFAULT_POLL_INTERVAL = 5.0

# --- the fleet tunnel ---------------------------------------------------------
# The hub is culvert (helm/edge/culvert), and everything below reaches it
# through its own CLI in its own pod. Nothing here talks to the tunnel directly.

# The Argo cluster secret bootstrap.sh writes, and the annotation on it carrying
# the tunnel's Elastic IP (terraform/modules/edge/aws).
CLUSTER_SECRET_NAMESPACE = "argocd"
CLUSTER_SECRET = "secret/dfe-cluster"
TUNNEL_ADDRESS_JSONPATH = r"jsonpath={.metadata.annotations.dfe\.hyperi\.io/tunnel_address}"

# dfe-common.selectorLabels on the culvert chart -- {project}-{component}.
CULVERT_SELECTOR = "app.kubernetes.io/name=dfe-culvert"
# culvert's own paths: the PKI directory its scripts default to, the peer
# allocation map its WireGuard half writes there, and where it drops a config.
CULVERT_ALLOCATIONS = "/etc/vpn/pki/wireguard/allocations.json"
CULVERT_CLIENTS = "/etc/vpn/clients"
# The interface culvert brings up, and the separate one the toolbox brings up.
HUB_INTERFACE = "wg0"
ADMIN_INTERFACE = "dfe-admin"
ADMIN_PEER_NAME = "bastion-admin"
ADMIN_KEY_PATH = f"/etc/wireguard/{ADMIN_INTERFACE}.key"
ADMIN_CONF_PATH = f"/etc/wireguard/{ADMIN_INTERFACE}.conf"
# culvert writes this wherever the client generated its own keypair, so the
# private half never leaves the machine that minted it (lib/wireguard.py).
PRIVATE_KEY_PLACEHOLDER = "YOUR_PRIVATE_KEY_HERE"
# The admin peer this machine minted, its deadline and the namespace that
# issued it, so `down` revokes THAT peer rather than guessing at a name.
ADMIN_STATE = REPO_ROOT / ".tmp" / "bastion-admin-peer.json"
DEFAULT_ADMIN_TTL_MINUTES = 60
# Mirrors peers.classes.admin.reach in helm/edge/culvert/values.yaml -- the
# ports an appliance accepts from the admin peer on its tunnel interface.
ADMIN_REACH = (22, 443)
# Whether the hub can carry an admin that dials in AS A PEER. culvert installs
# the client-to-client DROP over every pair of tunnel interfaces before the
# admin ACCEPT, so a peer source is dropped before any rule naming it is
# reached; hyperi-io/culvert#40 is the peer class that would carry it, and
# `join` refuses until it lands. `hub` routes instead and needs none of it.
ADMIN_PEER_SUPPORTED = False
# The WireGuard /24 culvert carves out of vpn.clientCIDR, read off the pod's own
# environment rather than recomputed here (helm/edge/culvert/_helpers.tpl).
WG_NETWORK_ENV = "CULVERT_WG_NETWORK"


class BastionError(RuntimeError):
    """A bastion verb cannot proceed -- the message is what dfe-ops prints."""


# --- the dial editor ----------------------------------------------------------


_KEY_LINE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?P<rest>\s*(#.*)?$|\s+\S.*$)")


def _set_toolbox_field(text: str, field: str, value: str) -> str:
    """Set a scalar field inside the dial's top-level `toolbox:` block.

    A focused editor, not a general YAML writer: it walks from the `toolbox:`
    line (column 0) to the next column-0, non-blank line, tracking the key path
    by indentation, and replaces the line whose path relative to `toolbox:`
    equals `field`. `field` is a dotted path -- `enabled` is the top-level
    toggle, `pod.enabled` is the in-cluster pod's own, and the dial holds both.
    Matching on the bare key instead hit whichever came first in the file, which
    is `pod.enabled`, so `bastion up` wrote a quoted "true" into a field the
    chart's validate.yaml refuses and left the real toggle alone.

    Every OTHER line -- comments, sibling fields, the `aws:`/`session:`
    sub-blocks -- survives verbatim, the same guarantee render_dial.py's own
    `_merge_env` makes for the env file. Raises BastionError by name when the
    block or field is missing, so `up`/`down` fail loudly on a dial copied
    before this field existed rather than silently doing nothing.
    """
    wanted = field.split(".")
    lines = text.splitlines()
    out: list[str] = []
    in_block = False
    found = False
    # (indent, key) for each open map level inside the toolbox: block.
    path: list[tuple[int, str]] = []
    for line in lines:
        if not in_block:
            if re.match(r"^toolbox:\s*(#.*)?$", line):
                in_block = True
                path = []
            out.append(line)
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if stripped and indent == 0:
            in_block = False
            out.append(line)
            continue
        match = _KEY_LINE.match(line) if stripped and not stripped.startswith("#") else None
        if match is None:
            out.append(line)
            continue
        while path and path[-1][0] >= indent:
            path.pop()
        path.append((indent, match.group("key")))
        if not found and [key for _, key in path] == wanted:
            out.append(f'{match.group("indent")}{match.group("key")}: "{value}"')
            found = True
            continue
        out.append(line)

    if not found:
        raise BastionError(
            f"no toolbox.{field} field in {DIAL.name} -- copy the toolbox: block from "
            f"{DIAL_TEMPLATE.name} before running dfe-ops bastion"
        )
    rendered = "\n".join(out)
    return rendered + "\n" if text.endswith("\n") else rendered


def _read_dial() -> str:
    if not DIAL.is_file():
        raise BastionError(
            f"no deployment dial at {DIAL} -- copy {DIAL_TEMPLATE.name} to "
            f"{DIAL.name} and populate it, including a toolbox: block"
        )
    return DIAL.read_text(encoding="utf-8")


def _set_toolbox_enabled(enabled: bool, *, ttl_minutes: int | None = None) -> None:
    text = _set_toolbox_field(_read_dial(), "enabled", "true" if enabled else "false")
    if ttl_minutes is not None:
        text = _set_toolbox_field(text, "ttl_minutes", str(ttl_minutes))
    DIAL.write_text(text, encoding="utf-8")
    print(f"dfe-ops bastion: set toolbox.enabled = {str(enabled).lower()} in {DIAL.name}", file=sys.stderr)


# --- the tofu boundary --------------------------------------------------------
# One function every tofu/render_dial.py call in this module shares, so a test
# mocks exactly one boundary for both -- the same shape aws_cli.run_aws is for
# the AWS CLI half.


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kwargs)  # type: ignore[call-overload]


def _run_interactive(cmd: list[str]) -> int:
    """Run a command that needs a real terminal (a live Session Manager
    session), inheriting this process's stdio rather than capturing it."""
    return _run(cmd).returncode


def _render_dial_tofu() -> bool:
    result = _run([sys.executable, str(RENDER_DIAL), "--tofu"], cwd=REPO_ROOT)
    return result.returncode == 0


def _apply_toolbox() -> bool:
    result = _run(["tofu", f"-chdir={AWS_ROOT}", "apply", "-auto-approve", *TOOLBOX_TARGETS])
    return result.returncode == 0


def _destroy_toolbox() -> bool:
    """Take the toolbox down with `tofu destroy -target=module.toolbox`.

    An apply would have to CREATE or UPDATE every other resource in the target
    set before it got to the instance, so a resource broken anywhere in that
    set leaves the instance running and billing. A targeted destroy creates
    nothing, so the only thing that can stop it is the toolbox itself.
    """
    result = _run(["tofu", f"-chdir={AWS_ROOT}", "destroy", "-auto-approve", *TOOLBOX_TARGETS])
    return result.returncode == 0


def _tofu_outputs() -> dict[str, object]:
    """`tofu output -json` from the aws root, or {} on any failure -- a bare
    `dfe-ops bastion status` on a deployment that has never been applied
    should report "not enabled", not crash."""
    result = _run(
        ["tofu", f"-chdir={AWS_ROOT}", "output", "-json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _output(outputs: dict[str, object], name: str, default: object = "") -> object:
    entry = outputs.get(name)
    if not isinstance(entry, dict):
        return default
    return entry.get("value", default)


def _instance_id() -> str:
    instance_id = str(_output(_tofu_outputs(), "toolbox_instance_id"))
    if not instance_id:
        raise BastionError("no toolbox instance -- run `dfe-ops bastion up` first")
    return instance_id


# --- the kubectl boundary -----------------------------------------------------
# One function every culvert call goes through, so a test mocks exactly this for
# the hub verbs the way it mocks _run for tofu.


def _kubectl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _namespace(args: argparse.Namespace) -> str:
    return getattr(args, "namespace", None) or os.environ.get("DFE_NAMESPACE") or "dfe"


def _culvert_pod(namespace: str) -> str:
    """The running culvert pod, by the chart's own selector labels."""
    result = _kubectl([
        "-n", namespace, "get", "pods",
        "-l", CULVERT_SELECTOR,
        "--field-selector=status.phase=Running",
        "-o", "jsonpath={.items[0].metadata.name}",
    ])
    pod = result.stdout.strip()
    if result.returncode != 0 or not pod:
        raise BastionError(
            f"no running culvert pod in namespace {namespace} -- the fleet tunnel is"
            " an opt-in app (docs/deployment/edge-vpn.md), so deploy it before joining"
        )
    return pod


def _culvert(namespace: str, pod: str, argv: list[str]) -> str:
    """Run one of culvert's own commands in its pod and return its stdout."""
    result = _kubectl(["-n", namespace, "exec", pod, "--", *argv])
    if result.returncode != 0:
        raise BastionError(f"culvert `{' '.join(argv)}` failed: {result.stderr.strip()}")
    return result.stdout


def _route_facts(namespace: str, pod: str) -> tuple[str, str]:
    """The culvert pod's own address and the client range it forwards into.

    One `kubectl get pod -o json` for both, because they are read together and
    a second call could answer about a different pod after a roll.
    """
    result = _kubectl(["-n", namespace, "get", "pod", pod, "-o", "json"])
    if result.returncode != 0:
        raise BastionError(f"cannot read the culvert pod {pod}: {result.stderr.strip()}")
    try:
        body = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise BastionError(f"the culvert pod {pod} returned no readable JSON") from error

    pod_ip = str((body.get("status") or {}).get("podIP") or "")
    if not pod_ip:
        raise BastionError(f"the culvert pod {pod} carries no podIP yet -- wait for it to be Running")

    containers = (body.get("spec") or {}).get("containers") or [{}]
    env = {str(item.get("name")): str(item.get("value", "")) for item in containers[0].get("env") or []}
    client_range = env.get(WG_NETWORK_ENV, "")
    if not client_range:
        raise BastionError(
            f"the culvert pod {pod} carries no {WG_NETWORK_ENV}, so the tunnel runs no WireGuard"
            " listener and there is no client range to route"
        )
    return pod_ip, client_range


def _covers(client_range: str, address: str) -> bool:
    """Whether a routed range actually carries a peer's tunnel address."""
    try:
        return ipaddress.ip_address(address) in ipaddress.ip_network(client_range, strict=False)
    except ValueError:
        return False


def _program_hub_route(instance_id: str, client_range: str, pod_ip: str) -> None:
    """Point the tunnel's client range at the culvert pod, on this call.

    `replace` rather than `add`, because the pod address changes on every roll
    and a stale route is a reach-back that times out against a healthy tunnel.
    """
    _ssm_run(instance_id, [
        "set -eu",
        f"ip route replace {client_range} via {pod_ip}",
        f"ip route get {client_range.split('/')[0]}",
    ], comment="route the tunnel client range at the culvert pod")


def _tunnel_address() -> str:
    """The Elastic IP the tunnel answers on, off the cluster secret.

    Empty on `edge.ingest.tunnel.address.mode: byo`, where the deployer holds an
    address this deployment never sees and opens the toolbox's UDP egress
    themselves (terraform/modules/toolbox/aws variables.tf `tunnel`).
    """
    result = _kubectl([
        "-n", CLUSTER_SECRET_NAMESPACE, "get", CLUSTER_SECRET,
        "-o", TUNNEL_ADDRESS_JSONPATH,
    ])
    return result.stdout.strip() if result.returncode == 0 else ""


# --- the hub's peers ----------------------------------------------------------


def _wg_table(text: str) -> dict[str, str]:
    """`wg show <if> <field>` output -- one tab-separated pair per line."""
    table: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip():
            table[parts[0].strip()] = parts[1].strip()
    return table


def _handshake(epoch: str) -> str:
    try:
        seconds = int(epoch)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return "never"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _peers(namespace: str, pod: str) -> list[dict[str, str]]:
    """Every peer on the hub: name, tunnel address and last handshake.

    Joined on the tunnel address rather than read out of wg0.conf, because that
    file carries the server's own private key and none of this needs it.
    """
    allowed = _wg_table(_culvert(namespace, pod, ["wg", "show", HUB_INTERFACE, "allowed-ips"]))
    handshakes = _wg_table(
        _culvert(namespace, pod, ["wg", "show", HUB_INTERFACE, "latest-handshakes"])
    )
    try:
        allocations = json.loads(_culvert(namespace, pod, ["cat", CULVERT_ALLOCATIONS]) or "{}")
    except json.JSONDecodeError:
        allocations = {}
    by_address = {str(address): str(name) for name, address in allocations.items()}

    peers: list[dict[str, str]] = []
    for pubkey, allowed_ips in allowed.items():
        address = allowed_ips.split(",")[0].strip().split("/")[0]
        peers.append({
            "name": by_address.get(address, "(unallocated)"),
            "address": address,
            "handshake": _handshake(handshakes.get(pubkey, "0")),
        })
    return sorted(peers, key=lambda peer: peer["name"])


# --- SSM Run Command ----------------------------------------------------------


def _ssm_run(instance_id: str, commands: list[str], *, comment: str,
             timeout: float = DEFAULT_WAIT_TIMEOUT) -> str:
    """One AWS-RunShellScript invocation, waited out, returning its stdout.

    Run Command rather than a Session, because `join` installs a file and brings
    an interface up unattended and a Session needs a terminal. Nothing secret
    goes in `commands`: the parameters are retrievable from SSM afterwards.
    """
    sent = aws_cli.run_aws([
        "ssm", "send-command",
        "--instance-ids", instance_id,
        "--document-name", "AWS-RunShellScript",
        "--comment", comment,
        "--parameters", json.dumps({"commands": commands}),
        "--output", "json",
    ], timeout=60)
    if sent.returncode != 0:
        raise BastionError(f"{comment}: ssm send-command failed -- {sent.stderr.strip()}")
    try:
        command_id = json.loads(sent.stdout or "{}")["Command"]["CommandId"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise BastionError(f"{comment}: ssm send-command returned no command id") from error

    deadline = time.monotonic() + timeout
    while True:
        got = aws_cli.run_aws([
            "ssm", "get-command-invocation",
            "--command-id", command_id,
            "--instance-id", instance_id,
            "--output", "json",
        ], timeout=30)
        if got.returncode == 0:
            try:
                body = json.loads(got.stdout or "{}")
            except json.JSONDecodeError:
                body = {}
            status = str(body.get("Status", ""))
            if status and status not in ("Pending", "InProgress", "Delayed"):
                if status != "Success":
                    raise BastionError(
                        f"{comment}: the instance reported {status} -- "
                        f"{str(body.get('StandardErrorContent', '')).strip()}"
                    )
                return str(body.get("StandardOutputContent", ""))
        if time.monotonic() >= deadline:
            raise BastionError(f"{comment}: the Run Command invocation never finished")
        time.sleep(DEFAULT_POLL_INTERVAL)


# --- join ---------------------------------------------------------------------


def _mint_instance_key(instance_id: str) -> str:
    """The admin peer's keypair, minted ON the instance so the private half
    never crosses a wire or lands in the SSM command history."""
    out = _ssm_run(instance_id, [
        "set -eu",
        "command -v wg >/dev/null 2>&1 || dnf install -y wireguard-tools",
        "install -d -m 0700 /etc/wireguard",
        f"test -s {ADMIN_KEY_PATH} || (umask 077; wg genkey > {ADMIN_KEY_PATH})",
        f"wg pubkey < {ADMIN_KEY_PATH}",
    ], comment="mint the admin peer keypair")
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        raise BastionError(
            "the instance returned no public key -- check that wireguard-tools"
            " installed (AL2023 pulls it from the default repos)"
        )
    return lines[-1]


def _peer_address(config: str) -> str:
    """The tunnel address the issued config carries."""
    for line in config.splitlines():
        if line.startswith("Address = "):
            return line.split("=", 1)[1].strip().split("/")[0]
    raise BastionError("the issued config carries no Address line")


def _admin_config(config: str, address: str) -> str:
    """The issued config, made fit for the instance.

    The Endpoint host becomes the tunnel's own address, so the dial does not
    wait on a published DNS name. The DNS line goes, because wg-quick needs
    resolvconf to honour it and the admin peer has no use for the fleet's
    resolvers. AllowedIPs narrows to the peer's own /24, so only hub traffic
    goes down the tunnel and the instance keeps its own route out.
    """
    peer_address = _peer_address(config)
    octets = peer_address.split(".")
    if len(octets) != 4:
        raise BastionError(f"the issued config's Address {peer_address!r} is not IPv4")
    hub_range = f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"

    out: list[str] = []
    for line in config.splitlines():
        if line.startswith("DNS = "):
            continue
        if line.startswith("AllowedIPs = "):
            out.append(f"AllowedIPs = {hub_range}")
            continue
        if line.startswith("Endpoint = "):
            port = line.rsplit(":", 1)[-1].strip()
            out.append(f"Endpoint = {address}:{port}")
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def _issue_admin_config(namespace: str, pod: str, public_key: str) -> str:
    """Mint the admin peer on the hub and read back the config it wrote."""
    _culvert(namespace, pod, [
        "generate-client",
        "--name", ADMIN_PEER_NAME,
        "--protocol", "wireguard",
        "--pubkey", public_key,
    ])
    return _culvert(namespace, pod, ["cat", f"{CULVERT_CLIENTS}/{ADMIN_PEER_NAME}-wg-split.conf"])


def _install_admin_config(instance_id: str, config: str) -> None:
    """Write the config, fill the private key in locally, bring the tunnel up."""
    _ssm_run(instance_id, [
        "set -eu",
        "umask 077",
        f"cat > {ADMIN_CONF_PATH} <<'DFE_ADMIN_CONF'\n{config}DFE_ADMIN_CONF",
        f'sed -i "s|{PRIVATE_KEY_PLACEHOLDER}|$(cat {ADMIN_KEY_PATH})|" {ADMIN_CONF_PATH}',
        f"wg-quick down {ADMIN_INTERFACE} >/dev/null 2>&1 || true",
        f"wg-quick up {ADMIN_CONF_PATH}",
    ], comment="install the admin peer config and bring its tunnel up")


def _prove_handshake(instance_id: str, hub_address: str) -> None:
    """A handshake, not a bring-up: wg-quick reports success on a config that
    reaches nothing, so the peer is only joined once the hub has answered."""
    out = _ssm_run(instance_id, [
        "set -eu",
        f"ping -c 2 -W 2 {hub_address} >/dev/null 2>&1 || true",
        f"wg show {ADMIN_INTERFACE} latest-handshakes",
    ], comment="prove the admin peer completed a handshake")
    if not any(_handshake(value) != "never" for value in _wg_table(out).values()):
        raise BastionError(
            "the admin peer is installed but has completed no handshake -- check the"
            " toolbox security group's UDP egress rule and the tunnel's own"
            " loadBalancerSourceRanges"
        )


def _record_admin_peer(namespace: str, ttl_minutes: int) -> None:
    """The peer's name, namespace and deadline, so `down` revokes what this
    machine minted and the deadline is a fact on disk rather than an intention."""
    ADMIN_STATE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(ADMIN_STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({
            "name": ADMIN_PEER_NAME,
            "namespace": namespace,
            "ttl_minutes": ttl_minutes,
            "expires_at": int(time.time()) + ttl_minutes * 60,
        }, handle)


def cmd_bastion_join(args: argparse.Namespace) -> int:
    namespace = _namespace(args)
    ttl_minutes = args.ttl or DEFAULT_ADMIN_TTL_MINUTES
    # Minting a peer that every isolation rule then drops looks like a working
    # join and reaches nothing, so the verb says which upstream change it waits
    # on rather than issuing a credential for a hole that is not open.
    if not ADMIN_PEER_SUPPORTED:
        print(
            "dfe-ops bastion: joining the hub AS A PEER reaches no appliance yet. culvert installs"
            " the client-to-client DROP over every pair of tunnel interfaces before its admin"
            " ACCEPT, so a peer source is dropped first, and hyperi-io/culvert#40 is the peer class"
            " that would carry the exception. Use `dfe-ops bastion hub <peer>`, which routes the"
            " client range at the culvert pod and needs no peer at all.",
            file=sys.stderr,
        )
        return 1
    try:
        instance_id = _instance_id()
        pod = _culvert_pod(namespace)
        address = _tunnel_address()
        if not address:
            raise BastionError(
                "the cluster secret carries no dfe.hyperi.io/tunnel_address, so there"
                " is no address to dial and the toolbox security group has no UDP"
                " egress rule -- set edge.ingest.tunnel.address.mode to forwarder, or"
                " open that egress yourself against the address you brought"
            )
        public_key = _mint_instance_key(instance_id)
        config = _admin_config(_issue_admin_config(namespace, pod, public_key), address)
        _install_admin_config(instance_id, config)
        peer_address = _peer_address(config)
        hub_address = ".".join([*peer_address.split(".")[:3], "1"])
        _prove_handshake(instance_id, hub_address)
    except BastionError as error:
        print(f"dfe-ops bastion: {error}", file=sys.stderr)
        return 1

    _record_admin_peer(namespace, ttl_minutes)
    print(
        f"dfe-ops bastion: joined the hub as {ADMIN_PEER_NAME} on {peer_address}, "
        f"revoke by {_handshake(str(int(time.time()) + ttl_minutes * 60))}",
        file=sys.stderr,
    )
    print(
        "A config that reaches EVERY appliance is the one credential client"
        " isolation does not stop, so it is minted per session and never stored --"
        " `dfe-ops bastion down` revokes it before the instance is terminated.",
        file=sys.stderr,
    )
    print("  dfe-ops bastion peers", file=sys.stderr)
    print("  dfe-ops bastion hub <peer>", file=sys.stderr)
    return 0


# --- peers --------------------------------------------------------------------


def cmd_bastion_peers(args: argparse.Namespace) -> int:
    namespace = _namespace(args)
    try:
        peers = _peers(namespace, _culvert_pod(namespace))
    except BastionError as error:
        print(f"dfe-ops bastion: {error}", file=sys.stderr)
        return 1
    if not peers:
        print("dfe-ops bastion: the hub has no peers", file=sys.stderr)
        return 0
    print(f"{'name':<28} {'tunnel address':<16} last handshake", file=sys.stderr)
    for peer in peers:
        print(f"{peer['name']:<28} {peer['address']:<16} {peer['handshake']}", file=sys.stderr)
    return 0


# --- hub ----------------------------------------------------------------------


def cmd_bastion_hub(args: argparse.Namespace) -> int:
    namespace = _namespace(args)
    try:
        instance_id = _instance_id()
        document = str(_output(_tofu_outputs(), "toolbox_ssm_session_document"))
        if args.port not in ADMIN_REACH:
            raise BastionError(
                f"port {args.port} is outside the admin class's reach {list(ADMIN_REACH)}"
                " (peers.classes.admin.reach in helm/edge/culvert/values.yaml)"
            )
        pod = _culvert_pod(namespace)
        peers = _peers(namespace, pod)
        peer = next((p for p in peers if p["name"] == args.peer), None)
        if peer is None:
            names = ", ".join(p["name"] for p in peers) or "(none)"
            raise BastionError(f"unknown peer {args.peer!r} -- the hub carries: {names}")
        pod_ip, client_range = _route_facts(namespace, pod)
        if not _covers(client_range, peer["address"]):
            raise BastionError(
                f"peer {peer['name']} is at {peer['address']}, outside the {client_range} the hub"
                " forwards -- a route to that range would not carry it"
            )
        _program_hub_route(instance_id, client_range, pod_ip)
    except BastionError as error:
        print(f"dfe-ops bastion: {error}", file=sys.stderr)
        return 1

    # A Port session cannot reach a peer: every forward document fixes its host
    # and port at PLAN time (toolbox/aws/CONTRACT.md), and a peer's tunnel
    # address is not known then. The logged shell is the session that can.
    print(
        f"dfe-ops bastion: {client_range} routed at the culvert pod {pod_ip}; {peer['name']} is at"
        f" {peer['address']}:{args.port} through the tunnel, last handshake {peer['handshake']}",
        file=sys.stderr,
    )
    reach = f"ssh {peer['address']}" if args.port == 22 else f"curl https://{peer['address']}/"
    print(f"  {reach}", file=sys.stderr)
    # The instance route is one half: a VPC delivers a packet by looking its
    # DESTINATION up, so the tunnel range also needs a VPC route at the culvert
    # node's interface with that interface's source/destination check off.
    print(
        f"The VPC must also route {client_range} at the culvert node's network interface, with that"
        " interface's source/destination check off, or the packet never leaves the subnet.",
        file=sys.stderr,
    )
    print(
        "This is the LOGGED shell session; a forward cannot reach a peer, because"
        " every forward document fixes its host and port at plan time.",
        file=sys.stderr,
    )
    return _run_interactive(
        ["aws", "ssm", "start-session", "--target", instance_id, "--document-name", document]
    )


# --- up ------------------------------------------------------------------


def _wait_for_online(instance_id: str, *, timeout: float, poll: float = DEFAULT_POLL_INTERVAL) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        result = aws_cli.run_aws(
            [
                "ssm", "describe-instance-information",
                "--filters", f"Key=InstanceIds,Values={instance_id}",
                "--output", "json",
            ],
            timeout=30,
        )
        if result.returncode == 0:
            try:
                entries = json.loads(result.stdout or "{}").get("InstanceInformationList", [])
            except json.JSONDecodeError:
                entries = []
            if entries and entries[0].get("PingStatus") == "Online":
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def cmd_bastion_up(args: argparse.Namespace) -> int:
    try:
        _set_toolbox_enabled(True, ttl_minutes=args.ttl)
    except BastionError as error:
        print(f"dfe-ops bastion: {error}", file=sys.stderr)
        return 1

    if not _render_dial_tofu():
        print("dfe-ops bastion: render_dial.py --tofu failed", file=sys.stderr)
        return 1

    if not _apply_toolbox():
        print("dfe-ops bastion: tofu apply failed", file=sys.stderr)
        return 1

    outputs = _tofu_outputs()
    instance_id = str(_output(outputs, "toolbox_instance_id"))
    if not instance_id:
        print("dfe-ops bastion: apply succeeded but toolbox_instance_id is empty", file=sys.stderr)
        return 1

    print(f"dfe-ops bastion: waiting for {instance_id} to report PingStatus Online...", file=sys.stderr)
    if not _wait_for_online(instance_id, timeout=args.wait_timeout):
        print(
            f"dfe-ops bastion: {instance_id} did not report Online within {args.wait_timeout:.0f}s -- "
            f"check the instance's user-data log (/var/log/cloud-init-output.log) once you can reach it",
            file=sys.stderr,
        )
        return 1

    print(f"dfe-ops bastion: {instance_id} is Online", file=sys.stderr)
    print("  dfe-ops bastion shell", file=sys.stderr)
    targets = _output(outputs, "toolbox_targets", {})
    for name in sorted(targets) if isinstance(targets, dict) else []:
        print(f"  dfe-ops bastion forward {name} <local-port>", file=sys.stderr)
    return 0


# --- shell -----------------------------------------------------------------


def cmd_bastion_shell(args: argparse.Namespace) -> int:
    outputs = _tofu_outputs()
    instance_id = str(_output(outputs, "toolbox_instance_id"))
    document = str(_output(outputs, "toolbox_ssm_session_document"))
    if not instance_id:
        print("dfe-ops bastion: no toolbox instance -- run `dfe-ops bastion up` first", file=sys.stderr)
        return 1

    return _run_interactive(
        ["aws", "ssm", "start-session", "--target", instance_id, "--document-name", document]
    )


# --- forward -----------------------------------------------------------------


def _write_scratch_kubeconfig(*, local_port: int, server_host: str, cluster_name: str, region: str) -> Path:
    """A per-session scratch kubeconfig, 0600 from creation (os.open, never a
    chmod after the fact -- that has a window). tls-server-name keeps
    certificate verification intact through the tunnel; insecure-skip-tls-verify
    is never written here, on purpose."""
    SCRATCH_KUBECONFIG.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        "- name: toolbox-forward\n"
        "  cluster:\n"
        f"    server: https://127.0.0.1:{local_port}\n"
        f"    tls-server-name: {server_host}\n"
        "contexts:\n"
        "- name: toolbox-forward\n"
        "  context:\n"
        "    cluster: toolbox-forward\n"
        "    user: toolbox-forward\n"
        "current-context: toolbox-forward\n"
        "users:\n"
        "- name: toolbox-forward\n"
        "  user:\n"
        "    exec:\n"
        "      apiVersion: client.authentication.k8s.io/v1beta1\n"
        "      command: aws\n"
        "      args:\n"
        "      - eks\n"
        "      - get-token\n"
        "      - --cluster-name\n"
        f"      - {cluster_name}\n"
        "      - --region\n"
        f"      - {region}\n"
    )
    fd = os.open(SCRATCH_KUBECONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return SCRATCH_KUBECONFIG


def cmd_bastion_forward(args: argparse.Namespace) -> int:
    outputs = _tofu_outputs()
    instance_id = str(_output(outputs, "toolbox_instance_id"))
    if not instance_id:
        print("dfe-ops bastion: no toolbox instance -- run `dfe-ops bastion up` first", file=sys.stderr)
        return 1

    targets = _output(outputs, "toolbox_targets", {})
    target = targets.get(args.target) if isinstance(targets, dict) else None
    if target is None:
        names = ", ".join(sorted(targets)) if isinstance(targets, dict) and targets else "(none)"
        print(f"dfe-ops bastion: unknown target {args.target!r} -- known targets: {names}", file=sys.stderr)
        return 1

    document_name = str(target.get("document_name", ""))
    if not document_name:
        print(f"dfe-ops bastion: target {args.target!r} has no forward document", file=sys.stderr)
        return 1

    # Not a suggestion -- Session Manager genuinely writes no transcript for a
    # Port session, by AWS's own design (toolbox/aws/CONTRACT.md #6).
    print(
        "dfe-ops bastion: this forward session is NOT recorded by Session Manager. "
        "CloudTrail records that a session started and against which document; the "
        "traffic inside the tunnel is not logged.",
        file=sys.stderr,
    )

    if args.target == "eks-api":
        cluster_name = str(_output(outputs, "cluster_name"))
        region = str(_output(outputs, "DFE_REGION"))
        kubeconfig_path = _write_scratch_kubeconfig(
            local_port=args.local_port,
            server_host=str(target.get("host", "")),
            cluster_name=cluster_name,
            region=region,
        )
        print(
            f"dfe-ops bastion: wrote {kubeconfig_path} (0600, expires with this session -- "
            f"use it with `kubectl --kubeconfig {kubeconfig_path} ...`)",
            file=sys.stderr,
        )

    return _run_interactive(
        [
            "aws", "ssm", "start-session",
            "--target", instance_id,
            "--document-name", document_name,
            "--parameters", f"localPortNumber={args.local_port}",
        ]
    )


# --- down ------------------------------------------------------------------


def _revoke_admin_peer() -> int:
    """Revoke the admin peer this machine minted, and prove it off the hub.

    Runs BEFORE the destroy, because the instance is terminated rather than
    stopped and a peer left behind is a credential nobody tracks. A WireGuard
    peer carries no CRL entry -- culvert revokes it by removing it from the
    live interface and refuses to report success when it cannot -- so the proof
    is the hub's own peer list, which is stronger than a certificate serial.
    """
    if not ADMIN_STATE.is_file():
        print("  [ok] admin peer: none joined from this machine", file=sys.stderr)
        return 0
    try:
        state = json.loads(ADMIN_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    name = str(state.get("name") or ADMIN_PEER_NAME)
    namespace = str(state.get("namespace") or "dfe")

    try:
        pod = _culvert_pod(namespace)
        _culvert(namespace, pod, ["revoke-client", "--protocol", "wireguard", name])
        remaining = [peer["name"] for peer in _peers(namespace, pod)]
    except BastionError as error:
        print(
            f"  [FAIL] admin peer {name}: {error} -- revoke it by hand with"
            f" `kubectl -n {namespace} exec <culvert pod> --"
            f" revoke-client --protocol wireguard {name}`",
            file=sys.stderr,
        )
        return 1

    if name in remaining:
        print(f"  [FAIL] admin peer {name}: still on the hub after revocation", file=sys.stderr)
        return 1
    ADMIN_STATE.unlink(missing_ok=True)
    print(f"  [ok] admin peer {name}: revoked and off the hub", file=sys.stderr)
    return 0


def _prove_teardown(instance_id: str, problems: int = 0) -> int:
    """Query and print each of the four things a leak would look like --
    never trust a single check, and never claim done without evidence.

    `problems` carries the admin peer's own verdict in, so one summary line
    covers both halves of a `down`.
    """

    if instance_id:
        result = aws_cli.run_aws(
            [
                "ec2", "describe-instances",
                "--instance-ids", instance_id,
                "--query", "Reservations[].Instances[].State.Name",
                "--output", "json",
            ],
            timeout=30,
        )
        try:
            states = json.loads(result.stdout or "[]") if result.returncode == 0 else []
        except json.JSONDecodeError:
            states = []
        remaining = [s for s in states if s != "terminated"]
        ok = not remaining
        print(f"  [{'ok' if ok else 'FAIL'}] instance {instance_id}: {states or 'gone'}", file=sys.stderr)
        problems += 0 if ok else 1

        sessions = aws_cli.run_aws(
            [
                "ssm", "describe-sessions", "--state", "Active",
                "--filters", f"key=Target,value={instance_id}",
                "--output", "json",
            ],
            timeout=30,
        )
        try:
            active = json.loads(sessions.stdout or "{}").get("Sessions", []) if sessions.returncode == 0 else []
        except json.JSONDecodeError:
            active = []
        ok = not active
        print(f"  [{'ok' if ok else 'FAIL'}] active sessions: {len(active)}", file=sys.stderr)
        problems += 0 if ok else 1
    else:
        print("  [ok] instance: never brought up", file=sys.stderr)
        print("  [ok] active sessions: never brought up", file=sys.stderr)

    volumes = aws_cli.run_aws(
        [
            "ec2", "describe-volumes",
            "--filters", "Name=tag:dfe.hyperi.io/component,Values=toolbox",
            "--query", "Volumes[].VolumeId",
            "--output", "json",
        ],
        timeout=30,
    )
    try:
        volume_ids = json.loads(volumes.stdout or "[]") if volumes.returncode == 0 else []
    except json.JSONDecodeError:
        volume_ids = []
    ok = not volume_ids
    print(f"  [{'ok' if ok else 'FAIL'}] volumes: {volume_ids or 'none'}", file=sys.stderr)
    problems += 0 if ok else 1

    enis = aws_cli.run_aws(
        [
            "ec2", "describe-network-interfaces",
            "--filters", "Name=tag:dfe.hyperi.io/component,Values=toolbox",
            "--query", "NetworkInterfaces[].NetworkInterfaceId",
            "--output", "json",
        ],
        timeout=30,
    )
    try:
        eni_ids = json.loads(enis.stdout or "[]") if enis.returncode == 0 else []
    except json.JSONDecodeError:
        eni_ids = []
    ok = not eni_ids
    print(f"  [{'ok' if ok else 'FAIL'}] network interfaces: {eni_ids or 'none'}", file=sys.stderr)
    problems += 0 if ok else 1

    print(
        f"=== bastion down {'CLEAN' if not problems else 'INCOMPLETE'} ({problems} problem(s)) ===",
        file=sys.stderr,
    )
    return 1 if problems else 0


def cmd_bastion_down(args: argparse.Namespace) -> int:
    outputs_before = _tofu_outputs()
    instance_id = str(_output(outputs_before, "toolbox_instance_id"))

    # Revoke FIRST: the config dies with the instance, but the peer entry on the
    # hub does not, and a failure here still terminates -- the instance is what
    # bills and what holds the private key -- with the summary reporting it.
    admin_problems = _revoke_admin_peer()

    try:
        _set_toolbox_enabled(False)
    except BastionError as error:
        print(f"dfe-ops bastion: {error}", file=sys.stderr)
        return 1

    if not _render_dial_tofu():
        print("dfe-ops bastion: render_dial.py --tofu failed", file=sys.stderr)
        return 1

    if not _destroy_toolbox():
        print("dfe-ops bastion: tofu destroy failed", file=sys.stderr)
        return 1

    if SCRATCH_KUBECONFIG.exists():
        SCRATCH_KUBECONFIG.unlink()
        print(f"dfe-ops bastion: removed {SCRATCH_KUBECONFIG}", file=sys.stderr)

    return _prove_teardown(instance_id, admin_problems)


# --- status ------------------------------------------------------------------


def cmd_bastion_status(args: argparse.Namespace) -> int:
    outputs = _tofu_outputs()
    instance_id = str(_output(outputs, "toolbox_instance_id"))
    if not instance_id:
        print("dfe-ops bastion: not enabled -- no toolbox instance", file=sys.stderr)
        return 0

    ping = aws_cli.run_aws(
        [
            "ssm", "describe-instance-information",
            "--filters", f"Key=InstanceIds,Values={instance_id}",
            "--output", "json",
        ],
        timeout=30,
    )
    try:
        entries = json.loads(ping.stdout or "{}").get("InstanceInformationList", []) if ping.returncode == 0 else []
    except json.JSONDecodeError:
        entries = []
    ping_status = entries[0].get("PingStatus", "Unknown") if entries else "not registered"
    print(f"instance:        {instance_id} ({ping_status})", file=sys.stderr)

    sessions = aws_cli.run_aws(
        [
            "ssm", "describe-sessions", "--state", "Active",
            "--filters", f"key=Target,value={instance_id}",
            "--output", "json",
        ],
        timeout=30,
    )
    try:
        active = json.loads(sessions.stdout or "{}").get("Sessions", []) if sessions.returncode == 0 else []
    except json.JSONDecodeError:
        active = []
    print(f"active sessions: {len(active)}", file=sys.stderr)

    targets = _output(outputs, "toolbox_targets", {})
    names = ", ".join(sorted(targets)) if isinstance(targets, dict) and targets else "(none)"
    print(f"forward targets: {names}", file=sys.stderr)

    # The admin peer's deadline is a fact on disk rather than an intention, so
    # an operator can see a peer that has outlived the session that minted it.
    if ADMIN_STATE.is_file():
        try:
            state = json.loads(ADMIN_STATE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        expires = int(state.get("expires_at") or 0)
        overdue = " (OVERDUE -- run `dfe-ops bastion down`)" if expires and expires < time.time() else ""
        print(
            f"admin peer:      {state.get('name', ADMIN_PEER_NAME)}, "
            f"revoke by {_handshake(str(expires))}{overdue}",
            file=sys.stderr,
        )
    else:
        print("admin peer:      none joined from this machine", file=sys.stderr)
    return 0


# --- parser ------------------------------------------------------------------


def add_bastion_subparser(sub: argparse._SubParsersAction) -> None:
    """Register `dfe-ops bastion` and its actions."""
    bastion = sub.add_parser(
        "bastion",
        help="up/join/peers/hub/shell/forward/down/status for the on-demand SSM-managed toolbox instance",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    actions = bastion.add_subparsers(dest="bastion_action", required=True, metavar="<action>")

    def with_namespace(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
        """The namespace culvert runs in, for every verb that reaches its pod."""
        parser.add_argument(
            "--namespace", default=None,
            help="namespace the fleet tunnel runs in (default: DFE_NAMESPACE, else dfe)",
        )
        return parser

    up = actions.add_parser(
        "up",
        help="flip toolbox.enabled on, apply the toolbox target, wait for PingStatus Online",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    up.add_argument("--ttl", type=int, default=None, metavar="MIN", help="override toolbox.ttl_minutes for this call")
    up.add_argument(
        "--wait-timeout", type=float, default=DEFAULT_WAIT_TIMEOUT,
        help="seconds to wait for the instance to report PingStatus Online",
    )
    up.set_defaults(func=cmd_bastion_up)

    join = with_namespace(actions.add_parser(
        "join",
        help="mint an admin peer on the fleet tunnel -- refused until hyperi-io/culvert#40 lands",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    ))
    join.add_argument(
        "--ttl", type=int, default=None, metavar="MIN",
        help="minutes before the admin peer is due for revocation, recorded for `down` and `status`",
    )
    join.set_defaults(func=cmd_bastion_join)

    peers = with_namespace(actions.add_parser(
        "peers",
        help="the hub's peers: name, tunnel address and last handshake",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    ))
    peers.set_defaults(func=cmd_bastion_peers)

    hub = with_namespace(actions.add_parser(
        "hub",
        help="route the client range at the culvert pod and reach one appliance, in the LOGGED shell Session",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    ))
    hub.add_argument("peer", help="a name from `dfe-ops bastion peers`")
    hub.add_argument(
        "--port", type=int, default=ADMIN_REACH[0],
        help=f"the appliance port to reach, within the admin class's reach {list(ADMIN_REACH)}",
    )
    hub.set_defaults(func=cmd_bastion_hub)

    shell = actions.add_parser(
        "shell",
        help="open the logged interactive shell Session",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    shell.set_defaults(func=cmd_bastion_shell)

    forward = actions.add_parser(
        "forward",
        help="port-forward to a named target -- NOT recorded by Session Manager",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    forward.add_argument("target", help="a name from `dfe-ops bastion status`'s forward targets")
    forward.add_argument("local_port", type=int, help="local port to bind on this machine")
    forward.set_defaults(func=cmd_bastion_forward)

    down = actions.add_parser(
        "down",
        help="revoke the admin peer, flip toolbox.enabled off, destroy the toolbox target, and prove nothing remains",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    down.set_defaults(func=cmd_bastion_down)

    status = actions.add_parser(
        "status",
        help="report the toolbox's current state",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    status.set_defaults(func=cmd_bastion_status)
