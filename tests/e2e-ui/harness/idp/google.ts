// Project:   dfe-infra
// File:      tests/e2e-ui/harness/idp/google.ts
// Purpose:   Continue a Google sign-in from a session a person seeded once
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// Google refuses automated password entry, so this module never types one: the
// browser context starts from a storage state seeded by hand, and the module
// only answers the account chooser and the consent screen. A password prompt
// means that session has expired.

import { firstOf, type IdpModule } from './types';

// Google's refusal of an authorize request is a redirect to this page with authError.
const ERROR_PATH = '/signin/oauth/error';

// The chooser and consent screens a seeded session can still show, at most once each.
const MAX_SCREENS = 3;

export const google: IdpModule = {
  kind: 'google',
  needsPassword: false,
  signIn: async (page, user, callback) => {
    const account = page.locator(`[data-identifier="${user.login}"]`);
    const consent = page.getByRole('button', { name: /^(Continue|Allow)$/ });
    const password = page.locator('input[name="Passwd"]');
    for (let screen = 0; screen < MAX_SCREENS; screen += 1) {
      const shown = await firstOf({
        done: callback,
        account: account.waitFor(),
        consent: consent.waitFor(),
        password: password.waitFor(),
      });
      if (shown === 'done') {
        return;
      }
      if (shown === 'password') {
        throw new Error(
          'Google asked for a password: the seeded session has expired, reseed it (README)',
        );
      }
      await (shown === 'account' ? account : consent).click();
    }
  },
  authorizeError: ({ status, location }) => {
    if (location !== '') {
      const target = new URL(location, 'https://accounts.google.com');
      if (target.pathname.startsWith(ERROR_PATH)) {
        const reason = target.searchParams.get('authError') ?? 'no authError';
        return `Google refused the authorize request (authError ${reason.slice(0, 120)})`;
      }
    }
    return status >= 400
      ? `Google answered the authorize request with ${status}`
      : undefined;
  },
};
