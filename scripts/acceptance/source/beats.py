#  Project:      dfe-infra
#  File:         acceptance/source/beats.py
#  Purpose:      Stand a real filebeat and logstash pair beside a compose
#                deployment so a pushed case feeds the receiver the envelope a
#                deployment actually sends, and take the pair down again.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""source.beats -- the corpus through a real Beats agent, not a POST.

The POSTed cases send a reconstructed Elastic Agent envelope. A deployment gets
the agent's own, two ways: a Beats agent shipping lumberjack to Logstash and
Logstash's http output posting the event at the receiver (``--via logstash``),
or the agent shipping lumberjack straight to the receiver's own listener
(``--via beats``). The Elastic ingest pipelines dfe-transform-elastic compiles
in were written against that envelope.

This is a variation of the pushed cases rather than a case of its own: the same
corpus lines, the same source, the same proof, reaching the receiver the other
way. Compose only for now -- a Kubernetes run records the step as skipped,
because a Job is a second wiring and dfe-infra #326 asked for docker first.
"""

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from acceptance.clients import RECORD_TEXT

# The shippers a site runs now. The Elasticsearch release dfe-transform-elastic
# compiles its ingest pipelines from (9.2.2, its scripts/compat.py) is a separate
# pin and does not move with them.
SHIPPER_VERSION = "9.5.5"
FILEBEAT_IMAGE = (
    f"docker.elastic.co/beats/filebeat:{SHIPPER_VERSION}"
    "@sha256:4b2042fcbeb781800bc7271d400f764bb7d670310dfd4fe03ade53d6c7ab78aa"
)
LOGSTASH_IMAGE = (
    f"docker.elastic.co/logstash/logstash:{SHIPPER_VERSION}"
    "@sha256:d170c92166492ff82433395734121dcd2c73140038a03e252299384a01c9aae5"
)
# Lumberjack's own port, which is what both ends default to.
BEATS_PORT = 5044
# What filebeat writes as agent.type on every event it ships.
AGENT_TYPE = "filebeat"
# Not `default`, which dfe-transform-elastic writes where a record names none, so
# a stored row carries this only where the stamp survived.
DATA_STREAM_NAMESPACE = "acceptance"
# The receiver's switches a --via beats run turns on and puts back afterwards. An
# enabled listener refuses to start without client certificates unless told to
# accept every client that can reach the port.
RECEIVER_SERVICE = "dfe-receiver"
LUMBERJACK_SWITCHES = {
    "config.lumberjack.enabled": True,
    "config.lumberjack.accept_unauthenticated": True,
}
# The same switches as the receiver reads them from its environment, for a
# deployment whose engine refuses the write.
LUMBERJACK_ENV = (
    "DFE_RECEIVER_LUMBERJACK__ENABLED=true DFE_RECEIVER_LUMBERJACK__ACCEPT_UNAUTHENTICATED=true"
)
# Where the corpus is mounted and what the agent tails, which is the file path
# every event it ships carries.
CORPUS_MOUNT = "/corpus"
CORPUS_FILE = "cisco_ios.log"
AGENT_PATH = f"{CORPUS_MOUNT}/{CORPUS_FILE}"
# Logstash boots a JVM and compiles the pipeline before it listens.
LOGSTASH_READY_DEADLINE = 240.0
# The line Logstash prints once every pipeline is accepting.
LOGSTASH_READY_LINE = "Pipelines running"
# Filebeat ships a file it is already tailing within one scan interval.
SHIP_DEADLINE = 180.0
# The smallest fingerprint filestream accepts, against a corpus slice of a few
# hundred bytes.
FINGERPRINT_LENGTH = 64


@dataclass(frozen=True, slots=True)
class Pair:
    """The containers one run stands up, and where they send.

    ``lumberjack`` is the receiver's own listener, host:port, on a --via beats
    run. Empty means filebeat ships to this run's own logstash.
    """

    network: str
    receiver_url: str
    workdir: Path
    run_id: str
    lumberjack: str = ""

    @property
    def logstash(self) -> str:
        return f"dfe-src-logstash-{self.run_id}"

    @property
    def filebeat(self) -> str:
        return f"dfe-src-filebeat-{self.run_id}"

    @property
    def target(self) -> str:
        """Where filebeat's lumberjack output connects."""
        return self.lumberjack or f"{self.logstash}:{BEATS_PORT}"

    @property
    def containers(self) -> tuple[str, ...]:
        """The containers this run starts, so teardown removes nothing it never made."""
        return (self.filebeat,) if self.lumberjack else (self.filebeat, self.logstash)


def docker(*argv: str, timeout: float = 180.0) -> tuple[int, str]:
    """One docker command, with its output whichever stream it chose."""
    done = subprocess.run(
        ["docker", *argv], capture_output=True, text=True, timeout=timeout, check=False
    )
    return done.returncode, (done.stdout or done.stderr).strip()


def write_inputs(pair: Pair, lines: list[str], dataset: str, marker_field: str,
                 marker: dict[str, str]) -> Path:
    """The corpus on disk plus the two configs, under this run's own directory.

    Filebeat tails a file, so the corpus is written as a file rather than
    streamed: that is what a deployment's agent is pointed at.

    Args:
        pair: The containers this run stands up.
        lines: The corpus lines the agent ships.
        dataset: The data stream the source is matched on.
        marker_field: The root field the run's counts read.
        marker: What that field holds on every event the agent ships.

    Returns:
        The directory the corpus file is in.
    """
    corpus_dir = pair.workdir / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / CORPUS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    marker_lines = "".join(f'            {key}: "{value}"\n' for key, value in marker.items())

    # filestream needs an id of its own, and it identifies a file by a fingerprint
    # of the first 1024 bytes by default -- a corpus slice shorter than that is
    # never ingested at all, so the fingerprint is cut to the smallest it allows.
    # The operator's config stamps the data stream the source matches, as an
    # integration policy does, plus the run marker the counts read and the run tag.
    (pair.workdir / "filebeat.yml").write_text(
        "filebeat.inputs:\n"
        "  - type: filestream\n"
        f"    id: {pair.run_id}\n"
        "    paths:\n"
        f"      - {AGENT_PATH}\n"
        f"    prospector.scanner.fingerprint.length: {FINGERPRINT_LENGTH}\n"
        "    processors:\n"
        "      - add_fields:\n"
        "          target: data_stream\n"
        "          fields:\n"
        f"            dataset: {dataset}\n"
        f"            namespace: {DATA_STREAM_NAMESPACE}\n"
        "            type: logs\n"
        "      - add_fields:\n"
        f"          target: {marker_field}\n"
        "          fields:\n"
        f"{marker_lines}"
        "      - add_tags:\n"
        f'          tags: ["e2e_run:{pair.run_id}"]\n'
        "output.logstash:\n"
        f'  hosts: ["{pair.target}"]\n'
        "logging.level: info\n",
        encoding="utf-8",
    )
    if pair.lumberjack:
        return corpus_dir

    # Logstash adds nothing: the events it posts are the envelopes filebeat built,
    # a pipeline batch to one JSON array, which the receiver splits per element.
    (pair.workdir / "logstash.conf").write_text(
        "input {\n"
        f"  beats {{ port => {BEATS_PORT} }}\n"
        "}\n"
        "output {\n"
        "  http {\n"
        f'    url => "{pair.receiver_url}"\n'
        '    http_method => "post"\n'
        '    format => "json_batch"\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    return corpus_dir


def start_logstash(pair: Pair) -> tuple[bool, str]:
    """Logstash on the stack's network, waited for until its pipeline accepts."""
    code, out = docker(
        "run", "-d", "--name", pair.logstash, "--network", pair.network,
        "-v", f"{pair.workdir / 'logstash.conf'}:/usr/share/logstash/pipeline/logstash.conf:ro",
        "-e", "XPACK_MONITORING_ENABLED=false",
        "-e", "LS_JAVA_OPTS=-Xms256m -Xmx512m",
        LOGSTASH_IMAGE,
    )
    if code != 0:
        return False, f"logstash did not start: {out[:200]}"
    until = time.monotonic() + LOGSTASH_READY_DEADLINE
    while time.monotonic() < until:
        _, logs = docker("logs", pair.logstash)
        if LOGSTASH_READY_LINE in logs:
            return True, f"logstash {SHIPPER_VERSION} listening on {BEATS_PORT}"
        time.sleep(5)
    _, logs = docker("logs", "--tail", "5", pair.logstash)
    return False, f"logstash never ran its pipeline within {LOGSTASH_READY_DEADLINE:.0f}s: {logs[-200:]}"


def start_filebeat(pair: Pair) -> tuple[bool, str]:
    """Filebeat tailing the corpus, shipping lumberjack to the pair's target.

    ``--strict.perms=false`` because the config is mounted from the host and
    filebeat otherwise refuses a file its own uid does not own.
    """
    code, out = docker(
        "run", "-d", "--name", pair.filebeat, "--network", pair.network,
        "-v", f"{pair.workdir / 'filebeat.yml'}:/usr/share/filebeat/filebeat.yml:ro",
        "-v", f"{pair.workdir / 'corpus'}:{CORPUS_MOUNT}:ro",
        FILEBEAT_IMAGE, "filebeat", "-e", "--strict.perms=false",
    )
    if code != 0:
        return False, f"filebeat did not start: {out[:200]}"
    return True, f"filebeat {SHIPPER_VERSION} tailing {AGENT_PATH}, shipping to {pair.target}"


def published(pair: Pair, wanted: int, deadline: float = SHIP_DEADLINE) -> tuple[int, str]:
    """How many events filebeat says it published, polled until it has sent them all.

    Filebeat's own count, not the receiver's: it separates "the agent never
    shipped" from "the stack never took it", which are different findings.
    """
    until = time.monotonic() + deadline
    seen = 0
    while True:
        _, logs = docker("logs", pair.filebeat)
        seen = max(seen, _published_events(logs))
        if seen >= wanted:
            return seen, f"filebeat published {seen} of {wanted} lines"
        if time.monotonic() >= until:
            return seen, (
                f"filebeat published {seen} of {wanted} lines in {deadline:.0f}s"
                + (f", and it warned: {_last_warning(logs)}" if _last_warning(logs) else "")
            )
        time.sleep(5)


def _last_warning(logs: str) -> str:
    """Filebeat's last warn line, which is where it says why it shipped nothing."""
    for line in reversed(logs.splitlines()):
        try:
            entry = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if entry.get("log.level") == "warn":
            return str(entry.get("message") or "")[:200]
    return ""


def _published_events(logs: str) -> int:
    """The acked-event total out of filebeat's periodic metrics lines.

    Each snapshot reports the interval's delta, so a run's total is their sum.
    """
    total = 0
    for line in logs.splitlines():
        if "monitoring" not in line or "events" not in line:
            continue
        try:
            body = json.loads(line[line.index("{") :])
        except (ValueError, json.JSONDecodeError):
            continue
        events = (((body.get("monitoring") or {}).get("metrics") or {}).get("libbeat") or {})
        published = ((events.get("output") or {}).get("events") or {}).get("acked")
        if isinstance(published, int):
            total += published
    return total


def stop(pair: Pair) -> str:
    """Remove the containers this run starts, whether or not they ever ran."""
    removed = []
    for name in pair.containers:
        code, _ = docker("rm", "-f", name, timeout=60.0)
        removed.append(f"{name} {'removed' if code == 0 else 'was not there'}")
    return "; ".join(removed)


def _field(*path: str) -> str:
    """One string field of the stored record, read by path."""
    keys = ", ".join(f"'{key}'" for key in path)
    return f"JSONExtractString({RECORD_TEXT}, {keys})"


# The rows that came off the agent, by the field rather than a substring: the
# JSON text escapes every / in the path.
_AGENT_ROWS = f"WHERE {_field('log', 'file', 'path')} = '{AGENT_PATH}'"
# Both fields as the agent's config sent them.
_KEPT = (
    f"{_field('agent', 'type')} = '{AGENT_TYPE}' "
    f"AND {_field('data_stream', 'namespace')} = '{DATA_STREAM_NAMESPACE}'"
)


def envelope_evidence(store, table: str) -> tuple[int, list[str], str]:
    """How many landed rows came off the agent, and what one of them arrived with.

    Read back out of the datastore rather than asserted from the config here: the
    question dfe-infra #326 raises is which of the Logstash envelope the beats
    meta schema keeps, and the rows that landed are the answer to it. The agent's
    own file path is the tell -- the POSTed envelope carries no such field -- and
    a transform rewrites each record into its own
    shape, so it is counted rather than assumed of every row.
    """
    where = _AGENT_ROWS
    carrying = store.scalar(f"SELECT count() FROM {table} {where}")
    if not carrying:
        return 0, [], f"no row in {table} carries {AGENT_PATH}, so none of them came off the agent"
    rows = store.query(f"SELECT {RECORD_TEXT} FROM {table} {where} LIMIT 1")
    if not rows or not rows[0] or not rows[0][0]:
        return carrying, [], f"{table} would not hand back a row carrying {AGENT_PATH}"
    try:
        body = json.loads(rows[0][0])
    except (ValueError, json.JSONDecodeError) as exc:
        return carrying, [], f"the stored record is not JSON: {exc}"
    return carrying, sorted(body), ""


def kept_outcome(store, table: str) -> tuple[str, str]:
    """Whether every row off the agent kept its agent.type and data stream namespace.

    Elasticsearch stores both as they arrived, and the beats meta schema makes
    a column of agent.type, so a transform writing its own name over it fills
    that column wrongly.

    Args:
        store: The datastore.
        table: The source's own table.

    Returns:
        The status and detail for ``Driver.record``.
    """
    carrying = store.scalar(f"SELECT count() FROM {table} {_AGENT_ROWS}")
    if not carrying:
        return "failed", f"no row in {table} carries {AGENT_PATH}, so none of them came off the agent"
    kept = store.scalar(f"SELECT count() FROM {table} {_AGENT_ROWS} AND {_KEPT}")
    detail = (
        f"{kept} of {carrying} agent row(s) kept agent.type {AGENT_TYPE} (filebeat's own) and "
        f"data_stream.namespace {DATA_STREAM_NAMESPACE} (operator-stamped with add_fields)"
    )
    if kept == carrying:
        return "done", detail
    rows = store.query(
        f"SELECT {_field('agent', 'type')}, {_field('data_stream', 'namespace')} "
        f"FROM {table} {_AGENT_ROWS} AND NOT ({_KEPT}) LIMIT 1"
    )
    if rows and len(rows[0]) == 2:
        detail += f". One landed with agent.type {rows[0][0]!r} and data_stream.namespace {rows[0][1]!r}"
    return "failed", detail


def held(config: object, paths) -> dict[str, object]:
    """The values an instance's overlay already sets at *paths*, read off its config route.

    A path the overlay does not set is absent from the result, so putting the
    instance back means clearing it rather than writing a default over it.

    Args:
        config: The engine's ``GET /apps/<service>/<instance>/config`` body.
        paths: The config paths asked about.

    Returns:
        Each set path and its value.
    """
    wanted = set(paths)
    found: dict[str, object] = {}
    if not isinstance(config, dict):
        return found
    for field in config.get("fields") or []:
        if isinstance(field, dict) and field.get("path") in wanted and field.get("is_set"):
            found[field["path"]] = field.get("value")
    # A path the app's contract does not declare is reported here once it is set.
    for field in config.get("unknown") or []:
        if isinstance(field, dict) and field.get("path") in wanted:
            found[field["path"]] = field.get("value")
    return found


def restore_plan(before: dict[str, object], paths) -> tuple[dict[str, object], list[str]]:
    """The writes that put *paths* back as *before* found them.

    Returns:
        The values to write back, and the paths to clear to the app's default.
    """
    return dict(before), [path for path in paths if path not in before]
