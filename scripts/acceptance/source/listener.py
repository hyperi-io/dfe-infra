#  Project:      dfe-infra
#  File:         acceptance/source/listener.py
#  Purpose:      Switch a receiver listener on through the engine for one run,
#                and put the overlay back as the run found it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""source.listener -- a receiver listener a run opens and closes again.

The lumberjack and syslog listeners are off by default and refuse to start
without client authentication unless told to accept every client that can reach
them. A run that needs one writes the switches through the engine, and puts the
paths back to what the overlay held before it, so the receiver is left as found.
"""

import shlex
from collections.abc import Mapping
from urllib.parse import urlencode

from acceptance.onboarding import wizard
from acceptance.source import beats, steps

RECEIVER_SERVICE = beats.RECEIVER_SERVICE


class Listener:
    """One listener's switches, and what the overlay held at those paths before the run."""

    def __init__(
        self, slug: str, switches: Mapping[str, object], env_hint: str
    ) -> None:
        """Name the listener.

        Args:
            slug: The listener's name in the step table, as in ``receiver-lumberjack``.
            switches: The overlay paths the run sets, and the value each takes.
            env_hint: The receiver environment that does the same, for an engine that refuses.
        """
        self.slug = slug
        self.switches = dict(switches)
        self.env_hint = env_hint
        self._opened: tuple[str, dict[str, object]] | None = None

    @property
    def is_open(self) -> bool:
        return self._opened is not None

    @property
    def route(self) -> str:
        """The config route the switches were written to, empty before :meth:`open`."""
        return self._opened[0] if self._opened else ""

    @staticmethod
    def _route(run) -> str:
        """The receiver's config route, or empty when the engine lists no instance of it."""
        for entry in run.engine.call("GET", "/apps").body or []:
            if isinstance(entry, dict) and entry.get("service") == RECEIVER_SERVICE:
                instances = [str(i) for i in entry.get("instances") or []]
                if instances:
                    return f"/apps/{RECEIVER_SERVICE}/{instances[0]}/config"
        return ""

    def open(self, run) -> list[str] | None:
        """Write the switches, remembering what they replaced.

        Nothing is written that cannot be put back: the overlay's current values at
        those paths are read first, and :meth:`close` restores them.

        Returns:
            The restart hints the write reported, or None when the listener did not open.
        """
        driver = run.driver
        route = self._route(run)
        if not route:
            driver.record(
                self.slug, "failed", f"the engine lists no {RECEIVER_SERVICE} instance"
            )
            return None
        current = run.engine.call("GET", route)
        if current.status != 200:
            driver.record(
                self.slug,
                "failed",
                f"could not read {route} to put it back afterwards: {current.status} {current.body}",
            )
            return None
        before = beats.held(current.body, self.switches)
        wrote = run.engine.call("PUT", route, {"changes": dict(self.switches)})
        if wrote.status != 200:
            driver.record(
                self.slug,
                "failed",
                f"the engine refused the switch: {wrote.status} {wrote.body}. Set {self.env_hint} "
                "in the receiver's environment instead",
            )
            return None
        self._opened = (route, before)
        driver.record(
            self.slug,
            "done",
            f"{', '.join(self.switches)} set through {route} for this run, and "
            f"put back by the teardown to what the overlay held ({before or 'nothing'})",
        )
        return steps.reply_hints(wrote.body)

    def close(self, run) -> wizard.StepResult:
        """Put the paths back as the run found them, and apply that."""
        route, before = self._opened or ("", {})
        rewrite, clears = beats.restore_plan(before, self.switches)
        hints: list[str] = []
        refused = []
        if rewrite:
            reply = run.engine.call("PUT", route, {"changes": rewrite})
            hints += steps.reply_hints(reply.body)
            if reply.status != 200:
                refused.append(f"rewrite {reply.status} {reply.body}")
        if clears:
            query = urlencode({"path": clears}, doseq=True)
            reply = run.engine.call("DELETE", f"{route}?{query}")
            hints += steps.reply_hints(reply.body)
            if reply.status != 200:
                refused.append(f"clear {reply.status} {reply.body}")
        restored = f"{self.slug}-restored"
        if refused:
            return wizard.StepResult(
                restored,
                "failed",
                f"{route} was not put back ({', '.join(refused)}), so the listener may still be open",
            )
        status, detail = steps.restart_outcome(
            shlex.split(run.args.restart_exec), hints
        )
        return wizard.StepResult(
            restored,
            "failed" if status == "failed" else "done",
            f"{route} put back to {before or 'the app defaults'}, restart {status}: {detail}",
        )
