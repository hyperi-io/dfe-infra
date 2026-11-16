#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_s3_empty.py
#  Purpose:      Guard the teardown's bucket emptying: only force_destroy buckets
#                are picked from state, every version and delete marker is listed
#                once across parallel key ranges and deleted 1000 keys a call, and
#                a failed list or delete raises rather than reporting the bucket empty.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for `s3_empty.py`. A fake `aws` answers with the s3api JSON shapes, so no account is touched."""

import itertools
import json
import math
import string
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


Entry = tuple[str, str, bool]  # key, version id, is a delete marker


class FakeS3:
    """Answers list-object-versions from a sorted key set and records each delete-objects request."""

    def __init__(
        self,
        entries: list[Entry],
        *,
        list_fails: bool = False,
        range_list_fails: bool = False,
        delete_errors: bool = False,
    ) -> None:
        self.entries = sorted(entries, key=lambda entry: entry[0])
        self.list_fails = list_fails
        self.range_list_fails = range_list_fails
        self.delete_errors = delete_errors
        self.deleted: list[list[dict]] = []
        self.list_calls: list[list[str]] = []
        self.lock = threading.Lock()

    def __call__(
        self, args: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        assert timeout == s3_empty.CALL_TIMEOUT
        if args[1] == "list-object-versions":
            return self._list(args)
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

    def _list(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        def option(name: str) -> str:
            for at, arg in enumerate(args):
                if arg == name:
                    return args[at + 1]
                if arg.startswith(f"{name}="):
                    return arg.split("=", 1)[1]
            return ""

        assert "--no-paginate" in args
        assert "--starting-token" not in args
        with self.lock:
            self.list_calls.append(args)
        range_call = "--delimiter" not in args and bool(option("--key-marker"))
        if self.list_fails or (self.range_list_fails and range_call):
            return subprocess.CompletedProcess(args, 255, "", "AccessDenied")
        prefix = option("--prefix")
        delimiter = option("--delimiter")
        key_marker = option("--key-marker")
        version_marker = option("--version-id-marker")
        max_keys = int(option("--max-keys"))
        versions: list[dict] = []
        markers: list[dict] = []
        prefixes: list[dict] = []
        count = 0
        last = ("", "")
        truncated = False
        skipping_versions = bool(key_marker and version_marker)
        seen: set[str] = set()
        for key, version_id, is_marker in self.entries:
            if not key.startswith(prefix) or key < key_marker:
                continue
            if key == key_marker:
                if not version_marker:
                    continue
                if skipping_versions:
                    skipping_versions = version_id != version_marker
                    continue
            rolled = ""
            if delimiter:
                if key_marker.endswith(delimiter) and key.startswith(key_marker):
                    continue
                rest = key[len(prefix) :]
                cut = rest.find(delimiter)
                rolled = prefix + rest[: cut + 1] if cut >= 0 else ""
            if rolled in seen:
                continue
            if count == max_keys:
                truncated = True
                break
            count += 1
            if rolled:
                seen.add(rolled)
                prefixes.append({"Prefix": rolled})
                last = (rolled, "")
            else:
                (markers if is_marker else versions).append(
                    {"Key": key, "VersionId": version_id}
                )
                last = (key, version_id)
        page: dict = {"IsTruncated": truncated}
        for name, found in (
            ("Versions", versions),
            ("DeleteMarkers", markers),
            ("CommonPrefixes", prefixes),
        ):
            if found:
                page[name] = found
        if truncated:
            page["NextKeyMarker"], page["NextVersionIdMarker"] = last
        return subprocess.CompletedProcess(args, 0, json.dumps(page), "")


def _versions(start: int, count: int, *, marker: bool = False) -> list[Entry]:
    return [(f"k{n}", f"v{n}", marker) for n in range(start, start + count)]


def _clickhouse_shaped(prefix_count: int, keys_each: int) -> list[Entry]:
    """`data/<3 letters>/<29 letters>` keys with extra versions, delete markers, a stem key and a bare prefix key."""
    letters = itertools.product(string.ascii_lowercase, repeat=3)
    prefixes = ["".join(next(letters)) for _ in range(prefix_count)]
    entries: list[Entry] = [("data/format.json", "f0", False)]
    for prefix in prefixes:
        entries.append((f"data/{prefix}/", f"{prefix}-dir", False))
        entries.append((f"data/{prefix}.", f"{prefix}-edge", False))
        for n in range(keys_each):
            key = f"data/{prefix}/{prefix}{n:026d}"
            entries.append((key, f"{prefix}-{n}-a", False))
            if n % 5 == 0:
                entries.append((key, f"{prefix}-{n}-b", False))
            if n % 7 == 0:
                entries.append((key, f"{prefix}-{n}-m", True))
    return entries


def _deleted(s3: FakeS3) -> list[tuple[str, str]]:
    return [
        (entry["Key"], entry["VersionId"]) for batch in s3.deleted for entry in batch
    ]


def _range_starts(s3: FakeS3) -> set[str]:
    """The boundary markers of the key ranges after the first, which all end in U+10FFFD."""
    markers = {
        arg.split("=", 1)[1]
        for call in s3.list_calls
        if "--delimiter" not in call
        for arg in call
        if arg.startswith("--key-marker=")
    }
    return {marker for marker in markers if marker.endswith("\U0010fffd")}


def test_dot_segment_prefixes_are_deleted_once() -> None:
    """Prefixes ending in a byte below "/" once gave descending bounds and double deletes."""
    stems = ["../", "./", "-x/", "a/", "b/", "c/"]
    entries: list[Entry] = []
    for stem in stems:
        for n in range(3):
            entries.append((f"data/{stem}k{n}", f"{stem}{n}", False))
        entries.append((f"data/{stem}/", f"{stem}slash", False))
    # Direct keys between "data/.." and "data/...", the window the old bounds overlapped.
    entries += [("data/..-edge", "e1", False), ("data/...", "e2", False)]
    s3 = FakeS3(entries)
    assert s3_empty.empty_bucket("b", "us-west-2", run=s3, workers=16) == len(entries)
    deleted = _deleted(s3)
    assert len(deleted) == len(set(deleted))
    assert sorted(deleted) == sorted((key, version) for key, version, _ in entries)


def test_a_flat_bucket_falls_back_to_one_range_and_deletes_in_batches() -> None:
    entries = [
        *_versions(0, 1600),
        *_versions(5000, 400, marker=True),
        *_versions(6000, 3),
    ]
    s3 = FakeS3(entries)
    assert s3_empty.empty_bucket("b", "us-west-2", run=s3, workers=4) == 2003
    assert sorted(len(batch) for batch in s3.deleted) == [3, 1000, 1000]
    assert sorted(_deleted(s3)) == sorted((key, version) for key, version, _ in entries)
    assert not _range_starts(s3)


@pytest.mark.parametrize(
    ("prefix_count", "keys_each", "workers", "ranges"),
    [
        pytest.param(300, 30, 4, 4, id="few-hundred-prefixes-four-ranges"),
        pytest.param(300, 30, 16, 16, id="few-hundred-prefixes-sixteen-ranges"),
        pytest.param(5, 3, 16, 5, id="fewer-prefixes-than-workers"),
        pytest.param(1, 3, 4, 1, id="single-prefix-one-range"),
    ],
)
def test_a_clickhouse_shaped_bucket_is_deleted_once_across_ranges(
    prefix_count: int, keys_each: int, workers: int, ranges: int
) -> None:
    entries = _clickhouse_shaped(prefix_count, keys_each)
    s3 = FakeS3(entries)
    assert s3_empty.empty_bucket("b", "us-west-2", run=s3, workers=workers) == len(
        entries
    )
    deleted = _deleted(s3)
    assert len(deleted) == len(set(deleted))
    assert sorted(deleted) == sorted((key, version) for key, version, _ in entries)
    assert ("data/format.json", "f0") in deleted
    assert ("data/aaa/", "aaa-dir") in deleted
    assert len(_range_starts(s3)) == ranges - 1


def test_parallel_listing_needs_fewer_sequential_calls_than_a_serial_listing() -> None:
    entries = _clickhouse_shaped(300, 30)
    s3 = FakeS3(entries)
    s3_empty.empty_bucket("b", "us-west-2", run=s3, workers=8)
    serial = math.ceil(len(entries) / s3_empty.BATCH)
    discovery = [call for call in s3.list_calls if "--delimiter" in call]
    longest_range = math.ceil(len(entries) / 8 / s3_empty.BATCH) + 1
    assert len(discovery) == 2
    assert len(discovery) + longest_range < serial


def test_a_stem_with_more_direct_keys_than_discovery_pages_is_one_range() -> None:
    direct = [(f"data/key{n:06d}", f"v{n}", False) for n in range(32_000)]
    entries = [*direct, ("data/aaa/x", "x0", False), ("data/bbb/x", "x1", False)]
    s3 = FakeS3(entries)
    assert s3_empty.empty_bucket("b", "us-west-2", run=s3, workers=4) == len(entries)
    assert len(_deleted(s3)) == len(set(_deleted(s3))) == len(entries)
    assert not _range_starts(s3)


def test_an_empty_bucket_sends_no_delete() -> None:
    s3 = FakeS3([])
    assert s3_empty.empty_bucket("b", "us-west-2", run=s3) == 0
    assert s3.deleted == []


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        pytest.param(
            FakeS3([], list_fails=True),
            "listing s3://b failed: AccessDenied",
            id="list-refused",
        ),
        pytest.param(
            FakeS3(_clickhouse_shaped(40, 5), range_list_fails=True),
            "listing s3://b failed: AccessDenied",
            id="one-range-list-refused",
        ),
        pytest.param(
            FakeS3(_versions(0, 5), delete_errors=True),
            "s3://b refused 1 of 5 deletes, first k0: AccessDenied no",
            id="delete-errors",
        ),
        pytest.param(
            FakeS3(_clickhouse_shaped(40, 5), delete_errors=True),
            "s3://b refused 1 of ",
            id="delete-errors-across-ranges",
        ),
    ],
)
def test_a_failure_raises_rather_than_reporting_empty(
    fake: FakeS3, message: str
) -> None:
    with pytest.raises(s3_empty.EmptyError, match=message):
        s3_empty.empty_bucket("b", "us-west-2", run=fake, workers=4)
