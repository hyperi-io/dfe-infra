#  Project:      dfe-infra
#  File:         scripts/dfe_suite/rebuild.py
#  Purpose:      Move one consumer onto a new producer version and release it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rebuilding a consumer: bump the pin, regenerate what the pin feeds, gate, land.

Two shapes, one story. A Rust consumer takes a precise ``cargo update`` and
re-emits the Dockerfile and chart its deployment contract owns; a Python
consumer moves the constraint floor, relocks, and proves the lock resolved the
version that was asked for rather than a stale one a ``>=`` floor still admits.

Both take the ``package`` to bump, defaulting to scalo so the compatibility
CLI is unchanged. A caller that has the graph passes the producer node's own
``package``: the rebuilds are otherwise a scalo-shaped hole that would rewrite
scalo's pin whichever producer released.

Both end the same way: a staged change lands through
:func:`hyperi_ai.suite.landing.land_via_pr`, and the release is followed to a
moved artefact.

A Rust consumer with ``build.skip_optimize`` set cannot release through the
merge: hyperi-ci refuses a stable build with its optimisation stage skipped
unless the run carries the per-run ``release-unoptimized`` input, and a push
event carries no inputs. So when the consumer's ``.hyperi-ci.yaml`` sets it, or
``release_unoptimized`` forces it, the change lands with no trailer and is
released through one consented ``workflow_dispatch`` instead.

A regenerated chart replaces only the directory holding the committed
``Chart.yaml``, and never one whose app pins hand fixes to it: that chart stays
as committed and the app's own chart drift tests decide.

The local Rust gate builds every feature, less any the consumer's suite.yaml
node lists under ``local_gate_exclude_features`` -- a feature that links a
system library the host may lack -- and every feature that turns one on.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from dfe_suite.artefacts import Artefact
from dfe_suite.graph import load_graph
from dfe_suite.landing import (
    CI_WORKFLOW_FILE,
    _slugify,
    dispatch_release,
    follow_release,
    land_via_pr,
)
from dfe_suite.proc import (
    DEFAULT_ORG,
    RUN_TIMEOUT_PYTHON,
    RUN_TIMEOUT_RUST,
    FleetError,
    require_tools,
    run,
    say,
    warn,
)
from dfe_suite.repos import (
    git,
    has_staged_changes,
    repo_slug,
    sync_main,
)


def _emit_dockerfile(repo: Path, app: str) -> None:
    """Regenerate the committed Dockerfile from the app's deployment contract.

    Three spellings are tried because the fleet has not standardised on one;
    collapse this to a single call once every app emits to stdout. ``--bin``
    disambiguates the apps that ship a second binary (pgo-driver).

    Raises:
        FleetError: If no known spelling produced a Dockerfile.
    """
    base = ["cargo", "run", "--quiet", "--bin", app, "--"]
    target = repo / "Dockerfile"

    stdout_form = run([*base, "emit-dockerfile"], cwd=repo, check=False)
    if stdout_form.returncode == 0 and stdout_form.stdout.strip():
        target.write_text(stdout_form.stdout, encoding="utf-8", newline="\n")
        return

    # Emit to a scratch path first, so a half-failed run cannot truncate the
    # committed Dockerfile.
    with tempfile.TemporaryDirectory(prefix="scalo-fleet-") as tmp:
        scratch = Path(tmp) / "Dockerfile"
        for form in (
            [*base, "--emit-dockerfile", str(scratch)],
            [*base, "emit-dockerfile", str(scratch)],
        ):
            emitted = run(form, cwd=repo, check=False).returncode == 0
            if emitted and scratch.exists() and scratch.stat().st_size:
                shutil.copyfile(scratch, target)
                return
    raise FleetError(f"cannot regenerate the Dockerfile for {app} -- no known emit CLI")


# The names the fleet gives its chart drift tests: a test fn, module or file
# whose name carries one of these compares the committed chart, or its values,
# with what the generator writes.
_DRIFT_TEST_NAMES = (
    "helm_contract",
    "committed_chart_matches_the_generator",
    "checked_in_chart_matches_generated",
    "checked_in_chart_matches_generate_chart",
    "committed_chart_config_block_matches_the_contract_default",
    "the_chart_config_block_matches_the_contract",
    "checked_in_keda_scaledobject_survives_emit_chart",
)

# scalo's own drift check: a test that calls it is a drift test whatever its name.
_SCALO_DRIFT_CALL = re.compile(r"\b(?:assert_no_chart_drift|check_chart_drift)\s*\(")

# A fn or mod declared at the start of a line, so a comment that mentions a
# name -- `// TODO: write a helm_contract test` -- declares nothing.
_RUST_ITEM = re.compile(
    r"^\s*(?:#\[[^\]]*\]\s*)*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(fn|mod)\s+(\w+)"
)

# A committed chart that pins a hand edit: a HAND_FIXED exemption list, or a
# scalo ChartPatch (non_exhaustive, so built only through ChartPatch::new).
_HAND_FIX = re.compile(r"\bHAND_FIXED\b|\bChartPatch::new\s*\(")

# Where a Rust app keeps code and tests: `crates/` holds a workspace's members.
_RUST_SOURCE_ROOTS = ("tests", "src", "crates")


def _rust_sources(repo: Path) -> list[Path]:
    """Every Rust file under the app's source roots, build output excluded."""
    found: list[Path] = []
    for name in _RUST_SOURCE_ROOTS:
        root = repo / name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.rs")):
            parts = path.relative_to(root).parts
            if any(part == "target" or part.startswith(".") for part in parts):
                continue
            found.append(path)
    return found


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """The file's lines, numbered from 1, with comment-only lines dropped."""
    text = path.read_text(encoding="utf-8", errors="replace")
    numbered = enumerate(text.splitlines(), start=1)
    return [(number, line) for number, line in numbered if not line.lstrip().startswith("//")]


def _chart_drift_tests(repo: Path) -> dict[str, Path]:
    """The app's chart drift tests, by the name a nextest filter selects them on.

    An in-repo ``chart/`` is the app's contract MIRROR, not the deploy
    artefact -- dfe-infra's ``helm/charts/*`` is what deploys -- and the fleet
    keeps each mirror honest with tests of many names (issue #84). A chart
    those tests check is maintained, not stale.

    Args:
        repo: The consumer checkout.

    Returns:
        Each fn, module or file name that makes a drift test, mapped to the
        file it is in. Empty when the app has none.
    """
    found: dict[str, Path] = {}
    for path in _rust_sources(repo):
        if any(name in path.stem for name in _DRIFT_TEST_NAMES):
            found.setdefault(path.stem, path)
        enclosing = ""
        for _number, line in _code_lines(path):
            item = _RUST_ITEM.match(line)
            if item is not None:
                kind, ident = item.groups()
                if kind == "fn":
                    enclosing = ident
                if any(name in ident for name in _DRIFT_TEST_NAMES):
                    found.setdefault(ident, path)
            elif enclosing and _SCALO_DRIFT_CALL.search(line):
                found.setdefault(enclosing, path)
    return found


def _hand_fix_markers(repo: Path) -> list[str]:
    """Where the app pins a hand edit to its committed chart, as ``file:line``.

    Args:
        repo: The consumer checkout.

    Returns:
        The first marker in each file that carries one, or empty when none does.
    """
    markers: list[str] = []
    for path in _rust_sources(repo):
        for number, line in _code_lines(path):
            if _HAND_FIX.search(line):
                markers.append(f"{path.relative_to(repo)}:{number}")
                break
    return markers


def _chart_root(directory: Path) -> Path | None:
    """The chart in ``directory``: its own ``Chart.yaml``, else one level down.

    Args:
        directory: A ``chart/`` directory, committed or freshly emitted.

    Returns:
        The directory holding ``Chart.yaml``, or None when neither level has one.

    Raises:
        FleetError: If more than one chart sits one level down.
    """
    if (directory / "Chart.yaml").is_file():
        return directory
    nested = sorted(path.parent for path in directory.glob("*/Chart.yaml"))
    if len(nested) > 1:
        raise FleetError(
            f"{directory} holds {len(nested)} charts "
            f"({', '.join(path.name for path in nested)}) and the fleet regenerates "
            f"one. Pass --no-chart and regenerate them by hand."
        )
    return nested[0] if nested else None


def _chart_differences(fresh: Path, committed: Path) -> list[str]:
    """Every file that differs between two chart trees, relative to each root."""

    def files(root: Path) -> dict[str, Path]:
        return {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}

    new, old = files(fresh), files(committed)
    differing: list[str] = []
    for rel in sorted(new.keys() | old.keys()):
        if rel not in old:
            differing.append(f"{rel} (only in the fresh emit)")
        elif rel not in new:
            differing.append(f"{rel} (only in the committed chart)")
        elif new[rel].read_bytes() != old[rel].read_bytes():
            differing.append(rel)
    return differing


def _emit_to_scratch(repo: Path, app: str, scratch: Path) -> Path | None:
    """Emit a fresh chart into ``scratch`` and return the chart root in it.

    A chart is a directory, so nothing can go to stdout: only the path-taking
    spellings the fleet uses are tried here, and none touches the committed
    chart.

    Returns:
        The fresh chart, or None when no spelling produced one.
    """
    base = ["cargo", "run", "--quiet", "--bin", app, "--"]
    for form in (
        [*base, "emit-chart", str(scratch)],
        [*base, "--emit-chart", str(scratch)],
        [*base, "--emit-helm", str(scratch)],
    ):
        shutil.rmtree(scratch, ignore_errors=True)
        if run(form, cwd=repo, check=False).returncode != 0 or not scratch.is_dir():
            continue
        fresh = _chart_root(scratch)
        if fresh is not None:
            return fresh
    return None


def _drift_filter(tests: dict[str, Path]) -> str:
    """The nextest filterset that selects every named drift test.

    A fn or module name is matched against test names. A file directly under a
    ``tests/`` directory is an integration-test binary, whose own test names
    need not carry the file's, so it is selected whole by binary name -- and
    only then, because nextest refuses a ``binary()`` that names no binary.
    """
    expression = f"test(/{'|'.join(sorted(tests))}/)"
    for name, path in sorted(tests.items()):
        if path.stem == name and path.parent.name == "tests":
            expression += f" | binary(={name})"
    return expression


def _run_drift_tests(
    repo: Path, tests: dict[str, Path], *, why: str, features: Sequence[str]
) -> None:
    """Run the app's chart drift tests and stop the rebuild if any fails.

    Args:
        repo: The consumer checkout.
        tests: The drift tests, from :func:`_chart_drift_tests`.
        why: What the failure means for this chart, appended to the error.
        features: The cargo feature flags, from :func:`_gate_features`.

    Raises:
        FleetError: Naming the tests and the exact command, when any fails.
    """
    expression = _drift_filter(tests)
    names = ", ".join(sorted(tests))
    say(f"gate: the chart drift tests ({names})")
    require_tools("cargo-nextest")
    argv = ["cargo", "nextest", "run", "--workspace", *features, "-E", expression]
    if run(argv, cwd=repo, check=False, capture=False).returncode != 0:
        raise FleetError(
            f"the chart drift tests failed ({names}). {why} Rerun them: {shlex.join(argv)}"
        )


# The suite.yaml node key naming the features the local gate leaves out.
_GATE_EXCLUDE_KEY = "local_gate_exclude_features"

# The dfe-infra checkout this module ships in, so the gate reads the suite.yaml
# that matches the tool reading it.
_SUITE_CHECKOUT = Path(__file__).resolve().parents[2]


def _gate_exclusions(app: str) -> list[str]:
    """The features the consumer's suite.yaml node keeps out of the local gate.

    Args:
        app: The consumer's node name, which is its checkout directory name.

    Returns:
        ``package/feature`` entries; empty when the app is not a suite member
        or its node excludes nothing.

    Raises:
        FleetError: If the graph cannot be read, or the key holds anything but
            a list of ``package/feature`` entries.
    """
    nodes = load_graph(dfe_infra=_SUITE_CHECKOUT).get("nodes")
    node = nodes.get(app) if isinstance(nodes, dict) else None
    excluded = node.get(_GATE_EXCLUDE_KEY, []) if isinstance(node, dict) else []
    well_formed = isinstance(excluded, list) and all(
        isinstance(entry, str) and "/" in entry for entry in excluded
    )
    if not well_formed:
        raise FleetError(
            f"suite.yaml node {app}: {_GATE_EXCLUDE_KEY} is {excluded!r}, not a "
            f"list of package/feature entries"
        )
    return excluded


def _gate_features(repo: Path, excluded: Sequence[str]) -> list[str]:
    """The cargo feature flags the local gate builds with.

    ``--all-features``, unless the node excludes some. Then every feature of
    every workspace member is named instead, less each excluded one and each
    feature that turns one on, so an umbrella such as ``full`` cannot bring it
    back; ``--no-default-features`` stops a ``default`` doing the same.

    Args:
        repo: The consumer checkout.
        excluded: ``package/feature`` entries, from :func:`_gate_exclusions`.

    Returns:
        The flags that follow ``--workspace``.

    Raises:
        FleetError: If ``cargo metadata`` does not describe the workspace, or an
            entry names a feature no workspace member has.
    """
    if not excluded:
        return ["--all-features"]
    argv = ["cargo", "metadata", "--format-version", "1", "--no-deps"]
    try:
        packages = json.loads(run(argv, cwd=repo).stdout)["packages"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise FleetError(
            f"{shlex.join(argv)} in {repo} did not describe the workspace: {exc}"
        ) from exc

    features: dict[str, dict[str, list[str]]] = {}
    # A feature names a dependency by its key in Cargo.toml, which a rename changes.
    members_by_key: dict[str, dict[str, str]] = {}
    for package in packages:
        features[package["name"]] = package.get("features") or {}
        members_by_key[package["name"]] = {
            dep.get("rename") or dep["name"]: dep["name"]
            for dep in package.get("dependencies") or []
        }
    for entry in excluded:
        member, _, feature = entry.partition("/")
        if feature not in features.get(member, {}):
            raise FleetError(
                f"suite.yaml keeps {entry} out of the {repo.name} gate, but no "
                f"workspace member of {repo} has that feature"
            )

    dropped = set(excluded)

    def turns_on_dropped(member: str, feature: str, seen: set[str]) -> bool:
        name = f"{member}/{feature}"
        if name in dropped:
            return True
        if name in seen:
            return False
        seen.add(name)
        for value in features[member].get(feature, []):
            if value.startswith("dep:"):
                continue
            key, slash, target = value.partition("/")
            if not slash:
                if turns_on_dropped(member, value, seen):
                    return True
                continue
            other = members_by_key[member].get(key.removesuffix("?"))
            if other in features and turns_on_dropped(other, target, seen):
                return True
        return False

    kept: list[str] = []
    for member in sorted(features):
        for feature in sorted(features[member]):
            if not turns_on_dropped(member, feature, set()):
                kept.append(f"{member}/{feature}")
    return ["--no-default-features", "--features", ",".join(kept)]


def _emit_chart(
    repo: Path,
    app: str,
    *,
    run_drift_tests: bool = False,
    features: Sequence[str] = ("--all-features",),
) -> Path | None:
    """Regenerate the committed Helm chart, if the app ships one.

    A stale chart fails nothing -- it just stops matching what the app would
    produce, so chart changes never reach the fleet. Hence: regenerate, or
    defer to the drift tests that already check it, and never leave it stale
    and unchecked.

    The fresh emit always lands in scratch first. It replaces only the
    directory holding the committed ``Chart.yaml`` (``chart/`` or one level
    down), and never a chart that pins hand fixes: there the fresh emit stays
    in scratch and the app's drift tests decide.

    Args:
        repo: The consumer checkout.
        app: The binary name, which is also the directory name.
        run_drift_tests: Gate on the app's chart drift tests here. True when
            the caller skipped the test suite, which is otherwise where they
            run.
        features: The cargo feature flags the drift tests build with, from
            :func:`_gate_features`.

    Returns:
        The chart directory it rewrote, or None when it left the chart as
        committed.

    Raises:
        FleetError: If the chart cannot be located, if the app commits a chart
            with neither an emit-chart subcommand nor a drift test, or if a
            drift test fails.
    """
    top = repo / "chart"
    if not top.is_dir():
        say("no chart/ -- skipping (this app does not ship one)")
        return None
    chart = _chart_root(top)
    if chart is None:
        raise FleetError(
            f"{top} holds no Chart.yaml, at its root or one level down. Pass "
            f"--no-chart if it is not a Helm chart."
        )
    where = chart.relative_to(repo)
    tests = _chart_drift_tests(repo)
    markers = _hand_fix_markers(repo)
    if not tests:
        warn(f"{app} has no chart drift test -- nothing checks {where} against the generator")

    regenerated: Path | None = None
    with tempfile.TemporaryDirectory(prefix="scalo-fleet-") as tmp:
        fresh = _emit_to_scratch(repo, app, Path(tmp) / "chart")
        if fresh is None:
            why = f"{where} is as committed, and no emit-chart CLI could regenerate it."
            if _without_emitter(repo, app, chart, tests=tests, markers=markers):
                regenerated = chart
        else:
            differing = _chart_differences(fresh, chart)
            if not differing:
                why = f"{where} already matches a fresh emit."
                say(f"{where} already matches a fresh emit -- nothing to regenerate")
            elif markers:
                say(
                    f"{where} pins hand fixes ({', '.join(markers)}) -- the fresh emit "
                    f"stays in scratch and the chart drift tests decide"
                )
                if not tests:
                    warn(f"{where} is left as committed and not regenerated")
                    return None
                _run_drift_tests(
                    repo,
                    tests,
                    why=(
                        f"{where} is left as committed. Files that differ from a fresh "
                        f"emit: {', '.join(differing)}. Regenerate the files the "
                        f"tests name and keep each hand fix."
                    ),
                    features=features,
                )
                say(f"the chart drift tests pass -- {where} stays as committed")
                return None
            else:
                shutil.rmtree(chart)
                shutil.copytree(fresh, chart)
                regenerated = chart
                why = f"{where} was just regenerated from the contract."
                say(f"regenerated {where} from a fresh emit ({len(differing)} file(s))")

    if run_drift_tests and tests:
        _run_drift_tests(repo, tests, why=why, features=features)
    return regenerated


def _without_emitter(
    repo: Path,
    app: str,
    chart: Path,
    *,
    tests: dict[str, Path],
    markers: list[str],
) -> bool:
    """Handle a chart no path-taking emit-chart spelling could regenerate.

    Drift tests take the chart over. With neither tests nor hand fixes, the
    in-place ``emit-chart`` is the last resort, and only for a chart at
    ``chart/`` itself, which is the one place that form is known to write.

    Returns:
        True when the in-place form rewrote the chart.

    Raises:
        FleetError: If nothing regenerates or checks the chart.
    """
    where = chart.relative_to(repo)
    if tests:
        say(f"no emit-chart CLI -- leaving {where} to its chart drift tests")
        return False
    if markers:
        warn(f"{where} pins hand fixes ({', '.join(markers)}) -- left as committed")
        return False
    if chart == repo / "chart":
        chart_yaml = chart / "Chart.yaml"
        # A regeneration rewrites Chart.yaml even when the content is
        # unchanged, so mtime -- not content -- proves the in-place emit ran.
        before = chart_yaml.stat().st_mtime
        base = ["cargo", "run", "--quiet", "--bin", app, "--"]
        emitted = run([*base, "emit-chart"], cwd=repo, check=False).returncode == 0
        if emitted and chart_yaml.exists() and chart_yaml.stat().st_mtime != before:
            return True
    raise FleetError(
        f"{app} commits {where} but exposes neither a known emit-chart CLI nor a "
        f"chart drift test (a test named for {', '.join(_DRIFT_TEST_NAMES)}, or "
        f"one calling scalo's assert_no_chart_drift) -- refusing to leave it stale "
        f"and unchecked. Pass --no-chart if the chart is maintained some other way."
    )


# The per-run consent hyperi-ci needs before it releases a build whose
# optimisation stage was skipped. Neither has a repo variable or config key.
_CONSENT_INPUTS = ("skip-optimize", "release-unoptimized")

_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def _unoptimized_dispatch(slug: str) -> list[str]:
    """The ``gh workflow run`` that releases main's HEAD with the consent set."""
    argv = ["gh", "workflow", "run", CI_WORKFLOW_FILE, "-R", slug, "--ref", "main"]
    argv.extend(["-f", "from-head=true"])
    for name in _CONSENT_INPUTS:
        argv.extend(["-f", f"{name}=true"])
    return argv


def _forwards(value: object, name: str) -> bool:
    """True when a ``with:`` value passes the dispatch input ``name`` through.

    A constant does not count, because ``'true'`` would make the consent
    permanent, and neither does a reference to any other input name.
    """
    if not isinstance(value, str):
        return False
    reference = re.compile(r"(?<![\w-])inputs\." + re.escape(name) + r"(?![\w-])")
    return any(reference.search(expr) for expr in _EXPRESSION.findall(value))


def _consent_gaps(doc: object) -> list[str]:
    """Every piece of the consent a parsed caller workflow is missing.

    Args:
        doc: The parsed ``ci.yml``.

    Returns:
        One line per missing piece; empty when the caller declares both inputs
        and at least one job that calls a reusable workflow forwards both.
    """
    workflow = doc if isinstance(doc, dict) else {}
    triggers = workflow.get("on")
    dispatch = triggers.get("workflow_dispatch") if isinstance(triggers, dict) else None
    inputs = dispatch.get("inputs") if isinstance(dispatch, dict) else None
    declared = inputs if isinstance(inputs, dict) else {}
    gaps = [
        f"on.workflow_dispatch.inputs.{name} is not declared"
        for name in _CONSENT_INPUTS
        if name not in declared
    ]

    jobs = workflow.get("jobs")
    callers: dict[str, dict] = {}
    if isinstance(jobs, dict):
        for job_id, job in jobs.items():
            if isinstance(job, dict) and job.get("uses"):
                callers[str(job_id)] = job
    if not callers:
        gaps.append("no job calls a reusable workflow (jobs.<id>.uses)")
        return gaps

    unforwarded: list[str] = []
    for job_id, job in callers.items():
        passed = job.get("with")
        passed = passed if isinstance(passed, dict) else {}
        job_gaps = [
            f"jobs.{job_id}.with.{name} does not pass inputs.{name}"
            for name in _CONSENT_INPUTS
            if not _forwards(passed.get(name), name)
        ]
        if not job_gaps:
            return gaps
        unforwarded.extend(job_gaps)
    return gaps + unforwarded


def _load_yaml(path: Path) -> object:
    """Parse one YAML file, so a commented-out line reads as absent.

    Raises:
        FleetError: If ruamel.yaml is missing or the file does not parse.
    """
    # Imported here so a path that reads no YAML stays stdlib-only.
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.error import YAMLError
    except ImportError as exc:
        raise FleetError(
            f"ruamel.yaml is required to read {path} "
            f"(scripts/tests/requirements-ci.txt pins it)"
        ) from exc
    try:
        return YAML(typ="safe").load(path.read_text(encoding="utf-8", errors="replace"))
    except YAMLError as exc:
        raise FleetError(f"{path} does not parse as YAML: {exc}") from exc


def _skip_optimize(repo: Path) -> bool:
    """Whether the consumer's ``.hyperi-ci.yaml`` sets ``build.skip_optimize: true``.

    Only a YAML boolean counts: the consented dispatch skips optimisation for
    the release it cuts, so a value that merely reads as true must not choose it.

    Raises:
        FleetError: If the file is there and cannot be read as YAML.
    """
    config = repo / ".hyperi-ci.yaml"
    if not config.is_file():
        return False
    doc = _load_yaml(config)
    build = doc.get("build") if isinstance(doc, dict) else None
    return isinstance(build, dict) and build.get("skip_optimize") is True


def _release_path(*, forced: bool, configured: bool) -> str:
    """Which release path a Rust rebuild takes, and why, as one log line."""
    config = ".hyperi-ci.yaml sets build.skip_optimize: true"
    if forced and configured:
        return f"consented workflow_dispatch -- --release-unoptimized given, and {config}"
    if configured:
        return (
            f"consented workflow_dispatch -- {config}, and hyperi-ci refuses a "
            f"stable release of that build without the per-run consent"
        )
    if forced:
        return (
            "consented workflow_dispatch -- --release-unoptimized given, though "
            ".hyperi-ci.yaml does not set build.skip_optimize, so this release "
            "skips optimisation by choice"
        )
    return (
        "publish trailer on the squash merge -- .hyperi-ci.yaml does not set "
        "build.skip_optimize"
    )


def _check_unoptimized_caller(repo: Path) -> Path:
    """Prove the caller workflow can carry the per-run consent to hyperi-ci.

    Reads the WORKING TREE: the caller is edited in place before the run, and
    ``git add -u`` folds that edit into the release commit. The file is parsed,
    never grepped, so a commented-out line counts as absent.

    Args:
        repo: The consumer checkout.

    Returns:
        The caller workflow that passed.

    Raises:
        FleetError: Naming the file and every missing piece, when the caller
            cannot carry the consent or does not parse.
    """
    workflow = repo / ".github" / "workflows" / CI_WORKFLOW_FILE
    if not workflow.is_file():
        raise FleetError(f"{workflow} is not there -- nothing to dispatch the release through")
    gaps = _consent_gaps(_load_yaml(workflow))
    if gaps:
        raise FleetError(
            f"{workflow} cannot carry the consent for an unoptimised release: "
            f"{'; '.join(gaps)}. Declare {' and '.join(_CONSENT_INPUTS)} as "
            f"workflow_dispatch inputs and pass each to the reusable workflow's "
            f"with: as its own inputs.<name>."
        )
    return workflow


def rebuild_rust(
    repo: Path,
    version: str,
    *,
    package: str = "scalo",
    subject: str = "",
    run_tests: bool = True,
    emit_chart: bool = True,
    watch: bool = True,
    dry_run: bool = False,
    org: str = DEFAULT_ORG,
    release_unoptimized: bool = False,
) -> int:
    """Move one Rust consumer onto a new producer release and ship it.

    Args:
        repo: The consumer's checkout.
        version: The producer version to move onto.
        package: The crate to bump, which is the producer's ``package`` in the
            graph. The default keeps the compatibility CLI on scalo.
        subject: Commit subject; defaults to a ``fix:`` rebuild line.
        run_tests: Run the local nextest gate. False for a Docker-bound suite,
            which CI runs as the publish gate instead.
        emit_chart: Regenerate ``chart/``. False for an app whose chart is
            maintained some other way.
        watch: Follow the release after the merge.
        dry_run: Say what would happen, touch nothing.
        org: GitHub org used when the remote cannot be read.
        release_unoptimized: Force the consented release, which a consumer
            whose ``.hyperi-ci.yaml`` sets ``build.skip_optimize: true`` takes
            anyway: check its caller workflow can carry the per-run consent
            before touching anything, land with no release trailer, then
            release main's HEAD through a ``workflow_dispatch`` that sets
            ``skip-optimize`` and ``release-unoptimized``.

    Returns:
        The process exit code -- non-zero means STOP.

    Raises:
        FleetError: If any step fails, including a caller workflow that cannot
            carry the consent.
    """
    require_tools("git", "gh", "cargo")
    repo = Path(repo).expanduser().resolve()
    if not (repo / ".git").exists():
        raise FleetError(f"{repo} is not a git checkout")
    app = repo.name
    slug = repo_slug(repo, org)
    subject = subject or f"fix: rebuild on {package} {version}"

    # Build scratch that survives a reboot and stays warm between apps.
    # CARGO_TARGET_DIR as an ENV, not --target-dir: some tests locate the
    # built binary via $CARGO_TARGET_DIR, and the flag does not set the env.
    # The path keeps the old scalo-rebuild.sh name so existing warm caches are
    # reused rather than orphaned.
    cache_root = (
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        / "hyperi-ai/scalo-rebuild"
    )
    target = Path(os.environ.get("SCALO_REBUILD_TARGET", str(cache_root / app)))
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)
        os.environ["CARGO_TARGET_DIR"] = str(target)

    say(f"{app} -> {package} {version} (repo {slug}, target {target})")
    if dry_run:
        say("DRY RUN -- nothing will be changed or pushed")

    sync_main(repo, dry_run=dry_run)

    configured = _skip_optimize(repo)
    unoptimized = release_unoptimized or configured
    say(f"release path: {_release_path(forced=release_unoptimized, configured=configured)}")

    dispatch: list[str] = []
    if unoptimized:
        # Before the bump and the build: a caller that cannot carry the consent
        # would otherwise merge, then have its release refused.
        caller = _check_unoptimized_caller(repo)
        say(
            f"preflight: {caller.relative_to(repo)} declares and forwards "
            f"{' and '.join(_CONSENT_INPUTS)}"
        )
        dispatch = _unoptimized_dispatch(slug)

    # Resolved on a dry run too, so a stale exclusion fails before a real run.
    excluded = _gate_exclusions(app)
    features = _gate_features(repo, excluded)
    kept_out = f" (suite.yaml keeps {', '.join(excluded)} out)" if excluded else ""
    say(f"gate features: {' '.join(features)}{kept_out}")

    if dry_run:
        gate = "fmt, clippy, nextest" if run_tests else "fmt, clippy"
        artefacts = "the Dockerfile and docs/ artefacts"
        if emit_chart:
            artefacts = "the Dockerfile, chart/ and docs/ artefacts"
        say(f"[dry-run] would pin {package} to {version} and relock")
        say(f"[dry-run] would regenerate {artefacts}")
        say(f"[dry-run] would gate on {gate}, then commit '{subject}'")
        if unoptimized:
            say(f"[dry-run] would land it on {slug} main via a PR without the release trailer")
            say("[dry-run] would wait for the merge's push run, then dispatch the release:")
            say(f"[dry-run]   {' '.join(dispatch)}")
            if watch:
                say("[dry-run] would follow the dispatched run until the GitHub release moves")
            else:
                say("[dry-run] would return once the dispatch is sent (--no-watch)")
        else:
            say(f"[dry-run] would land it on {slug} main via a PR and follow the release")
        say("dry run complete -- nothing changed, nothing pushed")
        return 0

    say(f"bump {package} -> {version}")
    # The lock may still pin the crate as a PATH source from a local
    # verification build, and --precise then fails with "did not match any
    # packages". `cargo fetch` re-resolves it from crates.io first.
    precise = ["cargo", "update", "-p", package, "--precise", version]
    if run(precise, cwd=repo, check=False).returncode != 0:
        run(["cargo", "fetch"], cwd=repo, capture=False)
        run(precise, cwd=repo, capture=False)

    say("regenerate the Dockerfile from the deployment contract")
    _emit_dockerfile(repo, app)
    # config-schema is a scalo StandardCommand, so every app spells it the same
    # way. It writes into docs/, where the fleet commits config-schema.* and
    # capability-catalog.*. Before the chart, whose step is the one that stops
    # a rebuild, so a stopped rebuild leaves docs/ current.
    say("regenerate the committed config artefacts")
    run(
        [
            "cargo",
            "run",
            "--quiet",
            "--bin",
            app,
            "--",
            "config-schema",
            "--dir",
            "docs",
        ],
        cwd=repo,
        capture=False,
    )
    regenerated: Path | None = None
    if emit_chart:
        say("regenerate the Helm chart from the deployment contract")
        regenerated = _emit_chart(repo, app, run_drift_tests=not run_tests, features=features)
    else:
        say("chart/ regeneration SKIPPED (--no-chart)")

    say("gate: fmt")
    run(["cargo", "fmt", "--all", "--", "--check"], cwd=repo, capture=False)
    say("gate: clippy (all targets, gate features, -D warnings)")
    run(
        ["cargo", "clippy", "--workspace", "--all-targets", *features, "--", "-D", "warnings"],
        cwd=repo,
        capture=False,
    )
    if run_tests:
        # nextest, not `cargo test`: these apps hold process-global state (the
        # metrics recorder, the config registry) that cross-talks under cargo
        # test's in-process parallelism and produces false failures.
        say("gate: tests (nextest, gate features)")
        require_tools("cargo-nextest")
        run(
            ["cargo", "nextest", "run", "--workspace", *features],
            cwd=repo,
            capture=False,
        )
    else:
        # dfe-loader's integration and e2e suites are Docker-bound (ClickHouse
        # and Redpanda via testcontainers), so the local gate fails on a
        # Docker-less box for infra reasons rather than code ones. CI runs the
        # full suite as the publish gate.
        say("gate: tests SKIPPED (--no-tests) -- CI runs the suite as the publish gate")

    # add -u folds in migration work already in the tree, while leaving
    # untracked legacy files (.releaserc.yaml, set-version.py) out.
    git("add", "-u", cwd=repo)
    if regenerated is not None:
        # A template the generator now writes is untracked, which add -u skips.
        git("add", "--all", "--", str(regenerated.relative_to(repo)), cwd=repo)
    if not has_staged_changes(repo):
        say(f"{app} already current on scalo {version} -- nothing to commit")
        return 0

    artefact = Artefact(kind="ghrelease", name=slug)
    before = artefact.baseline()
    note = f"Rebuild on {package} {version}."
    if unoptimized:
        note = f"{note} Released unoptimised by a consented workflow_dispatch."
    merged = land_via_pr(
        repo=repo,
        slug=slug,
        branch=f"scalo/{_slugify(f'rebuild-{version}')}",
        subject=subject,
        note=note,
        publish=not unoptimized,
        dry_run=False,
    )
    assert merged is not None

    if unoptimized:
        dispatch_release(
            repo=repo,
            slug=slug,
            sha=merged,
            dispatch=dispatch,
            artefact=artefact,
            before=before,
            timeout=RUN_TIMEOUT_RUST,
            just_merged=True,
            watch=watch,
        )
        return 0

    if not watch:
        say(
            f"merged {merged[:12]} -- not watching (--no-watch). "
            f"Check: gh run list -R {slug}"
        )
        return 0

    follow_release(
        slug=slug,
        sha=merged,
        artefact=artefact,
        before=before,
        timeout=RUN_TIMEOUT_RUST,
    )
    return 0


def _bump_py_constraint(repo: Path, version: str, *, package: str = "scalo") -> None:
    """Rewrite the version floor in pyproject.toml, preserving extras.

    The dep is an extras-carrying constraint, e.g.
    ``scalo[expression,http,metrics,opentelemetry]>=2.29.7``. A plain
    ``uv add`` rewrites the whole specifier and can silently drop the extras,
    which fails at RUNTIME rather than at build -- so the gates below would
    never catch it.

    Args:
        repo: The consumer's checkout.
        version: The version to raise the floor to.
        package: The distribution to rewrite.

    Raises:
        FleetError: If no constraint on that distribution is there to rewrite.
    """
    path = repo / "pyproject.toml"
    source = path.read_text(encoding="utf-8")
    pattern = re.compile(rf'("{re.escape(package)}(?:\[[^\]]*\])?)>=[0-9][^"]*"')
    updated, count = pattern.subn(lambda m: f'{m.group(1)}>={version}"', source)
    if not count:
        raise FleetError(f"no '{package}...>=X.Y.Z' constraint found in pyproject.toml")
    if updated != source:
        path.write_text(updated, encoding="utf-8", newline="\n")
    say(f"rewrote {count} {package} constraint(s) to >={version}")


def _assert_locked(repo: Path, version: str, *, package: str = "scalo") -> None:
    """Prove uv.lock resolved the version we asked for.

    ``>=`` is a floor, not a pin: a stale cache or a yanked release can leave
    the lock behind, and that would sail through the gates and ship the wrong
    dependency.

    Args:
        repo: The consumer's checkout.
        version: The version the lock has to carry.
        package: The distribution to read out of the lock.

    Raises:
        FleetError: If the package is absent from the lock or resolved elsewhere.
    """
    lock = (repo / "uv.lock").read_text(encoding="utf-8")
    match = re.search(
        rf'name\s*=\s*"{re.escape(package)}"\s*\nversion\s*=\s*"([^"]+)"', lock
    )
    if not match:
        raise FleetError(f"{package} not found in uv.lock")
    if match.group(1) != version:
        raise FleetError(
            f"uv.lock resolved {package} {match.group(1)}, expected {version}"
        )
    say(f"lock confirms {package} {version}")


def rebuild_python(
    repo: Path,
    version: str,
    *,
    package: str = "scalo",
    subject: str = "",
    watch: bool = True,
    dry_run: bool = False,
    org: str = DEFAULT_ORG,
) -> int:
    """Move one Python consumer onto a new producer release and ship it.

    Args:
        repo: The consumer's checkout.
        version: The producer version to move onto.
        package: The distribution to bump, which is the producer's ``package``
            in the graph. The default keeps the compatibility CLI on scalo.
        subject: Commit subject; defaults to a ``fix:`` rebuild line.
        watch: Follow the release after the merge.
        dry_run: Say what would happen, touch nothing.
        org: GitHub org used when the remote cannot be read.

    Returns:
        The process exit code -- non-zero means STOP.
    """
    require_tools("git", "gh", "uv", "hyperi-ci")
    repo = Path(repo).expanduser().resolve()
    if not (repo / ".git").exists():
        raise FleetError(f"{repo} is not a git checkout")
    app = repo.name
    slug = repo_slug(repo, org)
    subject = subject or f"fix: rebuild on {package} {version}"

    say(f"{app} -> {package} {version} (repo {slug})")
    if dry_run:
        say("DRY RUN -- nothing will be changed or pushed")

    sync_main(repo, dry_run=dry_run)

    if dry_run:
        say(f"[dry-run] would raise the {package} floor to >={version} and relock")
        say(f"[dry-run] would assert uv.lock resolved exactly {version}")
        say(f"[dry-run] would gate on `hyperi-ci check`, then commit '{subject}'")
        say(f"[dry-run] would land it on {slug} main via a PR and follow the release")
        say("dry run complete -- nothing changed, nothing pushed")
        return 0

    say(f"bump the {package} constraint -> >={version}")
    _bump_py_constraint(repo, version, package=package)

    say(f"refresh the lock for {package} only")
    run(["uv", "lock", "--upgrade-package", package], cwd=repo, capture=False)
    _assert_locked(repo, version, package=package)

    say("gate: hyperi-ci check (quality + full test suite)")
    run(["hyperi-ci", "check"], cwd=repo, capture=False)

    git("add", "-u", cwd=repo)
    if not has_staged_changes(repo):
        say(f"{app} already current on scalo {version} -- nothing to commit")
        return 0

    artefact = Artefact(kind="ghrelease", name=slug)
    before = artefact.baseline()
    merged = land_via_pr(
        repo=repo,
        slug=slug,
        branch=f"scalo/{_slugify(f'rebuild-{version}')}",
        subject=subject,
        note=f"Rebuild on {package} {version}.",
        publish=True,
        dry_run=False,
    )
    assert merged is not None

    if not watch:
        say(
            f"merged {merged[:12]} -- not watching (--no-watch). "
            f"Check: gh run list -R {slug}"
        )
        return 0

    follow_release(
        slug=slug,
        sha=merged,
        artefact=artefact,
        before=before,
        timeout=RUN_TIMEOUT_PYTHON,
    )
    return 0
