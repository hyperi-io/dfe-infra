#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         composition.py
#  Purpose:      Derive the DEFAULT COMPOSITION -- which apps a profile deploys
#                when nobody says otherwise -- from apps.yaml, and generate the
#                seeded app set each profile's values file carries. Stdlib only.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""composition -- what a profile deploys by default, read from apps.yaml.

`default_in` on each app in apps.yaml is the only place the default composition
is written down. Everything that has to know it -- the deploy repo's seeded app
set, the Compose projection, the composition table in the docs -- derives it
from here rather than restating a list per tier, which is how slim came to omit
the archiver with nothing recording why.

    import composition

    composition.default_apps("slim")       # ('dfe-engine', 'dfe-loader', ...)
    composition.offered_in("mesh")         # everything that MAY run there
    composition.idle_when("dfe-archiver")  # the config paths meaning "no work"

The seeded app set is written into each Kubernetes profile's values file as a
GENERATED block, because Helm cannot read apps.yaml:

    python3 scripts/composition.py --write-seed

`scripts/tests/test_composition.py` fails when a committed block and this
derivation disagree, so the generated lists cannot quietly drift.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import profiles

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "apps.yaml"

# Delimiters around the generated block in each profile values file.
SEED_BEGIN = "# BEGIN seeded apps -- rendered by `python3 scripts/composition.py --write-seed`\n"
SEED_END = "# END seeded apps\n"
SEED_BANNER = (
    "# The apps this tier deploys by default. Derived from apps.yaml's\n"
    "# `default_in`, which is where the default composition is declared; edit\n"
    "# the manifest, then re-render. A Helm list is replaced rather than merged,\n"
    "# so this states the whole set.\n"
)


class CompositionError(Exception):
    """Raised when the manifest is missing, malformed, or names a bad profile."""


def _apps() -> dict[str, dict]:
    """The manifest's `apps:` mapping.

    Returns:
        App name -> its declaration.

    Raises:
        CompositionError: The manifest is missing, unreadable, or declares no
            apps.
    """
    if not MANIFEST.is_file():
        raise CompositionError(f"{MANIFEST} not found")
    try:
        from ruamel.yaml import YAML
    except ImportError as error:  # pragma: no cover - runner dependency
        raise CompositionError(
            "ruamel.yaml is required to read apps.yaml (scripts/tests/requirements-ci.txt pins it)"
        ) from error
    doc = YAML(typ="safe").load(MANIFEST.read_text(encoding="utf-8", errors="replace"))
    apps = (doc or {}).get("apps")
    if not isinstance(apps, dict) or not apps:
        raise CompositionError(f"{MANIFEST} declares no apps")
    return apps


def _key(app: dict, name: str) -> list[str] | None:
    """One composition key, or None when the app does not declare it."""
    value = app.get(name)
    if value is None:
        return None
    if not isinstance(value, list):
        raise CompositionError(f"{name} must be a list, got {value!r}")
    return [str(v) for v in value]


def offered_in(profile: str) -> tuple[str, ...]:
    """Every app that MAY be deployed in a profile.

    Args:
        profile: One of `profiles.PROFILE_NAMES`.

    Returns:
        App names, in manifest order.

    Raises:
        CompositionError: The profile is not a declared one.
    """
    _check(profile)
    return tuple(
        name
        for name, app in _apps().items()
        if _key(app or {}, "profiles") is None or profile in _key(app, "profiles")
    )


def default_apps(profile: str) -> tuple[str, ...]:
    """The apps a profile deploys when nobody says otherwise.

    Args:
        profile: One of `profiles.PROFILE_NAMES`.

    Returns:
        App names, sorted, which is the order the seed writes them in.

    Raises:
        CompositionError: The profile is not a declared one.
    """
    _check(profile)
    chosen = []
    for name, raw in _apps().items():
        app = raw or {}
        offered = _key(app, "profiles")
        if offered is not None and profile not in offered:
            continue
        default = _key(app, "default_in")
        # Absent means "wherever it is offered".
        if default is None or profile in default:
            chosen.append(name)
    return tuple(sorted(chosen))


def idle_when(app: str) -> tuple[str, ...]:
    """The config paths whose emptiness means the app has no work.

    Args:
        app: An app name from the manifest.

    Returns:
        Dot-paths, in manifest order; empty when the app always has work.

    Raises:
        CompositionError: The app is not in the manifest.
    """
    apps = _apps()
    if app not in apps:
        raise CompositionError(f"{app!r} is not an app in {MANIFEST.name}")
    return tuple(_key(apps[app] or {}, "idle_when") or ())


def deployment_name(app: str) -> str:
    """The Kubernetes object name a single-multiplicity app renders under.

    `dfe-common.fullname` is `{project}-{component}`, and every chart here sets
    `project: dfe`, so an app already carrying the prefix is its own name and
    one that does not gains it.

    Args:
        app: An app name from the manifest.

    Returns:
        The Deployment name.
    """
    return app if app.startswith("dfe-") else f"dfe-{app}"


def _check(profile: str) -> None:
    if profile not in profiles.PROFILE_NAMES:
        raise CompositionError(
            f"{profile!r} is not a deploy profile (have: {', '.join(profiles.PROFILE_NAMES)})"
        )


def seed_block(profile: str) -> str:
    """The generated `deployRepo.seedApps` block for a Kubernetes profile.

    Args:
        profile: A Kubernetes profile name.

    Returns:
        The whole delimited block, markers included, newline-terminated.
    """
    lines = [SEED_BEGIN, SEED_BANNER, "deployRepo:\n", "  seedApps:\n"]
    lines += [f"    - {name}\n" for name in default_apps(profile)]
    lines.append(SEED_END)
    return "".join(lines)


def _split(text: str) -> tuple[str, str, str]:
    """(before, block, after) around the seed markers, or a block to append."""
    start = text.find(SEED_BEGIN)
    end = text.find(SEED_END)
    if start < 0 or end < 0 or end < start:
        # First render: the block goes at the end of the file.
        tail = text if text.endswith("\n") else text + "\n"
        return tail + "\n", "", ""
    return text[:start], text[start : end + len(SEED_END)], text[end + len(SEED_END) :]


def write_seed(check_only: bool = False) -> int:
    """Render the seeded app set into every Kubernetes profile values file.

    Args:
        check_only: Report drift and change nothing.

    Returns:
        Process exit status: 1 when a committed block is stale.
    """
    stale: list[str] = []
    for mode in profiles.MODES:
        path = REPO_ROOT / profiles.PROFILES[mode].argocd_values
        text = path.read_text(encoding="utf-8", errors="replace")
        before, current, after = _split(text)
        fresh = seed_block(mode)
        if current == fresh:
            continue
        if check_only:
            stale.append(str(path.relative_to(REPO_ROOT)))
            continue
        path.write_text(before + fresh + after, encoding="utf-8", newline="\n")
        print(f"wrote the seeded app set into {path.relative_to(REPO_ROOT)}", file=sys.stderr)
    if stale:
        print(
            "STALE against apps.yaml -- run `python3 scripts/composition.py "
            f"--write-seed`: {', '.join(stale)}",
            file=sys.stderr,
        )
        return 1
    return 0


def _table() -> str:
    """One row per profile: what it deploys by default."""
    rows = []
    for profile in profiles.PROFILE_NAMES:
        rows.append(f"{profile}:")
        for app in default_apps(profile):
            rows.append(f"    {app}")
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments, defaulting to sys.argv[1:].

    Returns:
        Process exit status.
    """
    parser = argparse.ArgumentParser(
        prog="composition.py",
        description="The default composition per profile, derived from apps.yaml.",
    )
    parser.add_argument(
        "--profile",
        metavar="NAME",
        help="print just this profile's default app set, one per line",
    )
    parser.add_argument(
        "--write-seed",
        action="store_true",
        help="render the seeded app set into every Kubernetes profile values file",
    )
    parser.add_argument(
        "--check-seed",
        action="store_true",
        help="report a stale committed seed block and exit 1 (for CI)",
    )
    args = parser.parse_args(argv)

    try:
        if args.write_seed or args.check_seed:
            return write_seed(check_only=args.check_seed)
        if args.profile:
            sys.stdout.write("".join(f"{a}\n" for a in default_apps(args.profile)))
            return 0
        sys.stdout.write(_table())
    except CompositionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
