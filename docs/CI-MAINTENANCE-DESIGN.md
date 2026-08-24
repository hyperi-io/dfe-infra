<!--
  Project:      dfe-infra
  File:         docs/CI-MAINTENANCE-DESIGN.md
  Purpose:      How a shared fact stays current across repos: a bot to move it,
                a gate to prove it moved.
  License:      BUSL-1.1
  Copyright:    (c) 2026 HYPERI PTY LIMITED
-->

# Keeping shared facts current

Every shared fact gets two mechanisms, never one. **A bot to move it, a gate to
prove it moved.**

A bot alone raises a pull request and then has no opinion about whether anyone
merges it. A gate alone tells you something is stale and leaves a human to work
out what the new value should be. Neither half is sufficient, and each one
without the other fails quietly.

## Why one half is not enough

dfe-schemas is consumed as a git submodule by dfe-engine, dfe-loader and
dfe-fetcher. Renovate ships its `git-submodules` manager DISABLED -- it is
opt-in beta -- so no bot watched those pins and nothing reported the silence.
The three consumers drifted to three different commits: engine current, loader
26 behind, fetcher 27.

The gap was not cosmetic. The stale two were missing the `_org_id` rename on
`detection_checkpoint` and the fix for JSON columns that cannot be `Nullable`,
so two apps were building against a schema definition the third had already
corrected. Nothing failed. Nothing warned.

Adding only the bot would have raised three PRs and then permitted one repo to
sit on an unmerged bump indefinitely. Adding only the gate would have gone red
with no automated way to go green.

## The two halves

**The bot: Renovate.** Configured once in the org preset
(`github>hyperi-io/renovate-config`), so a repo joins by having a dependency
rather than by remembering to opt in. It raises a PR under the standing policy
-- `fix(deps):` so merging ships, a 7-day cooldown for external dependencies,
PR-only with no automerge.

**The gate: a `check_*_drift.py` in `scripts/`.** Each one asserts that a fact
resolved in more than one place still agrees, and fails CI when it does not.
They share a shape: read the SSoT, read every mirror, report each disagreement
by name, exit non-zero.

| guard | asserts |
|---|---|
| `check_versions_drift.py` | every pin mirror matches `versions.yaml` |
| `check_namespace_drift.py` | the operator-namespace SSoT matches where the appsets install |
| `check_submodule_drift.py` | every consumer of a shared submodule pins the same commit |
| `check_governance_drift.py` | governance actions resolve to real chart values |
| `check_image_pins.py` | image references carry a digest |

They run in `helm-lint.yml` on every PR, and the deploy-affecting ones run again
in the `dfe-ops stack-deploy` pre-flight.

## Rules a guard follows

- **Name what disagreed, not that something did.** `dfe-loader: pins
  dfe-schemas at b7e58c6, 26 commits behind 6586f54` is actionable; "drift
  detected" is not.
- **Say how to close it.** The submodule guard ends with "Renovate raises the
  bump PR; merging it is what closes this."
- **A skip is not a pass.** When a guard cannot verify -- no `gh`, no token, a
  private repo it cannot read -- it says so loudly and never reports green.
  `check_governance_drift.py` takes `--require` to turn absence into failure on
  the CI legs expected to have the checkout.
- **Prove the failure path.** A guard nobody has seen fail is not a guard, so
  each has a failure-path test that reproduces the defect it exists for.
  `test_check_namespace_drift.py` feeds it the exact shape of dfe-infra#136.
- **Fail on an unverifiable entry.** A consumer listed with nothing to compare
  it against fails rather than passing silently.

## Propagation, where a mirror cannot be avoided

Some SSoT values legitimately have mirrors -- `versions.yaml` feeds appset pins,
`Chart.yaml` appVersions and bare `version:` keys, and Renovate can only edit
the SSoT. `renovate-propagate.yml` closes that: it triggers on Renovate's own
branch push, runs `check_versions_drift.py --fix` to write every mirror,
re-resolves the digests and pushes back, so the PR arrives green rather than
red-with-homework.

Prefer deriving over mirroring. The otel collector held a second copy of the
ClickHouse endpoint and crashlooped on the profile it was not written for; the
fix was to derive it from the shared fact, which removed the mirror rather than
synchronising it.

## Adding a shared fact

1. Put the SSoT in one file and say in a comment that it is the SSoT.
2. Derive consumers from it where you can; mirror only where you must.
3. Let Renovate move it if an upstream owns the value.
4. Write the guard, with a failure-path test.
5. Wire the guard into `helm-lint.yml`, and into the pre-flight if a wrong
   value would reach a cluster.
