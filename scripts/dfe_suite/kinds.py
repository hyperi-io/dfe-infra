#  Project:      dfe-infra
#  File:         scripts/dfe_suite/kinds.py
#  Purpose:      One check per suite edge kind: what moved, and what has to be done.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What checking one edge of the suite graph actually involves.

The graph in dfe-infra's ``suite.yaml`` labels every edge with a KIND, and the
kind is what says how to check it. Some kinds are mechanical -- a version range
either admits the new version or it does not, two blob hashes are either equal
or they are not -- and those are answered here for real. The rest are not, and
those return an honest result naming what a person has to do rather than a
guess dressed up as an answer.

Every check in this module is READ ONLY. It reports; the acting walk is a
separate stage.

The matchers are deliberately small. They cover the comparators the suite
actually uses -- Cargo's ``>=a, <b`` / caret / tilde / wildcard / exact, and
PEP 440's ``>=``, ``<``, ``==``, ``!=``, ``~=`` -- and answer ``None`` for
anything else instead of pretending. A version carrying a PRE-RELEASE is
``None`` too unless the requirement names one: both ecosystems exclude
pre-releases from a plain range, and answering yes there would move a consumer
onto a release candidate.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from dfe_suite.proc import FleetError, run
from dfe_suite.repos import find_repo

# What the caller does next. A short token, so the acting walk can branch on it
# without reading prose; the human-readable half is always in the detail.
ACTION_REBUILD_RUST = "rebuild_rust"
ACTION_REBUILD_PYTHON = "rebuild_python"
ACTION_WIDEN_RANGE = "widen-range"
ACTION_RUN_TESTS = "run-tests"
ACTION_REGENERATE = "regenerate-and-diff"
ACTION_CONTRACT_TEST = "run-contract-test"
ACTION_REVENDOR = "re-vendor"
ACTION_HUMAN_READ = "human-read"
ACTION_BUMP_APP = "bump-app"
ACTION_MOVE_PIN = "move-pin"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The verdict on one edge.

    Attributes:
        moved: True when the consumer has something to change, False when it
            provably has not, None when the answer needs work this check does
            not do (a build, a regeneration, or a person reading code).
        detail: One line naming the evidence the verdict rests on.
        action: The action token, or None when the edge needs nothing.
    """

    moved: bool | None
    detail: str
    action: str | None


class Evidence(NamedTuple):
    """One ``<repo>/<path>[:<line>[-<end>]]`` reference off an edge."""

    repo: str
    path: str
    start: int | None
    end: int | None

    def describe(self) -> str:
        """The reference as it is written on the edge."""
        if self.start is None:
            return f"{self.repo}/{self.path}"
        if self.end is None or self.end == self.start:
            return f"{self.repo}/{self.path}:{self.start}"
        return f"{self.repo}/{self.path}:{self.start}-{self.end}"


_EVIDENCE = re.compile(
    r"^(?P<repo>[^/\s]+)/(?P<path>[^\s:]+)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$"
)


def parse_evidence(text: str) -> Evidence | None:
    """The one reference an edge field holds.

    A field is exactly ONE reference. An edge with two sides names the
    consumer's copy in ``evidence`` and the producer's in ``source``, so a
    second reference never shares a field with the first.

    Args:
        text: The raw ``evidence`` or ``source`` string.

    Returns:
        The parsed reference, or None when the text is not one.
    """
    match = _EVIDENCE.match((text or "").strip())
    if match is None:
        return None
    start = match.group("start")
    end = match.group("end")
    return Evidence(
        repo=match.group("repo"),
        path=match.group("path"),
        start=int(start) if start else None,
        end=int(end) if end else None,
    )


def locate(evidence: Evidence, consumer_repo: Path) -> Path:
    """The file on disk a reference points at.

    The consumer's own checkout is the one the caller handed in; anything
    naming a different repo is resolved the usual way, so a two-sided edge can
    be compared without the caller knowing where the other side lives.

    Raises:
        FleetError: If the file is not there.
    """
    root = (
        consumer_repo
        if evidence.repo == consumer_repo.name
        else find_repo(evidence.repo)
    )
    path = root / evidence.path
    if not path.is_file():
        raise FleetError(f"{evidence.describe()} does not exist at {path}")
    return path


def cited_lines(evidence: Evidence, consumer_repo: Path) -> str:
    """The text at a reference: the cited line range, or the whole file.

    Raises:
        FleetError: If the file is not there, or the line range is past its end.
    """
    path = locate(evidence, consumer_repo)
    text = path.read_text(encoding="utf-8", errors="replace")
    if evidence.start is None:
        return text
    lines = text.splitlines()
    if evidence.start > len(lines):
        raise FleetError(
            f"{evidence.describe()} cites line {evidence.start} but the file has "
            f"{len(lines)}"
        )
    end = evidence.end or evidence.start
    return "\n".join(lines[evidence.start - 1 : end])


# ---------------------------------------------------------------------------
# Version-range matchers
# ---------------------------------------------------------------------------


_POST_OR_DEV = re.compile(r"\.(?:post|dev)\d+$")


def _release(version: str) -> tuple[int, ...] | None:
    """The numeric release segment of a version, or None if it is not one.

    A ``.postN`` or ``.devN`` suffix is stripped and the release read as
    itself, because both order against the release they hang off. Anything
    else non-numeric -- an epoch (``1!2.0``), a PEP 440 pre-release written
    with no separator (``1.0rc1``), a word -- answers None rather than a guess.
    """
    text = version.strip().lstrip("vV")
    text = re.split(r"[-+]", text, maxsplit=1)[0]
    while True:
        stripped = _POST_OR_DEV.sub("", text)
        if stripped == text:
            break
        text = stripped
    if not text:
        return None
    parts: list[int] = []
    for chunk in text.split("."):
        if not chunk.isdigit():
            return None
        parts.append(int(chunk))
    return tuple(parts)


def _has_prerelease(version: str) -> bool:
    """Whether a version carries a pre-release segment, Cargo's spelling."""
    text = version.strip().lstrip("vV")
    return "-" in re.split(r"\+", text, maxsplit=1)[0]


def _pad(parts: tuple[int, ...], width: int) -> tuple[int, ...]:
    """The release segment zero-extended to ``width`` components."""
    return parts + (0,) * (width - len(parts))


def _compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """-1, 0 or 1, comparing two release segments of any length."""
    width = max(len(left), len(right))
    a, b = _pad(left, width), _pad(right, width)
    return (a > b) - (a < b)


def _caret_upper(parts: tuple[int, ...]) -> tuple[int, ...]:
    """The exclusive upper bound of a caret range, Cargo's rules.

    The leftmost non-zero component is what may not change, so ``^1.2.3``
    stops at 2.0.0 while ``^0.2.3`` stops at 0.3.0.
    """
    index = next((i for i, part in enumerate(parts) if part), len(parts) - 1)
    return _pad((*parts[:index], parts[index] + 1), 3)


def _tilde_upper(parts: tuple[int, ...]) -> tuple[int, ...]:
    """The exclusive upper bound of a Cargo tilde range."""
    if len(parts) >= 2:
        return (parts[0], parts[1] + 1, 0)
    return (parts[0] + 1, 0, 0)


def _wildcard_bounds(spec: str) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """The half-open bounds of a ``1.2.*`` style range, or None."""
    head = spec[: spec.index("*")].rstrip(".")
    if not head:
        return ((0,), ())  # a bare `*` admits everything; empty upper means none
    parts = _release(head)
    if parts is None:
        return None
    return (parts, _pad((*parts[:-1], parts[-1] + 1), 3))


def _admits_one(comparator: str, version: tuple[int, ...]) -> bool | None:
    """Whether one comparator admits a version, or None if it is not understood."""
    spec = comparator.strip()
    if not spec:
        return None
    if "*" in spec:
        # `==1.2.*` and `1.2.*` are the same half-open window; `!=1.2.*` is
        # that window inverted, which is the whole of what PEP 440 adds here.
        excluding = spec.startswith("!=")
        bounds = _wildcard_bounds(spec.lstrip("!=~^"))
        if bounds is None:
            return None
        lower, upper = bounds
        if not upper:
            inside = True
        else:
            inside = _compare(version, lower) >= 0 and _compare(version, upper) < 0
        return not inside if excluding else inside

    for operator in (">=", "<=", "===", "==", "!=", "~=", ">", "<", "^", "~", "="):
        if spec.startswith(operator):
            parts = _release(spec[len(operator) :])
            if parts is None:
                return None
            order = _compare(version, parts)
            if operator == ">=":
                return order >= 0
            if operator == "<=":
                return order <= 0
            if operator == ">":
                return order > 0
            if operator == "<":
                return order < 0
            if operator in {"==", "===", "="}:
                return order == 0
            if operator == "!=":
                return order != 0
            if operator == "~=":
                # PEP 440 compatible release: a floor, with the last-but-one
                # component fixed. One component alone is not a legal `~=`.
                if len(parts) < 2:
                    return None
                upper = (*parts[:-2], parts[-2] + 1)
                return order >= 0 and _compare(version, upper) < 0
            if operator == "^":
                return order >= 0 and _compare(version, _caret_upper(parts)) < 0
            if operator == "~":
                return order >= 0 and _compare(version, _tilde_upper(parts)) < 0

    # A bare version. Cargo reads it as a caret range; PEP 440 has no such form
    # and the python matcher refuses it before reaching here.
    parts = _release(spec)
    if parts is None:
        return None
    return _compare(version, parts) >= 0 and _compare(version, _caret_upper(parts)) < 0


def _admits_all(spec: str, version: str, *, bare_is_caret: bool) -> bool | None:
    """Whether every comma-separated comparator in ``spec`` admits ``version``."""
    target = _release(version)
    if target is None:
        return None
    comparators = [part for part in spec.split(",") if part.strip()]
    if not comparators:
        return None
    # A plain range admits no pre-release in either ecosystem; a range written
    # to admit one carries a pre-release itself, as `<3.0.0-0` does.
    if _has_prerelease(version) and not any(_has_prerelease(c) for c in comparators):
        return None
    verdict = True
    for comparator in comparators:
        text = comparator.strip()
        if not bare_is_caret and text[:1].isdigit():
            return None
        answer = _admits_one(text, target)
        if answer is None:
            return None
        verdict = verdict and answer
    return verdict


def semver_admits(spec: str, version: str) -> bool | None:
    """Whether a Cargo version requirement admits ``version``.

    Args:
        spec: The requirement, e.g. ``">=2.10.14, <3"``, ``"^1.2"``, ``"1.2.3"``.
        version: The release to test.

    Returns:
        True or False, or None when the requirement is not one this understands.
    """
    return _admits_all(spec, version, bare_is_caret=True)


def pep440_admits(spec: str, version: str) -> bool | None:
    """Whether a PEP 440 specifier set admits ``version``.

    Args:
        spec: The specifier set, e.g. ``">=2.29.7"``, ``"~=1.2"``, ``"==1.2.*"``.
        version: The release to test.

    Returns:
        True or False, or None when the specifier is not one this understands.
        A bare version with no operator is not a PEP 440 specifier, so it is
        None rather than a guess.
    """
    return _admits_all(spec, version, bare_is_caret=False)


_CARGO_TABLE = re.compile(r'version\s*=\s*"([^"]*)"')
_CARGO_BARE = re.compile(r'^\s*[A-Za-z0-9_-]+\s*=\s*"([^"]*)"')
_PY_STRING = re.compile(r'"([^"]*)"')
# What may follow the package name and still leave the line a declaration OF
# that package: extras, a space before the specifier, or a comparator.
_PY_AFTER_NAME = ("", "[", " ", "<", ">", "=", "!", "~")


def cargo_range(text: str, package: str) -> str | None:
    """The version requirement the cited line declares FOR ``package``.

    A line declaring something else has a range on it too, so the name is
    checked first: without that, the check reads a neighbouring dependency's
    range and reports on the wrong crate.

    Args:
        text: The cited line.
        package: The crate the producer publishes.

    Returns:
        The requirement, or None when the line does not declare that crate or
        declares it without a version (a workspace, path or git dependency).
    """
    if text.lstrip().startswith("#"):
        return None
    if not re.search(rf"^\s*{re.escape(package)}\s*(?:=|\.workspace\b)", text):
        return None
    table = _CARGO_TABLE.search(text)
    if table:
        return table.group(1)
    bare = _CARGO_BARE.search(text)
    return bare.group(1) if bare else None


def python_range(text: str, package: str) -> str | None:
    """The specifier set the cited line declares FOR ``package``.

    Args:
        text: The cited line.
        package: The distribution the producer publishes.

    Returns:
        The specifier set, or None when the line declares another
        distribution, or declares this one with no specifier at all.
    """
    if text.lstrip().startswith("#"):
        return None
    for candidate in _PY_STRING.findall(text):
        requirement = candidate.strip()
        if not requirement.startswith(package):
            continue
        rest = requirement[len(package) :]
        if rest[:1] not in _PY_AFTER_NAME:
            continue
        if rest.startswith("["):
            close = rest.find("]")
            if close < 0:
                continue
            rest = rest[close + 1 :]
        spec = rest.split(";")[0].strip()
        return spec or None
    return None


# ---------------------------------------------------------------------------
# One check per kind
# ---------------------------------------------------------------------------


def _first(edge: dict) -> Evidence:
    """The edge's evidence reference.

    Raises:
        FleetError: If the edge cites nothing this can parse.
    """
    ref = parse_evidence(str(edge.get("evidence") or ""))
    if ref is None:
        raise FleetError(f"edge {_name(edge)} cites no usable evidence")
    return ref


def _name(edge: dict) -> str:
    """``producer -> consumer`` for a message."""
    return f"{edge.get('from', '?')} -> {edge.get('to', '?')}"


def _with_note(edge: dict, detail: str) -> str:
    """The detail plus the edge's own note, where it carries one."""
    note = str(edge.get("note") or "").strip()
    return f"{detail} Note: {note}" if note else detail


def _range_result(
    edge: dict,
    ref: Evidence,
    spec: str | None,
    verdict: bool | None,
    producer_version: str,
    *,
    package: str,
    rebuild_action: str,
) -> CheckResult:
    """The shared verdict shape for the two range-declaring kinds."""
    if spec is None:
        return CheckResult(
            None,
            _with_note(
                edge,
                f"{ref.describe()} declares no version range for {package} -- "
                f"the line names something else, or names it with no range.",
            ),
            ACTION_HUMAN_READ,
        )
    if verdict is None:
        return CheckResult(
            None,
            _with_note(
                edge, f"{ref.describe()} declares '{spec}', which this cannot parse."
            ),
            ACTION_HUMAN_READ,
        )
    if verdict:
        return CheckResult(
            True,
            _with_note(
                edge,
                f"{ref.describe()} declares '{spec}', which admits {producer_version}.",
            ),
            rebuild_action,
        )
    return CheckResult(
        True,
        _with_note(
            edge,
            f"{ref.describe()} declares '{spec}', which does NOT admit "
            f"{producer_version} -- widen it there first.",
        ),
        ACTION_WIDEN_RANGE,
    )


def _no_package(edge: dict, ref: Evidence) -> CheckResult:
    """The verdict when the graph never said what the producer publishes."""
    return CheckResult(
        None,
        _with_note(
            edge,
            f"{edge.get('from', 'the producer')} declares no `package` in the "
            f"graph, so the range at {ref.describe()} cannot be attributed to "
            f"it -- add `package:` to that node.",
        ),
        ACTION_HUMAN_READ,
    )


def check_cargo_dep(
    edge: dict,
    *,
    producer_version: str,
    consumer_repo: Path,
    package: str | None = None,
    dry_run: bool = True,
) -> CheckResult:
    """Does the cited Cargo range admit the new version?

    Admitting means the lock can move with ``cargo update`` and the consumer
    rebuilds; not admitting means the range is widened at the cited line first.
    """
    ref = _first(edge)
    if not package:
        return _no_package(edge, ref)
    spec = cargo_range(cited_lines(ref, consumer_repo), package)
    verdict = semver_admits(spec, producer_version) if spec else None
    return _range_result(
        edge,
        ref,
        spec,
        verdict,
        producer_version,
        package=package,
        rebuild_action=ACTION_REBUILD_RUST,
    )


def check_python_dep(
    edge: dict,
    *,
    producer_version: str,
    consumer_repo: Path,
    package: str | None = None,
    dry_run: bool = True,
) -> CheckResult:
    """Does the cited PEP 440 specifier admit the new version?"""
    ref = _first(edge)
    if not package:
        return _no_package(edge, ref)
    spec = python_range(cited_lines(ref, consumer_repo), package)
    verdict = pep440_admits(spec, producer_version) if spec else None
    return _range_result(
        edge,
        ref,
        spec,
        verdict,
        producer_version,
        package=package,
        rebuild_action=ACTION_REBUILD_PYTHON,
    )


def check_python_dep_undeclared(
    edge: dict,
    *,
    producer_version: str,
    consumer_repo: Path,
    package: str | None = None,
    dry_run: bool = True,
) -> CheckResult:
    """No range exists, so only running the named import sites answers this."""
    ref = _first(edge)
    return CheckResult(
        None,
        _with_note(
            edge,
            f"No declared range. Install {producer_version} beside the consumer "
            f"and run the tests covering the import site at {ref.describe()}.",
        ),
        ACTION_RUN_TESTS,
    )


_SCHEMA_VERSION = re.compile(r"(?im)^\W*schema version:\s*(\S+)\s*$")
_REGENERATE = re.compile(r"(?im)^\W*regenerate with:\s*(.+?)\s*$")


def check_generated_file(
    edge: dict,
    *,
    producer_version: str,
    consumer_repo: Path,
    package: str | None = None,
    dry_run: bool = True,
) -> CheckResult:
    """Read the generated header, then hand back the regenerate-and-diff step.

    The header states the schema version the file was emitted at. What the new
    producer WOULD emit is inside the producer's own generator, which is a
    build away, so the honest answer is the command the file itself names.
    """
    ref = _first(edge)
    header = cited_lines(ref, consumer_repo)
    schema = _SCHEMA_VERSION.search(header)
    command = _REGENERATE.search(header)
    stamped = f"schema version {schema.group(1)}" if schema else "no schema version"
    how = (
        f"`{command.group(1).strip('` ')}`"
        if command
        else "whatever its own header names"
    )
    return CheckResult(
        None,
        _with_note(
            edge,
            f"{ref.describe()} carries {stamped}. Regenerate with {how} on "
            f"{producer_version} and commit the diff; an empty diff means "
            f"nothing moved.",
        ),
        ACTION_REGENERATE,
    )


def check_contract_guard(
    edge: dict,
    *,
    producer_version: str,
    consumer_repo: Path,
    package: str | None = None,
    dry_run: bool = True,
) -> CheckResult:
    """Hand back the consumer's own contract test, which names the divergence."""
    ref = _first(edge)
    if (consumer_repo / "Cargo.toml").is_file():
        command = "cargo nextest run --workspace --all-features"
    elif (consumer_repo / "pyproject.toml").is_file():
        command = "hyperi-ci check"
    else:
        command = "the consumer's own contract test"
    return CheckResult(
        None,
        _with_note(
            edge,
            f"{ref.describe()} guards the contract. Run `{command}` in "
            f"{consumer_repo} against {producer_version}; it names the field "
            f"that diverged.",
        ),
        ACTION_CONTRACT_TEST,
    )


def check_vendored_file(
    edge: dict,
    *,
    producer_version: str,
    consumer_repo: Path,
    package: str | None = None,
    dry_run: bool = True,
) -> CheckResult:
    """Compare the two committed copies by blob hash.

    Equal hashes mean the copies are in sync. This is the one kind that gives
    a straight yes or no offline, and it only works when the edge names BOTH
    sides: the consumer's copy in ``evidence`` and the producer's in ``source``.
    """
    consumer_ref = parse_evidence(str(edge.get("evidence") or ""))
    if consumer_ref is None:
        raise FleetError(f"edge {_name(edge)} cites no usable evidence")
    producer_ref = parse_evidence(str(edge.get("source") or ""))
    if producer_ref is None:
        return CheckResult(
            None,
            _with_note(
                edge,
                f"{consumer_ref.describe()} is the only copy cited, so there is "
                f"nothing to compare it against. Name the producer's copy as "
                f"`source`, or re-vendor from "
                f"{edge.get('from', 'the producer')} and diff by hand.",
            ),
            ACTION_HUMAN_READ,
        )
    refs = [consumer_ref, producer_ref]
    hashes = [_blob_hash(locate(ref, consumer_repo)) for ref in refs]
    shown = " vs ".join(ref.describe() for ref in refs)
    if hashes[0] == hashes[1]:
        return CheckResult(
            False,
            _with_note(edge, f"{shown}: blob hashes are equal ({hashes[0][:12]})."),
            None,
        )
    return CheckResult(
        True,
        _with_note(
            edge,
            f"{shown}: blob hashes differ ({hashes[0][:12]} vs {hashes[1][:12]}) "
            f"-- re-vendor the producer's copy and rebuild what derives from it.",
        ),
        ACTION_REVENDOR,
    )


def _blob_hash(path: Path) -> str:
    """The git blob hash of a file, as git itself computes it."""
    return run(["git", "hash-object", str(path)]).stdout.strip()


def check_mirrored_logic(
    edge: dict,
    *,
    producer_version: str,
    consumer_repo: Path,
    package: str | None = None,
    dry_run: bool = True,
) -> CheckResult:
    """Never mechanical: no file is copied, so nothing can be compared."""
    ref = parse_evidence(str(edge.get("evidence") or ""))
    shown = ref.describe() if ref else "the cited code"
    return CheckResult(
        None,
        _with_note(
            edge,
            f"{shown} reimplements {edge.get('from', 'the producer')} logic by "
            f"hand. Read both sides against {producer_version} -- no script can "
            f"answer this one.",
        ),
        ACTION_HUMAN_READ,
    )


def check_image_pin(
    edge: dict,
    *,
    producer_version: str,
    consumer_repo: Path,
    package: str | None = None,
    dry_run: bool = True,
) -> CheckResult:
    """The tag and its digest mirror move together, in dfe-infra, not here."""
    ref = _first(edge)
    app = str(edge.get("from") or "the app")
    return CheckResult(
        True,
        _with_note(
            edge,
            f"{ref.describe()} pins the image. Run `dfe-stack bump-app {app} "
            f"{producer_version}` in dfe-infra; the drift check confirms the "
            f"appVersion and the digest mirror agree.",
        ),
        ACTION_BUMP_APP,
    )


def check_version_pin(
    edge: dict,
    *,
    producer_version: str,
    consumer_repo: Path,
    package: str | None = None,
    dry_run: bool = True,
) -> CheckResult:
    """A tag with no artefact copied: move the pin, re-run the consumer's checks."""
    ref = _first(edge)
    return CheckResult(
        True,
        _with_note(
            edge,
            f"Move the pin at {ref.describe()} to {producer_version} and re-run "
            f"the consumer's own validation.",
        ),
        ACTION_MOVE_PIN,
    )


def check_derived_pins(
    edge: dict,
    *,
    producer_version: str,
    consumer_repo: Path,
    package: str | None = None,
    dry_run: bool = True,
) -> CheckResult:
    """Nothing is committed on the consumer side, so nothing can drift."""
    ref = parse_evidence(str(edge.get("evidence") or ""))
    shown = ref.describe() if ref else "the render site"
    return CheckResult(
        False,
        _with_note(
            edge,
            f"{shown} renders the producer's values at build time and commits "
            f"no copy, so the walk goes past this edge.",
        ),
        None,
    )


CheckFunction = Callable[..., CheckResult]

CHECKS: dict[str, CheckFunction] = {
    "cargo-dep": check_cargo_dep,
    "python-dep": check_python_dep,
    "python-dep-undeclared": check_python_dep_undeclared,
    "generated-file": check_generated_file,
    "contract-guard": check_contract_guard,
    "vendored-file": check_vendored_file,
    "mirrored-logic": check_mirrored_logic,
    "image-pin": check_image_pin,
    "version-pin": check_version_pin,
    "derived-pins": check_derived_pins,
}

# What each check does HERE, which is not always the whole check the graph
# describes -- the honest half of the answer is that some kinds defer.
KIND_SUMMARY: dict[str, str] = {
    "cargo-dep": (
        "reads the Cargo range at the cited line and answers whether it admits "
        "the new version; rebuild if it does, widen it first if it does not"
    ),
    "python-dep": (
        "reads the PEP 440 specifier at the cited line and answers whether it "
        "admits the new version; relock if it does, widen it first if it does not"
    ),
    "python-dep-undeclared": (
        "no range exists, so it hands back the named import sites to run the "
        "tests against"
    ),
    "generated-file": (
        "reads the generated header for its schema version and hands back the "
        "regenerate command the file itself names; an empty diff means nothing "
        "moved"
    ),
    "contract-guard": "hands back the consumer's contract test command to run",
    "vendored-file": (
        "compares the two copies by git blob hash when the edge cites both "
        "sides; unequal means re-vendor"
    ),
    "mirrored-logic": (
        "never mechanical -- no file is copied, so it hands back both sides for "
        "a person to read"
    ),
    "image-pin": (
        "hands back the `dfe-stack bump-app` command; the tag and digest move in "
        "dfe-infra, not here"
    ),
    "version-pin": "hands back the pin to move and the validation to re-run",
    "derived-pins": "nothing can drift, so the walk goes past it",
}


def check_edge(
    edge: dict,
    *,
    producer_version: str,
    consumer_repo: Path,
    package: str | None = None,
    dry_run: bool = True,
) -> CheckResult:
    """Run the check for an edge's kind, turning a read failure into a verdict.

    One unreadable file must not abort a whole walk, so a failure comes back as
    an undetermined result naming what went wrong.

    Args:
        edge: The edge as the graph records it.
        producer_version: The version the producer is releasing.
        consumer_repo: The consumer's checkout.
        package: What the producer publishes under, from its node. The two
            range kinds need it to tell the producer's declaration on the
            cited line from a neighbour's.
        dry_run: Reserved for the checks that could act; every check here reads.
    """
    kind = str(edge.get("kind") or "")
    checker = CHECKS.get(kind)
    if checker is None:
        return CheckResult(None, f"unknown edge kind '{kind}'.", ACTION_HUMAN_READ)
    try:
        return checker(
            edge,
            producer_version=producer_version,
            consumer_repo=consumer_repo,
            package=package,
            dry_run=dry_run,
        )
    except FleetError as exc:
        return CheckResult(None, str(exc), ACTION_HUMAN_READ)
