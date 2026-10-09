#  Project:      dfe-infra
#  File:         acceptance/source/beats.py
#  Purpose:      Stand a real filebeat and logstash pair beside a compose
#                deployment so a pushed case feeds the receiver the envelope a
#                deployment actually sends, and take the pair down again.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""source.beats -- the corpus through a Beats agent and Logstash, not a POST.

The POSTed cases send a reconstructed Elastic Agent envelope. The common path is
a Beats agent shipping lumberjack to Logstash and Logstash's http output posting
its whole event at the receiver, and the Elastic ingest pipelines
dfe-transform-elastic compiles in were written against that envelope.

This is a variation of the pushed cases rather than a case of its own: the same
corpus lines, the same source, the same proof, reaching the receiver the other
way. Compose only for now -- a Kubernetes run records the step as skipped,
because a Job is a second wiring and dfe-infra #326 asked for docker first.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# The Elastic line dfe-transform-elastic takes its compat ground truth from
# (its scripts/compat.py runs 9.2.2), so the envelope pushed here is the one the
# compiled pipelines were confirmed against.
ELASTIC_VERSION = "9.2.2"
FILEBEAT_IMAGE = (
    f"docker.elastic.co/beats/filebeat:{ELASTIC_VERSION}"
    "@sha256:43a8fc2b64051c8e2538ee0d0e1a79fc6e13625d1ee22bc0888c856c86aba863"
)
LOGSTASH_IMAGE = (
    f"docker.elastic.co/logstash/logstash:{ELASTIC_VERSION}"
    "@sha256:f05daf67296ff20e708f1971f78161d86dc37c577bb2dea922782099e6d732ab"
)
# Lumberjack's own port, which is what both ends default to.
BEATS_PORT = 5044
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
    """The two containers one run stands up, and where they send."""

    network: str
    receiver_url: str
    workdir: Path
    run_id: str

    @property
    def logstash(self) -> str:
        return f"dfe-src-logstash-{self.run_id}"

    @property
    def filebeat(self) -> str:
        return f"dfe-src-filebeat-{self.run_id}"


def docker(*argv: str, timeout: float = 180.0) -> tuple[int, str]:
    """One docker command, with its output whichever stream it chose."""
    done = subprocess.run(
        ["docker", *argv], capture_output=True, text=True, timeout=timeout, check=False
    )
    return done.returncode, (done.stdout or done.stderr).strip()


def write_inputs(pair: Pair, lines: list[str], dataset: str) -> Path:
    """The corpus on disk plus the two configs, under this run's own directory.

    Filebeat tails a file, so the corpus is written as a file rather than
    streamed: that is what a deployment's agent is pointed at.
    """
    corpus_dir = pair.workdir / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / CORPUS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")

    # filestream needs an id of its own, and it identifies a file by a fingerprint
    # of the first 1024 bytes by default -- a corpus slice shorter than that is
    # never ingested at all, so the fingerprint is cut to the smallest it allows.
    # The agent stamps the data stream the source matches, as an integration
    # policy does, and the run tag the stray search looks for.
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
        "            namespace: default\n"
        "            type: logs\n"
        "      - add_tags:\n"
        f'          tags: ["e2e_run:{pair.run_id}"]\n'
        "output.logstash:\n"
        f'  hosts: ["{pair.logstash}:{BEATS_PORT}"]\n'
        "logging.level: info\n",
        encoding="utf-8",
    )

    # Logstash adds nothing: the event it posts is the envelope filebeat built.
    (pair.workdir / "logstash.conf").write_text(
        "input {\n"
        f"  beats {{ port => {BEATS_PORT} }}\n"
        "}\n"
        "output {\n"
        "  http {\n"
        f'    url => "{pair.receiver_url}"\n'
        '    http_method => "post"\n'
        '    format => "json"\n'
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
            return True, f"logstash {ELASTIC_VERSION} listening on {BEATS_PORT}"
        time.sleep(5)
    _, logs = docker("logs", "--tail", "5", pair.logstash)
    return False, f"logstash never ran its pipeline within {LOGSTASH_READY_DEADLINE:.0f}s: {logs[-200:]}"


def start_filebeat(pair: Pair) -> tuple[bool, str]:
    """Filebeat tailing the corpus, shipping lumberjack to the logstash beside it.

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
    return True, f"filebeat {ELASTIC_VERSION} tailing {AGENT_PATH}"


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
    """Remove both containers, whether or not they ever ran."""
    removed = []
    for name in (pair.filebeat, pair.logstash):
        code, _ = docker("rm", "-f", name, timeout=60.0)
        removed.append(f"{name} {'removed' if code == 0 else 'was not there'}")
    return "; ".join(removed)


def envelope_evidence(store, table: str) -> tuple[int, list[str], str]:
    """How many landed rows came off the agent, and what one of them arrived with.

    Read back out of the datastore rather than asserted from the config here: the
    question dfe-infra #326 raises is which of the Logstash envelope the beats
    meta schema keeps, and the rows that landed are the answer to it. The agent's
    own file path is the tell -- the POSTed envelope carries no such field -- and
    a transform rewrites each record into its own
    shape, so it is counted rather than assumed of every row.
    """
    where = f"WHERE position(_raw, '{AGENT_PATH}') > 0"
    carrying = store.scalar(f"SELECT count() FROM {table} {where}")
    if not carrying:
        return 0, [], f"no row in {table} carries {AGENT_PATH}, so none of them came off the agent"
    rows = store.query(f"SELECT _raw FROM {table} {where} LIMIT 1")
    if not rows or not rows[0] or not rows[0][0]:
        return carrying, [], f"{table} would not hand back a row carrying {AGENT_PATH}"
    try:
        body = json.loads(rows[0][0])
    except (ValueError, json.JSONDecodeError) as exc:
        return carrying, [], f"the stored _raw is not JSON: {exc}"
    return carrying, sorted(body), ""
