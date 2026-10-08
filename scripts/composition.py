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

Helm reads only files inside the chart directory, so the manifest the engine
mounts and reflects is a copy each chart that mounts it carries -- dfe-extras,
and the dfe-engine chart until it is deleted:

    python3 scripts/composition.py --write-catalogue

`scripts/tests/test_composition.py` fails when a committed block or a chart's
copy and this manifest disagree, so neither can quietly drift.

Helm replaces a list rather than merging it, so a list item several apps share
(the wait-for-engine init container, the pressure trigger) sits whole in each
app's integration values. Each copy is a GENERATED block, between markers, of
its source under argocd/values/apps/_fragments:

    python3 scripts/composition.py --write-fragments

`scripts/tests/test_composition_fragments.py` fails on a block that differs from
its source.
"""

import argparse
import base64
import re
import subprocess
import sys
from pathlib import Path

import profiles

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "apps.yaml"

# The engine chart's copy, mounted into the engine pod as the app catalogue.
CHART_MANIFEST = REPO_ROOT / "helm" / "charts" / "dfe-engine" / "files" / "apps.yaml"

# dfe-extras' copy, which its app-catalogue ConfigMap mounts beside the thin chart.
EXTRAS_MANIFEST = REPO_ROOT / "helm" / "charts" / "dfe-extras" / "files" / "apps.yaml"

# Every chart copy --write-catalogue renders and --check-catalogue holds.
CATALOGUE_COPIES = (CHART_MANIFEST, EXTRAS_MANIFEST)

# The third copy: the snapshot bundled in the engine image, which answers when
# no chart mount is present. dfe-infra holds the SSoT and already owns the
# check, so this repo compares against it rather than trusting a stale image.
ENGINE_REPO = "hyperi-io/dfe-engine"
ENGINE_SNAPSHOT_PATH = "src/dfe_engine/appmgmt/apps.yaml"

# The axes this manifest declares; everything else in the file is comments.
DECLARATION_KEYS = ("apps", "kinds", "mesh")

CATALOGUE_BANNER = (
    "# GENERATED from apps.yaml at the repo root -- do not edit. Regenerate with:\n"
    "#     python3 scripts/composition.py --write-catalogue\n"
)

# Delimiters around the generated block in each profile values file.
SEED_BEGIN = "# BEGIN seeded apps -- rendered by `python3 scripts/composition.py --write-seed`\n"
SEED_END = "# END seeded apps\n"
SEED_BANNER = (
    "# The apps this tier deploys by default. Derived from apps.yaml's\n"
    "# `default_in`, which is where the default composition is declared; edit\n"
    "# the manifest, then re-render. A Helm list is replaced rather than merged,\n"
    "# so this states the whole set.\n"
)

# The apps' integration values, and the shared fragments their marked blocks copy.
APPS_VALUES = REPO_ROOT / "argocd" / "values" / "apps"
FRAGMENTS = APPS_VALUES / "_fragments"
FRAGMENT_BEGIN = re.compile(
    r"^(?P<indent> *)# BEGIN fragment (?P<name>[a-z0-9-]+)"
    r" -- rendered by `python3 scripts/composition.py --write-fragments`\n",
    re.MULTILINE,
)


class CompositionError(Exception):
    """Raised when the manifest is missing, malformed, or names a bad profile."""


class SnapshotUnavailableError(Exception):
    """Raised when the engine's bundled snapshot cannot be read at all.

    Distinct from `CompositionError` so the CLI can tell "the comparison
    could not run" (exit 2) apart from "the comparison ran and disagreed"
    (exit 1) -- a network or auth failure is not drift.
    """


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


def multiplicity(app: str) -> str:
    """How many deployments an app runs: `single`, or `per_config` (one per source).

    Args:
        app: An app name from the manifest.

    Returns:
        The declared multiplicity, or `single` when the app declares none.

    Raises:
        CompositionError: The app is not in the manifest.
    """
    apps = _apps()
    if app not in apps:
        raise CompositionError(f"{app!r} is not an app in {MANIFEST.name}")
    return str((apps[app] or {}).get("multiplicity") or "single")


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


def catalogue_copy() -> str:
    """A chart's copy of the manifest: the banner, then apps.yaml verbatim.

    Returns:
        The whole file body, newline-terminated.

    Raises:
        CompositionError: The manifest is missing.
    """
    if not MANIFEST.is_file():
        raise CompositionError(f"{MANIFEST} not found")
    # Copied byte for byte rather than re-serialised: a round trip through a YAML
    # writer can change a value (YAML 1.1 reads `no` and `off` as booleans).
    return CATALOGUE_BANNER + MANIFEST.read_text(encoding="utf-8")


def write_catalogue(check_only: bool = False) -> int:
    """Render the manifest into every chart that mounts it.

    Args:
        check_only: Report drift and change nothing.

    Returns:
        Process exit status: 1 when a committed copy is stale.
    """
    fresh = catalogue_copy()
    stale: list[str] = []
    for path in CATALOGUE_COPIES:
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        if current == fresh:
            continue
        where = str(path.relative_to(REPO_ROOT))
        if check_only:
            stale.append(where)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fresh, encoding="utf-8", newline="\n")
        print(f"wrote the app manifest into {where}", file=sys.stderr)
    if stale:
        print(
            "STALE against apps.yaml -- run `python3 scripts/composition.py "
            f"--write-catalogue`: {', '.join(stale)}",
            file=sys.stderr,
        )
        return 1
    return 0


def fragment(name: str) -> str:
    """A shared fragment's body: its source file less the leading comment block.

    Args:
        name: The fragment, a file name under argocd/values/apps/_fragments
            without its .yaml suffix.

    Returns:
        The body, newline-terminated, as written at column 0.

    Raises:
        CompositionError: There is no fragment of that name.
    """
    path = FRAGMENTS / f"{name}.yaml"
    if not path.is_file():
        raise CompositionError(f"no fragment {name!r} under {FRAGMENTS.relative_to(REPO_ROOT)}")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = 0
    while start < len(lines) and (lines[start].startswith("#") or not lines[start].strip()):
        start += 1
    return "".join(lines[start:])


def render_fragments(text: str, where: str = "") -> str:
    """``text`` with each marked block holding its fragment, indented to its marker.

    A block runs from a BEGIN marker to the first END marker of the same name at
    the same indentation.

    Args:
        text: A values file's text.
        where: The file, for an error message.

    Returns:
        The text with every block rewritten, unchanged when every block is current.

    Raises:
        CompositionError: A block has no END marker, or names no fragment.
    """
    out: list[str] = []
    pos = 0
    for begin in FRAGMENT_BEGIN.finditer(text):
        if begin.start() < pos:
            continue
        indent, name = begin["indent"], begin["name"]
        end_marker = re.compile(rf"^{indent}# END fragment {re.escape(name)}\n", re.MULTILINE)
        end = end_marker.search(text, begin.end())
        if end is None:
            raise CompositionError(f"{where}: BEGIN fragment {name} has no END marker below it")
        body = [
            f"{indent}{line}" if line.strip() else "\n"
            for line in fragment(name).splitlines(keepends=True)
        ]
        out += [text[pos : begin.end()], *body, end.group(0)]
        pos = end.end()
    out.append(text[pos:])
    return "".join(out)


def _app_values_files() -> list[Path]:
    """Every app's integration values file, the fragment sources aside."""
    return sorted(p for p in APPS_VALUES.glob("*/*.yaml") if p.parent != FRAGMENTS)


def write_fragments(check_only: bool = False) -> int:
    """Copy each shared fragment into every block that names it.

    Args:
        check_only: Report drift and change nothing.

    Returns:
        Process exit status: 1 when a committed block is stale.

    Raises:
        CompositionError: A block has no END marker, or names no fragment.
    """
    stale: list[str] = []
    for path in _app_values_files():
        where = str(path.relative_to(REPO_ROOT))
        text = path.read_text(encoding="utf-8")
        fresh = render_fragments(text, where)
        if fresh == text:
            continue
        if check_only:
            stale.append(where)
            continue
        path.write_text(fresh, encoding="utf-8", newline="\n")
        print(f"wrote the shared fragments into {where}", file=sys.stderr)
    if stale:
        print(
            f"STALE against {FRAGMENTS.relative_to(REPO_ROOT)} -- run `python3 "
            f"scripts/composition.py --write-fragments`: {', '.join(stale)}",
            file=sys.stderr,
        )
        return 1
    return 0


def _declarations(text: str) -> dict:
    """The declaration blocks a snapshot comparison cares about.

    Args:
        text: A manifest's raw YAML text.

    Returns:
        Only the `apps`, `kinds` and `mesh` top-level keys. `YAML(typ="safe")`
        already drops comments and normalises key order into plain dicts, so
        neither needs handling here.

    Raises:
        CompositionError: The text does not parse as a mapping.
    """
    from ruamel.yaml import YAML

    doc = YAML(typ="safe").load(text)
    if not isinstance(doc, dict):
        raise CompositionError("manifest does not parse as a mapping")
    return {key: doc[key] for key in DECLARATION_KEYS if key in doc}


def _diff_declarations(ssot, snapshot, path: str = "") -> list[str]:
    """Every path where two parsed declarations disagree.

    Args:
        ssot: A value (or sub-value) from this repo's parsed apps.yaml.
        snapshot: The matching value from the engine's parsed snapshot.
        path: The dotted path walked so far, for recursion.

    Returns:
        One line per differing path, naming both values. Dicts are compared
        key by key so a single changed field is named precisely; a list is
        compared as a whole value, since list order is a real difference
        here, not a comment or key-order artefact.
    """
    if isinstance(ssot, dict) and isinstance(snapshot, dict):
        diffs: list[str] = []
        for key in sorted(set(ssot) | set(snapshot)):
            sub = f"{path}.{key}" if path else key
            if key not in ssot:
                diffs.append(f"{sub}: absent from apps.yaml, engine has {snapshot[key]!r}")
            elif key not in snapshot:
                diffs.append(f"{sub}: apps.yaml has {ssot[key]!r}, absent from the engine")
            else:
                diffs.extend(_diff_declarations(ssot[key], snapshot[key], sub))
        return diffs
    if ssot != snapshot:
        return [f"{path}: apps.yaml={ssot!r}, engine={snapshot!r}"]
    return []


def _engine_snapshot_text(path: str | None) -> str:
    """The engine's bundled apps.yaml, from a checkout or the GitHub API.

    Args:
        path: A dfe-engine checkout root, or None to fetch `origin/main` over
            the GitHub API -- the CI path, where there is no such checkout.

    Returns:
        The snapshot file's raw text.

    Raises:
        SnapshotUnavailableError: The checkout has no such file, `gh` failed, or
            its response could not be decoded -- never raised for drift.
    """
    if path is not None:
        snapshot = Path(path) / ENGINE_SNAPSHOT_PATH
        if not snapshot.is_file():
            raise SnapshotUnavailableError(f"{snapshot} not found")
        return snapshot.read_text(encoding="utf-8", errors="replace")
    endpoint = f"repos/{ENGINE_REPO}/contents/{ENGINE_SNAPSHOT_PATH}"
    result = subprocess.run(
        ["gh", "api", endpoint, "--jq", ".content"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise SnapshotUnavailableError(
            f"gh api {endpoint} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    try:
        return base64.b64decode(result.stdout).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise SnapshotUnavailableError(f"could not decode the API response: {error}") from error


def check_engine_snapshot(path: str | None = None) -> int:
    """Compare the engine's bundled apps.yaml against this repo's SSoT.

    Args:
        path: A dfe-engine checkout root, or None to fetch over the API.

    Returns:
        0 when the declarations agree, 1 when they differ (each differing
        path printed with both values), 2 when the snapshot could not be
        read at all -- a network or auth failure is reported as unverified,
        never as drift.
    """
    if not MANIFEST.is_file():
        raise CompositionError(f"{MANIFEST} not found")
    ssot = _declarations(MANIFEST.read_text(encoding="utf-8", errors="replace"))
    try:
        snapshot_text = _engine_snapshot_text(path)
    except SnapshotUnavailableError as error:
        print(f"UNVERIFIED -- the engine snapshot could not be read: {error}", file=sys.stderr)
        return 2
    snapshot = _declarations(snapshot_text)
    diffs = _diff_declarations(ssot, snapshot)
    if diffs:
        print(
            "DRIFT -- the engine's bundled apps.yaml disagrees with the SSoT:",
            file=sys.stderr,
        )
        for line in diffs:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("the engine's bundled apps.yaml agrees with apps.yaml's declarations", file=sys.stderr)
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
    parser.add_argument(
        "--write-catalogue",
        action="store_true",
        help="render the manifest into every chart that mounts it into the engine pod",
    )
    parser.add_argument(
        "--check-catalogue",
        action="store_true",
        help="report a stale committed chart copy of the manifest and exit 1 (for CI)",
    )
    parser.add_argument(
        "--write-fragments",
        action="store_true",
        help="copy each shared fragment into every app values block that names it",
    )
    parser.add_argument(
        "--check-fragments",
        action="store_true",
        help="report an app values block that differs from its fragment and exit 1 (for CI)",
    )
    parser.add_argument(
        "--check-engine-snapshot",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help=(
            "compare the engine's bundled apps.yaml against this repo's SSoT; PATH is "
            "a dfe-engine checkout, omitted fetches origin/main over the GitHub API "
            "(exit 1 on drift, exit 2 when the snapshot cannot be read)"
        ),
    )
    args = parser.parse_args(argv)

    try:
        if args.check_engine_snapshot is not None:
            return check_engine_snapshot(args.check_engine_snapshot or None)
        if args.write_catalogue or args.check_catalogue:
            return write_catalogue(check_only=args.check_catalogue)
        if args.write_seed or args.check_seed:
            return write_seed(check_only=args.check_seed)
        if args.write_fragments or args.check_fragments:
            return write_fragments(check_only=args.check_fragments)
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
