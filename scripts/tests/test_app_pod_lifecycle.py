#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_app_pod_lifecycle.py
#  Purpose:      Prove every data-plane app pod drains inside its grace period,
#                restarts on a rotated Secret, mounts no API token and still
#                knows its namespace, on every profile and cloud.
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
dfe-engine is the control plane, not a data-plane app, and keeps its token. The
hunt runner and keda shim dfe-extras renders beside it run as accounts of their
own, which mount none.

The defaults are read from the goldens under fixtures/thin-renders, which
test_thin_renders.py holds equal to what Argo deploys. The dials render live
through the scalo-service library (_weave.library).

    python3 scripts/tests/test_app_pod_lifecycle.py

Needs `helm` on PATH. Runs under pytest too, which is how CI reaches it.
"""

import sys

from _expect import expect, standalone, summary
from _gate import goldens
from _weave import render_app

# Every data-plane app, and the grace its pods get. dfe-transform-vector waits
# up to 55 s for its child process to exit and then drains its bridge, so it
# carries a figure of its own.
APPS = {
    "dfe-archiver": 45,
    "dfe-fetcher": 45,
    "dfe-loader": 45,
    "dfe-receiver": 45,
    "dfe-transform-elastic": 45,
    "dfe-transform-vector": 90,
    "dfe-transform-vrl": 45,
}

# Where the dial cases render.
DIAL_CELL = ("slim", "local")

RELOAD_ANNOTATION = "reloader.stakater.com/auto"

# Where scalo's version_check::k8s_instance_id() reads, and the files it reads.
SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
INSTANCE_ID_FILES = {"ca.crt", "namespace"}

# The control plane, which the data-plane token rule must not reach.
CONTROL_PLANE = "dfe-engine"


def label(where: tuple[str, ...]) -> str:
    return "/".join(where[1:])


def deployment(docs: list[dict], name: str) -> dict:
    found = [d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == name]
    if len(found) != 1:
        raise SystemExit(f"expected one Deployment {name}, rendered {len(found)}")
    return found[0]


def pod_spec(docs: list[dict], name: str) -> dict:
    return deployment(docs, name)["spec"]["template"]["spec"]


def dial(app: str, overlay: dict) -> list[dict]:
    """One app rendered live with a deployer's instance values."""
    profile, cloud = DIAL_CELL
    return render_app(app, profile, cloud, overlay=overlay)


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
    for app, grace in APPS.items():
        for where, docs in goldens(app):
            got = pod_spec(docs, app).get("terminationGracePeriodSeconds")
            expect(
                f"{app} on {label(where)} waits {grace}s before SIGKILL",
                got == grace,
                f"got {got!r}",
            )


def test_a_chart_value_moves_the_grace_including_to_zero() -> None:
    """0 is a real setting, so the library must not read it as unset."""
    for value in (0, 90):
        docs = dial("dfe-loader", {"terminationGracePeriodSeconds": value})
        got = pod_spec(docs, "dfe-loader").get("terminationGracePeriodSeconds")
        expect(
            f"terminationGracePeriodSeconds={value} reaches the pod", got == value, f"got {got!r}"
        )


def test_a_rotated_secret_restarts_every_app() -> None:
    for app in APPS:
        for where, docs in goldens(app):
            annotations = deployment(docs, app)["metadata"].get("annotations") or {}
            expect(
                f"{app} on {label(where)} carries the Reloader annotation",
                annotations.get(RELOAD_ANNOTATION) == "true",
                f"got {annotations!r}",
            )


def test_reload_can_be_turned_off_without_a_null_annotations_map() -> None:
    metadata = deployment(dial("dfe-loader", {"reload": {"enabled": False}}), "dfe-loader")
    expect(
        "reload.enabled=false renders no annotations key at all",
        "annotations" not in metadata["metadata"],
        f"got {metadata['metadata'].get('annotations')!r}",
    )


def test_no_app_pod_mounts_an_api_token() -> None:
    for app in APPS:
        for where, docs in goldens(app):
            accounts = [d for d in docs if d.get("kind") == "ServiceAccount"]
            expect(
                f"{app} on {label(where)} renders one ServiceAccount that mounts no token",
                len(accounts) == 1 and accounts[0].get("automountServiceAccountToken") is False,
                f"got {accounts!r}",
            )
            pod = pod_spec(docs, app)
            expect(
                f"{app} on {label(where)} pod mounts no token",
                pod.get("automountServiceAccountToken") is False,
                f"got {pod.get('automountServiceAccountToken')!r}",
            )


def test_a_supplied_service_account_still_mounts_no_token() -> None:
    """The pod-level setting is what covers an account the operator brings."""
    docs = dial("dfe-loader", {"serviceAccount": {"create": False}})
    expect(
        "no ServiceAccount is rendered",
        not [d for d in docs if d.get("kind") == "ServiceAccount"],
        "serviceAccount.create=false still rendered one",
    )
    pod = pod_spec(docs, "dfe-loader")
    expect(
        "the pod still mounts no token",
        pod.get("automountServiceAccountToken") is False,
        f"got {pod.get('automountServiceAccountToken')!r}",
    )


def test_the_token_dial_turns_the_token_back_on_for_that_app() -> None:
    """Vault's Kubernetes auth logs in with the token, so one app can ask for it back.

    With the dial on, the kubelet's own automount supplies token, ca.crt and
    namespace at the service-account path, so the projection there must step
    aside rather than collide with it.
    """
    docs = dial("dfe-fetcher", {"serviceAccount": {"mountToken": True}})
    pod = pod_spec(docs, "dfe-fetcher")
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
    other = pod_spec(dial("dfe-loader", {}), "dfe-loader")
    expect(
        "another app keeps its token off",
        other.get("automountServiceAccountToken") is False,
        f"got {other.get('automountServiceAccountToken')!r}",
    )


def test_the_engine_keeps_its_token() -> None:
    """The engine mounts the kubelet's token, and each sidecar runs as an account of its own that mounts none.

    So only the engine's pod holds the engine's grants. Nothing else sits at the token's path.
    """
    for where, docs in goldens(CONTROL_PLANE):
        accounts = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "ServiceAccount"}
        engine_account = accounts.get(CONTROL_PLANE, {})
        expect(
            f"{CONTROL_PLANE} on {label(where)} renders its own ServiceAccount, which mounts its token",
            bool(engine_account) and engine_account.get("automountServiceAccountToken", True) is True,
            f"got {sorted(accounts)}",
        )
        workloads = [d for d in docs if d.get("kind") == "Deployment"]
        names = sorted(w["metadata"]["name"] for w in workloads)
        expect(
            f"{CONTROL_PLANE} on {label(where)} renders the engine, hunt runner and keda shim",
            names == ["dfe-engine", "dfe-hunt-runner", "dfe-keda-shim"],
            f"got {names}",
        )
        for workload in workloads:
            pod = workload["spec"]["template"]["spec"]
            name = workload["metadata"]["name"]
            at = f"{name} on {label(where)}"
            account = pod.get("serviceAccountName", "default")
            # The pod's own setting wins; unset, the account's decides, and Kubernetes defaults it on.
            mounted = pod.get("automountServiceAccountToken")
            if mounted is None:
                mounted = accounts.get(account, {}).get("automountServiceAccountToken", True)
            on_engine_account = account == CONTROL_PLANE
            expect(
                f"{at} runs as its own account",
                account == name,
                f"got {account!r}",
            )
            expect(
                f"{at} pod {'mounts its' if on_engine_account else 'mounts no'} token",
                mounted is on_engine_account,
                f"account {account!r}, got {mounted!r}",
            )
            for container in pod["containers"]:
                files, _ = files_at(pod, container, SERVICE_ACCOUNT_DIR)
                expect(
                    f"{at}/{container['name']} leaves the service-account path to the automount",
                    not files,
                    f"got {sorted(files)}",
                )


def test_the_instance_id_files_are_there_and_the_token_is_not() -> None:
    for app in APPS:
        for where, docs in goldens(app):
            pod = pod_spec(docs, app)
            for container in pod["containers"]:
                files, sources = files_at(pod, container, SERVICE_ACCOUNT_DIR)
                at = f"{app}/{container['name']} on {label(where)}"
                expect(
                    f"{at} sees ca.crt and namespace in the service-account directory",
                    INSTANCE_ID_FILES <= files,
                    f"got {sorted(files)}",
                )
                expect(
                    f"{at} sees no token there",
                    "token" not in files and not any("serviceAccountToken" in s for s in sources),
                    f"got {sorted(files)}",
                )
                config_maps = [s["configMap"] for s in sources if "configMap" in s]
                expect(
                    f"{at} takes ca.crt from the cluster's kube-root-ca.crt",
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
                    f"{at} takes namespace from the pod's own metadata",
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
    for app in APPS:
        for where, docs in goldens(app):
            for container in pod_spec(docs, app)["containers"]:
                entries = [e for e in container.get("env") or [] if e["name"] == "POD_NAMESPACE"]
                paths = [
                    e.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath") for e in entries
                ]
                expect(
                    f"{app}/{container['name']} on {label(where)} reads POD_NAMESPACE "
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
