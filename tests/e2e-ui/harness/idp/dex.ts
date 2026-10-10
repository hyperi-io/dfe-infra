// Project:   dfe-infra
// File:      tests/e2e-ui/harness/idp/dex.ts
// Purpose:   Sign in at the tester IdP's Dex password form
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED

import { type IdpModule } from './types';

export const dex: IdpModule = {
  kind: 'dex',
  needsPassword: true,
  // fill, never pressSequentially: a stored password with a trailing newline submits early.
  signIn: async (page, user) => {
    await page.locator('input[name="login"]').fill(user.login);
    await page.locator('input[name="password"]').fill(user.password);
    await page.locator('#submit-login').click();
  },
  authorizeError: ({ status }) =>
    status >= 400 ? `dex answered the authorize request with ${status}` : undefined,
};
