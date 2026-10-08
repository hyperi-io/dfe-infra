#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_cloud_run.py
#  Purpose:      Guard the run identity and expiry convention: what counts as
#                expired and what never does, both expiry forms, the portable
#                value rules, the OpenTofu half's defaults, and the run-record
#                checks a reaper relies on before it destroys anything.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for `cloud_run.py`.

    python3 -m pytest scripts/tests/test_cloud_run.py -q

Pure functions only: no cloud call, no clock but the ones passed in.
"""

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import cloud_run  # noqa: E402

RUN_TAGS_VARIABLES = REPO_ROOT / "terraform" / "modules" / "tf-run-tags" / "variables.tf"
# 2026-10-08T12:00:00Z, the epoch the OpenTofu module's own tests render.
NOW = 1791460800
KEYS = cloud_run.RunTagKeys()
# str.isdigit() accepts these, so they prove the ASCII guard in parse_expiry.
FULLWIDTH_DIGITS = chr(0xFF11) + chr(0xFF12)


# --- the two halves agree --------------------------------------------------------


def test_the_opentofu_module_defaults_are_the_python_defaults() -> None:
    """Two definitions of one convention drift unless something holds them equal."""
    text = RUN_TAGS_VARIABLES.read_text(encoding="utf-8")
    defaults = dict(re.findall(r'(\w+)\s*=\s*optional\(string,\s*"([^"]+)"\)', text))
    assert defaults == {
        "run": cloud_run.DEFAULT_RUN_KEY,
        "expiry": cloud_run.DEFAULT_EXPIRY_KEY,
        "format": cloud_run.DEFAULT_EXPIRY_FORMAT,
    }


def test_run_tags_write_what_the_opentofu_module_writes() -> None:
    """tf-run-tags' own tests assert the same epoch renders 2026-10-08T12:00:00Z."""
    assert cloud_run.run_tags("run-1", NOW) == {"dfe-e2e": "run-1", "expires-at": "2026-10-08T12:00:00Z"}


def test_the_epoch_format_writes_plain_seconds() -> None:
    keys = cloud_run.RunTagKeys(expiry_format="epoch")
    assert cloud_run.run_tags("run-1", NOW, keys) == {"dfe-e2e": "run-1", "expires-at": str(NOW)}


def test_run_tfvar_carries_the_keys_the_module_reads() -> None:
    assert cloud_run.run_tfvar("run-1", NOW) == {
        "id": "run-1",
        "expires_at": NOW,
        "keys": {"run": "dfe-e2e", "expiry": "expires-at", "format": "iso8601"},
    }


# --- reading an expiry -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-08T12:00:00Z", NOW),
        ("2026-10-08T22:00:00+10:00", NOW),
        ("2026-10-08T12:00:00.500Z", NOW),
        (str(NOW), NOW),
        ("0", 0),
    ],
)
def test_both_expiry_forms_are_read(raw: str, expected: int) -> None:
    assert cloud_run.parse_expiry(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "yesterday", "2026-10-08T12:00:00", "2026-10-08", "-5", "1.5", " 17", FULLWIDTH_DIGITS, "17 "],
)
def test_expected_fail_a_malformed_expiry_reads_as_none(raw: str | None) -> None:
    """A naive timestamp names no instant, and full-width digits are not ASCII digits."""
    assert cloud_run.parse_expiry(raw) is None


# --- classify ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tags", "state"),
    [
        ({"dfe-e2e": "run-1", "expires-at": "2026-10-08T11:59:59Z"}, cloud_run.ExpiryState.EXPIRED),
        ({"dfe-e2e": "run-1", "expires-at": "2026-10-08T12:00:01Z"}, cloud_run.ExpiryState.LIVE),
        ({"dfe-e2e": "run-1"}, cloud_run.ExpiryState.NO_EXPIRY),
        ({"dfe-e2e": "run-1", "expires-at": "soon"}, cloud_run.ExpiryState.MALFORMED),
        ({"expires-at": "1"}, cloud_run.ExpiryState.UNTAGGED),
        ({"dfe-e2e": "", "expires-at": "1"}, cloud_run.ExpiryState.UNTAGGED),
        ({}, cloud_run.ExpiryState.UNTAGGED),
    ],
)
def test_classify_names_why_a_resource_is_or_is_not_expired(tags: dict, state: cloud_run.ExpiryState) -> None:
    assert cloud_run.classify(tags, now=NOW, grace=0) is state


def test_the_grace_boundary_is_strict() -> None:
    """At exactly expiry + grace the run is still inside its margin."""
    def at(offset: int) -> cloud_run.ExpiryState:
        tags = {"dfe-e2e": "run-1", "expires-at": str(NOW - 3600 + offset)}
        return cloud_run.classify(tags, now=NOW, grace=3600)

    assert at(0) is cloud_run.ExpiryState.LIVE
    assert at(-1) is cloud_run.ExpiryState.EXPIRED
    assert at(1) is cloud_run.ExpiryState.LIVE


# --- portable values -----------------------------------------------------------------


@pytest.mark.parametrize("run_id", ["run-1", "r20261008t120000z-0a1b2c", "a" * 63, "under_score"])
def test_portable_run_ids_are_accepted(run_id: str) -> None:
    assert cloud_run.validate_run_id(run_id) == run_id


@pytest.mark.parametrize("run_id", ["", "Run-1", "a" * 64, "run:1", "run/1", "run 1", "run.1"])
def test_expected_fail_a_run_id_some_cloud_refuses_is_refused(run_id: str) -> None:
    with pytest.raises(cloud_run.RunTagError):
        cloud_run.validate_run_id(run_id)


def test_a_minted_run_id_is_portable_and_unique() -> None:
    first, second = cloud_run.new_run_id(NOW), cloud_run.new_run_id(NOW)
    assert cloud_run.validate_run_id(first) == first
    assert first.startswith("r20261008t120000z-")
    assert first != second


@pytest.mark.parametrize(
    "kwargs",
    [{"run": "1run"}, {"run": "Run"}, {"expiry": "dfe-e2e"}, {"expiry_format": "rfc2822"}],
)
def test_expected_fail_unportable_keys_or_formats_are_refused(kwargs: dict) -> None:
    with pytest.raises(cloud_run.RunTagError):
        cloud_run.RunTagKeys(**kwargs)


def test_keys_and_format_come_from_the_environment() -> None:
    keys = cloud_run.RunTagKeys.from_env(
        {"DFE_RUN_TAG_KEY": "ci-run", "DFE_RUN_EXPIRY_KEY": "ci-expiry", "DFE_RUN_EXPIRY_FORMAT": "epoch"}
    )
    assert (keys.run, keys.expiry, keys.expiry_format) == ("ci-run", "ci-expiry", "epoch")
    assert cloud_run.RunTagKeys.from_env({}) == cloud_run.RunTagKeys()


@pytest.mark.parametrize(("raw", "seconds"), [("90", 90), ("90s", 90), ("15m", 900), ("3h", 10800), ("1d", 86400)])
def test_durations_parse(raw: str, seconds: int) -> None:
    assert cloud_run.parse_duration(raw) == seconds


@pytest.mark.parametrize("raw", ["1.5h", "-1h", "h", "1w", "", "an hour"])
def test_expected_fail_a_malformed_duration_is_refused(raw: str) -> None:
    with pytest.raises(cloud_run.RunTagError):
        cloud_run.parse_duration(raw)


def test_a_negative_expiry_is_refused() -> None:
    with pytest.raises(cloud_run.RunTagError):
        cloud_run.run_tags("run-1", -1)


# --- per-run state and the run record --------------------------------------------------


def test_each_run_gets_its_own_state_and_record_keys() -> None:
    assert cloud_run.state_key("dfe-e2e-runs", "run-1") == "dfe-e2e-runs/run-1/terraform.tfstate"
    assert cloud_run.record_key("dfe-e2e-runs", "run-1") == "dfe-e2e-runs/run-1/run.json"
    assert cloud_run.state_key("a/b", "run-1") != cloud_run.state_key("a/b", "run-2")


@pytest.mark.parametrize("prefix", ["/runs", "runs/", "a/../b", "", "runs//x", "runs x"])
def test_expected_fail_a_state_prefix_that_is_not_a_plain_key_path_is_refused(prefix: str) -> None:
    with pytest.raises(cloud_run.RunTagError):
        cloud_run.validate_state_prefix(prefix)


def _record(**changes: object) -> dict:
    state = {"bucket": "example-state", "region": "us-west-2", "key": "dfe-e2e-runs/run-1/terraform.tfstate"}
    record = cloud_run.build_record(
        run_id="run-1",
        expires_at=NOW,
        keys=KEYS,
        tf_root="terraform/environments/aws",
        state=state,
        tfvars={"state": state, "run": cloud_run.run_tfvar("run-1", NOW), "tags": {"lifecycle": "ephemeral"}},
    )
    record.update(changes)
    return json.loads(json.dumps(record))


def test_a_well_formed_record_has_no_problems() -> None:
    assert cloud_run.record_problems(_record(), bucket="example-state", prefix="dfe-e2e-runs") == []


def test_expected_fail_a_record_pointing_at_another_state_is_refused() -> None:
    """A record names the state a destroy runs against, so it must be its own."""
    elsewhere = {"bucket": "example-state", "region": "us-west-2", "key": "production/terraform.tfstate"}
    record = _record(state=elsewhere)
    record["tfvars"]["state"] = elsewhere
    problems = cloud_run.record_problems(record, bucket="example-state", prefix="dfe-e2e-runs")
    assert any("own key" in p for p in problems)


def test_expected_fail_a_record_from_another_bucket_is_refused() -> None:
    problems = cloud_run.record_problems(_record(), bucket="some-other-bucket", prefix="dfe-e2e-runs")
    assert problems


def test_expected_fail_a_record_for_a_persistent_deployment_is_refused() -> None:
    record = _record()
    record["tfvars"]["tags"] = {"lifecycle": "persistent"}
    problems = cloud_run.record_problems(record, bucket="example-state", prefix="dfe-e2e-runs")
    assert any("ephemeral" in p for p in problems)


@pytest.mark.parametrize("tf_root", ["terraform/environments/../../etc", "/abs/path", "scripts", "terraform/environments/AWS"])
def test_expected_fail_a_record_whose_root_is_not_a_repo_root_is_refused(tf_root: str) -> None:
    problems = cloud_run.record_problems(_record(tf_root=tf_root), bucket="example-state", prefix="dfe-e2e-runs")
    assert any("tf_root" in p for p in problems)


def test_expected_fail_a_record_whose_run_variable_names_another_run_is_refused() -> None:
    record = _record()
    record["tfvars"]["run"]["id"] = "run-2"
    problems = cloud_run.record_problems(record, bucket="example-state", prefix="dfe-e2e-runs")
    assert any("run.id" in p for p in problems)


def test_expected_fail_an_unknown_schema_or_bad_run_id_is_refused() -> None:
    assert cloud_run.record_problems(_record(schema=99), bucket="example-state", prefix="dfe-e2e-runs")
    assert cloud_run.record_problems(_record(run_id="../x"), bucket="example-state", prefix="dfe-e2e-runs")


# --- the CLI ---------------------------------------------------------------------------


def test_mint_tfvars_prints_the_run_variable(capsys: pytest.CaptureFixture) -> None:
    assert cloud_run.main(["mint", "--ttl", "3h", "--run-id", "run-1", "--format", "tfvars"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["run"]["id"] == "run-1"
    assert body["run"]["keys"]["format"] == "iso8601"


def test_mint_refuses_an_unportable_run_id(capsys: pytest.CaptureFixture) -> None:
    assert cloud_run.main(["mint", "--ttl", "3h", "--run-id", "Run-1"]) == 2
    assert "not portable" in capsys.readouterr().err
