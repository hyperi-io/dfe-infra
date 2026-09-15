#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_suite_watch.py
#  Purpose:      Cover suite_watch's shaping, folding and selection logic
#                offline, with a fake `_gh` answering from canned JSON.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/suite_watch.py.

Every function past `_gh` takes plain data, so this replaces `_gh` with a
recorder keyed off a short label derived from the args (not the exact flag
order), and drives the rest with a small in-test suite.yaml/versions.yaml.

    python3 scripts/tests/test_suite_watch.py

No third-party deps and no test runner, matching the tool it tests.
"""

from __future__ import annotations

import datetime
import io
import json
import subprocess
import sys
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import suite_graph  # noqa: E402
import suite_watch  # noqa: E402

NOW = datetime.datetime(2026, 9, 14, 12, 0, 0, tzinfo=datetime.UTC)


def days_ago(n: float) -> str:
    ts = NOW - datetime.timedelta(days=n)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


SUITE = '''
schema: "1"
verified: "2026-09-03"
tags:
  audience:
    general: "general"
edge_kinds:
  cargo-dep:
    means: "range"
runtime_kinds:
nodes:
  dfe-engine:
    repo: hyperi-io/dfe-engine
    role: service
    language: python
    audience: suite
    maturity: ga
    support: standard
    licence: BUSL-1.1
    classification: product
    classification_source: org-property
    default_in_pass: true
    artefacts:
      - kind: container
        registry: ghcr
        public: false
  scalo-rs:
    repo: hyperi-io/scalo-rs
    role: library
    language: rust
    audience: general
    maturity: ga
    support: standard
    licence: Apache-2.0
    classification: oss
    classification_source: in-repo
    default_in_pass: true
    artefacts:
      - kind: crate
        registry: crates.io
        public: true
  ghost-repo:
    repo: hyperi-io/ghost-repo
    role: service
    language: python
    audience: suite
    maturity: ga
    support: standard
    licence: BUSL-1.1
    classification: product
    classification_source: org-property
    default_in_pass: true
    artefacts:
      - kind: container
        registry: ghcr
        public: false
edges:
lanes:
  - name: consumers
    members: [dfe-engine]
    why: "test lane"
'''

VERSIONS = '''
current: "2.2.0-rc.12"
stacks:
  2.2.0-rc.12:
    apps:
      dfe-engine: "v1.20.4"
'''


def build_graph() -> dict:
    return suite_graph.parse_block_yaml(SUITE)


def build_versions() -> dict:
    return suite_watch.parse_simple_yaml(VERSIONS)


class Args:
    """A minimal stand-in for argparse.Namespace, one attribute per flag."""

    def __init__(self, **kw) -> None:
        self.repo = kw.pop("repo", [])
        self.lane = kw.pop("lane", None)
        self.label = kw.pop("label", "rc14")
        self.stale_days = kw.pop("stale_days", 3)
        self.stack = kw.pop("stack", None)
        self.no_branches = kw.pop("no_branches", False)
        self.json = kw.pop("json", False)
        self.quiet = kw.pop("quiet", False)
        assert not kw, f"unknown Args field(s): {kw}"


def _repo_of(args: list[str]) -> str:
    return args[args.index("--repo") + 1]


def label_for(args: list[str]) -> str:
    """A short key for one canned gh call, robust to exact flag ordering."""
    if args[:2] == ["pr", "list"]:
        return f"pr:{_repo_of(args)}"
    if args[:2] == ["release", "list"]:
        return f"release:{_repo_of(args)}"
    if args[:2] == ["issue", "list"]:
        return f"issue:{_repo_of(args)}"
    if args[0] == "api":
        endpoint = args[1]
        if endpoint.endswith("/branches"):
            return f"branches:{endpoint[len('repos/'):-len('/branches')]}"
        if "/compare/" in endpoint:
            return f"compare:{endpoint[len('repos/'):]}"
        return f"repometa:{endpoint[len('repos/'):]}"
    raise AssertionError(f"unrecognised gh args: {args}")


def make_gh(table: dict[str, object]):
    """A fake _gh: canned answers keyed by label_for(args); unmatched raises."""
    calls: list[list[str]] = []

    def fake(args: list[str]) -> object:
        calls.append(list(args))
        key = label_for(args)
        if key not in table:
            raise AssertionError(f"no canned answer for {key!r} (args={args})")
        value = table[key]
        if isinstance(value, BaseException):
            raise value
        return value

    fake.calls = calls
    return fake


def patched(table: dict[str, object]):
    """Context manager swapping suite_watch._gh for a recorder, then restoring it."""

    class _Patch:
        def __enter__(self):
            self._orig = suite_watch._gh
            self.fake = make_gh(table)
            suite_watch._gh = self.fake
            return self.fake

        def __exit__(self, *exc):
            suite_watch._gh = self._orig

    return _Patch()


# A full, healthy answer set for one repo -- individual tests copy this and
# override just the bit they are pinning down.
def base_table(repo: str = "hyperi-io/dfe-engine") -> dict[str, object]:
    return {
        f"pr:{repo}": [],
        f"release:{repo}": [{"tagName": "v1.20.4", "publishedAt": days_ago(10)}],
        f"issue:{repo}": [],
        f"repometa:{repo}": {"default_branch": "main"},
        f"branches:{repo}": ["main"],
    }


def test_gh_parses_raw_scalar_lines_from_paginated_jq() -> None:
    """`gh api --jq '.[].name'` prints each match RAW (jq -r style), unquoted --
    not one JSON value per line. A branch name like 'main' or 'fix/x' is not
    valid JSON on its own, so a naive per-line json.loads raised on every real
    branch list; this is the regression test for that failure."""
    raw_output = "main\nfix/x\nrenovate/github-actions\n"

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=raw_output, stderr="")

    orig_run = subprocess.run
    subprocess.run = fake_run
    try:
        result = suite_watch._gh(["api", "repos/o/r/branches", "--paginate", "--jq", ".[].name"])
    finally:
        subprocess.run = orig_run
    expect("raw unquoted lines come back as plain strings, one per branch",
           result == ["main", "fix/x", "renovate/github-actions"], str(result))


def test_gh_still_reads_real_json_lines() -> None:
    """A --jq filter that selects an OBJECT (`.[] | {name}`) prints valid JSON
    per line, and that shape must still parse as objects, not raw text."""
    raw_output = '{"name": "main"}\n{"name": "fix/x"}\n'

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=raw_output, stderr="")

    orig_run = subprocess.run
    subprocess.run = fake_run
    try:
        result = suite_watch._gh(["api", "repos/o/r/branches", "--paginate", "--jq", ".[]"])
    finally:
        subprocess.run = orig_run
    expect("JSON objects per line still parse as objects",
           result == [{"name": "main"}, {"name": "fix/x"}], str(result))


def test_gh_raises_gh_error_on_nonzero_exit() -> None:
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="gh: Not Found (HTTP 404)\n")

    orig_run = subprocess.run
    subprocess.run = fake_run
    try:
        suite_watch._gh(["api", "repos/o/r"])
    except suite_watch.GhError as exc:
        expect("the gh: prefix is stripped from the reason", str(exc) == "Not Found (HTTP 404)")
    else:
        expect("a non-zero exit raises GhError", False, "did not raise")
    finally:
        subprocess.run = orig_run


def test_ci_fold() -> None:
    expect("no checks at all is none", suite_watch.fold_ci([]) == "none")
    expect(
        "every check completed and successful is pass",
        suite_watch.fold_ci([{"status": "COMPLETED", "conclusion": "SUCCESS"}]) == "pass",
    )
    expect(
        "a skipped check alongside a success still folds to pass",
        suite_watch.fold_ci(
            [
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "COMPLETED", "conclusion": "SKIPPED"},
            ]
        )
        == "pass",
    )
    expect(
        "an in-progress check with no others is pending",
        suite_watch.fold_ci([{"status": "IN_PROGRESS", "conclusion": None}]) == "pending",
    )
    expect(
        "a failure wins over a pending check",
        suite_watch.fold_ci(
            [
                {"status": "COMPLETED", "conclusion": "FAILURE"},
                {"status": "IN_PROGRESS", "conclusion": None},
            ]
        )
        == "fail",
    )
    expect(
        "a legacy StatusContext state is read too",
        suite_watch.fold_ci([{"state": "ERROR"}]) == "fail",
    )


def test_pin_status() -> None:
    apps = {"dfe-engine": "v1.20.4"}
    expect("equal tags (v-prefix both) is pinned",
           suite_watch.pin_status("dfe-engine", "v1.20.4", apps) == "pinned")
    expect("a newer release is unpinned",
           suite_watch.pin_status("dfe-engine", "v1.20.5", apps) == "unpinned")
    expect("a member with no app pin is n/a",
           suite_watch.pin_status("scalo-rs", "v0.5.0", {}) == "n/a")
    expect("no release at all is n/a",
           suite_watch.pin_status("dfe-engine", None, apps) == "n/a")
    expect("a bare pin without its v matches a v-prefixed release",
           suite_watch.pin_status("dfe-engine", "v1.20.4", {"dfe-engine": "1.20.4"}) == "pinned")


def test_resolve_stack_missing_is_graceful() -> None:
    root = build_versions()
    name, pins = suite_watch.resolve_stack(root, None)
    expect("default resolves `current`", name == "2.2.0-rc.12")
    expect("current stack's pins are found", pins is not None and pins["apps"]["dfe-engine"] == "v1.20.4")
    name, pins = suite_watch.resolve_stack(root, "2.2.0-rc.14")
    expect("an rc still being cut resolves by name", name == "2.2.0-rc.14")
    expect("...with no pin-set, rather than raising", pins is None)


def test_select_targets_lane_and_repo() -> None:
    graph = build_graph()
    all_targets = suite_watch.select_targets(graph, None, [])
    expect("no lane keeps every suite.yaml member, in file order",
           [t.name for t in all_targets] == ["dfe-engine", "scalo-rs", "ghost-repo"])
    lane_targets = suite_watch.select_targets(graph, "consumers", [])
    expect("a lane limits to its own members",
           [t.name for t in lane_targets] == ["dfe-engine"])
    extra_targets = suite_watch.select_targets(graph, "consumers", ["hyperi-io/dfe-vpn"])
    expect("a --repo extra is appended after the lane's members",
           [t.repo for t in extra_targets] == ["hyperi-io/dfe-engine", "hyperi-io/dfe-vpn"])
    try:
        suite_watch.select_targets(graph, "no-such-lane", [])
    except SystemExit as exc:
        expect("an unknown lane exits non-zero", exc.code == 2)
    else:
        expect("an unknown lane exits non-zero", False, "did not raise")
    try:
        suite_watch.select_targets(graph, None, ["not-a-slug"])
    except SystemExit as exc:
        expect("a malformed --repo exits non-zero", exc.code == 2)
    else:
        expect("a malformed --repo exits non-zero", False, "did not raise")


def test_pr_shaping_and_labels() -> None:
    repo = "hyperi-io/dfe-engine"
    table = base_table(repo)
    table[f"pr:{repo}"] = [
        {
            "number": 42,
            "title": "fix(loader): honour @source",
            "author": {"login": "kaz"},
            "isDraft": True,
            "updatedAt": days_ago(2),
            "labels": [{"name": "rc14"}],
            "headRefName": "fix/source",
            "baseRefName": "main",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        }
    ]
    with patched(table):
        prs = suite_watch.fetch_prs(repo, NOW)
    expect("one PR shaped", len(prs) == 1)
    pr = prs[0]
    expect("number/title/author carried through",
           (pr.number, pr.title, pr.author) == (42, "fix(loader): honour @source", "kaz"))
    expect("draft flag carried through", pr.draft is True)
    expect("head/base carried through", (pr.head, pr.base) == ("fix/source", "main"))
    expect("age in whole days", pr.age_days == 2)
    expect("labels carried through", pr.labels == ["rc14"])
    expect("ci folds to pass", pr.ci == "pass")


def test_branch_filtering() -> None:
    """The default branch, every open PR head, and any ahead_by==0 branch drop out."""
    repo = "hyperi-io/dfe-engine"
    table = base_table(repo)
    table[f"branches:{repo}"] = ["main", "fix/has-a-pr", "fix/merged-already", "fix/needs-a-pr"]
    table[f"compare:{repo}/compare/main...fix/merged-already"] = {
        "ahead_by": 0, "behind_by": 4, "commits": [],
    }
    table[f"compare:{repo}/compare/main...fix/needs-a-pr"] = {
        "ahead_by": 3,
        "behind_by": 1,
        "commits": [{"commit": {"committer": {"date": days_ago(5)}}}],
    }
    with patched(table):
        rows = suite_watch.fetch_branches(repo, {"fix/has-a-pr"}, NOW)
    expect("only the branch genuinely ahead survives", [b.name for b in rows] == ["fix/needs-a-pr"])
    row = rows[0]
    expect("ahead/behind carried through", (row.ahead_by, row.behind_by) == (3, 1))
    expect("age is the head commit's age", row.age_days == 5)


def test_stale_flag_both_sides() -> None:
    repo = "hyperi-io/dfe-engine"
    table = base_table(repo)
    table[f"issue:{repo}"] = [
        {"number": 1, "title": "just inside", "assignees": [], "updatedAt": days_ago(2)},
        {"number": 2, "title": "just outside", "assignees": [{"login": "derek"}], "updatedAt": days_ago(4)},
    ]
    with patched(table):
        rows = suite_watch.fetch_issues(repo, "rc14", 3, NOW)
    by_number = {r.number: r for r in rows}
    expect("an issue newer than stale-days is not stale", by_number[1].stale is False)
    expect("an issue older than stale-days is stale", by_number[2].stale is True)
    expect("no assignee prints as a dash", by_number[1].assignee == "-")
    expect("assignee logins are carried through", by_number[2].assignee == "derek")


def test_release_and_pin_in_context() -> None:
    repo = "hyperi-io/dfe-engine"
    apps = {"dfe-engine": "v1.20.4"}
    with patched({f"release:{repo}": [{"tagName": "v1.20.4", "publishedAt": days_ago(1)}]}):
        info = suite_watch.fetch_release(repo, "dfe-engine", apps)
    expect("a matching release is pinned", info is not None and info.pin_status == "pinned")
    with patched({f"release:{repo}": [{"tagName": "v1.20.9", "publishedAt": days_ago(1)}]}):
        info = suite_watch.fetch_release(repo, "dfe-engine", apps)
    expect("a moved-on release is unpinned", info.pin_status == "unpinned")
    with patched({f"release:{repo}": []}):
        info = suite_watch.fetch_release(repo, "dfe-engine", apps)
    expect("no release at all is None", info is None)
    with patched({f"release:{repo}": [{"tagName": "v0.1.0", "publishedAt": days_ago(1)}]}):
        info = suite_watch.fetch_release(repo, "scalo-rs", {})
    expect("a library with no app pin is n/a", info.pin_status == "n/a")


def test_unreachable_repo_does_not_abort_the_sweep() -> None:
    graph = build_graph()
    args = Args(label="rc14", no_branches=True)
    good, bad = "hyperi-io/dfe-engine", "hyperi-io/ghost-repo"
    table = base_table(good)
    table[f"pr:{bad}"] = suite_watch.GhError("Not Found (HTTP 404)")
    with patched(table):
        reports = [
            suite_watch.gather_repo(t, args, {"dfe-engine": "v1.20.4"}, NOW)
            for t in suite_watch.select_targets(graph, None, [])
            if t.name != "scalo-rs"  # not canned for this test; keep it short
        ]
    by_name = {r.name: r for r in reports}
    expect("the good repo gathered normally", by_name["dfe-engine"].unreachable is None)
    expect("the bad repo is marked unreachable, not raised",
           by_name["ghost-repo"].unreachable == "Not Found (HTTP 404)")
    expect("an unreachable repo carries no partial data",
           by_name["ghost-repo"].prs == [] and by_name["ghost-repo"].release is None)


def test_json_shape() -> None:
    graph = build_graph()
    versions = build_versions()
    args = Args(label="rc14", stack="2.2.0-rc.12", no_branches=True, json=True)
    table = {}
    table.update(base_table("hyperi-io/dfe-engine"))
    table.update(base_table("hyperi-io/scalo-rs"))
    table.update(base_table("hyperi-io/ghost-repo"))
    out = io.StringIO()
    with patched(table):
        rc = suite_watch.run(args, graph, versions, out, now=NOW)
    expect("run() always returns 0 -- a report, never a gate", rc == 0)
    payload = json.loads(out.getvalue())
    expect("top-level keys", set(payload) == {"swept_at", "stack", "repos"})
    expect("stack is the resolved name", payload["stack"] == "2.2.0-rc.12")
    expect("one entry per swept repo", len(payload["repos"]) == 3)
    engine = next(r for r in payload["repos"] if r["name"] == "dfe-engine")
    expect("release is typed as an object with pin_status",
           engine["release"] == {"tag": "v1.20.4", "published_at": days_ago(10), "pin_status": "pinned"})
    expect("branches is null when --no-branches was passed", engine["branches"] is None)
    scalo = next(r for r in payload["repos"] if r["name"] == "scalo-rs")
    expect("a library's release is n/a with no app pin", scalo["release"]["pin_status"] == "n/a")


def test_quiet_hides_uneventful_repos() -> None:
    reports = [
        suite_watch.RepoReport(name="quiet-one", repo="hyperi-io/quiet-one",
                                release=suite_watch.ReleaseInfo("v1.0.0", days_ago(1), "pinned")),
        suite_watch.RepoReport(name="loud-one", repo="hyperi-io/loud-one",
                                prs=[suite_watch.PrRow(1, "t", "a", False, "main", "b", 1, [], "pass")]),
    ]
    summary_counts = suite_watch.summarize(reports)
    loud_text = suite_watch.render_text(reports, summary_counts, quiet=False)
    quiet_text = suite_watch.render_text(reports, summary_counts, quiet=True)
    expect("non-quiet text mentions both repos",
           "quiet-one" in loud_text and "loud-one" in loud_text)
    expect("quiet text drops the uneventful repo", "quiet-one" not in quiet_text)
    expect("quiet text keeps the repo with something to show", "loud-one" in quiet_text)
    expect("the summary line always carries the true repo count", "2 repos" in quiet_text)


def main() -> int:
    with standalone():
        test_gh_parses_raw_scalar_lines_from_paginated_jq()
        test_gh_still_reads_real_json_lines()
        test_gh_raises_gh_error_on_nonzero_exit()
        test_ci_fold()
        test_pin_status()
        test_resolve_stack_missing_is_graceful()
        test_select_targets_lane_and_repo()
        test_pr_shaping_and_labels()
        test_branch_filtering()
        test_stale_flag_both_sides()
        test_release_and_pin_in_context()
        test_unreachable_repo_does_not_abort_the_sweep()
        test_json_shape()
        test_quiet_hides_uneventful_repos()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
