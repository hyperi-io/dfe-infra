// Project:   dfe-infra
// File:      tests/e2e-ui/specs/engine/oidc-rba.spec.ts
// Purpose:   OIDC login + role/org resolution for every fixture identity
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// The whole RBAC chain, per user: real IdP redirect flow -> engine callback
// -> minted token -> /auth/me roles and org_ids. A row expecting no roles is
// refused at the callback instead, with no token and no session. The table is
// the independent oracle (docs/auth-testing.md); a drifted group file or claim
// mapping fails the row that names the broken identity.
//
// Requires the tester IdP serving bootstrap/fixtures/tester-idp-users.toml
// (`dfe-ops idp deploy`), the engine wired to it with the group map in
// tester-idp-groups.toml (`dfe-ops idp wire-engine`), and E2E_FIXTURE_PASSWORD
// set to the TESTER_IDP_USER_PASSWORD that deploy wrote.

import { ENGINE_URL } from '../../harness/env';
import { expect, test } from '../../harness/fixtures';
import {
  engineOidcCallback,
  engineOidcLogin,
  expectNoRoleRefusal,
  FIXTURE_PASSWORD,
} from '../../harness/oidc';

interface Expectation {
  readonly user: string;
  readonly roles: string[];
  readonly orgIds: string[];
}

// dfe-nested-member expects NO roles and is refused at login: nested group
// membership verifiably does not reach the groups claim, so only direct groups grant.
const TRUTH_TABLE: Expectation[] = [
  { user: 'dfe-test', roles: ['admin', 'data_viewer'], orgIds: [] },
  { user: 'dfe-admin', roles: ['admin'], orgIds: [] },
  { user: 'dfe-test-org-viewer', roles: ['org_viewer'], orgIds: ['test_org'] },
  { user: 'dfe-nobody', roles: [], orgIds: [] },
  { user: 'dfe-infra-admin', roles: ['infra_admin'], orgIds: [] },
  { user: 'dfe-infra-viewer', roles: ['infra_viewer'], orgIds: [] },
  { user: 'dfe-analyst', roles: ['data_analyst'], orgIds: [] },
  { user: 'dfe-analyst-viewer', roles: ['data_analyst_viewer'], orgIds: [] },
  { user: 'dfe-viewer', roles: ['data_viewer'], orgIds: [] },
  { user: 'dfe-operator', roles: ['dfe_operator'], orgIds: [] },
  {
    user: 'dfe-multi-viewer',
    roles: ['org_viewer'],
    orgIds: ['test_org', 'test_org_2'],
  },
  { user: 'dfe-nested-member', roles: [], orgIds: [] },
];

test.describe('OIDC login and role resolution', () => {
  test.skip(
    FIXTURE_PASSWORD === '',
    'E2E_FIXTURE_PASSWORD not set (see docs/auth-testing.md)',
  );

  for (const expected of TRUTH_TABLE) {
    if (expected.roles.length === 0) {
      test(`${expected.user} is refused at login`, async ({ page }) => {
        const result = await engineOidcCallback(
          page,
          expected.user,
          FIXTURE_PASSWORD,
        );
        await expectNoRoleRefusal(page.context(), result, expected.user);
      });
      continue;
    }
    test(`${expected.user} resolves its fixture roles`, async ({ page }) => {
      const session = await engineOidcLogin(
        page,
        expected.user,
        FIXTURE_PASSWORD,
      );
      const resp = await page.request.get(`${ENGINE_URL}/api/v1/auth/me`, {
        headers: { Authorization: `Bearer ${session.token}` },
      });
      expect(resp.ok()).toBeTruthy();
      const me = (await resp.json()) as { roles: string[]; org_ids: string[] };
      expect(me.roles.sort()).toEqual([...expected.roles].sort());
      expect(me.org_ids.sort()).toEqual([...expected.orgIds].sort());
    });
  }
});
