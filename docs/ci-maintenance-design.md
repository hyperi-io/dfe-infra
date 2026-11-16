<!--
  Project:      dfe-infra
  File:         docs/ci-maintenance-design.md
  Purpose:      How a shared fact stays current across repos: a bot to move it,
                a gate to show it moved.
  License:      BUSL-1.1
  Copyright:    (c) 2026 HYPERI PTY LIMITED
-->

# Keeping shared facts current

Every shared fact gets two mechanisms, never one: a bot to move it, and a gate to show it moved.

A bot alone raises a pull request and has no opinion about whether anyone merges it. A gate alone says something is stale and leaves a human to work out the value.

dfe-schemas was once consumed as a git submodule by dfe-engine, dfe-loader and dfe-fetcher. Renovate ships its `git-submodules` manager DISABLED (it's opt-in beta), so no bot watched those pins and nothing reported the silence. The three consumers drifted to three different commits, engine current, loader 26 behind and fetcher 27, and two apps built against a schema definition the third had corrected, with nothing failing or warning. dfe-schemas ships as a pinned wheel and no suite repo has a submodule, so `check_submodule_drift.py` keeps an empty table for the next one.

The bot is Renovate. It's configured once in the org preset (`github>hyperi-io/renovate-config`), so a repo joins by having a dependency, not by remembering to opt in. It raises a PR under the standing policy: `fix(deps):` so merging ships, a 7-day cooldown for external dependencies, PR-only with no automerge.

The gate is a `check_*` script in `scripts/`. Each one asserts that a fact resolved in more than one place still agrees, and fails CI when it doesn't. They all work the same way: read the SSoT, read every mirror, report each disagreement by name, exit non-zero.

| guard | asserts |
|---|---|
| `check_versions_drift.py` | every pin mirror matches `versions.yaml` |
| `check_namespace_drift.py` | the operator-namespace SSoT matches where the appsets install |
| `check_submodule_drift.py` | every consumer of a shared submodule pins the same commit (none listed) |
| `check_governance_drift.py` | governance actions resolve to chart values that exist |
| `test_governance_actions.py` | the thin path rendered with each governance action leaves no pod reading a Secret nothing mints |
| `check_image_pins.py` | every app pin's tag exists in the registry and its digest equals the pinned one |

All but `check_image_pins.py` run in `helm-lint.yml` on every PR, and the deploy-affecting ones run again in the `dfe-ops stack-deploy` pre-flight. `check_image_pins.py` reads the registry, so it runs by hand when pins change.

## Rules a guard follows

- Name what disagreed, not that something did. `dfe-loader: pins dfe-schemas at b7e58c6, 26 commits behind 6586f54` is actionable. "drift detected" isn't.
- Say how to close it. The submodule guard ends with "Renovate raises the bump PR; merging it is what closes this." <!-- docs-gate: allow semicolon -- quotes the guard's own output -->
- A skip isn't a pass. When a guard can't verify (no `gh`, no token, a private repo it can't read) it says so loudly and never reports a pass. `check_governance_drift.py` takes `--require` to turn absence into failure on the CI legs expected to have the checkout.
- Test the failure path. A guard nobody has seen fail isn't a guard, so each has a failure-path test that reproduces the defect it exists for. `test_check_namespace_drift.py` feeds it the exact case of dfe-infra#136.
- Fail on an unverifiable entry. A consumer listed with nothing to compare it against fails instead of passing silently.

## Propagation, where a mirror cannot be avoided

Some SSoT values legitimately have mirrors. `versions.yaml` feeds appset pins, `Chart.yaml` appVersions and bare `version:` keys, and Renovate edits the SSoT alone. `renovate-propagate.yml` closes that gap:

- It triggers on Renovate's branch push and re-resolves the digests.
- It runs `check_versions_drift.py --fix` to write every mirror.
- It gates on the drift check and pushes back once it passes, so the PR arrives passing.

Renovate ignores the propagator's commits (`gitIgnoredAuthors`), so it still rebases the branch. `refresh-digests` rejects a stack with a git tag, because a released stack is frozen. The digest or app move goes into the next cut ([cutting-a-stack.md](cutting-a-stack.md)).

Prefer deriving over mirroring. The otel collector held a second copy of the ClickHouse endpoint and crashlooped on the profile it wasn't written for. The fix was to derive it from the shared fact, which removed the mirror instead of synchronising it.

## Hard-coding a shared fact is a defect

A literal that restates a fact defined elsewhere is the same failure as a stale mirror, minus the guard. Every one found reported a broken pipeline against a healthy one:

| literal | what it broke |
|---|---|
| otel exporter's ClickHouse endpoint | collector crashlooped on `no such host` -- single mode emits `dfe-clickhouse`, the operator a headless Service alone |
| smoke-test pod selector `app.kubernetes.io/name=dfe-clickhouse` | reported a dead datastore against three healthy servers |
| the same selector in the KEDA scale proof | `no ClickHouse pod found` |
| smoke-test `CH_URL` | built from a literal, ignoring the correct host resolved nine lines above it |

When you find one, replace it with the value's source, in this order:

1. Derive it from the shared fact in scope. The otel endpoint became `printf "tcp://%s:%v" .Values.clickhouse.host .Values.clickhouse.nativePort`.
2. Read it from the thing being tested. A smoke test asks the pod what it's configured with instead of asserting what it should be, so it tests the deployment and not its own assumptions.
3. Take it from the config cascade. For a scalo service, the terminal default belongs in the binary and every layer above it overrides. For a chart, the SSoT is `argocd/values/common.yaml` and the chart default is the standalone-render fallback and nothing more.

A literal is acceptable in exactly one case: it IS the definition, appears once, and is overridable. `bootstrap.sh` naming the bundled Forgejo URL is that case. It's the one statement of the fact and `DFE_CONFIG_REPO_URL` replaces it.

To add a shared fact:

1. Put the SSoT in one file and say in a comment that it's the SSoT.
2. Derive consumers from it where you can. Mirror where you must and nowhere else.
3. Let Renovate move it if an upstream owns the value.
4. Write the guard, with a failure-path test.
5. Wire the guard into `helm-lint.yml`, and into the pre-flight if a wrong value would reach a cluster.
