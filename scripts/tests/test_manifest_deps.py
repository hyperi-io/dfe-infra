#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         test_manifest_deps.py
#  Purpose:      Prove scripts/manifest_deps.py finds every declaration of a
#                package in a Cargo.toml or pyproject.toml, in every form those
#                manifests write one, and nothing that only looks like one,
#                and the versions a Cargo.lock or uv.lock resolves for one.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tests for scripts/manifest_deps.py.

    python3 -m pytest scripts/tests/test_manifest_deps.py -q
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from manifest_deps import LOCK_FILES, PACKAGE_KINDS, Declaration, declarations, locked, requirement


def _cargo(body: str) -> list[Declaration]:
    return declarations(f'[package]\nname = "app"\n\n{body}', "cargo-dep", "scalo")


def _python(body: str, package: str = "scalo") -> list[Declaration]:
    return declarations(f'[project]\nname = "app"\n{body}', "python-dep", package)


# --------------------------------------------------------------------- cargo


def test_an_inline_table_declaration_carries_its_version() -> None:
    found = _cargo('[dependencies]\nscalo = { version = ">=2.10.14, <3", features = ["a"] }\n')
    assert found == [Declaration("dependencies", ">=2.10.14, <3")]


def test_a_bare_string_declaration_is_its_version() -> None:
    assert _cargo('[dependencies]\nscalo = ">=2.10.14, <3"\n') == [
        Declaration("dependencies", ">=2.10.14, <3")
    ]


def test_an_inline_table_spread_over_lines_is_read_whole() -> None:
    body = '[dependencies]\nscalo = { features = [\n    "a",\n    "b",\n], version = "2.14.9" }\n'
    assert _cargo(body) == [Declaration("dependencies", "2.14.9")]


def test_a_dependency_table_of_its_own_is_a_declaration() -> None:
    assert _cargo('[dependencies.scalo]\nversion = "2.14.9"\n') == [
        Declaration("dependencies", "2.14.9")
    ]


def test_every_dependency_table_is_read_in_order() -> None:
    body = (
        '[dependencies]\nscalo = "2.14.9"\n\n'
        '[dev-dependencies]\nscalo = { version = "2.14.9", features = ["x"] }\n\n'
        '[build-dependencies]\nscalo = "2.14.9"\n\n'
        "[target.'cfg(unix)'.dependencies]\nscalo = \"2.14.9\"\n\n"
        '[workspace.dependencies]\nscalo = { version = ">=2.14.9, <3" }\n'
    )
    assert [d.table for d in _cargo(body)] == [
        "dependencies",
        "dev-dependencies",
        "build-dependencies",
        "target.cfg(unix).dependencies",
        "workspace.dependencies",
    ]


def test_a_renamed_dependency_is_read_by_the_package_it_names() -> None:
    body = '[dependencies]\nruntime = { package = "scalo", version = "2.14.9" }\n'
    assert _cargo(body) == [Declaration("dependencies", "2.14.9")]


def test_a_key_renamed_to_another_package_is_not_ours() -> None:
    assert _cargo('[dependencies]\nscalo = { package = "scalo-fork", version = "1" }\n') == []


@pytest.mark.parametrize(
    "value",
    ["{ workspace = true }", '{ git = "https://example.com/scalo", tag = "v2" }', '{ path = "../scalo" }'],
)
def test_a_declaration_with_no_version_carries_no_range(value: str) -> None:
    assert _cargo(f"[dependencies]\nscalo = {value}\n") == [Declaration("dependencies", None)]


def test_a_dotted_workspace_key_is_a_declaration_with_no_range() -> None:
    assert _cargo("[dependencies]\nscalo.workspace = true\n") == [
        Declaration("dependencies", None)
    ]


def test_a_path_dependency_with_a_version_carries_it() -> None:
    assert _cargo('[dependencies]\nscalo = { path = "../scalo", version = ">=2" }\n') == [
        Declaration("dependencies", ">=2")
    ]


def test_a_patch_and_a_feature_naming_the_crate_are_not_declarations() -> None:
    body = (
        '[patch.crates-io]\nscalo = { git = "https://example.com/scalo" }\n\n'
        '[features]\nkafka = ["scalo/transport-kafka"]\n'
    )
    assert _cargo(body) == []


def test_a_commented_out_declaration_declares_nothing() -> None:
    assert _cargo('[dependencies]\n# scalo = { version = ">=1.0" }\n') == []


def test_a_neighbouring_crate_is_not_read_as_ours() -> None:
    assert _cargo('[dependencies]\nscalo-extra = "1"\ntokio-util = ">=0.7, <1"\n') == []


# -------------------------------------------------------------------- python


def test_a_requirement_with_extras_carries_its_specifier() -> None:
    found = _python('dependencies = [\n    "httpx>=0.27",\n    "scalo[expression,http]>=2.30.0,<3",\n]\n')
    assert found == [Declaration("project.dependencies", ">=2.30.0,<3")]


def test_every_requirement_list_is_read() -> None:
    body = (
        'dependencies = ["scalo>=2.31.9,<3"]\n\n'
        '[project.optional-dependencies]\notel = ["scalo[opentelemetry]"]\n\n'
        '[dependency-groups]\ndev = [{ include-group = "lint" }, "scalo[deployment]>=2.31.9,<3"]\n'
        'lint = ["ruff>=0.17"]\n\n'
        '[build-system]\nrequires = ["scalo>=2"]\n'
    )
    assert _python(body) == [
        Declaration("project.dependencies", ">=2.31.9,<3"),
        Declaration("project.optional-dependencies.otel", None),
        Declaration("dependency-groups.dev", ">=2.31.9,<3"),
        Declaration("build-system.requires", ">=2"),
    ]


def test_another_distribution_is_not_ours() -> None:
    body = 'dependencies = ["dfe-schemas>=0.2.0", "scalo-extra>=1.0"]\n'
    assert _python(body) == []
    assert _python(body, "dfe-schemas") == [Declaration("project.dependencies", ">=0.2.0")]


def test_names_compare_the_way_pep_503_normalises_them() -> None:
    assert _python('dependencies = ["DFE_Schemas>=0.2.0"]\n', "dfe-schemas") == [
        Declaration("project.dependencies", ">=0.2.0")
    ]


def test_a_commented_out_requirement_and_an_unrelated_string_are_not_declarations() -> None:
    body = 'requires-python = ">=3.14"\ndependencies = [\n    # "scalo>=2.30.0" is deliberate\n]\n'
    assert _python(body) == []


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("scalo", ("scalo", None)),
        ("scalo >= 2.30.0", ("scalo", ">= 2.30.0")),
        ("scalo>=2; python_version>='3.12'", ("scalo", ">=2")),
        ("scalo[http] (>=2.30)", ("scalo", ">=2.30")),
        ("scalo @ https://example.com/scalo.whl", ("scalo", None)),
        ("  ", None),
    ],
)
def test_a_requirement_string_reads_as_pep_508_does(text: str, parsed: object) -> None:
    assert requirement(text) == parsed


# --------------------------------------------------------------------- errors


def test_a_manifest_that_is_not_toml_raises_a_value_error() -> None:
    with pytest.raises(ValueError, match="line 1"):
        declarations("[dependencies\nscalo = ", "cargo-dep", "scalo")


def test_a_kind_that_cites_no_declaration_is_refused() -> None:
    with pytest.raises(ValueError, match="mirrored-logic"):
        declarations("", "mirrored-logic", "scalo")


def test_an_empty_manifest_declares_nothing() -> None:
    assert declarations("", "cargo-dep", "scalo") == []
    assert declarations("", "python-dep", "scalo") == []


# ---------------------------------------------------------------------- locks

CARGO_LOCK = """\
version = 4

[[package]]
name = "app"
version = "0.1.0"

[[package]]
name = "scalo"
version = "2.14.9"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "scalo"
version = "2.15.0"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "scalo-macros"
version = "9.9.9"
"""

UV_LOCK = """\
version = 1
requires-python = ">=3.14"

[[package]]
name = "app"
version = "0.1.0"
source = { editable = "." }

[[package]]
name = "dfe-schemas"
version = "0.2.11"
source = { registry = "https://pypi.org/simple" }
"""


def test_a_cargo_lock_lists_every_resolved_copy_in_order() -> None:
    assert locked(CARGO_LOCK, "cargo-dep", "scalo") == ["2.14.9", "2.15.0"]


def test_a_neighbouring_crate_is_not_a_resolved_copy() -> None:
    assert locked(CARGO_LOCK, "cargo-dep", "scalo-macro") == []
    assert locked(CARGO_LOCK, "cargo-dep", "factbook") == []


def test_a_uv_lock_names_compare_the_way_pep_503_normalises_them() -> None:
    assert locked(UV_LOCK, "python-dep", "dfe_schemas") == ["0.2.11"]
    assert locked(UV_LOCK, "python-dep", "DFE.Schemas") == ["0.2.11"]


def test_a_crate_name_compares_exactly() -> None:
    lock = '[[package]]\nname = "dfe-clickhouse"\nversion = "0.1.4"\n'
    assert locked(lock, "cargo-dep", "dfe-clickhouse") == ["0.1.4"]
    assert locked(lock, "cargo-dep", "dfe_clickhouse") == []


def test_a_lock_with_no_packages_resolves_nothing() -> None:
    assert locked("", "cargo-dep", "scalo") == []
    assert locked("version = 1\n", "python-dep", "scalo") == []


def test_a_lock_that_is_not_toml_raises_a_value_error() -> None:
    with pytest.raises(ValueError, match="line 1"):
        locked("[[package\nname = ", "cargo-dep", "scalo")


def test_a_kind_that_has_no_lock_is_refused() -> None:
    with pytest.raises(ValueError, match="mirrored-logic"):
        locked("", "mirrored-logic", "scalo")


def test_each_package_kind_names_the_lock_it_resolves_into() -> None:
    assert LOCK_FILES == {"cargo-dep": "Cargo.lock", "python-dep": "uv.lock"}
    assert set(LOCK_FILES) == set(PACKAGE_KINDS)
