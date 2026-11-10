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
the DeleteObjects calls, 1000 keys each, from a pool of workers. A bucket tofu would
keep (force_destroy false) is never touched.
"""

import json
import tempfile
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from subprocess import CompletedProcess

import aws_cli

BATCH = 1000  # DeleteObjects' own ceiling
WORKERS = 16
CALL_TIMEOUT = 120.0

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
            if resource.get("type") == "aws_s3_bucket" and values.get("force_destroy") and values.get("bucket"):
                names.append(values["bucket"])
        pending.extend(module.get("child_modules", []))
    return names


def _keys(bucket: str, region: str, run: Runner) -> Iterator[list[dict[str, str]]]:
    """Every object version and delete marker in `bucket`, a page of at most BATCH at a time."""
    token = ""
    while True:
        args = ["s3api", "list-object-versions", "--bucket", bucket, "--region", region,
                "--max-items", str(BATCH), "--output", "json"]
        if token:
            args += ["--starting-token", token]
        listed = run(args, timeout=CALL_TIMEOUT)
        if listed.returncode != 0:
            raise EmptyError(f"listing s3://{bucket} failed: {listed.stderr.strip()[:300]}")
        page = json.loads(listed.stdout or "{}")
        keys = [{"Key": entry["Key"], "VersionId": entry["VersionId"]}
                for entry in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]]
        if keys:
            yield keys
        token = page.get("NextToken", "")
        if not token:
            return


def _delete(bucket: str, region: str, keys: list[dict[str, str]], run: Runner) -> int:
    """One DeleteObjects call for `keys`, which the CLI reads from a file because 1000 keys outgrow an argument."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as request:
        json.dump({"Objects": keys, "Quiet": True}, request)
        request.flush()
        deleted = run(["s3api", "delete-objects", "--bucket", bucket, "--region", region,
                       "--delete", Path(request.name).as_uri(), "--output", "json"], timeout=CALL_TIMEOUT)
    if deleted.returncode != 0:
        raise EmptyError(f"deleting from s3://{bucket} failed: {deleted.stderr.strip()[:300]}")
    errors = json.loads(deleted.stdout or "{}").get("Errors", [])
    if errors:
        first = errors[0]
        raise EmptyError(f"s3://{bucket} refused {len(errors)} of {len(keys)} deletes, first "
                         f"{first.get('Key')}: {first.get('Code')} {first.get('Message')}")
    return len(keys)


def empty_bucket(bucket: str, region: str, *, run: Runner = aws_cli.run_aws, workers: int = WORKERS) -> int:
    """Delete every object version and delete marker in `bucket`.

    Args:
        bucket: The bucket name.
        region: Its region.
        run: Runs `aws <args>`, as aws_cli.run_aws does.
        workers: DeleteObjects calls in flight at once.

    Returns:
        How many versions and markers were deleted.

    Raises:
        EmptyError: A listing or a delete failed.
    """
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures: list[Future[int]] = [pool.submit(_delete, bucket, region, keys, run)
                                      for keys in _keys(bucket, region, run)]
        return sum(future.result() for future in futures)
