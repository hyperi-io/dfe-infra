#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_teardown_engine_secrets.py
#  Purpose:      Prove `dfe-ops teardown --with-terraform` force-deletes the
#                secrets dfe-engine minted under its prefix once tofu destroy
#                succeeds, keeps them when it does not, and touches nothing
#                outside that prefix.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What a persistent AWS deployment's teardown does with dfe-engine's own secrets.

The engine creates them itself, so they sit outside tofu state: a destroy would
leave them in the account, holding their names. A test run's are swept by their
run tag; a deployment that is not a run has no such tag, so the teardown clears
the engine's prefix after the destroy. A destroy that is refused or fails leaves
the deployment up, and its engine still needs them, so they stay.

A persistent deployment's destroy forgets its KMS key on purpose (kms.tf), so the
teardown passes -suppress-forget-errors and ends on what it kept, read from the
root's retained_on_destroy output.

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
TF_DIR = "terraform/environments/aws"
# The shape bridge.get_tf_outputs really answers with: name -> (value, sensitive).
TF_OUTPUTS = {
    "DFE_CLOUD": ("aws", False),
    "DFE_REGION": ("us-west-2", False),
    "DFE_SECRETS_REGION": ("us-west-2", False),
    "DFE_ENGINE_SECRETS_PREFIX": (PREFIX, False),
}
KEY_ARN = "arn:aws:kms:us-west-2:000000000000:key/00000000-0000-0000-0000-000000000000"
# A persistent root's retained_on_destroy, as `tofu output -json` stringifies it.
FORGOTTEN = {
    "module.cluster.aws_kms_key.retained[0]": KEY_ARN,
    "module.cluster.module.clickhouse_object_store.aws_s3_bucket.retained[0]": "arn:aws:s3:::dfe-prod-clickhouse",
    "module.toolbox_session_logs.aws_s3_bucket.retained[0]": "arn:aws:s3:::dfe-prod-toolbox-session-logs",
}
PERSISTENT_OUTPUTS = {**TF_OUTPUTS, "retained_on_destroy": (json.dumps(FORGOTTEN), False)}
EPHEMERAL_OUTPUTS = {**TF_OUTPUTS, "retained_on_destroy": ("", False)}
KEPT_LINES = [
    *(f"  kept {address} ({arn})" for address, arn in sorted(FORGOTTEN.items())),
    "  a persistent deployment keeps these through tofu destroy: delete the buckets once nothing needs "
    "what they hold, then schedule the key's deletion",
]
LISTED = [
    {"Name": f"{PREFIX}/jwt/signing-key"},
    {"Name": f"{PREFIX}/ch/orgs/acme"},
    # The name filter is case-insensitive; this is another deployment's.
    {"Name": "DFE/Prod/engine/jwt/signing-key"},
]


class Calls:
    """Every command the teardown runs, in order, and none of them run for real."""

    def __init__(self, listing: list[dict] | Exception = LISTED, destroy: int = 0) -> None:
        self.listing = listing
        self.destroy = destroy
        self.seen: list[tuple[str, list[str]]] = []

    def stream(self, cmd: list[str], env: dict | None = None) -> int:
        self.seen.append(("run", cmd))
        return self.destroy if cmd[0] == "tofu" else 0

    def aws(self, args: list[str], region: str) -> dict:
        self.seen.append(("aws", [*args, "--region", region]))
        if isinstance(self.listing, Exception):
            raise self.listing
        return {"SecretList": self.listing} if args[1] == "list-secrets" else {}

    def deleted(self) -> list[str]:
        return [cmd[cmd.index("--secret-id") + 1] for _kind, cmd in self.seen if "delete-secret" in cmd]


def _bridge(monkeypatch: pytest.MonkeyPatch, outputs: dict | Exception) -> None:
    bridge = type(sys)("bridge")
    bridge.TerraformOutputError = type("TerraformOutputError", (RuntimeError,), {})

    def get_tf_outputs(_tf_dir: str) -> dict:
        if isinstance(outputs, Exception):
            raise bridge.TerraformOutputError(str(outputs))
        return dict(outputs)

    bridge.get_tf_outputs = get_tf_outputs
    monkeypatch.setitem(sys.modules, "bridge", bridge)


def _teardown(monkeypatch: pytest.MonkeyPatch, calls: Calls, outputs: dict | Exception, *argv: str) -> int:
    _bridge(monkeypatch, outputs)
    monkeypatch.setattr(dfeops, "_run_streaming", calls.stream)
    monkeypatch.setattr(dfeops.cloud_sweep, "run_aws", calls.aws)
    args = ["teardown", "--with-terraform", "--tf-dir", TF_DIR, "--tf-bin", "tofu", *argv]
    return dfeops.cmd_teardown(dfeops.build_parser().parse_args(args))


@pytest.mark.parametrize(
    ("outputs", "destroy_flags", "kept"),
    [
        pytest.param(TF_OUTPUTS, ["-auto-approve"], [], id="root-without-the-output"),
        pytest.param(EPHEMERAL_OUTPUTS, ["-auto-approve"], [], id="ephemeral"),
        pytest.param(PERSISTENT_OUTPUTS, ["-auto-approve", "-suppress-forget-errors"], KEPT_LINES,
                     id="persistent"),
    ],
)
def test_the_engine_prefix_is_cleared_once_tofu_destroy_succeeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    outputs: dict, destroy_flags: list[str], kept: list[str],
) -> None:
    """A persistent destroy forgets its key on purpose, so that is no failure, and the
    teardown ends on what it kept. An ephemeral one runs exactly as it always has."""
    calls = Calls()
    assert _teardown(monkeypatch, calls, outputs, "--force") == 0
    assert [kind for kind, _cmd in calls.seen] == ["run", "run", "aws", "aws", "aws"]
    assert calls.seen[0][1][-1] == "--force"
    assert calls.seen[1][1] == ["tofu", f"-chdir={TF_DIR}", "destroy", *destroy_flags]
    err = capsys.readouterr().err.splitlines()
    if kept:
        assert err[-len(kept):] == kept
    else:
        assert not [line for line in err if line.startswith("  kept module.") or "keeps these" in line]
    listing = calls.seen[2][1]
    assert listing[:3] == ["secretsmanager", "list-secrets", "--include-planned-deletion"]
    assert listing[listing.index("--filters") + 1] == f"Key=name,Values={PREFIX}/"
    assert calls.deleted() == [f"{PREFIX}/ch/orgs/acme", f"{PREFIX}/jwt/signing-key"]
    assert all("--force-delete-without-recovery" in cmd for _k, cmd in calls.seen if "delete-secret" in cmd)


def test_expected_fail_a_refused_destroy_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """Answering no at tofu's prompt exits 1, and the deployment it kept still reads its secrets."""
    calls = Calls(destroy=1)
    assert _teardown(monkeypatch, calls, TF_OUTPUTS) == 1
    assert calls.seen[1][1] == ["tofu", f"-chdir={TF_DIR}", "destroy"]
    assert calls.deleted() == []
    assert "kept 2 dfe-engine secret(s) under DFE_ENGINE_SECRETS_PREFIX in us-west-2" in capsys.readouterr().err


def test_expected_fail_a_failed_destroy_deletes_nothing_and_says_what_it_kept(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    calls = Calls(destroy=1)
    assert _teardown(monkeypatch, calls, TF_OUTPUTS, "--force") == 1
    assert [kind for kind, _cmd in calls.seen] == ["run", "run", "aws"]
    assert calls.seen[2][1][1] == "list-secrets"
    assert calls.deleted() == []
    err = capsys.readouterr().err
    assert "kept 2 dfe-engine secret(s) under DFE_ENGINE_SECRETS_PREFIX in us-west-2" in err
    assert f"tofu -chdir={TF_DIR} output -raw DFE_ENGINE_SECRETS_PREFIX" in err
    assert PREFIX not in err


def test_expected_fail_a_failed_persistent_destroy_still_ends_on_what_it_kept(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    """A bucket that still holds objects fails the destroy, and the summary still closes it."""
    calls = Calls(destroy=1)
    assert _teardown(monkeypatch, calls, PERSISTENT_OUTPUTS, "--force") == 1
    assert calls.seen[1][1][-1] == "-suppress-forget-errors"
    assert calls.deleted() == []
    err = capsys.readouterr().err.splitlines()
    assert err[-len(KEPT_LINES):] == KEPT_LINES


def test_a_root_with_no_engine_prefix_calls_no_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every cloud but AWS: the local root emits no DFE_ENGINE_SECRETS_PREFIX."""
    outputs = {k: v for k, v in TF_OUTPUTS.items() if k != "DFE_ENGINE_SECRETS_PREFIX"}
    for destroy in (0, 1):
        calls = Calls(destroy=destroy)
        assert _teardown(monkeypatch, calls, outputs, "--force") == destroy
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
    assert err.index("would run: tofu") < err.index("would then force-delete every secret under "
                                                    "DFE_ENGINE_SECRETS_PREFIX in us-west-2")
    assert PREFIX not in err


def test_expected_fail_a_store_that_cannot_be_cleared_fails_the_teardown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    calls = Calls(listing=dfeops.cloud_sweep.CloudSweepError("AccessDeniedException"))
    assert _teardown(monkeypatch, calls, TF_OUTPUTS, "--force") == 1
    assert calls.seen[1][1][:3] == ["tofu", f"-chdir={TF_DIR}", "destroy"]
    err = capsys.readouterr().err
    assert "could not clear dfe-engine's secrets in us-west-2" in err
    assert "--force-delete-without-recovery" in err
    assert PREFIX not in err


def test_expected_fail_unreadable_outputs_still_destroy_and_fail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    calls = Calls()
    assert _teardown(monkeypatch, calls, RuntimeError("no state"), "--force") == 1
    assert [kind for kind, _cmd in calls.seen] == ["run", "run"]
    assert "so dfe-engine's secrets will not be cleared" in capsys.readouterr().err


def test_the_listing_never_reads_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    for destroy in (0, 1):
        calls = Calls(destroy=destroy)
        _teardown(monkeypatch, calls, TF_OUTPUTS, "--force")
        assert not any("get-secret-value" in json.dumps(cmd) for _k, cmd in calls.seen)
