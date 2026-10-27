#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_private_key_secret_mounts.py
#  Purpose:      Fail when a Secret carrying a private key is mounted outside
#                the namespace that owns it, or readable by more than its group.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Private-key Secrets stay in their namespace and are mounted 0440 or tighter.

A Secret carrying tls.key (or any `*.key`, or a `*-keypair`) is the one object
whose exposure lets someone impersonate a service. The rule has two halves, read
off the renders rather than off the intent:

- a pod mounts it only from the namespace that owns it (the namespace of the Secret,
  or of the cert-manager Certificate that writes it)
- every mount of it, whole-volume or per item, is 0440 or narrower, and a mount that
  names no mode counts as 0644, the Kubernetes default

The owners come from every first-party chart rendered with the default cascade for
each tier, and the pods from those same renders plus every thin-chart render, so a
client chart that mounted the ClickHouse keypair from the app namespace fails here.

    python3 -m pytest scripts/tests/test_private_key_secret_mounts.py -q

Needs `helm` on PATH.
"""

from __future__ import annotations

import functools
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from _charts import CHART_TREES, REPO_ROOT  # noqa: E402
from _gate import CLOUDS, COMPONENTS, golden, object_id  # noqa: E402

TIERS = ("slim", "single", "scale")
VALUES = REPO_ROOT / "argocd" / "values"
APPSET = REPO_ROOT / "argocd" / "appsets" / "layer2-data.yaml"

# The namespace the thin charts deploy into.
APP_NS = "dfe"

# The widest mode a private-key mount may carry: read for the owner and the group.
WIDEST = 0o440
# What the kubelet applies to a Secret volume that names no mode.
KUBERNETES_DEFAULT = 0o644

# A Secret data key that holds a private key.
KEY_FILE = re.compile(r"(^tls\.key$|\.key$|private[-_.]?key)", re.IGNORECASE)
# A Secret named for the keypair it holds, wherever its object is rendered.
KEYPAIR_NAME = re.compile(r"-keypair$")

# Namespaces a first-party chart is deployed into by layer2-data.yaml, by chart name.
def _data_namespaces() -> dict[str, str]:
    appset = yaml.safe_load(APPSET.read_text(encoding="utf-8", errors="replace"))
    found: dict[str, str] = {}
    for generator in appset["spec"]["generators"]:
        for part in (generator.get("matrix") or {}).get("generators") or []:
            for element in (part.get("list") or {}).get("elements") or []:
                if "app" in element and "namespace" in element:
                    found[element["app"]] = element["namespace"]
    return found


def _charts() -> list[Path]:
    return [chart.parent for tree in CHART_TREES for chart in sorted(tree.glob("*/Chart.yaml"))]


@functools.cache
def chart_renders(tier: str) -> tuple[tuple[str, tuple[dict, ...]], ...]:
    """(namespace, objects) for each first-party chart rendered with the tier's cascade."""
    namespaces = _data_namespaces()
    renders = []
    for directory in _charts():
        name = directory.name
        cmd = [
            "helm", "template", name, str(directory), "--namespace", namespaces.get(name, APP_NS),
            "-f", str(VALUES / "common.yaml"), "-f", str(VALUES / f"profile-{tier}.yaml"),
            "--set", f"appNamespace={APP_NS}",
        ]
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        assert done.returncode == 0, f"helm template {name} ({tier}): {done.stderr}"
        docs = tuple(d for d in yaml.safe_load_all(done.stdout) if d)
        renders.append((namespaces.get(name, ""), docs))
    return tuple(renders)


@functools.cache
def thin_renders(tier: str) -> tuple[tuple[str, tuple[dict, ...]], ...]:
    return tuple((APP_NS, tuple(golden(s, tier, c))) for s in COMPONENTS for c in CLOUDS)


def _namespace(doc: dict, default: str) -> str:
    return str((doc.get("metadata") or {}).get("namespace") or default)


def key_secrets(renders) -> dict[str, set[str]]:
    """Secret name -> the namespaces that own it, for every Secret known to carry a private key."""
    owners: dict[str, set[str]] = {}
    for default, docs in renders:
        for doc in docs:
            kind, ns = doc.get("kind"), _namespace(doc, default)
            if kind == "Secret":
                keys = set(doc.get("data") or {}) | set(doc.get("stringData") or {})
                if any(KEY_FILE.search(key) for key in keys) or KEYPAIR_NAME.search(doc["metadata"]["name"]):
                    owners.setdefault(doc["metadata"]["name"], set()).add(ns)
            elif kind == "Certificate":
                secret = (doc.get("spec") or {}).get("secretName")
                if secret:
                    owners.setdefault(str(secret), set()).add(ns)
    return owners


def _volumes(node: object) -> Iterator[dict]:
    """Every volume in a document, wherever its pod spec sits (a CRD's template included)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "volumes" and isinstance(value, list):
                yield from (v for v in value if isinstance(v, dict))
            yield from _volumes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _volumes(value)


def _mounts(volume: dict) -> Iterator[tuple[str, object, list[dict]]]:
    """(secret name, defaultMode, items) for each Secret a volume mounts."""
    secret = volume.get("secret")
    if isinstance(secret, dict) and secret.get("secretName"):
        yield str(secret["secretName"]), secret.get("defaultMode"), secret.get("items") or []
    projected = volume.get("projected")
    if isinstance(projected, dict):
        for source in projected.get("sources") or []:
            part = source.get("secret") if isinstance(source, dict) else None
            if isinstance(part, dict) and part.get("name"):
                yield str(part["name"]), projected.get("defaultMode"), part.get("items") or []


def _mode(value: object) -> int:
    if value is None:
        return KUBERNETES_DEFAULT
    return int(value, 8) if isinstance(value, str) else int(value)


def violations(renders) -> list[str]:
    """Each mount of a private-key Secret from the wrong namespace or wider than 0440."""
    owners = key_secrets(renders)
    found = []
    for default, docs in renders:
        for doc in docs:
            ns = _namespace(doc, default)
            for volume in _volumes(doc):
                for secret, default_mode, items in _mounts(volume):
                    if secret not in owners and not KEYPAIR_NAME.search(secret):
                        continue
                    where = f"{object_id(doc)} in {ns or 'its namespace'} mounts {secret}"
                    if owners.get(secret) and ns and ns not in owners[secret]:
                        found.append(f"{where}, owned by {sorted(owners[secret])}")
                    modes = [_mode(default_mode)] if not items else [
                        _mode(item.get("mode", default_mode)) for item in items
                    ]
                    wide = [oct(m) for m in modes if m & ~WIDEST]
                    if wide:
                        found.append(f"{where} with mode {', '.join(wide)}, wider than {oct(WIDEST)}")
    return found


@pytest.mark.parametrize("tier", TIERS)
def test_private_key_secrets_stay_in_their_namespace_and_are_mounted_0440(tier: str) -> None:
    renders = chart_renders(tier) + thin_renders(tier)
    owners = key_secrets(renders)
    assert owners, "no private-key Secret was found, so the scan proved nothing"
    assert violations(renders) == []


# --- the scan itself, on shapes the charts do not currently produce ---------------------------


def _pod(ns: str, secret: str, **volume: object) -> dict:
    return {
        "kind": "Deployment", "metadata": {"name": "app", "namespace": ns},
        "spec": {"template": {"spec": {"volumes": [{"name": "k", "secret": {"secretName": secret, **volume}}]}}},
    }


def _owner(ns: str = "clickhouse") -> dict:
    return {"kind": "Certificate", "metadata": {"name": "c", "namespace": ns}, "spec": {"secretName": "tls"}}


@pytest.mark.parametrize(
    ("pod", "needle"),
    [
        (_pod("dfe", "tls", defaultMode=0o440), "owned by ['clickhouse']"),
        (_pod("clickhouse", "tls", defaultMode=0o644), "wider than 0o440"),
        (_pod("clickhouse", "tls"), "0o644"),
        (_pod("clickhouse", "tls", defaultMode=0o444), "0o444"),
        (_pod("clickhouse", "tls", defaultMode=0o400, items=[{"key": "tls.key", "path": "k", "mode": 0o666}]), "0o666"),
        (
            {"kind": "Pod", "metadata": {"name": "p", "namespace": "clickhouse"}, "spec": {"volumes": [
                {"name": "k", "projected": {"sources": [{"secret": {"name": "tls"}}]}}]}},
            "wider than",
        ),
        (
            {"kind": "ClickHouseInstallation", "metadata": {"name": "ch", "namespace": "clickhouse"}, "spec": {
                "templates": {"podTemplates": [{"spec": {"volumes": [{"secret": {"secretName": "tls"}}]}}]}}},
            "wider than",
        ),
    ],
)
def test_expected_fail_each_way_of_exposing_a_key_is_found(pod: dict, needle: str) -> None:
    assert any(needle in line for line in violations([("", (_owner(), pod))]))


@pytest.mark.parametrize(
    "pod",
    [
        _pod("clickhouse", "tls", defaultMode=0o440),
        _pod("clickhouse", "tls", defaultMode=0o400),
        _pod("clickhouse", "tls", defaultMode=0o440, items=[{"key": "tls.key", "path": "k", "mode": 0o400}]),
        _pod("dfe", "dfe-clickhouse-tls-ca", defaultMode=0o644),
    ],
)
def test_a_narrow_mount_in_the_owning_namespace_is_clean(pod: dict) -> None:
    assert violations([("", (_owner(), pod))]) == []


def test_a_secret_with_a_key_file_is_a_key_secret_without_a_certificate() -> None:
    secret = {"kind": "Secret", "metadata": {"name": "signer", "namespace": "a"}, "data": {"tls.key": "eA=="}}
    assert key_secrets([("", (secret,))]) == {"signer": {"a"}}
    assert violations([("", (secret, _pod("b", "signer", defaultMode=0o440)))])
