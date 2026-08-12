# Auth testing -- where the identities come from

Proving auth end to end needs real identities in a real identity provider. Those
already exist, and they are NOT in this repo. This page says what exists and how
to reach it, so nobody rebuilds it.

Nothing here is a credential. Every secret is held in the estate's secret store
and fetched by the tooling described below.

## The fixture

The estate's infrastructure repository carries a `dfe-oidc-testing` subproject.
One file defines every test user and group; a renderer projects that same
definition into four providers, so a test written once runs against any of them
by changing one variable:

- **dex** (backed by an LDAP directory) -- headless, deterministic, no browser,
  no rate limit. Develop against this one.
- **Entra ID**, **Okta**, **Google Workspace** -- real providers, for proving a
  real provider behaves rather than proving our plumbing.

The subproject also renders drop-in provider config for dfe-engine, so wiring the
engine to a provider is a file copy rather than a configuration exercise.

## The identities that matter to us

Twelve users span the RBAC roles. Two of them exist specifically to prove
**tenant isolation**, which is what makes them worth naming here:

| Fixture user | Carries | Proves |
|---|---|---|
| `dfe-acme-viewer` | `customer_viewer` on one org | Sees ONLY that org's rows |
| `dfe-multi-viewer` | `customer_viewer` on two orgs | Sees exactly those two, no more |
| `dfe-nobody` | no roles at all | Default deny -- every screen refuses |
| `dfe-admin` | `admin` | The allow-everything baseline |

`dfe-nobody` is the one people forget. A feature that renders for a user with no
roles is a bug, and it is the cheapest one to catch.

Provider coverage is uneven by design: Okta's free plan caps active users, and
the Google credential is deliberately read-only against the real directory. dex
carries the full set.

## What this repo has to do with it

Nothing automated, yet. The fixture is a laptop-and-engine asset: it renders
provider config you copy into a dfe-engine checkout. Nothing copies it into a
cluster, and no deployed environment here points at any of these providers.

Closing that gap is the work -- see the auth/tenancy plan and the cross-repo
issues it tracks. The relevant point for anyone picking this up: **the identity
provider side is done. Do not build another one.**

## Reaching it

The subproject's own `devpack/README.md` is the operating manual -- how to fetch
credentials from the secret store, which variable switches provider, and how to
read a failed login. Access to the underlying tenants is granted per person and
is documented in the infrastructure repository, not here.

Ask in the team channel if you need the pack and cannot find it.
