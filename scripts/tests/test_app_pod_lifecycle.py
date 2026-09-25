#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_app_pod_lifecycle.py
#  Purpose:      Prove every data-plane app pod drains inside its grace period,
#                restarts on a rotated Secret, mounts no API token and still
#                knows its namespace, on the chart defaults and every profile.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Render assertions for how a data-plane app pod stops and restarts.

A stage releasing held source acks drains for up to scalo's 5 s pre-stop plus
its 20 s gRPC drain deadline. Killed before that, it leaves delivered records
unacknowledged and the source sends them again, so the grace period is asserted
on every app and every profile rather than trusted to a default.

Env taken from a secretKeyRef resolves once at pod start, so a rotated Secret
reaches a running pod only through the Reloader annotation.

No data-plane app calls the Kubernetes API, so none mounts a service-account
token. scalo falls back to that mount for the pod's namespace, so POD_NAMESPACE
arrives from the downward API instead. Its version check derives the instance
id from ca.crt and namespace in the same directory, so those two files are
projected there on their own, and a pod start does not read as a new install.

    python3 scripts/tests/test_app_pod_lifecycle.py

Needs `helm` on PATH. Runs under pytest too, which is how CI reaches it.
"""

import subprocess
import sys
from functools import cache
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"

# Every data-plane app, and the grace its pods get. dfe-transform-vector waits
# up to 55 s for its child process to exit, so it carries a figure of its own.
APPS = {
    "dfe-archiver": 45,
    "dfe-fetcher": 45,
    "dfe-loader": 45,
    "dfe-receiver": 45,
    "dfe-transform-elastic": 45,
    "dfe-transform-vector": 70,
    "dfe-transform-vrl": 45,
}

# None is the chart's own values with no deploy cascade at all.
PROFILES = (None, "slim", "single", "scale", "mesh")

RELOAD_ANNOTATION = "reloader.stakater.com/auto"

# Where scalo's version_check::k8s_instance_id() reads, and the files it reads.
SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
INSTANCE_ID_FILES = {"ca.crt", "namespace"}


def label(profile: str | None) -> str:
    return "chart defaults" if profile is None else f"profile {profile}"


def helm_template(chart: str, args: list[str]) -> tuple[dict, ...]:
    cmd = ["helm", "template", chart, str(chart_dir(chart)), "--namespace", "dfe", *args]
    out = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {chart} {args}:\n{out.stderr}")
    return tuple(d for d in yaml.safe_load_all(out.stdout) if d)


@cache
def render(chart: str, profile: str | None) -> tuple[dict, ...]:
    """One chart alone, or under one profile with the cascade Argo layers."""
    if profile is None:
        return helm_template(chart, [])
    return helm_template(
        chart,
        [
            "--set",
            "appNamespace=dfe",
            "-f",
            str(VALUES / "common.yaml"),
            "-f",
            str(VALUES / f"profile-{profile}.yaml"),
        ],
    )


def with_values(chart: str, *assignments: str) -> tuple[dict, ...]:
    """One chart alone with `--set` overrides, for the dials a test turns."""
    args: list[str] = []
    for assignment in assignments:
        args += ["--set", assignment]
    return helm_template(chart, args)


def deployment(docs: tuple[dict, ...]) -> dict:
    found = [d for d in docs if d.get("kind") == "Deployment"]
    if len(found) != 1:
        raise SystemExit(f"expected one Deployment, rendered {len(found)}")
    return found[0]


def pod_spec(docs: tuple[dict, ...]) -> dict:
    return deployment(docs)["spec"]["template"]["spec"]


def files_at(pod: dict, container: dict, directory: str) -> tuple[set[str], list[dict]]:
    """File names a container sees in a directory, and the projected sources behind them.

    Only projected volumes are read, which is the one kind the service-account
    path is ever mounted from. A token source shows up by its path, so a
    projection that adds one fails the absence check rather than slipping by.
    """
    names = {m["name"] for m in container.get("volumeMounts") or [] if m["mountPath"] == directory}
    files: set[str] = set()
    sources: list[dict] = []
    for volume in pod.get("volumes") or []:
        if volume["name"] not in names:
            continue
        for source in (volume.get("projected") or {}).get("sources") or []:
            sources.append(source)
            for kind in ("configMap", "downwardAPI", "secret"):
                files |= {item["path"] for item in (source.get(kind) or {}).get("items") or []}
            if "serviceAccountToken" in source:
                files.add(source["serviceAccountToken"].get("path", "token"))
    return files, sources


def test_every_app_pod_outlasts_its_drain() -> None:
    for chart, grace in APPS.items():
        for profile in PROFILES:
            got = pod_spec(render(chart, profile)).get("terminationGracePeriodSeconds")
            expect(
                f"{chart} on {label(profile)} waits {grace}s before SIGKILL",
                got == grace,
                f"got {got!r}",
            )


def test_a_chart_value_moves_the_grace_including_to_zero() -> None:
    """0 is a real setting, so the helper must not read it as unset."""
    for value in (0, 90):
        got = pod_spec(with_values("dfe-loader", f"terminationGracePeriodSeconds={value}")).get(
            "terminationGracePeriodSeconds"
        )
        expect(
            f"terminationGracePeriodSeconds={value} reaches the pod", got == value, f"got {got!r}"
        )


def test_a_rotated_secret_restarts_every_app() -> None:
    for chart in APPS:
        for profile in PROFILES:
            annotations = deployment(render(chart, profile))["metadata"].get("annotations") or {}
            expect(
                f"{chart} on {label(profile)} carries the Reloader annotation",
                annotations.get(RELOAD_ANNOTATION) == "true",
                f"got {annotations!r}",
            )


def test_reload_can_be_turned_off_without_a_null_annotations_map() -> None:
    metadata = deployment(with_values("dfe-loader", "reload.enabled=false"))["metadata"]
    expect(
        "reload.enabled=false renders no annotations key at all",
        "annotations" not in metadata,
        f"got {metadata.get('annotations')!r}",
    )


def test_no_app_pod_mounts_an_api_token() -> None:
    for chart in APPS:
        for profile in PROFILES:
            docs = render(chart, profile)
            accounts = [d for d in docs if d.get("kind") == "ServiceAccount"]
            expect(
                f"{chart} on {label(profile)} renders one ServiceAccount that mounts no token",
                len(accounts) == 1 and accounts[0].get("automountServiceAccountToken") is False,
                f"got {accounts!r}",
            )
            expect(
                f"{chart} on {label(profile)} pod mounts no token",
                pod_spec(docs).get("automountServiceAccountToken") is False,
                f"got {pod_spec(docs).get('automountServiceAccountToken')!r}",
            )


def test_a_supplied_service_account_still_mounts_no_token() -> None:
    """The pod-level setting is what covers an account the operator brings."""
    docs = with_values("dfe-loader", "serviceAccount.create=false")
    expect(
        "no ServiceAccount is rendered",
        not [d for d in docs if d.get("kind") == "ServiceAccount"],
        "serviceAccount.create=false still rendered one",
    )
    expect(
        "the pod still mounts no token",
        pod_spec(docs).get("automountServiceAccountToken") is False,
        f"got {pod_spec(docs).get('automountServiceAccountToken')!r}",
    )


def test_the_token_dial_turns_the_token_back_on_for_that_app() -> None:
    """Vault's Kubernetes auth logs in with the token, so one app can ask for it back.

    With the dial on, the kubelet's own automount supplies token, ca.crt and
    namespace at the service-account path, so the chart's projection there must
    step aside rather than collide with it.
    """
    docs = with_values("dfe-fetcher", "serviceAccount.mountToken=true")
    pod = pod_spec(docs)
    accounts = [d for d in docs if d.get("kind") == "ServiceAccount"]
    expect(
        "serviceAccount.mountToken=true mounts the token on the ServiceAccount",
        len(accounts) == 1 and accounts[0].get("automountServiceAccountToken") is True,
        f"got {accounts!r}",
    )
    expect(
        "and on the pod",
        pod.get("automountServiceAccountToken") is True,
        f"got {pod.get('automountServiceAccountToken')!r}",
    )
    for container in pod["containers"]:
        files, _ = files_at(pod, container, SERVICE_ACCOUNT_DIR)
        mounts = [
            m for m in container.get("volumeMounts") or [] if m["mountPath"] == SERVICE_ACCOUNT_DIR
        ]
        expect(
            f"{container['name']} leaves the service-account path to the automount",
            not files and not mounts,
            f"got files {sorted(files)}, mounts {mounts!r}",
        )
    other = pod_spec(render("dfe-loader", None))
    expect(
        "another app keeps its token off",
        other.get("automountServiceAccountToken") is False,
        f"got {other.get('automountServiceAccountToken')!r}",
    )


def test_the_instance_id_files_are_there_and_the_token_is_not() -> None:
    for chart in APPS:
        for profile in PROFILES:
            pod = pod_spec(render(chart, profile))
            for container in pod["containers"]:
                files, sources = files_at(pod, container, SERVICE_ACCOUNT_DIR)
                where = f"{chart}/{container['name']} on {label(profile)}"
                expect(
                    f"{where} sees ca.crt and namespace in the service-account directory",
                    INSTANCE_ID_FILES <= files,
                    f"got {sorted(files)}",
                )
                expect(
                    f"{where} sees no token there",
                    "token" not in files and not any("serviceAccountToken" in s for s in sources),
                    f"got {sorted(files)}",
                )
                config_maps = [s["configMap"] for s in sources if "configMap" in s]
                expect(
                    f"{where} takes ca.crt from the cluster's kube-root-ca.crt",
                    [(c["name"], c["items"][0]["key"]) for c in config_maps]
                    == [("kube-root-ca.crt", "ca.crt")],
                    f"got {config_maps!r}",
                )
                fields = [
                    item["fieldRef"]["fieldPath"]
                    for s in sources
                    for item in (s.get("downwardAPI") or {}).get("items") or []
                ]
                expect(
                    f"{where} takes namespace from the pod's own metadata",
                    fields == ["metadata.namespace"],
                    f"got {fields!r}",
                )


def test_the_file_reader_catches_a_token() -> None:
    """The absence check is only as good as this read, so a token must show up."""
    pod = {
        "volumes": [
            {
                "name": "sa",
                "projected": {
                    "sources": [
                        {"serviceAccountToken": {"path": "token", "expirationSeconds": 3607}}
                    ]
                },
            }
        ]
    }
    container = {"volumeMounts": [{"name": "sa", "mountPath": SERVICE_ACCOUNT_DIR}]}
    files, _ = files_at(pod, container, SERVICE_ACCOUNT_DIR)
    expect("a projected token is read as a token file", "token" in files, f"got {sorted(files)}")


def test_every_app_container_knows_its_namespace() -> None:
    for chart in APPS:
        for profile in PROFILES:
            for container in pod_spec(render(chart, profile))["containers"]:
                entries = [e for e in container.get("env") or [] if e["name"] == "POD_NAMESPACE"]
                paths = [
                    e.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath") for e in entries
                ]
                expect(
                    f"{chart}/{container['name']} on {label(profile)} reads POD_NAMESPACE "
                    "from the downward API, once",
                    paths == ["metadata.namespace"],
                    f"got {entries!r}",
                )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
