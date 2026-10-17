#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/dfe_ops_diagnostics.py
#  Purpose:      `dfe-ops diagnostics` -- the app logs, workload state, Argo status
#                and Kafka offsets a failed cycle stage leaves, written
#                to a bundle before anything is torn down. Registered into dfe-ops
#                the way dfe_ops_failure_evidence.py is.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe-ops diagnostics -- what the DFE apps were doing when a stage failed.

    dfe-ops diagnostics --dir DIR --label LABEL [--kubeconfig F] [--context C] [--server S]
                        [--log-minutes 30] [--budget 300]

`dfe-ops cycle --diagnostics-dir DIR` runs it when any stage from deploy+e2e on fails,
before the destroy. The source acceptance runner runs it before it removes its source,
which takes that source's own transform with it. Each run writes DIR/<UTC time>-<label>/:

  workloads.txt              kubectl get pods,deploy,rs -A -o wide
  events/<namespace>.txt     every event in a DFE namespace, oldest first, the newest kept
                             when they pass the byte cap
  argo-applications.txt      each Argo Application's ApplicationSet, destination, sync,
                             revision, health, last operation, conditions, and every
                             resource not Synced and Healthy
  logs/<namespace>/<pod>.<container>.log
                             the last --log-minutes of each container of dfe-receiver,
                             dfe-loader, dfe-engine, dfe-hunt-runner and every
                             dfe-transform-* deployment, the per-source ones included
  logs/<namespace>/<pod>.<container>.previous.log
                             the run before, for a container that restarted
  kafka/engine.txt           the bootstrap address, security protocol and SASL Secret name
                             dfe-engine's Deployment sets, and the ConfigMaps it loads
  kafka/configmap-<name>.txt the bootstrap address and security protocol each of those
                             ConfigMaps sets
  kafka/client-pod.json      a redacted copy of the client pod and NetworkPolicy applied
  kafka/consumer-groups.txt  kafka-consumer-groups.sh --describe --all-groups
  kafka/end-offsets.txt      the end offset of every _land, _load and _dlq topic
  kafka/summary.txt          per topic: its end offset, and each group's committed offset
                             and lag, or that no group has one
  index.txt                  every capture, the command behind it and how it ended

A DFE namespace is one an Argo Application deploys into, one holding a logged app, or
argocd.

The Kafka reads run from a short-lived pod beside dfe-engine, the way the integration
smoke test reaches a managed broker: the kafka chart's own apache/kafka image, the
engine's bootstrap address and security protocol, and the engine's SASL Secret mounted as
files, so no credential crosses kubectl. The engine's Deployment and the ConfigMaps it
loads are read through jsonpaths naming those entries and no other. The pod and its
egress NetworkPolicy are the only objects this writes. They are deleted before it
returns, and the pod stops at activeDeadlineSeconds if the delete is lost. A deployment
with no Kafka bus has neither.

Every file passes through the failure-evidence `redact` before it is written, index.txt
included. The run stops at --budget seconds, every command inside it is bounded, and a
capture that fails or raises is one line in index.txt while the rest still run. It never
reads a Secret, and it exits 0 whatever it finds.
"""

import argparse
import contextlib
import json
import os
import shlex
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import dfe_ops_failure_evidence as fe
import yaml_subset
from dfe_ops_readiness_report import kubectl_prefix

REPO_ROOT = Path(__file__).resolve().parent.parent
KAFKA_VALUES = REPO_ROOT / "helm" / "charts" / "kafka" / "values.yaml"

BUDGET = 300.0  # seconds for the whole bundle, so the teardown behind it is not held up
CLEANUP_RESERVE = 20.0  # seconds of the budget kept back for the client pod's delete
LOG_MINUTES = 30
LOG_BYTES = 2 * 1024 * 1024  # newest bytes kept from one container's log
LOG_TOTAL = 64 * 1024 * 1024  # bytes every log of a bundle may keep together
LOG_TIMEOUT = 20.0  # seconds one log read may take; a pod on a dead node answers nothing
PREVIOUS_TAIL = 2000  # lines of a restarted container's previous run
MAX_LOGGED_PODS = 60
MAX_EVENT_NAMESPACES = 20
MAX_RESOURCES = 20  # resources not Synced and Healthy named per Application
# Share of the budget the log reads leave for the Kafka reads after them.
KAFKA_SHARE = 0.35
# Deployments whose logs are read: each name, and every `<name>-<suffix>` beside it.
LOGGED_APPS = ("dfe-receiver", "dfe-loader", "dfe-engine", "dfe-hunt-runner", "dfe-transform")
ENGINE = "dfe-engine"
ARGOCD = "argocd"
APPLICATIONS = "applications.argoproj.io"

# The engine's bus settings, read by name so no other entry of its environment is asked
# for. The chart puts the bootstrap list and protocol in a ConfigMap its envFrom loads and
# the SASL Secret in env, and an inline env value outranks the ConfigMap's, so both are read.
_ENV = 'spec.template.spec.containers[*].env[?(@.name=="{}")]'
_NEWLINE = '{"\\n"}'
ENGINE_KAFKA = (
    f"bootstrap={{.{_ENV.format('DFE_KAFKA_BOOTSTRAP_SERVERS')}.value}}{_NEWLINE}"
    f"protocol={{.{_ENV.format('DFE_KAFKA_SECURITY_PROTOCOL')}.value}}{_NEWLINE}"
    f"secret={{.{_ENV.format('DFE_KAFKA_SASL_MECHANISM')}.valueFrom.secretKeyRef.name}}{_NEWLINE}"
    f"configmaps={{.spec.template.spec.containers[*].envFrom[*].configMapRef.name}}{_NEWLINE}"
)
CONFIGMAP_KAFKA = (
    f"bootstrap={{.data.DFE_KAFKA_BOOTSTRAP_SERVERS}}{_NEWLINE}"
    f"protocol={{.data.DFE_KAFKA_SECURITY_PROTOCOL}}{_NEWLINE}"
)
MAX_ENGINE_CONFIGMAPS = 5
KAFKA_KEYS = ("bootstrap", "protocol", "secret")
KAFKA_CLI_IMAGE_ENV = "DFE_KAFKA_CLI_IMAGE"
CLIENT_NAME = "dfe-diagnostics"
CLIENT_MANIFEST = "kafka/client-pod.json"
CLIENT_LIFETIME = 600  # seconds before the client pod stops itself
RUN_LABEL = "dfe.hyperi.io/diagnostics-run"
CREDENTIAL_MOUNT = "/etc/dfe-kafka"
# The client container's own emptyDir, the one writable path under its read-only root.
CLIENT_TMP = "/tmp"  # noqa: S108
KAFKA_READY_TIMEOUT = 120.0  # seconds for the client pod's image pull and start
KAFKA_EXEC_TIMEOUT = 90.0
KAFKA_BIN = "/opt/kafka/bin"
# --timeout caps the wait on a group still rebalancing, which a crash-looping consumer keeps doing.
GROUPS_COMMAND = "kafka-consumer-groups.sh --describe --all-groups --timeout 10000"
# kafka-get-offsets.sh reads --topic as a regular expression over every topic.
OFFSETS_COMMAND = "kafka-get-offsets.sh --topic '.*_(land|load|dlq)'"
TOPIC_SUFFIXES = ("_land", "_load", "_dlq")
# The Kafka clients' own waits, so an unreachable broker ends a read before its box does.
CLIENT_TIMEOUTS = ("request.timeout.ms=20000", "default.api.timeout.ms=30000")
# Built inside the client pod from the mounted Secret, the integration smoke test's way.
SASL_LINES = rf"""  M=$(cat {CREDENTIAL_MOUNT}/sasl.mechanism)
  L=org.apache.kafka.common.security.scram.ScramLoginModule
  [ "$M" = PLAIN ] && L=org.apache.kafka.common.security.plain.PlainLoginModule
  printf 'sasl.mechanism=%s\n' "$M"
  printf 'sasl.jaas.config=%s required username="%s" password="%s";\n' "$L" \
    "$(sed 's/[\\"]/\\&/g' {CREDENTIAL_MOUNT}/username)" \
    "$(sed 's/[\\"]/\\&/g' {CREDENTIAL_MOUNT}/password)"
"""


# --- the bundle ------------------------------------------------------------------------


@dataclass(slots=True)
class Bundle:
    """One bundle's directory, kubectl prefix and clock. Every byte it writes is redacted.

    Captures stop at `deadline`. The client pod's delete runs after them and is done by
    `end`, the budget's own end.
    """

    out: Path
    prefix: list[str]
    budget: float
    deadline: float
    end: float
    runs: int = 0
    answered: int = 0
    files: set[str] = field(default_factory=set)
    log_left: int = LOG_TOTAL

    def note(self, name: str, how: str, args: Iterable[str] = ()) -> None:
        """One index entry: where a capture went, how it ended and the command behind it."""
        command = " ".join(args)
        entry = f"{name}: {how}\n" + (f"    kubectl {command}\n" if command else "")
        with (self.out / fe.INDEX).open("a", encoding="utf-8", newline="\n") as index:
            index.write(fe.redact(entry))

    def write(self, name: str, data: bytes) -> None:
        path = self.out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fe.redact_bytes(data))
        self.files.add(name)

    def kubectl(
        self,
        args: list[str],
        *,
        limit: int = fe.TEXT_BYTES,
        tail: bool = False,
        timeout: float = fe.CAPTURE_TIMEOUT,
        keep: float = 0.0,
    ) -> fe.Outcome | None:
        """Run kubectl, or None when no more than `keep` seconds of the captures' time are left."""
        left = self.deadline - time.monotonic() - keep
        if left <= 0:
            return None
        self.runs += 1
        argv = ["kubectl", *self.prefix, *args]
        outcome = fe.run_bounded(argv, timeout=min(timeout, left), limit=limit, tail=tail)
        if outcome.ok:
            self.answered += 1
        return outcome

    def capture(
        self,
        name: str,
        args: list[str],
        *,
        shown: list[str] | None = None,
        limit: int = fe.TEXT_BYTES,
        tail: bool = False,
        timeout: float = fe.CAPTURE_TIMEOUT,
        keep: float = 0.0,
    ) -> fe.Outcome | None:
        """`kubectl <args>` into the file `name`. What it kept is written even when it failed.

        `shown` is the command the index names, for one whose argv carries a script.
        """
        outcome = self.kubectl(args, limit=limit, tail=tail, timeout=timeout, keep=keep)
        if outcome and outcome.output:
            self.write(name, outcome.output)
        self.note(name, self.verdict(outcome, keep), args if shown is None else shown)
        return outcome

    def step(
        self, label: str, args: list[str], *, timeout: float = fe.CAPTURE_TIMEOUT
    ) -> fe.Outcome | None:
        """`kubectl <args>` for its effect: one index line, no file."""
        outcome = self.kubectl(args, timeout=timeout)
        self.note(label, fe.verdict(outcome, self.budget), args)
        return outcome

    def items(self, name: str, args: list[str]) -> list[dict] | None:
        """`kubectl <args> -o json` as its items, or None when it did not answer with them.

        The answer is held in memory and never written, so only the fields a caller picks
        reach a file.
        """
        full = [*args, "-o", "json"]
        outcome = self.kubectl(full, limit=fe.JSON_BYTES)
        how = fe.verdict(outcome, self.budget)
        found = None
        if outcome and outcome.truncated:
            how = f"answered with more than {fe.JSON_BYTES} bytes, so it was not parsed"
        elif outcome and outcome.ok:
            try:
                body = json.loads(outcome.output)
                found = [item for item in body.get("items") or [] if isinstance(item, dict)]
            except (json.JSONDecodeError, AttributeError):
                how = "answered with no JSON item list"
        self.note(name, how, full)
        return found

    def verdict(self, outcome: fe.Outcome | None, keep: float) -> str:
        """One index line on how a capture ended, an empty answer and a kept-back skip named."""
        if outcome is None and keep and time.monotonic() < self.deadline:
            return "not run: the time left is kept for the Kafka reads"
        if outcome and outcome.ok and not outcome.output:
            return "empty (0 bytes), no file written"
        return fe.verdict(outcome, self.budget)


def guarded[T](bundle: Bundle, label: str, run: Callable[[], T]) -> T | None:
    """`run()`, or None when it raised: the exception is one index line and the bundle goes on."""
    try:
        return run()
    except Exception as exc:  # one capture failing must never stop the rest, or what follows
        bundle.note(label, f"raised {type(exc).__name__}: {exc}")
        return None


# --- what to read, chosen off the cluster's own answers ---------------------------------


def owning_workload(pod: dict) -> str:
    """The Deployment, StatefulSet or DaemonSet a pod belongs to, or empty for a bare pod."""
    meta = pod.get("metadata") or {}
    refs = [ref for ref in meta.get("ownerReferences") or [] if isinstance(ref, dict)]
    owner = next((ref for ref in refs if ref.get("controller")), refs[0] if refs else {})
    name = str(owner.get("name") or "")
    if owner.get("kind") != "ReplicaSet":
        return name
    # A Deployment names each ReplicaSet `<deployment>-<pod-template-hash>`.
    template_hash = (meta.get("labels") or {}).get("pod-template-hash") or ""
    if template_hash and name.endswith(f"-{template_hash}"):
        return name.removesuffix(f"-{template_hash}")
    return name.rpartition("-")[0] or name


def logged(workload: str) -> bool:
    """Whether a workload is one whose logs a bundle reads."""
    return any(workload == app or workload.startswith(f"{app}-") for app in LOGGED_APPS)


def _identity(item: dict) -> tuple[str, str]:
    meta = item.get("metadata") or {}
    return str(meta.get("namespace") or ""), str(meta.get("name") or "")


def _by_workload(pod: dict) -> tuple[str, str, str]:
    namespace, name = _identity(pod)
    return namespace, owning_workload(pod), name


def log_targets(pods: list[dict]) -> list[dict]:
    """The pods of every logged app, by namespace, workload and name."""
    chosen = [pod for pod in pods if all(_identity(pod)) and logged(owning_workload(pod))]
    return sorted(chosen, key=_by_workload)


def engine_namespace(pods: list[dict]) -> str:
    """The namespace dfe-engine runs in, or empty when no pod of it is listed."""
    return next((_identity(pod)[0] for pod in pods if owning_workload(pod) == ENGINE), "")


def dfe_namespaces(apps: list[dict], targets: list[dict]) -> list[str]:
    """argocd, every namespace an Application deploys into, and every logged app's namespace."""
    found = {ARGOCD, *(_identity(pod)[0] for pod in targets)}
    for app in apps:
        destination = ((app.get("spec") or {}).get("destination") or {}).get("namespace")
        if destination:
            found.add(str(destination))
    return sorted(namespace for namespace in found if namespace)


def _text(value: object) -> str:
    """One line from something the cluster reported: runs of whitespace become one space."""
    return " ".join(str(value).split())


def _appset(app: dict) -> str:
    refs = (app.get("metadata") or {}).get("ownerReferences") or []
    return next((str(r.get("name")) for r in refs if r.get("kind") == "ApplicationSet"), "-")


def _by_appset(app: dict) -> tuple[str, str]:
    return _appset(app), _identity(app)[1]


def application_lines(apps: list[dict]) -> list[str]:
    """Each Application's appset, destination, sync, health and last operation, as text.

    Conditions, and each resource not Synced and Healthy, follow on lines of their own.
    """
    lines: list[str] = []
    for app in sorted(apps, key=_by_appset):
        spec, status = app.get("spec") or {}, app.get("status") or {}
        sync, health = status.get("sync") or {}, status.get("health") or {}
        revision = sync.get("revision") or ",".join(sync.get("revisions") or []) or "-"
        destination = (spec.get("destination") or {}).get("namespace") or "-"
        why = f": {_text(health['message'])}" if health.get("message") else ""
        lines.append(
            f"application {_identity(app)[1]} appset {_appset(app)} destination {destination} "
            f"sync {sync.get('status') or '-'} revision {revision} "
            f"health {health.get('status') or '-'}{why}"
        )
        operation = status.get("operationState") or {}
        if operation:
            said = f": {_text(operation['message'])}" if operation.get("message") else ""
            lines.append(
                f"  last operation {operation.get('phase') or '-'} started "
                f"{operation.get('startedAt') or '-'} finished {operation.get('finishedAt') or '-'}"
                f"{said}"
            )
        for cond in status.get("conditions") or []:
            lines.append(
                f"  condition {cond.get('type')}: {_text(cond.get('message') or '')} "
                f"(since {cond.get('lastTransitionTime') or '-'})"
            )
        lines += _resource_lines(status.get("resources") or [])
    return lines


def _resource_lines(resources: list[dict]) -> list[str]:
    """Each resource an Application owns that is not Synced and Healthy, up to MAX_RESOURCES."""
    lines = []
    for resource in resources:
        health = resource.get("health") or {}
        if resource.get("status") in (None, "Synced") and health.get("status") in (None, "Healthy"):
            continue
        why = f": {_text(health['message'])}" if health.get("message") else ""
        lines.append(
            f"  resource {resource.get('kind')} {resource.get('namespace') or '-'}/"
            f"{resource.get('name')} sync {resource.get('status') or '-'} "
            f"health {health.get('status') or '-'}{why}"
        )
    if len(lines) > MAX_RESOURCES:
        return [*lines[:MAX_RESOURCES], f"  +{len(lines) - MAX_RESOURCES} more resources"]
    return lines


# --- the Kafka reads -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KafkaFacts:
    """How dfe-engine reaches the bus, read off its Deployment and never off a Secret."""

    namespace: str
    bootstrap: str
    protocol: str
    # The Secret holding the SASL mechanism, username and password, by name. Empty: no SASL.
    secret: str


def settings(text: str) -> dict[str, list[str]]:
    """The `key=words` lines an ENGINE_KAFKA or CONFIGMAP_KAFKA read prints, word by word."""
    found: dict[str, list[str]] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            found[key.strip()] = value.split()
    return found


def kafka_facts(namespace: str, engine: str, configmaps: Iterable[str] = ()) -> KafkaFacts | None:
    """The bus dfe-engine dials, or None when it dials none.

    Args:
        namespace: Where the engine runs.
        engine: Its ENGINE_KAFKA read.
        configmaps: The CONFIGMAP_KAFKA read of each ConfigMap its envFrom loads, in that
            order. A later one outranks an earlier one, and the engine's own env both.
    """
    merged: dict[str, str] = {}
    for text in [*configmaps, engine]:
        read = settings(text)
        merged |= {key: read[key][0] for key in KAFKA_KEYS if read.get(key)}
    bootstrap = merged.get("bootstrap", "")
    if not bootstrap:
        return None
    secret = merged.get("secret", "")
    # The chart sets the protocol only beside SASL, and defaults it to SASL_PLAINTEXT.
    protocol = merged.get("protocol") or ("SASL_PLAINTEXT" if secret else "PLAINTEXT")
    return KafkaFacts(namespace, bootstrap, protocol, secret)


def bootstrap_ports(bootstrap: str) -> list[int]:
    """Every distinct port the bootstrap list names, which the client's egress has to open."""
    ports = set()
    for entry in bootstrap.split(","):
        _host, sep, port = entry.strip().rpartition(":")
        if sep and port.isdigit():
            ports.add(int(port))
    return sorted(ports)


def kafka_image(env: Mapping[str, str], values: str) -> str:
    """DFE_KAFKA_CLI_IMAGE, else the kafka chart's own apache/kafka pin, else empty."""
    if env.get(KAFKA_CLI_IMAGE_ENV):
        return env[KAFKA_CLI_IMAGE_ENV]
    version = yaml_subset.value_at(values, ("kafka", "version"))
    digest = yaml_subset.value_at(values, ("kafka", "imageDigest"))
    return f"apache/kafka:{version}@{digest}" if version and digest else ""


def client_manifest(
    pod: str, run_id: str, facts: KafkaFacts, image: str, ports: list[int]
) -> dict:
    """The client pod and its egress NetworkPolicy, as one List kubectl applies.

    The pod mounts the engine's SASL Secret by name, so its values stay in the cluster.
    """
    labels = {
        "app.kubernetes.io/name": CLIENT_NAME,
        "app.kubernetes.io/part-of": "dfe",
        RUN_LABEL: run_id,
    }
    metadata = {"name": pod, "namespace": facts.namespace, "labels": labels}
    mounts: list[dict] = [{"name": "tmp", "mountPath": CLIENT_TMP}]
    volumes: list[dict] = [{"name": "tmp", "emptyDir": {}}]
    if facts.secret:
        mounts.append({"name": "credential", "mountPath": CREDENTIAL_MOUNT, "readOnly": True})
        secret = {"secretName": facts.secret, "defaultMode": 0o440}
        volumes.append({"name": "credential", "secret": secret})
    container = {
        "name": "kafka",
        "image": image,
        "command": ["sleep", str(CLIENT_LIFETIME)],
        "env": [{"name": "LOG_DIR", "value": CLIENT_TMP}],
        "resources": {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"memory": "768Mi"},
        },
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
        "volumeMounts": mounts,
    }
    client = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": metadata,
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 1,
            "activeDeadlineSeconds": CLIENT_LIFETIME,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "fsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [container],
            "volumes": volumes,
        },
    }
    # The app namespace's baseline withholds the broker ports, as it does from the smoke client.
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": metadata,
        "spec": {
            "podSelector": {"matchLabels": {RUN_LABEL: run_id}},
            "policyTypes": ["Egress"],
            "egress": [{"ports": [{"port": port, "protocol": "TCP"} for port in ports]}],
        },
    }
    return {"apiVersion": "v1", "kind": "List", "items": [client, policy]}


def client_script(facts: KafkaFacts, command: str) -> str:
    """The shell the client pod runs: a private client config, one Kafka tool, then cleanup.

    Every value from the cluster is shell-quoted, and the SASL lines read the mounted
    Secret inside the pod into a file only the pod user can read.
    """
    config = [f"security.protocol={facts.protocol}", *CLIENT_TIMEOUTS]
    tool, _, rest = command.partition(" ")
    lines = [
        "P=$(umask 077; mktemp)",
        "trap 'rm -f \"$P\"' EXIT",
        "{",
        *(f"  echo {shlex.quote(line)}" for line in config),
        *([SASL_LINES.rstrip("\n")] if facts.secret else []),
        '} > "$P"',
        f"{KAFKA_BIN}/{tool} --bootstrap-server {shlex.quote(facts.bootstrap)} "
        f'--command-config "$P" {rest}',
        "rc=$?",
        'rm -f "$P"',
        "exit $rc",
    ]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class KafkaClient:
    """The client pod one bundle applied, whether kubectl accepted it, and the file it read."""

    facts: KafkaFacts
    pod: str
    run_id: str
    started: bool
    # The unredacted manifest outside the bundle, removed with the pod.
    manifest: Path


def start_kafka_client(bundle: Bundle, namespace: str, image: str) -> KafkaClient | None:
    """Apply the client pod beside dfe-engine, so its image pulls while the other reads run.

    Returns:
        The client, or None when nothing was applied: no engine, no bus, no port, no image.
        A failed apply still returns one, since it may have made half the List.
    """
    if not namespace:
        bundle.note("kafka/", "not run: no dfe-engine pod, so the bus it dials is unknown")
        return None
    read = ["-n", namespace, "get", "deploy", ENGINE, "-o", f"jsonpath={ENGINE_KAFKA}"]
    outcome = bundle.capture("kafka/engine.txt", read)
    if outcome is None or not outcome.ok:
        bundle.note("kafka/", "not run: dfe-engine's Kafka settings were not read")
        return None
    engine = outcome.output.decode("utf-8", errors="replace")
    loaded = []
    for name in settings(engine).get("configmaps", [])[:MAX_ENGINE_CONFIGMAPS]:
        args = ["-n", namespace, "get", "configmap", name, "-o", f"jsonpath={CONFIGMAP_KAFKA}"]
        answer = bundle.capture(f"kafka/configmap-{fe.safe_name(name)}.txt", args)
        if answer and answer.ok:
            loaded.append(answer.output.decode("utf-8", errors="replace"))
    facts = kafka_facts(namespace, engine, loaded)
    if facts is None:
        bundle.note("kafka/", "not run: dfe-engine dials no Kafka bus on this deployment")
        return None
    ports = bootstrap_ports(facts.bootstrap)
    if not ports:
        bundle.note("kafka/", f"not run: the bootstrap list {facts.bootstrap} names no port")
        return None
    if not image:
        why = f"not run: no client image, from {KAFKA_CLI_IMAGE_ENV} or the kafka chart's pin"
        bundle.note("kafka/", why)
        return None
    run_id = uuid.uuid4().hex[:8]
    pod = f"{CLIENT_NAME}-kafka-{run_id}"
    raw = (json.dumps(client_manifest(pod, run_id, facts, image, ports), indent=2) + "\n").encode()
    # Redaction mangles the JSON (`automountServiceAccountToken`), so kubectl reads its own copy.
    bundle.write(CLIENT_MANIFEST, raw)
    descriptor, name = tempfile.mkstemp(prefix=f"{CLIENT_NAME}-", suffix=".json")
    manifest = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
        applied = bundle.step(f"{CLIENT_MANIFEST}, applied", ["apply", "-f", str(manifest)])
    except BaseException:
        manifest.unlink(missing_ok=True)
        raise
    if applied is None:
        manifest.unlink(missing_ok=True)
        return None
    return KafkaClient(facts, pod, run_id, applied.ok, manifest)


def _exec_args(client: KafkaClient, command: str) -> tuple[list[str], list[str]]:
    """The exec argv for one Kafka tool, and the shorter form the index names it by."""
    head = ["-n", client.facts.namespace, "exec", client.pod, "-c", "kafka", "--", "sh", "-c"]
    return [*head, client_script(client.facts, command)], [*head, f"<client script: {command}>"]


def read_kafka(bundle: Bundle, client: KafkaClient) -> None:
    """Wait for the client pod, then read every group's offsets and the topics' end offsets."""
    wait = [
        "-n", client.facts.namespace, "wait", "--for=condition=Ready", f"pod/{client.pod}",
        f"--timeout={int(KAFKA_READY_TIMEOUT)}s",
    ]
    ready = bundle.step("(the client pod, waited for)", wait, timeout=KAFKA_READY_TIMEOUT + 10)
    if ready is None or not ready.ok:
        bundle.note("kafka/consumer-groups.txt", "not run: the client pod never became Ready")
        return
    answers = []
    for name, command in (
        ("kafka/consumer-groups.txt", GROUPS_COMMAND),
        ("kafka/end-offsets.txt", OFFSETS_COMMAND),
    ):
        argv, shown = _exec_args(client, command)
        outcome = bundle.capture(name, argv, shown=shown, timeout=KAFKA_EXEC_TIMEOUT)
        answers.append(outcome.output.decode("utf-8", errors="replace") if outcome else "")
    summary = "\n".join(kafka_summary_lines(*answers)) + "\n"
    bundle.write("kafka/summary.txt", summary.encode("utf-8"))
    bundle.note("kafka/summary.txt", "from the two reads above")


def stop_kafka_client(bundle: Bundle, client: KafkaClient) -> None:
    """Delete the client pod and its NetworkPolicy, then the manifest kubectl applied.

    The delete runs in the time CLEANUP_RESERVE kept back.
    """
    args = [
        "-n", client.facts.namespace, "delete", "pod,networkpolicy", "-l",
        f"{RUN_LABEL}={client.run_id}", "--ignore-not-found", "--wait=false",
    ]
    timeout = min(CLEANUP_RESERVE, max(1.0, bundle.end - time.monotonic()))
    try:
        bundle.runs += 1
        argv = ["kubectl", *bundle.prefix, *args]
        outcome = fe.run_bounded(argv, timeout=timeout, limit=fe.STDERR_BYTES)
        if outcome.ok:
            bundle.answered += 1
        deleted = "(the client pod and its NetworkPolicy, deleted)"
        bundle.note(deleted, fe.verdict(outcome, bundle.budget), args)
    finally:
        client.manifest.unlink(missing_ok=True)


def kafka_summary_lines(groups: str, offsets: str) -> list[str]:
    """Per _land, _load and _dlq topic: its end offset, and each group's commit and lag.

    Read off kafka-consumer-groups.sh (GROUP TOPIC PARTITION CURRENT-OFFSET LOG-END-OFFSET
    LAG ...) and kafka-get-offsets.sh (topic:partition:offset). A group with no committed
    offset prints `-`, and a group that never committed is not printed at all.
    """
    ends: dict[str, int] = {}
    for line in offsets.splitlines():
        topic, _, rest = line.strip().partition(":")
        _partition, _, offset = rest.partition(":")
        if topic and offset.isdigit():
            ends[topic] = ends.get(topic, 0) + int(offset)
    committed: dict[str, dict[str, list[int | None]]] = {}
    for line in groups.splitlines():
        parts = line.split()
        if len(parts) < 6 or not parts[2].isdigit():
            continue
        sums = committed.setdefault(parts[1], {}).setdefault(parts[0], [None, None])
        for slot, raw in enumerate((parts[3], parts[5])):
            if raw.isdigit():
                sums[slot] = (sums[slot] or 0) + int(raw)
    topics = sorted(topic for topic in {*ends, *committed} if topic.endswith(TOPIC_SUFFIXES))
    if not topics:
        return ["neither Kafka read answered with a row"]
    lines: list[str] = []
    for topic in topics:
        end = ends.get(topic)
        lines.append(f"{topic} end offset {'-' if end is None else end}")
        by_group = committed.get(topic) or {}
        if not by_group:
            lines.append("  no consumer group has an offset on it")
        for group, (done, lag) in sorted(by_group.items()):
            if done is None:
                lines.append(f"  group {group}: no offset committed")
            else:
                shown = "-" if lag is None else lag
                lines.append(f"  group {group}: committed {done}, lag {shown}")
    return lines


# --- the run ------------------------------------------------------------------------


def capture_events(bundle: Bundle, namespaces: list[str]) -> None:
    """Each DFE namespace's events, oldest first, the newest kept past the byte cap."""
    for namespace in namespaces[:MAX_EVENT_NAMESPACES]:
        args = ["-n", namespace, "get", "events", fe.OLDEST_FIRST, "-o", "wide"]
        bundle.capture(f"events/{fe.safe_name(namespace)}.txt", args, tail=True)
    skipped = namespaces[MAX_EVENT_NAMESPACES:]
    if skipped:
        bundle.note("events/", f"+{len(skipped)} namespaces not read: {', '.join(skipped)}")


def capture_applications(bundle: Bundle) -> list[dict]:
    """Every Argo Application's status into argo-applications.txt, and the list for the events."""
    apps = bundle.items("argo-applications.txt", ["get", APPLICATIONS, "-A"])
    if apps:
        text = "\n".join(application_lines(apps)) + "\n"
        bundle.write("argo-applications.txt", text.encode("utf-8"))
    elif apps is not None:
        bundle.note("argo-applications.txt", "no Application exists, nothing written")
    return apps or []


def _read_log(bundle: Bundle, name: str, args: list[str], keep: float) -> None:
    limit = min(LOG_BYTES, bundle.log_left)
    if limit <= 0:
        bundle.note(name, f"not read: the {LOG_TOTAL} byte limit on logs was reached", args)
        return
    outcome = bundle.capture(name, args, limit=limit, tail=True, timeout=LOG_TIMEOUT, keep=keep)
    if outcome:
        bundle.log_left -= len(outcome.output)


def capture_logs(bundle: Bundle, targets: list[dict], *, minutes: int, keep: float) -> None:
    """Each logged container's last `minutes`, and its previous run when it restarted."""
    for pod in targets[:MAX_LOGGED_PODS]:
        namespace, name = _identity(pod)
        for container, restarts in fe.containers_of(pod):
            stem = f"logs/{fe.safe_name(namespace)}/{fe.safe_name(name)}.{fe.safe_name(container)}"
            base = ["-n", namespace, "logs", name, "-c", container]
            _read_log(bundle, f"{stem}.log", [*base, f"--since={minutes}m", "--timestamps"], keep)
            if restarts:
                previous = [*base, "--previous", f"--tail={PREVIOUS_TAIL}", "--timestamps"]
                _read_log(bundle, f"{stem}.previous.log", previous, keep)
    skipped = [_identity(pod)[1] for pod in targets[MAX_LOGGED_PODS:]]
    if skipped:
        bundle.note("logs/", f"+{len(skipped)} pods not read: {', '.join(skipped)}")


def collect(bundle: Bundle, *, minutes: int, image: str) -> None:
    """Every capture, the client pod applied first so its image pulls while the rest run."""
    listed = "(the pods the logs are read from)"
    pods = guarded(bundle, listed, lambda: bundle.items(listed, ["get", "pods", "-A"]))
    targets = log_targets(pods or [])
    namespace = engine_namespace(pods or [])
    client = guarded(bundle, "kafka/", lambda: start_kafka_client(bundle, namespace, image))
    try:
        wide = guarded(
            bundle,
            "workloads.txt",
            lambda: bundle.capture("workloads.txt", ["get", "pods,deploy,rs", "-A", "-o", "wide"]),
        )
        if pods is None and not (wide and wide.ok):
            bundle.note("(the rest)", "not run: the API server answered neither read above")
            return
        apps = guarded(bundle, "argo-applications.txt", lambda: capture_applications(bundle)) or []
        guarded(bundle, "events/", lambda: capture_events(bundle, dfe_namespaces(apps, targets)))
        keep = bundle.budget * KAFKA_SHARE if client and client.started else 0.0
        guarded(bundle, "logs/", lambda: capture_logs(bundle, targets, minutes=minutes, keep=keep))
        if client and client.started:
            guarded(bundle, "kafka/", lambda: read_kafka(bundle, client))
    finally:
        if client:
            guarded(bundle, "(the client pod, deleted)", lambda: stop_kafka_client(bundle, client))


def _kafka_values() -> str:
    try:
        return KAFKA_VALUES.read_text(encoding="utf-8")
    except OSError:
        return ""


def cmd_diagnostics(args: argparse.Namespace) -> int:
    """`dfe-ops diagnostics`: write one bundle under --dir, and exit 0 whatever it finds."""
    start = time.monotonic()
    now = time.gmtime()
    out = Path(args.dir) / f"{time.strftime('%Y%m%dT%H%M%SZ', now)}-{fe.safe_name(args.label)}"
    try:
        out.mkdir(parents=True, exist_ok=True)
        header = f"diagnostics for {args.label}, read {time.strftime('%Y-%m-%dT%H:%M:%SZ', now)}\n"
        (out / fe.INDEX).write_text(header, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"  diagnostics skipped: {out} cannot be written: {exc}", file=sys.stderr)
        return 0
    prefix = kubectl_prefix(args) + (["--server", args.server] if args.server else [])
    reserve = min(CLEANUP_RESERVE, args.budget / 4)
    bundle = Bundle(out, prefix, args.budget, start + args.budget - reserve, start + args.budget)
    try:
        collect(bundle, minutes=args.log_minutes, image=kafka_image(os.environ, _kafka_values()))
    except Exception as exc:  # the bundle is evidence, never a reason to stop what runs after it
        with contextlib.suppress(OSError):
            bundle.note("(the bundle)", f"raised {type(exc).__name__}: {exc}")
    print(
        f"diagnostics: {bundle.answered} of {bundle.runs} kubectl calls answered, "
        f"{len(bundle.files)} file(s) under {out}. {fe.INDEX} says how each ended"
    )
    return 0


def _positive[N: (int, float)](kind: Callable[[str], N]) -> Callable[[str], N]:
    """An argparse type that reads `kind` and refuses zero or less."""

    def parse(raw: str) -> N:
        value = kind(raw)
        if value <= 0:
            raise argparse.ArgumentTypeError(f"{raw} is not more than zero")
        return value

    return parse


def add_diagnostics_subparser(sub: argparse._SubParsersAction) -> None:
    """Register `dfe-ops diagnostics`."""
    diag = sub.add_parser(
        "diagnostics",
        help="write the DFE apps' recent logs, workloads, events, Argo status and Kafka offsets "
        "to a timestamped bundle, for after a cycle stage fails and before the "
        "teardown",
        description="Writes DIR/<UTC time>-<label>/: kubectl get pods,deploy,rs -A -o wide, the "
        "events of argocd and of every namespace an Argo Application deploys into, every "
        "Application's sync and health with each resource not Synced and Healthy, the last "
        "--log-minutes of every container of dfe-receiver, dfe-loader, dfe-engine, "
        "dfe-hunt-runner and each dfe-transform-* deployment with the previous run of any that "
        "restarted, and every consumer group's offsets with the end offset of each _land, _load "
        "and _dlq topic. The Kafka reads run from a short-lived pod beside dfe-engine that mounts "
        "the engine's own SASL Secret, deleted before this returns. Every file is redacted, every "
        "command is bounded, the whole run stops at --budget, and a capture that fails is "
        "recorded in index.txt while the rest still run. Reads no Secret. Exits 0.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    diag.add_argument(
        "--dir", required=True, help="directory the bundle's own directory goes under"
    )
    diag.add_argument(
        "--label", default="manual", help="what failed: the end of the bundle's directory name"
    )
    diag.add_argument("--kubeconfig", default="", help="kubeconfig to read; empty is kubectl's own")
    diag.add_argument(
        "--context", default="", help="kubeconfig context to read; empty is its current one"
    )
    diag.add_argument(
        "--server",
        default="",
        help="API server address, for a kubeconfig whose server name does not resolve here",
    )
    diag.add_argument(
        "--log-minutes",
        type=_positive(int),
        default=LOG_MINUTES,
        help="minutes of each app's log to read",
    )
    diag.add_argument(
        "--budget", type=_positive(float), default=BUDGET, help="seconds the whole bundle may take"
    )
    # Its output lands in a CI log beside the failed stage, and a banner costs time inside the box.
    diag.set_defaults(func=cmd_diagnostics, banner=False)
