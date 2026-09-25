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
arrives from the downward API instead.

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
