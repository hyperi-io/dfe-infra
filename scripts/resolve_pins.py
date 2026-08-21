#!/usr/bin/env python3
#  Project:      dfe-infra
#  File:         resolve_pins.py
#  Purpose:      Resolve a pinned version (tag) to its registry digest and write
#                it into versions.yaml `digests:` for the current stack -- the
#                automation that kills the hand look-up-a-sha-and-paste-it loop
#                (dfe-infra#116). Reuses registry_pins for the tag -> digest
#                core, so dfe-infra and dfe-deploy share one resolver.
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""resolve_pins -- pin a version, get (and set) its digest.

versions.yaml splits each DFE app pin in two: the human-readable TAG in `apps:`
(so the chart appVersion checks work) and the immutable DIGEST in `digests:`
(the half that actually pins the pull). Choosing the tag is a decision; finding
its sha is mechanical -- and today it is done by hand, in two repos, which is
where the wrong sha creeps in. This resolves the digest for whatever tags the
CURRENT stack's `apps:` holds and, on --write, sets `digests:` to match.

Three modes over the current stack (default: every app that already carries a
digest -- the published set):

  (default)   resolve + print each app's tag -> registry digest and whether the
              recorded digest is fresh. Read-only.
  --check     the same, but exit non-zero if any recorded digest is stale or the
              tag is missing from the registry. The "are my pins fresh" gate.
  --write     rewrite `digests:` for the current stack in place for every stale
              pin, preserving all formatting and comments (ruamel round-trip).

Ad-hoc resolution (the dfe-deploy path -- resolve one arbitrary tag, no file):

    python3 scripts/resolve_pins.py --app dfe-loader --version v1.18.21
    # prints ghcr.io/hyperi-io/dfe-loader:v1.18.21@sha256:...

Supply-chain cooldown: --write will NOT adopt a digest whose image was
published less than --cooldown-days ago (default 7), matching the pin policy at
the top of versions.yaml. Pass --allow-fresh for an internal or security bump
that must land inside the window (DFE-owned apps carry no upstream cooldown, so
this is their normal path when pinning a just-cut release).

Division of labour with the rest of the tooling (one SSoT per concern):
  * `dfe-stack bump-app <app> <ver>` sets the TAG (apps: + Chart.yaml appVersion).
  * this tool sets the DIGEST (digests:) to match the tags apps: holds.
  * `dfe-stack refresh-digests` does the same for the third-party
    services-digests: (via docker buildx), which this deliberately leaves alone.
So the flow is: bump-app to choose the version, then resolve_pins --write to pin
its sha.

Needs an authenticated `gh` (read:packages). ruamel.yaml for the write path.
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry_pins import (  # noqa: E402
    RegistryError,
    Resolved,
    resolve,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSIONS = REPO_ROOT / "versions.yaml"
DEFAULT_ORG = "hyperi-io"
DEFAULT_REGISTRY = "ghcr.io/hyperi-io"
DEFAULT_COOLDOWN_DAYS = 7


# --- versions.yaml reading (stack-aware; resolves the `current` pointer) ------
def _load_yaml():
    """Load versions.yaml round-trip (ruamel) so a write preserves formatting.

    ruamel keeps comments, quoting and layout, so setting one leaf changes one
    line. Imported lazily -- the read-only modes work without it installed.
    """
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    return yaml


def load_stack(stack: str | None):
    """Return (yaml, data, stack_name, stack_map) for the requested stack.

    Defaults to the `current` pointer -- the stack under development, the same
    one the drift-check validates -- rather than the first stack in the file.
    """
    yaml = _load_yaml()
    data = yaml.load(VERSIONS.read_text(encoding="utf-8", errors="replace"))
    name = stack or data.get("current")
    stacks = data.get("stacks", {})
    if not name:
        raise SystemExit("versions.yaml has no `current` pointer and no --stack given")
    if name not in stacks:
        have = ", ".join(stacks) or "none"
        raise SystemExit(f"stack {name!r} not in versions.yaml stacks: (have: {have})")
    return yaml, data, name, stacks[name]


# --- resolution ---------------------------------------------------------------
class Outcome:
    """One app's resolution verdict against the registry."""

    FRESH = "fresh"      # recorded digest == registry
    STALE = "stale"      # recorded digest != registry (or absent) -- a write target
    MISSING = "missing"  # the tag is not on the registry at all
    ERROR = "error"      # gh/API failure resolving this app


class Row:
    def __init__(self, app: str, tag: str, recorded: str, outcome: str,
                 resolved: Resolved | None, detail: str = "") -> None:
        self.app = app
        self.tag = tag
        self.recorded = recorded
        self.outcome = outcome
        self.resolved = resolved
        self.detail = detail


def _selected_apps(stack_map, requested: list[str]) -> list[str]:
    """Apps to resolve: the explicit --app list, else every app with a digest.

    The default is the PUBLISHED set (apps carrying a digest) so the unpublished
    alpha stubs -- in apps: but with no image on GHCR -- are not probed and
    reported as spurious 404s.
    """
    apps = stack_map.get("apps", {})
    digests = stack_map.get("digests", {})
    if requested:
        unknown = [a for a in requested if a not in apps]
        if unknown:
            raise SystemExit(
                f"no apps.{'/'.join(unknown)} pin in the stack (have: {', '.join(apps)})"
            )
        return list(requested)
    return [a for a in apps if a in digests]


def resolve_rows(stack_map, org: str, apps: list[str],
                 tag_override: str | None) -> list[Row]:
    """Resolve each selected app's tag to its registry digest."""
    app_pins = stack_map.get("apps", {})
    digests = stack_map.get("digests", {})
    rows: list[Row] = []
    for app in apps:
        tag = tag_override or str(app_pins.get(app, ""))
        recorded = str(digests.get(app, ""))
        try:
            found = resolve(org, app, tag)
        except RegistryError as exc:
            rows.append(Row(app, tag, recorded, Outcome.ERROR, None, str(exc)))
            continue
        if found is None:
            rows.append(Row(app, tag, recorded, Outcome.MISSING, None,
                            "tag not on the registry (release without an image? hyperi-ci#102)"))
        elif found.digest == recorded:
            rows.append(Row(app, tag, recorded, Outcome.FRESH, found))
        else:
            rows.append(Row(app, tag, recorded, Outcome.STALE, found))
    return rows


def _age_days(published: datetime.datetime | None) -> float | None:
    if published is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - published).total_seconds() / 86400.0


# --- output -------------------------------------------------------------------
_SYMBOL = {
    Outcome.FRESH: "ok   ",
    Outcome.STALE: "STALE",
    Outcome.MISSING: "MISS ",
    Outcome.ERROR: "ERR  ",
}


def _print_row(row: Row) -> None:
    reg = row.resolved.digest if row.resolved else "-"
    age = _age_days(row.resolved.published) if row.resolved else None
    age_s = f"  ({age:.1f}d old)" if age is not None else ""
    line = f"  {_SYMBOL[row.outcome]} {row.app} {row.tag}"
    if row.outcome == Outcome.STALE:
        line += f"\n        recorded: {row.recorded or '(none)'}\n        registry: {reg}{age_s}"
    elif row.outcome == Outcome.FRESH:
        line += f" -> {reg[:26]}...{age_s}"
    elif row.detail:
        line += f" -- {row.detail}"
    print(line)


# --- write --------------------------------------------------------------------
def apply_writes(yaml, data, stack_map, rows: list[Row],
                 cooldown_days: int, allow_fresh: bool) -> tuple[list[str], list[str]]:
    """Set digests: for each STALE row, cooldown permitting. Returns (written, held).

    Each digest is independent, so a held (too-fresh) pin does not block the
    others -- unlike the drift-check's all-or-nothing mirror propagation, where
    every write carries copies of ONE value.
    """
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString as DQ

    digests = stack_map.setdefault("digests", {})
    written: list[str] = []
    held: list[str] = []
    for row in rows:
        if row.outcome != Outcome.STALE or row.resolved is None:
            continue
        age = _age_days(row.resolved.published)
        if age is not None and age < cooldown_days and not allow_fresh:
            held.append(
                f"  HELD  {row.app} {row.tag}: image is {age:.1f}d old "
                f"(< {cooldown_days}d cooldown) -- pass --allow-fresh for an "
                f"internal or security bump"
            )
            continue
        # Quote the value so it matches the sha256: strings already in the block.
        digests[row.app] = DQ(row.resolved.digest)
        written.append(f"  set   digests.{row.app} = {row.resolved.digest}")

    if written:
        with VERSIONS.open("w", encoding="utf-8", newline="\n") as fh:
            yaml.dump(data, fh)
    return written, held


# --- command ------------------------------------------------------------------
def cmd_pin(args: argparse.Namespace) -> int:
    if args.version and len(args.app) != 1:
        print("--version requires exactly one --app", file=sys.stderr)
        return 2
    if args.check and args.write:
        print("--check and --write are mutually exclusive", file=sys.stderr)
        return 2

    # Every mode needs the stack map (tags + recorded digests); only --write
    # dumps the mutated tree back, so a clean read-only run never rewrites.
    yaml, data, name, stack_map = load_stack(args.stack)

    apps = _selected_apps(stack_map, args.app)
    print(f"=== resolve_pins: stack {name}, org {args.org} "
          f"({len(apps)} app(s)) ===", file=sys.stderr)

    rows = resolve_rows(stack_map, args.org, apps, args.version)

    # Ad-hoc single-tag resolution: print the pull-by-digest ref and stop unless
    # a write was asked for. This is the shape dfe-deploy calls.
    if args.version:
        row = rows[0]
        if row.outcome in (Outcome.MISSING, Outcome.ERROR):
            print(f"cannot resolve {row.app} {row.tag}: {row.detail}", file=sys.stderr)
            return 1
        print(f"{args.registry}/{row.app}:{row.tag}@{row.resolved.digest}")
        if not args.write:
            return 0
        pinned_tag = str(stack_map.get("apps", {}).get(row.app, ""))
        if pinned_tag != row.tag:
            print(
                f"refusing to write: apps.{row.app} is {pinned_tag!r}, not "
                f"{row.tag!r}. Bump the tag first (dfe-stack bump-app {row.app} "
                f"{row.tag}), then resolve_pins --write.",
                file=sys.stderr,
            )
            return 1

    for row in rows:
        _print_row(row)

    errors = [r for r in rows if r.outcome == Outcome.ERROR]
    missing = [r for r in rows if r.outcome == Outcome.MISSING]
    stale = [r for r in rows if r.outcome == Outcome.STALE]

    if errors:
        print(f"\n{len(errors)} app(s) failed to resolve (gh/API error).", file=sys.stderr)
        return 1

    if args.write:
        written, held = apply_writes(yaml, data, stack_map, rows,
                                     args.cooldown_days, args.allow_fresh)
        if written:
            print("\n" + "\n".join(written))
            print(f"\n{len(written)} digest(s) written to versions.yaml (stack {name}).")
        if held:
            print("\n" + "\n".join(held), file=sys.stderr)
        if missing:
            print(f"\n{len(missing)} tag(s) missing from the registry -- not written.",
                  file=sys.stderr)
        if not written and not held and not missing:
            print("\nevery pin already matches the registry -- nothing to write.")
        # Incomplete if anything could not be pinned.
        return 1 if (held or missing) else 0

    if args.check:
        if stale or missing:
            print(f"\n{len(stale)} stale, {len(missing)} missing -- "
                  f"pins are NOT fresh (run with --write).", file=sys.stderr)
            return 1
        print(f"\nOK -- all {len(rows)} pin(s) match the registry.")
        return 0

    # Default informational mode.
    print(f"\n{len(stale)} stale, {len(missing)} missing, "
          f"{len(rows) - len(stale) - len(missing)} fresh. "
          f"Use --write to update, --check to gate.")
    return 0


def add_pin_subparser(sub: argparse._SubParsersAction) -> None:
    """Register the `pin` subcommand on an existing subparsers action.

    Shared with dfe-ops so `dfe-ops pin` and `resolve_pins.py` are one code path.
    """
    p = sub.add_parser(
        "pin",
        help="resolve a version's registry digest and (with --write) set digests: for the current stack",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--app", action="append", default=[], metavar="NAME",
                   help="restrict to this app (repeatable; default: every published app)")
    p.add_argument("--version", default=None, metavar="TAG",
                   help="resolve this tag instead of the apps: pin (needs one --app)")
    p.add_argument("--stack", default=None, help="stack version (default: versions.yaml `current`)")
    p.add_argument("--org", default=DEFAULT_ORG, help="GH org owning the packages")
    p.add_argument("--registry", default=DEFAULT_REGISTRY, help="registry prefix for printed refs")
    p.add_argument("--check", action="store_true",
                   help="read-only: exit non-zero if any pin is stale or missing")
    p.add_argument("--write", action="store_true",
                   help="rewrite digests: in place for stale pins (cooldown-gated)")
    p.add_argument("--cooldown-days", type=int, default=DEFAULT_COOLDOWN_DAYS,
                   help="do not write a digest whose image is younger than this")
    p.add_argument("--allow-fresh", action="store_true",
                   help="override the cooldown (internal or security bump)")
    p.set_defaults(func=cmd_pin)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="resolve_pins",
        description=__doc__.split("\n")[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=False, metavar="<subcommand>")
    add_pin_subparser(sub)
    ap.set_defaults(func=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    # Allow bare invocation (no `pin` word) to mean `pin`, so the standalone tool
    # reads naturally while dfe-ops keeps the subcommand form.
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or raw[0].startswith("-"):
        raw = ["pin", *raw]
    args = ap.parse_args(raw)
    if args.func is None:
        ap.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
