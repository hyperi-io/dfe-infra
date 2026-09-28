#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_readiness_gate.py
#  Purpose:      Prove the deploy readiness gate judges the namespaces this
#                deploy owns, and reads a restart recency rather than any string
#                carrying an `m`.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Behaviour tests for bootstrap/smoke-test-readiness.sh.

The gate decides whether a deploy is declared up, so both of its ways of being
wrong cost a real run: a healthy pod whose lifetime restart count is hours old
was reported as live churn (`5h12m` ends in `m`, and so does `164m`), and a
denylist of the cluster's own namespaces failed the gate on whatever the
distribution ships that kube-system does not cover.

A fake `kubectl` first on PATH answers every query from a JSON fixture, so the
gate's own decisions are what is under test and no cluster is involved.

    python3 scripts/tests/test_readiness_gate.py

No third-party deps and no test runner, matching the script it tests.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GATE = REPO_ROOT / "bootstrap" / "smoke-test-readiness.sh"
DESTROY = REPO_ROOT / "bootstrap" / "destroy.sh"

# Answers kubectl's four read shapes off FAKE_KUBECTL_FIXTURE; an absent key is
# an empty result, which is what a cluster with none of that kind returns. The
# admin-links ConfigMap is the exception: absent, it is NotFound, as kubectl says.
FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
if "configmap" in args or "gateway" in args:
    fixture = json.load(open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8"))
    if "gateway" in args:
        print(json.dumps({"items": [
            {"metadata": {"name": "dfe-gateway"}, "status": {"addresses": [{"value": a}]}}
            for a in fixture.get("gateways", [])
        ]}))
        sys.exit(0)
    if "admin_links" not in fixture:
        print('Error from server (NotFound): configmaps "dfe-admin-links" not found', file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"data": {"admin_links.json": json.dumps(fixture["admin_links"])}}))
    sys.exit(0)
if "annotate" in args:
    with open(os.environ["FAKE_KUBECTL_LOG"], "a", encoding="utf-8") as log:
        log.write(" ".join(args) + "\\n")
    sys.exit(0)
if "exec" in args:
    fixture = json.load(open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8"))
    if "exec_error" in fixture:
        print(fixture["exec_error"], file=sys.stderr)
        sys.exit(fixture.get("exec_rc", 1))
    answer = fixture.get("setup_status")
    if answer is None:
        sys.exit(1)
    print(answer)
    sys.exit(0)
if "applications.argoproj.io" in args:
    key = "applications"
elif "pods" in args:
    key = "pods"
elif "deployment,statefulset" in args:
    key = "ns_workloads"
elif "deployment" in args:
    key = "deployments"
elif "statefulset" in args:
    key = "statefulsets"
elif "daemonset" in args:
    key = "daemonsets"
else:
    key = None
with open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8") as fh:
    fixture = json.load(fh)
for line in fixture.get(key) or []:
    print(line)
"""


def run_gate(
    fixture: dict,
    cwd: str | None = None,
    annotations: list[str] | None = None,
    **env_overrides: str,
) -> subprocess.CompletedProcess:
    """The gate against a fixed cluster reading, with no wait between polls.

    Every `kubectl annotate` the gate issues is appended to `annotations`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        bindir = Path(tmp)
        kubectl = bindir / "kubectl"
        kubectl.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
        kubectl.chmod(0o755)
        fixture_file = bindir / "fixture.json"
        fixture_file.write_text(json.dumps(fixture), encoding="utf-8", newline="\n")
        log_file = bindir / "annotate.log"
        log_file.touch()

        env = dict(os.environ)
        env.pop("DFE_NS", None)
        env.pop("DFE_ENV", None)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
        env["FAKE_KUBECTL_FIXTURE"] = str(fixture_file)
        env["FAKE_KUBECTL_LOG"] = str(log_file)
        # One pass: a fixed reading never converges, so a poll loop would only
        # burn the timeout before reaching the same verdict.
        env["READINESS_TIMEOUT"] = "0"
        env["READINESS_INTERVAL"] = "1"
        env["READINESS_ADMIN_UI_WAIT"] = "0"
        env.update(env_overrides)
        result = subprocess.run(
            ["bash", str(GATE)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=cwd,
            check=False,
        )
        if annotations is not None:
            annotations.extend(log_file.read_text(encoding="utf-8").splitlines())
        return result


def test_an_application_holding_a_comparison_error_is_hard_refreshed() -> None:
    """Argo caches the error to its comparison expiry, well after the repo is back."""
    annotations: list[str] = []
    run_gate(
        {
            "applications": [
                "argocd dfe-engine-default ComparisonError",
                "argocd dfe-loader-default SyncError ComparisonError",
                "argocd dfe-ui-default",
                "argocd dfe-receiver-default OrphanedResourceWarning",
            ],
        },
        annotations=annotations,
    )
    refreshed = sorted(
        next(word for word in line.split() if word.startswith("dfe-")) for line in annotations
    )
    expect(
        "only the two apps holding a ComparisonError are refreshed, and hard",
        refreshed == ["dfe-engine-default", "dfe-loader-default"]
        and all("argocd.argoproj.io/refresh=hard" in line for line in annotations),
        f"{annotations}",
    )


def test_no_application_means_no_refresh() -> None:
    annotations: list[str] = []
    run_gate({"pods": ["dfe-local dfe-engine-0 1/1 Running 0 6d"]}, annotations=annotations)
    expect(
        "nothing is annotated on a cluster with no Applications",
        annotations == [],
        f"{annotations}",
    )


def test_a_file_matching_the_namespace_glob_does_not_blind_the_gate() -> None:
    """The dfe-* pattern must stay a pattern whatever the working directory holds."""
    with tempfile.TemporaryDirectory() as cwd:
        (Path(cwd) / "dfe-access.md").write_text("stray\n", encoding="utf-8", newline="\n")
        out = run_gate(
            {
                "pods": ["dfe-local dfe-engine-0 1/1 Running 0 6d"],
                "deployments": ["dfe-local dfe-ui 2 1"],
                "ns_workloads": ["dfe-ui 1/2 2 1 5d"],
            },
            cwd=cwd,
        )
    expect(
        "an unready dfe-* workload still fails beside a dfe-* file",
        out.returncode != 0 and "deployment dfe-local/dfe-ui 1/2 ready" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_an_hours_old_restart_count_is_not_live_churn() -> None:
    """The #218 follow-up: `5h12m` also ends in `m`, and matched the churn glob.

    Restart counts are lifetime, so a pod that settled hours ago is healthy and
    a gate that fails on it fails every long-lived deploy.
    """
    out = run_gate({"pods": ["dfe-local dfe-engine-0 1/1 Running 42 (5h12m ago) 6d"]})
    expect(
        "a pod last restarted 5h12m ago passes",
        out.returncode == 0,
        f"rc={out.returncode} {out.stdout}",
    )


def test_a_restart_hours_ago_in_minutes_is_not_live_churn() -> None:
    """kubectl prints `(164m ago)` for a restart under three hours old.

    After an overnight control-plane outage, cnpg-operator, envoy-gateway and
    keda-operator carried 80-104 lifetime restarts and had been stable for 164
    minutes, and the gate still called that live churn.
    """
    out = run_gate({"pods": ["cnpg cnpg-operator-0 1/1 Running 104 (164m ago) 6d"]})
    expect(
        "a pod last restarted 164m ago passes",
        out.returncode == 0,
        f"rc={out.returncode} {out.stdout}",
    )


def test_the_churn_window_is_a_knob() -> None:
    """A deployment that wants a wider window sets one, and it is obeyed."""
    pods = {"pods": ["cnpg cnpg-operator-0 1/1 Running 104 (164m ago) 6d"]}
    out = run_gate(pods, READINESS_CHURN_MINUTES="200")
    expect(
        "164m is live churn once the window is 200 minutes",
        out.returncode != 0 and "runaway restarts" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_a_restart_minutes_ago_is_live_churn() -> None:
    """The case the recency check exists for has to still fail."""
    out = run_gate({"pods": ["dfe-local dfe-engine-0 1/1 Running 42 (2m ago) 6d"]})
    expect(
        "a pod last restarted 2m ago fails",
        out.returncode != 0 and "runaway restarts" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_a_restart_seconds_ago_is_live_churn() -> None:
    """The other recency kubectl emits, so neither unit is checked alone."""
    out = run_gate({"pods": ["dfe-local dfe-engine-0 1/1 Running 42 (45s ago) 6d"]})
    expect(
        "a pod last restarted 45s ago fails",
        out.returncode != 0 and "runaway restarts" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_a_days_old_restart_count_is_not_live_churn() -> None:
    """`2d3h` carries no `m` or `s` terminator and must not be read as one."""
    out = run_gate({"pods": ["dfe-local dfe-engine-0 1/1 Running 42 (2d3h ago) 9d"]})
    expect(
        "a pod last restarted 2d3h ago passes",
        out.returncode == 0,
        f"rc={out.returncode} {out.stdout}",
    )


def test_a_namespace_the_deploy_does_not_own_is_not_judged() -> None:
    """The allowlist replaces a denylist that named kube-system and Rancher only.

    calico-system, tigera-operator, longhorn-system and metallb-system are the
    cluster's, not the deploy's, and each of them failed the gate on a real RKE2.
    """
    foreign = [
        "calico-system calico-node-abc 0/1 CrashLoopBackOff 9 (30s ago) 5d",
        "tigera-operator tigera-operator-1 0/1 Error 3 (20s ago) 5d",
        "longhorn-system longhorn-manager-x 0/1 CrashLoopBackOff 40 (10s ago) 5d",
        "metallb-system speaker-y 0/1 ImagePullBackOff 0 5d",
    ]
    out = run_gate({"pods": [*foreign, "dfe-local dfe-engine-0 1/1 Running 0 6d"]})
    expect(
        "four unhealthy cluster-owned pods do not fail the deploy's gate",
        out.returncode == 0,
        f"rc={out.returncode} {out.stdout}",
    )


def test_a_namespace_the_deploy_does_own_is_judged() -> None:
    """The allowlist is not a way of ignoring everything."""
    out = run_gate({"pods": ["clickhouse dfe-clickhouse-0 0/1 CrashLoopBackOff 9 (30s ago) 5d"]})
    expect(
        "a crashlooping pod in an owned namespace fails",
        out.returncode != 0 and "clickhouse/dfe-clickhouse-0" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_the_app_namespace_joins_the_allowlist() -> None:
    """DFE_NAMESPACE is a deployment's own choice and need not start with dfe-."""
    pods = ["analytics dfe-engine-0 0/1 CrashLoopBackOff 9 (30s ago) 5d"]
    workloads = {"pods": pods, "ns_workloads": ["dfe-engine 1/1 1 1 5d"]}
    unset = run_gate(workloads)
    expect(
        "an unnamed namespace is not judged",
        unset.returncode == 0,
        f"rc={unset.returncode} {unset.stdout}",
    )
    named = run_gate(workloads, DFE_NS="analytics")
    expect(
        "the same namespace is judged once DFE_NS names it",
        named.returncode != 0 and "analytics/dfe-engine-0" in named.stdout,
        f"rc={named.returncode} {named.stdout}",
    )


def test_the_presence_check_still_fires() -> None:
    """A deploy that produced nothing passes every check that judges what exists."""
    out = run_gate({"pods": ["dfe-local dfe-engine-0 1/1 Running 0 6d"]}, DFE_NS="dfe-local")
    expect(
        "an app namespace with no workloads fails",
        out.returncode != 0 and "NO app workloads" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_an_unready_workload_in_an_owned_namespace_fails() -> None:
    """Replica counts are the half a pod scan cannot see."""
    out = run_gate(
        {
            "pods": ["dfe-local dfe-engine-0 1/1 Running 0 6d"],
            "deployments": ["dfe-local dfe-ui 2 1"],
            "ns_workloads": ["dfe-ui 1/2 2 1 5d"],
        },
        DFE_NS="dfe-local",
    )
    expect(
        "a deployment short of desired replicas fails",
        out.returncode != 0 and "deployment dfe-local/dfe-ui 1/2 ready" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


# A converged single-pod deploy: every other check passes, so the credential
# verdict is the only thing left to decide the exit code.
HEALTHY = {
    "pods": ["dfe-local dfe-engine-0 1/1 Running 0 6d"],
    "ns_workloads": ["dfe-engine 1/1 1 1 6d"],
}


def test_default_credentials_fail_a_non_dev_deploy() -> None:
    """A healthy stack on the shipped admin password is open, not up (#233).

    The engine only reaches this state in a dev posture, so a deploy declaring
    any other posture and still reporting it has not taken the minted password.
    """
    out = run_gate({**HEALTHY, "setup_status": "True"}, DFE_NS="dfe-local", DFE_ENV="production")
    expect(
        "default_credentials in a production posture fails the gate",
        out.returncode != 0 and "default_credentials" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_a_dev_posture_may_run_the_default() -> None:
    """Tyre-kicking a dev deploy on a known password is the point of the exception."""
    out = run_gate({**HEALTHY, "setup_status": "True"}, DFE_NS="dfe-local", DFE_ENV="local")
    expect(
        "a dev posture on the default passes, and says why",
        out.returncode == 0 and "dev posture" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_an_answer_with_surrounding_whitespace_is_still_read() -> None:
    """The engine strips before comparing; an exact compare here would not match."""
    out = run_gate(
        {**HEALTHY, "setup_status": "  True  "}, DFE_NS="dfe-local", DFE_ENV="production"
    )
    expect(
        "a padded True still fails a production posture",
        out.returncode != 0 and "default_credentials" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_a_minted_password_passes_in_any_posture() -> None:
    """The check judges the credential, not the posture."""
    out = run_gate({**HEALTHY, "setup_status": "False"}, DFE_NS="dfe-local", DFE_ENV="production")
    expect(
        "default_credentials false passes",
        out.returncode == 0 and "minted admin password" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_an_engine_that_serves_no_such_field_warns_rather_than_fails() -> None:
    """An image older than dfe-engine #300 answers 200 without the field.

    The probe RAN, so the gate knows the engine is reachable and only the
    contract is missing; blocking every deploy on that would be worse.
    """
    out = run_gate({**HEALTHY, "setup_status": "unknown"}, DFE_NS="dfe-local", DFE_ENV="production")
    expect(
        "a missing field warns and passes",
        out.returncode == 0 and "did not answer" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


# The probe cannot run: an RBAC denial on exec, a wrong READINESS_ENGINE_TARGET,
# and a non-200 from setup-status all reach the gate as a non-zero exec.
UNRUNNABLE = {
    "rbac denial": 'Error from server (Forbidden): pods "dfe-engine-0" is forbidden',
    "wrong target": 'Error from server (NotFound): deployments.apps "dfe-engine" not found',
    "a non-200 from setup-status": "urllib.error.HTTPError: HTTP Error 503: Service Unavailable",
}


def test_a_probe_that_cannot_run_fails_a_non_dev_deploy() -> None:
    """The fail-open the gate shipped with: an unrunnable check passed as a warn.

    A production deploy on the shipped password passed whenever the probe could
    not run, which is every case the probe exists to catch.
    """
    for cause, message in UNRUNNABLE.items():
        out = run_gate(
            {**HEALTHY, "exec_error": message, "exec_rc": 1},
            DFE_NS="dfe-local",
            DFE_ENV="production",
        )
        expect(
            f"{cause} fails a production posture",
            out.returncode != 0 and "could not RUN" in out.stdout,
            f"rc={out.returncode} {out.stdout}",
        )


def test_a_probe_that_cannot_run_only_warns_in_a_dev_posture() -> None:
    """A dev deploy may run the shipped password, so it may also fail to prove it."""
    for cause, message in UNRUNNABLE.items():
        out = run_gate(
            {**HEALTHY, "exec_error": message, "exec_rc": 1},
            DFE_NS="dfe-local",
            DFE_ENV="local",
        )
        expect(
            f"{cause} warns in a dev posture",
            out.returncode == 0 and "could not ask" in out.stdout,
            f"rc={out.returncode} {out.stdout}",
        )


def test_the_credential_check_needs_the_namespace() -> None:
    """Without DFE_NS there is no engine to ask, the same as the presence check."""
    out = run_gate({**HEALTHY, "setup_status": "True"}, DFE_ENV="production")
    expect(
        "no namespace skips the credential check",
        out.returncode == 0 and "no DFE_NS" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


@contextmanager
def admin_ui_server(routes: dict[str, tuple[int, str]]) -> Iterator[int]:
    """A plain-HTTP stand-in for the gateway on a port the kernel picks.

    `routes` maps a path to (status, Location), and may be filled in once the
    port is known.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, location = routes.get(self.path, (404, ""))
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            """The gate's own output is what the tests read."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def healthy_with_admin_ui(port: int) -> dict:
    """A converged deploy whose gateway, at 127.0.0.1, lists one admin UI on `port`."""
    return {
        **HEALTHY,
        "setup_status": "False",
        "admin_links": [{"name": "Argo CD", "url": f"http://argocd.dfe.test:{port}"}],
        "gateways": ["127.0.0.1"],
    }


def test_an_admin_ui_that_redirects_to_itself_fails_the_gate() -> None:
    """How the Argo CD redirect loop shipped: every pod Ready, the UI unreachable."""
    routes: dict[str, tuple[int, str]] = {}
    with admin_ui_server(routes) as port:
        routes["/"] = (307, f"http://argocd.dfe.test:{port}/")
        out = run_gate(healthy_with_admin_ui(port), DFE_NS="dfe-local", DFE_ENV="local")
    expect(
        "a looping admin UI fails a gate every other check passed",
        out.returncode != 0
        and "[FAIL] admin ui Argo CD: redirect loop" in out.stdout
        and "an admin UI does not load through the gateway" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_an_admin_ui_that_loads_passes_the_gate() -> None:
    routes: dict[str, tuple[int, str]] = {"/": (200, "")}
    with admin_ui_server(routes) as port:
        out = run_gate(healthy_with_admin_ui(port), DFE_NS="dfe-local", DFE_ENV="local")
    expect(
        "a loading admin UI passes, and says so",
        out.returncode == 0 and "[PASS] admin ui Argo CD" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def test_a_gateway_listing_no_admin_ui_skips_the_check() -> None:
    """dfe-docker, or a gateway that predates dfe-admin-links, has nothing to probe."""
    out = run_gate({**HEALTHY, "setup_status": "False"}, DFE_NS="dfe-local", DFE_ENV="local")
    expect(
        "no dfe-admin-links is a named skip, not a failure",
        out.returncode == 0 and "no dfe-admin-links ConfigMap in dfe-local" in out.stdout,
        f"rc={out.returncode} {out.stdout}",
    )


def destroyed_namespaces() -> set[str]:
    """Every namespace destroy.sh removes, off its own delete calls."""
    text = DESTROY.read_text(encoding="utf-8", errors="replace")
    names: set[str] = set()
    for line in text.splitlines():
        listed = re.search(r"^for ns in (.+); do$", line.strip())
        if listed:
            names.update(listed.group(1).split())
            continue
        single = re.search(r"delete ns \"?([A-Za-z0-9-]+)\"?\s", line)
        if single:
            names.add(single.group(1))
    if 'grep "namespace/dfe-"' in text:
        names.add("dfe-*")
    return names


def watched_namespaces() -> set[str]:
    """The gate's default allowlist, off its own WATCH_NS assignment."""
    text = GATE.read_text(encoding="utf-8", errors="replace")
    default = re.search(r'WATCH_NS="\$\{READINESS_WATCH_NS:-(.+?)\}"', text)
    return set(default.group(1).split()) if default else set()


def test_the_gate_watches_exactly_what_the_teardown_removes() -> None:
    """The allowlist is a hand copy of destroy.sh's lists and drifts silently.

    A namespace added to the teardown but not here is created by the deploy and
    never judged, so the gate passes a deploy whose workloads are crashlooping.
    """
    destroyed, watched = destroyed_namespaces(), watched_namespaces()
    expect("both lists parse", bool(destroyed) and bool(watched), f"{destroyed} / {watched}")
    expect(
        "every namespace the teardown removes is judged",
        destroyed - watched == set(),
        f"unwatched: {sorted(destroyed - watched)}",
    )
    expect(
        "and the gate judges nothing the teardown leaves behind",
        watched - destroyed == set(),
        f"undestroyed: {sorted(watched - destroyed)}",
    )


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
