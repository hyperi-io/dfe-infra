#  Project:      dfe-infra
#  File:         scripts/tests/test_dfe_sweep.py
#  Purpose:      Prove the suite sweep derives its members from data and never counts an
#                unreadable tool, or a bridge alert it missed, as clean.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/dfe-sweep and scripts/dfe_suite/sweep.py, with no network.

GitHub is replaced by recorded API answers handed to the collector, and gh by a stand-in
executable on PATH where the transport itself is under test. Membership runs against both
fixture files and the repository's own versions.yaml and suite.yaml.

    python3 -m pytest scripts/tests/test_dfe_sweep.py -q
"""

import dataclasses
import functools
import importlib.util
import json
import os
import re
import stat
import sys
from datetime import UTC, datetime, timedelta
from importlib.machinery import SourceFileLoader
from itertools import chain
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from dfe_suite import sweep  # noqa: E402
from dfe_suite.graph import load_graph  # noqa: E402
from dfe_suite.proc import FleetError  # noqa: E402

# The CLI has no .py suffix, so it is loaded by path; it imports scalo only when it runs.
_CLI = REPO_ROOT / "scripts" / "dfe-sweep"
_SPEC = importlib.util.spec_from_file_location(
    "dfe_sweep_cli", _CLI, loader=SourceFileLoader("dfe_sweep_cli", str(_CLI))
)
cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli)

ORG = "example-org"
EMPTY_GRAPH: dict = {"nodes": {}, "edges": []}

VERSIONS = """\
schema: 2
current: "2.3.0-rc.1"
latest: "2.2.0"
stacks:

  2.2.0:
    maturity: release
    apps:
      dfe-engine: "v1.0.0"        # a trailing comment
      dfe-loader: "v1.1.0"
      # Alpha and unpublished. A commented pin is read by nothing.
      # dfe-transform-wasm: "2.2.0"
      # dfe-transform-splack: "2.2.0"
    digests:
      dfe-engine: "sha256:aaaa"
    content:
      dfe-schemas: "v0.2.0"
      # dfe-docs: "v0.1.0"

  2.3.0-rc.1:
    maturity: rc
    apps:
      dfe-engine: "v1.2.0"
      dfe-ui: "v2.0.0"
    content:
      dfe-deploy: "v1.0.0"
"""


def _root(tmp_path: Path, text: str = VERSIONS) -> dict:
    path = tmp_path / "versions.yaml"
    path.write_text(text, encoding="utf-8")
    return sweep.read_versions(path)


def _resolve(root: dict, graph: dict = EMPTY_GRAPH, **kwargs) -> tuple[str, dict, list[str]]:
    kwargs.setdefault("pointer", "latest")
    kwargs.setdefault("infra", "dfe-infra")
    stack, members, notes = sweep.resolve_members(root, graph, org=ORG, **kwargs)
    return stack, {m.name: m for m in members}, notes


def _uncomment(text: str, names: tuple[str, ...]) -> str:
    for name in names:
        text = re.sub(rf"(?m)^(\s*)# ({re.escape(name)}: )", r"\1\2", text)
    return text


# ---------------------------------------------------------------------------
# versions.yaml
# ---------------------------------------------------------------------------


def test_latest_pointer_reads_the_latest_stack(tmp_path: Path) -> None:
    stack, members, _ = _resolve(_root(tmp_path))
    assert stack == "2.2.0"
    assert list(members) == ["dfe-engine", "dfe-loader", "dfe-schemas", "dfe-infra"]
    assert members["dfe-engine"].why == ("stack apps",)
    assert members["dfe-schemas"].why == ("stack content",)
    assert members["dfe-infra"].why == ("infra",)


def test_derive_suite_off_keeps_only_infra_topics_and_include(tmp_path: Path) -> None:
    topics = {"ops-tools": (f"{ORG}/ops-tools", ["infra-member"])}
    _, members, _ = _resolve(
        _root(tmp_path),
        infra="ops-infra",
        topics=topics,
        include=("extra-repo",),
        derive_suite=False,
    )
    assert list(members) == ["ops-infra", "ops-tools", "extra-repo"]
    assert members["ops-infra"].why == ("infra",)
    assert members["ops-tools"].why == ("topic infra-member",)
    for name in ("dfe-engine", "dfe-loader", "dfe-schemas", "dfe-infra"):
        assert name not in members


def test_derive_suite_defaults_on(tmp_path: Path) -> None:
    _, members, _ = _resolve(_root(tmp_path))
    assert "dfe-engine" in members


def test_a_commented_pin_is_not_a_member(tmp_path: Path) -> None:
    _, members, _ = _resolve(_root(tmp_path))
    for name in ("dfe-transform-wasm", "dfe-transform-splack", "dfe-docs"):
        assert name not in members


def test_uncommenting_a_pin_is_the_only_change_that_adds_it(tmp_path: Path) -> None:
    names = ("dfe-transform-wasm", "dfe-transform-splack")
    _, before, _ = _resolve(_root(tmp_path))
    _, after, _ = _resolve(_root(tmp_path, _uncomment(VERSIONS, names)))
    assert set(after) - set(before) == set(names)
    assert set(before) - set(after) == set()
    assert after["dfe-transform-wasm"].why == ("stack apps",)
    assert after["dfe-transform-wasm"].repo == f"{ORG}/dfe-transform-wasm"


def test_an_empty_latest_falls_back_to_current(tmp_path: Path) -> None:
    text = VERSIONS.replace('latest: "2.2.0"', 'latest: ""')
    stack, members, _ = _resolve(_root(tmp_path, text))
    assert stack == "2.3.0-rc.1"
    assert set(members) == {"dfe-engine", "dfe-ui", "dfe-deploy", "dfe-infra"}


def test_the_current_pointer_and_an_explicit_key(tmp_path: Path) -> None:
    root = _root(tmp_path)
    assert _resolve(root, pointer="current")[0] == "2.3.0-rc.1"
    assert _resolve(root, pointer="2.2.0")[0] == "2.2.0"


def test_a_stack_that_is_not_there_is_a_run_error(tmp_path: Path) -> None:
    with pytest.raises(FleetError, match=re.escape("9.9.9")):
        _resolve(_root(tmp_path), pointer="9.9.9")


def test_no_pointer_at_all_is_a_run_error(tmp_path: Path) -> None:
    text = VERSIONS.replace('latest: "2.2.0"', 'latest: ""').replace(
        'current: "2.3.0-rc.1"', 'current: ""'
    )
    with pytest.raises(FleetError, match="pointer"):
        _resolve(_root(tmp_path, text))


def test_the_repo_versions_yaml_admits_wasm_and_splack_by_uncommenting_alone(
    tmp_path: Path,
) -> None:
    """Against the real versions.yaml and suite.yaml, the claim the tool is built on."""
    names = ("dfe-transform-wasm", "dfe-transform-splack")
    text = (REPO_ROOT / "versions.yaml").read_text(encoding="utf-8")
    graph = load_graph(dfe_infra=REPO_ROOT)
    _, before, _ = _resolve(_root(tmp_path, text), graph)
    for name in names:
        assert name not in before, f"{name} is already a member; this test has served its turn"
    uncommented = _uncomment(text, names)
    assert uncommented != text, "the commented-out wasm and splack pins are gone from the file"
    _, after, _ = _resolve(_root(tmp_path, uncommented), graph)
    assert set(after) - set(before) == set(names)
    assert set(before) - set(after) == set()
    assert "infra" in before["dfe-infra"].why


def test_the_repo_suite_yaml_members_follow_default_in_pass(tmp_path: Path) -> None:
    text = (REPO_ROOT / "versions.yaml").read_text(encoding="utf-8")
    graph = load_graph(dfe_infra=REPO_ROOT)
    _, members, _ = _resolve(_root(tmp_path, text), graph)
    for name, node in graph["nodes"].items():
        listed = "suite member" in (members[name].why if name in members else ())
        assert listed is (node.get("default_in_pass") is not False), name


# ---------------------------------------------------------------------------
# suite.yaml in-edges
# ---------------------------------------------------------------------------

GRAPH = {
    "nodes": {
        "dfe-engine": {"repo": "example-org/dfe-engine", "default_in_pass": True},
        "lib-a": {"repo": "other-org/lib-a", "default_in_pass": False},
        "lib-b": {"default_in_pass": False},
        "lib-c": {"repo": "lib-c-renamed", "default_in_pass": False},
        "consumer-only": {"repo": "example-org/consumer-only", "default_in_pass": False},
        "listed-lib": {"repo": "example-org/listed-lib", "default_in_pass": True},
        "dfe-transform-wasm": {"maturity": "alpha", "default_in_pass": False},
    },
    "edges": [
        {"from": "lib-a", "to": "dfe-engine", "kind": "python-dep"},
        {"from": "lib-b", "to": "lib-a", "kind": "cargo-dep"},
        {"from": "lib-a", "to": "dfe-loader", "kind": "cargo-dep"},
        {"from": "dfe-engine", "to": "consumer-only", "kind": "generated-file"},
        {"from": "lib-c", "to": "dfe-schemas", "kind": "python-dep"},
    ],
}


def test_producers_are_walked_transitively_and_say_what_they_feed(tmp_path: Path) -> None:
    _, members, _ = _resolve(_root(tmp_path), GRAPH)
    assert members["lib-a"].why == ("dependency of dfe-engine, dfe-loader",)
    assert members["lib-b"].why == ("dependency of lib-a",)
    assert "consumer-only" not in members, "an out-edge is not a reason to sweep a repo"


def test_a_node_repo_field_names_the_github_repo(tmp_path: Path) -> None:
    _, members, _ = _resolve(_root(tmp_path), GRAPH)
    assert members["lib-a"].repo == "other-org/lib-a"
    assert members["lib-b"].repo == f"{ORG}/lib-b"
    assert members["lib-c"].repo == f"{ORG}/lib-c-renamed"
    assert members["dfe-loader"].repo == f"{ORG}/dfe-loader"


def test_the_walk_starts_from_stack_members_only() -> None:
    feeds = sweep.producers_of(GRAPH, ["dfe-schemas"])
    assert feeds == {"lib-c": ["dfe-schemas"]}


def test_every_suite_node_not_held_back_is_a_member(tmp_path: Path) -> None:
    _, members, _ = _resolve(_root(tmp_path), GRAPH)
    assert members["listed-lib"].why == ("suite member",)
    assert members["listed-lib"].repo == "example-org/listed-lib"
    assert members["dfe-engine"].why == ("stack apps", "suite member")
    assert "dfe-transform-wasm" not in members
    assert sweep.suite_members(GRAPH) == ["dfe-engine", "listed-lib"]


def test_a_held_back_node_joins_when_the_stack_pins_it(tmp_path: Path) -> None:
    text = _uncomment(VERSIONS, ("dfe-transform-wasm",))
    _, members, _ = _resolve(_root(tmp_path, text), GRAPH)
    assert members["dfe-transform-wasm"].why == ("stack apps",)


# ---------------------------------------------------------------------------
# include / exclude
# ---------------------------------------------------------------------------


def test_include_adds_by_name_or_slug(tmp_path: Path) -> None:
    _, members, _ = _resolve(_root(tmp_path), include=["vector-vrl", "far-org/thing"])
    assert members["vector-vrl"].why == ("config include",)
    assert members["vector-vrl"].repo == f"{ORG}/vector-vrl"
    assert members["thing"].repo == "far-org/thing"


def test_exclude_beats_every_other_reason(tmp_path: Path) -> None:
    _, members, notes = _resolve(
        _root(tmp_path),
        GRAPH,
        include=["dfe-hyperdx"],
        exclude=["dfe-hyperdx", "other-org/lib-a", "dfe-infra"],
    )
    for name in ("dfe-hyperdx", "lib-a", "dfe-infra"):
        assert name not in members
    assert "lib-b" in members, "exclude drops the member, not what the walk found through it"
    assert notes == []


def test_an_exclude_that_matches_nothing_is_said(tmp_path: Path) -> None:
    _, _, notes = _resolve(_root(tmp_path), exclude=["no-such-repo"])
    assert notes == ["exclude 'no-such-repo' matches no member"]


def test_comma_separated_lists_are_accepted(tmp_path: Path) -> None:
    _, members, _ = _resolve(_root(tmp_path), include=["a-one,a-two"], exclude=["a-two"])
    assert "a-one" in members
    assert "a-two" not in members


SETTINGS = {
    "org": "example-org",
    "infra_repo": "dfe-infra",
    "versions_file": "versions.yaml",
    "suite_dir": ".",
    "stack": "latest",
    "include": ["from-settings"],
    "exclude": [],
    "tools": {tool: True for tool in sweep.TOOLS},
    "bot_logins": ["renovate[bot]", "dependabot[bot]"],
    "format": "text",
    "workers": 4,
    "gh_timeout_seconds": 120,
    "backlog": {
        "label": "backlog",
        "statuses": ["Backlog", "Next", "In progress", "Done"],
        "priorities": ["P0", "P1", "P2", "P3"],
        "project_limit": 500,
    },
    "slack": {"group_replies": 2, "title": "DFE suite sweep", "timezone": "UTC"},
}


def _options(argv: list[str], **overrides) -> cli.Options:
    settings = {**SETTINGS, **overrides}
    return cli.effective(settings, cli.build_parser().parse_args(argv))


def test_the_command_line_wins_over_the_settings() -> None:
    options = _options(["--include", "a", "--include", "b,c", "--format", "json", "--org", "o"])
    assert options.include == ("a", "b", "c")
    assert options.output == "json"
    assert options.org == "o"


def test_derive_suite_is_a_setting_defaulting_on() -> None:
    assert _options([]).derive_suite is True
    assert _options([], derive_suite=False).derive_suite is False


@pytest.mark.parametrize("bad", ["false", 0, "no", None])
def test_derive_suite_refuses_anything_but_a_boolean(bad: object) -> None:
    with pytest.raises(FleetError, match="derive_suite"):
        _options([], derive_suite=bad)


def test_an_unset_flag_defers_to_the_settings() -> None:
    options = _options([], include="x, y", workers="6")
    assert options.include == ("x", "y")
    assert options.config.workers == 6
    assert options.versions_file == REPO_ROOT / "versions.yaml"


def test_tools_switched_off_in_the_settings_do_not_run() -> None:
    options = _options([], tools={**SETTINGS["tools"], "issues": False, "ci_runs": False})
    assert "issues" not in options.config.tools
    assert "ci_runs" not in options.config.tools
    assert "dependabot" in options.config.tools


def test_the_backlog_label_is_a_setting() -> None:
    options = _options([], backlog={**SETTINGS["backlog"], "label": "triage"})
    assert options.config.backlog.label == "triage"
    assert options.config.backlog.backlog_status == "Backlog"
    assert options.config.backlog.priorities == ("P0", "P1", "P2", "P3")
    assert options.project_limit == 500


@pytest.mark.parametrize(
    ("argv", "overrides", "message"),
    [
        (["--tools", "dependabot,nonsense"], {}, "unknown tool"),
        ([], {"tools": {"dependabot": "yes"}}, "tools.dependabot"),
        ([], {"workers": 0}, "workers"),
        ([], {"format": "xml"}, "format"),
        ([], {"org": ""}, "org"),
        ([], {"bot_logins": []}, "bot_logins"),
        (["--check-bridge", "--tools", "dependabot"], {}, "check-bridge"),
        ([], {"backlog": None}, "backlog"),
        ([], {"backlog": {**SETTINGS["backlog"], "label": ""}}, "backlog.label"),
        ([], {"backlog": {**SETTINGS["backlog"], "statuses": []}}, "backlog.statuses"),
        ([], {"backlog": {**SETTINGS["backlog"], "project_limit": 0}}, "backlog.project_limit"),
        ([], {"gh_timeout_seconds": 0}, "gh_timeout_seconds"),
    ],
)
def test_a_bad_setting_is_a_run_error_naming_it(argv, overrides, message) -> None:
    with pytest.raises(FleetError, match=message):
        _options(argv, **overrides)


# ---------------------------------------------------------------------------
# Collection, with recorded API answers
# ---------------------------------------------------------------------------

MEMBER = sweep.Member("dfe-engine", "example-org/dfe-engine", ("stack apps",))
CONFIG = sweep.SweepConfig(
    tools=frozenset(sweep.TOOLS),
    bot_logins=frozenset({"renovate[bot]", "dependabot[bot]"}),
    workers=2,
    org="example-org",
)
HEAD = "a" * 40
PROJECTS = sweep.ProjectIndex(numbers={"dfe-engine": 7, "dfe-ui": 8})


def _issue_node(number: int, status: str, *, labels=(), priority="", effort="", state="OPEN",
                repo="example-org/dfe-engine", kind="Task") -> dict:
    fields = [
        {"value": value, "field": {"name": name}}
        for name, value in (("Priority", priority), ("Effort", effort))
        if value
    ]
    return {
        "fieldValueByName": {"name": status} if status else None,
        "content": {
            "number": number,
            "title": f"item {number}",
            "url": f"https://example.com/{repo}/issues/{number}",
            "createdAt": "2026-10-01T00:00:00Z",
            "state": state,
            "repository": {"nameWithOwner": repo},
            "issueType": {"name": kind} if kind else None,
            "labels": {"nodes": [{"name": label} for label in labels]},
            "issueFieldValues": {"nodes": [*fields, {}]},
        },
    }


PROJECT_NODES = [
    _issue_node(31, "Backlog", priority="P2", effort="4h"),
    _issue_node(33, "Backlog", labels=("backlog",), priority="P0"),
    _issue_node(32, "Next", labels=("backlog",)),
    _issue_node(36, "In progress", priority="P1"),
    _issue_node(40, "Backlog", state="CLOSED"),
    _issue_node(31, "Backlog", repo="other-org/elsewhere"),
    {"fieldValueByName": {"name": "Backlog"}, "content": {}},
    {"fieldValueByName": None, "content": None},
]


def _graphql(nodes: list | Exception = PROJECT_NODES):
    """A GraphQL stand-in answering every query with these nodes. Records every call."""
    calls: list[tuple[str, dict, str]] = []

    def graphql(query: str, variables: dict, jq: str) -> list:
        calls.append((query, variables, jq))
        if isinstance(nodes, Exception):
            raise nodes
        return list(nodes)

    return graphql, calls


def _collect(api, *, projects=PROJECTS, nodes=PROJECT_NODES, config=CONFIG):
    graphql, _ = _graphql(nodes)
    return sweep.collect_repo(MEMBER, api, config, graphql=graphql, projects=projects)

DASHBOARD_BODY = """\
This issue lists Renovate updates and detected dependencies.

## Repository problems

These problems occurred while renovating this repository. [View logs](https://example.com).

 - WARN: Package lookup failures

## Errored

These updates encountered an error and will be retried.

 - [ ] <!-- retry-branch=renovate/foo-1.x -->chore(deps): update dependency foo to v1.2.3

## Rate-Limited

 - [ ] <!-- unlimit-branch=renovate/bar-2.x -->chore(deps): update bar to v2
 - [ ] <!-- create-all-rate-limited-prs -->**Create all rate-limited PRs at once**

## Deprecations / Replacements

| Datasource | Name | Replacement PR? |
|------------|------|-----------------|
| npm | `old-lib` | [#12](https://example.com/12) |

## Open

 - [ ] <!-- rebase-branch=renovate/baz -->[chore(deps): update baz](../pull/9)

## Detected dependencies

 - not flagged
"""


def _answers(overrides: dict | None = None):
    """An API stand-in: endpoint prefix -> rows, or a GhError to raise. Records every call."""
    table = {
        "repos/example-org/dfe-engine/commits/": [{"sha": HEAD}],
        "repos/example-org/dfe-engine/dependabot/alerts": [
            {"number": 7, "html_url": "https://example.com/d/7", "created_at": "2026-10-01",
             "severity": "high", "ghsa": "GHSA-1", "cve": "CVE-2026-1", "summary": "bad",
             "package": "requests", "manifest": "uv.lock"},
        ],
        "repos/example-org/dfe-engine/code-scanning/alerts": [
            {"number": 3, "html_url": "https://example.com/c/3", "rule_id": "py/sqli",
             "rule": "SQL", "rule_severity": "error", "security_severity": "critical",
             "tool": "CodeQL", "path": "a.py", "line": 4},
            {"number": 4, "html_url": "https://example.com/c/4", "rule_id": "lint",
             "rule": "style", "rule_severity": "note", "security_severity": None,
             "tool": "zizmor", "path": "w.yml", "line": None},
        ],
        "repos/example-org/dfe-engine/secret-scanning/alerts": [
            {"number": 1, "html_url": "https://example.com/s/1", "secret_type": "x",
             "secret_type_display_name": "Token", "validity": "inactive",
             "publicly_leaked": False},
        ],
        "repos/example-org/dfe-engine/security-advisories": [
            {"ghsa_id": "GHSA-t", "state": "triage", "severity": "high", "summary": "report"},
            {"ghsa_id": "GHSA-p", "state": "published", "severity": "low", "summary": "old"},
        ],
        "repos/example-org/dfe-engine/pulls": [
            {"number": 20, "title": "chore(deps): bump", "user": "renovate[bot]",
             "html_url": "https://example.com/p/20"},
            {"number": 21, "title": "fix: a person", "user": "someone"},
        ],
        "repos/example-org/dfe-engine/issues": [
            {"number": 30, "title": "Dependency Dashboard", "user": "renovate[bot]",
             "html_url": "https://example.com/i/30", "is_pr": False, "body": DASHBOARD_BODY},
            {"number": 31, "title": "a real bug", "user": "someone", "is_pr": False,
             "labels": ["bug"], "type": "Bug", "html_url": "https://example.com/i/31",
             "created_at": "2026-10-02"},
            {"number": 32, "title": "labelled, promoted", "user": "someone", "is_pr": False,
             "labels": ["backlog"], "created_at": "2026-10-03"},
            {"number": 33, "title": "labelled, in the backlog", "user": "someone",
             "is_pr": False, "labels": ["backlog"], "created_at": "2026-10-03"},
            {"number": 34, "title": "labelled, on no project", "user": "someone",
             "is_pr": False, "labels": ["backlog", "bug"], "created_at": "2026-10-03"},
            {"number": 35, "title": "unlabelled, on no project", "user": "someone",
             "is_pr": False, "labels": [], "html_url": "https://example.com/i/35",
             "created_at": "2026-10-04"},
            {"number": 36, "title": "unlabelled, in progress", "user": "someone",
             "is_pr": False, "labels": ["bug"], "type": "Bug",
             "html_url": "https://example.com/i/36", "created_at": "2026-10-05"},
            {"number": 20, "title": "chore(deps): bump", "user": "renovate[bot]",
             "is_pr": True},
        ],
        # The repo-wide run list answers inconsistently, so no read may reach it.
        "repos/example-org/dfe-engine/actions/runs": AssertionError("repo-wide run list read"),
        # Workflow 3 is disabled and has no answer, so reading its runs fails the test.
        "repos/example-org/dfe-engine/actions/workflows": [
            {"id": 1, "path": ".github/workflows/ci.yml", "state": "active"},
            {"id": 2, "path": ".github/workflows/release.yml", "state": "active"},
            {"id": 3, "path": ".github/workflows/old.yml", "state": "disabled_manually"},
        ],
        "repos/example-org/dfe-engine/actions/workflows/1/runs": [
            {"id": 100, "name": "CI", "path": ".github/workflows/ci.yml", "workflow_id": 1,
             "conclusion": "failure", "head_sha": HEAD, "created_at": "2026-10-06T01:00:00Z",
             "check_suite_id": 500, "html_url": "https://example.com/r/100"},
        ],
        "repos/example-org/dfe-engine/actions/workflows/2/runs": [
            {"id": 101, "name": "Release", "path": ".github/workflows/release.yml",
             "workflow_id": 2, "conclusion": "success", "head_sha": HEAD,
             "created_at": "2026-10-06T02:00:00Z", "check_suite_id": 501},
        ],
        "repos/example-org/dfe-engine/check-suites/500/check-runs": [
            {"id": 7000, "name": "test", "html_url": "https://example.com/j/7000",
             "annotations": 2},
            {"id": 7001, "name": "lint", "html_url": "https://example.com/j/7001",
             "annotations": 1},
            {"id": 7002, "name": "quiet", "annotations": 0},
        ],
        "repos/example-org/dfe-engine/check-suites/501/check-runs": [],
        "repos/example-org/dfe-engine/check-runs/7000/annotations": [
            {"annotation_level": "warning", "message": "Node.js 20 is deprecated"},
            {"annotation_level": "notice", "message": "just a notice"},
        ],
        "repos/example-org/dfe-engine/check-runs/7001/annotations": [
            {"annotation_level": "warning", "message": "Node.js 20 is deprecated"},
        ],
        "repos/example-org/dfe-engine": [{"default_branch": "main", "archived": False}],
    }
    table.update(overrides or {})
    calls: list[tuple[str, str, bool]] = []

    def api(endpoint: str, jq: str, paginate: bool) -> list[dict]:
        calls.append((endpoint, jq, paginate))
        for prefix in sorted(table, key=len, reverse=True):
            if endpoint == prefix or endpoint.startswith(prefix + "?") or (
                prefix.endswith("/") and endpoint.startswith(prefix)
            ):
                answer = table[prefix]
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"no recorded answer for {endpoint}")

    return api, calls


def _by_tool(result: sweep.RepoResult) -> dict[str, list[sweep.Finding]]:
    grouped: dict[str, list[sweep.Finding]] = {}
    for finding in result.findings:
        grouped.setdefault(finding.tool, []).append(finding)
    return grouped


def test_every_tool_is_collected_from_the_default_branch() -> None:
    api, calls = _answers()
    result = _collect(api)
    assert result.unreadable == []
    assert (result.default_branch, result.head_sha) == ("main", HEAD)
    tools = _by_tool(result)
    assert [f.severity for f in tools["dependabot"]] == ["high"]
    assert [(f.id, f.severity) for f in tools["code_scanning"]] == [("3", "critical"),
                                                                    ("4", "low")]
    assert [f.severity for f in tools["secret_scanning"]] == ["medium"]
    assert [f.id for f in tools["advisories"]] == ["GHSA-t"]
    assert [f.id for f in tools["bot_prs"]] == ["20"]
    assert [f.id for f in tools["ci_runs"]] == ["100"]
    assert [issue["number"] for issue in result.issues] == [35, 36]
    assert result.project == 7
    code_scanning = next(c for c in calls if "/code-scanning/" in c[0])
    assert "ref=refs%2Fheads%2Fmain" in code_scanning[0]


def test_issues_and_backlog_are_work_items_not_findings() -> None:
    api, _ = _answers()
    result = _collect(api)
    assert not {"issues", "backlog"} & {f.tool for f in result.findings}
    report = sweep.build_report("2.2.0", [result], CONFIG.tools, generated_at="t")
    assert not {"issues", "backlog"} & set(report["totals"]["by_tool"])
    assert report["issues"][1] == {
        "repo": "example-org/dfe-engine", "number": 36, "title": "unlabelled, in progress",
        "labels": ["bug"], "type": "Bug", "url": "https://example.com/i/36",
        "created_at": "2026-10-05",
    }
    assert all(set(row) == set(sweep.BACKLOG_FIELDS) for row in report["backlog"])
    assert report["totals"]["work_items"] == {"issues": 2, "backlog": 5, "backlog_drift": 3}
    assert report["members"][0]["project"] == 7
    json.dumps(report)


def test_the_secret_value_is_never_requested() -> None:
    api, calls = _answers()
    _collect(api)
    endpoint, jq, _ = next(c for c in calls if "/secret-scanning/" in c[0])
    assert "hide_secret=true" in endpoint
    fields = re.findall(r"[{,]\s*([\w]+)", jq)
    assert "secret" not in fields
    assert "secret" not in re.findall(r"\.(\w+)", jq)


def test_a_refused_tool_is_not_readable_and_never_zero() -> None:
    api, _ = _answers({
        "repos/example-org/dfe-engine/code-scanning/alerts": sweep.GhError(
            "x", 403, "Code scanning is not enabled for this repository. (HTTP 403)"),
        "repos/example-org/dfe-engine/secret-scanning/alerts": sweep.GhError(
            "x", 404, "Secret scanning is disabled on this repository. (HTTP 404)"),
    })
    result = _collect(api)
    unreadable = {u.tool: u.reason for u in result.unreadable}
    assert set(unreadable) == {"code_scanning", "secret_scanning"}
    assert "HTTP 403" in unreadable["code_scanning"]
    assert "HTTP 404" in unreadable["secret_scanning"]
    tools = _by_tool(result)
    assert "code_scanning" not in tools
    assert tools["dependabot"], "one refused tool does not stop the others"
    report = sweep.build_report("2.2.0", [result], CONFIG.tools, generated_at="t")
    assert report["totals"]["unreadable"] == 2
    assert report["totals"]["by_tool"]["code_scanning"] == 0
    assert "NOT READABLE (2)" in sweep.render_text(report)


def test_a_repo_github_will_not_show_is_unreadable_for_every_tool() -> None:
    api, _ = _answers({
        "repos/example-org/dfe-engine": sweep.GhError("x", 404, "Not Found (HTTP 404)"),
    })
    result = _collect(api)
    assert result.findings == []
    assert {u.tool for u in result.unreadable} == set(sweep.TOOLS)
    assert all("repo not readable" in u.reason for u in result.unreadable)


def test_a_refused_ci_read_is_reported_once_per_tool() -> None:
    api, _ = _answers({
        "repos/example-org/dfe-engine/check-suites/500/check-runs": sweep.GhError(
            "x", 403, "Resource not accessible (HTTP 403)"),
        "repos/example-org/dfe-engine/check-suites/501/check-runs": sweep.GhError(
            "x", 403, "Resource not accessible (HTTP 403)"),
    })
    result = _collect(api)
    assert [u.tool for u in result.unreadable] == ["ci_annotations"]


def test_annotations_are_one_finding_per_distinct_warning() -> None:
    api, _ = _answers()
    annotations = _by_tool(_collect(api))["ci_annotations"]
    assert len(annotations) == 1
    assert annotations[0].severity == "low"
    assert annotations[0].extra["jobs"] == ["test", "lint"]


def test_each_active_workflow_is_read_once_on_the_default_branch() -> None:
    api, calls = _answers()
    _collect(api)
    listed = [call for call in calls if call[0].startswith(
        "repos/example-org/dfe-engine/actions/workflows?")]
    assert [paginate for _, _, paginate in listed] == [True]
    runs = [endpoint for endpoint, _, _ in calls if endpoint.endswith("&per_page=1")]
    assert runs == [
        f"repos/example-org/dfe-engine/actions/workflows/{n}/runs"
        "?branch=main&status=completed&per_page=1"
        for n in (1, 2)
    ]


def test_a_faked_inconsistent_repo_wide_list_cannot_change_the_result() -> None:
    stale = [{"id": 90, "name": "CI", "workflow_id": 1, "conclusion": "failure",
              "head_sha": "b" * 40, "created_at": "2026-09-23T02:33:04Z"}]
    fresh = [{"id": 110, "name": "CI", "workflow_id": 1, "conclusion": "success",
              "head_sha": HEAD, "created_at": "2026-10-07T01:00:00Z"}]
    results = []
    for answer in (stale, fresh, []):
        api, calls = _answers({"repos/example-org/dfe-engine/actions/runs": answer})
        results.append(_collect(api))
        assert not any("/actions/runs" in endpoint for endpoint, _, _ in calls)
    assert results[0] == results[1] == results[2]
    assert [f.id for f in _by_tool(results[0])["ci_runs"]] == ["100"]


def test_each_workflows_own_newest_run_wins() -> None:
    newest = {"id": 120, "name": "CI", "path": ".github/workflows/ci.yml", "workflow_id": 1,
              "conclusion": "success", "head_sha": HEAD, "created_at": "2026-10-07T03:00:00Z"}
    older = {**newest, "id": 100, "conclusion": "failure", "created_at": "2026-10-06T01:00:00Z"}
    for answer in ([newest], [newest, older]):
        api, _ = _answers({"repos/example-org/dfe-engine/actions/workflows/1/runs": answer})
        assert "ci_runs" not in _by_tool(_collect(api))
    api, _ = _answers({"repos/example-org/dfe-engine/actions/workflows/1/runs": [older]})
    assert [f.id for f in _by_tool(_collect(api))["ci_runs"]] == ["100"]


def test_a_dependabot_update_run_is_never_read_but_a_code_scanning_run_is() -> None:
    failed = {"conclusion": "failure", "head_sha": "c" * 40, "created_at": "2026-03-01T00:00:00Z"}
    api, calls = _answers({
        "repos/example-org/dfe-engine/actions/workflows": [
            {"id": 1, "path": ".github/workflows/ci.yml", "state": "active"},
            {"id": 7, "path": "dynamic/dependabot/dependabot-updates", "state": "active"},
            {"id": 8, "path": "dynamic/github-code-scanning/codeql", "state": "active"},
        ],
        "repos/example-org/dfe-engine/actions/workflows/7/runs": AssertionError("dependabot runs read"),
        "repos/example-org/dfe-engine/actions/workflows/8/runs": [
            {**failed, "id": 108, "name": "CodeQL", "path": "dynamic/github-code-scanning/codeql",
             "workflow_id": 8},
        ],
    })
    findings = _by_tool(_collect(api))["ci_runs"]
    assert sorted(f.id for f in findings) == ["100", "108"]
    assert not any("/workflows/7/" in endpoint for endpoint, _, _ in calls)


def test_a_workflow_whose_runs_cannot_be_read_is_named_and_the_rest_still_count() -> None:
    api, _ = _answers({
        "repos/example-org/dfe-engine/actions/workflows/2/runs": sweep.GhError(
            "x", None, "gh timed out after 120s"),
    })
    result = _collect(api)
    assert [(u.tool, u.reason) for u in result.unreadable] == [
        ("ci_runs", ".github/workflows/release.yml: gh timed out after 120s"),
        ("ci_annotations", ".github/workflows/release.yml: gh timed out after 120s"),
    ]
    assert [f.id for f in _by_tool(result)["ci_runs"]] == ["100"]


def test_a_refused_workflow_list_is_unreadable_and_never_zero() -> None:
    api, _ = _answers({
        "repos/example-org/dfe-engine/actions/workflows": sweep.GhError(
            "x", 403, "Resource not accessible by integration (HTTP 403)"),
    })
    result = _collect(api)
    assert [u.tool for u in result.unreadable] == ["ci_runs", "ci_annotations"]
    assert not {"ci_runs", "ci_annotations"} & set(_by_tool(result))


def test_a_failed_run_behind_the_head_says_so() -> None:
    run = {"id": 5, "name": "CI", "conclusion": "failure", "head_sha": "c" * 40}
    (finding,) = sweep.run_findings("r", [run], HEAD)
    assert finding.severity == "high"
    assert "not the head" in finding.title
    assert finding.extra["at_head"] is False


def test_the_dashboard_flags_what_waits_and_nothing_else() -> None:
    items = sweep.dashboard_items(DASHBOARD_BODY)
    sections = [(section, severity, key) for section, severity, key, _ in items]
    assert sections == [
        ("Repository problems", "medium", "WARN: Package lookup failures"),
        ("Errored", "medium", "renovate/foo-1.x"),
        ("Rate-Limited", "low", "renovate/bar-2.x"),
        ("Deprecations / Replacements", "low", "npm old-lib #12"),
    ]


def test_a_dashboard_comment_is_stripped_however_it_ends() -> None:
    assert sweep._clean("a <!-- one\ntwo --> b") == "a b"
    assert sweep._clean("a <!-- one --!> b") == "a b"


def test_severity_words_land_on_the_ladder() -> None:
    assert sweep.level("moderate", "info") == "medium"
    assert sweep.level("error", "info") == "info"
    assert sweep.level(None, "medium") == "medium"
    assert sweep.level("CRITICAL", "low") == "critical"


def test_only_a_security_tools_own_severity_reaches_critical() -> None:
    rules = sweep.code_scanning_findings("r", [
        {"number": 1, "security_severity": "critical", "rule_severity": "warning"},
        {"number": 2, "security_severity": None, "rule_severity": "error"},
        {"number": 3, "security_severity": None, "rule_severity": "critical"},
    ])
    assert [f.severity for f in rules] == ["critical", "high", "medium"]
    secrets = sweep.secret_scanning_findings("r", [
        {"number": 1, "validity": "active", "publicly_leaked": True},
        {"number": 2, "validity": "inactive", "publicly_leaked": False},
    ])
    assert [f.severity for f in secrets] == ["high", "medium"]
    capped = {"critical", "high"}
    assert set(sweep.ANNOTATION_SEVERITY.values()).isdisjoint(capped)
    assert {severity for _, severity in sweep.DASHBOARD_SECTIONS}.isdisjoint(capped)
    assert "critical" not in sweep.RUN_SEVERITY.values()
    assert "critical" not in sweep.RULE_SEVERITY.values()


def test_findings_rank_by_severity_then_repo_tool_and_id() -> None:
    def make(severity: str, repo: str, ident: str) -> sweep.Finding:
        return sweep.Finding(repo, "bot_prs", ident, severity, "t", "u", "c")

    ranked = sweep.rank([make("low", "a", "10"), make("critical", "b", "2"),
                         make("low", "a", "9"), make("info", "a", "1")])
    assert [(f.severity, f.id) for f in ranked] == [
        ("critical", "2"), ("low", "9"), ("low", "10"), ("info", "1")
    ]


def test_the_sweep_keeps_member_order() -> None:
    api, _ = _answers({"repos/example-org/dfe-ui": sweep.GhError("x", 404, "Not Found")})
    other = sweep.Member("dfe-ui", "example-org/dfe-ui", ("stack apps",))
    graphql, _ = _graphql()
    results = sweep.sweep([other, MEMBER], api, CONFIG, graphql=graphql, projects=PROJECTS)
    assert [r.member.name for r in results] == ["dfe-ui", "dfe-engine"]
    assert results[0].unreadable
    assert not results[0].findings
    assert results[1].unreadable == []


# ---------------------------------------------------------------------------
# The backlog, as hyperi-ai's pm backlog models it
# ---------------------------------------------------------------------------


def test_backlog_rows_flatten_open_issues_only() -> None:
    rows = sweep.backlog_rows("example-org/dfe-engine", 7, PROJECT_NODES)
    assert [(r["number"], r["issue_repo"]) for r in rows] == [
        (31, "example-org/dfe-engine"),
        (33, "example-org/dfe-engine"),
        (32, "example-org/dfe-engine"),
        (36, "example-org/dfe-engine"),
        (31, "other-org/elsewhere"),
    ]
    first = rows[0]
    assert {key: first[key] for key in sweep.BACKLOG_FIELDS} == {
        "repo": "example-org/dfe-engine",
        "project": 7,
        "status": "Backlog",
        "priority": "P2",
        "effort": "4h",
        "type": "Task",
        "number": 31,
        "title": "item 31",
        "url": "https://example.com/example-org/dfe-engine/issues/31",
        "created_at": "2026-10-01T00:00:00Z",
    }
    assert rows[2]["priority"] == ""
    assert rows[1]["labels"] == ["backlog"]


def test_backlog_is_grouped_by_status_then_highest_priority_first() -> None:
    model = sweep.BacklogModel()
    rows = [
        {"status": "Next", "priority": "P0", "number": 1},
        {"status": "Backlog", "priority": "", "number": 2},
        {"status": "Backlog", "priority": "P3", "number": 3},
        {"status": "Backlog", "priority": "P1", "number": 4},
        {"status": "Triage", "priority": "P0", "number": 5},
        {"status": "Backlog", "priority": "P1", "number": 0},
    ]
    ordered = [(r["status"], r["priority"], r["number"]) for r in sweep.sort_backlog(rows, model)]
    assert ordered == [
        ("Backlog", "P1", 0),
        ("Backlog", "P1", 4),
        ("Backlog", "P3", 3),
        ("Backlog", "", 2),
        ("Next", "P0", 1),
        ("Triage", "P0", 5),
    ]


def test_no_project_is_a_normal_state_not_unreadable() -> None:
    api, _ = _answers()
    result = _collect(api, projects=sweep.ProjectIndex(numbers={"some-other-repo": 3}))
    assert result.project == sweep.NO_PROJECT
    assert [(r["number"], r["project"], r["status"]) for r in result.backlog] == [
        (32, None, ""), (33, None, ""), (34, None, ""),
    ], "with no project the label alone puts an issue on the backlog"
    assert [issue["number"] for issue in result.issues] == [31, 35, 36]
    assert result.unreadable == []
    assert [(d["number"], d["problem"]) for d in result.drift] == [
        (32, "labelled backlog but the repo has no backlog project"),
        (33, "labelled backlog but the repo has no backlog project"),
        (34, "labelled backlog but the repo has no backlog project"),
    ]


def test_projects_the_token_cannot_list_are_unreadable() -> None:
    api, _ = _answers()
    missing = "your authentication token is missing required scopes [read:project]"
    result = _collect(api, projects=sweep.ProjectIndex(error=missing))
    assert result.project == sweep.NOT_READ
    assert [(u.tool, u.reason) for u in result.unreadable] == [
        ("backlog", f"projects not readable: {missing}")
    ]
    assert result.drift == [], "drift cannot be judged without the project"
    assert [issue["number"] for issue in result.issues] == [31, 35, 36]
    assert [row["number"] for row in result.backlog] == [32, 33, 34]


@pytest.mark.parametrize(
    ("nodes", "reason"),
    [
        (sweep.GhError("graphql", None, "GraphQL: Something went wrong"), "Something went wrong"),
        ([{"missing_project": True}], "project 7 not readable"),
    ],
)
def test_a_project_that_cannot_be_read_is_unreadable(nodes, reason) -> None:
    api, _ = _answers()
    result = _collect(api, nodes=nodes)
    assert result.project == sweep.NOT_READ
    assert [u.tool for u in result.unreadable] == ["backlog"]
    assert reason in result.unreadable[0].reason


def test_the_backlog_query_reads_and_names_the_project() -> None:
    api, _ = _answers()
    graphql, calls = _graphql()
    sweep.collect_repo(MEMBER, api, CONFIG, graphql=graphql, projects=PROJECTS)
    ((query, variables, jq),) = calls
    assert query.lstrip().startswith("query")
    assert "mutation" not in query
    assert variables == {"org": "example-org", "number": 7}
    assert "first: 100" in query
    assert "missing_project" in jq


def test_label_drift_is_both_directions_and_only_this_repo() -> None:
    api, _ = _answers()
    drift = {(d["number"], d["problem"]) for d in _collect(api).drift}
    assert drift == {
        (31, "at Backlog without the backlog label"),
        (32, "labelled backlog but at Next"),
        (34, "labelled backlog but not on the project"),
    }


def test_the_summary_counts_by_status_and_priority() -> None:
    api, _ = _answers()
    report = sweep.build_report("2.2.0", [_collect(api)], CONFIG.tools, generated_at="t")
    text = sweep.render_text(report, CONFIG.backlog)
    summary = text.split("SUMMARY", 1)[1].split("\n\n", 1)[0]
    assert (
        "example-org/dfe-engine: 2 active issue(s); backlog project 7: 5 item(s), status "
        "Backlog 3, Next 1, no status 1; priority P0 1, P2 1, unprioritised 3; 3 label drift"
    ) in summary
    assert "    backlog  Backlog      P0   #33 item 33" in summary
    assert "    backlog  -            -    #34 labelled, on no project" in summary
    assert text.index("SUMMARY") < text.index("CRITICAL")
    assert "work items: 2 active issue(s), 5 backlog item(s), 3 label drift" in text
    assert (
        "  gating: 14 (12 finding(s), 2 active issue(s), 0 not readable), the backlog never gates"
    ) in text


def test_a_repo_with_no_project_says_so_in_the_summary() -> None:
    api, _ = _answers()
    result = _collect(api, projects=sweep.ProjectIndex())
    report = sweep.build_report("2.2.0", [result], CONFIG.tools, generated_at="t")
    assert (
        "3 active issue(s); backlog, no project: 3 item(s), status no status 3; priority "
        "unprioritised 3; 3 label drift"
    ) in sweep.render_text(report)
    empty = {"issues": [], "backlog": [], "drift": []}
    model = sweep.BacklogModel()
    assert sweep._work_counts(sweep.NO_PROJECT, empty, model) == (
        "0 active issue(s); backlog: no project"
    )
    assert sweep._work_counts(sweep.NOT_READ, empty, model) == (
        "0 active issue(s); backlog: project not read"
    )
    assert sweep._work_counts("", empty, model) == "0 active issue(s); backlog: project off"


# ---------------------------------------------------------------------------
# --format slack
# ---------------------------------------------------------------------------


# The time every Slack report is generated at; the rows below were raised 13 days before it.
NOW = "2026-10-14T00:00:00+00:00"
RAISED = "2026-10-01T00:00:00Z"


def _finding(repo: str, tool: str, ident: int, severity: str, title: str = "t") -> sweep.Finding:
    return sweep.Finding(f"example-org/{repo}", tool, str(ident), severity, title,
                         f"https://example.com/{repo}/{tool}/{ident}", RAISED)


def _slack_report(findings=(), *, issues=0, title="an issue", bridge=None, unreadable=()):
    members: dict[str, list] = {}
    for finding in findings:
        members.setdefault(finding.repo, []).append(finding)
    member = sweep.Member("dfe-engine", "example-org/dfe-engine", ("stack apps",))
    rows = [
        {"repo": "example-org/dfe-engine", "number": n, "title": f"{title} {n}", "labels": [],
         "type": "", "url": f"https://example.com/i/{n}", "created_at": RAISED}
        for n in range(1, issues + 1)
    ]
    results = [
        sweep.RepoResult(member, "main", HEAD, False, members.pop(member.repo, []),
                         list(unreadable), issues=rows, project=sweep.NO_PROJECT)
    ]
    results += [
        sweep.RepoResult(sweep.Member(repo.split("/")[1], repo, ("stack apps",)), "main", HEAD,
                         False, found, [])
        for repo, found in members.items()
    ]
    return sweep.build_report("2.2.0", results, sweep.TOOLS, bridge, generated_at=NOW)


EVERYTHING = [
    _finding("scalo-py", "secret_scanning", 1, "high"),
    _finding("dfe-ui", "dependabot", 2, "high"),
    _finding("dfe-engine", "code_scanning", 3, "critical"),
    _finding("logreducer", "ci_runs", 4, "high"),
    _finding("dfe-loader", "ci_annotations", 5, "low"),
    _finding("dfe-docker", "bot_prs", 6, "low"),
    _finding("dfe-infra", "renovate_dashboard", 7, "low"),
    _finding("dfe-ui", "dependabot", 8, "medium"),
    _finding("dfe-ui", "advisories", 9, "high"),
]


def test_the_slack_summary_never_passes_eight_lines() -> None:
    many = EVERYTHING + [_finding(f"repo-{n}", "code_scanning", n, "high") for n in range(300)]
    bridge = sweep.bridge_section([_alert("dependabot", "o/r", "https://example.com/1")], 9)
    unreadable = [sweep.Unreadable("example-org/dfe-engine", "backlog", "no scope")]
    report = _slack_report(many, issues=50, bridge=bridge, unreadable=unreadable)
    summary = sweep.render_slack(report)["summary"].splitlines()
    assert len(summary) == sweep.SLACK_SUMMARY_LINES
    assert summary[0].startswith("*DFE suite sweep*: stack 2.2.0, ")
    assert summary[-1].startswith("*Alert bridge gap*")


def test_the_gating_line_leads_the_slack_summary_and_secret_scanning_follows() -> None:
    summary = sweep.render_slack(_slack_report(EVERYTHING, issues=2))["summary"].splitlines()
    assert summary[1] == "*Gating: 11* -- 9 findings, 2 active issues"
    assert summary[2] == "*Secret scanning: 1 open alert(s)* on scalo-py 1"
    assert summary[3].startswith("*Security: 1 critical, 2 high* on ")
    assert summary[4] == "*Failing main CI*: 1 workflow(s) on logreducer 1"


def test_an_all_clear_sweep_is_a_two_line_summary() -> None:
    clear = sweep.render_slack(_slack_report())["summary"].splitlines()
    assert clear[1] == "All clear: nothing open on any member."
    assert len(clear) == 2
    held = sweep.bridge_section([], 4)
    assert len(sweep.render_slack(_slack_report(bridge=held))["summary"].splitlines()) == 2


def test_slack_stats_always_carry_every_key_as_an_integer() -> None:
    empty = sweep.render_slack(_slack_report())["stats"]
    assert list(empty) == list(sweep.SLACK_STATS)
    assert set(empty.values()) == {0, 1}
    assert empty["members"] == 1
    stats = sweep.render_slack(_slack_report(EVERYTHING, issues=3))["stats"]
    assert {k: stats[k] for k in ("secrets", "critical", "high", "ci_failing", "issues",
                                  "bot_prs")} == {
        "secrets": 1, "critical": 1, "high": 2, "ci_failing": 1, "issues": 3, "bot_prs": 1,
    }
    assert all(isinstance(value, int) for value in stats.values())


def _summary_line(pattern: str, summary: str) -> re.Match:
    match = re.search(pattern, summary, re.MULTILINE)
    assert match, f"{pattern!r} not in:\n{summary}"
    return match


def test_stats_count_severity_exactly_as_the_summary_lines_do() -> None:
    findings = [
        *EVERYTHING,
        _finding("scalo-py", "secret_scanning", 10, "high"),
        _finding("scalo-rs", "secret_scanning", 11, "medium"),
        _finding("dfe-ui", "ci_runs", 12, "high"),
        _finding("dfe-ui", "ci_runs", 13, "low"),
        _finding("dfe-loader", "code_scanning", 14, "high"),
        _finding("dfe-loader", "code_scanning", 15, "critical"),
        _finding("dfe-loader", "ci_annotations", 16, "medium"),
    ]
    slack = sweep.render_slack(_slack_report(findings))
    stats, summary = slack["stats"], slack["summary"]
    security = _summary_line(r"^\*Security: (\d+) critical, (\d+) high\* on ", summary)
    rest = _summary_line(r"^Medium (\d+), low (\d+): (.*)$", summary)
    secrets = _summary_line(r"^\*Secret scanning: (\d+) open alert\(s\)\*", summary)
    failing = _summary_line(r"^\*Failing main CI\*: (\d+) workflow\(s\)", summary)
    assert (stats["critical"], stats["high"]) == tuple(map(int, security.groups())) == (2, 3)
    assert (stats["medium"], stats["low"]) == tuple(map(int, rest.groups()[:2])) == (3, 4)
    assert stats["secrets"] == int(secrets.group(1)) == 3
    assert stats["ci_failing"] == int(failing.group(1)) == 2
    groups = [int(part.rsplit(" ", 1)[1]) for part in rest.group(3).split(", ")]
    assert sum(groups) == stats["medium"] + stats["low"]


def test_slack_detail_groups_come_in_order_and_empty_ones_are_left_out() -> None:
    findings = [
        _finding("dfe-ui", "dependabot", 2, "high"),
        _finding("scalo-py", "secret_scanning", 1, "critical"),
        _finding("dfe-loader", "ci_annotations", 5, "low"),
    ]
    detail = sweep.render_slack(_slack_report(findings, issues=2))["detail"]
    assert [reply["group"] for reply in detail] == [
        "Secret scanning", "Dependabot", "CI on main", "Active issues"
    ]
    assert detail[1]["text"].splitlines()[:3] == [
        "*Dependabot* -- 1 (1 high)",
        "*dfe-ui*",
        "- <https://example.com/dfe-ui/dependabot/2|dependabot #2> high: t (13d)",
    ]


def _shown(detail: list[dict]) -> int:
    return sum(reply["text"].count("\n- <") for reply in detail)


def _more(detail: list[dict]) -> int:
    return int(detail[-1]["text"].splitlines()[-1].strip("_").split()[0])


def test_a_long_group_continues_under_a_cont_header() -> None:
    findings = [_finding("dfe-ui", "dependabot", n, "medium", "x" * 120) for n in range(60)]
    detail = sweep.render_slack(_slack_report(findings), group_replies=10)["detail"]
    assert len(detail) > 2
    assert {reply["group"] for reply in detail} == {"Dependabot"}
    assert detail[0]["text"].startswith("*Dependabot* -- 60 (60 medium)")
    for reply in detail[1:]:
        assert reply["text"].startswith("*Dependabot (cont.)*\n*dfe-ui*\n- <")
    assert all(len(reply["text"]) < sweep.SLACK_REPLY_CHARS for reply in detail)
    assert _shown(detail) == 60


@pytest.mark.parametrize(
    ("url", "ending"),
    [
        ("", "more, run `dfe-sweep` for the full list_"),
        ("https://example.com/full", "more, see <https://example.com/full|full report>_"),
    ],
)
def test_a_group_past_its_budget_ends_with_how_many_more(url, ending) -> None:
    detail = sweep.render_slack(_slack_report(issues=2000, title="y" * 200), report_url=url)[
        "detail"
    ]
    assert [reply["group"] for reply in detail] == ["Active issues"] * 2
    assert detail[-1]["text"].splitlines()[-1].endswith(ending)
    assert _more(detail) == 2000 - _shown(detail)
    assert all(len(reply["text"]) < sweep.SLACK_REPLY_CHARS for reply in detail)


def test_the_whole_thread_still_stops_at_twenty_replies() -> None:
    slack = sweep.render_slack(_slack_report(issues=2000, title="y" * 200), group_replies=50)
    detail = slack["detail"]
    assert len(detail) == sweep.SLACK_REPLIES
    assert _more(detail) == 2000 - _shown(detail)


def test_every_non_empty_group_posts_however_long_the_others_are() -> None:
    flood = [_finding("dfe-engine", "code_scanning", n, "critical", "z" * 250)
             for n in range(400)]
    flood += [_finding("dfe-loader", "ci_annotations", n, "low", "w" * 250) for n in range(400)]
    flood += [
        _finding("scalo-py", "secret_scanning", 1, "high"),
        _finding("dfe-ui", "dependabot", 2, "high"),
        _finding("dfe-ui", "advisories", 3, "high"),
        _finding("dfe-docker", "bot_prs", 4, "low"),
    ]
    unreadable = [sweep.Unreadable("example-org/dfe-hyperdx", "code_scanning", "HTTP 403")]
    report = _slack_report(flood, issues=300, unreadable=unreadable)
    row = {"repo": "example-org/dfe-engine", "number": 9, "title": "t", "url": "https://e.com/9"}
    report["backlog"] = [{**row, "project": 1, "status": "Backlog", "priority": "P1",
                          "effort": "", "type": "Task"}]
    report["backlog_drift"] = [{**row, "status": "Backlog", "problem": "at Backlog"}]
    detail = sweep.render_slack(report)["detail"]
    groups = list(dict.fromkeys(reply["group"] for reply in detail))
    assert groups == [
        "Secret scanning", "Code scanning", "Dependabot", "Security advisories", "CI on main",
        "Bot PRs and Renovate", "Active issues", "Not readable", "Backlog items",
        "Backlog drift",
    ]
    for name in ("Code scanning", "CI on main", "Active issues"):
        replies = [reply for reply in detail if reply["group"] == name]
        assert len(replies) == 2, name
        assert "more, run `dfe-sweep`" in replies[-1]["text"].splitlines()[-1]
    assert len(detail) <= sweep.SLACK_REPLIES


def test_a_hostile_title_renders_inert() -> None:
    hostile = "<!channel> & <script>"
    findings = [_finding("dfe-ui", "dependabot", 2, "high", hostile)]
    slack = sweep.render_slack(_slack_report(findings, issues=1, title=hostile))
    text = slack["summary"] + "".join(reply["text"] for reply in slack["detail"])
    assert "<!channel>" not in text
    assert "<script>" not in text
    assert "&lt;!channel&gt; &amp; &lt;script&gt;" in text
    assert sweep.slack_link("https://example.com/a>b", "x") == "x"
    assert sweep.slack_escape("a|b<c") == "a|b&lt;c"


def _assert_replies_fit(detail: list[dict]) -> None:
    for reply in detail:
        assert len(reply["text"]) <= sweep.SLACK_REPLY_CHARS, reply["group"]
        assert "\n- " in reply["text"], f"header-only reply in {reply['group']}"


def test_an_issue_with_many_long_labels_still_fits_a_reply() -> None:
    report = _slack_report(issues=3)
    report["issues"][0]["labels"] = [f"label-{n:02d}-" + "x" * 41 for n in range(100)]
    detail = sweep.render_slack(report)["detail"]
    _assert_replies_fit(detail)
    assert detail[0]["text"].count("\n- <") == 3
    assert detail[0]["text"].splitlines()[2].endswith("... (13d)")


def test_an_escape_heavy_title_fits_and_is_never_cut_inside_an_entity() -> None:
    findings = [_finding("dfe-ui", "dependabot", n, "high", "&" * 300) for n in range(40)]
    findings += [_finding("dfe-ui", "dependabot", 99, "high", "<&>" * 100)]
    detail = sweep.render_slack(_slack_report(findings), group_replies=20)["detail"]
    _assert_replies_fit(detail)
    for reply in detail:
        for line in reply["text"].splitlines():
            assert not re.search(r"&[a-z]*\.\.\.$", line), line


def test_one_oversize_line_is_clipped_not_left_alone_in_a_reply() -> None:
    finding = _finding("dfe-infra", "renovate_dashboard", 1, "low")
    huge = sweep.Finding(finding.repo, finding.tool, "33:" + "k" * 9000, "low", "t" * 9000,
                         finding.url, finding.created_at)
    detail = sweep.render_slack(_slack_report([huge, finding]))["detail"]
    _assert_replies_fit(detail)
    assert sum(reply["text"].count("\n- ") for reply in detail) == 2


def test_pack_never_emits_a_header_only_reply() -> None:
    lines = [("repo", "*r*"), ("item", "- " + "a" * 9000), ("repo", "*s*"), ("item", "- b")]
    replies = sweep._pack("Group", "2", lines)
    assert all(any(kind == "item" for kind, _ in reply) for reply in replies)
    for reply in replies:
        assert len("\n".join(text for _, text in reply)) <= sweep.SLACK_REPLY_CHARS


def test_a_clipped_line_never_ends_inside_a_link() -> None:
    clipped = sweep._clip_line("- <https://example.com/" + "p" * 50 + "|label> tail", 40)
    assert "<" not in clipped
    assert clipped.endswith("...")


def test_with_more_keeps_the_last_item_and_says_how_many_were_cut() -> None:
    reply = [("header", "*G*"), ("repo", "*r*")]
    reply += [("item", f"- {'i' * 300} {n}") for n in range(20)]
    text = sweep._with_more(reply, 5, "")
    assert len(text) <= sweep.SLACK_REPLY_CHARS
    last = text.splitlines()[-1]
    shown = text.count("\n- ")
    assert last == f"_{5 + 20 - shown} more, run `dfe-sweep` for the full list_"
    assert sweep._with_more(reply[:3], 0, "") == "*G*\n*r*\n" + reply[2][1]


# ---------------------------------------------------------------------------
# --format slack headline and ages
# ---------------------------------------------------------------------------

SYDNEY = ZoneInfo("Australia/Sydney")


def _headline(generated_at: str, **kwargs) -> str:
    report = _slack_report()
    report["generated_at"] = generated_at
    return sweep.render_slack(report, **kwargs)["summary"].splitlines()[0]


@pytest.mark.parametrize(
    ("generated_at", "zone", "expected"),
    [
        # Sydney keeps daylight saving from the first Sunday of October to the first of April.
        ("2026-10-12T18:00:00+00:00", SYDNEY, "2026-10-13 05:00 AEDT"),
        ("2026-07-12T19:00:00+00:00", SYDNEY, "2026-07-13 05:00 AEST"),
        ("2026-10-03T15:59:00+00:00", SYDNEY, "2026-10-04 01:59 AEST"),
        ("2026-10-03T16:00:00+00:00", SYDNEY, "2026-10-04 03:00 AEDT"),
        ("2026-10-12T18:00:00+00:00", UTC, "2026-10-12 18:00 UTC"),
        ("2026-10-12T18:00:00Z", UTC, "2026-10-12 18:00 UTC"),
        ("2026-10-13T05:00:59+11:00", UTC, "2026-10-12 18:00 UTC"),
        ("2026-10-12T18:00:00+00:00", ZoneInfo("Europe/Berlin"), "2026-10-12 20:00 CEST"),
    ],
)
def test_the_headline_time_is_in_the_zone_with_that_instants_abbreviation(
    generated_at, zone, expected
) -> None:
    line = _headline(generated_at, zone=zone)
    assert line == f"*DFE suite sweep*: stack 2.2.0, 1 members, {expected}"


def test_the_headline_defaults_to_the_old_title_and_utc() -> None:
    assert _headline("2026-10-12T18:00:00+00:00") == (
        "*DFE suite sweep*: stack 2.2.0, 1 members, 2026-10-12 18:00 UTC"
    )


def test_the_headline_title_is_a_parameter_and_is_escaped() -> None:
    assert _headline(NOW, title="DFE daily summary", zone=SYDNEY) == (
        "*DFE daily summary*: stack 2.2.0, 1 members, 2026-10-14 11:00 AEDT"
    )
    assert _headline(NOW, title="<!channel> & co").startswith("*&lt;!channel&gt; &amp; co*: ")


@pytest.mark.parametrize("bad", ["t", "", "yesterday", "2026-13-45T00:00:00Z"])
def test_a_generated_time_that_is_not_a_timestamp_is_a_run_error(bad: str) -> None:
    with pytest.raises(FleetError, match="not an RFC 3339 timestamp"):
        _headline(bad)


def _ago(**span) -> str:
    return (datetime.fromisoformat(NOW) - timedelta(**span)).isoformat()


@pytest.mark.parametrize(
    ("span", "suffix"),
    [
        ({}, " (<1h)"),
        ({"minutes": 59}, " (<1h)"),
        ({"minutes": 59, "seconds": 59}, " (<1h)"),
        ({"hours": 1}, " (1h)"),
        ({"hours": 5, "minutes": 30}, " (5h)"),
        ({"hours": 23, "minutes": 59}, " (23h)"),
        ({"hours": 24}, " (1d)"),
        ({"hours": 47, "minutes": 59}, " (1d)"),
        ({"days": 2}, " (2d)"),
        ({"days": 6}, " (6d)"),
        ({"days": 6, "hours": 23}, " (6d)"),
        ({"days": 400}, " (400d)"),
        ({"minutes": -5}, " (<1h)"),
    ],
)
def test_an_age_is_hours_under_a_day_then_whole_days(span, suffix) -> None:
    assert sweep._age_suffix(_ago(**span), datetime.fromisoformat(NOW)) == suffix


def test_an_age_reads_a_bare_date_and_an_offset_time() -> None:
    now = datetime.fromisoformat(NOW)
    assert sweep._age_suffix("2026-10-13", now) == " (1d)"
    assert sweep._age_suffix("2026-10-13T10:00:00+10:00", now) == " (1d)"
    assert sweep._age_suffix("2026-10-13T23:30:00Z", now) == " (<1h)"


def test_a_row_with_no_time_has_no_age_and_a_bad_time_is_a_run_error() -> None:
    now = datetime.fromisoformat(NOW)
    assert sweep._age_suffix("", now) == ""
    assert sweep._age_suffix(None, now) == ""
    with pytest.raises(FleetError, match="not an RFC 3339 timestamp"):
        sweep._age_suffix("last week", now)


def test_every_age_the_suffix_writes_is_one_the_clipper_recognises() -> None:
    now = datetime.fromisoformat(NOW)
    spans = [{}, {"hours": 7}, {"days": 1}, {"days": 365}, {"minutes": -1}]
    for span in spans:
        assert sweep._AGE_TAIL.search(f"- row{sweep._age_suffix(_ago(**span), now)}")


def test_every_line_kind_but_not_readable_ends_in_its_age() -> None:
    run = dataclasses.replace(
        _finding("example-dfe", "ci_runs", 38433690502, "high", "CI: failure"),
        created_at=_ago(days=6, hours=3),
    )
    report = _slack_report([run], issues=1, unreadable=[sweep.Unreadable("o/r", "backlog", "x")])
    report["issues"][0]["created_at"] = _ago(hours=5)
    row = {"repo": "example-org/dfe-engine", "number": 1, "title": "an issue 1",
           "url": "https://example.com/i/1"}
    report["backlog"] = [{**row, "project": 7, "status": "Next", "priority": "P1", "effort": "",
                          "type": "Task", "created_at": _ago(minutes=20)}]
    report["backlog_drift"] = [{**row, "status": "Next", "problem": "labelled backlog but at Next",
                                "created_at": _ago(days=2, hours=23)}]
    slack = sweep.render_slack(report)
    lines = {reply["group"]: reply["text"].splitlines()[-1] for reply in slack["detail"]}
    assert lines["CI on main"] == (
        "- <https://example.com/example-dfe/ci_runs/38433690502|ci_runs #38433690502> "
        "high: CI: failure (6d)"
    )
    assert lines["Active issues"] == "- <https://example.com/i/1|#1> an issue 1 (5h)"
    assert lines["Backlog items"] == "- <https://example.com/i/1|#1> Next P1: an issue 1 (<1h)"
    assert lines["Backlog drift"] == (
        "- <https://example.com/i/1|#1> labelled backlog but at Next: an issue 1 (2d)"
    )
    assert lines["Not readable"] == "- backlog: x"
    assert all(entry["line"] in lines.values() for entry in slack["keys"])


def test_an_issues_age_follows_its_labels_at_the_very_end_of_the_line() -> None:
    report = _slack_report(issues=1)
    report["issues"][0]["labels"] = ["bug", "backlog"]
    (reply,) = sweep.render_slack(report)["detail"]
    assert reply["text"].splitlines()[-1].endswith("an issue 1 [bug, backlog] (13d)")


def test_drift_and_backlog_rows_carry_the_created_at_of_their_issue() -> None:
    api, _ = _answers()
    result = _collect(api)
    assert {d["number"]: d["created_at"] for d in result.drift} == {
        31: "2026-10-01T00:00:00Z",
        32: "2026-10-03",
        34: "2026-10-03",
    }
    # Project items carry the project's createdAt, and a labelled issue off the project its own.
    assert {(r["number"], r["created_at"]) for r in result.backlog} == {
        (31, "2026-10-01T00:00:00Z"),
        (32, "2026-10-01T00:00:00Z"),
        (33, "2026-10-01T00:00:00Z"),
        (34, "2026-10-03"),
    }


def test_a_clipped_line_keeps_its_age_whole() -> None:
    link = "- <https://example.com/" + "p" * 50 + "|label> "
    for cap in range(12, 80):
        clipped = sweep._clip_line(link + "tail " * 40 + " (6d)", cap)
        assert len(clipped) <= cap, cap
        assert clipped.endswith("... (6d)"), cap
        assert "<" not in clipped or clipped.rfind("<") < clipped.rfind(">"), cap
    entities = "&amp;" * 60 + " (<1h)"
    for cap in range(12, 80):
        clipped = sweep._clip_line(entities, cap)
        assert len(clipped) <= cap, cap
        assert re.search(r"(&amp;)*\.\.\. \(<1h\)$", clipped), (cap, clipped)
    assert sweep._clip_line("short (6d)", 40) == "short (6d)"
    assert sweep._clip_line("x" * 50, 20) == "x" * 17 + "..."


def test_a_long_title_is_clipped_before_its_age_in_detail_and_keys_alike() -> None:
    finding = _finding("dfe-ui", "dependabot", 2, "high", "w" * 9000)
    slack = sweep.render_slack(_slack_report([finding], issues=1, title="v" * 9000))
    lines = [line for reply in slack["detail"] for line in reply["text"].splitlines()]
    items = [line for line in lines if line.startswith("- <")]
    assert len(items) == 2
    assert all(line.endswith("... (13d)") for line in items)
    assert [entry["line"] for entry in slack["keys"]] == items


def test_keys_do_not_depend_on_the_age_the_title_or_the_zone() -> None:
    def keys(generated_at: str, created_at: str, **kwargs) -> list[tuple[str, str]]:
        report = _keys_report()
        report["generated_at"] = generated_at
        for finding in report["findings"]:
            finding["created_at"] = created_at
        for rows in (report["issues"], report["backlog"], report["backlog_drift"]):
            for row in rows:
                row["created_at"] = created_at
        slack = sweep.render_slack(report, **kwargs)
        return [(entry["key"], entry["group"]) for entry in slack["keys"]]

    plain = keys(NOW, RAISED)
    assert plain == keys("2027-03-01T09:00:00+00:00", "2026-02-01T00:00:00Z")
    assert plain == keys(NOW, _ago(minutes=5), title="DFE daily summary", zone=SYDNEY)
    assert len(plain) == 14
    assert not any(" (" in key for key, _ in plain)


def test_the_same_report_gives_different_lines_as_it_ages_and_the_same_keys() -> None:
    first = sweep.render_slack(_keys_report())["keys"]
    report = _keys_report()
    report["generated_at"] = "2026-10-20T00:00:00+00:00"
    later = sweep.render_slack(report)["keys"]
    assert [e["key"] for e in first] == [e["key"] for e in later]
    pairs = zip(first, later, strict=True)
    aged = [(a["line"], b["line"]) for a, b in pairs if a["group"] != "Not readable"]
    assert all(a.endswith(" (13d)") and b.endswith(" (19d)") for a, b in aged)
    unread = [e["line"] for e in later if e["group"] == "Not readable"]
    assert unread == [e["line"] for e in first if e["group"] == "Not readable"]


# ---------------------------------------------------------------------------
# --format slack keys
# ---------------------------------------------------------------------------


def _note(message: str, *, level: str = "warning", path: str = ".github", line: int = 1,
          title: str = "") -> dict:
    return {"annotation_level": level, "path": path, "start_line": line, "title": title,
            "message": message}


def _annotation_keys(run: int, notes: list[dict], *, job: str = "ci / test",
                     workflow: str = ".github/workflows/ci.yml") -> list[str]:
    """The keys of one run's annotations, its run and job ids drawn from ``run``."""
    run_row = {"id": run, "name": f"CI #{run}", "path": workflow, "created_at": "2026-10-06"}
    jobs = [({"name": job, "html_url": f"https://example.com/r/{run}/job/{run + 1}"}, notes)]
    findings = sweep.annotation_findings("example-org/dfe-engine", run_row, jobs)
    return [sweep.finding_key(dataclasses.asdict(f)) for f in findings]


# Where uv unpacks a tool for one run; an annotation's path, never a file the tests open.
UV_TOOL = "/tmp/.tmp{}/archive-v0/{}/site-packages/hyperi_ci/common.py"  # noqa: S108

# The same annotations as two runs of one workflow on one commit raise them.
FIRST_RUN = [
    _note("Run 37415111799 took 1m 12s at 2026-10-06T04:44:13Z, see "
          "https://github.com/o/r/actions/runs/37415111799/job/112126939388", line=40),
    _note("[ERROR   ] hyperi_ci.common:error:389 - release-commit: GitHub refused the update",
          level="failure", line=389,
          path=UV_TOOL.format("0mtx6K", "utSTM5hRKd-Af4GC")),
    _note("1 releasable commit sits unreleased since v1.1.9, tagged 0 days ago (1 patch)."),
    _note("The self-hosted runner: arc-16cpu-x7k2p-runner-9fz4q lost communication with the "
          "server.", level="failure"),
    _note("Failed to save: Unable to reserve cache with key Linux-cargo-0a1b2c3d4e5f6a7b"),
    _note("/home/runner/work/_temp/6b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e.sh: line 3: x: not found",
          level="failure"),
]
SECOND_RUN = [
    _note("Run 37498576073 took 48.2s at 2026-10-07T09:01:55Z, see "
          "https://github.com/o/r/actions/runs/37498576073/job/112389570836", line=52),
    _note("[ERROR   ] hyperi_ci.common:error:384 - release-commit: GitHub refused the update",
          level="failure", line=384,
          path=UV_TOOL.format("nultcQ", "V0ayI27J5vtroQxV")),
    _note("1 releasable commit sits unreleased since v1.1.9, tagged 3 days ago (1 patch)."),
    _note("The self-hosted runner: arc-16cpu-m4q8z-runner-2hd7c lost communication with the "
          "server.", level="failure"),
    _note("Failed to save: Unable to reserve cache with key Linux-cargo-9f8e7d6c5b4a3f2e"),
    _note("/home/runner/work/_temp/0f9e8d7c-6b5a-4c3d-9e2f-1a0b9c8d7e6f.sh: line 3: x: not found",
          level="failure"),
]


def test_two_runs_of_one_ci_annotation_give_one_key() -> None:
    first = _annotation_keys(37415111799, FIRST_RUN)
    assert first == _annotation_keys(37498576073, SECOND_RUN)
    assert len(set(first)) == len(FIRST_RUN)
    assert all(key.startswith("example-org/dfe-engine:.github/workflows/ci.yml:ci / test:")
               for key in first)
    assert not any("37415111799" in key or "/job/" in key for key in first)


def test_different_ci_annotations_get_different_keys() -> None:
    base = _note("vulture: issues found (non-blocking)")
    variants = [
        _annotation_keys(1, [base]),
        _annotation_keys(1, [_note("ty: issues found (non-blocking)")]),
        _annotation_keys(1, [{**base, "annotation_level": "failure"}]),
        _annotation_keys(1, [{**base, "path": "docs/a.md"}]),
        _annotation_keys(1, [{**base, "title": "lint"}]),
        _annotation_keys(1, [base], job="ci / build"),
        _annotation_keys(1, [base], workflow=".github/workflows/release.yml"),
        _annotation_keys(1, [_note("cargo-deny: RUSTSEC-2026-0012 is unsound")]),
        _annotation_keys(1, [_note("cargo-deny: RUSTSEC-2026-0034 is unsound")]),
        _annotation_keys(1, [_note("pip-audit: CVE-2026-1111 in requests")]),
        _annotation_keys(1, [_note("pip-audit: CVE-2026-2222 in requests")]),
    ]
    keys = [key for (key,) in variants]
    assert len(set(keys)) == len(keys)


def test_the_normaliser_keeps_advisory_ids_and_drops_what_varies() -> None:
    text = "CVE-2026-12345 at 2026-10-07T01:02:03Z after 1m 30s on runner: arc-x7k2p, job 4471"
    assert sweep.normalise_message(text) == (
        "CVE-2026-12345 at <number>-<number>-<number>T<number>:<number>:<number>Z after "
        "<duration> on <runner>, job <number>"
    )
    assert sweep.normalise_message("  two\n  lines  ") == "two lines"
    assert sweep.normalise_message(None) == ""
    assert sweep.normalise_message("to v1.2.3 digest a29215f", keep_numbers=True) == (
        "to v1.2.3 digest <sha>"
    )


def test_a_failed_main_run_is_keyed_on_its_workflow_never_its_run() -> None:
    def key(run: int, conclusion: str = "failure", path: str = ".github/workflows/ci.yml",
            sha: str = HEAD) -> str:
        row = {"id": run, "name": f"Update #{run}", "path": path, "conclusion": conclusion,
               "head_sha": sha, "html_url": f"https://example.com/r/{run}"}
        (finding,) = sweep.run_findings("example-org/dfe-engine", [row], HEAD)
        return sweep.finding_key(dataclasses.asdict(finding))

    assert key(100) == key(200) == key(300, sha="c" * 40)
    assert key(100).startswith("example-org/dfe-engine:.github/workflows/ci.yml::")
    assert len({key(100), key(100, "timed_out"), key(100, path="dynamic/x")}) == 3


def test_dashboard_entries_keep_their_versions_and_lose_their_digests() -> None:
    def key(text: str, section: str = "Pending Status Checks") -> str:
        row = {"repo": "example-org/dfe-infra", "tool": "renovate_dashboard", "id": "9:b",
               "title": f"{section}: {text}", "url": "https://example.com/i/9"}
        return sweep.finding_key(row)

    digest = "fix(deps): update debian:trixie-slim Docker digest to a29215f"
    assert key(digest) == key(digest.replace("a29215f", "0b1c2d3"))
    assert key("update foo to v1.2.4") != key("update foo to v2.0.0")
    assert key("update foo to v1.2.4") != key("update foo to v1.2.4", "Errored")
    assert key(digest).startswith("example-org/dfe-infra:renovate_dashboard:")


def _keys_report() -> dict:
    """A report with a row in every group, issue 1 open, on the backlog and drifting."""
    ci_extra = {"workflow": ".github/workflows/ci.yml", "jobs": ["test"], "level": "warning",
                "path": ".github", "title": "", "message": "Node.js 20 is deprecated"}
    rebuilt = ("ci_runs", "ci_annotations", "renovate_dashboard")
    findings = [f for f in EVERYTHING if f.tool not in rebuilt]
    findings += [
        dataclasses.replace(_finding("dfe-loader", "ci_annotations", 5, "low"), extra=ci_extra),
        dataclasses.replace(_finding("logreducer", "ci_runs", 4, "high"),
                            extra={"workflow": ".github/workflows/ci.yml",
                                   "conclusion": "failure"}),
        dataclasses.replace(_finding("dfe-infra", "renovate_dashboard", 7, "low"),
                            title="Errored: update foo to v2"),
    ]
    unreadable = [sweep.Unreadable("example-org/dfe-engine", "backlog", "no scope"),
                  sweep.Unreadable("example-org/dfe-engine", "backlog", "timed out")]
    report = _slack_report(findings, issues=2, unreadable=unreadable)
    row = {"repo": "example-org/dfe-engine", "number": 1, "title": "an issue 1",
           "url": "https://example.com/i/1", "created_at": RAISED}
    report["backlog"] = [{**row, "project": 7, "status": "Next", "priority": "P1",
                          "effort": "", "type": "Task"}]
    report["backlog_drift"] = [{**row, "status": "Next",
                                "problem": "labelled backlog but at Next"}]
    return report


def test_slack_keys_list_every_row_once_with_its_detail_line() -> None:
    slack = sweep.render_slack(_keys_report())
    keys, detail = slack["keys"], slack["detail"]
    assert list(slack) == ["summary", "detail", "stats", "keys"]
    assert len({entry["key"] for entry in keys}) == len(keys)
    assert list(dict.fromkeys(entry["group"] for entry in keys)) == list(
        dict.fromkeys(reply["group"] for reply in detail)
    )
    lines = list(chain.from_iterable(reply["text"].splitlines() for reply in detail))
    assert all(entry["line"] in lines for entry in keys)
    # Every item line in detail but the second not-readable line, whose key the first holds.
    assert len(keys) == sum(text.startswith("- ") for text in lines) - 1
    by_group: dict[str, list[str]] = {}
    for entry in keys:
        by_group.setdefault(entry["group"], []).append(entry["key"])
    assert by_group["Dependabot"] == ["https://example.com/dfe-ui/dependabot/2",
                                      "https://example.com/dfe-ui/dependabot/8"]
    assert by_group["Active issues"] == ["https://example.com/i/1", "https://example.com/i/2"]
    assert by_group["Backlog items"] == ["example-org/dfe-engine:backlog:https://example.com/i/1"]
    assert by_group["Backlog drift"] == ["example-org/dfe-engine:drift:https://example.com/i/1"]
    assert by_group["Not readable"] == ["example-org/dfe-engine:backlog"]
    assert [key.rsplit(":", 1)[0] for key in by_group["CI on main"]] == [
        "example-org/logreducer:.github/workflows/ci.yml:",
        "example-org/dfe-loader:.github/workflows/ci.yml:test",
    ]
    assert by_group["Bot PRs and Renovate"][0] == "https://example.com/dfe-docker/bot_prs/6"
    assert by_group["Bot PRs and Renovate"][1].startswith(
        "example-org/dfe-infra:renovate_dashboard:"
    )


def test_rows_the_reply_budget_cut_are_still_keyed() -> None:
    slack = sweep.render_slack(_slack_report(issues=2000, title="y" * 200))
    assert _shown(slack["detail"]) < 2000
    assert len(slack["keys"]) == 2000


def test_a_key_line_is_clipped_exactly_as_detail_clips_it() -> None:
    report = _slack_report(issues=1, title="<!channel> &")
    report["issues"][0]["labels"] = [f"label-{n:02d}-" + "x" * 41 for n in range(100)]
    slack = sweep.render_slack(report)
    (entry,) = slack["keys"]
    assert entry["line"].endswith("... (13d)")
    assert "&lt;!channel&gt; &amp;" in entry["line"]
    assert entry["line"] == slack["detail"][0]["text"].splitlines()[2]


def _ci_answers(run: int, cache_key: str, took: str) -> dict:
    """Main CI as one run shows it: its ids from ``run``, its annotation's volatile parts given."""
    suite, job = run + 1, run + 2
    return {
        "repos/example-org/dfe-engine/actions/workflows": [
            {"id": 1, "path": ".github/workflows/ci.yml", "state": "active"},
        ],
        "repos/example-org/dfe-engine/actions/workflows/1/runs": [
            {"id": run, "name": f"CI #{run}", "path": ".github/workflows/ci.yml",
             "workflow_id": 1, "conclusion": "failure", "head_sha": HEAD,
             "created_at": "2026-10-06T01:00:00Z", "check_suite_id": suite,
             "html_url": f"https://example.com/r/{run}"},
        ],
        f"repos/example-org/dfe-engine/check-suites/{suite}/check-runs": [
            {"id": job, "name": "test", "html_url": f"https://example.com/r/{run}/job/{job}",
             "annotations": 1},
        ],
        f"repos/example-org/dfe-engine/check-runs/{job}/annotations": [
            _note(f"Failed to save cache {cache_key} after {took}", line=run % 97),
        ],
    }


def test_two_sweeps_a_run_apart_give_the_same_keys() -> None:
    def keys(run: int, cache_key: str, took: str) -> list[str]:
        api, _ = _answers(_ci_answers(run, cache_key, took))
        report = sweep.build_report("2.2.0", [_collect(api)], CONFIG.tools, generated_at=NOW)
        return [entry["key"] for entry in sweep.render_slack(report)["keys"]]

    first = keys(37415111799, "Linux-cargo-0a1b2c3d4e5f", "1m 12s")
    assert first == keys(37498576073, "Linux-cargo-9f8e7d6c5b4a", "48.2s")
    ci = [key for key in first if ":.github/workflows/ci.yml:" in key]
    assert len(ci) == 2
    assert not any("37415111799" in key for key in first)


def test_the_slack_format_and_report_url_are_settings() -> None:
    options = _options(["--format", "slack", "--report-url", "https://example.com/r"])
    assert (options.output, options.report_url) == ("slack", "https://example.com/r")
    assert _options([], report_url="").report_url == ""
    with pytest.raises(FleetError, match="report_url"):
        _options(["--report-url", "http://example.com/r"])
    assert _options([], slack={**SETTINGS["slack"], "group_replies": 3}).group_replies == 3
    with pytest.raises(FleetError, match=re.escape("slack.group_replies")):
        _options([], slack={**SETTINGS["slack"], "group_replies": 0})


def _zone_name(options: cli.Options) -> str:
    """The abbreviation the options' zone gives a July instant."""
    return datetime(2026, 7, 1, tzinfo=UTC).astimezone(options.slack_zone).tzname()


def test_the_slack_title_and_timezone_are_settings_with_flags_over_them() -> None:
    plain = _options([])
    assert (plain.slack_title, _zone_name(plain)) == ("DFE suite sweep", "UTC")
    slack = {**SETTINGS["slack"], "title": "From settings", "timezone": "Australia/Sydney"}
    configured = _options([], slack=slack)
    assert (configured.slack_title, _zone_name(configured)) == ("From settings", "AEST")
    flagged = _options(
        ["--slack-title", "From flag", "--slack-timezone", "Europe/Berlin"], slack=slack
    )
    assert (flagged.slack_title, _zone_name(flagged)) == ("From flag", "CEST")
    assert _options([], slack={**slack, "title": "  padded  "}).slack_title == "padded"


@pytest.mark.parametrize(
    ("argv", "slack", "message"),
    [
        ([], {"timezone": "Nope/Zone"}, "slack.timezone"),
        ([], {"timezone": "Australia"}, "slack.timezone"),
        ([], {"timezone": "utc"}, "slack.timezone"),
        ([], {"timezone": ""}, "slack.timezone"),
        ([], {"timezone": None}, "slack.timezone"),
        ([], {"timezone": 10}, "slack.timezone"),
        ([], {"timezone": "../etc/passwd"}, "slack.timezone"),
        (["--slack-timezone", "Nope/Zone"], {}, "slack.timezone"),
        (["--slack-timezone", ""], {}, "slack.timezone"),
        ([], {"title": ""}, "slack.title"),
        ([], {"title": "   "}, "slack.title"),
        ([], {"title": None}, "slack.title"),
        ([], {"title": 7}, "slack.title"),
        (["--slack-title", ""], {}, "slack.title"),
    ],
)
def test_a_bad_slack_title_or_zone_is_a_run_error_naming_the_setting(argv, slack, message) -> None:
    with pytest.raises(FleetError, match=re.escape(message)):
        _options(argv, slack={**SETTINGS["slack"], **slack})


def test_the_slack_title_and_timezone_must_be_there() -> None:
    for missing in ("title", "timezone"):
        slack = {k: v for k, v in SETTINGS["slack"].items() if k != missing}
        with pytest.raises(FleetError, match=re.escape(f"slack.{missing}")):
            _options([], slack=slack)


def test_an_unknown_zone_stops_the_run_before_anything_is_read(capsys) -> None:
    settings = {**SETTINGS, "slack": {**SETTINGS["slack"], "timezone": "Nope/Zone"}}
    code = cli.main(["--format", "slack"], load=lambda: (settings, print, print))
    captured = capsys.readouterr()
    assert code == cli.EXIT_ERROR
    assert captured.out == ""
    assert "setting sweep.slack.timezone is 'Nope/Zone'" in captured.err


def test_the_report_file_is_a_setting_checked_before_the_sweep(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _options([]).report_file is None
    assert _options([], report_file="  ").report_file is None
    assert _options(["--report-file", "r.txt"]).report_file == Path.cwd() / "r.txt"
    assert _options([], report_file="s.txt").report_file == Path.cwd() / "s.txt"
    for bad in (str(tmp_path), str(tmp_path / "missing" / "r.txt"), ["r.txt"]):
        with pytest.raises(FleetError, match="report_file"):
            _options([], report_file=bad)


# ---------------------------------------------------------------------------
# Gating: findings and active issues gate, the backlog never does
# ---------------------------------------------------------------------------

# The keys _keys_report gives. A poster stores its new and resolved history under these, so no
# change to how rows are grouped or counted may change one of them.
KEYS_OF_THE_KEYS_REPORT = {
    "https://example.com/scalo-py/secret_scanning/1",
    "https://example.com/dfe-engine/code_scanning/3",
    "https://example.com/dfe-ui/dependabot/2",
    "https://example.com/dfe-ui/dependabot/8",
    "https://example.com/dfe-ui/advisories/9",
    "example-org/logreducer:.github/workflows/ci.yml::16d34b5e7bcb",
    "example-org/dfe-loader:.github/workflows/ci.yml:test:7b014fde8738",
    "https://example.com/dfe-docker/bot_prs/6",
    "example-org/dfe-infra:renovate_dashboard:ff3fe8c77c3c",
    "example-org/dfe-engine:drift:https://example.com/i/1",
    "https://example.com/i/1",
    "https://example.com/i/2",
    "example-org/dfe-engine:backlog:https://example.com/i/1",
    "example-org/dfe-engine:backlog",
}


def _backlog_only_report(unreadable=()) -> dict:
    """One member with one backlog item drifting off its label, and nothing else open."""
    member = sweep.Member("dfe-engine", "example-org/dfe-engine", ("stack apps",))
    row = {"repo": member.repo, "number": 9, "title": "parked", "url": "https://example.com/i/9",
           "created_at": RAISED}
    backlog = {**row, "project": 7, "status": "Backlog", "priority": "P1", "effort": "",
               "type": "Task"}
    drift = {**row, "status": "Backlog", "problem": "at Backlog without the backlog label"}
    result = sweep.RepoResult(member, "main", HEAD, False, [], list(unreadable),
                              backlog=[backlog], drift=[drift], project=7)
    return sweep.build_report("2.2.0", [result], sweep.TOOLS, generated_at=NOW)


def test_a_backlog_labelled_issue_lands_in_the_backlog_and_not_in_active_issues() -> None:
    api, _ = _answers()
    result = _collect(api)
    active = {issue["number"] for issue in result.issues}
    parked = {row["number"] for row in result.backlog
              if row["issue_repo"] == "example-org/dfe-engine"}
    # Labelled and at Next, labelled and at Backlog, and labelled with no project item.
    for number in (32, 33, 34):
        assert number not in active, number
        assert number in parked, number
    off_project = next(row for row in result.backlog if row["number"] == 34)
    assert (off_project["project"], off_project["status"]) == (None, "")
    report = sweep.build_report("2.2.0", [result], CONFIG.tools, generated_at=NOW)
    groups = {entry["key"]: entry["group"] for entry in sweep.render_slack(report)["keys"]}
    assert groups["example-org/dfe-engine:backlog:34"] == "Backlog items"
    assert "example-org/dfe-engine:issue:34" not in groups
    assert groups["https://example.com/i/35"] == "Active issues"


def test_the_backlog_status_alone_parks_an_issue_and_a_later_status_is_active() -> None:
    api, _ = _answers()
    result = _collect(api)
    assert [issue["number"] for issue in result.issues] == [35, 36]
    own = [row for row in result.backlog if row["issue_repo"] == "example-org/dfe-engine"]
    assert 31 in {row["number"] for row in own}, "at Backlog with no label is on the backlog"
    assert 36 not in {row["number"] for row in result.backlog}, "In progress is active work"


@pytest.mark.parametrize("with_project", [True, False])
def test_every_open_issue_is_active_or_on_the_backlog_and_never_both(with_project) -> None:
    repo = "example-org/dfe-engine"
    api, _ = _answers()
    issues = sweep.issue_items(repo, api(f"repos/{repo}/issues", "", True), CONFIG.bot_logins)
    rows = sweep.backlog_rows(repo, 7, PROJECT_NODES) if with_project else []
    active, backlog = sweep.backlog_split(repo, issues, rows, sweep.BacklogModel())
    active_numbers = {issue["number"] for issue in active}
    parked = {row["number"] for row in backlog if row["issue_repo"] == repo}
    assert not active_numbers & parked
    assert active_numbers | parked == {issue["number"] for issue in issues}
    assert len(parked) == len([row for row in backlog if row["issue_repo"] == repo])


def test_gating_counts_findings_and_active_issues_and_never_the_backlog() -> None:
    api, _ = _answers()
    report = sweep.build_report("2.2.0", [_collect(api)], CONFIG.tools, generated_at=NOW)
    assert report["totals"]["gating"] == {"total": 14, "findings": 12, "issues": 2,
                                          "unreadable": 0}
    assert len(report["backlog"]) == 5
    stats = sweep.render_slack(report)["stats"]
    assert {key: stats[key] for key in ("gating", "findings", "issues", "backlog", "drift")} == {
        "gating": 14, "findings": 12, "issues": 2, "backlog": 5, "drift": 3,
    }


def test_a_sweep_with_only_the_backlog_open_gates_nothing() -> None:
    slack = sweep.render_slack(_backlog_only_report())
    assert (slack["stats"]["gating"], slack["stats"]["backlog"]) == (0, 1)
    assert slack["summary"].splitlines()[1:] == [
        "*Gating: 0* -- 0 findings, 0 active issues. Not gating: backlog 1 (P1 1), 1 label drift"
    ]
    assert [entry["group"] for entry in slack["keys"]] == ["Backlog items", "Backlog drift"]
    assert not any(entry["gates"] for entry in slack["keys"])


def test_one_tool_refused_for_two_reasons_gates_once_like_its_one_key() -> None:
    unreadable = [sweep.Unreadable("example-org/dfe-engine", "ci_annotations", "HTTP 403"),
                  sweep.Unreadable("example-org/dfe-engine", "ci_annotations", "HTTP 404")]
    report = _backlog_only_report(unreadable)
    assert report["totals"]["gating"]["unreadable"] == 1
    slack = sweep.render_slack(report)
    gating_keys = {entry["key"] for entry in slack["keys"] if entry["gates"]}
    assert gating_keys == {"example-org/dfe-engine:ci_annotations"}
    assert slack["stats"]["gating"] == len(gating_keys)


def test_an_unreadable_backlog_never_gates_but_any_other_unreadable_tool_does() -> None:
    unreadable = [sweep.Unreadable("example-org/dfe-engine", "backlog", "no scope"),
                  sweep.Unreadable("example-org/dfe-engine", "issues", "HTTP 403")]
    report = _backlog_only_report(unreadable)
    assert report["totals"]["gating"] == {"total": 1, "findings": 0, "issues": 0,
                                          "unreadable": 1}
    slack = sweep.render_slack(report)
    gates = {entry["key"]: entry["gates"] for entry in slack["keys"]}
    assert gates["example-org/dfe-engine:backlog"] is False
    assert gates["example-org/dfe-engine:issues"] is True
    assert slack["summary"].splitlines()[1] == (
        "*Gating: 1* -- 0 findings, 0 active issues, 1 not readable. Not gating: backlog 1 "
        "(P1 1), 1 label drift, 1 not readable"
    )
    assert "  gating: 1 (0 finding(s), 0 active issue(s), 1 not readable)" in sweep.render_text(
        report
    )


def test_every_key_says_whether_it_gates_and_only_backlog_rows_never_do() -> None:
    slack = sweep.render_slack(_keys_report())
    flags: dict[str, set[bool]] = {}
    for entry in slack["keys"]:
        assert type(entry["gates"]) is bool, entry
        flags.setdefault(entry["group"], set()).add(entry["gates"])
    assert flags.pop("Backlog items") == {False}
    assert flags.pop("Backlog drift") == {False}
    # Both of this report's unreadable rows are the backlog tool.
    assert flags.pop("Not readable") == {False}
    assert set(flags) == {"Secret scanning", "Code scanning", "Dependabot", "Security advisories",
                          "CI on main", "Bot PRs and Renovate", "Active issues"}
    assert all(value == {True} for value in flags.values())
    # No two gating rows here share a key, so the count of gating keys is the gating stat.
    assert sum(entry["gates"] for entry in slack["keys"]) == slack["stats"]["gating"] == 11


def test_the_split_keeps_every_key_a_poster_has_stored() -> None:
    keys = [entry["key"] for entry in sweep.render_slack(_keys_report())["keys"]]
    assert set(keys) == KEYS_OF_THE_KEYS_REPORT
    assert len(keys) == len(KEYS_OF_THE_KEYS_REPORT)


# ---------------------------------------------------------------------------
# The bridge subset check
# ---------------------------------------------------------------------------


def _alert(tool: str, repo: str, url: str, repo_id: int | None = None) -> sweep.BridgeAlert:
    return sweep.BridgeAlert(tool, repo, url, 1, "2026-10-01", repo_id)


def _result(findings=(), *, member=MEMBER, repo_id=None, full_name="") -> sweep.RepoResult:
    return sweep.RepoResult(member, "main", HEAD, False, list(findings), [],
                            repo_id=repo_id, full_name=full_name)


def test_an_org_alert_the_sweep_found_is_not_a_gap() -> None:
    finding = sweep.Finding("example-org/dfe-engine", "dependabot", "7", "high", "t",
                            "https://example.com/d/7", "c")
    alerts = [_alert("dependabot", "Example-Org/DFE-Engine", "https://example.com/d/7")]
    missing, outside = sweep.bridge_gap(alerts, [_result([finding])])
    assert (missing, outside) == ([], [])


def test_an_org_alert_on_a_member_the_sweep_missed_is_a_gap() -> None:
    alerts = [_alert("secret_scanning", "example-org/dfe-engine", "https://example.com/s/2")]
    missing, outside = sweep.bridge_gap(alerts, [_result()])
    assert [a.url for a in missing] == ["https://example.com/s/2"]
    assert outside == []


def test_an_org_alert_on_a_non_member_is_listed_but_not_a_gap() -> None:
    alerts = [_alert("code_scanning", "example-org/side-project", "https://example.com/c/9")]
    missing, outside = sweep.bridge_gap(alerts, [_result()])
    assert missing == []
    assert [a.repo for a in outside] == ["example-org/side-project"]


def test_a_renamed_member_cannot_hide_a_gap() -> None:
    renamed = sweep.Member("old-name", "example-org/old-name", ("stack apps",))
    alerts = [
        _alert("dependabot", "example-org/new-name", "https://example.com/d/1", repo_id=42),
        _alert("dependabot", "example-org/newer-name", "https://example.com/d/2"),
    ]
    by_id = [_result(member=renamed, repo_id=42)]
    by_name = [_result(member=renamed, full_name="example-org/newer-name")]
    assert [a.url for a in sweep.bridge_gap(alerts[:1], by_id)[0]] == ["https://example.com/d/1"]
    assert [a.url for a in sweep.bridge_gap(alerts[1:], by_name)[0]] == [
        "https://example.com/d/2"
    ]


def test_the_repo_read_carries_its_id_and_current_name() -> None:
    api, _ = _answers({
        "repos/example-org/dfe-engine": [
            {"id": 42, "full_name": "example-org/dfe-engine-renamed",
             "default_branch": "main", "archived": False},
        ],
    })
    result = _collect(api)
    assert (result.repo_id, result.full_name) == (42, "example-org/dfe-engine-renamed")
    assert sweep.REPO_JQ.startswith("{id, full_name,")
    assert "repo_id: .repository.id" in sweep.BRIDGE_JQ


def test_the_bridge_reads_the_three_org_lists_without_secret_values() -> None:
    calls: list[str] = []

    def api(endpoint: str, jq: str, paginate: bool) -> list[dict]:
        calls.append(endpoint)
        assert paginate
        return [{"repo": "o/r", "html_url": f"https://example.com/{len(calls)}", "number": 1}]

    alerts = sweep.bridge_alerts("o", api)
    assert [a.tool for a in alerts] == ["dependabot", "code_scanning", "secret_scanning"]
    assert calls[2] == "orgs/o/secret-scanning/alerts?state=open&per_page=100&hide_secret=true"
    assert "secret" not in re.findall(r"[{,]\s*(\w+)", sweep.BRIDGE_JQ)


def test_a_bridge_gap_is_in_the_report() -> None:
    missing = [_alert("dependabot", "o/r", "https://example.com/1")]
    bridge = sweep.bridge_section(missing, 3)
    report = sweep.build_report("2.2.0", [], [], bridge, generated_at="t")
    assert report["bridge"]["member_alerts"] == 3
    assert "MISSING o/r dependabot: https://example.com/1" in sweep.render_text(report)
    json.dumps(report)


def test_alerts_on_non_members_stay_out_of_every_output() -> None:
    alerts = [
        _alert("dependabot", "example-org/dfe-engine", "https://example.com/d/1"),
        _alert("code_scanning", "example-org/side-project", "https://example.com/c/9"),
    ]
    missing, outside = sweep.bridge_gap(alerts, [_result()])
    bridge = sweep.bridge_section(missing, len(alerts) - len(outside))
    report = sweep.build_report("2.2.0", [_result()], [], bridge, generated_at=NOW)
    outputs = [
        json.dumps(report),
        sweep.render_text(report),
        json.dumps(sweep.render_slack(report)),
    ]
    assert report["bridge"]["member_alerts"] == 1
    assert not any("side-project" in out or "non-member" in out for out in outputs)


def test_a_bridge_with_no_gap_adds_no_summary_line() -> None:
    report = _slack_report(EVERYTHING, bridge=sweep.bridge_section([], 6))
    summary = sweep.render_slack(report)["summary"]
    assert "bridge" not in summary.lower()


# ---------------------------------------------------------------------------
# Member topics
# ---------------------------------------------------------------------------


def test_a_member_topic_brings_a_repo_in(tmp_path: Path) -> None:
    topics = {
        "macbash": ("example-org/macbash", ["hyperi-common"]),
        "dfe-engine": ("example-org/dfe-engine", ["dfe-suite"]),
    }
    _, members, _ = _resolve(_root(tmp_path), topics=topics)
    assert members["macbash"].why == ("topic hyperi-common",)
    assert members["macbash"].repo == "example-org/macbash"
    assert members["dfe-engine"].why == ("stack apps", "topic dfe-suite")


def test_an_exclude_beats_a_member_topic(tmp_path: Path) -> None:
    topics = {"macbash": ("example-org/macbash", ["hyperi-common"])}
    _, members, notes = _resolve(_root(tmp_path), topics=topics, exclude=["macbash"])
    assert "macbash" not in members
    assert notes == []


def test_topic_members_reads_each_topic_and_skips_archived_repos() -> None:
    calls: list[str] = []
    rows = {
        "dfe-suite": [{"name": "dfe-engine", "full_name": "o/dfe-engine", "archived": False}],
        "hyperi-common": [
            {"name": "scalo-py", "full_name": "o/scalo-py", "archived": False},
            {"name": "old-lib", "full_name": "o/old-lib", "archived": True},
            {"name": "dfe-engine", "full_name": "o/dfe-engine", "archived": False},
        ],
    }

    def api(endpoint: str, jq: str, paginate: bool) -> list[dict]:
        calls.append(endpoint)
        assert jq == sweep.TOPIC_JQ
        assert paginate
        return rows[endpoint.split("topic%3A")[1].split("&")[0]]

    found = sweep.topic_members("o", ["dfe-suite", "hyperi-common"], api)
    assert found == {
        "dfe-engine": ("o/dfe-engine", ["dfe-suite", "hyperi-common"]),
        "scalo-py": ("o/scalo-py", ["hyperi-common"]),
    }
    assert calls[0] == "search/repositories?q=org%3Ao%20topic%3Adfe-suite&per_page=100"


def test_a_topic_that_cannot_be_listed_fails_the_run() -> None:
    def api(endpoint: str, jq: str, paginate: bool) -> list[dict]:
        raise sweep.GhError(endpoint, 403, "rate limited (HTTP 403)")

    with pytest.raises(FleetError, match="topic 'dfe-suite'"):
        sweep.topic_members("o", ["dfe-suite"], api)


def test_no_member_topics_makes_no_call() -> None:
    def api(endpoint: str, jq: str, paginate: bool) -> list[dict]:
        raise AssertionError(endpoint)

    assert sweep.topic_members("o", [], api) == {}


# ---------------------------------------------------------------------------
# The gh transport, against a stand-in gh on PATH
# ---------------------------------------------------------------------------


def _fake_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, shebang: str = "#!/bin/sh"
) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(f"{shebang}\n{body}", encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path('/usr/bin')}:{Path('/bin')}")
    return tmp_path / "argv"


def test_gh_api_is_a_get_with_no_body_fields(tmp_path: Path, monkeypatch) -> None:
    argv_file = _fake_gh(tmp_path, monkeypatch,
                         f'printf "%s\\n" "$@" > "{tmp_path / "argv"}"\n'
                         "echo '{\"a\": 1}'\necho '{\"a\": 2}'\n")
    rows = sweep.gh_api("repos/o/r/pulls?state=open", ".[] | tojson", True)
    assert rows == [{"a": 1}, {"a": 2}]
    argv = argv_file.read_text(encoding="utf-8").splitlines()
    assert argv[:4] == ["api", "--method", "GET", "-H"]
    assert "--paginate" in argv
    assert not {"-f", "-F", "--field", "--raw-field", "--input"} & set(argv)


def test_gh_api_turns_a_refusal_into_its_status(tmp_path: Path, monkeypatch) -> None:
    _fake_gh(tmp_path, monkeypatch,
             "echo '{\"message\":\"Not Found\",\"status\":\"404\"}'\n"
             "echo 'gh: Not Found (HTTP 404)' >&2\nexit 1\n")
    with pytest.raises(sweep.GhError) as caught:
        sweep.gh_api("repos/o/r/code-scanning/alerts", ".[] | tojson", True)
    assert caught.value.status == 404
    assert caught.value.reason == "Not Found (HTTP 404)"


def test_gh_graphql_pages_a_query_with_typed_variables(tmp_path: Path, monkeypatch) -> None:
    argv_file = _fake_gh(tmp_path, monkeypatch,
                         f'printf "%s\\n" "$@" > "{tmp_path / "argv"}"\n'
                         "echo '{\"content\": null}'\n")
    rows = sweep.gh_graphql(sweep.BACKLOG_QUERY, {"org": "o", "number": 7}, sweep.BACKLOG_JQ)
    assert rows == [{"content": None}]
    argv = argv_file.read_text(encoding="utf-8").splitlines()
    assert argv[:3] == ["api", "graphql", "--paginate"]
    assert ["-f", "org=o"] == argv[argv.index("org=o") - 1:argv.index("org=o") + 1]
    assert ["-F", "number=7"] == argv[argv.index("number=7") - 1:argv.index("number=7") + 1]


def test_gh_graphql_refuses_a_mutation() -> None:
    with pytest.raises(ValueError, match="mutation"):
        sweep.gh_graphql("mutation { deleteIssue(input: {}) { clientMutationId } }", {}, ".")


def test_a_missing_project_scope_is_kept_as_the_reason(tmp_path: Path, monkeypatch) -> None:
    _fake_gh(tmp_path, monkeypatch,
             "echo 'error: your authentication token is missing required scopes "
             "[read:project]' >&2\necho 'To request it, run:  gh auth refresh' >&2\nexit 1\n")
    index = sweep.project_index("o", 500)
    assert index.numbers == {}
    assert index.error == "your authentication token is missing required scopes [read:project]"


def test_the_project_list_is_read_by_title(tmp_path: Path, monkeypatch) -> None:
    argv_file = _fake_gh(tmp_path, monkeypatch,
                         f'printf "%s\\n" "$@" > "{tmp_path / "argv"}"\n'
                         "echo '{\"title\": \"dfe-engine\", \"number\": 7}'\n"
                         "echo '{\"title\": \"dfe-engine\", \"number\": 9}'\n")
    index = sweep.project_index("o", 250)
    assert index == sweep.ProjectIndex(numbers={"dfe-engine": 7})
    argv = argv_file.read_text(encoding="utf-8").splitlines()
    assert argv[:2] == ["project", "list"]
    assert argv[argv.index("--limit") + 1] == "250"


def test_a_hung_gh_is_reported_unreadable_not_waited_on(tmp_path: Path, monkeypatch) -> None:
    _fake_gh(tmp_path, monkeypatch, "exec sleep 5\n")
    api = functools.partial(sweep.gh_api, timeout=0.3)
    config = dataclasses.replace(CONFIG, tools=frozenset({"dependabot"}))
    result = sweep.collect_repo(MEMBER, api, config)
    assert [(u.tool, u.reason) for u in result.unreadable] == [
        ("dependabot", "repo not readable: gh timed out after 0.3s")
    ]


# ---------------------------------------------------------------------------
# The command line, end to end against a stand-in gh
# ---------------------------------------------------------------------------


def test_the_callers_environment_and_directory_come_back(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SWEEP_PROBE", "before")
    start = os.getcwd()
    with cli.caller_state_kept(tmp_path):
        assert Path.cwd() == tmp_path.resolve()
        os.environ["SWEEP_PROBE"] = "changed"
        os.environ["SWEEP_DOTENV_ONLY"] = "loaded"
    assert os.environ["SWEEP_PROBE"] == "before"
    assert "SWEEP_DOTENV_ONLY" not in os.environ
    assert os.getcwd() == start

    def fail_inside() -> None:
        with cli.caller_state_kept(tmp_path):
            os.environ["SWEEP_DOTENV_ONLY"] = "loaded"
            raise RuntimeError

    with pytest.raises(RuntimeError):
        fail_inside()
    assert "SWEEP_DOTENV_ONLY" not in os.environ
    assert os.getcwd() == start


# Answers every read the sweep makes with plain records, as gh would after its --jq.
FAKE_GH = """\
import json, os, sys
args = sys.argv[1:]
if args[0] == "project" or args[1] == "graphql":
    sys.exit(0)
endpoint = args[args.index("--jq") - 1]
parts = endpoint.split("?")[0].split("/")
rows = []
if endpoint == "rate_limit":
    rows = [{"remaining": 5000, "limit": 5000}]
elif parts[0] == "repos" and len(parts) == 3:
    rows = [{"id": None, "full_name": "/".join(parts[1:]), "default_branch": "main"}]
elif "commits" in parts:
    rows = [{"sha": "a" * 40}]
elif parts[0] == "orgs" and "dependabot" in parts and os.environ.get("FAKE_GH_GAP"):
    rows = [{"html_url": "https://example.com/missed", "repo": os.environ["FAKE_GH_GAP"]}]
for row in rows:
    print(json.dumps(row))
"""


BRIDGE_ARGV = ["--check-bridge", "--tools", "dependabot,code_scanning,secret_scanning"]


def _run_cli(
    tmp_path: Path, monkeypatch, capsys, argv: list[str], gap: str = ""
) -> tuple[int, str]:
    _fake_gh(tmp_path, monkeypatch, FAKE_GH, shebang="#!/usr/bin/env python3")
    if gap:
        monkeypatch.setenv("FAKE_GH_GAP", gap)
    versions = tmp_path / "versions.yaml"
    versions.write_text(VERSIONS, encoding="utf-8")
    settings = {**SETTINGS, "versions_file": str(versions), "suite_dir": str(REPO_ROOT),
                "include": [], "workers": 8}
    logged: list[str] = []
    code = cli.main(argv, load=lambda: (settings, logged.append, logged.append))
    return code, capsys.readouterr().out


def _main(tmp_path: Path, monkeypatch, capsys, gap: str = "") -> tuple[int, dict]:
    code, out = _run_cli(tmp_path, monkeypatch, capsys, ["--format", "json", *BRIDGE_ARGV], gap)
    return code, json.loads(out)


def test_a_clean_sweep_exits_zero(tmp_path: Path, monkeypatch, capsys) -> None:
    code, report = _main(tmp_path, monkeypatch, capsys)
    assert code == cli.EXIT_OK
    assert report["stack"] == "2.2.0"
    assert {"dfe-engine", "dfe-infra"} <= {m["name"] for m in report["members"]}
    assert report["findings"] == []
    assert report["unreadable"] == []
    assert report["bridge"]["missing"] == []
    assert report["totals"]["gating"] == {"total": 0, "findings": 0, "issues": 0,
                                          "unreadable": 0}


def test_an_org_alert_the_sweep_missed_exits_three(tmp_path: Path, monkeypatch, capsys) -> None:
    code, report = _main(tmp_path, monkeypatch, capsys, gap="hyperi-io/dfe-engine")
    assert code == cli.EXIT_BRIDGE_GAP == 3
    assert [a["url"] for a in report["bridge"]["missing"]] == ["https://example.com/missed"]


def test_one_run_prints_the_slack_json_and_writes_the_text_report(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    gap = "hyperi-io/dfe-engine"
    code, text = _run_cli(tmp_path, monkeypatch, capsys, ["--format", "text", *BRIDGE_ARGV], gap)
    assert code == cli.EXIT_BRIDGE_GAP
    assert "  MISSING hyperi-io/dfe-engine dependabot: https://example.com/missed" in text
    written = tmp_path / "report.txt"
    argv = ["--format", "slack", "--report-file", str(written), *BRIDGE_ARGV]
    code, out = _run_cli(tmp_path, monkeypatch, capsys, argv, gap)
    assert code == cli.EXIT_BRIDGE_GAP
    slack = json.loads(out)
    assert list(slack) == ["summary", "detail", "stats", "keys"]
    assert "*Alert bridge gap*" in slack["summary"]
    assert list(slack["stats"]) == list(sweep.SLACK_STATS)
    assert slack["stats"]["gating"] == 0
    assert written.read_text(encoding="utf-8") == text


def test_the_slack_headline_takes_its_title_and_zone_from_the_flags(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    shown = r"\d{4}-\d\d-\d\d \d\d:\d\d"
    argv = ["--format", "slack", *BRIDGE_ARGV]
    code, out = _run_cli(tmp_path, monkeypatch, capsys, argv)
    assert code == cli.EXIT_OK
    headline = json.loads(out)["summary"].splitlines()[0]
    assert re.fullmatch(rf"\*DFE suite sweep\*: stack 2\.2\.0, \d+ members, {shown} UTC", headline)
    argv = ["--slack-title", "DFE daily summary", "--slack-timezone", "Australia/Sydney", *argv]
    code, out = _run_cli(tmp_path, monkeypatch, capsys, argv)
    assert code == cli.EXIT_OK
    headline = json.loads(out)["summary"].splitlines()[0]
    pattern = rf"\*DFE daily summary\*: stack 2\.2\.0, \d+ members, {shown} (AEDT|AEST)"
    assert re.fullmatch(pattern, headline)
