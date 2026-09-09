// Project:   dfe-infra
// File:      tests/e2e-ui/specs/hyperdx/tenancy.spec.ts
// Purpose:   Org tenancy through the hyperdx UI: per-org sources, row isolation
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// The browser-level half of the tenancy proof: the seeded "nerk events" source
// renders exactly the nerk rows, "acme events" exactly the acme rows. Requires
// an auth-mode hyperdx seeded via DEFAULT_CONNECTIONS/DEFAULT_SOURCES and the
// 3+2 demo rows in dfe.main.
//
// Sources are resolved to ids via the API and selected by URL: driving the
// picker resets the time window to Live Tail, which hides seeded rows.

import { type Page } from '@playwright/test';

import { sourcesByName } from '../../harness/ch';
import { expect, test } from '../../harness/fixtures';

const LOOKBACK_MS = 90 * 24 * 3600 * 1000;

async function countEventsFor(page: Page, sourceName: string): Promise<number> {
  const sources = await sourcesByName(page.request);
  const id = sources.get(sourceName);
  if (id === undefined) {
    throw new Error(`source ${sourceName} is not seeded`);
  }
  const to = Date.now();
  const from = to - LOOKBACK_MS;
  await page.goto(
    `/search?source=${id}&where=&select=&whereLanguage=lucene&isLive=false&from=${from}&to=${to}`,
  );
  await expect(page.getByText('End of Results')).toBeVisible({
    timeout: 30_000,
  });
  return page.getByRole('button', { name: 'View details for log entry' }).count();
}

test.describe('org tenancy through the UI', () => {
  test('both per-org sources are offered', async ({ hyperdxPage }) => {
    await hyperdxPage.goto('/search');
    await hyperdxPage.getByTestId('source-selector').click();
    await expect(
      hyperdxPage.getByRole('option', { name: 'nerk events' }),
    ).toBeVisible();
    await expect(
      hyperdxPage.getByRole('option', { name: 'acme events' }),
    ).toBeVisible();
  });

  test('nerk source renders exactly the nerk rows', async ({ hyperdxPage }) => {
    expect(await countEventsFor(hyperdxPage, 'nerk events')).toBe(3);
  });

  test('acme source renders exactly the acme rows', async ({ hyperdxPage }) => {
    expect(await countEventsFor(hyperdxPage, 'acme events')).toBe(2);
  });
});
