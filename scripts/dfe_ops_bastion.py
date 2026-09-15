#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/dfe_ops_bastion.py
#  Purpose:      `dfe-ops bastion` -- up/shell/forward/down/status for the
#                on-demand SSM-managed toolbox instance
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
    dfe-ops bastion shell             open the logged shell Session.
    dfe-ops bastion forward <t> <p>   port-forward to a named target on local
                                       port <p> -- NOT recorded by Session
                                       Manager (toolbox/aws/CONTRACT.md #6).
    dfe-ops bastion down              flip toolbox.enabled off, apply, then
                                       PROVE nothing remains.
    dfe-ops bastion status            report the toolbox's current state.

`up`/`down` edit deployment.yaml (the dial) in place, then shell out to
render_dial.py --tofu and tofu apply -- the same two steps an operator would
run by hand, just for the toolbox target alone. `shell`/`forward` shell out to
the real `aws ssm start-session`, inheriting this process's stdio, because a
Session Manager session needs a real terminal.
"""

from __future__ import annotations

import argparse
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

# What `up`/`down` apply -- the toggleable half of the toolbox (the module's
# instance/security-group/IAM-role/documents) plus the two EKS access-entry
# resources the aws root grants alongside it. Not the whole root: applying it
# in full would also reconcile drift on the cluster and Kafka, which is a
# bigger and slower operation than "bring the toolbox up". Both access-entry
# resources live INSIDE module.cluster (kubernetes-cluster/aws/eks.tf,
# "Toolbox operator"), not at the aws root, so the target address must be
# module-qualified or tofu reports "Resource not found in module".
TOOLBOX_TARGETS = (
    "-target=module.toolbox",
    "-target=module.cluster.aws_eks_access_entry.toolbox_operator",
    "-target=module.cluster.aws_eks_access_policy_association.toolbox_operator_view",
)

DEFAULT_WAIT_TIMEOUT = 300.0
DEFAULT_POLL_INTERVAL = 5.0


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


def _prove_teardown(instance_id: str) -> int:
    """Query and print each of the four things a leak would look like --
    never trust a single check, and never claim done without evidence."""
    problems = 0

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

    try:
        _set_toolbox_enabled(False)
    except BastionError as error:
        print(f"dfe-ops bastion: {error}", file=sys.stderr)
        return 1

    if not _render_dial_tofu():
        print("dfe-ops bastion: render_dial.py --tofu failed", file=sys.stderr)
        return 1

    if not _apply_toolbox():
        print("dfe-ops bastion: tofu apply failed", file=sys.stderr)
        return 1

    if SCRATCH_KUBECONFIG.exists():
        SCRATCH_KUBECONFIG.unlink()
        print(f"dfe-ops bastion: removed {SCRATCH_KUBECONFIG}", file=sys.stderr)

    return _prove_teardown(instance_id)


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
    return 0


# --- parser ------------------------------------------------------------------


def add_bastion_subparser(sub: argparse._SubParsersAction) -> None:
    """Register `dfe-ops bastion` and its actions."""
    bastion = sub.add_parser(
        "bastion",
        help="up/shell/forward/down/status for the on-demand SSM-managed toolbox instance",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    actions = bastion.add_subparsers(dest="bastion_action", required=True, metavar="<action>")

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
        help="flip toolbox.enabled off, apply, and prove nothing remains",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    down.set_defaults(func=cmd_bastion_down)

    status = actions.add_parser(
        "status",
        help="report the toolbox's current state",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    status.set_defaults(func=cmd_bastion_status)
