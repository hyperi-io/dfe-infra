#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         acceptance/edge/run.py
#  Purpose:      Send from an allow-listed address outside the cloud to the
#                receiver's public door, and prove every hop: the door opens on
#                the list, the send is accepted, it lands in main, the console
#                and HyperDX both find it, and a send from the cluster's own
#                egress is refused.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage:
#    python3 scripts/acceptance/edge/run.py \
#        --ui-url https://dfe.example --engine-url https://dfe.example \
#        --namespace dfe --kubectl "kubectl --kubeconfig .tmp/run.kubeconfig" \
#        --allow 203.0.113.7/32 --egress-cidrs 198.51.100.1/32,198.51.100.2/32 \
#        --gateway-address gateway.example --hyperdx-url https://hyperdx.example --insecure
#    (dfe-ops acceptance --suite edge writes the allow list to the deploy repo
#     first, restores it after, and exports the datastore and the admin login as
#     the DFE_E2E_* env)
"""edge.run -- ingest from outside the cloud, the owner's definition of working.

One row per claim, in the order they happen: the receiver's public door opens
on the allow list, a send from the allow-listed address is accepted, its row
lands in main, the console's Observe search finds it, HyperDX's own search finds
it, the engine's minted secrets sit in Secrets Manager on the deployment key
(skipped off AWS), and a send from the cluster's NAT egress, which the list does
not name, is refused. A row that cannot run because one before it failed is recorded failed,
never skipped, so the table cannot be read as green.

This file is the order the rows happen in. What each decides is in ``steps``.
"""

import argparse
import os
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from acceptance.clients import Datastore, Engine
from acceptance.edge import steps as edge
from acceptance.onboarding import run as onboarding
from acceptance.onboarding import wizard
from acceptance.source import steps as source_steps
from acceptance.source.run import report

__all__ = ["build_parser", "ingest_rows", "main", "report", "run"]


def ingest_rows(results: list[wizard.StepResult], kube: list[str], namespace: str, allow: list[str],
                store: Datastore, run_id: str) -> tuple[edge.Endpoint | None, bool, bool]:
    """The public door, the allow-listed send and the row in main.

    Returns:
        The endpoint (None when none came up), whether the send was accepted,
        and whether its row landed.
    """
    endpoint, detail = edge.wait_endpoint(kube, namespace, allow, edge.ENDPOINT_DEADLINE)
    edge.row(results, "public-endpoint", "done" if endpoint else "failed", detail)
    if endpoint is None:
        edge.row(results, "accepted", "failed", "not sent: no public endpoint came up")
        edge.row(results, "landed", "failed", "nothing was sent to land")
        return None, False, False
    accepted, detail = edge.post_marker(endpoint.ingest_url, edge.marker_record(run_id), edge.ACCEPT_DEADLINE)
    edge.row(results, "accepted", "done" if accepted else "failed", detail)
    if not accepted:
        edge.row(results, "landed", "failed", "nothing was accepted to land")
        return endpoint, False, False
    return endpoint, True, edge.record_landed(results, store, run_id, edge.LANDED_DEADLINE)


def console_rows(args: argparse.Namespace, results: list[wizard.StepResult], engine: Engine,
                 admin_user: str, password: str, new_password: str, run_id: str) -> None:
    """Sign in to the console and search the main source for the marker on its Observe page."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        edge.row(results, "observe", "failed", (
            "playwright is not installed: python3 -m pip install --require-hashes -r "
            "scripts/acceptance/requirements.txt"
        ))
        return
    with sync_playwright() as play:
        browser = play.chromium.launch(
            channel=args.channel, headless=not args.headed, args=onboarding.resolver_args(args.resolve)
        )
        context = browser.new_context(viewport={"width": 1440, "height": 900}, ignore_https_errors=args.insecure)
        driver = onboarding.Driver(context.new_page(), args.ui_url, Path(args.shots_dir))
        driver.results = results
        try:
            source_steps.open_console_settled(
                driver, engine, admin_user, password, args.org, args.first_user, new_password
            )
            source_steps.record_observe(driver, edge.MAIN_SOURCE, marker=run_id)
        except Exception as exc:  # a Playwright timeout IS the finding
            driver.record("observe", "failed", source_steps.failure_line(exc))
        finally:
            context.close()
            browser.close()


def run(args: argparse.Namespace) -> int:
    admin_user = os.environ.get("DFE_E2E_ENGINE_USER", "admin")
    password = os.environ.get("DFE_E2E_ADMIN_PASSWORD", "")
    if not password:
        print("edge: no login; run through dfe-ops acceptance --suite edge, which exports it", file=sys.stderr)
        return 2
    store = Datastore.from_env(os.environ)
    verify = not args.insecure
    # Straight to the engine when a forward is up; the console still goes through the gateway.
    api_url = os.environ.get("DFE_E2E_ENGINE_URL") or args.engine_url
    new_password = os.environ.get("DFE_E2E_ADMIN_NEW_PASSWORD", "")
    engine = Engine(api_url, admin_user, password, verify or api_url != args.engine_url, new_password=new_password)
    kube = shlex.split(args.kubectl)
    # The HyperDX host is the gateway's, which this machine may not resolve on its own.
    onboarding._apply_resolve_map(args.resolve)
    run_id = edge.new_run_id()
    print(f"  edge run {run_id}: sending from {', '.join(args.allow)}", file=sys.stderr)

    results: list[wizard.StepResult] = []
    endpoint, accepted, landed = ingest_rows(results, kube, args.namespace, args.allow, store, run_id)
    if landed:
        console_rows(args, results, engine, admin_user, password, new_password, run_id)
        edge.record_hyperdx(results, args.hyperdx_url, engine, run_id, verify, edge.HYPERDX_DEADLINE)
    else:
        edge.row(results, "observe", "failed", "not searched: nothing landed to find")
        edge.row(results, "hyperdx", "failed", "not searched: nothing landed to find")
    edge.record_minted_secrets(results, kube)
    if endpoint is not None and accepted:
        edge.record_refused(
            results, kube, args.namespace, endpoint, args.gateway_address, args.allow,
            edge.split_cidrs(args.egress_cidrs), run_id,
            console_url=args.ui_url, receiver_deployment=args.receiver_deployment,
        )
    else:
        edge.row(results, "refused", "failed", (
            "not probed: without an accepted send a refusal cannot tell the list from a dead load balancer"
        ))

    shots = Path(args.shots_dir)
    held = (password, new_password, store.password, engine.password, engine.token)
    print()
    print(report(results, shots, secrets=held))
    print(f"\nscreenshots: {shots}")
    return wizard.exit_code(results)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="edge",
        description="Send from an allow-listed address to the receiver's public door and prove it end to end.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--ui-url", required=True, help="console base URL")
    parser.add_argument("--engine-url", required=True, help="engine API base URL")
    parser.add_argument("--namespace", required=True, help="namespace the receiver runs in")
    parser.add_argument("--kubectl", required=True, metavar="PREFIX",
                        help="kubectl and the flags that aim it at the deployment's cluster, as one string")
    parser.add_argument("--allow", action="append", required=True, metavar="CIDR",
                        help="every network the ingest allow list names, this run's sender among them (repeatable)")
    parser.add_argument("--egress-cidrs", required=True, metavar="CIDRS",
                        help="the cluster's NAT egress, comma separated: where a send from inside it comes from")
    parser.add_argument("--gateway-address", default="",
                        help="the gateway load balancer's address, which the in-cluster probe reaches as its control, "
                             "asking for the console's own hostname")
    parser.add_argument("--hyperdx-url", default="", help="HyperDX's URL through the gateway")
    parser.add_argument("--receiver-deployment", default="dfe-receiver",
                        help="the receiver's Deployment, whose image and placement the probe pod borrows")
    parser.add_argument("--org", default="acceptance", help="organisation to create when the deployment still owes its setup wizard")
    parser.add_argument("--first-user", default="operator", help="first user to create when the deployment still owes its setup wizard")
    parser.add_argument("--shots-dir", default=".tmp/edge", help="where the step table and screenshots go")
    parser.add_argument("--channel", default="chrome", help="browser channel; chrome is the testing browser")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--insecure", action="store_true", help="accept a certificate this machine does not trust")
    parser.add_argument("--resolve", action="append", default=[], metavar="HOST:IP", type=onboarding._host_ip,
                        help="resolve HOST to IP inside the browser and this run (repeatable)")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
