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

import { DFE_UI_URL, ENGINE_URL, HYPERDX_URL } from './harness/env';

// A trace records typed passwords and bearer tokens (microsoft/playwright#19992),
// so a project that signs in to a real IdP keeps no trace, screenshot or video.
const NO_ARTIFACTS = { trace: 'off', screenshot: 'off', video: 'off' } as const;

// A failed test writes the page's aria snapshot, typed password included, to
// error-context.md whatever the artifact settings say. Playwright reads this
// switch from the environment only, so it covers every project.
process.env.PLAYWRIGHT_NO_COPY_PROMPT = '1';

export default defineConfig({
  testDir: './specs',
  fullyParallel: false, // one deployment under test; specs share server state
  retries: 0,
  workers: 1,
  timeout: 60_000,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    // A CI artifact may be published, so traces stay local.
    trace: process.env.CI ? 'off' : 'retain-on-failure',
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
    {
      name: 'engine',
      testMatch: 'engine/**/*.spec.ts',
      testIgnore: 'engine/oidc-live.spec.ts',
      use: { ...devices['Desktop Chrome'], baseURL: ENGINE_URL },
    },
    {
      name: 'oidc-live',
      testMatch: 'engine/oidc-live.spec.ts',
      use: { ...devices['Desktop Chrome'], baseURL: ENGINE_URL, ...NO_ARTIFACTS },
    },
    {
      name: 'oidc-live-selftest',
      testMatch: 'selftest/**/*.spec.ts',
      use: { ...devices['Desktop Chrome'], ...NO_ARTIFACTS },
    },
    {
      name: 'tenancy',
      testMatch: 'tenancy/**/*.spec.ts',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
