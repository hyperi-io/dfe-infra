#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         verify_embed.py
#  Purpose:      Verify the dfe-ui <-> hyperdx SEAMLESS EMBED (Option B): log in to
#                dfe-ui, open /observe/<feature>, and assert the dfe-ui shell hosts a
#                chromeless hyperdx iframe (dfe-ui owns the nav; hyperdx has none).
#                Python-Playwright, python3 allow-list. Grows with the embed work.
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Verify the DFE seamless embed end-to-end.

    python3 scripts/verify_embed.py --dfeui http://localhost:3000 \
        --hyperdx http://localhost:8080 --user dev --password dev --feature search

Checks: login succeeds; /observe/<feature> renders an <iframe> whose src targets the
hyperdx feature with ?embed=1; the iframed hyperdx is CHROMELESS (no AppNav); and the
dfe-ui sidebar (the single, owning nav) is present around it. Screenshots for eyeball.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

# Where the eyeball screenshot lands. NOT a hardcoded path: this script is part of
# the product suite and runs on an external org's deploy host as readily as ours.
DEFAULT_SHOT = Path(tempfile.gettempdir()) / "dfe-embed.png"


def _fill_login(page, user: str, pw: str) -> bool:
    u = page.locator(
        'input[name="username"], input[type="email"], input[name="email"], '
        'input[type="text"]:not([type="hidden"]), '
        'input[placeholder*="ser" i], input[id*="user" i], input[id*="email" i]'
    ).first
    p = page.locator('input[type="password"]').first
    if u.count() == 0 or p.count() == 0:
        return False
    u.fill(user)
    p.fill(pw)
    btn = page.locator(
        'button[type="submit"], button:has-text("Log in"), button:has-text("Login"), '
        'button:has-text("Sign in")'
    ).first
    if btn.count() == 0:
        return False
    btn.click()
    return True


def run(
    dfeui: str,
    hyperdx: str,
    user: str,
    pw: str,
    feature: str,
    hdx_user: str,
    hdx_pw: str,
    shot: Path,
) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed", file=sys.stderr)
        return 2

    dfeui = dfeui.rstrip("/")
    hyperdx = hyperdx.rstrip("/")
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # Pre-authenticate the hyperdx origin in THIS browser context so the iframe
        # (same origin :8080) shares the session cookie -- in prod the shared OIDC
        # does this automatically; locally we log in once.
        page.goto(hyperdx + "/login", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        if _fill_login(page, hdx_user, hdx_pw):
            try:
                page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
            except Exception:
                pass

        page.goto(dfeui + "/login", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        filled = _fill_login(page, user, pw)
        if filled:
            try:
                page.wait_for_url(lambda u: "/login" not in u, timeout=20000)
            except Exception:
                pass
        after_login_url = page.evaluate("() => location.pathname")

        # Go to the embedded observe view.
        page.goto(dfeui + f"/observe/{feature}", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

        iframe_el = page.query_selector("iframe")
        iframe_src = iframe_el.get_attribute("src") if iframe_el else None
        # dfe-ui's own sidebar present (the single owning nav)?
        has_dfeui_sidebar = page.evaluate(
            '() => !!document.querySelector(\'aside, [class*="sider" i], nav, [class*="Sidebar" i]\')'
        )
        # Reach INTO the hyperdx iframe (playwright crosses origins for tests).
        hframe = next((f for f in page.frames if hyperdx in (f.url or "")), None)
        frame_has_appnav = None
        frame_url = hframe.url if hframe else None
        if hframe:
            try:
                frame_has_appnav = hframe.evaluate(
                    "() => !!document.querySelector('[data-testid=\"app-nav\"]')"
                )
            except Exception as exc:  # frame still navigating
                errors.append(f"frame probe: {exc}")

        # Theme sync: post a dark theme to the iframe (as dfe-ui does on toggle),
        # then read the frame's applied Mantine color scheme.
        theme_applied = None
        if hframe:
            try:
                page.evaluate(
                    '() => { const f = document.querySelector("iframe");'
                    " if (f) f.contentWindow.postMessage"
                    '({type:"DFE_SET_THEME", theme:"dark"}, "*"); }'
                )
                page.wait_for_timeout(1800)
                theme_applied = hframe.evaluate(
                    '() => document.documentElement.getAttribute("data-mantine-color-scheme")'
                )
            except Exception as exc:
                errors.append(f"theme probe: {exc}")

        shot.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shot), full_page=True)
        browser.close()

    logged_in = "/login" not in after_login_url
    src_ok = (
        bool(iframe_src)
        and hyperdx in iframe_src
        and "embed=1" in iframe_src
        and feature in iframe_src
    )
    frame_ok = hframe is not None
    chromeless_ok = frame_has_appnav is False
    frame_authed = frame_ok and "/login" not in (frame_url or "") and feature in (frame_url or "")

    checks = [
        (
            "dfe-ui login",
            logged_in,
            f"still at {after_login_url}" if not logged_in else "",
        ),
        (
            "observe iframe present",
            bool(iframe_el),
            "no <iframe> on /observe" if not iframe_el else "",
        ),
        ("iframe src -> hyperdx embed", src_ok, f"src={iframe_src}"),
        ("hyperdx frame loaded", frame_ok, f"frame_url={frame_url}"),
        (
            "hyperdx chromeless in iframe",
            chromeless_ok,
            f"has_appnav={frame_has_appnav}",
        ),
        (
            "hyperdx authenticated in iframe",
            frame_authed,
            f"frame_url={frame_url}",
        ),
        ("dfe-ui sidebar (owning nav)", bool(has_dfeui_sidebar), ""),
        (
            "theme sync (dark applied)",
            theme_applied == "dark",
            f"frame color-scheme={theme_applied}",
        ),
    ]
    print(f"dfe-ui {dfeui}  hyperdx {hyperdx}  feature={feature}")
    print(f"  after login -> {after_login_url}")
    print(f"  iframe src  -> {iframe_src}")
    print(f"  hyperdx frame url -> {frame_url}  appnav_in_frame={frame_has_appnav}")
    print(f"  screenshot -> {shot}\n")
    failures = 0
    for name, ok, note in checks:
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))
        failures += 0 if ok else 1
    if errors:
        print(f"\n  console errors ({len(errors)}):")
        for e in errors[:12]:
            print(f"    - {e[:200]}")
    print(f"\n{len(checks) - failures}/{len(checks)} checks passed.")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify the dfe-ui<->hyperdx seamless embed.")
    ap.add_argument("--dfeui", default="http://localhost:3000")
    ap.add_argument("--hyperdx", default="http://localhost:8080")
    ap.add_argument("--user", default="dev", help="dfe-ui login (mock-backed)")
    ap.add_argument("--password", default="dev")
    ap.add_argument("--feature", default="search")
    ap.add_argument("--hdx-user", default="dev@dfe.local", help="hyperdx login")
    ap.add_argument("--hdx-password", default="DfeLocalDev123!")
    ap.add_argument(
        "--screenshot",
        type=Path,
        default=DEFAULT_SHOT,
        help=f"where to write the eyeball screenshot (default: {DEFAULT_SHOT})",
    )
    args = ap.parse_args()
    return run(
        args.dfeui,
        args.hyperdx,
        args.user,
        args.password,
        args.feature,
        args.hdx_user,
        args.hdx_password,
        args.screenshot,
    )


if __name__ == "__main__":
    sys.exit(main())
