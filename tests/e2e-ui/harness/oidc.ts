// Project:   dfe-infra
// File:      tests/e2e-ui/harness/oidc.ts
// Purpose:   Drive the engine's OIDC redirect flow through the IdP login form
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// The browser walks the real flow: engine /login 302s to the IdP, the form is
// submitted, the IdP redirects back to the engine callback, whose JSON body
// carries the engine-minted token. The token is returned rather than relying
// on the dfe_token cookie, which is Secure-only and dropped on http rigs.

import { type Page } from '@playwright/test';

import { ENGINE_URL } from './env';

export const OIDC_PROVIDER = process.env.E2E_OIDC_PROVIDER ?? 'dex';

export const FIXTURE_PASSWORD = process.env.E2E_FIXTURE_PASSWORD ?? '';

export interface OidcSession {
  readonly token: string;
  readonly email: string;
  readonly groups: string[];
}

export async function engineOidcLogin(
  page: Page,
  username: string,
  password: string,
): Promise<OidcSession> {
  await page.goto(`${ENGINE_URL}/api/v1/auth/oidc/${OIDC_PROVIDER}/login`);
  await page.locator('#login').fill(username);
  await page.locator('#password').fill(password);
  await page.locator('button[type="submit"]').click();
  await page.waitForURL(`${ENGINE_URL}/**`, { timeout: 30_000 });
  const body = await page.evaluate(() => document.body.innerText);
  const result = JSON.parse(body) as {
    access_token?: string;
    token?: string;
    email: string;
    groups: string[];
  };
  const token = result.access_token ?? result.token;
  if (token === undefined) {
    throw new Error(`callback carried no token: ${body.slice(0, 200)}`);
  }
  return { token, email: result.email, groups: result.groups };
}
