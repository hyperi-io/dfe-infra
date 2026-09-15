#  Project:      dfe-infra
#  File:         acceptance/source/steps.py
#  Purpose:      The steps every source case shares -- login, sweep, deploy,
#                the instance and reporting waits, routing, the archive, the
#                HyperDX read-back -- written once and called by both cases.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""source.steps -- what is the same whatever the source is.

A filebeat source and a fetched AWS source differ in how they are created, how
records reach them and what proves the records arrived. Everything between those
halves is identical, so it lives here: one deploy with one 502/504 read-back, one
instance wait, one archive assertion. A case supplies its service name and its
deadlines; the polling is not written twice.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from acceptance.clients import Datastore, Engine, remove_source, setup_status, tls_context
from acceptance.onboarding import run as onboarding
from acceptance.onboarding import wizard

STEP_TIMEOUT_MS = onboarding.STEP_TIMEOUT_MS
# The receiver rolls onto a new rule and a per-source instance is spawned by
# Argo, so the first record can take minutes to arrive.
ROUTING_DEADLINE = 900.0
INGEST_RETRY_WINDOW = 180.0
# The archiver discovers a new landing topic on scalo's 60 s refresh and flushes
# its buffer on the archiver's own flush_age_secs (60 s by default), so a file
# for a topic created mid-run is two intervals away.
ARCHIVE_DEADLINE = 300.0
# Source names this suite mints, so a sweep can tell its own strays from a
# deployment's real sources.
RUN_PREFIXES = ("fb", "cw", "onboard")


# --- console helpers ---------------------------------------------------------


def select_option(page, combobox_index: int, text: str) -> None:
    """Pick *text* from the nth Ant Design select on the page."""
    page.get_by_role("combobox").nth(combobox_index).click(timeout=STEP_TIMEOUT_MS)
    _pick_open_option(page, text)


def select_labelled(page, label: str, text: str) -> None:
    """Pick *text* from the Ant Design select the form labels *label*.

    By label rather than by position: the source form force-renders every tab,
    so the page's combobox order counts controls the operator cannot see.
    """
    page.get_by_label(label, exact=True).click(timeout=STEP_TIMEOUT_MS)
    _pick_open_option(page, text)


def _pick_open_option(page, text: str) -> None:
    option = page.locator(
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content",
        has_text=text,
    )
    option.first.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
    option.first.click(timeout=STEP_TIMEOUT_MS)


def refusal(exc: Exception) -> str:
    """One line naming what the console could not do, for an api-fallback row."""
    return f"the console could not: {type(exc).__name__}: {str(exc).splitlines()[0]}"


def console_outcome(console_detail: str, refused: str, api_detail: str) -> tuple[str, str]:
    """The (status, detail) a console-first step records.

    The console doing the work is the pass. A control the console does not have
    is a finding about the console, not a failed run, so the API does the work
    and the row says so -- which is what lets an older console still pass.

    Args:
        console_detail: What the console did, when it could.
        refused: Why it could not; empty when it did.
        api_detail: What the API did instead.

    Returns:
        The status and detail for ``Driver.record``.
    """
    if not refused:
        return "done", console_detail
    return "api-fallback", f"{api_detail} ({refused})"


def open_console(driver, engine: Engine, admin_user: str, password: str, org: str, first_user: str) -> None:
    """Sign in, completing the first-login wizard when the deployment still owes it.

    The wizard sits behind the login and holds the console there until it is
    finished, so a deployment nobody has onboarded has no Sources page for this
    suite to test against. The walk is the onboarding suite's own, not a second
    copy of it, and it records the login and each screen as it goes.
    """
    status = setup_status(engine.base, engine.verify)
    if wizard.setup_complete(status):
        onboarding.sign_in(driver, admin_user, password)
        driver.page.wait_for_url("**/sources", timeout=STEP_TIMEOUT_MS * 2)
        driver.record("login", "done", f"signed in as {admin_user}")
        return
    onboarding.walk_wizard(
        driver,
        wizard.expected_slugs(wizard.engine_steps(status), wizard.pending_steps(status)),
        org, first_user, password, password, admin_user, password,
    )
    # The wizard hands the console to the account it just made; the rest of this
    # run is the admin's, which is who its API calls are.
    onboarding.sign_in(driver, admin_user, password)
    driver.page.wait_for_url("**/sources", timeout=STEP_TIMEOUT_MS * 2)
    driver.record("console", "done", f"the console opened for {admin_user} after the setup wizard")


def sweep_strays(engine: Engine, verify: bool) -> str:
    """Remove sources an earlier run left behind, so this run starts clean."""
    listing = engine.call("GET", "/sources")
    names = [str(item["name"]) for item in (listing.body or {}).get("items", [])]
    strays = [n for n in names if n.startswith(RUN_PREFIXES) and n != "filebeat"]
    removed = [remove_source(engine.base, verify, engine.token, n, 60.0) for n in strays]
    return "; ".join(removed) if removed else "no strays"


# --- the source's own lifecycle ----------------------------------------------


def deploy(engine: Engine, name: str) -> dict:
    reply = engine.call("POST", f"/sources/{name}/deploy")
    if reply.status in (502, 504):
        # A deploy commits to the deploy repo and reconciles the apps, which can
        # outlast a gateway's upstream timeout; the read-back is the verdict.
        until = time.monotonic() + 180
        while time.monotonic() < until:
            current = engine.call("GET", f"/sources/{name}")
            if current.status == 200 and current.body.get("deployed_version"):
                return {"applied": True, "topics_ensured": [], "apps_synced": [f"read back after a {reply.status}"]}
            time.sleep(5)
    if reply.status != 200 or not reply.body.get("applied"):
        raise RuntimeError(f"deploy did not apply: {reply.status} {reply.body}")
    if reply.body.get("apps_sync_error"):
        raise RuntimeError(f"the apps could not follow the source: {reply.body['apps_sync_error']}")
    return reply.body


def record_deploy(driver, engine: Engine, name: str) -> dict:
    """The deploy step, and the body the steps after it read."""
    deployed = deploy(engine, name)
    driver.record(
        "deploy", "done",
        f"applied; topics ensured {sorted(deployed.get('topics_ensured') or [])}; apps synced {deployed.get('apps_synced')}",
    )
    return deployed


def wait_instance(engine: Engine, service: str, name: str, deadline: float) -> str:
    """The instance the source owns appears in the apps list."""
    until = time.monotonic() + deadline
    while True:
        apps = engine.call("GET", "/apps")
        entry = next((a for a in (apps.body or []) if a.get("service") == service), None)
        instances = [str(i) for i in (entry or {}).get("instances", [])]
        if name in instances:
            return f"{service}/{name} is an instance the engine knows"
        if time.monotonic() >= until:
            return f"no {service}/{name} instance after {deadline:.0f}s; instances: {instances}"
        time.sleep(5)


def wait_reporting(engine: Engine, service: str, name: str, deadline: float) -> str:
    """The instance is up and reporting telemetry through the engine."""
    until = time.monotonic() + deadline
    last = ""
    while True:
        reply = engine.call("GET", f"/apps/{service}/{name}/status")
        if reply.status == 200 and reply.body.get("reporting"):
            return f"{service}/{name} reporting after {int(reply.body.get('uptime_seconds') or 0)}s up"
        last = f"{reply.status} {reply.body}"
        if time.monotonic() >= until:
            return f"{service}/{name} not reporting after {deadline:.0f}s; last {last[:160]}"
        time.sleep(10)


def record_instance_up(driver, engine: Engine, service: str, name: str,
                       instance_step: str, instance_deadline: float,
                       reporting_step: str, reporting_deadline: float) -> None:
    """The two waits every per-source app gets, under the case's own step names."""
    detail = wait_instance(engine, service, name, instance_deadline)
    driver.record(instance_step, "done" if "knows" in detail else "failed", detail)
    detail = wait_reporting(engine, service, name, reporting_deadline)
    driver.record(reporting_step, "done" if "reporting after" in detail else "failed", detail)


def record_table(driver, store: Datastore, name: str) -> None:
    """The source's own table exists, when this run can read the datastore."""
    exists = store.table_exists(name) if store.host else None
    driver.record(
        "table", "done" if exists else ("skipped" if exists is None else "failed"),
        f"dfe.{name} {'exists' if exists else 'is missing'}" if exists is not None else "no datastore access in this run",
    )


# --- feeding and proving -----------------------------------------------------


def post_corpus(receiver_url: str, verify: bool, engine_repo: Path, corpus_file: Path,
                name: str, run: str, per_module: int) -> int:
    """POST the wrapped corpus, one request per record."""
    sys.path.insert(0, str(engine_repo))
    from tests.e2e import filebeat_corpus as corpus  # type: ignore[import-not-found]

    items = corpus.samples(corpus_file, limit=per_module)
    bodies = corpus.wrap_all(items, source=name, run=run)

    for body in bodies:
        request = urllib.request.Request(
            receiver_url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        # The receiver rolls onto a new rule mid-run, so a dropped connection
        # or a 5xx from a pod on its way out is retried; a 4xx is a rejection.
        until = time.monotonic() + INGEST_RETRY_WINDOW
        while True:
            try:
                with urllib.request.urlopen(request, timeout=30, context=tls_context(verify)) as response:
                    if response.status >= 300:
                        raise RuntimeError(f"the receiver refused a record: {response.status}")
                break
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    raise RuntimeError(f"the receiver rejected a record: {exc.code} {exc.read()[:200]!r}") from exc
                if time.monotonic() >= until:
                    raise RuntimeError(f"the receiver kept failing for {INGEST_RETRY_WINDOW:.0f}s: {exc.code}") from exc
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                if time.monotonic() >= until:
                    raise RuntimeError(f"the receiver never answered within {INGEST_RETRY_WINDOW:.0f}s: {exc}") from exc
            time.sleep(2)
    return len(bodies)


def wait_routed(receiver_url: str, verify: bool, engine_repo: Path, corpus_file: Path,
                store: Datastore, name: str, deadline: float) -> str:
    """Probe until a record actually reaches the source's table.

    Before the receiver has rolled onto the new rule the probes land in the
    catch-all, so the run's own payload is sent only once this returns.

    The proof is the table growing, not a marker: a transform rewrites each
    record into the program's own shape and the probe tag does not survive it,
    so a marker search passes only while the transform is not yet consuming.
    """
    probe = f"probe-{uuid.uuid4().hex[:8]}"
    until = time.monotonic() + deadline
    before = store.scalar(f"SELECT count() FROM {name}")
    passes = 0
    while True:
        post_corpus(receiver_url, verify, engine_repo, corpus_file, name, probe, 1)
        passes += 1
        if store.scalar(f"SELECT count() FROM {name}") > before:
            return f"routed into dfe.{name} after {passes} probe pass(es)"
        if time.monotonic() >= until:
            return f"NOT routed into dfe.{name} within {deadline:.0f}s ({passes} probe passes)"
        time.sleep(20)


def wait_gain(store: Datastore, table: str, baseline: int, wanted: int, deadline: float,
              where: str = "", interval: float = 5.0) -> int:
    """Rows gained over *baseline*, polled until *wanted* arrive or the deadline passes.

    A transformed source table carries the program's output, not the record as
    posted, so a run marker cannot be searched for there; the gain is the proof.
    """
    until = time.monotonic() + deadline
    while True:
        gained = store.scalar(f"SELECT count() FROM {table}{where}") - baseline
        if gained >= wanted:
            return gained
        if time.monotonic() >= until:
            return gained
        time.sleep(interval)


# --- the archive -------------------------------------------------------------


def in_archiver(prefix: list[str], script: str) -> tuple[int, str]:
    """Run *script* under a shell inside the archiver, wherever it runs.

    The prefix is the whole of what makes this k8s or Compose: `kubectl exec
    deploy/dfe-archiver --` or `docker exec dfe-archiver`. Nothing else in this
    step knows which one it is.
    """
    done = subprocess.run(
        [*prefix, "sh", "-c", script], capture_output=True, text=True, check=False
    )
    return done.returncode, (done.stdout or done.stderr).strip()


def archive_directory(prefixes: list[list[str]]) -> tuple[str, str]:
    """The local directory the archiver writes to, or why this step cannot run.

    Read from the container rather than passed in, so the runner carries no
    second copy of a path the chart owns.
    """
    for prefix in prefixes:
        code, out = in_archiver(prefix, "printenv ARCHIVER_DESTINATION")
        if code != 0 or not out:
            continue
        if not out.startswith("file://"):
            return "", f"destination {out} is not local disk; this step proves the file path only"
        return out[len("file://") :], ""
    return "", "the archiver names no destination, so it archives nothing"


def wait_archived(
    prefixes: list[list[str]], directory: str, name: str, deadline: float
) -> tuple[str, bool]:
    """Files under the run's own landing topic, polled until the archiver flushes.

    The topic directory carries the source name, which is minted per run, so a
    file below it belongs to this run and nothing else. Its CONTENT is
    zstd-compressed and the image ships no decompressor, so the proof is the
    path and a non-zero size. Every replica is asked: the consumer group splits
    the topic's partitions, so the run's records are archived by whichever
    replicas hold them.
    """
    topic = f"{name}_land"
    # -printf is GNU findutils, which every DFE app image carries (debian-slim).
    script = f"find {directory}/{topic} -type f -size +0c -printf '%s %p\\n'"
    until = time.monotonic() + deadline
    while True:
        found: list[str] = []
        for prefix in prefixes:
            code, out = in_archiver(prefix, script)
            if code == 0:
                found += [line for line in out.splitlines() if line.strip()]
        if found:
            total = sum(int(line.split(" ", 1)[0]) for line in found)
            return (
                f"{len(found)} file(s) across {len(prefixes)} replica(s), {total} bytes "
                f"under {topic}, first {found[0].split(' ', 1)[1]}",
                True,
            )
        if time.monotonic() >= until:
            return (
                f"no archive file under {directory}/{topic} on any of {len(prefixes)} "
                f"replica(s) within {deadline:.0f}s",
                False,
            )
        time.sleep(15)


def record_archive(driver, archive_exec: list[list[str]], name: str) -> None:
    """The archive step, shared by every case: the source's own landing topic on disk."""
    if not archive_exec:
        driver.record("archived", "skipped", "no --archive-exec: nothing to look inside")
        return
    directory, refused = archive_directory(archive_exec)
    if refused:
        driver.record("archived", "skipped", refused)
        return
    detail, wrote = wait_archived(archive_exec, directory, name, ARCHIVE_DEADLINE)
    driver.record("archived", "done" if wrote else "failed", detail)


def apply_restarts(driver, prefix: list[str], reported: list[str]) -> None:
    """Restart the apps the engine says cannot take a write where they stand.

    The engine computes this, so the runner does not have to: a hint appears only
    where the engine renders the app's config itself -- a Compose deployment that
    named a config directory -- and only where the write changed something the
    running process cannot pick up. A Kubernetes deploy therefore reports none,
    because the chart's checksum rolls the pod.

    Without it the app takes the new file and goes on consuming the topics it
    started with, so the source it was just given never moves a record.
    """
    hints = sorted({str(hint) for hint in reported if hint})
    if not hints:
        driver.record("restart", "skipped", "the engine reports every write taken where it stands")
        return
    if not prefix:
        driver.record(
            "restart", "failed",
            f"{len(hints)} app(s) need restarting and this run was given no --restart-exec: {hints}",
        )
        return
    done = []
    for hint in hints:
        # appconfig.RESTART_HINT: "restart required: docker compose restart
        # <service>", so the service is the last word and the rest is the reason.
        service = hint.rsplit(" ", 1)[-1]
        reply = subprocess.run([*prefix, service], capture_output=True, text=True, check=False)
        done.append(f"{service} {'restarted' if reply.returncode == 0 else f'REFUSED: {(reply.stderr or reply.stdout).strip()[:120]}'}")
    driver.record(
        "restart",
        "done" if all("restarted" in entry for entry in done) else "failed",
        "; ".join(done),
    )


# --- HyperDX -----------------------------------------------------------------


def hyperdx_teams_holding(engine: Engine, name: str) -> tuple[list[str], int, str]:
    """Teams carrying the HyperDX source *name*, how many hold any, and why not.

    The engine reads this back off the fork, so it is the fork's own account of
    where the source landed rather than the deploy's word for it.
    """
    reply = engine.call("GET", "/hyperdx/sources")
    if reply.status == 404:
        return [], 0, "this engine build has no HyperDX source listing"
    if reply.status != 200:
        return [], 0, f"the listing answered {reply.status}"

    teams = (reply.body or {}).get("teams") or []
    holding = [
        str(team.get("team_name") or team.get("team"))
        for team in teams
        if any(source.get("name") == name for source in team.get("sources") or [])
    ]
    with_any = sum(1 for team in teams if team.get("sources"))
    return holding, with_any, ""


def assert_hyperdx_source(engine: Engine, name: str) -> tuple[str, str]:
    """The step result for `name` being present on the teams a human reads from."""
    holding, with_any, refused = hyperdx_teams_holding(engine, name)
    if refused:
        return "skipped", refused
    if holding:
        return "done", f"HyperDX source {name} on {len(holding)} team(s): {', '.join(holding)}"
    if with_any == 0:
        # A team is created on first HyperDX sign-in and gets its ClickHouse
        # connection with it, so a deployment nobody has signed into has no team
        # for the source to land on and the write had nowhere to go.
        return "skipped", "no HyperDX team holds any DFE source yet; nobody has signed in"
    return "failed", f"no HyperDX team carries {name}, though {with_any} hold other DFE sources"


def assert_hyperdx_source_gone(engine: Engine, name: str) -> tuple[str, str]:
    """The step result for `name` being off every team after teardown."""
    holding, _, refused = hyperdx_teams_holding(engine, name)
    if refused:
        return "skipped", refused
    if holding:
        return "failed", f"HyperDX source {name} still on {', '.join(holding)}"
    return "done", f"HyperDX source {name} is gone from every team"


def record_hyperdx_source(driver, engine: Engine, name: str, deployed: dict) -> None:
    """Read the source back off the fork rather than trusting the deploy's own report.

    The deploy counts what it wrote, the listing says what is there, and only the
    second is what a human opens HyperDX to.
    """
    state, detail = assert_hyperdx_source(engine, name)
    if state == "failed" and deployed.get("hyperdx_source_error"):
        detail = f"{detail}; the deploy said: {deployed['hyperdx_source_error']}"
    driver.record("hyperdx-source", state, detail)


# --- the console's Observe page ----------------------------------------------

# The rows were fed minutes ago and HyperDX searches the last fifteen by default,
# so the wait is for the query to run, not for data to arrive.
OBSERVE_DEADLINE = 90.0
RESULTS_LINE = re.compile(r"^(\d+) Results?$")


def observe_outcome(name: str, frame_url: str, blocked: str, picked: bool, results: str) -> tuple[str, str]:
    """The step result for the console's Observe search over *name*.

    Args:
        name: The source the search should show rows for.
        frame_url: Where the HyperDX frame ended up; a chrome-error URL is a frame the browser refused.
        blocked: The console's own error line when it refused the frame.
        picked: Whether the frame's source picker offered the source.
        results: The frame's results line, such as "12 Results".

    Returns:
        The status and detail for ``Driver.record``.
    """
    if not frame_url or frame_url.startswith("chrome-error://"):
        return "failed", f"the HyperDX frame did not load ({blocked or 'no frame on the page'})"
    if not picked:
        return "failed", f"the frame's source picker does not offer {name}"
    found = RESULTS_LINE.match(results or "")
    if found and int(found.group(1)) > 0:
        return "done", f"Observe search over {name}: {results}"
    return "failed", f"Observe search over {name} returned {results or 'no results line'}"


def hyperdx_frame(page):
    """The HyperDX frame the console embeds, once the browser has settled it."""
    page.locator("iframe").first.wait_for(state="attached", timeout=STEP_TIMEOUT_MS)
    handle = page.locator("iframe").first.element_handle()
    frame = handle.content_frame() if handle else None
    if frame is not None and not frame.url.startswith("chrome-error://"):
        frame.wait_for_load_state("domcontentloaded", timeout=STEP_TIMEOUT_MS)
    return frame


def search_results(frame, name: str) -> tuple[bool, str]:
    """Pick *name* in the frame's source picker and read the results line."""
    picker = frame.get_by_placeholder("Data Source")
    picker.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
    # The frame opens on a source of its own choosing; when that is already this
    # one the picker holds its name and the dropdown does not list it again.
    if picker.input_value().strip() != name:
        picker.click(timeout=STEP_TIMEOUT_MS)
        picker.fill(name)
        option = frame.get_by_role("option").filter(has_text=name)
        # count() takes no auto-wait, so right after fill() it can read the dropdown
        # before a slow render populates it. A genuinely absent option still falls
        # through to the count() check below.
        try:
            option.first.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
        except Exception:  # absence is decided by count() below, not this wait
            pass
        if not option.count():
            return False, ""
        option.first.click(timeout=STEP_TIMEOUT_MS)
    frame.get_by_role("button", name="Run", exact=True).click(timeout=STEP_TIMEOUT_MS)
    line = frame.get_by_text(RESULTS_LINE)
    until = time.monotonic() + OBSERVE_DEADLINE
    results = ""
    while True:
        if line.count():
            results = line.first.inner_text().strip()
            found = RESULTS_LINE.match(results)
            if found and int(found.group(1)) > 0:
                return True, results
        if time.monotonic() >= until:
            return True, results
        time.sleep(5)


def record_observe(driver, name: str) -> None:
    """Open the console's Observe search on the source and see its rows.

    The console iframes HyperDX from a second origin, so this one step meets
    what a tester meets: the embed's frame-ancestors, the login shared across
    the two origins, and the source's own view, together.
    """
    page = driver.page
    blocked: list[str] = []

    def on_console(message) -> None:
        if message.type == "error" and "frame-ancestors" in message.text:
            blocked.append(message.text.splitlines()[0])

    page.on("console", on_console)
    frame_url, picked, results = "", False, ""
    try:
        page.goto(f"{driver.ui}/observe/search", wait_until="domcontentloaded", timeout=STEP_TIMEOUT_MS * 2)
        frame = hyperdx_frame(page)
        frame_url = frame.url if frame is not None else ""
        if frame is not None and not frame_url.startswith("chrome-error://"):
            picked, results = search_results(frame, name)
    except Exception as exc:
        results = refusal(exc)
    finally:
        page.remove_listener("console", on_console)
    driver.record("observe", *observe_outcome(name, frame_url, "; ".join(blocked), picked, results))
