#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/cloud_run.py
#  Purpose:      The run identity and expiry tags every cloud test run carries,
#                and the run record its reaper reads, defined once for
#                cloud_sweep.py, the guarded cloud cycle and cloud_reaper.py.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""cloud_run -- which test run created a cloud resource, and when it may go.

Every resource an unattended test run creates carries two tags:

    dfe-e2e=<run id>                   the run that made it
    expires-at=2026-10-08T12:00:00Z    when that run's teardown should have finished

Both keys are configurable (`DFE_RUN_TAG_KEY`, `DFE_RUN_EXPIRY_KEY`). A run id
follows the strictest of the three clouds -- a GCP label allows only lowercase
letters, digits, `_` and `-`, at most 63 characters. The expiry is written as
ISO-8601 UTC by default, the form an AWS account guardrail reads, or as epoch
seconds (`DFE_RUN_EXPIRY_FORMAT=epoch-seconds`), which is the only form a GCP label can
hold. Either form is READ wherever it is found.

A resource is EXPIRED only when it carries a run tag, its expiry parses, and
expiry plus grace is in the past. A missing or malformed value is never
expired: a reaper removes what it can prove is stale and nothing else.

A run that provisions through OpenTofu also leaves a RUN RECORD beside its
state, `<prefix>/<run id>/run.json`, holding the variables a later `tofu
destroy` needs. `record_problems` is the check a reaper runs before it trusts
one, because a record names a state to destroy.

    python3 scripts/cloud_run.py mint --ttl 3h
    python3 scripts/cloud_run.py mint --ttl 3h --format tfvars
"""

import argparse
import json
import os
import re
import secrets
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

DEFAULT_RUN_KEY = "dfe-e2e"
DEFAULT_EXPIRY_KEY = "expires-at"
RUN_KEY_ENV = "DFE_RUN_TAG_KEY"
EXPIRY_KEY_ENV = "DFE_RUN_EXPIRY_KEY"

# How an expiry is WRITTEN, by the names the account guardrails and their sweeper use.
# Both forms are always read.
EXPIRY_FORMATS = ("iso8601", "epoch-seconds")
DEFAULT_EXPIRY_FORMAT = "iso8601"
EXPIRY_FORMAT_ENV = "DFE_RUN_EXPIRY_FORMAT"
_ISO_WRITE = "%Y-%m-%dT%H:%M:%SZ"
# An explicit zone is required: a naive timestamp names no instant.
_ISO_READ = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$")

# Where per-run state and run records live inside the state bucket.
DEFAULT_STATE_PREFIX = "dfe-e2e-runs"
STATE_PREFIX_ENV = "DFE_RUN_STATE_PREFIX"
STATE_OBJECT = "terraform.tfstate"
RECORD_OBJECT = "run.json"
RECORD_SCHEMA = 1

# GCP's label rules, the narrowest of the three clouds: a key starts with a letter.
_TAG_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_RUN_ID = re.compile(r"^[a-z0-9_-]{1,63}$")
_STATE_PREFIX = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
_TF_ROOT = re.compile(r"^terraform/environments/[a-z0-9-]+$")
_DURATION = re.compile(r"^(\d+)([smhd]?)$")
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


class RunTagError(ValueError):
    """A run id, tag key, duration or state prefix the convention refuses."""


@dataclass(frozen=True, slots=True)
class RunTagKeys:
    """The two tag keys a run writes and the form its expiry is written in."""

    run: str = DEFAULT_RUN_KEY
    expiry: str = DEFAULT_EXPIRY_KEY
    expiry_format: str = DEFAULT_EXPIRY_FORMAT

    def __post_init__(self) -> None:
        for key in (self.run, self.expiry):
            if not _TAG_KEY.match(key):
                raise RunTagError(
                    f"tag key {key!r} is not portable: start with a lowercase letter, then "
                    "lowercase letters, digits, '_' or '-', at most 63 characters"
                )
        if self.run == self.expiry:
            raise RunTagError(f"the run and expiry tag keys are both {self.run!r}")
        if self.expiry_format not in EXPIRY_FORMATS:
            raise RunTagError(
                f"expiry format {self.expiry_format!r} is not one of {', '.join(EXPIRY_FORMATS)}"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RunTagKeys:
        """Read DFE_RUN_TAG_KEY, DFE_RUN_EXPIRY_KEY and DFE_RUN_EXPIRY_FORMAT, else the defaults.

        Args:
            env: The environment to read; the process environment when None.

        Returns:
            The validated keys and format.
        """
        source = os.environ if env is None else env
        return cls(
            run=source.get(RUN_KEY_ENV) or DEFAULT_RUN_KEY,
            expiry=source.get(EXPIRY_KEY_ENV) or DEFAULT_EXPIRY_KEY,
            expiry_format=source.get(EXPIRY_FORMAT_ENV) or DEFAULT_EXPIRY_FORMAT,
        )

    def as_tfvar(self) -> dict[str, str]:
        """The `keys` block of the OpenTofu `run` variable (terraform/modules/tf-run-tags)."""
        return {"run": self.run, "expiry": self.expiry, "format": self.expiry_format}


class ExpiryState(StrEnum):
    """What a resource's tags say about whether its run is over."""

    UNTAGGED = "untagged"
    NO_EXPIRY = "no-expiry"
    MALFORMED = "malformed-expiry"
    LIVE = "live"
    EXPIRED = "expired"


def validate_run_id(run_id: str) -> str:
    """Return *run_id* unchanged, or raise RunTagError when a cloud would refuse it."""
    if not _RUN_ID.match(run_id):
        raise RunTagError(
            f"run id {run_id!r} is not portable: lowercase letters, digits, '_' or '-', "
            "1 to 63 characters"
        )
    return run_id


def new_run_id(now: float | None = None) -> str:
    """Mint a run id that sorts by start time and is unique across concurrent runs."""
    moment = time.gmtime(time.time() if now is None else now)
    return f"r{time.strftime('%Y%m%dt%H%M%Sz', moment)}-{secrets.token_hex(3)}"


def format_expiry(expires_at: int, expiry_format: str = DEFAULT_EXPIRY_FORMAT) -> str:
    """Write epoch seconds as an expiry tag value: ISO-8601 UTC, or plain digits."""
    if expiry_format == "epoch-seconds":
        return str(int(expires_at))
    if expiry_format == "iso8601":
        return datetime.fromtimestamp(int(expires_at), UTC).strftime(_ISO_WRITE)
    raise RunTagError(f"expiry format {expiry_format!r} is not one of {', '.join(EXPIRY_FORMATS)}")


def run_tags(run_id: str, expires_at: int, keys: RunTagKeys | None = None) -> dict[str, str]:
    """The two tags a run's resources carry.

    Args:
        run_id: The run, checked by validate_run_id.
        expires_at: Epoch seconds after which the run's resources are stale.
        keys: The tag keys and expiry format; the defaults when None.

    Returns:
        ``{run key: run id, expiry key: the expiry in the configured format}``.

    Raises:
        RunTagError: The run id is not portable, or the expiry is negative.
    """
    keys = keys or RunTagKeys()
    if expires_at < 0:
        raise RunTagError(f"expires-at {expires_at} is before the epoch")
    return {
        keys.run: validate_run_id(run_id),
        keys.expiry: format_expiry(expires_at, keys.expiry_format),
    }


def parse_expiry(raw: str | None) -> int | None:
    """Epoch seconds from an expiry tag value in either form, or None for anything else.

    Plain ASCII digits are epoch seconds. Otherwise the value must be an
    ISO-8601 timestamp with an explicit zone (`Z` or `+hh:mm`); a naive one is
    malformed, because it names no instant.
    """
    if raw is None or not raw.isascii():
        return None
    if raw.isdigit():
        return int(raw)
    if not _ISO_READ.match(raw):
        return None
    try:
        return int(datetime.fromisoformat(raw).timestamp())
    except ValueError:
        return None


def classify(
    tags: Mapping[str, str], *, now: float, grace: int, keys: RunTagKeys | None = None
) -> ExpiryState:
    """Say whether a resource with these tags belongs to a run that is over.

    Args:
        tags: The resource's tags, key to value.
        now: The current time in epoch seconds.
        grace: Seconds past expires-at before the resource counts as expired.
        keys: The tag keys; the defaults when None.

    Returns:
        EXPIRED only when the run tag is present, the expiry parses and
        expiry + grace is strictly before *now*; every other case says why not.
    """
    keys = keys or RunTagKeys()
    if not tags.get(keys.run):
        return ExpiryState.UNTAGGED
    raw = tags.get(keys.expiry)
    if raw is None:
        return ExpiryState.NO_EXPIRY
    expires_at = parse_expiry(raw)
    if expires_at is None:
        return ExpiryState.MALFORMED
    return ExpiryState.EXPIRED if now > expires_at + grace else ExpiryState.LIVE


def run_tfvar(run_id: str, expires_at: int, keys: RunTagKeys | None = None) -> dict[str, object]:
    """The OpenTofu `run` variable that renders these same tags (terraform/modules/tf-run-tags)."""
    keys = keys or RunTagKeys()
    return {"id": validate_run_id(run_id), "expires_at": int(expires_at), "keys": keys.as_tfvar()}


def parse_duration(raw: str) -> int:
    """Seconds from ``90``, ``90s``, ``15m``, ``3h`` or ``1d``.

    Raises:
        RunTagError: Anything else, including a negative or fractional value.
    """
    match = _DURATION.match(raw.strip())
    if match is None:
        raise RunTagError(f"duration {raw!r} is not N, Ns, Nm, Nh or Nd")
    return int(match[1]) * _DURATION_UNITS[match[2]]


# --- per-run state and the run record ------------------------------------------


def validate_state_prefix(prefix: str) -> str:
    """Return *prefix* unchanged, or raise RunTagError for one that is not a plain key path."""
    if not _STATE_PREFIX.match(prefix) or ".." in prefix.split("/"):
        raise RunTagError(
            f"state prefix {prefix!r} must be '/'-separated segments of letters, digits, "
            "'.', '_' or '-', with no leading or trailing '/'"
        )
    return prefix


def state_key(prefix: str, run_id: str) -> str:
    """The state object a run's OpenTofu backend writes, one per run id."""
    return f"{validate_state_prefix(prefix)}/{validate_run_id(run_id)}/{STATE_OBJECT}"


def record_key(prefix: str, run_id: str) -> str:
    """The run record's object key, beside that run's state."""
    return f"{validate_state_prefix(prefix)}/{validate_run_id(run_id)}/{RECORD_OBJECT}"


def build_record(
    *,
    run_id: str,
    expires_at: int,
    keys: RunTagKeys,
    tf_root: str,
    state: Mapping[str, str],
    tfvars: Mapping[str, object],
) -> dict[str, object]:
    """The run record a reaper needs to destroy this run's state without its operator.

    Args:
        run_id: The run.
        expires_at: Epoch seconds the run is due to be gone by.
        keys: The tag keys the run wrote.
        tf_root: The OpenTofu root, relative to the repository root.
        state: The run's backend: bucket, region and its per-run key.
        tfvars: Every variable the run applied with, the overlay included.

    Returns:
        The record, ready for json.dumps.
    """
    return {
        "schema": RECORD_SCHEMA,
        "run_id": validate_run_id(run_id),
        "expires_at": int(expires_at),
        "keys": keys.as_tfvar(),
        "tf_root": tf_root,
        "state": dict(state),
        "tfvars": dict(tfvars),
    }


def record_problems(record: Mapping[str, object], *, bucket: str, prefix: str) -> list[str]:
    """Every reason a reaper must not act on this run record; empty means trust it.

    A record names the state a `tofu destroy` runs against, so one that points
    anywhere but its own per-run key, or at a deployment that is not ephemeral,
    is refused rather than followed.

    Args:
        record: The parsed run.json.
        bucket: The bucket it was read from.
        prefix: The per-run prefix it was listed under.

    Returns:
        One line per problem.
    """
    problems: list[str] = []
    if record.get("schema") != RECORD_SCHEMA:
        problems.append(f"schema {record.get('schema')!r} is not {RECORD_SCHEMA}")
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
        return [*problems, f"run_id {run_id!r} is not a portable run id"]
    if not isinstance(record.get("expires_at"), int) or isinstance(record.get("expires_at"), bool):
        problems.append("expires_at is not an integer")
    tf_root = record.get("tf_root")
    if not isinstance(tf_root, str) or not _TF_ROOT.match(tf_root):
        problems.append(f"tf_root {tf_root!r} is not terraform/environments/<root>")
    expected_state = {"bucket": bucket, "key": state_key(prefix, run_id)}
    state = record.get("state")
    if not isinstance(state, Mapping) or any(state.get(k) != v for k, v in expected_state.items()):
        problems.append(f"state {state!r} is not this run's own key {expected_state}")
    tfvars = record.get("tfvars")
    if not isinstance(tfvars, Mapping):
        return [*problems, "tfvars is not an object"]
    if tfvars.get("state") != state:
        problems.append("tfvars.state differs from the record's state")
    run_var = tfvars.get("run")
    if not isinstance(run_var, Mapping) or run_var.get("id") != run_id:
        problems.append("tfvars.run.id differs from the record's run_id")
    tags = tfvars.get("tags")
    if not isinstance(tags, Mapping) or tags.get("lifecycle") != "ephemeral":
        problems.append("tfvars.tags.lifecycle is not ephemeral")
    return problems


# --- CLI -------------------------------------------------------------------------


def _mint(args: argparse.Namespace) -> int:
    keys = RunTagKeys.from_env()
    run_id = validate_run_id(args.run_id) if args.run_id else new_run_id()
    expires_at = int(time.time()) + parse_duration(args.ttl)
    tags = run_tags(run_id, expires_at, keys)
    if args.format == "env":
        print(f"DFE_RUN_ID={run_id}")
        print(f"DFE_RUN_EXPIRES_AT={expires_at}")
    elif args.format == "tfvars":
        print(json.dumps({"run": run_tfvar(run_id, expires_at, keys)}, indent=2))
    else:
        print(json.dumps({"run_id": run_id, "expires_at": expires_at, "tags": tags}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The cloud_run CLI: mint a run id and its expiry."""
    parser = argparse.ArgumentParser(description="The run identity and expiry tags a cloud test run carries.")
    sub = parser.add_subparsers(dest="command", required=True)
    mint = sub.add_parser("mint", help="mint a run id and the tags its resources carry")
    mint.add_argument("--ttl", required=True, help="how long until the run's resources are stale: 90, 15m, 3h, 1d")
    mint.add_argument("--run-id", default=None, help="use this run id instead of minting one")
    mint.add_argument(
        "--format",
        choices=["json", "env", "tfvars"],
        default="json",
        help="json: id, expiry and tags; env: DFE_RUN_* lines; tfvars: the OpenTofu `run` variable",
    )
    mint.set_defaults(func=_mint)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; exit 2 on a value the convention refuses."""
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RunTagError as exc:
        print(f"cloud_run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
