#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         suite_watch.py
#  Purpose:      One sweep of every suite.yaml member's open PRs, branches
#                without a PR, latest release vs its versions.yaml pin, and
#                open issues under a label -- a REPORT, never a gate.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""suite_watch -- one sweep of every suite member's PRs, branches, releases and issues.

Orchestrating a release cycle across the whole DFE suite means the same four
`gh` calls repeated by hand for every repo in suite.yaml. This runs them once,
in suite.yaml order, and prints what moved: open PRs with their CI state,
branches carrying commits with no PR open, the latest release against the pin
in versions.yaml, and the open issues under a label.

This is a REPORT. It exits 0 whatever it finds -- an unpinned release, a stale
issue, an unreachable repo are all things to look at, never a build failure.

    python3 scripts/suite_watch.py
    python3 scripts/suite_watch.py --label rc14 --stack 2.2.0-rc.14
    python3 scripts/suite_watch.py --lane consumers --no-branches
    python3 scripts/suite_watch.py --repo hyperi-io/dfe-vpn --json
    python3 scripts/suite_watch.py --quiet --stale-days 7

Needs an authenticated `gh` (ambient auth only -- no token ever appears in
argv). Stdlib only, and writes nothing anywhere.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
VERSIONS_FILE = REPO_ROOT / "versions.yaml"

sys.path.insert(0, str(SCRIPTS))
import suite_graph  # noqa: E402

# ---------------------------------------------------------------------------
# The one boundary to GitHub. Every other function in this file takes plain
# data, so the test double only ever has to replace this.
# ---------------------------------------------------------------------------


class GhError(RuntimeError):
    """`gh` refused or failed a call -- the reason is the whole point of this."""


def _gh(args: list[str]) -> object:
    """Run `gh` and return its parsed JSON.

    `gh <cmd> --json ...` and a bare `gh api ...` each print one JSON value.
    `gh api ... --paginate --jq '...'` prints one value PER LINE instead --
    plain `--paginate` with no --jq concatenates whole pages with no separator
    at all, which is why every paginated call here goes through a --jq filter.
    A --jq filter that selects a bare scalar (`.[].name`) also prints it RAW,
    the same as `jq -r` -- unquoted, so it is not valid JSON on its own line.
    So the whole output is tried as one JSON value first; failing that, each
    line is tried as its own JSON value; failing THAT too, the line is kept
    as a plain string, which is what a raw scalar line actually is.

    Raises:
        GhError: `gh` exited non-zero -- a missing repo, no access, or a bad
            call. The reason is gh's own last line of stderr.
    """
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        stderr_lines = [line for line in proc.stderr.strip().splitlines() if line.strip()]
        reason = stderr_lines[-1] if stderr_lines else (proc.stdout.strip() or "gh failed")
        raise GhError(reason.removeprefix("gh: "))
    text = proc.stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    rows: list[object] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append(line)
    return rows


# ---------------------------------------------------------------------------
# versions.yaml reading -- the same indentation-nested-scalar-maps reader as
# scripts/dfe-stack and scripts/check_versions_drift.py. Copied rather than
# imported: dfe-stack carries no .py suffix and is a CLI, not a library, so
# every reader of this shape keeps its own copy rather than a fourth parser.
# ---------------------------------------------------------------------------


def parse_simple_yaml(text: str) -> dict:
    """Indent-aware parse of the versions.yaml subset (nested maps of scalars)."""
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        m = re.match(r'^([A-Za-z0-9_.-]+):\s*(?:"([^"]*)"|([^#]*?))?\s*(?:#.*)?$', raw.strip())
        if not m:
            continue
        key = m.group(1)
        quoted = m.group(2)
        value = quoted if quoted is not None else (m.group(3) or "").strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "" and quoted is None:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = value
    return root


def load_versions(path: Path) -> dict:
    return parse_simple_yaml(path.read_text(encoding="utf-8", errors="replace"))


def resolve_stack(root: dict, stack: str | None) -> tuple[str, dict | None]:
    """(name, pin-set) for `stack`, default the `current` pointer.

    A None pin-set means that stack is not (yet) in versions.yaml -- an rc
    still being cut, say -- so every release compares as n/a against it
    rather than the tool crashing on a name that is only half-real.
    """
    stacks = root.get("stacks", {})
    name = stack or root.get("current") or ""
    return name, stacks.get(name)


def _norm_version(v: str) -> str:
    """Strip a single leading v, matching dfe-stack's own tag/pin comparison."""
    return v[1:] if v.startswith("v") else v


def pin_status(app_name: str, release_tag: str | None, apps_pin: dict) -> str:
    """'pinned' (matches), 'unpinned' (does not, either direction), or 'n/a'.

    n/a covers a member with no app pin at all (a library or content member)
    and a member with no release yet -- neither has a basis for comparison.
    'unpinned' folds both directions (release ahead of the pin, or behind it)
    into one word: either way the recorded pin no longer matches the release.
    """
    if release_tag is None:
        return "n/a"
    pin = apps_pin.get(app_name)
    if not pin:
        return "n/a"
    return "pinned" if _norm_version(release_tag) == _norm_version(pin) else "unpinned"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class RepoTarget:
    name: str
    repo: str


@dataclass
class PrRow:
    number: int
    title: str
    author: str
    draft: bool
    base: str
    head: str
    age_days: int
    labels: list[str]
    ci: str


@dataclass
class BranchRow:
    name: str
    ahead_by: int
    behind_by: int
    age_days: int


@dataclass
class IssueRow:
    number: int
    title: str
    assignee: str
    age_days: int
    stale: bool


@dataclass
class ReleaseInfo:
    tag: str
    published_at: str
    pin_status: str


@dataclass
class RepoReport:
    name: str
    repo: str
    unreachable: str | None = None
    release: ReleaseInfo | None = None
    prs: list[PrRow] = field(default_factory=list)
    branches: list[BranchRow] | None = None  # None means --no-branches skipped it
    issues: list[IssueRow] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Repo selection
# ---------------------------------------------------------------------------


def select_targets(graph: dict, lane: str | None, extra_repos: list[str]) -> list[RepoTarget]:
    """suite.yaml members (in file order, optionally limited to one lane) plus
    any --repo extras, in the order they were given."""
    nodes = graph.get("nodes", {})
    if lane is not None:
        lanes = {entry["name"]: entry for entry in graph.get("lanes", [])}
        if lane not in lanes:
            print(
                f"error: no lane named {lane!r}; lanes are {', '.join(sorted(lanes))}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        keep = set(lanes[lane].get("members", []))
    else:
        keep = set(nodes)
    targets = [RepoTarget(name=n, repo=nodes[n]["repo"]) for n in nodes if n in keep]
    for extra in extra_repos:
        if extra.count("/") != 1:
            print(f"error: --repo wants OWNER/NAME, got {extra!r}", file=sys.stderr)
            raise SystemExit(2)
        targets.append(RepoTarget(name=extra.split("/", 1)[1], repo=extra))
    return targets


# ---------------------------------------------------------------------------
# CI fold
# ---------------------------------------------------------------------------

_FAIL_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_FAIL_STATES = {"FAILURE", "ERROR"}
_PENDING_STATUSES = {"QUEUED", "IN_PROGRESS", "PENDING", "REQUESTED", "WAITING"}
_PENDING_STATES = {"PENDING", "EXPECTED"}


def fold_ci(rollup: list[dict]) -> str:
    """One word for a PR's whole check rollup: pass, fail, pending, or none.

    A CheckRun (GitHub Actions) carries status/conclusion; a StatusContext (a
    legacy commit status) carries state instead -- the rollup can hold either
    shape, so both are read. Any failing entry wins over any pending one.
    """
    if not rollup:
        return "none"
    failed = pending = False
    for check in rollup:
        conclusion = (check.get("conclusion") or "").upper()
        state = (check.get("state") or "").upper()
        status = (check.get("status") or "").upper()
        if conclusion in _FAIL_CONCLUSIONS or state in _FAIL_STATES:
            failed = True
        elif status in _PENDING_STATUSES or state in _PENDING_STATES or (
            status and status != "COMPLETED"
        ):
            pending = True
    if failed:
        return "fail"
    if pending:
        return "pending"
    return "pass"


# ---------------------------------------------------------------------------
# Per-section fetchers -- each takes plain data and one repo slug, and calls
# only _gh for I/O.
# ---------------------------------------------------------------------------


def _age_days(timestamp: str, now: datetime.datetime) -> int:
    ts = datetime.datetime.fromisoformat(timestamp)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.UTC)
    return (now - ts).days


def fetch_prs(repo: str, now: datetime.datetime) -> list[PrRow]:
    raw = _gh(
        [
            "pr", "list", "--repo", repo, "--state", "open", "--limit", "100",
            "--json", "number,title,author,isDraft,updatedAt,labels,headRefName,"
                      "statusCheckRollup,baseRefName",
        ]
    )
    rows = []
    for item in raw or []:
        rows.append(
            PrRow(
                number=item["number"],
                title=item["title"],
                author=(item.get("author") or {}).get("login", "-"),
                draft=bool(item.get("isDraft")),
                base=item.get("baseRefName", ""),
                head=item.get("headRefName", ""),
                age_days=_age_days(item["updatedAt"], now),
                labels=[lbl["name"] for lbl in item.get("labels", [])],
                ci=fold_ci(item.get("statusCheckRollup") or []),
            )
        )
    return rows


def fetch_release(repo: str, app_name: str, apps_pin: dict) -> ReleaseInfo | None:
    raw = _gh(["release", "list", "--repo", repo, "--limit", "1", "--json", "tagName,publishedAt"])
    if not raw:
        return None
    tag = raw[0]["tagName"]
    published = raw[0]["publishedAt"]
    return ReleaseInfo(tag=tag, published_at=published, pin_status=pin_status(app_name, tag, apps_pin))


def fetch_issues(repo: str, label: str, stale_days: int, now: datetime.datetime) -> list[IssueRow]:
    raw = _gh(
        [
            "issue", "list", "--repo", repo, "--state", "open", "--label", label,
            "--limit", "100", "--json", "number,title,assignees,updatedAt",
        ]
    )
    rows = []
    for item in raw or []:
        age = _age_days(item["updatedAt"], now)
        assignee = ", ".join(a["login"] for a in item.get("assignees", [])) or "-"
        rows.append(
            IssueRow(
                number=item["number"], title=item["title"], assignee=assignee,
                age_days=age, stale=age > stale_days,
            )
        )
    return rows


def fetch_branches(repo: str, pr_heads: set[str], now: datetime.datetime) -> list[BranchRow]:
    meta = _gh(["api", f"repos/{repo}"]) or {}
    default_branch = meta.get("default_branch", "main")
    # --paginate with --jq prints one value per line; a repo with exactly one
    # branch still parses as a single bare string rather than a one-item
    # list, so both shapes are normalised here.
    raw_names = _gh(["api", f"repos/{repo}/branches", "--paginate", "--jq", ".[].name"])
    names = raw_names if isinstance(raw_names, list) else ([raw_names] if raw_names else [])
    candidates = [n for n in names if n != default_branch and n not in pr_heads]
    rows = []
    for name in candidates:
        cmp_data = _gh(["api", f"repos/{repo}/compare/{default_branch}...{name}"]) or {}
        ahead = cmp_data.get("ahead_by", 0)
        if not ahead:
            continue
        commits = cmp_data.get("commits") or []
        commit_date = commits[-1]["commit"]["committer"]["date"] if commits else now.isoformat()
        rows.append(
            BranchRow(
                name=name, ahead_by=ahead, behind_by=cmp_data.get("behind_by", 0),
                age_days=_age_days(commit_date, now),
            )
        )
    return rows


def gather_repo(
    target: RepoTarget, args: argparse.Namespace, apps_pin: dict, now: datetime.datetime
) -> RepoReport:
    """Every section for one repo, or one unreachable line if `gh` refuses it.

    The whole gather is one try/except: a repo `gh` cannot resolve at all
    fails on the first call, and a repo that goes away mid-sweep (deleted,
    access revoked) is reported the same way rather than half-populated.
    """
    try:
        prs = fetch_prs(target.repo, now)
        release = fetch_release(target.repo, target.name, apps_pin)
        issues = fetch_issues(target.repo, args.label, args.stale_days, now)
        branches = None if args.no_branches else fetch_branches(
            target.repo, {pr.head for pr in prs}, now
        )
    except GhError as exc:
        return RepoReport(name=target.name, repo=target.repo, unreachable=str(exc))
    return RepoReport(
        name=target.name, repo=target.repo, release=release, prs=prs, branches=branches,
        issues=issues,
    )


# ---------------------------------------------------------------------------
# Output -- text and JSON share the same gathered data
# ---------------------------------------------------------------------------


def summarize(reports: list[RepoReport]) -> dict:
    return {
        "repos": len(reports),
        "prs": sum(len(r.prs) for r in reports),
        "branches": sum(len(r.branches or []) for r in reports),
        "issues": sum(len(r.issues) for r in reports),
        "unpinned": sum(1 for r in reports if r.release and r.release.pin_status == "unpinned"),
        "stale_issues": sum(sum(1 for i in r.issues if i.stale) for r in reports),
        "unreachable": sum(1 for r in reports if r.unreachable),
    }


def to_dict(r: RepoReport) -> dict:
    return {
        "name": r.name,
        "repo": r.repo,
        "unreachable": r.unreachable,
        "release": None if r.release is None else {
            "tag": r.release.tag,
            "published_at": r.release.published_at,
            "pin_status": r.release.pin_status,
        },
        "prs": [
            {
                "number": p.number, "title": p.title, "author": p.author, "draft": p.draft,
                "base": p.base, "head": p.head, "age_days": p.age_days, "labels": p.labels,
                "ci": p.ci,
            }
            for p in r.prs
        ],
        "branches": None if r.branches is None else [
            {"name": b.name, "ahead_by": b.ahead_by, "behind_by": b.behind_by, "age_days": b.age_days}
            for b in r.branches
        ],
        "issues": [
            {
                "number": i.number, "title": i.title, "assignee": i.assignee,
                "age_days": i.age_days, "stale": i.stale,
            }
            for i in r.issues
        ],
    }


def _header_line(r: RepoReport) -> str:
    if r.unreachable:
        return f"{r.repo}: unreachable ({r.unreachable})"
    if r.release is None:
        return f"== {r.repo}  release none"
    return f"== {r.repo}  release {r.release.tag} ({r.release.pin_status})"


def _pr_line(pr: PrRow) -> str:
    draft = " draft" if pr.draft else ""
    labels = f" [{', '.join(pr.labels)}]" if pr.labels else ""
    return f"  PR #{pr.number} {pr.title}  {pr.author}{draft} ci={pr.ci} {pr.age_days}d{labels}"


def _branch_line(b: BranchRow) -> str:
    return f"  BR {b.name}  +{b.ahead_by}/-{b.behind_by} {b.age_days}d"


def _issue_line(i: IssueRow) -> str:
    stale = " stale" if i.stale else ""
    return f"  IS #{i.number} {i.title}  {i.assignee} {i.age_days}d{stale}"


def _has_something(r: RepoReport) -> bool:
    if r.unreachable:
        return True
    if r.prs or r.issues or r.branches:
        return True
    return bool(r.release and r.release.pin_status == "unpinned")


def _summary_line(s: dict) -> str:
    return (
        f"-- {s['repos']} repos, {s['prs']} open PRs, {s['branches']} branches without a PR, "
        f"{s['issues']} labelled issues, {s['unpinned']} unpinned releases, "
        f"{s['stale_issues']} stale issues --"
    )


def render_text(reports: list[RepoReport], summary: dict, quiet: bool) -> str:
    lines: list[str] = []
    for r in reports:
        if quiet and not _has_something(r):
            continue
        lines.append(_header_line(r))
        if r.unreachable:
            continue
        for pr in r.prs:
            lines.append(_pr_line(pr))
        for b in r.branches or []:
            lines.append(_branch_line(b))
        for i in r.issues:
            lines.append(_issue_line(i))
    lines.append("")
    lines.append(_summary_line(summary))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def run(
    args: argparse.Namespace, graph: dict, versions_root: dict, out, *,
    now: datetime.datetime | None = None,
) -> int:
    """The sweep, over data the caller supplies -- no file or network I/O of
    its own beyond _gh, so a test drives this with a canned graph and clock."""
    now = now or datetime.datetime.now(datetime.UTC)
    targets = select_targets(graph, args.lane, args.repo)
    stack_name, pin_set = resolve_stack(versions_root, args.stack)
    if pin_set is None:
        print(
            f"note: stack {stack_name!r} not in versions.yaml -- every release "
            f"compares as n/a",
            file=sys.stderr,
        )
    apps_pin = (pin_set or {}).get("apps", {})
    reports = [gather_repo(t, args, apps_pin, now) for t in targets]
    summary = summarize(reports)
    if args.json:
        payload = {
            "swept_at": now.isoformat(timespec="seconds"),
            "stack": stack_name,
            "repos": [to_dict(r) for r in reports],
        }
        print(json.dumps(payload, indent=2), file=out)
    else:
        print(render_text(reports, summary, args.quiet), file=out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="suite_watch",
        description=__doc__.split("\n\n")[0].replace("\n", " "),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument(
        "--repo", action="append", default=[], metavar="OWNER/NAME",
        help="sweep this repo too, beyond suite.yaml (repeatable)",
    )
    ap.add_argument(
        "--lane", default=None, metavar="NAME",
        help="limit the sweep to one suite.yaml lane's members",
    )
    ap.add_argument(
        "--label", default="rc14", metavar="LABEL",
        help="open-issue label to report",
    )
    ap.add_argument(
        "--stale-days", type=int, default=3, metavar="N",
        help="an issue not updated in this many days is flagged stale",
    )
    ap.add_argument(
        "--stack", default=None, metavar="VERSION",
        help="compare app pins against this versions.yaml stack instead of `current`",
    )
    ap.add_argument(
        "--no-branches", action="store_true",
        help="skip the branches-without-a-PR sweep (the expensive half)",
    )
    ap.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    ap.add_argument(
        "--quiet", action="store_true", help="print only repos that have something to show",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    graph = suite_graph.load()
    versions_root = load_versions(VERSIONS_FILE)
    return run(args, graph, versions_root, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
