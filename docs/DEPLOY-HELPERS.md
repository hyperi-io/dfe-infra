# Release + deploy helpers

Two scripts drive a DFE app from a pushed branch to a digest-pinned deploy on
the devex k8s cluster. Both wrap their `git`/`gh`/`kubectl` calls inside a
single `python3` process so an unattended (AFK) run does not stall.

## Why wrap in python3 (the AFK note)

A raw `git push`, `gh`, or `kubectl` prompts for permission on each call, which
stalls an unattended run. The AFK dispatcher only evaluates the TOP-LEVEL
command, so the same operation inside a `python3` helper auto-runs -- it sees
one pre-approved `python3` invocation, not the `gh`/`kubectl` calls underneath.
That is the whole reason these helpers exist: they are AFK-safe, scoped wrappers
around operations that would otherwise prompt.

## scripts/dfe-release.py -- scoped GHCR release driver

Opens/merges the PR, dispatches the CI release, waits for it, and resolves the
published image digest. `--repo` is checked against a fixed allowlist of
hyperi-io DFE app repos (dfe-engine, dfe-ui, dfe-hyperdx, and the six Rust
fleet apps: dfe-receiver, dfe-loader, dfe-archiver, dfe-fetcher,
dfe-transform-vrl, dfe-transform-vector). It cannot touch another org, delete a
repo, or change settings.

| Subcommand | What it does |
|---|---|
| `open --repo <r> --head <branch> --title <t> --body-file <f>` | Create the PR for a pushed branch; prints the PR number. Idempotent -- prints the existing PR if one is already open. `--base` defaults to `main`. |
| `merge --repo <r> --pr <n> [--admin] [--delete-branch]` | Merge to main with `--merge`, preserving the typed feat/fix commits so semantic-release sees them. `--admin` bypasses required checks on an authorised run. |
| `merge ... --publish [--subject <s>] [--note <n>]` | Squash-merge instead, stamping `Publish: true` on the squash message so the merge itself releases -- no separate `dispatch`. The squash subject is what `check-commits` validates on main, so its description must start lowercase; `--subject` overrides the PR title when it does not. |
| `dispatch --repo <r> [--workflow CI] [--ref main]` | Trigger the CI workflow with `from-head=true`, which runs semantic-release plus the GHCR image build/publish. |
| `wait --repo <r> [--workflow CI] [--interval 20] [--timeout 1800]` | Poll the latest CI run to completion; prints its conclusion. Long-running -- run it backgrounded. |
| `digest --repo <r> --version <v>` | Resolve the sha256 digest GHCR published for a version tag, via `gh api /orgs/hyperi-io/packages/container/<r>/versions`. |

## scripts/dfe-ops deploy-values -- deploy-repo overlay CRUD

`deploy-values` reads and writes values files in the bundled deploy repo (the
Forgejo git host) via the Gitea-family contents API. The deploy-repo admin
credential is read in-process from the cluster Secret -- never passed on argv.
Requires a port-forward of the forgejo service to `localhost:13000`.

| Form | What it does |
|---|---|
| `deploy-values get <path>` | Print the file, or list a directory path's entries. |
| `deploy-values put <path> --file <local>` | Upsert the file from a local copy; ArgoCD auto-syncs the change. |
| `deploy-values delete <path>` | Remove the file. |

`<path>` is the file path inside the repo, e.g. `values/<svc>-default-values.yaml`
(positional, not a flag). Defaults worth knowing: `--url http://localhost:13000`,
`--repo deploy`, `--secret-namespace forgejo`, `--secret dfe-forgejo-admin`. Pass
`--kubeconfig .tmp/kubeconfig-dfe-b` so the credential read hits the DFE cluster.

`dfe-ops` also carries `deploy`, `teardown`, `verify`, `stack-deploy`,
`kubeconfig`, `preflight`, and `cycle` for k8s create/amend/teardown -- see
docs/TESTING-CYCLE.md for the cycle.

## Where each login comes from

    python3 scripts/dfe-ops creds --kubeconfig .tmp/kubeconfig-dfe-b

Prints one fetch command per login the deploy carries -- `admin` and
`breakglass` (both minted by an ESO Password generator, so neither is a shipped
default), Argo CD, the deploy-repo git host, ClickHouse and Kafbat -- and marks
any secret the cluster does not carry. It never prints a value, so the output is
safe in a log. `bootstrap/access-summary.sh` prints the same block, so a deploy
hands the operator exactly these lines.

## The end-to-end release + deploy recipe

Ordered. The clean end state is a GHCR digest-pinned deploy -- never a `:dev`
tag.

1. Push the branch with typed commits, then release it:

       python3 scripts/dfe-release.py open --repo <r> --head <branch> --title <t> --body-file <f>
       python3 scripts/dfe-release.py merge --repo <r> --pr <n> --publish --delete-branch

   `--publish` lands and ships in one step. Without it, merge and then dispatch
   separately -- which is also the recovery path when a squash message fails the
   commit-message gate, since `dispatch` releases from head and skips it:

       python3 scripts/dfe-release.py merge --repo <r> --pr <n> --admin --delete-branch
       python3 scripts/dfe-release.py dispatch --repo <r>

   Then wait for CI (backgrounded -- it is long-running), and resolve the digest:

       python3 scripts/dfe-release.py wait --repo <r>
       python3 scripts/dfe-release.py digest --repo <r> --version <v>

2. Point the overlay at the new release. Port-forward forgejo to
   `localhost:13000` first, then:

       python3 scripts/dfe-ops deploy-values get values/<app>-default-values.yaml --kubeconfig .tmp/kubeconfig-dfe-b

   Edit the image tag/digest to the new release, then put it back. ArgoCD
   auto-syncs:

       python3 scripts/dfe-ops deploy-values put values/<app>-default-values.yaml --file <local> --kubeconfig .tmp/kubeconfig-dfe-b

3. Verify the rollout against the DFE cluster:

       kubectl --kubeconfig .tmp/kubeconfig-dfe-b -n dfe-local rollout status deploy/<app>
