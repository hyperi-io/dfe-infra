// Project:   dfe-infra
// File:      tests/e2e-ui/specs/tenancy/org-matrix.spec.ts
// Purpose:   org_id isolation matrix for the fixture identities, via ClickHouse
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// The ClickHouse end of the RBAC chain, as the reconciler-minted identities:
// an org-scoped group sees only its org, platform roles see everything, a
// group spanning several registered orgs fails closed (no identity minted at
// all), and a query-text pin override is refused. Row counts follow the
// deployment's demo seeds (SEEDS below).
//
// Env contract: E2E_CH_URL (default localhost:18124) and E2E_CH_CREDS_JSON,
// a {username: password} map for the identities under test -- minted by the
// governance reconciler, never committed.

import { type APIRequestContext } from '@playwright/test';

import { expect, test } from '../../harness/fixtures';

const CH_URL = process.env.E2E_CH_URL ?? 'http://localhost:18124';
const CREDS: Record<string, string> = JSON.parse(
  process.env.E2E_CH_CREDS_JSON ?? '{}',
);

const SEEDS: Record<string, number> = {
  nerk: 3,
  acme: 2,
  test_org: 4,
  test_org_2: 1,
};
const TOTAL = Object.values(SEEDS).reduce((a, b) => a + b, 0);

async function chCountAs(
  api: APIRequestContext,
  user: string,
  sql: string,
): Promise<{ ok: boolean; value?: number; body: string }> {
  const resp = await api.post(`${CH_URL}/?default_format=JSONCompact`, {
    headers: {
      'X-ClickHouse-User': user,
      'X-ClickHouse-Key': CREDS[user] ?? '',
      'Content-Type': 'text/plain',
    },
    data: sql,
  });
  const body = await resp.text();
  if (!resp.ok()) {
    return { ok: false, body };
  }
  const parsed = JSON.parse(body) as { data: [[string | number]] };
  return { ok: true, value: Number(parsed.data[0]?.[0]), body };
}

const COUNT = 'SELECT count() FROM dfe.main';

test.describe('org_id isolation matrix', () => {
  test.skip(
    Object.keys(CREDS).length === 0,
    'E2E_CH_CREDS_JSON not set (mint via the governance reconciler)',
  );

  test('org-scoped group sees only its org', async ({ request }) => {
    const r = await chCountAs(request, 'dfe_grp_dfe-test-org-viewers', COUNT);
    expect(r.ok, r.body).toBeTruthy();
    expect(r.value).toBe(SEEDS.test_org);
  });

  test('org-level identity sees only its org', async ({ request }) => {
    const r = await chCountAs(request, 'dfe_org_test_org_2', COUNT);
    expect(r.ok, r.body).toBeTruthy();
    expect(r.value).toBe(SEEDS.test_org_2);
  });

  test('platform analyst reads across every org', async ({ request }) => {
    const r = await chCountAs(request, 'dfe_grp_dfe-analysts', COUNT);
    expect(r.ok, r.body).toBeTruthy();
    expect(r.value).toBe(TOTAL);
  });

  test('platform admin group reads across every org', async ({ request }) => {
    const r = await chCountAs(request, 'dfe_grp_dfe-admins', COUNT);
    expect(r.ok, r.body).toBeTruthy();
    expect(r.value).toBe(TOTAL);
  });

  test('a group spanning several orgs has no identity at all', async ({
    request,
  }) => {
    // Fail-closed: the binding is skipped, so authentication itself fails.
    const resp = await request.post(`${CH_URL}/?default_format=JSONCompact`, {
      headers: {
        'X-ClickHouse-User': 'dfe_grp_dfe-multi-viewers',
        'X-ClickHouse-Key': 'any',
        'Content-Type': 'text/plain',
      },
      data: COUNT,
    });
    expect(resp.status()).toBe(403);
  });

  test('query-text pin override is refused, never other rows', async ({
    request,
  }) => {
    const r = await chCountAs(
      request,
      'dfe_grp_dfe-test-org-viewers',
      `${COUNT} SETTINGS SQL_current_tenant_id='nerk'`,
    );
    expect(r.ok).toBeFalsy();
    expect(r.body).toMatch(/READONLY|SETTING_CONSTRAINT_VIOLATION|164|452/);
  });
});

// Platform telemetry: composed onto admin and infra-admin identities only.
// Requires the deployment's otel.probe seed (one row).
const OTEL_COUNT = 'SELECT count() FROM otel.probe';

test.describe('otel database access', () => {
  test.skip(
    Object.keys(CREDS).length === 0,
    'E2E_CH_CREDS_JSON not set (mint via the governance reconciler)',
  );

  for (const user of ['dfe_grp_dfe-admins', 'dfe_grp_dfe-infra']) {
    test(`${user} reads otel`, async ({ request }) => {
      const r = await chCountAs(request, user, OTEL_COUNT);
      expect(r.ok, r.body).toBeTruthy();
      expect(r.value).toBe(1);
    });
  }

  for (const user of ['dfe_grp_dfe-analysts', 'dfe_grp_dfe-test-org-viewers']) {
    test(`${user} is denied otel`, async ({ request }) => {
      const r = await chCountAs(request, user, OTEL_COUNT);
      expect(r.ok).toBeFalsy();
      expect(r.body).toMatch(/ACCESS_DENIED|Not enough privileges|497/);
    });
  }
});
