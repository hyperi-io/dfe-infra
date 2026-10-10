// Project:   dfe-infra
// File:      tests/e2e-ui/harness/idp/entra.ts
// Purpose:   Sign in at Microsoft Entra ID's hosted login page
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED

import { firstOf, type IdpModule } from './types';

// Entra's scripts drop keystrokes typed before they settle; dfe-ui's sign-in helper calibrated this.
const SETTLE_MS = 6_000;

// Entra answers a refused authorize request 200 with the AADSTS code in the page.
const AADSTS = /AADSTS\d{5,}/;

export const entra: IdpModule = {
  kind: 'entra',
  needsPassword: true,
  signIn: async (page, user, callback) => {
    // login_hint usually skips the username screen; the hidden password input is never the target.
    const username = page.locator('input[name="loginfmt"]:visible');
    const password = page.locator('input[name="passwd"]:visible');
    const first = await firstOf({
      username: username.waitFor(),
      password: password.waitFor(),
    });
    if (first === 'username') {
      await username.fill(user.login);
      await page.locator('#idSIButton9').click();
      await password.waitFor();
    }
    await page.waitForTimeout(SETTLE_MS);
    await password.click();
    await page.keyboard.type(user.password, { delay: 60 });
    await page.keyboard.press('Enter');
    // "Stay signed in?" shows unless the tenant turned it off; either answer completes the login.
    const next = await firstOf({
      staySignedIn: page.locator('#KmsiCheckboxField').waitFor(),
      done: callback,
    });
    if (next === 'staySignedIn') {
      await page.locator('#idSIButton9').click();
    }
  },
  authorizeError: ({ status, body }) => {
    const code = AADSTS.exec(body)?.[0];
    if (code !== undefined) {
      return `Entra refused the authorize request with ${code}`;
    }
    return status >= 400
      ? `Entra answered the authorize request with ${status}`
      : undefined;
  },
};
