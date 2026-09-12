#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         acceptance/source/run.py
#  Purpose:      Create a real source through the console (meta schema, receiver
#                match, transform, archive), feed it real data, and prove every
#                hop: its own table, the transform's columns, the archive.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage:
#    python3 scripts/acceptance/source/run.py \
#        --ui-url https://dfe.example --engine-url https://dfe.example \
#        --engine-repo /path/to/dfe-engine --shots-dir .tmp/source --insecure
#    (the receiver, the datastore and the admin login come from the DFE_E2E_*
#     env that dfe-ops acceptance --suite source exports; --access-summary
#     is the stand-alone alternative for the login)
"""source.run -- the post-deploy source test, in a browser.

Runs AFTER the deploy's own POST has passed. It is the first thing an operator
does with a working deployment: add a source for real data, and see it land.

The filebeat case: the shipped meta schema, the cheapest receiver match, the
bundled filebeat VRL with its timezone table, the archive on. Every step is a
row in the report and a screenshot; a step the console cannot do falls back to
the engine API and says so, because that is a finding about the console.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import access_summary as access_summary_file

from acceptance.onboarding import run as onboarding
from acceptance.onboarding import wizard
from acceptance.source import fetcher
from acceptance.source.engine import Datastore, Engine

META_SCHEMA = "meta/beats/filebeat"
META_SCHEMA_VERSION = "1.0.0"
HEADER = "common-header/timeseries"
HEADER_VERSION = "1.0.1"
MATCH_FIELD = "_source"
TRANSFORM_ENGINE = "vrl"
PROGRAM = "pipelines/filebeat/filebeat.vrl"
ENRICHMENT = "pipelines/filebeat/timezones.csv"
CORPUS = "tests/fixtures/filebeat/filebeat-testdata.tar.gz"
# A column only the transform sets on the umbrella branch; absent from the body.
TRANSFORMED_COLUMN = "log_file_path"
STEP_TIMEOUT_MS = onboarding.STEP_TIMEOUT_MS
LAND_DEADLINE = 300.0
TRANSFORM_DEADLINE = 300.0
# The receiver rolls onto a new rule and the transform instance is spawned by
# Argo, so the first record can take minutes to arrive.
ROUTING_DEADLINE = 900.0
INGEST_RETRY_WINDOW = 180.0
# The archiver discovers a new landing topic on scalo's 60 s refresh and flushes
# its buffer on the archiver's own flush_age_secs (60 s by default), so a file
# for a topic created mid-run is two intervals away.
ARCHIVE_DEADLINE = 300.0


def select_option(page, combobox_index: int, text: str) -> None:
    """Pick *text* from the nth Ant Design select on the page."""
    page.get_by_role("combobox").nth(combobox_index).click(timeout=STEP_TIMEOUT_MS)
    option = page.locator(
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content",
        has_text=text,
    )
    option.first.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
    option.first.click(timeout=STEP_TIMEOUT_MS)


def create_source_in_console(driver: onboarding.Driver, name: str) -> None:
    """Add Source: both tabs, archive on, the shipped meta schema picked."""
    # The page header repeats the nav entry as a link, so the nav one is first.
    driver.page.get_by_role("link", name="Sources", exact=True).first.click(timeout=STEP_TIMEOUT_MS)
    driver.page.wait_for_url("**/sources**", timeout=STEP_TIMEOUT_MS)
    driver.button("Add Source").first.click(timeout=STEP_TIMEOUT_MS)
    driver.page.get_by_placeholder("Enter source").fill(name)
    driver.page.get_by_placeholder("Enter display name").fill(f"Filebeat source test {name}")
    driver.page.get_by_placeholder("Enter description").fill(
        "Post-deploy source test: real filebeat lines through the bundled VRL, archived."
    )
    # The switches are Enabled (on) then Archive (off); the archive one is second.
    driver.page.get_by_role("switch").nth(1).click(timeout=STEP_TIMEOUT_MS)
    driver.page.get_by_placeholder("Enter field").fill(MATCH_FIELD)
    driver.page.get_by_placeholder("Enter value").fill(name)
    driver.record("add-source-configuration", "done", f"filled the Configuration tab for {name}, archive on")

    driver.page.get_by_role("tab", name="Meta Schema", exact=True).click(timeout=STEP_TIMEOUT_MS)
    driver.page.get_by_text("Define Schema", exact=True).click(timeout=STEP_TIMEOUT_MS)
    # The selects group by family and show the leaf: common-header > timeseries.
    select_option(driver.page, 0, HEADER.rsplit("/", 1)[-1])
    select_option(driver.page, 1, HEADER_VERSION)
    select_option(driver.page, 2, META_SCHEMA.rsplit("/", 1)[-1])
    select_option(driver.page, 3, META_SCHEMA_VERSION)
    driver.record("add-source-meta-schema", "done", f"picked {HEADER} {HEADER_VERSION} and {META_SCHEMA} {META_SCHEMA_VERSION}")

    driver.button("Add Source").last.click(timeout=STEP_TIMEOUT_MS)
    driver.page.get_by_text("Source created successfully").wait_for(timeout=STEP_TIMEOUT_MS)
    driver.record("add-source", "done", f"the console created {name}")


def attach_transform(engine: Engine, name: str, transform_repo: Path) -> str:
    """The transform and its files go through the API: the console has no control for them."""
    current = engine.call("GET", f"/sources/{name}")
    if current.status != 200:
        raise RuntimeError(f"cannot read {name} back: {current.status} {current.body}")
    body = {
        "source": name,
        "display_name": current.body.get("display_name"),
        "description": current.body.get("description"),
        "match": current.body.get("match"),
        "header": {"type": HEADER, "version": HEADER_VERSION},
        "schema": {"meta_schema": META_SCHEMA, "meta_schema_version": META_SCHEMA_VERSION},
        "transform": {"engine": TRANSFORM_ENGINE},
        "archive": True,
    }
    updated = engine.call("PUT", f"/sources/{name}", body)
    if updated.status != 200:
        raise RuntimeError(f"attaching the transform was refused: {updated.status} {updated.body}")
    return f"transform {TRANSFORM_ENGINE} set on {name}"


def upload_program(engine: Engine, name: str, transform_repo: Path) -> tuple[str, list[str]]:
    """The program and its table go into the instance's file sets, which exist after the deploy.

    Returns the step detail and the restart commands the writes call for: the
    instance's config is only rendered with the source's topics once the overlay
    exists, so these writes are where a Compose deployment learns it must roll
    the app, not the deploy before them.
    """
    service = f"dfe-transform-{TRANSFORM_ENGINE}"
    program = (transform_repo / PROGRAM).read_text(encoding="utf-8")
    put = engine.call("PUT", f"/apps/{service}/{name}/files/transforms/{Path(PROGRAM).name}", {"content": program})
    if put.status not in (200, 201):
        raise RuntimeError(f"the program upload was refused: {put.status} {put.body}")
    table = (transform_repo / ENRICHMENT).read_text(encoding="utf-8")
    put_table = engine.call("PUT", f"/apps/{service}/{name}/files/enrichment/{Path(ENRICHMENT).name}", {"content": table})
    enrichment = f"enrichment table {'accepted' if put_table.status in (200, 201) else f'REFUSED {put_table.status}: {put_table.body}'}"
    hints: list[str] = []
    for reply in (put, put_table):
        hints += [str(h) for h in ((reply.body or {}).get("restart_required") or [])]
    return f"program {len(program)} bytes accepted, {enrichment}", hints


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


def wait_instance(engine: Engine, name: str, deadline: float, service: str = "") -> str:
    """The instance the source owns appears in the apps list."""
    service = service or f"dfe-transform-{TRANSFORM_ENGINE}"
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


def wait_reporting(engine: Engine, name: str, deadline: float, service: str = "") -> str:
    """The instance is up and reporting telemetry through the engine."""
    service = service or f"dfe-transform-{TRANSFORM_ENGINE}"
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


def wait_routed(receiver_url: str, verify: bool, engine_repo: Path, transform_repo: Path, store: Datastore, name: str, deadline: float) -> str:
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
        feed(receiver_url, verify, engine_repo, transform_repo, name, probe, 1)
        passes += 1
        if store.scalar(f"SELECT count() FROM {name}") > before:
            return f"routed into dfe.{name} after {passes} probe pass(es)"
        if time.monotonic() >= until:
            return f"NOT routed into dfe.{name} within {deadline:.0f}s ({passes} probe passes)"
        time.sleep(20)


def feed(receiver_url: str, verify: bool, engine_repo: Path, transform_repo: Path, name: str, run: str, per_module: int) -> int:
    """POST the wrapped corpus, one request per record."""
    sys.path.insert(0, str(engine_repo))
    from tests.e2e import filebeat_corpus as corpus  # type: ignore[import-not-found]

    items = corpus.samples(transform_repo / CORPUS, limit=per_module)
    bodies = corpus.wrap_all(items, source=name, run=run)
    import json
    import urllib.error
    import urllib.request

    from acceptance.source.engine import _context

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
                with urllib.request.urlopen(request, timeout=30, context=_context(verify)) as response:
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


def wait_gain(store: Datastore, table: str, baseline: int, wanted: int, deadline: float, where: str = "") -> int:
    """Rows gained over *baseline*, polled until *wanted* arrive or the deadline passes.

    A transformed source table carries the program's output, not the record as
    posted, so a run marker cannot be searched for there; the gain is the proof.
    """
    until = time.monotonic() + deadline
    gained = 0
    while True:
        gained = store.scalar(f"SELECT count() FROM {table}{where}") - baseline
        if gained >= wanted:
            return gained
        if time.monotonic() >= until:
            return gained
        time.sleep(5)


def in_archiver(prefix: list[str], script: str) -> tuple[int, str]:
    """Run *script* under a shell inside the archiver, wherever it runs.

    The prefix is the whole of what makes this k8s or Compose: `kubectl exec
    deploy/dfe-archiver --` or `docker exec dfe-archiver`. Nothing else in this
    step knows which one it is.
    """
    import subprocess

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


def companion(repo: Path, name: str) -> Path:
    """The checkout beside *repo*'s main clone, so a worktree resolves the same as a clone."""
    import subprocess

    common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    home = (repo / common).resolve().parent if common else repo
    return home.parent / name


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


def open_console(driver, engine: Engine, admin_user: str, password: str, org: str, first_user: str) -> None:
    """Sign in, completing the first-login wizard when the deployment still owes it.

    The wizard sits behind the login and holds the console there until it is
    finished, so a deployment nobody has onboarded has no Sources page for this
    suite to test against. The walk is the onboarding suite's own, not a second
    copy of it, and it records the login and each screen as it goes.
    """
    status = onboarding.setup_status(engine.base, engine.verify)
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
    strays = [n for n in names if n.startswith(("fb", "cw", "onboard")) and n != "filebeat"]
    removed = [
        onboarding.remove_source(engine.base, verify, engine.token, n, 60.0) for n in strays
    ]
    return "; ".join(removed) if removed else "no strays"


def filebeat_case(
    driver, engine: Engine, store: Datastore, args: argparse.Namespace,
    name: str, run_id: str, engine_repo: Path, transform_repo: Path,
    receiver_url: str, verify: bool, archive_exec: list[list[str]],
    restart_exec: list[str],
) -> None:
    """Real filebeat lines pushed at the receiver, through the bundled VRL, archived."""
    create_source_in_console(driver, name)
    driver.record(
        "attach-transform", "api-fallback",
        attach_transform(engine, name, transform_repo) + " (the console has no transform control)",
    )
    deployed = deploy(engine, name)
    driver.record(
        "deploy", "done",
        f"applied; topics ensured {sorted(deployed.get('topics_ensured') or [])}; apps synced {deployed.get('apps_synced')}",
    )
    # Read it back off the fork rather than trusting the deploy's own
    # report: the deploy counts what it wrote, the listing says what is
    # there, and only the second is what a human opens HyperDX to.
    state, detail = assert_hyperdx_source(engine, name)
    if state == "failed" and deployed.get("hyperdx_source_error"):
        detail = f"{detail}; the deploy said: {deployed['hyperdx_source_error']}"
    driver.record("hyperdx-source", state, detail)
    detail, hints = upload_program(engine, name, transform_repo)
    driver.record("upload-program", "api-fallback", detail)
    # After the program, not before: a restart that beats the file to disk leaves
    # the app idling on a transform it has no program for.
    apply_restarts(driver, restart_exec, [*(deployed.get("restart_required") or []), *hints])
    exists = store.table_exists(name) if store.host else None
    driver.record(
        "table", "done" if exists else ("skipped" if exists is None else "failed"),
        f"dfe.{name} {'exists' if exists else 'is missing'}" if exists is not None else "no datastore access in this run",
    )
    detail = wait_instance(engine, name, 120.0)
    driver.record("transform-instance", "done" if "knows" in detail else "failed", detail)
    detail = wait_reporting(engine, name, ROUTING_DEADLINE)
    driver.record("transform-reporting", "done" if "reporting after" in detail else "failed", detail)
    if not receiver_url:
        driver.record("feed", "skipped", "no receiver in this run (DFE_E2E_RECEIVER_URL unset)")
        return
    detail = wait_routed(receiver_url, verify, engine_repo, transform_repo, store, name, ROUTING_DEADLINE)
    driver.record("routed", "done" if detail.startswith("routed") else "failed", detail)
    transformed_where = f" WHERE {TRANSFORMED_COLUMN} IS NOT NULL"
    before = store.scalar(f"SELECT count() FROM {name}")
    before_transformed = store.scalar(f"SELECT count() FROM {name}{transformed_where}")
    posted = feed(receiver_url, verify, engine_repo, transform_repo, name, run_id, args.per_module)
    driver.record("feed", "done", f"posted {posted} corpus records tagged e2e_run:{run_id}")
    # The program rewrites each record into ECS and the tag with it, so the
    # proof is the gain in the source's own table; the catch-all still holds
    # the record as posted, so a stray there is found by the tag.
    landed = wait_gain(store, name, before, posted, LAND_DEADLINE)
    strayed = store.scalar(f"SELECT count() FROM main WHERE _raw LIKE '%{run_id}%'")
    driver.record(
        "landed", "done" if landed and not strayed else "failed",
        f"dfe.{name} gained {landed} of {posted} posted rows, {strayed} strayed into dfe.main",
    )
    transformed = wait_gain(store, name, before_transformed, 1, TRANSFORM_DEADLINE, transformed_where)
    driver.record(
        "transformed", "done" if transformed else "failed",
        f"{transformed} new rows carry {TRANSFORMED_COLUMN}, which only the transform sets",
    )
    record_archive(driver, archive_exec, name)


def apply_restarts(driver, prefix: list[str], reported: list[str]) -> None:
    """Restart the apps the engine says cannot take a write where they stand.

    On Kubernetes a controller rolls the pod, so the engine reports nothing here.
    A Compose deployment has nobody to do it: the app takes the new file but goes
    on consuming the topics it started with, so the source it was just given
    never moves a record. The engine hands back one command per app and this is
    what runs them.
    """
    hints = sorted({str(hint) for hint in reported if hint})
    if not hints:
        driver.record("restart", "skipped", "the deploy reports every write taken where it stands")
        return
    if not prefix:
        driver.record(
            "restart", "failed",
            f"{len(hints)} app(s) need restarting and this run was given no --restart-exec: {hints}",
        )
        return
    import subprocess

    done = []
    for hint in hints:
        # The engine's hint ends in the service name: "restart required: docker
        # compose restart dfe-transform-vrl".
        service = hint.rsplit(" ", 1)[-1]
        reply = subprocess.run([*prefix, service], capture_output=True, text=True, check=False)
        done.append(f"{service} {'restarted' if reply.returncode == 0 else f'REFUSED: {(reply.stderr or reply.stdout).strip()[:120]}'}")
    driver.record(
        "restart",
        "done" if all("restarted" in entry for entry in done) else "failed",
        "; ".join(done),
    )


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


def cloudwatch_case(
    driver, engine: Engine, store: Datastore, args: argparse.Namespace,
    name: str, schema: str, archive_exec: list[list[str]], restart_exec: list[str],
) -> None:
    """A meta schema authored by hand, and an AWS upstream a fetcher polls on its own.

    Nothing is pushed and nothing is written to AWS: the source's stanza is the
    whole configuration, and the proof is rows appearing inside a poll interval.
    """
    case = fetcher.AWS_CASES[args.aws_service]
    csv_file = Path(args.shots_dir) / f"{Path(schema).name}.csv"
    csv_file.parent.mkdir(parents=True, exist_ok=True)
    csv_file.write_text(fetcher.schema_csv(case), encoding="utf-8")
    try:
        driver.record(
            "author-schema", "done",
            fetcher.author_schema_in_console(driver, case, schema, csv_file, STEP_TIMEOUT_MS),
        )
    except Exception as exc:  # the console refusing to author IS the finding
        driver.record(
            "author-schema", "api-fallback",
            fetcher.author_schema_by_api(engine, case, schema)
            + f" (the console could not: {type(exc).__name__}: {str(exc).splitlines()[0]})",
        )
    driver.record("add-source-survey", "api-fallback", fetcher.survey_origin(driver, STEP_TIMEOUT_MS))
    body = fetcher.source_body(
        case, name, schema, args.aws_region, args.aws_log_group, args.poll_interval_secs
    )
    driver.record("add-source", "api-fallback", fetcher.create_source(engine, body))
    deployed = deploy(engine, name)
    driver.record(
        "deploy", "done",
        f"applied; topics ensured {sorted(deployed.get('topics_ensured') or [])}; apps synced {deployed.get('apps_synced')}",
    )
    state, detail = assert_hyperdx_source(engine, name)
    if state == "failed" and deployed.get("hyperdx_source_error"):
        detail = f"{detail}; the deploy said: {deployed['hyperdx_source_error']}"
    driver.record("hyperdx-source", state, detail)
    apply_restarts(driver, restart_exec, deployed.get("restart_required") or [])
    exists = store.table_exists(name) if store.host else None
    driver.record(
        "table", "done" if exists else ("skipped" if exists is None else "failed"),
        f"dfe.{name} {'exists' if exists else 'is missing'}" if exists is not None else "no datastore access in this run",
    )
    detail = wait_instance(engine, name, fetcher.INSTANCE_DEADLINE, fetcher.FETCHER_SERVICE)
    driver.record("fetcher-instance", "done" if "knows" in detail else "failed", detail)
    detail = wait_reporting(engine, name, fetcher.REPORTING_DEADLINE, fetcher.FETCHER_SERVICE)
    driver.record("fetcher-reporting", "done" if "reporting after" in detail else "failed", detail)
    service_name = fetcher.telemetry_name(engine, name)
    samples, since = fetcher.idle_history(store, service_name, int(fetcher.REPORTING_DEADLINE))
    driver.record(
        "fetcher-idle", "done",
        f"{service_name} published {samples} pipeline_idle sample(s)"
        + (f", last {since}s ago" if since is not None else "; it was created carrying its stanza, so it had work from the start"),
    )
    if not store.host:
        driver.record("fetched", "skipped", "no datastore access in this run")
        return
    # A poll interval to fetch, and the landing topic plus the loader's own batch
    # window before the row is queryable.
    deadline = args.poll_interval_secs * 2 + 300
    gained = fetcher.wait_rows(store, name, 0, deadline)
    driver.record(
        "fetched", "done" if gained else "failed",
        f"dfe.{name} holds {gained} row(s) from {fetcher.upstream_note(case, args.aws_log_group)} "
        f"within {deadline:.0f}s, nothing written to AWS",
    )
    if gained:
        set_rows = store.scalar(f"SELECT count() FROM {name} WHERE {case.proof_column} != ''")
        driver.record(
            "fetched-columns", "done" if set_rows else "failed",
            f"{set_rows} of {gained} row(s) carry {case.proof_column}, which only this upstream sets",
        )
    record_archive(driver, archive_exec, name)


def run(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    admin_user = os.environ.get("DFE_E2E_ENGINE_USER", "admin")
    password = os.environ.get("DFE_E2E_ADMIN_PASSWORD", "")
    if args.access_summary:
        rows = access_summary_file.parse(Path(args.access_summary).read_text(encoding="utf-8"))
        admin_user, password = rows[access_summary_file.ADMIN_LABEL]
    if not password:
        print("source: no login; pass --access-summary or run through dfe-ops acceptance", file=sys.stderr)
        return 2
    receiver_url = os.environ.get("DFE_E2E_RECEIVER_URL", "")
    store = Datastore(
        host=os.environ.get("DFE_E2E_CH_HOST", ""),
        port=int(os.environ.get("DFE_E2E_CH_PORT", "8123")),
        user=os.environ.get("DFE_E2E_CH_USER", "default"),
        password=os.environ.get("DFE_E2E_CH_PASSWORD", ""),
        database=os.environ.get("DFE_E2E_CH_DB", "dfe"),
    )
    engine_repo = Path(args.engine_repo).resolve()
    transform_repo = Path(args.transform_repo).resolve() if args.transform_repo else companion(engine_repo, "dfe-transform-vrl")
    verify = not args.insecure
    # The API calls go straight to the engine when a forward is up: a deploy or a
    # delete can outlast a gateway's timeout. The console still goes through the gateway.
    api_url = os.environ.get("DFE_E2E_ENGINE_URL") or args.engine_url
    engine = Engine(api_url, admin_user, password, verify or api_url != args.engine_url)
    archive_exec = [shlex.split(one) for one in args.archive_exec]
    restart_exec = shlex.split(args.restart_exec)

    fetched = args.case == "cloudwatch"
    name = fetcher.new_name("cw" if fetched else "fb")
    schema = fetcher.schema_path(fetcher.AWS_CASES[args.aws_service], name) if fetched else ""
    run_id = f"src-{uuid.uuid4().hex[:12]}"
    shots = Path(args.shots_dir)
    created = False
    with sync_playwright() as play:
        launch_args = [f"--host-resolver-rules=MAP {host} {ip}" for host, ip in args.resolve]
        browser = play.chromium.launch(channel=args.channel, headless=not args.headed, args=launch_args)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, ignore_https_errors=args.insecure)
        driver = onboarding.Driver(context.new_page(), args.ui_url, shots)
        try:
            try:
                open_console(driver, engine, admin_user, password, args.org, args.first_user)
            except Exception as exc:
                # Chrome reconfigures its certificate verifier right after
                # launch and fails the first navigation with ERR_CERT_VERIFIER_CHANGED.
                if "ERR_CERT_VERIFIER_CHANGED" not in str(exc):
                    raise
                driver.page.wait_for_timeout(3000)
                open_console(driver, engine, admin_user, password, args.org, args.first_user)
            driver.record("sweep", "done", sweep_strays(engine, verify))
            if fetched:
                cloudwatch_case(driver, engine, store, args, name, schema, archive_exec, restart_exec)
            else:
                filebeat_case(
                    driver, engine, store, args, name, run_id, engine_repo,
                    transform_repo, receiver_url, verify, archive_exec, restart_exec,
                )
        except Exception as exc:  # a Playwright timeout IS the finding
            driver.record("run", "failed", f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
        finally:
            context.close()
            browser.close()

    try:
        created = engine.call("GET", f"/sources/{name}").status == 200
    except OSError as exc:
        # The step table is what this run produces; an engine that has gone away
        # is a finding to print, not a traceback that loses every row above it.
        driver.results.append(wizard.StepResult("teardown", "failed", f"the engine did not answer: {exc}"))
        created = False
    if created and not args.keep:
        detail = onboarding.remove_source(api_url, engine.verify, engine.token or "", name, onboarding.TEARDOWN_DEADLINE)
        driver.results.append(wizard.StepResult("teardown", "done" if detail.startswith("removed") else "failed", detail))
        # A delete that leaves the source behind gives every team a view over a
        # table that no longer exists.
        state, detail = assert_hyperdx_source_gone(engine, name)
        driver.results.append(wizard.StepResult("hyperdx-source-removed", state, detail))
    elif created:
        driver.results.append(wizard.StepResult("teardown", "kept", f"{name} left deployed (--keep)"))
    # The schema the run authored is the run's to clean up; a kept source still
    # reads from it.
    if schema and not args.keep:
        detail = fetcher.remove_schema(engine, schema)
        driver.results.append(
            wizard.StepResult("schema-removed", "done" if detail.startswith("removed") else "failed", detail)
        )

    print()
    print(wizard.report_table(driver.results))
    print(f"\nscreenshots: {shots}")
    return wizard.exit_code(driver.results)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="source",
        description="Create a source through the console and prove it end to end.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--ui-url", required=True, help="console base URL")
    parser.add_argument("--engine-url", required=True, help="engine API base URL")
    parser.add_argument("--engine-repo", required=True, help="dfe-engine checkout (the corpus wrapper lives in its e2e tests)")
    parser.add_argument("--transform-repo", default="", help="dfe-transform-vrl checkout holding the bundled pipeline and corpus (default: beside the engine repo)")
    parser.add_argument("--access-summary", default="", metavar="FILE", help="the deploy's own summary, for the login when not run through dfe-ops")
    parser.add_argument("--org", default="acceptance", help="organisation to create when the deployment still owes its setup wizard")
    parser.add_argument("--first-user", default="operator", help="first user to create when the deployment still owes its setup wizard")
    parser.add_argument("--archive-exec", action="append", default=[], metavar="PREFIX",
                        help="command prefix that runs a shell inside one archiver replica, for the "
                             "archive assertion; repeatable, one per replica (k8s: kubectl -n <ns> "
                             "exec <pod> --; docker: docker exec dfe-archiver)")
    parser.add_argument("--restart-exec", default="", metavar="PREFIX",
                        help="command prefix that restarts one app by service name, for the apps a "
                             "deploy reports it cannot apply where they stand (docker: docker restart); "
                             "unused on Kubernetes, where the controller rolls the pod")
    parser.add_argument("--case", default="filebeat", choices=("filebeat", "cloudwatch"),
                        help="filebeat pushes real lines at the receiver; cloudwatch authors a meta "
                             "schema and lets a fetcher pull an AWS upstream")
    parser.add_argument("--aws-service", default="cloudwatch_logs", choices=tuple(sorted(fetcher.AWS_CASES)),
                        help="the AWS service the cloudwatch case's fetcher polls")
    parser.add_argument("--aws-region", default=os.environ.get("DFE_E2E_AWS_REGION", ""),
                        help="region the fetcher signs for (default: DFE_E2E_AWS_REGION)")
    parser.add_argument("--aws-log-group", default=os.environ.get("DFE_E2E_AWS_LOG_GROUP", ""),
                        help="CloudWatch log group to poll (default: DFE_E2E_AWS_LOG_GROUP)")
    parser.add_argument("--poll-interval-secs", type=int, default=60,
                        help="how often the fetcher polls its upstream")
    parser.add_argument("--per-module", type=int, default=20, help="corpus lines per filebeat module to feed")
    parser.add_argument("--shots-dir", default=".tmp/source", help="where the per-step screenshots go")
    parser.add_argument("--channel", default="chrome", help="browser channel; chrome is the testing browser")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--keep", action="store_true", help="leave the source deployed")
    parser.add_argument("--insecure", action="store_true", help="accept a certificate this machine does not trust")
    parser.add_argument("--resolve", action="append", default=[], metavar="HOST:IP", type=onboarding._host_ip,
                        help="resolve HOST to IP inside the browser (repeatable)")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
