// Project:   dfe-infra
// File:      tests/e2e-ui/harness/users.ts
// Purpose:   Test identities the suite acts as
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// Today: one local (email/password) account for auth-mode hyperdx. The OIDC
// role matrix grows here from the shared 12-user fixture (docs/AUTH-TESTING.md)
// as its login path lands, together with the per-role capability truth table
// the RBA specs assert against -- hand-written, never derived from engine
// config, so the tests stay an independent oracle.

export interface TestUser {
  readonly email: string;
  readonly password: string;
}

export const LOCAL_ADMIN: TestUser = {
  email: process.env.E2E_USER ?? 'e2e-admin@example.com',
  password: process.env.E2E_PASSWORD ?? 'E2e-Admin-Passw0rd!',
};
