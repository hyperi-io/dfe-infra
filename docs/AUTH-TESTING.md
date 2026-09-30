# Auth testing -- where the identities come from

Proving auth end to end needs real identities in a real identity provider. `dfe-ops idp` stands one up on the deployment's own cluster, so a test never borrows a shared IdP or edits its redirect URIs.

## The tester IdP

`dfe-ops idp deploy` installs Dex with a glauth LDAP directory behind it and routes it on the deployment's gateway. The users live in glauth because Dex's static password database emits no `groups` claim, and the groups claim is what the engine maps to roles.

- `deploy` generates the client secret and one shared user password and writes both to a mode-0600 file (`--secrets-out`, default `.tmp/tester-idp.env`). Neither is printed or committed. The e2e specs read that password as `E2E_FIXTURE_PASSWORD`, from the file's `TESTER_IDP_USER_PASSWORD` line.
- `wire-engine` hands the client credentials, the provider definition, the group map and the CA bundle to the engine's namespace, then prints the chart values that pick them up.
- `status` reports whether it is serving, and `teardown` removes it.

Every environment value is a flag: `python3 scripts/dfe-ops idp deploy --help` lists them.

## The fixture

Two files, and the group NAMES are the contract between them:

- `bootstrap/fixtures/tester-idp-users.toml` -- the users and groups the IdP serves. `--users-file` on `deploy` points it at a different directory.
- `bootstrap/fixtures/tester-idp-groups.toml` -- the engine's role, scope and org ids for each of those groups. `wire-engine` writes it to the `dfe-auth-groups` ConfigMap, which `authConfig.groupsConfigMap` seeds into the engine. `--groups-file` points it at a different map.
- Each group file carries `source_provider`, the `--provider` name (default `dex`), and `source_id`, the claim value that links to it (the entry's `source_id`, else its name). From dfe-engine #669 on, the engine links a claim value to a group only through those two, never through the group's name, so a login through any other provider name gets none of these groups.

The engine seeds its own four default groups only into an empty group store, so the map repeats them. Every user is a row in `tests/e2e-ui/specs/engine/oidc-rba.spec.ts`, and `scripts/tests/test_tester_idp.py` fails if the two files stop granting what that spec asserts.

The set is wider than a happy-path login on purpose:

| Fixture user | Carries | Proves |
|---|---|---|
| `dfe-test` | `dfe-admins` and `dfe-viewers` | A multi-group claim resolves to the union of the roles |
| `dfe-admin` | `dfe-admins` | The allow-everything baseline |
| `dfe-test-org-viewer` | `dfe-test-org-viewers`, scoped to `test_org` | An org-scoped role binds at that org and names it in `org_ids` |
| `dfe-multi-viewer` | `dfe-multi-viewers`, system scope over `test_org` and `test_org_2` | One group confers two orgs, and an org role held at system scope does not over-grant |
| `dfe-nobody` | `dfe-nogroup`, which maps to no role | Authentication succeeds and authorisation denies -- every screen refuses |
| `dfe-nested-member` | `dfe-nested-child`, which nests `dfe-nested-parent` | Nested membership never reaches the groups claim, so only direct groups grant |

`dfe-infra-admin`, `dfe-infra-viewer`, `dfe-analyst`, `dfe-analyst-viewer`, `dfe-viewer` and `dfe-operator` carry one role group each.

`dfe-nobody` is the one people forget. A feature that renders for a user with no roles is a bug, and it is the cheapest one to catch.

## Real providers

The tester IdP proves the engine's login, claim and role mapping. Proving a real provider such as Entra ID, Okta or Google Workspace behaves needs identities in your own tenant of it.
