#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         audit_fonts.py
#  Purpose:      Log in to a running DFE UI and audit the rendered font-family of
#                every visible leaf-text element, grouped by font with samples, so
#                we can find text that is NOT on the standard (Inter for base, IBM
#                Plex Mono for code/SQL/logs). Python-Playwright, python3 allow-list.
#                Reuses the auth flow from hdx_login.py.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Audit which fonts the authenticated UI actually renders.

    python3 scripts/audit_fonts.py --base http://localhost:8080

Prints each distinct font-family in use with a count + a text sample, and flags any
that are neither Inter (base/display) nor a Plex/monospace (code). That is how we
locate the "old font" leftovers (e.g. Martel Sans, or an unstyled default).
"""

from __future__ import annotations

import argparse
import sys

from hdx_login import DEFAULT_EMAIL, DEFAULT_PASSWORD, _fill_auth_form  # sibling module

STANDARD_OK = ("inter", "plex", "mono", "monospace")  # allowed substrings

FONT_WALK = """
() => {
  const groups = {};
  const els = document.querySelectorAll('body *');
  for (const el of els) {
    if (el.children.length) continue;              // leaf nodes only
    const t = (el.innerText || '').trim();
    if (!t) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue; // visible only
    const ff = getComputedStyle(el).fontFamily;
    const key = ff;
    if (!groups[key]) groups[key] = {count: 0, samples: []};
    groups[key].count++;
    if (groups[key].samples.length < 4) groups[key].samples.push(t.slice(0, 42));
  }
  return groups;
}
"""


def run(base: str, email: str, password: str) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed", file=sys.stderr)
        return 2

    base = base.rstrip("/")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        for route, confirm in (("/register", True), ("/login", False)):
            page.goto(base + route, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            if _fill_auth_form(page, email, password, confirm):
                break
        try:
            page.wait_for_url(lambda u: "/register" not in u and "/login" not in u, timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(6000)
        groups = page.evaluate(FONT_WALK)
        browser.close()

    print(f"Font audit @ {base} (authenticated)\n")
    # Sort: non-standard fonts first (the problems), then by count.
    items = sorted(groups.items(), key=lambda kv: (_is_ok(kv[0]), -kv[1]["count"]))
    bad = 0
    for ff, info in items:
        ok = _is_ok(ff)
        if not ok:
            bad += 1
        mark = "ok  " if ok else "FLAG"
        print(f"  [{mark}] x{info['count']:<3} {ff}")
        for s in info["samples"]:
            print(f"           - {s!r}")
    print(f"\n{len(items)} distinct font-families; {bad} off-standard (neither Inter nor mono).")
    return 1 if bad else 0


def _is_ok(ff: str) -> bool:
    low = ff.lower()
    return any(tok in low for tok in STANDARD_OK)


def main() -> int:
    ap = argparse.ArgumentParser(description="Authenticated font audit for a DFE UI.")
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    args = ap.parse_args()
    return run(args.base, args.email, args.password)


if __name__ == "__main__":
    sys.exit(main())
