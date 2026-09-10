#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         acceptance/onboarding/run.py
#  Purpose:      Drive a deployment's setup wizard and first console session in a
#                real browser, so a deploy is not called good until an operator
#                could actually have onboarded it. Playwright (Chrome, clean
#                profile); credentials from the caller, never a literal.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage:
#    python3 scripts/acceptance/onboarding/run.py \
#        --ui-url https://dfe.example --engine-url https://dfe.example/api \
#        --shots-dir .tmp/onboarding
#    (the admin password comes from DFE_E2E_ADMIN_PASSWORD; dfe-ops acceptance
#     --suite onboarding fetches it from the lane and exports it)
"""onboarding.run -- the wizard, then the console, in a browser.

The first thing an operator does with a new deployment is open it and be walked
through setup. Nothing else in the acceptance suite covers that: the API tests
authenticate straight past it, so a wizard that cannot be finished ships.

Two phases. The wizard phase signs in as the admin the deploy minted (the wizard
sits behind the login) and walks the screens the engine's own setup contract asks
for -- and asserts that a screen it does NOT ask for never appears, which is what
catches a console still demanding a step the product dropped. Then the console
phase logs in as the account the wizard just created, checks the navigation
renders, and creates one source through the UI.

Every step screenshots. A step that cannot complete fails the run, and the run's
exit code fails the deploy.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import access_summary as access_summary_file

from acceptance.onboarding import wizard

# The nav entries every role sees, so the check is a rendered console rather than
# a page that returned 200 with an empty shell.
CONSOLE_LANDMARKS = ("Sources", "Meta Schemas")

# The login page's local-account tab, shown once a provider is registered.
LOCAL_LOGIN_TAB = "Login with Local"

STEP_TIMEOUT_MS = 30_000
# How long the run keeps checking that the source it made has gone.
TEARDOWN_DEADLINE = 120.0


def _context(verify: bool) -> ssl.SSLContext | None:
    """The TLS posture for this run's own API calls."""
    if verify:
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def source_names(engine_url: str, verify: bool, token: str) -> tuple[str, ...]:
    """Every source the deployment currently carries."""
    request = urllib.request.Request(
        f"{engine_url.rstrip('/')}/api/v1/sources",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=60, context=_context(verify)) as response:
        return tuple(str(item["name"]) for item in json.loads(response.read())["items"])


def remove_source(engine_url: str, verify: bool, token: str, name: str, deadline: float) -> str:
    """Delete a source the run created, and confirm it went.

    Through the API rather than the console: this is the run tidying up after
    itself, not part of what it claims to prove.

    The confirmation is a read, not the DELETE's status. A delete commits to the
    deploy repo and reconciles the apps, which outlasts a gateway's own timeout,
    so a 504 on a delete that landed would otherwise read as a source left behind.

    Args:
        engine_url: Engine API base.
        verify: Whether to verify TLS.
        token: A bearer token for the engine.
        name: The source to remove.
        deadline: Seconds to keep checking that it went.

    Returns:
        One line saying whether it went.
    """
    request = urllib.request.Request(
        f"{engine_url.rstrip('/')}/api/v1/sources/{name}",
        method="DELETE",
        headers={"Authorization": f"Bearer {token}"},
    )
    status = ""
    try:
        with urllib.request.urlopen(request, timeout=120, context=_context(verify)) as response:
            status = str(response.status)
    except urllib.error.HTTPError as exc:
        status = str(exc.code)
    except (urllib.error.URLError, OSError) as exc:
        status = type(exc).__name__

    until = time.monotonic() + deadline
    while True:
        try:
            if name not in source_names(engine_url, verify, token):
                return f"removed {name} (delete answered {status})"
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            pass
        if time.monotonic() >= until:
            return f"could NOT remove {name}: still there {deadline:.0f}s after a {status}"
        time.sleep(5)


def engine_token(engine_url: str, verify: bool, user: str, password: str) -> str:
    """A bearer token for the run's own tidy-up, or empty when login fails."""
    request = urllib.request.Request(
        f"{engine_url.rstrip('/')}/api/v1/auth/login",
        data=json.dumps({"username": user, "password": password}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60, context=_context(verify)) as response:
            return str(json.loads(response.read())["access_token"])
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return ""


def _fetch(url: str, verify: bool) -> dict:
    """One unauthenticated GET, decoded."""
    with urllib.request.urlopen(url, timeout=30, context=_context(verify)) as response:
        return json.loads(response.read())


def setup_status(engine_url: str, verify: bool) -> dict:
    """The deployment's setup contract.

    Args:
        engine_url: Engine API base.
        verify: Whether to verify TLS.

    Returns:
        The setup-status document.

    Raises:
        OnboardingError: The engine would not answer.
    """
    url = f"{engine_url.rstrip('/')}/api/v1/auth/setup-status"
    try:
        return _fetch(url, verify)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise wizard.OnboardingError(f"the engine did not serve {url}: {exc}") from exc


class Driver:
    """One browser session, walking one deployment."""

    def __init__(self, page, ui_url: str, shots: Path) -> None:
        self.page = page
        self.ui = ui_url.rstrip("/")
        self.shots = shots
        self.results: list[wizard.StepResult] = []
        self.seen: list[str] = []

    def shot(self, name: str) -> str:
        """Screenshot the current page, and return where it went."""
        self.shots.mkdir(parents=True, exist_ok=True)
        path = self.shots / f"{len(self.results):02d}-{name}.png"
        self.page.screenshot(path=str(path), full_page=True)
        return str(path)

    def record(self, slug: str, status: str, detail: str) -> None:
        self.results.append(wizard.StepResult(slug, status, detail, self.shot(slug)))

    def current_slug(self) -> str:
        """The wizard screen the console is on, or empty when it is not on one."""
        path = self.page.url.split("?")[0].rstrip("/")
        segment = path.rsplit("/", 1)[-1]
        return segment if segment in wizard.WIZARD_SLUGS else ""

    def button(self, name: str):
        return self.page.get_by_role("button", name=name, exact=True)

    def textbox(self, name: str):
        return self.page.get_by_role("textbox", name=name, exact=True)


def sign_in(driver: Driver, user: str, password: str) -> None:
    """Log in through the console's own form as a local account."""
    # The console is a single-page app that never goes network-idle, so the
    # form is waited for by its own control rather than by the page settling.
    driver.page.goto(f"{driver.ui}/login", wait_until="domcontentloaded",
                     timeout=STEP_TIMEOUT_MS * 2)
    # A deployment with an identity provider registered opens on the OIDC tab,
    # and the local accounts sit behind the other one.
    local = driver.page.get_by_role("tab", name=LOCAL_LOGIN_TAB, exact=True)
    if local.count():
        local.first.click(timeout=STEP_TIMEOUT_MS)
    driver.textbox("Username").wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
    driver.textbox("Username").fill(user)
    driver.textbox("Password").fill(password)
    driver.button("Login").click(timeout=STEP_TIMEOUT_MS)


def walk_wizard(driver: Driver, expected: tuple[str, ...], org: str, user: str, password: str,
                breakglass_password: str, admin_user: str, admin_password: str) -> None:
    """Complete every screen the deployment asks for, in the console's order.

    Each screen is completed rather than clicked past: a wizard that can be
    skipped proves nothing about whether its work lands. The wizard sits behind
    the login, so the run signs in as the admin the deploy minted first.
    """
    sign_in(driver, admin_user, admin_password)
    driver.page.wait_for_url("**/setup**", timeout=STEP_TIMEOUT_MS * 2)
    driver.record("login", "done", f"signed in as {admin_user}; the console opened the wizard")
    driver.page.goto(f"{driver.ui}/setup/{wizard.WELCOME}", wait_until="domcontentloaded",
                     timeout=STEP_TIMEOUT_MS * 2)
    driver.button("Next").wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
    driver.seen.append(driver.current_slug())
    driver.button("Next").click(timeout=STEP_TIMEOUT_MS)
    driver.record(wizard.WELCOME, "done", "the wizard opened and moved on")

    driver.page.wait_for_url(f"**/setup/{wizard.ORGANISATION}", timeout=STEP_TIMEOUT_MS)
    driver.seen.append(wizard.ORGANISATION)
    driver.textbox("Name").fill(org)
    driver.textbox("Display Name").fill(org.replace("-", " ").title())
    driver.button("Save").click(timeout=STEP_TIMEOUT_MS)
    driver.record(wizard.ORGANISATION, "done", f"created the organisation {org}")

    if wizard.FIRST_USER not in expected:
        # The first user already exists, so the organisation was the last required
        # step and the console hands over to the workspace without a complete screen.
        driver.page.wait_for_url("**/sources", timeout=STEP_TIMEOUT_MS)
        driver.record(
            wizard.COMPLETE,
            "absent-as-expected",
            "the first user already existed, so the wizard ended at the organisation",
        )
        return

    driver.page.wait_for_url(f"**/setup/{wizard.LOGIN}", timeout=STEP_TIMEOUT_MS)
    driver.seen.append(wizard.LOGIN)
    # Local accounts are the path every deployment has; an external IdP is the
    # operator's own registration and cannot be driven from here.
    driver.button("Skip for now").click(timeout=STEP_TIMEOUT_MS)
    driver.record(wizard.LOGIN, "done", "left the identity provider for the operator to register")

    driver.page.wait_for_url(f"**/setup/{wizard.FIRST_USER}", timeout=STEP_TIMEOUT_MS)
    driver.seen.append(wizard.FIRST_USER)
    driver.textbox("Username").fill(user)
    driver.textbox("Password").fill(password)
    driver.button("Create Account").click(timeout=STEP_TIMEOUT_MS)
    driver.record(wizard.FIRST_USER, "done", f"created the first user {user}")

    if wizard.RESET_BREAK_GLASS in expected:
        driver.page.wait_for_url(f"**/setup/{wizard.RESET_BREAK_GLASS}", timeout=STEP_TIMEOUT_MS)
        driver.seen.append(wizard.RESET_BREAK_GLASS)
        driver.textbox("New Password").fill(breakglass_password)
        driver.button("Reset Password").click(timeout=STEP_TIMEOUT_MS)
        driver.record(
            wizard.RESET_BREAK_GLASS,
            "tolerated",
            "this engine still declares admin_password, so the screen is expected; the "
            "break-glass password is now the credential this run was given",
        )
    else:
        landed = driver.current_slug()
        if landed == wizard.RESET_BREAK_GLASS:
            driver.record(
                wizard.RESET_BREAK_GLASS,
                "failed",
                "the engine no longer declares admin_password, so this screen must not appear",
            )
            return
        driver.record(
            wizard.RESET_BREAK_GLASS,
            "absent-as-expected",
            "the engine declares no admin_password step and the console did not ask for one",
        )

    driver.page.wait_for_url(f"**/setup/{wizard.COMPLETE}", timeout=STEP_TIMEOUT_MS)
    driver.seen.append(wizard.COMPLETE)
    driver.record(wizard.COMPLETE, "done", "the wizard finished")


def check_console(driver: Driver, user: str, password: str) -> str:
    """Log in as the account the wizard made, and use the console once.

    Returns:
        The source it created, for the caller to remove. Empty when it made none.
    """
    sign_in(driver, user, password)
    driver.page.wait_for_url("**/sources", timeout=STEP_TIMEOUT_MS * 2)
    driver.record("login", "done", f"signed in as {user}")

    missing = [
        entry
        for entry in CONSOLE_LANDMARKS
        if driver.page.get_by_role("link", name=entry, exact=True).count() == 0
    ]
    if missing:
        driver.record(
            "navigation",
            "failed",
            f"the console rendered without {', '.join(missing)}",
        )
        return ""
    driver.record("navigation", "done", f"the console renders {', '.join(CONSOLE_LANDMARKS)}")

    name = f"onboard{uuid.uuid4().hex[:8]}"
    driver.button("Add Source").first.click(timeout=STEP_TIMEOUT_MS)
    driver.textbox("Source").fill(name)
    driver.textbox("Display Name").fill(name)
    driver.textbox("Field").fill("app")
    driver.textbox("Value").fill(name)
    driver.button("Add Source").last.click(timeout=STEP_TIMEOUT_MS)
    driver.page.get_by_text("Source created successfully").wait_for(timeout=STEP_TIMEOUT_MS)
    driver.record("create-source", "done", f"created {name} through the console")
    return name


def run(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "playwright is not installed: python3 -m pip install -r "
            "scripts/acceptance/requirements.txt && python3 -m playwright install chrome",
            file=sys.stderr,
        )
        return 2

    _apply_resolve_map(args.resolve)
    admin_user, password = args.admin_user, os.environ.get("DFE_E2E_ADMIN_PASSWORD", "")
    if args.access_summary:
        # Sign in with exactly what the person who ran the deploy is holding: if
        # that file is wrong, the suite is what finds out.
        summary = Path(args.access_summary)
        if not summary.is_file():
            print(f"onboarding: no access summary at {summary}", file=sys.stderr)
            return 2
        rows = access_summary_file.parse(summary.read_text(encoding="utf-8"))
        if access_summary_file.ADMIN_LABEL not in rows:
            print(f"onboarding: {summary} carries no admin row", file=sys.stderr)
            return 2
        admin_user, password = rows[access_summary_file.ADMIN_LABEL]
    if not password:
        print(
            "the onboarding suite signs in with the password the deploy minted; pass "
            "--access-summary, or let dfe-ops acceptance export DFE_E2E_ADMIN_PASSWORD",
            file=sys.stderr,
        )
        return 2

    try:
        status = setup_status(args.engine_url, not args.insecure)
    except wizard.OnboardingError as exc:
        print(f"onboarding: {exc}", file=sys.stderr)
        return 2
    steps = wizard.engine_steps(status)
    expected = wizard.expected_slugs(steps, wizard.pending_steps(status))
    complete = wizard.setup_complete(status)
    print(f"  engine setup steps: {', '.join(steps) or 'none'}", file=sys.stderr)
    print(f"  screens this deployment must show: {', '.join(expected)}", file=sys.stderr)

    if complete and not args.console_only:
        print(
            "onboarding: this deployment has already been through the wizard, so the "
            "wizard half cannot be proved here; run it on a fresh deploy, or pass "
            "--console-only to prove the console half alone",
            file=sys.stderr,
        )
        return 1

    shots = Path(args.shots_dir)
    created = ""
    with sync_playwright() as play:
        # A clean profile every run: a browser carrying a previous session would
        # walk past the login this suite exists to exercise.
        # A gateway hostname this machine cannot resolve is mapped inside the
        # browser, so the run reaches the deployment the way a user does.
        launch_args = [f"--host-resolver-rules=MAP {host} {ip}" for host, ip in args.resolve]
        browser = play.chromium.launch(
            channel=args.channel, headless=not args.headed, args=launch_args
        )

        def fresh_context():
            return browser.new_context(
                viewport={"width": 1440, "height": 900}, ignore_https_errors=args.insecure
            )

        context = fresh_context()
        driver = Driver(context.new_page(), args.ui_url, shots)
        try:
            if not complete:
                walk_wizard(
                    driver, expected, args.org, args.first_user, password, password,
                    admin_user, password,
                )
                surplus = wizard.unexpected_screens(expected, driver.seen)
                if surplus:
                    driver.record(
                        "wizard",
                        "failed",
                        f"screens this deployment does not ask for: {', '.join(surplus)}",
                    )
                # The first user arrives on their own browser, not the admin's session.
                context.close()
                context = fresh_context()
                driver.page = context.new_page()
            # The console session is the first user's when this run created one,
            # otherwise the admin's.
            made_first_user = not complete and wizard.FIRST_USER in expected
            created = check_console(
                driver, args.first_user if made_first_user else admin_user, password
            )
        except Exception as exc:  # a Playwright timeout IS the finding
            driver.record("run", "failed", f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
        finally:
            context.close()
            browser.close()

    if created:
        token = engine_token(args.engine_url, not args.insecure, admin_user, password)
        detail = (
            remove_source(args.engine_url, not args.insecure, token, created, TEARDOWN_DEADLINE)
            if token
            else f"could NOT remove {created}: the engine refused the tidy-up login"
        )
        driver.results.append(
            wizard.StepResult(
                "teardown", "done" if detail.startswith("removed") else "failed", detail
            )
        )

    print()
    print(wizard.report_table(driver.results))
    print(f"\nscreenshots: {shots}")
    return wizard.exit_code(driver.results)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="onboarding",
        description="Drive a deployment's setup wizard and first console session.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--ui-url", required=True, help="console base URL")
    parser.add_argument("--engine-url", required=True, help="engine API base URL")
    parser.add_argument("--org", default="acceptance", help="organisation the wizard creates")
    parser.add_argument("--first-user", default="operator",
                        help="the account the wizard creates and the console then uses")
    parser.add_argument("--admin-user", default="admin",
                        help="login for --console-only, when no first user is created")
    parser.add_argument("--access-summary", default="", metavar="FILE",
                        help="the deploy's own summary, so the run signs in with what its launcher holds")
    parser.add_argument("--shots-dir", default=".tmp/onboarding",
                        help="where the per-step screenshots go")
    parser.add_argument("--channel", default="chrome",
                        help="browser channel; chrome is the testing browser")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--console-only", action="store_true",
                        help="skip the wizard on a deployment already set up")
    parser.add_argument("--insecure", action="store_true",
                        help="accept a certificate this machine does not trust")
    parser.add_argument("--resolve", action="append", default=[], metavar="HOST:IP",
                        type=_host_ip, help="resolve HOST to IP inside the browser (repeatable)")
    return parser


def _apply_resolve_map(pairs: list[tuple[str, str]]) -> None:
    """Resolve the mapped hosts to their addresses for this process's own calls.

    The browser gets the same map as a launch argument; this covers the
    urllib calls the run makes beside it, with TLS still verified (or not)
    against the hostname.
    """
    if not pairs:
        return
    mapping = dict(pairs)
    original = socket.getaddrinfo

    def resolve(host, port, *rest, **kwargs):
        return original(mapping.get(host, host), port, *rest, **kwargs)

    socket.getaddrinfo = resolve


def _host_ip(value: str) -> tuple[str, str]:
    host, sep, ip = value.rpartition(":")
    if not sep or not host or not ip:
        raise argparse.ArgumentTypeError(f"expected HOST:IP, got {value!r}")
    return host, ip


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
