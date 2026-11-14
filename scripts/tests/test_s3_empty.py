#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_s3_empty.py
#  Purpose:      Guard the teardown's bucket emptying: only force_destroy buckets
#                are picked from state, every version and delete marker on every
#                page is deleted 1000 keys a call, and a failed list or delete
#                raises rather than reporting the bucket empty.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for `s3_empty.py`. A fake `aws` answers with the s3api JSON shapes, so no account is touched."""

import json
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import s3_empty  # noqa: E402


def _bucket(address: str, name: str, force_destroy: bool) -> dict:
    return {
        "address": address,
        "type": "aws_s3_bucket",
        "values": {"bucket": name, "force_destroy": force_destroy},
    }


STATE = {
    "values": {
        "root_module": {
            "resources": [
                _bucket("aws_s3_bucket.logs", "logs", True),
                {
                    "address": "aws_sqs_queue.q",
                    "type": "aws_sqs_queue",
                    "values": {"force_destroy": True},
                },
            ],
            "child_modules": [
                {
                    "resources": [
                        _bucket(
                            "module.cluster.aws_s3_bucket.retained[0]", "kept", False
                        )
                    ],
                    "child_modules": [
                        {
                            "resources": [
                                _bucket(
                                    "module.cluster.module.ch.aws_s3_bucket.ephemeral[0]",
                                    "ch-objects",
                                    True,
                                )
                            ]
                        }
                    ],
                }
            ],
        }
    }
}


@pytest.mark.parametrize(
    ("state", "want"),
    [
        pytest.param(
            STATE, ["logs", "ch-objects"], id="nested-modules-force-destroy-only"
        ),
        pytest.param({}, [], id="empty-state"),
        pytest.param({"values": {"root_module": {}}}, [], id="no-resources"),
    ],
)
def test_force_destroy_buckets(state: dict, want: list[str]) -> None:
    assert s3_empty.force_destroy_buckets(state) == want


class FakeS3:
    """Answers list-object-versions from `pages` and records each delete-objects request."""

    def __init__(
        self,
        pages: list[dict],
        *,
        list_fails: bool = False,
        delete_errors: bool = False,
    ) -> None:
        self.pages = pages
        self.list_fails = list_fails
        self.delete_errors = delete_errors
        self.deleted: list[list[dict]] = []
        self.lock = threading.Lock()

    def __call__(
        self, args: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        assert timeout == s3_empty.CALL_TIMEOUT
        if args[1] == "list-object-versions":
            if self.list_fails:
                return subprocess.CompletedProcess(args, 255, "", "AccessDenied")
            index = (
                int(args[args.index("--starting-token") + 1])
                if "--starting-token" in args
                else 0
            )
            page = dict(self.pages[index])
            if index + 1 < len(self.pages):
                page["NextToken"] = str(index + 1)
            return subprocess.CompletedProcess(args, 0, json.dumps(page), "")
        request = json.loads(
            Path(urlparse(args[args.index("--delete") + 1]).path).read_text(
                encoding="utf-8"
            )
        )
        assert request["Quiet"] is True
        with self.lock:
            self.deleted.append(request["Objects"])
        errors = [
            {
                "Key": request["Objects"][0]["Key"],
                "Code": "AccessDenied",
                "Message": "no",
            }
        ]
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"Errors": errors} if self.delete_errors else {}), ""
        )


def _versions(start: int, count: int) -> list[dict]:
    return [{"Key": f"k{n}", "VersionId": f"v{n}"} for n in range(start, start + count)]


def test_every_version_and_marker_on_every_page_is_deleted_in_batches() -> None:
    pages = [
        {"Versions": _versions(0, 1000)},
        {"Versions": _versions(1000, 600), "DeleteMarkers": _versions(5000, 400)},
        {"DeleteMarkers": _versions(6000, 3)},
    ]
    s3 = FakeS3(pages)
    assert s3_empty.empty_bucket("b", "us-west-2", run=s3, workers=4) == 2003
    assert sorted(len(batch) for batch in s3.deleted) == [3, 1000, 1000]
    deleted = {entry["Key"] for batch in s3.deleted for entry in batch}
    assert {"k0", "k1599", "k5399", "k6002"} <= deleted


def test_an_empty_bucket_sends_no_delete() -> None:
    s3 = FakeS3([{}])
    assert s3_empty.empty_bucket("b", "us-west-2", run=s3) == 0
    assert s3.deleted == []


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        pytest.param(
            FakeS3([{}], list_fails=True),
            "listing s3://b failed: AccessDenied",
            id="list-refused",
        ),
        pytest.param(
            FakeS3([{"Versions": _versions(0, 5)}], delete_errors=True),
            "s3://b refused 1 of 5 deletes, first k0: AccessDenied no",
            id="delete-errors",
        ),
    ],
)
def test_a_failure_raises_rather_than_reporting_empty(
    fake: FakeS3, message: str
) -> None:
    with pytest.raises(s3_empty.EmptyError, match=message):
        s3_empty.empty_bucket("b", "us-west-2", run=fake)
