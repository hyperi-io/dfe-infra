#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         probe_egress.py
#  Purpose:      Prove from INSIDE a running pod that it can open a TCP
#                connection to a given host:port, so an egress policy is
#                verified by a connection rather than by a rendered manifest.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""probe_egress.py -- does this app's pod actually reach that endpoint?

A rendered NetworkPolicy proves nothing about reachability. A default-deny drop
looks exactly like a slow endpoint from outside the cluster, so the only honest
check runs in the pod that the policy selects.

Read-only: it execs a probe in an EXISTING pod and installs nothing. A pod with
no usable probe tool is reported as NO-PROBE, never as a pass.

Results:
  OK        the TCP handshake completed
  REFUSED   packets arrived and nothing was listening -- egress is NOT the fault
  TIMEOUT   no answer, which is what a NetworkPolicy drop looks like
  NO-PROBE  the container has no python3, bash, nc or curl to probe with

GENERIC: app, namespace, targets, label key and kube context are all flags, so it
serves any deployment and any cluster. Nothing environment-specific is compiled in.

Usage:
    python3 scripts/probe_egress.py --app APP --namespace NS --target HOST:PORT ...

Examples (RFC 5737 / RFC 2606 documentation addresses, never a real estate):
    # External ClickHouse from the loader, both TLS ports:
    python3 scripts/probe_egress.py --context dfe --namespace dfe-local \\
        --app dfe-loader --target ch.example.com:9440 --target ch.example.com:8443

    # External Kafka from every receiver pod:
    python3 scripts/probe_egress.py --namespace dfe-local --app dfe-receiver \\
        --target broker.example.com:9096 --all-pods

    # A pod that does not carry the DFE label convention:
    python3 scripts/probe_egress.py --namespace otel --label app=collector \\
        --target otlp.example.com:4317

Exit status is non-zero if any probe is anything but OK.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys

DEFAULT_LABEL_KEY = "app.kubernetes.io/name"
# Bounded so a dropped packet reports TIMEOUT instead of hanging the run.
DEFAULT_TIMEOUT = 5
# Headroom over the in-pod connect timeout for exec setup and image start-up.
EXEC_OVERHEAD = 20

OK, REFUSED, TIMEOUT, NO_PROBE, ERROR = "OK", "REFUSED", "TIMEOUT", "NO-PROBE", "ERROR"


def kubectl(args: list[str], context: str | None, timeout: int) -> subprocess.CompletedProcess:
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += args
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)


def running_pods(namespace: str, selector: str, context: str | None) -> list[str]:
    """Pod names matching the selector, Running phase only."""
    out = kubectl(
        ["get", "pods", "-n", namespace, "-l", selector, "-o", "json", "--request-timeout=30s"],
        context,
        60,
    )
    if out.returncode != 0:
        raise SystemExit(f"kubectl get pods failed: {out.stderr.strip()}")
    pods = json.loads(out.stdout).get("items", [])
    return [
        p["metadata"]["name"]
        for p in pods
        if p.get("status", {}).get("phase") == "Running"
    ]


def _python_probe(host: str, port: int, timeout: int) -> str:
    """A socket connect that names its own outcome, so nothing is inferred."""
    return (
        "import socket,sys\n"
        f"s=socket.socket();s.settimeout({timeout})\n"
        "try:\n"
        f"    s.connect(({host!r},{port}))\n"
        "    print('DFE_EGRESS_OK')\n"
        "except socket.timeout:\n"
        "    print('DFE_EGRESS_TIMEOUT')\n"
        "except ConnectionRefusedError:\n"
        "    print('DFE_EGRESS_REFUSED')\n"
        "except OSError as e:\n"
        "    print('DFE_EGRESS_ERROR ' + str(e))\n"
        "finally:\n"
        "    s.close()\n"
    )


def probe_commands(host: str, port: int, timeout: int) -> list[tuple[str, list[str]]]:
    """The probe ladder, most precise first. A missing tool falls to the next."""
    src = _python_probe(host, port, timeout)
    quoted = shlex.quote(f"{host}:{port}")
    return [
        ("python3", ["python3", "-c", src]),
        ("python", ["python", "-c", src]),
        # /dev/tcp is a bash feature, absent from POSIX sh.
        ("bash", ["bash", "-c", f"exec 3<>/dev/tcp/{host}/{port} && echo DFE_EGRESS_OK"]),
        ("nc", ["nc", "-z", "-w", str(timeout), host, str(port)]),
        ("curl", ["curl", "-s", "-o", "/dev/null", "--connect-timeout", str(timeout),
                  f"telnet://{quoted}"]),
    ]


def _tool_missing(result: subprocess.CompletedProcess) -> bool:
    text = (result.stderr or "") + (result.stdout or "")
    return result.returncode == 127 or "executable file not found" in text or (
        "not found" in text and "no such file" in text.lower()
    )


def classify(tool: str, result: subprocess.CompletedProcess) -> str | None:
    """None means the tool is absent -- try the next rung."""
    if _tool_missing(result):
        return None
    if "DFE_EGRESS_OK" in result.stdout:
        return OK
    if "DFE_EGRESS_TIMEOUT" in result.stdout:
        return TIMEOUT
    if "DFE_EGRESS_REFUSED" in result.stdout:
        return REFUSED
    if "DFE_EGRESS_ERROR" in result.stdout:
        return ERROR
    if result.returncode == 0:
        return OK
    # nc and curl report failure without saying why. curl 28 is its connect
    # timeout; everything else here is indistinguishable from a refusal.
    if tool == "curl" and result.returncode == 28:
        return TIMEOUT
    return REFUSED if tool in ("nc", "curl", "bash") else ERROR


def probe(pod: str, namespace: str, host: str, port: int, timeout: int,
          context: str | None, container: str | None) -> tuple[str, str]:
    """Return (result, tool). Walks the ladder until a tool is present."""
    for tool, argv in probe_commands(host, port, timeout):
        args = ["exec", pod, "-n", namespace]
        if container:
            args += ["-c", container]
        args += ["--", *argv]
        try:
            out = kubectl(args, context, timeout + EXEC_OVERHEAD)
        except subprocess.TimeoutExpired:
            return TIMEOUT, tool
        verdict = classify(tool, out)
        if verdict is not None:
            return verdict, tool
    return NO_PROBE, "-"


def parse_target(target: str) -> tuple[str, int]:
    host, _, port = target.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"--target must be HOST:PORT, got {target!r}")
    return host, int(port)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--namespace", "-n", required=True, help="namespace the pod runs in")
    parser.add_argument("--app", help=f"value of {DEFAULT_LABEL_KEY} identifying the app")
    parser.add_argument("--label", help="full label selector, instead of --app")
    parser.add_argument("--label-key", default=DEFAULT_LABEL_KEY,
                        help=f"label key --app matches on (default {DEFAULT_LABEL_KEY})")
    parser.add_argument("--target", action="append", required=True, metavar="HOST:PORT",
                        help="endpoint to reach; repeat for several")
    parser.add_argument("--context", help="kubectl context (default: current)")
    parser.add_argument("--container", "-c", help="container to exec in (default: first)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"per-connect timeout in seconds (default {DEFAULT_TIMEOUT})")
    parser.add_argument("--all-pods", action="store_true",
                        help="probe every matching pod, not just the first")
    args = parser.parse_args()

    if not args.app and not args.label:
        parser.error("one of --app or --label is required")
    selector = args.label or f"{args.label_key}={args.app}"
    # Argument validation before the first cluster call, so a typo costs nothing.
    targets = [parse_target(t) for t in args.target]

    pods = running_pods(args.namespace, selector, args.context)
    if not pods:
        print(f"NO PODS  {selector} in {args.namespace} has no Running pod")
        return 1
    if not args.all_pods:
        pods = pods[:1]

    failures = 0
    for pod in pods:
        for host, port in targets:
            verdict, tool = probe(pod, args.namespace, host, port, args.timeout,
                                  args.context, args.container)
            if verdict != OK:
                failures += 1
            print(f"{verdict:9} {pod} -> {host}:{port}  (via {tool})")

    if failures:
        print(f"\n{failures} probe(s) did not connect")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
