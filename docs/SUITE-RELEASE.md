<!--
  Project:      dfe-infra
  File:         docs/SUITE-RELEASE.md
  Purpose:      How a scalo release and the fleet rebuild behind it are driven
                from this repo - what `scripts/dfe-suite` does at each step, and
                what it refuses to do. The acting half of docs/suite-graph.md.
  Language:     Markdown
  License:      BUSL-1.1
  Copyright:    (c) 2026 HYPERI PTY LIMITED
-->

# Driving a suite release

`docs/suite-graph.md` says what the graph IS. This page says how it is walked:
one library releases, and every consumer the graph points at moves onto it.

`scripts/dfe-suite` is the tool. It reads `suite.yaml` through `suite_graph`,
the same reader `dfe-stack suite` uses, so there is one parser and one picture.

**Dry-run first, every time.** `-n` on any subcommand says what would happen and
touches nothing.

```
./scripts/dfe-suite -n walk scalo-rs 2.11.0
```

## The order a pass runs in

1. `check <producer> <version>` - read-only. One line per out-edge: which
   consumer, what kind of edge, whether it moved, and what the next step is.
2. `release <node>` - ship the library. `scalo-rs` goes to crates.io,
   `scalo-py` to PyPI. Stage the fix first; the subject is the squash message.
3. `walk <producer> <version>` - run the same checks and rebuild each consumer
   whose check said so, serially, one PR per consumer.
4. `signals <member>` - the open issues, bot PRs, code-scanning findings and CI
   WARNINGS on a member, before its rebuild commit lands.

`kinds` prints what each edge kind's check actually does here, including the
ones no script can answer.

`ship-rs`, `ship-py`, `rebuild-rs` and `rebuild-py` are aliases for the first
three, with the names the older tooling used.

## What it will not do

**Main is protected on every repo the suite touches, so nothing pushes to it.**
A change lands as a branch, a PR, and a squash merge. Approvals required: zero,
so this still runs unattended - but `git push origin main` is gone for good.

**The squash message is the publish trigger.** hyperi-ci reads
`git log -1 --format=%B` on `refs/heads/main` and looks for a `Publish: true`
trailer on a line of its own. The message authored at merge time carries it, so
the merge event IS the publish run. `hyperi-ci publish` is the escape hatch for
the one case with no new commit to land: a re-release of main's HEAD as it
stands.

**Every wait is an active poll with a hard deadline, and it early-fails.** The
moment a job or a required check goes red the tool stops and names it, rather
than blocking opaquely for an hour on something that died ten minutes in.

**A green run is never proof of a release.** The publish stage can be gated out
and the run still ends green, so the last thing every command does is read the
registry and require the published version to have MOVED from the baseline it
took before starting. An unreadable registry refuses to start: without a
baseline that assertion is a no-op that reports success.

## What a rebuild re-does

A Rust consumer takes `cargo update -p <package> --precise <version>`, then
regenerates the Dockerfile and the config artefacts its deployment contract
owns, then gates on fmt, clippy and nextest.

The committed `chart/` is the app's contract MIRROR, not the deploy artefact -
`helm/charts/*` here is what deploys. Where the app keeps a `helm_contract`
test, the chart is left to that gate; where it has an emit-chart subcommand, the
chart is regenerated; where it has neither, the rebuild REFUSES rather than
leave the chart stale and unchecked. `--no-chart` is the opt-out for an app
whose chart is maintained some other way.

A Python consumer moves the `>=` floor in `pyproject.toml` - preserving the
extras, which a plain `uv add` can silently drop - relocks that one package, and
then proves `uv.lock` resolved the version asked for. A floor is not a pin: a
stale cache resolves something older and sails through every gate.

The first rebuild after a checkout is a cold cargo build. The warm target
directory is `~/.cache/dfe-suite/rebuild/<app>`, overridable with
`DFE_SUITE_REBUILD_TARGET`.

## What the walk leaves for a person

Some edge kinds are mechanical and some are not. A walk reports every edge it
did not answer, counts them, and exits 2 when any are left - so a lockstep edge
cannot be swallowed by a sibling edge that happened to dispatch a rebuild first.
A failed rebuild stops the walk at that consumer, and the list is printed either
way.

Rebuilds are automated for the `scalo` package only. Any other producer's edges
are reported and left, rather than rewritten by a rebuild path that was written
for a different pin.

## Landing a GHCR app release

`scripts/dfe-release.py` is the other caller: it lands a branch somebody else
pushed and resolves the GHCR digest the release published. Both tools open the
PR, wait, and squash-merge through `scripts/suite/landing.py`, so there is one
definition of what landing on main means.

Its `ship` verb STOPS with exit 3 when a run is already queued or in progress on
main for that repo: the publish workflow's concurrency group is keyed on the
branch, so merging then would cancel that run and its release would never land.

## Where the code is

- `scripts/dfe-suite` - the CLI.
- `scripts/suite/` - the package: `proc`, `repos`, `artefacts`, `landing`,
  `rebuild`, `ship`, `graph`, `kinds`.
- `scripts/tests/test_dfe_suite.py`, `test_suite_release.py`,
  `test_suite_kinds.py` - offline, with a recorder in place of `subprocess.run`.

The `signals` subcommand shells out to hyperi-ai's `tools/fixes_scan.py`, which
is the same read-only gather `/fixes` runs, so the two never disagree about what
is open. It is the one subcommand that needs hyperi-ai installed; set
`HYPERI_AI_HOME` if it is somewhere unusual.
