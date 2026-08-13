// Project:   dfe-infra
// File:      tests/e2e-ui/specs/dfe-ui/smoke.spec.ts
// Purpose:   dfe-ui loads from the deployed stack
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// Landing smoke: the deployed dfe-ui serves its shell. Grows into login and
// role-visibility specs when the engine OIDC path reaches the UI.

import { expect, test } from '@playwright/test';

test('landing page serves the app shell', async ({ page }) => {
  const response = await page.goto('/');
  expect(response?.ok()).toBeTruthy();
  await expect(page).toHaveTitle(/DFE UI/);
});
