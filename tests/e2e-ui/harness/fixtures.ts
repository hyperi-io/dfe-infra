// Project:   dfe-infra
// File:      tests/e2e-ui/harness/fixtures.ts
// Purpose:   Extended Playwright test with authenticated-session fixtures
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// hyperdxState logs in ONCE per worker and caches the session as storageState;
// hyperdxPage hands each test a page already inside that session, so specs
// never repeat the login flow. Multi-user support (the OIDC role matrix) will
// generalise the worker fixture into a per-user state cache in the same shape.

import * as path from 'node:path';

import { test as base, type Page } from '@playwright/test';

import { hyperdxLocalLogin } from './auth';
import { HYPERDX_URL } from './env';
import { LOCAL_ADMIN } from './users';

interface HarnessFixtures {
  hyperdxPage: Page;
}

interface HarnessWorkerFixtures {
  hyperdxState: string;
}

export const test = base.extend<HarnessFixtures, HarnessWorkerFixtures>({
  hyperdxState: [
    async ({ browser }, use, workerInfo) => {
      const statePath = path.join(
        workerInfo.project.outputDir,
        `hyperdx-state-${workerInfo.workerIndex}.json`,
      );
      const context = await browser.newContext({ baseURL: HYPERDX_URL });
      await hyperdxLocalLogin(context, LOCAL_ADMIN);
      await context.storageState({ path: statePath });
      await context.close();
      await use(statePath);
    },
    { scope: 'worker' },
  ],
  hyperdxPage: async ({ browser, hyperdxState }, use) => {
    const context = await browser.newContext({
      baseURL: HYPERDX_URL,
      storageState: hyperdxState,
    });
    const page = await context.newPage();
    await use(page);
    await context.close();
  },
});

export { expect } from '@playwright/test';
