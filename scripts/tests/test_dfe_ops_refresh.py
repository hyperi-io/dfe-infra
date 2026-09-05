#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_dfe_ops_refresh.py
#  Purpose:      Prove `dfe-ops refresh` waits for the syncs it triggered and
#                refuses a smoke run with no namespace to run it in.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Behaviour tests for the refresh subcommand of scripts/dfe-ops.

Two failures a live run produced. The readiness gate exits on its FIRST clean
poll, so run straight after the annotate it passed against the pods the previous
revision left behind -- nothing compared any Application to the pushed commit.
And with no --env-file the smoke stage ran with an empty namespace, where every
dfe-namespace suite fails on `namespaces "dfe" not found`.

A fake `kubectl` first on PATH answers from a canned reading, so no cluster is
involved and the flip from OutOfSync to Synced is scripted.

    python3 scripts/tests/test_dfe_ops_refresh.py

No third-party deps and no test runner, matching the CLI it tests.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dfe-ops"

# dfe-ops carries no extension, so it is loaded by path rather than imported.
_loader = importlib.machinery.SourceFileLoader("dfeops", str(SCRIPT))
_spec = importlib.util.spec_from_loader("dfeops", _loader)
dfeops = importlib.util.module_from_spec(_spec)
sys.modules["dfeops"] = dfeops
_loader.exec_module(dfeops)

TRACKED = "https://git.example.com/dfe/deploy.git"
# The dfe-cluster secret a normal deploy carries; None makes the fake read fail.
TRACKED_SECRET = {"metadata": {"annotations": {"dfe.hyperi.io/config_repo_url": TRACKED}}}

# Records every invocation, and serves Application JSON that flips to Synced on
# the call named by FAKE_KUBECTL_FLIP_AT.
FAKE_KUBECTL = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
calls = os.environ["FAKE_KUBECTL_CALLS"]
with open(calls, "a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\\n")
with open(calls, encoding="utf-8") as fh:
    nth = len([ln for ln in fh if "applications" in ln and "annotate" not in ln])

if "annotate" in args:
    print("application.argoproj.io/dfe-engine annotated")
    sys.exit(0)
with open(os.environ["FAKE_KUBECTL_FIXTURE"], encoding="utf-8") as fh:
    fixture = json.load(fh)
if "secret" in args:
    if fixture["secret"] is None:
        print('Error from server (Forbidden): secrets "dfe-cluster" is forbidden', file=sys.stderr)
        sys.exit(1)
    print(json.dumps(fixture["secret"]))
    sys.exit(0)
flip_at = int(os.environ.get("FAKE_KUBECTL_FLIP_AT", "0"))
key = "after" if flip_at and nth >= flip_at else "before"
print(json.dumps({"items": fixture[key]}))
"""


def app(
    name: str, repo: str, sync: str = "Synced", health: str = "Healthy", phase: str = "Succeeded"
) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"source": {"repoURL": repo}},
        "status": {
            "sync": {"status": sync},
            "health": {"status": health},
            "operationState": {"phase": phase},
        },
    }


_failures = 0


def expect(name: str, condition: bool, detail: str = "") -> None:
    global _failures
    if condition:
        print(f"PASS  {name}")
    else:
        _failures += 1
        print(f"FAIL  {name}  {detail}")


class FakeCluster:
    """A temp dir holding the fake kubectl, its fixture and its call log."""

    def __init__(
        self,
        before: list[dict],
        after: list[dict] | None = None,
        flip_at: int = 0,
        secret: dict | None = TRACKED_SECRET,
    ) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        kubectl = self.dir / "kubectl"
        kubectl.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
        kubectl.chmod(0o755)
        self.calls = self.dir / "calls.log"
        fixture = self.dir / "fixture.json"
        fixture.write_text(
            json.dumps(
                {
                    "before": before,
                    "after": after if after is not None else before,
                    "secret": secret,
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        self.env = dict(os.environ)
        self.env["PATH"] = f"{self.dir}{os.pathsep}{self.env['PATH']}"
        self.env["FAKE_KUBECTL_CALLS"] = str(self.calls)
        self.env["FAKE_KUBECTL_FIXTURE"] = str(fixture)
        self.env["FAKE_KUBECTL_FLIP_AT"] = str(flip_at)

    def called(self) -> list[str]:
        if not self.calls.exists():
            return []
        return [ln for ln in self.calls.read_text(encoding="utf-8").splitlines() if ln]

    def __enter__(self) -> FakeCluster:
        return self

    def __exit__(self, *_exc) -> None:
        self._tmp.cleanup()


@contextlib.contextmanager
def no_poll_wait() -> Iterator[None]:
    """Poll the fake cluster without sleeping, and put the interval back after."""
    saved = dfeops._SYNC_POLL_INTERVAL
    dfeops._SYNC_POLL_INTERVAL = 0
    try:
        yield
    finally:
        dfeops._SYNC_POLL_INTERVAL = saved


def refresh_args(**overrides) -> argparse.Namespace:
    """The refresh parser's own defaults, so a flag rename breaks this too."""
    args = dfeops.build_parser().parse_args(["refresh"])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def run_refresh(cluster: FakeCluster, args: argparse.Namespace) -> tuple[int, list[str]]:
    """cmd_refresh with the two shell-out stages recorded rather than run.

    The gate and the smoke suite have their own tests; running them here would
    put a whole deploy validation behind a flag check.
    """
    stages: list[str] = []

    def fake_stream(cmd: list[str], env: dict | None = None) -> int:
        stages.append(Path(cmd[-1]).name)
        return 0

    def fake_verify(_args: argparse.Namespace) -> int:
        stages.append("smoke")
        return 0

    saved_env = dict(os.environ)
    saved_stream, saved_verify = dfeops._run_streaming, dfeops.cmd_verify
    dfeops._run_streaming, dfeops.cmd_verify = fake_stream, fake_verify
    os.environ.update(cluster.env)
    os.environ.pop("DFE_NAMESPACE", None)
    try:
        return dfeops.cmd_refresh(args), stages
    finally:
        dfeops._run_streaming, dfeops.cmd_verify = saved_stream, saved_verify
        os.environ.clear()
        os.environ.update(saved_env)


def test_a_namespaceless_refresh_is_refused_before_anything_is_annotated() -> None:
    """Observed live: every dfe-namespace suite failed on `namespaces "dfe" not found`.

    The refusal has to come before the annotate, or a run that cannot finish has
    already asked the cluster to sync.
    """
    with FakeCluster([app("dfe-engine", TRACKED)]) as cluster:
        rc, stages = run_refresh(cluster, refresh_args(readiness_timeout=1))
        expect("it refuses", rc == 2, f"rc={rc}")
        expect("and ran no kubectl at all", cluster.called() == [], f"{cluster.called()}")
        expect("and reached neither the gate nor the smoke suite", stages == [], f"{stages}")


def test_skip_verify_needs_no_namespace() -> None:
    """The gate alone is namespace-free, so the refusal must not reach it."""
    with FakeCluster([app("dfe-engine", TRACKED)]) as cluster:
        rc, stages = run_refresh(cluster, refresh_args(readiness_timeout=1, skip_verify=True))
        expect("the refresh runs", rc == 0, f"rc={rc}")
        expect(
            "the gate ran and the smoke suite did not",
            stages == ["smoke-test-readiness.sh"],
            f"{stages}",
        )


def test_a_named_namespace_satisfies_the_check() -> None:
    """--namespace is the other half of the message the refusal prints."""
    with FakeCluster([app("dfe-engine", TRACKED)]) as cluster:
        rc, stages = run_refresh(cluster, refresh_args(readiness_timeout=1, namespace="dfe-local"))
        expect("the refresh runs", rc == 0, f"rc={rc}")
        expect("both stages ran", stages == ["smoke-test-readiness.sh", "smoke"], f"{stages}")


def test_the_gate_does_not_start_before_the_sync_finishes() -> None:
    """The gate exits on its first clean poll, so it must not see the old pods."""
    before = [app("dfe-engine", TRACKED, sync="OutOfSync", phase="Running")]
    with FakeCluster(before, [app("dfe-engine", TRACKED)], flip_at=2) as cluster, no_poll_wait():
        rc, stages = run_refresh(cluster, refresh_args(readiness_timeout=30, namespace="dfe-local"))
        reads = [
            c for c in cluster.called() if "applications.argoproj.io" in c and "annotate" not in c
        ]
        expect("the refresh succeeds", rc == 0, f"rc={rc}")
        expect("after two readings of the Applications", len(reads) == 2, f"{reads}")
        expect(
            "and the gate still ran", stages == ["smoke-test-readiness.sh", "smoke"], f"{stages}"
        )


def test_a_sync_that_never_lands_stops_the_refresh() -> None:
    """Annotated-and-hoped is the failure the wait exists to turn into a red run."""
    with FakeCluster([app("dfe-engine", TRACKED, sync="OutOfSync")]) as cluster, no_poll_wait():
        rc, stages = run_refresh(cluster, refresh_args(readiness_timeout=0, namespace="dfe-local"))
        expect("the refresh fails", rc != 0, f"rc={rc}")
        expect("and neither later stage ran", stages == [], f"{stages}")


def test_the_wait_returns_once_the_applications_report_synced() -> None:
    """The gate's first clean poll comes before Argo has replaced any pod."""
    before = [app("dfe-engine", TRACKED, sync="OutOfSync", phase="Running")]
    after = [app("dfe-engine", TRACKED)]
    with FakeCluster(before, after, flip_at=2) as cluster, no_poll_wait():
        rc = dfeops._wait_for_sync(
            ["kubectl"], "argocd", [dfeops._normalise_repo(TRACKED)], 30, cluster.env
        )
        reads = [c for c in cluster.called() if "applications" in c]
        expect("the wait succeeds", rc == 0, f"rc={rc}")
        expect("only after a second reading", len(reads) == 2, f"{reads}")


def test_the_wait_fails_loudly_on_timeout() -> None:
    """A refresh that never syncs must not hand the gate the old pods."""
    stuck = [app("dfe-engine", TRACKED, sync="OutOfSync")]
    with FakeCluster(stuck) as cluster, no_poll_wait():
        rc = dfeops._wait_for_sync(
            ["kubectl"], "argocd", [dfeops._normalise_repo(TRACKED)], 0, cluster.env
        )
        expect("the wait fails", rc == 1, f"rc={rc}")


def test_an_application_on_another_repo_is_not_waited_on() -> None:
    """A cluster carries Applications this deploy did not push to."""
    apps = [
        app("someone-else", "https://git.example.com/other/thing", sync="OutOfSync"),
        app("dfe-engine", TRACKED),
    ]
    with FakeCluster(apps) as cluster, no_poll_wait():
        rc = dfeops._wait_for_sync(
            ["kubectl"], "argocd", [dfeops._normalise_repo(TRACKED)], 0, cluster.env
        )
        expect("an untracked Application does not hold the wait", rc == 0, f"rc={rc}")


def test_a_running_operation_is_not_settled() -> None:
    """Synced plus Healthy while an operation runs is the previous revision's state."""
    tracked = [dfeops._normalise_repo(TRACKED)]
    running = dfeops._converging([app("dfe-engine", TRACKED, phase="Running")], tracked)
    expect(
        "a Running operation is still converging",
        running == ["dfe-engine (operation Running)"],
        f"{running}",
    )
    degraded = dfeops._converging([app("dfe-engine", TRACKED, health="Degraded")], tracked)
    expect(
        "a Degraded Application is still converging",
        degraded == ["dfe-engine (health Degraded)"],
        f"{degraded}",
    )
    settled = dfeops._converging([app("dfe-engine", TRACKED)], tracked)
    expect("a Synced, Healthy, idle Application is settled", settled == [], f"{settled}")


def test_a_multi_source_application_matches_on_any_of_its_repos() -> None:
    """layer2-apps is multi-source: the deploy repo is the second entry."""
    multi = {
        "metadata": {"name": "layer2-apps"},
        "spec": {
            "sources": [
                {"repoURL": "https://github.com/hyperi-io/dfe-infra"},
                {"repoURL": TRACKED},
            ]
        },
        "status": {"sync": {"status": "OutOfSync"}},
    }
    pending = dfeops._converging([multi], [dfeops._normalise_repo(TRACKED)])
    expect(
        "the multi-source Application is tracked",
        pending == ["layer2-apps (sync OutOfSync)"],
        f"{pending}",
    )


def test_two_spellings_of_one_repo_are_the_same_repo() -> None:
    """The annotation and the Application need not agree on .git or a slash."""
    expect(
        "trailing .git, slash and case are normalised away",
        dfeops._normalise_repo("https://Git.example.com/dfe/deploy.git/")
        == dfeops._normalise_repo("https://git.example.com/dfe/deploy"),
        dfeops._normalise_repo("https://Git.example.com/dfe/deploy.git/"),
    )


def test_the_tracked_repos_come_off_the_cluster_secret() -> None:
    """The deploy's repos are annotations, not a flag an operator has to remember."""
    with FakeCluster([]) as cluster:
        repos, err = dfeops._tracked_repos(["kubectl"], "argocd", cluster.env)
        expect(
            "the config repo annotation is read",
            repos == [dfeops._normalise_repo(TRACKED)] and err == "",
            f"{repos} / {err}",
        )


def test_an_unreadable_cluster_secret_is_an_error_not_an_empty_list() -> None:
    """Both readings returned [], so a secret nobody could read looked annotationless.

    The refresh then waited on every Application on the cluster instead of
    stopping on the one thing it could not see.
    """
    with FakeCluster([app("dfe-engine", TRACKED)], secret=None) as cluster:
        repos, err = dfeops._tracked_repos(["kubectl"], "argocd", cluster.env)
        expect("no repos come back", repos == [], f"{repos}")
        expect("and kubectl's own message does", "forbidden" in err, f"{err}")
        rc, stages = run_refresh(cluster, refresh_args(readiness_timeout=1, namespace="dfe-local"))
        expect("the refresh stops", rc != 0, f"rc={rc}")
        expect("before the gate or the smoke suite", stages == [], f"{stages}")


def test_a_secret_with_no_annotations_only_warns() -> None:
    """A deploy that never annotated is judged whole, which is the wider wait."""
    with FakeCluster([app("dfe-engine", TRACKED)], secret={"metadata": {}}) as cluster:
        repos, err = dfeops._tracked_repos(["kubectl"], "argocd", cluster.env)
        expect("no repos and no error", repos == [] and err == "", f"{repos} / {err}")
        rc, stages = run_refresh(cluster, refresh_args(readiness_timeout=1, namespace="dfe-local"))
        expect("the refresh still runs", rc == 0, f"rc={rc}")
        expect("both stages ran", stages == ["smoke-test-readiness.sh", "smoke"], f"{stages}")


def test_the_cli_still_parses() -> None:
    """The refresh flags the refusal message names have to exist."""
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "refresh", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    expect("--help works", out.returncode == 0, out.stderr)
    expect(
        "and names both namespace flags",
        "--env-file" in out.stdout and "--namespace" in out.stdout,
        out.stdout,
    )


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'FAILED' if _failures else 'ALL PASSED'} -- {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
