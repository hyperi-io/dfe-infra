#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_clickhouse_server_log.py
#  Purpose:      Prove every ClickHouse and Keeper server the chart deploys
#                ships a bounded log at a production level, and that a level
#                the server would reject fails the render instead.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Assertions for clickhouse.serverLog and clickhouse.keeper.log.

Left unset, the operator writes a trace-level log of 50 files x 1000M onto each
server's data PVC and prints the same trace to stdout, where the otel collector
picks it up. A deploy with no traffic filled its disk that way (dfe-infra#371).
The render is the only place the bound can be checked before a cluster pays for
it, so each server's logger is read back out of what the chart emits.

    python3 scripts/tests/test_clickhouse_server_log.py

Needs `helm` on PATH.
"""

import re
import subprocess
import sys
from pathlib import Path

import yaml

from _charts import chart_dir
from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VALUES = REPO_ROOT / "argocd" / "values"
CHART = chart_dir("clickhouse-cluster")

CLUSTER_CASCADE = [VALUES / "common.yaml", VALUES / "local.yaml", VALUES / "profile-scale.yaml"]
SINGLE_CASCADE = [VALUES / "common.yaml", VALUES / "local.yaml", VALUES / "profile-single.yaml"]

# "A few hundred MB" per server, for the log and the err.log together.
CEILING_BYTES = 512 * 1024**2
SHIPPED_LEVEL = "information"
# What an unset logger means, per server, for the failure message.
OPERATOR_DEFAULT = "the operator's applies: trace, 50 files of 1000M, on the data PVC"
IMAGE_DEFAULT = "the image's config.xml applies: trace, 10 files of 1000M, on the node's disk"
SINGLE_LOGGER_PATH = "/etc/clickhouse-server/config.d/dfe-logger.yaml"
_SIZE = re.compile(r"^\s*(\d+)\s*([KM]?)\s*$")
_UNIT = {"": 1, "K": 1024, "M": 1024**2}


def _cmd(cascade: list[Path], sets: tuple[str, ...]) -> list[str]:
    cmd = ["helm", "template", "clickhouse-cluster", str(CHART), "--set", "appNamespace=dfe-local"]
    for v in cascade:
        cmd += ["-f", str(v)]
    for s in sets:
        cmd += ["--set", s]
    return cmd


def render(cascade: list[Path], *sets: str) -> list[dict]:
    out = subprocess.run(
        _cmd(cascade, sets),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if out.returncode != 0:
        raise SystemExit(f"helm template failed for {sets}:\n{out.stderr}")
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def render_error(*sets: str) -> str:
    """The stderr of a render that MUST fail. Empty string means it did not."""
    out = subprocess.run(
        _cmd(CLUSTER_CASCADE, sets),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return "" if out.returncode == 0 else out.stderr


def one(docs: list[dict], kind: str) -> dict:
    matches = [d for d in docs if d.get("kind") == kind]
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one {kind}, got {len(matches)}")
    return matches[0]


def size_bytes(size: object) -> int | None:
    """Bytes for a size-based rotation (`50M`, `512K`, `1048576`); None for anything else."""
    match = _SIZE.match(str(size))
    if not match:
        return None
    return int(match.group(1)) * _UNIT[match.group(2)]


def disk_bound(logger: dict) -> int | None:
    """Most bytes the log and the err.log can hold: each keeps a live file plus count archives."""
    size = size_bytes(logger.get("size"))
    count = logger.get("count")
    if size is None or not isinstance(count, int) or count < 1:
        return None
    return 2 * (count + 1) * size


def check_logger(where: str, logger: dict | None, unset: str) -> None:
    expect(f"{where} sets its own logger", isinstance(logger, dict), f"absent, so {unset}")
    if not isinstance(logger, dict):
        return
    expect(
        f"{where} logs at {SHIPPED_LEVEL}",
        logger.get("level") == SHIPPED_LEVEL,
        f"got {logger.get('level')!r}",
    )
    bound = disk_bound(logger)
    expect(
        f"{where} keeps its log under {CEILING_BYTES // 1024**2} MiB",
        bound is not None and bound <= CEILING_BYTES,
        f"size {logger.get('size')!r} x count {logger.get('count')!r} bounds it at {bound}",
    )


def cr_logger(docs: list[dict], kind: str) -> dict | None:
    return (one(docs, kind)["spec"].get("settings") or {}).get("logger")


def single_logger(docs: list[dict]) -> dict | None:
    config_map = one(docs, "ConfigMap")
    fragment = (config_map.get("data") or {}).get("dfe-logger.yaml")
    return (yaml.safe_load(fragment) or {}).get("logger") if fragment else None


def test_cluster_mode_bounds_the_server_log() -> None:
    check_logger(
        "the ClickHouseCluster",
        cr_logger(render(CLUSTER_CASCADE), "ClickHouseCluster"),
        OPERATOR_DEFAULT,
    )


def test_keeper_bounds_its_log() -> None:
    check_logger(
        "the KeeperCluster", cr_logger(render(CLUSTER_CASCADE), "KeeperCluster"), OPERATOR_DEFAULT
    )


def test_single_mode_bounds_the_server_log() -> None:
    docs = render(SINGLE_CASCADE)
    check_logger("the single-mode server", single_logger(docs), IMAGE_DEFAULT)
    mounts = one(docs, "StatefulSet")["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    expect(
        "the single-mode server reads the logger from config.d",
        any(m.get("mountPath") == SINGLE_LOGGER_PATH for m in mounts),
        f"mounts: {[m.get('mountPath') for m in mounts]}",
    )


def test_one_value_reaches_every_server_it_names() -> None:
    sets = (
        "clickhouse.serverLog.level=warning",
        "clickhouse.serverLog.size=20M",
        "clickhouse.serverLog.count=5",
    )
    cluster = render(CLUSTER_CASCADE, *sets)
    want = {"level": "warning", "size": "20M", "count": 5}
    expect(
        "an overridden serverLog reaches the ClickHouseCluster",
        cr_logger(cluster, "ClickHouseCluster") == want,
    )
    expect(
        "an overridden serverLog reaches single mode",
        single_logger(render(SINGLE_CASCADE, *sets)) == want,
    )
    expect(
        "serverLog leaves Keeper on its own value",
        (cr_logger(cluster, "KeeperCluster") or {}).get("level") == SHIPPED_LEVEL,
    )
    keeper_only = render(CLUSTER_CASCADE, "clickhouse.keeper.log.level=warning")
    keeper = cr_logger(keeper_only, "KeeperCluster") or {}
    expect("keeper.log reaches the KeeperCluster", keeper.get("level") == "warning")


def test_a_setting_the_server_would_reject_fails_the_render() -> None:
    for value in ("verbose", "Information", ""):
        err = render_error(f"clickhouse.serverLog.level={value}")
        expect(f"serverLog.level={value!r} is refused", "must be one of" in err, err[:200])
    err = render_error("clickhouse.keeper.log.level=verbose")
    expect("keeper.log.level=verbose is refused", "clickhouse.keeper.log.level" in err, err[:200])
    for value in ("0", "-1"):
        err = render_error(f"clickhouse.serverLog.count={value}")
        expect(f"serverLog.count={value} is refused", "must be 1 or more" in err, err[:200])
    expect("the shipped values pass", render_error() == "")


def main() -> int:
    with standalone():
        test_cluster_mode_bounds_the_server_log()
        test_keeper_bounds_its_log()
        test_single_mode_bounds_the_server_log()
        test_one_value_reaches_every_server_it_names()
        test_a_setting_the_server_would_reject_fails_the_render()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
