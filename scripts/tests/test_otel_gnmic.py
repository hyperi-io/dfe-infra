#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_otel_gnmic.py
#  Purpose:      Prove the gnmic workload renders only when enabled, pushes to the
#                gateway's OTLP port and refuses to render with no device.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The otel-collector chart's gnmic workload.

    python3 scripts/tests/test_otel_gnmic.py

Needs `helm` on PATH. No test runner, matching the other checks here.
"""

import subprocess
import sys
from pathlib import Path

import yaml

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHART = REPO_ROOT / "helm" / "charts" / "otel-collector"
ENABLED = (
    "gnmic.enabled=true",
    "gnmic.targets.router-1.address=router-1.example.com:57400",
)


def render(*sets: str) -> subprocess.CompletedProcess:
    cmd = ["helm", "template", "otel", str(CHART)]
    for s in sets:
        cmd += ["--set", s]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def rendered(*sets: str) -> list[dict]:
    out = render(*sets)
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for otel-collector:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def named(docs: list[dict], kind: str, suffix: str) -> list[dict]:
    return [
        d
        for d in docs
        if d.get("kind") == kind and d["metadata"]["name"].endswith(suffix)
    ]


def test_nothing_renders_by_default() -> None:
    docs = rendered()
    found = [d["metadata"]["name"] for d in docs if "gnmic" in d["metadata"]["name"]]
    expect("no gnmic object renders", not found, repr(found))


def test_the_workload_pushes_to_the_gateway_grpc_port() -> None:
    docs = rendered(*ENABLED)
    config = named(docs, "ConfigMap", "-gnmic-config")
    expect("one gnmic ConfigMap renders", len(config) == 1, f"got {len(config)}")
    settings = yaml.safe_load(config[0]["data"]["gnmic.yaml"])
    output = settings["outputs"]["otel-collector"]
    expect("the output is otlp over gRPC", output["type"] == "otlp", repr(output))
    expect(
        "it dials the gateway service on 4317",
        output["endpoint"] == "dfe-otel-collector-gateway:4317",
        repr(output["endpoint"]),
    )
    expect(
        "its rows carry service.name gnmic",
        output["resource-attributes"] == {"service.name": "gnmic"},
        repr(output["resource-attributes"]),
    )
    expect(
        "the device is a target",
        "router-1" in settings["targets"],
        repr(settings["targets"]),
    )


def test_one_pinned_unprivileged_replica() -> None:
    docs = rendered(*ENABLED)
    deployment = named(docs, "Deployment", "-gnmic")[0]
    pod = deployment["spec"]["template"]["spec"]
    image = pod["containers"][0]["image"]
    expect("one replica", deployment["spec"]["replicas"] == 1, "")
    expect("the image is pinned by digest", "@sha256:" in image, image)
    expect(
        "the pod mounts no service account token",
        pod["automountServiceAccountToken"] is False,
        repr(pod.get("automountServiceAccountToken")),
    )
    expect(
        "the root filesystem is read-only",
        pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True,
        "",
    )


def test_the_login_secret_reaches_the_container() -> None:
    docs = rendered(*ENABLED, "gnmic.credentials.secretName=device-login")
    env = named(docs, "Deployment", "-gnmic")[0]["spec"]["template"]["spec"][
        "containers"
    ][0]["env"]
    refs = {e["name"]: e["valueFrom"]["secretKeyRef"]["name"] for e in env}
    expect(
        "GNMIC_USERNAME and GNMIC_PASSWORD read the named Secret",
        refs == {"GNMIC_USERNAME": "device-login", "GNMIC_PASSWORD": "device-login"},
        repr(refs),
    )


def test_enabled_with_no_device_is_refused() -> None:
    out = render("gnmic.enabled=true")
    expect("the render fails", out.returncode != 0, out.stdout[:200])
    expect("it names the field", "gnmic.targets" in out.stderr, out.stderr[-400:])


def main() -> int:
    with standalone():
        test_nothing_renders_by_default()
        test_the_workload_pushes_to_the_gateway_grpc_port()
        test_one_pinned_unprivileged_replica()
        test_the_login_secret_reaches_the_container()
        test_enabled_with_no_device_is_refused()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
