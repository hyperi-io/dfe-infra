#  Project:      dfe-infra
#  File:         acceptance/edge/steps.py
#  Purpose:      What each row of the edge suite decides: the receiver's public
#                door and when it is open, the allow-listed send, the row in
#                main, the send from the cluster's own egress that must be
#                refused, the search HyperDX runs for the marker, and where
#                dfe-engine keeps the secrets it mints.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""edge.steps -- the decisions behind the edge suite's rows.

Each decision is a function of what kubectl, the receiver, the datastore or
HyperDX answered, so the tests make it with fabricated answers and the runner
supplies the real ones. Standard library only, like the rest of the suites.
"""

import ipaddress
import json
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from acceptance.clients import Datastore, tls_context
from acceptance.onboarding import wizard
from acceptance.source import steps as source_steps

MARKER_KIND = "edge"
# The landing table, and the HyperDX source the fork seeds over it on every team.
MAIN_TABLE = "main"
MAIN_SOURCE = "main"
# argocd/values/apps/dfe-receiver/values.yaml labels each load balancer it renders.
EXPOSURE_SELECTOR = "dfe.hyperi.io/exposure=public"
# The TCP door; the UDP one is `-public-udp` and takes no POST.
PUBLIC_SUFFIX = "-public"
# Argo picks up the deploy-repo write, the controller provisions the load balancer, and its name reaches DNS.
ENDPOINT_DEADLINE = 1200.0
ENDPOINT_INTERVAL = 15.0
# A new load balancer drops connections until its targets pass their health checks.
ACCEPT_DEADLINE = 600.0
SEND_TIMEOUT = 15
LANDED_DEADLINE = 300.0
HYPERDX_DEADLINE = 120.0
HYPERDX_WINDOW_MINUTES = 60
# The path the fork's browser client posts to (packages/app/src/clickhouse.ts). With a
# trailing slash the frontend answers its own 308, and a redirect is never followed.
CLICKHOUSE_PROXY = "/api/clickhouse-proxy"
# The cookie dfe-ui plants across the domain for the console's HyperDX iframe, which sends no
# Authorization header, so the search here authenticates the way a user's does.
TOKEN_COOKIE = "dfe_token"
POLL_INTERVAL = 5.0
# How long the run waits on the probe pod, and the pod's own limit inside that.
PROBE_DEADLINE = 300.0
PROBE_POD_DEADLINE = 240
PROBE_POD = "edge-probe"
PROBE_CONNECT_SECONDS = 20
# curl's exits for a connection the far end never accepted: refused or unreachable, and timed out.
REFUSED_EXITS = (7, 28)
# The URLs reach the shell as variables, so nothing a Service reports is ever parsed as shell.
# The control dials the gateway under the console's own name: Envoy picks a listener by SNI and
# closes a handshake for any name none of them carries, the load balancer's own included.
PROBE_SCRIPT = (
    f'control=$(curl -sk -o /dev/null -w "%{{http_code}}" --max-time {PROBE_CONNECT_SECONDS} '
    '--connect-to "$PROBE_CONTROL_CONNECT" "$PROBE_CONTROL_URL"); echo "control $? ${control:-000}"; '
    f'target=$(curl -sk -o /dev/null -w "%{{http_code}}" --max-time {PROBE_CONNECT_SECONDS} '
    '"$PROBE_TARGET_URL"); echo "target $? ${target:-000}"'
)
_READING = re.compile(r"^(control|target) (\d+) (\d{3})$")
_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# The cluster secret bootstrap writes, whose annotations carry the aws root's facts.
CLUSTER_SECRET_NAMESPACE = "argocd"
CLUSTER_SECRET = "dfe-cluster"
FACT = "dfe.hyperi.io/"
# What every engine mints on its first boot; each organisation adds a ch/orgs/<org>.
MINTED = ("jwt/signing-key", "ch/service/query_reader")
ORG_PASSWORDS = "ch/orgs/"
AWS_TIMEOUT = 60


def row(results: list[wizard.StepResult], slug: str, status: str, detail: str) -> None:
    """Append one step-table row that carries no screenshot."""
    results.append(wizard.StepResult(slug, status, detail))


def new_run_id() -> str:
    """A run id that is one search token and needs no quoting in SQL."""
    return f"edge{uuid.uuid4().hex[:16]}"


def marker_record(run_id: str) -> dict:
    """The record the allow-listed send carries: the run's marker and no field a source routes on."""
    return {
        source_steps.MARKER: source_steps.run_marker(run_id, MARKER_KIND),
        "message": f"edge acceptance send {run_id}",
    }


def split_cidrs(raw: str) -> list[str]:
    """A comma-separated network list, blanks dropped."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _first_line(text: str) -> str:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[0].strip() if lines else ""


def _host(address: str) -> str:
    return f"[{address}]" if ":" in address else address


# --- kubectl --------------------------------------------------------------------


def kubectl(kube: list[str], *argv: str, stdin: str = "") -> tuple[int, str, str]:
    """Run kubectl and return (exit code, stdout, stderr); never raises on a non-zero exit."""
    done = subprocess.run(
        [*kube, *argv], input=stdin or None, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    return done.returncode, done.stdout, done.stderr


def kubectl_json(kube: list[str], *argv: str) -> tuple[int, dict, str]:
    """kubectl ... -o json, parsed; (exit code, document or {}, first stderr line)."""
    rc, out, err = kubectl(kube, *argv, "-o", "json")
    if rc != 0:
        return rc, {}, _first_line(err)
    try:
        return 0, json.loads(out), ""
    except json.JSONDecodeError as exc:
        return 1, {}, f"unparseable kubectl output: {exc}"


# --- the public endpoint ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Endpoint:
    """The receiver's public load balancer, as its Service reports it."""

    name: str
    address: str
    port: int
    source_ranges: tuple[str, ...]

    @property
    def ingest_url(self) -> str:
        """Where the receiver takes a JSON record."""
        return f"http://{_host(self.address)}:{self.port}/ingest"

    @property
    def liveness_url(self) -> str:
        """A path the receiver answers on the same listener, for a probe that only asks whether it answers."""
        return f"http://{_host(self.address)}:{self.port}/livez"


def public_endpoint(services: Mapping) -> Endpoint | None:
    """The TCP load balancer the receiver renders, once it has an address and an http port.

    Args:
        services: `kubectl get service -l <EXPOSURE_SELECTOR> -o json`.

    Returns:
        The endpoint, or None while none has both.
    """
    for item in services.get("items") or []:
        name = str((item.get("metadata") or {}).get("name") or "")
        spec = item.get("spec") or {}
        if spec.get("type") != "LoadBalancer" or not name.endswith(PUBLIC_SUFFIX):
            continue
        ingress = ((item.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        addresses = [str(entry.get("hostname") or entry.get("ip") or "") for entry in ingress]
        address = next((one for one in addresses if one), "")
        http = next((p for p in spec.get("ports") or [] if p.get("name") == "http"), None)
        if address and http:
            ranges = tuple(str(cidr) for cidr in spec.get("loadBalancerSourceRanges") or [])
            return Endpoint(name, address, int(http["port"]), ranges)
    return None


def resolves(host: str) -> bool:
    """Whether this machine can resolve *host* at all."""
    try:
        socket.getaddrinfo(host, None)
    except OSError:
        return False
    return True


def endpoint_problem(endpoint: Endpoint, allow: list[str], resolve) -> str:
    """Why *endpoint* is not yet the door the run asked for, or empty when it is.

    The load balancer has to carry the list the deploy repo now names, not the
    one before it, or the accepted send proves an older fence.
    """
    if set(endpoint.source_ranges) != set(allow):
        return (
            f"{endpoint.name} loadBalancerSourceRanges are {', '.join(endpoint.source_ranges) or 'empty'}, "
            f"not {', '.join(allow)}"
        )
    if not resolve(endpoint.address):
        return f"{endpoint.address} does not resolve yet"
    return ""


def wait_endpoint(kube: list[str], namespace: str, allow: list[str], deadline: float,
                  *, interval: float = ENDPOINT_INTERVAL) -> tuple[Endpoint | None, str]:
    """Poll the receiver's Services until the public door carries *allow* and resolves.

    Returns:
        The endpoint and a line naming it, or None and why there is none.
    """
    until = time.monotonic() + deadline
    last = "nothing listed yet"
    while True:
        rc, doc, err = kubectl_json(kube, "-n", namespace, "get", "service", "-l", EXPOSURE_SELECTOR)
        endpoint = public_endpoint(doc) if rc == 0 else None
        if rc != 0:
            last = f"could not list the receiver's Services: {err}"
        elif endpoint is None:
            last = f"no LoadBalancer Service labelled {EXPOSURE_SELECTOR} has an address and an http port"
        else:
            last = endpoint_problem(endpoint, allow, resolves)
            if not last:
                return endpoint, (
                    f"{endpoint.name} at {endpoint.address}:{endpoint.port}, admitting "
                    f"{', '.join(endpoint.source_ranges)}"
                )
        if time.monotonic() >= until:
            return None, (
                f"no public endpoint within {deadline:.0f}s: {last}. Check exposure.groups.ingest "
                "(enabled, door loadbalancer) in the deploy repo's overlay, and the load balancer controller"
            )
        time.sleep(interval)


# --- the allow-listed send ------------------------------------------------------------


def post_marker(url: str, body: dict, deadline: float, *, interval: float = POLL_INTERVAL) -> tuple[bool, str]:
    """POST *body* to the public door until it answers 2xx, a 4xx rejects it, or *deadline* passes.

    A new load balancer drops connections until its targets are healthy, so no
    answer and a 5xx are retried; a 4xx is the receiver refusing the record.

    Returns:
        Whether the send was accepted, and one line of evidence.
    """
    data = json.dumps(body).encode()
    started = time.monotonic()
    attempts, last = 0, ""
    while True:
        attempts += 1
        request = urllib.request.Request(
            url, data=data, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=SEND_TIMEOUT) as response:
                status = response.status
            elapsed = time.monotonic() - started
            return True, f"POST {url} answered {status} after {attempts} attempt(s), {elapsed:.0f}s"
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return False, f"POST {url} was rejected: {exc.code} {exc.read()[:200]!r}"
            last = f"answered {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            last = str(getattr(exc, "reason", exc))
        if time.monotonic() - started >= deadline:
            return False, (
                f"POST {url} never answered 2xx in {deadline:.0f}s ({attempts} attempt(s), last: {last})"
            )
        time.sleep(interval)


# --- the row in main ------------------------------------------------------------------


def record_landed(results: list[wizard.StepResult], store: Datastore, run_id: str, deadline: float) -> bool:
    """The marker's row in main, read straight off the datastore; True when it is there."""
    if not store.host:
        row(results, "landed", "failed", "no datastore access in this run, so the row in main cannot be read")
        return False
    found = source_steps.wait_rows(
        store, MAIN_TABLE, 1, deadline, where=source_steps.marked(run_id, MARKER_KIND)
    )
    if found:
        row(results, "landed", "done", f"{found} row(s) carrying {run_id} in {store.database}.{MAIN_TABLE}")
        return True
    row(results, "landed", "failed", f"{run_id} is not in {store.database}.{MAIN_TABLE} after {deadline:.0f}s")
    return False


# --- the refused send -----------------------------------------------------------------


def egress_inside_allow(egress: list[str], allow: list[str]) -> list[str]:
    """Each egress network the allow list already admits, as `<egress> in <listed>`."""
    listed = [ipaddress.ip_network(cidr, strict=False) for cidr in allow]
    inside = []
    for cidr in egress:
        network = ipaddress.ip_network(cidr, strict=False)
        for admitted in listed:
            if network.version == admitted.version and network.overlaps(admitted):
                inside.append(f"{cidr} in {admitted}")
    return inside


def probe_readings(logs: str) -> dict[str, tuple[int, str]]:
    """The probe's `control` and `target` lines: curl's exit code and the HTTP code it saw."""
    readings: dict[str, tuple[int, str]] = {}
    for line in logs.splitlines():
        found = _READING.match(line.strip())
        if found:
            readings[found.group(1)] = (int(found.group(2)), found.group(3))
    return readings


def refused_outcome(readings: Mapping[str, tuple[int, str]], endpoint: Endpoint, gateway: str,
                    egress: list[str], allow: list[str]) -> tuple[str, str]:
    """The step result for the send from inside the cluster.

    A refusal proves the list only beside a control the same pod reached: a pod
    that reaches nothing is an egress policy talking, not the load balancer.

    Args:
        readings: probe_readings of the probe pod's log.
        endpoint: The receiver's public door.
        gateway: The gateway load balancer the control reached for.
        egress: The cluster's NAT egress.
        allow: The allow list the door carries.

    Returns:
        The status and detail for the row.
    """
    control, target = readings.get("control"), readings.get("target")
    door = f"{endpoint.address}:{endpoint.port}"
    if control is None or target is None:
        missing = "control" if control is None else "target"
        return "failed", f"the probe printed no {missing} reading, so nothing was measured"
    if target[1] != "000":
        return "failed", (
            f"the receiver at {door} answered {target[1]} from inside the cluster, whose NAT egress "
            f"{', '.join(egress)} the allow list {', '.join(allow)} does not name: the load balancer is "
            "not enforcing the list"
        )
    if control[0] in REFUSED_EXITS:
        return "failed", (
            f"the probe pod reached nothing, not even the gateway at {gateway} (curl exit {control[0]}), "
            "so a refusal from it would prove nothing about the allow list"
        )
    if control[0] != 0 or control[1] == "000":
        return "failed", (
            f"the probe pod's control through the gateway at {gateway} got no HTTP answer "
            f"(curl exit {control[0]}), so a refusal from it would prove nothing about the allow list"
        )
    if target[0] not in REFUSED_EXITS:
        return "failed", (
            f"the send to {door} from inside the cluster ended in curl exit {target[0]}, which is not a "
            "refused or timed-out connect"
        )
    how = "timed out" if target[0] == 28 else "was refused"
    return "done", (
        f"the receiver at {door} {how} from inside the cluster, whose NAT egress {', '.join(egress)} is "
        f"outside the allow list {', '.join(allow)}; the same pod reached the gateway at {gateway} "
        f"(answered {control[1]})"
    )


@dataclass(frozen=True, slots=True)
class Control:
    """What the probe asks the gateway for: the console's URL, and curl's --connect-to pair."""

    url: str
    connect: str


def gateway_control(console_url: str, gateway: str) -> Control | None:
    """The console's own name and port, dialled at *gateway*; None when the URL names no host."""
    parts = urllib.parse.urlsplit(console_url)
    host = parts.hostname or ""
    if not host or not gateway:
        return None
    port = parts.port or 443
    return Control(f"https://{_host(host)}:{port}/", f"{_host(host)}:{port}:{_host(gateway)}:{port}")


def probe_pod(namespace: str, receiver: Mapping, control: Control, target_url: str) -> dict:
    """The probe Pod: the receiver's own image, placement and security context, running two curls.

    The image is one the cluster already pulls and carries curl, which the
    receiver's own init container calls.
    """
    template = ((receiver.get("spec") or {}).get("template") or {}).get("spec") or {}
    source = (template.get("containers") or [{}])[0]
    container: dict = {
        "name": "probe",
        "image": source.get("image", ""),
        "command": ["/bin/sh", "-c", PROBE_SCRIPT],
        "env": [
            {"name": "PROBE_CONTROL_URL", "value": control.url},
            {"name": "PROBE_CONTROL_CONNECT", "value": control.connect},
            {"name": "PROBE_TARGET_URL", "value": target_url},
        ],
        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"},
                      "limits": {"cpu": "100m", "memory": "64Mi"}},
    }
    if source.get("securityContext"):
        container["securityContext"] = source["securityContext"]
    spec: dict = {
        "restartPolicy": "Never",
        "activeDeadlineSeconds": PROBE_POD_DEADLINE,
        "automountServiceAccountToken": False,
        "containers": [container],
    }
    for key in ("securityContext", "nodeSelector", "tolerations", "imagePullSecrets"):
        if template.get(key):
            spec[key] = template[key]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": PROBE_POD, "namespace": namespace,
                     "labels": {"app.kubernetes.io/name": "dfe-edge-probe"}},
        "spec": spec,
    }


def copy_secret(kube: list[str], source_ns: str, target_ns: str, name: str) -> bool:
    """Copy one Secret's type and data, never its metadata, and never print either.

    `create`, not `apply`: apply keeps the whole object, data included, in an
    annotation that `kubectl describe` prints.
    """
    rc, out, _ = kubectl(kube, "-n", source_ns, "get", "secret", name, "-o", "json")
    if rc != 0:
        return False
    try:
        original = json.loads(out)
    except json.JSONDecodeError:
        return False
    copy = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": original.get("type") or "Opaque",
        "data": original.get("data") or {},
        "metadata": {"name": name, "namespace": target_ns},
    }
    rc, _, _ = kubectl(kube, "create", "-f", "-", stdin=json.dumps(copy))
    return rc == 0


def wait_pod(kube: list[str], namespace: str, deadline: float) -> tuple[str, str]:
    """The probe pod's phase once it finishes or *deadline* passes, and any container's wait reason."""
    until = time.monotonic() + deadline
    phase, waiting = "Pending", ""
    while True:
        rc, doc, _ = kubectl_json(kube, "-n", namespace, "get", "pod", PROBE_POD)
        if rc == 0:
            status = doc.get("status") or {}
            phase = str(status.get("phase") or phase)
            states = [(c.get("state") or {}).get("waiting") or {} for c in status.get("containerStatuses") or []]
            reasons = [str(state.get("reason")) for state in states if state.get("reason")]
            waiting = f" ({', '.join(reasons)})" if reasons else ""
            if phase in ("Succeeded", "Failed"):
                return phase, waiting
        if time.monotonic() >= until:
            return phase, waiting
        time.sleep(POLL_INTERVAL)


def _probe(kube: list[str], app_ns: str, probe_ns: str, receiver: Mapping, endpoint: Endpoint,
           gateway: str, control: Control, allow: list[str], egress: list[str]) -> tuple[str, str]:
    namespace = {"apiVersion": "v1", "kind": "Namespace",
                 "metadata": {"name": probe_ns, "labels": {"app.kubernetes.io/managed-by": "dfe-acceptance"}}}
    rc, _, err = kubectl(kube, "create", "-f", "-", stdin=json.dumps(namespace))
    if rc != 0:
        return "failed", f"could not create namespace {probe_ns}: {_first_line(err)}"
    template = ((receiver.get("spec") or {}).get("template") or {}).get("spec") or {}
    for ref in template.get("imagePullSecrets") or []:
        name = str(ref.get("name") or "")
        if name and not copy_secret(kube, app_ns, probe_ns, name):
            return "failed", f"could not copy the receiver's pull secret {name} into {probe_ns}"
    pod = probe_pod(probe_ns, receiver, control, endpoint.liveness_url)
    rc, _, err = kubectl(kube, "create", "-f", "-", stdin=json.dumps(pod))
    if rc != 0:
        return "failed", f"could not create the probe pod in {probe_ns}: {_first_line(err)}"
    phase, waiting = wait_pod(kube, probe_ns, PROBE_DEADLINE)
    if phase != "Succeeded":
        return "failed", f"the probe pod in {probe_ns} ended {phase}{waiting}, so nothing was measured"
    rc, logs, err = kubectl(kube, "-n", probe_ns, "logs", f"pod/{PROBE_POD}")
    if rc != 0:
        return "failed", f"could not read the probe pod's log: {_first_line(err)}"
    return refused_outcome(probe_readings(logs), endpoint, gateway, egress, allow)


def record_refused(results: list[wizard.StepResult], kube: list[str], namespace: str, endpoint: Endpoint,
                   gateway: str, allow: list[str], egress: list[str], run_id: str, *,
                   console_url: str, receiver_deployment: str = "dfe-receiver") -> None:
    """Send to the public door from inside the cluster, and expect no answer.

    The probe runs in a namespace of its own: every DFE namespace carries a
    default-deny egress baseline, which would refuse the send before the load
    balancer ever saw it. The namespace goes whatever the probe found. Its
    control asks the gateway for *console_url*, a name the gateway answers on.
    """
    inside = egress_inside_allow(egress, allow)
    if not egress:
        row(results, "refused", "failed", "no NAT egress was named, so nothing says the send came from off the list")
        return
    if inside:
        row(results, "refused", "failed", (
            f"the allow list already admits the cluster's NAT egress ({'; '.join(inside)}), so a refusal could "
            "not prove it"
        ))
        return
    if not gateway:
        row(results, "refused", "failed", (
            "no gateway address to reach as the control, so a refusal could not be told from a pod with no way out"
        ))
        return
    control = gateway_control(console_url, gateway)
    if control is None:
        row(results, "refused", "failed", (
            f"no console host in {console_url!r} for the control to ask the gateway for, so a refusal could not "
            "be told from a pod with no way out"
        ))
        return
    rc, receiver, err = kubectl_json(kube, "-n", namespace, "get", "deployment", receiver_deployment)
    if rc != 0:
        row(results, "refused", "failed", f"could not read deployment/{receiver_deployment} for the probe's image: {err}")
        return
    probe_ns = f"{run_id}-probe"
    try:
        status, detail = _probe(kube, namespace, probe_ns, receiver, endpoint, gateway, control, allow, egress)
    finally:
        kubectl(kube, "delete", "namespace", probe_ns, "--wait=false", "--ignore-not-found")
    row(results, "refused", status, detail)


# --- HyperDX ----------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib carries the token cookie to whatever host one names."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main_source(sources: object) -> dict | None:
    """The team's `main` source out of HyperDX's source listing, or None."""
    if not isinstance(sources, list):
        return None
    return next((s for s in sources if isinstance(s, dict) and s.get("name") == MAIN_SOURCE), None)


def _plain(value: object, what: str) -> str:
    text = str(value or "")
    if not _PLAIN.match(text):
        raise ValueError(f"{what} {text!r} is not a plain name, so it is not put in a query")
    return text


def hyperdx_count_sql(source: Mapping, run_id: str, window_minutes: int = HYPERDX_WINDOW_MINUTES) -> str:
    """The count HyperDX's search runs for a bare term over *source*, as its proxy takes it.

    A bare Lucene term on a JSON implicit column renders as toString(column)
    ILIKE, which this mirrors over the source's own table and time column.

    Raises:
        ValueError: A name from the source, or the run id, is not a plain identifier.
    """
    origin = source.get("from") or {}
    database = _plain(origin.get("databaseName"), "the source's database")
    table = _plain(origin.get("tableName"), "the source's table")
    timestamp = _plain(source.get("timestampValueExpression"), "the source's timestamp column")
    implicit = _plain(source.get("implicitColumnExpression"), "the source's implicit column")
    term = _plain(run_id, "the run id")
    return (
        f"SELECT count() FROM `{database}`.`{table}` "
        f"WHERE `{timestamp}` >= now() - INTERVAL {int(window_minutes)} MINUTE "
        f"AND toString(`{implicit}`) ILIKE '%{term}%' FORMAT JSONCompact"
    )


def hyperdx_call(base: str, path: str, token: str, verify: bool, *, body: str | None = None,
                 headers: Mapping[str, str] | None = None) -> tuple[int, object]:
    """One call to HyperDX's API through its frontend, as the embedded app makes it.

    The token rides in the ``dfe_token`` cookie, as it does from the console's
    iframe.

    Returns:
        The status and the decoded body, or the body's first 200 characters
        when it is not JSON.
    """
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=tls_context(verify)))
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=body.encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers={"Cookie": f"{TOKEN_COOKIE}={token}", "Accept": "application/json", **(headers or {})},
    )
    try:
        with opener.open(request, timeout=60) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:200].decode(errors="replace")
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw[:200].decode(errors="replace")


def _count(answer: object) -> int:
    try:
        return int(answer["data"][0][0])  # type: ignore[index]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"the search answered {str(answer)[:120]!r}, which carries no count") from exc


def record_hyperdx(results: list[wizard.StepResult], base: str, engine, run_id: str, verify: bool,
                   deadline: float) -> None:
    """Find the marker through HyperDX's own API, under an engine-minted token.

    The two calls the search page makes, with its credential: the team's
    sources, to resolve `main` and its connection, then the query through
    HyperDX's ClickHouse proxy, which runs it as the team's own ClickHouse user.
    """
    if not base:
        row(results, "hyperdx", "failed", (
            "no HyperDX URL: no HTTPRoute under the console's domain sends to dfe-hyperdx; pass --hyperdx-url"
        ))
        return
    try:
        issued = engine.password
        engine.login()
        if engine.password != issued:
            # That login made the forced change, which ends the session its token belongs to.
            engine.login()
    except (OSError, RuntimeError) as exc:
        row(results, "hyperdx", "failed", f"the engine would not mint a token for HyperDX: {exc}")
        return
    until = time.monotonic() + deadline
    try:
        while True:
            status, sources = hyperdx_call(base, "/api/sources", engine.token, verify)
            if status != 200:
                row(results, "hyperdx", "failed", f"GET {base}/api/sources answered {status}: {sources}")
                return
            source = main_source(sources)
            if source is not None:
                break
            if time.monotonic() >= until:
                row(results, "hyperdx", "failed", (
                    f"HyperDX lists no main source for this token's team after {deadline:.0f}s; the fork seeds "
                    "main on a team's first engine-token request (dfe/controllers/org-connection.ts)"
                ))
                return
            time.sleep(POLL_INTERVAL)
        sql = hyperdx_count_sql(source, run_id)
        proxy = {"x-hyperdx-connection-id": str(source.get("connection") or ""), "Content-Type": "text/plain"}
        while True:
            status, answer = hyperdx_call(base, CLICKHOUSE_PROXY, engine.token, verify, body=sql,
                                          headers=proxy)
            if status != 200:
                row(results, "hyperdx", "failed", (
                    f"the search through {base}{CLICKHOUSE_PROXY} answered {status}: {answer}"
                ))
                return
            found = _count(answer)
            if found:
                row(results, "hyperdx", "done", (
                    f"HyperDX's search over its {MAIN_SOURCE} source found {found} row(s) carrying {run_id}"
                ))
                return
            if time.monotonic() >= until:
                row(results, "hyperdx", "failed", (
                    f"HyperDX's search over its {MAIN_SOURCE} source found 0 rows carrying {run_id} in the "
                    f"last {HYPERDX_WINDOW_MINUTES} minutes"
                ))
                return
            time.sleep(POLL_INTERVAL)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        row(results, "hyperdx", "failed", f"HyperDX at {base} could not be searched: {exc}")


# --- dfe-engine's minted secrets ---------------------------------------------------------


def cluster_facts(kube: list[str]) -> tuple[dict[str, str] | None, str]:
    """The cluster secret's annotations, read without its data; None and why when unreadable."""
    rc, out, err = kubectl(
        kube, "-n", CLUSTER_SECRET_NAMESPACE, "get", "secret", CLUSTER_SECRET,
        "-o", "jsonpath={.metadata.annotations}",
    )
    if rc != 0:
        return None, f"could not read secret/{CLUSTER_SECRET} in {CLUSTER_SECRET_NAMESPACE}: {_first_line(err)}"
    try:
        found = json.loads(out or "{}")
    except json.JSONDecodeError as exc:
        return None, f"secret/{CLUSTER_SECRET}'s annotations are not JSON: {exc}"
    return {str(key): str(value) for key, value in found.items()}, ""


def list_engine_secrets(prefix: str, region: str) -> tuple[list[dict] | None, str]:
    """ListSecrets under ``prefix``/: names, keys and tags, never a value; None and why on failure.

    The name filter is a case-insensitive prefix match, so each entry is checked again here.
    """
    cmd = [
        "aws", "secretsmanager", "list-secrets", "--region", region,
        "--filters", f"Key=name,Values={prefix}/", "--output", "json",
    ]
    try:
        done = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False, timeout=AWS_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"aws secretsmanager list-secrets did not run: {exc}"
    if done.returncode != 0:
        return None, f"aws secretsmanager list-secrets failed: {_first_line(done.stderr)}"
    try:
        listed = json.loads(done.stdout or "{}").get("SecretList") or []
    except json.JSONDecodeError as exc:
        return None, f"aws secretsmanager list-secrets printed no JSON: {exc}"
    return [entry for entry in listed if str(entry.get("Name", "")).startswith(f"{prefix}/")], ""


def _key_id(key: str) -> str:
    """A KMS key's id, whether it is written as its ARN or bare."""
    return key.rsplit("/", 1)[-1]


def minted_outcome(listed: list[dict], prefix: str, kms_key: str, tags: Mapping[str, str]) -> tuple[str, str]:
    """The step result for where the engine keeps what it mints.

    Args:
        listed: The ListSecrets entries under ``prefix``.
        prefix: The cluster secret's engine_secrets_prefix.
        kms_key: The cluster secret's kms_key_arn.
        tags: What each secret must carry: the run's tags, or the deployment's off a run.

    Returns:
        The status and detail for the row.
    """
    by_name = {str(entry.get("Name")): entry for entry in listed}
    wanted = [f"{prefix}/{path}" for path in MINTED]
    orgs = sorted(name for name in by_name if name.startswith(f"{prefix}/{ORG_PASSWORDS}"))
    absent = [name for name in wanted if name not in by_name]
    if not orgs:
        absent.append(f"{prefix}/{ORG_PASSWORDS}<org>")
    if absent:
        return "failed", f"Secrets Manager holds no {', '.join(absent)}"
    problems = []
    for name in [*wanted, *orgs]:
        entry = by_name[name]
        key = str(entry.get("KmsKeyId") or "")
        if not key or _key_id(key) != _key_id(kms_key):
            problems.append(f"{name} is encrypted with {key or 'the AWS managed key'}, not {kms_key}")
        carried = {str(tag.get("Key")): str(tag.get("Value")) for tag in entry.get("Tags") or []}
        lacking = sorted(k for k, v in tags.items() if carried.get(k) != v)
        if lacking:
            problems.append(f"{name} does not carry {', '.join(lacking)}")
    if problems:
        return "failed", "; ".join(problems)
    names = ", ".join(name.removeprefix(f"{prefix}/") for name in [*wanted, *orgs])
    return "done", (
        f"{names} under {prefix}/, each on {kms_key} "
        f"and carrying {', '.join(sorted(tags)) or 'no tag the cluster secret names'}"
    )


def record_minted_secrets(results: list[wizard.StepResult], kube: list[str]) -> None:
    """Where dfe-engine keeps what it mints: each secret in Secrets Manager, on the deployment key.

    Off AWS the cluster secret names no engine_secrets_prefix and the engine keeps
    its files on its own volume, so the row is skipped rather than failed.
    """
    slug = "minted-secrets-in-store"
    facts, why = cluster_facts(kube)
    if facts is None:
        row(results, slug, "failed", why)
        return
    prefix = facts.get(f"{FACT}engine_secrets_prefix", "")
    if not prefix:
        row(results, slug, "skipped", (
            "the cluster secret names no engine_secrets_prefix, so the engine keeps its secrets on its "
            "own volume (every cloud but AWS)"
        ))
        return
    # A run's own tags tell its secrets from another run's; off a run, the deployment's.
    try:
        tags = json.loads(facts.get(f"{FACT}run_tags") or facts.get(f"{FACT}resource_tags") or "{}")
    except json.JSONDecodeError as exc:
        row(results, slug, "failed", f"the cluster secret's tags are not JSON: {exc}")
        return
    listed, why = list_engine_secrets(prefix, facts.get(f"{FACT}region", ""))
    if listed is None:
        row(results, slug, "failed", why)
        return
    status, detail = minted_outcome(
        listed, prefix, facts.get(f"{FACT}kms_key_arn", ""), {str(key): str(value) for key, value in tags.items()},
    )
    row(results, slug, status, detail)
