#  Project:      dfe-infra
#  File:         scripts/dfe_suite/rebuild.py
#  Purpose:      Move one consumer onto a new producer version and release it.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Rebuilding a consumer: bump the pin, regenerate what the pin feeds, gate, land.

Two shapes, one story. A Rust consumer takes a precise ``cargo update`` and
re-emits the Dockerfile its deployment contract owns; a Python
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

No chart is regenerated: hyperi-ci assembles the published one from the
deployment contract when the release is cut.

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
        say(f"[dry-run] would pin {package} to {version} and relock")
        say("[dry-run] would regenerate the Dockerfile and docs/ artefacts")
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
    # capability-catalog.*.
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
