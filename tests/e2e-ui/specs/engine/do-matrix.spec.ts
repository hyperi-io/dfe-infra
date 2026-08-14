// Project:   dfe-infra
// File:      tests/e2e-ui/specs/engine/do-matrix.spec.ts
// Purpose:   Per-role SEE/DO probes against the engine API
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// What each role can DO (status per probe) and SEE (org visibility): admin
// passes authz everywhere, platform viewers read but never write, the
// org-scoped viewer and the no-roles identity get data surfaces only or
// nothing. The write probe sends an empty body: 422 proves authz passed
// without mutating anything, 403 proves it never got that far.
//
// Requires the deployed engine wired to an IdP serving the shared fixture,
// with the demo orgs registered, and E2E_FIXTURE_PASSWORD in the env.

import { ENGINE_URL } from '../../harness/env';
import { expect, test } from '../../harness/fixtures';
import { engineOidcLogin, FIXTURE_PASSWORD } from '../../harness/oidc';

interface Probe {
  readonly name: string;
  readonly method: 'GET' | 'POST';
  readonly path: string;
  readonly expect: Record<string, number>;
}

const PROBES: Probe[] = [
  {
    name: 'read accounts (account:read)',
    method: 'GET',
    path: '/api/v1/auth/accounts',
    expect: {
      'dfe-admin': 200,
      'dfe-analyst': 403,
      'dfe-viewer': 403,
      'dfe-operator': 403,
      'dfe-nobody': 403,
    },
  },
  {
    name: 'read sources (source:read)',
    method: 'GET',
    path: '/api/v1/sources',
    expect: {
      'dfe-admin': 200,
      'dfe-analyst': 200,
      'dfe-viewer': 200,
      'dfe-operator': 403,
      'dfe-nobody': 403,
    },
  },
  {
    name: 'create org (org:write, empty body)',
    method: 'POST',
    path: '/api/v1/orgs',
    expect: {
      'dfe-admin': 422,
      'dfe-analyst': 403,
      'dfe-viewer': 403,
      'dfe-operator': 403,
      'dfe-nobody': 403,
    },
  },
];

const ORG_VISIBILITY: Record<string, 'all' | 'none'> = {
  'dfe-admin': 'all',
  'dfe-viewer': 'all',
  'dfe-test-org-viewer': 'none',
  'dfe-nobody': 'none',
};

test.describe('per-role DO matrix', () => {
  test.skip(
    FIXTURE_PASSWORD === '',
    'E2E_FIXTURE_PASSWORD not set (see README: fetch-secrets)',
  );

  for (const probe of PROBES) {
    for (const [user, status] of Object.entries(probe.expect)) {
      test(`${user}: ${probe.name} -> ${status}`, async ({ page }) => {
        const session = await engineOidcLogin(page, user, FIXTURE_PASSWORD);
        const resp = await page.request.fetch(`${ENGINE_URL}${probe.path}`, {
          method: probe.method,
          headers: {
            Authorization: `Bearer ${session.token}`,
            'Content-Type': 'application/json',
          },
          data: probe.method === 'POST' ? '{}' : undefined,
        });
        expect(resp.status()).toBe(status);
      });
    }
  }

  for (const [user, visibility] of Object.entries(ORG_VISIBILITY)) {
    test(`${user} sees ${visibility} orgs`, async ({ page }) => {
      const session = await engineOidcLogin(page, user, FIXTURE_PASSWORD);
      const resp = await page.request.get(`${ENGINE_URL}/api/v1/orgs`, {
        headers: { Authorization: `Bearer ${session.token}` },
      });
      expect(resp.ok()).toBeTruthy();
      const body = (await resp.json()) as { items: Array<{ name: string }> };
      const names = body.items.map(o => o.name);
      if (visibility === 'all') {
        expect(names).toContain('test_org');
        expect(names.length).toBeGreaterThanOrEqual(2);
      } else {
        expect(names).toEqual([]);
      }
    });
  }
});
