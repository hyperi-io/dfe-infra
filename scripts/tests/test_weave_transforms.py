#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_weave_transforms.py
#  Purpose:      Prove the archiver and transform thin charts wait for what they
#                need, take a deployer's env and files, and read the secrets
#                their contracts declare.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the dfe-archiver and dfe-transform-* thin charts carry.

    python3 -m pytest scripts/tests/test_weave_transforms.py -q

- dfe-transform-elastic waits for dfe-engine and takes a deployer's extraEnv
- dfe-archiver's Kafka and S3 secret references come from its contract
- transform files a deployer sets under fileSets reach the pod, and the app is
  told where they are

The default renders are the goldens (_gate.golden). The overlay cases render
live, which needs the scalo-service library (_weave.library).
"""

import json

import pytest

from _gate import PROFILES, configmap_files, golden
from _weave import contract, render_app

SERVICES = ("dfe-archiver", "dfe-transform-vrl", "dfe-transform-vector", "dfe-transform-elastic")


def _pod(docs: list[dict]) -> dict:
    return next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]


def _env(docs: list[dict]) -> dict[str, dict]:
    return {e["name"]: e for e in _pod(docs)["containers"][0].get("env") or []}


def _render_with(service: str, profile: str, overlay: dict) -> list[dict]:
    return render_app(service, profile, "local", overlay=overlay)


@pytest.mark.parametrize("profile", PROFILES)
def test_elastic_waits_for_the_engine(profile: str) -> None:
    pod = _pod(golden("dfe-transform-elastic", profile, "local"))
    assert [i["name"] for i in pod["initContainers"]] == ["wait-for-engine"]


def test_elastic_takes_a_deployers_extra_env() -> None:
    overlay = {"extraEnv": {"DFE_TRANSFORM_ELASTIC_SOURCE_ENVELOPE": "beats"}}
    new = _render_with("dfe-transform-elastic", "single", overlay)
    assert _env(new)["DFE_TRANSFORM_ELASTIC_SOURCE_ENVELOPE"]["value"] == "beats"
    default = _env(golden("dfe-transform-elastic", "single", "local"))
    assert "DFE_TRANSFORM_ELASTIC_SOURCE_ENVELOPE" not in default


def test_the_archiver_contract_declares_its_secrets() -> None:
    declared = json.loads(contract("dfe-archiver").read_text(encoding="utf-8"))["secrets"]
    assert {g["group_name"]: g.get("optional", False) for g in declared} == {
        "kafka": False,
        "s3": True,
    }
    env = _env(golden("dfe-archiver", "scale", "aws"))
    assert env["KAFKA_SASL_MECHANISM"]["valueFrom"]["secretKeyRef"] == {
        "name": "dfe-kafka-user",
        "key": "sasl.mechanism",
    }
    assert env["S3_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]["optional"] is True


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("profile", ["slim", "mesh"])
def test_the_direct_profiles_carry_no_kafka_credentials(service: str, profile: str) -> None:
    refs = [
        e["valueFrom"]["secretKeyRef"]["name"]
        for e in _env(golden(service, profile, "aws")).values()
        if "secretKeyRef" in e.get("valueFrom", {})
    ]
    assert "dfe-kafka-user" not in refs


@pytest.mark.parametrize("service", ["dfe-transform-vrl", "dfe-transform-vector"])
def test_transform_files_reach_the_pod(service: str) -> None:
    files = [{"name": "10-route.vrl", "content": "  .x = 1\n.y = 2\n"}]
    docs = _render_with(service, "single", {"fileSets": {"transforms": {"files": files}}})
    configmap = next(d for d in docs if d["metadata"]["name"] == f"{service}-transforms")
    assert configmap_files(configmap) == {"10-route.vrl": "  .x = 1\n.y = 2\n"}
    directory = f"/etc/{service}-transforms"
    mounts = {m["mountPath"]: m for m in _pod(docs)["containers"][0]["volumeMounts"]}
    assert mounts[directory]["readOnly"] is True
    assert _env(docs)["DFE_TRANSFORM_TRANSFORMS_DIR"]["value"] == directory


def test_without_files_the_app_is_not_pointed_at_the_empty_mount() -> None:
    """An empty value is unset to the app, so its own config names the programs."""
    env = _env(golden("dfe-transform-vrl", "single", "local"))
    assert env["DFE_TRANSFORM_TRANSFORMS_DIR"]["value"] == ""
