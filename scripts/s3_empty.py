#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/s3_empty.py
#  Purpose:      Empty a deployment's force_destroy S3 buckets with parallel
#                DeleteObjects calls before tofu destroy deletes them.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Empty a deployment's force_destroy S3 buckets before `tofu destroy` deletes them.

tofu's force_destroy empties a bucket one batch at a time, and a ClickHouse object
store holds hundreds of objects per part, so the delete alone can outrun a cloud
cycle's run length. This lists every object version and delete marker and sends
the DeleteObjects calls, 1000 keys each, from a pool of workers. A listing is the
critical path, so the key space under the bucket's stem prefix is split into disjoint
ranges that list in parallel. A bucket tofu would keep (force_destroy false) is never touched.
"""

import json
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from subprocess import CompletedProcess

import aws_cli

BATCH = 1000  # DeleteObjects' own ceiling
WORKERS = 16
CALL_TIMEOUT = 120.0
STEM_DEPTH = 4  # single-prefix levels to descend
DISCOVERY_PAGES = 30  # a stem with more pages than this is listed as one range

type Runner = Callable[..., CompletedProcess[str]]


class EmptyError(RuntimeError):
    """A listing or a delete failed, so the bucket may still hold objects."""


def force_destroy_buckets(state: dict) -> list[str]:
    """The names of the aws_s3_bucket resources in `tofu show -json` state that set force_destroy.

    Args:
        state: The parsed output of `tofu show -json`.

    Returns:
        Bucket names, in state order.
    """
    names: list[str] = []
    pending = [state.get("values", {}).get("root_module", {})]
    while pending:
        module = pending.pop(0)
        for resource in module.get("resources", []):
            values = resource.get("values") or {}
            if (
                resource.get("type") == "aws_s3_bucket"
                and values.get("force_destroy")
                and values.get("bucket")
            ):
                names.append(values["bucket"])
        pending.extend(module.get("child_modules", []))
    return names


def _list_page(
    bucket: str,
    region: str,
    run: Runner,
    *,
    prefix: str = "",
    delimiter: bool = False,
    key_marker: str = "",
    version_marker: str = "",
) -> dict:
    """One list-object-versions call, at most BATCH entries, resumed from the markers."""
    args = [
        "s3api",
        "list-object-versions",
        "--bucket",
        bucket,
        "--region",
        region,
        "--no-paginate",
        "--max-keys",
        str(BATCH),
        "--output",
        "json",
    ]
    if prefix:
        args += ["--prefix", prefix]
    if delimiter:
        args += ["--delimiter", "/"]
    if key_marker:
        args.append(f"--key-marker={key_marker}")
    if version_marker:
        args.append(f"--version-id-marker={version_marker}")
    listed = run(args, timeout=CALL_TIMEOUT)
    if listed.returncode != 0:
        raise EmptyError(f"listing s3://{bucket} failed: {listed.stderr.strip()[:300]}")
    return json.loads(listed.stdout or "{}")


def _stem(bucket: str, region: str, run: Runner) -> tuple[str, dict]:
    """The deepest prefix that is the bucket's only content, and its delimiter listing.

    ClickHouse's object disk writes `<endpoint path>/<3 letters>/<29 letters>`, so
    the stem is the endpoint path and the level under it fans out.
    """
    prefix = ""
    page = _list_page(bucket, region, run, delimiter=True)
    for _ in range(STEM_DEPTH):
        prefixes = page.get("CommonPrefixes", [])
        sole_prefix = (
            len(prefixes) == 1
            and not page.get("Versions")
            and not page.get("DeleteMarkers")
            and not page.get("IsTruncated")
        )
        if not sole_prefix:
            break
        prefix = prefixes[0]["Prefix"]
        page = _list_page(bucket, region, run, prefix=prefix, delimiter=True)
    return prefix, page


def _prefixes(
    bucket: str, region: str, run: Runner, stem: str, first: dict
) -> list[str] | None:
    """The sorted prefixes directly under `stem`, or None past DISCOVERY_PAGES pages."""
    prefixes: list[str] = []
    page = first
    fetched = 1
    while True:
        prefixes.extend(entry["Prefix"] for entry in page.get("CommonPrefixes", []))
        if not page.get("IsTruncated"):
            return prefixes
        if fetched == DISCOVERY_PAGES:
            return None
        page = _list_page(
            bucket,
            region,
            run,
            prefix=stem,
            delimiter=True,
            key_marker=page.get("NextKeyMarker", ""),
            version_marker=page.get("NextVersionIdMarker", ""),
        )
        fetched += 1


def _bounds(prefixes: list[str] | None, count: int) -> list[tuple[str, str | None]]:
    """Up to `count` disjoint (after, through) key bounds that together cover every key under the stem.

    A range starting at prefix P is bounded by the prefix before it plus U+10FFFD, so
    the bounds ascend strictly and every key under P sorts above its bound.
    """
    if not prefixes or len(prefixes) < 2:
        return [("", None)]
    ranges = min(count, len(prefixes))
    starts = [index * len(prefixes) // ranges for index in range(ranges)]
    marks = ["", *(prefixes[start - 1] + "\U0010fffd" for start in starts[1:])]
    return list(zip(marks, [*marks[1:], None], strict=True))


def _list_range(
    bucket: str,
    region: str,
    run: Runner,
    stem: str,
    bounds: tuple[str, str | None],
    submit: Callable[[list[dict[str, str]]], None],
    stop: threading.Event,
) -> None:
    """Page the keys of `stem` after `bounds[0]` up to and including `bounds[1]` into `submit`."""
    key_marker, through = bounds
    version_marker = ""
    while not stop.is_set():
        page = _list_page(
            bucket,
            region,
            run,
            prefix=stem,
            key_marker=key_marker,
            version_marker=version_marker,
        )
        entries = [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
        keys = [
            {"Key": entry["Key"], "VersionId": entry["VersionId"]}
            for entry in entries
            if through is None or entry["Key"] <= through
        ]
        if keys:
            submit(keys)
        past_range = len(keys) < len(entries)
        if past_range or not page.get("IsTruncated"):
            return
        key_marker = page.get("NextKeyMarker", "")
        version_marker = page.get("NextVersionIdMarker", "")


def _delete(bucket: str, region: str, keys: list[dict[str, str]], run: Runner) -> int:
    """One DeleteObjects call for `keys`, which the CLI reads from a file because 1000 keys outgrow an argument."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as request:
        json.dump({"Objects": keys, "Quiet": True}, request)
        request.flush()
        deleted = run(
            [
                "s3api",
                "delete-objects",
                "--bucket",
                bucket,
                "--region",
                region,
                "--delete",
                Path(request.name).as_uri(),
                "--output",
                "json",
            ],
            timeout=CALL_TIMEOUT,
        )
    if deleted.returncode != 0:
        raise EmptyError(
            f"deleting from s3://{bucket} failed: {deleted.stderr.strip()[:300]}"
        )
    errors = json.loads(deleted.stdout or "{}").get("Errors", [])
    if errors:
        first = errors[0]
        raise EmptyError(
            f"s3://{bucket} refused {len(errors)} of {len(keys)} deletes, first "
            f"{first.get('Key')}: {first.get('Code')} {first.get('Message')}"
        )
    return len(keys)


def empty_bucket(
    bucket: str, region: str, *, run: Runner = aws_cli.run_aws, workers: int = WORKERS
) -> int:
    """Delete every object version and delete marker in `bucket`.

    Args:
        bucket: The bucket name.
        region: Its region.
        run: Runs `aws <args>`, as aws_cli.run_aws does.
        workers: Key ranges listed at once, and DeleteObjects calls in flight at once.

    Returns:
        How many versions and markers were deleted.

    Raises:
        EmptyError: A listing or a delete failed.
    """
    stem, first = _stem(bucket, region, run)
    bounds = _bounds(_prefixes(bucket, region, run, stem, first), workers)
    deletes: list[Future[int]] = []
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=workers) as deleters:

        def submit(keys: list[dict[str, str]]) -> None:
            deletes.append(deleters.submit(_delete, bucket, region, keys, run))

        with ThreadPoolExecutor(max_workers=len(bounds)) as listers:
            listings = [
                listers.submit(
                    _list_range, bucket, region, run, stem, bound, submit, stop
                )
                for bound in bounds
            ]
            try:
                for listing in as_completed(listings):
                    listing.result()
            except BaseException:
                stop.set()
                raise
        return sum(delete.result() for delete in deletes)
