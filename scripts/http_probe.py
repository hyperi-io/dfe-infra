#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         http_probe.py
#  Purpose:      Minimal curl-equivalent HTTP client (stdlib only) for smoke
#                tests + health checks where curl is unavailable or blocked.
#                Generic - method, URL, headers, body are all flags. Runs under
#                the python3 allow-list.
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A tiny curl for scripts: `python3 scripts/http_probe.py <METHOD> <URL> [opts]`.

    python3 scripts/http_probe.py GET http://localhost:8000/health/ready
    python3 scripts/http_probe.py POST http://localhost:8000/api/v1/auth/login \
        --json '{"username":"admin","password":"..."}'
    python3 scripts/http_probe.py GET https://api.internal/things -H "Authorization: Bearer $TOK" -k

Prints the status line then the body. Exits 0 on a response (any status) unless
``--fail`` is set, which exits non-zero on HTTP >= 400 (like ``curl -f``); a
connection/timeout error always exits non-zero. stdlib urllib only - no curl, no
requests, so it works anywhere python3 does.
"""

from __future__ import annotations

import argparse
import json as _json
import ssl
import sys
import urllib.error
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Minimal curl-equivalent HTTP client (stdlib only).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("method", help="HTTP method (GET, POST, PUT, DELETE, ...)")
    ap.add_argument("url", help="request URL")
    ap.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        metavar="'K: V'",
        help="request header (repeatable)",
    )
    ap.add_argument("-d", "--data", default=None, help="raw request body")
    ap.add_argument(
        "--json",
        default=None,
        help="JSON request body (also sets Content-Type: application/json)",
    )
    ap.add_argument(
        "-k",
        "--insecure",
        action="store_true",
        help="skip TLS certificate verification",
    )
    ap.add_argument("--timeout", type=float, default=15.0, help="timeout (seconds)")
    ap.add_argument("--head", action="store_true", help="print response headers too")
    ap.add_argument(
        "--fail", action="store_true", help="exit non-zero on HTTP status >= 400"
    )
    args = ap.parse_args()

    headers = {}
    for h in args.header:
        if ":" not in h:
            print(f"bad header (expected 'K: V'): {h!r}", file=sys.stderr)
            return 2
        k, v = h.split(":", 1)
        headers[k.strip()] = v.strip()

    body: bytes | None = None
    if args.json is not None:
        body = args.json.encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    elif args.data is not None:
        body = args.data.encode("utf-8")

    ctx = None
    if args.insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        args.url, data=body, headers=headers, method=args.method.upper()
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout, context=ctx) as resp:  # noqa: S310 - explicit URL from flags
            status = resp.status
            resp_headers = resp.headers
            payload = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # a response WITH an error status
        status = exc.code
        resp_headers = exc.headers
        payload = exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:  # no response
        print(f"request failed: {exc}", file=sys.stderr)
        return 1

    print(f"HTTP {status}")
    if args.head:
        for k, v in resp_headers.items():
            print(f"{k}: {v}")
        print()
    print(payload)

    if args.fail and status >= 400:
        return 22  # mirror curl -f's exit code
    return 0


if __name__ == "__main__":
    sys.exit(main())
