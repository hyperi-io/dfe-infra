// Project:   dfe-infra
// File:      tests/e2e-ui/specs/selftest/oidc-live-mock.spec.ts
// Purpose:   Prove the oidc-live harness mechanics against a local mock, offline
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// The same defineOidcLiveTests the live spec runs, fed the synthetic fixtures in
// fixtures/oidc-live and pointed at harness/idp/mock-idp.ts. A pass here says the
// harness flow works; it says nothing about a real IdP's markup.

import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test } from '@playwright/test';

import { entra } from '../../harness/idp/entra';
import { google } from '../../harness/idp/google';
import { GOOGLE_SESSION_COOKIE, type MockOidc, startMockOidc } from '../../harness/idp/mock-idp';
import { okta } from '../../harness/idp/okta';
import { type IdpKind } from '../../harness/idp/types';
import {
  defineOidcLiveTests,
  IDP_MODULES,
  loadLiveProviders,
  parseExpectations,
  probeAuthorize,
  signInThroughIdp,
} from '../../harness/oidc-live';

const FIXTURES = path.join(path.dirname(fileURLToPath(import.meta.url)), '../../fixtures/oidc-live');
const PASSWORD = 'mock-password';

const KINDS: Record<string, IdpKind> = {
  dex: 'dex',
  okta: 'okta',
  entra: 'entra',
  'google-workspace': 'google',
};

let mock: MockOidc | undefined;
const env: Record<string, string | undefined> = { E2E_OIDC_PASSWORD: PASSWORD };

function mockUrl(): string {
  if (mock === undefined) throw new Error('the mock is not running');
  return mock.url;
}

test.beforeAll(async ({}, testInfo) => {
  const providers: Record<string, { kind: IdpKind; refuse?: boolean }> = {};
  for (const [name, kind] of Object.entries(KINDS)) {
    providers[name] = { kind };
    providers[`${name}-refused`] = { kind, refuse: true };
  }
  const user = (roles: string[]) => ({ password: PASSWORD, roles });
  mock = await startMockOidc({
    providers,
    users: {
      'mock-dex-admin': user(['admin']),
      'mock-dex-nobody': user([]),
      'mock-okta-admin': user(['admin']),
      'mock-okta-nobody': user([]),
      'mock-entra-nested': user(['data_analyst']),
      'mock-entra-nobody': user([]),
      'mock-google-viewer': user(['data_viewer']),
    },
  });
  const state = testInfo.outputPath('google-session.json');
  const cookie = {
    name: GOOGLE_SESSION_COOKIE,
    value: 'seeded',
    domain: '127.0.0.1',
    path: '/',
    expires: -1,
    httpOnly: false,
    secure: false,
    sameSite: 'Lax',
  };
  fs.mkdirSync(path.dirname(state), { recursive: true });
  fs.writeFileSync(state, JSON.stringify({ cookies: [cookie], origins: [] }));
  env.E2E_OIDC_STORAGE_STATE_GOOGLE_WORKSPACE = state;
});

test.afterAll(async () => {
  await mock?.close();
});

defineOidcLiveTests(
  loadLiveProviders({ E2E_OIDC_EXPECTATIONS: FIXTURES }),
  mockUrl,
  env,
);

test.describe('harness checks', () => {
  for (const [name, kind] of Object.entries(KINDS)) {
    test(`the ${kind} probe catches a refused authorize request`, async ({ request }) => {
      const reason = await probeAuthorize(request, IDP_MODULES[kind], mockUrl(), `${name}-refused`);
      expect(reason).toBeDefined();
    });
  }

  test('the classifiers read the measured refusal signals', () => {
    const none = { status: 200, location: '', body: '' };
    expect(
      google.authorizeError({
        ...none,
        status: 302,
        location: 'https://accounts.google.com/signin/oauth/error?authError=abc',
      }),
    ).toContain('authError abc');
    expect(
      google.authorizeError({
        ...none,
        status: 302,
        location: 'https://accounts.google.com/v3/signin/identifier?x=1',
      }),
    ).toBeUndefined();
    expect(okta.authorizeError({ ...none, status: 400 })).toContain('400');
    expect(okta.authorizeError({ ...none, status: 302, location: '/signin' })).toBeUndefined();
    expect(entra.authorizeError({ ...none, body: 'AADSTS700016: app not found' })).toContain(
      'AADSTS700016',
    );
    expect(entra.authorizeError({ ...none, body: '<html>Sign in</html>' })).toBeUndefined();
  });

  test('a roles-only expectation carries no login', () => {
    const parsed = parseExpectations(
      { provider: 'okta', users: { a: ['admin'], b: { login: 'x', roles: [] } } },
      'inline',
    );
    expect(parsed.kind).toBe('okta');
    expect(parsed.users.map((u) => [u.fixture, u.login])).toEqual([
      ['a', undefined],
      ['b', 'x'],
    ]);
    expect(() => parseExpectations({ users: {} }, 'inline')).toThrow(/provider/);
    expect(() => parseExpectations({ provider: 'p', users: { a: {} } }, 'inline')).toThrow(/roles/);
  });

  test('a probe-only provider joins the expectations ones', () => {
    const providers = loadLiveProviders({
      E2E_OIDC_EXPECTATIONS: path.join(FIXTURES, 'expectations-okta.json'),
      E2E_OIDC_PROBE: 'corp-sso:google, okta',
    });
    expect(providers.map((p) => [p.provider, p.kind, p.users.length])).toEqual([
      ['corp-sso', 'google', 0],
      ['okta', 'okta', 3],
    ]);
  });

  test('an expired Google session fails instead of typing a password', async ({ browser }) => {
    const context = await browser.newContext();
    try {
      const page = await context.newPage();
      await expect(
        signInThroughIdp(page, google, mockUrl(), 'google-workspace', {
          login: 'mock-google-viewer',
          password: '',
        }),
      ).rejects.toThrow(/seeded session has expired/);
    } finally {
      await context.close();
    }
  });
});
