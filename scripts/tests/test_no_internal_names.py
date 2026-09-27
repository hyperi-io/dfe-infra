#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_no_internal_names.py
#  Purpose:      Guard the public-at-GA rule: no internal estate hostname, host
#                or cluster nickname, vault path or private address survives
#                anywhere in the tree.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Refuse an internal estate name anywhere a reader of the public repo can see it.

dfe-infra becomes public at GA, so every committed byte ships to the world. An
internal hostname, a host nickname or an RFC 1918 address written down here
tells an outsider how the development estate is laid out, and it cannot be
taken back once the visibility flips -- rewriting a repo's history to remove it
is painful and easy to get wrong. Documentation names exist for this: RFC 2606
`example.com` / `example.internal` for hostnames, and the RFC 5737 ranges
192.0.2.0/24, 198.51.100.0/24 and 203.0.113.0/24 for addresses.

The sweep reads every tracked file and names the file and line of each hit.

    python3 -m pytest scripts/tests/test_no_internal_names.py -q
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The estate's name (which is also its zone), the private repo that runs it,
# its OpenBao base paths, the hosts, clusters, nodes and deployments addressed
# by nickname, and the two private ranges it numbers. Each is matched
# case-insensitively, so a capitalised product or heading is caught alongside a
# hostname. The `secret/dfe` path stops short of a hyphen: `secret/dfe-cluster`
# is a Kubernetes kind/name reference to a product Secret, not a vault path.
# Word boundaries keep "cluster bootstrap" and "dfe-backup" out of the match.
INTERNAL = re.compile(
    r"devex"
    r"|hyperi-infra"
    r"|secret/dfe(?![\w-])"
    r"|kv/services\b"
    r"|kv/infrastructure\b"
    r"|kv/dfe-test\b"
    r"|tyrell"
    r"|hypersec"
    r"|10\.66\."
    r"|10\.1\.2\."
    r"|dragonfly"
    r"|desktop-derek"
    r"|ghostburner"
    r"|proxmox"
    r"|\bdfe-b\b"
    r"|dfe-k8s-[0-9]"
    r"|\bcluster b\b",
    re.IGNORECASE,
)

# The vendor-domain hosts the product names on purpose: the Kubernetes label and
# annotation prefix, and the public brand-token site. Any other host under
# hyperi.io is estate; an apex mail address (sales@hyperi.io) has no host label.
PUBLIC_HOSTS = frozenset({"dfe.hyperi.io", "graphics.hyperi.io"})
_VENDOR_HOST = re.compile(r"(?<![\w.-])((?:[\w-]+\.)+hyperi\.io)(?![\w-])", re.IGNORECASE)

# versions.yaml and constraints/ are the version SSoT and are compared
# byte-for-byte against main by the drift guards, so they are never rewritten
# here. This file carries the pattern itself and would match on every line.
EXCLUDED = {
    "versions.yaml",
    "scripts/tests/test_no_internal_names.py",
}
EXCLUDED_PREFIXES = ("constraints/",)


def _tracked_files() -> list[str]:
    """Every file git tracks -- exactly the set that ships when the repo goes public."""
    listed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [name for name in listed.split("\0") if name]


def _in_scope(name: str) -> bool:
    return name not in EXCLUDED and not name.startswith(EXCLUDED_PREFIXES)


def _names_the_estate(line: str) -> bool:
    if INTERNAL.search(line):
        return True
    return any(host.lower() not in PUBLIC_HOSTS for host in _VENDOR_HOST.findall(line))


def _hits(name: str) -> list[str]:
    try:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError):
        # A binary blob carries no prose to leak, and a listed-but-absent path is
        # a submodule pointer rather than a file.
        return []
    return [
        f"{name}:{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if _names_the_estate(line)
    ]


def test_no_tracked_file_names_the_internal_estate() -> None:
    found: list[str] = []
    for name in _tracked_files():
        if _in_scope(name):
            found += _hits(name)
    assert not found, (
        "internal estate names found -- this repo goes public, so use a "
        "documentation name (example.com, example.internal) or an RFC 5737 "
        "address instead:\n" + "\n".join(found)
    )


def test_the_sweep_would_catch_a_leak() -> None:
    """The guard is worth nothing if the pattern never matches, so prove it does."""
    for sample in (
        "k8s-1.devex.hyperi.io",
        "on the DevEx fleet",
        "hyperi-io/hyperi-infra",
        "ref: secret/dfe",
        "secret/dfe/ghcr-pull-secret",
        "(secret/dfe)",
        "10.66.0.200",
        "Proxmox VE",
        "dragonfly",
        "ghostburner",
        "Registry: Harbor (`harbor.hyperi.io/dfe/dfe-engine`)",
        "https://grafana.ops.hyperi.io/d/abc",
        "ops@mail.hyperi.io",
        "--kubeconfig .tmp/kubeconfig-dfe-b",
        'apply_fetcher_credentials(KUBE, "dfe-b", ...)',
        "Target dedicated DFE worker nodes (dfe-k8s-1/2/3).",
        '{"node": "dfe-k8s-2"}',
        "| kind, Cluster B |",
        "DFE clean cluster (Cluster B) overlay",
        "ref: kv/services",
        "kv/services/harbor",
        'DEFAULT_VAULT_PATH = "kv/infrastructure/ssh"',
        "kv/dfe-test/aws",
    ):
        assert _names_the_estate(sample), sample


def test_a_kubernetes_secret_reference_is_not_a_vault_path() -> None:
    """kubectl names a product Secret `secret/dfe-<name>`; that is product text."""
    for sample in (
        "secret/dfe-cluster",
        "secret/dfe-fetcher-credentials configured",
    ):
        assert not _names_the_estate(sample), sample


def test_the_product_s_own_vendor_names_are_product_text() -> None:
    """The label prefix, the brand-token site and the apex mail addresses ship on purpose."""
    for sample in (
        "dfe.hyperi.io/managed: \"true\"",
        "selected by dfe.hyperi.io/profile.",
        "verbatim from https://graphics.hyperi.io/tokens/tokens.css.",
        "**Email**: sales@hyperi.io",
        "- **Security reports**: security@hyperi.io",
        'git config user.email "ci@hyperi.io"',
        "the idempotent cluster bootstrap",
        "the cluster baseline (detect-or-install)",
        "a dfe-backup volume",
    ):
        assert not _names_the_estate(sample), sample
