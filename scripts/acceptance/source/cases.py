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

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path

from acceptance.clients import Datastore, Engine
from acceptance.onboarding import wizard
from acceptance.source import fetcher, steps
from acceptance.source.steps import STEP_TIMEOUT_MS

# The bundled filebeat program is a few hundred kilobytes, so the editor write
# and its commit get a longer bound than a form control.
EDITOR_TIMEOUT_MS = STEP_TIMEOUT_MS * 4
# How long a committed file is waited for on the engine, since the console's own
# notice is per panel and a panel the run has left keeps its copy.
COMMIT_DEADLINE = 180.0


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


class Case:
    """One source kind, create to proof.

    The runner owns the order; a case owns what is true only of its own kind.
    """

    #: The per-source app this kind's source deploys.
    service: str = ""
    #: The checkout beside the engine repo this kind reads its inputs from, for
    #: a run that was given no --transform-repo. Empty when it reads none.
    companion_repo: str = ""
    #: What the two shared waits report as, so the report keeps this kind's words.
    instance_step: str = ""
    reporting_step: str = ""
    instance_deadline: float = 120.0
    reporting_deadline: float = steps.ROUTING_DEADLINE

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.name = ""

    def create(self, run: Run) -> None:
        """Bring the source into being, through the console where it can."""
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
    """Real filebeat lines pushed at the receiver, through the bundled VRL, archived.

    The shipped meta schema, the cheapest receiver match, the bundled filebeat
    program with its timezone table, the archive on.
    """

    META_SCHEMA = "meta/beats/filebeat"
    META_SCHEMA_VERSION = "1.0.0"
    HEADER = "common-header/timeseries"
    HEADER_VERSION = "1.0.1"
    MATCH_FIELD = "_source"
    TRANSFORM_ENGINE = "vrl"
    PROGRAM = "pipelines/filebeat/filebeat.vrl"
    ENRICHMENT = "pipelines/filebeat/timezones.csv"
    CORPUS = "tests/fixtures/filebeat/filebeat-testdata.tar.gz"
    # Corpus modules to feed; empty is every module the wrapper names.
    MODULES: tuple[str, ...] = ()
    # Display name and description the console form is filled with.
    DISPLAY = "Filebeat source test"
    DESCRIPTION = "Post-deploy source test: real filebeat lines through the bundled VRL, archived."
    # A column only the transform sets on the umbrella branch; absent from the body.
    TRANSFORMED_COLUMN = "log_file_path"
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
        self._before = 0
        self._before_transformed = 0

    # -- create ---------------------------------------------------------------

    def create(self, run: Run) -> None:
        driver, page = run.driver, run.driver.page
        # The page header repeats the nav entry as a link, so the nav one is first.
        page.get_by_role("link", name="Sources", exact=True).first.click(timeout=STEP_TIMEOUT_MS)
        page.wait_for_url("**/sources**", timeout=STEP_TIMEOUT_MS)
        driver.button("Add Source").first.click(timeout=STEP_TIMEOUT_MS)
        page.get_by_placeholder("Enter source").fill(self.name)
        page.get_by_placeholder("Enter display name").fill(f"{self.DISPLAY} {self.name}")
        page.get_by_placeholder("Enter description").fill(self.DESCRIPTION)
        # The switches are Enabled (on) then Archive (off); the archive one is second.
        page.get_by_role("switch").nth(1).click(timeout=STEP_TIMEOUT_MS)
        page.get_by_placeholder("Enter field").fill(self.MATCH_FIELD)
        page.get_by_placeholder("Enter value").fill(self.name)
        driver.record("add-source-configuration", "done",
                      f"filled the Configuration tab for {self.name}, archive on")

        page.get_by_role("tab", name="Meta Schema", exact=True).click(timeout=STEP_TIMEOUT_MS)
        page.get_by_text("Define Schema", exact=True).click(timeout=STEP_TIMEOUT_MS)
        # The selects group by family and show the leaf: common-header > timeseries.
        steps.select_option(page, 0, self.HEADER.rsplit("/", 1)[-1])
        steps.select_option(page, 1, self.HEADER_VERSION)
        steps.select_option(page, 2, self.META_SCHEMA.rsplit("/", 1)[-1])
        steps.select_option(page, 3, self.META_SCHEMA_VERSION)
        driver.record("add-source-meta-schema", "done",
                      f"picked {self.HEADER} {self.HEADER_VERSION} and "
                      f"{self.META_SCHEMA} {self.META_SCHEMA_VERSION}")

        # The Transform tab renders only once a meta schema is picked, so it is
        # filled in here and the create carries the transform with it.
        refused = ""
        try:
            self._pick_transform(page)
        except Exception as exc:  # the console refusing the control IS the finding
            refused = steps.refusal(exc)

        driver.button("Add Source").last.click(timeout=STEP_TIMEOUT_MS)
        page.get_by_text("Source created successfully").wait_for(timeout=STEP_TIMEOUT_MS)
        driver.record("add-source", "done", f"the console created {self.name}")
        self._record_transform(run, refused)

    def _pick_transform(self, page) -> None:
        """Transform tab: Define Transform, then the engine the catalogue offers.

        The picker lists the catalogued transform apps by service, so the option
        is the app's name and the value it sets is the engine.
        """
        page.get_by_role("tab", name="Transform", exact=True).click(timeout=STEP_TIMEOUT_MS)
        page.get_by_text("Define Transform", exact=True).click(timeout=STEP_TIMEOUT_MS)
        steps.select_labelled(page, "Transform Engine", self.service)

    def _record_transform(self, run: Run, refused: str) -> None:
        """The attach-transform row, read back off the engine rather than off the form."""
        console_detail, api_detail = "", ""
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
            api_detail = self._attach_by_api(run)
        status, detail = steps.console_outcome(console_detail, refused, api_detail)
        run.driver.record("attach-transform", status, detail)

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
        return {key: str(attached[key]) for key in self._transform_body() if attached.get(key)}

    def _attach_by_api(self, run: Run) -> str:
        """The same transform through the engine, for a console without the control."""
        engine = run.engine
        current = engine.call("GET", f"/sources/{self.name}")
        if current.status != 200:
            raise RuntimeError(f"cannot read {self.name} back: {current.status} {current.body}")
        transform = self._transform_body()
        body = {
            "source": self.name,
            "display_name": current.body.get("display_name"),
            "description": current.body.get("description"),
            "match": current.body.get("match"),
            "header": {"type": self.HEADER, "version": self.HEADER_VERSION},
            "schema": {"meta_schema": self.META_SCHEMA, "meta_schema_version": self.META_SCHEMA_VERSION},
            "transform": transform,
            "archive": True,
        }
        updated = engine.call("PUT", f"/sources/{self.name}", body)
        if updated.status != 200:
            raise RuntimeError(f"attaching the transform was refused: {updated.status} {updated.body}")
        return f"transform {transform} set on {self.name}"

    # -- provision ------------------------------------------------------------

    def provision(self, run: Run) -> list[str]:
        """The program and its table, into the instance's file sets.

        Those sets only exist once the deploy has created the instance, which is
        why this is not part of create.
        """
        wanted = [
            (self.PROGRAM_SET, Path(self.PROGRAM).name, (run.transform_repo / self.PROGRAM).read_text(encoding="utf-8")),
            (self.ENRICHMENT_SET, Path(self.ENRICHMENT).name, (run.transform_repo / self.ENRICHMENT).read_text(encoding="utf-8")),
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
        version = str((run.engine.call("GET", f"/sources/{self.name}").body or {}).get("current") or "")
        page.goto(
            f"{driver.ui}/sources?source_name={self.name}&source_version={version}&tab=processing",
            wait_until="domcontentloaded", timeout=STEP_TIMEOUT_MS * 2,
        )
        page.get_by_role("heading", name=self.service, exact=True).first.wait_for(
            state="visible", timeout=STEP_TIMEOUT_MS
        )
        # The card for a deployed instance is the only one carrying file sets; a
        # catalogued app with no instance for this source offers no editor.
        page.get_by_text(f"Instance {self.name}", exact=True).first.wait_for(
            state="visible", timeout=STEP_TIMEOUT_MS
        )
        landed = []
        for set_name, filename, content in wanted:
            self._commit_file(page, set_name, filename, content)
            size = self._wait_committed(run, set_name, filename, len(content))
            landed.append(f"{filename} {size} bytes")
        return f"the console committed {', '.join(landed)} to {self.service}/{self.name}"

    def _commit_file(self, page, set_name: str, filename: str, content: str) -> None:
        """New file, name it, put the content in the editor, Commit file."""
        tab = page.get_by_role("tab", name=set_name, exact=True)
        if tab.count():
            tab.first.click(timeout=STEP_TIMEOUT_MS)
        page.get_by_role("button", name="New file").first.click(timeout=STEP_TIMEOUT_MS)
        page.get_by_label("New filename", exact=True).fill(filename, timeout=STEP_TIMEOUT_MS)
        editor = page.locator(f"#{set_name}-editor")
        editor.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
        editor.locator(".ace_content").click(timeout=STEP_TIMEOUT_MS)
        # One insert, not keystrokes: the program is a few hundred kilobytes.
        page.keyboard.insert_text(content)
        # Live autocompletion opens on the insert and would swallow the click.
        page.keyboard.press("Escape")
        page.get_by_role("button", name="Commit file").first.click(timeout=EDITOR_TIMEOUT_MS)

    def _wait_committed(self, run: Run, set_name: str, filename: str, size: int) -> int:
        """The file as the engine holds it, which is the proof the commit landed."""
        until = time.monotonic() + COMMIT_DEADLINE
        last = ""
        while True:
            reply = run.engine.call(
                "GET", f"/apps/{self.service}/{self.name}/files/{set_name}/{filename}"
            )
            if reply.status == 200 and int((reply.body or {}).get("size_bytes") or 0) >= size:
                return int(reply.body["size_bytes"])
            last = f"{reply.status} {str(reply.body)[:120]}"
            if time.monotonic() >= until:
                raise RuntimeError(
                    f"{set_name}/{filename} was not committed within {COMMIT_DEADLINE:.0f}s; last {last}"
                )
            time.sleep(5)

    def _upload_by_api(self, run: Run, wanted: list[tuple[str, str, str]]) -> tuple[str, list[str]]:
        """The same files through the engine, and the restarts the writes call for.

        The instance's config is only rendered with the source's topics once the
        overlay exists, so these writes are where a Compose deployment learns it
        must restart the app, on top of whatever the deploy before them reported.
        """
        told, hints = [], []
        for set_name, filename, content in wanted:
            reply = run.engine.call(
                "PUT", f"/apps/{self.service}/{self.name}/files/{set_name}/{filename}",
                {"content": content},
            )
            if reply.status not in (200, 201):
                raise RuntimeError(f"the {set_name} upload was refused: {reply.status} {reply.body}")
            told.append(f"{filename} {len(content)} bytes accepted")
            hints += [str(h) for h in ((reply.body or {}).get("restart_required") or [])]
        return ", ".join(told), hints

    # -- feed and prove -------------------------------------------------------

    def feed(self, run: Run) -> None:
        if not run.receiver_url:
            run.driver.record("feed", "skipped", "no receiver in this run (DFE_E2E_RECEIVER_URL unset)")
            return
        corpus = run.transform_repo / self.CORPUS
        detail = steps.wait_routed(
            run.receiver_url, run.verify, run.engine_repo, corpus, run.store, self.name,
            steps.ROUTING_DEADLINE, self.MODULES,
        )
        run.driver.record("routed", "done" if detail.startswith("routed") else "failed", detail)
        self._before = run.store.scalar(f"SELECT count() FROM {self.name}")
        self._before_transformed = run.store.scalar(
            f"SELECT count() FROM {self.name}{self._transformed_where}"
        )
        self._posted = steps.post_corpus(
            run.receiver_url, run.verify, run.engine_repo, corpus, self.name,
            run.run_id, run.args.per_module, self.MODULES,
        )
        run.driver.record("feed", "done",
                          f"posted {self._posted} corpus records tagged e2e_run:{run.run_id}")

    def prove(self, run: Run) -> None:
        if not run.receiver_url:
            return
        # The program rewrites each record into ECS and the tag with it, so the
        # proof is the gain in the source's own table; the catch-all still holds
        # the record as posted, so a stray there is found by the tag.
        landed = steps.wait_gain(run.store, self.name, self._before, self._posted, self.LAND_DEADLINE)
        strayed = run.store.scalar(f"SELECT count() FROM main WHERE _raw LIKE '%{run.run_id}%'")
        run.driver.record(
            "landed", "done" if landed and not strayed else "failed",
            f"dfe.{self.name} gained {landed} of {self._posted} posted rows, {strayed} strayed into dfe.main",
        )
        transformed = steps.wait_gain(
            run.store, self.name, self._before_transformed, 1, self.TRANSFORM_DEADLINE,
            self._transformed_where,
        )
        run.driver.record(
            "transformed", "done" if transformed else "failed",
            f"{transformed} new rows carry {self.TRANSFORMED_COLUMN}, which only the transform sets",
        )

    @property
    def _transformed_where(self) -> str:
        return f" WHERE {self.TRANSFORMED_COLUMN} IS NOT NULL"


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
    # dfe-transform-elastic sources.yaml `filebeat.<entry>.<transform>`, which
    # apps.yaml's catalogue.variant_pattern builds and the deploy writes to the
    # app's own config.source.name.
    VARIANT = "filebeat.cisco_ios.default"
    # The one corpus module this variant transforms. cisco_umbrella is delivered
    # from an S3 bucket and takes no receiver intake; cisco_meraki's pipeline is
    # framed as a body rather than a syslog line.
    MODULES = ("cisco_ios",)
    # ECS source.ip, which meta/beats/filebeat declares and the cisco_ios
    # transform reads out of the syslog body; the posted record carries no
    # such field.
    TRANSFORMED_COLUMN = "source_ip"
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
            "upload-program", "skipped",
            f"{self.service} reads no authored files; {self.VARIANT} is compiled in",
        )
        return []


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

    def create(self, run: Run) -> None:
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
            api_detail = fetcher.author_schema_by_api(run.engine, self.upstream, self.schema)
        driver.record("author-schema", *steps.console_outcome(console_detail, refused, api_detail))

        # Add Source has no fetcher origin (dfe-ui #286), so the survey IS the
        # finding: the form is opened, read for a fetcher control, and reported.
        driver.record("add-source-survey", "api-fallback", fetcher.survey_origin(driver, STEP_TIMEOUT_MS))
        body = fetcher.source_body(
            self.upstream, self.name, self.schema, run.args.aws_region,
            run.args.aws_log_group, run.args.poll_interval_secs,
        )
        driver.record("add-source", "api-fallback", fetcher.create_source(run.engine, body))

    def feed(self, run: Run) -> None:
        """A fetched source feeds itself; what this records is that it had work."""
        service_name = fetcher.telemetry_name(run.engine, self.name)
        samples, since = fetcher.idle_history(run.store, service_name, int(self.reporting_deadline))
        run.driver.record(
            "fetcher-idle", "done",
            f"{service_name} published {samples} pipeline_idle sample(s)"
            + (f", last {since}s ago" if since is not None
               else "; it was created carrying its stanza, so it had work from the start"),
        )

    def prove(self, run: Run) -> None:
        if not run.store.host:
            run.driver.record("fetched", "skipped", "no datastore access in this run")
            return
        # A poll interval to fetch, and the landing topic plus the loader's own
        # batch window before the row is queryable.
        deadline = run.args.poll_interval_secs * 2 + 300
        gained = steps.wait_gain(run.store, self.name, 0, 1, deadline, interval=15.0)
        run.driver.record(
            "fetched", "done" if gained else "failed",
            f"dfe.{self.name} holds {gained} row(s) from "
            f"{fetcher.upstream_note(self.upstream, run.args.aws_log_group)} "
            f"within {deadline:.0f}s, nothing written to AWS",
        )
        if gained:
            column = self.upstream.proof_column
            set_rows = run.store.scalar(f"SELECT count() FROM {self.name} WHERE {column} != ''")
            run.driver.record(
                "fetched-columns", "done" if set_rows else "failed",
                f"{set_rows} of {gained} row(s) carry {column}, which only this upstream sets",
            )

    def cleanup(self, run: Run) -> list[wizard.StepResult]:
        """The schema the run authored is the run's to remove; a kept source still reads from it."""
        if run.args.keep:
            return []
        detail = fetcher.remove_schema(run.engine, self.schema)
        return [wizard.StepResult(
            "schema-removed", "done" if detail.startswith("removed") else "failed", detail
        )]


#: Every case a run may ask for, which is also the runner's --case choices, so
#: a case cannot exist without being reachable.
CASES: dict[str, type[Case]] = {
    "filebeat": FilebeatCase,
    "cloudwatch": FetchedAwsCase,
    "elastic": ElasticCase,
}


def build(args: argparse.Namespace) -> Case:
    """The case this run was asked for."""
    return CASES[args.case](args)
