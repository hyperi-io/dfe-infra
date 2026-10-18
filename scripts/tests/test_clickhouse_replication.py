#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_clickhouse_replication.py
#  Purpose:      Prove every ClickHouse the chart deploys gives dfe-engine Keeper,
#                the replication macros, the cluster they name and SQL users in
#                Keeper, in single mode and cluster mode alike.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What dfe-engine's Replicated DDL needs from a self-hosted ClickHouse, per mode.

dfe-engine creates every table as an argumentless Replicated engine inside a
Replicated database, and refuses at startup a self-hosted server that lacks Keeper,
the {cluster}, {shard} and {replica} macros, or a remote_servers cluster the
{cluster} macro names (dfe-schemas PlatformProbe.require_replication). A cluster of
more than one server also needs its SQL users and roles in a replicated access
storage, as the first writable user_directories entry.

Single mode writes all of it into one config.d file, with Keeper embedded. Its
user_directories must REPLACE the image's list: appended, local_directory stays first
and takes every role the engine creates. Keeper creates its znodes under a generated identity, so
a pod that reaches 9181 cannot write a SQL user into them.

Cluster mode gets all of it from the operator, so the chart's extraConfig must not
carry any of those keys: a merged override shadows what the operator wrote. The
operator fixture below is tied to the operator pin in versions.yaml.

    python3 scripts/tests/test_clickhouse_replication.py

Needs `helm` on PATH.
"""

import functools
import re
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
VERSIONS = REPO_ROOT / "versions.yaml"
CHART = chart_dir("clickhouse-cluster")

SINGLE_PROFILES = ("slim", "single")
CLUSTER_PROFILES = ("scale", "mesh")
CLOUDS = ("local", "aws")

CONFIG_D = "/etc/clickhouse-server/config.d/"
REPLICATION_FILE = "dfe-replication.xml"
DATA_DIR = "/var/lib/clickhouse"
# The four-letter words that only read. The rest reset stats, move leadership or force recovery.
READ_ONLY_WORDS = {"conf", "cons", "dirs", "envi", "isro", "mntr", "ruok", "srvr", "stat", "wchs"}

# The keys clickhouse-operator v0.0.8 writes itself (templates/base.yaml.tmpl, cluster.yaml.tmpl).
OPERATOR_VERSION = "0.0.8"
OPERATOR_OWNED = ("keeper_server", "macros", "remote_servers", "user_directories", "zookeeper")


@functools.cache
def render(profile: str, cloud: str) -> tuple[dict, ...]:
    cmd = [
        "helm",
        "template",
        "clickhouse-cluster",
        str(CHART),
        "--namespace",
        "clickhouse",
        "-f",
        str(VALUES / "common.yaml"),
        "-f",
        str(VALUES / f"{cloud}.yaml"),
        "-f",
        str(VALUES / f"profile-{profile}.yaml"),
        "--set",
        "appNamespace=dfe",
    ]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {profile}/{cloud}:\n{out.stderr}")
    return tuple(d for d in yaml.safe_load_all(out.stdout) if d)


def one(docs: tuple[dict, ...], kind: str) -> dict:
    matches = [d for d in docs if d.get("kind") == kind]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one {kind}, got {len(matches)}")
    return matches[0]


def replication_config(docs: tuple[dict, ...]) -> ElementTree.Element | None:
    text = (one(docs, "ConfigMap").get("data") or {}).get(REPLICATION_FILE)
    return ElementTree.fromstring(text) if text else None


def text_at(root: ElementTree.Element, path: str) -> str:
    element = root.find(path)
    return (element.text or "").strip() if element is not None else ""


def server_container(docs: tuple[dict, ...]) -> dict:
    return one(docs, "StatefulSet")["spec"]["template"]["spec"]["containers"][0]


def single_cells() -> list[tuple[str, tuple[dict, ...]]]:
    return [(f"{p}/{c}", render(p, c)) for p in SINGLE_PROFILES for c in CLOUDS]


def test_single_mode_mounts_the_replication_config() -> None:
    for where, docs in single_cells():
        root = replication_config(docs)
        expect(f"{where}: the ConfigMap carries {REPLICATION_FILE}", root is not None)
        mounts = {m.get("mountPath"): m for m in server_container(docs).get("volumeMounts") or []}
        mount = mounts.get(CONFIG_D + REPLICATION_FILE) or {}
        expect(
            f"{where}: the server reads it from config.d",
            mount.get("subPath") == REPLICATION_FILE,
            f"mounts: {sorted(mounts)}",
        )


def test_single_mode_runs_keeper_embedded_beside_its_tables() -> None:
    for where, docs in single_cells():
        root = replication_config(docs)
        if root is None:
            continue
        port = text_at(root, "keeper_server/tcp_port")
        nodes = [(text_at(n, "host"), text_at(n, "port")) for n in root.findall("zookeeper/node")]
        expect(
            f"{where}: the server connects to its own Keeper",
            nodes == [("localhost", port)],
            f"got {nodes}",
        )
        raft = [text_at(s, "id") for s in root.findall("keeper_server/raft_configuration/server")]
        expect(
            f"{where}: Keeper is an ensemble of one",
            raft == [text_at(root, "keeper_server/server_id")],
            f"got {raft}",
        )
        mounts = {m.get("name"): m.get("mountPath") for m in server_container(docs).get("volumeMounts") or []}
        for path in ("log_storage_path", "snapshot_storage_path"):
            value = text_at(root, f"keeper_server/{path}")
            # Lose one without the other and every replicated table goes read-only.
            expect(
                f"{where}: Keeper's {path} is on the data PVC",
                mounts.get("data") == DATA_DIR and value.startswith(DATA_DIR + "/"),
                f"got {value!r}, data PVC at {mounts.get('data')!r}",
            )
        words = set(text_at(root, "keeper_server/four_letter_word_white_list").split(","))
        expect(
            f"{where}: only the read-only four-letter words answer on {port}",
            words <= READ_ONLY_WORDS,
            f"got {sorted(words)}",
        )


def test_single_mode_keeper_takes_a_generated_identity() -> None:
    for where, docs in single_cells():
        root = replication_config(docs)
        if root is None:
            continue
        identity = root.find("zookeeper/identity")
        env_name = identity.attrib.get("from_env", "") if identity is not None else ""
        container = server_container(docs)
        env = {e["name"]: e for e in container.get("env") or []}
        value = (env.get(env_name) or {}).get("value", "")
        match = re.fullmatch(r"\w+:\$\(([A-Za-z_][A-Za-z0-9_]*)\)", value)
        expect(f"{where}: the identity is user:$(VAR)", match is not None, f"{env_name}={value!r}")
        if match is None:
            continue
        names = [e["name"] for e in container.get("env") or []]
        expect(
            f"{where}: the kubelet expands it, so the password is declared first",
            match.group(1) in names and names.index(match.group(1)) < names.index(env_name),
            f"env order {names}",
        )
        ref = ((env.get(match.group(1)) or {}).get("valueFrom") or {}).get("secretKeyRef") or {}
        writers = [
            d
            for d in docs
            if d.get("kind") == "ExternalSecret" and d["spec"]["target"]["name"] == ref.get("name")
        ]
        generated = [
            e
            for w in writers
            for e in w["spec"].get("dataFrom") or []
            if (e.get("sourceRef") or {}).get("generatorRef")
        ]
        expect(
            f"{where}: the password comes from a generator-backed ExternalSecret in the render",
            ref.get("key") == "password" and len(writers) == 1 and bool(generated),
            f"ref {ref}, writers {len(writers)}",
        )


def test_single_mode_names_a_cluster_of_this_one_server() -> None:
    for where, docs in single_cells():
        root = replication_config(docs)
        if root is None:
            continue
        macros = {child.tag: (child.text or "").strip() for child in root.find("macros")}
        expect(
            f"{where}: the cluster, shard and replica macros",
            set(macros) == {"cluster", "shard", "replica"},
            f"got {macros}",
        )
        cluster = root.find(f"remote_servers/{macros.get('cluster', '')}")
        replicas = (
            [(text_at(r, "host"), text_at(r, "port")) for r in cluster.findall("shard/replica")]
            if cluster is not None
            else []
        )
        expect(
            f"{where}: remote_servers holds the cluster the macro names, with this server alone",
            replicas == [("localhost", "9000")],
            f"got {replicas}",
        )


def test_single_mode_keeps_sql_users_in_keeper() -> None:
    for where, docs in single_cells():
        root = replication_config(docs)
        if root is None:
            continue
        directories = root.find("user_directories")
        children = [child.tag for child in directories] if directories is not None else []
        expect(
            f"{where}: user_directories replaces the image's list",
            directories is not None and directories.attrib == {"replace": "replace"},
            f"got {None if directories is None else directories.attrib}",
        )
        expect(
            f"{where}: replicated is the first writable storage",
            children == ["users_xml", "replicated"],
            f"got {children}",
        )


def test_cluster_mode_leaves_replication_to_the_operator() -> None:
    for profile in CLUSTER_PROFILES:
        for cloud in CLOUDS:
            where = f"{profile}/{cloud}"
            docs = render(profile, cloud)
            spec = one(docs, "ClickHouseCluster")["spec"]
            extra = spec["settings"].get("extraConfig") or {}
            shadowed = sorted(set(extra) & set(OPERATOR_OWNED))
            expect(
                f"{where}: extraConfig shadows none of the operator's replication config",
                not shadowed,
                f"got {shadowed}",
            )
            keeper = one(docs, "KeeperCluster")["metadata"]["name"]
            expect(
                f"{where}: the cluster references the Keeper the chart deploys",
                (spec.get("keeperClusterRef") or {}).get("name") == keeper,
                f"ref {spec.get('keeperClusterRef')}, KeeperCluster {keeper}",
            )
            identities = [
                d["metadata"]["name"]
                for d in docs
                if d.get("kind") == "ExternalSecret" and "keeper-identity" in d["metadata"]["name"]
            ]
            expect(f"{where}: no single-mode Keeper identity is rendered", not identities, f"got {identities}")


def test_fixture_matches_the_pinned_operator() -> None:
    """A fixture recorded for one operator is not evidence about the next."""
    doc = yaml.safe_load(VERSIONS.read_text(encoding="utf-8"))
    pinned = doc["stacks"][doc["current"]]["operators"]["clickhouse-operator"]
    expect(
        "operator pin matches the version the replication fixture was read from",
        pinned == OPERATOR_VERSION,
        f"versions.yaml pins {pinned}; re-read base.yaml.tmpl and cluster.yaml.tmpl at that tag "
        "and update OPERATOR_OWNED and OPERATOR_VERSION",
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
