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
#     --suite onboarding fetches it from the lane and exports it. The console
#     forces the admin to change that issued password at first login, to
#     DFE_E2E_ADMIN_NEW_PASSWORD, which the run signs the admin in with after.)
"""onboarding.run -- the wizard, then the console, in a browser.

The first thing an operator does with a new deployment is open it and be walked
through setup. Nothing else in the acceptance suite covers that: the API tests
authenticate straight past it, so a wizard that cannot be finished ships.

Two phases. The wizard phase signs in as the admin the deploy minted (the wizard
sits behind the login), makes the password change the console forces on that
issued credential, and walks the screens the engine's own setup contract asks
for -- and asserts that a screen it does NOT ask for never appears, which is what
catches a console still demanding a step the product dropped. Then the console
phase logs in as the account the wizard just created, checks the navigation
renders, and creates one source through the UI.

Every step screenshots. A step that cannot complete fails the run, and the run's
exit code fails the deploy.
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import access_summary as access_summary_file

from acceptance.clients import engine_token, remove_source, setup_status
from acceptance.onboarding import wizard

# The nav entries every role sees, so the check is a rendered console rather than
# a page that returned 200 with an empty shell.
CONSOLE_LANDMARKS = ("Sources", "Meta Schemas")

# The login page's local-account tab, shown once a provider is registered.
LOCAL_LOGIN_TAB = "Login with Local"

# The Add Source drawer's name field: "Source Name *" from ui v1.7.0, "Source" before it.
SOURCE_NAME_LABELS = ("Source Name", "Source")

# Where the console holds an account on an issued password until it sets its own.
CHANGE_PASSWORD_PATH = "/change-password"

STEP_TIMEOUT_MS = 30_000
# Any wizard screen: /setup itself redirects straight on to one.
SETUP_SCREEN = re.compile(r"/setup/\w+")
# The screens the console can open the wizard on, and the run can carry on from.
LANDINGS = (wizard.WELCOME, wizard.ORGANISATION, wizard.LOGIN)
# How long the run keeps checking that the source it made has gone.
TEARDOWN_DEADLINE = 120.0


def _label(*names: str) -> re.Pattern[str]:
    """Match a field by its label, with or without the console's required marker.

    A required field's accessible name carries the asterisk the console renders
    beside it ("Username *"), and an optional one a trailing space ("Name "), so
    an exact match finds neither. Anchoring keeps "Name" off "Username".

    Args:
        *names: Every label the field has carried across console releases; any
            one of them matches.

    Returns:
        A pattern for the field's accessible name.
    """
    alternatives = "|".join(re.escape(name) for name in names)
    return re.compile(rf"^\s*(?:{alternatives})\s*\*?\s*$")


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

    def textbox(self, *names: str):
        return self.page.get_by_role("textbox", name=_label(*names))


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


def _left_login(url: str) -> bool:
    return "/login" not in url.split("?")[0]


def sign_in_as_admin(driver: Driver, user: str, password: str, new_password: str) -> str:
    """Sign the admin in, completing the forced change a fresh deployment's admin is due.

    The admin holds either the password the deploy issued or, once a run has made
    the change, ``new_password``: the issued one is tried first and the new one
    when the console refuses it.

    Returns:
        The admin's password from here on.

    Raises:
        wizard.OnboardingError: The console demands a change and no new password
            was given, or it accepted the issued password without demanding one.
        playwright.sync_api.TimeoutError: The console refused every password
            the run holds.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    sign_in(driver, user, password)
    try:
        driver.page.wait_for_url(_left_login, timeout=STEP_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        # Still on the login form, so the issued password was refused.
        if not new_password:
            raise
        sign_in(driver, user, new_password)
        driver.page.wait_for_url(_left_login, timeout=STEP_TIMEOUT_MS)
        return new_password
    if CHANGE_PASSWORD_PATH not in driver.page.url:
        if new_password and password != new_password:
            raise wizard.OnboardingError(
                f"the console let '{user}' in on its issued password without the forced change"
            )
        return password
    if not new_password:
        raise wizard.OnboardingError(
            f"the console holds '{user}' on its issued password until it sets its own. "
            "Set DFE_E2E_ADMIN_NEW_PASSWORD to the password to change it to"
        )
    for label in ("New Password", "Confirm Password"):
        driver.textbox(label).fill(new_password)
    driver.button("Set password").click(timeout=STEP_TIMEOUT_MS)
    driver.page.wait_for_url(
        lambda url: CHANGE_PASSWORD_PATH not in url, timeout=STEP_TIMEOUT_MS * 2
    )
    driver.record("change-password", "done", f"'{user}' replaced its issued password")
    return new_password


def walk_organisation(driver: Driver, org: str) -> None:
    """Complete the organisation screen the console is on."""
    driver.page.wait_for_url(f"**/setup/{wizard.ORGANISATION}", timeout=STEP_TIMEOUT_MS)
    driver.seen.append(wizard.ORGANISATION)
    name = driver.textbox("Name")
    if name.count():
        name.fill(org)
        driver.textbox("Display Name").fill(org.replace("-", " ").title())
        driver.button("Save").click(timeout=STEP_TIMEOUT_MS)
        driver.record(wizard.ORGANISATION, "done", f"created the organisation {org}")
        return
    # A deployment that already has one shows the screen with no form on it, and
    # the walk moves on rather than asserting a second organisation.
    driver.button("Next").click(timeout=STEP_TIMEOUT_MS)
    driver.record(
        wizard.ORGANISATION, "done", "an organisation already existed, so the screen was satisfied"
    )


def walk_wizard(driver: Driver, expected: tuple[str, ...], org: str, user: str, password: str,
                breakglass_password: str, admin_user: str, admin_password: str,
                new_admin_password: str = "") -> str:
    """Complete every screen the deployment asks for, in the console's order.

    Each screen is completed rather than clicked past: a wizard that can be
    skipped proves nothing about whether its work lands. The wizard sits behind
    the login, so the run signs in as the admin the deploy minted first, and
    makes the forced password change the console puts before the wizard.

    Returns:
        The admin's password from here on.
    """
    admin_password = sign_in_as_admin(driver, admin_user, admin_password, new_admin_password)
    driver.page.wait_for_url(SETUP_SCREEN, timeout=STEP_TIMEOUT_MS * 2)
    landed = driver.current_slug()
    driver.record(
        "login",
        "done",
        f"signed in as {admin_user}; the console opened the wizard at {landed or driver.page.url}",
    )
    # The console opens on the first pending step, so the run follows it rather than
    # navigating to a screen of its own choosing.
    if landed not in LANDINGS:
        driver.record(
            "landing",
            "failed",
            f"the console opened the wizard at {landed or driver.page.url}, a screen this "
            "run does not know how to complete",
        )
        return admin_password
    if landed == wizard.WELCOME:
        driver.seen.append(wizard.WELCOME)
        driver.button("Next").click(timeout=STEP_TIMEOUT_MS)
        driver.record(wizard.WELCOME, "done", "the wizard opened on welcome and moved on")
        driver.page.wait_for_url(f"**/setup/{wizard.ORGANISATION}", timeout=STEP_TIMEOUT_MS)
        landed = wizard.ORGANISATION

    if landed == wizard.LOGIN:
        driver.record(
            wizard.ORGANISATION,
            "absent-as-expected",
            "an organisation already existed, so the console opened past its screen",
        )
    else:
        walk_organisation(driver, org)

    if wizard.FIRST_USER not in expected:
        # The first user already exists, so the organisation was the last required
        # step and the console hands over to the workspace without a complete screen.
        driver.page.wait_for_url("**/sources", timeout=STEP_TIMEOUT_MS)
        driver.record(
            wizard.COMPLETE,
            "absent-as-expected",
            "the first user already existed, so the wizard ended at the organisation",
        )
        return admin_password

    driver.page.wait_for_url(f"**/setup/{wizard.LOGIN}", timeout=STEP_TIMEOUT_MS)
    driver.seen.append(wizard.LOGIN)
    # Local accounts are the path every deployment has; an external IdP is the
    # operator's own registration and cannot be driven from here.
    driver.button("Skip for now").click(timeout=STEP_TIMEOUT_MS)
    driver.record(wizard.LOGIN, "done", "left the identity provider for the operator to register")

    driver.page.wait_for_url(f"**/setup/{wizard.FIRST_USER}", timeout=STEP_TIMEOUT_MS)
    driver.seen.append(wizard.FIRST_USER)
    driver.textbox("Username").fill(user)
    # The console requires an address here. `.invalid` is reserved, so the account
    # this run makes can never be mailed by a deployment that wires up email.
    driver.textbox("Email").fill(f"{user}@acceptance.invalid")
    driver.textbox("Password").fill(password)
    driver.button("Create Account").click(timeout=STEP_TIMEOUT_MS)
    # The console keeps a rejected form on the screen with its field errors, so
    # leaving it is what says the account was made rather than the click landing.
    driver.page.wait_for_url(
        lambda url: f"/setup/{wizard.FIRST_USER}" not in url, timeout=STEP_TIMEOUT_MS
    )
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
            return admin_password
        driver.record(
            wizard.RESET_BREAK_GLASS,
            "absent-as-expected",
            "the engine declares no admin_password step and the console did not ask for one",
        )

    driver.page.wait_for_url(f"**/setup/{wizard.COMPLETE}", timeout=STEP_TIMEOUT_MS)
    driver.seen.append(wizard.COMPLETE)
    driver.record(wizard.COMPLETE, "done", "the wizard finished")
    return admin_password


def check_console(driver: Driver, user: str, password: str, new_password: str = "") -> str:
    """Log in as the account the wizard made, and use the console once.

    ``new_password`` is for the admin, when no first user was made: a deployment
    set up by other means can still hold it on its issued password.

    Returns:
        The source it created, for the caller to remove. Empty when it made none.
    """
    if new_password:
        sign_in_as_admin(driver, user, password, new_password)
    else:
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
    driver.textbox(*SOURCE_NAME_LABELS).fill(name)
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

    # A fresh deployment's admin is forced to replace the issued password first.
    new_admin_password = os.environ.get("DFE_E2E_ADMIN_NEW_PASSWORD", "")
    admin_password = password
    shots = Path(args.shots_dir)
    created = ""
    with sync_playwright() as play:
        # A clean profile every run: a browser carrying a previous session would
        # walk past the login this suite exists to exercise.
        # A gateway hostname this machine cannot resolve is mapped inside the
        # browser, so the run reaches the deployment the way a user does.
        browser = play.chromium.launch(
            channel=args.channel, headless=not args.headed, args=resolver_args(args.resolve)
        )

        def fresh_context():
            return browser.new_context(
                viewport={"width": 1440, "height": 900}, ignore_https_errors=args.insecure
            )

        context = fresh_context()
        driver = Driver(context.new_page(), args.ui_url, shots)
        try:
            if not complete:
                admin_password = walk_wizard(
                    driver, expected, args.org, args.first_user, password, password,
                    admin_user, password, new_admin_password,
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
            if made_first_user:
                created = check_console(driver, args.first_user, password)
            else:
                created = check_console(driver, admin_user, admin_password, new_admin_password)
        except Exception as exc:  # a Playwright timeout IS the finding
            driver.record("run", "failed", f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
        finally:
            context.close()
            browser.close()

    if created:
        # The admin holds the issued password or the one this run changed it to.
        token = engine_token(args.engine_url, not args.insecure, admin_user, admin_password)
        if not token and new_admin_password:
            token = engine_token(args.engine_url, not args.insecure, admin_user, new_admin_password)
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


def resolver_args(pairs: list[tuple[str, str]]) -> list[str]:
    """The browser's launch arguments for a host map, as ONE resolver-rules flag.

    Chromium keeps only the last --host-resolver-rules it is given, so one flag
    per pair left every host but the last one unresolved.
    """
    if not pairs:
        return []
    return ["--host-resolver-rules=" + ", ".join(f"MAP {host} {ip}" for host, ip in pairs)]


def _host_ip(value: str) -> tuple[str, str]:
    host, sep, ip = value.rpartition(":")
    if not sep or not host or not ip:
        raise argparse.ArgumentTypeError(f"expected HOST:IP, got {value!r}")
    return host, ip


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
