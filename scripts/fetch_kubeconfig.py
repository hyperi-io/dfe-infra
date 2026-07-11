#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         fetch_kubeconfig.py
#  Purpose:      Fetch a kubeconfig from an RKE2/K8s node via an SSH private key
#                held in OpenBao/Vault, rewriting the API server to a reachable
#                address. Generic - every environment specific (Vault path, node,
#                remote path, server) is a flag, so it works for any cluster, not
#                just one. Runs under the python3 allow-list.
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Pull a node's kubeconfig over SSH, keyed by an OpenBao/Vault-stored SSH key.

RKE2 writes its admin kubeconfig on the server node (default
``/etc/rancher/rke2/rke2.yaml``) with the API server as ``https://127.0.0.1:6443``.
This fetches it over SSH and rewrites the server to an address you can reach, so
``kubectl --kubeconfig <out>`` works from your workstation.

    # DFE cluster on devex (server node dfe-k8s-1):
    python3 scripts/fetch_kubeconfig.py --node 10.66.0.204 --out .tmp/dfe.kubeconfig

    # name the merged context, point kubectl at a specific API URL:
    python3 scripts/fetch_kubeconfig.py --node k8s-1.example --server \
        https://api.k8s.example:6443 --context prod --out .tmp/prod.kubeconfig

The SSH private key is read from Vault/OpenBao (``--vault-path`` / ``--vault-field``,
base64-encoded) into a 0600 temp file that is removed on exit - it is never written
to a durable path. Requires ``VAULT_ADDR`` + a token in the environment (as the
other scripts here assume) and the ``bao`` (or ``--vault-cmd vault``) + ``ssh``
CLIs. No third-party Python deps.
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
import tempfile

DEFAULT_USER = "ubuntu"
DEFAULT_VAULT_PATH = "kv/infrastructure/ssh"
DEFAULT_VAULT_FIELD = "private_key_b64"
DEFAULT_REMOTE_PATH = "/etc/rancher/rke2/rke2.yaml"
DEFAULT_VAULT_CMD = "bao"
# rke2/k3s write the loopback server here; rewrite it to a reachable address.
_LOOPBACK_SERVERS = ("https://127.0.0.1:6443", "https://[::1]:6443")


def _run(cmd: list[str], *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing output; raise with stderr on failure."""
    return subprocess.run(  # noqa: S603 - args are built from validated flags
        cmd,
        input=input_bytes,
        capture_output=True,
        check=True,
    )


def _read_ssh_key(vault_cmd: str, path: str, field: str) -> bytes:
    """Read the base64 SSH private key from Vault/OpenBao and decode it."""
    proc = _run([vault_cmd, "kv", "get", f"-field={field}", path])
    return base64.b64decode(proc.stdout)


def _fetch_remote(user: str, node: str, remote_path: str, key_file: str) -> str:
    """SSH to the node and return the remote kubeconfig text."""
    proc = _run(
        [
            "ssh",
            "-i",
            key_file,
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=15",
            f"{user}@{node}",
            f"cat {remote_path} 2>/dev/null || sudo cat {remote_path}",
        ]
    )
    return proc.stdout.decode("utf-8")


def _rewrite_server(kubeconfig: str, server: str) -> str:
    """Replace the loopback API-server URL with a reachable one."""
    out = kubeconfig
    for loopback in _LOOPBACK_SERVERS:
        out = out.replace(loopback, server)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch a node's kubeconfig via a Vault/OpenBao-stored SSH key.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--node", required=True, help="node host/IP to SSH to (the API server node)")
    ap.add_argument("--user", default=DEFAULT_USER, help="SSH user")
    ap.add_argument(
        "--vault-path", default=DEFAULT_VAULT_PATH, help="Vault/OpenBao kv path of the SSH key"
    )
    ap.add_argument(
        "--vault-field", default=DEFAULT_VAULT_FIELD, help="field holding the base64 private key"
    )
    ap.add_argument("--vault-cmd", default=DEFAULT_VAULT_CMD, help="secrets CLI: bao or vault")
    ap.add_argument(
        "--remote-path", default=DEFAULT_REMOTE_PATH, help="kubeconfig path on the node"
    )
    ap.add_argument(
        "--server",
        default=None,
        help="rewrite the API server to this URL (default: https://<node>:6443)",
    )
    ap.add_argument("--out", default="kubeconfig", help="local output kubeconfig path")
    args = ap.parse_args()

    if not os.environ.get("VAULT_ADDR"):
        print("VAULT_ADDR is not set (export it, or source your env)", file=sys.stderr)
        return 2

    server = args.server or f"https://{args.node}:6443"

    key_bytes = _read_ssh_key(args.vault_cmd, args.vault_path, args.vault_field)
    key_fd, key_file = tempfile.mkstemp(prefix="kc-ssh-", suffix=".key")
    try:
        os.write(key_fd, key_bytes)
        os.close(key_fd)
        os.chmod(key_file, 0o600)
        raw = _fetch_remote(args.user, args.node, args.remote_path, key_file)
    finally:
        os.unlink(key_file)  # the private key never persists

    kubeconfig = _rewrite_server(raw, server)

    out_path = args.out
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(kubeconfig)
    os.chmod(out_path, 0o600)

    print(f"wrote {out_path} (server {server})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()
        print(f"command failed: {' '.join(exc.cmd)}\n{stderr}", file=sys.stderr)
        sys.exit(1)
