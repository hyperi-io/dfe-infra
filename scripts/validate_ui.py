#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         validate_ui.py
#  Purpose:      Reusable BROWSER validator for the DFE work UIs (hyperdx fork +
#                dfe-ui): drive a headless Chromium via the Playwright PYTHON
#                package and assert rendered look-and-feel (Inter font, brand
#                palette, chrome-at-top), catch console errors, and screenshot for
#                eyeballing. Runs under the `python3` allow-list (no per-call MCP
#                prompts). The browser half of validate_app.py; grow both as we add
#                cases. This IS the Playwright test suite (Python), reusable in CI.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Headless-browser validation of a running DFE UI.

    python3 scripts/validate_ui.py --url http://localhost:8080
    python3 scripts/validate_ui.py --url http://localhost:8080 --shot ui.png

Checks: page loads; body font-family is Inter; a Mantine/brand palette is applied;
the app is not stuck on a loading splash; console errors are surfaced (they are the
usual reason a data UI hangs). Exit non-zero if a hard check fails. Requires the
playwright package + chromium (`python3 -m pip install playwright &&
python3 -m playwright install chromium`).
"""

from __future__ import annotations

import argparse
import sys

EXPECT_FONT = "Inter"
# A splash string that means the app never finished bootstrapping (backend/config).
STUCK_MARKERS = ("Loading DFE", "Loading HyperDX")

PROBE_JS = """
() => {
  const cs = getComputedStyle(document.body);
  const root = getComputedStyle(document.documentElement);
  const props = {};
  for (const n of ['--color-brand-primary','--mantine-primary-color-filled',
                   '--mantine-font-family','--color-brand']) {
    const v = root.getPropertyValue(n).trim();
    if (v) props[n] = v;
  }
  const nav = document.querySelector('nav, header, [class*="AppNav"], [class*="TopNav"], [data-testid*="nav"]');
  const r = nav ? nav.getBoundingClientRect() : null;
  return {
    title: document.title,
    scheme: document.documentElement.getAttribute('data-mantine-color-scheme')
            || document.documentElement.getAttribute('data-theme') || '',
    bodyFont: cs.fontFamily,
    bodyBg: cs.backgroundColor,
    props,
    // width+height matter: a TOP bar is wide + short; a left SIDEBAR is tall +
    // narrow. Both sit at top:0, so top alone can't tell them apart (was an F-P).
    nav: nav ? {tag: nav.tagName, top: Math.round(r.top), left: Math.round(r.left),
                width: Math.round(r.width), height: Math.round(r.height)} : null,
    text: (document.body.innerText || '').slice(0, 160),
  };
}
"""


def validate(url: str, shot: str | None, settle_ms: int) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright not installed: python3 -m pip install playwright"
            " && python3 -m playwright install chromium",
            file=sys.stderr,
        )
        return 2

    console_errors: list[str] = []
    page_errors: list[str] = []
    checks: list[tuple[str, bool, str]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        checks.append(
            (
                "http",
                bool(resp and resp.ok),
                "" if resp and resp.ok else f"status {resp.status if resp else 'none'}",
            )
        )
        page.wait_for_timeout(settle_ms)  # let the SPA hydrate + fetch
        info = page.evaluate(PROBE_JS)
        if shot:
            page.screenshot(path=shot, full_page=True)
        browser.close()

    font_ok = EXPECT_FONT.lower() in (info["bodyFont"] or "").lower()
    checks.append(("font-inter", font_ok, "" if font_ok else f"body font={info['bodyFont']!r}"))

    palette_ok = bool(info["props"])
    checks.append(
        (
            "palette",
            palette_ok,
            "" if palette_ok else "no brand/mantine CSS custom properties found",
        )
    )

    stuck = any(m in (info["text"] or "") for m in STUCK_MARKERS)
    checks.append(
        ("not-stuck", not stuck, "" if not stuck else f"app stuck on splash: {info['text']!r}")
    )

    # A TOP bar sits at top:0 AND is short (a bar). A full-height LEFT sidebar also
    # sits at top:0 -- so require a small height to tell them apart (else a sidebar
    # false-passes, as it did before eyeballing). The fork's condensed-top embed nav
    # is still PENDING, so today's upstream sidebar SHOULD fail this check.
    nav = info["nav"]
    nav_top_ok = bool(nav) and nav["top"] <= 8 and nav["height"] <= 120
    checks.append(
        (
            "chrome-at-top",
            nav_top_ok,
            ""
            if nav_top_ok
            else f"chrome is not a top bar (nav={nav}) -- likely the upstream side nav",
        )
    )

    print(f"URL: {url}")
    print(f"  title={info['title']!r} scheme={info['scheme']!r}")
    print(f"  bodyFont={info['bodyFont']!r}")
    print(f"  palette={info['props']}")
    print(f"  bodyText={info['text']!r}")
    if shot:
        print(f"  screenshot -> {shot}")
    print()
    failures = 0
    for name, ok, note in checks:
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" -- {note}" if note else ""))
        if not ok:
            failures += 1
    if console_errors or page_errors:
        print(f"\n  console errors ({len(console_errors)}), page errors ({len(page_errors)}):")
        for e in (console_errors + page_errors)[:20]:
            print(f"    - {e[:200]}")
    print(f"\n{len(checks) - failures}/{len(checks)} checks passed.")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Headless-browser DFE UI validator.")
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--shot", default=None, help="save a full-page screenshot to this path")
    ap.add_argument("--settle-ms", type=int, default=6000, help="wait after load for SPA hydrate")
    args = ap.parse_args()
    return validate(args.url, args.shot, args.settle_ms)


if __name__ == "__main__":
    sys.exit(main())
