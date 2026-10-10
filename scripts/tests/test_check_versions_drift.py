#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_check_versions_drift.py
#  Purpose:      Prove the drift check's FAILURE paths fire, not just its
#                happy path -- a guard nobody has seen fail is not a guard.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Failure-path tests for scripts/check_versions_drift.py.

A drift check that only ever prints OK proves nothing: the same output comes
from a check that swept the tree and one whose patterns match nothing. These
tests regress the value a pin used to hold, or drop a waiver, and assert the
check NOTICES.

Everything runs in memory -- no tracked file is edited, so a failed run leaves
no mess to clean up.

    python3 scripts/tests/test_check_versions_drift.py

No third-party deps and no test runner, matching the check it tests: both run
on a bare CI image.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import sys
import tempfile
from pathlib import Path

from _expect import expect, standalone, summary

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_versions_drift.py"

spec = importlib.util.spec_from_file_location("drift", SCRIPT)
drift = importlib.util.module_from_spec(spec)
# @dataclass resolves sys.modules[cls.__module__] while decorating, so the
# module must be registered before exec_module rather than after.
sys.modules["drift"] = drift
spec.loader.exec_module(drift)


def test_sweep_is_clean_as_committed() -> None:
    problems = drift.reverse_sweep()
    expect("sweep is clean as committed", problems == [], f"got {problems}")


def test_sweep_reads_a_real_tree() -> None:
    """Guards the failure mode where the roots stop resolving and it sweeps air."""
    files = drift.sweep_files()
    expect("sweep reaches a non-trivial file set", len(files) > 100, f"got {len(files)}")


def test_sweep_reaches_the_repo_root() -> None:
    """The root is swept non-recursively, so top-level pins are not invisible."""
    files = drift.sweep_files()
    expect(
        "deployment.example.yaml is in the swept set",
        Path("deployment.example.yaml") in files,
        f"root files swept: {[f for f in files if f.parent == Path('.')]}",
    )


def test_the_operators_own_dial_is_not_swept() -> None:
    """deployment.yaml is the checker's INPUT, not a mirror of versions.yaml.

    It carries a stack pin no check can ever read, so sweeping it reported it
    unswept forever and `dfe-ops stack-deploy` refused every deploy that put the
    dial where the tooling documents. It is gitignored, and so is every other
    local artefact the sweep must stay out of.
    """
    dial = REPO_ROOT / "deployment.yaml"
    pre_existing = dial.is_file()
    try:
        if not pre_existing:
            dial.write_text('version:\n  pin: "2.2.0-rc.14"\n', encoding="utf-8")
        drift.sweep_files.cache_clear()
        files = drift.sweep_files()
        expect(
            "the gitignored dial is outside the swept set",
            Path("deployment.yaml") not in files,
            f"root files swept: {[f for f in files if f.parent == Path('.')]}",
        )
        expect(
            "the tracked example is still inside it",
            Path("deployment.example.yaml") in files,
            "the exclusion is too broad",
        )
    finally:
        if not pre_existing and dial.is_file():
            dial.unlink()
        drift.sweep_files.cache_clear()


def test_the_exclusion_reads_gitignore_rather_than_one_filename() -> None:
    """A second local artefact at the root must stay out without another edit."""
    names = drift.root_ignored_names()
    expect("deployment.yaml is root-anchored in .gitignore", "deployment.yaml" in names,
           f"got {sorted(names)}")
    expect("a tracked root file is not excluded", "deployment.example.yaml" not in names,
           "the example would vanish from the sweep")


def test_stack_pin_surfaces_without_its_check() -> None:
    """Drop the check and the example's stack pin must come back as unswept.

    Proves the root sweep and the `pin:` pattern both work, rather than the
    literal being invisible to the sweep and merely covered by a check.
    """
    original = drift.CHECKS
    try:
        drift.CHECKS = [c for c in original if c.label != "stack pin (deployment example)"]
        unswept = [
            p for p in drift.reverse_sweep() if "[unswept]" in p and "deployment.example.yaml" in p
        ]
        expect(
            "the example's stack pin is visible to the sweep",
            len(unswept) == 1,
            f"got {unswept}",
        )
    finally:
        drift.CHECKS = original


def test_stale_stack_pin_is_caught() -> None:
    """A worked example left on the previous stack must be reported."""
    versions = drift.load_versions()
    check = next(c for c in drift.CHECKS if c.label == "stack pin (deployment example)")
    real_read = drift.read_source

    def poisoned(file_path: Path) -> str:
        text = real_read(file_path)
        if file_path.as_posix() == "deployment.example.yaml":
            return text.replace(versions["pointers.current"], "2.1.0-rc.9")
        return text

    try:
        drift.read_source = poisoned
        expect(
            "an example pinning a superseded stack is caught",
            drift.extract_value(check) != versions["pointers.current"],
        )
    finally:
        drift.read_source = real_read

    expect(
        "the example matches the current stack as committed",
        drift.extract_value(check) == versions["pointers.current"],
    )


def test_missing_sweep_root_is_fatal() -> None:
    """A renamed directory must stop the run, not quietly shrink the sweep."""
    original = drift.SWEEP_ROOTS
    try:
        drift.SWEEP_ROOTS = (*original, "does-not-exist")
        drift.sweep_files.cache_clear()
        raised = False
        try:
            drift.sweep_files()
        except SystemExit:
            raised = True
        expect("a missing sweep root is fatal", raised, "no SystemExit raised")
    finally:
        drift.SWEEP_ROOTS = original
        drift.sweep_files.cache_clear()


def test_waiver_that_excuses_nothing_is_reported() -> None:
    original = drift.SWEEP_WAIVERS
    try:
        drift.SWEEP_WAIVERS = (
            *original,
            ("helm/charts/does-not-exist/values.yaml", "image tag", "nothing"),
        )
        problems = drift.reverse_sweep()
        expect(
            "a waiver matching nothing is reported as rot",
            any("[stale]" in p and "does-not-exist" in p for p in problems),
            f"got {problems}",
        )
    finally:
        drift.SWEEP_WAIVERS = original


def test_dropping_a_waiver_resurfaces_its_literals() -> None:
    original = drift.SWEEP_WAIVERS
    try:
        drift.SWEEP_WAIVERS = tuple(w for w in original if w[0] != "*/Chart.yaml")
        unswept = [p for p in drift.reverse_sweep() if "[unswept]" in p]
        expect(
            "dropping the Chart.yaml waiver resurfaces its literals",
            len(unswept) > 20,
            f"got {len(unswept)}",
        )
    finally:
        drift.SWEEP_WAIVERS = original


def test_fix_is_a_noop_when_nothing_drifts() -> None:
    writes, fixed, refused = drift.plan_fix(drift.load_versions())
    expect("--fix writes nothing when the tree is clean", writes == {}, f"{writes}")
    expect("--fix reports no repairs when clean", fixed == [], f"{fixed}")
    expect("--fix refuses nothing when clean", refused == [], f"{refused}")


def test_fix_propagates_one_ssot_key_to_every_mirror() -> None:
    """One SSoT bump must reach ALL of a key's mirrors, not just Renovate's one.

    A ClickHouse lift moves services.clickhouse-version and, by constraints rule
    clickhouse-keeper-server-pairing, services.clickhouse-keeper with it. Between
    them they have four mirrors: the server version and the keeper tag in
    values.yaml, the chart appVersion, and the dfe-toolbox base image's
    clickhouse-client build ARG. Only the keeper tag is helm-values, so Renovate
    could never have carried the other three.
    """
    versions = dict(drift.load_versions())
    versions["services.clickhouse-version"] = "26.3.17.110"
    versions["services.clickhouse-keeper"] = "26.3.17.110"

    writes, fixed, refused = drift.plan_fix(versions)
    expect("propagation refuses nothing on a plain bump", refused == [], f"{refused}")
    expect(
        "all four clickhouse mirrors are rewritten",
        len([f for f in fixed if "clickhouse" in f]) == 4,
        f"{fixed}",
    )
    chart = writes.get(Path("helm/charts/clickhouse-cluster/Chart.yaml"), "")
    values = writes.get(Path("helm/charts/clickhouse-cluster/values.yaml"), "")
    expect(
        "the chart appVersion carries the new value",
        'appVersion: "26.3.17.110"' in chart,
    )
    expect(
        "both values.yaml mirrors carry it",
        values.count("26.3.17.110") == 2,
        f"count={values.count('26.3.17.110')}",
    )
    expect(
        "surrounding content is untouched",
        "clickhouse-keeper" in values and "dfe-clickhouse" in values,
    )


def test_fix_moves_both_halves_of_a_contract_entry() -> None:
    """The engine mounts each app's contract by running that app's pinned image.

    The entry names tag@sha256 and the node pulls by digest, so a tag rewritten
    on its own would name one release and run another.
    """
    versions = dict(drift.load_versions())
    digest = "sha256:" + "9" * 64
    versions["apps.dfe-loader"] = "v1.18.99"
    versions["digests.dfe-loader"] = digest

    writes, _, refused = drift.plan_fix(versions)
    expect("propagation refuses nothing on a plain app bump", refused == [], f"{refused}")
    values = writes.get(Path("helm/charts/dfe-engine/values.yaml"), "")
    expect(
        "the loader's content entry carries the new tag@sha256",
        f'ref: "ghcr.io/hyperi-io/dfe-loader:v1.18.99@{digest}"' in values,
        f"{[line for line in values.splitlines() if 'dfe-loader:' in line]}",
    )
    expect(
        "and the five other entries are untouched",
        values.count("v1.18.99") == 1 and values.count(digest) == 1,
        f"tag={values.count('v1.18.99')} digest={values.count(digest)}",
    )


def test_a_contract_entry_ref_is_visible_to_the_sweep() -> None:
    """Drop its checks and the ref must come back as unswept.

    `ref:` is read by nothing else in the tree, so without the sweep pattern a
    seventh entry could be added with no check and pass.
    """
    original = drift.CHECKS
    try:
        drift.CHECKS = [c for c in original if "dfe-loader contract entry" not in c.label]
        unswept = [p for p in drift.reverse_sweep() if "[unswept]" in p and "content ref" in p]
        expect(
            "the unchecked ref surfaces, and only that one",
            len(unswept) == 1 and "dfe-engine/values.yaml" in unswept[0],
            f"got {unswept}",
        )
    finally:
        drift.CHECKS = original


def test_fix_moves_both_entries_one_app_backs() -> None:
    """The elastic image backs two entries, its contract and its catalogue.

    Each is its own copy of the same app pin, so a bump that reached only the
    first would leave the catalogue emitted by a release the deployment no
    longer runs.
    """
    versions = dict(drift.load_versions())
    digest = "sha256:" + "8" * 64
    versions["apps.dfe-transform-elastic"] = "v1.9.99"
    versions["digests.dfe-transform-elastic"] = digest

    writes, _, refused = drift.plan_fix(versions)
    expect("propagation refuses nothing on a plain app bump", refused == [], f"{refused}")
    values = writes.get(Path("helm/charts/dfe-engine/values.yaml"), "")
    new_ref = f'ref: "ghcr.io/hyperi-io/dfe-transform-elastic:v1.9.99@{digest}"'
    expect(
        "the contract and the catalogue entry both carry the new tag@sha256",
        values.count(new_ref) == 2,
        f"{[line for line in values.splitlines() if 'dfe-transform-elastic:' in line]}",
    )
    catalogue = values.split("name: dfe-transform-elastic-catalogue", 1)[-1].split("\n\n", 1)[0]
    expect("one of them is the catalogue entry", new_ref in catalogue, catalogue)


def test_the_catalogue_entry_ref_is_visible_to_the_sweep() -> None:
    """A second ref for an app already checked must not hide behind the first.

    Each check claims its first match only, so without its own anchored check the
    catalogue ref would be a pin nobody reads.
    """
    original = drift.CHECKS
    try:
        drift.CHECKS = [c for c in original if "catalogue entry" not in c.label]
        unswept = [p for p in drift.reverse_sweep() if "[unswept]" in p and "content ref" in p]
        text = drift.read_source(Path("helm/charts/dfe-engine/values.yaml"))
        line = text[: text.index("name: dfe-transform-elastic-catalogue")].count("\n") + 1
        expect(
            "the unchecked catalogue ref surfaces, and only that one",
            len(unswept) == 1 and "dfe-engine/values.yaml" in unswept[0],
            f"got {unswept}",
        )
        expect(
            "at the catalogue entry, not the contract entry above it",
            bool(unswept) and f"values.yaml:{line + 4} " in unswept[0],
            f"entry at line {line}, got {unswept}",
        )
    finally:
        drift.CHECKS = original


def test_fix_refuses_rather_than_guessing() -> None:
    """A pattern that stopped matching means the file changed shape.

    Writing at a guessed offset would corrupt a chart, so the whole run must
    refuse -- and refuse ATOMICALLY, leaving no partial propagation behind.
    """
    versions = dict(drift.load_versions())
    versions["services.clickhouse-version"] = "26.3.17.110"
    original = drift.CHECKS
    try:
        drift.CHECKS = [
            drift.Check(
                "deliberately unmatchable",
                "services.clickhouse-version",
                Path("helm/charts/clickhouse-cluster/values.yaml"),
                r"this-pattern-matches-nothing-([0-9]+)",
            ),
            *original,
        ]
        writes, fixed, refused = drift.plan_fix(versions)
        expect(
            "an unmatchable pattern is refused",
            any("deliberately unmatchable" in r for r in refused),
            f"{refused}",
        )
        expect(
            "the refusal names the file so it can be repaired",
            any("values.yaml" in r for r in refused),
            f"{refused}",
        )
        # The other mirrors ARE planned -- so it is the refusal, not an empty
        # plan, that stops apply_fix writing. That is what makes it atomic.
        expect(
            "valid mirrors are still planned alongside the refusal",
            bool(writes) and bool(fixed),
            f"writes={len(writes)} fixed={len(fixed)}",
        )
    finally:
        drift.CHECKS = original


def test_fix_refuses_overlapping_spans() -> None:
    """Two checks claiming the same bytes would splice into garbage."""
    versions = dict(drift.load_versions())
    versions["services.clickhouse-version"] = "26.3.17.110"
    original = drift.CHECKS
    try:
        # Two patterns whose capture groups overlap on the same literal.
        target = Path("helm/charts/clickhouse-cluster/values.yaml")
        drift.CHECKS = [
            drift.Check(
                "overlap A",
                "services.clickhouse-version",
                target,
                r"\n  version:\s*\"([^\"]+)\"",
            ),
            drift.Check(
                "overlap B",
                "services.clickhouse-version",
                target,
                r"\n  version:\s*\"([^\"]+)\"",
            ),
        ]
        _, _, refused = drift.plan_fix(versions)
        expect(
            "overlapping spans are refused, not spliced",
            any("overlapping bytes" in r for r in refused),
            f"{refused}",
        )
    finally:
        drift.CHECKS = original


def test_regressed_appversions_are_caught() -> None:
    """The values these charts carried before the reverse sweep found them.

    dfe-common.labels stamps app.kubernetes.io/version from .Chart.AppVersion,
    so a stale value here ships a wrong version label onto every rendered
    object -- silent, because no image tag depends on it.
    """
    versions = drift.load_versions()
    regressions = {
        "helm/charts/clickhouse-cluster/Chart.yaml": (
            "services.clickhouse-version",
            "clickhouse-cluster chart appVersion",
            '"24.8"',
        ),
        "helm/charts/kafka/Chart.yaml": (
            "services.kafka-version",
            "kafka chart appVersion",
            '"3.9.0"',
        ),
    }
    real_read = drift.read_source

    def poisoned(file_path: Path) -> str:
        text = real_read(file_path)
        entry = regressions.get(file_path.as_posix())
        if not entry:
            return text
        key, _, old = entry
        return text.replace(f'"{versions[key]}"', old)

    try:
        drift.read_source = poisoned
        for key, label, old in regressions.values():
            check = next(c for c in drift.CHECKS if c.label == label)
            actual = drift.extract_value(check)
            expect(
                f"{label}: the pre-fix value {old} is caught as drift",
                actual != versions[key],
                f"{actual!r} vs SSoT {versions[key]!r}",
            )
    finally:
        drift.read_source = real_read

    for key, label, _ in regressions.values():
        check = next(c for c in drift.CHECKS if c.label == label)
        expect(
            f"{label}: matches SSoT as committed",
            drift.extract_value(check) == versions[key],
        )


def test_the_metallb_pin_is_accounted_for_by_a_reason() -> None:
    """bootstrap.sh is its only reader, so there is no mirror to check it against."""
    versions = drift.load_versions()
    expect("bootstrap.metallb is in the current stack", "bootstrap.metallb" in versions,
           f"got {sorted(k for k in versions if k.startswith('bootstrap.'))}")
    expect("no CHECKS entry claims it",
           "bootstrap.metallb" not in {c.key for c in drift.CHECKS})
    expect("an UNCONSUMED reason accounts for it instead",
           drift.unconsumed_reason("bootstrap.metallb") is not None)
    bootstrap_sh = (REPO_ROOT / "bootstrap" / "bootstrap.sh").read_text(encoding="utf-8")
    expect("and the recorded reason is true -- bootstrap.sh reads the key",
           "bootstrap.metallb" in bootstrap_sh, "no runtime read of the pin")


def test_dropping_the_metallb_reason_reports_the_pin_dead() -> None:
    """A pin accounted for by nothing has to FAIL the run, not pass quietly."""
    original = drift.UNCONSUMED
    captured = io.StringIO()
    try:
        drift.UNCONSUMED = {k: v for k, v in original.items() if k != "bootstrap.metallb"}
        with contextlib.redirect_stderr(captured), contextlib.redirect_stdout(io.StringIO()):
            rc = drift.main()
    finally:
        drift.UNCONSUMED = original
    expect("an unaccounted pin fails the check", rc == 1, f"got rc={rc}")
    expect("and the failure names the key",
           "bootstrap.metallb" in captured.getvalue(), captured.getvalue())


def test_stack_flag_selects_a_different_block() -> None:
    """`--stack` must change which block is flattened, not just be accepted."""
    rc12 = drift.load_versions("2.2.0-rc.12")
    rc13 = drift.load_versions("2.2.0-rc.13")
    expect("rc.12 and rc.13 pin dfe-engine differently",
           rc12["apps.dfe-engine"] != rc13["apps.dfe-engine"],
           f"{rc12['apps.dfe-engine']!r} vs {rc13['apps.dfe-engine']!r}")
    expect("the selected stack id travels as pointers.current",
           rc12["pointers.current"] == "2.2.0-rc.12" and rc13["pointers.current"] == "2.2.0-rc.13")


def test_default_stack_matches_the_current_pointer() -> None:
    """No --stack means the same thing it always did: whatever `current` names."""
    default = drift.load_versions()
    explicit = drift.load_versions(default["pointers.current"])
    expect("an explicit --stack matching current agrees with the default",
           default == explicit)


def test_unknown_stack_is_fatal() -> None:
    raised = False
    try:
        drift.load_versions("not-a-real-stack")
    except SystemExit:
        raised = True
    expect("an unknown --stack value is fatal", raised)


def test_pending_keys_are_noted_once_each_not_once_per_mirror() -> None:
    """rc.13 pins none of the rc.14-only keys, so each is NOTED -- once, however
    many mirrors point at it, or the wall of notes trains the reader to skip
    them.

    The run's exit code is not part of the subject. Every mirror in the tree
    holds the value of the CURRENT stack, so auditing an older one reports the
    whole cut as drift -- that is the audit working, and this check is about
    the notes beside it."""
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(io.StringIO()):
        drift.main(["--stack", "2.2.0-rc.13"])
    out = captured.getvalue()
    # hashicorp-aws has a CHECKS entry per terraform dir -- eight of them.
    aws_provider_notes = [ln for ln in out.splitlines() if "providers.hashicorp-aws" in ln]
    expect("the aws provider pin is noted exactly once",
           len(aws_provider_notes) == 1, f"{aws_provider_notes}")
    expect("and the note says the stack does not pin it",
           "does not pin it yet" in aws_provider_notes[0], aws_provider_notes[0])


def test_pending_mirrors_must_be_deleted_once_the_stack_pins_them_all() -> None:
    """A temporary exemption with no expiry rots into a permanent one."""
    original = drift.PENDING_MIRRORS
    captured = io.StringIO()
    try:
        # apps.dfe-engine is pinned by every stack, so this stands in for the
        # day rc.14 lands and every real entry resolves.
        drift.PENDING_MIRRORS = {"apps.dfe-engine": "stand-in for a landed pin"}
        with contextlib.redirect_stderr(captured), contextlib.redirect_stdout(io.StringIO()):
            rc = drift.main()
    finally:
        drift.PENDING_MIRRORS = original
    expect("a fully-landed PENDING_MIRRORS fails the run", rc == 1, f"got rc={rc}")
    expect("and the failure says to delete it",
           "delete PENDING_MIRRORS" in captured.getvalue(), captured.getvalue())


def test_a_mistyped_stack_flag_is_rejected_rather_than_ignored() -> None:
    """The one failure a drift gate must not have: a typo that audits the wrong
    stack and still exits 0."""
    raised = False
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            drift._parse_args(["--satck", "2.2.0-rc.14"])
    except SystemExit:
        raised = True
    expect("an unrecognised flag is fatal", raised)
    args = drift._parse_args(["--stack", "2.2.0-rc.13"])
    expect("the spelling it does own still parses", args.stack == "2.2.0-rc.13")


def test_fix_refuses_a_stack_that_is_not_current() -> None:
    """--fix WRITES the tree, and the tree mirrors `current`; propagating an
    older block over it would silently downgrade every chart appVersion, appset
    pin and Dockerfile ARG, and the verify pass that follows would then agree
    because it compares against the same wrong block."""
    written = False

    def _never(_versions: dict[str, str]) -> tuple[list[str], list[str]]:
        nonlocal written
        written = True
        return [], []

    original = drift.apply_fix
    raised = ""
    try:
        drift.apply_fix = _never
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            drift.main(["--fix", "--stack", "2.2.0-rc.12"])
    except SystemExit as exc:
        raised = str(exc)
    finally:
        drift.apply_fix = original
    expect("the combination is refused", "refusing" in raised, f"got {raised!r}")
    expect("and the refusal names the stack", "2.2.0-rc.12" in raised, f"got {raised!r}")
    expect("and nothing was written", not written)


def test_pending_mirrors_is_exactly_the_documented_rc14_set() -> None:
    """Widening this list is the cheapest way to make a real drift failure go
    away, so it has to be a reviewed edit rather than a quiet one."""
    expect("PENDING_MIRRORS holds the eighteen rc.14 patterns and no more",
           set(drift.PENDING_MIRRORS) == {
               "operators.karpenter",
               "operators.aws-load-balancer-controller",
               "operators.karpenter-al2023-ami",
               "services.aws-msk-iam-auth",
               "services.cruise-control-ui",
               "services.busybox",
               "services-digests.busybox",
               "services.envoy-gateway-proxy",
               "services-digests.envoy-gateway-proxy",
               "services.clickhouse-keeper",
               "services-digests.clickhouse-keeper",
               "services-digests.forgejo",
               "services-digests.valkey",
               "toolbox.*",
               "providers.hashicorp-aws",
               "providers.confluentinc-confluent",
               "providers.redpanda-data-redpanda",
               "providers.hashicorp-archive",
           },
           f"{sorted(drift.PENDING_MIRRORS)}")


def _dead_guards_over(rules: str, versions: dict[str, str]) -> list[str]:
    """Run dead_guards against a throwaway versions.yaml + constraints pair.

    dead_guards resolves the constraints path out of versions.yaml and then off
    REPO_ROOT, so both module globals move to a temp tree -- nothing tracked is
    touched and a failed run leaves no mess.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "constraints").mkdir()
        (root / "constraints" / "under-test.yaml").write_text(
            'schema: 1\nstack: "9.9.9"\n\nrules:\n\n' + rules, encoding="utf-8"
        )
        (root / "versions.yaml").write_text(
            'schema: 2\ncurrent: "9.9.9"\n\nstacks:\n\n  9.9.9:\n'
            '    constraints: "constraints/under-test.yaml"\n',
            encoding="utf-8",
        )
        original_root, original_file = drift.REPO_ROOT, drift.VERSIONS_FILE
        try:
            drift.REPO_ROOT = root
            drift.VERSIONS_FILE = root / "versions.yaml"
            return drift.dead_guards(versions, "9.9.9")
        finally:
            drift.REPO_ROOT, drift.VERSIONS_FILE = original_root, original_file


# One rule per guard form, watching the same key, so a test can move that key
# and see which forms notice (dfe-infra#295: only when-equals did).
_RANGE_RULE = (
    "  ceiling:\n"
    '    severity: "error"\n'
    '    when-key: "operators.some-operator"\n'
    '    when-range: ">=0.51.0 <0.52.0"\n'
    '    require-key: "services.some-service"\n'
    '    require-range: ">=4.1.0 <=4.2.0"\n'
)
_EQUALS_RULE = (
    "  pairing:\n"
    '    severity: "warn"\n'
    '    when-key: "operators.some-operator"\n'
    '    when-equals: "0.51.0"\n'
    '    require-key: "services.some-service"\n'
    '    require-equals: "4.2.0"\n'
)


def test_a_when_range_guard_the_pin_has_left_is_reported_dead() -> None:
    """Lifting a pinned operator outside its range used to report nothing at
    all, so an error-severity rule went inert and read as passing."""
    problems = _dead_guards_over(
        _RANGE_RULE, {"operators.some-operator": "1.2.0", "services.some-service": "4.2.0"}
    )
    expect("the range guard is reported", len(problems) == 1, f"got {problems}")
    if problems:
        expect("and the message names when-range", "when-range" in problems[0], problems[0])
        expect("and the value that left it", "1.2.0" in problems[0], problems[0])


def test_a_when_range_guard_the_pin_still_satisfies_is_left_alone() -> None:
    """The inverse: a live range must not be reported, or the check cries wolf
    on every stack and stops being read."""
    problems = _dead_guards_over(
        _RANGE_RULE, {"operators.some-operator": "0.51.0", "services.some-service": "4.2.0"}
    )
    expect("a live range guard raises nothing", problems == [], f"got {problems}")


def test_both_guard_forms_are_reported_by_the_same_move() -> None:
    """The defect was asymmetry: one pin move, one form reported, one silent."""
    problems = _dead_guards_over(
        _RANGE_RULE + "\n" + _EQUALS_RULE,
        {"operators.some-operator": "1.2.0", "services.some-service": "4.2.0"},
    )
    expect("both rules are reported", len(problems) == 2, f"got {problems}")


def test_the_committed_constraints_carry_no_dead_guard() -> None:
    """Two of the four live rules are when-range, so this also catches the range
    comparison being inverted."""
    versions = drift.load_versions()
    problems = drift.dead_guards(versions, versions["pointers.current"])
    expect("the committed rules can all still fire", problems == [], f"got {problems}")


_HELM_LINT = Path(".github/workflows/helm-lint.yml")
_RELEASE = Path(".github/workflows/release.yml")


def _workflow(version: str) -> str:
    return f'name: x\nenv:\n  HELM_VERSION: "{version}"\njobs: {{}}\n'


def test_the_committed_workflows_agree_on_helm() -> None:
    texts = {path: drift.read_source(path) for path in drift.HELM_WORKFLOWS}
    problems = drift.helm_version_problems(texts)
    expect("helm-lint.yml and release.yml install one Helm", problems == [], f"got {problems}")
    expect("and both files are read", set(texts) == {_HELM_LINT, _RELEASE}, f"{sorted(texts)}")


def test_a_helm_version_moved_in_one_workflow_is_drift() -> None:
    problems = drift.helm_version_problems(
        {_HELM_LINT: _workflow("4.3.0"), _RELEASE: _workflow("4.2.4")}
    )
    expect("one drift line", len(problems) == 1, f"got {problems}")
    if problems:
        expect("naming both values", "4.3.0" in problems[0] and "4.2.4" in problems[0], problems[0])
        expect("and both files", str(_RELEASE) in problems[0], problems[0])


def test_a_workflow_that_stops_pinning_helm_is_reported() -> None:
    """A literal the pattern no longer finds would otherwise compare as agreement."""
    problems = drift.helm_version_problems(
        {_HELM_LINT: _workflow("4.2.4"), _RELEASE: "env:\n  HELM_VERSION: ${{ vars.HELM }}\n"}
    )
    expect("the file with no literal is named", any(str(_RELEASE) in p for p in problems),
           f"got {problems}")


def test_the_toolbox_helm_is_not_tied_to_the_workflows() -> None:
    """toolbox.helm is the toolbox image's own pin, a separate decision."""
    texts = {_HELM_LINT: _workflow("4.2.4"), _RELEASE: _workflow("4.2.4")}
    expect("agreeing workflows pass whatever toolbox.helm says",
           drift.helm_version_problems(texts) == [])
    expect("and no check holds HELM_VERSION to it",
           not any(c.file in (_HELM_LINT, _RELEASE) for c in drift.CHECKS))


_APPS_APPSET = Path("argocd/appsets/layer2-apps.yaml")
_EDGE_APPSET = Path("argocd/appsets/layer2-edge.yaml")


def _chart_pin_texts() -> dict[Path, str]:
    return {path: drift.read_source(path) for path in drift.CHART_PIN_APPSETS}


def test_the_committed_chart_pin_maps_match_chart_digests() -> None:
    versions = drift.load_versions()
    problems = drift.chart_pin_problems(versions, _chart_pin_texts())
    expect("both appsets' maps hold exactly what they deploy", problems == [], f"got {problems}")
    keys = drift.chart_digest_keys(versions)
    expect("chart-digests pins eleven components", len(keys) == 11, f"{sorted(keys)}")
    checked = {c.key for c in drift.CHECKS if c.key.startswith("chart-digests.")}
    expect("and every one of them is read by a check",
           checked == {f"chart-digests.{k}" for k in keys}, f"{sorted(checked)}")


def test_a_chart_pin_map_missing_a_service_is_drift() -> None:
    """The appset fails that service's render, so its Application never moves."""
    texts = _chart_pin_texts()
    texts[_APPS_APPSET] = re.sub(r'\n\s*"dfe-ui" "[^"]*"', "", texts[_APPS_APPSET], count=1)
    problems = drift.chart_pin_problems(drift.load_versions(), texts)
    expect("one problem, naming the service the map lacks",
           len(problems) == 1 and "lacks ['dfe-ui']" in problems[0], f"got {problems}")


def test_a_chart_pin_map_carrying_a_service_it_does_not_deploy_is_drift() -> None:
    texts = _chart_pin_texts()
    texts[_EDGE_APPSET] = texts[_EDGE_APPSET].replace(
        '"culvert" "', '"dfe-ui" "unpublished"\n                "culvert" "', 1
    )
    problems = drift.chart_pin_problems(drift.load_versions(), texts)
    expect("one problem, naming the extra service",
           len(problems) == 1 and "carries ['dfe-ui']" in problems[0], f"got {problems}")


def test_fix_writes_a_chart_digest_into_every_appset_that_pins_it() -> None:
    """culvert's digest sits in both maps: layer2-edge and the bridge in layer2-apps."""
    digest = "sha256:" + "7" * 64
    versions = dict(drift.load_versions())
    versions["chart-digests.culvert"] = digest
    writes, fixed, refused = drift.plan_fix(versions)
    expect("propagation refuses nothing", refused == [], f"{refused}")
    expect("both culvert pins are rewritten",
           len([f for f in fixed if "culvert chart digest" in f]) == 2, f"{fixed}")
    for path in (_APPS_APPSET, _EDGE_APPSET):
        written = drift.chart_pin_map(writes.get(path, ""))
        expect(f"{path} carries the new digest", written.get("culvert") == digest, f"{written}")
    apps = drift.chart_pin_map(writes.get(_APPS_APPSET, ""))
    expect("and no other service's pin moves",
           {k: v for k, v in apps.items() if k != "culvert"}
           == {k: v for k, v in drift.chart_pin_map(drift.read_source(_APPS_APPSET)).items()
               if k != "culvert"},
           f"{apps}")


def test_main_ignores_the_ambient_argv() -> None:
    """main() takes its argv from the caller, so this suite running under the
    test runner's own flags (-q, a file path) never reaches the parser."""
    original = sys.argv
    try:
        sys.argv = ["check_versions_drift.py", "-q", "some/path"]
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            rc = drift.main()
    finally:
        sys.argv = original
    expect("the tree is clean against the default stack", rc == 0, f"got rc={rc}")


def main() -> int:
    with standalone():
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
        return summary()


if __name__ == "__main__":
    sys.exit(main())
