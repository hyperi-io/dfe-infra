// Project:   dfe-infra
// File:      tests/e2e-ui/playwright.config.ts
// Purpose:   Browser-level acceptance config for the deployed DFE stack
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// One Playwright project per deployed app, each with its own baseURL; specs
// live under specs/<app>/. Endpoints come from harness/env.ts (env-driven, so
// the same suite runs against any deployment). Run one app with
// --project=hyperdx / --project=dfe-ui.
//
// Chromium with a fresh profile per run -- never a personal browser profile.

import { defineConfig, devices } from '@playwright/test';

import { DFE_UI_URL, HYPERDX_URL } from './harness/env';

export default defineConfig({
  testDir: './specs',
  fullyParallel: false, // one deployment under test; specs share server state
  retries: 0,
  workers: 1,
  timeout: 60_000,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'hyperdx',
      testMatch: 'hyperdx/**/*.spec.ts',
      use: { ...devices['Desktop Chrome'], baseURL: HYPERDX_URL },
    },
    {
      name: 'dfe-ui',
      testMatch: 'dfe-ui/**/*.spec.ts',
      use: { ...devices['Desktop Chrome'], baseURL: DFE_UI_URL },
    },
  ],
});
