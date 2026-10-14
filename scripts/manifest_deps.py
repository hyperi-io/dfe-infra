#  Project:      dfe-infra
#  File:         scripts/manifest_deps.py
#  Purpose:      The one reader of where a Cargo.toml or pyproject.toml declares
#                a package, and the version range each declaration carries.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""manifest_deps -- find a package's declarations in a dependency manifest.

    from manifest_deps import declarations

    declarations(Path("Cargo.toml").read_text(), "cargo-dep", "scalo")
    # [Declaration(table="dependencies", spec="2.14.9"),
    #  Declaration(table="dev-dependencies", spec="2.14.9")]

A cargo-dep or python-dep edge in suite.yaml cites the consumer's manifest by
file alone, and the declaration is found here by the name the producer
publishes. A line number would move with every dependency added above it, so
the citation would rot on the next bump.

The manifest is read as TOML, so a declaration is found whatever form it takes:
an inline table, a bare string, a `[dependencies.scalo]` table, a renamed
dependency (`alias = { package = "scalo" }`). A table that is not a dependency
table, `[patch.crates-io]` say, is never read as one.

check_suite_drift.py holds the citation to a declaration that exists, and
dfe_suite.kinds answers whether each declared range admits a new version. Two
readers would let the drift check pass an edge the walk then cannot read.

Stdlib only: check_suite_drift.py runs on a bare CI image.
"""

import re
import tomllib
from typing import NamedTuple

PACKAGE_KINDS = ("cargo-dep", "python-dep")

# Cargo still accepts the underscore spellings, so a manifest written with them declares too.
_CARGO_TABLES = (
    "dependencies",
    "dev-dependencies",
    "build-dependencies",
    "dev_dependencies",
    "build_dependencies",
)

# PEP 508: a name, optional extras, then a specifier, a URL or nothing, then an optional marker.
_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?:\[[^\]]*\])?\s*(?P<rest>[^;]*)"
)


class Declaration(NamedTuple):
    """One place a manifest declares a package.

    Attributes:
        table: The dotted TOML table holding it, e.g. ``dev-dependencies`` or
            ``project.optional-dependencies.otel``.
        spec: The version range it declares, or None where it declares none: a
            workspace, path or git dependency, a URL requirement, or a bare name.
    """

    table: str
    spec: str | None


def _cargo_tables(manifest: dict) -> list[tuple[str, dict]]:
    """Every dependency table in a Cargo manifest, as (dotted name, table)."""
    scopes: list[tuple[str, object]] = [("", manifest)]
    targets = manifest.get("target")
    if isinstance(targets, dict):
        scopes += [(f"target.{cfg}.", body) for cfg, body in targets.items()]
    tables = [
        (f"{prefix}{name}", scope[name])
        for prefix, scope in scopes
        if isinstance(scope, dict)
        for name in _CARGO_TABLES
        if isinstance(scope.get(name), dict)
    ]
    workspace = manifest.get("workspace")
    if isinstance(workspace, dict) and isinstance(workspace.get("dependencies"), dict):
        tables.append(("workspace.dependencies", workspace["dependencies"]))
    return tables


def _cargo(manifest: dict, package: str) -> list[Declaration]:
    """The declarations of crate ``package`` in a parsed Cargo.toml."""
    found: list[Declaration] = []
    for table, entries in _cargo_tables(manifest):
        for key, value in entries.items():
            crate = value.get("package", key) if isinstance(value, dict) else key
            if crate != package:
                continue
            if isinstance(value, str):
                spec = value
            elif isinstance(value, dict) and isinstance(value.get("version"), str):
                spec = value["version"]
            else:
                spec = None
            found.append(Declaration(table, spec or None))
    return found


def _normalise(name: str) -> str:
    """A distribution name as PEP 503 compares it."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement(text: str) -> tuple[str, str | None] | None:
    """(name, specifier) for one PEP 508 requirement string, or None if it is not one.

    Args:
        text: The requirement, e.g. ``"scalo[http]>=2.30,<3 ; python_version >= '3.14'"``.

    Returns:
        The distribution name as written and the specifier set, which is None
        for a bare name or a URL requirement.
    """
    match = _REQUIREMENT.match(text)
    if match is None:
        return None
    rest = match.group("rest").strip()
    if rest.startswith("@"):
        return match.group("name"), None
    spec = rest.removeprefix("(").removesuffix(")").strip()
    return match.group("name"), spec or None


def _python_lists(manifest: dict) -> list[tuple[str, object]]:
    """Every requirement list in a parsed pyproject.toml, as (dotted name, list)."""
    project = manifest.get("project")
    project = project if isinstance(project, dict) else {}
    lists: list[tuple[str, object]] = [("project.dependencies", project.get("dependencies"))]
    for section, prefix in (
        (project.get("optional-dependencies"), "project.optional-dependencies"),
        (manifest.get("dependency-groups"), "dependency-groups"),
    ):
        if isinstance(section, dict):
            lists += [(f"{prefix}.{name}", entries) for name, entries in section.items()]
    build = manifest.get("build-system")
    if isinstance(build, dict):
        lists.append(("build-system.requires", build.get("requires")))
    tool = manifest.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    if isinstance(uv, dict):
        lists.append(("tool.uv.dev-dependencies", uv.get("dev-dependencies")))
    return lists


def _python(manifest: dict, package: str) -> list[Declaration]:
    """The declarations of distribution ``package`` in a parsed pyproject.toml."""
    wanted = _normalise(package)
    found: list[Declaration] = []
    for table, entries in _python_lists(manifest):
        if not isinstance(entries, list):
            continue
        # A dependency group may include another group by table; that is not a requirement.
        for entry in entries:
            parsed = requirement(entry) if isinstance(entry, str) else None
            if parsed is not None and _normalise(parsed[0]) == wanted:
                found.append(Declaration(table, parsed[1]))
    return found


def declarations(text: str, kind: str, package: str) -> list[Declaration]:
    """Every declaration of ``package`` in a manifest, in the manifest's table order.

    Args:
        text: The manifest's contents: a Cargo.toml for ``cargo-dep``, a
            pyproject.toml for ``python-dep``.
        kind: The edge kind, one of PACKAGE_KINDS.
        package: The name the producer publishes under.

    Returns:
        One Declaration per place the manifest declares the package; empty when
        it declares it nowhere.

    Raises:
        ValueError: The text is not TOML, or ``kind`` is not a package kind.
    """
    if kind not in PACKAGE_KINDS:
        raise ValueError(f"{kind!r} is not one of {', '.join(PACKAGE_KINDS)}")
    manifest = tomllib.loads(text)
    return _cargo(manifest, package) if kind == "cargo-dep" else _python(manifest, package)
