#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_weave_transforms.py
#  Purpose:      Prove the archiver and transform thin charts carry what their
#                2.2.0 charts lacked or derived, which a render diff against
#                2.2.0 cannot show on its own.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the dfe-archiver and dfe-transform-* thin charts add over 2.2.0.

    python3 -m pytest scripts/tests/test_weave_transforms.py -q

- dfe-transform-elastic waits for dfe-engine and takes a deployer's extraEnv,
  where its 2.2.0 chart did neither
- dfe-archiver's Kafka and S3 secret references come from its contract, which
  declared none
- transform files a deployer sets under fileSets reach the pod, and the app is
  told where they are

Render (b) needs the scalo-service library (_weave.library).
"""

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from _gate import PROFILES, cell
from _weave import contract, render_app

SERVICES = ("dfe-archiver", "dfe-transform-vrl", "dfe-transform-vector", "dfe-transform-elastic")


def _pod(docs: list[dict]) -> dict:
    return next(d for d in docs if d["kind"] == "Deployment")["spec"]["template"]["spec"]


def _env(docs: list[dict]) -> dict[str, dict]:
    return {e["name"]: e for e in _pod(docs)["containers"][0].get("env") or []}


def _render_with(service: str, profile: str, overlay: dict, which: str = "new") -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="dfe-weave-transforms-") as tmp:
        path = Path(tmp) / "values" / f"{service}-default-values.yaml"
        path.parent.mkdir()
        body = {"deploy": {"service": service, "instance": "default"}, **overlay}
        path.write_text(yaml.safe_dump(body), encoding="utf-8", newline="\n")
        return render_app(service, profile, "local", which, deploy_repo=Path(tmp))


@pytest.mark.parametrize("profile", PROFILES)
def test_elastic_waits_for_the_engine(profile: str) -> None:
    c = cell("dfe-transform-elastic", profile, "local")
    assert "initContainers" not in _pod(c.old)
    assert [i["name"] for i in _pod(c.new)["initContainers"]] == ["wait-for-engine"]


def test_elastic_takes_a_deployers_extra_env() -> None:
    overlay = {"extraEnv": {"DFE_TRANSFORM_ELASTIC_SOURCE_ENVELOPE": "beats"}}
    new = _render_with("dfe-transform-elastic", "single", overlay)
    old = _render_with("dfe-transform-elastic", "single", overlay, which="old")
    assert _env(new)["DFE_TRANSFORM_ELASTIC_SOURCE_ENVELOPE"]["value"] == "beats"
    assert "DFE_TRANSFORM_ELASTIC_SOURCE_ENVELOPE" not in _env(old)


def test_the_archiver_contract_declares_its_secrets() -> None:
    declared = json.loads(contract("dfe-archiver").read_text(encoding="utf-8"))["secrets"]
    assert {g["group_name"]: g.get("optional", False) for g in declared} == {
        "kafka": False,
        "s3": True,
    }
    env = _env(cell("dfe-archiver", "scale", "aws").new)
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
        for e in _env(cell(service, profile, "aws").new).values()
        if "secretKeyRef" in e.get("valueFrom", {})
    ]
    assert "dfe-kafka-user" not in refs


@pytest.mark.parametrize("service", ["dfe-transform-vrl", "dfe-transform-vector"])
def test_transform_files_reach_the_pod(service: str) -> None:
    files = [{"name": "10-route.vrl", "content": "  .x = 1\n.y = 2\n"}]
    docs = _render_with(service, "single", {"fileSets": {"transforms": {"files": files}}})
    configmap = next(d for d in docs if d["metadata"]["name"] == f"{service}-transforms")
    assert configmap["data"] == {"10-route.vrl": "  .x = 1\n.y = 2\n"}
    directory = f"/etc/{service}-transforms"
    mounts = {m["mountPath"]: m for m in _pod(docs)["containers"][0]["volumeMounts"]}
    assert mounts[directory]["readOnly"] is True
    assert _env(docs)["DFE_TRANSFORM_TRANSFORMS_DIR"]["value"] == directory


def test_without_files_the_app_is_not_pointed_at_the_empty_mount() -> None:
    """An empty value is unset to the app, so its own config names the programs."""
    env = _env(cell("dfe-transform-vrl", "single", "local").new)
    assert env["DFE_TRANSFORM_TRANSFORMS_DIR"]["value"] == ""
