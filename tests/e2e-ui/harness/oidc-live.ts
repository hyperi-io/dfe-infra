// Project:   dfe-infra
// File:      tests/e2e-ui/harness/oidc-live.ts
// Purpose:   Live OIDC sign-in against hosted IdPs, and the expectations it asserts
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// The browser runs the engine's real authorization-code flow: the engine's
// login with ?redirect=false hands back the authorize URL, the harness adds
// login_hint, the IdP's own page signs the user in, and the engine's callback
// mints the token whose /auth/me roles are checked against the expectations.
// Every tenant value -- logins, provider names, passwords -- comes from env or
// files the env names, never from this repository.

import * as fs from 'node:fs';
import * as path from 'node:path';

import {
  type APIRequestContext,
  type Browser,
  expect,
  type Page,
  test,
} from '@playwright/test';

import { dex } from './idp/dex';
import { entra } from './idp/entra';
import { google } from './idp/google';
import { okta } from './idp/okta';
import {
  IDP_KINDS,
  type IdpKind,
  type IdpModule,
  type IdpUser,
} from './idp/types';

export const IDP_MODULES: Record<IdpKind, IdpModule> = { dex, okta, entra, google };

export type Env = Readonly<Record<string, string | undefined>>;

export interface LiveUser {
  // The fixture name: the only user identity a test title or log line carries.
  readonly fixture: string;
  readonly login: string | undefined;
  readonly roles: readonly string[];
}

export interface LiveProvider {
  // The engine's provider name, a path segment of its login and callback.
  readonly provider: string;
  readonly kind: IdpKind | undefined;
  readonly users: readonly LiveUser[];
}

// The engine route that answers 403 to a caller holding no role.
export const ROLE_GATED_PATH = '/api/v1/oidc-providers';

const LIVE_TIMEOUT_MS = 120_000;

export function envSuffix(provider: string): string {
  return provider.toUpperCase().replace(/[^A-Z0-9]/g, '_');
}

export function inferKind(provider: string, declared?: unknown): IdpKind | undefined {
  if (typeof declared === 'string') {
    const kind = IDP_KINDS.find((candidate) => candidate === declared);
    if (kind === undefined) {
      throw new Error(`kind ${JSON.stringify(declared)} is not one of ${IDP_KINDS.join(', ')}`);
    }
    return kind;
  }
  const name = provider.toLowerCase();
  if (name === 'dex' || name.startsWith('dex-')) return 'dex';
  if (name.includes('okta')) return 'okta';
  if (name.includes('entra') || name.includes('azure')) return 'entra';
  if (name.includes('google')) return 'google';
  return undefined;
}

function isRoleList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((role) => typeof role === 'string');
}

// Reads {"provider", "users": {fixture: {"login", "roles"} | [roles]}}; a list-only
// user carries no login and its test skips with the reason.
export function parseExpectations(raw: unknown, source: string): LiveProvider {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    throw new Error(`${source}: not a JSON object`);
  }
  const doc = raw as Record<string, unknown>;
  const provider = doc.provider;
  if (typeof provider !== 'string' || provider === '') {
    throw new Error(`${source}: "provider" must name the engine provider`);
  }
  const usersRaw = doc.users;
  if (typeof usersRaw !== 'object' || usersRaw === null || Array.isArray(usersRaw)) {
    throw new Error(`${source}: "users" must map fixture names to expectations`);
  }
  const users: LiveUser[] = [];
  for (const [fixture, entry] of Object.entries(usersRaw)) {
    if (isRoleList(entry)) {
      users.push({ fixture, login: undefined, roles: entry });
      continue;
    }
    const fields = (typeof entry === 'object' && entry !== null ? entry : {}) as Record<
      string,
      unknown
    >;
    if (!isRoleList(fields.roles)) {
      throw new Error(`${source}: user ${fixture} needs "roles", a list of role names`);
    }
    const login = typeof fields.login === 'string' && fields.login !== '' ? fields.login : undefined;
    users.push({ fixture, login, roles: fields.roles });
  }
  return { provider, kind: inferKind(provider, doc.kind), users };
}

function expectationFiles(entry: string): string[] {
  if (!fs.statSync(entry).isDirectory()) {
    return [entry];
  }
  return fs
    .readdirSync(entry)
    .filter((name) => name.startsWith('expectations-') && name.endsWith('.json'))
    .sort()
    .map((name) => path.join(entry, name));
}

// E2E_OIDC_EXPECTATIONS lists files or directories of expectations-*.json;
// E2E_OIDC_PROBE adds providers to probe that have no users yet (name or name:kind).
export function loadLiveProviders(env: Env): LiveProvider[] {
  const byName = new Map<string, LiveProvider>();
  const listed = (env.E2E_OIDC_EXPECTATIONS ?? '').split(path.delimiter).filter(Boolean);
  for (const entry of listed) {
    for (const file of expectationFiles(entry)) {
      const parsed = parseExpectations(JSON.parse(fs.readFileSync(file, 'utf-8')), file);
      if (byName.has(parsed.provider)) {
        throw new Error(`${file}: provider ${parsed.provider} is listed twice`);
      }
      byName.set(parsed.provider, parsed);
    }
  }
  const probes = (env.E2E_OIDC_PROBE ?? '').split(',').map((s) => s.trim()).filter(Boolean);
  for (const probe of probes) {
    const [provider = '', kind] = probe.split(':');
    if (!byName.has(provider)) {
      byName.set(provider, { provider, kind: inferKind(provider, kind), users: [] });
    }
  }
  return [...byName.values()].sort((a, b) => a.provider.localeCompare(b.provider));
}

// The password env var for a provider: E2E_OIDC_PASSWORD_<PROVIDER>, else E2E_OIDC_PASSWORD.
export function passwordEnvName(provider: string, env: Env): string {
  const specific = `E2E_OIDC_PASSWORD_${envSuffix(provider)}`;
  return env[specific] !== undefined ? specific : 'E2E_OIDC_PASSWORD';
}

export function storageStateEnvName(provider: string): string {
  return `E2E_OIDC_STORAGE_STATE_${envSuffix(provider)}`;
}

export async function authorizationUrl(
  request: APIRequestContext,
  engineUrl: string,
  provider: string,
  loginHint?: string,
): Promise<string> {
  const response = await request.get(
    `${engineUrl}/api/v1/auth/oidc/${provider}/login?redirect=false`,
  );
  expect(response.status(), `engine login for provider ${provider}`).toBe(200);
  const body = (await response.json()) as { authorization_url?: unknown };
  if (typeof body.authorization_url !== 'string') {
    throw new Error(`engine login for ${provider} carried no authorization_url`);
  }
  const url = new URL(body.authorization_url);
  if (loginHint !== undefined) {
    url.searchParams.set('login_hint', loginHint);
  }
  return url.toString();
}

// Why the IdP refused the engine's authorize request, or undefined when it accepted it.
export async function probeAuthorize(
  request: APIRequestContext,
  idp: IdpModule,
  engineUrl: string,
  provider: string,
): Promise<string | undefined> {
  const url = await authorizationUrl(request, engineUrl, provider);
  const response = await request.get(url, { maxRedirects: 0, failOnStatusCode: false });
  const location = response.headers()['location'] ?? '';
  // An OAuth error the IdP sends back to the redirect URI rather than rendering.
  if (location.includes(`/api/v1/auth/oidc/${provider}/callback`)) {
    const error = new URL(location, url).searchParams.get('error');
    if (error !== null) {
      return `${idp.kind} returned the authorize request with error=${error}`;
    }
  }
  const body = location === '' ? await response.text() : '';
  return idp.authorizeError({ status: response.status(), location, body });
}

export interface EngineSession {
  readonly token: string;
  readonly groups: readonly string[];
}

// Runs the whole flow in `page`: the engine session cookie set by the login call
// rides the page's own context, so the callback finds its state and verifier.
export async function signInThroughIdp(
  page: Page,
  idp: IdpModule,
  engineUrl: string,
  provider: string,
  user: IdpUser,
): Promise<EngineSession> {
  const url = await authorizationUrl(page.request, engineUrl, provider, user.login);
  const callbackPath = `/api/v1/auth/oidc/${provider}/callback`;
  const callback = page.waitForResponse(
    (response) => new URL(response.url()).pathname === callbackPath,
    { timeout: LIVE_TIMEOUT_MS },
  );
  // Handled here so a sign-in that throws first leaves no unhandled rejection behind.
  callback.catch(() => undefined);
  await page.goto(url);
  await idp.signIn(page, user, callback);
  const response = await callback;
  const text = await response.text();
  if (response.status() !== 200) {
    throw new Error(`engine callback answered ${response.status()}: ${text.slice(0, 300)}`);
  }
  const body = JSON.parse(text) as { access_token?: unknown; groups?: unknown };
  if (typeof body.access_token !== 'string') {
    throw new Error('engine callback carried no access_token');
  }
  return { token: body.access_token, groups: isRoleList(body.groups) ? body.groups : [] };
}

export async function engineRoles(
  request: APIRequestContext,
  engineUrl: string,
  token: string,
): Promise<string[]> {
  const response = await request.get(`${engineUrl}/api/v1/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(response.status(), '/auth/me with the minted token').toBe(200);
  const me = (await response.json()) as { roles?: unknown };
  return isRoleList(me.roles) ? [...me.roles].sort() : [];
}

export async function roleGatedStatus(
  request: APIRequestContext,
  engineUrl: string,
  token: string,
): Promise<number> {
  const response = await request.get(`${engineUrl}${ROLE_GATED_PATH}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return response.status();
}

type UserPlan =
  | { readonly skip: string }
  | {
      readonly skip: undefined;
      readonly idp: IdpModule;
      readonly user: IdpUser;
      readonly storageState: string | undefined;
    };

export function planUser(provider: LiveProvider, user: LiveUser, env: Env): UserPlan {
  const idp = provider.kind === undefined ? undefined : IDP_MODULES[provider.kind];
  if (idp === undefined) {
    return { skip: `no IdP kind for ${provider.provider}; set "kind" in its file` };
  }
  if (user.login === undefined) {
    return { skip: `no login for ${user.fixture}: its entry is roles only` };
  }
  if (!idp.needsPassword) {
    const stateEnv = storageStateEnvName(provider.provider);
    const storageState = env[stateEnv];
    if (storageState === undefined || storageState === '') {
      return { skip: `${stateEnv} unset: no seeded session to sign in from` };
    }
    return { skip: undefined, idp, user: { login: user.login, password: '' }, storageState };
  }
  const passwordEnv = passwordEnvName(provider.provider, env);
  const password = env[passwordEnv];
  if (password === undefined || password === '') {
    return { skip: `${passwordEnv} unset: no password for ${provider.provider}` };
  }
  return { skip: undefined, idp, user: { login: user.login, password }, storageState: undefined };
}

async function assertUser(
  browser: Browser,
  provider: LiveProvider,
  user: LiveUser,
  plan: Exclude<UserPlan, { readonly skip: string }>,
  engineUrl: string,
): Promise<void> {
  const context = await browser.newContext(
    plan.storageState === undefined ? {} : { storageState: plan.storageState },
  );
  try {
    const page = await context.newPage();
    const session = await signInThroughIdp(
      page,
      plan.idp,
      engineUrl,
      provider.provider,
      plan.user,
    );
    const roles = await engineRoles(context.request, engineUrl, session.token);
    expect(roles, `${user.fixture} roles`).toEqual([...user.roles].sort());
    if (user.roles.length === 0) {
      // The engine admits any authenticated user with a zero-role account; refusal is authorisation.
      const status = await roleGatedStatus(context.request, engineUrl, session.token);
      expect(status, `${user.fixture} on ${ROLE_GATED_PATH}`).toBe(403);
    }
  } finally {
    await context.close();
  }
}

// One describe per provider: the authorize probe, then one test per expected user.
// `engineUrl` is read when a test runs, so a self-test can start its server first.
export function defineOidcLiveTests(
  providers: readonly LiveProvider[],
  engineUrl: () => string,
  env: Env,
): void {
  if (providers.length === 0) {
    test('oidc-live has providers to test', () => {
      test.skip(true, 'E2E_OIDC_EXPECTATIONS and E2E_OIDC_PROBE are unset (tests/e2e-ui/README.md)');
    });
    return;
  }
  for (const provider of providers) {
    test.describe(`provider ${provider.provider}`, () => {
      test.describe.configure({ timeout: LIVE_TIMEOUT_MS });
      // Google's browser sign-in rides a hand-seeded session, so it never gates a run.
      const tag = provider.kind === 'google' ? ['@optional'] : [];

      test('the IdP accepts the engine authorize request', async ({ request }) => {
        const idp = provider.kind === undefined ? undefined : IDP_MODULES[provider.kind];
        test.skip(idp === undefined, `no IdP kind for ${provider.provider}`);
        if (idp === undefined) return;
        const reason = await probeAuthorize(request, idp, engineUrl(), provider.provider);
        expect(reason, 'the IdP refusal, if any').toBeUndefined();
      });

      for (const user of provider.users) {
        test(`${user.fixture} signs in with roles [${user.roles.join(', ')}]`, { tag }, async ({
          browser,
        }) => {
          const plan = planUser(provider, user, env);
          if (plan.skip !== undefined) {
            test.skip(true, plan.skip);
            return;
          }
          await assertUser(browser, provider, user, plan, engineUrl());
        });
      }
    });
  }
}
