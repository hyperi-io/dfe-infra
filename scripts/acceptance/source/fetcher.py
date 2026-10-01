#  Project:      dfe-infra
#  File:         acceptance/source/fetcher.py
#  Purpose:      The fetched-origin half of the source test: author a meta schema
#                by hand, point a fetcher at a cloud upstream, and prove the rows
#                it pulls land in the source's own table.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The fetcher cases of the source suite.

Where the filebeat case pushes data at the receiver, a fetched source has no
ingest step at all: the source's fetcher stanza is the whole configuration, one
deployment is stood up for it, and the proof is that rows appear on their own
within a poll interval.

The meta schema is authored by hand here rather than picked from the shipped
tree, because that is the part of the product an operator reaches for the first
time a source has no schema waiting for it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from acceptance.clients import Engine

HEADER = "common-header/timeseries"
HEADER_VERSION = "1.0.1"
SCHEMA_VERSION = "1.0.0"
# The AWS credential fields the source stanza references. The values live in the
# deployment's fetcher credential Secret (helm/charts/dfe-fetcher values
# credentials.secretName), so only the variable NAMES are ever written to git.
ACCESS_KEY_ENV = "AWS_ACCESS_KEY_ID"
SECRET_KEY_ENV = "AWS_SECRET_ACCESS_KEY"
FETCHER_SERVICE = "dfe-fetcher"
# An instance is created carrying its stanza, so Argo has to render, schedule and
# start a new deployment before anything reports.
INSTANCE_DEADLINE = 300.0
REPORTING_DEADLINE = 900.0


@dataclass(frozen=True)
class Column:
    """One authored meta-schema column, in the shape both the console and the API take."""

    name: str
    type: str
    use_case: str
    expr: str
    comment: str

    def csv_row(self) -> str:
        return f"{self.name},{self.type},{self.use_case},{self.expr},{self.comment}"

    def api_column(self) -> dict[str, str]:
        return {
            "name": self.name,
            "type": self.type,
            "use_case": self.use_case,
            "expr": self.expr,
            "comment": self.comment,
            "_field_type": "user_defined",
        }


CSV_HEADER = "name,type,use_case,expr,comment"


@dataclass(frozen=True)
class AwsCase:
    """One AWS service the fetcher polls, and the schema its records need.

    ``columns`` is what an operator would sit down and write for this upstream.
    ``proof_column`` is the one a row must carry for the record to have come from
    this service rather than from anywhere else.
    """

    service: str
    stem: str
    summary: str
    columns: tuple[Column, ...]
    proof_column: str
    service_config: tuple[str, ...] = field(default=())


# CloudWatch Logs, from FilterLogEvents. The event carries logStreamName,
# timestamp, message, ingestionTime and eventId and NOTHING else: the log group,
# the account and the region are properties of the CALL, not of the event, so
# those three columns are authored for the reader's sake and arrive empty until
# the fetcher stamps them on (dfe-fetcher src/source/aws/mod.rs fetch_cloudwatch_logs).
CLOUDWATCH_LOGS = AwsCase(
    service="cloudwatch_logs",
    stem="cloudwatch_logs",
    summary="CloudWatch Logs events, authored by hand for the source test",
    columns=(
        Column("timestamp", "datetime", "range", "@source: timestamp", "Event timestamp"),
        Column("log_group", "string", "dimension", "@source: logGroupName", "CloudWatch log group"),
        Column("log_stream", "string", "dimension", "@source: logStreamName", "CloudWatch log stream"),
        Column("message", "text", "fulltext", "@source: message", "Log event message"),
        Column("event_id", "string", "bloom", "@source: eventId", "Unique log event identifier"),
        Column("ingestion_time", "datetime", "range", "@source: ingestionTime", "When CloudWatch ingested it"),
        Column("aws_account", "string", "dimension", "@source: accountId", "AWS account the group belongs to"),
        Column("aws_region", "string", "dimension", "@source: awsRegion", "AWS region the group lives in"),
    ),
    proof_column="log_stream",
    service_config=("log_group_name",),
)

# CloudTrail, from LookupEvents. Every API call against the account is an event,
# so the upstream refreshes itself and needs nothing written to it.
CLOUDTRAIL = AwsCase(
    service="cloudtrail",
    stem="cloudtrail",
    summary="CloudTrail management events, authored by hand for the source test",
    columns=(
        Column("event_id", "string", "bloom", "@source: EventId", "Unique CloudTrail event identifier"),
        Column("event_name", "string", "dimension", "@source: EventName", "API action name"),
        Column("event_time", "datetime", "range", "@source: EventTime", "When the API call occurred"),
        Column("event_source", "string", "dimension", "@source: EventSource", "AWS service called"),
        Column("username", "string", "dimension", "@source: Username", "Identity that made the call"),
    ),
    proof_column="event_name",
)

AWS_CASES = {case.service: case for case in (CLOUDWATCH_LOGS, CLOUDTRAIL)}


def schema_path(case: AwsCase, name: str) -> str:
    """Where the authored schema lands.

    The run's own source name keeps it off the shipped ``meta/aws/<service>``,
    which already exists and which a create would be refused against.
    """
    return f"meta/aws/{case.stem}_{name}"


def schema_csv(case: AwsCase) -> str:
    """The column set as the console's CSV import takes it."""
    return "\n".join([CSV_HEADER, *(column.csv_row() for column in case.columns)]) + "\n"


def author_schema_in_console(driver, case: AwsCase, path: str, csv_file: Path, timeout_ms: int) -> str:
    """Meta Schemas > Add Schema: name it, upload the columns, review, create.

    The columns go in as the console's own DFE CSV import rather than row by row
    in the grid: it is the path an operator with a column list already in hand
    takes, and it is the one the form validates as a whole.
    """
    page = driver.page
    # The nav entry and the page's own tab share the label, so the address is
    # unambiguous where the link is not.
    page.goto(f"{driver.ui}/schemas/meta-schemas", wait_until="domcontentloaded", timeout=timeout_ms)
    page.get_by_role("button", name="Add Schema").first.click(timeout=timeout_ms)
    # The page carries a second, closed copy of this drawer for its empty state,
    # so every control is taken from the open one rather than from the document.
    form = page.locator(".ant-drawer-open")
    form.wait_for(state="visible", timeout=timeout_ms)
    parent, _, leaf = path.partition("/")[2].rpartition("/")
    form.get_by_placeholder("Enter path").first.fill(parent, timeout=timeout_ms)
    form.get_by_placeholder("Enter name").first.fill(leaf, timeout=timeout_ms)
    form.get_by_placeholder("Enter description").first.fill(case.summary, timeout=timeout_ms)
    form.locator("input[type=file]").first.set_input_files(str(csv_file), timeout=timeout_ms)
    # The uploaded tab appears only once the CSV parsed, so it is the proof the
    # columns were read before anything is submitted.
    form.get_by_role("tab", name="Uploaded Columns").wait_for(state="visible", timeout=timeout_ms)
    form.get_by_role("button", name="Review Schema").first.click(timeout=timeout_ms)
    form.get_by_role("button", name="Create Schema").first.click(timeout=timeout_ms)
    page.get_by_text("Schema created successfully").wait_for(timeout=timeout_ms)
    return f"the console authored {path} {SCHEMA_VERSION} with {len(case.columns)} columns"


def author_schema_by_api(engine: Engine, case: AwsCase, path: str) -> str:
    """The same schema through the engine, for when the console cannot author one."""
    body = {
        "current": SCHEMA_VERSION,
        "versions": {
            SCHEMA_VERSION: {
                "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "type": "model",
                "summary": case.summary,
                "columns": [column.api_column() for column in case.columns],
            }
        },
    }
    reply = engine.call("POST", f"/schemas/definitions/{path}", body)
    if reply.status not in (200, 201):
        raise RuntimeError(f"authoring {path} was refused: {reply.status} {reply.body}")
    return f"authored {path} {SCHEMA_VERSION} with {len(case.columns)} columns"


def remove_schema(engine: Engine, path: str) -> str:
    reply = engine.call("DELETE", f"/schemas/definitions/{path}")
    if reply.status not in (200, 204, 404):
        return f"{path} not removed: {reply.status} {reply.body}"
    return f"removed {path} (delete answered {reply.status})"


def survey_origin(driver, timeout_ms: int) -> str:
    """Open Add Source and record what the Origin section offers a fetched source.

    The console's fetcher control is the one thing this case cannot do through the
    browser, so the survey IS the finding: the form is opened, screenshotted and
    read for a fetcher control, and what it has instead is reported.
    """
    page = driver.page
    # By address rather than by the nav link: an earlier step may have left a
    # drawer open over it, and this survey's finding must not be that.
    page.goto(f"{driver.ui}/sources", wait_until="domcontentloaded", timeout=timeout_ms)
    driver.button("Add Source").first.click(timeout=timeout_ms)
    page.get_by_placeholder("Enter source").wait_for(state="visible", timeout=timeout_ms)
    offers_fetcher = page.get_by_text("Source Type", exact=True).count()
    match_fields = page.get_by_placeholder("Enter field").count()
    if offers_fetcher:
        return "Add Source offers a fetcher Source Type; the stanza still goes through the API"
    return (
        f"Add Source offers no fetcher control ({match_fields} receiver match field(s), "
        "no Source Type): a fetched source cannot be created from the console"
    )


def source_body(
    case: AwsCase,
    name: str,
    schema: str,
    region: str,
    log_group: str,
    poll_interval_secs: int,
) -> dict:
    """The source as the engine takes it: one fetcher stanza, no receiver match."""
    service: dict[str, object] = {"name": case.service}
    if "log_group_name" in case.service_config:
        service["config"] = {"log_group_name": log_group}
    return {
        "source": name,
        "display_name": f"AWS {case.service} source test {name}",
        "description": "Post-deploy source test: a fetched cloud source, archived.",
        "fetcher": {
            "source_type": "aws",
            "topic": "own",
            "config": {
                "region": region,
                # Names, not values: the source is committed to git, and the
                # deployment's fetcher credential Secret holds the two variables.
                "access_key_id": f"env:{ACCESS_KEY_ENV}",
                "secret_access_key": f"env:{SECRET_KEY_ENV}",
                "interval_secs": poll_interval_secs,
                "services": [service],
            },
        },
        "header": {"type": HEADER, "version": HEADER_VERSION},
        "schema": {"meta_schema": schema, "meta_schema_version": SCHEMA_VERSION},
        "archive": True,
    }


def create_source(engine: Engine, body: dict) -> str:
    reply = engine.call("POST", "/sources", body)
    if reply.status not in (200, 201):
        raise RuntimeError(f"the source was refused: {reply.status} {reply.body}")
    return f"created {body['source']} with an aws/{body['fetcher']['config']['services'][0]['name']} fetcher stanza"


def telemetry_name(engine: Engine, name: str) -> str:
    reply = engine.call("GET", f"/apps/{FETCHER_SERVICE}/{name}/status")
    return str((reply.body or {}).get("telemetry_name") or "")


def upstream_note(case: AwsCase, log_group: str) -> str:
    """What this case polls, named in the report so a dry upstream is not read as a DFE fault."""
    if case.service == "cloudwatch_logs":
        return f"aws/{case.service} on log group {log_group}"
    return f"aws/{case.service} on the account's own API activity"


def new_name(prefix: str = "cw") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"
