#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_fetcher_credentials.py
#  Purpose:      Prove the fetcher-credentials apply reads the store, writes the
#                Secret the chart names, and never puts a value in its output
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for dfe-ops apply_fetcher_credentials.

Runs offline against a recorder in place of subprocess.run, so neither `bao` nor
a cluster is needed. The value-leak assertions are the point: the credentials
pass through this code on their way to a Secret, and anything it prints ends up
in a run log a delegate pastes into a report.

    python3 -m pytest scripts/tests/test_fetcher_credentials.py
    python3 scripts/tests/test_fetcher_credentials.py
"""

from __future__ import annotations

import base64
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

SECRET_ID = "AKIAEXAMPLE0000TEST"
SECRET_KEY = "s3cr3t-example-key-never-real"
KUBE = ["kubectl", "--kubeconfig", "/dev/null"]


_loader = importlib.machinery.SourceFileLoader("dfeops_credentials", str(SCRIPTS / "dfe-ops"))
dfe_ops = importlib.util.module_from_spec(importlib.util.spec_from_loader("dfeops_credentials", _loader))
sys.modules["dfeops_credentials"] = dfe_ops
_loader.exec_module(dfe_ops)


class Reply:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class Recorder:
    """Stands in for subprocess.run, keeping every call for the assertions."""

    def __init__(self, kv_payload=None, kv_rc=0, kv_stderr="", apply_rc=0, apply_stderr=""):
        self.calls: list[dict] = []
        self.kv_payload = kv_payload
        self.kv_rc, self.kv_stderr = kv_rc, kv_stderr
        self.apply_rc, self.apply_stderr = apply_rc, apply_stderr

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, **kwargs})
        if cmd[0] in ("bao", "vault"):
            return Reply(self.kv_rc, json.dumps(self.kv_payload or {}), self.kv_stderr)
        return Reply(self.apply_rc, "secret/dfe-fetcher-credentials configured", self.apply_stderr)


def _kv_v2(**pairs):
    return {"data": {"data": pairs, "metadata": {"version": 3}}}


def _kv_v1(**pairs):
    return {"data": pairs}


def test_the_secret_carries_both_keys_base64_as_the_api_wants():
    rec = Recorder(_kv_v2(AWS_ACCESS_KEY_ID=SECRET_ID, AWS_SECRET_ACCESS_KEY=SECRET_KEY))
    dfe_ops.apply_fetcher_credentials(KUBE, "dfe-local", "kv/dfe-test/aws", run=rec)
    applied = json.loads(rec.calls[1]["input"])
    assert applied["kind"] == "Secret"
    assert applied["metadata"] == {"name": "dfe-fetcher-credentials", "namespace": "dfe-local"}
    assert base64.b64decode(applied["data"]["AWS_ACCESS_KEY_ID"]).decode() == SECRET_ID
    assert base64.b64decode(applied["data"]["AWS_SECRET_ACCESS_KEY"]).decode() == SECRET_KEY


def test_a_kv_v1_store_reads_the_same():
    rec = Recorder(_kv_v1(AWS_ACCESS_KEY_ID=SECRET_ID, AWS_SECRET_ACCESS_KEY=SECRET_KEY))
    dfe_ops.apply_fetcher_credentials(KUBE, "dfe-local", "kv/dfe-test/aws", run=rec)
    applied = json.loads(rec.calls[1]["input"])
    assert base64.b64decode(applied["data"]["AWS_ACCESS_KEY_ID"]).decode() == SECRET_ID


def test_a_store_naming_the_fields_its_own_way_reads_the_same():
    """The Secret's keys are the fetcher's env names; a store names the values its own way."""
    rec = Recorder(_kv_v2(
        access_key_id=SECRET_ID, secret_access_key=SECRET_KEY,
        region="ap-southeast-2", log_group_name="dfe-test", notes="a test account",
    ))
    dfe_ops.apply_fetcher_credentials(KUBE, "dfe-local", "kv/dfe-test/aws", run=rec)
    applied = json.loads(rec.calls[1]["input"])
    assert base64.b64decode(applied["data"]["AWS_ACCESS_KEY_ID"]).decode() == SECRET_ID
    assert base64.b64decode(applied["data"]["AWS_SECRET_ACCESS_KEY"]).decode() == SECRET_KEY


def test_nothing_beside_the_two_credentials_reaches_the_secret():
    """A store path holds more than the credentials, and none of it belongs in the Secret."""
    rec = Recorder(_kv_v2(
        access_key_id=SECRET_ID, secret_access_key=SECRET_KEY,
        account_id="111122223333", iam_user_arn="arn:aws:iam::111122223333:user/dfe-fetcher-test",
    ))
    dfe_ops.apply_fetcher_credentials(KUBE, "dfe-local", "kv/dfe-test/aws", run=rec)
    applied = json.loads(rec.calls[1]["input"])
    assert sorted(applied["data"]) == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]


def test_it_applies_through_the_caller_s_kubectl_and_namespace():
    rec = Recorder(_kv_v2(AWS_ACCESS_KEY_ID=SECRET_ID, AWS_SECRET_ACCESS_KEY=SECRET_KEY))
    dfe_ops.apply_fetcher_credentials(KUBE, "dfe-b", "kv/dfe-test/aws", run=rec)
    assert rec.calls[1]["cmd"] == [
        *KUBE, "-n", "dfe-b", "apply", "--server-side", "--force-conflicts",
        "--field-manager=dfe-ops", "-f", "-",
    ]


def test_the_apply_is_server_side_so_no_annotation_carries_the_values():
    """kubectl describe prints annotations and redacts data, so a client-side apply leaks."""
    rec = Recorder(_kv_v2(AWS_ACCESS_KEY_ID=SECRET_ID, AWS_SECRET_ACCESS_KEY=SECRET_KEY))
    dfe_ops.apply_fetcher_credentials(KUBE, "dfe-local", "kv/dfe-test/aws", run=rec)
    assert "--server-side" in rec.calls[1]["cmd"]


def test_the_store_is_read_with_the_named_cli():
    rec = Recorder(_kv_v2(AWS_ACCESS_KEY_ID=SECRET_ID, AWS_SECRET_ACCESS_KEY=SECRET_KEY))
    dfe_ops.apply_fetcher_credentials(KUBE, "dfe-local", "kv/dfe-test/aws", vault_cmd="vault", run=rec)
    assert rec.calls[0]["cmd"] == ["vault", "kv", "get", "-format=json", "kv/dfe-test/aws"]


def test_no_value_reaches_the_returned_line():
    rec = Recorder(_kv_v2(AWS_ACCESS_KEY_ID=SECRET_ID, AWS_SECRET_ACCESS_KEY=SECRET_KEY))
    detail = dfe_ops.apply_fetcher_credentials(KUBE, "dfe-local", "kv/dfe-test/aws", run=rec)
    assert SECRET_ID not in detail
    assert SECRET_KEY not in detail
    assert "AWS_ACCESS_KEY_ID" in detail
    assert "kv/dfe-test/aws" in detail


def test_no_value_reaches_the_command_line():
    """A value in argv is a value in every process listing on the host."""
    rec = Recorder(_kv_v2(AWS_ACCESS_KEY_ID=SECRET_ID, AWS_SECRET_ACCESS_KEY=SECRET_KEY))
    dfe_ops.apply_fetcher_credentials(KUBE, "dfe-local", "kv/dfe-test/aws", run=rec)
    for call in rec.calls:
        assert SECRET_ID not in " ".join(call["cmd"])
        assert SECRET_KEY not in " ".join(call["cmd"])


def test_a_half_populated_path_is_refused_before_anything_is_applied():
    rec = Recorder(_kv_v2(access_key_id=SECRET_ID))
    # Both accepted names are named, so the reader knows what to add.
    with pytest.raises(RuntimeError, match="AWS_SECRET_ACCESS_KEY or secret_access_key"):
        dfe_ops.apply_fetcher_credentials(KUBE, "dfe-local", "kv/dfe-test/aws", run=rec)
    assert len(rec.calls) == 1


def test_a_store_that_refuses_is_reported_without_its_output_body():
    rec = Recorder(kv_rc=2, kv_stderr="Error making API request.\nCode: 403. Errors:\n* permission denied")
    with pytest.raises(RuntimeError, match=r"Error making API request\.") as refused:
        dfe_ops.apply_fetcher_credentials(KUBE, "dfe-local", "kv/dfe-test/aws", run=rec)
    assert "permission denied" not in str(refused.value)


def test_a_refused_apply_is_a_failure_not_a_silent_pass():
    rec = Recorder(
        _kv_v2(AWS_ACCESS_KEY_ID=SECRET_ID, AWS_SECRET_ACCESS_KEY=SECRET_KEY),
        apply_rc=1, apply_stderr="Error from server (Forbidden): secrets is forbidden",
    )
    with pytest.raises(RuntimeError, match="dfe-fetcher-credentials") as refused:
        dfe_ops.apply_fetcher_credentials(KUBE, "dfe-local", "kv/dfe-test/aws", run=rec)
    assert "Forbidden" in str(refused.value)


def test_the_default_is_to_leave_the_secret_alone(tmp_path):
    """An unset path is the default, so the flag adds nothing to a run that does not ask."""
    parser = dfe_ops.build_parser()
    args = parser.parse_args(["acceptance", "--repo", str(tmp_path), "--suite", "source"])
    assert args.fetcher_credentials_vault_path == ""
    assert args.vault_cmd == "bao"


# --- standalone runner (mirrors the other tests in this dir) ------------------
def main() -> int:
    return pytest.main([__file__, "-q"])


if __name__ == "__main__":
    sys.exit(main())
