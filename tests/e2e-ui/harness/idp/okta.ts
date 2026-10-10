// Project:   dfe-infra
// File:      tests/e2e-ui/harness/idp/okta.ts
// Purpose:   Sign in at an Okta org's hosted Identity Engine widget
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// The test user carries a password authenticator only: an org policy that
// prompts for a second factor stops this module at that screen.

import { type IdpModule } from './types';

export const okta: IdpModule = {
  kind: 'okta',
  needsPassword: true,
  signIn: async (page, user) => {
    // login_hint prefills the identifier; filling it again is harmless.
    await page.locator('input[name="identifier"]').fill(user.login);
    await page.getByRole('button', { name: 'Next' }).click();
    // Okta names the password field credentials.passcode; the URL does not change between screens.
    await page
      .locator('input[name="credentials.passcode"]')
      .fill(user.password);
    await page.getByRole('button', { name: 'Verify' }).click();
  },
  // Okta refuses a bad client or redirect URI with a 400 page.
  authorizeError: ({ status }) =>
    status >= 400 ? `Okta answered the authorize request with ${status}` : undefined,
};
