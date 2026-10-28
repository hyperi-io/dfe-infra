---
sidebar_position: 2
sidebar_label: Users and OIDC
---

# Users and OIDC

Two local accounts work from the first minute, `admin` and `breakglass`. Your identity provider (IdP) comes next. Do it in this order.

## 1. Pick your IdP guide

Each guide is written for your IdP administrator. Send them the guide for your provider, the DFE console host and a provider name. They follow it once and send back the client ID, the client secret, the issuer and the group identifiers.

| IdP | Guide | Provider type | A group links on |
|---|---|---|---|
| Google Workspace | [Google](../dfe-engine/control-plane/idp-setup/google.md) | `google` | The Cloud Identity group id, the part after `groups/` |
| Microsoft Entra ID | [Entra](../dfe-engine/control-plane/idp-setup/entra.md) | `entra_id` | The group's object id (a GUID) |
| Okta | [Okta](../dfe-engine/control-plane/idp-setup/okta.md) | `okta` | The group name the `groups` claim carries |

Every guide uses the same redirect URI:

```text
https://<your DFE console host>/api/v1/auth/oidc/<provider-name>/callback
```

Any other OIDC provider registers as `generic`. It has no directory API, so its groups come from a claim in the token, or you map them by hand.

## 2. First login and break-glass

The deploy mints both passwords. Outside a dev setup the engine refuses to start on a default admin password.

| Target | Where the logins are |
|---|---|
| Docker | `make up` prints the access summary and `make creds` prints it again. A pipe or a log gets the `.env` key name in place of the password |
| Kubernetes | `dfe-ops stack-deploy` writes `.tmp/<stack>-<mode>/access-summary.md` in your dfe-infra checkout, readable by you only. `dfe-ops creds` prints the command that fetches each login |

Sign in as `admin`. The engine forces a password change at the first login. The console then walks a setup wizard: an organisation, a sign-in step for your IdP that you can skip for now, and your own account. The `admin` account stays after that as your local way in, and you reset its password under Settings.

`breakglass` is the recovery admin for when the IdP, the secret store or the engine's own state is gone. Its bcrypt hash is committed to your deploy repo at `governance/settings/auth.yaml`, so it survives a rebuild. Keep its password somewhere safe. With gitops off there is nowhere durable for the hash, and no `breakglass` account. Once your IdP sign-in works, `enabled: false` in that file switches it off, and a login attempt then says why it was refused.

## 3. Connect the IdP

Register the provider one of three ways: the wizard's sign-in step, the OIDC Provider Management tab in the console's admin settings, or `POST /api/v1/auth/oidc-providers`. You send the credential values. The engine writes them to the secret store, and the provider file keeps only the path.

Then check it before anyone tries to sign in. `GET /api/v1/auth/oidc-providers/<name>/verify-login` reports whether the client ID and secret resolve and the issuer's discovery document is reachable. It never returns the secret.

## 4. Map groups to roles

This is the step that bites. A group's name links nothing. An IdP group called `dfe-admins` gets no roles until an admin links it, because directory display names are not unique, and where users can create groups anyone can pick one.

A link takes two fields on the DFE group, set together:

- `source_provider`, the provider name from step 3.
- `source_id`, the identifier from the table above.

Set them in the console's Group Management tab, through `POST` or `PUT /api/v1/auth/groups`, or in the group's file. A linked group with no role admits nobody. The sign-in returns `401 no_role_granted`. So link your first group while signed in as `admin` or `breakglass`, and give it a role.

A fresh install seeds four groups:

| Group | Role | For |
|---|---|---|
| `dfe-admins` | `admin` | Full access |
| `dfe-analysts` | `data_analyst` | Sources, hunts, rules, queries |
| `dfe-viewers` | `data_viewer` | Dashboards and queries |
| `dfe-infra` | `infra_admin` | Services and deployments |

Roles sit on two planes. Control-plane roles decide what a person can do. `org_viewer` is the data-plane role that holds a person to the rows of their own organisations. [The vocabulary](../dfe-engine/control-plane/rbac-vocabulary.md) lists every role and the group names that carry them.

A provider in `api` mode lists your directory's groups on a schedule. A synced group arrives linked and with no roles. You assign them by hand.

## The detail

- [RBAC vocabulary](../dfe-engine/control-plane/rbac-vocabulary.md): every role, every default group, the naming rules.
- [Identity resolution and first login](../dfe-engine/control-plane/rbac-sign-in.md): the sign-in allowlist, local accounts, rotating the two passwords.
- [IdP group sync and links](../dfe-engine/control-plane/rbac-groups.md): the sync, and what the groups API refuses.
- [OIDC and RBAC architecture](../dfe-engine/control-plane/oidc-rbac-architecture.md): the provider model and the three group modes.
- [RBAC, multi-tenant ClickHouse and auth](../dfe-engine/control-plane/rbac.md): the full reference, with the ClickHouse isolation model.
- [The engine's own reads](../dfe-engine/control-plane/rbac-engine-reads.md): what the engine reads as itself.
- [How the console signs in](../dfe-ui/authentication.md), and [the gateway alternative](../dfe-infra/deployment/gateway-oidc.md), where Envoy takes the login instead.
