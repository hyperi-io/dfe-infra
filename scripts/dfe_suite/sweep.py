#  Project:      dfe-infra
#  File:         scripts/dfe_suite/sweep.py
#  Purpose:      Derive the suite's repos and collect the open findings and work items on them.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The read-only half of ``scripts/dfe-sweep``: who is in the suite, and what is open on each.

Membership is derived from data, never listed. A stack in versions.yaml names the repos the
product ships (the KEYS of its ``apps:`` and ``content:`` maps, so a commented-out pin is not a
member), every suite.yaml node not marked ``default_in_pass: false`` is a member, suite.yaml's
build-cycle in-edges add every repo the stack depends on, and the infra repo itself is added
last. The caller's include and exclude lists are applied on top.

Only a severity a security tool assigned reaches critical: a Dependabot or repository
advisory's, or a code-scanning rule's security severity. Secrets, rule levels, CI and the
Renovate dashboard top out at high, so critical always means a real security rating.

Collection reads each member's default branch through the GitHub REST API with ``gh api``,
always as a GET and always with a ``--jq`` projection, so a secret-scanning alert's secret value
never reaches this process. A feature GitHub refuses (403, 404) is reported as unreadable for
that tool on that repo, never as zero findings.

Open issues and the backlog are work items, kept apart from the severity-ranked findings. The
backlog follows hyperi-ai's ``pm backlog`` model: the org project titled exactly as the repo,
read with one GraphQL query (a ``query``, never a mutation) that joins the project's Status to
the Priority and Effort org issue fields. A repo with no such project is "no project"; projects
the token cannot list are unreadable. An open issue whose backlog label disagrees with its
project Status is reported as drift.

Everything that decides something is a pure function over plain data, so it is tested with no
network. The ``gh`` transport is the one function that touches the outside world.
"""

import functools
import hashlib
import importlib.machinery
import importlib.util
import json
import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path
from types import ModuleType
from urllib.parse import quote

from dfe_suite.graph import in_edges
from dfe_suite.proc import CommandTimeoutError, FleetError
from dfe_suite.proc import run as run_command

DFE_STACK = Path(__file__).resolve().parent.parent / "dfe-stack"

# Seconds one gh call may take when the caller sets none; the cascade's gh_timeout_seconds
# sets it for a run.
GH_TIMEOUT = 120.0
# GitHub serves at most this many records a page, whatever per_page asks for.
GH_PAGE_MAX = 100

# Most urgent first; the order findings are ranked and printed in.
SEVERITIES = ("critical", "high", "medium", "low", "info")

TOOLS = (
    "dependabot",
    "code_scanning",
    "secret_scanning",
    "advisories",
    "bot_prs",
    "renovate_dashboard",
    "issues",
    "backlog",
    "ci_runs",
    "ci_annotations",
)

# Tools that report work items rather than findings, so they are never severity-ranked.
WORK_TOOLS = ("issues", "backlog")

# The tools whose org webhook events the alert bridge forwards, and the org-wide list of each.
BRIDGED = {
    "dependabot": "orgs/{org}/dependabot/alerts?state=open&per_page=100",
    "code_scanning": "orgs/{org}/code-scanning/alerts?state=open&per_page=100",
    "secret_scanning": "orgs/{org}/secret-scanning/alerts?state=open&per_page=100&hide_secret=true",
}

STACK_SECTIONS = ("apps", "content")

# Projections run inside gh, so only these fields ever leave GitHub. `tojson` keeps one record
# per line whatever the value types.
REPO_JQ = "{id, full_name, default_branch, archived, visibility, html_url} | tojson"
HEAD_JQ = "{sha, date: .commit.committer.date} | tojson"
DEPENDABOT_JQ = (
    ".[] | {number, html_url, created_at, severity: .security_advisory.severity,"
    " ghsa: .security_advisory.ghsa_id, cve: .security_advisory.cve_id,"
    " summary: .security_advisory.summary, package: .dependency.package.name,"
    " ecosystem: .dependency.package.ecosystem, manifest: .dependency.manifest_path,"
    " scope: .dependency.scope,"
    " patched: .security_vulnerability.first_patched_version.identifier} | tojson"
)
CODE_SCANNING_JQ = (
    ".[] | {number, html_url, created_at, rule_id: .rule.id, rule: .rule.description,"
    " rule_severity: .rule.severity, security_severity: .rule.security_severity_level,"
    " tool: .tool.name, path: .most_recent_instance.location.path,"
    " line: .most_recent_instance.location.start_line} | tojson"
)
# No `secret` field: the projection is the guarantee, hide_secret on the URL the second one.
SECRET_SCANNING_JQ = (
    ".[] | {number, html_url, created_at, secret_type, secret_type_display_name, validity,"
    " publicly_leaked, push_protection_bypassed} | tojson"
)
ADVISORY_JQ = ".[] | {ghsa_id, cve_id, html_url, summary, severity, state, created_at} | tojson"
PULLS_JQ = (
    ".[] | {number, title, html_url, created_at, user: .user.login, draft, head: .head.ref}"
    " | tojson"
)
# A bot's issue body is kept so the Renovate dashboard can be read; nobody else's is fetched.
ISSUES_JQ = (
    ".[] | {number, title, html_url, created_at, user: .user.login, type: .type.name,"
    " is_pr: (.pull_request != null), labels: [.labels[].name],"
    ' body: (if .user.type == "Bot" then .body else null end)} | tojson'
)
PROJECTS_JQ = ".projects[] | {title, number} | tojson"

# hyperi-ai's `pm backlog` model: the backlog is the org project titled as the repo, its
# Status joined in one query to the Priority and Effort org issue fields.
# GitHub caps a page at 100 items and answers more with an error that nulls the project.
PRIORITY_FIELD = "Priority"
EFFORT_FIELD = "Effort"
# A backlog row as the report carries it; its labels and issue_repo stay internal.
BACKLOG_FIELDS = (
    "repo", "project", "status", "priority", "effort", "type", "number", "title", "url"
)
BACKLOG_QUERY = """\
query($org: String!, $number: Int!, $endCursor: String) {
  organization(login: $org) {
    projectV2(number: $number) {
      items(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          fieldValueByName(name: "Status") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          content {
            ... on Issue {
              number
              title
              url
              state
              createdAt
              repository { nameWithOwner }
              issueType { name }
              labels(first: 50) { nodes { name } }
              issueFieldValues(first: 10) {
                nodes {
                  ... on IssueFieldSingleSelectValue {
                    value
                    field { ... on IssueFieldSingleSelect { name } }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""
# A project number that resolves to nothing yields one marker row, so it is not read as empty.
BACKLOG_JQ = (
    "if .data.organization.projectV2 == null then {missing_project: true} | tojson"
    " else .data.organization.projectV2.items.nodes[] | tojson end"
)
RUNS_JQ = (
    ".workflow_runs[] | {id, name, workflow_id, path, event, conclusion, html_url, head_sha,"
    " created_at, check_suite_id} | tojson"
)
CHECK_RUNS_JQ = (
    ".check_runs[] | {id, name, html_url, annotations: .output.annotations_count} | tojson"
)
ANNOTATIONS_JQ = (
    ".[] | {path, start_line, annotation_level, title, message} | tojson"
)
BRIDGE_JQ = (
    ".[] | {html_url, number, created_at, repo: .repository.full_name,"
    " repo_id: .repository.id} | tojson"
)

# Only a severity a security tool assigned reaches critical: an advisory's, or a code-scanning
# rule's security severity. Every other mapping below tops out at high.
SEVERITY_ALIASES = {"moderate": "medium"}

# A code-scanning rule with no security severity falls back to its SARIF level.
RULE_SEVERITY = {"error": "high", "warning": "medium", "note": "low"}

# A workflow run's conclusion, as a finding. success, neutral and skipped are not findings.
RUN_SEVERITY = {
    "failure": "high",
    "timed_out": "high",
    "startup_failure": "high",
    "cancelled": "low",
    "action_required": "low",
    "stale": "low",
}

ANNOTATION_SEVERITY = {"failure": "medium", "warning": "low"}

# Renovate dashboard sections that list an update waiting on something, by heading prefix.
DASHBOARD_SECTIONS = (
    ("repository problems", "medium"),
    ("errored", "medium"),
    ("rate-limited", "low"),
    ("pending", "low"),
    ("deprecations", "low"),
)
DASHBOARD_TITLE = "dependency dashboard"

_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")
_HEADING = re.compile(r"^#{1,2}\s+(.*)$")
_BRANCH_MARK = re.compile(r"<!--\s*[\w-]+-branch=([^\s>]+)\s*-->")
_ALL_MARK = re.compile(r"<!--\s*[\w-]*-all-[\w-]*\s*-->")
# An HTML comment as browsers end one, across lines and with the `--!>` close too.
_COMMENT = re.compile(r"<!--.*?--!?>", re.DOTALL)
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_CHECKBOX = re.compile(r"^\[[ xX]\]\s*")
_TABLE_RULE = re.compile(r"^\|[\s|:-]+\|$")


class GhError(RuntimeError):
    """A ``gh api`` call that GitHub, or gh itself, refused."""

    def __init__(self, endpoint: str, status: int | None, message: str) -> None:
        """Record what was asked for and why it failed.

        Args:
            endpoint: The API path that was called.
            status: The HTTP status gh reported, or None when it reported none.
            message: gh's own error line.
        """
        super().__init__(f"{endpoint}: {message}")
        self.endpoint = endpoint
        self.status = status
        self.message = message

    @property
    def reason(self) -> str:
        """The refusal as one line for the report."""
        return self.message or f"HTTP {self.status}"


# One API read: (endpoint, jq projection, paginate) -> the projected records.
type Api = Callable[[str, str, bool], list[dict]]
# One GraphQL read: (query, variables, jq projection) -> the projected records, every page.
type Graphql = Callable[[str, dict[str, str | int], str], list[dict]]
# One Slack thread reply as (kind, text) lines; kind is header, repo, item or more.
type Reply = list[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class Member:
    """One repo the sweep covers, and every reason it is covered.

    Attributes:
        name: The suite name, as versions.yaml and suite.yaml spell it.
        repo: The GitHub ``owner/name`` slug.
        why: Each reason it is a member, in the order they were found.
    """

    name: str
    repo: str
    why: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Finding:
    """One open item a tool reports on one repo.

    Attributes:
        repo: The ``owner/name`` slug.
        tool: One of TOOLS.
        id: The tool's own identifier for the item, unique within repo and tool.
        severity: One of SEVERITIES.
        title: One line saying what it is.
        url: Where GitHub shows it.
        created_at: When GitHub first reported it.
        extra: The tool-specific fields behind the title.
    """

    repo: str
    tool: str
    id: str
    severity: str
    title: str
    url: str
    created_at: str
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Unreadable:
    """A tool the sweep could not read on a repo, which is never the same as zero findings.

    Attributes:
        repo: The ``owner/name`` slug.
        tool: One of TOOLS, or ``repo`` when the repo itself could not be read.
        reason: What GitHub or gh said.
    """

    repo: str
    tool: str
    reason: str


# A member's backlog project, as found: its number, "no project" (a normal state), or "not
# read" when the org's projects could not be listed, which is also an unreadable entry.
NO_PROJECT = "no project"
NOT_READ = "not read"


@dataclass(frozen=True, slots=True)
class RepoResult:
    """Everything the sweep learned about one member.

    Attributes:
        member: The member.
        default_branch: The branch read, or empty when the repo was unreadable.
        head_sha: That branch's head commit at read time, or empty when unread.
        archived: Whether GitHub reports the repo archived.
        findings: The open findings.
        unreadable: The tools that could not be read.
        issues: Open issues, as work items.
        backlog: Open items on the repo's project, as work items.
        drift: Open issues whose backlog label and project Status disagree.
        project: The project number, NO_PROJECT, NOT_READ, or empty when backlog is off.
        repo_id: GitHub's id for the repo, or None when it was not read.
        full_name: The repo's current ``owner/name``, which differs from the member's slug
            once a rename has made GitHub redirect the old one.
    """

    member: Member
    default_branch: str
    head_sha: str
    archived: bool
    findings: list[Finding]
    unreadable: list[Unreadable]
    issues: list[dict] = field(default_factory=list)
    backlog: list[dict] = field(default_factory=list)
    drift: list[dict] = field(default_factory=list)
    project: int | str = ""
    repo_id: int | None = None
    full_name: str = ""


@dataclass(frozen=True, slots=True)
class BacklogModel:
    """How a repo project's backlog is read.

    Attributes:
        label: The label every open issue at the first status carries, and no other does.
        statuses: The project Status options in workflow order; the first is the backlog.
        priorities: The Priority options, highest first.
    """

    label: str = "backlog"
    statuses: tuple[str, ...] = ("Backlog", "Next", "In progress", "Done")
    priorities: tuple[str, ...] = ("P0", "P1", "P2", "P3")

    @property
    def backlog_status(self) -> str:
        """The Status a captured, uncommitted item sits at."""
        return self.statuses[0] if self.statuses else "Backlog"


@dataclass(frozen=True, slots=True)
class SweepConfig:
    """The resolved settings collection runs on.

    Attributes:
        tools: The tools to collect, each one of TOOLS.
        bot_logins: The PR authors counted as bots, e.g. ``renovate[bot]``.
        workers: How many repos are read at once.
        run_window: How many recent completed runs to search for each workflow's latest.
        org: The owner of the repo projects.
        backlog: How the backlog is read.
    """

    tools: frozenset[str]
    bot_logins: frozenset[str]
    workers: int
    run_window: int
    org: str = ""
    backlog: BacklogModel = field(default_factory=BacklogModel)


@dataclass(frozen=True, slots=True)
class ProjectIndex:
    """The org's open projects by title, or why they could not be listed.

    Attributes:
        numbers: Project title to number.
        error: gh's refusal when the list could not be read, else empty.
    """

    numbers: dict[str, int] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True, slots=True)
class BridgeAlert:
    """One open org-level alert of the kind the webhook bridge forwards.

    Attributes:
        tool: One of the BRIDGED tools.
        repo: The ``owner/name`` slug it is on.
        url: Its html_url, which is also the key it is matched on.
        number: Its number within the repo.
        created_at: When it was raised.
        repo_id: GitHub's id for the repo, which a rename does not change.
    """

    tool: str
    repo: str
    url: str
    number: int | None
    created_at: str
    repo_id: int | None = None


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


@functools.cache
def _dfe_stack() -> ModuleType:
    """Import the extensionless scripts/dfe-stack, for its versions.yaml reader."""
    loader = importlib.machinery.SourceFileLoader("dfe_stack", str(DFE_STACK))
    spec = importlib.util.spec_from_loader("dfe_stack", loader)
    if spec is None:
        raise FleetError(f"cannot load {DFE_STACK}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def read_versions(path: Path) -> dict:
    """Parse versions.yaml with dfe-stack's own reader, so the two never disagree.

    Args:
        path: The versions.yaml to read.

    Returns:
        The parsed tree: top-level pointers plus ``stacks``.

    Raises:
        FleetError: If the file cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FleetError(f"cannot read {path}: {exc}") from exc
    return _dfe_stack().parse_simple_yaml(text)


def stack_name(root: dict, pointer: str) -> str:
    """The stack a pointer names: ``latest`` (falling back to ``current``), ``current``, or a key.

    Args:
        root: The parsed versions.yaml.
        pointer: ``latest``, ``current``, or a stack key such as ``2.2.0``.

    Returns:
        The stack key.

    Raises:
        FleetError: If the pointer resolves to nothing.
    """
    if pointer not in ("latest", "current"):
        return pointer
    name = str(root.get(pointer) or "").strip()
    if not name and pointer == "latest":
        name = str(root.get("current") or "").strip()
    if not name:
        raise FleetError(f"versions.yaml has no `{pointer}` stack pointer to read")
    return name


def stack_entries(root: dict, name: str) -> list[tuple[str, str]]:
    """The repo names a stack ships, each with the section it came from, in file order.

    Args:
        root: The parsed versions.yaml.
        name: The stack key.

    Returns:
        ``(name, section)`` pairs, ``apps`` before ``content``. A name in both is listed once.

    Raises:
        FleetError: If the stack is missing or names no repo in either section.
    """
    stacks = root.get("stacks")
    stack = stacks.get(name) if isinstance(stacks, dict) else None
    if not isinstance(stack, dict):
        have = ", ".join(stacks) if isinstance(stacks, dict) else "none"
        raise FleetError(f"stack {name!r} is not in versions.yaml (have: {have})")
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for section in STACK_SECTIONS:
        block = stack.get(section)
        if not isinstance(block, dict):
            continue
        for key in block:
            if key not in seen:
                seen.add(key)
                entries.append((key, section))
    if not entries:
        raise FleetError(f"stack {name!r} has no {' or '.join(STACK_SECTIONS)} entries")
    return entries


def repo_slug(graph: dict, name: str, org: str) -> str:
    """The GitHub repo a suite name lives in: the node's ``repo`` field, else ``org/name``."""
    nodes = graph.get("nodes")
    node = nodes.get(name) if isinstance(nodes, dict) else None
    repo = str(node.get("repo") or "") if isinstance(node, dict) else ""
    if not repo:
        return f"{org}/{name}"
    return repo if "/" in repo else f"{org}/{repo}"


def producers_of(graph: dict, seeds: Iterable[str]) -> dict[str, list[str]]:
    """Walk build-cycle in-edges transitively from the seeds.

    Args:
        graph: The suite graph, as ``dfe-stack suite`` answers it.
        seeds: The names to start from.

    Returns:
        Every producer reached, mapped to the names it feeds, in discovery order. A seed that
        feeds another member appears too.
    """
    feeds: dict[str, list[str]] = {}
    queue = list(dict.fromkeys(seeds))
    seen = set(queue)
    while queue:
        consumer = queue.pop(0)
        for edge in in_edges(graph, consumer):
            producer = str(edge.get("from") or "")
            if not producer:
                continue
            consumers = feeds.setdefault(producer, [])
            if consumer not in consumers:
                consumers.append(consumer)
            if producer not in seen:
                seen.add(producer)
                queue.append(producer)
    return feeds


def suite_members(graph: dict) -> list[str]:
    """suite.yaml's nodes in file order, less any whose ``default_in_pass`` is false.

    An alpha or beta node sits out with ``default_in_pass: false`` until it is ready, and
    joins the sweep when it flips or when a stack pins it.
    """
    nodes = graph.get("nodes")
    if not isinstance(nodes, dict):
        return []
    return [
        name
        for name, node in nodes.items()
        if not (isinstance(node, dict) and node.get("default_in_pass") is False)
    ]


def split_names(values: Iterable[object]) -> list[str]:
    """Flatten entries that may each be a comma-separated list."""
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in str(value).split(",") if part.strip())
    return out


def resolve_members(
    root: dict,
    graph: dict,
    *,
    org: str,
    pointer: str,
    infra: str,
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
) -> tuple[str, list[Member], list[str]]:
    """Derive the members: stack, suite.yaml nodes, the stack's producers, infra, then config.

    A suite.yaml node with ``default_in_pass: false`` is in only when the stack pins it, the
    walk reaches it, or the include list names it.

    Args:
        root: The parsed versions.yaml.
        graph: The suite graph.
        org: The owner for a name suite.yaml gives no repo for.
        pointer: The stack pointer, see stack_name.
        infra: The infra repo's suite name.
        include: Names or ``owner/name`` slugs to add.
        exclude: Names or slugs to drop; applied last, so it beats every other reason.

    Returns:
        The stack key read, the members in discovery order, and one note per exclude entry
        that matched nothing.

    Raises:
        FleetError: If the stack cannot be read.
    """
    name = stack_name(root, pointer)
    why: dict[str, list[str]] = {}
    for entry, section in stack_entries(root, name):
        why.setdefault(entry, []).append(f"stack {section}")
    seeds = list(why)
    for member in suite_members(graph):
        why.setdefault(member, []).append("suite member")
    feeds = producers_of(graph, seeds)
    for producer, consumers in feeds.items():
        why.setdefault(producer, []).append(f"dependency of {', '.join(consumers)}")
    why.setdefault(infra, []).append("infra")
    slugs: dict[str, str] = {}
    for entry in split_names(include):
        member = entry.rsplit("/", 1)[-1]
        if "/" in entry:
            slugs[member] = entry
        why.setdefault(member, []).append("config include")
    members = [
        Member(member, slugs.get(member) or repo_slug(graph, member, org), tuple(reasons))
        for member, reasons in why.items()
    ]
    notes: list[str] = []
    for entry in split_names(exclude):
        wanted = entry.lower()
        kept = [m for m in members if wanted not in (m.name.lower(), m.repo.lower())]
        if len(kept) == len(members):
            notes.append(f"exclude {entry!r} matches no member")
        members = kept
    return name, members, notes


# ---------------------------------------------------------------------------
# The gh transport
# ---------------------------------------------------------------------------


def gh_api(endpoint: str, jq: str, paginate: bool, *, timeout: float = GH_TIMEOUT) -> list:
    """GET one REST endpoint through ``gh api`` and return the projected records.

    Read-only by construction: the method is pinned to GET and query parameters ride in the
    path, because ``gh api -f`` would switch the request to a POST.

    Args:
        endpoint: The API path, query string included.
        jq: A projection emitting one JSON text per record.
        paginate: Follow the Link header through every page.
        timeout: Seconds gh may take, every page included.

    Returns:
        One decoded value per output line.

    Raises:
        GhError: If gh exits non-zero, times out, or prints something that is not JSON.
    """
    argv = ["gh", "api", "--method", "GET", "-H", "Accept: application/vnd.github+json"]
    if paginate:
        argv.append("--paginate")
    return _run_gh([*argv, endpoint, "--jq", jq], endpoint, timeout)


def gh_graphql(
    query: str, variables: dict[str, str | int], jq: str, *, timeout: float = GH_TIMEOUT
) -> list:
    """Run one read-only GraphQL query through ``gh api graphql``, every page of it.

    Args:
        query: A ``query`` document taking ``$endCursor`` and selecting ``pageInfo``.
        variables: Its other variables; an int is sent typed, anything else as a string.
        jq: A projection emitting one JSON text per record.
        timeout: Seconds gh may take, every page included.

    Returns:
        One decoded value per output line, across every page.

    Raises:
        GhError: If gh exits non-zero, times out, or prints something that is not JSON.
        ValueError: If the document is not a query.
    """
    if not query.lstrip().startswith("query"):
        raise ValueError("gh_graphql runs queries only, never a mutation")
    argv = ["gh", "api", "graphql", "--paginate", "-f", f"query={query}"]
    for name, value in variables.items():
        argv += ["-F", f"{name}={value}"] if isinstance(value, int) else ["-f", f"{name}={value}"]
    return _run_gh([*argv, "--jq", jq], "graphql", timeout)


def project_index(org: str, limit: int, *, timeout: float = GH_TIMEOUT) -> ProjectIndex:
    """The org's open projects by title; a refusal is kept, never read as no projects."""
    argv = ["gh", "project", "list", "--owner", org, "--format", "json", "--limit", str(limit)]
    try:
        rows = _run_gh([*argv, "--jq", PROJECTS_JQ], f"projects of {org}", timeout)
    except GhError as exc:
        return ProjectIndex(error=exc.reason)
    numbers: dict[str, int] = {}
    for row in rows:
        if isinstance(row.get("number"), int):
            numbers.setdefault(_text(row.get("title")), row["number"])
    return ProjectIndex(numbers=numbers)


def _error_line(stderr: str, stdout: str) -> str:
    """gh's own error line: the one it prefixes, else the first it printed."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in lines:
        for prefix in ("gh: ", "error: "):
            if line.startswith(prefix):
                return line.removeprefix(prefix)
    return lines[0] if lines else stdout.strip()[:200]


def _run_gh(argv: list[str], label: str, timeout: float) -> list:
    """Run gh through proc.run and decode one JSON value per output line."""
    try:
        proc = run_command(argv, check=False, timeout=timeout)
    except CommandTimeoutError as exc:
        raise GhError(label, None, str(exc)) from exc
    except OSError as exc:
        raise GhError(label, None, f"cannot run gh: {exc}") from exc
    if proc.returncode != 0:
        match = _HTTP_STATUS.search(proc.stderr)
        status = int(match.group(1)) if match else None
        raise GhError(label, status, _error_line(proc.stderr, proc.stdout))
    rows = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise GhError(label, None, f"not JSON: {line[:200]}") from exc
    return rows


# ---------------------------------------------------------------------------
# Records -> findings (pure)
# ---------------------------------------------------------------------------


def level(value: object, default: str) -> str:
    """A tool's severity word, on the ladder; ``default`` when it has none we know."""
    word = str(value or "").strip().lower()
    word = SEVERITY_ALIASES.get(word, word)
    return word if word in SEVERITIES else default


def _text(value: object) -> str:
    return str(value) if value is not None else ""


def dependabot_findings(repo: str, rows: list[dict]) -> list[Finding]:
    """Open Dependabot alerts as findings, at the advisory's severity."""
    findings = []
    for row in rows:
        ident = row.get("cve") or row.get("ghsa") or ""
        package = row.get("package") or "?"
        findings.append(
            Finding(
                repo=repo,
                tool="dependabot",
                id=_text(row.get("number")),
                severity=level(row.get("severity"), "medium"),
                title=f"{package} {ident}: {_text(row.get('summary'))}".strip(),
                url=_text(row.get("html_url")),
                created_at=_text(row.get("created_at")),
                extra={
                    key: row.get(key)
                    for key in ("ghsa", "cve", "package", "ecosystem", "manifest", "scope",
                                "patched")
                },
            )
        )
    return findings


def code_scanning_findings(repo: str, rows: list[dict]) -> list[Finding]:
    """Open code-scanning alerts as findings: security severity first, else the rule's own."""
    findings = []
    for row in rows:
        rule_level = _text(row.get("rule_severity")).strip().lower()
        severity = level(row.get("security_severity"), "") or RULE_SEVERITY.get(
            rule_level, "medium"
        )
        where = row.get("path") or "?"
        if row.get("line"):
            where = f"{where}:{row['line']}"
        findings.append(
            Finding(
                repo=repo,
                tool="code_scanning",
                id=_text(row.get("number")),
                severity=severity,
                title=f"{row.get('rule_id') or '?'}: {_text(row.get('rule'))} ({where})",
                url=_text(row.get("html_url")),
                created_at=_text(row.get("created_at")),
                extra={
                    key: row.get(key)
                    for key in ("rule_id", "tool", "path", "line", "rule_severity",
                                "security_severity")
                },
            )
        )
    return findings


def secret_scanning_findings(repo: str, rows: list[dict]) -> list[Finding]:
    """Open secret-scanning alerts: high, or medium once GitHub says the secret is inactive.

    GitHub gives a secret alert no severity, so it never reaches critical, which is kept for a
    severity a security tool assigned; the report counts secrets on their own instead.
    """
    findings = []
    for row in rows:
        kind = row.get("secret_type_display_name") or row.get("secret_type") or "secret"
        leaked = ", publicly leaked" if row.get("publicly_leaked") else ""
        inactive = row.get("validity") == "inactive" and not row.get("publicly_leaked")
        findings.append(
            Finding(
                repo=repo,
                tool="secret_scanning",
                id=_text(row.get("number")),
                severity="medium" if inactive else "high",
                title=f"{kind}{leaked}",
                url=_text(row.get("html_url")),
                created_at=_text(row.get("created_at")),
                extra={
                    key: row.get(key)
                    for key in ("secret_type", "validity", "publicly_leaked",
                                "push_protection_bypassed")
                },
            )
        )
    return findings


# Triage holds a private vulnerability report and draft an advisory being written; a published
# advisory has already been disclosed with its fix.
OPEN_ADVISORY_STATES = ("triage", "draft")


def advisory_findings(repo: str, rows: list[dict]) -> list[Finding]:
    """Repository security advisories still in triage or draft."""
    findings = []
    for row in rows:
        if row.get("state") not in OPEN_ADVISORY_STATES:
            continue
        ident = row.get("ghsa_id") or "?"
        findings.append(
            Finding(
                repo=repo,
                tool="advisories",
                id=_text(ident),
                severity=level(row.get("severity"), "medium"),
                title=f"{ident} ({row.get('state')}): {_text(row.get('summary'))}",
                url=_text(row.get("html_url")),
                created_at=_text(row.get("created_at")),
                extra={"cve": row.get("cve_id"), "state": row.get("state")},
            )
        )
    return findings


def bot_pr_findings(repo: str, rows: list[dict], bot_logins: frozenset[str]) -> list[Finding]:
    """Open pull requests whose author is one of the bot logins."""
    findings = []
    for row in rows:
        author = _text(row.get("user"))
        if author not in bot_logins:
            continue
        findings.append(
            Finding(
                repo=repo,
                tool="bot_prs",
                id=_text(row.get("number")),
                severity="low",
                title=f"#{row.get('number')} {_text(row.get('title'))} ({author})",
                url=_text(row.get("html_url")),
                created_at=_text(row.get("created_at")),
                extra={"author": author, "branch": row.get("head"), "draft": row.get("draft")},
            )
        )
    return findings


def is_dashboard(row: dict, bot_logins: frozenset[str]) -> bool:
    """Whether an issue is a bot's Dependency Dashboard."""
    title = _text(row.get("title")).lower()
    return _text(row.get("user")) in bot_logins and DASHBOARD_TITLE in title


def issue_items(repo: str, rows: list[dict], bot_logins: frozenset[str]) -> list[dict]:
    """Open issues as work items, less pull requests and the dashboard, which is a finding."""
    return [
        {
            "repo": repo,
            "number": row.get("number"),
            "title": _text(row.get("title")),
            "labels": list(row.get("labels") or []),
            "type": _text(row.get("type")),
            "url": _text(row.get("html_url")),
            "created_at": _text(row.get("created_at")),
        }
        for row in rows
        if not row.get("is_pr") and not is_dashboard(row, bot_logins)
    ]


def _issue_field(content: dict, name: str) -> str:
    """One named single-select issue-field value off a GraphQL issue node, or empty."""
    for node in (content.get("issueFieldValues") or {}).get("nodes") or []:
        if isinstance(node, dict) and (node.get("field") or {}).get("name") == name:
            return _text(node.get("value"))
    return ""


def backlog_rows(repo: str, project: int, nodes: list[dict]) -> list[dict]:
    """Flatten a project's item nodes to one row per OPEN issue, as hyperi-ai's pm does.

    Args:
        repo: The member whose project it is.
        project: The project number.
        nodes: The item nodes BACKLOG_QUERY returns.

    Returns:
        One row per open issue: drafts, pull requests and closed issues are not backlog.
    """
    rows = []
    for item in nodes:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, dict) or not content.get("number"):
            continue
        if content.get("state") != "OPEN":
            continue
        labels = (content.get("labels") or {}).get("nodes") or []
        rows.append(
            {
                "repo": repo,
                "project": project,
                "status": _text((item.get("fieldValueByName") or {}).get("name")),
                "priority": _issue_field(content, PRIORITY_FIELD),
                "effort": _issue_field(content, EFFORT_FIELD),
                "type": _text((content.get("issueType") or {}).get("name")),
                "number": content["number"],
                "title": _text(content.get("title")),
                "url": _text(content.get("url")),
                "issue_repo": _text((content.get("repository") or {}).get("nameWithOwner"))
                or repo,
                "labels": [_text(label.get("name")) for label in labels if isinstance(label, dict)],
            }
        )
    return rows


def sort_backlog(rows: list[dict], model: BacklogModel) -> list[dict]:
    """Group by Status in workflow order, then highest priority first, unprioritised last.

    An unknown Status sorts after the known ones, by name.
    """
    status_rank = {name: n for n, name in enumerate(model.statuses)}
    priority_rank = {name: n for n, name in enumerate(model.priorities)}
    return sorted(
        rows,
        key=lambda r: (
            status_rank.get(r["status"], len(status_rank)),
            r["status"],
            priority_rank.get(r["priority"], len(priority_rank)),
            r["number"],
        ),
    )


def backlog_drift(
    repo: str,
    issues: list[dict],
    backlog: list[dict],
    model: BacklogModel,
    *,
    has_project: bool = True,
) -> list[dict]:
    """Open issues whose backlog label and project Status disagree.

    Args:
        repo: The member.
        issues: Its open issues, as issue_items returns them.
        backlog: Its project rows, as backlog_rows returns them.
        model: The label and the backlog Status.
        has_project: Whether the repo has a backlog project at all.

    Returns:
        An item at the backlog Status without the label, and a labelled issue anywhere else,
        including one that is not on the repo's project or whose repo has none.
    """
    own = [row for row in backlog if row["issue_repo"].lower() == repo.lower()]
    status_of = {row["number"]: row["status"] for row in own}
    at, label = model.backlog_status, model.label
    drift = [
        {
            "repo": repo,
            "number": row["number"],
            "title": row["title"],
            "url": row["url"],
            "status": row["status"],
            "problem": f"at {at} without the {label} label",
        }
        for row in own
        if row["status"] == at and label not in row["labels"]
    ]
    for issue in issues:
        if label not in issue["labels"]:
            continue
        status = status_of.get(issue["number"])
        if status == at:
            continue
        if issue["number"] in status_of:
            where = f"at {status or 'no status'}"
        else:
            where = "not on the project" if has_project else "the repo has no backlog project"
        drift.append(
            {
                "repo": repo,
                "number": issue["number"],
                "title": issue["title"],
                "url": issue["url"],
                "status": status or "",
                "problem": f"labelled {label} but {where}",
            }
        )
    return sorted(drift, key=lambda row: row["number"])


def _dashboard_severity(heading: str) -> str | None:
    lowered = heading.strip().lower()
    for prefix, severity in DASHBOARD_SECTIONS:
        if lowered.startswith(prefix):
            return severity
    return None


def _clean(text: str) -> str:
    text = _COMMENT.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _CHECKBOX.sub("", text.strip())
    return " ".join(text.replace("**", "").replace("`", "").split())


def dashboard_items(body: str) -> list[tuple[str, str, str, str]]:
    """The entries in a Renovate dashboard's flagged sections.

    A bullet or a table row under a heading DASHBOARD_SECTIONS names is one entry. The
    "act on all of them" checkboxes and a table's header row are not.

    Args:
        body: The dashboard issue's markdown.

    Returns:
        ``(section, severity, key, text)`` per entry, where key is Renovate's branch name when
        the entry carries one and the cleaned text otherwise.
    """
    items: list[tuple[str, str, str, str]] = []
    section, severity = "", None
    for raw in body.splitlines():
        line = raw.strip()
        heading = _HEADING.match(line)
        if heading:
            section = _clean(heading.group(1))
            severity = _dashboard_severity(section)
            continue
        if severity is None or not line or _ALL_MARK.search(line):
            continue
        if _TABLE_RULE.match(line):
            if items and items[-1][0] == section and items[-1][2].startswith("|"):
                items.pop()
            continue
        if line.startswith("- "):
            text = _clean(line[2:])
            branch = _BRANCH_MARK.search(line)
            key = branch.group(1) if branch else text
        elif line.startswith("|"):
            cells = [_clean(cell) for cell in line.strip("|").split("|")]
            text = " ".join(cell for cell in cells if cell)
            key = "|" + text
        else:
            continue
        if text:
            items.append((section, severity, key, text))
    return [(sec, sev, key.removeprefix("|"), text) for sec, sev, key, text in items]


def dashboard_findings(repo: str, row: dict) -> list[Finding]:
    """One finding per flagged dashboard entry, linked to the dashboard issue."""
    number = _text(row.get("number"))
    return [
        Finding(
            repo=repo,
            tool="renovate_dashboard",
            id=f"{number}:{key}",
            severity=severity,
            title=f"{section}: {text}",
            url=_text(row.get("html_url")),
            created_at=_text(row.get("created_at")),
            extra={"section": section, "issue": row.get("number")},
        )
        for section, severity, key, text in dashboard_items(_text(row.get("body")))
    ]


def latest_runs(rows: list[dict]) -> list[dict]:
    """The newest completed run of each workflow, newest first."""
    ordered = sorted(rows, key=lambda row: _text(row.get("created_at")), reverse=True)
    seen: set[object] = set()
    latest = []
    for row in ordered:
        workflow = row.get("workflow_id") or row.get("path") or row.get("name")
        if workflow in seen:
            continue
        seen.add(workflow)
        latest.append(row)
    return latest


def run_findings(repo: str, runs: list[dict], head_sha: str) -> list[Finding]:
    """Each workflow's latest run on the default branch, where it did not succeed."""
    findings = []
    for run in runs:
        conclusion = _text(run.get("conclusion"))
        severity = RUN_SEVERITY.get(conclusion)
        if severity is None:
            continue
        at_head = bool(head_sha) and run.get("head_sha") == head_sha
        behind = bool(head_sha) and not at_head
        where = f" at {_text(run.get('head_sha'))[:12]}, not the head" if behind else ""
        findings.append(
            Finding(
                repo=repo,
                tool="ci_runs",
                id=_text(run.get("id")),
                severity=severity,
                title=f"{_text(run.get('name'))}: {conclusion}{where}",
                url=_text(run.get("html_url")),
                created_at=_text(run.get("created_at")),
                extra={
                    "workflow": run.get("path"),
                    "event": run.get("event"),
                    "head_sha": run.get("head_sha"),
                    "at_head": at_head,
                },
            )
        )
    return findings


def annotation_findings(
    repo: str, run: dict, jobs: list[tuple[dict, list[dict]]]
) -> list[Finding]:
    """A run's warning and failure annotations, one finding per distinct annotation.

    The same annotation raised by several jobs of the run is one finding naming each job.

    Args:
        repo: The ``owner/name`` slug.
        run: The workflow run.
        jobs: Each check run of the run's suite, with its annotations.

    Returns:
        The findings, in the order first seen.
    """
    grouped: dict[tuple, tuple[dict, dict, list[str]]] = {}
    for job, annotations in jobs:
        for note in annotations:
            if _text(note.get("annotation_level")) not in ANNOTATION_SEVERITY:
                continue
            key = (
                note.get("annotation_level"),
                note.get("path"),
                note.get("start_line"),
                note.get("title"),
                note.get("message"),
            )
            entry = grouped.setdefault(key, (note, job, []))
            entry[2].append(_text(job.get("name")))
    findings = []
    for key, (note, job, names) in grouped.items():
        digest = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:12]
        message = _text(note.get("message")).strip()
        first = message.splitlines()[0] if message else ""
        heading = _text(note.get("title")).strip()
        label = f"{heading}: {first}" if heading and heading not in first else first
        findings.append(
            Finding(
                repo=repo,
                tool="ci_annotations",
                id=f"{run.get('id')}:{digest}",
                severity=ANNOTATION_SEVERITY[_text(note.get("annotation_level"))],
                title=f"{_text(run.get('name'))}: {label}".strip(),
                url=_text(job.get("html_url")),
                created_at=_text(run.get("created_at")),
                extra={
                    "level": note.get("annotation_level"),
                    "path": note.get("path"),
                    "line": note.get("start_line"),
                    "message": message,
                    "jobs": names,
                    "run": run.get("id"),
                },
            )
        )
    return findings


# ---------------------------------------------------------------------------
# One repo
# ---------------------------------------------------------------------------


def _read(
    member: Member,
    tool: str,
    unreadable: list[Unreadable],
    read: Callable[[], list[Finding]],
) -> list[Finding]:
    """Run one tool's read, turning a refusal into an unreadable entry for that tool alone."""
    try:
        return read()
    except GhError as exc:
        unreadable.append(Unreadable(member.repo, tool, exc.reason))
        return []


def _ci(
    member: Member,
    branch: str,
    head_sha: str,
    api: Api,
    config: SweepConfig,
    unreadable: list[Unreadable],
) -> list[Finding]:
    """The CI tools: each workflow's latest run, and that run's annotations."""
    repo = member.repo
    wanted = [tool for tool in ("ci_runs", "ci_annotations") if tool in config.tools]
    endpoint = (
        f"repos/{repo}/actions/runs?branch={quote(branch, safe='')}&status=completed"
        f"&per_page={min(config.run_window, GH_PAGE_MAX)}"
    )
    try:
        runs = latest_runs(api(endpoint, RUNS_JQ, False))
    except GhError as exc:
        unreadable.extend(Unreadable(repo, tool, exc.reason) for tool in wanted)
        return []
    findings = run_findings(repo, runs, head_sha) if "ci_runs" in config.tools else []
    if "ci_annotations" not in config.tools:
        return findings
    for run in runs:
        suite = run.get("check_suite_id")
        if not suite:
            continue

        def annotated(run: dict = run, suite: object = suite) -> list[Finding]:
            jobs = api(f"repos/{repo}/check-suites/{suite}/check-runs?per_page=100",
                       CHECK_RUNS_JQ, True)
            pairs = [
                (job, api(f"repos/{repo}/check-runs/{job['id']}/annotations?per_page=100",
                          ANNOTATIONS_JQ, True))
                for job in jobs
                if job.get("annotations")
            ]
            return annotation_findings(repo, run, pairs)

        findings += _read(member, "ci_annotations", unreadable, annotated)
    return findings


def collect_repo(
    member: Member,
    api: Api,
    config: SweepConfig,
    *,
    graphql: Graphql | None = None,
    projects: ProjectIndex | None = None,
) -> RepoResult:
    """Read every enabled tool on one member's default branch.

    Args:
        member: The member to read.
        api: The REST read, gh_api in a live run.
        config: The resolved settings.
        graphql: The GraphQL read the backlog needs, gh_graphql in a live run.
        projects: The org's projects by title, listed once per sweep.

    Returns:
        What was found and what could not be read. A repo GitHub will not show at all is one
        unreadable entry per enabled tool.
    """
    repo = member.repo
    unreadable: list[Unreadable] = []
    try:
        meta = api(f"repos/{repo}", REPO_JQ, False)[0]
    except (GhError, IndexError) as exc:
        reason = exc.reason if isinstance(exc, GhError) else "empty answer"
        lost = [Unreadable(repo, tool, f"repo not readable: {reason}") for tool in TOOLS
                if tool in config.tools]
        return RepoResult(member, "", "", False, [], lost)
    branch = _text(meta.get("default_branch"))
    head_sha = ""
    try:
        head = api(f"repos/{repo}/commits/{quote(branch, safe='')}", HEAD_JQ, False)
        head_sha = _text(head[0].get("sha")) if head else ""
    except GhError as exc:
        unreadable.append(Unreadable(repo, "head", exc.reason))
    ref = quote(f"refs/heads/{branch}", safe="")
    readers: dict[str, Callable[[], list[Finding]]] = {
        "dependabot": lambda: dependabot_findings(
            repo, api(f"repos/{repo}/dependabot/alerts?state=open&per_page=100",
                      DEPENDABOT_JQ, True)),
        "code_scanning": lambda: code_scanning_findings(
            repo, api(f"repos/{repo}/code-scanning/alerts?state=open&per_page=100&ref={ref}",
                      CODE_SCANNING_JQ, True)),
        "secret_scanning": lambda: secret_scanning_findings(
            repo, api(f"repos/{repo}/secret-scanning/alerts?state=open&per_page=100"
                      "&hide_secret=true", SECRET_SCANNING_JQ, True)),
        "advisories": lambda: advisory_findings(
            repo, api(f"repos/{repo}/security-advisories?per_page=100", ADVISORY_JQ, True)),
        "bot_prs": lambda: bot_pr_findings(
            repo, api(f"repos/{repo}/pulls?state=open&per_page=100", PULLS_JQ, True),
            config.bot_logins),
    }
    findings: list[Finding] = []
    for tool, read in readers.items():
        if tool in config.tools:
            findings += _read(member, tool, unreadable, read)
    dashboard, issues = _issues(member, api, config, unreadable)
    findings += dashboard
    if {"ci_runs", "ci_annotations"} & config.tools:
        findings += _ci(member, branch, head_sha, api, config, unreadable)
    project: int | str = ""
    backlog: list[dict] = []
    drift: list[dict] = []
    if "backlog" in config.tools:
        project, backlog = _backlog(member, config, graphql, projects, unreadable)
        if project != NOT_READ:
            drift = backlog_drift(
                repo, issues or [], backlog, config.backlog, has_project=project != NO_PROJECT
            )
    # One refusal repeated per workflow run is still one gap.
    distinct = list(dict.fromkeys(unreadable))
    return RepoResult(
        member,
        branch,
        head_sha,
        bool(meta.get("archived")),
        findings,
        distinct,
        issues=(issues or []) if "issues" in config.tools else [],
        backlog=sort_backlog(backlog, config.backlog),
        drift=drift,
        project=project,
        repo_id=meta.get("id") if isinstance(meta.get("id"), int) else None,
        full_name=_text(meta.get("full_name")),
    )


def _issues(
    member: Member, api: Api, config: SweepConfig, unreadable: list[Unreadable]
) -> tuple[list[Finding], list[dict] | None]:
    """Open issues and the Renovate dashboard, which share one read the backlog check uses too.

    Returns:
        The dashboard findings, and the issues as work items, or None when they were not read.
    """
    wanted = [t for t in ("issues", "renovate_dashboard", "backlog") if t in config.tools]
    if not wanted:
        return [], None
    repo = member.repo
    try:
        rows = api(f"repos/{repo}/issues?state=open&per_page=100", ISSUES_JQ, True)
    except GhError as exc:
        unreadable.extend(Unreadable(repo, tool, exc.reason) for tool in wanted)
        return [], None
    findings: list[Finding] = []
    if "renovate_dashboard" in wanted:
        for row in rows:
            if not row.get("is_pr") and is_dashboard(row, config.bot_logins):
                findings += dashboard_findings(repo, row)
    return findings, issue_items(repo, rows, config.bot_logins)


def _backlog(
    member: Member,
    config: SweepConfig,
    graphql: Graphql | None,
    projects: ProjectIndex | None,
    unreadable: list[Unreadable],
) -> tuple[int | str, list[dict]]:
    """The member's project and its open items, or why there are none.

    Returns:
        The project number with its rows, NO_PROJECT when the org has none titled after the
        repo, or NOT_READ, with an unreadable entry, when the projects could not be read.
    """
    repo = member.repo
    if projects is None or graphql is None:
        unreadable.append(Unreadable(repo, "backlog", "projects not listed"))
        return NOT_READ, []
    if projects.error:
        unreadable.append(Unreadable(repo, "backlog", f"projects not readable: {projects.error}"))
        return NOT_READ, []
    number = projects.numbers.get(repo.rsplit("/", 1)[-1])
    if number is None:
        return NO_PROJECT, []
    try:
        nodes = graphql(BACKLOG_QUERY, {"org": config.org, "number": number}, BACKLOG_JQ)
    except GhError as exc:
        unreadable.append(Unreadable(repo, "backlog", exc.reason))
        return NOT_READ, []
    if any(node.get("missing_project") for node in nodes if isinstance(node, dict)):
        unreadable.append(Unreadable(repo, "backlog", f"project {number} not readable"))
        return NOT_READ, []
    return number, backlog_rows(repo, number, nodes)


def sweep(
    members: list[Member],
    api: Api,
    config: SweepConfig,
    progress: Callable[[str], None] | None = None,
    *,
    graphql: Graphql | None = None,
    projects: ProjectIndex | None = None,
) -> list[RepoResult]:
    """Read every member, ``config.workers`` at a time, and return results in member order."""

    def one(member: Member) -> RepoResult:
        result = collect_repo(member, api, config, graphql=graphql, projects=projects)
        if progress is not None:
            progress(
                f"{member.repo}: {len(result.findings)} finding(s), {len(result.issues)} "
                f"issue(s), {len(result.backlog)} backlog item(s), "
                f"{len(result.unreadable)} unreadable"
            )
        return result

    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as pool:
        return list(pool.map(one, members))


# ---------------------------------------------------------------------------
# The bridge subset check
# ---------------------------------------------------------------------------


def bridge_alerts(org: str, api: Api) -> list[BridgeAlert]:
    """Every open org-level alert of the three kinds the webhook bridge forwards.

    Raises:
        GhError: If any of the three org lists cannot be read; a partial list proves nothing.
    """
    alerts = []
    for tool, endpoint in BRIDGED.items():
        for row in api(endpoint.format(org=org), BRIDGE_JQ, True):
            alerts.append(
                BridgeAlert(
                    tool=tool,
                    repo=_text(row.get("repo")),
                    url=_text(row.get("html_url")),
                    number=row.get("number"),
                    created_at=_text(row.get("created_at")),
                    repo_id=row.get("repo_id"),
                )
            )
    return alerts


def bridge_gap(
    alerts: list[BridgeAlert], results: list[RepoResult]
) -> tuple[list[BridgeAlert], list[BridgeAlert]]:
    """Split org alerts into those on members the sweep missed, and those on non-members.

    A member is matched on GitHub's repo id when both sides have one, so a renamed repo that
    GitHub redirects still counts as a member; its slug and its current name match too.

    Args:
        alerts: The org's open bridged alerts.
        results: Every member's result.

    Returns:
        ``(missing, outside)``: alerts on a member whose url no finding carries, and alerts on
        a repo that is not a member.
    """
    ids = {result.repo_id for result in results if result.repo_id is not None}
    names = {result.member.repo.lower() for result in results}
    names |= {result.full_name.lower() for result in results if result.full_name}
    found = {finding.url for finding in all_findings(results) if finding.url}

    def on_member(alert: BridgeAlert) -> bool:
        return alert.repo_id in ids or alert.repo.lower() in names

    missing = [a for a in alerts if on_member(a) and a.url not in found]
    outside = [a for a in alerts if not on_member(a)]
    return missing, outside


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _report_row(row: dict) -> dict:
    """A backlog row as the report carries it."""
    return {key: row[key] for key in BACKLOG_FIELDS}


def all_findings(results: Iterable[RepoResult]) -> list[Finding]:
    """Every member's findings, in member order."""
    return list(chain.from_iterable(result.findings for result in results))


def _id_key(value: str) -> tuple[int, int, str]:
    return (0, int(value), "") if value.isdigit() else (1, 0, value)


def rank(findings: Iterable[Finding]) -> list[Finding]:
    """Findings ordered by severity, then repo, tool and id."""
    order = {severity: n for n, severity in enumerate(SEVERITIES)}
    return sorted(
        findings,
        key=lambda f: (order.get(f.severity, len(SEVERITIES)), f.repo, f.tool, _id_key(f.id)),
    )


def build_report(
    stack: str,
    results: list[RepoResult],
    tools: Iterable[str],
    bridge: dict | None = None,
    generated_at: str | None = None,
) -> dict:
    """The sweep as one JSON-ready mapping.

    Args:
        stack: The stack key membership was read from.
        results: One result per member, in member order.
        tools: The tools that ran, for the per-tool totals.
        bridge: The bridge check's outcome, when it ran.
        generated_at: An RFC 3339 timestamp, or None for now.

    Returns:
        ``generated_at``, ``stack``, ``members``, the severity-ranked ``findings``, the work
        items ``issues``, ``backlog`` and ``backlog_drift``, ``unreadable`` and ``totals``,
        plus ``bridge`` when given.
    """
    findings = rank(all_findings(results))
    unreadable = list(chain.from_iterable(result.unreadable for result in results))
    wanted = set(tools) - set(WORK_TOOLS)
    enabled = [tool for tool in TOOLS if tool in wanted]
    issues = list(chain.from_iterable(result.issues for result in results))
    rows = chain.from_iterable(result.backlog for result in results)
    backlog = [_report_row(row) for row in rows]
    drift = list(chain.from_iterable(result.drift for result in results))
    report = {
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "stack": stack,
        "members": [
            {
                "name": result.member.name,
                "repo": result.member.repo,
                "why": list(result.member.why),
                "default_branch": result.default_branch,
                "head_sha": result.head_sha,
                "archived": result.archived,
                "project": result.project,
            }
            for result in results
        ],
        "findings": [asdict(f) for f in findings],
        "issues": issues,
        "backlog": backlog,
        "backlog_drift": drift,
        "unreadable": [asdict(u) for u in unreadable],
        "totals": {
            "members": len(results),
            "findings": len(findings),
            "unreadable": len(unreadable),
            "by_severity": {s: sum(f.severity == s for f in findings) for s in SEVERITIES},
            "by_tool": {t: sum(f.tool == t for f in findings) for t in enabled},
            "by_repo": {
                r.member.repo: sum(f.repo == r.member.repo for f in findings) for r in results
            },
            "work_items": {
                "issues": len(issues),
                "backlog": len(backlog),
                "backlog_drift": len(drift),
            },
            "work_by_repo": {
                r.member.repo: {
                    "issues": len(r.issues),
                    "backlog": len(r.backlog),
                    "backlog_drift": len(r.drift),
                    "project": r.project,
                }
                for r in results
            },
        },
    }
    if bridge is not None:
        report["bridge"] = bridge
    return report


def _tally(values: Iterable[str], order: Iterable[str], blank: str) -> str:
    """``name count`` pairs in the given order, then any other name, then the blank count."""
    counts: dict[str, int] = {}
    for value in values:
        counts[value or blank] = counts.get(value or blank, 0) + 1
    names = [name for name in order if name in counts]
    names += sorted(name for name in counts if name not in names and name != blank)
    names += [blank] if blank in counts else []
    return ", ".join(f"{name} {counts[name]}" for name in names) or "none"


def _work_by_repo(report: dict) -> dict[str, dict[str, list[dict]]]:
    """The report's issues, backlog rows and drift, grouped by member in member order."""
    by_repo: dict[str, dict[str, list[dict]]] = {
        m["repo"]: {"issues": [], "backlog": [], "drift": []} for m in report["members"]
    }
    for key, source in (("issues", "issues"), ("backlog", "backlog"), ("drift", "backlog_drift")):
        for row in report[source]:
            by_repo.setdefault(row["repo"], {"issues": [], "backlog": [], "drift": []})[
                key
            ].append(row)
    return by_repo


def _work_counts(project: int | str, items: dict[str, list[dict]], model: BacklogModel) -> str:
    """One member's work items as counts: issues, then its backlog by Status and by Priority."""
    if project in ("", NOT_READ):
        backlog = f"backlog {project or 'off'}"
    elif project == NO_PROJECT:
        backlog = "backlog: no project"
    else:
        statuses = _tally((r["status"] for r in items["backlog"]), model.statuses, "no status")
        priorities = _tally(
            (r["priority"] for r in items["backlog"]), model.priorities, "unprioritised"
        )
        backlog = (
            f"backlog project {project}: {len(items['backlog'])} item(s), status "
            f"{statuses}; priority {priorities}; {len(items['drift'])} label drift"
        )
    return f"{len(items['issues'])} open issue(s); {backlog}"


def render_summary(report: dict, model: BacklogModel) -> list[str]:
    """The work items per member: counts first, then each backlog item, drift and issue."""
    lines = ["SUMMARY (work items, not severity-ranked)"]
    projects = {m["repo"]: m.get("project", "") for m in report["members"]}
    for repo, items in _work_by_repo(report).items():
        lines.append(f"  {repo}: {_work_counts(projects.get(repo, ''), items, model)}")
        lines.extend(
            f"    backlog  {r['status'] or '-':<12} {r['priority'] or '-':<4} #{r['number']} "
            f"{r['title']}  {r['url']}"
            for r in items["backlog"]
        )
        lines.extend(
            f"    drift    #{r['number']} {r['problem']}: {r['title']}  {r['url']}"
            for r in items["drift"]
        )
        lines.extend(
            f"    issue    #{r['number']} "
            f"{('[' + ', '.join(r['labels']) + '] ') if r['labels'] else ''}"
            f"{r['title']}  {r['url']}"
            for r in items["issues"]
        )
    lines.append("")
    return lines


def bridge_section(missing: list[BridgeAlert], outside: list[BridgeAlert], total: int) -> dict:
    """The bridge check's outcome for the report."""
    return {
        "org_alerts": total,
        "missing": [asdict(a) for a in missing],
        "outside_members": [asdict(a) for a in outside],
    }


def render_members(stack: str, members: list[dict]) -> str:
    """The member list with the reasons for each, as text."""
    lines = [f"stack {stack}: {len(members)} member(s)"]
    for member in members:
        at = ""
        if member.get("default_branch"):
            at = f" @ {member['default_branch']} {str(member.get('head_sha') or '?')[:12]}"
        lines.append(f"  {member['repo']}{at} -- {'; '.join(member['why'])}")
    return "\n".join(lines)


def render_text(report: dict, model: BacklogModel | None = None) -> str:
    """The report as text.

    Members, the work-item summary, findings by severity then repo, what was not readable,
    the bridge check, then the totals.
    """
    lines = [render_members(report["stack"], report["members"]), ""]
    lines += render_summary(report, model or BacklogModel())
    by_severity: dict[str, dict[str, list[dict]]] = {}
    for finding in report["findings"]:
        by_severity.setdefault(finding["severity"], {}).setdefault(finding["repo"], []).append(
            finding
        )
    for severity in SEVERITIES:
        repos = by_severity.get(severity)
        if not repos:
            continue
        count = sum(len(items) for items in repos.values())
        lines.append(f"{severity.upper()} ({count})")
        for repo, items in repos.items():
            lines.append(f"  {repo}")
            lines.extend(
                f"    {f['tool']:<18} {f['id']:<10} {f['title']}  {f['url']}" for f in items
            )
        lines.append("")
    if report["unreadable"]:
        lines.append(f"NOT READABLE ({len(report['unreadable'])})")
        lines.extend(
            f"  {u['repo']} {u['tool']}: {u['reason']}" for u in report["unreadable"]
        )
        lines.append("")
    bridge = report.get("bridge")
    if bridge is not None:
        lines.append(
            f"BRIDGE: {bridge['org_alerts']} open org alert(s), "
            f"{len(bridge['missing'])} on members missing from the sweep, "
            f"{len(bridge['outside_members'])} on non-members"
        )
        lines.extend(f"  MISSING {a['repo']} {a['tool']}: {a['url']}" for a in bridge["missing"])
        lines.extend(
            f"  outside {a['repo']} {a['tool']}: {a['url']}" for a in bridge["outside_members"]
        )
        lines.append("")
    totals = report["totals"]
    severities = ", ".join(f"{s} {n}" for s, n in totals["by_severity"].items())
    tools = ", ".join(f"{t} {n}" for t, n in totals["by_tool"].items())
    lines.append(
        f"totals: {totals['findings']} finding(s) across {totals['members']} member(s), "
        f"{totals['unreadable']} unreadable"
    )
    work = totals["work_items"]
    lines.append(f"  by severity: {severities}")
    lines.append(f"  by tool: {tools}")
    lines.append(
        f"  work items: {work['issues']} open issue(s), {work['backlog']} backlog item(s), "
        f"{work['backlog_drift']} label drift"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Slack: printed for a separate poster, which owns the channel and the token
# ---------------------------------------------------------------------------

SLACK_SUMMARY_LINES = 8
SLACK_REPLY_CHARS = 3500
SLACK_REPLIES = 20
# The per-group budget when the caller sets none; the cascade's slack.group_replies sets it.
SLACK_GROUP_REPLIES = 2
# A link URL longer than this is shown as text, which bounds the "N more" line a reply keeps
# room for.
SLACK_URL_CHARS = 500
SLACK_MORE_CHARS = SLACK_URL_CHARS + 60
# A link label past this many characters is clipped for reading.
SLACK_LABEL_CHARS = 100
SLACK_STATS = (
    "secrets", "critical", "high", "medium", "low", "ci_failing", "issues", "backlog",
    "bot_prs", "drift", "unreadable", "members",
)
# The stats keys counted from findings; slack_buckets fills one list per key.
SLACK_FINDING_STATS = ("secrets", "critical", "high", "medium", "low", "ci_failing")
# The only tools stats critical and high, and the summary's Security line, count.
SECURITY_TOOLS = ("dependabot", "code_scanning", "advisories")
# The finding groups, each with the tools it holds, in the order their thread replies are
# posted; slack_groups follows them with drift, open issues, backlog items and not readable.
SLACK_FINDING_GROUPS = (
    ("Secret scanning", ("secret_scanning",)),
    ("Code scanning", ("code_scanning",)),
    ("Dependabot", ("dependabot",)),
    ("Security advisories", ("advisories",)),
    ("CI on main", ("ci_runs", "ci_annotations")),
    ("Bot PRs and Renovate", ("bot_prs", "renovate_dashboard")),
)
_SAFE_URL = re.compile(r"^https://[^\s<>|]+$")


def slack_escape(text: object) -> str:
    """Make a string inert in Slack mrkdwn: no link, mention or markup can come out of it."""
    return _text(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def safe_url(url: object) -> bool:
    """Whether a URL is https, short enough to link, and carries nothing that ends a link."""
    text = _text(url)
    return len(text) <= SLACK_URL_CHARS and bool(_SAFE_URL.match(text))


def slack_link(url: object, label: object) -> str:
    """A link built here, its label clipped and escaped; an unsafe URL is shown as text."""
    text = _text(label)
    if len(text) > SLACK_LABEL_CHARS:
        text = text[: SLACK_LABEL_CHARS - 3] + "..."
    if safe_url(url):
        return f"<{_text(url)}|{slack_escape(text)}>"
    return slack_escape(text)


type Buckets = dict[str, list[dict]]


def slack_buckets(findings: Iterable[dict]) -> Buckets:
    """The findings behind each finding count in the Slack summary and stats, by stats key.

    critical and high hold SECURITY_TOOLS findings only, so a secret alert counts under secrets
    and a failed main run under ci_failing alone. medium and low hold every tool's. The summary's
    lines and the stats are both read off these lists, so the two cannot disagree.
    """
    buckets: Buckets = {key: [] for key in SLACK_FINDING_STATS}
    for finding in findings:
        tool, severity = finding["tool"], finding["severity"]
        if tool == "secret_scanning":
            buckets["secrets"].append(finding)
        if tool == "ci_runs" and severity == "high":
            buckets["ci_failing"].append(finding)
        if severity in ("medium", "low"):
            buckets[severity].append(finding)
        elif severity in ("critical", "high") and tool in SECURITY_TOOLS:
            buckets[severity].append(finding)
    return buckets


def slack_stats(report: dict, buckets: Buckets) -> dict[str, int]:
    """The fixed integer counts a poster routes on; every key is present, 0 when none.

    Args:
        report: The report build_report made.
        buckets: slack_buckets over the report's findings.

    Returns:
        One integer per SLACK_STATS key, in that order.
    """
    totals = report["totals"]
    counts = {key: len(buckets[key]) for key in SLACK_FINDING_STATS}
    counts |= {
        "issues": totals["work_items"]["issues"],
        "backlog": totals["work_items"]["backlog"],
        "bot_prs": sum(f["tool"] == "bot_prs" for f in report["findings"]),
        "drift": totals["work_items"]["backlog_drift"],
        "unreadable": totals["unreadable"],
        "members": totals["members"],
    }
    return {key: int(counts[key]) for key in SLACK_STATS}


def _short(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


def _repo_counts(rows: Iterable[dict], limit: int = 4) -> str:
    """The repos the rows fall on, busiest first, escaped, the tail folded into a count."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[_short(row["repo"])] = counts.get(_short(row["repo"]), 0) + 1
    ranked = sorted(counts.items(), key=lambda pair: -pair[1])
    shown = ", ".join(f"{slack_escape(name)} {n}" for name, n in ranked[:limit])
    rest = len(ranked) - limit
    return f"{shown}, +{rest} more" if rest > 0 else shown


def slack_summary(report: dict, model: BacklogModel, buckets: Buckets) -> str:
    """At most SLACK_SUMMARY_LINES lines of mrkdwn: the headline, then what matters most first.

    Args:
        report: The report build_report made.
        model: The backlog model, for the priority order.
        buckets: slack_buckets over the report's findings, the same lists the stats count.

    Returns:
        The parent message.
    """
    findings = report["findings"]
    stats = slack_stats(report, buckets)
    lines = [
        f"*DFE suite sweep*: stack {slack_escape(report['stack'])}, {stats['members']} members, "
        f"{slack_escape(report['generated_at'])}"
    ]
    bridge = report.get("bridge")
    gap = bool(bridge and bridge["missing"])
    work = stats["issues"] + stats["backlog"] + stats["drift"]
    if not findings and not work and not stats["unreadable"] and not gap:
        holds = "; the alert bridge is a subset" if bridge else ""
        lines.append(f"All clear: nothing open on any member{holds}.")
        return "\n".join(lines)
    if stats["secrets"]:
        lines.append(
            f"*Secret scanning: {stats['secrets']} open alert(s)* on "
            f"{_repo_counts(buckets['secrets'])}"
        )
    security = buckets["critical"] + buckets["high"]
    if security:
        lines.append(
            f"*Security: {stats['critical']} critical, {stats['high']} high* on "
            f"{_repo_counts(security)}"
        )
    if stats["ci_failing"]:
        lines.append(
            f"*Failing main CI*: {stats['ci_failing']} workflow(s) on "
            f"{_repo_counts(buckets['ci_failing'])}"
        )
    rest = buckets["medium"] + buckets["low"]
    if rest:
        groups = []
        for name, tools in SLACK_FINDING_GROUPS:
            count = sum(f["tool"] in tools for f in rest)
            if count:
                groups.append(f"{name} {count}")
        lines.append(f"Medium {stats['medium']}, low {stats['low']}: {', '.join(groups)}")
    if work or stats["bot_prs"]:
        priorities = _tally((r["priority"] for r in report["backlog"]), model.priorities,
                            "unprioritised")
        backlog = f" ({slack_escape(priorities)})" if stats["backlog"] else ""
        lines.append(
            f"Work items: {stats['issues']} open issues, {stats['backlog']} backlog{backlog}, "
            f"{stats['bot_prs']} bot PRs, {stats['drift']} label drift"
        )
    if stats["unreadable"]:
        lines.append(
            f"Not readable: {stats['unreadable']} on {_repo_counts(report['unreadable'])}"
        )
    if gap:
        lines.append(
            f"*Alert bridge gap*: {len(bridge['missing'])} org alert(s) on members missing "
            f"from the sweep"
        )
    elif bridge is not None:
        lines.append(
            f"Alert bridge is a subset: {bridge['org_alerts']} open org alerts, "
            f"{len(bridge['outside_members'])} on non-members"
        )
    return "\n".join(lines[:SLACK_SUMMARY_LINES])


def _severity_note(rows: list[dict]) -> str:
    counts = [(s, sum(r["severity"] == s for r in rows)) for s in SEVERITIES]
    return ", ".join(f"{n} {s}" for s, n in counts if n)


def _by_repo_lines(rows: list[dict], line: Callable[[dict], str]) -> list[tuple[str, str]]:
    """``(kind, text)`` lines: a bold repo header, then that repo's rows, in first-seen order."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["repo"], []).append(row)
    lines: list[tuple[str, str]] = []
    for repo, items in grouped.items():
        lines.append(("repo", f"*{slack_escape(_short(repo))}*"))
        lines.extend(("item", line(item)) for item in items)
    return lines


# A title past this many characters is clipped for reading; _pack separately clips any whole
# line too long for a reply.
SLACK_TITLE_CHARS = 300


def _title(value: object) -> str:
    text = _text(value)
    clipped = text if len(text) <= SLACK_TITLE_CHARS else text[: SLACK_TITLE_CHARS - 3] + "..."
    return slack_escape(clipped)


def _finding_line(row: dict) -> str:
    label = f"{row['tool']} #{row['id']}"
    return f"- {slack_link(row['url'], label)} {row['severity']}: {_title(row['title'])}"


def _drift_line(row: dict) -> str:
    link = slack_link(row["url"], f"#{row['number']}")
    return f"- {link} {slack_escape(row['problem'])}: {_title(row['title'])}"


def _issue_line(row: dict) -> str:
    link = slack_link(row["url"], f"#{row['number']}")
    labels = f" [{slack_escape(', '.join(row['labels']))}]" if row["labels"] else ""
    return f"- {link} {_title(row['title'])}{labels}"


def _backlog_line(row: dict) -> str:
    link = slack_link(row["url"], f"#{row['number']}")
    where = f"{slack_escape(row['status'] or '-')} {slack_escape(row['priority'] or '-')}"
    return f"- {link} {where}: {_title(row['title'])}"


def _unreadable_line(row: dict) -> str:
    return f"- {slack_escape(row['tool'])}: {slack_escape(row['reason'])}"


def slack_groups(report: dict, model: BacklogModel) -> list[tuple[str, str, list]]:
    """Every non-empty group, in posting order: (name, count note, ``(kind, text)`` lines)."""
    groups = []
    for name, tools in SLACK_FINDING_GROUPS:
        rows = [f for f in report["findings"] if f["tool"] in tools]
        if rows:
            note = f"{len(rows)} ({_severity_note(rows)})"
            groups.append((name, note, _by_repo_lines(rows, _finding_line)))
    issues, backlog = report["issues"], report["backlog"]
    priorities = _tally((r["priority"] for r in backlog), model.priorities, "unprioritised")
    work = (
        ("Backlog drift", report["backlog_drift"], "", _drift_line),
        ("Open issues", issues, f" on {len({r['repo'] for r in issues})} repo(s)", _issue_line),
        ("Backlog items", backlog, f" ({slack_escape(priorities)})", _backlog_line),
        ("Not readable", report["unreadable"], "", _unreadable_line),
    )
    for name, rows, note, line in work:
        if rows:
            groups.append((name, f"{len(rows)}{note}", _by_repo_lines(rows, line)))
    return groups


def slack_detail(
    report: dict,
    model: BacklogModel,
    report_url: str = "",
    group_replies: int = SLACK_GROUP_REPLIES,
) -> list[dict[str, str]]:
    """The thread replies: one or more per non-empty group, each under SLACK_REPLY_CHARS.

    A group too long for one reply continues in replies headed ``*<group> (cont.)*``, up to
    ``group_replies`` of them, so a long group cannot crowd a later one out; past that budget
    the group's last reply says how many of its lines were left out. Past SLACK_REPLIES in all,
    the groups that did not fit are counted into the thread's last reply.

    Args:
        report: The report build_report made.
        model: The backlog model, for the priority order.
        report_url: Where the full report can be read, or empty.
        group_replies: The most replies one group may take.

    Returns:
        ``{"group", "text"}`` per reply, in posting order.
    """
    # (group, reply lines, item lines cut after this reply)
    replies: list[tuple[str, Reply, int]] = []
    left_out = 0
    for name, note, lines in slack_groups(report, model):
        packed = _pack(name, note, lines)
        room = min(group_replies, SLACK_REPLIES - len(replies))
        if room <= 0:
            left_out += _items(packed)
            continue
        kept = packed[:room]
        replies += [(name, reply, 0) for reply in kept[:-1]]
        replies.append((name, kept[-1], _items(packed[room:])))
    if left_out:
        name, last, cut = replies[-1]
        replies[-1] = (name, last, cut + left_out)
    return [
        {"group": name, "text": _with_more(reply, cut, report_url)}
        for name, reply, cut in replies
    ]


def _items(replies: list[Reply]) -> int:
    """How many item lines the replies hold, headers aside."""
    return sum(kind == "item" for kind, _ in chain.from_iterable(replies))


def _pack(name: str, note: str, lines: list[tuple[str, str]]) -> list[Reply]:
    """One group's lines as replies under SLACK_REPLY_CHARS, each continuation re-headed.

    Every line passes through here, and each is clipped to _line_cap, so any reply can hold its
    header, a repo header, one item and the "N more" line; a reply is never only a header.
    """
    first = f"*{slack_escape(name)}* -- {note}"
    cont = f"*{slack_escape(name)} (cont.)*"
    cap = _line_cap(max(first, cont, key=len))
    replies: list[Reply] = []
    reply: Reply = [("header", first)]
    repo_header = ""
    for kind, raw in lines:
        text = _clip_line(raw, cap)
        if kind == "repo":
            repo_header = text
        size = sum(len(t) + 1 for _, t in reply) + len(text)
        if size >= SLACK_REPLY_CHARS and _has_items(reply):
            replies.append(_without_trailing_repo(reply))
            reply = [("header", cont)]
            if kind == "item":
                reply.append(("repo", repo_header))
        reply.append((kind, text))
    replies.append(reply)
    return [reply for reply in replies if _has_items(reply)]


def _has_items(reply: Reply) -> bool:
    return any(kind == "item" for kind, _ in reply)


def _line_cap(header: str) -> int:
    """The longest line a reply under ``header`` takes beside one repo line and "N more"."""
    return (SLACK_REPLY_CHARS - len(header) - SLACK_MORE_CHARS - 4) // 2


def _clip_line(text: str, cap: int) -> str:
    """A line cut to ``cap`` characters, never inside a link or an escaped character."""
    if len(text) <= cap:
        return text
    cut = text[: cap - 3]
    if cut.rfind("<") > cut.rfind(">"):
        cut = cut[: cut.rfind("<")]
    if cut.rfind("&") > cut.rfind(";"):
        cut = cut[: cut.rfind("&")]
    return cut + "..."


def _with_more(reply: Reply, cut: int, report_url: str) -> str:
    """A reply's text under SLACK_REPLY_CHARS, ending with how many item lines were cut.

    Lines come off the end until the reply and its "N more" line fit, but never its last item.
    """
    lines = list(reply)
    while True:
        more = _more_line(cut, report_url) if cut else ""
        size = sum(len(t) + 1 for _, t in lines) + len(more)
        if size < SLACK_REPLY_CHARS or not _has_items(lines[:-1]):
            break
        kind, _ = lines.pop()
        cut += kind == "item"
    if cut:
        lines = [*_without_trailing_repo(lines), ("more", _more_line(cut, report_url))]
    return "\n".join(text for _, text in lines)


def _without_trailing_repo(lines: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """A reply minus a repo header left with no items under it."""
    while len(lines) > 1 and lines[-1][0] == "repo":
        lines = lines[:-1]
    return lines


def _more_line(count: int, report_url: str) -> str:
    if safe_url(report_url):
        return f"_{count} more, see {slack_link(report_url, 'full report')}_"
    return f"_{count} more, run `dfe-sweep` for the full list_"


def render_slack(
    report: dict,
    model: BacklogModel | None = None,
    report_url: str = "",
    group_replies: int = SLACK_GROUP_REPLIES,
) -> dict:
    """The report for a Slack poster: summary (parent message), detail (thread), stats.

    Args:
        report: The report build_report made.
        model: The backlog model, for the priority order.
        report_url: Where the full report can be read, for the cut-off line.
        group_replies: The most thread replies one group may take.

    Returns:
        ``{"summary": str, "detail": [{"group", "text"}], "stats": {name: int}}``.
    """
    model = model or BacklogModel()
    buckets = slack_buckets(report["findings"])
    return {
        "summary": slack_summary(report, model, buckets),
        "detail": slack_detail(report, model, report_url, group_replies),
        "stats": slack_stats(report, buckets),
    }
