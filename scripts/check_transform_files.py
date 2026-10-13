#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/check_transform_files.py
#  Purpose:      Check that each instance of an app consuming file sets mounts
#                the files the engine lists for it. Stdlib only.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""check_transform_files -- the engine's files against what each instance mounts.

    python3 scripts/check_transform_files.py --namespace dfe [--kubeconfig <file>]

apps.yaml names each file set an app consumes: where the engine keeps its files
in an instance's values file (values_path) and where the chart mounts them
(mount_path). The engine's files API lists the set from that values file in its
own deploy-repo working tree, at DFE_GITOPS_LOCAL_PATH in its pod, which this
reads the same file from. Each instance's pod is then asked what its mount
holds. A chart that reads the set from another key mounts nothing while the
engine still lists every file, which reads healthy everywhere else. A set the instance runs its programs from
(apps.yaml `dir_setting`) that the engine lists empty fails as well: once the
engine reads the same missing key the chart does, both sides are empty, and a
transform with no program crash-loops or passes records through untransformed.

An instance Deployment is named <service>-<instance>. One named the bare
service fails too: the per-config identity was lost and the instance renders
under the chart's name.

Exit 0 every set matches (or no instance consumes one), 1 a set differs, 2 the
cluster or the engine could not be read.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import values_migration

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "apps.yaml"
ENGINE_TARGET = "deploy/dfe-engine"
EXIT_OK, EXIT_DIFFERS, EXIT_UNREAD = 0, 1, 2


def _kubectl(kubeconfig: str | None, *args: str) -> subprocess.CompletedProcess:
    cmd = ["kubectl", *(["--kubeconfig", kubeconfig] if kubeconfig else []), "--request-timeout=20s", *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def file_sets(manifest: str) -> dict[str, list[dict[str, str]]]:
    """Each app in apps.yaml that consumes a file set, and its sets' name, values_path and mount_path."""
    found: dict[str, list[dict[str, str]]] = {}
    for app in values_migration.child_keys(manifest, "apps"):
        sets = values_migration.list_items(manifest, f"apps.{app}.files") or []
        sets = [s for s in sets if s.get("name") and s.get("values_path") and s.get("mount_path")]
        if sets:
            found[app] = sets
    return found


def instances(deployments: list[str], apps: list[str]) -> tuple[list[tuple[str, str, str]], list[str]]:
    """((app, instance, Deployment) for each instance, each Deployment named the bare app)."""
    found: list[tuple[str, str, str]] = []
    bare: list[str] = []
    for name in deployments:
        if name in apps:
            bare.append(name)
            continue
        app = next((a for a in sorted(apps, key=len, reverse=True) if name.startswith(f"{a}-")), None)
        if app is not None:
            found.append((app, name[len(app) + 1 :], name))
    return found, bare


def compare(app: str, instance: str, overlay: str, mounted: dict[str, list[str] | None], sets: list[dict[str, str]]) -> list[str]:
    """One line per set: [PASS] where the pod mounts what the engine lists, else [FAIL] with the difference."""
    lines = []
    for file_set in sets:
        listed = values_migration.list_items(overlay, file_set["values_path"]) or []
        wanted = sorted(item["name"] for item in listed if item.get("name"))
        seen = mounted.get(file_set["name"])
        where = f"{app}/{instance} {file_set['name']}"
        if file_set.get("dir_setting") and not wanted:
            lines.append(
                f"[FAIL] {where}: the engine lists no file in the set the instance runs its programs from, so it "
                f"has none to run -- an overlay whose files sit under a key other than {file_set['values_path']}"
            )
        elif seen is None:
            lines.append(f"[FAIL] {where}: the pod's mount could not be listed, and the engine lists {wanted}")
        elif sorted(seen) != wanted:
            lines.append(f"[FAIL] {where}: the engine lists {wanted}, the pod mounts {sorted(seen)}")
        else:
            lines.append(f"[PASS] {where}: {len(wanted)} file(s), as the engine lists them")
    return lines


def mount_path(overlay: str, file_set: dict[str, str]) -> str:
    """Where the chart mounts a set: the overlay's fileSets.<set>.mountPath, else apps.yaml's."""
    parent = file_set["values_path"].rsplit(".", 1)[0]
    return values_migration.value_of(overlay, f"{parent}.mountPath") or file_set["mount_path"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--namespace", required=True, help="the DFE app namespace")
    parser.add_argument("--kubeconfig", default=None, help="kubeconfig for the cluster")
    parser.add_argument("--engine", default=ENGINE_TARGET, help="the engine's workload, for kubectl exec")
    args = parser.parse_args(argv)

    sets_by_app = file_sets(MANIFEST.read_text(encoding="utf-8"))
    listed = _kubectl(args.kubeconfig, "-n", args.namespace, "get", "deployments", "-o", "json")
    if listed.returncode != 0:
        print(f"[UNREAD] cannot list Deployments: {listed.stderr.strip()}")
        return EXIT_UNREAD
    names = [item["metadata"]["name"] for item in json.loads(listed.stdout or "{}").get("items") or []]
    found, bare = instances(names, sorted(sets_by_app))
    lines = [f"[FAIL] {name}: an instance renders under the bare chart name, so its identity was lost" for name in bare]
    for app, instance, deployment in found:
        read = _kubectl(
            args.kubeconfig, "-n", args.namespace, "exec", args.engine, "--",
            "sh", "-c", 'cat "$DFE_GITOPS_LOCAL_PATH/values/$1"', "sh", f"{app}-{instance}-values.yaml",
        )
        if read.returncode != 0:
            print(f"[UNREAD] the engine's values file for {app}/{instance}: {read.stderr.strip()}")
            return EXIT_UNREAD
        mounted: dict[str, list[str] | None] = {}
        for file_set in sets_by_app[app]:
            ls = _kubectl(
                args.kubeconfig, "-n", args.namespace, "exec", f"deploy/{deployment}", "--",
                "ls", "-1", mount_path(read.stdout, file_set),
            )
            mounted[file_set["name"]] = ls.stdout.split() if ls.returncode == 0 else None
        lines += compare(app, instance, read.stdout, mounted, sets_by_app[app])
    for line in lines or ["[PASS] no instance of an app consuming a file set runs here"]:
        print(line)
    return EXIT_DIFFERS if any(line.startswith("[FAIL]") for line in lines) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
