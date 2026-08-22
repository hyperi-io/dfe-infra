#!/usr/bin/env python3
# Project:   DFE Infra
# File:      scripts/verify-dashboard-tiles.py
# Purpose:   Run every raw-SQL tile in a HyperDX dashboard template against ClickHouse
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Execute every raw-SQL tile in a HyperDX dashboard template against ClickHouse.

Mirrors packages/common-utils/src/macros.ts and rawSqlParams.ts: the macros are
expanded to ClickHouse query parameters exactly as HyperDX expands them, then the
statement runs over the HTTP interface with those parameters bound. A tile passes
only when the statement executes AND the result shape matches what the chart
component infers -- a Date/DateTime column for a time series, a numeric column
for every display type that plots a value. Catches the "healthy 200 but an empty
tile" class of dashboard bug without a browser.

    scripts/verify-dashboard-tiles.py dfe-throughput.json dfe-kafka.json
    CH_PASSWORD_FILE=/path/pw scripts/verify-dashboard-tiles.py --ch-url http://ch:8123 *.json
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request

DATE_TYPES = ("Date", "DateTime")
NUMERIC = re.compile(r"^(Nullable\()?(U?Int\d+|Float\d+|Decimal)")

# The chart component reads a time series off a Date/DateTime column, so a tile
# that casts its bucket to an integer renders nothing.
TIME_SERIES_TYPES = {"line", "stacked_bar"}
# Search and markdown tiles carry no query.
VALUELESS_TYPES = {"search", "markdown"}

START_MS = "{startDateMilliseconds:Int64}"
END_MS = "{endDateMilliseconds:Int64}"
INTERVAL = "{intervalSeconds:Int64}"
INTERVAL_MS_PARAM = "{intervalMilliseconds:Int64}"


def _dt(ms_param: str) -> str:
    return f"toDateTime(fromUnixTimestamp64Milli({ms_param}))"


def _dt64(ms_param: str) -> str:
    return f"fromUnixTimestamp64Milli({ms_param})"


def _date(ms_param: str) -> str:
    return f"toDate(fromUnixTimestamp64Milli({ms_param}))"


def _args(sql: str, start: int) -> tuple[list[str], int]:
    """Split a macro's parenthesised argument list, honouring nesting."""
    if start >= len(sql) or sql[start] != "(":
        return [], 0
    depth = 0
    for i in range(start, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                inner = sql[start + 1 : i]
                return [a.strip() for a in inner.split(",") if a.strip()], i + 1 - start
    raise ValueError("unterminated macro argument list")


def expand(sql: str, source_table: str | None) -> str:
    """Expand the $__ macros the way replaceMacros does."""

    def zero(name: str, value: str) -> None:
        nonlocal sql
        sql = re.sub(rf"\$__{name}\b", lambda _: value, sql)

    def with_args(name: str, render) -> None:
        nonlocal sql
        while True:
            match = re.search(rf"\$__{name}\b", sql)
            if not match:
                return
            args, length = _args(sql, match.end())
            sql = sql[: match.start()] + render(args) + sql[match.end() + length :]

    # Longest names first, exactly as replaceMacros sorts them.
    with_args(
        "dateTimeFilter",
        lambda a: f"({a[0]} >= {_date(START_MS)} AND {a[0]} <= {_date(END_MS)})"
        f" AND ({a[1]} >= {_dt(START_MS)} AND {a[1]} <= {_dt(END_MS)})",
    )
    with_args(
        "timeInterval_ms",
        lambda a: f"toStartOfInterval(toDateTime64({a[0]}, 3), INTERVAL {INTERVAL_MS_PARAM} millisecond)",
    )
    with_args(
        "timeInterval",
        lambda a: f"toStartOfInterval(toDateTime({a[0]}), INTERVAL {INTERVAL} second)",
    )
    with_args(
        "timeFilter_ms",
        lambda a: f"{a[0]} >= {_dt64(START_MS)} AND {a[0]} <= {_dt64(END_MS)}",
    )
    with_args(
        "timeFilter",
        lambda a: f"{a[0]} >= {_dt(START_MS)} AND {a[0]} <= {_dt(END_MS)}",
    )
    with_args(
        "dateFilter",
        lambda a: f"{a[0]} >= {_date(START_MS)} AND {a[0]} <= {_date(END_MS)}",
    )
    zero("fromTime_ms", _dt64(START_MS))
    zero("toTime_ms", _dt64(END_MS))
    zero("fromTime", _dt(START_MS))
    zero("toTime", _dt(END_MS))
    zero("interval_s", INTERVAL)
    zero("filters", "(1=1 /** no filters applied */)")
    if source_table:
        zero("sourceTable", source_table)
    return sql


def run(sql: str, now_ms: int, cfg: argparse.Namespace) -> dict:
    params = {
        "query": sql + "\nFORMAT JSONCompact",
        "param_startDateMilliseconds": str(now_ms - cfg.window_ms),
        "param_endDateMilliseconds": str(now_ms),
        "param_intervalSeconds": str(cfg.interval_s),
        "param_intervalMilliseconds": str(cfg.interval_s * 1000),
        "date_time_output_format": "iso",
    }
    req = urllib.request.Request(
        cfg.ch_url + "/?" + urllib.parse.urlencode(params),
        headers={"X-ClickHouse-User": cfg.ch_user, "X-ClickHouse-Key": cfg.ch_password},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def check_shape(display: str, meta: list[dict]) -> str | None:
    """What the chart component needs from the result set, or None if satisfied."""
    if display in VALUELESS_TYPES:
        return None
    types = [c["type"] for c in meta]
    if display in TIME_SERIES_TYPES and not any(
        t.startswith(DATE_TYPES) for t in types
    ):
        return f"no Date/DateTime column (got {types})"
    if not any(NUMERIC.match(t) for t in types):
        return f"no numeric column (got {types})"
    return None


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="verify-dashboard-tiles.py",
        description="Run each raw-SQL dashboard tile against ClickHouse and check its shape.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("dashboards", nargs="+", help="dashboard template JSON file(s)")
    ap.add_argument(
        "--ch-url",
        default=os.environ.get("CH_URL", "http://127.0.0.1:18123"),
        help="ClickHouse HTTP endpoint (env CH_URL)",
    )
    ap.add_argument(
        "--ch-user",
        default=os.environ.get("CH_USER", "dfe_ch_system_probe"),
        help="ClickHouse user (env CH_USER)",
    )
    ap.add_argument(
        "--ch-password-file",
        default=os.environ.get("CH_PASSWORD_FILE", "/dev/null"),
        help="file holding the ClickHouse password (env CH_PASSWORD_FILE)",
    )
    ap.add_argument(
        "--window-minutes",
        type=int,
        default=60,
        help="query window the dashboard opens with",
    )
    ap.add_argument(
        "--interval-seconds",
        type=int,
        default=60,
        help="time-bucket width the dashboard opens with",
    )
    args = ap.parse_args(argv)
    args.window_ms = args.window_minutes * 60 * 1000
    args.interval_s = args.interval_seconds
    args.ch_password = (
        pathlib.Path(args.ch_password_file)
        .read_text(encoding="utf-8", errors="replace")
        .strip()
    )
    return args


def main(argv: list[str] | None = None) -> int:
    cfg = _parse_args(argv)
    now_ms = int(time.time() * 1000)
    failures = 0
    total = 0
    for path in cfg.dashboards:
        doc = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        print(f"\n=== {doc['name']} ({pathlib.Path(path).name}) ===")
        for tile in doc["tiles"]:
            config = tile["config"]
            if config.get("configType") != "sql":
                continue
            total += 1
            display = config.get("displayType", "line")
            try:
                sql = expand(config["sqlTemplate"], config.get("_verifySourceTable"))
                result = run(sql, now_ms, cfg)
            except Exception as err:  # noqa: BLE001 - the report is the point
                failures += 1
                detail = getattr(err, "read", None)
                msg = detail().decode("utf-8", errors="replace")[:400] if detail else err
                print(f"  FAIL {tile['id']}: {msg}")
                continue
            problem = check_shape(display, result.get("meta", []))
            if problem:
                failures += 1
                print(f"  FAIL {tile['id']} [{display}]: {problem}")
                continue
            print(f"  ok   {tile['id']} [{display}] rows={result.get('rows', 0)}")
    print(f"\n{total - failures}/{total} raw-SQL tiles pass")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
