// Project:   dfe-infra
// File:      tests/e2e-ui/harness/oidc.ts
// Purpose:   Drive the engine's OIDC redirect flow through the IdP login form
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// The browser walks the real flow: engine /login 302s to the IdP, the form is
// submitted, the IdP redirects back to the engine callback, whose JSON body
// carries the engine-minted token, or the engine's refusal of a user whose IdP
// groups link to no DFE role. The token is returned rather than relying on the
// dfe_token cookie, which is Secure-only and dropped on http rigs.

import { type BrowserContext, expect, type Page } from '@playwright/test';

import { ENGINE_URL } from './env';

export const OIDC_PROVIDER = process.env.E2E_OIDC_PROVIDER ?? 'dex';

export const FIXTURE_PASSWORD = process.env.E2E_FIXTURE_PASSWORD ?? '';

// The code the engine's callback answers 401 with for a user holding no role.
const NO_ROLE_GRANTED: number = 'no_role_granted';

// The engine's session cookie, set only on an admitted sign-in.
const ENGINE_SESSION_COOKIE = 'dfe_token';

export interface OidcSession {
  readonly token: string;
  readonly email: string;
  readonly groups: string[];
}

// The engine callback's answer: a token on admission, an error code on refusal.
export interface CallbackResult {
  readonly status: number;
  readonly token: string | undefined;
  readonly code: string | undefined;
  readonly email: string;
  readonly groups: string[];
  // The engine's message, or the start of a body that is not JSON.
  readonly detail: string;
}

function isStringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function stringField(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function jsonObject(text: string): Record<string, unknown> | undefined {
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // A proxy's error page is not JSON and carries none of the engine's fields.
  }
  return undefined;
}

export function readCallback(status: number, body: string): CallbackResult {
  const doc = jsonObject(body);
  const field = (key: string): string | undefined => stringField(doc?.[key]);
  const groups = doc?.groups;
  return {
    status,
    token: field('access_token') ?? field('token'),
    code: field('code'),
    email: field('email') ?? '',
    groups: isStringList(groups) ? groups : [],
    detail: doc === undefined ? body.slice(0, 300) : (field('message') ?? field('detail') ?? ''),
  };
}

// The token of an admitted sign-in; anything else throws with the engine's status and code.
export function sessionToken(result: CallbackResult): string {
  if (result.status === 200 && result.token !== undefined) {
    return result.token;
  }
  const code = result.code === undefined ? '' : ` ${result.code}`;
  throw new Error(`engine callback answered ${result.status}${code} with no session: ${result.detail}`);
}

// Runs the IdP form flow and returns the engine callback's answer, admitted or refused.
export async function engineOidcCallback(
  page: Page,
  username: string,
  password: string,
): Promise<CallbackResult> {
  const callbackPath = `/api/v1/auth/oidc/${OIDC_PROVIDER}/callback`;
  await page.goto(`${ENGINE_URL}/api/v1/auth/oidc/${OIDC_PROVIDER}/login`);
  await page.locator('#login').fill(username);
  await page.locator('#password').fill(password);
  const callback = page.waitForResponse(
    (response) => new URL(response.url()).pathname === callbackPath,
    { timeout: 30_000 },
  );
  // Handled here so a submit that throws first leaves no unhandled rejection behind.
  callback.catch(() => undefined);
  await page.locator('button[type="submit"]').click();
  const response = await callback;
  return readCallback(response.status(), await response.text());
}

export async function engineOidcLogin(
  page: Page,
  username: string,
  password: string,
): Promise<OidcSession> {
  const result = await engineOidcCallback(page, username, password);
  return { token: sessionToken(result), email: result.email, groups: result.groups };
}

// A user holding no role is refused at the callback with no token and no session cookie.
export async function expectNoRoleRefusal(
  context: BrowserContext,
  result: CallbackResult,
  who: string,
): Promise<void> {
  expect(result.status, `${who} callback status (${result.detail})`).toBe(401);
  expect(result.code, `${who} refusal code`).toBe(NO_ROLE_GRANTED);
  expect(result.token, `${who} callback token`).toBeUndefined();
  const cookies = (await context.cookies()).map((cookie) => cookie.name);
  expect(cookies, `${who} session cookie`).not.toContain(ENGINE_SESSION_COOKIE);
}
