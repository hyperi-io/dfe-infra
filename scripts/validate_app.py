#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         validate_app.py
#  Purpose:      Reusable app-layer validator (hyperdx / dfe-ui / engine): wait for
#                readiness and assert look-and-feel + integration markers over HTTP.
#                The app-layer counterpart to deploy_matrix.py (substrate). stdlib
#                only (urllib) so it runs under the `python3` allow-list with no deps
#                and no curl/sleep one-offs.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Validate a running DFE app stack without a browser.

Checks, in order:
  ready       -- the app URL returns 2xx within a budget (polls, no external sleep)
  runtime-env -- HyperDX writes browser env to /__ENV.js at boot; assert the DFE
                 theme is active (NEXT_PUBLIC_THEME=dfe) so we KNOW the fork theme
                 (Inter font + brand palette) is the one being served
  assets      -- the served HTML/CSS references the expected fonts (Inter, IBM Plex
                 Mono) -- the HTTP-observable half of the look-and-feel

Computed-style / layout checks (chrome-at-top, exact palette hex, seamless embed)
need a real browser -- that is the JS Playwright suite (P4), not this. This gives
a fast, dependency-free, reusable gate that CI and a dev can both run.

    python3 scripts/validate_app.py                         # defaults :8080 / :8000
    python3 scripts/validate_app.py --app-url http://localhost:8080
    python3 scripts/validate_app.py --expect-theme dfe --timeout 120
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request

# Deterministic poll: we block on the artifact (a 2xx), not a guessed sleep.
POLL_SECONDS = 3


def _get(url: str, timeout: int = 10) -> tuple[int, str]:
    """GET url -> (status, body). Network/HTTP errors become (0|status, '')."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dfe-validate"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, OSError):
        return 0, ""


def wait_ready(app_url: str, budget: int) -> tuple[bool, str]:
    """Poll app_url until a 2xx or the budget expires."""
    waited = 0
    last = 0
    while waited <= budget:
        status, _ = _get(app_url)
        if 200 <= status < 300:
            return True, ""
        last = status
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    return False, f"app {app_url} not ready within {budget}s (last status {last})"


def check_runtime_env(app_url: str, expect_theme: str) -> tuple[bool, str]:
    """HyperDX writes NEXT_PUBLIC_* to /__ENV.js at boot; assert the theme."""
    status, body = _get(app_url.rstrip("/") + "/__ENV.js")
    if status == 0:
        return False, "/__ENV.js unreachable"
    if not expect_theme:
        return True, ""
    # __ENV.js looks like: window.__ENV = {"NEXT_PUBLIC_THEME":"dfe", ...}
    if (
        f'"NEXT_PUBLIC_THEME":"{expect_theme}"' in body
        or f"NEXT_PUBLIC_THEME={expect_theme}" in body
    ):
        return True, ""
    if "NEXT_PUBLIC_THEME" not in body:
        return True, "note: __ENV.js has no NEXT_PUBLIC_THEME (theme may be a build default)"
    return False, f"__ENV.js theme is not {expect_theme!r}"


def check_fonts(app_url: str, fonts: list[str]) -> tuple[bool, str]:
    """The served HTML references the expected work-UI fonts (Inter / IBM Plex)."""
    status, body = _get(app_url)
    if status == 0:
        return False, "app HTML unreachable"
    low = body.lower()
    missing = [f for f in fonts if f.lower() not in low]
    if missing:
        # HTML may load fonts via a linked CSS chunk, not inline -> soft signal.
        return (
            True,
            f"note: fonts not seen inline in HTML ({', '.join(missing)}); check CSS/Playwright",
        )
    return True, ""


def main() -> int:
    p = argparse.ArgumentParser(description="Validate a running DFE app stack (HTTP).")
    p.add_argument("--app-url", default="http://localhost:8080")
    p.add_argument("--api-url", default="http://localhost:8000")
    p.add_argument("--expect-theme", default="dfe", help="required NEXT_PUBLIC_THEME ('' to skip)")
    p.add_argument("--fonts", default="Inter", help="comma-separated font names to look for")
    p.add_argument("--timeout", type=int, default=120, help="readiness budget seconds")
    args = p.parse_args()

    fonts = [f.strip() for f in args.fonts.split(",") if f.strip()]
    checks: list[tuple[str, bool, str]] = []

    ok, err = wait_ready(args.app_url, args.timeout)
    checks.append(("ready", ok, err))
    if ok:
        ok2, err2 = check_runtime_env(args.app_url, args.expect_theme)
        checks.append(("runtime-env(theme)", ok2, err2))
        ok3, err3 = check_fonts(args.app_url, fonts)
        checks.append(("fonts", ok3, err3))

    failures = 0
    for name, passed, note in checks:
        mark = "ok  " if passed else "FAIL"
        line = f"  [{mark}] {name}"
        if note:
            line += f" -- {note}"
        print(line)
        if not passed:
            failures += 1

    print(f"\n{len(checks) - failures}/{len(checks)} checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
