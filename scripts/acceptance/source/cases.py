#  Project:      dfe-infra
#  File:         acceptance/source/cases.py
#  Purpose:      The source kinds the suite stands up, each as one object: how
#                it is created, how records reach it, and what proves they
#                arrived. Everything between those halves is in steps.py.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""source.cases -- what differs between a pushed source and a fetched one.

A case owns its own constants, its create/provision/feed/prove hooks and the
step names its waits report under. The runner calls those hooks in one fixed
order and supplies every step they have in common, so no case carries a second
copy of a poll.

Console first, API second. Where the console has the control the case drives it
and the row reads ``done``; where it does not, the API does the same work and
the row reads ``api-fallback`` with the reason -- which is a finding about the
console, and is what lets an older console still pass a run.
"""

import argparse
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from acceptance.clients import Datastore, Engine, remove_source
from acceptance.onboarding import run as onboarding
from acceptance.onboarding import wizard
from acceptance.source import beats, fetcher, steps, syslog
from acceptance.source.listener import Listener
from acceptance.source.steps import STEP_TIMEOUT_MS

# The bundled filebeat program is a few hundred kilobytes, so the editor write
# and its commit get a longer bound than a form control.
EDITOR_TIMEOUT_MS = STEP_TIMEOUT_MS * 4
# The --via values that ship the corpus through a real filebeat rather than a POST.
SHIPPED = ("logstash", "beats")
# How long a committed file is waited for on the engine, since the console's own
# notice is per panel and a panel the run has left keeps its copy.
COMMIT_DEADLINE = 180.0
# The engine route the console's Add Source posts to, whatever origin serves it.
SOURCES_ROUTE = "/api/v1/sources"


def source_created(response) -> bool:
    """Whether a reply the browser saw is the engine answering the console's Add Source."""
    path = urlsplit(response.url).path.rstrip("/")
    return response.request.method == "POST" and path.endswith(SOURCES_ROUTE)


@dataclass
class Run:
    """Everything a case's hooks are given: the browser, the clients, this run's names."""

    driver: object
    engine: Engine
    store: Datastore
    args: argparse.Namespace
    name: str
    run_id: str
    engine_repo: Path
    transform_repo: Path
    receiver_url: str
    verify: bool
    #: Where the program a case uploads is read from. The same checkout as the
    #: corpus for every case but vector, whose app ships its own pipeline.
    program_repo: Path | None = None

    def programs(self) -> Path:
        return self.program_repo or self.transform_repo


class Case:
    """One source kind, create to proof.

    The runner owns the order; a case owns what is true only of its own kind.
    """

    #: The per-source app this kind's source deploys.
    service: str = ""
    #: The checkout beside the engine repo this kind reads its inputs from, for
    #: a run that was given no --transform-repo. Empty when it reads none.
    companion_repo: str = ""
    #: The checkout the program comes from, when it is not the one above. Only
    #: the vector case splits them: its app ships the pipeline, dfe-transform-vrl
    #: ships the corpus both of them grade against.
    program_repo: str = ""
    #: What the two shared waits report as, so the report keeps this kind's words.
    instance_step: str = ""
    reporting_step: str = ""
    instance_deadline: float = 120.0
    reporting_deadline: float = steps.ROUTING_DEADLINE
    #: Whether the run ends on the console's Observe search and the archive assertion.
    #: A case whose source is neither archived nor shown in HyperDX turns them off.
    observes: bool = True
    archives: bool = True
    #: Whether the run signs in through the console. A case that only drives the engine API
    #: and the receiver's listeners needs the engine's login alone.
    console: bool = True

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.name = ""

    def create(self, run: Run) -> list[str]:
        """Bring the source into being, through the console where it can.

        Returns the restart hints the create's writes reported: on a direct tier
        the create alone rewrites the receiver's destinations, so its reply is the
        only one that names the receiver.
        """
        raise NotImplementedError

    def provision(self, run: Run) -> list[str]:
        """Files the deployed instance needs, written once the deploy has made it.

        Returns the restart hints the writes reported, for the shared restart step.
        """
        return []

    def feed(self, run: Run) -> None:
        """Get records moving, and record what that took."""
        raise NotImplementedError

    def prove(self, run: Run) -> None:
        """Assert the records landed as this kind of source implies."""
        raise NotImplementedError

    def cleanup(self, run: Run) -> list[wizard.StepResult]:
        """Rows for anything besides the source itself that this run authored."""
        return []


# --- filebeat: real lines pushed at the receiver ------------------------------


class FilebeatCase(Case):
    """Real cisco_ios lines in the Elastic Agent envelope, through the bundled VRL, archived.

    The shipped meta schema, a receiver match on the shipper's own
    ``data_stream.dataset``, the bundled filebeat program with its timezone
    table, the archive on.
    """

    META_SCHEMA = "meta/beats/filebeat"
    META_SCHEMA_VERSION = "1.1.0"
    HEADER = "common-header/timeseries"
    HEADER_VERSION = "1.0.1"
    # Elastic's own identifier for the source, which the Agent sends on every record.
    MATCH_FIELD = "data_stream.dataset"
    MATCH_VALUE = "cisco_ios.log"
    TRANSFORM_ENGINE = "vrl"
    PROGRAM = "pipelines/filebeat/filebeat.vrl"
    ENRICHMENT = "pipelines/filebeat/timezones.csv"
    CORPUS = "tests/fixtures/filebeat/filebeat-testdata.tar.gz"
    # The one module MATCH_VALUE names: one equals match routes one dataset, so
    # cisco_meraki and cisco_umbrella lines would miss the source by design.
    MODULES: tuple[str, ...] = ("cisco_ios",)
    # Display name and description the console form is filled with.
    DISPLAY = "Filebeat source test"
    DESCRIPTION = "Post-deploy source test: real cisco_ios lines through the bundled VRL, archived."
    # ECS source.ip, which meta/beats/filebeat declares and the cisco_ios branch
    # reads out of the syslog body; the posted envelope carries no such field.
    TRANSFORMED_COLUMN = "source_ip"
    # The field that column is filled from, as the corpus's expected events name it.
    TRANSFORMED_FIELD = "source.ip"
    # The file sets the transform app declares (dfe-infra apps.yaml).
    PROGRAM_SET = "transforms"
    ENRICHMENT_SET = "enrichment"
    LAND_DEADLINE = 300.0
    TRANSFORM_DEADLINE = 300.0

    service = f"dfe-transform-{TRANSFORM_ENGINE}"
    companion_repo = "dfe-transform-vrl"
    instance_step = "transform-instance"
    reporting_step = "transform-reporting"
    #: Prefix of the source name this kind mints, which the sweep knows.
    prefix = "fb"

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(args)
        self.name = fetcher.new_name(self.prefix)
        self._posted = 0
        #: The agent's containers, on a run that ships through a real filebeat only.
        self._pair: beats.Pair | None = None
        #: The receiver's lumberjack listener, which a --via beats run opens and cleanup closes.
        self._listener = Listener(
            "receiver-lumberjack", beats.LUMBERJACK_SWITCHES, beats.LUMBERJACK_ENV
        )
        #: Whether the source asks to be archived, which only a deployment running
        #: an archiver can honour.
        self._archive = True

    # -- create ---------------------------------------------------------------

    def create(self, run: Run) -> list[str]:
        driver, page = run.driver, run.driver.page
        self._archive = steps.archiver_runs(run.engine)
        # The page header repeats the nav entry as a link, so the nav one is first.
        page.get_by_role("link", name="Sources", exact=True).first.click(
            timeout=STEP_TIMEOUT_MS
        )
        page.wait_for_url("**/sources**", timeout=STEP_TIMEOUT_MS)
        driver.button("Add Source").first.click(timeout=STEP_TIMEOUT_MS)
        page.get_by_placeholder("Enter source").fill(self.name)
        page.get_by_placeholder("Enter display name").fill(
            f"{self.DISPLAY} {self.name}"
        )
        page.get_by_placeholder("Enter description").fill(self.DESCRIPTION)
        if self._archive:
            # The switches are Enabled (on) then Archive (off); the archive one is second.
            page.get_by_role("switch").nth(1).click(timeout=STEP_TIMEOUT_MS)
        page.get_by_placeholder("Enter field").fill(self.MATCH_FIELD)
        page.get_by_placeholder("Enter value").fill(self.MATCH_VALUE)
        archived = (
            "archive on"
            if self._archive
            else f"archive off, because this deployment runs no {steps.ARCHIVER_SERVICE}"
        )
        driver.record(
            "add-source-configuration",
            "done",
            f"filled the Configuration tab for {self.name}, {archived}",
        )

        # Own table (the form's default) shows this tab, which opens on the meta schema select.
        page.get_by_role("tab", name="Table Settings", exact=True).click(
            timeout=STEP_TIMEOUT_MS
        )
        # The selects group by family and show the leaf: meta > beats > filebeat.
        steps.select_field(
            page, "schema_meta_schema", self.META_SCHEMA.rsplit("/", 1)[-1]
        )
        steps.select_field(page, "schema_meta_schema_version", self.META_SCHEMA_VERSION)
        # The header is a deployment default the form fills in; picking one is an override.
        driver.button("Override Defaults").click(timeout=STEP_TIMEOUT_MS)
        steps.select_field(page, "header_type", self.HEADER.rsplit("/", 1)[-1])
        steps.select_field(page, "header_version", self.HEADER_VERSION)
        driver.record(
            "add-source-meta-schema",
            "done",
            f"picked {self.HEADER} {self.HEADER_VERSION} and "
            f"{self.META_SCHEMA} {self.META_SCHEMA_VERSION}",
        )

        # The Transform tab renders only once a meta schema is picked, so it is
        # filled in here and the create carries the transform with it.
        refused = ""
        try:
            self._pick_transform(page)
        except Exception as exc:  # the console refusing the control IS the finding
            refused = steps.refusal(exc)

        # The console shows no restart hint, so the reply it was handed is read here.
        with page.expect_response(source_created, timeout=STEP_TIMEOUT_MS) as created:
            driver.button("Add Source").last.click(timeout=STEP_TIMEOUT_MS)
        page.get_by_text("Source created successfully").wait_for(
            timeout=STEP_TIMEOUT_MS
        )
        driver.record("add-source", "done", f"the console created {self.name}")
        return steps.reply_hints(created.value.json()) + self._record_transform(
            run, refused
        )

    def _pick_transform(self, page) -> None:
        """Transform tab: Define Transform, then the engine the catalogue offers.

        The picker lists the catalogued transform apps by service, so the option
        is the app's name and the value it sets is the engine.
        """
        page.get_by_role("tab", name="Transform", exact=True).click(
            timeout=STEP_TIMEOUT_MS
        )
        page.get_by_text("Define Transform", exact=True).click(timeout=STEP_TIMEOUT_MS)
        steps.select_field(page, "transform_engine", self.service)

    def _record_transform(self, run: Run, refused: str) -> list[str]:
        """The attach-transform row, read back off the engine rather than off the form.

        Returns the restart hints an API attach reported, empty when the console's
        create carried the transform.
        """
        console_detail, api_detail = "", ""
        hints: list[str] = []
        if not refused:
            attached = self._attached_transform(run)
            if attached == self._transform_body():
                console_detail = (
                    f"the console's Transform tab set {self.service}; {self.name} reads "
                    f"back with transform {attached}"
                )
            else:
                refused = f"the create carried transform {attached or 'none'}"
        if refused:
            api_detail, hints = self._attach_by_api(run)
        status, detail = steps.console_outcome(console_detail, refused, api_detail)
        run.driver.record("attach-transform", status, detail)
        return hints

    def _transform_body(self) -> dict[str, str]:
        """The transform block this source is meant to carry.

        A kind whose app selects one of several compiled-in programs adds the
        key naming it; the read-back is compared against exactly this.
        """
        return {"engine": self.TRANSFORM_ENGINE}

    def _attached_transform(self, run: Run) -> dict[str, str]:
        """The keys of that block the source actually reads back with.

        The engine answers every key of its transform model, so only the ones
        asked for are read and an unset one is left out rather than compared as
        an empty string.
        """
        current = run.engine.call("GET", f"/sources/{self.name}")
        attached = (current.body or {}).get("transform") or {}
        return {
            key: str(attached[key])
            for key in self._transform_body()
            if attached.get(key)
        }

    def _attach_by_api(self, run: Run) -> tuple[str, list[str]]:
        """The same transform through the engine, and the restarts the write reported."""
        engine = run.engine
        current = engine.call("GET", f"/sources/{self.name}")
        if current.status != 200:
            raise RuntimeError(
                f"cannot read {self.name} back: {current.status} {current.body}"
            )
        transform = self._transform_body()
        body = {
            "source": self.name,
            "display_name": current.body.get("display_name"),
            "description": current.body.get("description"),
            "match": current.body.get("match"),
            "header": {"type": self.HEADER, "version": self.HEADER_VERSION},
            "schema": {
                "meta_schema": self.META_SCHEMA,
                "meta_schema_version": self.META_SCHEMA_VERSION,
            },
            "transform": transform,
            "archive": self._archive,
        }
        updated = engine.call("PUT", f"/sources/{self.name}", body)
        if updated.status != 200:
            raise RuntimeError(
                f"attaching the transform was refused: {updated.status} {updated.body}"
            )
        return f"transform {transform} set on {self.name}", steps.reply_hints(
            updated.body
        )

    # -- provision ------------------------------------------------------------

    def provision(self, run: Run) -> list[str]:
        """The program and its table, into the instance's file sets.

        Those sets only exist once the deploy has created the instance, which is
        why this is not part of create.
        """
        programs = run.programs()
        wanted = [
            (
                self.PROGRAM_SET,
                Path(self.PROGRAM).name,
                (programs / self.PROGRAM).read_text(encoding="utf-8"),
            ),
            (
                self.ENRICHMENT_SET,
                Path(self.ENRICHMENT).name,
                (programs / self.ENRICHMENT).read_text(encoding="utf-8"),
            ),
        ]
        console_detail, api_detail, refused = "", "", ""
        hints: list[str] = []
        try:
            console_detail = self._upload_in_console(run, wanted)
        except Exception as exc:  # the console refusing the control IS the finding
            refused = steps.refusal(exc)
        if refused:
            api_detail, hints = self._upload_by_api(run, wanted)
        status, detail = steps.console_outcome(console_detail, refused, api_detail)
        run.driver.record("upload-program", status, detail)
        return hints

    def _upload_in_console(self, run: Run, wanted: list[tuple[str, str, str]]) -> str:
        """Processing tab -> the instance's file sets -> one commit per file."""
        driver, page = run.driver, run.driver.page
        version = str(
            (run.engine.call("GET", f"/sources/{self.name}").body or {}).get("current")
            or ""
        )
        page.goto(
            f"{driver.ui}/sources?source_name={self.name}&source_version={version}&tab=processing",
            wait_until="domcontentloaded",
            timeout=STEP_TIMEOUT_MS * 2,
        )
        page.get_by_role("heading", name="Transform", exact=True).first.wait_for(
            state="visible", timeout=STEP_TIMEOUT_MS
        )
        # Each catalogued transform is a radio on that card, and only the one with
        # an instance for this source reads Running and opens its file sets below.
        page.get_by_role("radio", name=self._running_option).first.wait_for(
            state="visible", timeout=STEP_TIMEOUT_MS
        )
        landed = []
        for set_name, filename, content in wanted:
            self._commit_file(page, set_name, filename, content)
            size = self._wait_committed(run, set_name, filename, len(content))
            landed.append(f"{filename} {size} bytes")
        return (
            f"the console committed {', '.join(landed)} to {self.service}/{self.name}"
        )

    def _commit_file(self, page, set_name: str, filename: str, content: str) -> None:
        """New file, name it, put the content in the editor, Commit file."""
        tab = page.get_by_role("tab", name=set_name, exact=True)
        if tab.count():
            tab.first.click(timeout=STEP_TIMEOUT_MS)
        page.get_by_role("button", name="New file").first.click(timeout=STEP_TIMEOUT_MS)
        page.get_by_label("New filename", exact=True).fill(
            filename, timeout=STEP_TIMEOUT_MS
        )
        editor = page.locator(f"#{set_name}-editor")
        editor.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
        editor.locator(".ace_content").click(timeout=STEP_TIMEOUT_MS)
        # One insert, not keystrokes: the program is a few hundred kilobytes.
        page.keyboard.insert_text(content)
        # Live autocompletion opens on the insert and would swallow the click.
        page.keyboard.press("Escape")
        page.get_by_role("button", name="Commit file").first.click(
            timeout=EDITOR_TIMEOUT_MS
        )

    def _wait_committed(self, run: Run, set_name: str, filename: str, size: int) -> int:
        """The file as the engine holds it, which is the proof the commit landed."""
        until = time.monotonic() + COMMIT_DEADLINE
        last = ""
        while True:
            reply = run.engine.call(
                "GET", f"/apps/{self.service}/{self.name}/files/{set_name}/{filename}"
            )
            if (
                reply.status == 200
                and int((reply.body or {}).get("size_bytes") or 0) >= size
            ):
                return int(reply.body["size_bytes"])
            last = f"{reply.status} {str(reply.body)[:120]}"
            if time.monotonic() >= until:
                raise RuntimeError(
                    f"{set_name}/{filename} was not committed within {COMMIT_DEADLINE:.0f}s; last {last}"
                )
            time.sleep(5)

    def _upload_by_api(
        self, run: Run, wanted: list[tuple[str, str, str]]
    ) -> tuple[str, list[str]]:
        """The same files through the engine, and the restarts the writes call for.

        The instance's config is only rendered with the source's topics once the
        overlay exists, so these writes are where a Compose deployment learns it
        must restart the app, on top of whatever the deploy before them reported.
        """
        told, hints = [], []
        for set_name, filename, content in wanted:
            reply = run.engine.call(
                "PUT",
                f"/apps/{self.service}/{self.name}/files/{set_name}/{filename}",
                {"content": content},
            )
            if reply.status not in (200, 201):
                raise RuntimeError(
                    f"the {set_name} upload was refused: {reply.status} {reply.body}"
                )
            told.append(f"{filename} {len(content)} bytes accepted")
            hints += steps.reply_hints(reply.body)
        return ", ".join(told), hints

    # -- feed and prove -------------------------------------------------------

    def feed(self, run: Run) -> None:
        if not run.receiver_url:
            run.driver.record(
                "feed",
                "skipped",
                "no receiver in this run (DFE_E2E_RECEIVER_URL unset)",
            )
            return
        corpus = run.transform_repo / self.CORPUS
        detail = steps.wait_routed(
            run.receiver_url,
            run.verify,
            run.engine_repo,
            corpus,
            run.store,
            self.name,
            run.run_id,
            steps.ROUTING_DEADLINE,
            self.MODULES,
        )
        run.driver.record(
            "routed", "done" if detail.startswith("routed") else "failed", detail
        )
        if run.args.via in SHIPPED:
            self._feed_through_agent(run, corpus)
            return
        self._posted = steps.post_corpus(
            run.receiver_url,
            run.verify,
            run.engine_repo,
            corpus,
            run.run_id,
            run.args.per_module,
            self.MODULES,
        )
        run.driver.record(
            "feed",
            "done",
            f"posted {self._posted} corpus records marked {steps.MARKER}.run {run.run_id}",
        )

    def _feed_through_agent(self, run: Run, corpus: Path) -> None:
        """The same corpus lines, shipped by a real filebeat.

        The POSTed envelope is reconstructed. ``--via logstash`` pushes the one
        Logstash's http output builds, and ``--via beats`` the one filebeat sends
        the receiver's own lumberjack listener, with the operator's filebeat
        config adding the data stream the source matches either way.
        """
        driver, args = run.driver, run.args
        direct = args.via == "beats"
        if not args.beats_network:
            driver.record(
                "feed",
                "skipped",
                f"--via {args.via} needs --beats-network, the docker network the stack runs on",
            )
            return
        if direct and not self._open_lumberjack(run):
            driver.record(
                "feed", "failed", "the receiver has no lumberjack listener to ship to"
            )
            return
        self._pair = beats.Pair(
            network=args.beats_network,
            receiver_url=args.beats_receiver_url,
            workdir=Path(args.shots_dir) / f"beats-{run.run_id}",
            run_id=run.run_id,
            lumberjack=args.beats_lumberjack if direct else "",
        )
        lines = steps.corpus_lines(
            run.engine_repo, corpus, args.per_module, self.MODULES
        )
        beats.write_inputs(
            self._pair,
            lines,
            self.MATCH_VALUE,
            steps.MARKER,
            steps.run_marker(run.run_id, steps.CORPUS),
        )
        if not direct:
            ready, detail = beats.start_logstash(self._pair)
            driver.record("logstash", "done" if ready else "failed", detail)
            if not ready:
                driver.record("feed", "failed", "no logstash to ship through")
                return
        shipping, detail = beats.start_filebeat(self._pair)
        driver.record("filebeat", "done" if shipping else "failed", detail)
        if not shipping:
            driver.record("feed", "failed", "no filebeat to ship with")
            return
        sent, detail = beats.published(self._pair, len(lines))
        self._posted = sent
        route = (
            f"lumberjack straight to the receiver at {self._pair.target}"
            if direct
            else f"lumberjack to logstash and its http output to {self._pair.receiver_url}"
        )
        driver.record(
            "feed", "done" if sent >= len(lines) else "failed", f"{detail}, {route}"
        )

    def _open_lumberjack(self, run: Run) -> bool:
        """Switch the receiver's lumberjack listener on through the engine, as an operator would."""
        hints = self._listener.open(run)
        if hints is None:
            return False
        prefix = shlex.split(run.args.restart_exec)
        status, detail = steps.restart_outcome(prefix, hints)
        run.driver.record("receiver-restart", status, detail)
        return status != "failed"

    def prove(self, run: Run) -> None:
        if not run.receiver_url:
            run.driver.record(
                "landed",
                "unproven",
                "no receiver in this run, so nothing was posted to land",
            )
            return
        # The routing probes land in this table too, so only rows marked as this run's corpus count.
        corpus = steps.marked(run.run_id, steps.CORPUS)
        landed = steps.wait_rows(
            run.store, self.name, self._posted, self.LAND_DEADLINE, corpus
        )
        strayed = steps.strayed_rows(run.store, run.run_id)
        whole = self._posted > 0 and landed >= self._posted
        run.driver.record(
            "landed",
            "done" if whole and not strayed else "failed",
            f"dfe.{self.name} holds {landed} of {self._posted} corpus rows marked "
            f"{steps.MARKER}.run {run.run_id}, {strayed} strayed into dfe.main",
        )
        run.driver.record(
            "dataset", *steps.dataset_outcome(run.store, self.name, self.MATCH_VALUE)
        )
        # Not every corpus line carries an address, so the bar is the corpus's own expected count.
        expected = steps.expected_carrying(
            run.engine_repo,
            run.transform_repo / self.CORPUS,
            run.args.per_module,
            self.MODULES,
            self.TRANSFORMED_FIELD,
        )
        transformed = steps.wait_rows(
            run.store,
            self.name,
            expected,
            self.TRANSFORM_DEADLINE,
            f"{corpus} AND {self._transformed}",
        )
        every = expected > 0 and transformed >= expected
        run.driver.record(
            "transformed",
            "done" if every else "failed",
            f"{transformed} of {self._posted} corpus rows carry {self.TRANSFORMED_COLUMN}, "
            f"which only the transform sets; the corpus expects {expected}",
        )
        if run.args.via in SHIPPED:
            self._record_envelope(run)
            run.driver.record(
                "envelope-kept", *beats.kept_outcome(run.store, self.name)
            )

    def _record_envelope(self, run: Run) -> None:
        """Name the envelope that reached the table, off the rows themselves.

        The agent's own file path is the half the corpus wrapper cannot produce,
        so a table without it is not evidence that this path fed anything.
        """
        carrying, keys, refused = beats.envelope_evidence(run.store, self.name)
        if refused:
            run.driver.record("envelope", "failed", refused)
            return
        run.driver.record(
            "envelope",
            "done",
            f"{carrying} row(s) carry log.file.path {beats.AGENT_PATH}, which only a Beats "
            f"agent sets, and one of them arrived with {', '.join(keys)}",
        )

    @property
    def _transformed(self) -> str:
        return f"{self.TRANSFORMED_COLUMN} IS NOT NULL"

    @property
    def _running_option(self) -> re.Pattern[str]:
        """This app's radio on the Transform card, once it has an instance for the source.

        The console names an option by its service, its state tag and a note, as in
        ``dfe-transform-vrl Selected Running for this source.``
        """
        return re.compile(rf"{re.escape(self.service)}[\s\S]*Running")

    def cleanup(self, run: Run) -> list[wizard.StepResult]:
        """Take down anything this run stood up beside the stack, and put the receiver back."""
        rows = []
        if self._pair is not None:
            rows.append(
                wizard.StepResult("beats-removed", "done", beats.stop(self._pair))
            )
        if self._listener.is_open:
            try:
                rows.append(self._listener.close(run))
            except (OSError, RuntimeError) as exc:
                rows.append(
                    wizard.StepResult(
                        "receiver-lumberjack-restored",
                        "failed",
                        f"{self._listener.route} was not put back ({exc}), so lumberjack may still be open",
                    )
                )
        return rows


# --- elastic: the same lines, through a transform compiled into the image -----


class ElasticCase(FilebeatCase):
    """Real cisco_ios lines pushed at the receiver, through dfe-transform-elastic.

    The same push and the same meta schema as the filebeat case, with two halves
    of its own. The app carries one transform per Elastic data stream and an
    instance runs one of them, named by ``transform.variant``, so the create has
    a second field the console has no control for. There is no program to write
    either: the transform is compiled in and the app declares no file sets.
    """

    TRANSFORM_ENGINE = "elastic"
    # A variant dfe-transform-elastic compiles in, `filebeat.<package>.<transform>` as the dfe-schemas catalogue lists it, which the deploy writes to the app's own config.source.name.
    VARIANT = "filebeat.cisco_ios.default"
    DISPLAY = "Elastic transform source test"
    DESCRIPTION = (
        "Post-deploy source test: real cisco_ios lines through the compiled-in "
        "elastic transform, archived."
    )

    service = f"dfe-transform-{TRANSFORM_ENGINE}"
    prefix = "el"

    def _transform_body(self) -> dict[str, str]:
        """Engine and variant together, because one without the other deploys nothing.

        The engine writes the variant into the instance's config only when the
        source carries one, so an instance created without it starts on no
        transform at all.
        """
        return {"engine": self.TRANSFORM_ENGINE, "variant": self.VARIANT}

    def provision(self, run: Run) -> list[str]:
        """Nothing to write, and one row saying why.

        dfe-transform-elastic declares no file sets: the transform is compiled
        into the image and selected by name.
        """
        run.driver.record(
            "upload-program",
            "skipped",
            f"{self.service} reads no authored files; {self.VARIANT} is compiled in",
        )
        return []


# --- vector: the same program, run by a supervised Vector ---------------------


class VectorCase(FilebeatCase):
    """The same filebeat lines and the same program, through dfe-transform-vector.

    dfe-transform-vector's bundled pipeline EMBEDS dfe-transform-vrl's
    filebeat.vrl byte-identical, wrapped in a Vector `remap` and carrying the
    same timezone table (its pipelines/filebeat/README.md records the provenance
    and the commit). So this is the filebeat case with the program written as
    Vector YAML into an app that supervises Vector, and the same column proves
    it: one program, two runtimes, one expected output.

    The corpus still comes out of dfe-transform-vrl; only the program moves, so
    the case names two checkouts.
    """

    TRANSFORM_ENGINE = "vector"
    # dfe-transform-vector pipelines/filebeat/. The wrapper writes a transform
    # file into Vector's config dir verbatim, so the file declares its own
    # top-level enrichment_tables and the VRL's timezone lookups resolve.
    PROGRAM = "pipelines/filebeat/filebeat.yaml"
    ENRICHMENT = "pipelines/filebeat/timezones.csv"
    DISPLAY = "Vector transform source test"
    DESCRIPTION = (
        "Post-deploy source test: real filebeat lines through the supervised "
        "Vector pipeline, archived."
    )

    service = f"dfe-transform-{TRANSFORM_ENGINE}"
    #: The corpus archive, shared with the filebeat and elastic cases.
    companion_repo = "dfe-transform-vrl"
    #: The checkout the program and its table are read from.
    program_repo = "dfe-transform-vector"
    prefix = "vc"


# --- syslog: raw vendor lines at the receiver's own listeners ----------------


class SyslogCase(Case):
    """The dfe-docker demo's cisco-asa and fortinet syslog, over every listener the cycle can reach.

    One source on the syslog listener detects the vendor per line and routes it to the
    source named for that vendor, which is the label the detector stamps, so the vendor
    tables carry the vendor's name and not a per-run one. Each transport sends the whole corpus, and on TCP a second
    connection carries a good line, an oversize one and a good line. The oversize message
    is discarded and counted, and the connection carries on. Every table must hold exactly
    what was sent, and the receiver's own counters must agree, so a short count names the
    hop that lost the line.
    """

    service = "dfe-transform-elastic"
    instance_step = "transform-instance"
    reporting_step = "transform-reporting"
    prefix = "sy"
    observes = False
    archives = False
    console = False
    LAND_DEADLINE = 180.0
    # A transport whose rows reached no replica by now is a miss, and stops waiting.
    MISS_DEADLINE = 30.0
    PROBE_EVERY = 20.0
    SETTLE_POLL = 10.0

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(args)
        self.name = fetcher.new_name(self.prefix)
        #: The table each vendor's lines land in, which the detector names by the vendor.
        self.tables = {vendor: vendor for vendor in syslog.VENDORS}
        self._listener = Listener(
            "receiver-syslog", syslog.SYSLOG_SWITCHES, syslog.SYSLOG_ENV
        )
        self._before: dict[str, int] | None = None
        #: Whether the datastore answers clusterAllReplicas, None until the first count asks.
        self._clustered: bool | None = None
        #: The tables this run created, which are the only ones cleanup removes.
        self._made: dict[str, str] = {}
        self._good = 0
        self._oversize = 0

    # -- create ---------------------------------------------------------------

    def create(self, run: Run) -> list[str]:
        """Open the listener, then create the vendor tables and the intake over the engine API."""
        hints = self._listener.open(run)
        if hints is None:
            raise RuntimeError(
                "the receiver's syslog listener did not open, so there is nothing to send to"
            )
        reused = []
        for vendor, table in self.tables.items():
            if run.engine.call("GET", f"/sources/{table}").status == 200:
                reused.append(table)
                continue
            created = run.engine.call(
                "POST", "/sources", syslog.vendor_body(vendor, table)
            )
            if created.status not in (200, 201, 202):
                raise RuntimeError(
                    f"creating {table} for {vendor} was refused: {created.status} {created.body}"
                )
            self._made[vendor] = table
            steps.deploy(run.engine, table)
            # A source created dormant gets no table, so it is deployed first and set dormant after.
            dormant = run.engine.call(
                "PATCH", f"/sources/{table}", {"state": "dormant"}
            )
            if dormant.status != 200:
                raise RuntimeError(
                    f"setting {table} dormant was refused: {dormant.status} {dormant.body}"
                )
        created = run.engine.call("POST", "/sources", syslog.intake_body(self.name))
        if created.status not in (200, 201, 202):
            raise RuntimeError(
                f"creating the intake {self.name} was refused: {created.status} {created.body}"
            )
        made = ", ".join(self._made.values()) or "none"
        kept = f", and found {', '.join(reused)} already there" if reused else ""
        run.driver.record(
            "add-source",
            "api-fallback",
            f"the engine created {self.name} on the syslog listener and the vendor tables {made}{kept}",
        )
        # A dormant vendor source runs no transform, so only the intake's write can need a restart.
        return hints + steps.reply_hints(created.body)

    # -- feed -----------------------------------------------------------------

    @staticmethod
    def _tag(run: Run, slug: str) -> str:
        """The hostname a transport's lines carry, which names the run."""
        return f"{run.run_id}-{slug}"

    @staticmethod
    def _exec(run: Run) -> list[str]:
        return shlex.split(run.args.syslog_exec)

    def _counts(self, run: Run) -> dict[str, int]:
        """Rows each vendor table holds on its fullest replica, which is the run's alone."""
        return {
            vendor: self._count(run, table) for vendor, table in self.tables.items()
        }

    def _replicas(self, run: Run, table: str, where: str = "") -> dict[str, int]:
        """Rows *table* holds on each replica, under "" for a datastore with no cluster.

        A part lands on the replica that took the INSERT and reaches the others later,
        so one replica's count runs short until they fetch it.
        """
        clause = f" WHERE {where}" if where else ""
        if self._clustered is not False:
            sql = (
                f"SELECT hostName(), count() FROM clusterAllReplicas(getMacro('cluster'), "
                f"{run.store.database}, `{table}`){clause} GROUP BY 1"
            )
            try:
                rows = self._retried(run, sql)
            except RuntimeError:
                if self._clustered:
                    raise
                self._clustered = False
            else:
                self._clustered = True
                return {str(host): int(count) for host, count in rows}
        return {"": self._scalar(run, f"SELECT count() FROM `{table}`{clause}")}

    def _count(self, run: Run, table: str, where: str = "") -> int:
        """Rows *table* holds on its fullest replica."""
        return max(self._replicas(run, table, where).values(), default=0)

    @staticmethod
    def _retried(run: Run, sql: str, tries: int = 4) -> list[list]:
        """One query, retried on a dropped connection."""
        for attempt in range(tries):
            try:
                return run.store.query(sql)
            except OSError:
                if attempt == tries - 1:
                    raise
                time.sleep(2)
        raise AssertionError("unreachable")

    @staticmethod
    def _scalar(run: Run, sql: str, tries: int = 4) -> int:
        """One count, retried on a dropped connection, which ClickHouse's HTTP port gives under load."""
        for attempt in range(tries):
            try:
                return run.store.scalar(sql)
            except OSError:
                if attempt == tries - 1:
                    raise
                time.sleep(2)
        raise AssertionError("unreachable")

    def _strayed(self, run: Run) -> int:
        """Run-tagged lines that reached dfe.main, which no source's match claimed."""
        host = "_json.hostname.:String"
        return self._count(
            run,
            "main",
            f"startsWith({host}, '{run.run_id}-') AND NOT endsWith({host}, '-probe')",
        )

    #: Where the syslog header's hostname lands: the elastic transform's ECS copy, or
    #: the receiver's own field on a row that went through untransformed.
    TAG_PATHS = ("_json.log.syslog.hostname.:String", "_json.hostname.:String")

    def _by_tag(self, run: Run, tag: str) -> dict[str, int]:
        """Rows carrying *tag* in each vendor table, the intake table and dfe.main."""
        match = " OR ".join(f"{path} = '{tag}'" for path in self.TAG_PATHS)
        tables = {**self.tables, "intake": self.name, "dfe.main": "main"}
        return {
            label: self._count(run, table, match) for label, table in tables.items()
        }

    @staticmethod
    def _counted(run: Run) -> bool:
        """Whether the run was told where the receiver's counters are."""
        args = run.args
        return bool(
            args.receiver_metrics_url
            or (args.receiver_metrics_selector and args.kubectl)
        )

    @staticmethod
    def _counters(run: Run) -> dict[str, int]:
        """The receiver's syslog counters now, summed over every pod when a selector is given.

        Raises:
            RuntimeError: The counters could not be read.
        """
        args = run.args
        if args.receiver_metrics_selector and args.kubectl:
            return syslog.read_pod_counters(
                shlex.split(args.kubectl),
                args.namespace,
                args.receiver_metrics_selector,
            )
        return syslog.read_counters(args.receiver_metrics_url)

    @classmethod
    def _read_counters(cls, run: Run) -> dict[str, int] | None:
        """The receiver's syslog counters now, None when none were named or they did not answer."""
        if not cls._counted(run):
            return None
        try:
            return cls._counters(run)
        except RuntimeError:
            return None

    def _accepted(self, run: Run) -> int | None:
        """What the receiver has accepted since the run began, None when it cannot be read."""
        if self._before is None:
            return None
        now = self._read_counters(run)
        return None if now is None else now["accepted"] - self._before["accepted"]

    def _probe(
        self, run: Run, start: dict[str, int]
    ) -> tuple[bool, dict[str, int], int]:
        """Send a cisco-asa line until its table gains a row, then wait for the counts to settle.

        The receiver rolls onto the new rule and Argo spawns the intake's transform, so the
        first lines can take minutes to route. Probes are tagged so they are never counted
        as the corpus, and the settled counts are the baseline the transports add to.

        Returns:
            Whether a row arrived, the counts after they settled, and the probes sent.
        """
        args, line = run.args, syslog.corpus("cisco-asa", 1)[0]
        host = self._tag(run, "probe")
        until = time.monotonic() + steps.ROUTING_DEADLINE
        tcp, sent = syslog.TRANSPORTS[0], 0
        while self._counts(run)["cisco-asa"] <= start["cisco-asa"]:
            probe = syslog.frame(tcp, syslog.message(line, host, syslog.stamp_now()))
            syslog.send_tcp(
                args.syslog_host, args.syslog_port, [probe], self._exec(run)
            )
            sent += 1
            if time.monotonic() >= until:
                return False, self._counts(run), sent
            time.sleep(self.PROBE_EVERY)
        last = self._counts(run)
        while True:
            time.sleep(self.SETTLE_POLL)
            now = self._counts(run)
            if now == last:
                return True, now, sent
            last = now

    def _land(
        self, run: Run, wanted: dict[str, int], base: dict[str, int], deadline: float
    ) -> tuple[dict[str, int], str]:
        """Rows by vendor on the fullest replica, polled until every replica holds *wanted*.

        Stops at *deadline*, or at MISS_DEADLINE when no new row reached any replica.

        Returns:
            The counts, and per vendor the replica that held its rows first and when every
            replica did, so a slow fetch shows without failing the step.
        """
        start = time.monotonic()
        first: dict[str, tuple[str, float]] = {}
        every: dict[str, float] = {}
        while True:
            spread = {
                vendor: self._replicas(run, table)
                for vendor, table in self.tables.items()
            }
            found = {
                vendor: max(held.values(), default=0) for vendor, held in spread.items()
            }
            took = time.monotonic() - start
            for vendor, want in wanted.items():
                holding = sorted(
                    host for host, count in spread[vendor].items() if count >= want
                )
                if holding and vendor not in first:
                    first[vendor] = (holding[0], took)
                if (
                    holding
                    and len(holding) == len(spread[vendor])
                    and vendor not in every
                ):
                    every[vendor] = took
            missed = took >= self.MISS_DEADLINE and found == base
            if all(vendor in every for vendor in wanted) or missed or took >= deadline:
                return found, self._lag_note(wanted, first, every, took)
            time.sleep(5)

    @staticmethod
    def _lag_note(
        wanted: dict[str, int],
        first: dict[str, tuple[str, float]],
        every: dict[str, float],
        took: float,
    ) -> str:
        """Where each vendor's rows were first seen, and how long every replica took."""
        parts = []
        for vendor in wanted:
            if vendor not in first:
                parts.append(f"{vendor} on no replica after {took:.0f}s")
                continue
            host, at = first[vendor]
            tail = (
                f"every replica at {every[vendor]:.0f}s"
                if vendor in every
                else f"not on every replica after {took:.0f}s"
            )
            parts.append(
                f"{vendor} first on {host or 'the datastore'} at {at:.0f}s, {tail}"
            )
        return "replicas: " + ", ".join(parts)

    def _send(
        self, run: Run, transport: syslog.Transport, lines: dict[str, list[str]]
    ) -> tuple[int, int, str]:
        """Send one transport's corpus.

        Returns:
            The good frames, the oversize ones, and what the sender saw of the connection.
        """
        args, prefix = run.args, self._exec(run)
        host, stamp = self._tag(run, transport.slug), syslog.stamp_now()
        frames = [
            syslog.frame(transport, syslog.message(line, host, stamp))
            for vendor in syslog.VENDORS
            for line in lines[vendor]
        ]
        if not transport.tcp:
            note = syslog.send_udp(
                args.syslog_host, args.syslog_udp_port, frames, prefix
            )
            return len(frames), 0, note
        notes = [syslog.send_tcp(args.syslog_host, args.syslog_port, frames, prefix)]
        # A second connection: a good line, one past the limit, then a good line.
        good = syslog.frame(
            transport, syslog.message(lines["cisco-asa"][0], host, stamp)
        )
        big = syslog.frame(transport, syslog.oversize(host, stamp))
        notes.append(
            syslog.send_tcp(
                args.syslog_host, args.syslog_port, [good, big, good], prefix, watch=1
            )
        )
        return len(frames) + 2, 1, " ".join(note for note in notes if note)

    def feed(self, run: Run) -> None:
        driver, args = run.driver, run.args
        if not run.store.host:
            driver.record(
                "feed",
                "unproven",
                "no datastore access in this run, so nothing sent could be counted",
            )
            return
        start = self._counts(run)
        self._before = self._read_counters(run)
        routed, base, probes = self._probe(run, start)
        table = self.tables["cisco-asa"]
        if routed:
            driver.record(
                "routed",
                "done",
                f"cisco-asa lines reached dfe.`{table}` through the syslog listener",
            )
        else:
            hops = syslog.hop_report(
                probes,
                self._accepted(run),
                sum(base[v] - start[v] for v in base),
                self._count(run, self.name),
                self._strayed(run),
            )
            driver.record(
                "routed",
                "failed",
                f"no cisco-asa row in dfe.`{table}` after {steps.ROUTING_DEADLINE:.0f}s of probes; {hops}",
            )
            return
        self._before = self._read_counters(run)
        lines = {
            vendor: syslog.corpus(vendor, args.syslog_lines)
            for vendor in syslog.VENDORS
        }
        seen = dict(base)
        started = dict(base)
        for transport in syslog.TRANSPORTS:
            good, big, note = self._send(run, transport, lines)
            self._good += good
            self._oversize += big
            # The TCP connection around the oversize frame adds a good cisco-asa line either side.
            expected = {
                vendor: len(rows)
                + (2 if transport.tcp and vendor == "cisco-asa" else 0)
                for vendor, rows in lines.items()
            }
            wanted = {
                vendor: seen[vendor] + count for vendor, count in expected.items()
            }
            found, spread = self._land(run, wanted, seen, self.LAND_DEADLINE)
            landed = {vendor: found[vendor] - seen[vendor] for vendor in found}
            seen = found
            hops = syslog.hop_report(
                self._good,
                self._accepted(run),
                sum(found[v] - started[v] for v in found),
                self._count(run, self.name),
                self._strayed(run),
            )
            tagged = self._by_tag(run, self._tag(run, transport.slug))
            status, detail = syslog.phase_outcome(expected, landed, hops, note)
            placed = {**expected, "intake": 0, "dfe.main": 0}
            if tagged != placed:
                status = "failed"
            by_tag = ", ".join(f"{label} {count}" for label, count in tagged.items())
            driver.record(
                f"syslog-{transport.slug}",
                status,
                f"{detail}; by tag: {by_tag}; {spread}",
            )
        driver.record(
            "syslog-tls",
            "skipped",
            "NOT COVERED: the receiver chart has no TLS syslog listener, "
            "and no overlay in the cycle sets syslog.tls.enabled",
        )
        driver.record(
            "feed",
            "done",
            f"sent {self._good} good frame(s) and {self._oversize} oversize frame(s) over "
            f"{', '.join(t.slug for t in syslog.TRANSPORTS)}, tagged {run.run_id}",
        )

    # -- prove ----------------------------------------------------------------

    def prove(self, run: Run) -> None:
        driver = run.driver
        if not run.store.host or not self._good:
            driver.record(
                "syslog-counters",
                "unproven",
                "nothing was sent, so there is nothing to count",
            )
            return
        strayed = self._strayed(run)
        driver.record(
            "unrouted",
            "failed" if strayed else "done",
            f"{strayed} run-tagged line(s) reached dfe.main, where a line no source's match claims lands",
        )
        if not self._counted(run) or self._before is None:
            driver.record(
                "syslog-counters",
                "unproven",
                "the receiver's counters were not read before the send (set "
                "--receiver-metrics-selector with --kubectl, or --receiver-metrics-url)",
            )
            return
        try:
            after = self._counters(run)
        except RuntimeError as exc:
            driver.record("syslog-counters", "failed", str(exc))
            return
        driver.record(
            "syslog-counters",
            *syslog.counters_outcome(self._good, self._oversize, self._before, after),
        )

    def cleanup(self, run: Run) -> list[wizard.StepResult]:
        """Close the listener, and remove the vendor tables the runner's own teardown does not know."""
        rows = []
        if not run.args.keep:
            for vendor, table in self._made.items():
                detail = remove_source(
                    run.engine.base,
                    run.engine.verify,
                    run.engine.token or "",
                    table,
                    onboarding.TEARDOWN_DEADLINE,
                )
                rows.append(
                    wizard.StepResult(
                        f"teardown-{vendor}",
                        "done" if detail.startswith("removed") else "failed",
                        detail,
                    )
                )
        if self._listener.is_open:
            try:
                rows.append(self._listener.close(run))
            except (OSError, RuntimeError) as exc:
                rows.append(
                    wizard.StepResult(
                        "receiver-syslog-restored",
                        "failed",
                        f"{self._listener.route} was not put back ({exc}), so syslog may still be open",
                    )
                )
        return rows


# --- a fetched AWS upstream --------------------------------------------------


class FetchedAwsCase(Case):
    """A meta schema authored by hand, and an AWS upstream a fetcher polls on its own.

    Nothing is pushed and nothing is written to AWS: the source's stanza is the
    whole configuration, and the proof is rows appearing inside a poll interval.
    Which AWS service is polled comes from ``--aws-service``.
    """

    service = fetcher.FETCHER_SERVICE
    instance_step = "fetcher-instance"
    reporting_step = "fetcher-reporting"
    instance_deadline = fetcher.INSTANCE_DEADLINE
    reporting_deadline = fetcher.REPORTING_DEADLINE

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(args)
        self.upstream = fetcher.AWS_CASES[args.aws_service]
        self.name = fetcher.new_name("cw")
        self.schema = fetcher.schema_path(self.upstream, self.name)

    def create(self, run: Run) -> list[str]:
        driver = run.driver
        csv_file = Path(run.args.shots_dir) / f"{Path(self.schema).name}.csv"
        csv_file.parent.mkdir(parents=True, exist_ok=True)
        csv_file.write_text(fetcher.schema_csv(self.upstream), encoding="utf-8")
        console_detail, api_detail, refused = "", "", ""
        try:
            console_detail = fetcher.author_schema_in_console(
                driver, self.upstream, self.schema, csv_file, STEP_TIMEOUT_MS
            )
        except Exception as exc:  # the console refusing to author IS the finding
            refused = steps.refusal(exc)
            api_detail = fetcher.author_schema_by_api(
                run.engine, self.upstream, self.schema
            )
        driver.record(
            "author-schema", *steps.console_outcome(console_detail, refused, api_detail)
        )

        # Add Source has no fetcher origin (dfe-ui #286), so the survey IS the
        # finding: the form is opened, read for a fetcher control, and reported.
        driver.record(
            "add-source-survey",
            "api-fallback",
            fetcher.survey_origin(driver, STEP_TIMEOUT_MS),
        )
        body = fetcher.source_body(
            self.upstream,
            self.name,
            self.schema,
            run.args.aws_region,
            run.args.aws_log_group,
            run.args.poll_interval_secs,
        )
        detail, hints = fetcher.create_source(run.engine, body)
        driver.record("add-source", "api-fallback", detail)
        return hints

    def feed(self, run: Run) -> None:
        """A fetched source feeds itself; what this records is that it had work."""
        service_name = fetcher.telemetry_name(run.engine, self.name)
        samples, since = steps.idle_history(
            run.store, service_name, int(self.reporting_deadline)
        )
        run.driver.record(
            "fetcher-idle",
            "done",
            f"{service_name} published {samples} pipeline_idle sample(s)"
            + (
                f", last {since}s ago"
                if since is not None
                else "; it was created carrying its stanza, so it had work from the start"
            ),
        )

    def prove(self, run: Run) -> None:
        if not run.store.host:
            run.driver.record("fetched", "unproven", "no datastore access in this run")
            return
        # A poll interval to fetch, and the landing topic plus the loader's own
        # batch window before the row is queryable.
        deadline = run.args.poll_interval_secs * 2 + 300
        gained = steps.wait_rows(run.store, self.name, 1, deadline, interval=15.0)
        run.driver.record(
            "fetched",
            "done" if gained else "failed",
            f"dfe.{self.name} holds {gained} row(s) from "
            f"{fetcher.upstream_note(self.upstream, run.args.aws_log_group)} "
            f"within {deadline:.0f}s, nothing written to AWS",
        )
        if gained:
            columns = self.upstream.proof_columns
            carried = " AND ".join(f"{column} != ''" for column in columns)
            short = run.store.scalar(
                f"SELECT count() FROM {self.name} WHERE NOT ({carried})"
            )
            run.driver.record(
                "fetched-columns",
                "failed" if short else "done",
                f"{short} of the landed row(s) lack one of {', '.join(columns)}",
            )

    def cleanup(self, run: Run) -> list[wizard.StepResult]:
        """The schema the run authored is the run's to remove; a kept source still reads from it."""
        if run.args.keep:
            return []
        detail = fetcher.remove_schema(run.engine, self.schema)
        return [
            wizard.StepResult(
                "schema-removed",
                "done" if detail.startswith("removed") else "failed",
                detail,
            )
        ]


#: Every case a run may ask for, which is also the runner's --case choices, so
#: a case cannot exist without being reachable.
CASES: dict[str, type[Case]] = {
    "filebeat": FilebeatCase,
    "cloudwatch": FetchedAwsCase,
    "elastic": ElasticCase,
    "vector": VectorCase,
    "syslog": SyslogCase,
}


def build(args: argparse.Namespace) -> Case:
    """The case this run was asked for."""
    return CASES[args.case](args)
