#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_teardown_engine_secrets.py
#  Purpose:      Prove `dfe-ops teardown --with-terraform` force-deletes the
#                secrets dfe-engine minted under its prefix before tofu destroy,
#                and nothing outside that prefix.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What a persistent AWS deployment's teardown does with dfe-engine's own secrets.

The engine creates them itself, so they sit outside tofu state: a destroy would
leave them in the account, holding their names, encrypted with a key scheduled
for deletion. A test run's are swept by their run tag; a deployment that is not a
run has no such tag, so the teardown clears the engine's prefix first.

    python3 -m pytest scripts/tests/test_dfe_ops_teardown_engine_secrets.py -q
"""

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops_teardown_engine_secrets", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops_teardown_engine_secrets", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops_teardown_engine_secrets"] = dfeops
_loader.exec_module(dfeops)

PREFIX = "dfe/prod/engine"
# The shape bridge.get_tf_outputs really answers with: name -> (value, sensitive).
TF_OUTPUTS = {
    "DFE_CLOUD": ("aws", False),
    "DFE_REGION": ("us-west-2", False),
    "DFE_SECRETS_REGION": ("us-west-2", False),
    "DFE_ENGINE_SECRETS_PREFIX": (PREFIX, False),
}
LISTED = [
    {"Name": f"{PREFIX}/jwt/signing-key"},
    {"Name": f"{PREFIX}/ch/orgs/acme"},
    # The name filter is case-insensitive; this is another deployment's.
    {"Name": "DFE/Prod/engine/jwt/signing-key"},
]


class Calls:
    """Every command the teardown runs, in order, and none of them run for real."""

    def __init__(self, listing: list[dict] | Exception = LISTED) -> None:
        self.listing = listing
        self.seen: list[tuple[str, list[str]]] = []

    def stream(self, cmd: list[str], env: dict | None = None) -> int:
        self.seen.append(("run", cmd))
        return 0

    def aws(self, args: list[str], region: str) -> dict:
        self.seen.append(("aws", [*args, "--region", region]))
        if isinstance(self.listing, Exception):
            raise self.listing
        return {"SecretList": self.listing} if args[1] == "list-secrets" else {}


def _bridge(monkeypatch: pytest.MonkeyPatch, outputs: dict) -> None:
    bridge = type(sys)("bridge")
    bridge.get_tf_outputs = lambda _tf_dir: dict(outputs)
    bridge.TerraformOutputError = type("TerraformOutputError", (RuntimeError,), {})
    monkeypatch.setitem(sys.modules, "bridge", bridge)


def _teardown(monkeypatch: pytest.MonkeyPatch, calls: Calls, outputs: dict, *argv: str) -> int:
    _bridge(monkeypatch, outputs)
    monkeypatch.setattr(dfeops, "_run_streaming", calls.stream)
    monkeypatch.setattr(dfeops.cloud_sweep, "run_aws", calls.aws)
    args = ["teardown", "--with-terraform", "--tf-dir", "terraform/environments/aws", "--tf-bin", "tofu", *argv]
    return dfeops.cmd_teardown(dfeops.build_parser().parse_args(args))


def test_the_engine_prefix_is_cleared_after_the_workloads_and_before_tofu_destroy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = Calls()
    assert _teardown(monkeypatch, calls, TF_OUTPUTS, "--force") == 0
    kinds = [kind for kind, _cmd in calls.seen]
    assert kinds == ["run", "aws", "aws", "aws", "run"]
    assert calls.seen[0][1][-1] == "--force"
    assert calls.seen[-1][1][:3] == ["tofu", "-chdir=terraform/environments/aws", "destroy"]
    listing = calls.seen[1][1]
    assert listing[:3] == ["secretsmanager", "list-secrets", "--include-planned-deletion"]
    assert listing[listing.index("--filters") + 1] == f"Key=name,Values={PREFIX}/"
    deleted = [cmd[cmd.index("--secret-id") + 1] for kind, cmd in calls.seen if "delete-secret" in cmd]
    assert deleted == [f"{PREFIX}/ch/orgs/acme", f"{PREFIX}/jwt/signing-key"]
    assert all("--force-delete-without-recovery" in cmd for _k, cmd in calls.seen if "delete-secret" in cmd)


def test_a_root_with_no_engine_prefix_calls_no_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every cloud but AWS: the local root emits no DFE_ENGINE_SECRETS_PREFIX."""
    calls = Calls()
    outputs = {k: v for k, v in TF_OUTPUTS.items() if k != "DFE_ENGINE_SECRETS_PREFIX"}
    assert _teardown(monkeypatch, calls, outputs, "--force") == 0
    assert [kind for kind, _cmd in calls.seen] == ["run", "run"]


def test_a_teardown_without_terraform_keeps_the_engine_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """The infrastructure stays, so a redeploy reads the same signing key and passwords."""
    calls = Calls()
    _bridge(monkeypatch, TF_OUTPUTS)
    monkeypatch.setattr(dfeops, "_run_streaming", calls.stream)
    monkeypatch.setattr(dfeops.cloud_sweep, "run_aws", calls.aws)
    assert dfeops.cmd_teardown(dfeops.build_parser().parse_args(["teardown", "--force"])) == 0
    assert [kind for kind, _cmd in calls.seen] == ["run"]


def test_a_dry_run_names_the_prefix_and_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    calls = Calls()
    assert _teardown(monkeypatch, calls, TF_OUTPUTS, "--dry-run") == 0
    assert [kind for kind, _cmd in calls.seen] == ["run"]
    err = capsys.readouterr().err
    assert "would force-delete every secret under DFE_ENGINE_SECRETS_PREFIX in us-west-2" in err
    assert PREFIX not in err


def test_expected_fail_a_store_that_cannot_be_cleared_still_destroys_and_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Leaving the infrastructure up over a few secrets costs more than the secrets."""
    calls = Calls(listing=dfeops.cloud_sweep.CloudSweepError("AccessDeniedException"))
    assert _teardown(monkeypatch, calls, TF_OUTPUTS, "--force") == 1
    assert calls.seen[-1][1][:3] == ["tofu", "-chdir=terraform/environments/aws", "destroy"]
    err = capsys.readouterr().err
    assert "could not clear dfe-engine's secrets in us-west-2" in err
    assert "output -raw DFE_ENGINE_SECRETS_PREFIX" in err
    assert "--force-delete-without-recovery" in err


def test_the_listing_never_reads_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls()
    _teardown(monkeypatch, calls, TF_OUTPUTS, "--force")
    assert not any("get-secret-value" in json.dumps(cmd) for _k, cmd in calls.seen)
