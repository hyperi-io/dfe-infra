#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_clickhouse_admin_password.py
#  Purpose:      Prove the ClickHouse server and the apps read one admin password,
#                and that ESO cannot mint a second one under a running server.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The in-cluster ClickHouse admin password, from the generator to its one reader.

ClickHouse reads the password once, when the server starts. The engine reads a
copy that is refreshed hourly, at its own start. A second value minted into the
source Secret after one side has read the first leaves the two on different
passwords until ClickHouse restarts, and every connection fails with code 194.
ESO's CreatedOnce does not prevent that -- it re-runs the generator on any
reconcile that finds no recorded sync -- so the target Secret has to be immutable.

Per tier and cloud, what has to hold:

1. One ExternalSecret writes the Secret, from a Password generator, CreatedOnce
   and immutable.
2. The server reads that Secret and key, in cluster and single mode alike.
3. The app-namespace copy reads that Secret and key from the server's namespace.
4. The engine reads the copy, and the loader and HyperDX do not: each app
   connects as its own user (test_clickhouse_app_identity.py).

    python3 scripts/tests/test_clickhouse_admin_password.py

Needs `helm` on PATH. Runs under pytest too, which is how CI reaches it.
"""

import functools
import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary
from _gate import CLOUDS, PROFILES, effective_env, golden

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
DATA_NS = "clickhouse"
APP_NS = "dfe"

# Each app's deploy.service, its workload, the env var carrying a ClickHouse
# password, and whether that is the admin copy.
CLIENTS = (
    ("dfe-engine", "Deployment/dfe-engine", "DFE_CLICKHOUSE_PASSWORD", True),
    ("dfe-loader", "Deployment/dfe-loader", "DFE_LOADER__CLICKHOUSE__PASSWORD", False),
    ("hyperdx", "Deployment/dfe-hyperdx", "CLICKHOUSE_PASSWORD", False),
)


@functools.cache
def server(profile: str, cloud: str) -> tuple[dict, ...]:
    """The clickhouse-cluster chart as layer2-data renders it for one cell."""
    cmd = [
        "helm",
        "template",
        "clickhouse-cluster",
        str(chart_dir("clickhouse-cluster")),
        "--namespace",
        DATA_NS,
        "-f",
        str(VALUES / "common.yaml"),
        "-f",
        str(VALUES / f"{cloud}.yaml"),
        "-f",
        str(VALUES / f"profile-{profile}.yaml"),
        "--set",
        f"appNamespace={APP_NS}",
    ]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {profile}/{cloud}:\n{out.stderr}")
    return tuple(d for d in yaml.safe_load_all(out.stdout) if d)


def of_kind(docs: tuple[dict, ...], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def secret_ref(entry: object) -> tuple[str | None, str | None]:
    """(secret, key) of one env entry; (None, None) for a literal or nothing."""
    ref = ((entry if isinstance(entry, dict) else {}).get("valueFrom") or {}).get("secretKeyRef")
    return (ref.get("name"), ref.get("key")) if ref else (None, None)


def server_reads(docs: tuple[dict, ...]) -> tuple[str | None, str | None]:
    """(secret, key) the ClickHouse server takes its admin password from."""
    for cluster in of_kind(docs, "ClickHouseCluster"):
        ref = cluster["spec"]["settings"]["defaultUserPassword"]["secret"]
        return ref["name"], ref["key"]
    for sts in of_kind(docs, "StatefulSet"):
        for container in sts["spec"]["template"]["spec"]["containers"]:
            env = {e["name"]: e for e in container.get("env") or []}
            if "CLICKHOUSE_PASSWORD" in env:
                return secret_ref(env["CLICKHOUSE_PASSWORD"])
    return None, None


def writers(docs: tuple[dict, ...], secret: str | None) -> list[dict]:
    """Every ExternalSecret in the server's namespace that writes `secret`."""
    return [d for d in of_kind(docs, "ExternalSecret") if d["spec"]["target"]["name"] == secret]


def generator_of(es: dict) -> str | None:
    for entry in es["spec"].get("dataFrom") or []:
        ref = (entry.get("sourceRef") or {}).get("generatorRef") or {}
        if ref.get("kind") == "Password":
            return ref["name"]
    return None


def app_copy(docs: tuple[dict, ...]) -> dict | None:
    """The ClusterExternalSecret that projects the password into the app namespace."""
    for ces in of_kind(docs, "ClusterExternalSecret"):
        names = [
            s["matchLabels"]["kubernetes.io/metadata.name"]
            for s in ces["spec"]["namespaceSelectors"]
        ]
        data = ces["spec"]["externalSecretSpec"].get("data") or []
        if APP_NS in names and [d["secretKey"] for d in data] == ["password"]:
            return ces
    return None


def store_namespace(docs: tuple[dict, ...], name: str) -> str | None:
    for store in of_kind(docs, "ClusterSecretStore"):
        if store["metadata"]["name"] == name:
            return store["spec"]["provider"]["kubernetes"]["remoteNamespace"]
    return None


def test_one_generated_password_that_cannot_be_reminted() -> None:
    for profile in PROFILES:
        for cloud in CLOUDS:
            where = f"{profile}/{cloud}"
            docs = server(profile, cloud)
            secret, key = server_reads(docs)
            expect(
                f"{where}: the server reads its admin password from a Secret",
                secret is not None and key == "password",
                f"got {(secret, key)}",
            )
            found = writers(docs, secret)
            expect(
                f"{where}: one ExternalSecret writes {secret}", len(found) == 1, f"got {len(found)}"
            )
            if len(found) != 1:
                continue
            spec = found[0]["spec"]
            gen = generator_of(found[0])
            expect(
                f"{where}: {secret} comes from a Password generator rendered beside it",
                gen is not None
                and gen in {d["metadata"]["name"] for d in of_kind(docs, "Password")},
                f"got {gen}",
            )
            expect(
                f"{where}: {secret} is written once",
                spec.get("refreshPolicy") == "CreatedOnce",
                f"got {spec.get('refreshPolicy')}",
            )
            expect(
                f"{where}: {secret} is immutable, so a re-run generator cannot replace it",
                spec["target"].get("immutable") is True,
                f"got {spec['target']}",
            )


def test_the_apps_read_the_password_the_server_reads() -> None:
    for profile in PROFILES:
        for cloud in CLOUDS:
            where = f"{profile}/{cloud}"
            docs = server(profile, cloud)
            secret, key = server_reads(docs)
            ces = app_copy(docs)
            expect(f"{where}: the password is copied into {APP_NS}", ces is not None, "no copy")
            if ces is None:
                continue
            spec = ces["spec"]["externalSecretSpec"]
            remote = spec["data"][0]["remoteRef"]
            expect(
                f"{where}: the copy reads the server's Secret and key",
                (remote["key"], remote["property"]) == (secret, key),
                f"got {remote}",
            )
            source = store_namespace(docs, spec["secretStoreRef"]["name"])
            expect(
                f"{where}: the copy reads it from the server's namespace",
                source == DATA_NS,
                f"got {source}",
            )
            copied = (spec["target"]["name"], spec["data"][0]["secretKey"])
            for service, workload, var, admin in CLIENTS:
                env = effective_env(golden(service, profile, cloud), workload)
                ref = secret_ref(env.get(var))
                expect(
                    f"{where}: {service} {'reads' if admin else 'does not read'} {var} from the copy",
                    (ref == copied) is admin,
                    f"got {ref}, copy {copied}",
                )


def main() -> int:
    with standalone():
        test_one_generated_password_that_cannot_be_reminted()
        test_the_apps_read_the_password_the_server_reads()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
