#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         scripts/check_member_pins.py
#  Purpose:      Check every suite member's backing-service image pins against
#                the current versions.yaml stack, tag and digest.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Hold every suite member's backing-service pins to versions.yaml.

    python3 scripts/check_member_pins.py                    # every member
    python3 scripts/check_member_pins.py dfe-loader scalo-rs
    python3 scripts/check_member_pins.py --repos ~/projects --no-fetch
    python3 scripts/check_member_pins.py --api              # no checkouts needed
    python3 scripts/dfe-suite pins dfe-loader               # the same scan

versions.yaml's current stack is the source of truth. Its image map is the
annotations above the pins, read by scripts/stack_images.py, so a service joins
this check by being annotated and digested there and nowhere else.

Each member is read at its main, never from a working tree: the question is
what the member's main ships, not what a checkout holds. A member checked out
here is read at origin/main after a `git fetch`, found the way dfe-suite finds
one (`<NAME>_DIR`, then `$HYPERI_PROJECTS_ROOT`, then the usual roots) or under
--repos. Any other member, or every member with --api, is read through
`gh api` from the repo its suite.yaml node names: main's commit, its tree, then
each candidate file's contents.

Three pin shapes are read, in .rs, .py, .sh, .yml and .yaml files:

    an annotated bare tag, and its <STEM>_DIGEST beside it
        // renovate: datasource=docker depName=apache/kafka
        const KAFKA_TAG: &str = "4.3.1";
        const KAFKA_DIGEST: &str = "sha256:...";
    a compose image, digested or not
        image: clickhouse/clickhouse-server:26.3.32.14@sha256:...
    a shell default
        IMAGE="${OVERRIDE:-docker.redpanda.com/redpandadata/redpanda:v26.2.2@sha256:...}"

Each pin of an image the stack carries is compared on its tag and its digest; a
pin with no digest is drift, because its tag can be rebuilt under it. A pin of
an image the stack REJECTS (a `rejects=` reference on the annotation, such as
apache/kafka-native for apache/kafka) fails, naming the image to use. A managed
Kafka version spelling (MSK's `3.9.x.kraft`) is not an image tag and is skipped.

suite.yaml's `pin_waivers:` excuses one member's file and image, with a reason
and no version. A waiver that excuses nothing fails, so none outlives its pin.
The dfe-infra node is this repo, held by check_versions_drift.py, and a node
classified `fork` follows its upstream's merge rules, so neither is scanned.

Exit 0 every pin matches; 1 an edit is needed (drift, a rejected image or a
stale waiver); 2 nothing drifted but a member could not be read.
"""

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import quote

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import suite_graph  # noqa: E402
from dfe_suite.proc import FleetError  # noqa: E402
from dfe_suite.repos import find_repo  # noqa: E402
from stack_images import StackImage, normalise_ref, stack_images  # noqa: E402

VERSIONS_FILE = REPO_ROOT / "versions.yaml"
MAIN = "origin/main"
SELF = "dfe-infra"
EXEMPT_CLASSIFICATIONS = ("fork",)

EXIT_CLEAN = 0
EXIT_DRIFT = 1
EXIT_UNREAD = 2

# Files that can carry one of the three shapes; lockfiles never do.
SUFFIXES = (".rs", ".py", ".sh", ".yml", ".yaml")
PATHSPECS = (*(f"*{suffix}" for suffix in SUFFIXES), ":(exclude)*lock*")
CANDIDATE = r"sha256:[0-9a-f]{64}|renovate:|image:"
_CANDIDATE = re.compile(CANDIDATE)
# Concurrent contents reads per member through gh api, well under GitHub's secondary rate limit.
API_WORKERS = 8

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REF = r"[a-z0-9][a-z0-9._/-]*"
_TAG = r"[A-Za-z0-9_][A-Za-z0-9._-]*"
_ANNOTATION = re.compile(
    rf"^\s*(?:#|//+)\s*(?:renovate:.*?depName=(?P<a>\S+)|image:\s*(?P<b>{_REF})(?:\s|$))"
)
_COMMENT = re.compile(r"^\s*(?:#|//)")
# `const NAME: &str = "v"`, `NAME = "v"`, `NAME="v"`, `export NAME="v"`, `name: "v"`.
_ASSIGN = re.compile(
    r"""^\s*(?:pub(?:\([^)]*\))?\s+)?(?:(?:const|static|let|export)\s+)?"""
    r"""(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)\s*(?::\s*[^=\n]*?)?\s*[=:]\s*["'](?P<value>[^"'\s]+)["']"""
)
_COMPOSE = re.compile(
    rf"""^\s*-?\s*image:\s*["']?(?P<ref>{_REF}):(?P<tag>{_TAG})(?:@(?P<digest>sha256:[0-9a-f]{{64}}))?["']?\s*(?:#.*)?$"""
)
_COMPOSE_DEFAULT = re.compile(
    rf"""^\s*-?\s*image:\s*["']?(?P<ref>{_REF}):\$\{{[A-Za-z0-9_]+:-(?P<tag>{_TAG})"""
    r"""(?:@(?P<digest>sha256:[0-9a-f]{64}))?\}"""
)
_SHELL_DEFAULT = re.compile(
    rf"""\$\{{[A-Za-z0-9_]+:-(?P<ref>{_REF}):(?P<tag>{_TAG})(?:@(?P<digest>sha256:[0-9a-f]{{64}}))?\}}"""
)
# MSK's version spelling, which names a managed broker rather than an image tag.
_MANAGED_KAFKA = re.compile(r"^\d+\.\d+\.x(?:\.kraft)?$|\.kraft$")


@dataclass(frozen=True, slots=True)
class Pin:
    """One image pin found in a member file. `line` is the tag's line;
    `digest_line` is 0 when the pin carries no digest."""

    path: str
    line: int
    ref: str
    tag: str
    digest: str = ""
    digest_line: int = 0
    shape: str = ""


@dataclass(frozen=True, slots=True)
class Finding:
    """One edit a member needs. `field` is tag, digest or image."""

    member: str
    path: str
    line: int
    image: str
    field: str
    old: str
    new: str
    waived_by: str = ""

    def render(self) -> str:
        where = f"{self.path}:{self.line}"
        waived = f"  (waived: {self.waived_by})" if self.waived_by else ""
        return f"  {where}  {self.image}  {self.field}: {self.old} -> {self.new}{waived}"


@dataclass(slots=True)
class MemberReport:
    """What one member's scan found. `unread` is why it could not be read."""

    member: str
    sha: str = ""
    source: str = ""
    pins: int = 0
    findings: list[Finding] = field(default_factory=list)
    unread: str = ""
    exempt: str = ""

    @property
    def edits(self) -> list[Finding]:
        return [f for f in self.findings if not f.waived_by]


# --- extraction ------------------------------------------------------------------


def _split(value: str) -> tuple[str, str]:
    """(tag, digest) of a bare tag or a `tag@sha256:` value."""
    tag, _, digest = value.partition("@")
    return tag, digest if _DIGEST.fullmatch(digest) else ""


def _digest_for(lines: list[str], name: str) -> tuple[str, int]:
    """The `<stem>_DIGEST` beside a `<stem>_TAG` (or `_VERSION`), with its
    1-based line: the value may sit on the line after a wrapped `=`."""
    stem = re.sub(r"_(?:TAG|VERSION)$", "", name, flags=re.IGNORECASE)
    wanted = re.compile(
        rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:(?:const|static|let|export)\s+)?{re.escape(stem)}_DIGEST\b"
        r"\s*(?::\s*[^=\n]*?)?\s*=\s*(?P<rest>.*)$",
        re.IGNORECASE,
    )
    for index, line in enumerate(lines):
        m = wanted.match(line)
        if not m:
            continue
        for offset, text in enumerate((m["rest"], lines[index + 1] if index + 1 < len(lines) else "")):
            hit = _DIGEST.search(text)
            if hit:
                return hit[0], index + 1 + offset
        return "", 0
    return "", 0


def _annotated(path: str, lines: list[str], index: int, ref: str) -> Pin | None:
    """The pin an annotation at `index` names: the first non-comment line
    after it, which is a compose image or a quoted assignment."""
    for at in range(index + 1, len(lines)):
        line = lines[at]
        if _COMMENT.match(line):
            continue
        compose = _COMPOSE.match(line)
        if compose:
            return Pin(path, at + 1, compose["ref"], compose["tag"], compose["digest"] or "",
                       at + 1 if compose["digest"] else 0, "compose")
        assign = _ASSIGN.match(line)
        if not assign:
            return None
        tag, digest = _split(assign["value"])
        if not re.fullmatch(_TAG, tag):
            return None
        if digest:
            return Pin(path, at + 1, ref, tag, digest, at + 1, "annotated")
        digest, digest_line = _digest_for(lines, assign["name"])
        return Pin(path, at + 1, ref, tag, digest, digest_line, "annotated")
    return None


def extract_pins(path: str, text: str) -> list[Pin]:
    """Every pin of the three shapes in one file, each line read once."""
    lines = text.splitlines()
    pins: list[Pin] = []
    claimed: set[int] = set()
    for index, line in enumerate(lines):
        annotation = _ANNOTATION.match(line)
        if annotation:
            pin = _annotated(path, lines, index, annotation["a"] or annotation["b"])
            if pin is not None and pin.line not in claimed:
                pins.append(pin)
                claimed.add(pin.line)
    for index, line in enumerate(lines, start=1):
        if index in claimed:
            continue
        m = _COMPOSE.match(line) or _COMPOSE_DEFAULT.match(line)
        shape = "compose"
        if m is None:
            m = _SHELL_DEFAULT.search(line)
            shape = "shell"
        if m is not None:
            digest = m["digest"] or ""
            pins.append(Pin(path, index, m["ref"], m["tag"], digest, index if digest else 0, shape))
    return pins


# --- comparison ------------------------------------------------------------------


def compare(member: str, pins: list[Pin], images: list[StackImage]) -> list[Finding]:
    """The edits that bring each pin of a stack image onto the stack."""
    by_ref = {ref: image for image in images for ref in image.refs}
    rejected = {normalise_ref(ref): image for image in images for ref in image.rejects}
    findings: list[Finding] = []
    for pin in pins:
        ref = normalise_ref(pin.ref)
        if ref in rejected:
            image = rejected[ref]
            findings.append(Finding(
                member, pin.path, pin.line, pin.ref, "image",
                f"{pin.ref}:{pin.tag}", f"{image.ref}:{image.tag}@{image.digest}",
            ))
            continue
        image = by_ref.get(ref)
        if image is None or _MANAGED_KAFKA.search(pin.tag):
            continue
        if pin.tag != image.tag:
            findings.append(Finding(member, pin.path, pin.line, pin.ref, "tag", pin.tag, image.tag))
        if pin.digest != image.digest:
            findings.append(Finding(
                member, pin.path, pin.digest_line or pin.line, pin.ref, "digest",
                pin.digest or "(none)", image.digest,
            ))
    return findings


# --- waivers ---------------------------------------------------------------------


def waivers(graph: dict) -> list[dict]:
    return [w for w in graph.get("pin_waivers") or [] if isinstance(w, dict)]


def _waiver_name(waiver: dict) -> str:
    return f"{waiver.get('member')} {waiver.get('file')} {waiver.get('image')}"


def apply_waivers(reports: list[MemberReport], graph: dict) -> list[Finding]:
    """Mark each finding a waiver covers, and return a finding per waiver that
    covered nothing in a member this run read."""
    used: set[int] = set()
    entries = waivers(graph)
    for report in reports:
        for i, finding in enumerate(report.findings):
            for n, waiver in enumerate(entries):
                if (waiver.get("member") == finding.member and waiver.get("file") == finding.path
                        and normalise_ref(str(waiver.get("image", ""))) == normalise_ref(finding.image)):
                    report.findings[i] = replace(finding, waived_by=str(waiver.get("reason")))
                    used.add(n)
                    break
    read = {r.member for r in reports if not r.unread and not r.exempt}
    return [
        Finding(str(w.get("member")), str(w.get("file")), 0, str(w.get("image")), "waiver",
                "excuses nothing", "remove it from suite.yaml pin_waivers")
        for n, w in enumerate(entries) if n not in used and w.get("member") in read
    ]


# --- reading a member at origin/main ---------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def read_member(repo: Path, *, fetch: bool) -> tuple[str, dict[str, str]]:
    """(short sha of origin/main, path -> text of every candidate file there).

    Raises:
        FleetError: When the fetch fails or origin/main does not resolve.
    """
    if fetch:
        fetched = _git(repo, "fetch", "--quiet", "origin", "main")
        if fetched.returncode != 0:
            raise FleetError(f"git fetch origin main failed: {fetched.stderr.strip()}")
    sha = _git(repo, "rev-parse", "--short=9", MAIN)
    if sha.returncode != 0:
        raise FleetError(f"{MAIN} does not resolve in {repo}")
    listed = _git(repo, "grep", "-l", "-I", "-E", CANDIDATE, MAIN, "--", *PATHSPECS)
    if listed.returncode not in (0, 1):
        raise FleetError(f"git grep failed: {listed.stderr.strip()}")
    files: dict[str, str] = {}
    for entry in listed.stdout.splitlines():
        path = entry.removeprefix(f"{MAIN}:")
        shown = _git(repo, "show", f"{MAIN}:{path}")
        if shown.returncode != 0:
            raise FleetError(f"cannot read {MAIN}:{path}: {shown.stderr.strip()}")
        files[path] = shown.stdout
    return sha.stdout.strip(), files


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", "api", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def _candidate(path: str) -> bool:
    return path.endswith(SUFFIXES) and "lock" not in path.rsplit("/", 1)[-1]


def read_member_api(slug: str) -> tuple[str, dict[str, str]]:
    """(short sha of main, path -> text of every candidate file there), read
    through `gh api` for a member with no checkout here.

    Raises:
        FleetError: When main, its tree or a file cannot be read, or the tree
            listing is truncated and would leave files unread.
    """
    head = _gh(f"repos/{slug}/commits/main", "--jq", ".sha")
    sha = head.stdout.strip()
    if head.returncode != 0 or not sha:
        raise FleetError(f"gh api cannot read {slug} main: {head.stderr.strip() or 'no sha'}")
    listed = _gh(f"repos/{slug}/git/trees/{sha}?recursive=1")
    try:
        tree = json.loads(listed.stdout) if listed.returncode == 0 else None
    except json.JSONDecodeError:
        tree = None
    if not isinstance(tree, dict):
        raise FleetError(f"gh api cannot list {slug} at {sha}: {listed.stderr.strip()}")
    if tree.get("truncated"):
        raise FleetError(f"gh api listed {slug} truncated, so some files would go unread")
    paths = sorted(
        str(entry.get("path") or "") for entry in tree.get("tree") or []
        if entry.get("type") == "blob" and _candidate(str(entry.get("path") or ""))
    )
    with ThreadPoolExecutor(max_workers=API_WORKERS) as pool:
        bodies = pool.map(
            lambda path: _gh(f"repos/{slug}/contents/{quote(path)}?ref={sha}", "-H", "Accept: application/vnd.github.raw"),
            paths,
        )
        files: dict[str, str] = {}
        for path, body in zip(paths, bodies, strict=True):
            if body.returncode != 0:
                raise FleetError(f"gh api cannot read {slug}:{path}: {body.stderr.strip()}")
            if _CANDIDATE.search(body.stdout):
                files[path] = body.stdout
    return sha[:9], files


def read_any(
    member: str, node: dict, *, repos: str | None, fetch: bool, api: bool
) -> tuple[str, str, dict[str, str]]:
    """(source, short sha, files) of a member's main: its checkout here, else
    `gh api` against the repo suite.yaml names.

    Raises:
        FleetError: When neither route can read it, naming both reasons.
    """
    local_miss = "--api"
    if not api:
        try:
            sha, files = read_member(member_repo(member, repos), fetch=fetch)
            return "checkout", sha, files
        except FleetError as exc:
            local_miss = str(exc)
    slug = node.get("repo")
    if not slug:
        raise FleetError(f"{local_miss}; and suite.yaml names no repo to read through gh api")
    try:
        sha, files = read_member_api(str(slug))
    except FleetError as exc:
        raise FleetError(f"{local_miss}; and {exc}") from exc
    return "gh api", sha, files


def member_repo(member: str, repos: str | None) -> Path:
    """Where a member is checked out, by --repos or dfe-suite's own lookup."""
    if repos:
        path = Path(repos).expanduser() / member
        if not (path / ".git").exists():
            raise FleetError(f"{path} is not a git checkout")
        return path
    return find_repo(member)


def exemption(member: str, node: dict) -> str:
    if member == SELF:
        return "this repo: check_versions_drift.py holds its pins"
    if node.get("classification") in EXEMPT_CLASSIFICATIONS:
        return f"a {node['classification']}: it follows its upstream's merge rules"
    return ""


def scan_member(
    member: str, node: dict, images: list[StackImage], *, repos: str | None, fetch: bool, api: bool = False
) -> MemberReport:
    report = MemberReport(member, exempt=exemption(member, node))
    if report.exempt:
        return report
    try:
        report.source, report.sha, files = read_any(member, node, repos=repos, fetch=fetch, api=api)
    except FleetError as exc:
        report.unread = str(exc)
        return report
    for path in sorted(files):
        pins = extract_pins(path, files[path])
        report.pins += len(pins)
        report.findings += compare(member, pins, images)
    return report


def scan(
    members: list[str] | None = None,
    *,
    repos: str | None = None,
    fetch: bool = True,
    api: bool = False,
    graph: dict | None = None,
    versions_text: str | None = None,
) -> tuple[str, list[StackImage], list[MemberReport], list[Finding]]:
    """(stack, its images, one report per member, stale waivers).

    Raises:
        FleetError: When a named member is not a suite node.
    """
    graph = graph if graph is not None else suite_graph.load()
    nodes = graph.get("nodes") or {}
    unknown = sorted(set(members or ()) - set(nodes))
    if unknown:
        raise FleetError(f"not a suite member: {', '.join(unknown)} (suite.yaml nodes)")
    text = versions_text if versions_text is not None else VERSIONS_FILE.read_text(encoding="utf-8")
    stack, images = stack_images(text)
    reports = [
        scan_member(name, nodes[name], images, repos=repos, fetch=fetch, api=api)
        for name in (members or sorted(nodes))
    ]
    return stack, images, reports, apply_waivers(reports, graph)


def render(stack: str, images: list[StackImage], reports: list[MemberReport], stale: list[Finding]) -> list[str]:
    out = [f"member-pins: stack {stack}, {len(images)} digested image(s) in versions.yaml"]
    for report in reports:
        where = f"{report.member} @ main {report.sha} via {report.source}"
        if report.exempt:
            out.append(f"{report.member}: exempt -- {report.exempt}")
        elif report.unread:
            out.append(f"{report.member}: NOT READ -- {report.unread}")
        elif not report.findings:
            out.append(f"{where}: ok, {report.pins} pin(s) checked")
        else:
            out.append(f"{where}: {len(report.edits)} edit(s), {report.pins} pin(s) checked")
            out.extend(finding.render() for finding in report.findings)
    if stale:
        out.append("suite.yaml pin_waivers:")
        out.extend(f"  {_waiver_name({'member': f.member, 'file': f.path, 'image': f.image})}: "
                   f"{f.old} -- {f.new}" for f in stale)
    return out


def exit_code(reports: list[MemberReport], stale: list[Finding]) -> int:
    if stale or any(report.edits for report in reports):
        return EXIT_DRIFT
    if any(report.unread for report in reports):
        return EXIT_UNREAD
    return EXIT_CLEAN


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_member_pins.py",
        description="Check every suite member's backing-service pins against versions.yaml.",
    )
    parser.add_argument("member", nargs="*", help="members to scan (default: every suite.yaml node)")
    parser.add_argument("--repos", help="directory holding the member checkouts")
    parser.add_argument("--no-fetch", action="store_true", help="read origin/main as it stands, without a fetch")
    parser.add_argument(
        "--api", action="store_true", help="read every member's main through gh api, even one checked out here"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        stack, images, reports, stale = scan(
            args.member or None, repos=args.repos, fetch=not args.no_fetch, api=args.api
        )
    except FleetError as exc:
        print(f"member-pins: {exc}", file=sys.stderr)
        return EXIT_DRIFT
    for line in render(stack, images, reports, stale):
        print(line, flush=True)
    code = exit_code(reports, stale)
    edits = sum(len(report.edits) for report in reports) + len(stale)
    unread = sum(1 for report in reports if report.unread)
    print(f"member-pins: {edits} edit(s), {unread} member(s) not read -- exit {code}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
