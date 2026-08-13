// Project:   dfe-infra
// File:      tests/e2e-ui/specs/hyperdx/tenancy-proxy.spec.ts
// Purpose:   Row isolation + override refusal via the authenticated query proxy
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// DOM-independent half of the tenancy proof: drives hyperdx's clickhouse-proxy
// with a logged-in session's cookies. Asserts the counts per org connection
// and that a query-text override of the tenant pin is a hard refusal.

import { chCount, chQuery, connectionsByName } from '../../harness/ch';
import { expect, test } from '../../harness/fixtures';

test('per-connection isolation and override refusal via the proxy', async ({
  hyperdxPage,
}) => {
  const api = hyperdxPage.request;

  const byName = await connectionsByName(api);
  expect([...byName.keys()].sort()).toEqual(['acme', 'nerk']);

  const counts: Record<string, number> = {};
  for (const [name, id] of byName) {
    counts[name] = await chCount(api, id, 'SELECT count() FROM dfe.default');
  }
  expect(counts).toEqual({ nerk: 3, acme: 2 });

  // Pin override in attacker-authored query text must fail whole-query
  // (readonly tier wall 164 / pin wall 452), never fall back to other rows.
  const nerkId = byName.get('nerk');
  if (nerkId === undefined) {
    throw new Error('nerk connection missing');
  }
  const attack = await chQuery(
    api,
    nerkId,
    "SELECT count() FROM dfe.default SETTINGS SQL_current_tenant_id='acme'",
  );
  expect(attack.ok()).toBeFalsy();
  const body = await attack.text();
  expect(body).toMatch(/READONLY|SETTING_CONSTRAINT_VIOLATION|164|452/);
});
