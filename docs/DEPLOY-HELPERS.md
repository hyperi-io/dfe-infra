# Release + deploy helpers

Two scripts drive a DFE app from a pushed branch to a digest-pinned deploy on a
target k8s cluster. Both wrap their `git`/`gh`/`kubectl` calls inside a single
`python3` process so an unattended (AFK) run does not stall.

## Why wrap in python3 (the AFK note)

A raw `git push`, `gh`, or `kubectl` prompts for permission on each call, which
stalls an unattended run. The AFK dispatcher only evaluates the TOP-LEVEL
command, so the same operation inside a `python3` helper auto-runs -- it sees
one pre-approved `python3` invocation, not the `gh`/`kubectl` calls underneath.
That is the whole reason these helpers exist: they are AFK-safe, scoped wrappers
around operations that would otherwise prompt.

## scripts/dfe-release.py -- scoped GHCR release driver

`--repo` is checked against a fixed allowlist of hyperi-io DFE app repos
(dfe-engine, dfe-ui, dfe-hyperdx, and the six Rust fleet apps: dfe-receiver,
dfe-loader, dfe-archiver, dfe-fetcher, dfe-transform-vrl,
dfe-transform-vector). It cannot touch another org, delete a repo, or change
settings.

| Subcommand | What it does |
|---|---|
| `open --repo <r> --head <branch> --title <t> --body-file <f>` | Create the PR for a pushed branch; prints the PR number. Idempotent -- prints the existing PR if one is already open. `--base` defaults to `main`. |
| `merge --repo <r> --pr <n> [--admin] [--delete-branch]` | Merge to main with `--merge`, preserving the typed feat/fix commits so semantic-release sees them. `--admin` bypasses required checks on an authorised run. |
| `merge ... --publish [--subject <s>] [--note <n>]` | Squash-merge instead, stamping `Publish: true` on the squash message so the merge itself releases -- no separate `dispatch`. The squash subject is what `check-commits` validates on main, so its description must start lowercase; `--subject` overrides the PR title when it does not. |
| `dispatch --repo <r> [--workflow CI] [--ref main]` | Trigger the CI workflow with `from-head=true`, which runs semantic-release plus the GHCR image build/publish. It releases from head, so it skips the commit-message gate. |
| `wait --repo <r> [--workflow CI] [--interval 20] [--timeout 1800]` | Poll the latest CI run to completion; prints its conclusion. Long-running -- run it backgrounded. |
| `digest --repo <r> --version <v>` | Resolve the sha256 digest GHCR published for a version tag, via `gh api /orgs/hyperi-io/packages/container/<r>/versions`. |
| `ship --repo <r> --branch <b> --title <t> [--publish] [--body-file <f>]` | The whole chain: open/reuse the PR, merge, wait for CI, resolve the digest. Prints `SHIPPED <repo> <tag> <digest>`, progress on stderr. Long-running, like `wait`. |

The GHCR reads need a `read:packages` token, which `gh login` usually lacks.
`--env-file PATH` (repeatable, later wins) takes it from a flat KEY=VALUE file's
`GHCR_TOKEN` or `GH_TOKEN` -- ambient `GH_TOKEN` wins, and the value is never
printed. It precedes the subcommand; `dfe-stack` takes it too:

    python3 scripts/dfe-release.py --env-file bootstrap/.env digest --repo <r> --version <v>
    python3 scripts/dfe-stack --env-file bootstrap/.env verify

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

Both Secret names are helm values (`auth.adminSecretName`,
`auth.breakglassSecretName`), and `creds` reads the names the live engine
Deployment carries, so a renamed Secret still prints a fetch line that works.

Read both with the access you deployed with -- the kubeconfig for the two Secrets
on Kubernetes, the host `.env` on docker. Rotate the admin password through that
same Secret or `.env` key and never delete it, because the engine reasserts that
value on every boot. The break-glass plaintext may be deleted once you have
recorded it offline: the engine hashed it into the deploy repo on first boot and
reconciles the account from that hash.

### First login: the summary on your own machine

    python3 scripts/dfe-ops access-summary --kubeconfig .tmp/kubeconfig-dfe-b

`stack-deploy`, `cycle` and `bootstrap/bootstrap.sh` all end here, so a deploy
started any of the three ways writes `.tmp/<stack>-<mode>/access-summary.md` on
the machine that ran it, mode 0600. Unlike everything above it carries the two
passwords in PLAINTEXT, because a fetch command is no use to someone who has not
logged in yet. It also names the console, the engine API, and the three things to
do next. Record the values and delete the file; it is gitignored and must never
be committed or copied into the cluster.

### Moved: the admin password (#234)

`dfe-engine-seed-accounts` no longer carries an `admin-password` key. Two
Secrets each claiming to be the admin password is what #234 collapsed.

- `admin` -> Secret `dfe-engine-admin`, key `admin-password`.
- `breakglass` -> Secret `dfe-engine-breakglass`, key `breakglass-password`.

Both are minted in-cluster by an ESO Password generator with `CreatedOnce`, so
neither is a shipped default and neither rotates under a live session.
`dfe-engine-seed-accounts` keeps only its `seed-accounts` key -- the named team
logins (#106). A deployment still reading `{.data.admin-password}` off the seed
Secret gets nothing back; use `dfe-ops creds`.

## Adding a second OIDC provider

A provider is a NAME, and the name is what everything keys off -- the callback
path `/api/v1/auth/oidc/<name>/callback`, the login route
`/api/v1/auth/oidc/<name>/login`, the entry the console's provider picker shows,
and the env var pair the engine reads its client credentials from. Nothing about
the vendor is special-cased, so a second provider is the same four objects the
tester IdP already uses, with a different name:

1. A Secret in the app namespace holding `client-id` and `client-secret` for the
   RP client registered at that IdP -- an ExternalSecret pulling from the
   estate's secret store where the `ClusterSecretStore` is ready, otherwise the
   Secret directly.
2. A key `<name>.yaml` added to the providers ConfigMap named by
   `authConfig.providersConfigMap` -- the same ConfigMap every provider shares,
   one key each. It carries `issuer`, `client_id_env`, `client_secret_env`,
   `scopes` and the `groups` block; `type` picks the directory adapter and
   `groups.mode: token_claim` means the id_token's claim IS the answer.
3. An entry under `oidc.providers` in the deployment's engine values, mapping
   those two env var names onto the Secret's keys.
4. The callback URI registered at the IdP. It has to be the URI the engine
   actually emits, which is why `api.forwardedAllowIps` matters: the gateway
   terminates TLS and forwards plain http, so unless its peer address is
   believed the engine builds `http://` callbacks and a hosted IdP refuses to
   register them.

Two things bite on a private-CA deployment. `authConfig.caBundleConfigMap` sets
`SSL_CERT_FILE`, which REPLACES the trust store rather than adding to it -- the
chart's init container merges the image's public roots in for that reason, so a
deployment can reach a hosted IdP and its own private-CA IdP at once. And the
bundle has to hold the CA that signed the CURRENT gateway certificate: a
reissued CA leaves a stale bundle behind, and the failure reads as an HTTP 500
on `/login` whose log line is `certificate verify failed`.

## Making a deploy-repo push reach Argo immediately

Argo CD polls the deploy repo every 300 s with up to 60 s of jitter, so a source
written through the engine takes 5 to 9 minutes to reach a pod. A push webhook
to Argo's `/api/webhook` collapses that to the sync itself.

On the BUNDLED deploy repo this is automatic: `bootstrap.sh` mints a shared
secret into `dfe-argo-webhook` (forgejo namespace) and `argocd-secret`'s
`webhook.gogs.secret`, and the Forgejo chart's setup Job registers the hook. The
Job says which of the two it could not find rather than failing the sync, so a
deployment with an adopted Argo CD still comes up -- it just keeps polling.

On an EXTERNAL deploy repo the same hook is configured on the provider. Argo CD
verifies the delivery against a key in `argocd-secret`, so mint one and put it
in both places:

    kubectl -n argocd patch secret argocd-secret --type merge \
      -p '{"stringData":{"webhook.github.secret":"<shared secret>"}}'

Then add the webhook on the provider, against the Argo CD server's public
address:

| Provider | Where | Payload URL | Content type | Secret field | Events |
|---|---|---|---|---|---|
| GitHub | repo Settings -> Webhooks -> Add | `https://<argocd host>/api/webhook` | `application/json` | Secret | Just the push event |
| GitLab | project Settings -> Webhooks | `https://<argocd host>/api/webhook` | n/a | Secret token, and `webhook.gitlab.secret` in argocd-secret | Push events |

Argo CD's handler parses GitHub, GitLab, Bitbucket, Bitbucket Server, Azure
DevOps and Gogs. It has no Gitea or Forgejo parser, which is why the bundled
path registers a `gogs`-type hook. A self-hosted Argo behind a private CA needs
the provider to trust that CA, or the deliveries fail verification and the
deployment silently falls back to the poll.

## Trusting the DFE certificate

Which CA signed the gateway's `*.<domain>` certificate is a per-deployment
choice. [DEPLOY-TLS-TRUST.md](DEPLOY-TLS-TRUST.md) covers both modes -- the
self-signed default and estate PKI -- and what each asks of a developer box.

## The end-to-end release + deploy recipe

Ordered. The clean end state is a GHCR digest-pinned deploy -- never a `:dev`
tag.

1. Push the branch with typed commits, then ship it:

       python3 scripts/dfe-release.py ship --repo <r> --branch <b> --title <t> --publish --body-file <f>

   Exit 3 means a publish is in flight on main for that repo -- merging now
   would cancel it, so wait and re-run. Recover a failed gate with `merge
   --admin` then `dispatch`.

2. Point the overlay at the new release. Port-forward forgejo to
   `localhost:13000` first, then:

       python3 scripts/dfe-ops deploy-values get values/<app>-default-values.yaml --kubeconfig .tmp/kubeconfig-dfe-b

   Edit the image tag/digest to the new release, then put it back. ArgoCD
   auto-syncs:

       python3 scripts/dfe-ops deploy-values put values/<app>-default-values.yaml --file <local> --kubeconfig .tmp/kubeconfig-dfe-b

3. Verify the rollout against the DFE cluster:

       kubectl --kubeconfig .tmp/kubeconfig-dfe-b -n dfe-local rollout status deploy/<app>
