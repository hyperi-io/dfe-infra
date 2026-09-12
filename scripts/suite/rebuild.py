#  Project:      dfe-infra
#  File:         scripts/suite/rebuild.py
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

Both take the ``package`` to bump, defaulting to scalo so the four scalo-shaped
aliases on the CLI are unchanged. A caller that has the graph passes the
producer node's own ``package``: the rebuilds are otherwise a scalo-shaped hole
that would rewrite scalo's pin whichever producer released.

Both end the same way: a staged change lands through
:func:`suite.landing.land_via_pr`, and the release is followed to a moved
artefact.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from suite.artefacts import Artefact
from suite.landing import follow_release, land_via_pr, slugify
from suite.proc import (
    DEFAULT_ORG,
    RUN_TIMEOUT_PYTHON,
    RUN_TIMEOUT_RUST,
    FleetError,
    require_tools,
    run,
    say,
)
from suite.repos import git, has_staged_changes, repo_slug, sync_main


def emit_dockerfile(repo: Path, app: str) -> None:
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
    with tempfile.TemporaryDirectory(prefix="dfe-suite-") as tmp:
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


# What a real helm_contract gate looks like in Rust source: the module the
# suite declares (`mod helm_contract;`) or a test function whose name carries
# the word. A bare mention -- `// TODO: write a helm_contract test` -- is the
# opposite of a gate, so a substring search over the file would disarm the
# chart check on a comment.
HELM_CONTRACT_CODE = re.compile(
    r"(?m)^\s*(?:mod\s+helm_contract\b"
    r"|(?:#\[\w+\]\s*)?(?:pub\s+)?fn\s+\w*helm_contract\w*\s*\()"
)


def chart_contract_test(repo: Path) -> Path | None:
    """The app's chart contract test, if it gates the chart with one.

    An in-repo ``chart/`` is the app's contract MIRROR, not the deploy
    artefact -- dfe-infra's ``helm/charts/*`` is what deploys -- and each app
    keeps its mirror honest with a ``helm_contract`` test that values-syncs the
    chart against the app's own defaults. A chart the gates already test is
    maintained, not stale.

    Args:
        repo: The consumer checkout.

    Returns:
        The test file, or None when the app has no such gate.
    """
    for parent in ("tests", "src"):
        root = repo / parent
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.rs")):
            if "helm_contract" in path.name:
                return path
            if HELM_CONTRACT_CODE.search(path.read_text(encoding="utf-8", errors="replace")):
                return path
    return None


def emit_chart(repo: Path, app: str, *, run_contract_test: bool = False) -> None:
    """Regenerate the committed Helm chart, if the app ships one.

    A stale chart fails nothing -- it just stops matching what the app would
    produce, so chart changes never reach the fleet. Hence: regenerate, or
    defer to the contract test that already checks it; never leave it stale
    and unchecked.

    The contract test is checked FIRST because it is a filesystem scan, while
    every emit-chart spelling costs a full debug build to find out that this
    app has no such subcommand.

    Args:
        repo: The consumer checkout.
        app: The binary name, which is also the directory name.
        run_contract_test: Run the contract test here when the chart is
            deferred to it. True when the caller skipped the tests gate, which
            is otherwise the very gate the chart was just handed to.

    Raises:
        FleetError: If the app commits a chart with neither an emit-chart
            subcommand nor a contract test, or if the contract test fails.
    """
    chart = repo / "chart"
    if not chart.is_dir():
        say("no chart/ -- skipping (this app does not ship one)")
        return

    contract = chart_contract_test(repo)
    if contract is not None:
        say(
            f"{contract.relative_to(repo)} values-syncs chart/ against the app's "
            f"defaults -- leaving the chart to that gate"
        )
        if run_contract_test:
            # --no-tests skips the suite this chart was just deferred to, so
            # run that one test on its own. dfe-loader is both the app with the
            # contract test and the app --no-tests exists for.
            say("gate: the chart contract test (the full suite is skipped)")
            require_tools("cargo-nextest")
            run(
                [
                    "cargo",
                    "nextest",
                    "run",
                    "--workspace",
                    "--all-features",
                    "-E",
                    "test(/helm_contract/)",
                ],
                cwd=repo,
                capture=False,
            )
        return

    base = ["cargo", "run", "--quiet", "--bin", app, "--"]
    chart_yaml = chart / "Chart.yaml"

    # A chart is a directory, so nothing can go to stdout. Try the two
    # path-taking spellings against scratch first, and only fall back to the
    # in-place form, which overwrites the committed chart as it runs.
    with tempfile.TemporaryDirectory(prefix="dfe-suite-") as tmp:
        scratch = Path(tmp) / "chart"
        for form in (
            [*base, "emit-chart", str(scratch)],
            [*base, "--emit-chart", str(scratch)],
        ):
            if (
                run(form, cwd=repo, check=False).returncode == 0
                and (scratch / "Chart.yaml").exists()
            ):
                shutil.rmtree(chart)
                shutil.copytree(scratch, chart)
                return
            shutil.rmtree(scratch, ignore_errors=True)

    # A regeneration rewrites Chart.yaml even when the content is unchanged,
    # so mtime -- not content -- is what proves the in-place emit ran.
    before = chart_yaml.stat().st_mtime if chart_yaml.exists() else 0.0
    emitted = run([*base, "emit-chart"], cwd=repo, check=False).returncode == 0
    if emitted and chart_yaml.exists() and chart_yaml.stat().st_mtime != before:
        return

    raise FleetError(
        f"{app} commits a chart/ but exposes neither a known emit-chart CLI nor "
        f"a helm_contract test -- refusing to leave it stale and unchecked. Pass "
        f"--no-chart if the chart is maintained some other way."
    )


def rebuild_rust(
    repo: Path,
    version: str,
    *,
    package: str = "scalo",
    subject: str = "",
    run_tests: bool = True,
    emit_chart_files: bool = True,
    watch: bool = True,
    dry_run: bool = False,
    org: str = DEFAULT_ORG,
) -> int:
    """Move one Rust consumer onto a new producer release and ship it.

    Args:
        repo: The consumer's checkout.
        version: The producer version to move onto.
        package: The crate to bump, which is the producer's ``package`` in the
            graph. The default keeps the scalo-shaped alias on scalo.
        subject: Commit subject; defaults to a ``fix:`` rebuild line.
        run_tests: Run the local nextest gate. False for a Docker-bound suite,
            which CI runs as the publish gate instead.
        emit_chart_files: Regenerate ``chart/``. False for an app whose chart is
            maintained some other way.
        watch: Follow the release after the merge.
        dry_run: Say what would happen, touch nothing.
        org: GitHub org used when the remote cannot be read.

    Returns:
        The process exit code -- non-zero means STOP.
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
    cache_root = (
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "dfe-suite/rebuild"
    )
    target = Path(os.environ.get("DFE_SUITE_REBUILD_TARGET", str(cache_root / app)))
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)
        os.environ["CARGO_TARGET_DIR"] = str(target)

    say(f"{app} -> {package} {version} (repo {slug}, target {target})")
    if dry_run:
        say("DRY RUN -- nothing will be changed or pushed")

    sync_main(repo, dry_run=dry_run)

    if dry_run:
        gate = "fmt, clippy, nextest" if run_tests else "fmt, clippy"
        artefacts = "the Dockerfile and docs/ artefacts"
        if emit_chart_files:
            artefacts = "the Dockerfile, chart/ and docs/ artefacts"
        say(f"[dry-run] would pin {package} to {version} and relock")
        say(f"[dry-run] would regenerate {artefacts}")
        say(f"[dry-run] would gate on {gate}, then commit '{subject}'")
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
    emit_dockerfile(repo, app)
    if emit_chart_files:
        say("regenerate the Helm chart from the deployment contract")
        emit_chart(repo, app, run_contract_test=not run_tests)
    else:
        say("chart/ regeneration SKIPPED (--no-chart)")
    # config-schema is a scalo StandardCommand, so every app spells it the same
    # way. It writes into docs/, where the fleet commits config-schema.* and
    # capability-catalog.*.
    say("regenerate the committed config artefacts")
    run(
        ["cargo", "run", "--quiet", "--bin", app, "--", "config-schema", "--dir", "docs"],
        cwd=repo,
        capture=False,
    )

    say("gate: fmt")
    run(["cargo", "fmt", "--all", "--", "--check"], cwd=repo, capture=False)
    say("gate: clippy (all targets, all features, -D warnings)")
    run(
        [
            "cargo",
            "clippy",
            "--workspace",
            "--all-targets",
            "--all-features",
            "--",
            "-D",
            "warnings",
        ],
        cwd=repo,
        capture=False,
    )
    if run_tests:
        # nextest, not `cargo test`: these apps hold process-global state (the
        # metrics recorder, the config registry) that cross-talks under cargo
        # test's in-process parallelism and produces false failures.
        say("gate: tests (nextest, all features)")
        require_tools("cargo-nextest")
        run(["cargo", "nextest", "run", "--workspace", "--all-features"], cwd=repo, capture=False)
    else:
        # dfe-loader's integration and e2e suites are Docker-bound (ClickHouse
        # and Redpanda via testcontainers), so the local gate fails on a
        # Docker-less box for infra reasons rather than code ones. CI runs the
        # full suite as the publish gate.
        say("gate: tests SKIPPED (--no-tests) -- CI runs the suite as the publish gate")

    # add -u folds in migration work already in the tree, while leaving
    # untracked legacy files out.
    git("add", "-u", cwd=repo)
    if not has_staged_changes(repo):
        say(f"{app} already current on {package} {version} -- nothing to commit")
        return 0

    artefact = Artefact(kind="ghrelease", name=slug)
    before = artefact.baseline()
    merged = land_via_pr(
        repo=repo,
        slug=slug,
        branch=f"suite/{slugify(f'rebuild-{version}')}",
        subject=subject,
        note=f"Rebuild on {package} {version}.",
        publish=True,
        dry_run=False,
    )
    assert merged is not None

    if not watch:
        say(f"merged {merged[:12]} -- not watching (--no-watch). Check: gh run list -R {slug}")
        return 0

    follow_release(
        slug=slug,
        sha=merged,
        artefact=artefact,
        before=before,
        timeout=RUN_TIMEOUT_RUST,
    )
    return 0


def bump_py_constraint(repo: Path, version: str, *, package: str = "scalo") -> None:
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


def assert_locked(repo: Path, version: str, *, package: str = "scalo") -> None:
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
    match = re.search(rf'name\s*=\s*"{re.escape(package)}"\s*\nversion\s*=\s*"([^"]+)"', lock)
    if not match:
        raise FleetError(f"{package} not found in uv.lock")
    if match.group(1) != version:
        raise FleetError(f"uv.lock resolved {package} {match.group(1)}, expected {version}")
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
            in the graph. The default keeps the scalo-shaped alias on scalo.
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
    bump_py_constraint(repo, version, package=package)

    say(f"refresh the lock for {package} only")
    run(["uv", "lock", "--upgrade-package", package], cwd=repo, capture=False)
    assert_locked(repo, version, package=package)

    say("gate: hyperi-ci check (quality + full test suite)")
    run(["hyperi-ci", "check"], cwd=repo, capture=False)

    git("add", "-u", cwd=repo)
    if not has_staged_changes(repo):
        say(f"{app} already current on {package} {version} -- nothing to commit")
        return 0

    artefact = Artefact(kind="ghrelease", name=slug)
    before = artefact.baseline()
    merged = land_via_pr(
        repo=repo,
        slug=slug,
        branch=f"suite/{slugify(f'rebuild-{version}')}",
        subject=subject,
        note=f"Rebuild on {package} {version}.",
        publish=True,
        dry_run=False,
    )
    assert merged is not None

    if not watch:
        say(f"merged {merged[:12]} -- not watching (--no-watch). Check: gh run list -R {slug}")
        return 0

    follow_release(
        slug=slug,
        sha=merged,
        artefact=artefact,
        before=before,
        timeout=RUN_TIMEOUT_PYTHON,
    )
    return 0
