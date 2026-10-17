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

`stack.previous` is derived rather than cloned -- the `--from` block is the new
version's predecessor, so `cut` re-points it there and `check-upgrade` reports
the consecutive path as verified without a hand edit.

A content repo recorded by commit rather than tag (dfe-docker cuts no per-stack
tag) is re-resolved at its `main` head, and the comment above the pin is
rewritten to cite that commit's date and subject. Every other pin keeps the
comment it was cloned with, so a moved app or tag needs its evidence rewritten
by hand before `check_pin_evidence.py` passes.

Third-party pins carry over untouched. Those are Renovate's to move, and several
are deliberately held below a ceiling -- the ClickHouse LTS line, the Kafka
version strimzi supports. A cut is about OUR components catching up, not about
overriding a policy gate.

| flag | effect |
|---|---|
| `--from` | stack to clone the shape from (default `current`) |
| `--maturity` | alpha, beta, rc or release (default: inherit) |
| `--apps` | move only these; the rest hold at their `--from` pins |
| `--replace-withdrawn` | re-cut a withdrawn version under the same name (see below) |
| `--dry-run` | print the plan, write nothing |

Output names every app as MOVED or held, with the reason, so the cut is
reviewable as a diff rather than trusted.

## What it refuses

- A version that already exists, unless it is withdrawn and `--replace-withdrawn` is passed.
- `--replace-withdrawn` on a version that is missing or not withdrawn, or with `--from` naming the withdrawn stack itself.
- An app whose `apps.<name>` pin is missing from the source block.
- Writing anything when the `current:` pointer cannot be moved, so a cut is
  never half-written.

An app with no published release is HELD, not fatal -- the alpha transforms have
never shipped to GHCR and a cut must not stall on them. A registry failure that
is not a missing package DOES stop the cut, so auth or network trouble never
reads as "unpublished".

## A released stack is frozen

Once a stack's git tag exists, its block in `versions.yaml` and its `constraints/<version>.yaml` are the record of what shipped. `dfe-stack refresh-digests`, `dfe-stack bump-app` and `resolve_pins.py --write` refuse to write to it, and `scripts/tests/test_stack_freeze.py` fails on any other edit. A digest or app move goes into the next cut.

## Withdrawn stacks

A `support: withdrawn` stack was pulled after its tag was cut. Environments may still run it, so it stays an upgrade source, but nothing upgrades onto it.

A cut FROM a withdrawn stack is `support: standard`. The comments on its `maturity:` and `support:` lines are dropped, because they explain the withdrawal, not the new candidate. The cut prints `support withdrawn -> standard` when it does this.

Cutting the withdrawn version again under the same name needs `--replace-withdrawn`:

    python3 scripts/dfe-stack cut 2.3.0 --from 2.3.0-rc.4 --maturity release --replace-withdrawn --dry-run

Before the normal cut it:

- splices the withdrawn block out of `versions.yaml`, with any comment directly above it;
- rewrites every `stack.previous` that names it to the withdrawn stack's own predecessors. Otherwise the candidate cut from the withdrawn block and the new release each name the other as predecessor, a cycle in the upgrade graph;
- removes the withdrawn stack's constraints file, so the new one is cloned from `--from` rather than kept.

The freeze test allows that `stack.previous` rewrite in a tagged block and no other edit, and only for a stack the `versions.yaml` at that block's tag records as withdrawn.

`--dry-run` prints each of these and writes nothing. The cut never moves `latest:` -- the release workflow does that on a release tag -- and never touches the withdrawn stack's git tag. That workflow publishes nothing for a version whose tag already exists, so the withdrawn tag has to be deleted before the re-cut reaches `main`. The repo's tag ruleset blocks deleting any tag and has no bypass actors, so an org admin has to allow the deletion first.

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
`release-gate` fails a release-maturity stack that still has unpublished apps, or a DFE image whose pinned index lacks `linux/amd64` or `linux/arm64`. A deployment schedules onto arm64 (the AWS node pools) or amd64 (most on-prem nodes), and an image missing either pulls fine and dies at exec on the other.

At every maturity it also fails when `content.dfe-schemas` differs from the dfe-schemas the pinned dfe-engine's `uv.lock` locks: move `content.dfe-schemas` to that locked version.

The architecture check reads each index from the registry by digest, through `docker buildx imagetools`, which needs no login because every DFE package is public. A registry it cannot read fails the gate. Below `release` the gate never touches the registry.

`dfe-stack platforms --stack <version>` prints the same per-image verdict for any stack and exits 1 on a gap:

    python3 scripts/dfe-stack platforms --stack 2.2.0-rc.14

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

`alpha` -> `beta` -> `rc` -> `release`. Only a release-maturity stack may auto-advance the top-level `latest:` pointer, and only when every app is published and every DFE image carries both architectures. Everything below release may carry unpublished or single-arch apps, which is what lets an rc exist while a component is still in flight.

### This ladder is the suite's, not a repo's

`rc` exists only at this layer. It is the stage where the components come
together and churn against each other, one level above any single repo.

An individual repo runs hyperi-ci's ladder instead, which is `alpha` ->
`beta` -> `release` and lines up 1:1 with semantic-release's prerelease
branches. A repo never cuts an `rc`. Where 1.1.1 is released and 1.1.2 is not
ready, that repo ships `1.1.2-beta.N` off its `beta` branch while `main` keeps
serving 1.1.1 to anyone pinning a stable version.

Both ladders end at `release`, and the shared vocabulary is deliberate -- the
only word that differs between the layers is `rc`. hyperi-ci's side of this is
`docs/versioning-and-the-suite.md` in that repo. <!-- doc-paths: ignore -->

Related: `docs/CI-MAINTENANCE-DESIGN.md` for how shared facts stay current, and
`docs/TESTING-CYCLE.md` for proving a cut on a real cluster.
