<!--
  Project:      dfe-infra
  File:         docs/CUTTING-A-STACK.md
  Purpose:      What a DFE stack version is, how to cut one, and how it reaches
                every deploy target.
  License:      BUSL-1.1
  Copyright:    (c) 2026 HYPERI PTY LIMITED
-->

# Cutting a stack version

A stack version is one certified set of pins -- every DFE app, every backing
service, every operator, each at an exact version and digest. `versions.yaml` is
the SSoT and everything else is a projection of it.

Cut one with:

    python3 scripts/dfe-stack cut 2.2.0-rc.7 --dry-run
    python3 scripts/dfe-stack cut 2.2.0-rc.7

## What `cut` does

It clones the `--from` block (default: `current`) as the SHAPE, so a key cannot
be dropped by omission, then for each DFE-owned app resolves the newest
published release and repins the matching digest.

Third-party pins carry over untouched. Those are Renovate's to move, and several
are deliberately held below a ceiling -- the ClickHouse LTS line, the Kafka
version strimzi supports. A cut is about OUR components catching up, not about
overriding a policy gate.

| flag | effect |
|---|---|
| `--from` | stack to clone the shape from (default `current`) |
| `--maturity` | alpha, beta, rc or release (default: inherit) |
| `--apps` | move only these; the rest hold at their `--from` pins |
| `--dry-run` | print the plan, write nothing |

Output names every app as MOVED or held, with the reason, so the cut is
reviewable as a diff rather than trusted.

## What it refuses

- A version that already exists.
- An app whose `apps.<name>` pin is missing from the source block.
- Writing anything when the `current:` pointer cannot be moved, so a cut is
  never half-written.

An app with no published release is HELD, not fatal -- the alpha transforms have
never shipped to GHCR and a cut must not stall on them. A registry failure that
is not a missing package DOES stop the cut, so auth or network trouble never
reads as "unpublished".

## After the cut

`cut` moves the pins and the `current` pointer. Three checks finish the job:

    python3 scripts/dfe-stack compat-check --stack 2.2.0-rc.7
    python3 scripts/check_versions_drift.py --fix
    python3 scripts/dfe-stack release-gate --stack 2.2.0-rc.7

`compat-check` validates the interdependency locks in
`constraints/<version>.yaml`. `cut` clones that file from the source stack, so
it exists already -- review the rules rather than recreating them, because a
lock is a property of the components and only a component move invalidates one.
An existing file is never overwritten.
`check_versions_drift.py --fix` propagates the SSoT into the pin mirrors that
Renovate cannot reach (appset pins, `Chart.yaml` appVersions).
`release-gate` fails a release-maturity stack that still has unpublished apps.

## How a cut reaches each target

One cut serves every deploy target, because each one consumes a projection of
the same block.

**k8s** reads `versions.yaml` directly -- bootstrap through
`read_versions.py`, the charts through the appset pins that
`check_versions_drift.py` keeps in step.

**docker** consumes it through the release artifact.
`dfe-stack render --target docker` emits the `.env` pin fragment; the release
workflow packs it into the signed OCI artifact
`ghcr.io/hyperi-io/dfe-stack-manifest:<version>`; dfe-docker's
`make stack VERSION=X.Y.Z` pulls it -- from a local checkout when
`DFE_INFRA_DIR` names one, else `oras pull`. It never falls back to `latest`.

So cutting correctly IS the carry-through. There is no second decision to make
for docker, and no place for the two paths to disagree.

## The maturity ladder

`alpha` -> `beta` -> `rc` -> `release`. Only a release-maturity stack may
auto-advance the top-level `latest:` pointer, and only when every app is
published. Everything below release may carry unpublished apps, which is what
lets an rc exist while a component is still in flight.

Related: `docs/CI-MAINTENANCE-DESIGN.md` for how shared facts stay current, and
`docs/TESTING-CYCLE.md` for proving a cut on a real cluster.
