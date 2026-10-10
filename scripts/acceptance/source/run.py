#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         acceptance/source/run.py
#  Purpose:      Create a real source through the console (meta schema, receiver
#                match, transform, archive), feed it real data, and prove every
#                hop: its own table, the transform's columns, the archive.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
#
#  Usage:
#    python3 scripts/acceptance/source/run.py \
#        --ui-url https://dfe.example --engine-url https://dfe.example \
#        --engine-repo /path/to/dfe-engine --shots-dir .tmp/source --insecure
#    (the receiver, the datastore and the admin login come from the DFE_E2E_*
#     env that dfe-ops acceptance --suite source exports; --access-summary
#     is the stand-alone alternative for the login)
"""source.run -- the post-deploy source test, in a browser.

Runs AFTER the deploy's own POST has passed. It is the first thing an operator
does with a working deployment: add a source for real data, and see it land.

This file is the order the steps happen in and nothing else. What every source
has in common is in ``steps``; what is true of one kind of source is in
``cases``. Every step is a row in the report and a screenshot; a step the
console cannot do falls back to the engine API and says so, because that is a
finding about the console.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import access_summary as access_summary_file

from acceptance.clients import Datastore, Engine, companion, remove_source
from acceptance.onboarding import run as onboarding
from acceptance.onboarding import wizard
from acceptance.source import cases, fetcher, steps

# The report's file name in the screenshot directory.
STEP_TABLE = "steps.txt"


def walk(run: cases.Run, case: cases.Case, archive_exec: list[list[str]], restart_exec: list[str]) -> None:
    """Every source's journey, in one order, with the case filling in its own halves."""
    case.create(run)
    deployed = steps.record_deploy(run.driver, run.engine, case.name)
    steps.record_hyperdx_source(run.driver, run.engine, case.name, deployed)
    hints = case.provision(run)
    # After the case's files, not before: a restart that beats them to disk
    # leaves the app idling on a transform it has no program for.
    steps.apply_restarts(
        run.driver, restart_exec, [*(deployed.get("restart_required") or []), *hints]
    )
    steps.record_table(run.driver, run.store, case.name)
    steps.record_instance_up(
        run.driver, run.engine, run.store, case.service, case.name,
        case.instance_step, case.instance_deadline,
        case.reporting_step, case.reporting_deadline,
    )
    case.feed(run)
    case.prove(run)
    steps.record_archive(run.driver, archive_exec, case.name)
    steps.record_observe(run.driver, case.name)


def teardown(run: cases.Run, case: cases.Case, api_url: str) -> None:
    """Remove what the run made, and prove HyperDX lost the view with it."""
    driver = run.driver
    try:
        created = run.engine.call("GET", f"/sources/{case.name}").status == 200
    except (OSError, RuntimeError) as exc:
        # The step table is what this run produces; an engine that has gone away or
        # refused the login is a finding to print, not a traceback that loses every row above it.
        refused = f"the engine did not serve the run: {exc}"
        driver.results.append(wizard.StepResult("teardown", "failed", refused))
        return
    if created and not run.args.keep:
        detail = remove_source(
            api_url, run.engine.verify, run.engine.token or "", case.name, onboarding.TEARDOWN_DEADLINE
        )
        driver.results.append(
            wizard.StepResult("teardown", "done" if detail.startswith("removed") else "failed", detail)
        )
        # A delete that leaves the source behind gives every team a view over a
        # table that no longer exists.
        state, detail = steps.assert_hyperdx_source_gone(run.engine, case.name)
        driver.results.append(wizard.StepResult("hyperdx-source-removed", state, detail))
    elif created:
        driver.results.append(wizard.StepResult("teardown", "kept", f"{case.name} left deployed (--keep)"))
    driver.results.extend(case.cleanup(run))


def run(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    admin_user = os.environ.get("DFE_E2E_ENGINE_USER", "admin")
    password = os.environ.get("DFE_E2E_ADMIN_PASSWORD", "")
    if args.access_summary:
        rows = access_summary_file.parse(Path(args.access_summary).read_text(encoding="utf-8"))
        admin_user, password = rows[access_summary_file.ADMIN_LABEL]
    if not password:
        print("source: no login; pass --access-summary or run through dfe-ops acceptance", file=sys.stderr)
        return 2
    store = Datastore(
        host=os.environ.get("DFE_E2E_CH_HOST", ""),
        port=int(os.environ.get("DFE_E2E_CH_PORT", "8123")),
        user=os.environ.get("DFE_E2E_CH_USER", "default"),
        password=os.environ.get("DFE_E2E_CH_PASSWORD", ""),
        database=os.environ.get("DFE_E2E_CH_DB", "dfe"),
    )
    case = cases.build(args)
    engine_repo = Path(args.engine_repo).resolve()
    if args.transform_repo:
        transform_repo = Path(args.transform_repo).resolve()
    elif case.companion_repo:
        transform_repo = companion(engine_repo, case.companion_repo)
    else:
        transform_repo = engine_repo
    # The vector case reads its program out of its own app's checkout and the
    # corpus out of dfe-transform-vrl, so one --transform-repo cannot serve both.
    if args.program_repo:
        program_repo = Path(args.program_repo).resolve()
    elif case.program_repo:
        program_repo = companion(engine_repo, case.program_repo)
    else:
        program_repo = None
    verify = not args.insecure
    # The API calls go straight to the engine when a forward is up: a deploy or a
    # delete can outlast a gateway's timeout. The console still goes through the gateway.
    api_url = os.environ.get("DFE_E2E_ENGINE_URL") or args.engine_url
    # What a fresh deployment's admin is changed to at its forced first-login change.
    new_password = os.environ.get("DFE_E2E_ADMIN_NEW_PASSWORD", "")
    engine = Engine(
        api_url, admin_user, password, verify or api_url != args.engine_url,
        new_password=new_password,
    )
    archive_exec = [shlex.split(one) for one in args.archive_exec]
    restart_exec = shlex.split(args.restart_exec)

    shots = Path(args.shots_dir)
    with sync_playwright() as play:
        browser = play.chromium.launch(
            channel=args.channel, headless=not args.headed, args=onboarding.resolver_args(args.resolve)
        )
        context = browser.new_context(viewport={"width": 1440, "height": 900}, ignore_https_errors=args.insecure)
        driver = onboarding.Driver(context.new_page(), args.ui_url, shots)
        current = cases.Run(
            driver=driver, engine=engine, store=store, args=args, name=case.name,
            run_id=f"src-{uuid.uuid4().hex[:12]}", engine_repo=engine_repo,
            transform_repo=transform_repo, program_repo=program_repo,
            receiver_url=os.environ.get("DFE_E2E_RECEIVER_URL", ""), verify=verify,
        )
        try:
            try:
                steps.open_console(
                    driver, engine, admin_user, password, args.org, args.first_user, new_password
                )
            except Exception as exc:
                # Chrome reconfigures its certificate verifier right after
                # launch and fails the first navigation with ERR_CERT_VERIFIER_CHANGED.
                if "ERR_CERT_VERIFIER_CHANGED" not in str(exc):
                    raise
                driver.page.wait_for_timeout(3000)
                steps.open_console(
                    driver, engine, admin_user, password, args.org, args.first_user, new_password
                )
            driver.record("sweep", "done", steps.sweep_strays(engine, verify))
            walk(current, case, archive_exec, restart_exec)
        except Exception as exc:  # a Playwright timeout IS the finding
            driver.record("run", "failed", f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
        finally:
            context.close()
            browser.close()

    teardown(current, case, api_url)

    held = (password, new_password, store.password, engine.password, engine.token)
    print()
    print(report(driver.results, shots, secrets=held))
    print(f"\nscreenshots: {shots}")
    return wizard.exit_code(driver.results)


def report(results: list[wizard.StepResult], shots: Path, secrets: tuple[str, ...] = ()) -> str:
    """The step table plus a line naming each failed and each unproven row.

    Also written to STEP_TABLE beside the screenshots, so the two travel together
    as one record of the run. A detail is free text, an exception's first line
    among them, and the table is printed to a job log that can be public, so
    every secret the run holds is replaced before either copy is made.

    Args:
        results: The run's step rows, in the order they happened.
        shots: The screenshot directory.
        secrets: The run's passwords and token; an empty one is skipped.

    Returns:
        The report text.
    """
    lines = [wizard.report_table(results)]
    failed = [row.slug for row in results if row.status == "failed"]
    if failed:
        # By name, so a stage summary that says only "failed" points at the step that did.
        lines.append(f"\nFAILED: {', '.join(failed)}")
    unproven = [row.slug for row in results if row.status == "unproven"]
    if unproven:
        # A row that ran and could not decide is neither a pass nor a failure,
        # and is said again here so a long table cannot be read as green.
        lines.append(f"\nUNPROVEN: {', '.join(unproven)} -- read the detail before claiming the source works")
    text = wizard.redact("\n".join(lines), secrets)
    shots.mkdir(parents=True, exist_ok=True)
    (shots / STEP_TABLE).write_text(text + "\n", encoding="utf-8")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="source",
        description="Create a source through the console and prove it end to end.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--ui-url", required=True, help="console base URL")
    parser.add_argument("--engine-url", required=True, help="engine API base URL")
    parser.add_argument("--engine-repo", required=True, help="dfe-engine checkout (the corpus wrapper lives in its e2e tests)")
    parser.add_argument("--transform-repo", default="", help="checkout holding the corpus archive; every pushed case names dfe-transform-vrl (default: the case's own, beside the engine repo)")
    parser.add_argument("--program-repo", default="", help="checkout holding the program a case uploads, when its app ships one of its own; only the vector case needs it (default: the corpus checkout above)")
    parser.add_argument("--access-summary", default="", metavar="FILE", help="the deploy's own summary, for the login when not run through dfe-ops")
    parser.add_argument("--org", default="acceptance", help="organisation to create when the deployment still owes its setup wizard")
    parser.add_argument("--first-user", default="operator", help="first user to create when the deployment still owes its setup wizard")
    parser.add_argument("--archive-exec", action="append", default=[], metavar="PREFIX",
                        help="command prefix that runs a shell inside one archiver replica, for the "
                             "archive assertion; repeatable, one per replica (k8s: kubectl -n <ns> "
                             "exec <pod> --; docker: docker exec dfe-archiver)")
    parser.add_argument("--restart-exec", default="", metavar="PREFIX",
                        help="command prefix that restarts one app by service name, for the apps the "
                             "engine reports it cannot apply where they stand (docker: docker "
                             "restart); unused on Kubernetes, where the controller rolls the pod")
    parser.add_argument("--case", default="filebeat", choices=tuple(cases.CASES),
                        help="filebeat pushes real lines at the receiver through the bundled VRL; "
                             "vector pushes the same lines through the same program run by a "
                             "supervised Vector; elastic pushes cisco_ios lines at a transform "
                             "compiled into its app; cloudwatch authors a meta schema and lets a "
                             "fetcher pull an AWS upstream")
    parser.add_argument("--aws-service", default="cloudwatch_logs", choices=tuple(sorted(fetcher.AWS_CASES)),
                        help="the AWS service the cloudwatch case's fetcher polls")
    parser.add_argument("--aws-region", default=os.environ.get("DFE_E2E_AWS_REGION", ""),
                        help="region the fetcher signs for (default: DFE_E2E_AWS_REGION)")
    parser.add_argument("--aws-log-group", default=os.environ.get("DFE_E2E_AWS_LOG_GROUP", ""),
                        help="CloudWatch log group to poll (default: DFE_E2E_AWS_LOG_GROUP)")
    parser.add_argument("--poll-interval-secs", type=int, default=60,
                        help="how often the fetcher polls its upstream")
    parser.add_argument("--per-module", type=int, default=20, help="corpus lines per filebeat module to feed")
    parser.add_argument("--via", default="post", choices=("post", "logstash"),
                        help="how the pushed cases reach the receiver: post wraps each corpus line "
                             "and POSTs it; logstash stands a real filebeat and logstash pair beside "
                             "a compose deployment and pushes the envelope a deployment sends")
    parser.add_argument("--beats-network", default="", metavar="NETWORK",
                        help="docker network the stack runs on, which the --via logstash pair joins")
    parser.add_argument("--beats-receiver-url", default="http://dfe-receiver:8080/ingest",
                        metavar="URL", help="the receiver's ingest URL as seen from that network")
    parser.add_argument("--shots-dir", default=".tmp/source", help="where the per-step screenshots go")
    parser.add_argument("--channel", default="chrome", help="browser channel; chrome is the testing browser")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--keep", action="store_true", help="leave the source deployed")
    parser.add_argument("--insecure", action="store_true", help="accept a certificate this machine does not trust")
    parser.add_argument("--resolve", action="append", default=[], metavar="HOST:IP", type=onboarding._host_ip,
                        help="resolve HOST to IP inside the browser (repeatable)")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
