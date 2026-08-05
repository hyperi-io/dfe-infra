#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         hdx_login.py
#  Purpose:      Reusable auth-flow test for the DFE work UIs: register-or-login a
#                user (default dev creds) via the Playwright PYTHON package, then
#                assert the app renders its authenticated shell (past the "Loading
#                DFE" splash) with the fork look-and-feel. Runs under the python3
#                allow-list. Companion to validate_ui.py (read-only) -- this one
#                DRIVES the login the user asked to exercise.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Exercise the HyperDX-fork login and validate the authenticated app.

    python3 scripts/hdx_login.py --base http://localhost:8080
    python3 scripts/hdx_login.py --email dev@dfe.local --password 'DfeLocalDev123!'

First run REGISTERS the first user (HyperDX first-run creates the team). Later runs
LOGIN. The password must meet the form policy (>=12 chars, >=1 upper, etc). Exits
non-zero if the app does not reach its authenticated shell.
"""

from __future__ import annotations

import argparse
import sys

DEFAULT_EMAIL = "dev@dfe.local"
DEFAULT_PASSWORD = "DfeLocalDev123!"  # noqa: S105 - local dev placeholder only
SPLASH = ("Loading DFE", "Loading HyperDX")


def _first_visible(page, selectors: list[str]):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def _fill_auth_form(page, email: str, password: str, confirm: bool) -> bool:
    email_in = _first_visible(
        page,
        [
            'input[type="email"]',
            'input[name="email"]',
            'input[placeholder*="Email" i]',
            'input[id*="email" i]',
        ],
    )
    pw_ins = page.locator('input[type="password"]')
    if not email_in or pw_ins.count() == 0:
        return False
    email_in.fill(email)
    pw_ins.nth(0).fill(password)
    if confirm and pw_ins.count() > 1:
        pw_ins.nth(1).fill(password)
    submit = _first_visible(
        page,
        [
            'button[type="submit"]',
            'button:has-text("Sign up")',
            'button:has-text("Sign Up")',
            'button:has-text("Setup")',
            'button:has-text("Register")',
            'button:has-text("Log in")',
            'button:has-text("Login")',
        ],
    )
    if not submit:
        return False
    submit.click()
    return True


def run(base: str, email: str, password: str) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright not installed: python3 -m pip install playwright"
            " && python3 -m playwright install chromium",
            file=sys.stderr,
        )
        return 2

    base = base.rstrip("/")
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # Idempotent: LOGIN first (the user usually already exists on a repeat run);
        # if that does not leave the auth page, REGISTER (first run). Submitting the
        # form and then checking we left /login|/register is the real success signal.
        did = False
        for route, confirm in (("/login", False), ("/register", True)):
            page.goto(base + route, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            if not _fill_auth_form(page, email, password, confirm):
                continue
            try:
                page.wait_for_url(
                    lambda u: "/register" not in u and "/login" not in u, timeout=12000
                )
                did = True
                break
            except Exception:
                continue  # still on the auth page -> try the other route
        if not did:
            print("could not authenticate at /login or /register", file=sys.stderr)
            browser.close()
            return 1

        # Wait for the app to leave the auth page and render its shell.
        try:
            page.wait_for_url(lambda u: "/register" not in u and "/login" not in u, timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(6000)

        info = page.evaluate("""() => {
          const nav = document.querySelector('nav, header, [class*="AppNav"], [class*="TopNav"], [data-testid*="nav"]');
          const r = nav ? nav.getBoundingClientRect() : null;
          return {
            url: location.pathname,
            title: document.title,
            font: getComputedStyle(document.body).fontFamily,
            text: (document.body.innerText || '').slice(0, 160),
            nav: nav ? {tag: nav.tagName, top: Math.round(r.top), height: Math.round(r.height)} : null,
          };
        }""")
        page.screenshot(path=base_shot(), full_page=True)

        # DFE embed route-guard: a disabled feature must not be reachable by URL.
        blocked = ["/alerts", "/sessions", "/service-map", "/team"]
        guard = []
        for r in blocked:
            page.goto(base + r, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1200)
            guard.append((r, page.evaluate("() => location.pathname")))

        # DFE chromeless embed: ?embed=1 must render NO AppNav (dfe-ui owns the nav).
        page.goto(base + "/search?embed=1", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3500)
        embed_has_nav = page.evaluate("() => !!document.querySelector('[data-testid=\"app-nav\"]')")
        browser.close()

    stuck = any(m in (info["text"] or "") for m in SPLASH)
    on_auth = "/register" in info["url"] or "/login" in info["url"]
    checks = [
        ("left-auth-page", not on_auth, f"still on {info['url']}" if on_auth else ""),
        ("not-stuck", not stuck, f"splash: {info['text']!r}" if stuck else ""),
        ("app-shell", bool(info["nav"]), "no chrome/nav rendered" if not info["nav"] else ""),
        ("font-inter", "inter" in (info["font"] or "").lower(), ""),
        (
            "routes-blocked",
            all(final != r for r, final in guard),
            "; ".join(f"{r}->{final}" for r, final in guard),
        ),
        (
            "embed-chromeless",
            not embed_has_nav,
            "AppNav still rendered with ?embed=1" if embed_has_nav else "",
        ),
    ]
    print(f"auth as {email} -> {info['url']}  title={info['title']!r}")
    print(f"  nav={info['nav']}  text={info['text']!r}")
    print(f"  screenshot -> {base_shot()}\n")
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


def base_shot() -> str:
    return "/Volumes/projects/dfe-infra/.tmp/hdx-authed.png"


def main() -> int:
    ap = argparse.ArgumentParser(description="HyperDX fork login-flow validator.")
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--password", default=DEFAULT_PASSWORD)
    args = ap.parse_args()
    return run(args.base, args.email, args.password)


if __name__ == "__main__":
    sys.exit(main())
