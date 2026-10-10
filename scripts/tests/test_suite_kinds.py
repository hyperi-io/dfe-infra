"""Tests for dfe_suite.kinds -- one check per suite edge kind.

Real files in temp directories and real git repositories, per the project
policy: nothing is mocked and no module attribute is patched. The two-sided
vendored-file check resolves its other half through ``find_repo``, so those
tests set ``HYPERI_PROJECTS_ROOT`` at the temp root and let the real lookup
run. Nothing here touches the network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from dfe_suite.kinds import (
    ACTION_BUMP_APP,
    ACTION_CONTRACT_TEST,
    ACTION_HUMAN_READ,
    ACTION_MOVE_PIN,
    ACTION_REBUILD_PYTHON,
    ACTION_REBUILD_RUST,
    ACTION_REGENERATE,
    ACTION_REVENDOR,
    ACTION_RUN_TESTS,
    ACTION_WIDEN_RANGE,
    CHECKS,
    KIND_SUMMARY,
    cargo_range,
    check_cargo_dep,
    check_contract_guard,
    check_derived_pins,
    check_edge,
    check_generated_file,
    check_image_pin,
    check_mirrored_logic,
    check_python_dep,
    check_python_dep_undeclared,
    check_vendored_file,
    check_version_pin,
    parse_evidence,
    pep440_admits,
    python_range,
    semver_admits,
)
from dfe_suite.repos import repo_slug

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(repo: Path, *args: str) -> None:
    """Run git in a temp checkout, failing the test on a non-zero exit."""
    env = dict(os.environ)
    env.update(_GIT_ENV)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")


def _set_env(saved: dict[str, str]) -> None:
    """Put the environment back exactly as it was."""
    os.environ.clear()
    os.environ.update(saved)


class EvidenceTests(unittest.TestCase):
    """Evidence is the only thing an edge says about where to look."""

    def test_a_single_line_reference(self) -> None:
        ref = parse_evidence("dfe-loader/Cargo.toml:35")
        assert ref.repo == "dfe-loader"
        assert ref.path == "Cargo.toml"
        assert ref.start == 35
        assert ref.end is None

    def test_a_line_range(self) -> None:
        ref = parse_evidence("dfe-loader/Dockerfile:8-12")
        assert (ref.start, ref.end) == (8, 12)
        assert ref.describe() == "dfe-loader/Dockerfile:8-12"

    def test_a_whole_file_reference(self) -> None:
        ref = parse_evidence("dfe-engine/src/dfe_engine/appmgmt/apps.yaml")
        assert ref.path == "src/dfe_engine/appmgmt/apps.yaml"
        assert ref.start is None

    def test_a_field_holds_exactly_one_reference(self) -> None:
        # Two sides in one field is the older shape; the producer's copy is
        # `source` now, so a comma-joined pair is not a reference at all.
        assert parse_evidence("a-repo/one.json, b-repo/two.json") is None

    def test_junk_is_dropped_rather_than_guessed_at(self) -> None:
        assert parse_evidence("no-slash-here") is None
        assert parse_evidence("") is None


class KindTableTests(unittest.TestCase):
    """A kind with a check and no summary prints nothing under `kinds`."""

    def test_every_check_has_a_summary_and_the_reverse(self) -> None:
        assert set(CHECKS) == set(KIND_SUMMARY)


class SemverMatcherTests(unittest.TestCase):
    """Cargo requirements, which is what every cargo-dep edge cites."""

    def test_a_bounded_range_admits_inside_it(self) -> None:
        assert semver_admits(">=2.10.14, <3", "2.11.0")

    def test_a_bounded_range_rejects_the_next_major(self) -> None:
        assert not semver_admits(">=2.10.14, <3", "3.0.0")

    def test_a_floor_rejects_below_it(self) -> None:
        assert not semver_admits(">=2.10.14, <3", "2.10.13")

    def test_caret_stops_at_the_next_major(self) -> None:
        assert semver_admits("^1.2.3", "1.9.0")
        assert not semver_admits("^1.2.3", "2.0.0")

    def test_caret_below_one_stops_at_the_next_minor(self) -> None:
        assert semver_admits("^0.2.3", "0.2.9")
        assert not semver_admits("^0.2.3", "0.3.0")

    def test_tilde_stops_at_the_next_minor(self) -> None:
        assert semver_admits("~1.2.3", "1.2.9")
        assert not semver_admits("~1.2.3", "1.3.0")

    def test_exact_admits_only_itself(self) -> None:
        assert semver_admits("=1.2.3", "1.2.3")
        assert not semver_admits("=1.2.3", "1.2.4")

    def test_a_bare_version_is_a_caret_range(self) -> None:
        assert semver_admits("1.2.3", "1.9.0")
        assert not semver_admits("1.2.3", "2.0.0")

    def test_a_wildcard_bounds_the_component_below_it(self) -> None:
        assert semver_admits("1.2.*", "1.2.9")
        assert not semver_admits("1.2.*", "1.3.0")
        assert semver_admits("*", "7.0.0")

    def test_a_pre_release_target_is_not_answered_by_a_plain_range(self) -> None:
        # Cargo excludes pre-releases from a plain range, so reading the
        # candidate as its release would move a consumer onto an rc.
        assert semver_admits(">=2.10.14, <3", "2.11.0-rc1") is None
        assert semver_admits(">=2.10.14", "2.10.14-rc.1") is None
        assert semver_admits("^1.2.3", "1.2.3-beta") is None

    def test_a_range_that_names_a_pre_release_answers_normally(self) -> None:
        assert semver_admits(">=2.10, <3.0.0-0", "2.11.0")

    def test_build_metadata_is_not_a_pre_release(self) -> None:
        assert semver_admits(">=2.10.14, <3", "2.11.0+build7")

    def test_an_unreadable_requirement_is_not_a_verdict(self) -> None:
        # None means "this matcher does not understand it", never "no".
        assert semver_admits("not a range", "1.0.0") is None
        assert semver_admits(">=1.0.0", "not a version") is None
        assert semver_admits("", "1.0.0") is None


class Pep440MatcherTests(unittest.TestCase):
    """PEP 440 specifier sets, which is what every python-dep edge cites."""

    def test_a_floor_admits_above_it(self) -> None:
        assert pep440_admits(">=2.29.7", "2.29.20")
        assert not pep440_admits(">=2.29.7", "2.29.6")

    def test_a_bounded_set_rejects_the_next_major(self) -> None:
        assert pep440_admits(">=2.29.14,<3", "2.29.20")
        assert not pep440_admits(">=2.29.14,<3", "3.0.0")

    def test_exact_admits_only_itself(self) -> None:
        assert pep440_admits("==1.2.3", "1.2.3")
        assert not pep440_admits("==1.2.3", "1.2.4")

    def test_exclusion_rejects_only_itself(self) -> None:
        assert not pep440_admits("!=1.2.3", "1.2.3")
        assert pep440_admits("!=1.2.3", "1.2.4")

    def test_compatible_release_fixes_the_last_but_one_component(self) -> None:
        assert pep440_admits("~=1.2.3", "1.2.9")
        assert not pep440_admits("~=1.2.3", "1.3.0")
        assert pep440_admits("~=1.2", "1.9.0")
        assert not pep440_admits("~=1.2", "2.0.0")

    def test_a_prefix_match_bounds_the_component_below_it(self) -> None:
        assert pep440_admits("==1.2.*", "1.2.9")
        assert not pep440_admits("==1.2.*", "1.3.0")

    def test_a_bare_version_is_not_a_specifier(self) -> None:
        # Unlike Cargo, PEP 440 has no bare form, so it is undetermined.
        assert pep440_admits("1.2.3", "1.2.3") is None

    def test_a_single_component_compatible_release_is_refused(self) -> None:
        assert pep440_admits("~=1", "1.5.0") is None

    def test_an_excluding_prefix_match_rejects_the_whole_window(self) -> None:
        assert not pep440_admits("!=1.2.*", "1.2.9")
        assert pep440_admits("!=1.2.*", "1.3.0")

    def test_a_post_or_dev_release_orders_with_its_release(self) -> None:
        assert pep440_admits(">=1.0,<2", "1.0.0.post1")
        assert pep440_admits(">=1.0,<2", "1.0.0.dev3")
        assert not pep440_admits(">=1.0,<2", "2.0.0.post1")

    def test_a_pep440_pre_release_with_no_separator_is_undetermined(self) -> None:
        # `1.0.0rc1` is not a release segment this reads, and PEP 440 excludes
        # it from a plain specifier anyway.
        assert pep440_admits(">=1.0", "1.0.0rc1") is None


class RangeReaderTests(unittest.TestCase):
    """A cited line carries several dependencies; only the producer's counts."""

    def test_a_cargo_table_line_declaring_the_package(self) -> None:
        line = 'scalo = { version = ">=2.10.14, <3", features = ["a"] }'
        assert cargo_range(line, "scalo") == ">=2.10.14, <3"

    def test_a_cargo_line_declaring_something_else_is_not_read(self) -> None:
        line = 'tokio = { version = ">=1.48, <2", features = ["full"] }'
        assert cargo_range(line, "scalo") is None

    def test_a_cargo_bare_string_dependency(self) -> None:
        assert cargo_range('scalo = ">=2.10.14, <3"', "scalo") == ">=2.10.14, <3"

    def test_cargo_workspace_git_and_path_forms(self) -> None:
        assert cargo_range("scalo = { workspace = true }", "scalo") is None
        assert cargo_range("scalo.workspace = true", "scalo") is None
        assert cargo_range('scalo = { git = "https://x", tag = "v2.10.14" }', "scalo") is None
        assert cargo_range('scalo = { path = "../scalo", version = ">=2" }', "scalo") == ">=2"

    def test_a_commented_out_cargo_line_declares_nothing(self) -> None:
        assert cargo_range('# scalo = { version = ">=1.0" }', "scalo") is None

    def test_a_python_requirement_with_extras(self) -> None:
        line = '    "scalo[expression,http,metrics]>=2.30.0",'
        assert python_range(line, "scalo") == ">=2.30.0"

    def test_a_python_requirement_naming_another_distribution(self) -> None:
        assert python_range('    "dfe-schemas>=0.2.0",', "scalo") is None
        assert python_range('    "dfe-schemas>=0.2.0",', "dfe-schemas") == ">=0.2.0"

    def test_a_python_prefix_is_not_a_name_match(self) -> None:
        assert python_range('    "scalo-extra>=1.0",', "scalo") is None

    def test_a_python_requirement_with_a_space_or_a_marker(self) -> None:
        assert python_range('    "scalo >= 2.30.0",', "scalo") == ">= 2.30.0"
        assert python_range("    \"scalo>=2; python_version>='3.12'\",", "scalo") == ">=2"

    def test_a_python_declaration_with_no_range(self) -> None:
        assert python_range('    "scalo",', "scalo") is None

    def test_a_commented_out_python_requirement_declares_nothing(self) -> None:
        assert python_range('    # "scalo>=2.30.0" is deliberate', "scalo") is None

    def test_an_unrelated_quoted_string_is_not_a_requirement(self) -> None:
        assert python_range('requires-python = ">=3.12"', "scalo") is None


class CargoDepTests(unittest.TestCase):
    """The range at the cited line is what decides the action."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="suite-kinds-cargo-")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "dfe-loader"
        self.repo.mkdir()

    def _manifest(self, dependency: str) -> dict:
        (self.repo / "Cargo.toml").write_text(
            f'[package]\nname = "dfe-loader"\n\n[dependencies]\ntokio = "1"\n'
            f"{dependency}\n",
            encoding="utf-8",
            newline="\n",
        )
        return {
            "from": "scalo-rs",
            "to": "dfe-loader",
            "kind": "cargo-dep",
            "type": "potential",
            "evidence": "dfe-loader/Cargo.toml:6",
        }

    def test_a_range_that_admits_asks_for_a_rebuild(self) -> None:
        edge = self._manifest(
            'scalo = { version = ">=2.10.14, <3", features = ["config"] }'
        )
        result = check_cargo_dep(
            edge, producer_version="2.11.0", consumer_repo=self.repo, package="scalo"
        )
        assert result.moved
        assert result.action == ACTION_REBUILD_RUST
        assert ">=2.10.14, <3" in result.detail

    def test_a_range_that_does_not_admit_asks_for_a_wider_one(self) -> None:
        edge = self._manifest('scalo = { version = ">=2.10.14, <3" }')
        result = check_cargo_dep(
            edge, producer_version="3.0.0", consumer_repo=self.repo, package="scalo"
        )
        assert result.action == ACTION_WIDEN_RANGE
        # The detail has to name the line, or the finding is unactionable.
        assert "dfe-loader/Cargo.toml:6" in result.detail

    def test_a_bare_string_dependency_is_read_the_same_way(self) -> None:
        edge = self._manifest('scalo = ">=2.10.14, <3"')
        result = check_cargo_dep(
            edge, producer_version="2.11.0", consumer_repo=self.repo, package="scalo"
        )
        assert result.action == ACTION_REBUILD_RUST

    def test_the_edges_note_rides_along(self) -> None:
        edge = self._manifest('scalo = ">=2.10.14, <3"')
        edge["note"] = "A second range sits at Cargo.toml:180."
        result = check_cargo_dep(
            edge, producer_version="2.11.0", consumer_repo=self.repo, package="scalo"
        )
        assert "Cargo.toml:180" in result.detail

    def test_a_line_with_no_range_is_undetermined(self) -> None:
        edge = self._manifest("# scalo is inherited from the workspace")
        result = check_cargo_dep(
            edge, producer_version="2.11.0", consumer_repo=self.repo, package="scalo"
        )
        assert result.moved is None
        assert result.action == ACTION_HUMAN_READ

    def test_a_line_declaring_a_NEIGHBOURING_crate_is_not_read_as_ours(self) -> None:
        # The line has a perfectly good range on it; it is just not scalo's.
        edge = self._manifest('tokio-util = { version = ">=0.7, <1" }')
        result = check_cargo_dep(
            edge, producer_version="2.11.0", consumer_repo=self.repo, package="scalo"
        )
        assert result.moved is None
        assert result.action == ACTION_HUMAN_READ
        assert "no version range for scalo" in result.detail

    def test_a_producer_with_no_package_in_the_graph_says_so(self) -> None:
        edge = self._manifest('scalo = ">=2.10.14, <3"')
        result = check_cargo_dep(
            edge, producer_version="2.11.0", consumer_repo=self.repo, package=None
        )
        assert result.moved is None
        assert result.action == ACTION_HUMAN_READ
        assert "declares no `package`" in result.detail


class PythonDepTests(unittest.TestCase):
    """Extras sit between the name and the specifier, so the parse must skip them."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="suite-kinds-py-")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "dfe-engine"
        self.repo.mkdir()

    def _pyproject(self, requirement: str) -> dict:
        (self.repo / "pyproject.toml").write_text(
            f'[project]\nname = "dfe-engine"\ndependencies = [\n'
            f'    "httpx>=0.27",\n    {requirement},\n]\n',
            encoding="utf-8",
            newline="\n",
        )
        return {
            "from": "scalo-py",
            "to": "dfe-engine",
            "kind": "python-dep",
            "type": "potential",
            "evidence": "dfe-engine/pyproject.toml:5",
        }

    def test_a_specifier_that_admits_asks_for_a_rebuild(self) -> None:
        edge = self._pyproject('"scalo[expression,http]>=2.29.7"')
        result = check_python_dep(
            edge, producer_version="2.29.20", consumer_repo=self.repo, package="scalo"
        )
        assert result.moved
        assert result.action == ACTION_REBUILD_PYTHON
        assert ">=2.29.7" in result.detail

    def test_a_specifier_that_does_not_admit_asks_for_a_wider_one(self) -> None:
        edge = self._pyproject('"scalo>=2.30.0"')
        result = check_python_dep(
            edge, producer_version="2.29.20", consumer_repo=self.repo, package="scalo"
        )
        assert result.action == ACTION_WIDEN_RANGE
        assert "dfe-engine/pyproject.toml:5" in result.detail

    def test_an_environment_marker_does_not_break_the_parse(self) -> None:
        edge = self._pyproject("\"scalo>=2.29.7 ; python_version >= '3.12'\"")
        result = check_python_dep(
            edge, producer_version="2.29.20", consumer_repo=self.repo, package="scalo"
        )
        assert result.action == ACTION_REBUILD_PYTHON

    def test_a_line_declaring_a_NEIGHBOURING_distribution_is_not_ours(self) -> None:
        edge = self._pyproject('"dfe-schemas>=0.2.0"')
        result = check_python_dep(
            edge, producer_version="2.29.20", consumer_repo=self.repo, package="scalo"
        )
        assert result.moved is None
        assert "no version range for scalo" in result.detail

    def test_a_missing_file_is_a_verdict_not_a_crash(self) -> None:
        edge = {
            "from": "scalo-py",
            "to": "dfe-engine",
            "kind": "python-dep",
            "evidence": "dfe-engine/pyproject.toml:5",
        }
        result = check_edge(
            edge,
            producer_version="2.29.20",
            consumer_repo=self.repo,
            package="scalo",
        )
        assert result.moved is None
        assert "does not exist" in result.detail


class UndeclaredDependencyTests(unittest.TestCase):
    """No range exists, so only running the named import sites answers it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="suite-kinds-undeclared-")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)

    def test_the_import_site_is_handed_back_with_the_version_to_install(self) -> None:
        edge = {
            "from": "logreducer",
            "to": "dfe-engine",
            "kind": "python-dep-undeclared",
            "type": "potential",
            "evidence": "dfe-engine/src/dfe_engine/sampling/service.py:200",
            "note": "Also imported at service.py:236.",
        }
        result = check_python_dep_undeclared(
            edge, producer_version="1.4.0", consumer_repo=self.repo
        )
        assert result.moved is None
        assert result.action == ACTION_RUN_TESTS
        assert "service.py:200" in result.detail
        assert "1.4.0" in result.detail
        assert "service.py:236" in result.detail


class GeneratedFileTests(unittest.TestCase):
    """The header states what emitted the file; both branches of that read."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="suite-kinds-generated-")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "dfe-loader"
        self.repo.mkdir()

    def _dockerfile(self, header: str) -> dict:
        (self.repo / "Dockerfile").write_text(
            f"# line one\n{header}\nFROM scratch\n", encoding="utf-8", newline="\n"
        )
        return {
            "from": "scalo-rs",
            "to": "dfe-loader",
            "kind": "generated-file",
            "type": "lockstep",
            "evidence": "dfe-loader/Dockerfile:2",
        }

    def test_a_header_with_a_schema_version_and_a_command(self) -> None:
        edge = self._dockerfile(
            "# schema version: 3\n# regenerate with: `cargo run -- emit-dockerfile`"
        )
        edge["evidence"] = "dfe-loader/Dockerfile:2-3"
        result = check_generated_file(
            edge, producer_version="2.11.0", consumer_repo=self.repo
        )
        assert result.moved is None
        assert result.action == ACTION_REGENERATE
        assert "schema version 3" in result.detail
        assert "cargo run -- emit-dockerfile" in result.detail

    def test_a_header_with_neither_says_so_rather_than_inventing_one(self) -> None:
        edge = self._dockerfile("# generated, do not edit")
        result = check_generated_file(
            edge, producer_version="2.11.0", consumer_repo=self.repo
        )
        assert "no schema version" in result.detail
        assert "its own header names" in result.detail


class ContractGuardTests(unittest.TestCase):
    """The command handed back is the consumer's own, per ecosystem."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="suite-kinds-contract-")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "dfe-engine"
        self.repo.mkdir()
        (self.repo / "guard.py").write_text("x = 1\n", encoding="utf-8", newline="\n")

    def _edge(self) -> dict:
        return {
            "from": "scalo-py",
            "to": "dfe-engine",
            "kind": "contract-guard",
            "type": "lockstep",
            "evidence": "dfe-engine/guard.py:1",
        }

    def _run(self) -> str:
        return check_contract_guard(
            self._edge(), producer_version="2.30.0", consumer_repo=self.repo
        ).detail

    def test_a_rust_consumer_gets_nextest(self) -> None:
        (self.repo / "Cargo.toml").write_text(
            "[package]\n", encoding="utf-8", newline="\n"
        )
        assert "cargo nextest run" in self._run()

    def test_a_python_consumer_gets_the_ci_check(self) -> None:
        (self.repo / "pyproject.toml").write_text(
            "[project]\n", encoding="utf-8", newline="\n"
        )
        assert "hyperi-ci check" in self._run()

    def test_a_consumer_that_is_neither_is_told_to_run_its_own(self) -> None:
        assert "the consumer's own contract test" in self._run()

    def test_the_action_is_the_contract_test_whichever_it_is(self) -> None:
        result = check_contract_guard(
            self._edge(), producer_version="2.30.0", consumer_repo=self.repo
        )
        assert result.moved is None
        assert result.action == ACTION_CONTRACT_TEST


class PinTests(unittest.TestCase):
    """The two pin kinds move in dfe-infra, so they hand back a command."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="suite-kinds-pins-")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "dfe-infra"
        self.repo.mkdir()
        (self.repo / "Chart.yaml").write_text(
            "a\nb\nc\nd\ne\nappVersion: v1\n", encoding="utf-8", newline="\n"
        )

    def test_an_image_pin_hands_back_bump_app_naming_the_app(self) -> None:
        edge = {
            "from": "dfe-engine",
            "to": "dfe-infra",
            "kind": "image-pin",
            "type": "lockstep",
            "evidence": "dfe-infra/Chart.yaml:6",
        }
        result = check_image_pin(
            edge, producer_version="2.5.0", consumer_repo=self.repo
        )
        assert result.moved
        assert result.action == ACTION_BUMP_APP
        assert "dfe-stack bump-app dfe-engine 2.5.0" in result.detail

    def test_a_version_pin_hands_back_the_pin_to_move(self) -> None:
        edge = {
            "from": "dfe-schemas",
            "to": "dfe-infra",
            "kind": "version-pin",
            "type": "lockstep",
            "evidence": "dfe-infra/Chart.yaml:6",
        }
        result = check_version_pin(
            edge, producer_version="0.3.0", consumer_repo=self.repo
        )
        assert result.moved
        assert result.action == ACTION_MOVE_PIN
        assert "dfe-infra/Chart.yaml:6" in result.detail
        assert "0.3.0" in result.detail


class VendoredFileTests(unittest.TestCase):
    """Two committed copies, compared by the hash git itself would give them."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="suite-kinds-vendor-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.addCleanup(_set_env, dict(os.environ))
        # The producer half resolves through find_repo, so point the real
        # lookup at this temp root rather than the developer's projects tree.
        os.environ["HYPERI_PROJECTS_ROOT"] = str(self.root)

        self.producer = self._repo("suite-producer-xyzzy")
        self.consumer = self._repo("suite-consumer-xyzzy")

    def _repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        return repo

    def _edge(self) -> dict:
        return {
            "from": "suite-producer-xyzzy",
            "to": "suite-consumer-xyzzy",
            "kind": "vendored-file",
            "type": "lockstep",
            "evidence": "suite-consumer-xyzzy/apps.yaml",
            "source": "suite-producer-xyzzy/apps.yaml",
        }

    def _write(self, repo: Path, body: str) -> None:
        (repo / "apps.yaml").write_text(body, encoding="utf-8", newline="\n")

    def test_equal_copies_have_nothing_to_move(self) -> None:
        self._write(self.producer, "apps:\n  - one\n")
        self._write(self.consumer, "apps:\n  - one\n")
        result = check_vendored_file(
            self._edge(), producer_version="1.0.0", consumer_repo=self.consumer
        )
        assert not result.moved
        assert result.action is None
        assert "equal" in result.detail

    def test_unequal_copies_ask_to_be_re_vendored(self) -> None:
        self._write(self.producer, "apps:\n  - one\n  - two\n")
        self._write(self.consumer, "apps:\n  - one\n")
        result = check_vendored_file(
            self._edge(), producer_version="1.0.0", consumer_repo=self.consumer
        )
        assert result.moved
        assert result.action == ACTION_REVENDOR
        assert "differ" in result.detail

    def test_the_consumer_copy_is_the_one_in_evidence(self) -> None:
        self._write(self.producer, "apps:\n  - one\n  - two\n")
        self._write(self.consumer, "apps:\n  - one\n")
        result = check_vendored_file(
            self._edge(), producer_version="1.0.0", consumer_repo=self.consumer
        )
        shown = result.detail.split(":")[0]
        assert shown.startswith("suite-consumer-xyzzy/apps.yaml vs"), shown

    def test_no_source_means_there_is_nothing_to_compare_against(self) -> None:
        self._write(self.consumer, "apps:\n  - one\n")
        edge = self._edge()
        del edge["source"]
        result = check_vendored_file(
            edge, producer_version="1.0.0", consumer_repo=self.consumer
        )
        assert result.moved is None
        assert result.action == ACTION_HUMAN_READ
        assert "only copy cited" in result.detail

    def test_two_sides_crammed_into_evidence_is_no_longer_a_citation(self) -> None:
        self._write(self.consumer, "apps:\n  - one\n")
        edge = self._edge()
        edge["evidence"] = (
            "suite-consumer-xyzzy/apps.yaml, suite-producer-xyzzy/apps.yaml"
        )
        result = check_edge(edge, producer_version="1.0.0", consumer_repo=self.consumer)
        assert result.moved is None
        assert "cites no usable evidence" in result.detail


class HoistedHelperTests(unittest.TestCase):
    """The hoisted slug helper, which answers without a toolchain behind it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="suite-hoisted-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "suite-consumer-xyzzy"
        self.repo.mkdir()

    def _gh(self, body: str) -> None:
        """A real gh on a scratch PATH, so the lookup runs for real."""
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text(body, encoding="utf-8", newline="\n")
        gh.chmod(0o755)
        saved = dict(os.environ)
        self.addCleanup(_set_env, saved)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

    def test_the_slug_comes_from_the_remote_when_gh_can_read_it(self) -> None:
        self._gh("#!/bin/sh\necho 'someone-else/renamed'\n")
        assert repo_slug(self.repo, "hyperi-io") == "someone-else/renamed"

    def test_a_failing_gh_falls_back_to_the_org_and_the_directory_name(self) -> None:
        self._gh("#!/bin/sh\necho 'not logged in' >&2\nexit 1\n")
        assert repo_slug(self.repo, "hyperi-io") == "hyperi-io/suite-consumer-xyzzy"


class WalkPastTests(unittest.TestCase):
    """The two kinds that are answered without reading anything."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="suite-kinds-walk-")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)

    def test_derived_pins_moves_nothing_and_asks_for_nothing(self) -> None:
        edge = {
            "from": "dfe-infra",
            "to": "dfe-docker",
            "kind": "derived-pins",
            "type": "derived",
            "evidence": "dfe-docker/scripts/stack.py:17-28",
        }
        result = check_derived_pins(
            edge, producer_version="9.9.9", consumer_repo=self.repo
        )
        assert not result.moved
        assert result.action is None
        assert "goes past this edge" in result.detail

    def test_mirrored_logic_is_handed_to_a_person(self) -> None:
        edge = {
            "from": "dfe-loader",
            "to": "dfe-engine",
            "kind": "mirrored-logic",
            "type": "potential",
            "evidence": "dfe-engine/src/dfe_engine/plugins/loader.py:36",
            "note": "Mirrors the loader's own config validation.",
        }
        result = check_mirrored_logic(
            edge, producer_version="4.5.6", consumer_repo=self.repo
        )
        assert result.moved is None
        assert result.action == ACTION_HUMAN_READ
        assert "loader.py:36" in result.detail
        assert "config validation" in result.detail

    def test_an_unknown_kind_is_reported_not_guessed(self) -> None:
        result = check_edge(
            {"from": "a", "to": "b", "kind": "brand-new-kind"},
            producer_version="1.0.0",
            consumer_repo=self.repo,
        )
        assert result.moved is None
        assert "brand-new-kind" in result.detail


if __name__ == "__main__":
    unittest.main()
