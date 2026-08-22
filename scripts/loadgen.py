#!/usr/bin/env python3
# Project:   DFE Infra
# File:      scripts/loadgen.py
# Purpose:   Hold a steady ingest rate at a DFE receiver for a fixed duration
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Hold a steady event rate at a DFE receiver for a fixed duration.

A single burst gives a dashboard one spike; a sustained rate gives it a shape to
draw, which is what actually proves a time-series tile. Generic and
parameterised -- point it at any receiver ingest endpoint (a port-forward, an
in-cluster Service, a docker or local process) and pick the rate and duration.

    scripts/loadgen.py --url http://127.0.0.1:18081/ingest --seconds 600 --rate 40
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
import urllib.request

# Default flavour payload dimensions. Overridable so the tool is not tied to one
# demo dataset; the values are cosmetic (they populate _json paths for the tiles).
ORGS = ["zaphod", "hoopy-frood", "magrathea", "vogon"]
EVENTS = ["towel-check", "ultimate-answer", "improbability-drive", "poetry-reading"]
LEVELS = ["INFO", "WARN", "ERROR", "DEBUG"]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="loadgen.py",
        description="Sustained event load against a DFE receiver ingest endpoint.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument(
        "--url",
        default="http://127.0.0.1:18081/ingest",
        help="receiver ingest endpoint (port-forward, Service, or local process)",
    )
    ap.add_argument("--seconds", type=int, default=600, help="how long to hold the rate")
    ap.add_argument("--rate", type=int, default=40, help="target events per second")
    ap.add_argument("--workers", type=int, default=8, help="concurrent sender threads")
    ap.add_argument("--source", default="default", help="_source value on each event")
    ap.add_argument("--timeout", type=int, default=30, help="per-request timeout (s)")
    ap.add_argument(
        "--seed", type=int, default=20260821, help="base RNG seed (per-worker offset)"
    )
    return ap.parse_args(argv)


def _run(args: argparse.Namespace) -> int:
    lock = threading.Lock()
    tally = {"sent": 0, "failed": 0}
    deadline = time.monotonic() + args.seconds
    interval = args.workers / args.rate if args.rate > 0 else 0.0

    def worker(worker_id: int) -> None:
        rng = random.Random(args.seed + worker_id)
        n = worker_id
        while time.monotonic() < deadline:
            body = json.dumps(
                {
                    "_source": args.source,
                    "org_id": rng.choice(ORGS),
                    "logoriginal": (
                        f"[loadgen] {rng.choice(LEVELS)} event {n} "
                        "from the Heart of Gold"
                    ),
                    "event": rng.choice(EVENTS),
                    "seq": n,
                    "improbability_factor": rng.random() * 1e8,
                    "dont_panic": True,
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                args.url, data=body, headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                    resp.read()
                with lock:
                    tally["sent"] += 1
            except Exception:  # noqa: BLE001 - the tally is the point
                with lock:
                    tally["failed"] += 1
            n += args.workers
            time.sleep(interval)

    threads = [
        threading.Thread(target=worker, args=(w,)) for w in range(args.workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print(f"sent={tally['sent']} failed={tally['failed']} over {args.seconds}s")
    return 1 if tally["sent"] == 0 else 0


def main(argv: list[str] | None = None) -> int:
    return _run(_parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
