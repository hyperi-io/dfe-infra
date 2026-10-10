// Project:   dfe-infra
// File:      tests/e2e-ui/harness/idp/types.ts
// Purpose:   The contract every per-IdP sign-in module meets
//
// License:   BUSL-1.1
// Copyright: (c) 2026 HYPERI PTY LIMITED
//
// A module drives one IdP's own hosted login page and classifies its answer to
// an authorize request. The engine half of the flow (PKCE, state, nonce, code
// exchange, ID-token checks) is the engine's, never the module's.

import { type Page } from '@playwright/test';

export type IdpKind = 'dex' | 'okta' | 'entra' | 'google';

export const IDP_KINDS: readonly IdpKind[] = ['dex', 'okta', 'entra', 'google'];

export interface IdpUser {
  // The sign-in identifier: a dex or Okta login, an Entra UPN, a Google email.
  readonly login: string;
  // Empty for an IdP signed into from a seeded session instead.
  readonly password: string;
}

// `callback` settles once the engine's callback has answered, so a module can
// tell an optional screen it is waiting on from a login that already finished.
export type SignIn = (
  page: Page,
  user: IdpUser,
  callback: Promise<unknown>,
) => Promise<void>;

export interface AuthorizeResponse {
  readonly status: number;
  // The Location header, empty when there is none.
  readonly location: string;
  readonly body: string;
}

export interface IdpModule {
  readonly kind: IdpKind;
  // False where the harness reuses a seeded browser session (Google).
  readonly needsPassword: boolean;
  readonly signIn: SignIn;
  // Why the IdP refused an authorize request, or undefined when it accepted it.
  readonly authorizeError: (response: AuthorizeResponse) => string | undefined;
}

// Resolves to the key of the first promise that resolves; a later rejection of
// any other is absorbed by Promise.race.
export async function firstOf<K extends string>(
  candidates: Record<K, Promise<unknown>>,
): Promise<K> {
  const entries = Object.entries(candidates) as [K, Promise<unknown>][];
  return Promise.race(entries.map(([key, settled]) => settled.then(() => key)));
}
