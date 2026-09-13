#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         acceptance/onboarding/wizard.py
#  Purpose:      What the setup wizard's steps ARE, and which of them this
#                deployment is expected to show -- decided from the engine's own
#                setup contract rather than a release number, so the same file
#                is strict on a stack that dropped a step and tolerant of one
#                that has not been upgraded yet. Stdlib only, no browser.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""onboarding.wizard -- the wizard's shape, without driving it.

The console walks a fixed list of screens and the engine's ``auth/setup-status``
says which of them the deployment still needs. Keeping the two apart here is what
lets the acceptance run be STRICT: a screen the engine no longer asks for must
not appear, and a screen it does ask for must be completed rather than skipped
past.

Nothing here opens a browser or a socket. It takes the setup-status document and
returns the plan the driver walks, so ``scripts/tests/test_onboarding.py``
exercises every branch with fabricated documents.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

# The console's own order (dfe-ui SetupWizard/server.helpers.ts SETUP_WIZARD_STEPS).
# The slug is the URL segment: /setup/<slug>.
WELCOME = "welcome"
ORGANISATION = "configureOrganisation"
LOGIN = "configureLogin"
FIRST_USER = "configureUser"
RESET_BREAK_GLASS = "resetBreakGlassAccount"
COMPLETE = "complete"

WIZARD_SLUGS: tuple[str, ...] = (
    WELCOME,
    ORGANISATION,
    LOGIN,
    FIRST_USER,
    RESET_BREAK_GLASS,
    COMPLETE,
)

# The engine step that puts the break-glass screen in front of the operator. A
# deployment whose setup contract no longer lists it must not render the screen.
ADMIN_PASSWORD_STEP = "admin_password"


class OnboardingError(RuntimeError):
    """The deployment cannot be onboarded, and the run says why."""


@dataclass(frozen=True, slots=True)
class StepResult:
    """One screen's outcome, for the table the run returns."""

    slug: str
    status: str
    """done, tolerated, absent-as-expected, or failed."""

    detail: str
    shot: str = ""
    """Screenshot path, when one was taken."""


def engine_steps(status: Mapping) -> tuple[str, ...]:
    """The steps the engine's setup contract declares.

    Args:
        status: The ``GET /api/v1/auth/setup-status`` document.

    Returns:
        The step ids, in the engine's order.

    Raises:
        OnboardingError: The document carries no initial_setup block.
    """
    initial = status.get("initial_setup")
    if not isinstance(initial, Mapping):
        raise OnboardingError(
            "setup-status carried no initial_setup block, so what the wizard must "
            "show cannot be decided"
        )
    return tuple(str(s) for s in initial.get("steps") or ())


def setup_complete(status: Mapping) -> bool:
    """Whether this deployment has already been through the wizard."""
    initial = status.get("initial_setup")
    return bool(isinstance(initial, Mapping) and initial.get("complete"))


def pending_steps(status: Mapping) -> tuple[str, ...]:
    """The declared steps the engine still reports as pending.

    Args:
        status: The ``GET /api/v1/auth/setup-status`` document.

    Returns:
        The pending step ids, in the engine's order; every declared step when the
        document does not say.
    """
    initial = status.get("initial_setup")
    if not isinstance(initial, Mapping) or initial.get("pending_steps") is None:
        return engine_steps(status)
    return tuple(str(s) for s in initial.get("pending_steps") or ())


def expected_slugs(steps: Iterable[str], pending: Iterable[str] | None = None) -> tuple[str, ...]:
    """The screens this deployment must show, given the engine's steps.

    The break-glass reset screen is driven by the engine's ``admin_password``
    step. Reading the deployment's own contract rather than a version keeps one
    rule for both releases: while the engine asks for the step the screen is
    expected, and the moment it stops the screen must be gone.

    The console leaves the wizard the moment no required step is pending, so a
    deployment whose first user already exists (seeded accounts) shows the
    organisation screen and nothing after it, and never reaches ``complete``.

    Args:
        steps: The engine's declared setup steps.
        pending: The steps still pending; every declared step when omitted.

    Returns:
        The wizard slugs the run walks, in console order.
    """
    declared = set(steps)
    still = declared if pending is None else set(pending)
    slugs = [WELCOME]
    if "organisations" in still:
        slugs.append(ORGANISATION)
    if "first_user" in still:
        slugs += [LOGIN, FIRST_USER]
        if ADMIN_PASSWORD_STEP in declared and ADMIN_PASSWORD_STEP in still:
            slugs.append(RESET_BREAK_GLASS)
        slugs.append(COMPLETE)
    return tuple(slugs)


def unexpected_screens(expected: Sequence[str], seen: Iterable[str]) -> tuple[str, ...]:
    """Screens the run met that the deployment's contract does not ask for.

    A screen the engine dropped but the console still renders is the failure this
    exists to catch: the operator is asked for something the product no longer
    needs, and no assertion on the screens that DID appear would notice.

    Args:
        expected: The slugs ``expected_slugs`` returned.
        seen: The slugs the driver actually landed on.

    Returns:
        The surplus slugs, in the order they were met.
    """
    allowed = set(expected)
    return tuple(slug for slug in seen if slug not in allowed)


def report_table(results: Sequence[StepResult]) -> str:
    """The per-step table, one row per screen.

    Args:
        results: The run's outcomes, in the order they happened.

    Returns:
        A fixed-width table.
    """
    width = max((len(r.slug) for r in results), default=4)
    status = max((len(r.status) for r in results), default=6)
    rows = [f"{'step':<{width}}  {'status':<{status}}  detail"]
    rows += [f"{r.slug:<{width}}  {r.status:<{status}}  {r.detail}" for r in results]
    return "\n".join(rows)


def exit_code(results: Sequence[StepResult]) -> int:
    """0 only when every screen finished. Any failure fails the deploy."""
    return 1 if any(r.status == "failed" for r in results) else 0
