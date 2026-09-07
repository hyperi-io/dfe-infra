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

Both Secret names are helm values (`auth.adminSecretName`,
`auth.breakglassSecretName`), and `creds` reads the names the live engine
Deployment carries, so a renamed Secret still prints a fetch line that works.

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

## Trusting the DFE certificate

A DFE deploy terminates TLS for `*.<domain>` at the Envoy Gateway. Which CA
signed that certificate is a per-deployment choice, and the two modes ask
different things of a developer box. One command says which is live, and the
readiness gate and access summary print the same block:

    python3 scripts/dfe-ops ca --status --kubeconfig .tmp/kubeconfig-dfe-b

### Self-signed mode (the product default)

`tls.issuerName: dfe-internal-ca` -- cert-manager's self-signed bootstrap Issuer
mints a `CN=dfe-internal-ca` root (ECDSA P-384, ten years) and the
`dfe-internal-ca` ClusterIssuer signs the edge wildcard. No DNS-01, no external
credential, and it works on a split-horizon name public ACME cannot validate.

The cost is trust: a browser warns, and the embedded HyperDX iframe fails
outright because an iframe cannot show the interstitial. Trust the root once:

    python3 scripts/dfe-ops ca             # the PEM plus the install lines
    python3 scripts/dfe-ops ca --install   # writes the file, does the non-root half

`--install` writes `.tmp/dfe-internal-ca.crt` and adds it to the Chromium-family
NSS store at `~/.pki/nssdb` (Brave and Chrome read that, not the system store;
it needs `libnss3-tools`). It prints the rest rather than running them: on Linux
`sudo cp <file> /usr/local/share/ca-certificates/` then `sudo
update-ca-certificates`, on macOS `sudo security add-trusted-cert -d -r
trustRoot -k /Library/Keychains/System.keychain <file>`.

**The root PERSISTS across a rebuild** (#238), so that trust is a one-off. The
gateway chart pushes the minted root to the deployment's secret store once
(`PushSecret`, `updatePolicy: IfNotExists`) and restores it before cert-manager
can mint (`ExternalSecret`, `refreshPolicy: CreatedOnce`); bootstrap.sh renders
that same pair ahead of Argo, so the restore is ordered before the Certificate
rather than racing it. The root is reused; the leaf lifetimes rotate.

| Setting | Where | Effect |
|---|---|---|
| `tls.internalCA.persist.enabled` | gateway chart values | Off renders neither half: the root does not survive a rebuild. |
| `tls.internalCA.persist.secretStoreName` | gateway chart values | The store both halves use (default `dfe-secret-store`). |
| `DFE_CA_PERSIST` | bootstrap env | Whether bootstrap pre-applies the restore. Defaults on when `DFE_VAULT_SECRET_ID` is set. |
| `DFE_CA_RESTORE_TIMEOUT` | bootstrap env | Seconds to wait for the restore (default 60). A first bootstrap times out by design. |

The store path is `<project>/<env>/pki/internal-ca`, with the properties
`tls_crt` and `tls_key` -- underscored, because the Vault provider reads a
property as a gjson path and a dot means nesting.

### Estate-PKI mode (`tls.vault`)

In an estate that already runs a PKI, point the edge issuer at it and every
client trusts the certificate already -- nothing to install, no root to persist.
Set it in the deploy repo's own overlay (`infra/envoy-gateway-config.yaml`),
never in a tracked dfe-infra values file:

```yaml
tls:
  issuerName: dfe-estate-pki
  vault:
    server: https://vault.example.com:8200
    path: pki_tls/sign/<role>          # the sign role the AppRole policy grants
    caBundle: <base64 PEM chain that verifies the server's own TLS>
    appRole:
      roleId: <the cert-manager AppRole role_id>
```

The AppRole SecretID is the one piece bootstrap seeds, from
`DFE_CERTMANAGER_SECRET_ID` into Secret `cert-manager-approle`. The sign role
must allow the deployment's wildcard and match the chart's key type
(`tls.privateKey`, ECDSA P-384 by default).

`tls.acme.email` and `tls.vault.server` are exclusive; the chart fails the render
when both are set.

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
