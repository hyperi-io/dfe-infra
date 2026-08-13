// Project:   dfe-infra
// File:      tests/e2e-ui/harness/auth.ts
// Purpose:   Login strategies against the deployed apps
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// hyperdx ships its login/register PAGES dev-only (users come from OIDC), so
// the local strategy drives the ungated API routes instead: register claims a
// fresh install (409 = already claimed), login sets the session cookie on the
// browser context. The OIDC redirect-flow strategy lands here in the same
// shape once the dex-backed fixture is reachable from the suite.

import { type BrowserContext } from '@playwright/test';

import { HYPERDX_URL } from './env';
import { type TestUser } from './users';

export async function hyperdxLocalLogin(
  context: BrowserContext,
  user: TestUser,
): Promise<void> {
  const api = context.request;

  const reg = await api.post(`${HYPERDX_URL}/api/register/password`, {
    data: {
      email: user.email,
      password: user.password,
      confirmPassword: user.password,
    },
  });
  if (!reg.ok() && reg.status() !== 409) {
    throw new Error(`register failed: ${reg.status()} ${await reg.text()}`);
  }

  // Success is a 303 towards the app root; following it would leave this
  // origin, so stop at the redirect itself.
  const login = await api.post(`${HYPERDX_URL}/api/login/password`, {
    data: { email: user.email, password: user.password },
    maxRedirects: 0,
  });
  if (login.status() !== 303) {
    throw new Error(`login failed: ${login.status()} ${await login.text()}`);
  }

  const me = await api.get(`${HYPERDX_URL}/api/me`);
  if (!me.ok()) {
    throw new Error(`no session after login: ${me.status()}`);
  }
}
