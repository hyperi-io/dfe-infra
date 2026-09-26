# Auth testing -- where the identities come from

Proving auth end to end needs real identities in a real identity provider. `dfe-ops idp` stands one up on the deployment's own cluster, so a test never borrows a shared IdP or edits its redirect URIs.

## The tester IdP

`dfe-ops idp deploy` installs Dex with a glauth LDAP directory behind it and routes it on the deployment's gateway. The users live in glauth because Dex's static password database emits no `groups` claim, and the groups claim is what the engine maps to roles.

- `deploy` generates the client secret and one shared user password and writes both to a mode-0600 file (`--secrets-out`, default `.tmp/tester-idp.env`). Neither is printed or committed.
- `wire-engine` hands the client credentials, the provider definition and the CA bundle to the engine's namespace.
- `status` reports whether it is serving, and `teardown` removes it.

Every environment value is a flag: `python3 scripts/dfe-ops idp deploy --help` lists them.

## The fixture

The users and groups are `bootstrap/fixtures/tester-idp-users.toml`. Group NAMES are the contract with the engine's group-to-role files. `--users-file` points the IdP at a different directory shape.

The set is wider than a happy-path login on purpose:

| Fixture user | Carries | Proves |
|---|---|---|
| `dfe-admin` | `dfe-admins` | The allow-everything baseline |
| `dfe-multi-viewer` | `dfe-viewers` and `dfe-analysts` | A multi-group claim resolves to the union of the roles |
| `dfe-nobody` | `dfe-nogroup`, which maps to no role | Authentication succeeds and authorisation denies -- every screen refuses |
| `dfe-nested-member` | `dfe-nested-child`, which nests `dfe-nested-parent` | A group-to-role mapping resolves exactly one way through a nested group |

`dfe-analyst`, `dfe-viewer` and `dfe-operator` carry one role group each.

`dfe-nobody` is the one people forget. A feature that renders for a user with no roles is a bug, and it is the cheapest one to catch.

## Real providers

The tester IdP proves the engine's login, claim and role mapping. Proving a real provider such as Entra ID, Okta or Google Workspace behaves needs identities in your own tenant of it.
